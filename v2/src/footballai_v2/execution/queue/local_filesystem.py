"""Process-safe filesystem queue for local development, not distributed use."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from footballai_v2.contracts.v1 import AnalysisRunStatus, utc_now, validate_run_id
from footballai_v2.execution.contracts import ExecutionJob
from footballai_v2.storage import AnalysisRepository, RunNotFoundError


class DuplicateJobError(FileExistsError):
    pass


class QueueRecordError(ValueError):
    pass


class LocalFilesystemQueue:
    """Atomic-rename queue whose records contain safe IDs and profile metadata only."""

    STATES = ("queued", "claimed", "completed", "failed", "cancelled")
    MAX_RECORD_BYTES = 16 * 1024

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("queue root cannot be a symlink")
        for state in self.STATES:
            (self.root / state).mkdir(exist_ok=True)
        (self.root / "run-index").mkdir(exist_ok=True)

    def _path(self, state: str, job_id: str) -> Path:
        if state not in self.STATES:
            raise ValueError("invalid queue state")
        try:
            canonical = str(__import__("uuid").UUID(job_id))
        except (ValueError, AttributeError) as exc:
            raise QueueRecordError("invalid job ID") from exc
        if canonical != job_id:
            raise QueueRecordError("invalid job ID")
        return self.root / state / f"{canonical}.json"

    def enqueue(self, job: ExecutionJob) -> None:
        reservation = self.root / "run-index" / job.run_id
        try:
            reservation_fd = os.open(reservation, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(reservation_fd)
        except FileExistsError as exc:
            raise DuplicateJobError(job.run_id) from exc
        destination = self._path("queued", job.job_id)
        payload = (json.dumps(job.to_dict(), sort_keys=True) + "\n").encode()
        fd, temporary = tempfile.mkstemp(prefix=".enqueue-", dir=self.root / "queued")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise DuplicateJobError(job.job_id) from exc
        except Exception:
            reservation.unlink(missing_ok=True)
            raise
        finally:
            Path(temporary).unlink(missing_ok=True)

    def claim(self, worker_id: str) -> ExecutionJob | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("invalid worker ID")
        for source in sorted((self.root / "queued").glob("*.json")):
            try:
                job = self._read(source)
            except FileNotFoundError:
                # Another worker won the atomic rename after this directory scan.
                continue
            except QueueRecordError:
                if source.exists():
                    self._move_raw(source, self.root / "failed" / source.name)
                continue
            claimed = replace(job, claimed_at=utc_now(), worker_id=worker_id)
            destination = self._path("claimed", job.job_id)
            try:
                os.replace(source, destination)
            except FileNotFoundError:
                continue
            self._write_replace(destination, claimed)
            return claimed
        return None

    def complete(self, job: ExecutionJob) -> None:
        self._terminal_move(job, "completed")

    def fail(self, job: ExecutionJob) -> None:
        self._terminal_move(job, "failed")

    def cancel(self, run_id: str) -> bool:
        validate_run_id(run_id)
        for state in ("queued", "claimed"):
            for source in (self.root / state).glob("*.json"):
                try:
                    job = self._read(source)
                except QueueRecordError:
                    continue
                if job.run_id == run_id:
                    self._move_raw(source, self._path("cancelled", job.job_id))
                    return True
        return False

    def recover_abandoned(self, timeout_seconds: float, store: AnalysisRepository) -> int:
        cutoff = utc_now() - timedelta(seconds=max(timeout_seconds, 1))
        recovered = 0
        for source in (self.root / "claimed").glob("*.json"):
            try:
                job = self._read(source)
                run = store.load(job.run_id)
            except (QueueRecordError, RunNotFoundError):
                self._move_raw(source, self.root / "failed" / source.name)
                continue
            if run.status.is_terminal:
                state = "completed" if run.status in {AnalysisRunStatus.SUCCEEDED, AnalysisRunStatus.PARTIAL} else run.status.value
                if state not in self.STATES:
                    state = "failed"
                self._move_raw(source, self._path(state, job.job_id))
            elif job.claimed_at and job.claimed_at < cutoff:
                reset = replace(job, claimed_at=None, worker_id=None)
                destination = self._path("queued", job.job_id)
                self._move_raw(source, destination)
                self._write_replace(destination, reset)
                recovered += 1
        return recovered

    def _terminal_move(self, job: ExecutionJob, state: str) -> None:
        destination = self._path(state, job.job_id)
        if destination.exists():
            return
        source = self._path("claimed", job.job_id)
        if source.exists():
            os.replace(source, destination)

    def _read(self, path: Path) -> ExecutionJob:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > self.MAX_RECORD_BYTES:
            raise QueueRecordError("unsafe queue record")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return ExecutionJob.from_dict(value)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise QueueRecordError("malformed queue record") from exc

    @staticmethod
    def _move_raw(source: Path, destination: Path) -> None:
        if destination.exists():
            source.unlink(missing_ok=True)
        else:
            os.replace(source, destination)

    @staticmethod
    def _write_replace(path: Path, job: ExecutionJob) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(job.to_dict(), handle, sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
