"""Predetermined schedules and notification receipts use public owner participants."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid

from job_search.commands import CommandContext, CommandExecutor, Delegation, DomainError, Principal, digest
from job_search.applications.api import ApplicationOperations, SCHEMA


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = "2026-10-09T12:00:00Z"
        self.executor = CommandExecutor(Path(self.tmp.name) / "candidate.db", {"applications": SCHEMA}, clock=lambda: self.now)
        self.api = ApplicationOperations()
        self.human = Principal("human", "human", {"*"})
        self.app = self.human_call("save_job", {"job_source": {"source": "greenhouse", "source_id": "one"}})

    def human_call(self, operation, payload):
        return self.executor.run(CommandContext(self.human, uuid.uuid4().hex), operation, payload,
                                 lambda tx: getattr(self.api, operation)(tx, payload))

    def work(self, operation, payload, callback, origin="scheduled"):
        worker = Principal("scheduler", "worker", {operation})
        # This fixture also works against the pre-integration authority registry:
        # a bounded trusted grant cannot authorize any other operation or payload.
        grant = Delegation("human", "scheduler", operation, digest(payload), "2027-01-01T00:00:00Z")
        return self.executor.run(CommandContext(worker, uuid.uuid4().hex, origin, grant), operation, payload, callback)

    def schedule(self, **extra):
        return self.human_call("schedule_operation", {"application_id": self.app["id"], "operation": "create_task", "input": {"kind": "follow_up", "description": "Exact e\u0301\n follow-up"}, "due_at": "2026-10-10T12:00:00Z", "reason": "Create this specific task tomorrow", **extra})

    def run_schedule(self, identifier):
        return self.work("run_scheduled", {"schedule_id": identifier}, lambda tx: self.api.run_schedule(tx, identifier))

    def queue(self):
        return self.work("queue_due_reminders", {}, lambda tx: self.api.queue_due_reminders(tx))

    def receipt(self, handoff, receipt_id="receipt"):
        payload = {"delivery_id": handoff["delivery_id"], "reminder_id": handoff["reminder_id"], "expected_version": handoff["expected_version"], "receipt_id": receipt_id, "outcome": "delivered", "observed_at": self.now}
        return self.work("record_delivery", payload, lambda tx: self.api.record_delivery(tx, payload), "result")

    def test_schedule_authorizes_exact_one_shot_task_without_accepting_it_early(self):
        schedule = self.schedule()
        self.assertEqual(schedule["issuer_id"], "human")
        with self.executor.read() as con:
            self.assertEqual(self.api.list_records(con, self.app["id"], "tasks")["items"], [])
            self.assertEqual(self.api.due_schedules(con, self.now)["items"], [])
            self.assertEqual(con.execute("SELECT count(*) FROM command_history WHERE operation='create_task'").fetchone()[0], 0)
        with self.assertRaises(DomainError):
            self.run_schedule(schedule["id"])
        self.now = "2026-10-10T12:00:00Z"
        first = self.run_schedule(schedule["id"])
        self.assertEqual(first["task"]["description"], "Exact e\u0301\n follow-up")
        self.assertEqual(first["task"]["causation_id"], schedule["id"])
        self.assertEqual(first["schedule"]["status"], "executed")
        self.assertIsNone(self.run_schedule(schedule["id"])["task"])
        with self.executor.read() as con:
            self.assertEqual(len(self.api.list_records(con, self.app["id"], "tasks")["items"]), 1)

    def test_inferred_and_agent_requests_cannot_create_schedule_authority(self):
        payload = {"application_id": self.app["id"], "operation": "create_task", "input": {"kind": "follow_up", "description": "Inferred silence"}, "due_at": "2026-10-10T12:00:00Z", "reason": "Guess"}
        for principal, origin in ((self.human, "inferred"), (Principal("agent", "agent", {"propose_changes"}), "direct")):
            context = CommandContext(principal, uuid.uuid4().hex, origin)
            with self.assertRaises(DomainError):
                self.executor.run(context, "propose_changes", {}, lambda tx: self.api.schedule_operation(tx, payload))
        with self.assertRaises(DomainError):
            self.schedule(operation="send_reply")
        with self.assertRaises(DomainError):
            self.schedule(input={"kind": "follow_up", "description": "Conditional", "if_no_reply": True})

    def test_closure_cancels_schedules_and_reopening_does_not_restore_them(self):
        schedule = self.schedule()
        with self.executor.read() as con:
            preview = self.api.preview_closure(con, self.app["id"])
        self.assertIn("schedules:" + schedule["id"], preview["expected_records"])
        closed = self.human_call("close_application", {"application_id": self.app["id"], "expected_version": 1, "expected_records": preview["expected_records"], "reason": "Stop"})
        self.human_call("reopen_application", {"application_id": self.app["id"], "expected_version": closed["version"], "reason": "New pursuit"})
        self.now = "2026-10-10T12:00:00Z"
        result = self.run_schedule(schedule["id"])
        self.assertEqual(result["schedule"]["status"], "cancelled")
        self.assertIsNone(result["task"])

    def test_schedule_consumption_and_task_creation_rollback_together(self):
        schedule = self.schedule()
        self.now = "2026-10-10T12:00:00Z"
        def broken(tx):
            self.api.run_schedule(tx, schedule["id"])
            raise RuntimeError("process interrupted")
        with self.assertRaises(RuntimeError):
            self.work("run_scheduled", {"schedule_id": schedule["id"]}, broken)
        with self.executor.read() as con:
            self.assertEqual(self.api.get_record(con, "schedules", schedule["id"])["status"], "pending")
            self.assertEqual(self.api.list_records(con, self.app["id"], "tasks")["items"], [])
        self.assertEqual(self.run_schedule(schedule["id"])["schedule"]["status"], "executed")

    def test_cancel_schedule_requires_version(self):
        schedule = self.schedule()
        with self.assertRaises(DomainError):
            self.human_call("cancel_schedule", {"schedule_id": schedule["id"], "expected_version": 9, "reason": "Stale"})
        self.human_call("cancel_schedule", {"schedule_id": schedule["id"], "expected_version": 1, "reason": "No longer needed"})
        self.now = "2026-10-10T12:00:00Z"
        self.assertIsNone(self.run_schedule(schedule["id"])["task"])

    def test_notification_handoff_and_receipt_once_never_complete_task(self):
        task = self.human_call("create_task", {"application_id": self.app["id"], "kind": "reply", "description": "Reply", "due_at": "2026-10-10T12:00:00Z", "reminders_enabled": True})
        self.assertEqual(self.queue()["items"], [])
        self.now = "2026-10-10T12:00:00Z"
        handoff = self.queue()["items"][0]
        self.assertEqual(handoff["destination"], "owner")
        self.assertEqual(self.queue()["items"], [])
        first = self.receipt(handoff)
        self.assertEqual(first["status"], "delivered")
        self.assertEqual(self.receipt(handoff), first)
        with self.executor.read() as con:
            self.assertEqual(self.api.get_record(con, "tasks", task["id"])["status"], "open")
            self.assertEqual(self.api.get_record(con, "reminders", handoff["reminder_id"])["status"], "delivered")
            self.assertEqual(con.execute("SELECT count(*) FROM command_work WHERE kind='deliver_owner_notification'").fetchone()[0], 1)

    def test_delivery_after_snooze_records_reality_without_overwriting_new_schedule(self):
        task = self.human_call("create_task", {"application_id": self.app["id"], "kind": "reply", "description": "Reply", "due_at": "2026-10-10T12:00:00Z", "reminders_enabled": True})
        self.now = "2026-10-10T12:00:00Z"
        handoff = self.queue()["items"][0]
        self.human_call("snooze_task", {"task_id": task["id"], "expected_version": 1, "until": "2026-10-11T12:00:00Z"})
        result = self.receipt(handoff)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["observed_outcome"], "delivered")
        with self.executor.read() as con:
            self.assertEqual(self.api.get_record(con, "reminders", handoff["reminder_id"])["status"], "pending")
            self.assertEqual(self.api.get_record(con, "tasks", task["id"])["status"], "open")

    def test_imported_pending_reminder_stays_inert_until_human_reenables(self):
        worker = Principal("converter", "worker", {"import_snapshot"})
        payload = {"id": "historical-reminder", "application_id": self.app["id"], "status": "pending", "kind": "standalone", "related_id": None, "at": "2026-10-08T12:00:00Z", "next_notification_at": "2026-10-08T12:00:00Z"}
        self.executor.run(CommandContext(worker, "import", "migration"), "import_snapshot", payload, lambda tx: self.api.import_record(tx, "reminders", payload))
        self.assertEqual(self.queue()["items"], [])
        self.human_call("reenable_reminder", {"reminder_id": "historical-reminder", "expected_version": 1, "due_at": "2026-10-10T12:00:00Z", "reason": "Notify tomorrow instead"})
        self.now = "2026-10-10T12:00:00Z"
        self.assertEqual(self.queue()["items"][0]["reminder_id"], "historical-reminder")

    def test_notification_receipt_requires_exact_handoff_and_trusted_result(self):
        reminder = self.human_call("create_reminder", {"application_id": self.app["id"], "at": "2026-10-10T12:00:00Z", "description": "Reminder"})
        self.now = "2026-10-10T12:00:00Z"
        handoff = self.queue()["items"][0]
        payload = {"delivery_id": handoff["delivery_id"], "reminder_id": reminder["id"], "expected_version": 1, "receipt_id": "r", "outcome": "delivered", "observed_at": self.now}
        with self.assertRaises(DomainError):
            self.human_call("record_delivery", payload)
        for wrong in ({"expected_version": 2}, {"reminder_id": "another"}, {"observed_at": "2026-10-09T12:00:00Z"}):
            bad = {**payload, **wrong}
            with self.assertRaises(DomainError):
                self.work("record_delivery", bad, lambda tx: self.api.record_delivery(tx, bad), "result")
        self.receipt(handoff, "r")
        altered = {**payload, "outcome": "delivered", "observed_at": "2026-10-10T11:59:59Z"}
        with self.assertRaises(DomainError):
            self.work("record_delivery", altered, lambda tx: self.api.record_delivery(tx, altered), "result")


if __name__ == "__main__":
    unittest.main()
