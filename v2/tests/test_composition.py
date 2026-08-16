"""Composition root: correct adapter selection and fail-fast validation."""

from __future__ import annotations

import os

import pytest

from footballai_v2 import composition
from footballai_v2.composition import BackendConfigurationError, validate_backend_configuration
from footballai_v2.execution.coordinator import ExecutionSettings
from footballai_v2.execution.queue.local_filesystem import LocalFilesystemQueue
from footballai_v2.storage import LocalAnalysisRunStore


def _local_settings(tmp_path, **overrides):
    return ExecutionSettings(
        run_root=tmp_path / "runs",
        queue_root=tmp_path / "queue",
        **overrides,
    )


def test_local_selection_builds_local_adapters(tmp_path):
    settings = _local_settings(tmp_path)
    assert isinstance(composition.create_analysis_repository(settings), LocalAnalysisRunStore)
    assert isinstance(composition.create_object_storage(settings), LocalAnalysisRunStore)
    assert isinstance(composition.create_job_queue(settings), LocalFilesystemQueue)


def test_validate_rejects_unknown_backend():
    # ExecutionSettings validates on construction, so probe the validator with a
    # lookalike carrying an unsupported backend name.
    class _Selection:
        database_backend = "mysql"
        object_storage_backend = "local"
        queue_backend = "local"

    with pytest.raises(BackendConfigurationError):
        validate_backend_configuration(_Selection())


def test_postgres_selection_requires_url(tmp_path, monkeypatch):
    monkeypatch.delenv("FOOTBALLAI_DATABASE_URL", raising=False)
    with pytest.raises(BackendConfigurationError, match="FOOTBALLAI_DATABASE_URL"):
        _local_settings(tmp_path, database_backend="postgres")


def test_azure_blob_selection_requires_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FOOTBALLAI_BLOB_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("FOOTBALLAI_BLOB_ACCOUNT_URL", raising=False)
    with pytest.raises(BackendConfigurationError, match="FOOTBALLAI_BLOB"):
        _local_settings(tmp_path, object_storage_backend="azure_blob")


def test_service_bus_queue_builds_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FOOTBALLAI_SERVICEBUS_CONNECTION_STRING",
        "Endpoint=sb://example.servicebus.windows.net/;SharedAccessKeyName=k;SharedAccessKey=dg==",
    )
    monkeypatch.setenv("FOOTBALLAI_SERVICEBUS_QUEUE", "footballai-jobs")
    settings = _local_settings(tmp_path, queue_backend="azure_service_bus")
    from footballai_v2.execution.queue.azure_service_bus import AzureServiceBusQueue

    queue = composition.create_job_queue(settings)
    assert isinstance(queue, AzureServiceBusQueue)
    queue.close()


def test_service_bus_managed_identity_builds_without_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("FOOTBALLAI_SERVICEBUS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv(
        "FOOTBALLAI_SERVICEBUS_NAMESPACE", "footballai-stg.servicebus.windows.net"
    )
    monkeypatch.setenv("FOOTBALLAI_SERVICEBUS_QUEUE", "analysis-jobs")
    settings = _local_settings(tmp_path, queue_backend="azure_service_bus")

    queue = composition.create_job_queue(settings)

    assert queue._client.fully_qualified_namespace == "footballai-stg.servicebus.windows.net"
    queue.close()


_DATABASE_URL = os.getenv("FOOTBALLAI_TEST_DATABASE_URL")


@pytest.mark.skipif(not _DATABASE_URL, reason="requires FOOTBALLAI_TEST_DATABASE_URL")
def test_postgres_repository_is_built_and_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOTBALLAI_DATABASE_URL", _DATABASE_URL)
    settings = _local_settings(tmp_path, database_backend="postgres")
    from footballai_v2.storage.postgres import PostgreSQLAnalysisRepository

    repository = composition.create_analysis_repository(settings)
    assert isinstance(repository, PostgreSQLAnalysisRepository)
    assert repository.verify_schema() is True
