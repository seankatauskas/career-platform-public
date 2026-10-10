"""Offline combined Review paging and exact human evidence-read boundaries."""
from pathlib import Path
import tempfile
import unittest

from job_search.application_mail import ApplicationMailReader
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import AgentApplicationAdapter, HumanApplicationAdapter
from job_search.commands import CommandContext, DomainError, Principal


class Archive:
    def __init__(self):
        self.bodies = {}
        self.accounts = []

    def for_account(self, account_id):
        self.accounts.append(account_id)
        return self

    def read_message(self, reference):
        return self.bodies[reference]


class OwnerDashboardReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / "owners.db", clock=lambda: "2026-10-09T12:00:00Z")
        self.human = HumanApplicationAdapter(self.runtime, "reviewer")
        self.agent = AgentApplicationAdapter(self.runtime)
        self.serial = 0
        self.apps = [self.command("save_job", {"job_source": {"source": "fixture", "source_id": str(i), "title": "Role " + str(i)}}) for i in range(2)]
        self.archive = Archive()
        self.reader = ApplicationMailReader(self.runtime, self.archive, ("allowed",))

    def key(self):
        self.serial += 1
        return str(self.serial)

    def command(self, operation, payload):
        return self.human.command(operation, payload, self.key())

    def message(self, version=1, *, account="allowed", text="Exact e\u0301 source", identity="message"):
        ref = account + ":" + str(version)
        self.archive.bodies[ref] = text
        return self.runtime.command(CommandContext(Principal("mail", "worker", {"record_message"}), self.key(), "inferred"), "record_message", {
            "account_id": account, "provider_message_id": identity, "source_version": version,
            "direction": "incoming", "authored_text": text, "archive_ref": ref})

    def failure(self, source, application_id=None):
        return self.runtime.executor.run(CommandContext(Principal("analysis", "worker", {"record_analysis"}), self.key(), "inferred"), "record_analysis", {},
            lambda tx: self.runtime.understanding.record_source_failure(tx,
                source_refs=[{"source_id": source["id"], "revision": source["revision"], "sha256": source["source_sha256"]}],
                failure_code="source_unavailable", candidate_ids=[application_id] if application_id else []))

    def proposal(self, app):
        return self.agent.call("propose_changes", {"operation": "add_note", "input": {"application_id": app["id"], "text": self.key()},
            "application_id": app["id"]}, idempotency_key=self.key())

    def action(self, app):
        envelope = {"kind": "create_calendar_entry", "application_id": app["id"], "pursuit_no": 1, "account_id": "allowed", "target": {},
            "payload": {"starts_at": "2026-10-10T13:00:00Z", "ends_at": "2026-10-10T14:00:00Z", "location": self.key()},
            "context_versions": {"application:" + app["id"]: app["version"]}, "consequence": None}
        return self.runtime.executor.run(CommandContext(self.human.principal, self.key()), "prepare_calendar_change", envelope,
            lambda tx: self.runtime.actions.prepare_calendar_change(tx, envelope))

    def dump(self):
        with self.runtime.executor.read() as con:
            return tuple(con.iterdump())

    def test_combined_queue_pages_each_group_without_starving_or_mutating(self):
        source = self.message()
        expected = {"proposal": set(), "external_action": set(), "processing": set(), "processing_history": set()}
        for _ in range(3):
            expected["proposal"].add(self.proposal(self.apps[0])["id"])
            expected["external_action"].add(self.action(self.apps[0])["action_id"])
            source = self.message(identity=self.key())
            expected["processing_history"].add(self.failure(source, self.apps[0]["id"])["id"])
            with self.runtime.executor.read() as con:
                expected["processing"].add(self.runtime.understanding.processing_for_source(con, source["id"], source["revision"])["issue_id"])
        before = self.dump()
        page = self.runtime.queries.dashboard_review(limit=1)
        self.assertEqual(len(page["items"]), 4)
        self.assertEqual(set(page["pages"]), {"proposals", "external_actions", "processing", "processing_history"})
        self.assertTrue(page["truncated"])
        for group, kind in (("proposals", "proposal"), ("external_actions", "external_action"), ("processing", "processing"), ("processing_history", "processing_history")):
            actual = [item["id"] for item in page["items"] if item["kind"] == kind]
            cursor = page["pages"][group]["next_cursor"]
            while cursor:
                continuation = self.runtime.queries.dashboard_review(group=group, limit=1, cursor=cursor)
                self.assertEqual(set(continuation["pages"]), {group})
                self.assertTrue(all(item["kind"] == kind for item in continuation["items"]))
                actual.extend(item["id"] for item in continuation["items"])
                cursor = continuation["pages"][group]["next_cursor"]
            self.assertEqual(set(actual), expected[kind])
            self.assertEqual(len(actual), len(expected[kind]))
        self.assertEqual(self.dump(), before)

    def test_filters_precede_limits_and_cursors_cannot_change_group_or_application(self):
        source = self.message()
        for app in self.apps:
            for _ in range(2):
                self.proposal(app)
                self.action(app)
                linked = self.message(identity=self.key())
                self.command("link_message", {"message_id": linked["id"], "application_id": app["id"]})
                self.failure(linked, app["id"])
        unassociated = self.failure(source)
        selected = self.runtime.queries.dashboard_review(application_id=self.apps[1]["id"], limit=1)
        self.assertEqual(len(selected["items"]), 4)
        self.assertTrue(all(item["application_id"] == self.apps[1]["id"] for item in selected["items"]))
        self.assertNotIn(unassociated["id"], [item["id"] for item in selected["items"]])
        global_processing = self.runtime.queries.dashboard_review(group="processing", limit=100)
        self.assertIn(unassociated["id"], [item["processing"]["analysis_id"] for item in global_processing["items"]])
        for item in global_processing["items"]:
            self.assertEqual(item["processing"]["sources"][0]["owner"], "correspondence")
        cursor = selected["pages"]["proposals"]["next_cursor"]
        for args in ({"group": "external_actions", "application_id": self.apps[1]["id"]},
                     {"group": "proposals", "application_id": self.apps[0]["id"]},
                     {"group": "proposals"}, {}):
            with self.assertRaises(DomainError):
                self.runtime.queries.dashboard_review(cursor=cursor, **args)

    def test_review_excludes_rejected_actions_and_accepted_proposals(self):
        app = self.apps[0]
        rejected = self.action(app)
        self.command("reject_action", {"action_id": rejected["action_id"], "expected_digest": rejected["digest"]})
        accepted = self.proposal(app)
        self.command("review_changes", {"decisions": [{"proposal_id": accepted["id"], "expected_version": accepted["version"], "decision": "accept"}]})
        pending = self.proposal(app)
        action = self.action(app)
        page = self.runtime.queries.dashboard_review(limit=1)
        self.assertEqual({item["id"] for item in page["items"]}, {pending["id"], action["action_id"]})

    def test_review_source_reads_exact_unassociated_revision_and_enforces_account_and_digest(self):
        old = self.message(text="Exact e\u0301\n<script>source</script>")
        current = self.message(2, text="Changed source")
        before = self.dump()
        value = self.reader.read_review_source(old["id"], old["revision"], old["source_sha256"])
        self.assertEqual(value["text"], "Exact e\u0301\n<script>source</script>")
        self.assertEqual(value["revision"], old["revision"])
        self.assertTrue(value["coverage"]["complete"])
        self.assertEqual(self.dump(), before)
        with self.assertRaises(DomainError):
            self.reader.read_review_source(current["id"], current["revision"], old["source_sha256"])
        denied = self.message(account="denied")
        calls = list(self.archive.accounts)
        with self.assertRaises(DomainError) as error:
            self.reader.read_review_source(denied["id"], denied["revision"], denied["source_sha256"])
        self.assertEqual(error.exception.code, "not_authorized")
        self.assertEqual(self.archive.accounts, calls)

    def test_proposal_spans_resolve_the_preserved_revision_without_replacing_a_claimed_hash(self):
        original = self.message(text="Original quoted source")
        self.message(2, text="Latest different source")
        span = {"source_id": original["id"], "revision": original["revision"], "start": 0, "end": 8, "quote": "Original"}
        self.agent.call("propose_changes", {"operation": "add_note", "input": {"application_id": self.apps[0]["id"], "text": "Review exact source"},
            "application_id": self.apps[0]["id"], "evidence": [span, {**span, "sha256": "f" * 64}]}, idempotency_key=self.key())
        before = self.dump()
        evidence = self.runtime.queries.dashboard_review(group="proposals")["items"][0]["evidence"]
        self.assertEqual(evidence[0], {**span, "owner": "correspondence", "sha256": original["source_sha256"]})
        self.assertEqual(evidence[1]["sha256"], "f" * 64)
        self.assertEqual(self.dump(), before)

    def test_review_source_truncation_missing_archive_and_tamper_never_claim_full_evidence(self):
        source = self.message(text="Exact source text")
        limited = self.reader.read_review_source(source["id"], source["revision"], source["source_sha256"], limit=5)
        self.assertEqual(limited["text"], "Exact")
        self.assertFalse(limited["coverage"]["complete"])
        with self.assertRaises(DomainError):
            self.reader.read_review_source(source["id"], source["revision"], source["source_sha256"], limit=250001)
        self.archive.bodies["allowed:1"] = "Tampered"
        with self.assertRaises(DomainError):
            self.reader.read_review_source(source["id"], source["revision"], source["source_sha256"])
        del self.archive.bodies["allowed:1"]
        missing = self.reader.read_review_source(source["id"], source["revision"], source["source_sha256"])
        self.assertFalse(missing["coverage"]["complete"])
        self.assertIsNone(missing["text"])


if __name__ == "__main__":
    unittest.main()
