"""FastAPI application factory for the local V2 run store."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import UUID4

from footballai_v2.api.legacy_adapter import LegacyDataError, LegacyRunAdapter
from footballai_v2.api.models import (
    ArtifactListResponse,
    ArtifactView,
    AttemptLink,
    DirectUploadAuthorization,
    DirectUploadAuthorizeRequest,
    DirectUploadAuthorizeResponse,
    DirectUploadFinalizeRequest,
    HealthResponse,
    ManifestResponse,
    OperationResponse,
    PipelineProfile,
    PipelineProfileListResponse,
    PlayerDetailResponse,
    PlayerListResponse,
    ProvenanceView,
    RunDetailResponse,
    RunListItem,
    RunListResponse,
    ProgressResponse,
    QueuedRunResponse,
    ReadinessResponse,
    StageView,
    TeamSummaryResponse,
)
from footballai_v2.contracts.v1 import (
    ANALYSIS_RUN_CONTRACT_VERSION,
    AnalysisRun,
    AnalysisRunStatus,
    DataOrigin,
    InvalidStatusTransition,
)
from footballai_v2.execution.adapters import profile_catalog
from footballai_v2.execution.coordinator import AnalysisCoordinator, ExecutionSettings, UploadValidationError
from footballai_v2.execution.progress import active_stage, overall_progress
from footballai_v2.runtime_health import checks_ready
from footballai_v2.runtime_readiness import build_readiness_probes
from footballai_v2.storage import (
    InvalidStorageObjectError,
    ManifestConflictError,
    RunNotFoundError,
    StorageConflictError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StorageProviderUnavailableError,
)
from footballai_v2.storage.ports import UploadAuthorizer
from footballai_v2.storage.upload_service import DirectUploadService


logger = logging.getLogger("footballai_v2.api")


def _timestamp(value) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _warnings(run: AnalysisRun) -> list[str]:
    value = run.parameters.get("quality_warnings", [])
    return [str(item) for item in value] if isinstance(value, list) else []


def _stage_view(stage) -> StageView:
    payload = stage.to_dict()
    return StageView(
        stage_id=stage.stage_id,
        stage_name=stage.stage_name.value,
        required=stage.required,
        status=stage.status.value,
        progress_percent=float(stage.progress_percent),
        started_at=payload["started_at"],
        finished_at=payload["finished_at"],
        produced_artifact_ids=list(stage.produced_artifact_ids),
        error=payload["error"],
        performance_metrics=dict(stage.performance_metrics),
        message=stage.message,
    )


def _public_input_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme == "file" or (not parsed.scheme and uri.startswith("/")):
        return "local-input://redacted"
    return uri


def _public_manifest(run: AnalysisRun) -> dict[str, Any]:
    payload = run.to_dict()
    payload["input"] = {**payload["input"], "uri": _public_input_uri(run.input.uri)}
    return payload


def _validate_origins(origins: Sequence[str], environment: str) -> list[str]:
    validated = []
    for origin in origins:
        parsed = urlparse(origin)
        local_origin = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        remote_origin = environment in {"staging", "production"} and parsed.scheme == "https"
        if (
            not (local_origin or remote_origin)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                f"CORS origin must be HTTP localhost for local use or HTTPS in staging/production: {origin!r}"
            )
        validated.append(origin.rstrip("/"))
    return validated


def create_app(
    run_root: str | Path,
    *,
    queue_root: str | Path | None = None,
    allowed_origins: Sequence[str] = ("http://localhost:5173",),
    settings: ExecutionSettings | None = None,
    upload_service: DirectUploadService | None = None,
) -> FastAPI:
    """Create a local, read-oriented API bound to one configured run root."""
    execution_settings = settings or ExecutionSettings.from_environment(run_root, queue_root)
    coordinator = AnalysisCoordinator(execution_settings)
    # The API depends only on the ports. It never assumes it shares a filesystem
    # with the worker: lifecycle comes from the repository, artifact bytes from
    # object storage. In local mode both resolve to the fused store; in split mode
    # they are PostgreSQL and Blob respectively.
    repository = coordinator.repository
    object_storage = coordinator.object_storage
    direct_upload = upload_service
    if direct_upload is None and isinstance(object_storage, UploadAuthorizer):
        direct_upload = DirectUploadService(
            object_storage,
            repository,
            coordinator.queue,
            max_upload_bytes=execution_settings.max_upload_bytes,
            code_reference=coordinator.code_reference(),
        )
    # Bounded, cached readiness probes for the configured planes (built once).
    readiness_probes = build_readiness_probes(execution_settings, repository, object_storage)
    app = FastAPI(
        title="FootballAi V2 local analysis API",
        version="1.0.0",
        description="Local upload, execution-control, and results API for versioned FootballAi analysis runs.",
    )
    app.state.run_store = repository
    app.state.object_storage = object_storage
    app.state.direct_upload_service = direct_upload
    origins = _validate_origins(allowed_origins, execution_settings.environment)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
    )

    @app.exception_handler(StorageError)
    async def storage_error_handler(_request: Request, exc: StorageError) -> JSONResponse:
        if isinstance(exc, StorageNotFoundError):
            status, message = 404, "The requested storage object was not found."
        elif isinstance(exc, StorageConflictError):
            status, message = 409, "The storage object conflicts with existing immutable data."
        elif isinstance(exc, StorageIntegrityError):
            status, message = 422, "The storage object failed integrity verification."
        elif isinstance(exc, InvalidStorageObjectError):
            status, message = 422, "The uploaded object is invalid."
        elif isinstance(exc, StorageProviderUnavailableError):
            status, message = 503, "Object storage is temporarily unavailable."
        else:
            status, message = 503, "Object storage is temporarily unavailable."
        return JSONResponse(
            status_code=status,
            content={"detail": {"error_code": exc.code, "message": message}},
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
        logger.info(
            "request_complete request_id=%s method=%s path=%s status=%s elapsed_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    def load_run(run_id: UUID4) -> AnalysisRun:
        try:
            return repository.load(str(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Analysis run not found") from exc

    def adapter(run: AnalysisRun) -> LegacyRunAdapter:
        try:
            return LegacyRunAdapter(object_storage, run)
        except LegacyDataError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="footballai-api",
            contract_version=ANALYSIS_RUN_CONTRACT_VERSION,
        )

    @app.get("/api/ready", response_model=ReadinessResponse)
    def readiness(response: Response) -> ReadinessResponse:
        checks = {name: probe.status() for name, probe in readiness_probes.items()}
        ready = checks_ready(checks)
        if not ready:
            response.status_code = 503
        return ReadinessResponse(
            status="ready" if ready else "not_ready",
            service="footballai-api",
            environment=execution_settings.environment,
            checks=checks,
        )

    @app.get("/api/v1/pipeline-profiles", response_model=PipelineProfileListResponse, summary="List local execution profiles")
    def pipeline_profiles() -> PipelineProfileListResponse:
        return PipelineProfileListResponse(profiles=[PipelineProfile(**item) for item in profile_catalog(include_test=execution_settings.allow_test_profiles)])

    def require_direct_upload() -> DirectUploadService:
        if direct_upload is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "direct_upload_unavailable",
                    "message": "Direct object-storage upload is unavailable in this deployment.",
                },
            )
        return direct_upload

    @app.post(
        "/api/v1/uploads/authorize",
        response_model=DirectUploadAuthorizeResponse,
        summary="Authorize one bounded direct video upload",
    )
    def authorize_direct_upload(
        request: DirectUploadAuthorizeRequest,
    ) -> DirectUploadAuthorizeResponse:
        authorized = require_direct_upload().authorize(content_type=request.content_type)
        grant = authorized.grant
        return DirectUploadAuthorizeResponse(
            run_id=authorized.run_id,
            upload=DirectUploadAuthorization(
                method=grant.method,
                url=grant.url,
                headers=dict(grant.headers),
                max_bytes=grant.max_bytes,
                expires_at=grant.expires_at,
                required_content_type=grant.required_content_type,
            ),
        )

    @app.post(
        "/api/v1/uploads/finalize",
        response_model=QueuedRunResponse,
        status_code=202,
        summary="Verify a direct upload and enqueue an immutable attempt",
    )
    def finalize_direct_upload(request: DirectUploadFinalizeRequest) -> QueuedRunResponse:
        match_name = request.match_name.strip()
        if not match_name:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "invalid_metadata", "message": "Match name cannot be blank."},
            )
        profile = next(
            (
                item
                for item in profile_catalog(include_test=execution_settings.allow_test_profiles)
                if item["profile_id"] == request.pipeline_profile
            ),
            None,
        )
        if profile is None:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "unknown_profile", "message": "Unknown pipeline profile."},
            )
        if not profile["available"]:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "profile_unavailable", "message": "Selected pipeline profile is unavailable."},
            )
        try:
            origin = DataOrigin(request.data_origin)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "invalid_origin", "message": "Invalid data origin."},
            ) from exc
        if origin is DataOrigin.LEGACY_V1:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "invalid_origin", "message": "Legacy origin is reserved for imported artifacts."},
            )
        run = require_direct_upload().finalize(
            str(request.run_id),
            profile=request.pipeline_profile,
            data_origin=origin,
            parameters={
                "match_name": match_name,
                "home_team": request.home_team.strip(),
                "away_team": request.away_team.strip(),
                "competition": request.competition.strip(),
                "match_date": request.match_date.strip(),
                "venue": request.venue.strip(),
                "notes": request.notes.strip(),
                "data_origin": origin.value,
                "quality_warnings": list(profile["warnings"]),
            },
        )
        return _queued_response(run)

    @app.post(
        "/api/v1/analyses", response_model=QueuedRunResponse, status_code=202,
        summary="Stream a validated football video into a queued V2 analysis",
        description="The request stores and probes one bounded video, creates an immutable attempt, and enqueues only a safe job reference. Analysis runs in the separate worker.",
    )
    def create_analysis(
        video: UploadFile = File(description="One MP4, MOV, MKV, or WebM football video."),
        match_name: str = Form(min_length=1, max_length=160),
        home_team: str = Form(default="", max_length=100), away_team: str = Form(default="", max_length=100),
        competition: str = Form(default="", max_length=120), match_date: str = Form(default="", max_length=32),
        venue: str = Form(default="", max_length=160), notes: str = Form(default="", max_length=1000),
        data_origin: str = Form(default="real", max_length=32), pipeline_profile: str = Form(default="demo_fast", max_length=64),
    ) -> QueuedRunResponse:
        temporary = None
        if not match_name.strip():
            raise HTTPException(status_code=422, detail={"error_code": "invalid_metadata", "message": "Match name cannot be blank."})
        try:
            temporary, checksum, size, extension = coordinator.stream_upload(video.file, video.filename or "")
            run = coordinator.create_analysis(
                temporary, filename=video.filename or "", checksum=checksum, size_bytes=size, extension=extension,
                content_type=video.content_type or "application/octet-stream",
                metadata={"match_name": match_name.strip(), "home_team": home_team.strip(), "away_team": away_team.strip(), "competition": competition.strip(), "match_date": match_date.strip(), "venue": venue.strip(), "notes": notes.strip(), "data_origin": data_origin, "pipeline_profile": pipeline_profile},
            )
        except UploadValidationError as exc:
            if temporary is not None: temporary.unlink(missing_ok=True)
            raise HTTPException(status_code=413 if exc.code == "upload_too_large" else 422, detail={"error_code": exc.code, "message": exc.safe_message}) from exc
        return _queued_response(run)

    @app.get("/api/v1/runs", response_model=RunListResponse)
    def list_runs() -> RunListResponse:
        items = []
        for run in repository.list_runs():
            progress = (
                sum(float(stage.progress_percent) for stage in run.stages) / len(run.stages)
                if run.stages
                else 0
            )
            items.append(
                RunListItem(
                    run_id=run.run_id,
                    logical_analysis_id=run.logical_analysis_id,
                    origin=run.data_origin.value,
                    status=run.status.value,
                    attempt_number=run.attempt_number,
                    created_at=_timestamp(run.created_at),
                    pipeline_version=run.pipeline_version,
                    warning_count=len(_warnings(run)),
                    stage_progress_percent=progress,
                )
            )
        return RunListResponse(runs=items)

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetailResponse)
    def run_detail(run_id: UUID4) -> RunDetailResponse:
        run = load_run(run_id)
        chain = [
            AttemptLink(
                run_id=item.run_id,
                attempt_number=item.attempt_number,
                status=item.status.value,
                created_at=_timestamp(item.created_at),
            )
            for item in sorted(
                (
                    item
                    for item in repository.list_runs()
                    if item.logical_analysis_id == run.logical_analysis_id
                ),
                key=lambda item: item.attempt_number,
            )
        ]
        return RunDetailResponse(
            run_id=run.run_id,
            logical_analysis_id=run.logical_analysis_id,
            attempt_number=run.attempt_number,
            previous_attempt_run_id=run.previous_attempt_run_id,
            status=run.status.value,
            origin=run.data_origin.value,
            contract_version=run.contract_version,
            created_at=_timestamp(run.created_at),
            started_at=_timestamp(run.started_at),
            completed_at=_timestamp(run.completed_at),
            partial_reason=run.partial_reason,
            cancellation_reason=run.cancellation_reason,
            failure=run.failure.to_dict() if run.failure else None,
            provenance=ProvenanceView(
                input_uri=_public_input_uri(run.input.uri),
                input_checksum=run.input.sha256,
                input_media_type=run.input.media_type,
                repository=run.code.repository,
                code_revision=run.code.revision,
                code_dirty=run.code.dirty,
                pipeline_version=run.pipeline_version,
                parameters=dict(run.parameters),
                models=[item.to_dict() for item in run.models],
            ),
            warnings=_warnings(run),
            attempt_chain=chain,
            stages=[_stage_view(item) for item in run.stages],
        )

    @app.get("/api/v1/runs/{run_id}/progress", response_model=ProgressResponse, summary="Read weighted stage progress")
    def progress(run_id: UUID4) -> ProgressResponse:
        run = load_run(run_id)
        updated = run.completed_at or next((stage.finished_at or stage.started_at for stage in reversed(run.stages) if stage.finished_at or stage.started_at), None) or run.created_at
        can_clone = object_storage.has_input(run.run_id)
        return ProgressResponse(
            run_id=run.run_id, logical_analysis_id=run.logical_analysis_id, attempt_number=run.attempt_number,
            status=run.status.value, overall_progress_percent=overall_progress(run), active_stage=active_stage(run),
            stages=[_stage_view(item) for item in run.stages], created_at=_timestamp(run.created_at), updated_at=_timestamp(updated),
            can_cancel=run.status in {AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING},
            can_retry=run.status in {AnalysisRunStatus.FAILED, AnalysisRunStatus.PARTIAL},
            can_create_new_from_input=can_clone,
        )

    @app.post("/api/v1/runs/{run_id}/cancel", response_model=OperationResponse, summary="Persistently request cancellation")
    def cancel(run_id: UUID4) -> OperationResponse:
        run = load_run(run_id)
        if run.status.is_terminal:
            raise HTTPException(status_code=409, detail="Terminal analysis attempts cannot be cancelled")
        if run.status is AnalysisRunStatus.QUEUED:
            coordinator.queue.cancel(run.run_id)
            repository.save(run.cancel(reason="Cancelled before execution."))
            return OperationResponse(run_id=run.run_id, status="cancelled", message="Queued analysis cancelled.")
        repository.request_cancellation(run.run_id)
        return OperationResponse(run_id=run.run_id, status="running", message="Cancellation requested; the worker will stop at a safe checkpoint.")

    @app.post("/api/v1/runs/{run_id}/retry", response_model=QueuedRunResponse, status_code=202, summary="Create a new retry attempt")
    def retry(run_id: UUID4) -> QueuedRunResponse:
        load_run(run_id)
        try:
            return _queued_response(coordinator.retry(str(run_id)))
        except InvalidStatusTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ManifestConflictError, OSError) as exc:
            raise HTTPException(status_code=409, detail="The source input could not be reused safely.") from exc

    @app.post("/api/v1/runs/{run_id}/clone", response_model=QueuedRunResponse, status_code=202, summary="Create a new logical analysis from uploaded input")
    def clone(run_id: UUID4) -> QueuedRunResponse:
        load_run(run_id)
        try:
            return _queued_response(coordinator.clone(str(run_id)))
        except (ManifestConflictError, RunNotFoundError, OSError) as exc:
            raise HTTPException(status_code=409, detail="This run has no reusable uploaded input.") from exc

    @app.get("/api/v1/runs/{run_id}/manifest", response_model=ManifestResponse)
    def manifest(run_id: UUID4) -> ManifestResponse:
        return ManifestResponse(manifest=_public_manifest(load_run(run_id)))

    @app.get("/api/v1/runs/{run_id}/artifacts", response_model=ArtifactListResponse)
    def artifacts(run_id: UUID4) -> ArtifactListResponse:
        run = load_run(run_id)
        return ArtifactListResponse(
            run_id=run.run_id,
            artifacts=[
                ArtifactView(
                    artifact_id=item.artifact_id,
                    name=item.name,
                    category=item.category.value,
                    relative_path=item.relative_path,
                    media_type=item.media_type,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                    schema_version=item.schema_version,
                    integrity_state=(
                        "verified" if object_storage.artifact_integrity(run.run_id, item.artifact_id) else "invalid"
                    ),
                )
                for item in run.artifacts
            ],
        )

    @app.get("/api/v1/runs/{run_id}/summary", response_model=TeamSummaryResponse)
    def summary(run_id: UUID4) -> TeamSummaryResponse:
        run = load_run(run_id)
        return adapter(run).team_summary()

    @app.get("/api/v1/runs/{run_id}/players", response_model=PlayerListResponse)
    def players(run_id: UUID4) -> PlayerListResponse:
        run = load_run(run_id)
        return adapter(run).player_list()

    @app.get("/api/v1/runs/{run_id}/players/{player_id}", response_model=PlayerDetailResponse)
    def player(run_id: UUID4, player_id: int) -> PlayerDetailResponse:
        run = load_run(run_id)
        try:
            return adapter(run).player_detail(player_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Player track not found") from exc

    return app


def _queued_response(run: AnalysisRun) -> QueuedRunResponse:
    return QueuedRunResponse(run_id=run.run_id, logical_analysis_id=run.logical_analysis_id, attempt_number=run.attempt_number, status=run.status.value, progress_url=f"/api/v1/runs/{run.run_id}/progress")
    DirectUploadAuthorization,
    DirectUploadAuthorizeRequest,
    DirectUploadAuthorizeResponse,
    DirectUploadFinalizeRequest,
