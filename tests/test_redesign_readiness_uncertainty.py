"""Retired policy is inactive; provider uncertainty remains visible until proven."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
import unittest
import uuid

from job_search.application_installation import freeze_legacy
from job_search.application_migration import convert_snapshot
from job_search.application_readiness import application_readiness
from job_search.application_runtime import ApplicationRuntime
from job_search.commands import CommandContext, Principal
from job_search.commands.installation import record_restore
from job_search.db import connect
from job_search.external_actions.api import ProviderOutcome
from job_search.external_actions.worker import ExternalActionWorker
from job_search.notifications import NotificationIntent
from job_search.outlook.state import SQLiteOutlookState
from job_search.readiness import readiness_report
from job_search.service import JobSearchLedger
from tests import test_redesign_readiness as owner_fixtures
from tests import test_redesign_external_actions as action_fixtures
from tests import test_job_search_readiness as work_fixtures


class ReadinessUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owner_fixtures.OwnerReadinessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        self.now = datetime.now(timezone.utc)
        self.runtime.executor.clock = self.stamp
        with self.runtime.executor.read() as con:
            self.application = self.runtime.applications.list_applications(con)["items"][0]

    def stamp(self):
        return self.now.isoformat().replace("+00:00", "Z")

    def human(self, operation, payload):
        return self.runtime.command(CommandContext(Principal("human", "human", {"*"}), uuid.uuid4().hex), operation, payload)

    def test_retired_connector_failure_is_ignored_but_new_sync_failure_is_visible(self):
        SQLiteOutlookState(self.fixture.path).set_health("outlook:retired-account:inbox", "failed", "Old classifier failed")
        caps = {c["id"]: c for c in readiness_report(self.fixture.path, application_backend="owners")["capabilities"]}
        self.assertNotEqual(caps["outlook"]["status"], "blocked")
        helper = work_fixtures.DomainTests()
        helper.path = self.fixture.path
        helper.work("new-sync-failure", "applications.mail.sync", status="dead",
            schedule="applications.mail.sync", at=self.now + timedelta(seconds=1))
        caps = {c["id"]: c for c in readiness_report(self.fixture.path, application_backend="owners",
            now=self.now + timedelta(seconds=2))["capabilities"]}
        self.assertEqual(caps["outlook"]["reason_code"], "latest_work_failed")

    def test_unknown_historical_delivery_remains_visible_without_reactivating_old_topics(self):
        ledger = JobSearchLedger(self.fixture.path)
        notification = ledger.publish_notification(NotificationIntent("reminder.due", "historical",
            "Historical reminder", "Exact old reminder", self.application["id"]))["notification"]
        with closing(connect(self.fixture.path)) as con:
            con.execute("UPDATE notification_outbox SET status='dead',last_error='delivery_reconciliation_required' WHERE notification_id=?",
                (notification["notification_id"],))
            con.commit()
        report = readiness_report(self.fixture.path, application_backend="owners")
        self.assertEqual(report["metrics"]["pending_reconciliation"], 1)
        cap = next(c for c in report["capabilities"] if c["id"] == "notification_outbox")
        self.assertEqual(cap["reason_code"], "external_reconciliation_required")
        # Actual receipt resolution removes uncertainty; the retired topic does
        # not become active domain health merely because its history remains.
        with closing(connect(self.fixture.path)) as con:
            con.execute("UPDATE notification_outbox SET status='delivered' WHERE notification_id=?", (notification["notification_id"],))
            con.commit()
        self.assertEqual(readiness_report(self.fixture.path, application_backend="owners")["metrics"]["pending_reconciliation"], 0)

    def test_restore_acknowledgment_does_not_hide_quarantine_or_double_count_action(self):
        ops, executor = self.runtime.actions, self.runtime.executor
        context = lambda operation: CommandContext(Principal("human", "human", {"*"}), uuid.uuid4().hex)
        envelope = {"kind": "create_calendar_entry", "account_id": "fictional", "application_id": self.application["id"],
            "pursuit_no": 1, "target": {}, "context_versions": {"application:" + self.application["id"]: self.application["version"]},
            "payload": {"starts_at": (self.now + timedelta(days=1)).isoformat(), "ends_at": (self.now + timedelta(days=1, hours=1)).isoformat()}}
        action = executor.run(context("prepare_calendar_change"), "prepare_calendar_change", envelope,
            lambda tx: ops.prepare_calendar_change(tx, envelope))
        executor.run(context("authorize_action"), "authorize_action", {},
            lambda tx: ops.authorize_action(tx, action["action_id"], action["digest"], applicability=lambda tx, envelope: True))
        provider = action_fixtures.FakeProvider()
        provider.write_error = TimeoutError("Uncertain offline fixture")
        worker = ExternalActionWorker(executor, ops, provider, action_fixtures.ExternalActionsTest.worker_context,
            lambda tx, envelope: True, allow_test_dispatch=True)
        self.assertEqual(worker.execute(action["action_id"])["execution"], "uncertain")
        reminder = self.human("create_reminder", {"application_id": self.application["id"],
            "at": (self.now + timedelta(minutes=1)).isoformat(), "description": "Exact reminder"})
        self.now += timedelta(minutes=2)
        handoff = self.runtime.process_scheduled()["notifications"]["items"][0]
        with sqlite3.connect(executor.path) as con:
            con.execute("BEGIN IMMEDIATE")
            revision = record_restore(con, restored_at=self.stamp(), quarantined_records=[
                ("external_action", action["action_id"]), ("reminder", reminder["id"])])
        executor.acknowledge_restore(expected_restore_revision=revision, operator="fictional", reason="Reviewed uncertainty")
        state, caps = application_readiness(self.fixture.config)
        self.assertFalse(state["restore_review_required"])
        self.assertEqual((state["restore_quarantined_actions"], state["restore_quarantined_reminders"], state["uncertain"]), (1, 1, 2))
        self.assertEqual(caps[1]["reason_code"], "external_reconciliation_required")
        executor.run(action_fixtures.ExternalActionsTest.worker_context("reconcile_action", "actual-receipt"),
            "reconcile_action", {}, lambda tx: ops.reconcile_result(tx, action["action_id"],
                ProviderOutcome("succeeded", {"remote_id": "verified"},
                    {"verified": "created", "remote_id": "verified", "etag": "exact-version", "transaction_id": action["operation_id"]})))
        self.runtime.command(self.runtime.result_context("record_delivery", "actual-delivery"), "record_delivery",
            {"delivery_id": handoff["delivery_id"], "reminder_id": reminder["id"], "expected_version": reminder["version"],
             "receipt_id": "verified-sidecar-receipt", "outcome": "delivered", "observed_at": self.stamp()})
        state, _ = application_readiness(self.fixture.config)
        self.assertEqual((state["restore_quarantined_actions"], state["restore_quarantined_reminders"], state["uncertain"]), (0, 0, 0))

    def test_binding_preserves_provider_intent_while_cancelling_retired_execution(self):
        import tempfile
        from pathlib import Path
        from tests.test_job_search_ledger import make_service, start
        with tempfile.TemporaryDirectory() as directory:
            path, ledger = make_service(directory)
            app = start(ledger)["application"]["application_id"]
            pending = ledger.publish_notification(NotificationIntent("reminder.due", "unstarted", "Title", "Body", app))["notification"]
            delivering = ledger.publish_notification(NotificationIntent("reminder.due", "in-flight", "Title", "Body", app))["notification"]
            with closing(connect(path)) as con:
                con.execute("UPDATE notification_outbox SET status='delivering',lease_owner='old-worker',lease_token='old-token',lease_expires_at='2000-01-01T00:00:00Z' WHERE notification_id=?", (delivering["notification_id"],))
                con.execute("""INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,created_at,max_attempts,external_outcome,last_error)
                    VALUES('old-send','career.actions.execute','old-send','{}','running',?,?,1,'in_flight','provider response lost')""", (self.stamp(), self.stamp()))
                con.commit()
            report = convert_snapshot(path, Path(directory) / "owners")
            runtime = ApplicationRuntime(Path(directory) / "owners/candidate.sqlite")
            freeze_legacy(path, runtime, operator="fictional", report=report)
            with closing(connect(path)) as con:
                rows = {r["notification_id"]: dict(r) for r in con.execute("SELECT * FROM notification_outbox")}
                self.assertEqual(rows[pending["notification_id"]]["status"], "cancelled")
                self.assertEqual(rows[delivering["notification_id"]]["status"], "dead")
                self.assertEqual(rows[delivering["notification_id"]]["last_error"], "delivery_reconciliation_required")
                self.assertIsNone(rows[delivering["notification_id"]]["lease_token"])
                work = con.execute("SELECT status,external_outcome,last_error FROM work_items WHERE work_id='old-send'").fetchone()
                self.assertEqual(tuple(work), ("cancelled", "in_flight", "provider response lost"))
            self.assertEqual(readiness_report(path, application_backend="owners")["metrics"]["pending_reconciliation"], 2)

    def test_recovery_resolves_retired_delivery_without_requeueing_its_old_workflow(self):
        from types import SimpleNamespace
        from job_search.contracts import MutationContext
        from job_search.delivery_recovery import NotificationRecoveryService
        from job_search.hermes_delivery import delivery_fingerprint
        ledger = JobSearchLedger(self.fixture.path)
        for topic, application_id, outcome, expected in (
            ("reminder.due", self.application["id"], "not_delivered", "cancelled"),
            ("reminder.due", self.application["id"], "delivered", "delivered"),
            ("shortlist.ready", "", "not_delivered", "pending"),
        ):
            with self.subTest(topic=topic, outcome=outcome):
                identity = uuid.uuid4().hex
                notification = ledger.publish_notification(NotificationIntent(topic, identity, "Title", "Body", application_id))["notification"]
                nid = notification["notification_id"]
                with closing(connect(self.fixture.path)) as con:
                    con.execute("UPDATE notification_outbox SET status='dead',attempts=1,last_error='delivery_reconciliation_required' WHERE notification_id=?", (nid,))
                    con.commit()
                fingerprint = delivery_fingerprint("telegram:owner", "Title", "Body")
                receipt = {"state": "reconciliation_required", "attempts": 1, "payload_sha256": fingerprint}
                def reconcile(_identifier, **decision):
                    receipt["state"] = "retryable" if decision["outcome"] == "not_delivered" else "delivered"
                bridge = SimpleNamespace(expected_target="telegram:owner", status=lambda identifier: dict(receipt), reconcile=reconcile)
                result = NotificationRecoveryService(ledger, bridge).reconcile(nid,
                    expected_attempts=1, expected_payload_sha256=fingerprint, outcome=outcome,
                    context=MutationContext(identity, "user", "dashboard"))
                self.assertEqual(result["status"], expected)
                with closing(connect(self.fixture.path)) as con:
                    self.assertEqual(con.execute("SELECT status FROM notification_outbox WHERE notification_id=?", (nid,)).fetchone()[0], expected)

    def test_readiness_does_not_change_operational_or_owner_state(self):
        def snapshot(path):
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
                return tuple(con.iterdump())
        paths = (self.fixture.path, self.runtime.executor.path)
        before = [snapshot(path) for path in paths]
        readiness_report(self.fixture.path, application_backend="owners")
        application_readiness(self.fixture.config)
        self.assertEqual(before, [snapshot(path) for path in paths])


if __name__ == "__main__":
    unittest.main()
