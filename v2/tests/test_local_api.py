"""Local FastAPI safety and legacy-response tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from footballai_v2.api import create_app
from footballai_v2.contracts.v1 import (
    AnalysisRun,
    CodeReference,
    DataOrigin,
    InputReference,
)
from footballai_v2.importers import LegacyV1Importer
from footballai_v2.storage import LocalAnalysisRunStore


RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
MISSING_RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"


def create_legacy_source(root: Path) -> Path:
    source = root / "legacy-source"
    source.mkdir()
    (source / "meta.json").write_text(
        json.dumps({"duration_s": 90.0, "effective_fps": 5.0}), encoding="utf-8"
    )
    (source / "player_summary.json").write_text(
        json.dumps(
            {
                "match_duration_s": 90.0,
                "total_players": 2,
                "players": {
                    "12": {
                        "track_id": 12,
                        "total_distance_m": 100.0,
                        "mean_speed_ms": 2.0,
                        "peak_speed_ms": 6.0,
                        "total_sprints": 1,
                        "active_time_s": 50.0,
                        "coverage_frac": 0.5,
                        "blocks_present": [0, 1],
                        "heatmap": [[0.2, 0.8], [0.5, 0.5]],
                        "speed_timeline": {"0": 1.5, "1": 2.5},
                    },
                    "99": {
                        "track_id": 99,
                        "total_distance_m": 50.0,
                        "mean_speed_ms": 1.0,
                        "peak_speed_ms": 3.0,
                        "total_sprints": 0,
                        "active_time_s": 30.0,
                        "coverage_frac": 0.2,
                        "blocks_present": [0],
                        "heatmap": [[1.0]],
                        "speed_timeline": {"0": 1.0},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (source / "risk_scores.json").write_text(
        json.dumps(
            {
                "12": {
                    "track_id": 12,
                    "risk_score": 0.4,
                    "risk_flag": "MEDIUM",
                    "reason": "Approximate legacy indicator.",
                    "fatigue_indicators": {"h1_distance_m": 40, "h2_distance_m": 60},
                    "score_breakdown": {"distance_change": 0.2},
                },
                "99": {
                    "track_id": 99,
                    "risk_score": None,
                    "risk_flag": "INSUFFICIENT",
                    "reason": "Coverage is incomplete.",
                    "fatigue_indicators": {},
                    "score_breakdown": {},
                },
            }
        ),
        encoding="utf-8",
    )
    (source / "player_stats.parquet").write_bytes(b"PAR1player-statsPAR1")
    (source / "raw_tracks.parquet").write_bytes(b"PAR1raw-tracksPAR1")
    return source


@pytest.fixture
def api_context(tmp_path):
    source = create_legacy_source(tmp_path)
    run_root = tmp_path / "runs"
    store = LocalAnalysisRunStore(run_root)
    importer = LegacyV1Importer(
        store,
        CodeReference("https://github.com/example/FootballAi", "8" * 40),
    )
    run = importer.import_directory(source, run_id=RUN_ID)
    app = create_app(run_root, allowed_origins=("http://localhost:5173",))
    return TestClient(app), run, store


def test_health_has_contract_and_request_diagnostics(api_context):
    client, _, _ = api_context
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "footballai-api",
        "contract_version": "footballai.analysis-run/v1",
    }
    assert response.headers["x-request-id"]
    assert float(response.headers["x-response-time-ms"]) >= 0


def test_readiness_checks_real_local_dependencies(api_context):
    client, _, _ = api_context
    response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "footballai-api",
        "environment": "local",
        "checks": {
            "run_storage": "ready",
            "queue": "ready",
            "video_probe": "ready",
        },
    }


def test_list_runs_exposes_stable_overview_fields(api_context):
    client, run, _ = api_context
    response = client.get("/api/v1/runs")
    assert response.status_code == 200
    item = response.json()["runs"][0]
    assert item["run_id"] == run.run_id
    assert item["logical_analysis_id"] == run.logical_analysis_id
    assert item["origin"] == "legacy_v1"
    assert item["status"] == "succeeded"
    assert item["warning_count"] == 8
    assert item["stage_progress_percent"] > 0


def test_invalid_uuid_and_missing_run_are_safe(api_context):
    client, _, _ = api_context
    invalid = client.get("/api/v1/runs/not-a-uuid")
    missing = client.get(f"/api/v1/runs/{MISSING_RUN_ID}")
    assert invalid.status_code == 422
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Analysis run not found"


def test_run_detail_has_attempt_provenance_stages_and_warnings(api_context):
    client, run, _ = api_context
    payload = client.get(f"/api/v1/runs/{run.run_id}").json()
    assert payload["attempt_chain"] == [
        {
            "run_id": run.run_id,
            "attempt_number": 1,
            "status": "succeeded",
            "created_at": payload["created_at"],
        }
    ]
    assert payload["provenance"]["input_checksum"] == run.input.sha256
    assert payload["stages"][0]["stage_name"] == "ingestion"
    assert len(payload["warnings"]) == 8


def test_manifest_and_artifacts_never_expose_internal_absolute_paths(api_context, tmp_path):
    client, run, store = api_context
    manifest = client.get(f"/api/v1/runs/{run.run_id}/manifest")
    artifacts = client.get(f"/api/v1/runs/{run.run_id}/artifacts")
    assert manifest.status_code == 200
    assert artifacts.status_code == 200
    combined = manifest.text + artifacts.text
    assert str(tmp_path) not in combined
    assert all(item["integrity_state"] == "verified" for item in artifacts.json()["artifacts"])
    assert all(not item["relative_path"].startswith("/") for item in artifacts.json()["artifacts"])

    local_input = AnalysisRun.new(
        run_id="cccccccc-cccc-4ccc-8ccc-ccccccccccc1",
        data_origin=DataOrigin.REAL,
        input=InputReference(f"file://{tmp_path}/private-match.mp4", "a" * 64, "video/mp4"),
        code=CodeReference("https://github.com/example/FootballAi", "8" * 40),
        pipeline_version="2.0.0",
    )
    store.create(local_input)
    redacted = client.get(f"/api/v1/runs/{local_input.run_id}/manifest")
    assert redacted.json()["manifest"]["input"]["uri"] == "local-input://redacted"
    assert str(tmp_path) not in redacted.text


def test_team_summary_transforms_legacy_metrics_and_warnings(api_context):
    client, run, _ = api_context
    payload = client.get(f"/api/v1/runs/{run.run_id}/summary").json()
    assert payload["legacy"] is True
    assert payload["total_tracks"] == 2
    assert payload["scored_tracks"] == 1
    assert payload["insufficient_tracks"] == 1
    assert payload["distance"]["total_m"] == 150
    assert payload["advisory_distribution"] == {"INSUFFICIENT": 1, "MEDIUM": 1}
    assert len(payload["blocks"]) == 2
    assert len(payload["warnings"]) == 8


def test_player_list_uses_unverified_track_labels(api_context):
    client, run, _ = api_context
    payload = client.get(f"/api/v1/runs/{run.run_id}/players").json()
    assert len(payload["players"]) == 2
    assert payload["players"][0]["label"] == "Legacy track 12"
    assert payload["players"][0]["identity_verified"] is False
    assert payload["players"][0]["advisory_level"] == "MEDIUM"


def test_player_detail_exposes_timelines_heatmap_and_advisory_label(api_context):
    client, run, _ = api_context
    response = client.get(f"/api/v1/runs/{run.run_id}/players/12")
    assert response.status_code == 200
    payload = response.json()
    assert payload["label"] == "Unverified player track 12"
    assert payload["identity_verified"] is False
    assert payload["active_span_seconds"] == 50
    assert len(payload["heatmap"]) == 2
    assert [item["minute"] for item in payload["speed_timeline"]] == [0, 15]
    assert payload["distance_timeline"][-1]["value"] == 100
    assert payload["advisory"]["label"] == "Workload and Fatigue Advisory"
    assert payload["advisory"]["advisory_only"] is True


def test_missing_player_track_is_404(api_context):
    client, run, _ = api_context
    response = client.get(f"/api/v1/runs/{run.run_id}/players/404")
    assert response.status_code == 404
    assert response.json()["detail"] == "Player track not found"


def test_registered_artifact_corruption_returns_safe_422(api_context):
    client, run, store = api_context
    item = next(artifact for artifact in run.artifacts if artifact.artifact_id == "legacy-player-summary")
    store.artifact_path(run.run_id, item.relative_path).write_bytes(b"x" * item.size_bytes)
    response = client.get(f"/api/v1/runs/{run.run_id}/summary")
    assert response.status_code == 422
    assert "unavailable or malformed" in response.json()["detail"]
    assert "Traceback" not in response.text


def test_cors_allows_only_configured_local_dashboard_origin(api_context):
    client, _, _ = api_context
    allowed = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    denied = client.get("/api/health", headers={"Origin": "https://example.com"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize(
    "origin",
    ["https://localhost:5173", "https://example.com", "http://user@localhost:5173"],
)
def test_application_rejects_nonlocal_cors_configuration(tmp_path, origin):
    with pytest.raises(ValueError, match="localhost"):
        create_app(tmp_path / "runs", allowed_origins=(origin,))
