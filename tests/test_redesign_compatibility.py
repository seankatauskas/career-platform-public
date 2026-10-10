"""Compatibility translates meaning without granting authority or writing state."""
import unittest

from job_search.application_compatibility import translate_dashboard, translate_tool, legacy_record
from job_search.commands import DomainError


class CompatibilityTest(unittest.TestCase):
    def test_dashboard_task_and_agent_proposal_have_same_exact_payload(self):
        values = {"kind": "reply", "owner": "applicant", "note": " e\u0301\n ", "due_at": "2026-10-10T12:00:00Z"}
        dashboard = translate_dashboard("/api/v1/lifecycle/tasks/create", {"application_id": "app", "values": values, "idempotency_key": "one"})
        agent = translate_tool("propose_application_update", {"application_id": "app", "kind": "task", "payload": {"values": values}, "idempotency_key": "two"})
        self.assertEqual(dashboard.input, agent.input)
        self.assertEqual(dashboard.kind, "command")
        self.assertEqual(agent.kind, "proposal")
        self.assertEqual(agent.operation, "create_task")
        self.assertEqual(agent.input["description"], values["note"])
        self.assertEqual(agent.idempotency_key, "two")

    def test_updates_require_explicit_relevant_revision(self):
        with self.assertRaises(DomainError):
            translate_dashboard("tasks/transition", {"task_id": "t", "operation": "complete", "values": {"reason": "Done"}})
        result = translate_dashboard("tasks/transition", {"task_id": "t", "operation": "complete", "values": {"reason": "Done", "revision_no": 3}})
        self.assertEqual(result.input, {"task_id": "t", "expected_version": 3, "reason": "Done"})
        with self.assertRaises(DomainError):
            translate_dashboard("tasks/transition", {"task_id": "t", "operation": "complete", "values": {"reason": "Done", "revision_no": 3, "expected_version": 3}})

    def test_interview_proposal_does_not_authorize_calendar_or_lifecycle_change(self):
        result = translate_tool("propose_interview_revision", {"application_id": "app", "details": {"status": "confirmed", "starts_at": "2026-10-10T12:00:00Z", "ends_at": "2026-10-10T13:00:00Z", "time_zone": "America/Chicago", "participants": ["Recruiter"], "join_url": "https://meet.example/room"}, "idempotency_key": "one"})
        self.assertEqual(result.kind, "proposal")
        self.assertEqual(result.operation, "schedule_interview")
        self.assertEqual(result.input["status"], "scheduled")
        self.assertEqual(result.input["timezone"], "America/Chicago")
        self.assertNotIn("authorize_action", result.input)
        with self.assertRaises(DomainError):
            translate_tool("propose_interview_revision", {"application_id": "app", "details": {"round_id": "round", "status": "rescheduled"}, "idempotency_key": "two"})

    def test_obsolete_automatic_and_implicit_semantics_rejected(self):
        for path in ("follow-up/configure", "interviews/import", "mail/link", "corrections/decide"):
            with self.assertRaises(DomainError):
                translate_dashboard(path, {})
        with self.assertRaises(DomainError):
            translate_dashboard("tasks/create", {"application_id": "app", "values": {"kind": "reply"}, "auto_apply": True})
        with self.assertRaises(DomainError):
            translate_tool("propose_application_update", {"application_id": "app", "kind": "phase", "payload": {"target_phase": "active"}})
        with self.assertRaises(DomainError):
            translate_dashboard("tasks/create", {"application_id": "app", "values": {"kind": "reply", "evidence_id": "unmapped-legacy-id"}})

    def test_reminders_use_proposals_and_stable_query_cursors(self):
        reminder = translate_tool("create_reminder", {"application_id": "app", "due_at": "2026-10-10T12:00:00Z", "note": "Call", "idempotency_key": "remind"})
        self.assertEqual(reminder.kind, "proposal")
        self.assertEqual(reminder.input["description"], "Call")
        query = translate_tool("list_application_tasks", {"application_id": "app", "limit": 10, "offset": 0, "cursor": "cursor"})
        self.assertEqual(query.kind, "query")
        self.assertEqual(query.operation, "tasks")
        self.assertNotIn("offset", query.input)
        with self.assertRaises(DomainError):
            translate_tool("list_application_tasks", {"application_id": "app", "offset": 10})
        self.assertEqual(translate_tool("list_reminders", {"status": "completed"}).input["status"], "delivered")

    def test_response_aliases_are_pure(self):
        original = {"id": "task", "version": 3, "description": "Exact", "responsible_party": "employer"}
        result = legacy_record("tasks", original)
        self.assertEqual(result["revision_no"], 3)
        self.assertEqual(result["owner"], "employer")
        self.assertEqual(result["note"], "Exact")
        self.assertNotIn("revision_no", original)


if __name__ == "__main__":
    unittest.main()
