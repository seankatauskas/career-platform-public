"""External actions fail closed at every uncertain provider boundary; no network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import uuid

from job_search.commands import CommandContext, CommandExecutor, DomainError, Principal
from job_search.external_actions.api import ExternalActionOperations, ProviderOutcome, SCHEMA, PreEffectTransientError
from job_search.external_actions.service import digest
from job_search.external_actions.service import validate_envelope
from job_search.external_actions.reply import ReplyProvider, source_digest
from job_search.external_actions.calendar import CalendarProvider
from job_search.external_actions.worker import ExternalActionWorker
from job_search.external_actions.preparation import prepare_reply_context


class FakeProvider:
    test_only = True

    def __init__(self):
        self.writes = 0
        self.reads = 0
        self.preflight_error = None
        self.write_error = None
        self.outcome = ProviderOutcome("accepted", {"remote_id": "remote"})
        self.reconciled = ProviderOutcome("succeeded", {"remote_id": "remote"}, {"verified": "sent", "remote_id": "remote"})

    def preflight(self, action):
        self.reads += 1
        if self.preflight_error:
            raise self.preflight_error

    def perform(self, action):
        self.writes += 1
        if self.write_error:
            raise self.write_error
        return self.outcome

    def reconcile(self, action):
        self.reads += 1
        return self.reconciled


class ExternalActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.executor = CommandExecutor(Path(self.tmp.name) / "candidate.db", {"external_actions": SCHEMA}, clock=lambda: self.now.isoformat().replace("+00:00", "Z"))
        self.ops = ExternalActionOperations()
        self.human = Principal("human", "human", {"*"})
        self.provider = FakeProvider()
        self.applicable = True
        self.worker = ExternalActionWorker(self.executor, self.ops, self.provider, self.worker_context,
                                           lambda tx, envelope: self.applicable, allow_test_dispatch=True)

    @staticmethod
    def worker_context(operation, key):
        return CommandContext(Principal("worker", "worker", {operation}), key, "result")

    def human_run(self, op, payload, fn, key=None):
        return self.executor.run(CommandContext(self.human, key or uuid.uuid4().hex), op, payload, fn)

    def work_run(self, op, fn):
        return self.executor.run(self.worker_context(op, uuid.uuid4().hex), op, {}, fn)

    def prepare(self, *, kind="send_reply", consequence=True, body="Exact e\u0301\n  body"):
        envelope = {"kind": kind, "account_id": "account", "application_id": "app", "pursuit_no": 1,
                    "target": {"message_id": "message", "provider_message_id": "provider-message", "source_hash": "a" * 64, "provider_source_hash": "b" * 64},
                    "payload": {"recipients": ["recruiter@example.com"], "subject": "Re: Position", "body": body},
                    "context_versions": {"application:app": 1},
                    "consequence": {"operation": "complete_task", "task_id": "task", "expected_version": 1,
                                    "completion_rule": "verified_send"} if consequence else None}
        return self.human_run("prepare_reply", envelope, lambda tx: self.ops.prepare_reply(tx, envelope))

    def approve(self, action):
        return self.human_run("authorize_action", {"id": action["action_id"], "digest": action["digest"]},
                              lambda tx: self.ops.authorize_action(tx, action["action_id"], action["digest"], applicability=lambda tx, env: self.applicable))

    def get(self, action):
        with self.executor.read() as con:
            return self.ops.get(con, action["action_id"])

    def test_exact_approval_includes_context_account_recipient_and_unicode(self):
        action = self.prepare()
        self.assertNotEqual(action["digest"], self.prepare(body="Exact é\n  body")["digest"])
        approved = self.approve(action)
        self.assertEqual(approved["approved_until"], "2026-10-09T12:15:00.000000Z")
        self.assertEqual(approved["envelope"]["payload"]["body"], "Exact e\u0301\n  body")
        self.assertEqual(approved["digest"], digest(approved["envelope"]))
        with self.assertRaises(DomainError):
            self.human_run("authorize_action", {}, lambda tx: self.ops.authorize_action(tx, action["action_id"], "bad", applicability=lambda tx, e: True))
        with self.executor.read() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM command_work WHERE kind='execute_action'").fetchone()[0], 1)

    def test_agent_cannot_authorize_through_nested_bookkeeping_operation(self):
        action = self.prepare()
        context = CommandContext(Principal("agent", "agent", {"prepare_reply"}), "attack")
        with self.assertRaises(DomainError):
            self.executor.run(context, "prepare_reply", {}, lambda tx: self.ops.authorize_action(tx, action["action_id"], action["digest"], applicability=lambda tx, e: True))

    def test_expiry_and_context_change_prevent_write(self):
        action = self.approve(self.prepare())
        self.now += timedelta(minutes=16)
        self.assertEqual(self.worker.execute(action["action_id"])["execution"], "cancelled")
        self.assertEqual(self.provider.writes, 0)
        action = self.approve(self.prepare())
        self.applicable = False
        self.assertEqual(self.worker.execute(action["action_id"])["execution"], "cancelled")
        self.assertEqual(self.provider.writes, 0)

    def test_owner_context_exception_persists_blocked_attempt_before_any_write(self):
        action = self.approve(self.prepare())
        def stale(tx, envelope):
            raise DomainError("version_conflict", "Task changed after approval")
        self.worker.applicability = stale
        result = self.worker.execute(action["action_id"])
        self.assertEqual(result["execution"], "cancelled")
        self.assertEqual(result["authorization"], "revoked")
        self.assertEqual(result["error_code"], "context_invalid:version_conflict")
        self.assertEqual(self.provider.writes, 0)
        with self.executor.read() as con:
            attempt = con.execute("SELECT ended_at,outcome FROM action_attempts WHERE action_id=?", (action["action_id"],)).fetchone()
            self.assertIsNotNone(attempt["ended_at"])
            self.assertEqual(attempt["outcome"], "cancelled")
            self.assertEqual(self.ops.list_results(con)["items"], [])
        pending = self.prepare()
        with self.assertRaises(DomainError):
            self.human_run("authorize_action", {}, lambda tx: self.ops.authorize_action(tx, pending["action_id"], pending["digest"], applicability=stale))
        self.assertEqual(self.get(pending)["authorization"], "pending")

    def test_approval_rejects_unimplemented_targets_and_ignored_delete_payload(self):
        calendar = {"kind": "create_calendar_entry", "account_id": "account", "application_id": "app", "pursuit_no": 1,
                    "context_versions": {"application:app": 1}, "target": {},
                    "payload": {"starts_at": "2026-10-10T12:00:00Z", "ends_at": "2026-10-10T13:00:00Z"}}
        self.assertEqual(validate_envelope(calendar)["target"], {})
        with self.assertRaises(DomainError):
            validate_envelope({**calendar, "target": {"calendar_id": "different-calendar"}})
        with self.assertRaises(DomainError):
            validate_envelope({**calendar, "kind": "update_calendar_entry", "target": {"remote_id": "r", "etag": "e", "transaction_id": "t", "calendar_id": "different-calendar"}})
        with self.assertRaises(DomainError):
            validate_envelope({**calendar, "kind": "cancel_calendar_entry", "target": {"remote_id": "r", "etag": "e", "transaction_id": "t"}})
        action = self.prepare()
        envelope = {key: value for key, value in action["envelope"].items() if key not in {"encoding_version", "operation_id", "expires_at"}}
        envelope["target"] = {**envelope["target"], "recipient": "ignored@example.com"}
        with self.assertRaises(DomainError):
            validate_envelope(envelope)

    def test_preflight_retry_backoff_prevents_early_dispatch(self):
        action = self.approve(self.prepare())
        self.provider.preflight_error = PreEffectTransientError()
        result = self.worker.execute(action["action_id"])
        self.assertEqual(result["execution"], "queued")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(self.worker.execute(action["action_id"])["attempt_count"], 1)
        self.assertEqual(self.provider.reads, 1)

    def test_accepted_is_not_sent_and_result_delivery_survives_restart(self):
        action = self.approve(self.prepare())
        self.assertEqual(self.worker.execute(action["action_id"])["execution"], "awaiting_confirmation")
        with self.executor.read() as con:
            self.assertEqual(self.ops.list_results(con)["items"], [])
        self.now += timedelta(hours=1)
        self.assertEqual(self.worker.reconcile(action["action_id"])["execution"], "succeeded")
        with self.executor.read() as con:
            results = self.ops.list_results(con, delivery="pending")["items"]
            self.assertEqual(len(results), 1)
            self.assertEqual(con.execute("SELECT count(*) FROM command_work WHERE kind='apply_action_result'").fetchone()[0], 1)
        self.worker.reconcile(action["action_id"])
        self.assertEqual(self.provider.writes, 1)
        result = results[0]
        acknowledged = self.work_run("acknowledge_result", lambda tx: self.ops.acknowledge_result(tx, result["result_id"], "conflict", reason="task_changed"))
        self.assertEqual(acknowledged["delivery"], "conflict")
        self.assertEqual(self.get(action)["execution"], "succeeded")
        self.assertEqual(self.work_run("acknowledge_result", lambda tx: self.ops.acknowledge_result(tx, result["result_id"], "applied"))["delivery"], "conflict")

    def test_timeout_cannot_be_retried_even_if_human_reapproves(self):
        action = self.approve(self.prepare())
        self.provider.write_error = TimeoutError()
        self.assertEqual(self.worker.execute(action["action_id"])["execution"], "uncertain")
        with self.assertRaises(DomainError):
            self.worker.execute(action["action_id"])
        with self.assertRaises(DomainError):
            self.approve(action)
        self.assertEqual(self.provider.writes, 1)

    def test_pre_effect_transient_retry_is_bounded(self):
        action = self.approve(self.prepare())
        self.provider.preflight_error = PreEffectTransientError()
        for attempt in range(5):
            result = self.worker.execute(action["action_id"])
            self.assertEqual(result["attempt_count"], attempt + 1)
            self.now += timedelta(seconds=min(300, 30 * 2 ** attempt) + 1)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(self.provider.writes, 0)

    def test_proven_nonexecution_can_retry_only_under_valid_approval(self):
        action = self.approve(self.prepare())
        self.provider.write_error = TimeoutError()
        self.worker.execute(action["action_id"])
        self.provider.reconciled = ProviderOutcome("not_executed", observation={"proof": "provider_negative_receipt"})
        self.assertEqual(self.worker.reconcile(action["action_id"])["execution"], "queued")
        self.provider.write_error = None
        self.now += timedelta(seconds=31)
        self.worker.execute(action["action_id"])
        self.now += timedelta(minutes=16)
        self.assertEqual(self.worker.reconcile(action["action_id"])["execution"], "failed")

    def test_lease_loss_fences_late_worker_and_does_not_reset_intent(self):
        action = self.approve(self.prepare())
        claim = self.work_run("claim_action", lambda tx: self.ops.claim_action(tx, action["action_id"], "one"))
        self.work_run("begin_write", lambda tx: self.ops.begin_write(tx, action["action_id"], claim["fence"], applicability=lambda tx, env: True))
        self.now += timedelta(seconds=61)
        self.work_run("expire_actions", self.ops.expire_leases)
        self.assertEqual(self.get(action)["execution"], "uncertain")
        late = self.work_run("finish_attempt", lambda tx: self.ops.finish_attempt(tx, action["action_id"], claim["fence"], ProviderOutcome("succeeded")))
        self.assertEqual(late["execution"], "uncertain")
        with self.executor.read() as con:
            self.assertTrue(self.ops.has_unresolved(con, "app"))
            self.assertEqual(self.ops.list_results(con)["items"], [])
            self.assertEqual(con.execute("SELECT count(*) FROM action_checkpoints WHERE phase='late_observation'").fetchone()[0], 1)

    def test_context_invalidation_records_inflight_result_honestly(self):
        action = self.approve(self.prepare())
        self.worker.execute(action["action_id"])
        self.human_run("close_application", {}, lambda tx: self.ops.invalidate_context(tx, "app", "closed"))
        self.assertEqual(self.get(action)["authorization"], "revoked")
        self.assertEqual(self.worker.reconcile(action["action_id"])["execution"], "succeeded")
        with self.executor.read() as con:
            self.assertEqual(len(self.ops.list_results(con, delivery="pending")["items"]), 1)

    def test_unexecuted_action_invalidation_never_resurrects(self):
        action = self.approve(self.prepare())
        self.human_run("close_application", {}, lambda tx: self.ops.invalidate_context(tx, "app", "closed"))
        self.assertEqual(self.get(action)["execution"], "cancelled")
        with self.assertRaises(DomainError):
            self.approve(action)

    def test_draft_cannot_carry_task_completion_consequence(self):
        with self.assertRaises(DomainError):
            self.prepare(kind="create_reply_draft")
        action = self.approve(self.prepare(kind="create_reply_draft", consequence=False))
        self.provider.outcome = ProviderOutcome("succeeded", observation={"verified": "draft"})
        self.worker.execute(action["action_id"])
        with self.executor.read() as con:
            self.assertEqual(self.ops.list_results(con, delivery="pending")["items"], [])

    def test_paused_candidate_cannot_enable_live_provider_by_flag(self):
        action = self.approve(self.prepare())
        paused = ExternalActionWorker(self.executor, self.ops, self.provider, self.worker_context, lambda tx, e: True)
        self.assertEqual(paused.execute(action["action_id"])["status"], "paused")
        self.provider.test_only = False
        paused = ExternalActionWorker(self.executor, self.ops, self.provider, self.worker_context, lambda tx, e: True, allow_test_dispatch=True)
        self.assertEqual(paused.execute(action["action_id"])["status"], "paused")
        self.assertEqual(self.provider.writes, 0)

    def test_response_encoding_failure_rolls_back_authorization_and_work(self):
        action = self.prepare()
        def fail(tx):
            self.ops.authorize_action(tx, action["action_id"], action["digest"], applicability=lambda tx, e: True)
            return {"broken": object()}
        with self.assertRaises(TypeError):
            self.human_run("authorize_action", {}, fail)
        self.assertEqual(self.get(action)["authorization"], "pending")
        with self.executor.read() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM command_work").fetchone()[0], 0)

    def test_history_conversion_retains_payload_without_executable_approval(self):
        record = {"status": "approved", "payload": "e\u0301", "uncertain": True, "application_id": "app"}
        context = CommandContext(Principal("converter", "worker", {"import_snapshot"}), "migration", "migration")
        result = self.executor.run(context, "import_snapshot", record, lambda tx: self.ops.import_history(tx, "legacy", record))
        self.assertFalse(result["dispatch_enabled"])
        with self.executor.read() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM action_revisions").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM command_work").fetchone()[0], 0)
            self.assertTrue(self.ops.has_unresolved(con, "app"))
            self.assertEqual(self.ops.list_history(con, "app")["items"][0]["record"], record)


class FakeOutlook:
    def __init__(self):
        self.source = {"id": "source", "conversationId": "thread", "subject": "Position", "isDraft": False,
                       "lastModifiedDateTime": "2026-10-09T10:00:00Z", "from": {"emailAddress": {"address": "recruiter@example.com"}},
                       "body": {"contentType": "text", "content": "Please reply"}}
        self.message = {"id": "draft", "isDraft": True, "toRecipients": [{"emailAddress": {"address": "recruiter@example.com"}}],
                        "subject": "Re: Position", "body": {"contentType": "text", "content": ""}, "parentFolderId": "drafts"}
        self.calls = []
        self.events = {}

    def preflight_action(self, kind):
        self.calls.append(("preflight", kind))

    def read_message_body(self, remote):
        return deepcopy(self.source if remote == "source" else self.message)

    def create_reply_draft(self, source):
        self.calls.append(("create", source))
        return {"id": "draft"}

    def update_reply_draft(self, remote, body):
        self.calls.append(("update", remote))
        self.message["body"]["content"] = body
        return {"id": remote}

    def send_reply_draft(self, remote):
        self.calls.append(("send", remote))
        return {}

    def read_owned_event(self, remote):
        return deepcopy(self.events[remote])

    def write_private_commitment(self, start, end, transaction, **fields):
        remote = fields.get("remote_id") or "event"
        self.events[remote] = {"id": remote, "transactionId": transaction, "isOrganizer": True,
                               "sensitivity": "private", "attendees": [], "showAs": "busy", "subject": "Private career commitment",
                               "body": {"contentType": "text", "content": ""}, "@odata.etag": "etag2",
                               "start": {"dateTime": start, "timeZone": "UTC"}, "end": {"dateTime": end, "timeZone": "UTC"}}
        self.calls.append(("calendar_write", remote))
        return {"id": remote}

    def delete_private_commitment(self, remote, etag):
        self.calls.append(("delete", remote))
        del self.events[remote]
        return {}


class ProviderTest(unittest.TestCase):
    setUp = ExternalActionsTest.setUp
    human_run = ExternalActionsTest.human_run
    approve = ExternalActionsTest.approve
    worker_context = staticmethod(ExternalActionsTest.worker_context)
    def reply_action(self, kind="send_reply"):
        client = FakeOutlook()
        envelope = {"kind": kind, "account_id": "account", "application_id": "app", "pursuit_no": 1,
                    "target": {"message_id": "internal-message", "provider_message_id": "source", "source_hash": "a" * 64, "provider_source_hash": source_digest(client.source)},
                    "payload": {"recipients": ["recruiter@example.com"], "subject": "Re: Position", "body": "Exact e\u0301\n reply"},
                    "context_versions": {"application:app": 1}, "consequence": None}
        action = self.human_run("prepare_reply", envelope, lambda tx: self.ops.prepare_reply(tx, envelope))
        self.approve(action)
        provider = ReplyProvider("account", client, sent_folder_id="sent")
        provider.test_only = True
        worker = ExternalActionWorker(self.executor, self.ops, provider, self.worker_context, lambda tx, e: True, allow_test_dispatch=True)
        return client, action, worker

    def test_real_reply_handler_waits_for_exact_sent_observation(self):
        client, action, worker = self.reply_action()
        self.assertEqual(worker.execute(action["action_id"])["execution"], "awaiting_confirmation")
        self.assertEqual([call[0] for call in client.calls].count("send"), 1)
        self.assertEqual(worker.reconcile(action["action_id"])["execution"], "uncertain")
        client.message.update(isDraft=False, parentFolderId="sent", sentDateTime="2026-10-09T12:00:01Z")
        self.assertEqual(worker.reconcile(action["action_id"])["execution"], "succeeded")
        self.assertEqual([call[0] for call in client.calls].count("send"), 1)

    def test_read_only_preparation_blocks_missing_context_and_preserves_versions(self):
        client = FakeOutlook()
        kwargs = dict(account_id="account", provider_account_id="account", message_id="internal-message",
                      provider_message_id="source", source_hash="a" * 64, context_versions={"message:internal-message": 2},
                      body="Exact e\u0301")
        result = prepare_reply_context(client, **kwargs)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.target["provider_source_hash"], source_digest(client.source))
        self.assertEqual(result.payload["body"], "Exact e\u0301")
        self.assertEqual(client.calls, [])
        client.source["id"] = "other-message"
        result = prepare_reply_context(client, **kwargs)
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.target)
        self.assertEqual(result.context_versions, {"message:internal-message": 2})

    def test_changed_sent_body_preserves_observation_but_no_success(self):
        client, action, worker = self.reply_action()
        worker.execute(action["action_id"])
        client.message.update(isDraft=False, parentFolderId="sent", sentDateTime="2026-10-09T12:00:01Z")
        client.message["body"]["content"] = "Different"
        result = worker.reconcile(action["action_id"])
        self.assertEqual(result["execution"], "uncertain")
        self.assertEqual(result["error_code"], "sent_content_differs")
        with self.executor.read() as con:
            self.assertEqual(self.ops.list_results(con)["items"], [])

    def test_wrong_account_cannot_reconcile_send_or_calendar_success(self):
        client, action, worker = self.reply_action()
        worker.execute(action["action_id"])
        client.message.update(isDraft=False, parentFolderId="sent", sentDateTime="2026-10-09T12:00:01Z")
        worker.provider.account_id = "different-account"
        client.read_message_body = lambda _: self.fail("Wrong account evidence must not be read")
        result = worker.reconcile(action["action_id"])
        self.assertEqual(result["execution"], "uncertain")
        self.assertEqual(result["error_code"], "account_mismatch")
        client, action, worker = self.calendar_action()
        worker.execute(action["action_id"])
        worker.provider.account_id = "different-account"
        client.read_owned_event = lambda _: self.fail("Wrong account evidence must not be read")
        result = worker.reconcile(action["action_id"])
        self.assertEqual(result["execution"], "uncertain")
        self.assertEqual(result["error_code"], "account_mismatch")

    def test_calendar_exact_success_requires_plain_text_body(self):
        client, action, worker = self.calendar_action()
        worker.execute(action["action_id"])
        client.events["event"]["body"]["contentType"] = "html"
        result = worker.reconcile(action["action_id"])
        self.assertEqual(result["execution"], "uncertain")
        self.assertEqual(result["error_code"], "calendar_content_differs")

    def test_source_or_account_change_blocks_provider_write(self):
        client, action, worker = self.reply_action()
        client.source["body"]["content"] = "New evidence"
        self.assertEqual(worker.execute(action["action_id"])["execution"], "failed")
        self.assertNotIn("create", [call[0] for call in client.calls])

    def test_real_draft_handler_verifies_exact_content_without_sending(self):
        client, action, worker = self.reply_action("create_reply_draft")
        self.assertEqual(worker.execute(action["action_id"])["execution"], "succeeded")
        self.assertNotIn("send", [call[0] for call in client.calls])

    def calendar_action(self):
        envelope = {"kind": "create_calendar_entry", "account_id": "account", "application_id": "app", "pursuit_no": 1,
                    "target": {}, "payload": {"starts_at": "2026-10-10T12:00:00Z", "ends_at": "2026-10-10T13:00:00Z"},
                    "context_versions": {"interview:round": 1}}
        action = self.human_run("prepare_calendar_change", envelope, lambda tx: self.ops.prepare_calendar_change(tx, envelope))
        self.approve(action)
        client = FakeOutlook()
        provider = CalendarProvider("account", client)
        provider.test_only = True
        worker = ExternalActionWorker(self.executor, self.ops, provider, self.worker_context, lambda tx, e: True, allow_test_dispatch=True)
        return client, action, worker

    def test_calendar_verification_requires_exact_identity_and_ownership(self):
        client, action, worker = self.calendar_action()
        self.assertEqual(worker.execute(action["action_id"])["execution"], "awaiting_confirmation")
        client.events["event"]["attendees"] = [{"emailAddress": {"address": "employer@example.com"}}]
        self.assertEqual(worker.reconcile(action["action_id"])["execution"], "uncertain")
        client.events["event"]["attendees"] = []
        self.assertEqual(worker.reconcile(action["action_id"])["execution"], "succeeded")
        with self.executor.read() as con:
            self.assertEqual(self.ops.get_owned_event(con, "account", "event")["transaction_id"], action["operation_id"])

    def test_calendar_target_must_have_platform_ownership_receipt(self):
        envelope = {"kind": "cancel_calendar_entry", "account_id": "account", "application_id": "app", "pursuit_no": 1,
                    "target": {"remote_id": "employer", "transaction_id": "claimed", "etag": "1"}, "payload": {}, "context_versions": {}}
        with self.assertRaises(DomainError):
            self.human_run("prepare_calendar_change", envelope, lambda tx: self.ops.prepare_calendar_change(tx, envelope))

    def test_owned_calendar_update_checks_exact_remote_version(self):
        client, action, worker = self.calendar_action()
        worker.execute(action["action_id"])
        worker.reconcile(action["action_id"])
        envelope = {"kind": "update_calendar_entry", "account_id": "account", "application_id": "app", "pursuit_no": 1,
                    "target": {"remote_id": "event", "transaction_id": action["operation_id"], "etag": "etag2"},
                    "payload": {"starts_at": "2026-10-11T12:00:00Z", "ends_at": "2026-10-11T13:00:00Z"}, "context_versions": {}}
        update = self.human_run("prepare_calendar_change", envelope, lambda tx: self.ops.prepare_calendar_change(tx, envelope))
        self.approve(update)
        client.events["event"]["@odata.etag"] = "changed-elsewhere"
        self.assertEqual(worker.execute(update["action_id"])["execution"], "failed")
        self.assertEqual([call[0] for call in client.calls].count("calendar_write"), 1)

    def test_calendar_delete_timeout_never_infers_success_from_absence(self):
        client, action, worker = self.calendar_action()
        worker.execute(action["action_id"])
        worker.reconcile(action["action_id"])
        envelope = {"kind": "cancel_calendar_entry", "account_id": "account", "application_id": "app", "pursuit_no": 1,
                    "target": {"remote_id": "event", "transaction_id": action["operation_id"], "etag": "etag2"},
                    "payload": {}, "context_versions": {}}
        cancel = self.human_run("prepare_calendar_change", envelope, lambda tx: self.ops.prepare_calendar_change(tx, envelope))
        self.approve(cancel)
        def timed_out_delete(remote, etag):
            del client.events[remote]
            raise TimeoutError()
        client.delete_private_commitment = timed_out_delete
        self.assertEqual(worker.execute(cancel["action_id"])["execution"], "uncertain")
        self.assertEqual(worker.reconcile(cancel["action_id"])["execution"], "uncertain")
        with self.assertRaises(DomainError):
            worker.execute(cancel["action_id"])


if __name__ == "__main__":
    unittest.main()
