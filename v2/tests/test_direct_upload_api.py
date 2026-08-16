"""HTTP coverage for the provider-neutral direct-upload workflow."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from footballai_v2.api import create_app
from footballai_v2.execution.queue.local_filesystem import LocalFilesystemQueue
from footballai_v2.storage import LocalAnalysisRunStore, StorageIntegrityError
from footballai_v2.storage.object_storage import InMemoryObjectStorage
from footballai_v2.storage.upload_service import DirectUploadService
from footballai_v2.execution.coordinator import AnalysisCoordinator


def _client(tmp_path, storage=None):
    run_root = tmp_path / "runs"
    queue_root = tmp_path / "queue"
    repository = LocalAnalysisRunStore(run_root)
    object_storage = storage or InMemoryObjectStorage()
    service = DirectUploadService(
        object_storage,
        repository,
        LocalFilesystemQueue(queue_root),
        max_upload_bytes=1024,
        code_reference=AnalysisCoordinator.code_reference(),
    )
    app = create_app(run_root, queue_root=queue_root, upload_service=service)
    return TestClient(app), object_storage, repository


def test_authorize_and_finalize_are_mounted_and_enqueue_once(tmp_path, caplog):
    client, storage, repository = _client(tmp_path)
    caplog.set_level(logging.INFO)

    authorized = client.post(
        "/api/v1/uploads/authorize", json={"content_type": "video/mp4"}
    )
    assert authorized.status_code == 200
    payload = authorized.json()
    assert set(payload) == {"run_id", "upload"}
    assert "object_reference" not in payload["upload"]
    assert payload["upload"]["method"] == "PUT"
    assert payload["upload"]["max_bytes"] == 1024

    run_id = payload["run_id"]
    storage.put_input(run_id, b"tiny-video", extension=".mp4", content_type="video/mp4")
    request = {
        "run_id": run_id,
        "match_name": "Student staging demo",
        "pipeline_profile": "demo_fast",
        "data_origin": "evaluation",
    }
    finalized = client.post("/api/v1/uploads/finalize", json=request)
    duplicate = client.post("/api/v1/uploads/finalize", json=request)

    assert finalized.status_code == 202
    assert duplicate.status_code == 202
    assert finalized.json()["run_id"] == run_id
    assert duplicate.json()["run_id"] == run_id
    assert repository.load(run_id).parameters["match_name"] == "Student staging demo"
    assert payload["upload"]["url"] not in caplog.text


def test_direct_upload_rejects_unavailable_local_capability(tmp_path):
    client = TestClient(create_app(tmp_path / "runs", queue_root=tmp_path / "queue"))
    response = client.post(
        "/api/v1/uploads/authorize", json={"content_type": "video/mp4"}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "direct_upload_unavailable"


def test_direct_upload_rejects_invalid_metadata_before_object_finalization(tmp_path):
    client, storage, _repository = _client(tmp_path)
    run_id = client.post(
        "/api/v1/uploads/authorize", json={"content_type": "video/mp4"}
    ).json()["run_id"]
    storage.put_input(run_id, b"tiny-video", extension=".mp4", content_type="video/mp4")
    response = client.post(
        "/api/v1/uploads/finalize",
        json={"run_id": run_id, "match_name": "   ", "pipeline_profile": "demo_fast"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_metadata"


class _BrokenStorage(InMemoryObjectStorage):
    def finalize_upload(self, *args, **kwargs):
        raise StorageIntegrityError("azure-internal-path/account/container")


def test_storage_integrity_failure_is_sanitized(tmp_path):
    client, _storage, _repository = _client(tmp_path, _BrokenStorage())
    run_id = client.post(
        "/api/v1/uploads/authorize", json={"content_type": "video/mp4"}
    ).json()["run_id"]
    response = client.post(
        "/api/v1/uploads/finalize",
        json={"run_id": run_id, "match_name": "Safe failure"},
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "error_code": "integrity_failure",
            "message": "The storage object failed integrity verification.",
        }
    }
    assert "azure-internal" not in response.text
