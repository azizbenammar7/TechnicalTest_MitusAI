"""Upload, worker, lifecycle operation, and safe API tests."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from footballai_v2.api import create_app
from footballai_v2.execution.coordinator import AnalysisCoordinator, ExecutionSettings, UploadValidationError
from footballai_v2.execution.executor import AnalysisExecutor


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    settings = ExecutionSettings(tmp_path / "runs", tmp_path / "queue", max_upload_bytes=1024 * 1024, allow_test_profiles=True)
    monkeypatch.setattr(AnalysisCoordinator, "probe_video", lambda self, path, extension: {"duration_seconds": 60.0, "container": "mp4", "size_bytes": path.stat().st_size})
    app = create_app(settings.run_root, settings=settings, allowed_origins=("http://localhost:5173",))
    return TestClient(app), settings


def upload(client: TestClient, *, profile="demo_fast", filename="match.mp4", content=b"bounded-video-bytes"):
    return client.post("/api/v1/analyses", files={"video": (filename, content, "video/mp4")}, data={"match_name": "Tunis Derby", "home_team": "Home", "away_team": "Away", "pipeline_profile": profile, "data_origin": "evaluation"})


def execute_one(client: TestClient, settings: ExecutionSettings):
    job = client.app.state.run_store and AnalysisCoordinator(settings).queue.claim("test-worker")
    assert job
    status = AnalysisExecutor(client.app.state.run_store, client.app.state.object_storage, stage_delay_seconds=0).execute(job, "test-worker")
    if status.value in {"succeeded", "partial"}: AnalysisCoordinator(settings).queue.complete(job)
    else: AnalysisCoordinator(settings).queue.fail(job)
    return job, status


def test_profiles_report_demo_and_optional_v1_requirements(workflow):
    client, _ = workflow; response = client.get("/api/v1/pipeline-profiles")
    assert response.status_code == 200
    profiles = {item["profile_id"]: item for item in response.json()["profiles"]}
    assert profiles["demo_fast"]["available"] is True
    assert profiles["demo_fast"]["gpu"] == "not_required"
    assert "V1-compatible analysis" in profiles["v1_compat"]["warnings"][0]


def test_streaming_upload_records_checksum_safe_name_and_provenance(workflow):
    client, _ = workflow; content = b"bounded-video-bytes"; response = upload(client, content=content)
    assert response.status_code == 202
    run_id = response.json()["run_id"]; detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["provenance"]["input_checksum"] == hashlib.sha256(content).hexdigest()
    stored = client.app.state.run_store.input_path(run_id)
    assert stored.name == "source.mp4" and stored.read_bytes() == content
    assert str(stored.parent) not in client.get(f"/api/v1/runs/{run_id}/manifest").text


@pytest.mark.parametrize("filename", ["../match.mp4", "..match.mp4", "/tmp/match.mp4"])
def test_upload_rejects_path_traversal_filenames(workflow, filename):
    client, _ = workflow; response = upload(client, filename=filename)
    assert response.status_code == 422 and response.json()["detail"]["error_code"] == "unsafe_filename"


def test_upload_rejects_empty_unsupported_and_oversized_files(workflow):
    client, settings = workflow
    assert upload(client, content=b"").json()["detail"]["error_code"] == "empty_upload"
    assert upload(client, filename="match.exe").json()["detail"]["error_code"] == "unsupported_extension"
    settings_small = ExecutionSettings(settings.run_root / "small", settings.queue_root / "small", max_upload_bytes=3)
    small = TestClient(create_app(settings_small.run_root, settings=settings_small))
    assert upload(small, content=b"1234").status_code == 413


def test_upload_rejects_incorrect_media_type(workflow):
    client, _ = workflow
    response = client.post("/api/v1/analyses", files={"video": ("match.mp4", b"x", "text/plain")}, data={"match_name": "Match"})
    assert response.status_code == 422 and response.json()["detail"]["error_code"] == "unsupported_media_type"


def test_invalid_metadata_is_bounded_by_typed_form_validation(workflow):
    client, _ = workflow
    response = client.post("/api/v1/analyses", files={"video": ("match.mp4", b"x", "video/mp4")}, data={"match_name": "x" * 161})
    assert response.status_code == 422


def test_worker_advances_all_stages_and_publishes_stable_artifacts(workflow):
    client, settings = workflow; created = upload(client).json(); _, status = execute_one(client, settings)
    assert status.value == "succeeded"
    progress = client.get(created["progress_url"]).json()
    assert progress["overall_progress_percent"] == 100 and progress["active_stage"] is None
    assert all(stage["status"] == "succeeded" for stage in progress["stages"])
    artifacts = client.get(f"/api/v1/runs/{created['run_id']}/artifacts").json()["artifacts"]
    schemas = {item["schema_version"] for item in artifacts}
    assert {"footballai.team-summary/v1", "footballai.track-summary/v1", "footballai.track-detail/v1", "footballai.workload-advisory/v1", "footballai.analysis-diagnostics/v1"} <= schemas


def test_demo_output_is_deterministic_and_changes_with_checksum(workflow):
    client, settings = workflow
    first = upload(client, content=b"same").json(); execute_one(client, settings)
    second = upload(client, content=b"same").json(); execute_one(client, settings)
    third = upload(client, content=b"different").json(); execute_one(client, settings)
    def team(run_id): return client.get(f"/api/v1/runs/{run_id}/summary").json()["distance"]["total_m"]
    assert team(first["run_id"]) == team(second["run_id"])
    assert team(first["run_id"]) != team(third["run_id"])
    assert "Synthetic workflow result" in client.get(f"/api/v1/runs/{first['run_id']}").json()["warnings"][0]


def test_queued_and_running_cancellation_are_persistent(workflow):
    client, settings = workflow
    queued = upload(client).json(); assert client.post(f"/api/v1/runs/{queued['run_id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/v1/runs/{queued['run_id']}/retry").status_code == 409
    running = upload(client).json(); coordinator = AnalysisCoordinator(settings); job = coordinator.queue.claim("worker"); assert job
    run = coordinator.repository.load(running["run_id"]); coordinator.repository.save(run.start(stages=run.stages))
    assert client.post(f"/api/v1/runs/{run.run_id}/cancel").json()["status"] == "running"
    assert AnalysisExecutor(coordinator.repository, coordinator.object_storage, stage_delay_seconds=0).execute(job, "worker").value == "cancelled"
    assert client.get(f"/api/v1/runs/{run.run_id}/progress").json()["can_create_new_from_input"] is True


def test_failure_retry_and_clone_preserve_attempt_rules(workflow):
    client, settings = workflow; failed = upload(client, profile="test_fail").json(); execute_one(client, settings)
    detail = client.get(f"/api/v1/runs/{failed['run_id']}").json()
    assert detail["status"] == "failed" and detail["failure"]["error_code"] == "test_stage_failure"
    retry = client.post(f"/api/v1/runs/{failed['run_id']}/retry"); assert retry.status_code == 202
    retry_id = retry.json()["run_id"]; assert retry.json()["attempt_number"] == 2
    execute_one(client, settings); assert client.get(f"/api/v1/runs/{retry_id}/progress").json()["status"] == "succeeded"
    chain = client.get(f"/api/v1/runs/{retry_id}").json()["attempt_chain"]
    assert [item["status"] for item in chain] == ["failed", "succeeded"]
    clone = client.post(f"/api/v1/runs/{retry_id}/clone").json()
    assert clone["logical_analysis_id"] != failed["logical_analysis_id"] and clone["attempt_number"] == 1


def test_partial_attempt_has_useful_artifacts_and_is_retryable(workflow):
    client, settings = workflow; coordinator = AnalysisCoordinator(settings)
    temp, checksum, size, extension = coordinator.stream_upload(io.BytesIO(b"partial"), "partial.mp4")
    run = coordinator.create_analysis(temp, filename="partial.mp4", checksum=checksum, size_bytes=size, extension=extension, content_type="video/mp4", metadata={"match_name": "Partial", "pipeline_profile": "demo_fast", "data_origin": "evaluation", "force_partial": True})
    execute_one(client, settings)
    progress = client.get(f"/api/v1/runs/{run.run_id}/progress").json()
    assert progress["status"] == "partial" and progress["can_retry"] is True
    assert client.get(f"/api/v1/runs/{run.run_id}/artifacts").json()["artifacts"]


def test_stream_reader_is_bounded_to_chunks(tmp_path):
    coordinator = AnalysisCoordinator(ExecutionSettings(tmp_path / "runs", tmp_path / "queue"))
    class Reader:
        calls = 0
        def read(self, size):
            assert size == 1024 * 1024; self.calls += 1
            return b"x" if self.calls == 1 else b""
    path, checksum, size, extension = coordinator.stream_upload(Reader(), "safe.mp4")
    assert path.exists() and size == 1 and checksum == hashlib.sha256(b"x").hexdigest() and extension == ".mp4"


def test_safe_errors_request_ids_cors_and_uuid_validation(workflow):
    client, _ = workflow
    missing = client.get("/api/v1/runs/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1/progress")
    assert missing.status_code == 404 and "/Users/" not in missing.text and "Traceback" not in missing.text
    assert client.get("/api/v1/runs/not-a-uuid/progress").status_code == 422
    allowed = client.options("/api/v1/analyses", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert client.get("/api/health").headers["x-request-id"]


def test_probe_timeout_is_sanitized(tmp_path, monkeypatch):
    import subprocess
    coordinator = AnalysisCoordinator(ExecutionSettings(tmp_path / "runs", tmp_path / "queue", ffprobe_timeout_seconds=.01))
    path = tmp_path / "video.mp4"; path.write_bytes(b"x")
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired(args[0], .01)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(UploadValidationError, match="timed out") as caught: coordinator.probe_video(path, ".mp4")
    assert caught.value.code == "video_probe_timeout"
