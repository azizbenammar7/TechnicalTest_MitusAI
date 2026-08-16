"""Behavioural contract every JobQueue adapter must satisfy.

Covers only the shared port guarantees -- enqueue/claim/complete/fail lifecycle
and exclusive single delivery -- so it runs against the local filesystem queue
and the Azure Service Bus adapter (driven by a fake broker). Broker-specific
behaviour (idempotent draining, dead-lettering, redelivery) is tested separately.
"""

from __future__ import annotations

import uuid

from footballai_v2.execution.contracts import ExecutionJob


def make_job() -> ExecutionJob:
    return ExecutionJob.new(str(uuid.uuid4()), str(uuid.uuid4()), 1, "demo_fast")


class JobQueueContract:
    """Subclasses must provide a ``queue`` fixture yielding an empty queue."""

    def test_claim_empty_returns_none(self, queue):
        assert queue.claim("worker-1") is None

    def test_enqueue_then_claim(self, queue):
        job = make_job()
        queue.enqueue(job)
        claimed = queue.claim("worker-1")
        assert claimed is not None
        assert claimed.run_id == job.run_id
        assert claimed.worker_id == "worker-1"

    def test_claim_is_exclusive(self, queue):
        job = make_job()
        queue.enqueue(job)
        first = queue.claim("worker-1")
        second = queue.claim("worker-2")
        assert first is not None
        assert second is None

    def test_complete_settles_job(self, queue):
        job = make_job()
        queue.enqueue(job)
        claimed = queue.claim("worker-1")
        queue.complete(claimed)
        assert queue.claim("worker-1") is None

    def test_fail_settles_job(self, queue):
        job = make_job()
        queue.enqueue(job)
        claimed = queue.claim("worker-1")
        queue.fail(claimed)
        assert queue.claim("worker-1") is None
