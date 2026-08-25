"""Azure Service Bus adapter for the provider-neutral JobQueue port.

Service Bus is at-least-once infrastructure, so the FootballAI execution path
must be idempotent and the *control plane* (repository) is the final authority on
whether a job is still executable. This adapter therefore:

* enqueues a job as one message carrying stable identifiers (run_id,
  logical_analysis_id, attempt_number) with the job id as the message id;
* on claim, drains duplicates whose run is already terminal, dead-letters
  poison/unknown/over-delivered messages, and otherwise hands back a job while a
  background :class:`LockRenewer` keeps the peek-lock alive;
* on complete/fail, settles the message and stops renewal.

Azure SDK types (``ServiceBusReceivedMessage``, ``ServiceBusClient`` and Azure
exceptions) never leave this module.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from footballai_v2.contracts.v1 import AnalysisRunStatus, utc_now
from footballai_v2.execution.contracts import ExecutionJob
from footballai_v2.execution.queue.lock_renewal import LockRenewer
from footballai_v2.storage.local_analysis_runs import RunNotFoundError

logger = logging.getLogger("footballai_v2.worker")

# Service Bus reports delivery_count starting at 1 on first delivery.
DEFAULT_MAX_DELIVERY = 5
DEFAULT_LOCK_RENEW_INTERVAL_SECONDS = 30.0
DEFAULT_LOCK_MAX_LIFETIME_SECONDS = 4 * 60 * 60
DEFAULT_RECEIVE_WAIT_SECONDS = 5.0
# How many duplicate/poison messages a single claim will drain before giving up.
_DRAIN_LIMIT = 16


class AzureServiceBusQueue:
    """Map FootballAI queue semantics onto Azure Service Bus peek-lock."""

    def __init__(
        self,
        client,
        queue_name: str,
        *,
        repository=None,
        max_delivery: int = DEFAULT_MAX_DELIVERY,
        receive_wait_seconds: float = DEFAULT_RECEIVE_WAIT_SECONDS,
        lock_renew_interval_seconds: float = DEFAULT_LOCK_RENEW_INTERVAL_SECONDS,
        lock_max_lifetime_seconds: float = DEFAULT_LOCK_MAX_LIFETIME_SECONDS,
    ) -> None:
        self._client = client
        self._queue_name = queue_name
        self._repository = repository
        self._max_delivery = max_delivery
        self._receive_wait = receive_wait_seconds
        self._lock_interval = lock_renew_interval_seconds
        self._lock_max_lifetime = lock_max_lifetime_seconds
        self._sender = None
        self._receiver = None
        self._inflight: dict[str, tuple[object, LockRenewer]] = {}

    # -- lazy Azure resources ------------------------------------------------

    def _get_sender(self):
        if self._sender is None:
            self._sender = self._client.get_queue_sender(self._queue_name)
        return self._sender

    def _get_receiver(self):
        if self._receiver is None:
            self._receiver = self._client.get_queue_receiver(self._queue_name)
        return self._receiver

    # -- JobQueue port -------------------------------------------------------

    def enqueue(self, job: ExecutionJob) -> None:
        from azure.servicebus import ServiceBusMessage

        message = ServiceBusMessage(
            json.dumps(job.to_dict(), sort_keys=True),
            application_properties={
                "run_id": job.run_id,
                "logical_analysis_id": job.logical_analysis_id,
                "attempt_number": job.attempt_number,
            },
            message_id=job.job_id,
        )
        self._get_sender().send_messages(message)

    def claim(self, worker_id: str) -> ExecutionJob | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("invalid worker ID")
        receiver = self._get_receiver()
        for _ in range(_DRAIN_LIMIT):
            messages = receiver.receive_messages(
                max_message_count=1, max_wait_time=self._receive_wait
            )
            if not messages:
                return None
            message = messages[0]
            job = self._parse(message)
            if job is None:
                self._dead_letter(receiver, message, "unparseable", "job body invalid")
                continue
            if int(getattr(message, "delivery_count", 1)) > self._max_delivery:
                self._dead_letter(receiver, message, "max_delivery", "redelivered too often")
                continue
            disposition = self._executability(job)
            if disposition == "drain":
                receiver.complete_message(message)
                logger.info("job_duplicate_drained run_id=%s", job.run_id)
                continue
            if disposition == "unknown":
                self._dead_letter(receiver, message, "unknown_run", "no control-plane record")
                continue
            renewer = LockRenewer(
                lambda m=message: receiver.renew_message_lock(m),
                interval_seconds=self._lock_interval,
                max_lifetime_seconds=self._lock_max_lifetime,
                name=f"renew-{job.run_id}",
            ).start()
            self._inflight[job.job_id] = (message, renewer)
            return replace(job, claimed_at=utc_now(), worker_id=worker_id)
        return None

    def complete(self, job: ExecutionJob) -> None:
        self._settle(job, dead_letter=False)

    def fail(self, job: ExecutionJob) -> None:
        # A recorded analysis failure is a terminal, immutable outcome, so the
        # message's processing is finished and the message is completed. Retrying
        # is a *new* run enqueued by the retry flow, not a redelivery of this
        # message. Infrastructure failures (crashes) never call fail(): the lock
        # simply expires and Service Bus redelivers.
        self._settle(job, dead_letter=False)

    def cancel(self, run_id: str) -> bool:
        # Cancellation is control-plane driven (repository + cancellation marker).
        # A queued Service Bus message cannot be selectively removed by property;
        # it is drained on a later claim once the run is terminal. Report that no
        # message was removed here.
        logger.info("queue_cancel_delegated_to_control_plane run_id=%s", run_id)
        return False

    def recover_abandoned(self, timeout_seconds: float, store) -> int:
        # Service Bus itself expires locks and redelivers; there is no local
        # "abandoned claimed" state to sweep.
        return 0

    def close(self) -> None:
        for _message, renewer in self._inflight.values():
            renewer.stop()
        self._inflight.clear()
        for resource in (self._receiver, self._sender):
            if resource is not None:
                try:
                    resource.close()
                except Exception:  # noqa: BLE001
                    pass
        self._receiver = None
        self._sender = None

    # -- helpers -------------------------------------------------------------

    def _settle(self, job: ExecutionJob, *, dead_letter: bool) -> None:
        entry = self._inflight.pop(job.job_id, None)
        if entry is None:
            return
        message, renewer = entry
        renewer.stop()
        receiver = self._get_receiver()
        if dead_letter:
            receiver.dead_letter_message(message)
        else:
            receiver.complete_message(message)

    def _executability(self, job: ExecutionJob) -> str:
        """Return 'run' (execute), 'drain' (already terminal), or 'unknown'."""
        if self._repository is None:
            return "run"
        try:
            run = self._repository.load(job.run_id)
        except RunNotFoundError:
            return "unknown"
        if run.status.is_terminal:
            return "drain"
        return "run"

    @staticmethod
    def _parse(message) -> ExecutionJob | None:
        try:
            body = _message_body_bytes(message)
            value = json.loads(body)
            if not isinstance(value, dict):
                return None
            return ExecutionJob.from_dict(value)
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _dead_letter(receiver, message, reason: str, description: str) -> None:
        logger.warning("job_dead_lettered reason=%s", reason)
        receiver.dead_letter_message(message, reason=reason, error_description=description)


def _message_body_bytes(message) -> bytes:
    body = message.body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    # ServiceBusReceivedMessage.body is a generator of byte chunks.
    return b"".join(
        chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
        for chunk in body
    )
