"""Direct-upload application service: authorize -> upload -> finalize -> enqueue.

Deterministic and provider-neutral: in-memory object storage, the local manifest
repository, and the local filesystem queue. No cloud, no emulator.
"""

from __future__ import annotations

import pytest

from footballai_v2.contracts.v1 import AnalysisRunStatus, CodeReference, DataOrigin
from footballai_v2.execution.queue.local_filesystem import LocalFilesystemQueue
from footballai_v2.storage import LocalAnalysisRunStore
from footballai_v2.storage.object_storage import InMemoryObjectStorage
from footballai_v2.storage.upload_service import DirectUploadService

CODE = CodeReference("https://github.com/example/FootballAi", "8" * 40)


@pytest.fixture
def service(tmp_path):
    storage = InMemoryObjectStorage()
    repository = LocalAnalysisRunStore(tmp_path / "runs")
    queue = LocalFilesystemQueue(tmp_path / "queue")
    upload = DirectUploadService(
        storage, repository, queue, max_upload_bytes=1_000_000, code_reference=CODE
    )
    return upload, storage, repository, queue


def test_authorize_scopes_grant_to_run(service):
    upload, _storage, _repository, _queue = service
    authorized = upload.authorize(content_type="video/mp4")
    assert authorized.grant.object_reference.startswith(f"runs/{authorized.run_id}/input/")
    assert authorized.grant.max_bytes == 1_000_000
    assert authorized.grant.required_content_type == "video/mp4"


def test_authorize_rejects_non_video(service):
    upload, *_ = service
    with pytest.raises(ValueError):
        upload.authorize(content_type="application/zip")


def test_finalize_creates_attempt_and_enqueues(service):
    upload, storage, repository, queue = service
    authorized = upload.authorize(content_type="video/mp4")
    storage.put_input(authorized.run_id, b"tiny-video", extension=".mp4", content_type="video/mp4")

    run = upload.finalize(authorized.run_id, data_origin=DataOrigin.EVALUATION)

    assert run.run_id == authorized.run_id
    assert run.status is AnalysisRunStatus.QUEUED
    assert run.input.uri == f"blob://{authorized.grant.object_reference}"
    assert len(run.input.sha256) == 64
    loaded = repository.load(run.run_id)
    assert loaded.attempt_number == 1
    claimed = queue.claim("test-worker")
    assert claimed is not None and claimed.run_id == run.run_id


def test_finalize_is_idempotent(service):
    upload, storage, repository, queue = service
    authorized = upload.authorize(content_type="video/mp4")
    storage.put_input(authorized.run_id, b"tiny-video", extension=".mp4", content_type="video/mp4")

    first = upload.finalize(authorized.run_id, data_origin=DataOrigin.EVALUATION)
    second = upload.finalize(authorized.run_id, data_origin=DataOrigin.EVALUATION)

    assert first.run_id == second.run_id
    # Exactly one job was enqueued despite the duplicate finalize.
    assert queue.claim("test-worker") is not None
    assert queue.claim("test-worker") is None


def test_finalize_before_upload_fails(service):
    upload, *_ = service
    authorized = upload.authorize(content_type="video/mp4")
    with pytest.raises(FileNotFoundError):
        upload.finalize(authorized.run_id)
