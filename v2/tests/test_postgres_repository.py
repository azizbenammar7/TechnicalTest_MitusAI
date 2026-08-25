"""Integration tests for the PostgreSQL control-plane repository.

These run only when ``FOOTBALLAI_TEST_DATABASE_URL`` points at a disposable
PostgreSQL instance (see ``make p2-db-up``). Without it the whole module is
skipped, so the fast deterministic suite never requires a database. The module
also runs the shared :class:`AnalysisRepositoryContract`.
"""

from __future__ import annotations

import os
import threading
from datetime import timedelta

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text  # noqa: E402

from footballai_v2.contracts.v1 import (  # noqa: E402
    AnalysisRun,
    AnalysisRunStatus,
    ArtifactCategory,
    ArtifactReference,
    InvalidStatusTransition,
    StageExecution,
    StageName,
    StageStatus,
    StructuredError,
    utc_now,
)
from footballai_v2.storage.postgres import (  # noqa: E402
    ConcurrencyConflictError,
    PostgreSQLAnalysisRepository,
)
from contracts.analysis_repository_contract import (  # noqa: E402
    CREATED,
    AnalysisRepositoryContract,
    make_run,
)

_DATABASE_URL = os.getenv("FOOTBALLAI_TEST_DATABASE_URL")
_TABLES = (
    "audit_events",
    "artifact_metadata",
    "stage_executions",
    "analysis_attempts",
    "logical_analyses",
)

pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="set FOOTBALLAI_TEST_DATABASE_URL to run PostgreSQL integration tests",
)


@pytest.fixture(scope="session")
def _migrated_url() -> str:
    from alembic import command
    from alembic.config import Config

    migrations_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")
    os.environ["FOOTBALLAI_DATABASE_URL"] = _DATABASE_URL
    cfg = Config()
    cfg.set_main_option("script_location", migrations_dir)
    command.upgrade(cfg, "head")
    return _DATABASE_URL


@pytest.fixture(scope="session")
def _engine(_migrated_url):
    engine = create_engine(_migrated_url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def _clean(_engine):
    with _engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(_TABLES)} CASCADE"))
    return _engine


@pytest.fixture
def repository(_clean):
    return PostgreSQLAnalysisRepository(_clean, verify_schema=True)


def _succeeded_stages() -> tuple[StageExecution, ...]:
    started = CREATED + timedelta(seconds=1)
    finished = CREATED + timedelta(seconds=2)
    return tuple(
        StageExecution(
            name.value,
            name,
            True,
            StageStatus.SUCCEEDED,
            100,
            1,
            started_at=started,
            finished_at=finished,
        )
        for name in StageName
    )


def _artifact() -> ArtifactReference:
    return ArtifactReference(
        artifact_id="team-summary",
        name="Team summary",
        category=ArtifactCategory.SUMMARY,
        relative_path="artifacts/team-summary.json",
        media_type="application/json",
        sha256="b" * 64,
        size_bytes=128,
        schema_version="footballai.team-summary/v1",
    )


class TestPostgresRepositoryContract(AnalysisRepositoryContract):
    """Run the shared control-plane contract against PostgreSQL."""


def test_verify_schema_passes(repository):
    assert repository.verify_schema() is True


def test_optimistic_concurrency_conflict(repository):
    run = make_run()
    repository.create(run)
    assert repository.version_of(run.run_id) == 1
    running = run.start(stages=run.stages)
    repository.save(running)  # version -> 2
    assert repository.version_of(run.run_id) == 2
    # A caller holding the stale version 1 is rejected.
    failed = running.fail(
        StructuredError("execution_failed", "Failed safely.", True, utc_now()),
        stages=running.stages,
    )
    with pytest.raises(ConcurrencyConflictError):
        repository.save(failed, expected_version=1)
    # The current version still succeeds.
    repository.save(failed, expected_version=2)
    assert repository.load(run.run_id).status is AnalysisRunStatus.FAILED


def test_concurrent_terminal_race_serialized(_clean):
    """Two writers racing a running attempt to terminal: exactly one wins."""
    repo_a = PostgreSQLAnalysisRepository(_clean, verify_schema=False)
    repo_b = PostgreSQLAnalysisRepository(_clean, verify_schema=False)
    run = make_run()
    repo_a.create(run)
    running = run.start(stages=run.stages)
    repo_a.save(running)

    failed = running.fail(
        StructuredError("execution_failed", "Worker failed.", True, utc_now()),
        stages=running.stages,
    )
    cancelled = running.cancel(reason="User cancelled.", stages=running.stages)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(repo, target):
        barrier.wait()
        try:
            repo.save(target)
            with lock:
                outcomes.append("ok")
        except Exception:
            with lock:
                outcomes.append("rejected")

    threads = [
        threading.Thread(target=attempt, args=(repo_a, failed)),
        threading.Thread(target=attempt, args=(repo_b, cancelled)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["ok", "rejected"]
    assert repository_status(_clean, run.run_id) in {
        AnalysisRunStatus.FAILED.value,
        AnalysisRunStatus.CANCELLED.value,
    }


def repository_status(engine, run_id: str) -> str:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status FROM analysis_attempts WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).first()
    return row[0]


def test_stage_and_artifact_projection(repository, _clean):
    run = make_run()
    repository.create(run)
    running = run.start(stages=run.stages)
    repository.save(running)
    succeeded = running.succeed([_artifact()], stages=_succeeded_stages())
    repository.save(succeeded)

    with _clean.connect() as connection:
        stage_count = connection.execute(
            text("SELECT count(*) FROM stage_executions WHERE run_id = :r"),
            {"r": run.run_id},
        ).scalar_one()
        artifact_rows = connection.execute(
            text(
                "SELECT artifact_id, sha256, size_bytes FROM artifact_metadata "
                "WHERE run_id = :r"
            ),
            {"r": run.run_id},
        ).all()
    assert stage_count == len(list(StageName))
    assert artifact_rows == [("team-summary", "b" * 64, 128)]


def test_create_retry_attempt_transaction(repository):
    first = make_run()
    repository.create(first)
    running = first.start(stages=first.stages)
    repository.save(running)
    failed = running.fail(
        StructuredError("execution_failed", "Failed safely.", True, utc_now()),
        stages=running.stages,
    )
    repository.save(failed)

    retry = repository.create_retry_attempt(first.run_id)
    assert retry.attempt_number == 2
    assert retry.previous_attempt_run_id == first.run_id
    assert retry.logical_analysis_id == first.logical_analysis_id
    assert repository.load(retry.run_id).status is AnalysisRunStatus.QUEUED


def test_create_retry_attempt_rejects_non_terminal(repository):
    run = make_run()
    repository.create(run)
    with pytest.raises(InvalidStatusTransition):
        repository.create_retry_attempt(run.run_id)


def test_audit_trail_records_lifecycle(repository):
    first = make_run()
    repository.create(first)
    running = first.start(stages=first.stages)
    repository.save(running)
    failed = running.fail(
        StructuredError("execution_failed", "Failed safely.", True, utc_now()),
        stages=running.stages,
    )
    repository.save(failed)
    repository.create_retry_attempt(first.run_id)

    events = [event["event_type"] for event in repository.audit_trail(first.logical_analysis_id)]
    assert events[0] == "attempt_created"
    assert "attempt_updated" in events
    assert events[-1] == "attempt_retried"
