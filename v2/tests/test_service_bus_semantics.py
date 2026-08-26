"""At-least-once semantics of the Azure Service Bus adapter (fake broker).

These validate the adapter's decision logic -- idempotent draining, dead-letter
policy, redelivery after crash, and cancellation delegation -- deterministically
without real Azure. Real Service Bus validation is pending.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from footballai_v2.contracts.v1 import AnalysisRunStatus
from footballai_v2.execution.contracts import ExecutionJob
from footballai_v2.execution.queue import azure_service_bus
from footballai_v2.execution.queue.azure_service_bus import AzureServiceBusQueue
from footballai_v2.storage.local_analysis_runs import RunNotFoundError
from fakes.service_bus import FakeServiceBusBroker, FakeServiceBusClient, _StoredMessage


class FakeRepository:
    def __init__(self) -> None:
        self._runs: dict[str, AnalysisRunStatus] = {}

    def set_status(self, run_id: str, status: AnalysisRunStatus) -> None:
        self._runs[run_id] = status

    def load(self, run_id: str):
        if run_id not in self._runs:
            raise RunNotFoundError(run_id)
        return SimpleNamespace(status=self._runs[run_id])


def make_job() -> ExecutionJob:
    return ExecutionJob.new(str(uuid.uuid4()), str(uuid.uuid4()), 1, "demo_fast")


def _queue(broker, repository=None, **kwargs):
    return AzureServiceBusQueue(
        FakeServiceBusClient(broker),
        "footballai-jobs",
        repository=repository,
        receive_wait_seconds=0.0,
        lock_renew_interval_seconds=0.01,
        **kwargs,
    )


def test_duplicate_delivery_for_terminal_run_is_drained():
    broker = FakeServiceBusBroker()
    repo = FakeRepository()
    queue = _queue(broker, repo)
    job = make_job()
    repo.set_status(job.run_id, AnalysisRunStatus.SUCCEEDED)  # already done
    queue.enqueue(job)

    assert queue.claim("worker-1") is None  # drained, not re-executed
    assert broker.active_count() == 0
    assert broker.dead_letter == []


def test_unknown_run_is_dead_lettered():
    broker = FakeServiceBusBroker()
    queue = _queue(broker, FakeRepository())  # empty repository
    queue.enqueue(make_job())

    assert queue.claim("worker-1") is None
    assert len(broker.dead_letter) == 1


def test_executable_run_is_claimed_and_completed():
    broker = FakeServiceBusBroker()
    repo = FakeRepository()
    queue = _queue(broker, repo)
    job = make_job()
    repo.set_status(job.run_id, AnalysisRunStatus.QUEUED)
    queue.enqueue(job)

    claimed = queue.claim("worker-1")
    assert claimed is not None and claimed.run_id == job.run_id
    queue.complete(claimed)
    assert broker.active_count() == 0


def test_w3c_trace_context_uses_message_properties_without_changing_body(monkeypatch):
    broker = FakeServiceBusBroker()
    repo = FakeRepository()
    job = make_job()
    repo.set_status(job.run_id, AnalysisRunStatus.QUEUED)
    detached = []
    monkeypatch.setattr(
        azure_service_bus,
        "inject_trace_context",
        lambda carrier: carrier.update({"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"}),
    )
    monkeypatch.setattr(azure_service_bus, "attach_trace_context", lambda carrier: carrier["traceparent"])
    monkeypatch.setattr(azure_service_bus, "detach_trace_context", detached.append)
    queue = _queue(broker, repo)

    queue.enqueue(job)
    assert broker.messages[0].application_properties["traceparent"].startswith("00-")
    assert ExecutionJob.from_dict(json.loads(broker.messages[0].body)) == job
    claimed = queue.claim("worker-1")
    assert claimed is not None
    queue.complete(claimed)
    assert detached == [broker.messages[0].application_properties["traceparent"]]


def test_over_delivered_message_is_dead_lettered():
    broker = FakeServiceBusBroker()
    repo = FakeRepository()
    job = make_job()
    repo.set_status(job.run_id, AnalysisRunStatus.QUEUED)
    queue = _queue(broker, repo, max_delivery=0)  # first delivery already exceeds
    queue.enqueue(job)

    assert queue.claim("worker-1") is None
    assert len(broker.dead_letter) == 1


def test_unparseable_message_is_dead_lettered():
    broker = FakeServiceBusBroker()
    broker.enqueue(_StoredMessage(body=b"not-json", application_properties={}, message_id="x"))
    queue = _queue(broker, FakeRepository())

    assert queue.claim("worker-1") is None
    assert len(broker.dead_letter) == 1


def test_redelivery_after_worker_crash():
    broker = FakeServiceBusBroker()
    repo = FakeRepository()
    job = make_job()
    repo.set_status(job.run_id, AnalysisRunStatus.QUEUED)

    first_worker = _queue(broker, repo)
    first_worker.enqueue(job)
    claimed = first_worker.claim("worker-1")
    assert claimed is not None
    # Worker crashes mid-execution: it never completes. The lock expires.
    broker.expire_locks()

    second_worker = _queue(broker, repo)
    redelivered = second_worker.claim("worker-2")
    assert redelivered is not None and redelivered.run_id == job.run_id


def test_cancel_is_delegated_to_control_plane():
    broker = FakeServiceBusBroker()
    repo = FakeRepository()
    queue = _queue(broker, repo)
    job = make_job()
    repo.set_status(job.run_id, AnalysisRunStatus.QUEUED)
    queue.enqueue(job)

    # The queue does not remove the message; cancellation is control-plane driven.
    assert queue.cancel(job.run_id) is False
    # Once the control plane marks the run cancelled (terminal), the message is
    # drained on the next claim instead of being executed.
    repo.set_status(job.run_id, AnalysisRunStatus.CANCELLED)
    assert queue.claim("worker-1") is None
    assert broker.active_count() == 0


def test_recover_abandoned_is_broker_managed():
    broker = FakeServiceBusBroker()
    queue = _queue(broker, FakeRepository())
    assert queue.recover_abandoned(300, FakeRepository()) == 0
