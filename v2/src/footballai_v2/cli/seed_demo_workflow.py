"""Create one tiny generated demo_fast run through the real local queue."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from footballai_v2.execution.coordinator import AnalysisCoordinator, ExecutionSettings
from footballai_v2.execution.executor import AnalysisExecutor


def main() -> None:
    settings = ExecutionSettings.from_environment(); coordinator = AnalysisCoordinator(settings)
    if any(run.status.value == "succeeded" and run.parameters.get("pipeline_profile") == "demo_fast" for run in coordinator.repository.list_runs()):
        return
    with tempfile.TemporaryDirectory(prefix="footballai-demo-") as temporary:
        fixture = Path(temporary) / "generated-demo.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x195f3b:s=320x180:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(fixture)], shell=False, check=True, timeout=30)
        with fixture.open("rb") as source:
            uploaded, checksum, size, extension = coordinator.stream_upload(source, fixture.name)
        run = coordinator.create_analysis(uploaded, filename=fixture.name, checksum=checksum, size_bytes=size, extension=extension, content_type="video/mp4", metadata={"match_name": "Generated local workflow demo", "home_team": "Demo Home", "away_team": "Demo Away", "competition": "Local evaluation", "match_date": "", "venue": "Generated fixture", "notes": "Created locally without private footage.", "data_origin": "evaluation", "pipeline_profile": "demo_fast"})
        job = coordinator.queue.claim("demo-seed"); assert job and job.run_id == run.run_id
        status = AnalysisExecutor(coordinator.repository, coordinator.object_storage, stage_delay_seconds=0).execute(job, "demo-seed")
        if status.value == "succeeded": coordinator.queue.complete(job)
        else: coordinator.queue.fail(job)


if __name__ == "__main__":
    main()
