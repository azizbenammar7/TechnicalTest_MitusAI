"""Deterministic tests for the background lock renewer (no real inference)."""

from __future__ import annotations

import threading
import time

from footballai_v2.execution.queue.lock_renewal import LockRenewer


def test_renews_repeatedly_until_stopped():
    counter = {"n": 0}

    def renew():
        counter["n"] += 1

    renewer = LockRenewer(
        renew, interval_seconds=0.02, max_lifetime_seconds=10
    ).start()
    time.sleep(0.12)
    renewer.stop()
    renewed = counter["n"]
    assert renewed >= 2
    # No further renewals after stop.
    time.sleep(0.05)
    assert counter["n"] == renewed


def test_stops_at_max_lifetime():
    counter = {"n": 0}

    def renew():
        counter["n"] += 1

    renewer = LockRenewer(
        renew, interval_seconds=0.02, max_lifetime_seconds=0.05
    ).start()
    time.sleep(0.3)
    # Bounded: the renewer stopped itself around max_lifetime rather than forever.
    assert counter["n"] <= 3
    renewer.stop()


def test_renew_errors_do_not_crash_renewer():
    calls = {"n": 0}

    def renew():
        calls["n"] += 1
        raise RuntimeError("transient renew failure")

    renewer = LockRenewer(
        renew, interval_seconds=0.02, max_lifetime_seconds=10
    ).start()
    time.sleep(0.1)
    renewer.stop()
    # It kept trying despite errors, and stop() returned cleanly.
    assert calls["n"] >= 2
    assert not any(t.name == "lock-renewer" and t.is_alive() for t in threading.enumerate())
