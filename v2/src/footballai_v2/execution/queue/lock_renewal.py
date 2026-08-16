"""Provider-neutral background lock renewal for long-running work.

A FootballAI analysis can run far longer than a queue message's lock. This
renewer keeps one message's lock alive on a background thread until the work
finishes, fails, or a bounded maximum lifetime is reached. It is deliberately
decoupled from Azure: it renews through a plain ``renew`` callable, so it can be
unit-tested with a fake and reused by any broker.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger("footballai_v2.worker")


class LockRenewer:
    """Renew a single lock on a timer until stopped or a maximum lifetime elapses."""

    def __init__(
        self,
        renew: Callable[[], None],
        *,
        interval_seconds: float,
        max_lifetime_seconds: float,
        name: str = "lock-renewer",
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_lifetime_seconds <= 0:
            raise ValueError("max_lifetime_seconds must be positive")
        self._renew = renew
        self._interval = interval_seconds
        self._max_lifetime = max_lifetime_seconds
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._renewals = 0
        self._elapsed = 0.0

    @property
    def renewals(self) -> int:
        return self._renewals

    def start(self) -> "LockRenewer":
        if self._thread is not None:
            raise RuntimeError("renewer already started")
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        # Wait one interval, renew, repeat -- bounded by max_lifetime so a leaked
        # renewer can never hold a lock forever.
        while not self._stop.wait(self._interval):
            self._elapsed += self._interval
            if self._elapsed >= self._max_lifetime:
                logger.warning("lock_renewal_max_lifetime_reached name=%s", self._name)
                return
            try:
                self._renew()
                self._renewals += 1
            except Exception:  # noqa: BLE001 - never leak broker error details
                # Renewal is best-effort; if it keeps failing the broker will
                # redeliver the message to another worker. Log without secrets.
                logger.warning("lock_renewal_failed name=%s", self._name)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._interval * 2 + 1)
        self._thread = None

    def __enter__(self) -> "LockRenewer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
