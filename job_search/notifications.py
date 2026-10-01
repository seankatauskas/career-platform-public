"""Durable, policy-gated notifications delivered through a fixed Hermes command.

Producers publish bounded notification intents through the public ledger service.  A
worker can inject :meth:`NotificationOutboxHandler.handle_task` under its own runtime
task registry.  This module never reads Outlook and never invokes a shell.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .contracts import (
    ContractError,
    parse_utc,
    payload_sha256,
    validate_identifier,
)

SHORTLIST_NOTIFICATION_TASK_KIND = "notification.shortlist_evaluate"
NOTIFICATION_DELIVERY_TASK_KIND = "notification.deliver"
HERMES_SEND_ARGV = ("hermes", "send", "--to")
SAFE_CONTEXT_KEYS = frozenset(
    {
        "action_id",
        "application_id",
        "ats",
        "event_id",
        "job_id",
        "lane",
        "phase",
        "proposal_id",
        "reminder_id",
        "session_id",
        "status",
        "workflow",
    }
)


class NotificationLedger(Protocol):
    """Narrow durable operations required by publishers and delivery workers."""

    def publish_notification(
        self,
        intent: "NotificationIntent",
        policy: "NotificationPolicy",
        *,
        available_at: str = "",
    ) -> Mapping[str, Any]: ...

    def claim_notification(
        self, worker_id: str, now: str, lease_seconds: int = 60
    ) -> Optional[Mapping[str, Any]]: ...

    def complete_notification(
        self,
        notification_id: str,
        lease_token: str,
        outcome: str,
        now: str,
        *,
        retry_at: Optional[str] = None,
        error: str = "",
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class NotificationIntent:
    topic: str
    source_id: str
    title: str
    body: str
    application_id: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        validate_identifier(self.topic, "topic")
        validate_identifier(self.source_id, "source_id")
        if self.application_id:
            validate_identifier(self.application_id, "application_id")
        if not self.title.strip() or len(self.title) > 200:
            raise ContractError("notification title must be 1 to 200 characters")
        if not self.body.strip() or len(self.body) > 2000:
            raise ContractError("notification body must be 1 to 2000 characters")
        if any(
            ord(character) < 32 and character not in {"\n", "\r", "\t"}
            for character in self.title + self.body
        ):
            raise ContractError("notification text contains unsafe control characters")
        if not isinstance(self.context, Mapping):
            raise ContractError("notification context must be an object")


@dataclass(frozen=True)
class NotificationPolicy:
    """Allowlist policy; disabled or unknown topics never reach Telegram."""

    policy_id: str = "telegram-v1"
    enabled_topics: frozenset[str] = frozenset(
        {
            "application.interview_requested",
            "application.interview_scheduled",
            "application.offer_received",
            "application.rejection_received",
            "attention.required",
            "mail.recruiter_update",
            "reminder.due",
            "shortlist.ready",
            "system.degraded",
        }
    )
    max_attempts: int = 5

    def evaluate(
        self, intent: NotificationIntent, *, available_at: str = ""
    ) -> Optional[Mapping[str, Any]]:
        intent.validate()
        validate_identifier(self.policy_id, "policy_id")
        if available_at:
            parse_utc(available_at)
        if intent.topic not in self.enabled_topics:
            return None
        if not 1 <= self.max_attempts <= 20:
            raise ContractError("notification policy max_attempts is invalid")
        fingerprint = payload_sha256(
            {"topic": intent.topic, "source_id": intent.source_id}
        )
        safe_context = {}
        for key, value in intent.context.items():
            if (
                not isinstance(key, str)
                or key not in SAFE_CONTEXT_KEYS
                or not isinstance(value, (str, int, bool, type(None)))
            ):
                continue
            if isinstance(value, str):
                value = value[:256]
            safe_context[key] = value
        notification = {
            "dedupe_key": f"notify:{fingerprint}",
            "topic": intent.topic,
            "policy_id": self.policy_id,
            "application_id": intent.application_id or None,
            "title": intent.title.strip(),
            "body": intent.body.strip(),
            "context": safe_context,
            "max_attempts": self.max_attempts,
        }
        if available_at:
            notification["available_at"] = available_at
        return notification


class DurableNotificationPublisher:
    """Policy-evaluates and durably enqueues one notification intent."""

    def __init__(
        self,
        ledger: NotificationLedger,
        policy: NotificationPolicy = NotificationPolicy(),
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._ledger = ledger
        self.policy = policy
        self._now = now or (lambda: datetime.now(timezone.utc))

    def publish(self, intent: NotificationIntent) -> Mapping[str, Any]:
        current = self._now()
        if current.tzinfo is None:
            raise ContractError("notification clock must be timezone-aware")
        return self._ledger.publish_notification(
            intent, self.policy, available_at=_utc_text(current)
        )


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str,
        timeout_seconds: int,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Production process adapter.  It always uses argv and never a shell."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str,
        timeout_seconds: int,
    ) -> CommandResult:
        completed = subprocess.run(
            tuple(argv),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class NotificationSendError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class HermesSendClient:
    """Send bounded plain text through one configured Hermes delivery target."""

    def __init__(
        self,
        runner: Optional[CommandRunner] = None,
        *,
        executable: Optional[Path] = None,
        target: str = "telegram",
        timeout_seconds: int = 30,
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("Hermes send timeout must be between 1 and 120 seconds")
        if (
            not isinstance(target, str)
            or not 1 <= len(target) <= 256
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?::[^\s\x00-\x1f\x7f]+)*", target)
        ):
            raise ValueError("Hermes notification target is invalid")
        command = str(executable) if executable is not None else HERMES_SEND_ARGV[0]
        if executable is not None and not Path(executable).is_absolute():
            raise ValueError("Hermes executable must be an absolute path")
        if not command or any(ord(character) < 32 for character in command):
            raise ValueError("Hermes executable is invalid")
        self._runner = runner or SubprocessCommandRunner()
        self.executable = command
        self.target = target
        self.timeout_seconds = timeout_seconds

    def send(self, notification: Mapping[str, Any]) -> None:
        title = str(notification.get("title") or "").strip()
        body = str(notification.get("body") or "").strip()
        encoded = title + ("\n\n" + body if body else "")
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise NotificationSendError(
                "notification message exceeds the Hermes limit", retryable=False
            )
        try:
            result = self._runner.run(
                (self.executable, *HERMES_SEND_ARGV[1:], self.target),
                input_text=encoded,
                timeout_seconds=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NotificationSendError(
                "hermes send is temporarily unavailable", retryable=True
            ) from exc
        if result.returncode:
            raise NotificationSendError(
                f"hermes send exited with status {result.returncode}",
                retryable=result.returncode not in {2, 64},
            )


class RemoteHermesSendClient:
    """Send through the credential-owning Hermes cloud sidecar."""

    def __init__(
        self,
        socket_path: Path,
        *,
        target: str = "telegram",
        timeout_seconds: int = 40,
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("Hermes send timeout must be between 1 and 120 seconds")
        if (
            not isinstance(target, str)
            or not 1 <= len(target) <= 256
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?::[^\s\x00-\x1f\x7f]+)*", target)
        ):
            raise ValueError("Hermes notification target is invalid")
        if not Path(socket_path).is_absolute():
            raise ValueError("Hermes delivery socket path must be absolute")
        from .hermes_delivery import HermesDeliveryClient

        self._client = HermesDeliveryClient(
            socket_path,
            expected_target=target,
            timeout_seconds=timeout_seconds,
        )
        self.target = target
        self.timeout_seconds = timeout_seconds

    def send(self, notification: Mapping[str, Any]) -> None:
        title = str(notification.get("title") or "").strip()
        body = str(notification.get("body") or "").strip()
        delivery_id = str(notification.get("notification_id") or "")
        from .hermes_delivery import HermesDeliveryBridgeError

        try:
            self._client.send(delivery_id, title, body)
        except HermesDeliveryBridgeError as exc:
            raise NotificationSendError(
                exc.code if exc.code in {"delivery_reconciliation_required", "delivery_payload_conflict"}
                else "Hermes delivery sidecar rejected the notification",
                retryable=exc.retryable,
            ) from exc


class NotificationSender(Protocol):
    def send(self, notification: Mapping[str, Any]) -> None: ...


class NotificationOutboxHandler:
    """Bounded lease/retry handler suitable for injection into the runtime worker."""

    def __init__(
        self,
        ledger: NotificationLedger,
        sender: NotificationSender,
        *,
        worker_id: str = "notification-worker",
        lease_seconds: int = 180,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        validate_identifier(worker_id, "worker_id")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("notification lease must be between 1 and 3600 seconds")
        sender_timeout = getattr(sender, "timeout_seconds", None)
        if (
            isinstance(sender_timeout, int)
            and not isinstance(sender_timeout, bool)
            and lease_seconds < sender_timeout + 30
        ):
            raise ValueError(
                "notification lease must cover the sender timeout plus 30 seconds"
            )
        self._ledger = ledger
        self._sender = sender
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))

    def handle_task(
        self, payload: Mapping[str, Any], _task_context: Any = None
    ) -> Mapping[str, Any]:
        """Drain a bounded batch; unknown lane/workflow fields are ignored."""

        raw_limit = payload.get("limit", 10)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise ContractError("notification task limit must be an integer")
        if not 1 <= raw_limit <= 50:
            raise ContractError("notification task limit must be between 1 and 50")
        heartbeat = getattr(_task_context, "heartbeat", None)

        def keep_work_lease() -> None:
            if heartbeat is not None and not heartbeat():
                raise RuntimeError("notification task lease was lost")

        delivered = retried = dead = 0
        for _ in range(raw_limit):
            keep_work_lease()
            now = self._now()
            if now.tzinfo is None:
                raise ContractError("notification clock must be timezone-aware")
            stamp = _utc_text(now)
            claimed = self._ledger.claim_notification(
                self.worker_id, stamp, self.lease_seconds
            )
            if claimed is None:
                break
            try:
                self._sender.send(claimed)
            except NotificationSendError as exc:
                retryable = exc.retryable and int(claimed["attempts"]) < int(
                    claimed["max_attempts"]
                )
                retry_at = (
                    _utc_text(
                        now
                        + timedelta(
                            seconds=min(
                                3600, 60 * (2 ** (int(claimed["attempts"]) - 1))
                            )
                        )
                    )
                    if retryable
                    else None
                )
                self._ledger.complete_notification(
                    str(claimed["notification_id"]),
                    str(claimed["lease_token"]),
                    "retryable_failure" if retryable else "permanent_failure",
                    stamp,
                    retry_at=retry_at,
                    error=str(exc),
                )
                if retryable:
                    retried += 1
                else:
                    dead += 1
            except Exception:
                retry_at = _utc_text(now + timedelta(seconds=60))
                completion = self._ledger.complete_notification(
                    str(claimed["notification_id"]),
                    str(claimed["lease_token"]),
                    "retryable_failure",
                    stamp,
                    retry_at=retry_at,
                    error="notification sender failed",
                )
                if completion.get("status") == "dead":
                    dead += 1
                else:
                    retried += 1
            else:
                self._ledger.complete_notification(
                    str(claimed["notification_id"]),
                    str(claimed["lease_token"]),
                    "succeeded",
                    stamp,
                )
                delivered += 1
            keep_work_lease()
        return {"delivered": delivered, "retried": retried, "dead": dead}


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CommandResult",
    "CommandRunner",
    "DurableNotificationPublisher",
    "HERMES_SEND_ARGV",
    "HermesSendClient",
    "RemoteHermesSendClient",
    "NOTIFICATION_DELIVERY_TASK_KIND",
    "NotificationIntent",
    "NotificationLedger",
    "NotificationOutboxHandler",
    "NotificationPolicy",
    "NotificationSendError",
    "SAFE_CONTEXT_KEYS",
    "SHORTLIST_NOTIFICATION_TASK_KIND",
    "SubprocessCommandRunner",
]
