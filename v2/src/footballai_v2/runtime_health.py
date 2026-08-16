"""Truthful local-adapter capability checks used by readiness and health probes."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def _writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".readiness-", dir=path)
        os.close(fd)
        Path(temporary).unlink()
        return True
    except OSError:
        return False


def local_dependency_checks(run_root: Path, queue_root: Path) -> dict[str, str]:
    return {
        "run_storage": "ready" if _writable_directory(run_root) else "unavailable",
        "queue": "ready" if _writable_directory(queue_root) else "unavailable",
        "video_probe": "ready" if shutil.which("ffprobe") else "unavailable",
    }


def checks_ready(checks: dict[str, str]) -> bool:
    return bool(checks) and all(value == "ready" for value in checks.values())
