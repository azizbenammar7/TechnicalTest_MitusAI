"""Environment-configured local queue worker entrypoint."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time

from footballai_v2 import composition
from footballai_v2.execution.coordinator import ExecutionSettings
from footballai_v2.execution.executor import AnalysisExecutor
from footballai_v2.logging_config import bind_log_context, log_event
from footballai_v2.observability import add_metric, configure_observability


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "1" if default else "0").strip().lower()
    if value not in {"0", "1", "false", "true"}:
        raise ValueError(f"{name} must be 0, 1, false, or true")
    return value in {"1", "true"}


def main() -> None:
    configure_observability("footballai-worker")
    settings = ExecutionSettings.from_environment()
    # The worker is built from configured provider-neutral planes; it never
    # assumes a shared filesystem with the API. Local, split (PostgreSQL + Blob +
    # local queue), and full Azure all resolve through the same composition root.
    repository = composition.create_analysis_repository(settings)
    object_storage = composition.create_object_storage(settings)
    queue = composition.create_job_queue(settings, repository=repository)
    configured_worker_id = os.getenv("FOOTBALLAI_WORKER_ID", "").strip()
    worker_id = (configured_worker_id or f"{socket.gethostname()}-{os.getpid()}")[:128]
    poll = _bounded_float("FOOTBALLAI_WORKER_POLL_SECONDS", .25, .05, 60)
    claim_timeout = _bounded_float("FOOTBALLAI_JOB_CLAIM_TIMEOUT_SECONDS", 300, 1, 86400)
    delay = _bounded_float("FOOTBALLAI_DEMO_STAGE_DELAY_SECONDS", .12, 0, 60)
    run_once = _enabled("FOOTBALLAI_WORKER_ONCE")
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped; stopped = True
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    queue.recover_abandoned(claim_timeout, repository)
    executor = AnalysisExecutor(repository, object_storage, stage_delay_seconds=delay)
    worker_logger = logging.getLogger("footballai_v2.worker")
    log_event(
        worker_logger, logging.INFO, "worker.started", "Worker started",
        worker_id=worker_id, status="ready",
    )
    while not stopped:
        job = queue.claim(worker_id)
        if job is None:
            if run_once:
                break
            time.sleep(poll); continue
        with bind_log_context(
            logical_analysis_id=job.logical_analysis_id, run_id=job.run_id,
            attempt_number=job.attempt_number,
            job_execution_id=os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME"),
            code_revision=os.getenv("FOOTBALLAI_CODE_REVISION"),
        ):
            log_event(worker_logger, logging.INFO, "worker.job_claimed", "Worker claimed analysis job", worker_id=worker_id, job_id=job.job_id, profile=job.pipeline_profile)
            status = executor.execute(job, worker_id)
            if status.value in {"succeeded", "partial"}: queue.complete(job)
            elif status.value == "cancelled": queue.cancel(job.run_id)
            else: queue.fail(job)
            add_metric(
                "worker_job_success" if status.value in {"succeeded", "partial"} else "worker_job_failure",
                service="worker", environment=settings.environment, status=status.value,
            )
        if run_once:
            break
    log_event(worker_logger, logging.INFO, "worker.stopped", "Worker stopped", worker_id=worker_id, status="stopped")


if __name__ == "__main__":
    main()
