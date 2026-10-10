"""User-authorized reconciliation of uncertain notification delivery.

The bridge journal and application outbox have separate databases. A bridge decision
is replayable with the same attempt/hash/outcome, so a crash before the application
commit is recovered by retrying the same command. This handler never sends messages.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from .contracts import ContractError, MutationContext, parse_utc, validate_identifier
from .db import connect
from .hermes_delivery import HermesDeliveryBridgeError, HermesDeliveryClient, delivery_fingerprint
from .service import JobSearchLedger
from .store import ConflictError, utc_now


class NotificationRecoveryService:
    def __init__(self, ledger: JobSearchLedger, bridge: HermesDeliveryClient) -> None:
        if not bridge.expected_target:
            raise ValueError("notification recovery requires a fixed delivery target")
        self.ledger = ledger
        self.bridge = bridge

    @staticmethod
    def _eligible(row: Mapping[str, Any], stamp: str) -> bool:
        if row["status"] in {"dead", "pending"}:
            return True
        return (row["status"] == "delivering" and bool(row["lease_expires_at"])
            and parse_utc(row["lease_expires_at"]) <= parse_utc(stamp))

    def list_pending(self, *, limit: int = 25) -> Mapping[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ContractError("notification recovery limit must be between 1 and 50")
        with connect(self.ledger.store.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM notification_outbox WHERE status IN ('dead','pending','delivering') "
                "ORDER BY CASE WHEN last_error='delivery_reconciliation_required' THEN 0 ELSE 1 END,created_at LIMIT ?",
                (limit + 1,),
            ).fetchall()
        stamp = utc_now()
        items = []
        for row in rows[:limit]:
            if not self._eligible(row, stamp):
                continue
            try:
                receipt = self.bridge.status(row["notification_id"])
            except HermesDeliveryBridgeError:
                return {"items": items, "bridge_available": False, "reason_code": "delivery_bridge_unavailable", "truncated": True}
            state = receipt.get("state")
            # Also finish interrupted app commits after a bridge reconciliation.
            if state not in {"in_flight", "reconciliation_required"} and not (
                row["last_error"] == "delivery_reconciliation_required" and state in {"delivered", "retryable", "abandoned"}
            ):
                continue
            items.append({key: row[key] for key in ("notification_id", "topic", "application_id", "created_at", "status", "attempts")})
            items[-1].update(bridge_state=state, expected_attempts=receipt["attempts"],
                expected_payload_sha256=receipt.get("payload_sha256"))
        return {"items": items, "bridge_available": True, "reason_code": None, "truncated": len(rows) > limit}

    def reconcile(self, notification_id: str, *, expected_attempts: int,
                  expected_payload_sha256: str, outcome: str,
                  context: MutationContext) -> Mapping[str, Any]:
        validate_identifier(notification_id, "notification_id")
        context.validate()
        if context.actor_kind != "user":
            raise ContractError("notification reconciliation requires a user decision")
        if not isinstance(outcome, str) or outcome not in {"delivered", "not_delivered", "abandoned"}:
            raise ContractError("notification reconciliation outcome is invalid")
        if isinstance(expected_attempts, bool) or not isinstance(expected_attempts, int) or expected_attempts < 1:
            raise ContractError("notification attempt is invalid")
        if not isinstance(expected_payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_payload_sha256):
            raise ContractError("notification payload fingerprint is invalid")
        request = {"notification_id": notification_id, "expected_attempts": expected_attempts,
            "expected_payload_sha256": expected_payload_sha256, "outcome": outcome}

        def checked_row(connection, stamp):
            row = connection.execute("SELECT * FROM notification_outbox WHERE notification_id=?", (notification_id,)).fetchone()
            if row is None:
                raise ContractError("notification was not found")
            if not self._eligible(row, stamp):
                raise ConflictError("notification is active or already resolved")
            fingerprint = delivery_fingerprint(self.bridge.expected_target, row["title"], row["body"])
            if fingerprint != expected_payload_sha256:
                raise ConflictError("notification payload no longer matches the reviewed receipt")
            receipt = self.bridge.status(notification_id)
            if receipt.get("state") not in {"in_flight", "reconciliation_required", "delivered", "retryable", "abandoned"}:
                raise ConflictError("notification has no uncertain delivery to reconcile")
            if receipt.get("attempts") != expected_attempts or receipt.get("payload_sha256") != fingerprint:
                raise ConflictError("notification delivery attempt changed")
            return row, receipt, fingerprint

        def prepare(connection, stamp):
            row, receipt, _fingerprint = checked_row(connection, stamp)
            if receipt["state"] not in {"in_flight", "reconciliation_required"} and row["last_error"] != "delivery_reconciliation_required":
                raise ConflictError("notification has no uncertain delivery to reconcile")
            # Persist the exact decision and pause delivery before crossing the
            # bridge. A crash then leaves an audited, visibly recoverable intent.
            connection.execute("UPDATE notification_outbox SET status='dead',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error='delivery_reconciliation_required' WHERE notification_id=?", (notification_id,))
            return {**request, "prepared_at": stamp, "actor_kind": context.actor_kind,
                "source_kind": context.source_kind}

        self.ledger.store._idempotent("prepare_notification_reconciliation", context, request, prepare)

        def operation(connection, stamp):
            row, _receipt, fingerprint = checked_row(connection, stamp)
            # Holding the app write transaction excludes a simultaneous worker
            # claim. The private bridge serializes this decision with its sender.
            self.bridge.reconcile(notification_id, expected_attempts=expected_attempts,
                expected_payload_sha256=fingerprint, outcome=outcome)
            status = {"delivered": "delivered", "not_delivered": "pending", "abandoned": "cancelled"}[outcome]
            if status == "pending" and (row["application_id"] or row["topic"] in {"reminder.due", "attention.required", "mail.recruiter_update"}):
                bound = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_owner_binding'").fetchone()
                if bound and connection.execute("SELECT 1 FROM application_owner_binding WHERE singleton=1").fetchone():
                    # Non-delivery proof resolves the old uncertainty, but does
                    # not authorize a retired business workflow to send again.
                    # A new owner reminder requires its own accepted record.
                    status = "cancelled"
            attempts = 0 if status == "pending" else row["attempts"]
            connection.execute(
                "UPDATE notification_outbox SET status=?,attempts=?,available_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,last_error='',delivered_at=? WHERE notification_id=?",
                (status, attempts, stamp, stamp if outcome == "delivered" else None, notification_id),
            )
            return {"notification_id": notification_id, "status": status, "outcome": outcome,
                "previous_attempts": row["attempts"], "attempts": attempts, "bridge_attempts": expected_attempts,
                "payload_sha256": fingerprint, "reconciled_at": stamp, "actor_kind": context.actor_kind,
                "source_kind": context.source_kind, "idempotency_key": context.idempotency_key}

        return self.ledger.store._idempotent("reconcile_notification_delivery", context, request, operation)
