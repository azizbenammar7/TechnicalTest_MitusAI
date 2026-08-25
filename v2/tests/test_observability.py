from __future__ import annotations

import json
import logging

import pytest

from footballai_v2.logging_config import JsonFormatter, bind_log_context, log_event, redact
from footballai_v2.observability import _metric_attributes


def test_json_logging_has_correlation_schema_and_no_legacy_level_field() -> None:
    formatter = JsonFormatter("footballai-api", "staging")
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "completed", (), None)
    record.event = "analysis.completed"
    record.duration_ms = 12.5
    with bind_log_context(
        request_id="req-1",
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        logical_analysis_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        attempt_number=2,
    ):
        payload = json.loads(formatter.format(record))
    assert payload == {
        "timestamp": payload["timestamp"],
        "severity": "info",
        "service": "footballai-api",
        "environment": "staging",
        "event": "analysis.completed",
        "message": "completed",
        "request_id": "req-1",
        "run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "logical_analysis_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "attempt_number": 2,
        "duration_ms": 12.5,
    }


@pytest.mark.parametrize(
    "unsafe",
    [
        "Authorization: Bearer eyJhbGciOi.super-secret.signature",
        "postgresql+psycopg://footballai:db-password@example.postgres.database.azure.com/db",
        "DefaultEndpointsProtocol=https;AccountKey=storage-secret;EndpointSuffix=core.windows.net",
        "https://example.blob.core.windows.net/c/o?sv=1&sig=sas-secret&sp=w",
    ],
)
def test_redaction_removes_credentials(unsafe: str) -> None:
    rendered = str(redact(unsafe))
    assert "super-secret" not in rendered
    assert "db-password" not in rendered
    assert "storage-secret" not in rendered
    assert "sas-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_sensitive_mapping_keys_are_redacted_recursively() -> None:
    assert redact({"safe": "yes", "DATABASE_URL": "secret", "nested": {"access_token": "token"}}) == {
        "safe": "yes",
        "DATABASE_URL": "[REDACTED]",
        "nested": {"access_token": "[REDACTED]"},
    }


def test_high_cardinality_metric_dimensions_are_rejected() -> None:
    with pytest.raises(ValueError, match="high-cardinality"):
        _metric_attributes({"run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"})
    assert _metric_attributes({"service": "worker", "status": "succeeded"}) == {
        "service": "worker",
        "status": "succeeded",
    }


def test_structured_logging_rejects_unreviewed_fields() -> None:
    with pytest.raises(ValueError, match="unsupported structured log fields"):
        log_event(logging.getLogger("test"), logging.INFO, "unsafe", "unsafe", authorization="secret")
