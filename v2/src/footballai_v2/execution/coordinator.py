"""Upload ingestion and immutable-attempt coordination."""

from __future__ import annotations

import hashlib
import json
import logging
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
from footballai_v2.logging_config import bind_log_context, log_event


MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm"}
CONTAINERS = {".mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, ".mov": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, ".mkv": {"matroska", "webm"}, ".webm": {"matroska", "webm"}}
logger = logging.getLogger("footballai_v2.api")


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
        # Backend selection and its required configuration are validated by the
        # composition root, which fails fast and never silently falls back.
        from footballai_v2.composition import validate_backend_configuration

        validate_backend_configuration(self)
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
    """Multipart ingestion + attempt lifecycle over provider-neutral planes.

    Depends only on an :class:`AnalysisRepository` (control plane), an
    :class:`ObjectStorage` (data plane), and a :class:`JobQueue` (delivery). The
    same coordinator runs local, split (PostgreSQL + Blob + local queue), or full
    Azure -- it never requires a shared filesystem. The browser still streams the
    upload to the API here (the legacy/local-friendly path); the direct-to-object
    :class:`DirectUploadService` remains the preferred cloud ingestion route.
    """

    def __init__(
        self,
        settings: ExecutionSettings,
        *,
        repository=None,
        object_storage=None,
        queue=None,
    ) -> None:
        from footballai_v2 import composition

        self.settings = settings
        self.repository = repository or composition.create_analysis_repository(settings)
        self.object_storage = object_storage or composition.create_object_storage(settings)
        self.queue = queue or composition.create_job_queue(settings, repository=self.repository)
        # API-local scratch for streaming + probing one upload before it is handed
        # to object storage. Ephemeral only; never authoritative and never shared.
        self.upload_root = Path(tempfile.gettempdir()) / "footballai-uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)

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
            code=self.code_reference(), pipeline_version=f"{profile}/1.0.0", parameters=parameters,
            models=models,
            stages=self._queued_stages(1, profile),
        )
        self.repository.create(run)
        try:
            # put_input_file copies bytes into object storage; the coordinator
            # keeps ownership of the scratch file and removes it in the finally.
            self.object_storage.put_input_file(run.run_id, temporary, extension=extension, content_type=MEDIA_TYPES[extension])
            with bind_log_context(run_id=run.run_id, logical_analysis_id=run.logical_analysis_id, attempt_number=run.attempt_number):
                self.queue.enqueue(ExecutionJob.new(run.run_id, run.logical_analysis_id, run.attempt_number, profile))
                log_event(logger, logging.INFO, "analysis.queued", "Analysis created and queued", profile=profile, status="queued")
        except Exception:
            if not run.status.is_terminal:
                from footballai_v2.contracts.v1 import StructuredError, utc_now
                self.repository.save(run.fail(StructuredError("ingestion_failed", "The validated upload could not be queued.", True, utc_now())))
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return run

    def retry(self, run_id: str) -> AnalysisRun:
        previous = self.repository.load(run_id)
        profile = str(previous.parameters["pipeline_profile"])
        retry = AnalysisRun.retry_from(previous).with_stages(self._queued_stages(previous.attempt_number + 1, profile))
        self.repository.create(retry)
        try:
            self.object_storage.copy_input(previous.run_id, retry.run_id)
            with bind_log_context(run_id=retry.run_id, logical_analysis_id=retry.logical_analysis_id, attempt_number=retry.attempt_number):
                self.queue.enqueue(ExecutionJob.new(retry.run_id, retry.logical_analysis_id, retry.attempt_number, profile))
                log_event(logger, logging.INFO, "analysis.retry_queued", "Analysis retry queued", profile=profile, status="queued")
        except Exception:
            self.repository.save(retry.fail(StructuredError("input_copy_failed", "The source input could not be copied safely.", False, utc_now())))
            raise
        return retry

    def clone(self, run_id: str) -> AnalysisRun:
        previous = self.repository.load(run_id)
        profile = str(previous.parameters["pipeline_profile"])
        clone = AnalysisRun.new(data_origin=previous.data_origin, input=previous.input, code=self.code_reference(), pipeline_version=previous.pipeline_version, parameters=dict(previous.parameters), models=previous.models, stages=self._queued_stages(1, profile))
        self.repository.create(clone)
        try:
            self.object_storage.copy_input(previous.run_id, clone.run_id)
            with bind_log_context(run_id=clone.run_id, logical_analysis_id=clone.logical_analysis_id, attempt_number=1):
                self.queue.enqueue(ExecutionJob.new(clone.run_id, clone.logical_analysis_id, 1, profile))
                log_event(logger, logging.INFO, "analysis.rerun_queued", "Analysis rerun queued", profile=profile, status="queued")
        except Exception:
            self.repository.save(clone.fail(StructuredError("input_copy_failed", "The source input could not be copied safely.", False, utc_now())))
            raise
        return clone

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
    def code_reference() -> CodeReference:
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
