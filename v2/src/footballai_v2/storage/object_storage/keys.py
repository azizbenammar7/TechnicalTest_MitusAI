"""Deterministic, run-scoped object keys shared by every object-storage adapter.

Every key is confined to ``runs/<run_id>/`` so an upload authorization can be
scoped to exactly one object and a browser can never address another run's data.
"""

from __future__ import annotations

from footballai_v2.contracts.v1 import validate_run_id
from footballai_v2.contracts.v1.analysis_run import validate_relative_artifact_path

INPUT_BLOB_NAME = "source"


def run_prefix(run_id: str) -> str:
    return f"runs/{validate_run_id(run_id)}/"


def input_prefix(run_id: str) -> str:
    return f"{run_prefix(run_id)}input/"


def input_object_key(run_id: str, extension: str) -> str:
    if not extension.startswith(".") or "/" in extension or "\\" in extension:
        raise ValueError("extension must be a leading-dot suffix such as '.mp4'")
    return f"{input_prefix(run_id)}{INPUT_BLOB_NAME}{extension}"


def artifact_object_key(run_id: str, relative_path: str) -> str:
    normalized = validate_relative_artifact_path(relative_path)
    return f"{run_prefix(run_id)}{normalized}"
