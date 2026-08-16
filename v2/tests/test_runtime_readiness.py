"""Bounded, cached readiness capability probes."""

from __future__ import annotations

import os

import pytest

from footballai_v2.runtime_readiness import CapabilityProbe, postgres_capability


def test_capability_probe_caches_within_ttl():
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return True

    probe = CapabilityProbe(check, ttl_seconds=100)
    assert probe.status() == "ready"
    assert probe.status() == "ready"
    assert calls["n"] == 1  # second call served from cache


def test_capability_probe_reports_unavailable_on_error():
    def check():
        raise RuntimeError("dependency down")

    probe = CapabilityProbe(check, ttl_seconds=100)
    assert probe.status() == "unavailable"


_DATABASE_URL = os.getenv("FOOTBALLAI_TEST_DATABASE_URL")


@pytest.mark.skipif(not _DATABASE_URL, reason="requires FOOTBALLAI_TEST_DATABASE_URL")
def test_postgres_capability_true_when_migrated():
    from sqlalchemy import create_engine

    engine = create_engine(_DATABASE_URL, future=True)
    try:
        assert postgres_capability(engine) is True
    finally:
        engine.dispose()
