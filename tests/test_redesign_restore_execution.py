"""Restored snapshots cannot replay approvals or notifications after activation."""
from contextlib import closing
from pathlib import Path
import sqlite3
import unittest

from job_search.application_execution import ProductionExecution
from job_search.application_runtime import ApplicationRuntime
from job_search.commands import CommandExecutor, DomainError
from job_search.commands.installation import (
    record_restore, _PRE_RESTORE_SCHEMA, SCHEMA as INSTALLATION_SCHEMA,
)
from job_search.external_actions.api import SCHEMA
from job_search.external_actions.worker import ExternalActionWorker
from tests import test_redesign_external_actions as action_fixtures
from tests import test_redesign_execution as notification_fixtures


def copy_database(executor, destination):
    with executor.read() as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)


def mark_restored(path, now, records):
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return record_restore(connection, restored_at=now, quarantined_records=records)


def reviewed_activation(executor):
    revision = executor.restore_status()["revision"]
    with unittest.TestCase().assertRaises(DomainError):
        executor.set_activation(paused=False, expected_revision=executor.activation_status()["revision"],
            operator="fictional", reason="Cannot skip review")
    executor.acknowledge_restore(expected_restore_revision=revision,
        operator="fictional", reason="Reviewed restored uncertainty; old work remains quarantined")
    assert executor.activation_status()["paused"]
    executor.set_activation(paused=False, expected_revision=executor.activation_status()["revision"],
        operator="fictional", reason="Enable newly authorized work")


class RestoredActionsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = action_fixtures.ExternalActionsTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def worker(self, executor):
        f = self.fixture
        return ExternalActionWorker(executor, f.ops, f.provider, f.worker_context,
            lambda tx, envelope: True, activation_status=executor.activation_status,
            activation_guard=executor.activation_guard)

    def test_review_cannot_reenable_action_executed_after_snapshot(self):
        f = self.fixture
        action = f.approve(f.prepare())
        f.executor.set_activation(paused=False, expected_revision=0, operator="fictional", reason="Test")
        backup = Path(f.tmp.name) / "restored.sqlite"
        copy_database(f.executor, backup)
        self.worker(f.executor).execute(action["action_id"])
        self.assertEqual(f.provider.writes, 1)
        mark_restored(backup, f.executor.clock(), [("external_action", action["action_id"])])
        restored = CommandExecutor(backup, {"external_actions": SCHEMA}, clock=f.executor.clock)
        reviewed_activation(restored)
        self.assertEqual(self.worker(restored).execute(action["action_id"])["status"], "restore_quarantined")
        self.assertEqual(f.provider.writes, 1)
        with restored.read() as con:
            preserved = f.ops.get(con, action["action_id"])
            self.assertEqual((preserved["authorization"], preserved["execution"]), ("approved", "queued"))
            self.assertEqual(con.execute("SELECT count(*) FROM action_attempts").fetchone()[0], 0)
        # Owner APIs also reject a direct claim, independently of composition.
        with self.assertRaises(DomainError):
            restored.run(f.worker_context("claim_action", "bypass"), "claim_action", {},
                lambda tx: f.ops.claim_action(tx, action["action_id"], "worker"))
        # The reviewed installation remains useful: a new exact proposal and
        # human authorization can execute without releasing the historical one.
        f.executor = restored
        fresh = f.approve(f.prepare(body="New reviewed request"))
        self.assertEqual(self.worker(restored).execute(fresh["action_id"])["execution"], "awaiting_confirmation")
        self.assertEqual(f.provider.writes, 2)

    def test_restored_write_intent_is_reconciled_without_resuming_writes(self):
        f = self.fixture
        action = f.approve(f.prepare())
        f.executor.set_activation(paused=False, expected_revision=0, operator="fictional", reason="Test")
        self.worker(f.executor).execute(action["action_id"])
        backup = Path(f.tmp.name) / "restored.sqlite"
        copy_database(f.executor, backup)
        mark_restored(backup, f.executor.clock(), [("external_action", action["action_id"])])
        restored = CommandExecutor(backup, {"external_actions": SCHEMA}, clock=f.executor.clock)
        reviewed_activation(restored)
        result = self.worker(restored).reconcile(action["action_id"])
        self.assertEqual(result["execution"], "succeeded")
        self.assertEqual(f.provider.writes, 1)


