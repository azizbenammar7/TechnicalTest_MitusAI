"""Stage executor that turns one claimed job into an immutable attempt.

The executor depends only on two provider-neutral ports: an
:class:`AnalysisRepository` for the control plane (lifecycle, stages, artifact
metadata, cancellation intent) and an :class:`ObjectStorage` for the data plane
(input materialization and artifact bytes). It never knows whether those resolve
to the local filesystem adapter or to PostgreSQL + Blob, and it never touches a
filesystem run directory, a cancellation marker, or a storage SDK directly.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from typing import Callable

from footballai_v2.contracts.v1 import (
    AnalysisRun, AnalysisRunStatus, ArtifactCategory, StageStatus, StructuredError, utc_now,
)
from footballai_v2.execution.adapters import DemoPipeline, V1CompatPipeline
from footballai_v2.execution.contracts import ExecutionJob
from footballai_v2.execution.errors import CancellationObserved, ExecutionFailure
from footballai_v2.storage.ports import AnalysisRepository, ObjectStorage


logger = logging.getLogger("footballai_v2.worker")


class AnalysisExecutor:
    def __init__(
        self,
        repository: AnalysisRepository,
        object_storage: ObjectStorage,
        *,
        stage_delay_seconds: float = .05,
    ) -> None:
        self.repository = repository
        self.object_storage = object_storage
        self.stage_delay_seconds = max(0, stage_delay_seconds)

    def execute(self, job: ExecutionJob, worker_id: str) -> AnalysisRunStatus:
        run = self.repository.load(job.run_id)
        # Idempotency: Service Bus is at-least-once, so a duplicate delivery of an
        # already-finished attempt must not run again. The repository -- never the
        # queue -- is the authority on whether this run should execute.
        if run.status.is_terminal:
            return run.status
        if run.status is AnalysisRunStatus.QUEUED:
            # A cancellation requested while still queued means the worker must not
            # start inference at all; it records the terminal cancelled state and
            # lets the queue drain the delivery.
            if self.repository.cancellation_requested(run.run_id):
                self.repository.save(run.cancel(reason="Cancelled before execution."))
                return AnalysisRunStatus.CANCELLED
            run = run.start(stages=run.stages); self.repository.save(run)
        artifacts = []
        active_index = -1
        adapter = V1CompatPipeline() if job.pipeline_profile == "v1_compat" else DemoPipeline()
        duration = float(run.parameters.get("video_probe", {}).get("duration_seconds", 0)) if isinstance(run.parameters.get("video_probe"), dict) else 0
        payloads = None
        started = time.perf_counter()
        try:
            for index, stage in enumerate(run.stages):
                active_index = index
                if stage.status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}:
                    continue
                if job.pipeline_profile == "v1_compat" and not stage.required:
                    skipped = replace(stage, status=StageStatus.SKIPPED, finished_at=utc_now(), message="Unsupported by the V1-compatible algorithm family")
                    run = run.with_stages((*run.stages[:index], skipped, *run.stages[index + 1:])); self.repository.save(run)
                    continue
                self._checkpoint(job.run_id)
                if job.pipeline_profile == "test_fail" and job.attempt_number == 1 and index == 2:
                    raise ExecutionFailure("test_stage_failure", "The test profile stopped at detection.")
                running = replace(stage, status=StageStatus.RUNNING, progress_percent=10, started_at=stage.started_at or utc_now(), finished_at=None, error=None, message=f"{stage.stage_name.value.replace('_', ' ').title()} in progress")
                run = run.with_stages((*run.stages[:index], running, *run.stages[index + 1:])); self.repository.save(run)
                if job.pipeline_profile == "v1_compat" and stage.stage_name.value == "detection":
                    payloads = self._build_artifacts(adapter, run, duration)
                if self.stage_delay_seconds:
                    time.sleep(self.stage_delay_seconds)
                self._checkpoint(job.run_id)
                frame_stage = stage.stage_name.value in {"detection", "tracking"}
                finished = replace(running, status=StageStatus.SUCCEEDED, progress_percent=100, finished_at=utc_now(), performance_metrics={"job_id": job.job_id, "run_id": job.run_id, "logical_analysis_id": job.logical_analysis_id, "attempt_number": job.attempt_number, "worker_id": worker_id, "stage": stage.stage_name.value, "status": "succeeded", "duration_seconds": round(self.stage_delay_seconds, 3), "frames_processed": 1 if frame_stage else 0, "processing_fps": round(1 / self.stage_delay_seconds, 2) if frame_stage and self.stage_delay_seconds else 0, "input_count": 1, "output_count": 1, "error_code": None}, message=f"{stage.stage_name.value.replace('_', ' ').title()} completed")
                run = run.with_stages((*run.stages[:index], finished, *run.stages[index + 1:])); self.repository.save(run)
            if payloads is None:
                payloads = self._build_artifacts(adapter, run, duration)
            category_by_id = {"team-summary": ArtifactCategory.SUMMARY, "track-summary": ArtifactCategory.TRACKS, "track-detail": ArtifactCategory.TRACKS, "workload-advisory": ArtifactCategory.WORKLOAD_ADVISORY, "analysis-diagnostics": ArtifactCategory.OTHER}
            schema_by_id = {key: value["schema"] for key, value in payloads.items()}
            for artifact_id, payload in payloads.items():
                artifacts.append(self.object_storage.write_artifact(run.run_id, artifact_id=artifact_id, name=artifact_id.replace("-", " ").title(), category=category_by_id[artifact_id], relative_path=f"artifacts/{artifact_id}.json", content=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(), media_type="application/json", schema_version=schema_by_id[artifact_id]))
            # Compatibility aliases let the preserved dashboard adapter read the stable generated schema.
            summary_alias = self.object_storage.write_artifact(run.run_id, artifact_id="legacy-player-summary", name="Dashboard track summary", category=ArtifactCategory.SUMMARY, relative_path="artifacts/dashboard-track-summary.json", content=(json.dumps(payloads["track-summary"], sort_keys=True) + "\n").encode(), media_type="application/json", schema_version="footballai.track-summary/v1")
            advisory_alias_payload = payloads["workload-advisory"]["tracks"]
            advisory_alias = self.object_storage.write_artifact(run.run_id, artifact_id="dashboard-workload-advisory", name="Dashboard workload advisory", category=ArtifactCategory.WORKLOAD_ADVISORY, relative_path="artifacts/dashboard-workload-advisory.json", content=(json.dumps(advisory_alias_payload, sort_keys=True) + "\n").encode(), media_type="application/json", schema_version="footballai.workload-advisory/v1")
            # Existing adapter expects the stable public ID workload-advisory, so it reads the full envelope below via generic support.
            artifacts.extend((summary_alias, advisory_alias))
            # Provider-neutral integrity gate: a terminal succeeded/partial state is
            # only claimed once every published artifact reads back byte-for-byte
            # from object storage. Verified through the port, never off a local disk.
            for reference in artifacts:
                if not self.object_storage.artifact_reference_integrity(run.run_id, reference):
                    raise ExecutionFailure("artifact_integrity_failed", "A generated artifact failed integrity verification.")
            current = self.repository.load(job.run_id)
            if current.parameters.get("force_partial") is True:
                stages = list(current.stages)
                stages[-1] = replace(stages[-1], status=StageStatus.PARTIAL, progress_percent=80, message="Useful artifacts published before a controlled partial stop")
                run = current.complete_partial(artifacts, "Controlled partial completion with useful artifacts.", stages=stages)
            else:
                run = current.succeed(artifacts, stages=current.stages)
            self.repository.save(run)
            logger.info("job_complete job_id=%s run_id=%s logical_analysis_id=%s attempt_number=%s worker_id=%s status=succeeded duration_seconds=%.3f", job.job_id, job.run_id, job.logical_analysis_id, job.attempt_number, worker_id, time.perf_counter() - started)
            return run.status
        except CancellationObserved:
            current = self.repository.load(job.run_id)
            stages = list(current.stages)
            if active_index >= 0 and stages[active_index].status is StageStatus.RUNNING:
                stages[active_index] = replace(stages[active_index], status=StageStatus.CANCELLED, finished_at=utc_now(), message="Cancelled at a safe checkpoint")
            self.repository.save(current.cancel(reason="Cancellation requested by user.", stages=stages))
            return AnalysisRunStatus.CANCELLED
        except Exception as exc:
            current = self.repository.load(job.run_id)
            if current.status.is_terminal:
                return current.status
            code = exc.code if isinstance(exc, ExecutionFailure) else "execution_failed"
            message = exc.safe_message if isinstance(exc, ExecutionFailure) else "Analysis execution failed safely."
            stages = list(current.stages)
            if active_index >= 0:
                stage = stages[active_index]
                error = StructuredError(code, message, True, utc_now())
                if stage.status is StageStatus.RUNNING:
                    stages[active_index] = replace(stage, status=StageStatus.FAILED, finished_at=utc_now(), error=error, message=message)
            failure = StructuredError(code, message, True, utc_now(), {"stage": stages[active_index].stage_name.value if active_index >= 0 else "ingestion"})
            self.repository.save(current.fail(failure, stages=stages))
            logger.error("job_failed job_id=%s run_id=%s logical_analysis_id=%s attempt_number=%s worker_id=%s stage=%s status=failed duration_seconds=%.3f frames_processed=0 processing_fps=0 input_count=1 output_count=0 error_code=%s", job.job_id, job.run_id, job.logical_analysis_id, job.attempt_number, worker_id, stages[active_index].stage_name.value if active_index >= 0 else "ingestion", time.perf_counter() - started, code)
            return AnalysisRunStatus.FAILED

    def _build_artifacts(self, adapter, run: AnalysisRun, duration: float) -> dict[str, dict]:
        """Materialize the source input through object storage for the pipeline.

        The context manager downloads to a bounded worker-local workspace (a no-op
        for the local adapter) and removes it once the pipeline returns, whatever
        the outcome. The pipeline only ever sees an ephemeral local path.
        """
        cancellation: Callable[[], bool] = lambda: self.repository.cancellation_requested(run.run_id)
        with self.object_storage.materialize_input(run.run_id) as input_path:
            return adapter.build_artifacts(run, duration, input_path, cancellation)

    def _checkpoint(self, run_id: str) -> None:
        if self.repository.cancellation_requested(run_id):
            raise CancellationObserved()
