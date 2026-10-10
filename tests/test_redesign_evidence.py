"""Offline evidence/understanding contracts through real owner transactions."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import unittest

from job_search.commands import CommandContext, CommandExecutor, DomainError, Principal
from job_search.correspondence import CorrespondenceOperations, SCHEMA as CORRESPONDENCE
from job_search.applications.understanding import AnalysisInput, SourceText, UnderstandingOperations, SCHEMA as UNDERSTANDING
from job_search.applications.understanding.contracts import validate_analysis


TEXT = "Application received. Complete the assessment. Contact us if you need help."


def source(text=TEXT, identity="message", role="authored", direction="incoming"):
    return SourceText(identity, "revision-1", hashlib.sha256(text.encode()).hexdigest(), text, direction, role)


def span(text, s=None):
    s = s or source()
    start = s.text.index(text)
    return {"source_id": s.source_id, "revision": s.revision, "start": start,
            "end": start + len(text), "quote": text}


def output(s=None):
    s = s or source()
    return {"relevance": "career_related", "associations": [{"kind": "application", "target_id": "app-1", "evidence": [span("Application", s)]}],
            "facts": [{"kind": "submission", "value": "received", "target_id": "app-1", "evidence": [span("Application received.", s)]}],
            "requests": [{"kind": "assessment", "outcome": "Complete the assessment", "responsible_party": "applicant",
                          "requirement": "required", "channel": "portal", "target_id": "app-1", "evidence": [span("Complete the assessment.", s)]},
                         {"kind": "reply", "outcome": "Contact support if needed", "responsible_party": "applicant",
                          "requirement": "optional", "channel": "email", "target_id": "app-1", "evidence": [span("Contact us if you need help.", s)]}],
            "temporal_facts": [], "uncertainties": []}


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = "2026-10-09T12:00:00Z"
        self.executor = CommandExecutor(Path(self.tmp.name) / "candidate.db",
            {"correspondence": CORRESPONDENCE, "understanding": UNDERSTANDING}, clock=lambda: self.now)
        self.corr, self.under = CorrespondenceOperations(), UnderstandingOperations()
        self.human = Principal("human", "human", frozenset({"*"}))
        self.worker = Principal("worker", "worker", frozenset({"record_message", "record_analysis", "project_analysis", "analyze_observation", "propose_changes"}))
        self.serial = 0

    def run_op(self, operation, fn, key=None, human=False, payload=None):
        self.serial += 1
        context = CommandContext(self.human if human else self.worker, key or str(self.serial), "direct" if human else "inferred")
        return self.executor.run(context, operation, payload or {}, fn)

    def store(self, s=None, model="m1", coverage=True, body=None):
        s = s or source()
        context = AnalysisInput((s,), ({"id": "app-1"},), coverage={"complete": coverage})
        return self.run_op("record_analysis", lambda tx: self.under.store_analysis(tx, context=context, model_version=model, output=body or output(s)))

    def project(self, analysis):
        return self.run_op("project_analysis", lambda tx: self.under.project_requests(tx, analysis_id=analysis["id"],
            application_id="app-1", message_id="message", association_required=True))["proposals"]

    def test_duplicate_account_scoped_and_out_of_order_revisions(self):
        def record(version, text=TEXT, account="account"):
            return self.run_op("record_message", lambda tx: self.corr.record_message(tx, account_id=account,
                provider_message_id="provider-id", source_version=version, direction="incoming", authored_text=text, archive_ref="archive-" + str(version)))
        newest = record(2)
        older = record(1, "Older text")
        self.assertEqual(newest["id"], older["id"])
        self.assertEqual(record(1, "Older text")["revision"], older["revision"])
        self.assertNotEqual(record(2, account="second")["id"], newest["id"])
        with self.executor.read() as con:
            self.assertEqual(self.corr.get(con, newest["id"])["revision"], newest["revision"])
            stored = repr([tuple(r) for r in con.execute("SELECT * FROM corr_revisions")])
            history = repr([tuple(r) for r in con.execute("SELECT * FROM command_history")])
            self.assertNotIn(TEXT, stored + history)
        with self.assertRaises(DomainError):
            record(2, "Replaced content")

    def test_analysis_renewal_is_fenced_by_live_token_and_terminal_status(self):
        context = AnalysisInput((source(),), ({"id": "app-1"},), coverage={"complete": True})
        def claim():
            return self.run_op("analyze_observation", lambda tx:self.under.claim_analysis(tx,context=context,model_version="model"))
        first = claim()
        self.now = "2026-10-09T12:04:00Z"
        renewed = self.run_op("analyze_observation", lambda tx:self.under.renew_analysis(tx,claim=first))
        self.assertEqual(renewed["lease_until"], "2026-10-09T12:09:00Z")
        self.now = "2026-10-09T12:10:00Z"
        with self.assertRaises(DomainError):
            self.run_op("analyze_observation", lambda tx:self.under.renew_analysis(tx,claim=first))
        second = claim()
        with self.assertRaises(DomainError):
            self.run_op("analyze_observation", lambda tx:self.under.renew_analysis(tx,claim=first))
        self.run_op("record_analysis", lambda tx:self.under.store_analysis(tx,context=context,
                    model_version="model",failure_code="provider_failed",claim=second))
        with self.assertRaises(DomainError):
            self.run_op("analyze_observation", lambda tx:self.under.renew_analysis(tx,claim=second))
        self.assertEqual(claim()["generation"], second["generation"] + 1)

    def test_archive_access_bounds_hash_and_account(self):
        record = self.run_op("record_message", lambda tx: self.corr.record_message(tx, account_id="account",
            provider_message_id="id", source_version="opaque-v1", direction="incoming", authored_text=TEXT, archive_ref="archive"))
        class Archive:
            def read_message(self, ref):
                return TEXT
        with self.executor.read() as con:
            with self.assertRaises(DomainError):
                self.corr.evidence(con, record["id"], archive=Archive())
            evidence = self.corr.evidence(con, record["id"], archive=Archive(), allowed_accounts=("account",), limit=10)
            self.assertEqual(evidence["text"], TEXT[:10])
            self.assertFalse(evidence["coverage"]["complete"])

    def test_archive_preserves_incomplete_declared_coverage_and_unloaded_attachments(self):
        class Archive:
            def read_message(self, ref):
                return TEXT
        for metadata in ({"coverage": {"complete": False, "reason": "sanitizer_truncated"}},
                         {"attachments": [{"id": "assessment-pdf"}]}):
            record = self.run_op("record_message", lambda tx: self.corr.record_message(tx, account_id="account",
                provider_message_id="id-" + str(self.serial), source_version="v1", direction="incoming",
                authored_text=TEXT, archive_ref="archive", metadata=metadata))
            with self.executor.read() as con:
                evidence = self.corr.evidence(con, record["id"], archive=Archive(), allowed_accounts=("account",))
                self.assertFalse(evidence["coverage"]["complete"])

    def test_analysis_persists_before_projection_and_requests_are_independent(self):
        analysis = self.store()
        with self.executor.read() as con:
            self.assertEqual(self.under.list_pending(con)["items"], [])
        proposals = self.project(analysis)
        self.assertEqual({p["operation"] for p in proposals}, {"link_message", "confirm_submission", "record_request"})
        self.assertEqual(len(proposals), 3)
        link = next(p for p in proposals if p["operation"] == "link_message")
        for p in proposals:
            self.assertEqual(p["status"], "pending")
            if p is not link:
                self.assertEqual(p["dependencies"], [link["id"]])
        self.assertEqual([p["id"] for p in self.project(analysis)], [p["id"] for p in proposals])

    def test_projected_association_binds_captured_versions_and_target(self):
        context = AnalysisInput((source(),), ({"id": "app-1"}, {"id": "app-2"}), versions={"message:message": 3,"application:app-2":8,"interview:other-app-round":9})
        value = output()
        value["requests"][0]["target_id"] = "app-2"
        analysis = self.run_op("record_analysis", lambda tx: self.under.store_analysis(tx, context=context, model_version="m1", output=value))
        proposals = self.run_op("project_analysis", lambda tx: self.under.project_requests(tx, analysis_id=analysis["id"],
            message_id="message", association_required=True))["proposals"]
        link = next(p for p in proposals if p["operation"] == "link_message")
        self.assertEqual(link["expected_versions"], {"message:message": 3})
        request = next(p for p in proposals if p["operation"] == "record_request")
        self.assertIn("target_disagrees_with_selected_association", request["blockers"])

    def test_rejected_reanalysis_suppressed_and_changed_wording_requires_comparison(self):
        proposals = self.project(self.store())
        request = next(p for p in proposals if p["operation"] == "record_request")
        self.run_op("review_changes", lambda tx: self.under.resolve(tx, proposal_id=request["id"], expected_version=1,
            decision="rejected", reason="Not applicable"), human=True)
        same = self.project(self.store(model="m2"))
        replay = next(p for p in same if p["operation"] == "record_request")
        self.assertEqual(replay["id"], request["id"])
        self.assertEqual(replay["status"], "rejected")
        changed = output()
        changed["requests"][0]["outcome"] = "Finish assessment on portal"
        replacement = next(p for p in self.project(self.store(model="m3", body=changed)) if p["operation"] == "record_request")
        self.assertIn("compare_prior:" + request["id"], replacement["blockers"])
        renewed = next(p for p in self.project(self.store(s=source(identity="new-message"))) if p["operation"] == "record_request")
        self.assertEqual(renewed["blockers"], [])

    def test_quote_history_and_sent_mail_create_no_new_requests(self):
        for s in (source(role="quoted"), source(direction="outgoing")):
            proposals = self.project(self.store(s=s))
            self.assertNotIn("record_request", [p["operation"] for p in proposals])

    def test_incomplete_coverage_blocks_acceptance(self):
        proposal = self.project(self.store(coverage=False))[0]
        self.assertIn("incomplete_evidence_coverage", proposal["blockers"])
        with self.assertRaises(DomainError):
            self.run_op("review_changes", lambda tx: self.under.resolve(tx, proposal_id=proposal["id"], expected_version=1,
                decision="applied"), human=True)

    def test_invalid_findings_and_time_normalization_are_rejected(self):
        context = AnalysisInput((source(),), ({"id": "app-1"},))
        for mutate in (lambda v: v.update(unknown=True),
                       lambda v: v.update(relevance=[]),
                       lambda v: v["facts"][0].update(confidence=float("nan")),
                       lambda v: v["facts"][0].update(kind=[]),
                       lambda v: v["facts"][0].update(target_id={}),
                       lambda v: v["facts"][0]["evidence"][0].update(source_id=[]),
                       lambda v: v["requests"][0].update(channel=[]),
                       lambda v: v["facts"][0]["evidence"][0].update(quote="invented"),
                       lambda v: v["facts"][0].update(target_id="not-a-candidate")):
            value = output()
            mutate(value)
            with self.assertRaises(DomainError):
                validate_analysis(value, context)
        value = output()
        value["temporal_facts"] = [{"kind": "deadline", "wording": "tomorrow", "normalized": "2026-10-10T00:00:00Z",
                                    "missing": ["timezone"], "evidence": [span("assessment")]}]
        with self.assertRaises(DomainError):
            validate_analysis(value, context)

    def test_claim_fencing_and_durable_failure(self):
        context = AnalysisInput((source(),), ({"id": "app-1"},))
        claim = self.run_op("analyze_observation", lambda tx: self.under.claim_analysis(tx, context=context, model_version="m1"))
        busy = self.run_op("analyze_observation", lambda tx: self.under.claim_analysis(tx, context=context, model_version="m1"))
        self.assertEqual(busy["status"], "busy")
        self.now = "2026-10-09T12:10:00Z"
        new = self.run_op("analyze_observation", lambda tx: self.under.claim_analysis(tx, context=context, model_version="m1"))
        with self.assertRaises(DomainError):
            self.run_op("record_analysis", lambda tx: self.under.store_analysis(tx, context=context, model_version="m1", output=output(), claim=claim))
        failed = self.run_op("record_analysis", lambda tx: self.under.store_analysis(tx, context=context, model_version="m1", failure_code="provider_failed", claim=new))
        self.assertEqual(failed["status"], "failed")
        latest = self.run_op("analyze_observation", lambda tx: self.under.claim_analysis(tx, context=context, model_version="m1"))
        recovered = self.run_op("record_analysis", lambda tx: self.under.store_analysis(tx, context=context, model_version="m1", output=output(), claim=latest))
        self.assertEqual(recovered["status"], "succeeded")

    def test_association_correction_preserves_evidence_and_history(self):
        msg = self.run_op("record_message", lambda tx: self.corr.record_message(tx, account_id="a", provider_message_id="m",
            source_version="v1", direction="incoming", authored_text=TEXT))
        assoc = self.run_op("review_changes", lambda tx: self.corr.link_message(tx, message_id=msg["id"], application_id="app-1"), human=True)
        changed = self.run_op("correct_association", lambda tx: self.corr.correct_association(tx, association_id=assoc["id"],
            application_id="app-2", expected_version=1, reason="Reviewed correction"), human=True)
        self.assertEqual(changed["version"], 2)
        with self.executor.read() as con:
            self.assertEqual(self.corr.get(con, msg["id"])["source_sha256"], msg["source_sha256"])
            self.assertEqual(self.corr.conversation(con, "app-1")["items"], [])
            self.assertEqual(len(self.corr.conversation(con, "app-2")["items"]), 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM command_history WHERE record_id=?", (assoc["id"],)).fetchone()[0], 2)

    def test_agent_cannot_resolve_even_via_propose_command(self):
        proposal = self.project(self.store())[0]
        with self.assertRaises(DomainError):
            self.run_op("propose_changes", lambda tx: self.under.resolve(tx, proposal_id=proposal["id"], expected_version=1, decision="rejected"))

    def test_browser_observation_requires_review_and_matches_exact_attempt(self):
        observation = {"id": "obs1", "application_id": "app-1", "device_id": "device1", "attempt_ref": "attempt1",
                       "activity": "website_acknowledgment", "source_digest": "a" * 64, "answers": {"name": "e\u0301"}}
        other = {"id": "sub1", "version": 2, "evidence": [{"device_id": "another-device", "attempt_ref": "attempt1"}]}
        projected = self.run_op("project_analysis", lambda tx: self.under.project_browser(tx, observation=observation, submissions=[other]))["proposals"][0]
        self.assertEqual(projected["status"], "pending")
        self.assertNotIn("submission_id", projected["input"])
        observation["id"] = "obs2"
        same = {"id": "sub2", "version": 3, "evidence": [{"device_id": "device1", "attempt_ref": "attempt1"}]}
        matched = self.run_op("project_analysis", lambda tx: self.under.project_browser(tx, observation=observation, submissions=[same]))["proposals"][0]
        self.assertEqual(matched["input"]["submission_id"], "sub2")
        self.assertEqual(matched["input"]["answers"], {"name": "e\u0301"})
        observation["id"] = "obs3"
        ambiguous = self.run_op("project_analysis", lambda tx: self.under.project_browser(tx, observation=observation, submissions=[same, dict(same, id="sub3")]))["proposals"][0]
        self.assertIn("ambiguous_submission_attempt", ambiguous["blockers"])
        observation["id"] = "obs4"
        truncated = self.run_op("project_analysis", lambda tx: self.under.project_browser(tx, observation=observation,
            submissions={"items": [], "truncated": True}))["proposals"][0]
        self.assertIn("incomplete_submission_coverage", truncated["blockers"])

    def browser(self, identity, activity="submission_attempt", **extra):
        return {"id": identity, "application_id": "app-1", "device_id": "device1", "attempt_ref": "attempt1",
                "activity": activity, "source_digest": "a" * 64, **extra}

    def project_browser(self, observation, **context):
        return self.run_op("project_analysis", lambda tx: self.under.project_browser(tx, observation=observation, **context))

    def test_attempt_ack_coalesces_without_resurrecting_or_downgrading(self):
        attempt, ack = self.browser("attempt"), self.browser("ack", "website_acknowledgment")
        first = self.project_browser(attempt)["proposals"][0]
        second = self.project_browser(ack)["proposals"][0]
        self.assertEqual(second["operation"], "confirm_submission")
        self.assertEqual({e["source_id"] for e in second["evidence"]}, {"attempt", "ack"})
        with self.executor.read() as con:
            self.assertEqual(self.under.get(con, first["id"])["status"], "superseded")
            self.assertEqual(self.under.get(con, first["id"])["version"], 2)
        self.assertEqual(self.project_browser(attempt)["proposals"][0]["id"], second["id"])
        self.assertEqual(self.project_browser(ack)["proposals"][0]["id"], second["id"])
        self.run_op("review_changes", lambda tx: self.under.resolve(tx, proposal_id=second["id"], expected_version=1, decision="rejected"), human=True)
        repeated = self.project_browser(ack)["proposals"][0]
        self.assertEqual(repeated["id"], second["id"])
        self.assertEqual(repeated["status"], "rejected")

    def test_browser_attempt_context_selects_ack_and_blocks_partial_coverage(self):
        attempt, ack = self.browser("attempt"), self.browser("ack", "website_acknowledgment")
        context = {"application_id": "app-1", "device_id": "device1", "attempt_ref": "attempt1",
                   "observations": [attempt, ack], "coverage": {"truncated": False}}
        result = self.project_browser(attempt, attempt=context)["proposals"][0]
        self.assertEqual(result["operation"], "confirm_submission")
        partial = self.project_browser(ack, attempt={**context, "coverage": {"truncated": True}})["proposals"][0]
        self.assertIn("incomplete_browser_evidence", partial["blockers"])
        restored = self.project_browser(ack, attempt=context)["proposals"][0]
        self.assertEqual(restored["status"], "pending")
        self.assertEqual(restored["blockers"], [])
        partial = restored
        with self.assertRaises(DomainError):
            self.project_browser(attempt, attempt={**context, "device_id": "another-device"})
        other = self.project_browser(self.browser("other", device_id="another-device"))["proposals"][0]
        with self.executor.read() as con:
            self.assertEqual(self.under.get(con, partial["id"])["status"], "pending")
            self.assertEqual(self.under.get(con, other["id"])["status"], "pending")

    def test_answer_capture_waits_then_attaches_exact_preserved_source(self):
        attempt = self.browser("attempt")
        capture = self.browser("capture", "answer_capture", source={"answer_snapshot": {"name": "e\u0301", "items": [None, {"raw": ""}]}})
        first = self.project_browser(attempt)["proposals"][0]
        waiting = self.project_browser(capture)
        self.assertEqual(waiting["status"], "awaiting_submission_review")
        self.assertEqual(waiting["proposals"], [])
        with self.executor.read() as con:
            self.assertEqual(self.under.get(con, first["id"])["status"], "pending")
        submission = {"id": "sub1", "version": 2, "status": "confirmed", "evidence": first["evidence"]}
        proposal = self.project_browser(capture, submissions=[submission])["proposals"][0]
        self.assertEqual(proposal["operation"], "attach_submission_answers")
        self.assertEqual(proposal["input"], {"submission_id": "sub1", "expected_version": 2, "observation_id": "capture"})
        self.assertEqual(proposal["expected_versions"], {"submission:sub1": 2})
        refreshed = self.project_browser(capture, submissions=[{**submission, "version": 3}])["proposals"][0]
        with self.executor.read() as con:
            self.assertEqual(self.under.get(con, proposal["id"])["status"], "superseded")
        self.run_op("review_changes", lambda tx: self.under.resolve(tx, proposal_id=refreshed["id"], expected_version=1, decision="rejected"), human=True)
        rejected = self.project_browser(capture, submissions=[{**submission, "version": 4}])["proposals"][0]
        self.assertEqual(rejected["status"], "rejected")
        attached = self.project_browser(capture, submissions=[{**submission, "answer_snapshots": [{"observation_id": "capture", "snapshot": capture["source"]["answer_snapshot"]}]}])
        self.assertEqual(attached["status"], "already_attached")
        self.assertEqual(self.project_browser(attempt, submissions=[submission])["status"], "already_confirmed")
        null_capture = self.browser("null", "answer_capture", source={"answer_snapshot": None})
        self.assertEqual(self.project_browser(null_capture, submissions=[submission])["proposals"][0]["input"]["observation_id"], "null")
        partial = self.project_browser(capture, submissions={"items": [submission], "truncated": True})
        self.assertEqual(partial["status"], "awaiting_submission_review")
        self.assertIn("incomplete_submission_coverage", partial["blockers"])

    def test_historical_correspondence_import_preserves_identity_and_creates_no_work(self):
        migration = Principal("converter", "worker", frozenset({"import_snapshot"}))
        records = {"messages": [{"id": "historic-msg", "account_id": "a", "provider_message_id": "p", "thread_id": None,
                                 "latest_revision": "historic-rev", "version": 2}],
                   "revisions": [{"id": "historic-rev", "message_id": "historic-msg", "source_version": "token",
                       "direction": "incoming", "occurred_at": None, "received_at": self.now,
                       "source_sha256": source().sha256, "archive_ref": "sealed-archive", "content_chars": len(TEXT), "metadata": {}}],
                   "associations": [{"id": "historic-association", "message_id": "historic-msg", "application_id": "old-app", "version": 3, "reason": "old decision"}]}
        context = CommandContext(migration, "import", "migration")
        result = self.executor.run(context, "import_snapshot", {}, lambda tx: self.corr.import_records(tx, records=records, source_snapshot="fixture-v1"))
        self.assertEqual(result["counts"]["revisions"], 1)
        with self.executor.read() as con:
            self.assertEqual(self.corr.get(con, "historic-msg")["revision"], "historic-rev")
            self.assertEqual(self.corr.get_association(con, "historic-association")["version"], 3)
            self.assertEqual(con.execute("SELECT count(*) FROM command_work").fetchone()[0], 0)

    def test_shared_analyzer_uses_provider_and_reserves_output_budget(self):
        import json
        from types import SimpleNamespace
        from job_search.applications.understanding import SharedAnalyzer
        class Provider:
            max_input_tokens = 100000
            calls = 0
            def count_tokens_upper_bound(self, text):
                return len(text)
            def generate(self, messages, **kwargs):
                self.calls += 1
                self.messages = messages
                return SimpleNamespace(text=json.dumps(output()))
        provider = Provider()
        context = AnalysisInput((source(),), ({"id": "app-1"},))
        self.assertEqual(SharedAnalyzer(provider).analyze(context), output())
        self.assertEqual(provider.calls, 1)
        provider.max_input_tokens = 100
        with self.assertRaises(DomainError):
            SharedAnalyzer(provider).analyze(context)
        self.assertEqual(provider.calls, 1)

    def test_strict_provider_schema_closes_nested_objects_and_decodes_offer_terms(self):
        import json
        from dataclasses import replace
        from job_search.applications.understanding import SharedAnalyzer
        from job_search.inference import OpenAICompatibleStructuredGenerator, load_inference_config
        from tests.test_job_search_inference import _profile, _completion
        config = replace(load_inference_config(_profile(Path(self.tmp.name))).structured_generation,
                         max_input_tokens=100000, default_max_output_tokens=8192)
        captured = []
        result = output()
        terms = {"equity": {"vesting": [1, 2]}, "exact_name": "e\u0301"}
        result["facts"] = [{"kind": "offer", "target_id": "app-1", "confidence": None,
                            "value": {"offer_id": None, "status": "offered", "terms": json.dumps(terms)},
                            "evidence": [span("Application received.")]}]
        def transport(url, headers, body, timeout, maximum):
            captured.append(json.loads(body))
            return _completion(json.dumps(result))
        provider = OpenAICompatibleStructuredGenerator(config, transport)
        context = AnalysisInput((source(),), ({"id": "app-1"},))
        decoded = SharedAnalyzer(provider).analyze(context)
        self.assertEqual(decoded["facts"][0]["value"], {"status": "offered", "terms": terms})
        schema = captured[0]["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        def closed(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value["additionalProperties"], False)
                    self.assertEqual(set(value["properties"]), set(value["required"]))
                    self.assertNotIn("operation", value["properties"])
                for child in value.values():
                    closed(child)
            elif isinstance(value, list):
                for child in value:
                    closed(child)
        closed(schema["schema"])
        self.assertIn("anyOf", schema["schema"]["properties"]["facts"]["items"])

    def test_analyzer_rejects_duplicate_json_fields(self):
        import json
        from types import SimpleNamespace
        from job_search.applications.understanding import SharedAnalyzer
        serialized = json.dumps(output())
        duplicated = serialized[:-1] + ', "relevance": "unrelated"}'
        provider = SimpleNamespace(max_input_tokens=100000, count_tokens_upper_bound=len,
            generate=lambda *args, **kwargs: SimpleNamespace(text=duplicated))
        with self.assertRaises(DomainError):
            SharedAnalyzer(provider).analyze(AnalysisInput((source(),), ({"id": "app-1"},)))

    def test_unavailable_source_has_durable_bounded_coverage_without_false_empty_analysis(self):
        refs = [{"source_id": "missing", "revision": "rev1", "sha256": "a" * 64}]
        failed = self.run_op("record_analysis", lambda tx: self.under.record_source_failure(tx,
            source_refs=refs, failure_code="source_unavailable", candidate_ids=["app-1"]))
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["output"])
        with self.executor.read() as con:
            page = self.under.coverage(con, application_id="app-1", source_id="missing", limit=1)
            self.assertEqual(page["items"][0]["sources"], refs)
            self.assertFalse(page["items"][0]["coverage"]["complete"])
            self.assertEqual(self.under.coverage(con, application_id="app-2")["items"], [])
            self.assertEqual(self.under.list_pending(con)["items"], [])

    def fact_proposals(self, values, *, versions=None, context_values=None):
        text = "Contact from recruiter. Interview details. Assessment details. Offer details. Outcome details. Submission details."
        s = source(text, identity="fact-message")
        context = AnalysisInput((s,), ({"id": "app-1"},), versions=versions or {}, context=context_values or {})
        body = {"relevance": "career_related", "associations": [], "facts": [], "requests": [], "temporal_facts": [], "uncertainties": []}
        for kind, value in values:
            body["facts"].append({"kind": kind, "value": value, "target_id": "app-1", "evidence": [span(kind.capitalize(), s)]})
        analysis = self.run_op("record_analysis", lambda tx: self.under.store_analysis(tx, context=context,
            model_version="facts-" + str(self.serial), output=body))
        return analysis, self.run_op("project_analysis", lambda tx: self.under.project_requests(tx,
            analysis_id=analysis["id"], application_id="app-1"))["proposals"]

    def test_contact_assessment_offer_and_complete_interview_facts_have_named_pending_operations(self):
        analysis, proposals = self.fact_proposals([
            ("contact", "Recruiter contact"),
            ("assessment", {"status": "requested", "description": "Coding assessment", "channel": "portal"}),
            ("offer", {"status": "offered", "terms": {"salary_text": "$100,000"}}),
            ("interview", {"status": "scheduled", "start_at": "2026-10-12T15:00:00Z", "end_at": "2026-10-12T16:00:00Z", "timezone": "America/Chicago"}),
        ])
        self.assertEqual({p["operation"] for p in proposals}, {"record_progress", "record_assessment", "record_offer", "schedule_interview"})
        for p in proposals:
            self.assertEqual(p["status"], "pending")
            self.assertEqual(p["blockers"], [])
            self.assertFalse(p["input"].get("create_task", False))
        with self.executor.read() as con:
            summaries = self.under.coverage(con, application_id="app-1")["items"][0]
            self.assertEqual(len(summaries["findings"]), 4)
            self.assertFalse(summaries["findings_truncated"])

    def test_incomplete_fact_values_stay_visible_without_guessed_times_or_terms(self):
        _, proposals = self.fact_proposals([
            ("interview", {"status": "scheduled", "start_at": "next Monday"}),
            ("offer", "It seems there may be an offer"),
            ("assessment", {"status": [], "description": "Unparsed state"}),
        ])
        self.assertEqual(len(proposals), 3)
        interview = next(p for p in proposals if p["operation"] == "schedule_interview")
        self.assertIn("needs_time", interview["blockers"])
        self.assertNotIn("end_at", interview["input"])
        self.assertNotIn("timezone", interview["input"])
        offer = next(p for p in proposals if p["operation"] == "record_offer")
        self.assertIn("needs_mapping", offer["blockers"])
        self.assertNotIn("terms", offer["input"])

    def test_existing_interview_and_outcome_require_captured_consequence_previews(self):
        _, blocked = self.fact_proposals([
            ("interview", {"interview_id": "round-1", "status": "requested"}),
            ("outcome", "rejected"),
        ])
        interview = next(p for p in blocked if p["operation"] == "schedule_interview")
        self.assertIn("needs_target", interview["blockers"])
        self.assertIn("requires_related_preview", interview["blockers"])
        closure = next(p for p in blocked if p["operation"] == "close_application")
        self.assertIn("requires_closure_preview", closure["blockers"])
        _, ready = self.fact_proposals([
            ("interview", {"interview_id": "round-2", "status": "requested"}),
            ("outcome", {"outcome": "withdrawn", "reason": "Explicit employer evidence"}),
        ], versions={"interview:round-2": 3, "application:app-1": 2}, context_values={
            "interview_previews": {"round-2": {"expected_related_versions": {"tasks:task-1": 2}}},
            "closure_previews": {"app-1": {"expected_version": 2, "expected_records": {"tasks:task-1": 2}}}})
        self.assertEqual(next(p for p in ready if p["operation"] == "schedule_interview")["input"]["expected_version"], 3)
        self.assertNotIn("requires_closure_preview", next(p for p in ready if p["operation"] == "close_application")["blockers"])
        self.assertEqual(next(p for p in ready if p["operation"] == "close_application")["input"]["expected_records"], {"tasks:task-1": 2})

    def test_submission_failure_is_never_projected_as_confirmation(self):
        _, proposals = self.fact_proposals([("submission", {"status": "failed"})])
        self.assertEqual(proposals[0]["operation"], "record_submission")
        self.assertEqual(proposals[0]["input"]["status"], "failed")

    def test_replacement_keeps_blockers_until_explicit_resolution_and_honors_empty_versions(self):
        original = self.run_op("propose_changes", lambda tx: self.under.propose(tx, operation="create_task",
            input={"application_id": "app-1", "description": "Needs details"}, application_id="app-1",
            expected_versions={"application:app-1": 1}, blockers=["needs_mapping", "needs_target"]))
        updated = self.run_op("review_changes", lambda tx: self.under.replace(tx, proposal_id=original["id"],
            expected_version=1, input={"application_id": "app-1", "description": "Exact task"},
            expected_versions={}, reason="Human supplied details", resolved_blockers=["needs_mapping"]), human=True)
        self.assertEqual(updated["expected_versions"], {})
        self.assertEqual(updated["blockers"], ["needs_target"])
        with self.assertRaises(DomainError):
            self.run_op("review_changes", lambda tx: self.under.replace(tx, proposal_id=updated["id"], expected_version=1,
                input=updated["input"], reason="Cannot resolve an absent blocker", resolved_blockers=["not_present"]), human=True)
        with self.executor.read() as con:
            self.assertEqual(self.under.get(con, updated["id"])["status"], "pending")


class AnalyzerTransportTest(unittest.TestCase):
    def provider(self, value=None, *, text=None, usage=None):
        import json
        from types import SimpleNamespace
        return SimpleNamespace(max_input_tokens=100000, count_tokens_upper_bound=lambda text: len(text.encode("utf-8")),
            generate=lambda *args, **kwargs: SimpleNamespace(text=text if text is not None else json.dumps(value), usage=usage or {}))

    def finding(self, source_text, quote, **span_fields):
        evidence = span(quote, source_text)
        evidence.update(span_fields)
        return {"relevance": "career_related", "associations": [], "facts": [], "requests": [{
            "kind": "reply", "outcome": "Reply", "responsible_party": "applicant", "requirement": "required", "channel": "email",
            "target_id": None, "evidence": [evidence]}], "temporal_facts": [], "uncertainties": []}

    def test_unique_exact_quote_repairs_unicode_counting_only_in_transport(self):
        from job_search.applications.understanding import SharedAnalyzer
        original = source("🙂 e\u0301\nPlease reply.\nThanks")
        context = AnalysisInput((original,))
        value = self.finding(original, "Please reply.", start=0, end=2)
        with self.assertRaises(DomainError):
            validate_analysis(value, context)
        decoded = SharedAnalyzer(self.provider(value)).analyze(context)
        self.assertEqual(decoded["requests"][0]["evidence"][0], span("Please reply.", original))
        validate_analysis(decoded, context)
        self.assertEqual(value["requests"][0]["evidence"][0]["start"], 0)

    def test_ambiguous_quote_wrong_source_and_revision_never_reanchor(self):
        from job_search.applications.understanding import SharedAnalyzer
        original = source("Please reply. Previous message: Please reply.")
        context = AnalysisInput((original,))
        for fields in ({"start": 1, "end": 2}, {"revision": "other-revision"}, {"source_id": "other-message"}, {"start": True}):
            with self.subTest(fields=fields), self.assertRaises(DomainError) as error:
                SharedAnalyzer(self.provider(self.finding(original, "Please reply.", **fields))).analyze(context)
            self.assertEqual(error.exception.code, "evidence_mismatch")
        exact = self.finding(original, "Please reply.")
        self.assertEqual(SharedAnalyzer(self.provider(exact)).analyze(context), exact)

    def test_quote_normalization_or_missing_words_are_not_repaired(self):
        from job_search.applications.understanding import SharedAnalyzer
        original = source("e\u0301 Please  reply.")
        for quote in ("é Please  reply.", "e\u0301 Please reply.", "Invented reply."):
            value = self.finding(original, "Please  reply.")
            value["requests"][0]["evidence"][0]["quote"] = quote
            with self.assertRaises(DomainError) as error:
                SharedAnalyzer(self.provider(value)).analyze(AnalysisInput((original,)))
            self.assertEqual(error.exception.code, "evidence_mismatch")

    def test_compact_prompt_retains_complete_email_and_semantic_context(self):
        import json
        from job_search.applications.understanding import SharedAnalyzer
        text = "🙂 e\u0301\nAuthored message.\nForwarded exact history:\n" + "Long source " * 1000
        original = source(text)
        job = {"id": "job", "title": "Engineer", "employer": "Example", "sources": [{"source": "fixture", "source_id": "role"}],
               "recorded_snapshot": {"job_url": "https://example.test/role"}, "recorded_snapshots": [{"description": "Copied history " * 20000}]}
        context = AnalysisInput((original,), ({"id": "app-1", "job": job},), {"application:app-1": 9}, context={
            "closure_previews": {"app-1": {"expected_records": {"task-" + str(i): 1 for i in range(1000)}}},
            "records": {"app-1": {"submissions": [{"id": "submission-1", "status": "confirmed", "occurred_at": "2026-10-09T12:00:00Z", "answers": {"essay": "Private old answer " * 20000}, "documents": [{"text": "Old document"}]}],
                "interviews": [{"id": "interview-1", "title": "Technical", "status": "scheduled", "start_at": "2026-10-10T12:00:00Z", "timezone": "UTC"}]}},
            "mail_projection": {"message_id": original.source_id, "revision": original.revision, "target_application_id": "app-1", "association_required": False}})
        before = context.descriptor()
        provider = self.provider({"relevance": "unrelated", "associations": [], "facts": [], "requests": [], "temporal_facts": [], "uncertainties": []})
        provider.max_input_tokens = 65536
        messages = []
        generate = provider.generate
        provider.generate = lambda value, **kwargs: messages.extend(value) or generate(value, **kwargs)
        SharedAnalyzer(provider).analyze(context)
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["sources"][0]["text"], text)
        self.assertEqual(payload["sources"][0]["sha256"], original.sha256)
        self.assertEqual(payload["candidates"][0]["job"]["job_url"], "https://example.test/role")
        self.assertEqual(payload["records"]["app-1"]["interviews"][0]["start_at"], "2026-10-10T12:00:00Z")
        self.assertNotIn("answers", payload["records"]["app-1"]["submissions"][0])
        self.assertNotIn("closure_previews", payload)
        self.assertEqual(context.descriptor(), before)

    def test_safe_failure_codes_distinguish_json_truncation_size_and_budget(self):
        from job_search.applications.understanding import SharedAnalyzer
        from job_search.applications.understanding.analyzer import ANALYSIS_FAILURE_REASONS
        context = AnalysisInput((source(),), ({"id": "app-1"},))
        cases = [(self.provider(text="private broken text"), "invalid_json"),
                 (self.provider({"facts": "not-an-array"}), "invalid_output"),
                 (self.provider(output(), usage={"finish_reason": "length"}), "output_truncated"),
                 (self.provider(text=" " * 65537), "output_too_large"),
                 (self.provider({**output(), "unexpected": "private"}), "invalid_output")]
        budget = self.provider(output()); budget.max_input_tokens = 100
        cases.append((budget, "context_budget_exceeded"))
        for provider, code in cases:
            with self.subTest(code=code), self.assertRaises(DomainError) as error:
                SharedAnalyzer(provider).analyze(context)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(str(error.exception), ANALYSIS_FAILURE_REASONS[code])

    def test_provider_envelope_budget_check_has_same_specific_failure(self):
        from job_search.applications.understanding import SharedAnalyzer
        from job_search.inference import InferenceTransportError
        provider = self.provider(output())
        def generate(*args, **kwargs):
            raise InferenceTransportError("inference request exceeds the configured context limit", retryable=False)
        provider.generate = generate
        with self.assertRaises(DomainError) as error:
            SharedAnalyzer(provider).analyze(AnalysisInput((source(),), ({"id": "app-1"},)))
        self.assertEqual(error.exception.code, "context_budget_exceeded")

    def test_unrelated_output_with_findings_is_rejected_not_silently_discarded(self):
        from job_search.applications.understanding import SharedAnalyzer
        context = AnalysisInput((source(),), ({"id": "app-1"},))
        value = {**output(), "relevance": "unrelated"}
        with self.assertRaises(DomainError):
            validate_analysis(value, context)
        with self.assertRaises(DomainError) as error:
            SharedAnalyzer(self.provider(value)).analyze(context)
        self.assertEqual(error.exception.code, "invalid_output")


if __name__ == "__main__":
    unittest.main()
