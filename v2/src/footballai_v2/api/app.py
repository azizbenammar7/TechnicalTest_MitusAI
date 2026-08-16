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
from pydantic import UUID4

from footballai_v2.api.legacy_adapter import LegacyDataError, LegacyRunAdapter
from footballai_v2.api.models import (
    ArtifactListResponse,
    ArtifactView,
    AttemptLink,
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
from footballai_v2.contracts.v1 import ANALYSIS_RUN_CONTRACT_VERSION, AnalysisRun, AnalysisRunStatus, InvalidStatusTransition
from footballai_v2.execution.adapters import profile_catalog
from footballai_v2.execution.cancellation import CancellationStore
from footballai_v2.execution.coordinator import AnalysisCoordinator, ExecutionSettings, UploadValidationError
from footballai_v2.execution.progress import active_stage, overall_progress
from footballai_v2.runtime_health import checks_ready, local_dependency_checks
from footballai_v2.storage import LocalAnalysisRunStore, ManifestConflictError, RunNotFoundError


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
) -> FastAPI:
    """Create a local, read-oriented API bound to one configured run root."""
    execution_settings = settings or ExecutionSettings.from_environment(run_root, queue_root)
    coordinator = AnalysisCoordinator(execution_settings)
    store = coordinator.store
    cancellations = CancellationStore(store.root)
    app = FastAPI(
        title="FootballAi V2 local analysis API",
        version="1.0.0",
        description="Local upload, execution-control, and results API for versioned FootballAi analysis runs.",
    )
    app.state.run_store = store
    origins = _validate_origins(allowed_origins, execution_settings.environment)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
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
            return store.load(str(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Analysis run not found") from exc

    def adapter(run: AnalysisRun) -> LegacyRunAdapter:
        try:
            return LegacyRunAdapter(store, run)
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
        checks = local_dependency_checks(execution_settings.run_root, execution_settings.queue_root)
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
        for run in store.list_runs():
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
                    for item in store.list_runs()
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
        can_clone = False
        try:
            store.input_path(run.run_id); can_clone = True
        except (ManifestConflictError, RunNotFoundError):
            pass
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
            store.save(run.cancel(reason="Cancelled before execution."))
            return OperationResponse(run_id=run.run_id, status="cancelled", message="Queued analysis cancelled.")
        cancellations.request(run.run_id)
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
                        "verified" if store.artifact_integrity(run.run_id, item.artifact_id) else "invalid"
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
