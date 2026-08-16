"""Bounded tests for production configuration and provider-neutral P2 ports."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from footballai_v2.api import create_app
from footballai_v2.execution.coordinator import ExecutionSettings
from footballai_v2.execution.queue import LocalFilesystemQueue, create_job_queue
from footballai_v2.storage import AnalysisRepository, LocalAnalysisRunStore, ObjectStorage


def test_local_store_implements_control_plane_and_object_storage_ports(tmp_path):
    store = LocalAnalysisRunStore(tmp_path / "runs")
    assert isinstance(store, AnalysisRepository)
    assert isinstance(store, ObjectStorage)


def test_queue_factory_preserves_local_adapter_and_rejects_unimplemented_cloud(tmp_path):
    assert isinstance(create_job_queue("local", tmp_path / "queue"), LocalFilesystemQueue)
    with pytest.raises(ValueError, match="not implemented|only the local"):
        create_job_queue("azure_service_bus", tmp_path / "queue")


def test_environment_configuration_selects_only_implemented_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOTBALLAI_ENVIRONMENT", "staging")
    monkeypatch.setenv("FOOTBALLAI_V2_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("FOOTBALLAI_V2_QUEUE_ROOT", str(tmp_path / "queue"))
    settings = ExecutionSettings.from_environment()
    assert settings.environment == "staging"
    assert settings.queue_backend == "local"
    assert settings.object_storage_backend == "local"
    assert settings.database_backend == "local_manifest"

    monkeypatch.setenv("FOOTBALLAI_QUEUE_BACKEND", "azure_service_bus")
    with pytest.raises(ValueError, match="not implemented"):
        ExecutionSettings.from_environment()


def test_production_cors_allows_explicit_https_origin(tmp_path):
    settings = ExecutionSettings(tmp_path / "runs", tmp_path / "queue", environment="production")
    client = TestClient(
        create_app(
            settings.run_root,
            settings=settings,
            allowed_origins=("https://staging.footballai.example",),
        )
    )
    response = client.get(
        "/api/health", headers={"Origin": "https://staging.footballai.example"}
    )
    assert response.headers["access-control-allow-origin"] == "https://staging.footballai.example"


def test_docker_build_files_copy_only_required_application_inputs():
    repository = Path(__file__).resolve().parents[2]
    dockerignore = (repository / ".dockerignore").read_text(encoding="utf-8")
    for excluded in (".git", ".env.*", ".models", "*.pt", "*.mp4", "data/runs", "data/job-queue"):
        assert excluded in dockerignore
    for dockerfile in ("api.Dockerfile", "worker.Dockerfile", "frontend.Dockerfile"):
        content = (repository / "docker" / dockerfile).read_text(encoding="utf-8")
        assert "USER " in content
        assert "COPY . " not in content


def test_production_requirement_files_are_fully_pinned():
    repository = Path(__file__).resolve().parents[2]
    for filename in ("requirements-api.txt", "requirements-worker-core.txt"):
        requirements = (repository / "v2" / filename).read_text(encoding="utf-8").splitlines()
        assert all("==" in line for line in requirements if line and not line.startswith("#"))
