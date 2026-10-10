"""Production worker adapters, composed from public owner APIs.

The operational DurableWorker owns scheduling, leases and command_work receipts.
These handlers never initialize a legacy application ledger. Provider effects and
notification sends require the injected persistent operator activation guard.
"""
from __future__ import annotations

import json
import uuid

from .commands import DomainError, digest
from .external_actions.service import instant, stamp
from .external_actions.binding import ConfiguredOutlookProvider, OutlookProviderResolver
from .external_actions.worker import ExternalActionWorker


class ProductionExecution:
    def __init__(self, runtime, provider_resolver, activation_status, activation_guard,
                 *, notification_client=None, notification_target=None):
        if not all(callable(value) for value in (provider_resolver, activation_status, activation_guard)):
            raise ValueError("Production execution requires trusted routing and activation interfaces")
        self.runtime, self.provider_resolver = runtime, provider_resolver
        self.activation_status, self.activation_guard = activation_status, activation_guard
        self.notification_client = notification_client
        self.notification_target = notification_target
        self.handlers = {"application.execute_action": self.execute_action,
                         "application.reconcile_action": self.reconcile_action,
                         "application.deliver_owner_notification": self.deliver_owner_notification,
                         "application.dispatch": self.dispatch}

    def _active(self):
        status = self.activation_status()
        return status if isinstance(status, dict) and status.get("paused") is False and type(status.get("revision")) is int else None

    def _worker(self, action_id, context=None):
        with self.runtime.executor.read() as con:
            action = self.runtime.actions.get(con, action_id)
        provider = self.provider_resolver(action["account_id"])
        return ExternalActionWorker(self.runtime.executor, self.runtime.actions, provider,
            self.runtime.result_context, self.runtime.workflows.applicability,
            activation_status=self.activation_status, activation_guard=self.activation_guard,
            lease_seconds=180, heartbeat=getattr(context, "heartbeat", None))

    def execute_action(self, payload, context=None):
        if self._active() is None:
            return {"status": "paused", "work_complete": False}
        action_id = payload["action_id"]
        with self.runtime.executor.read() as con:
            before = self.runtime.actions.get(con, action_id)
        if before["execution"] != "queued":
            return {"action_id": action_id, "status": before["execution"],
                    "work_complete": before["execution"] != "executing"}
        result = self._worker(action_id, context).execute(action_id)
        state = result.get("execution", result.get("status"))
        complete = state not in {"paused", "executing", "restore_quarantined"} and not result.get("dispatch_paused", False)
        if state == "queued":
            # A future retry is independently enqueued by the owner. Re-reading
            # an original queue item before its first claim must remain deferred.
            complete = result.get("fence", 0) != before["fence"] and not result.get("dispatch_paused", False)
        return {"action_id": action_id, "status": state, "work_complete": complete}

    def reconcile_action(self, payload, context=None):
        if self._active() is None:
            return {"status": "paused", "work_complete": False}
        result = self._worker(payload["action_id"], context).reconcile(payload["action_id"])
        return {"action_id": payload["action_id"], "status": result.get("execution", result.get("status")),
                "work_complete": result.get("status") != "paused"}

    def dispatch(self, payload=None, context=None):
        """Materialize bounded core work; no provider I/O occurs in this phase."""
        from .worker import FollowUpTask, TaskResult
        active = self._active()
        if active is None:
            return TaskResult({"status": "paused"})
        executor = self.runtime.executor
        cursors = (payload or {}).get("cursors", {})
        executor.run(self.runtime.result_context("expire_actions", uuid.uuid4().hex),
                     "expire_actions", {}, self.runtime.actions.expire_leases)
        now = executor.clock()
        with executor.read() as con:
            empty = {"items": [], "next_cursor": None}
            action_work = empty if cursors.get("actions", "") is None else executor.work_page(con, owner="external_actions", kind="execute_action", limit=100, after=cursors.get("actions", ""))
            notification_work = empty if cursors.get("notifications", "") is None else executor.work_page(con, owner="applications", kind="deliver_owner_notification", limit=100, after=cursors.get("notifications", ""))
            reconciliation = empty if cursors.get("reconciliation", "") is None else self.runtime.actions.eligible_reconciliation(con, now, limit=100, after=cursors.get("reconciliation", ""))
            reminder_work = empty if cursors.get("reminders", "") is None else executor.work_page(con, owner="applications", kind="reminder", limit=100, after=cursors.get("reminders", ""))
            retired_timers = []
            for item in reminder_work["items"]:
                reminder = self.runtime.applications.get_record(con, "reminders", item["payload"]["reminder_id"])
                if reminder["status"] != "pending" or reminder["version"] != item["payload"]["version"]:
                    retired_timers.append(item["id"])
        for work_id in retired_timers:
            executor.complete_work(work_id, owner="applications", kind="reminder")
        following = []
        # A failed operational queue batch must not strand durable owner work.
        # Preserve its failure history and retry at most once per hour or after
        # operator reactivation. Owner fences and stable sidecar receipt IDs
        # remain the authority for whether another provider write is allowed.
        recovery_round = str(active["revision"]) + ":" + str(int(instant(now).timestamp()) // 3600)
        for kind, work in (("application.execute_action", action_work),
                           ("application.deliver_owner_notification", notification_work)):
            for item in work["items"]:
                available = item["payload"].get("available_at")
                if available and instant(available) > instant(now):
                    continue
                following.append(FollowUpTask(kind, {**item["payload"], "owner_work_id": item["id"]},
                    dedupe_key="owner-work:" + item["id"] + ":" + recovery_round, max_attempts=5))
        round_key = str(active["revision"]) + ":" + str(int(instant(now).timestamp()) // 300)
        for action in reconciliation["items"]:
            following.append(FollowUpTask("application.reconcile_action", {"action_id": action["action_id"]},
                dedupe_key="owner-reconcile:" + action["action_id"] + ":" + str(action["fence"]) + ":" + round_key,
                max_attempts=1))
        remaining = {name: page["next_cursor"] for name, page in (("actions", action_work),
            ("notifications", notification_work), ("reconciliation", reconciliation), ("reminders", reminder_work))}
        if any(value is not None for value in remaining.values()):
            # A completed stream stays completed during this bounded scan.
            following.append(FollowUpTask("application.dispatch", {"cursors": remaining},
                dedupe_key="owner-dispatch:" + round_key + ":" + digest(remaining), max_attempts=1))
        return TaskResult({"status": "ready", "queued": len(following), "advisory_timers_completed": len(retired_timers),
                           "reconciliation_truncated": reconciliation["next_cursor"] is not None}, tuple(following))

    def _handoff(self, delivery_id):
        with self.runtime.executor.read() as con:
            handoff = self.runtime.applications.get_notification_handoff(con, delivery_id)
            work = self.runtime.executor.find_work(con, "applications", "deliver_owner_notification", delivery_id)
        if work is None:
            raise DomainError("not_found", "Notification lacks its original durable work")
        return handoff, json.loads(work["payload"])

    def deliver_owner_notification(self, payload, context=None):
        active = self._active()
        if active is None or self.notification_client is None or not self.notification_target:
            return {"status": "paused", "work_complete": False}
        delivery_id = payload["delivery_id"]
        handoff, original = self._handoff(delivery_id)
        title = "Application reminder"
        body = original.get("description") or "An application reminder is due."
        from .hermes_delivery import delivery_fingerprint, HermesDeliveryBridgeError
        fingerprint = delivery_fingerprint(self.notification_target, title, body)
        try:
            receipt = self.notification_client.status(delivery_id)
            state = receipt.get("state")
            if state not in {"not_found", "delivered", "retryable"}:
                return {"status": "needs_reconciliation", "work_complete": False}
            if state != "not_found" and receipt.get("payload_sha256") != fingerprint:
                return {"status": "receipt_conflict", "work_complete": False}
            if state != "delivered":
                def check(tx):
                    return {"allowed": self.activation_guard(tx, active["revision"]) and
                        self.runtime.applications.notification_delivery_applicable(tx.connection, delivery_id) and
                        not tx.restore_quarantined("reminder", handoff["reminder_id"]),
                        "restore_quarantined": tx.restore_quarantined("reminder", handoff["reminder_id"]),
                        "activation_revision": active["revision"], "delivery_id": delivery_id}
                permission = self.runtime.executor.run(self.runtime.result_context("claim_reminder", uuid.uuid4().hex),
                    "claim_reminder", {"delivery_id": delivery_id, "activation_revision": active["revision"]}, check)
                if not permission["allowed"]:
                    if permission["restore_quarantined"]:
                        return {"status": "restore_quarantined", "work_complete": False}
                    # A paused dispatch stays pending; a no-longer-relevant
                    # reminder is safely suppressed without fabricating delivery.
                    current = self._active()
                    return {"status": "paused" if current is None or current["revision"] != active["revision"] else "suppressed",
                            "work_complete": current is not None and current["revision"] == active["revision"]}
                heartbeat = getattr(context, "heartbeat", None)
                if heartbeat is not None and not heartbeat():
                    return {"status": "worker_lease_lost", "work_complete": False}
                self.notification_client.send(delivery_id, title, body)
                receipt = self.notification_client.status(delivery_id)
            if receipt.get("state") != "delivered" or receipt.get("payload_sha256") != fingerprint:
                return {"status": "needs_reconciliation", "work_complete": False}
        except HermesDeliveryBridgeError as exc:
            return {"status": "retryable" if exc.retryable else "needs_reconciliation", "work_complete": False}
        try:
            observed = receipt["updated_at"]
            if not isinstance(observed, str):
                raise ValueError()
            if len(observed) == 19:
                observed = observed.replace(" ", "T") + "Z"  # SQLite CURRENT_TIMESTAMP is UTC.
            # The bridge clock has second precision. Preserve a stable receipt
            # time at least as late as the exact originating handoff.
            observed_at = stamp(max(instant(observed), instant(handoff["created_at"])))
            if instant(observed_at) > instant(self.runtime.executor.clock()):
                raise ValueError()
        except (KeyError, ValueError, DomainError):
            return {"status": "receipt_time_invalid", "work_complete": False}
        receipt_id = "hermes:" + digest({"delivery_id": delivery_id, "sha256": fingerprint})
        result = self.runtime.command(self.runtime.result_context("record_delivery", receipt_id), "record_delivery", {
            "delivery_id": delivery_id, "reminder_id": handoff["reminder_id"],
            "expected_version": handoff["reminder_version"], "receipt_id": receipt_id,
            "outcome": "delivered", "observed_at": observed_at})
        return {**result, "work_complete": True}
