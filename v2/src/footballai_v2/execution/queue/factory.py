"""Configuration boundary for provider-neutral analysis queue adapters."""

from __future__ import annotations

from pathlib import Path

from footballai_v2.execution.queue.base import JobQueue
from footballai_v2.execution.queue.local_filesystem import LocalFilesystemQueue


def create_job_queue(backend: str, root: str | Path) -> JobQueue:
    if backend == "local":
        return LocalFilesystemQueue(root)
    raise ValueError(
        f"Unsupported queue backend {backend!r}; only the local adapter is implemented"
    )
