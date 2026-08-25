"""Safe, Container Apps-friendly structured logging for FootballAI services."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import traceback
from datetime import UTC, datetime
from typing import Any, Iterator, Mapping


_CONTEXT_FIELDS = (
    "request_id", "logical_analysis_id", "run_id", "attempt_number", "stage",
    "artifact_id", "job_execution_id", "code_revision",
)
_EVENT_FIELDS = _CONTEXT_FIELDS + (
    "duration_ms", "error_type", "error_code", "status", "method", "path",
    "worker_id", "job_id", "queue", "profile",
)
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|token|secret|password|database_url|connection_string|"
    r"account_key|storage_key|sharedaccesskey|shared_key|sas)", re.IGNORECASE,
)
_REDACTIONS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+:)[^@\s]+(@)"), r"\1[REDACTED]\2"),
    (re.compile(r"(?i)((?:AccountKey|SharedAccessKey|Password|ClientSecret|AccessToken)\s*=\s*)[^;\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:sig|skoid|sktid|skt|ske|sks|skv|sp|sv|se|st|ss|srt)=)[^&\s]+"), r"\1[REDACTED]"),
)
_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "footballai_log_context", default={}
)


def redact(value: Any, *, key: str = "") -> Any:
    """Return a log-safe value without mutating the caller's object."""
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@contextlib.contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Bind supported correlation fields for the current async/thread context."""
    invalid = set(fields) - set(_CONTEXT_FIELDS)
    if invalid:
        raise ValueError(f"unsupported log context fields: {', '.join(sorted(invalid))}")
    merged = {**_context.get(), **{key: value for key, value in fields.items() if value is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current_log_context() -> dict[str, Any]:
    return dict(_context.get())


def _trace_context() -> dict[str, str]:
    try:
        from opentelemetry import trace
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return {}
        return {"trace_id": format(span_context.trace_id, "032x"), "span_id": format(span_context.span_id, "016x")}
    except ImportError:
        return {}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "severity": record.levelname.lower(), "service": self.service,
            "environment": self.environment, "event": getattr(record, "event", "log"),
            "message": redact(record.getMessage()),
        }
        payload.update(_trace_context())
        payload.update({key: redact(value, key=key) for key, value in _context.get().items()})
        for field in _EVENT_FIELDS:
            if hasattr(record, field):
                payload[field] = redact(getattr(record, field), key=field)
        if record.exc_info:
            payload.setdefault("error_type", record.exc_info[0].__name__)
            payload["exception"] = redact("".join(traceback.format_exception(*record.exc_info)))
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class SafeTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return str(redact(super().format(record)))


class CorrelationFilter(logging.Filter):
    """Copy correlation context onto LogRecord for the OTel logging pipeline."""
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context.get().items():
            if not hasattr(record, key):
                setattr(record, key, redact(value, key=key))
        return True


def configure_logging(service: str) -> None:
    """Configure stderr logging; JSON is the default outside local/test."""
    level_name = os.getenv("FOOTBALLAI_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError("FOOTBALLAI_LOG_LEVEL must be a standard Python logging level")
    environment = os.getenv("FOOTBALLAI_ENVIRONMENT", "local").strip().lower()
    default_format = "text" if environment in {"local", "test"} else "json"
    output_format = os.getenv("FOOTBALLAI_LOG_FORMAT", default_format).strip().lower()
    if output_format not in {"json", "text"}:
        raise ValueError("FOOTBALLAI_LOG_FORMAT must be json or text")
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(
        JsonFormatter(service, environment)
        if output_format == "json"
        else SafeTextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def log_event(logger: logging.Logger, level: int, event: str, message: str, *, exc_info: bool = False, **fields: Any) -> None:
    """Emit one named event with an allowlisted field vocabulary."""
    invalid = set(fields) - set(_EVENT_FIELDS)
    if invalid:
        raise ValueError(f"unsupported structured log fields: {', '.join(sorted(invalid))}")
    logger.log(level, message, extra={"event": event, **fields}, exc_info=exc_info)
