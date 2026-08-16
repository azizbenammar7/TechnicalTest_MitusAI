"""Run the JobQueue contract against the local and Azure Service Bus adapters."""

from __future__ import annotations

import pytest

from footballai_v2.execution.queue.azure_service_bus import AzureServiceBusQueue
from footballai_v2.execution.queue.local_filesystem import LocalFilesystemQueue
from contracts.job_queue_contract import JobQueueContract
from fakes.service_bus import FakeServiceBusClient


class TestLocalFilesystemQueueContract(JobQueueContract):
    @pytest.fixture
    def queue(self, tmp_path):
        return LocalFilesystemQueue(tmp_path / "queue")


class TestAzureServiceBusQueueContract(JobQueueContract):
    @pytest.fixture
    def queue(self):
        # No repository: the shared contract does not exercise idempotency, so
        # every claimed message is treated as executable.
        return AzureServiceBusQueue(
            FakeServiceBusClient(), "footballai-jobs", receive_wait_seconds=0.0
        )
