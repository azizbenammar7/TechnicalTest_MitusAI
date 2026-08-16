"""Upload ingestion and immutable-attempt coordination."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Mapping

from footballai_v2.contracts.v1 import (
    AnalysisRun, CodeReference, DataOrigin, InputReference, ModelReference, StageExecution, StageName, StageStatus, StructuredError, utc_now,
)
from footballai_v2.execution.adapters import profile_catalog
from footballai_v2.execution.adapters.v1_compat_runtime import check_v1_compat_readiness
from footballai_v2.execution.contracts import ExecutionJob
from footballai_v2.execution.queue import create_job_queue
from footballai_v2.storage import LocalAnalysisRunStore, ManifestConflictError


MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm"}
CONTAINERS = {".mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, ".mov": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, ".mkv": {"matroska", "webm"}, ".webm": {"matroska", "webm"}}


class UploadValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message); self.code = code; self.safe_message = message


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    run_root: Path
    queue_root: Path
    max_upload_bytes: int = 250 * 1024 * 1024
    max_video_duration_seconds: float = 4 * 60 * 60
    allowed_extensions: tuple[str, ...] = (".mp4", ".mov", ".mkv", ".webm")
    ffprobe_timeout_seconds: float = 10
    allow_test_profiles: bool = False
    environment: str = "local"
    queue_backend: str = "local"
    object_storage_backend: str = "local"
    database_backend: str = "local_manifest"

    def __post_init__(self) -> None:
        if self.environment not in {"local", "staging", "production", "test"}:
            raise ValueError("FOOTBALLAI_ENVIRONMENT must be local, staging, production, or test")
        implemented = {
            "queue": (self.queue_backend, "local"),
            "object storage": (self.object_storage_backend, "local"),
            "database": (self.database_backend, "local_manifest"),
        }
        for label, (configured, supported) in implemented.items():
            if configured != supported:
                raise ValueError(
                    f"The configured {label} backend {configured!r} is not implemented; expected {supported!r}"
                )
        if self.max_upload_bytes < 1:
            raise ValueError("FOOTBALLAI_MAX_UPLOAD_BYTES must be positive")
        if self.max_video_duration_seconds <= 0:
            raise ValueError("FOOTBALLAI_MAX_VIDEO_DURATION_SECONDS must be positive")

    @classmethod
    def from_environment(cls, run_root: str | Path | None = None, queue_root: str | Path | None = None) -> "ExecutionSettings":
        extensions = tuple(
            item.strip().lower() if item.strip().startswith(".") else f".{item.strip().lower()}"
            for item in os.getenv("FOOTBALLAI_ALLOWED_VIDEO_EXTENSIONS", ".mp4,.mov,.mkv,.webm").split(",") if item.strip()
        )
        return cls(
            run_root=Path(run_root or os.getenv("FOOTBALLAI_V2_RUN_ROOT", "data/runs")),
            queue_root=Path(queue_root or os.getenv("FOOTBALLAI_V2_QUEUE_ROOT", "data/job-queue")),
            max_upload_bytes=int(os.getenv("FOOTBALLAI_MAX_UPLOAD_BYTES", str(250 * 1024 * 1024))),
            max_video_duration_seconds=float(os.getenv("FOOTBALLAI_MAX_VIDEO_DURATION_SECONDS", str(4 * 60 * 60))),
            allowed_extensions=extensions,
            ffprobe_timeout_seconds=float(os.getenv("FFPROBE_TIMEOUT_SECONDS", "10")),
            allow_test_profiles=os.getenv("FOOTBALLAI_ENABLE_TEST_PROFILES", "0") == "1",
            environment=os.getenv("FOOTBALLAI_ENVIRONMENT", "local").strip().lower(),
            queue_backend=os.getenv("FOOTBALLAI_QUEUE_BACKEND", "local").strip().lower(),
            object_storage_backend=os.getenv("FOOTBALLAI_OBJECT_STORAGE_BACKEND", "local").strip().lower(),
            database_backend=os.getenv("FOOTBALLAI_DATABASE_BACKEND", "local_manifest").strip().lower(),
        )


class AnalysisCoordinator:
    def __init__(self, settings: ExecutionSettings) -> None:
        self.settings = settings
        self.store = LocalAnalysisRunStore(settings.run_root)
        self.queue = create_job_queue(settings.queue_backend, settings.queue_root)
        self.upload_root = self.store.root / ".uploads"
        self.upload_root.mkdir(exist_ok=True)

    def stream_upload(self, source: BinaryIO, filename: str) -> tuple[Path, str, int, str]:
        extension = self._validate_filename(filename)
        digest = hashlib.sha256(); size = 0
        fd, temporary = tempfile.mkstemp(prefix="upload-", suffix=extension, dir=self.upload_root)
        try:
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_upload_bytes:
                        raise UploadValidationError("upload_too_large", "Video exceeds the configured upload limit.")
                    digest.update(chunk); target.write(chunk)
                target.flush(); os.fsync(target.fileno())
            if size == 0:
                raise UploadValidationError("empty_upload", "Uploaded video is empty.")
            return Path(temporary), digest.hexdigest(), size, extension
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    def probe_video(self, path: Path, extension: str) -> dict:
        before = path.stat()
        command = ["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration,size", "-of", "json", str(path)]
        try:
            completed = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=self.settings.ffprobe_timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            raise UploadValidationError("video_probe_timeout", "Video validation timed out.") from exc
        except OSError as exc:
            raise UploadValidationError("video_probe_unavailable", "Video validation is unavailable locally.") from exc
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise UploadValidationError("upload_changed", "Uploaded file changed during validation.")
        if completed.returncode != 0:
            raise UploadValidationError("invalid_video", "The uploaded file is not a valid supported video container.")
        try:
            info = json.loads(completed.stdout)["format"]
            duration = float(info["duration"])
            formats = set(str(info["format_name"]).split(","))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise UploadValidationError("invalid_video", "The uploaded file has incomplete video metadata.") from exc
        if not formats.intersection(CONTAINERS[extension]):
            raise UploadValidationError("container_mismatch", "Video contents do not match the selected file type.")
        if duration <= 0 or duration > self.settings.max_video_duration_seconds:
            raise UploadValidationError("invalid_duration", "Video duration is outside the configured limit.")
        return {"duration_seconds": round(duration, 3), "container": sorted(formats)[0], "size_bytes": before.st_size}

    def create_analysis(self, temporary: Path, *, filename: str, checksum: str, size_bytes: int, extension: str, content_type: str, metadata: Mapping[str, str]) -> AnalysisRun:
        if content_type != MEDIA_TYPES[extension]:
            temporary.unlink(missing_ok=True)
            raise UploadValidationError("unsupported_media_type", "The uploaded media type is not supported.")
        probe = self.probe_video(temporary, extension)
        profile = metadata.get("pipeline_profile", "demo_fast")
        profile_info = next((item for item in profile_catalog(include_test=self.settings.allow_test_profiles) if item["profile_id"] == profile), None)
        if profile_info is None:
            temporary.unlink(missing_ok=True); raise UploadValidationError("unknown_profile", "Unknown pipeline profile.")
        if not profile_info["available"]:
            temporary.unlink(missing_ok=True); raise UploadValidationError("profile_unavailable", "Selected pipeline profile is unavailable on this machine.")
        try:
            origin = DataOrigin(metadata.get("data_origin", "real"))
        except ValueError as exc:
            temporary.unlink(missing_ok=True); raise UploadValidationError("invalid_origin", "Invalid data origin.") from exc
        if origin is DataOrigin.LEGACY_V1:
            temporary.unlink(missing_ok=True); raise UploadValidationError("invalid_origin", "Legacy origin is reserved for imported artifacts.")
        warnings = list(profile_info["warnings"])
        parameters = {**dict(metadata), "pipeline_profile": profile, "original_filename": filename, "upload_size_bytes": size_bytes, "video_probe": probe, "quality_warnings": warnings}
        models: tuple[ModelReference, ...] = ()
        if profile == "v1_compat":
            readiness = check_v1_compat_readiness()
            if not readiness.ready or readiness.config is None:
                temporary.unlink(missing_ok=True)
                raise UploadValidationError("profile_unavailable", "Selected pipeline profile is unavailable on this machine.")
            parameters["v1_compat"] = readiness.config.public_dict()
            models = (ModelReference("yolov8m.pt", "ultralytics-yolov8m", readiness.config.model_sha256),)
        run = AnalysisRun.new(
            data_origin=origin, input=InputReference(f"run-input://source{extension}", checksum, MEDIA_TYPES[extension]),
            code=self._code_reference(), pipeline_version=f"{profile}/1.0.0", parameters=parameters,
            models=models,
            stages=self._queued_stages(1, profile),
        )
        run_dir = self.store.create(run)
        destination = run_dir / "input" / f"source{extension}"
        try:
            os.replace(temporary, destination)
            self.queue.enqueue(ExecutionJob.new(run.run_id, run.logical_analysis_id, run.attempt_number, profile))
        except Exception:
            if not run.status.is_terminal:
                from footballai_v2.contracts.v1 import StructuredError, utc_now
                self.store.save(run.fail(StructuredError("ingestion_failed", "The validated upload could not be queued.", True, utc_now())))
            temporary.unlink(missing_ok=True)
            raise
        return run

    def retry(self, run_id: str) -> AnalysisRun:
        previous = self.store.load(run_id)
        profile = str(previous.parameters["pipeline_profile"])
        retry = AnalysisRun.retry_from(previous).with_stages(self._queued_stages(previous.attempt_number + 1, profile))
        self.store.create(retry)
        try:
            self._copy_input(previous.run_id, retry.run_id)
            self.queue.enqueue(ExecutionJob.new(retry.run_id, retry.logical_analysis_id, retry.attempt_number, profile))
        except Exception:
            self.store.save(retry.fail(StructuredError("input_copy_failed", "The source input could not be copied safely.", False, utc_now())))
            raise
        return retry

    def clone(self, run_id: str) -> AnalysisRun:
        previous = self.store.load(run_id)
        profile = str(previous.parameters["pipeline_profile"])
        clone = AnalysisRun.new(data_origin=previous.data_origin, input=previous.input, code=self._code_reference(), pipeline_version=previous.pipeline_version, parameters=dict(previous.parameters), models=previous.models, stages=self._queued_stages(1, profile))
        self.store.create(clone)
        try:
            self._copy_input(previous.run_id, clone.run_id)
            self.queue.enqueue(ExecutionJob.new(clone.run_id, clone.logical_analysis_id, 1, profile))
        except Exception:
            self.store.save(clone.fail(StructuredError("input_copy_failed", "The source input could not be copied safely.", False, utc_now())))
            raise
        return clone

    def _copy_input(self, source_run_id: str, destination_run_id: str) -> None:
        source_run = self.store.load(source_run_id)
        source = self.store.input_path(source_run_id)
        destination = self.store.run_directory(destination_run_id) / "input" / source.name
        digest = hashlib.sha256()
        with source.open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk); writer.write(chunk)
            writer.flush(); os.fsync(writer.fileno())
        if digest.hexdigest() != source_run.input.sha256:
            destination.unlink(missing_ok=True)
            raise ManifestConflictError("source input integrity check failed")

    def _validate_filename(self, filename: str) -> str:
        if not filename or len(filename) > 255 or "\x00" in filename or Path(filename).name != filename or ".." in filename:
            raise UploadValidationError("unsafe_filename", "Video filename is unsafe.")
        extension = Path(filename).suffix.lower()
        if extension not in self.settings.allowed_extensions or extension not in MEDIA_TYPES:
            raise UploadValidationError("unsupported_extension", "Video file extension is not supported.")
        return extension

    @staticmethod
    def _queued_stages(attempt: int, profile: str = "demo_fast") -> tuple[StageExecution, ...]:
        unsupported = {StageName.IDENTITY_RESOLUTION, StageName.PITCH_CALIBRATION} if profile == "v1_compat" else set()
        return tuple(StageExecution(name.value, name, name not in unsupported, StageStatus.QUEUED, 0, attempt) for name in StageName)

    @staticmethod
    def _code_reference() -> CodeReference:
        revision = os.getenv("FOOTBALLAI_CODE_REVISION", "").lower()
        dirty = os.getenv("FOOTBALLAI_CODE_DIRTY")
        if not revision:
            try:
                repository_root = Path(__file__).resolve().parents[4]
                revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository_root, shell=False, capture_output=True, text=True, timeout=2, check=True).stdout.strip().lower()
                if dirty is None:
                    dirty = "1" if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repository_root, shell=False, capture_output=True, text=True, timeout=2, check=True).stdout.strip() else "0"
            except (OSError, subprocess.SubprocessError):
                revision = "0" * 40
        if len(revision) not in {40, 64} or any(c not in "0123456789abcdef" for c in revision):
            revision = "0" * 40
        return CodeReference("https://github.com/azizbenammar7/FootballAi", revision, dirty == "1")
