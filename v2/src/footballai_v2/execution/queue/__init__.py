"""Execution queue adapters."""

from footballai_v2.execution.queue.base import JobQueue
from footballai_v2.execution.queue.factory import create_job_queue
from footballai_v2.execution.queue.local_filesystem import (
    DuplicateJobError,
    LocalFilesystemQueue,
    QueueRecordError,
)

__all__ = [
    "DuplicateJobError",
    "JobQueue",
    "LocalFilesystemQueue",
    "QueueRecordError",
    "create_job_queue",
]
