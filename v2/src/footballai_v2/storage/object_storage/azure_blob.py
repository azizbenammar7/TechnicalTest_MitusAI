"""Azure Blob Storage adapter for the ObjectStorage + UploadAuthorizer ports.

All Azure SDK usage is confined to this module. Callers only ever exchange
opaque object references, provider-neutral :class:`ArtifactReference` /
:class:`UploadGrant` / :class:`FinalizedUpload` values, and plain bytes -- never
``BlobClient`` objects or Azure exception types.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContentSettings

from footballai_v2.contracts.v1 import ArtifactCategory, ArtifactReference, utc_now
from footballai_v2.storage.object_storage.credentials import (
    BlobCredentialStrategy,
    credential_from_environment,
)
from footballai_v2.storage.object_storage.keys import (
    artifact_object_key,
    input_object_key,
    input_prefix,
    run_prefix,
)
from footballai_v2.storage.object_storage.memory import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    UploadNotFoundError,
    _CONTENT_TYPE_EXTENSIONS,
)
from footballai_v2.storage.ports import FinalizedUpload, UploadGrant

logger = logging.getLogger("footballai_v2.storage.blob")


class AzureBlobObjectStorage:
    """Private-container Blob storage for uploaded inputs and published artifacts."""

    def __init__(self, credential: BlobCredentialStrategy, *, ensure_container: bool = True) -> None:
        self._credential = credential
        self._container = credential.container
        self._service = credential.service_client()
        self._client = self._service.get_container_client(self._container)
        if ensure_container:
            self._ensure_private_container()

    @classmethod
    def from_environment(cls) -> "AzureBlobObjectStorage":
        container = os.getenv("FOOTBALLAI_BLOB_CONTAINER", "footballai-runs").strip()
        if not container:
            raise ValueError("FOOTBALLAI_BLOB_CONTAINER must not be empty")
        return cls(credential_from_environment(container))

    def _ensure_private_container(self) -> None:
        try:
            # public_access defaults to None -> the container is private.
            self._client.create_container()
        except ResourceExistsError:
            pass

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
        digest = hashlib.sha256(content).hexdigest()
        blob = self._client.get_blob_client(key)
        try:
            blob.upload_blob(
                content,
                overwrite=False,  # write-once
                content_settings=ContentSettings(content_type=media_type),
                metadata={
                    "artifact_id": artifact_id,
                    "sha256": digest,
                    "size_bytes": str(len(content)),
                    "relative_path": relative_path,
                },
            )
        except ResourceExistsError as exc:
            raise ObjectAlreadyExistsError(key) from exc
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
        key, metadata = self._resolve_artifact(run_id, artifact_id)
        size = int(metadata["size_bytes"])
        if size > max_bytes:
            raise ValueError("registered artifact exceeds the configured read limit")
        content = self._client.get_blob_client(key).download_blob().readall()
        if len(content) != size or hashlib.sha256(content).hexdigest() != metadata["sha256"]:
            raise ValueError("registered artifact integrity check failed")
        return content

    def artifact_integrity(self, run_id: str, artifact_id: str) -> bool:
        try:
            self.read_artifact_bytes(run_id, artifact_id)
        except (ObjectNotFoundError, ValueError):
            return False
        return True

    def _resolve_artifact(self, run_id: str, artifact_id: str) -> tuple[str, dict[str, str]]:
        prefix = f"{run_prefix(run_id)}artifacts/"
        for blob in self._client.list_blobs(name_starts_with=prefix, include=["metadata"]):
            metadata = blob.metadata or {}
            if metadata.get("artifact_id") == artifact_id:
                return blob.name, metadata
        raise ObjectNotFoundError(f"artifact {artifact_id!r} not found for run {run_id}")

    # -- input ---------------------------------------------------------------

    def put_input(self, run_id: str, content: bytes, *, extension: str, content_type: str) -> str:
        """Server-side input write (used by the legacy/local ingestion path)."""
        key = input_object_key(run_id, extension)
        digest = hashlib.sha256(content).hexdigest()
        self._client.get_blob_client(key).upload_blob(
            content,
            overwrite=False,
            content_settings=ContentSettings(content_type=content_type),
            metadata={"sha256": digest, "size_bytes": str(len(content))},
        )
        return key

    @contextmanager
    def materialize_input(self, run_id: str) -> Iterator[Path]:
        key = self._single_input_key(run_id)
        directory = Path(tempfile.mkdtemp(prefix="footballai-input-"))
        target = directory / Path(key).name
        try:
            with target.open("wb") as handle:
                self._client.get_blob_client(key).download_blob().readinto(handle)
            yield target
        finally:
            target.unlink(missing_ok=True)
            directory.rmdir()

    def _single_input_key(self, run_id: str) -> str:
        prefix = input_prefix(run_id)
        names = [blob.name for blob in self._client.list_blobs(name_starts_with=prefix)]
        if len(names) != 1:
            raise ObjectNotFoundError(f"run {run_id} must contain exactly one input object")
        return names[0]

    def delete_object(self, object_reference: str) -> None:
        """Delete one uncommitted/temporary object; safe if it is already gone."""
        try:
            self._client.get_blob_client(object_reference).delete_blob()
        except ResourceNotFoundError:
            pass

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
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
        sas = self._credential.blob_write_sas(key, expiry=expiry)
        account_url = self._service.url.rstrip("/")
        url = f"{account_url}/{self._container}/{key}?{sas}"
        # The SAS URL is a secret in transit; log only the non-sensitive key.
        logger.info("upload_authorized run_id=%s object_key=%s", run_id, key)
        return UploadGrant(
            method="PUT",
            url=url,
            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": content_type},
            object_reference=key,
            max_bytes=max_bytes,
            expires_at=expiry,
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
            key = self._single_input_key(run_id)
            blob = self._client.get_blob_client(key)
            properties = blob.get_blob_properties()
        except (ObjectNotFoundError, ResourceNotFoundError) as exc:
            raise UploadNotFoundError(f"no uploaded object for run {run_id}") from exc
        size = int(properties.size)
        if size < 1 or size > max_bytes:
            raise ValueError("uploaded object size is outside the configured bound")
        content_type = (properties.content_settings.content_type or "").strip()
        if allowed_content_types and content_type not in allowed_content_types:
            raise ValueError("uploaded object content type is not allowed")
        # Compute the checksum from the stored bytes so the control plane records
        # a verified identity; never trust a browser-supplied hash.
        digest = hashlib.sha256()
        stream = blob.download_blob()
        for chunk in stream.chunks():
            digest.update(chunk)
        return FinalizedUpload(
            object_reference=key,
            sha256=digest.hexdigest(),
            size_bytes=size,
            content_type=content_type,
        )


def _extension_for(content_type: str) -> str:
    try:
        return _CONTENT_TYPE_EXTENSIONS[content_type]
    except KeyError as exc:
        raise ValueError(f"unsupported upload content type {content_type!r}") from exc
