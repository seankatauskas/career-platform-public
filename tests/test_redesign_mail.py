"""Offline production mail composition, privacy, and durable recovery checks."""
from datetime import datetime, timezone
import json
import hashlib
import os
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from job_search.application_mail import (ApplicationMailIngestor, OwnerMailCoordinator,
    OWNER_QUERY_VERSION, build_application_mail_handlers, capture_candidates)
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter
from job_search.commands import DomainError
from job_search.contracts import MailChange, ContractError
from job_search.db import connect, prepare_database
from job_search.inference.usage import UsageDeferred
from job_search.mail.revision_archive import ImmutableMailArchive
from job_search.outlook.state import SQLiteOutlookState
from job_search.worker import Worker, RetryableTaskError, PermanentTaskError
from tests.test_job_search_mail_archive_source import TestCipher
from tests.test_job_search_automation import enqueue_work


NOW = "2026-10-09T12:00:00Z"


class Key:
    def get_or_create_key(self):
        return hashlib.sha256(b"x" * 32).hexdigest()[:32], b"x" * 32


class Analyzer:
    def __init__(self):
        self.calls = []
        self.failure = None

    def analyze(self, context):
        self.calls.append(context)
        if self.failure:
            raise self.failure
        source = context.sources[0]
        app_id = context.candidates[0]["id"] if context.candidates else None
        def cite(text):
            start = source.text.index(text)
            return [{"source_id": source.source_id, "revision": source.revision,
                     "start": start, "end": start + len(text), "quote": text}]
        return {"relevance": "career_related",
            "associations": [{"kind": "application", "target_id": app_id, "evidence": cite("Example Engineer")}] if app_id else [],
            "facts": [{"kind": "submission", "value": "received", "target_id": app_id, "evidence": cite("receipt")}],
            "requests": [{"kind": "assessment", "target_id": app_id, "requirement": "required",
                          "responsible_party": "applicant", "channel": "portal", "outcome": "Complete assessment",
                          "evidence": cite("Complete assessment")}],
            "temporal_facts": [], "uncertainties": []}


class ProductionMailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.operational = root / "operations.db"
        prepare_database(self.operational, NOW)
        self.archive = ImmutableMailArchive(self.operational, Key(), cipher_factory=TestCipher)
        self.runtime = ApplicationRuntime(root / "owners.db", clock=lambda: NOW)
        self.human = HumanApplicationAdapter(self.runtime, "human")
        self.app = self.human.command("save_job", {"job_source": {"source": "fixture", "source_id": "job1",
                     "employer": "Example", "title": "Engineer"}}, "save")
        self.ingestor = ApplicationMailIngestor(self.runtime, self.archive, "account-a")
        self.analyzer = Analyzer()
        self.handlers = build_application_mail_handlers(self.runtime, self.archive, "account-a", self.analyzer, "fake-v1")
        self.context = SimpleNamespace(heartbeat=lambda: True, work_id="test-dispatch")

    def payload(self, identity="mail1", *, minute="00", body="receipt. Complete assessment.", **values):
        return {"id": identity, "subject": "Example Engineer", "body": {"contentType": "text", "content": body},
                "receivedDateTime": NOW, "lastModifiedDateTime": "2026-10-09T12:" + minute + ":00Z",
                "sender": {"emailAddress": {"address": "private-person@example.test"}}, **values}

    def ingest(self, **options):
        payload = self.payload(**options)
        return self.ingestor.ingest(payload["id"], payload, direction="incoming")

    def scheduled(self):
        return self.handlers["applications.mail.dispatch"]({}, self.context).follow_ups

    def run_incoming(self):
        return [self.handlers[item.task_kind](item.payload, self.context)
                for item in self.scheduled() if item.task_kind == "applications.mail.understand"]

    def test_blocking_model_renews_both_leases_beyond_five_minutes(self):
        from datetime import timedelta
        from threading import Event, current_thread, enumerate as threads
        self.ingest()
        now = [datetime.fromisoformat(NOW.replace("Z", "+00:00"))]
        self.runtime.executor.clock = lambda:now[0].isoformat().replace("+00:00", "Z")
        reached = Event()
        ticks = []
        def heartbeat():
            now[0] += timedelta(seconds=240)
            if current_thread().name == "understanding-lease":
                ticks.append(now[0])
                if len(ticks) == 3: reached.set()
            return True
        self.context.heartbeat = heartbeat
        original = self.analyzer.analyze
        def slow(context):
            self.assertTrue(reached.wait(5), "claim was not renewed during blocking inference")
            return original(context)
        with patch.object(self.analyzer, "analyze", slow), patch("job_search.application_runtime._ANALYSIS_HEARTBEAT_SECONDS", 0.01):
            self.run_incoming()
        self.assertGreaterEqual(len(ticks), 3)
        with self.runtime.executor.read() as con:
            self.assertEqual(con.execute("SELECT status FROM understand_claims").fetchone()[0], "succeeded")
        self.assertFalse(any(t.name == "understanding-lease" for t in threads()))

    def test_lost_worker_lease_discards_model_output_and_releases_claim(self):
        self.ingest()
        owned = [True]
        self.context.heartbeat = lambda:owned[0]
        original = self.analyzer.analyze
        def lose_lease(context):
            result = original(context)
            owned[0] = False
            return result
        with patch.object(self.analyzer, "analyze", lose_lease), self.assertRaises(RuntimeError):
            self.run_incoming()
        with self.runtime.executor.read() as con:
            self.assertEqual(con.execute("SELECT status FROM understand_claims").fetchone()[0], "failed")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM understand_findings").fetchone()[0], 0)
            self.assertTrue(self.runtime.executor.pending_work(con,owner="correspondence"))

    def test_configured_model_uses_generation_identity_for_reanalysis(self):
        from job_search.application_production import _mail_handlers
        config = SimpleNamespace(mail_classifier_config=None, remote_mail_inference_enabled=True,outlook_account_id="account-a",
            application_db=self.operational)
        generation = SimpleNamespace(model="same-name",generation_identity="weights-and-deployment-v2",default_max_output_tokens=8192)
        with patch("job_search.application_production._archive",return_value=self.archive), \
             patch("job_search.application_production.configured_understanding_profile",return_value=SimpleNamespace(structured_generation=generation)), \
             patch("job_search.inference.build_structured_provider",return_value=object()), \
             patch("job_search.application_mail.build_application_mail_handlers",return_value={}) as build:
            _mail_handlers(config,self.runtime,{},lambda:NOW,model=True)
        self.assertEqual(build.call_args.args[-1],generation.generation_identity)
        self.assertEqual(build.call_args.kwargs["operational_db"],self.operational)

    def test_all_scoped_mail_reaches_understanding_without_legacy_prefilters(self):
        from job_search.application_production import _sync_handler
        state = SQLiteOutlookState(self.operational)
        state.stage_changes("account-a","other-folder",[MailChange(immutable_id="outside",removed=False,received_at=NOW,modified_at=NOW)],OWNER_QUERY_VERSION)
        state.stage_changes("account-a","inbox",[MailChange(immutable_id="old",removed=False,
            received_at="2020-01-01T00:00:00Z",modified_at=NOW)],OWNER_QUERY_VERSION)
        changes = [MailChange(immutable_id="newsletter",removed=False,received_at=NOW,modified_at=NOW,
                    sender_address="news@example.test",subject="Lunch specials"),
                   MailChange(immutable_id="verification",removed=False,received_at=NOW,modified_at=NOW,
                    sender_address="noreply@greenhouse.io",subject="Verification code for your application")]
        reads,folders = [],[]
        def initial(*,folder):
            folders.append(folder)
            return "delta-start"
        def body(identity):
            reads.append(identity)
            return self.payload(identity,subject="Lunch specials" if identity=="newsletter" else "Verification code for your application")
        mail = SimpleNamespace(initial_all_history_delta_url=initial,
            read_delta_page=lambda url:SimpleNamespace(changes=changes,next_link=None,delta_link="delta-end"),read_message_body=body)
        config = SimpleNamespace(application_db=self.operational,outlook_account_id="account-a",
            outlook_mail_folders=("inbox",),outlook_new_messages_only=True,mail_recruiting_only=True)
        with patch("job_search.application_production._graph",return_value=(None,object())), \
             patch("job_search.application_production._archive",return_value=self.archive), \
             patch("job_search.outlook.mail.GraphMailClient",return_value=mail), \
             patch("job_search.activation.mail_start",return_value=NOW):
            handler = _sync_handler(config,self.runtime,{})
            report = handler({},self.context)
        self.assertEqual(folders,["inbox"])
        self.assertEqual(set(reads),{"newsletter","verification"})
        self.assertEqual(report["processing"]["processed"],2)
        self.assertEqual([row["immutable_message_id"] for row in state.pending_messages(account_id="account-a",query_version=OWNER_QUERY_VERSION)],["outside"])
        calls=[]
        class RelevanceAnalyzer:
            def analyze(self,context):
                calls.append(context)
                return {"relevance":"unrelated","associations":[],"facts":[],"requests":[],"temporal_facts":[],"uncertainties":[]}
        handlers = build_application_mail_handlers(self.runtime,self.archive,"account-a",RelevanceAnalyzer(),"new-understanding")
        for work in self.scheduled():
            handlers[work.task_kind](work.payload,self.context)
        self.assertEqual(len(calls),2)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.list_pending(con)["items"],[])
            self.assertEqual(con.execute("SELECT count(*) FROM corr_messages").fetchone()[0],2)
        with connect(self.operational) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM event_proposals").fetchone()[0],0)

    def test_understanding_preserves_complete_current_email_and_quoted_context(self):
        first = self.ingest(body="Current response.\nFrom: another@example.test\nForwarded interview details.\nOn Tuesday someone wrote:\nEarlier context.")
        text = self.archive.for_account("account-a").read_message(first["archive_ref"])
        self.assertIn("Forwarded interview details.",text)
        self.assertIn("Earlier context.",text)
        class Reader:
            def analyze(self,context):
                self.text = context.sources[0].text
                return {"relevance":"uncertain","associations":[],"facts":[],"requests":[],"temporal_facts":[],"uncertainties":[]}
        analyzer=Reader()
        self.runtime.analyze_message(first["id"],archive=self.archive.for_account("account-a"),allowed_accounts=("account-a",),
            analyzer=analyzer,model_version="full-email")
        self.assertEqual(analyzer.text,text)

    def test_archives_are_immutable_scoped_and_revision_exact(self):
        first = self.ingest()
        first_text = self.archive.for_account("account-a").read_message(first["archive_ref"])
        second = self.ingest(minute="01", body="receipt. Complete assessment. revised")
        self.assertNotEqual(first["archive_ref"], second["archive_ref"])
        self.assertEqual(self.archive.for_account("account-a").read_message(first["archive_ref"]), first_text)
        with self.assertRaises(DomainError):
            self.archive.for_account("account-b").read_message(first["archive_ref"])
        repeated = self.ingest()
        self.assertEqual(repeated["revision"], first["revision"])
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.correspondence.get(con, first["id"])["revision"], second["revision"])
        self.run_incoming()
        # Old source revisions remain readable, but queued work for them must
        # not create a new current processing issue after a newer email arrived.
        self.assertEqual({c.sources[0].revision for c in self.analyzer.calls}, {second["revision"]})
        self.assertEqual(self.archive.for_account("account-a").read_message(first["archive_ref"]), first_text)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.executor.pending_work(con, owner="correspondence"), [])

    def test_new_receipt_and_assessment_only_create_pending_reviews(self):
        message = self.ingest()
        self.run_incoming()
        self.assertEqual(len(self.analyzer.calls), 1)
        workspace = self.runtime.queries.workspace(self.app["id"])
        self.assertEqual(workspace["records"]["tasks"]["items"], [])
        with self.runtime.executor.read() as con:
            proposals = self.runtime.understanding.list_pending(con, application_id=self.app["id"])["items"]
        self.assertEqual({p["operation"] for p in proposals}, {"link_message", "confirm_submission", "record_request"})
        self.assertTrue(all(p["status"] == "pending" for p in proposals))
        self.assertEqual(self.run_incoming(), [])
        private = "private-person@example.test"
        with self.runtime.executor.read() as con:
            for table in ("command_history", "command_receipts", "command_work"):
                self.assertNotIn(private, repr([tuple(r) for r in con.execute("SELECT * FROM " + table)]))
        with connect(self.operational) as con:
            row = con.execute("SELECT * FROM owner_mail_archive").fetchone()
            self.assertNotIn(private.encode(), row["ciphertext"])
        self.assertEqual(self.archive.for_account("account-a").read_source(message["archive_ref"])["metadata"]["sender"]["emailAddress"]["address"], private)

    def test_archive_then_command_crash_replays_one_revision(self):
        original = self.runtime.command
        with patch.object(self.runtime, "command", side_effect=RuntimeError("fictional crash")):
            with self.assertRaises(RuntimeError):
                self.ingest()
        first = self.ingest()
        self.assertEqual(first["revision"], self.ingest()["revision"])
        with connect(self.operational) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM owner_mail_archive").fetchone()[0], 1)
        with self.runtime.executor.read() as con:
            self.assertEqual(len(self.runtime.executor.pending_work(con, owner="correspondence")), 1)

    def test_accounts_filter_before_limits_and_ack_fences_revision(self):
        state = SQLiteOutlookState(self.operational)
        def change(identity, modified=NOW):
            return MailChange(immutable_id=identity, removed=False, received_at=NOW, modified_at=modified)
        state.stage_changes("account-b", "inbox", [change("a-first")], OWNER_QUERY_VERSION)
        state.stage_changes("account-a", "inbox", [change("b-second")], OWNER_QUERY_VERSION)
        page = state.pending_messages(1, query_version=OWNER_QUERY_VERSION, account_id="account-a")
        self.assertEqual(page[0]["immutable_message_id"], "b-second")
        state.pending_messages(1, query_version=OWNER_QUERY_VERSION, account_id="account-a", received_since="2026-10-10T12:00:00Z")
        self.assertEqual(len(state.pending_messages(1, query_version=OWNER_QUERY_VERSION, account_id="account-b")), 1)
        state.stage_changes("account-a", "inbox", [change("new", "2026-10-09T12:01:00Z")], OWNER_QUERY_VERSION)
        self.assertFalse(state.mark_revision("account-a", "inbox", "new", query_version=OWNER_QUERY_VERSION, modified_at=NOW))
        self.assertTrue(state.mark_revision("account-a", "inbox", "new", query_version=OWNER_QUERY_VERSION, modified_at="2026-10-09T12:01:00Z"))

    def test_ingestion_coordinator_has_no_legacy_semantic_writer(self):
        state = SQLiteOutlookState(self.operational)
        state.stage_changes("account-a", "inbox", [MailChange(immutable_id="mail1", removed=False, received_at=NOW, modified_at=NOW)], OWNER_QUERY_VERSION)
        mail = SimpleNamespace(read_message_body=lambda identity: self.payload(identity, parentFolderId="opaque-inbox-id"))
        coordinator = OwnerMailCoordinator(mail, state, self.ingestor)
        result = coordinator.process_pending()
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.auto_applied, 0)
        self.assertEqual(self.scheduled()[0].task_kind, "applications.mail.understand")
        self.assertEqual(self.analyzer.calls, [])
        with connect(self.operational) as con:
            for table in ("event_proposals", "application_events", "mail_evidence", "lifecycle_mail_observations"):
                self.assertEqual(con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_outgoing_unknown_and_draft_are_preserved_without_inference(self):
        for direction in ("outgoing", "unknown", "draft"):
            payload = self.payload(direction)
            self.ingestor.ingest(direction, payload, direction=direction)
        for item in self.scheduled():
            result = self.handlers[item.task_kind](item.payload, self.context)
            self.assertEqual(result["inferred_changes"], 0)
        self.assertEqual(self.analyzer.calls, [])
        self.assertEqual(self.scheduled(), ())

    def test_provider_defer_releases_claim_and_preserves_worker_semantics(self):
        self.ingest()
        work = self.scheduled()[0]
        self.analyzer.failure = UsageDeferred("inference_daily_token_limit", "2026-10-10T12:00:00Z")
        with self.assertRaises(UsageDeferred):
            self.handlers[work.task_kind](work.payload, self.context)
        self.analyzer.failure = None
        result = self.handlers[work.task_kind](work.payload, self.context)
        self.assertEqual(result["status"], "processed")
        with self.runtime.executor.read() as con:
            summaries = self.runtime.understanding.coverage(con)["items"]
        self.assertEqual({item["status"] for item in summaries}, {"succeeded"})
        self.assertEqual(len(summaries),1)

    def test_projection_recovers_after_crash_without_second_inference(self):
        self.ingest()
        with patch.object(self.runtime, "project_message_analysis", side_effect=RuntimeError("fictional interrupted projection")):
            with self.assertRaises(RuntimeError):
                self.run_incoming()
        projection = next(item for item in self.scheduled() if item.task_kind == "applications.mail.project")
        result = self.handlers[projection.task_kind](projection.payload, self.context)
        self.assertEqual(result["status"], "processed")
        self.assertEqual(len(self.analyzer.calls), 1)
        self.assertEqual(self.run_incoming(), [])

    def test_attachment_and_candidate_coverage_block_acceptance(self):
        message = self.ingest(hasAttachments=True)
        self.run_incoming()
        self.assertFalse(self.analyzer.calls[0].coverage["complete"])
        with self.runtime.executor.read() as con:
            proposals = self.runtime.understanding.list_pending(con)["items"]
        self.assertTrue(all("incomplete_evidence_coverage" in p["blockers"] for p in proposals))
        for index in range(21):
            self.human.command("save_job", {"job_source": {"source": "fixture", "source_id": "other" + str(index)}}, "save" + str(index))
        candidates = capture_candidates(self.runtime, message["id"], "nothing matches")
        self.assertEqual(candidates["candidate_ids"], [])
        self.assertTrue(candidates["coverage"]["complete"])

    def test_candidate_limit_reports_real_matching_coverage(self):
        message = self.ingest()
        for index in range(21):
            self.human.command("save_job", {"job_source": {"source": "fixture", "source_id": "match" + str(index),
                "employer": "Example", "title": "Engineer"}}, "match" + str(index))
        candidates = capture_candidates(self.runtime, message["id"], "Example Engineer")
        self.assertEqual(len(candidates["candidate_ids"]), 20)
        self.assertFalse(candidates["coverage"]["complete"])

    def test_specific_analysis_failure_is_preserved_without_private_diagnostics(self):
        for index, code in enumerate(("context_budget_exceeded", "invalid_json", "evidence_mismatch", "output_truncated", "output_too_large")):
            with self.subTest(code=code):
                message = self.ingest(identity="failure" + str(index))
                self.analyzer.failure = DomainError(code, "private content must not be recorded")
                result = self.runtime.analyze_message(message["id"], archive=self.archive.for_account("account-a"),
                    allowed_accounts=("account-a",), analyzer=self.analyzer, model_version="fake-v1")
                self.assertEqual(result["failure"], code)
                with self.runtime.executor.read() as con:
                    analysis = self.runtime.understanding.get_analysis(con, result["analysis_id"])
                    self.assertEqual(analysis["failure_code"], code)
                    self.assertNotIn("private content", json.dumps(analysis))

    def test_review_retry_runs_model_lane_once_and_preserves_failed_attempt(self):
        message = self.ingest()
        self.analyzer.failure = DomainError("evidence_mismatch", "safe diagnosis")
        with self.assertRaises(PermanentTaskError):
            self.run_incoming()
        with self.runtime.executor.read() as con:
            issue = self.runtime.understanding.processing_for_source(con, message["id"], message["revision"])
        self.human.command("retry_processing", {"issue_id": issue["issue_id"], "expected_version": issue["version"],
            "expected_analysis_id": issue["analysis_id"], "reason": "Retry after exact quote anchoring fix"}, "retry-mail")
        retries = [item for item in self.scheduled() if item.payload["kind"] == "retry_processing"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].lane, "model")
        self.analyzer.failure = None
        result = self.handlers[retries[0].task_kind](retries[0].payload, self.context)
        self.assertEqual(result["status"], "processed")
        self.assertEqual(self.handlers[retries[0].task_kind](retries[0].payload, self.context)["status"], "already_processed")
        with self.runtime.executor.read() as con:
            issue = self.runtime.understanding.get_processing_issue(con, issue["issue_id"])
            self.assertEqual(issue["status"], "succeeded")
            self.assertEqual(issue["attempt_count"], 2)
            self.assertEqual(con.execute("SELECT count(*) FROM understand_analyses WHERE status='failed'").fetchone()[0], 1)
            self.assertTrue(all(p["status"] == "pending" for p in self.runtime.understanding.list_pending(con)["items"]))

    def test_manual_resolution_while_retry_model_runs_fences_its_result(self):
        message = self.ingest()
        self.analyzer.failure = DomainError("invalid_json", "safe diagnosis")
        with self.assertRaises(PermanentTaskError): self.run_incoming()
        with self.runtime.executor.read() as con:
            issue = self.runtime.understanding.processing_for_source(con, message["id"], message["revision"])
        queued = self.human.command("retry_processing", {"issue_id": issue["issue_id"], "expected_version": issue["version"],
            "expected_analysis_id": issue["analysis_id"], "reason": "Retry the failed analysis"}, "retry-mail")
        retry = next(item for item in self.scheduled() if item.payload["kind"] == "retry_processing")
        self.analyzer.failure = None
        original = self.analyzer.analyze
        def resolve_during_inference(context):
            self.human.command("resolve_processing", {"issue_id": queued["issue_id"], "expected_version": queued["version"],
                "expected_analysis_id": queued["analysis_id"], "reason": "Manually reviewed and handled"}, "resolve-mail")
            return original(context)
        with patch.object(self.analyzer, "analyze", resolve_during_inference):
            self.assertEqual(self.handlers[retry.task_kind](retry.payload, self.context)["status"], "superseded")
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.get_processing_issue(con, issue["issue_id"])["status"], "resolved_manually")
            self.assertEqual(self.runtime.understanding.list_pending(con)["items"], [])

    def test_explicit_retry_does_not_reuse_a_valid_but_uncertain_analysis(self):
        message = self.ingest()
        calls = []
        def uncertain(context):
            calls.append(context)
            return {"relevance": "uncertain", "associations": [], "facts": [], "requests": [],
                    "temporal_facts": [], "uncertainties": ["Could not determine relevance"]}
        with patch.object(self.analyzer, "analyze", uncertain):
            self.run_incoming()
            with self.runtime.executor.read() as con:
                issue = self.runtime.understanding.processing_for_source(con, message["id"], message["revision"])
            self.assertEqual(issue["failure_code"], "relevance_uncertain")
            self.human.command("retry_processing", {"issue_id": issue["issue_id"], "expected_version": issue["version"],
                "expected_analysis_id": issue["analysis_id"], "reason": "Request a fresh interpretation"}, "retry-uncertain")
            retry = next(item for item in self.scheduled() if item.payload["kind"] == "retry_processing")
            self.handlers[retry.task_kind](retry.payload, self.context)
            self.handlers[retry.task_kind](retry.payload, self.context)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].fingerprint(), calls[1].fingerprint())
        with self.runtime.executor.read() as con:
            current = self.runtime.understanding.get_processing_issue(con, issue["issue_id"])
            self.assertEqual(current["attempt_count"], 2)
            self.assertEqual(current["failure_code"], "relevance_uncertain")

    def test_original_routing_changes_conflict_with_reused_revision(self):
        payload = self.payload(body="receipt.  Complete assessment.")
        first = self.ingestor.ingest("mail1", payload, direction="incoming", source_version="fixed")
        changed = self.payload(body="receipt. Complete assessment.")
        with self.assertRaises(DomainError) as raised:
            self.ingestor.ingest("mail1", changed, direction="incoming", source_version="fixed")
        self.assertEqual(raised.exception.code, "idempotency_conflict")
        exact = self.archive.for_account("account-a").read_source(first["archive_ref"])
        self.assertEqual(exact["metadata"]["source_body"], payload["body"])

    def test_same_provider_id_in_other_account_cannot_be_dispatched_here(self):
        self.ingest()
        other = ApplicationMailIngestor(self.runtime, self.archive, "account-b")
        other.ingest("mail1", self.payload(), direction="incoming")
        self.assertEqual(len(self.scheduled()), 1)
        self.run_incoming()
        self.assertEqual(len(self.analyzer.calls), 1)
        with self.runtime.executor.read() as con:
            pending = self.runtime.executor.pending_work(con, owner="correspondence")
            source = self.runtime.correspondence.get(con, pending[0]["payload"]["message_id"])
        self.assertEqual(source["account_id"], "account-b")

    def test_dispatch_continuation_does_not_starve_work_after_first_hundred(self):
        for index in range(102):
            self.ingest(identity="mail" + str(index))
        first = self.scheduled()
        continuation = next(item for item in first if item.task_kind == "applications.mail.dispatch")
        later = self.handlers[continuation.task_kind](continuation.payload, self.context).follow_ups
        self.assertEqual(sum(item.task_kind == "applications.mail.understand" for item in first + later), 102)
        ids = [item.payload["work_id"] for item in first + later if item.task_kind == "applications.mail.understand"]
        self.assertEqual(len(set(ids)), 102)

    def test_unavailable_archive_retains_durable_failure_and_pending_work(self):
        self.ingest()
        def missing(reference):
            raise OSError("archive unavailable")
        broken = SimpleNamespace(for_account=lambda account: SimpleNamespace(read_message=missing))
        handlers = build_application_mail_handlers(self.runtime, broken, "account-a", self.analyzer, "fake-v1")
        work = self.scheduled()[0]
        with self.assertRaises(RetryableTaskError):
            handlers[work.task_kind](work.payload, self.context)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.coverage(con)["items"][0]["failure_code"], "source_unavailable")
            self.assertEqual(len(self.runtime.executor.pending_work(con, owner="correspondence")), 1)
        self.assertEqual(self.analyzer.calls, [])

    def test_rekey_copy_preserves_all_revision_bytes_and_immutability(self):
        from job_search.portable_export import _rekey_database
        first = self.ingest()
        self.ingest(minute="01", body="receipt. Complete assessment. updated")
        class ExistingKey(Key):
            get_existing_key = Key.get_or_create_key
        class TargetKey:
            def get_or_create_key(self):
                return hashlib.sha256(b"y" * 32).hexdigest()[:32], b"y" * 32
            get_existing_key = get_or_create_key
        target = Path(self.tmp.name) / "rekeyed.db"
        source = sqlite3.connect(self.operational)
        destination = sqlite3.connect(target)
        destination.row_factory = sqlite3.Row
        try:
            source.backup(destination)
            destination.execute("BEGIN IMMEDIATE")
            count = _rekey_database(destination, source_archive_key_provider=ExistingKey(),
                target_archive_key_provider=TargetKey(), cipher_factory=TestCipher, nonce_factory=os.urandom)
            destination.commit()
            self.assertEqual(count, (2, 0))
            with self.assertRaises(sqlite3.IntegrityError):
                destination.execute("UPDATE owner_mail_archive SET content_chars=0")
            destination.rollback()
        finally:
            source.close()
            destination.close()
        moved = ImmutableMailArchive(target, TargetKey(), cipher_factory=TestCipher)
        self.assertEqual(moved.for_account("account-a").read_source(first["archive_ref"]),
                         self.archive.for_account("account-a").read_source(first["archive_ref"]))
        with self.assertRaises(ContractError):
            ImmutableMailArchive(target, ExistingKey(), cipher_factory=TestCipher).for_account("account-a").read_message(first["archive_ref"])

    def test_predecessor_reads_are_scoped_readonly_and_survive_new_ingestion(self):
        from job_search.mail.archive import EncryptedMailArchive
        from job_search.service import JobSearchLedger
        from job_search.contracts import MutationContext
        from job_search.application_mail import worker_context
        ledger = JobSearchLedger(self.operational)
        old = EncryptedMailArchive(ledger, Key(), cipher_factory=TestCipher)
        text = "Historical exact e\u0301 receipt. Complete assessment."
        saved = old.archive_message(account_id="account-a", immutable_message_id="mail1", sanitized_text=text,
            truncated=False, context=MutationContext("old", "system", "fixture"))["archive"]
        predecessor = Path(self.tmp.name) / "predecessor.sqlite"
        source = sqlite3.connect(self.operational)
        destination = sqlite3.connect(predecessor)
        source.backup(destination)
        source.close()
        destination.close()
        before = hashlib.sha256(predecessor.read_bytes()).hexdigest()
        archive = ImmutableMailArchive(self.operational, Key(), cipher_factory=TestCipher, predecessor_path=predecessor)
        reference = "predecessor:mail_archive:" + saved["archive_id"]
        historic = self.runtime.command(worker_context("record_message", "historic"), "record_message", {
            "account_id": "account-a", "provider_message_id": "mail1", "source_version": 0,
            "direction": "incoming", "authored_text": text, "archive_ref": reference})
        self.ingest()
        self.assertEqual(archive.for_account("account-a").read_message(reference), text)
        with self.runtime.executor.read() as con:
            evidence = self.runtime.correspondence.evidence(con, historic["id"], revision=historic["revision"],
                archive=archive.for_account("account-a"), allowed_accounts=("account-a",))
        self.assertEqual(evidence["text"], text)
        with patch.object(archive, "_open") as decrypt:
            with self.assertRaises(DomainError):
                archive.for_account("account-b").read_message(reference)
            decrypt.assert_not_called()
        self.assertEqual(hashlib.sha256(predecessor.read_bytes()).hexdigest(), before)

    def test_human_body_reader_requires_association_and_configured_account(self):
        from job_search.application_mail import ApplicationMailReader
        message = self.ingest()
        reader = ApplicationMailReader(self.runtime, self.archive, ("account-a",))
        with self.assertRaises(DomainError):
            reader.read_message(self.app["id"], message["id"])
        self.human.command("link_message", {"application_id": self.app["id"], "message_id": message["id"], "expected_version": 0}, "link")
        result = reader.read_message(self.app["id"], message["id"])
        self.assertIn("Complete assessment", result["text"])
        self.assertEqual(set(result), {"message_id", "revision", "text", "coverage"})
        with self.assertRaises(DomainError):
            ApplicationMailReader(self.runtime, self.archive, ("account-b",)).read_message(self.app["id"], message["id"])

    def test_actual_core_dispatch_and_model_worker_complete_durable_work(self):
        self.ingest()
        now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        enqueue_work(self.operational, "dispatch", "applications.mail.dispatch", due=now)
        with connect(self.operational) as con:
            con.execute("UPDATE work_items SET payload_json=? WHERE work_id='dispatch'",
                        (json.dumps({"schedule_key": "mail-dispatch", "scheduled_for": NOW, "configuration": {}}),))
        core = Worker(self.operational, task_handlers=self.handlers, now_provider=lambda: now)
        report = core.tick()
        self.assertEqual(report["work"]["succeeded"], 1)
        model = Worker(self.operational, task_handlers=self.handlers, now_provider=lambda: now, lane="model")
        report = model.tick()
        self.assertEqual(report["work"]["succeeded"], 1)
        self.assertEqual(len(self.analyzer.calls), 1)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.executor.pending_work(con, owner="correspondence"), [])


if __name__ == "__main__":
    unittest.main()
