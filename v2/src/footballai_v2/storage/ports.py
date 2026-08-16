"""Provider-neutral persistence and object-storage boundaries for P2 adapters."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, runtime_checkable

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
