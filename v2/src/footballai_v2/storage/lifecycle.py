"""Provider-neutral analysis-attempt lifecycle rules.

These rules are the single authority for how one immutable attempt may change
state, independent of whether the record lives in a local manifest file or a
PostgreSQL control-plane row. Keeping them here means the local and cloud
repositories cannot silently diverge on immutability, the retry chain, or
provenance.
"""

from __future__ import annotations

from footballai_v2.contracts.v1 import AnalysisRun, AnalysisRunStatus

# A run namespace is created queued; from there only these forward transitions
# are legal. Terminal states never appear as a source key: they are immutable.
ALLOWED_STATUS_TRANSITIONS: dict[AnalysisRunStatus, frozenset[AnalysisRunStatus]] = {
    AnalysisRunStatus.QUEUED: frozenset(
        {
            AnalysisRunStatus.QUEUED,
            AnalysisRunStatus.RUNNING,
            AnalysisRunStatus.FAILED,
            AnalysisRunStatus.CANCELLED,
        }
    ),
    AnalysisRunStatus.RUNNING: frozenset(
        {
            AnalysisRunStatus.RUNNING,
            AnalysisRunStatus.SUCCEEDED,
            AnalysisRunStatus.PARTIAL,
            AnalysisRunStatus.FAILED,
            AnalysisRunStatus.CANCELLED,
        }
    ),
}

# Provenance and identity that one attempt fixes at creation and can never edit.
IMMUTABLE_PROVENANCE_FIELDS: tuple[str, ...] = (
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


class ManifestTransitionError(RuntimeError):
    """Raised when a stored attempt would violate its lifecycle or immutability."""


def ensure_creatable(run: AnalysisRun) -> None:
    """A new attempt namespace must be persisted in queued state."""
    if run.status is not AnalysisRunStatus.QUEUED:
        raise ManifestTransitionError("a run namespace must be created in queued state")


def ensure_transition_allowed(current: AnalysisRun, updated: AnalysisRun) -> None:
    """Validate that ``updated`` is a legal successor of the stored ``current``.

    Enforces three non-negotiable guarantees: terminal attempts are immutable,
    only allow-listed status transitions are legal, and provenance/identity can
    never change after the namespace is created.
    """
    if current.status.is_terminal:
        raise ManifestTransitionError("terminal attempts are immutable")
    permitted = ALLOWED_STATUS_TRANSITIONS.get(current.status)
    if permitted is None or updated.status not in permitted:
        raise ManifestTransitionError(
            f"cannot replace {current.status.value} attempt with {updated.status.value}"
        )
    changed = [
        name
        for name in IMMUTABLE_PROVENANCE_FIELDS
        if getattr(current, name) != getattr(updated, name)
    ]
    if changed:
        raise ManifestTransitionError(
            "run provenance and identity cannot change after namespace creation"
        )
