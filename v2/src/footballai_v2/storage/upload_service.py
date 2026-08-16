"""Provider-neutral direct-to-object-storage upload workflow.

Replaces the "browser streams 8 GiB through FastAPI" path with:

1. ``authorize`` -- mint a run id and return a bounded, short-lived, write-only,
   single-object upload grant. The browser uploads straight to object storage.
2. ``finalize`` -- verify the uploaded object (existence, size bound, allowed
   content type, server-computed checksum), then create the immutable analysis
   attempt in the control plane and enqueue exactly one job.

The service depends only on the ports (``UploadAuthorizer``,
``AnalysisRepository``-shaped ``create``, and ``JobQueue``), never on Azure. A
duplicate ``finalize`` for the same run is idempotent: it returns the existing
attempt instead of creating a second one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Mapping

from footballai_v2.contracts.v1 import (
    AnalysisRun,
    DataOrigin,
    InputReference,
    JsonValue,
    StageExecution,
    StageName,
    StageStatus,
)
from footballai_v2.execution.contracts import ExecutionJob
from footballai_v2.storage.local_analysis_runs import RunAlreadyExistsError
from footballai_v2.storage.errors import InvalidStorageObjectError
from footballai_v2.storage.ports import UploadGrant

VIDEO_CONTENT_TYPES: tuple[str, ...] = (
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
)


@dataclass(frozen=True, slots=True)
class AuthorizedUpload:
    run_id: str
    grant: UploadGrant


class DirectUploadService:
    def __init__(
        self,
        authorizer,
        repository,
        queue,
        *,
        max_upload_bytes: int,
        code_reference,
        expires_seconds: int = 900,
    ) -> None:
        self._authorizer = authorizer
        self._repository = repository
        self._queue = queue
        self._max_upload_bytes = max_upload_bytes
        self._code_reference = code_reference
        self._expires_seconds = expires_seconds

    def authorize(self, *, content_type: str) -> AuthorizedUpload:
        if content_type not in VIDEO_CONTENT_TYPES:
            raise InvalidStorageObjectError("unsupported upload content type")
        run_id = str(uuid.uuid4())
        grant = self._authorizer.authorize_upload(
            run_id,
            content_type=content_type,
            max_bytes=self._max_upload_bytes,
            expires_seconds=self._expires_seconds,
        )
        return AuthorizedUpload(run_id=run_id, grant=grant)

    def finalize(
        self,
        run_id: str,
        *,
        profile: str = "demo_fast",
        data_origin: DataOrigin = DataOrigin.REAL,
        parameters: Mapping[str, JsonValue] | None = None,
    ) -> AnalysisRun:
        finalized = self._authorizer.finalize_upload(
            run_id,
            max_bytes=self._max_upload_bytes,
            allowed_content_types=VIDEO_CONTENT_TYPES,
        )
        run = AnalysisRun.new(
            run_id=run_id,
            data_origin=data_origin,
            input=InputReference(
                f"blob://{finalized.object_reference}",
                finalized.sha256,
                finalized.content_type,
            ),
            code=self._code_reference,
            pipeline_version=f"{profile}/1.0.0",
            parameters={**dict(parameters or {}), "pipeline_profile": profile},
            stages=_queued_stages(1),
        )
        try:
            self._repository.create(run)
        except RunAlreadyExistsError:
            # At-least-once finalize: the attempt already exists; stay idempotent.
            return self._repository.load(run_id)
        self._queue.enqueue(
            ExecutionJob.new(run.run_id, run.logical_analysis_id, run.attempt_number, profile)
        )
        return run


def _queued_stages(attempt: int) -> tuple[StageExecution, ...]:
    return tuple(
        StageExecution(name.value, name, True, StageStatus.QUEUED, 0, attempt)
        for name in StageName
    )
