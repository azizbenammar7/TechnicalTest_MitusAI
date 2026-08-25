"""Docker health-check entry points that do not require curl."""

from __future__ import annotations

import json
import os
import sys
from urllib.request import urlopen

from footballai_v2.execution.coordinator import ExecutionSettings
from footballai_v2.runtime_health import checks_ready, local_dependency_checks


def main() -> None:
    target = sys.argv[1] if len(sys.argv) == 2 else ""
    if target == "api":
        port = int(os.getenv("FOOTBALLAI_API_PORT", "8000"))
        # False positive: scheme + loopback host are hardcoded and `port` is an
        # int, so no attacker-controlled value (and no file:// scheme) can reach
        # urlopen. This is the container's own liveness probe against itself.
        with urlopen(f"http://127.0.0.1:{port}/api/ready", timeout=2) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            payload = json.load(response)
        if response.status != 200 or payload.get("status") != "ready":
            raise SystemExit(1)
        return
    if target == "worker":
        settings = ExecutionSettings.from_environment()
        if not checks_ready(local_dependency_checks(settings.run_root, settings.queue_root)):
            raise SystemExit(1)
        return
    raise SystemExit("usage: python -m footballai_v2.healthcheck api|worker")


if __name__ == "__main__":
    main()
