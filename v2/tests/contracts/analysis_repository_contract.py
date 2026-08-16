"""Behavioural contract every :class:`AnalysisRepository` adapter must satisfy.

These tests exercise only *control-plane* semantics that do not depend on
artifact bytes, so the identical suite runs against the local manifest store and
the PostgreSQL repository. Byte-dependent behaviour (artifact integrity) belongs
to the object-storage contract instead.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from footballai_v2.contracts.v1 import (
    AnalysisRun,
    AnalysisRunStatus,
    CodeReference,
    DataOrigin,
    InputReference,
    StageExecution,
    StageName,
    StageStatus,
    StructuredError,
    utc_now,
)
from footballai_v2.storage.local_analysis_runs import (
    RunAlreadyExistsError,
    RunNotFoundError,
)

CREATED = datetime(2026, 8, 16, 10, tzinfo=timezone.utc)


def _queued_stages(attempt: int = 1) -> tuple[StageExecution, ...]:
    return tuple(
        StageExecution(name.value, name, True, StageStatus.QUEUED, 0, attempt)
        for name in StageName
    )


def make_run(
    *,
    run_id: str | None = None,
    logical_id: str | None = None,
) -> AnalysisRun:
    """Build a fresh queued first-attempt run with queued stages."""
    return AnalysisRun.new(
        logical_analysis_id=logical_id or str(uuid.uuid4()),
        run_id=run_id or str(uuid.uuid4()),
        data_origin=DataOrigin.SYNTHETIC,
        input=InputReference("run-input://source.mp4", "a" * 64, "video/mp4"),
        code=CodeReference("https://github.com/example/FootballAi", "8" * 40),
        pipeline_version="demo_fast/1.0.0",
        parameters={"pipeline_profile": "demo_fast"},
        stages=_queued_stages(),
        created_at=CREATED,
    )


class AnalysisRepositoryContract:
    """Mixin of adapter-agnostic lifecycle tests.

    Subclasses must provide a pytest ``repository`` fixture yielding an empty
    repository implementing the ``AnalysisRepository`` port.
    """

    def test_create_and_load_roundtrip(self, repository):
        run = make_run()
        repository.create(run)
        loaded = repository.load(run.run_id)
        assert loaded.run_id == run.run_id
        assert loaded.logical_analysis_id == run.logical_analysis_id
        assert loaded.status is AnalysisRunStatus.QUEUED
        assert loaded.attempt_number == 1
        assert loaded.previous_attempt_run_id is None

    def test_load_missing_run_raises(self, repository):
        with pytest.raises(RunNotFoundError):
            repository.load(str(uuid.uuid4()))

    def test_duplicate_create_rejected(self, repository):
        run = make_run()
        repository.create(run)
        with pytest.raises(RunAlreadyExistsError):
            repository.create(run)

    def test_running_then_failed_transitions(self, repository):
        run = make_run()
        repository.create(run)
        running = run.start(stages=run.stages)
        repository.save(running)
        assert repository.load(run.run_id).status is AnalysisRunStatus.RUNNING
        failed = running.fail(
            StructuredError("execution_failed", "Failed safely.", True, utc_now()),
            stages=running.stages,
        )
        repository.save(failed)
        assert repository.load(run.run_id).status is AnalysisRunStatus.FAILED

    def test_terminal_attempt_is_immutable(self, repository):
        run = make_run()
        repository.create(run)
        running = run.start(stages=run.stages)
        repository.save(running)
        failed = running.fail(
            StructuredError("execution_failed", "Failed safely.", True, utc_now()),
            stages=running.stages,
        )
        repository.save(failed)
        # Any further write to a terminal attempt must be rejected.
        with pytest.raises(RuntimeError):
            repository.save(running)

    def test_provenance_change_rejected(self, repository):
        run = make_run()
        repository.create(run)
        tampered = replace(
            run,
            status=AnalysisRunStatus.RUNNING,
            started_at=CREATED,
            pipeline_version="demo_fast/9.9.9",
        )
        with pytest.raises(RuntimeError):
            repository.save(tampered)

    def test_retry_chain_preserved(self, repository):
        first = make_run()
        repository.create(first)
        running = first.start(stages=first.stages)
        repository.save(running)
        failed = running.fail(
            StructuredError("execution_failed", "Failed safely.", True, utc_now()),
            stages=running.stages,
        )
        repository.save(failed)
        retry = AnalysisRun.retry_from(failed).with_stages(_queued_stages(2))
        repository.create(retry)
        loaded = repository.load(retry.run_id)
        assert loaded.attempt_number == 2
        assert loaded.previous_attempt_run_id == first.run_id
        assert loaded.logical_analysis_id == first.logical_analysis_id
        assert loaded.run_id != first.run_id

    def test_cancellation_is_terminal_and_immutable(self, repository):
        run = make_run()
        repository.create(run)
        running = run.start(stages=run.stages)
        repository.save(running)
        cancelled = running.cancel(reason="User cancelled.", stages=running.stages)
        repository.save(cancelled)
        assert repository.load(run.run_id).status is AnalysisRunStatus.CANCELLED
        with pytest.raises(RuntimeError):
            repository.save(running)

    def test_list_runs_returns_created(self, repository):
        first = make_run()
        second = make_run()
        repository.create(first)
        repository.create(second)
        ids = {run.run_id for run in repository.list_runs()}
        assert {first.run_id, second.run_id} <= ids
