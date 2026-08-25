"""A faithful in-process fake of Azure Service Bus peek-lock semantics.

Models exactly the surface the adapter uses: a sender that enqueues, a receiver
that leases one message at a time (incrementing delivery_count), and settlement
via complete / abandon / dead-letter / renew_message_lock. It also exposes
``expire_locks`` to simulate a worker crash or lock timeout so redelivery can be
tested deterministically. This is NOT real Azure -- it validates the adapter's
decision logic; real Service Bus validation is pending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _body_bytes(body: Any) -> bytes:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return b"".join(
        chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
        for chunk in body
    )


@dataclass
class _StoredMessage:
    body: bytes
    application_properties: dict
    message_id: str | None
    delivery_count: int = 0
    state: str = "available"  # available | locked | completed | deadletter


class _ReceivedMessage:
    """What ``receive_messages`` returns; wraps the broker-side stored message."""

    def __init__(self, stored: _StoredMessage) -> None:
        self._stored = stored

    @property
    def body(self):
        yield self._stored.body

    @property
    def application_properties(self) -> dict:
        return self._stored.application_properties

    @property
    def delivery_count(self) -> int:
        return self._stored.delivery_count

    @property
    def message_id(self):
        return self._stored.message_id


@dataclass
class FakeServiceBusBroker:
    messages: list[_StoredMessage] = field(default_factory=list)
    dead_letter: list[_StoredMessage] = field(default_factory=list)
    renewals: int = 0

    def enqueue(self, stored: _StoredMessage) -> None:
        self.messages.append(stored)

    def next_available(self) -> _StoredMessage | None:
        for stored in self.messages:
            if stored.state == "available":
                return stored
        return None

    def expire_locks(self) -> None:
        """Simulate lock timeout / worker crash: locked messages become available."""
        for stored in self.messages:
            if stored.state == "locked":
                stored.state = "available"

    def active_count(self) -> int:
        return sum(1 for stored in self.messages if stored.state in {"available", "locked"})


class FakeSender:
    def __init__(self, broker: FakeServiceBusBroker) -> None:
        self._broker = broker

    def send_messages(self, message) -> None:
        self._broker.enqueue(
            _StoredMessage(
                body=_body_bytes(message.body),
                application_properties=dict(message.application_properties or {}),
                message_id=message.message_id,
            )
        )

    def close(self) -> None:  # pragma: no cover - parity with SDK
        pass


class FakeReceiver:
    def __init__(self, broker: FakeServiceBusBroker) -> None:
        self._broker = broker

    def receive_messages(self, max_message_count: int = 1, max_wait_time: float | None = None):
        stored = self._broker.next_available()
        if stored is None:
            return []
        stored.state = "locked"
        stored.delivery_count += 1
        return [_ReceivedMessage(stored)]

    def complete_message(self, message: _ReceivedMessage) -> None:
        message._stored.state = "completed"

    def abandon_message(self, message: _ReceivedMessage) -> None:
        message._stored.state = "available"

    def dead_letter_message(self, message: _ReceivedMessage, **_kwargs) -> None:
        message._stored.state = "deadletter"
        self._broker.dead_letter.append(message._stored)

    def renew_message_lock(self, message: _ReceivedMessage) -> None:
        self._broker.renewals += 1

    def close(self) -> None:  # pragma: no cover - parity with SDK
        pass


class FakeServiceBusClient:
    def __init__(self, broker: FakeServiceBusBroker | None = None) -> None:
        self.broker = broker or FakeServiceBusBroker()

    def get_queue_sender(self, queue_name: str) -> FakeSender:
        return FakeSender(self.broker)

    def get_queue_receiver(self, queue_name: str, **_kwargs) -> FakeReceiver:
        return FakeReceiver(self.broker)
