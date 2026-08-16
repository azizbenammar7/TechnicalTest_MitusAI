"""In-memory object storage: the deterministic local data-plane adapter.

Used as the reference implementation in the ObjectStorage / UploadAuthorizer
contract suite so the fast test run needs no emulator, while the Azure adapter
is validated against the identical contract using Azurite.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from footballai_v2.contracts.v1 import ArtifactCategory, ArtifactReference, utc_now
from footballai_v2.storage.object_storage.keys import (
    artifact_object_key,
    input_object_key,
    input_prefix,
)
from footballai_v2.storage.ports import FinalizedUpload, UploadGrant


class ObjectNotFoundError(FileNotFoundError):
    """Raised when a requested object does not exist."""


class ObjectAlreadyExistsError(FileExistsError):
    """Raised when a write would overwrite an existing (write-once) object."""


class UploadNotFoundError(FileNotFoundError):
    """Raised when finalize runs before the authorized object was uploaded."""


class _StoredObject:
    __slots__ = ("data", "metadata", "content_type")

    def __init__(self, data: bytes, metadata: dict[str, str], content_type: str) -> None:
        self.data = data
        self.metadata = metadata
        self.content_type = content_type


class InMemoryObjectStorage:
    """Byte-accurate object storage with write-once and metadata integrity."""

    def __init__(self) -> None:
        self._objects: dict[str, _StoredObject] = {}

    # -- artifacts -----------------------------------------------------------

    def write_artifact(
        self,
        run_id: str,
        *,
        artifact_id: str,
        name: str,
        category: ArtifactCategory,
        relative_path: str,
        content: bytes,
        media_type: str,
        schema_version: str | None = None,
    ) -> ArtifactReference:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        key = artifact_object_key(run_id, relative_path)
        if key in self._objects:
            raise ObjectAlreadyExistsError(key)
        digest = hashlib.sha256(content).hexdigest()
        self._objects[key] = _StoredObject(
            content,
            {
                "artifact_id": artifact_id,
                "sha256": digest,
                "size_bytes": str(len(content)),
                "relative_path": relative_path,
            },
            media_type,
        )
        return ArtifactReference(
            artifact_id=artifact_id,
            name=name,
            category=category,
            relative_path=relative_path,
            media_type=media_type,
            sha256=digest,
            size_bytes=len(content),
            schema_version=schema_version,
        )

    def read_artifact_bytes(
        self, run_id: str, artifact_id: str, *, max_bytes: int = 25 * 1024 * 1024
    ) -> bytes:
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        key, stored = self._resolve_artifact(run_id, artifact_id)
        size = int(stored.metadata["size_bytes"])
        if size > max_bytes:
            raise ValueError("registered artifact exceeds the configured read limit")
        if len(stored.data) != size or hashlib.sha256(stored.data).hexdigest() != stored.metadata["sha256"]:
            raise ValueError("registered artifact integrity check failed")
        return stored.data

    def artifact_integrity(self, run_id: str, artifact_id: str) -> bool:
        try:
            self.read_artifact_bytes(run_id, artifact_id)
        except (ObjectNotFoundError, ValueError):
            return False
        return True

    def artifact_reference_integrity(self, run_id: str, reference: ArtifactReference) -> bool:
        try:
            _key, stored = self._resolve_artifact(run_id, reference.artifact_id)
        except ObjectNotFoundError:
            return False
        return (
            len(stored.data) == reference.size_bytes
            and hashlib.sha256(stored.data).hexdigest() == reference.sha256
        )

    def _resolve_artifact(self, run_id: str, artifact_id: str) -> tuple[str, _StoredObject]:
        from footballai_v2.storage.object_storage.keys import run_prefix

        prefix = f"{run_prefix(run_id)}artifacts/"
        for key, stored in self._objects.items():
            if key.startswith(prefix) and stored.metadata.get("artifact_id") == artifact_id:
                return key, stored
        raise ObjectNotFoundError(f"artifact {artifact_id!r} not found for run {run_id}")

    # -- input ---------------------------------------------------------------

    def put_input(self, run_id: str, content: bytes, *, extension: str, content_type: str) -> str:
        key = input_object_key(run_id, extension)
        digest = hashlib.sha256(content).hexdigest()
        self._objects[key] = _StoredObject(
            content,
            {"sha256": digest, "size_bytes": str(len(content))},
            content_type,
        )
        return key

    def put_input_file(
        self, run_id: str, source_path: str | Path, *, extension: str, content_type: str
    ) -> str:
        """Ingest a validated local file as this run's single input.

        The caller retains ownership of ``source_path``.
        """
        return self.put_input(
            run_id,
            Path(source_path).read_bytes(),
            extension=extension,
            content_type=content_type,
        )

    def copy_input(self, source_run_id: str, destination_run_id: str) -> None:
        key, stored = self._input_object(source_run_id)
        extension = Path(key).suffix
        self.put_input(
            destination_run_id,
            stored.data,
            extension=extension,
            content_type=stored.content_type,
        )

    def has_input(self, run_id: str) -> bool:
        try:
            self._input_object(run_id)
        except ObjectNotFoundError:
            return False
        return True

    @contextmanager
    def materialize_input(self, run_id: str) -> Iterator[Path]:
        key, stored = self._input_object(run_id)
        directory = Path(tempfile.mkdtemp(prefix="footballai-input-"))
        target = directory / Path(key).name
        try:
            target.write_bytes(stored.data)
            yield target
        finally:
            target.unlink(missing_ok=True)
            directory.rmdir()

    def _input_object(self, run_id: str) -> tuple[str, _StoredObject]:
        prefix = input_prefix(run_id)
        matches = [(key, stored) for key, stored in self._objects.items() if key.startswith(prefix)]
        if len(matches) != 1:
            raise ObjectNotFoundError(f"run {run_id} must contain exactly one input object")
        return matches[0]

    # -- direct upload boundary ---------------------------------------------

    def authorize_upload(
        self,
        run_id: str,
        *,
        content_type: str,
        max_bytes: int,
        expires_seconds: int = 900,
    ) -> UploadGrant:
        extension = _extension_for(content_type)
        key = input_object_key(run_id, extension)
        return UploadGrant(
            method="PUT",
            url=f"memory://upload/{key}",
            headers={"Content-Type": content_type},
            object_reference=key,
            max_bytes=max_bytes,
            expires_at=utc_now() + timedelta(seconds=expires_seconds),
            required_content_type=content_type,
        )

    def finalize_upload(
        self,
        run_id: str,
        *,
        max_bytes: int,
        allowed_content_types: tuple[str, ...] = (),
    ) -> FinalizedUpload:
        try:
            key, stored = self._input_object(run_id)
        except ObjectNotFoundError as exc:
            raise UploadNotFoundError(str(exc)) from exc
        size = int(stored.metadata["size_bytes"])
        if size < 1 or size > max_bytes:
            raise ValueError("uploaded object size is outside the configured bound")
        if allowed_content_types and stored.content_type not in allowed_content_types:
            raise ValueError("uploaded object content type is not allowed")
        return FinalizedUpload(
            object_reference=key,
            sha256=stored.metadata["sha256"],
            size_bytes=size,
            content_type=stored.content_type,
        )


_CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
}


def _extension_for(content_type: str) -> str:
    try:
        return _CONTENT_TYPE_EXTENSIONS[content_type]
    except KeyError as exc:
        raise ValueError(f"unsupported upload content type {content_type!r}") from exc