class RestoredNotificationsTest(unittest.TestCase):
    def test_sidecar_rewind_and_later_due_reminder_stay_quarantined(self):
        f = notification_fixtures.NotificationExecutionTest()
        f.setUp()
        self.addCleanup(f.doCleanups)
        future = f.human("create_reminder", {"application_id": f.reminder["application_id"],
            "at": "2026-10-09T12:30:00Z", "description": "Future reminder"})
        f.activate()
        backup = Path(f.tmp.name) / "restored.sqlite"
        copy_database(f.runtime.executor, backup)
        f.execution.deliver_owner_notification(f.handoff)
        receipt = dict(f.client.receipts[f.handoff["delivery_id"]])
        f.client.receipts.clear()  # The sidecar snapshot predates the first send.
        mark_restored(backup, f.now, [("reminder", f.reminder["id"]), ("reminder", future["id"])])
        restored = ApplicationRuntime(backup, clock=lambda: f.now)
        reviewed_activation(restored.executor)
        execution = ProductionExecution(restored, lambda account: None,
            restored.executor.activation_status, restored.executor.activation_guard,
            notification_client=f.client, notification_target="telegram")
        self.assertEqual(execution.deliver_owner_notification(f.handoff),
            {"status": "restore_quarantined", "work_complete": False})
        f.now = "2026-10-09T12:31:00Z"
        later = restored.process_scheduled()["notifications"]["items"]
        future_handoff = next(item for item in later if item["reminder_id"] == future["id"])
        self.assertEqual(execution.deliver_owner_notification(future_handoff)["status"], "restore_quarantined")
        self.assertEqual(f.client.sends, 1)
        # Positive receipt recovery is allowed; quarantine only forbids writes.
        f.client.receipts[f.handoff["delivery_id"]] = receipt
        self.assertTrue(execution.deliver_owner_notification(f.handoff)["work_complete"])
        self.assertEqual(f.client.sends, 1)


class RestoreSchemaTest(unittest.TestCase):
    def test_exact_prior_schema_upgrade_and_stale_review(self):
        import hashlib
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prior.sqlite"
            with sqlite3.connect(path) as con:
                con.executescript(_PRE_RESTORE_SCHEMA)
                con.execute("CREATE TABLE command_schema_versions(owner TEXT PRIMARY KEY,checksum TEXT NOT NULL)")
                con.execute("INSERT INTO command_schema_versions VALUES('installation',?)",
                    (hashlib.sha256(_PRE_RESTORE_SCHEMA.encode()).hexdigest(),))
            first = mark_restored(path, "2026-10-09T12:00:00Z", [("reminder", "old-reminder")])
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("SELECT checksum FROM command_schema_versions WHERE owner='installation'").fetchone()[0],
                    hashlib.sha256(INSTALLATION_SCHEMA.encode()).hexdigest())
            executor = CommandExecutor(path, {})
            second = mark_restored(path, "2026-10-09T12:01:00Z", [("reminder", "old-reminder")])
            with self.assertRaises(DomainError):
                executor.acknowledge_restore(expected_restore_revision=first, operator="fictional", reason="stale review")
            status = executor.acknowledge_restore(expected_restore_revision=second, operator="fictional", reason="reviewed both")
            self.assertFalse(status["required"])
            self.assertEqual(status["quarantined"], {"reminder": 1})
            with sqlite3.connect(path) as con:
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute("DELETE FROM installation_restore_quarantine")


if __name__ == "__main__":
    unittest.main()
