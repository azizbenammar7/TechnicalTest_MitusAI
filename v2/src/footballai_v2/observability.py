"""Optional OpenTelemetry configuration, spans, propagation, and bounded metrics."""

from __future__ import annotations

import contextlib
import os
from functools import lru_cache
from typing import Any, Iterator, Mapping, MutableMapping

from footballai_v2.logging_config import configure_logging


_METRIC_DIMENSIONS = {"service", "environment", "status", "stage", "profile"}
_configured = False


def configure_observability(service: str) -> None:
    """Configure console logs always and Azure Monitor only when explicitly enabled."""
    global _configured
    configure_logging(service)
    if _configured:
        return
    mode = os.getenv("FOOTBALLAI_OTEL_MODE", "disabled").strip().lower()
    if mode not in {"disabled", "azure_monitor"}:
        raise ValueError("FOOTBALLAI_OTEL_MODE must be disabled or azure_monitor")
    if mode == "disabled":
        _configured = True
        return
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
    if not connection_string or not client_id:
        raise ValueError("Azure Monitor mode requires APPLICATIONINSIGHTS_CONNECTION_STRING and AZURE_CLIENT_ID")
    ratio = float(os.getenv("FOOTBALLAI_TRACE_SAMPLE_RATIO", "0.2"))
    if not 0 < ratio <= 1:
        raise ValueError("FOOTBALLAI_TRACE_SAMPLE_RATIO must be greater than 0 and at most 1")
    os.environ.setdefault("OTEL_SERVICE_NAME", service)
    os.environ.setdefault("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    os.environ.setdefault("OTEL_TRACES_SAMPLER_ARG", str(ratio))
    os.environ.setdefault("OTEL_PYTHON_EXCLUDED_URLS", "/api/health,/api/ready")
    from azure.identity import ManagedIdentityCredential
    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(
        connection_string=connection_string,
        credential=ManagedIdentityCredential(client_id=client_id),
        disable_offline_storage=True,
        enable_live_metrics=False,
        enable_performance_counters=False,
    )
    _configured = True


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    safe_attributes = {key: value for key, value in attributes.items() if value is not None and isinstance(value, (str, bool, int, float))}
    with trace.get_tracer("footballai_v2").start_as_current_span(name, attributes=safe_attributes) as active_span:
        yield active_span


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    try:
        from opentelemetry.propagate import inject
        inject(carrier)
    except ImportError:
        return


def attach_trace_context(carrier: Mapping[str, Any]):
    try:
        from opentelemetry.context import attach
        from opentelemetry.propagate import extract
        normalized = {
            str(key): value.decode() if isinstance(value, bytes) else str(value)
            for key, value in carrier.items()
            if str(key).lower() in {"traceparent", "tracestate"}
        }
        return attach(extract(normalized)) if normalized else None
    except ImportError:
        return None


def detach_trace_context(token) -> None:
    if token is None:
        return
    try:
        from opentelemetry.context import detach
        detach(token)
    except ImportError:
        return


def _metric_attributes(attributes: Mapping[str, str]) -> dict[str, str]:
    invalid = set(attributes) - _METRIC_DIMENSIONS
    if invalid:
        raise ValueError(f"high-cardinality/unsupported metric dimensions: {', '.join(sorted(invalid))}")
    return dict(attributes)


@lru_cache(maxsize=32)
def _counter(name: str):
    from opentelemetry import metrics
    return metrics.get_meter("footballai_v2").create_counter(name)


@lru_cache(maxsize=32)
def _histogram(name: str, unit: str):
    from opentelemetry import metrics
    return metrics.get_meter("footballai_v2").create_histogram(name, unit=unit)


def add_metric(name: str, value: int = 1, **attributes: str) -> None:
    try:
        _counter(name).add(value, _metric_attributes(attributes))
    except ImportError:
        return


def record_metric(name: str, value: float, *, unit: str, **attributes: str) -> None:
    try:
        _histogram(name, unit).record(value, _metric_attributes(attributes))
    except ImportError:
        return
