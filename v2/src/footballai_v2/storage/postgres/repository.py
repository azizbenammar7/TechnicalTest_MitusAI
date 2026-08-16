"""PostgreSQL adapter for the provider-neutral :class:`AnalysisRepository` port.

This is a *control-plane* store: it owns the immutable analysis-attempt
lifecycle, the retry chain, stage state, artifact *metadata*, and an append-only
audit log. It never stores artifact or video bytes -- those belong to the
object-storage data plane.

Concurrency is enforced with pessimistic row locks (``SELECT ... FOR UPDATE``)
around every read-modify-write, so two writers (for example an API cancel and a
worker completion) are serialized and the second one correctly observes a
terminal state and is rejected. A monotonic ``version`` column additionally
supports optimistic rejection when a caller passes ``expected_version``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from footballai_v2.contracts.v1 import (
    AnalysisRun,
    AnalysisRunStatus,
    CodeReference,
    JsonValue,
    ModelReference,
)
from footballai_v2.storage.lifecycle import (
    ManifestTransitionError,
    ensure_creatable,
    ensure_transition_allowed,
)
from footballai_v2.storage.local_analysis_runs import (
    RunAlreadyExistsError,
    RunNotFoundError,
)
from footballai_v2.storage.postgres import schema


class ConcurrencyConflictError(RuntimeError):
    """Raised when an optimistic ``expected_version`` no longer matches storage."""


class SchemaOutOfDateError(RuntimeError):
    """Raised when the database schema does not match the expected revision."""


class PostgreSQLAnalysisRepository:
    """Transactional control-plane repository backed by PostgreSQL."""

    def __init__(self, engine: Engine, *, verify_schema: bool = True) -> None:
        self._engine = engine
        if verify_schema:
            self.verify_schema()

    # -- lifecycle -----------------------------------------------------------

    def create(self, run: AnalysisRun) -> str:
        """Persist a new queued attempt, its stages, and an audit event atomically."""
        ensure_creatable(run)
        with self._engine.begin() as connection:
            # Reuse the logical grouping across a retry chain; create it once.
            connection.execute(
                pg_insert(schema.logical_analyses)
                .values(
                    logical_analysis_id=run.logical_analysis_id,
                    data_origin=run.data_origin.value,
                    created_at=run.created_at,
                )
                .on_conflict_do_nothing(index_elements=["logical_analysis_id"])
            )
            existing = connection.execute(
                select(schema.analysis_attempts.c.run_id).where(
                    schema.analysis_attempts.c.run_id == run.run_id
                )
            ).first()
            if existing is not None:
                raise RunAlreadyExistsError(run.run_id)
            connection.execute(
                schema.analysis_attempts.insert().values(
                    **self._attempt_row(run, version=1)
                )
            )
            self._replace_projections(connection, run)
            self._append_audit(
                connection,
                run,
                event_type="attempt_created",
                detail={
                    "attempt_number": run.attempt_number,
                    "previous_attempt_run_id": run.previous_attempt_run_id,
                    "status": run.status.value,
                },
            )
        return run.run_id

    def load(self, run_id: str) -> AnalysisRun:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(schema.analysis_attempts.c.manifest).where(
                    schema.analysis_attempts.c.run_id == run_id
                )
            ).first()
        if row is None:
            raise RunNotFoundError(run_id)
        return AnalysisRun.from_dict(row[0])

    def save(self, run: AnalysisRun, *, expected_version: int | None = None) -> None:
        """Replace a non-terminal attempt after locking and validating the transition."""
        with self._engine.begin() as connection:
            current_row = connection.execute(
                select(
                    schema.analysis_attempts.c.manifest,
                    schema.analysis_attempts.c.version,
                )
                .where(schema.analysis_attempts.c.run_id == run.run_id)
                .with_for_update()
            ).first()
            if current_row is None:
                raise RunNotFoundError(run.run_id)
            current = AnalysisRun.from_dict(current_row[0])
            stored_version = current_row[1]
            if expected_version is not None and expected_version != stored_version:
                raise ConcurrencyConflictError(
                    f"expected version {expected_version}, found {stored_version}"
                )
            ensure_transition_allowed(current, run)
            connection.execute(
                schema.analysis_attempts.update()
                .where(schema.analysis_attempts.c.run_id == run.run_id)
                .values(**self._attempt_row(run, version=stored_version + 1))
            )
            self._replace_projections(connection, run)
            self._append_audit(
                connection,
                run,
                event_type="attempt_updated",
                detail={"status": run.status.value, "version": stored_version + 1},
            )

    def list_runs(self) -> tuple[AnalysisRun, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(schema.analysis_attempts.c.manifest).order_by(
                    schema.analysis_attempts.c.created_at.desc()
                )
            ).all()
        return tuple(AnalysisRun.from_dict(row[0]) for row in rows)

    def create_retry_attempt(
        self,
        previous_run_id: str,
        *,
        code: CodeReference | None = None,
        pipeline_version: str | None = None,
        parameters: Mapping[str, JsonValue] | None = None,
        models: Sequence[ModelReference] | None = None,
        run_id: str | None = None,
    ) -> AnalysisRun:
        """Transactionally create a new attempt linked to a failed/partial one.

        BEGIN; lock+read the previous attempt; verify it is failed or partial;
        mint a new run id; increment ``attempt_number``; preserve
        ``logical_analysis_id`` and link ``previous_attempt_run_id``; write an
        audit event; COMMIT.
        """
        with self._engine.begin() as connection:
            previous_row = connection.execute(
                select(schema.analysis_attempts.c.manifest)
                .where(schema.analysis_attempts.c.run_id == previous_run_id)
                .with_for_update()
            ).first()
            if previous_row is None:
                raise RunNotFoundError(previous_run_id)
            previous = AnalysisRun.from_dict(previous_row[0])
            # AnalysisRun.retry_from raises InvalidStatusTransition unless the
            # previous attempt is FAILED or PARTIAL.
            retry = AnalysisRun.retry_from(
                previous,
                code=code,
                pipeline_version=pipeline_version,
                parameters=parameters,
                models=models,
                run_id=run_id,
            )
            connection.execute(
                schema.analysis_attempts.insert().values(
                    **self._attempt_row(retry, version=1)
                )
            )
            self._replace_projections(connection, retry)
            self._append_audit(
                connection,
                retry,
                event_type="attempt_retried",
                detail={
                    "attempt_number": retry.attempt_number,
                    "previous_attempt_run_id": retry.previous_attempt_run_id,
                },
            )
        return retry

    # -- introspection helpers ----------------------------------------------

    def version_of(self, run_id: str) -> int:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(schema.analysis_attempts.c.version).where(
                    schema.analysis_attempts.c.run_id == run_id
                )
            ).first()
        if row is None:
            raise RunNotFoundError(run_id)
        return int(row[0])

    def audit_trail(self, logical_analysis_id: str) -> tuple[dict[str, Any], ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    schema.audit_events.c.event_type,
                    schema.audit_events.c.run_id,
                    schema.audit_events.c.attempt_number,
                    schema.audit_events.c.detail,
                    schema.audit_events.c.created_at,
                )
                .where(schema.audit_events.c.logical_analysis_id == logical_analysis_id)
                .order_by(schema.audit_events.c.id.asc())
            ).all()
        return tuple(
            {
                "event_type": row[0],
                "run_id": row[1],
                "attempt_number": row[2],
                "detail": row[3],
                "created_at": row[4],
            }
            for row in rows
        )

    def verify_schema(self) -> bool:
        """Return True when the applied Alembic revision matches the expected one.

        Raises :class:`SchemaOutOfDateError` if the database has not been
        migrated to the revision this code was built against.
        """
        with self._engine.connect() as connection:
            if not connection.dialect.has_table(connection, "alembic_version"):
                raise SchemaOutOfDateError(
                    "database is not migrated; run the P2 migration command"
                )
            row = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).first()
        current = row[0] if row else None
        if current != schema.SCHEMA_REVISION:
            raise SchemaOutOfDateError(
                f"database schema {current!r} does not match expected "
                f"{schema.SCHEMA_REVISION!r}; run the P2 migration command"
            )
        return True

    # -- row mapping ---------------------------------------------------------

    @staticmethod
    def _attempt_row(run: AnalysisRun, *, version: int) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "logical_analysis_id": run.logical_analysis_id,
            "attempt_number": run.attempt_number,
            "previous_attempt_run_id": run.previous_attempt_run_id,
            "status": run.status.value,
            "data_origin": run.data_origin.value,
            "pipeline_version": run.pipeline_version,
            "contract_version": run.contract_version,
            "manifest": run.to_dict(),
            "version": version,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
        }

    @staticmethod
    def _replace_projections(connection, run: AnalysisRun) -> None:
        connection.execute(
            delete(schema.stage_executions).where(
                schema.stage_executions.c.run_id == run.run_id
            )
        )
        if run.stages:
            connection.execute(
                schema.stage_executions.insert(),
                [
                    {
                        "run_id": run.run_id,
                        "stage_id": stage.stage_id,
                        "stage_name": stage.stage_name.value,
                        "required": stage.required,
                        "status": stage.status.value,
                        "progress_percent": float(stage.progress_percent),
                        "attempt_number": stage.attempt_number,
                    }
                    for stage in run.stages
                ],
            )
        connection.execute(
            delete(schema.artifact_metadata).where(
                schema.artifact_metadata.c.run_id == run.run_id
            )
        )
        if run.artifacts:
            connection.execute(
                schema.artifact_metadata.insert(),
                [
                    {
                        "run_id": run.run_id,
                        "artifact_id": artifact.artifact_id,
                        "name": artifact.name,
                        "category": artifact.category.value,
                        "relative_path": artifact.relative_path,
                        "media_type": artifact.media_type,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "schema_version": artifact.schema_version,
                    }
                    for artifact in run.artifacts
                ],
            )

    @staticmethod
    def _append_audit(
        connection,
        run: AnalysisRun,
        *,
        event_type: str,
        detail: Mapping[str, JsonValue],
    ) -> None:
        from footballai_v2.contracts.v1 import utc_now

        connection.execute(
            schema.audit_events.insert().values(
                logical_analysis_id=run.logical_analysis_id,
                run_id=run.run_id,
                attempt_number=run.attempt_number,
                event_type=event_type,
                detail=dict(detail),
                created_at=utc_now(),
            )
        )
