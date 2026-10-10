"""Current processing outcomes, retained attempts, and exact human recovery races."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_search.application_gateway import ApplicationGateway
from job_search.application_mail import ApplicationMailReader
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter
from job_search.applications.understanding import AnalysisInput, SourceText
from job_search.commands import CommandContext, DomainError, Principal
from job_search.service import JobSearchLedger


class ProcessingRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = ApplicationRuntime(self.root / "owners.db", clock=lambda:"2026-10-10T12:00:00Z")
        self.human = HumanApplicationAdapter(self.runtime, "reviewer")
        self.serial = 0
        self.source = self.message()

    def key(self):
        self.serial += 1
        return str(self.serial)

    def worker(self, operation, callback):
        return self.runtime.executor.run(CommandContext(Principal("worker", "worker", {operation}), self.key(), "inferred"), operation, {}, callback)

    def message(self, revision=1):
        return self.runtime.command(CommandContext(Principal("mail", "worker", {"record_message"}), self.key(), "inferred"), "record_message", {
            "account_id":"allowed", "provider_message_id":"mail", "source_version":revision,
            "direction":"incoming", "authored_text":"exact source " + str(revision), "archive_ref":"archive"})

    def failure(self, *, retry=None, code="invalid_output"):
        return self.worker("record_analysis", lambda tx:self.runtime.understanding.record_source_failure(tx,
            source_refs=[{"source_id":self.source["id"], "revision":self.source["revision"], "sha256":self.source["source_sha256"]}],
            failure_code=code, retry=retry))

    def success(self, *, complete=True, relevance="unrelated", retry=None):
        source = self.source
        context = AnalysisInput((SourceText(source["id"], source["revision"], source["source_sha256"], "exact source 1"),),
            coverage={"complete":complete, "reasons":[] if complete else ["attachments_not_loaded"]},
            context={"mail_projection":{"message_id":source["id"], "revision":source["revision"],
                       "target_application_id":None, "association_required":True}})
        output = {"relevance":relevance,"associations":[],"facts":[],"requests":[],"temporal_facts":[],"uncertainties":[]}
        return self.worker("record_analysis", lambda tx:self.runtime.understanding.store_analysis(tx, context=context,
            model_version="model", output=output, retry=retry))

    def issue(self):
        with self.runtime.executor.read() as con:
            return self.runtime.understanding.processing_for_source(con, self.source["id"], self.source["revision"])

    def decide(self, operation, issue=None, key=None):
        issue = issue or self.issue()
        return self.human.command(operation, {"issue_id":issue["issue_id"], "expected_version":issue["version"],
            "expected_analysis_id":issue["analysis_id"], "reason":"Reviewed this exact email"}, key or self.key())

    def current(self):
        return self.runtime.queries.dashboard_review(group="processing")["items"]

    def test_processing_review_uses_current_links_not_matching_candidates(self):
        apps = [self.human.command("save_job", {"job_source":{"source":"fixture", "source_id":str(i)}}, self.key()) for i in range(3)]
        self.worker("record_analysis", lambda tx:self.runtime.understanding.record_source_failure(tx,
            source_refs=[{"source_id":self.source["id"], "revision":self.source["revision"], "sha256":self.source["source_sha256"]}],
            failure_code="incomplete_coverage", candidate_ids=[a["id"] for a in apps[:2]]))
        def scoped(app):
            return self.runtime.queries.dashboard_review(application_id=app["id"], group="processing")["items"]
        self.assertIsNone(self.current()[0]["application_id"])
        self.assertTrue(all(scoped(app) == [] for app in apps))
        # A reviewed link can target an app absent from the model's candidates.
        link = self.human.command("link_message", {"message_id":self.source["id"], "application_id":apps[2]["id"]}, self.key())
        self.assertEqual(self.current()[0]["application_id"], apps[2]["id"])
        self.assertEqual(len(scoped(apps[2])), 1)
        self.assertEqual(scoped(apps[0]), [])
        with self.runtime.executor.read() as con:
            preview = self.runtime.workflows.preview_association_correction(con, link["id"])
        self.human.command("correct_association", {"association_id":link["id"], "application_id":apps[0]["id"],
            "expected_version":1, "preview_digest":preview["preview_digest"], "corrections":[], "reason":"Correct reviewed target"}, self.key())
        self.assertEqual(scoped(apps[2]), [])
        self.assertEqual(len(scoped(apps[0])), 1)
        self.assertEqual(self.current()[0]["processing"]["application_id"], apps[0]["id"])

    def test_linked_processing_pagination_skips_unlinked_issues(self):
        app = self.human.command("save_job", {"job_source":{"source":"fixture", "source_id":"role"}}, self.key())
        self.failure()
        ids = set()
        for i in range(3):
            self.source = self.runtime.command(CommandContext(Principal("mail", "worker", {"record_message"}), self.key(), "inferred"), "record_message", {
                "account_id":"allowed", "provider_message_id":"linked-" + str(i), "source_version":1,
                "direction":"incoming", "authored_text":"A source", "archive_ref":"archive"})
            self.failure()
            self.human.command("link_message", {"message_id":self.source["id"], "application_id":app["id"]}, self.key())
            ids.add(self.issue()["issue_id"])
        seen, cursor = [], None
        for _ in range(4):
            page = self.runtime.queries.dashboard_review(application_id=app["id"], group="processing", limit=1, cursor=cursor)
            seen.extend(item["id"] for item in page["items"])
            cursor = page["pages"]["processing"]["next_cursor"]
            if cursor is None:
                break
        self.assertIsNone(cursor)
        self.assertEqual(set(seen), ids)
        self.assertEqual(len(seen), len(ids))
        self.assertEqual(len(self.current()), 4)

    def test_repeated_failures_are_one_current_issue_and_success_requires_projection(self):
        first = self.failure()
        last = self.failure(code="invalid_json")
        self.assertEqual(len(self.current()), 1)
        self.assertEqual(self.issue()["attempt_count"], 2)
        self.assertEqual(self.issue()["analysis_id"], last["id"])
        history = self.runtime.queries.dashboard_review(group="processing_history")["items"]
        self.assertEqual({i["id"] for i in history}, {first["id"], last["id"]})
        analysis = self.success()
        self.assertEqual(self.issue()["failure_code"], "projection_pending")
        self.assertEqual(len(self.current()), 1)
        self.runtime.project_message_analysis(analysis["id"])
        self.assertEqual(self.current(), [])
        self.assertEqual(self.issue()["status"], "succeeded")
        self.assertEqual(self.issue()["attempt_count"], 3)

    def test_projection_failure_stays_current_and_can_finish_without_model_rerun(self):
        analysis = self.success()
        with patch.object(self.runtime.understanding, "project_requests", side_effect=DomainError("invalid_input", "sensitive provider text")):
            with self.assertRaises(DomainError): self.runtime.project_message_analysis(analysis["id"])
        self.assertEqual(self.issue()["failure_code"], "projection_failed")
        with self.runtime.executor.read() as con:
            self.assertNotIn("sensitive provider text", repr(tuple(con.iterdump())))
        self.runtime.project_message_analysis(analysis["id"])
        self.assertEqual(self.current(), [])

    def test_retry_is_exact_once_then_manual_resolution_fences_the_inflight_result(self):
        self.failure()
        issue = self.issue()
        queued = self.decide("retry_processing", issue, "retry-key")
        self.assertEqual(self.decide("retry_processing", issue, "retry-key"), queued)
        with self.assertRaises(DomainError): self.decide("retry_processing", issue)
        with self.runtime.executor.read() as con:
            work = self.runtime.executor.work_page(con)["items"]
            retries = [i for i in work if i["kind"] == "retry_processing"]
            self.assertEqual(len(retries), 1)
            retry = retries[0]["payload"]
            self.assertTrue(self.runtime.understanding.processing_retry_applicable(con, retry))
        self.decide("resolve_processing", queued)
        with self.assertRaises(DomainError): self.success(retry=retry)
        self.assertEqual(self.issue()["status"], "resolved_manually")
        self.assertEqual(self.issue()["attempt_count"], 1)
        delayed = self.success()  # An already-running ordinary delivery can arrive late.
        self.assertEqual(self.runtime.project_message_analysis(delayed["id"])["status"], "superseded")
        self.assertEqual(self.issue()["status"], "resolved_manually")
        self.assertEqual(self.current(), [])
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.list_pending(con)["items"], [])
            self.assertEqual(self.runtime.actions.list_actions(con)["items"], [])

    def test_new_revision_supersedes_old_issue_and_refuses_old_decisions(self):
        self.failure()
        old = self.issue()
        self.message(revision=2)
        self.assertEqual(self.issue()["status"], "superseded")
        self.assertEqual(self.current(), [])
        for operation in ("retry_processing", "resolve_processing"):
            with self.assertRaises(DomainError): self.decide(operation, old)
        self.assertEqual(len(self.runtime.queries.dashboard_review(group="processing_history")["items"]), 1)

    def test_new_attempt_invalidates_review_version_and_worker_cannot_resolve(self):
        self.failure()
        old = self.issue()
        self.failure()
        with self.assertRaises(DomainError): self.decide("resolve_processing", old)
        with self.assertRaises(DomainError):
            self.runtime.command(CommandContext(Principal("worker", "worker", {"resolve_processing"}), self.key()), "resolve_processing", {
                "issue_id":old["issue_id"], "expected_version":old["version"], "expected_analysis_id":old["analysis_id"], "reason":"not human"})

    def test_unrelated_incomplete_finishes_without_effects_but_uncertain_stays_open(self):
        unrelated = self.success(complete=False)
        result = self.runtime.project_message_analysis(unrelated["id"])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(self.issue()["status"], "succeeded")
        self.assertFalse(self.issue()["coverage"]["complete"])
        uncertain = self.success(relevance="uncertain")
        self.runtime.project_message_analysis(uncertain["id"])
        self.assertEqual(self.issue()["status"], "open")
        self.assertEqual(self.issue()["failure_code"], "relevance_uncertain")

    def test_known_schema_upgrade_preserves_attempts_and_pending_projection(self):
        from job_search.commands import CommandExecutor, encode
        from job_search.applications.understanding.api import _PREVIOUS_SCHEMA
        path = self.root / "old-schema.db"
        old = CommandExecutor(path, {"understanding":_PREVIOUS_SCHEMA})
        def seed(tx):
            with tx.scope("understanding"):
                for identity, source, status, complete, relevance, pending in (
                    ("failure", "same", "failed", True, None, False),
                    ("success", "same", "succeeded", True, "career_related", True),
                    ("unrelated", "other", "succeeded", False, "unrelated", False),
                    ("unclear", "unclear", "succeeded", True, "uncertain", False)):
                    descriptor = {"sources":[{"source_id":source,"revision":"r1","sha256":"a"*64}],
                                  "coverage":{"complete":complete},"candidates":[],"context":{},"versions":{}}
                    output = {"relevance":relevance} if relevance else None
                    tx.connection.execute("INSERT INTO understand_analyses VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        identity, identity, identity, encode(descriptor), "1", "1", "old", status,
                        encode(output) if output else None, "invalid_output" if status == "failed" else None,
                        "2026-10-09T12:00:00Z"))
                    if pending: tx.enqueue("understanding", "project_analysis", identity, {"analysis_id":identity})
            return {"seeded":True}
        old.run(CommandContext(Principal("converter", "worker", {"import_snapshot"}), "seed", "migration"), "import_snapshot", {}, seed)
        upgraded = ApplicationRuntime(path)
        with upgraded.executor.read() as con:
            pending = upgraded.understanding.processing_for_source(con, "same", "r1")
            self.assertEqual(pending["analysis_id"], "success")
            self.assertEqual(pending["attempt_count"], 2)
            self.assertEqual(pending["failure_code"], "projection_pending")
            self.assertEqual(upgraded.understanding.processing_for_source(con, "other", "r1")["status"], "succeeded")
            self.assertEqual(upgraded.understanding.processing_for_source(con, "unclear", "r1")["failure_code"], "relevance_uncertain")
            self.assertEqual(len(upgraded.understanding.coverage(con)["items"]), 4)
        # Opening again recognizes the new checksum without repeating backfill.
        ApplicationRuntime(path)

    def test_gateway_requires_source_account_grant_without_loading_private_body(self):
        self.failure()
        gateway = ApplicationGateway(self.runtime, JobSearchLedger(self.root / "operational.db"))
        issue = self.issue()
        payload = {"issue_id":issue["issue_id"], "expected_version":issue["version"],
                   "expected_analysis_id":issue["analysis_id"], "reason":"Not actionable"}
        self.runtime.mail_reader = ApplicationMailReader(self.runtime, None, ("other-account",))
        with self.assertRaises(DomainError): gateway.command("resolve_processing", payload, self.key(), "human")
        self.assertEqual(self.issue()["status"], "open")
        self.runtime.mail_reader = ApplicationMailReader(self.runtime, None, ("allowed",))
        result = gateway.command("resolve_processing", payload, self.key(), "human")
        self.assertEqual(result["status"], "resolved_manually")


if __name__ == "__main__": unittest.main()
