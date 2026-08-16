"""Composition root: select infrastructure adapters from configuration.

This is the only place that turns backend names into concrete adapters. It fails
fast and never silently falls back: selecting ``postgres`` / ``azure_blob`` /
``azure_service_bus`` without the required configuration raises a clear error at
startup. Azure/PostgreSQL SDK imports are deferred into the cloud branches so the
local path never pays for them.

Core domain and application code stays provider-neutral; only this module and the
infrastructure adapters know a backend's name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from footballai_v2.execution.coordinator import ExecutionSettings

QUEUE_BACKENDS = ("local", "azure_service_bus")
OBJECT_STORAGE_BACKENDS = ("local", "azure_blob")
DATABASE_BACKENDS = ("local_manifest", "postgres")


class BackendConfigurationError(ValueError):
    """Raised when a selected backend is missing required configuration."""


def _require_env(name: str, backend: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise BackendConfigurationError(
            f"backend {backend!r} requires environment variable {name}"
        )
    return value


# -- database / control plane -----------------------------------------------

def create_analysis_repository(settings: "ExecutionSettings"):
    backend = settings.database_backend
    if backend == "local_manifest":
        from footballai_v2.storage import LocalAnalysisRunStore

        return LocalAnalysisRunStore(settings.run_root)
    if backend == "postgres":
        url = _require_env("FOOTBALLAI_DATABASE_URL", backend)
        from sqlalchemy import create_engine

        from footballai_v2.storage.postgres import PostgreSQLAnalysisRepository

        engine = create_engine(url, future=True, pool_pre_ping=True)
        return PostgreSQLAnalysisRepository(engine)
    raise BackendConfigurationError(f"unknown database backend {backend!r}")


# -- object storage / data plane --------------------------------------------

def create_object_storage(settings: "ExecutionSettings"):
    backend = settings.object_storage_backend
    if backend == "local":
        from footballai_v2.storage import LocalAnalysisRunStore

        return LocalAnalysisRunStore(settings.run_root)
    if backend == "azure_blob":
        _require_container = os.getenv("FOOTBALLAI_BLOB_CONTAINER", "footballai-runs").strip()
        if not _require_container:
            raise BackendConfigurationError("azure_blob requires FOOTBALLAI_BLOB_CONTAINER")
        from footballai_v2.storage.object_storage.azure_blob import AzureBlobObjectStorage

        try:
            return AzureBlobObjectStorage.from_environment()
        except ValueError as exc:
            raise BackendConfigurationError(str(exc)) from exc
    raise BackendConfigurationError(f"unknown object storage backend {backend!r}")


# -- queue ------------------------------------------------------------------

def create_job_queue(settings: "ExecutionSettings", *, repository=None):
    backend = settings.queue_backend
    if backend == "local":
        from footballai_v2.execution.queue.local_filesystem import LocalFilesystemQueue

        return LocalFilesystemQueue(settings.queue_root)
    if backend == "azure_service_bus":
        connection = _require_env("FOOTBALLAI_SERVICEBUS_CONNECTION_STRING", backend)
        queue_name = _require_env("FOOTBALLAI_SERVICEBUS_QUEUE", backend)
        from azure.servicebus import ServiceBusClient

        from footballai_v2.execution.queue.azure_service_bus import AzureServiceBusQueue

        client = ServiceBusClient.from_connection_string(connection)
        return AzureServiceBusQueue(client, queue_name, repository=repository)
    raise BackendConfigurationError(f"unknown queue backend {backend!r}")


# -- startup validation ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BackendSelection:
    database_backend: str
    object_storage_backend: str
    queue_backend: str


def validate_backend_configuration(settings: "ExecutionSettings") -> BackendSelection:
    """Fail fast if any selected backend lacks required configuration.

    Only checks presence of required environment (cheap); it does not open a
    connection. Readiness performs the live capability check.
    """
    if settings.database_backend not in DATABASE_BACKENDS:
        raise BackendConfigurationError(
            f"FOOTBALLAI_DATABASE_BACKEND must be one of {DATABASE_BACKENDS}"
        )
    if settings.object_storage_backend not in OBJECT_STORAGE_BACKENDS:
        raise BackendConfigurationError(
            f"FOOTBALLAI_OBJECT_STORAGE_BACKEND must be one of {OBJECT_STORAGE_BACKENDS}"
        )
    if settings.queue_backend not in QUEUE_BACKENDS:
        raise BackendConfigurationError(
            f"FOOTBALLAI_QUEUE_BACKEND must be one of {QUEUE_BACKENDS}"
        )
    if settings.database_backend == "postgres":
        _require_env("FOOTBALLAI_DATABASE_URL", "postgres")
    if settings.object_storage_backend == "azure_blob":
        if not (
            os.getenv("FOOTBALLAI_BLOB_CONNECTION_STRING", "").strip()
            or os.getenv("FOOTBALLAI_BLOB_ACCOUNT_URL", "").strip()
        ):
            raise BackendConfigurationError(
                "azure_blob requires FOOTBALLAI_BLOB_CONNECTION_STRING or "
                "FOOTBALLAI_BLOB_ACCOUNT_URL"
            )
    if settings.queue_backend == "azure_service_bus":
        _require_env("FOOTBALLAI_SERVICEBUS_CONNECTION_STRING", "azure_service_bus")
        _require_env("FOOTBALLAI_SERVICEBUS_QUEUE", "azure_service_bus")
    return BackendSelection(
        database_backend=settings.database_backend,
        object_storage_backend=settings.object_storage_backend,
        queue_backend=settings.queue_backend,
    )
