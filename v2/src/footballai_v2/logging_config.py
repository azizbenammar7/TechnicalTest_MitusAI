"""Container-friendly structured logging shared by API and worker processes."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "environment": self.environment,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def configure_logging(service: str) -> None:
    """Configure stdout/stderr logs once without exposing exception internals."""
    level_name = os.getenv("FOOTBALLAI_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError("FOOTBALLAI_LOG_LEVEL must be a standard Python logging level")
    environment = os.getenv("FOOTBALLAI_ENVIRONMENT", "local")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service, environment))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
