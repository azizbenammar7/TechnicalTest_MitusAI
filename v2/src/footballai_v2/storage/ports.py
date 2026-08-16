"""Provider-neutral persistence and object-storage boundaries for P2 adapters."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from footballai_v2.contracts.v1 import AnalysisRun, ArtifactCategory, ArtifactReference


@runtime_checkable
class AnalysisRepository(Protocol):
    """Control-plane lifecycle records; a future PostgreSQL adapter owns these."""

    def create(self, run: AnalysisRun) -> object: ...
    def load(self, run_id: str) -> AnalysisRun: ...
    def save(self, run: AnalysisRun) -> None: ...
    def list_runs(self) -> tuple[AnalysisRun, ...]: ...


@runtime_checkable
class ObjectStorage(Protocol):
    """Large input/artifact bytes; local paths are an adapter implementation detail."""

    def materialize_input(self, run_id: str) -> AbstractContextManager[Path]: ...
    def read_artifact_bytes(self, run_id: str, artifact_id: str, *, max_bytes: int = ...) -> bytes: ...
    def artifact_integrity(self, run_id: str, artifact_id: str) -> bool: ...
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
    ) -> ArtifactReference: ...


@dataclass(frozen=True, slots=True)
class UploadGrant:
    """Bounded, provider-neutral permission to upload one input object directly.

    The ``url`` is a short-lived, write-only, single-object authorization (for
    Azure, a SAS URL). It is a secret in transit and must never be logged. The
    application boundary only ever sees the opaque ``object_reference``; browsers
    never receive storage account keys and cannot address arbitrary blobs.
    """

    method: str
    url: str
    headers: Mapping[str, str]
    object_reference: str
    max_bytes: int
    expires_at: datetime
    required_content_type: str


@dataclass(frozen=True, slots=True)
class FinalizedUpload:
    """Result of verifying a directly-uploaded object before analysis creation."""

    object_reference: str
    sha256: str
    size_bytes: int
    content_type: str


@runtime_checkable
class UploadAuthorizer(Protocol):
    """Direct-to-object-storage upload boundary for the cloud data plane."""

    def authorize_upload(
        self,
        run_id: str,
        *,
        content_type: str,
        max_bytes: int,
        expires_seconds: int = ...,
    ) -> UploadGrant: ...

    def finalize_upload(
        self,
        run_id: str,
        *,
        max_bytes: int,
        allowed_content_types: tuple[str, ...] = ...,
    ) -> FinalizedUpload: ...
