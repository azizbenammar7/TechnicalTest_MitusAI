from __future__ import annotations

import pytest

from footballai_v2.worker import _enabled


@pytest.mark.parametrize("value", ["1", "true", "TRUE"])
def test_worker_once_enabled(monkeypatch, value):
    monkeypatch.setenv("FOOTBALLAI_WORKER_ONCE", value)
    assert _enabled("FOOTBALLAI_WORKER_ONCE") is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE"])
def test_worker_once_disabled(monkeypatch, value):
    monkeypatch.setenv("FOOTBALLAI_WORKER_ONCE", value)
    assert _enabled("FOOTBALLAI_WORKER_ONCE") is False


def test_worker_once_rejects_ambiguous_value(monkeypatch):
    monkeypatch.setenv("FOOTBALLAI_WORKER_ONCE", "yes")
    with pytest.raises(ValueError, match="FOOTBALLAI_WORKER_ONCE"):
        _enabled("FOOTBALLAI_WORKER_ONCE")
