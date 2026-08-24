"""Bounded, cached capability probes for configured production dependencies.

Readiness must reflect whether a dependency can actually serve requests without
turning into an expensive operation on every probe. Each check is wrapped in a
short TTL cache so a burst of readiness requests performs at most one real check
per interval. Liveness (``/health``) never depends on these -- a transient cloud
outage must not restart a healthy process.
"""

from __future__ import annotations

import time
from typing import Callable


class CapabilityProbe:
    """Cache a boolean capability check for a bounded interval."""

    def __init__(self, check: Callable[[], bool], *, ttl_seconds: float = 10.0) -> None:
        self._check = check
        self._ttl = ttl_seconds
        # None (not 0.0) forces the first status() call to run the check: on a
        # freshly-booted host time.monotonic() can be smaller than the TTL, and
        # a 0.0 sentinel would then wrongly report "unavailable" until the clock
        # advanced past the TTL.
        self._checked_at: float | None = None
        self._ready = False

    def status(self) -> str:
        now = time.monotonic()
        if self._checked_at is None or now - self._checked_at >= self._ttl:
            try:
                self._ready = bool(self._check())
            except Exception:  # noqa: BLE001 - readiness never raises
                self._ready = False
            self._checked_at = now
        return "ready" if self._ready else "unavailable"


def postgres_capability(engine) -> bool:
    """True when PostgreSQL answers and its schema is at the expected revision."""
    from sqlalchemy import text

    from footballai_v2.storage.postgres.schema import SCHEMA_REVISION

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        if not connection.dialect.has_table(connection, "alembic_version"):
            return False
        row = connection.exec_driver_sql("SELECT version_num FROM alembic_version").first()
    return bool(row) and row[0] == SCHEMA_REVISION


def blob_capability(container_client) -> bool:
    """True when the configured private Blob container is reachable."""
    container_client.get_container_properties()
    return True


def build_readiness_probes(settings, repository, object_storage) -> dict[str, "CapabilityProbe"]:
    """Assemble bounded, cached readiness probes for the *configured* planes.

    Readiness reflects the backends actually in use: a split deployment probes
    PostgreSQL and the Blob container; a local deployment probes writable
    directories. Each probe reuses the already-composed adapter's connection and
    is cached, so a burst of ``/ready`` calls performs at most one real check per
    interval and never an expensive write. Liveness (``/health``) is unaffected.
    """
    import shutil
    from pathlib import Path

    from footballai_v2.runtime_health import _writable_directory

    probes: dict[str, CapabilityProbe] = {}

    # Control plane.
    if settings.database_backend == "postgres":
        engine = getattr(repository, "_engine", None)
        probes["database"] = CapabilityProbe(lambda: engine is not None and postgres_capability(engine))
    else:
        run_root = Path(settings.run_root)
        probes["run_storage"] = CapabilityProbe(lambda: _writable_directory(run_root))

    # Data plane (local artifact bytes share the run root already probed above).
    if settings.object_storage_backend == "azure_blob":
        client = getattr(object_storage, "_client", None)
        probes["object_storage"] = CapabilityProbe(lambda: client is not None and blob_capability(client))

    # Delivery plane.
    if settings.queue_backend == "local":
        queue_root = Path(settings.queue_root)
        probes["queue"] = CapabilityProbe(lambda: _writable_directory(queue_root))
    else:
        # Service Bus configuration is validated at composition; a bounded live
        # probe is deferred to real Azure validation (see docs).
        probes["queue"] = CapabilityProbe(lambda: True)

    # Multipart ingestion depends on ffprobe; the direct-upload path does not.
    probes["video_probe"] = CapabilityProbe(lambda: shutil.which("ffprobe") is not None)
    return probes
