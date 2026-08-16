"""Behavioural contract every object-storage adapter must satisfy.

Exercised against the in-memory reference adapter (always) and the Azure Blob
adapter via Azurite (when available), so both obey identical data-plane rules:
write-once, metadata-verified reads, bounded reads, and single-input
materialization.
"""

from __future__ import annotations

import uuid

import pytest

from footballai_v2.contracts.v1 import ArtifactCategory

CONTENT = b'{"schema":"footballai.team-summary/v1","value":42}\n'


class ObjectStorageContract:
    """Subclasses must provide a ``storage`` fixture and a fresh ``run_id`` fixture."""

    def test_write_then_read_roundtrip(self, storage, run_id):
        reference = storage.write_artifact(
            run_id,
            artifact_id="team-summary",
            name="Team summary",
            category=ArtifactCategory.SUMMARY,
            relative_path="artifacts/team-summary.json",
            content=CONTENT,
            media_type="application/json",
            schema_version="footballai.team-summary/v1",
        )
        assert reference.size_bytes == len(CONTENT)
        assert storage.read_artifact_bytes(run_id, "team-summary") == CONTENT
        assert storage.artifact_integrity(run_id, "team-summary") is True

    def test_write_is_write_once(self, storage, run_id):
        kwargs = dict(
            artifact_id="team-summary",
            name="Team summary",
            category=ArtifactCategory.SUMMARY,
            relative_path="artifacts/team-summary.json",
            content=CONTENT,
            media_type="application/json",
        )
        storage.write_artifact(run_id, **kwargs)
        with pytest.raises(FileExistsError):
            storage.write_artifact(run_id, **kwargs)

    def test_read_missing_artifact_is_not_integral(self, storage, run_id):
        assert storage.artifact_integrity(run_id, "does-not-exist") is False

    def test_read_respects_max_bytes(self, storage, run_id):
        storage.write_artifact(
            run_id,
            artifact_id="track-detail",
            name="Track detail",
            category=ArtifactCategory.TRACKS,
            relative_path="artifacts/track-detail.json",
            content=CONTENT,
            media_type="application/json",
        )
        with pytest.raises(ValueError):
            storage.read_artifact_bytes(run_id, "track-detail", max_bytes=1)

    def test_materialize_single_input(self, storage, run_id):
        storage.put_input(run_id, b"video-bytes", extension=".mp4", content_type="video/mp4")
        with storage.materialize_input(run_id) as path:
            assert path.read_bytes() == b"video-bytes"

    def test_authorize_and_finalize_upload(self, storage, run_id):
        grant = storage.authorize_upload(run_id, content_type="video/mp4", max_bytes=1024)
        assert grant.method == "PUT"
        assert grant.object_reference.startswith(f"runs/{run_id}/input/")
        assert grant.required_content_type == "video/mp4"
        # Simulate the browser's direct upload landing in storage.
        self.perform_upload(storage, run_id, grant, b"uploaded-video-bytes")
        finalized = storage.finalize_upload(
            run_id, max_bytes=1024, allowed_content_types=("video/mp4",)
        )
        assert finalized.size_bytes == len(b"uploaded-video-bytes")
        assert len(finalized.sha256) == 64
        assert finalized.content_type == "video/mp4"

    def test_finalize_before_upload_fails(self, storage, run_id):
        with pytest.raises(FileNotFoundError):
            storage.finalize_upload(run_id, max_bytes=1024)

    # Each adapter knows how to land the authorized upload in its backend.
    def perform_upload(self, storage, run_id, grant, content):  # pragma: no cover
        raise NotImplementedError
