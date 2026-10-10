"""Agent-sized reply input resolves trusted provider context without side effects."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid

from job_search.application_preparation import ReplyPreparationService, ReplyProviderContext
from job_search.application_runtime import ApplicationRuntime
from job_search.commands import CommandContext, DomainError, Principal


class ReadOnlyProvider:
    def __init__(self):
        self.calls = []
        self.on_read = None
        self.message = {"id": "remote-source", "isDraft": False, "subject": "Interview",
                        "body": {"contentType": "text", "content": "Please reply."},
                        "from": {"emailAddress": {"address": "recruiter@example.test"}},
                        "lastModifiedDateTime": "2026-10-09T11:00:00Z"}

    def read_message_body(self, provider_message_id):
        self.calls.append(provider_message_id)
        if self.on_read:
            self.on_read()
        return deepcopy(self.message)


class PreparationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / "candidate.db", clock=lambda: "2026-10-09T12:00:00Z")
        self.human = Principal("human", "human", {"*"})
        self.app = self.command("save_job", {"job_source": {"source": "fixture", "source_id": "one"}})
        worker = Principal("mail", "worker", {"record_message"})
        self.message = self.runtime.command(CommandContext(worker, "message", "inferred"), "record_message",
            {"account_id": "account", "provider_message_id": "remote-source", "source_version": 1,
             "direction": "incoming", "authored_text": "Please reply.", "archive_ref": "private-reference"})
        self.link = self.command("link_message", {"message_id": self.message["id"], "application_id": self.app["id"]})
        self.client = ReadOnlyProvider()
        self.resolved_accounts = []
        def resolver(account):
            self.resolved_accounts.append(account)
            return ReplyProviderContext("account", self.client)
        self.service = ReplyPreparationService(self.runtime, resolver)

    def command(self, operation, payload):
        return self.runtime.command(CommandContext(self.human, uuid.uuid4().hex), operation, payload)

    def task(self, application_id=None, rule="verified_send"):
        return self.command("create_task", {"application_id": application_id or self.app["id"], "kind": "reply", "description": "Reply", "completion_rule": rule})

    def save_prepared(self, result):
        # The real runtime integration performs exactly these public participant
        # calls after read-only preparation. No provider work occurs in this txn.
        envelope = result["envelope"]
        def save(tx):
            self.runtime.workflows.applicability(tx, envelope)
            return self.runtime.actions.prepare_reply(tx, envelope)
        return self.runtime.executor.run(CommandContext(self.human, uuid.uuid4().hex), "prepare_reply", {"envelope": envelope}, save)

    def test_agent_sized_request_builds_exact_bound_envelope_without_mutation(self):
        task = self.task()
        with self.runtime.executor.read() as con:
            history_before = con.execute("SELECT count(*) FROM command_history").fetchone()[0]
        result = self.service.prepare(self.message["id"], " Exact e\u0301\nreply ", task_id=task["id"])
        self.assertEqual(result["status"], "ready")
        envelope = result["envelope"]
        self.assertEqual(envelope["payload"], {"recipients": ["recruiter@example.test"], "subject": "Re: Interview", "body": " Exact e\u0301\nreply "})
        self.assertEqual(envelope["target"]["source_hash"], self.message["source_sha256"])
        self.assertEqual(envelope["context_versions"], {"application:" + self.app["id"]: 1, "message:" + self.message["id"]: 1, "association:" + self.link["id"]: 1, "task:" + task["id"]: 1})
        self.assertEqual(self.client.calls, ["remote-source"])
        self.assertEqual(self.resolved_accounts, ["account"])
        with self.runtime.executor.read() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM command_history").fetchone()[0], history_before)
            self.assertEqual(self.runtime.actions.list_actions(con, self.app["id"])["items"], [])
        action = self.save_prepared(result)
        self.assertEqual(action["authorization"], "pending")
        self.assertEqual(action["execution"], "not_started")

    def test_missing_or_wrong_account_context_blocks_without_provider_calls(self):
        service = ReplyPreparationService(self.runtime)
        self.assertEqual(service.prepare(self.message["id"], "Reply")["blockers"], ["provider_context_unavailable"])
        service = ReplyPreparationService(self.runtime, lambda account: ReplyProviderContext("another-account", self.client))
        self.assertEqual(service.prepare(self.message["id"], "Reply")["blockers"], ["account_mismatch"])
        self.assertEqual(self.client.calls, [])

    def test_missing_provider_identity_does_not_fabricate_a_ready_envelope(self):
        self.client.message["id"] = "unexpected-source"
        result = self.service.prepare(self.message["id"], "Reply")
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["envelope"])

    def test_changed_context_during_provider_read_is_rejected_by_owner_transaction(self):
        task = self.task()
        self.client.on_read = lambda: self.command("snooze_task", {"task_id": task["id"], "expected_version": 1, "until": "2026-10-10T12:00:00Z"})
        result = self.service.prepare(self.message["id"], "Reply", task_id=task["id"])
        self.assertEqual(result["status"], "ready")
        with self.assertRaises(DomainError) as caught:
            self.save_prepared(result)
        self.assertEqual(caught.exception.code, "version_conflict")
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.actions.list_actions(con, self.app["id"])["items"], [])

    def test_changed_association_prevents_saving_to_old_application(self):
        result = self.service.prepare(self.message["id"], "Reply")
        other = self.command("save_job", {"job_source": {"source": "fixture", "source_id": "two"}})
        self.runtime.executor.run(CommandContext(self.human, "relink"), "correct_association", {},
            lambda tx: self.runtime.correspondence.correct_association(tx, association_id=self.link["id"], application_id=other["id"], expected_version=1, reason="Reviewed correction"))
        with self.assertRaises(DomainError):
            self.save_prepared(result)

    def test_task_must_be_current_exact_communication_consequence(self):
        task = self.task(rule="human_decision")
        self.assertEqual(self.service.prepare(self.message["id"], "Reply", task_id=task["id"])["blockers"], ["task_consequence_not_applicable"])
        task = self.task()
        result = self.service.prepare(self.message["id"], "Draft", kind="create_reply_draft", task_id=task["id"])
        self.assertEqual(result["blockers"], ["draft_cannot_complete_task"])
        draft = self.service.prepare(self.message["id"], "Draft", kind="create_reply_draft")
        self.assertIsNone(draft["envelope"]["consequence"])

    def test_post_close_reply_requires_explicit_choice_and_has_no_task_consequence(self):
        self.command("close_application", {"application_id": self.app["id"], "expected_version": 1, "reason": "Withdraw", "outcome": "withdrawn"})
        self.assertEqual(self.service.prepare(self.message["id"], "Thank you")["blockers"], ["application_closed"])
        result = self.service.prepare(self.message["id"], "Thank you", allow_closed=True)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["envelope"]["allow_closed"])
        self.assertEqual(self.save_prepared(result)["authorization"], "pending")

    def test_unlinked_message_requires_review_before_provider_lookup(self):
        worker = Principal("mail", "worker", {"record_message"})
        unlinked = self.runtime.command(CommandContext(worker, "unlinked", "inferred"), "record_message", {"account_id": "account", "provider_message_id": "unlinked-source", "source_version": 1, "direction": "incoming", "authored_text": "Other", "archive_ref": "other-reference"})
        result = self.service.prepare(unlinked["id"], "Reply")
        self.assertEqual(result["blockers"], ["reviewed_association_required"])
        self.assertEqual(self.resolved_accounts, [])


if __name__ == "__main__":
    unittest.main()
