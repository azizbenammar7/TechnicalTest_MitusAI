"""Environment-configured local queue worker entrypoint."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time

from footballai_v2.execution.coordinator import ExecutionSettings
from footballai_v2.execution.executor import AnalysisExecutor
from footballai_v2.execution.queue import create_job_queue
from footballai_v2.logging_config import configure_logging
from footballai_v2.storage import LocalAnalysisRunStore


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def main() -> None:
    configure_logging("footballai-worker")
    settings = ExecutionSettings.from_environment()
    store = LocalAnalysisRunStore(settings.run_root)
    queue = create_job_queue(settings.queue_backend, settings.queue_root)
    configured_worker_id = os.getenv("FOOTBALLAI_WORKER_ID", "").strip()
    worker_id = (configured_worker_id or f"{socket.gethostname()}-{os.getpid()}")[:128]
    poll = _bounded_float("FOOTBALLAI_WORKER_POLL_SECONDS", .25, .05, 60)
    claim_timeout = _bounded_float("FOOTBALLAI_JOB_CLAIM_TIMEOUT_SECONDS", 300, 1, 86400)
    delay = _bounded_float("FOOTBALLAI_DEMO_STAGE_DELAY_SECONDS", .12, 0, 60)
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped; stopped = True
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    queue.recover_abandoned(claim_timeout, store)
    executor = AnalysisExecutor(store, stage_delay_seconds=delay)
    worker_logger = logging.getLogger("footballai_v2.worker")
    worker_logger.info(
        "worker_started worker_id=%s queue_backend=%s object_storage_backend=%s database_backend=%s",
        worker_id,
        settings.queue_backend,
        settings.object_storage_backend,
        settings.database_backend,
    )
    while not stopped:
        job = queue.claim(worker_id)
        if job is None:
            time.sleep(poll); continue
        status = executor.execute(job, worker_id)
        if status.value in {"succeeded", "partial"}: queue.complete(job)
        elif status.value == "cancelled": queue.cancel(job.run_id)
        else: queue.fail(job)
    worker_logger.info("worker_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
