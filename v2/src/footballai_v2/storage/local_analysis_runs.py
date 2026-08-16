"""Filesystem adapter for isolated V2 analysis-run outputs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from footballai_v2.contracts.v1 import (
    AnalysisRun,
    AnalysisRunStatus,
    ArtifactCategory,
    ArtifactReference,
    CodeReference,
    JsonValue,
    ModelReference,
    utc_now,
)
from footballai_v2.contracts.v1.analysis_run import (
    ContractValidationError,
    validate_relative_artifact_path,
    validate_run_id,
)


class RunAlreadyExistsError(FileExistsError):
    """Raised when a run ID has already reserved an output namespace."""


class RunNotFoundError(FileNotFoundError):
    """Raised when an analysis-run namespace does not exist."""


class ManifestConflictError(RuntimeError):
    """Raised when a manifest update does not match the stored run identity."""


class LocalAnalysisRunStore:
    """Store each analysis run under one non-overlapping directory.

    Layout::

        <root>/<run-id>/
            manifest.json
            artifacts/...

    Artifact files use exclusive creation. A repeated write cannot silently
    replace either an artifact from the same run or one from another run.
    """

    MANIFEST_NAME = "manifest.json"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)

    def create(self, run: AnalysisRun) -> Path:
        """Atomically reserve a new run namespace and persist its manifest."""
        if run.status is not AnalysisRunStatus.QUEUED:
            raise ManifestConflictError("a run namespace must be created in queued state")
        run_dir = self.run_directory(run.run_id)
        try:
            run_dir.mkdir()
        except FileExistsError as exc:
            raise RunAlreadyExistsError(run.run_id) from exc
        for child in ("input", "artifacts", "logs", "tmp"):
            (run_dir / child).mkdir()
        try:
            self._write_manifest(run, replace_existing=False)
        except Exception:
            # Only remove the empty namespace created by this call.
            self.manifest_path(run.run_id).unlink(missing_ok=True)
            for child in ("input", "artifacts", "logs", "tmp"):
                (run_dir / child).rmdir()
            run_dir.rmdir()
            raise
        return run_dir

    def load(self, run_id: str) -> AnalysisRun:
        run_dir = self.run_directory(run_id)
        self._validate_run_directory(run_dir)
        manifest_path = run_dir / self.MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise RunNotFoundError(run_id)
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ContractValidationError(f"invalid stored manifest for {run_id}") from exc
        run = AnalysisRun.from_dict(data)
        if run.run_id != run_id:
            raise ManifestConflictError("manifest run ID does not match its directory")
        return run

    def save(self, run: AnalysisRun) -> None:
        """Atomically replace a manifest without changing its run namespace."""
        current = self.load(run.run_id)
        if current.status.is_terminal:
            raise ManifestConflictError("terminal manifests are immutable")
        allowed = {
            AnalysisRunStatus.QUEUED: {
                AnalysisRunStatus.QUEUED,
                AnalysisRunStatus.RUNNING,
                AnalysisRunStatus.FAILED,
                AnalysisRunStatus.CANCELLED,
            },
            AnalysisRunStatus.RUNNING: {
                AnalysisRunStatus.RUNNING,
                AnalysisRunStatus.SUCCEEDED,
                AnalysisRunStatus.PARTIAL,
                AnalysisRunStatus.FAILED,
                AnalysisRunStatus.CANCELLED,
            },
        }
        if run.status not in allowed[current.status]:
            raise ManifestConflictError(
                f"cannot replace {current.status.value} manifest with {run.status.value}"
            )
        immutable_fields = (
            "contract_version",
            "logical_analysis_id",
            "run_id",
            "attempt_number",
            "previous_attempt_run_id",
            "data_origin",
            "input",
            "code",
            "pipeline_version",
            "parameters",
            "models",
            "created_at",
        )
        if any(getattr(current, name) != getattr(run, name) for name in immutable_fields):
            raise ManifestConflictError("run provenance cannot change after namespace creation")
        if run.status in {AnalysisRunStatus.SUCCEEDED, AnalysisRunStatus.PARTIAL}:
            self._verify_artifacts(run)
        self._write_manifest(run, replace_existing=True)

    def input_path(self, run_id: str) -> Path:
        """Return the single safe uploaded input registered for a run."""
        run_dir = self._require_run_directory(run_id)
        input_dir = run_dir / "input"
        candidates = [item for item in input_dir.iterdir() if item.is_file() and not item.is_symlink()]
        if len(candidates) != 1:
            raise ManifestConflictError("run must contain exactly one safe input file")
        self._require_within_run(candidates[0].resolve(), run_dir)
        return candidates[0]

    @contextmanager
    def materialize_input(self, run_id: str) -> Iterator[Path]:
        """Yield a local input path; cloud adapters may download to bounded temporary storage."""
        yield self.input_path(run_id)

    def put_input_file(
        self, run_id: str, source_path: str | Path, *, extension: str, content_type: str
    ) -> str:
        """Ingest a validated local file as this run's single input (write-once).

        The caller retains ownership of ``source_path`` and cleans it up. The
        returned reference is the run-scoped object key, matching the shape every
        object-storage adapter uses.
        """
        if not extension.startswith(".") or "/" in extension or "\\" in extension:
            raise ValueError("extension must be a leading-dot suffix such as '.mp4'")
        run_dir = self._require_run_directory(run_id)
        destination = run_dir / "input" / f"source{extension}"
        with Path(source_path).open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        return f"runs/{validate_run_id(run_id)}/input/source{extension}"

    def copy_input(self, source_run_id: str, destination_run_id: str) -> None:
        """Copy the single input of one run into another, verifying integrity."""
        source_run = self.load(source_run_id)
        source = self.input_path(source_run_id)
        destination = self._require_run_directory(destination_run_id) / "input" / source.name
        digest = hashlib.sha256()
        with source.open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if digest.hexdigest() != source_run.input.sha256:
            destination.unlink(missing_ok=True)
            raise ManifestConflictError("source input integrity check failed")

    def has_input(self, run_id: str) -> bool:
        """Return whether the run has exactly one safe uploaded input."""
        try:
            self.input_path(run_id)
        except (ManifestConflictError, RunNotFoundError, ContractValidationError):
            return False
        return True

    # -- cancellation (control-plane intent, local adapter) ------------------
    #
    # Cancellation is authoritative control-plane state exposed through the
    # AnalysisRepository port. The local adapter records intent as an atomic
    # marker file inside the run namespace; callers never see this mechanism --
    # they use request_cancellation / cancellation_requested only.
    CANCEL_MARKER = "cancel-request.json"

    def request_cancellation(self, run_id: str) -> None:
        run_dir = self._require_run_directory(run_id)
        if self.load(run_id).status.is_terminal:
            return
        payload = (
            json.dumps({"requested_at": utc_now().isoformat().replace("+00:00", "Z")}) + "\n"
        )
        fd, temporary = tempfile.mkstemp(prefix=".cancel-", dir=run_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, run_dir / self.CANCEL_MARKER)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def cancellation_requested(self, run_id: str) -> bool:
        marker = self.run_directory(run_id) / self.CANCEL_MARKER
        return marker.is_file() and not marker.is_symlink()

    def create_retry_attempt(
        self,
        previous_run_id: str,
        *,
        code: CodeReference | None = None,
        pipeline_version: str | None = None,
        parameters: Mapping[str, JsonValue] | None = None,
        models: Sequence[ModelReference] | None = None,
        run_id: str | None = None,
    ) -> AnalysisRun:
        """Create a new queued namespace linked to a failed or partial attempt.

        Logical input identity, origin, and logical analysis ID are copied from
        the previous manifest. Attempt-specific code, pipeline, configuration,
        and model provenance may be replaced explicitly and remains visible in
        the new manifest.
        """
        previous = self.load(previous_run_id)
        retry = AnalysisRun.retry_from(
            previous,
            code=code,
            pipeline_version=pipeline_version,
            parameters=parameters,
            models=models,
            run_id=run_id,
        )
        self.create(retry)
        return retry

    def list_runs(self) -> tuple[AnalysisRun, ...]:
        """Return valid stored manifests newest first, ignoring foreign entries."""
        runs: list[AnalysisRun] = []
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                validate_run_id(candidate.name)
                runs.append(self.load(candidate.name))
            except (ContractValidationError, ManifestConflictError, RunNotFoundError):
                continue
        return tuple(sorted(runs, key=lambda item: item.created_at, reverse=True))

    def read_artifact_bytes(
        self,
        run_id: str,
        artifact_id: str,
        *,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> bytes:
        """Read one registered artifact after namespace, size, and hash checks."""
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        run = self.load(run_id)
        artifact = next(
            (item for item in run.artifacts if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None:
            raise RunNotFoundError(f"artifact {artifact_id!r} is not registered")
        if artifact.size_bytes > max_bytes:
            raise ManifestConflictError("registered artifact exceeds the configured read limit")
        path = self.artifact_path(run_id, artifact.relative_path)
        if not path.is_file() or path.is_symlink():
            raise ManifestConflictError("registered artifact is missing or unsafe")
        content = path.read_bytes()
        if len(content) != artifact.size_bytes:
            raise ManifestConflictError("registered artifact size does not match")
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ManifestConflictError("registered artifact hash does not match")
        return content

    def artifact_integrity(self, run_id: str, artifact_id: str) -> bool:
        """Return whether a registered artifact passes bounded integrity checks."""
        try:
            self.read_artifact_bytes(run_id, artifact_id)
        except (ManifestConflictError, RunNotFoundError, OSError):
            return False
        return True

    def artifact_reference_integrity(self, run_id: str, reference: ArtifactReference) -> bool:
        """Verify a just-written artifact against its reference, without the manifest.

        Used by the executor to gate a terminal transition before the artifact
        list has been persisted, so it cannot resolve an id through the manifest.
        """
        try:
            path = self.artifact_path(run_id, reference.relative_path)
            if not path.is_file() or path.is_symlink():
                return False
            content = path.read_bytes()
        except (ContractValidationError, ManifestConflictError, RunNotFoundError, OSError):
            return False
        return (
            len(content) == reference.size_bytes
            and hashlib.sha256(content).hexdigest() == reference.sha256
        )

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
        """Write bytes once and return their content-addressed reference."""
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        run_dir = self._require_run_directory(run_id)
        if self.load(run_id).status is not AnalysisRunStatus.RUNNING:
            raise ManifestConflictError("artifacts can only be written while a run is running")
        normalized = validate_relative_artifact_path(relative_path)
        destination = run_dir.joinpath(*Path(normalized).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._require_within_run(destination.parent.resolve(), run_dir)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}-",
            suffix=".tmp",
            dir=destination.parent,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return ArtifactReference(
            artifact_id=artifact_id,
            name=name,
            category=category,
            relative_path=normalized,
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            schema_version=schema_version,
        )

    def run_directory(self, run_id: str) -> Path:
        canonical = validate_run_id(run_id)
        return self.root / canonical

    def manifest_path(self, run_id: str) -> Path:
        return self.run_directory(run_id) / self.MANIFEST_NAME

    def artifact_path(self, run_id: str, relative_path: str) -> Path:
        run_dir = self._require_run_directory(run_id)
        normalized = validate_relative_artifact_path(relative_path)
        candidate = run_dir.joinpath(*Path(normalized).parts)
        self._require_within_run(candidate.resolve(), run_dir)
        return candidate

    def _require_run_directory(self, run_id: str) -> Path:
        run_dir = self.run_directory(run_id)
        self._validate_run_directory(run_dir)
        manifest_path = run_dir / self.MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise RunNotFoundError(run_id)
        return run_dir

    def _validate_run_directory(self, run_dir: Path) -> None:
        if not run_dir.is_dir():
            raise RunNotFoundError(run_dir.name)
        if run_dir.is_symlink():
            raise ContractValidationError("analysis-run directory cannot be a symlink")
        self._require_within_run(run_dir.resolve(), self.root)

    def _verify_artifacts(self, run: AnalysisRun) -> None:
        for artifact in run.artifacts:
            path = self.artifact_path(run.run_id, artifact.relative_path)
            if not path.is_file() or path.is_symlink():
                raise ManifestConflictError(
                    f"artifact {artifact.name!r} is missing or is not a regular run artifact"
                )
            if path.stat().st_size != artifact.size_bytes:
                raise ManifestConflictError(
                    f"artifact {artifact.name!r} size does not match its manifest"
                )
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != artifact.sha256:
                raise ManifestConflictError(
                    f"artifact {artifact.name!r} hash does not match its manifest"
                )

    def _write_manifest(self, run: AnalysisRun, *, replace_existing: bool) -> None:
        path = self.manifest_path(run.run_id)
        payload = (
            json.dumps(run.to_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        if not replace_existing:
            with path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return
        fd, temp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _require_within_run(candidate: Path, run_dir: Path) -> None:
        resolved_run = run_dir.resolve()
        try:
            candidate.resolve().relative_to(resolved_run)
        except ValueError as exc:
            raise ContractValidationError("path escapes its analysis-run namespace") from exc
