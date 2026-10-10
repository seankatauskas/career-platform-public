"""Offline predecessor conversion using real legacy migrations and fictional data."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from job_search.application_migration import convert_snapshot, historical_rows, ARCHIVE_NAME, CANDIDATE_NAME
from job_search.application_runtime import ApplicationRuntime
from job_search.commands import DomainError
from job_search.contracts import ApplicationEventType, EventInput, EventProposalInput, ProducerKind
from job_search.db import connect
from tests.test_job_search_ledger import make_service, start, context, stamp


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path, self.ledger = make_service(self.tmp.name)
        self.app = start(self.ledger)["application"]["application_id"]
        self.destination = self.root / "converted"

    def convert(self):
        report = convert_snapshot(self.path, self.destination)
        return report, ApplicationRuntime(self.destination / CANDIDATE_NAME)

    def event(self, kind, payload=None, key=None):
        return self.ledger.record_event(EventInput(self.app, kind, stamp(), payload or {}, key or kind.value, context(key or kind.value)))

    def mail(self):
        body = "Please reply. Exact e\u0301\n  text"
        evidence = self.ledger.record_mail_evidence({"account_id": "account", "immutable_message_id": "remote-message",
            "sender": "recruiter@example.test", "subject": "Next step", "received_at": stamp(),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "excerpt": body}, context("evidence", "system", "outlook_sync"))["evidence"]
        observed = self.ledger.lifecycle.observe_mail({"account_id": "account", "immutable_message_id": "remote-message",
            "direction": "inbound", "source_at": stamp(), "received_at": evidence["received_at"], "evidence_id": evidence["evidence_id"],
            "sender": "recruiter@example.test", "subject": "Next step"}, context("observe", "system", "outlook_sync"))
        with connect(self.path) as con:
            message = dict(con.execute("SELECT * FROM lifecycle_mail_observations WHERE immutable_message_id='remote-message'").fetchone())
            con.execute("INSERT INTO lifecycle_mail_links VALUES(?,?,1,'reviewed',?)", (message["observation_id"], self.app, stamp()))
        return evidence, message

    def test_current_records_are_operational_and_old_work_stays_inert(self):
        task = self.ledger.lifecycle.create_task(self.app, {"kind": "reply", "owner": "applicant", "note": "Reply exactly", "due_at": stamp(86400)}, context("task"))["task"]
        reminder = self.ledger.create_reminder({"application_id": self.app, "due_at": stamp(3600), "note": "Review the role"}, context("reminder"))["reminder"]
        submission = self.ledger.record_submission(self.app, stamp(), context("submit"), payload={"resume": {"sha256": "a" * 64, "decision": "matched_upload"}})
        self.event(ApplicationEventType.INTERVIEW_SCHEDULED, {"starts_at": stamp(86400), "ends_at": stamp(90000), "time_zone": "UTC"})
        with connect(self.path) as con:
            con.execute("CREATE TABLE application_notes(note_id TEXT PRIMARY KEY,application_id TEXT,text TEXT,created_at TEXT,updated_at TEXT)")
            con.execute("INSERT INTO application_notes VALUES('note-exact',?,?,?,?)", (self.app, "Note e\u0301\n  exact", stamp(), stamp()))
            con.execute("INSERT INTO interview_rounds(round_id,application_id,status,starts_at,ends_at,time_zone,created_at,updated_at) VALUES('round',?,'confirmed',?,?,'UTC',?,?)", (self.app, stamp(86400), stamp(90000), stamp(), stamp()))
            old_pending = con.execute("SELECT count(*) FROM outbox_messages WHERE status='pending'").fetchone()[0]
        self.assertGreater(old_pending, 0)
        report, runtime = self.convert()
        self.assertTrue(report["ready_for_review"], report["issues"])
        workspace = runtime.queries.workspace(self.app)
        self.assertEqual(workspace["progress"]["stage"], "interviewing")
        self.assertEqual(workspace["records"]["notes"]["items"][0]["text"], "Note e\u0301\n  exact")
        self.assertEqual(workspace["records"]["tasks"]["items"][0]["id"], task["task_id"])
        self.assertEqual(workspace["records"]["reminders"]["items"][0]["id"], reminder["reminder_id"])
        self.assertEqual(workspace["records"]["submissions"]["items"][0]["documents"][0]["sha256"], "a" * 64)
        self.assertTrue(workspace["paused"])
        self.assertEqual(report["validation"]["pending_dispatch_count"], 0)
        with runtime.executor.read() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM app_feedback").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM command_work").fetchone()[0], 0)
        self.assertEqual(report["source_counts"]["outbox_messages"], old_pending)

    def test_unknown_direction_is_preserved_as_unresolved_evidence(self):
        _, message = self.mail()
        with connect(self.path) as con:
            con.execute("UPDATE lifecycle_mail_observations SET direction='unknown' WHERE observation_id=?", (message['observation_id'],))
        report, runtime = self.convert()
        self.assertTrue(report['ready_for_review'], report['issues'])
        self.assertEqual(runtime.queries.workspace(self.app)['conversation']['items'][0]['direction'], 'unknown')
        self.assertEqual(report['validation']['pending_dispatch_count'], 0)

    def test_immutable_archive_preserves_blobs_receipts_and_exact_answers(self):
        values = {"version": 1, "fields": [{"field_key": "why", "prompt": "Why?", "section": "", "control": "text", "value": "e\u0301\n  answer"},
            {"field_key": "consent", "prompt": "Consent", "section": "", "control": "checkbox", "value": True}], "omitted_fields": 0, "truncated_values": 0}
        with connect(self.path) as con:
            con.execute("INSERT INTO browser_devices VALUES('device','hash','origin','audience',?,NULL)", (stamp(),))
            con.execute("INSERT INTO browser_attempts VALUES('attempt','device',?,'attempted',?,?,'','{}')", (self.app, stamp(), stamp()))
            exact_json = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            con.execute("INSERT INTO application_answer_snapshots VALUES('capture',?,'attempt',?,?,?,'request-hash',?)", (self.app, stamp(), stamp(), "https://example.test/apply", exact_json))
            con.execute("INSERT INTO mail_archive VALUES('archive','account','message','key',?,?,?,?,1,0,?,?)", (b"n" * 12, b"ciphertext" * 2, "b" * 64, "c" * 64, stamp(), stamp()))
            receipts = [dict(row) for row in con.execute("SELECT * FROM command_results")]
        report, runtime = self.convert()
        archived = historical_rows(self.destination, "mail_archive")["items"][0]
        self.assertEqual(archived["ciphertext"], {"$sqlite_blob_base64": "Y2lwaGVydGV4dGNpcGhlcnRleHQ="})
        self.assertEqual(historical_rows(self.destination, "command_results")["items"], receipts)
        self.assertEqual(historical_rows(self.destination, "application_answer_snapshots")["items"][0]["snapshot_json"], exact_json)
        record = runtime.queries.workspace(self.app)["records"]["submissions"]["items"][0]
        self.assertEqual(record["answers"]["why"], "e\u0301\n  answer")
        self.assertIs(record["captured_answer_snapshots"][0]["snapshot"]["fields"][1]["value"], True)
        self.assertEqual(stat.S_IMODE((self.destination / ARCHIVE_NAME).stat().st_mode), 0o400)
        self.assertTrue(report["validation"]["archive_unchanged"])

    def test_pending_proposals_revalidate_and_uncertain_approvals_block_combination(self):
        evidence, message = self.mail()
        proposal = self.ledger.create_event_proposal(EventProposalInput(evidence_id=evidence["evidence_id"], proposed_application_id=self.app,
            event_type=ApplicationEventType.RECRUITER_CONTACT, producer_kind=ProducerKind.RULE, producer_version="old",
            confidence=1, candidate_application_ids=[self.app], evidence_quote="Please reply", span_start=0, span_end=12,
            payload={}, dedupe_key="proposal"), context("proposal", "system", "outlook_sync"))["proposal"]
        with connect(self.path) as con:
            con.execute("INSERT INTO career_send_proposals(proposal_id,application_id,account_id,evidence_id,payload_json,payload_hash,source_hash,status,expires_at,created_at,updated_at) VALUES('old-send',?,'account',?,'{\"body\":\"exact\"}',?,?,'uncertain',?,?,?)", (self.app, evidence["evidence_id"], "d" * 64, "e" * 64, stamp(600), stamp(), stamp()))
        report, runtime = self.convert()
        self.assertFalse(report["ready_for_review"])
        workspace = runtime.queries.workspace(self.app)
        self.assertEqual(workspace["review"]["items"][0]["id"], proposal["proposal_id"])
        self.assertEqual(workspace["review"]["items"][0]["status"], "pending")
        self.assertIn("migration_revalidation_required", workspace["review"]["items"][0]["blockers"])
        self.assertEqual(workspace["conversation"]["items"][0]["id"], message["observation_id"])
        self.assertEqual(workspace["conversation"]["items"][0]["provider_message_id"], "remote-message")
        with runtime.executor.read() as con:
            self.assertTrue(runtime.actions.has_unresolved(con, self.app))
            self.assertEqual(runtime.actions.list_actions(con, self.app)["items"], [])
            self.assertEqual(con.execute("SELECT count(*) FROM command_work").fetchone()[0], 0)

    def test_missing_references_and_manual_corrections_are_explicit_blockers(self):
        self.event(ApplicationEventType.MANUAL_CORRECTION, {"target_phase": "active", "reason": "Historical correction"})
        # Deliberately inconsistent fictional snapshot, using plain SQLite so the
        # converter must report the pre-existing missing reference honestly.
        with sqlite3.connect(self.path) as con:
            con.execute("INSERT INTO reminders VALUES('orphan','absent','note',?,'scheduled','old-reminder',?,NULL,NULL)", (stamp(60), stamp()))
        report, runtime = self.convert()
        self.assertFalse(report["ready_for_review"])
        codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("missing_application", codes)
        self.assertIn("source_foreign_key_missing", codes)
        self.assertIn("manual_correction_requires_semantic_review", codes)
        self.assertEqual(historical_rows(self.destination, "reminders")["items"][0]["application_id"], "absent")
        self.assertEqual(runtime.queries.workspace(self.app)["progress"]["stage"], "tracking")

    def browser_attempt(self, identity, app_id=None):
        with connect(self.path) as con:
            con.execute("INSERT OR IGNORE INTO browser_devices VALUES('device','hash','origin','audience',?,NULL)", (stamp(),))
            con.execute("INSERT INTO browser_attempts VALUES(?,'device',?,'attempted',?,?,'','{}')", (identity,app_id or self.app,stamp(),stamp()))
            snapshot = {"version":1,"fields":[{"field_key":"why","value":"Exact answer " + identity}],"omitted_fields":0,"truncated_values":0}
            con.execute("INSERT INTO application_answer_snapshots VALUES(?,?,?, ?,?,?,'request-hash',?)",
                ("capture-"+identity,app_id or self.app,identity,stamp(),stamp(),"https://example.test/apply",json.dumps(snapshot)))

    def _assert_confirmation_unlinked(self, count):
        for number in range(count):
            self.browser_attempt("attempt-"+str(number))
        confirmation = self.event(ApplicationEventType.SUBMISSION_CONFIRMED)
        report,runtime = self.convert()
        self.assertTrue(report["ready_for_review"],report["issues"])
        workspace = runtime.queries.workspace(self.app)
        self.assertEqual(workspace["progress"]["stage"],"active")
        submissions = workspace["records"]["submissions"]["items"]
        confirmed = next(row for row in submissions if row["status"] == "confirmed")
        self.assertEqual(confirmed["id"],confirmation["event"]["event_id"])
        self.assertEqual(confirmed["answers"],{})
        self.assertEqual(confirmed["documents"],[])
        self.assertFalse(confirmed["click_time_known"])
        self.assertEqual(confirmed["attempt_link"]["status"],"unresolved")
        self.assertIn("submission_attempt_link_unresolved",confirmed["migration_warnings"])
        captures = [row for row in submissions if row["id"].startswith("attempt-")]
        self.assertEqual(len(captures),count)
        self.assertTrue(all(row["status"] == "unreviewed" for row in captures))
        self.assertTrue(all(row["captured_answer_snapshots"] for row in captures))
        synthetic = [row for row in submissions if "legacy_confirmation_attempt_link_unverified" in row.get("migration_warnings",[])]
        self.assertEqual(len(synthetic),1)
        self.assertEqual(synthetic[0]["status"],"unreviewed")
        self.assertFalse(synthetic[0]["click_time_known"])
        self.assertEqual(synthetic[0]["documents"],[])
        self.assertTrue(all(issue["severity"] == "warning" for issue in report["issues"]))

    def test_confirmation_does_not_guess_ambiguous_attempt(self):
        self._assert_confirmation_unlinked(2)

    def test_confirmation_does_not_guess_sole_attempt(self):
        self._assert_confirmation_unlinked(1)

    def test_explicit_foreign_attempt_reference_remains_blocked(self):
        from job_search.contracts import MutationContext
        other = start(self.ledger,"job-other","start-other")["application"]["application_id"]
        self.browser_attempt("foreign",other)
        self.ledger.record_event(EventInput(self.app,ApplicationEventType.SUBMISSION_CONFIRMED,stamp(),{},"foreign-confirm",
            MutationContext("foreign-confirm","user","dashboard","foreign")))
        report,_ = self.convert()
        self.assertFalse(report["ready_for_review"])
        self.assertIn("conflicting_submission_attempt_identity",{issue["code"] for issue in report["issues"]})

    def test_explicit_missing_browser_attempt_reference_remains_blocked(self):
        from job_search.contracts import MutationContext
        self.ledger.record_event(EventInput(self.app,ApplicationEventType.SUBMISSION_OBSERVED,stamp(),{},"missing-attempt",
            MutationContext("missing-attempt","system","browser_extension","missing")))
        report,_ = self.convert()
        self.assertFalse(report["ready_for_review"])
        self.assertIn("missing_submission_attempt_identity",{issue["code"] for issue in report["issues"]})

    def repaired_confirmation(self, *, target_phase="active"):
        evidence,message = self.mail()
        independent = self.event(ApplicationEventType.SUBMISSION_CONFIRMED,key="independent")
        target = start(self.ledger,"job-correct","start-correct")["application"]["application_id"]
        def confirm(app_id,key):
            proposal = self.ledger.create_event_proposal(EventProposalInput(evidence_id=evidence["evidence_id"],
                proposed_application_id=app_id,event_type=ApplicationEventType.SUBMISSION_CONFIRMED,
                producer_kind=ProducerKind.RULE,producer_version="fixture",confidence=1,candidate_application_ids=[app_id],
                evidence_quote="Please reply",span_start=0,span_end=12,payload={},dedupe_key=key),context(key,"system","outlook_sync"))["proposal"]
            self.ledger.decide_event_proposal(proposal["proposal_id"],"accepted",app_id,"Reviewed",context(key+"-accept"))
            with connect(self.path) as con:
                return con.execute("SELECT applied_event_id FROM event_proposals WHERE proposal_id=?",(proposal["proposal_id"],)).fetchone()[0]
        old = confirm(self.app,"wrong-confirmation")
        proposal = self.ledger.lifecycle.propose_correction(self.app,"association",{
            "evidence_id":evidence["evidence_id"],"target_application_id":target,"target_phase":target_phase,"reason":"Receipt names the other job"},
            context("repair-proposal","hermes"))["proposal"]
        self.ledger.lifecycle.decide_correction(proposal["proposal_id"],"accepted",context("repair-accept","user","authorized_association_repair"))
        new = confirm(target,"right-confirmation")
        return evidence,message,target,old,new,independent["event"]["event_id"]

    def test_proven_historical_reassociation_retracts_only_wrong_support(self):
        evidence,message,target,old,new,independent = self.repaired_confirmation()
        report,runtime = self.convert()
        self.assertTrue(report["ready_for_review"],report["issues"])
        source = runtime.queries.workspace(self.app)
        self.assertEqual(source["progress"]["stage"],"active")
        rows = {row["id"]:row for row in source["records"]["submissions"]["items"]}
        self.assertEqual(rows[old]["status"],"retracted")
        self.assertEqual(rows[old]["evidence"],[])
        self.assertEqual(rows[old]["superseded_evidence"][0]["legacy_evidence_id"],evidence["evidence_id"])
        self.assertEqual(rows[old]["correction_provenance"]["target_confirmation_event_ids"],[new])
        self.assertEqual(rows[independent]["status"],"confirmed")
        target_view = runtime.queries.workspace(target)
        self.assertEqual(target_view["progress"]["stage"],"active")
        self.assertEqual(target_view["conversation"]["items"][0]["id"],message["observation_id"])
        self.assertEqual(source["conversation"]["items"],[])
        archived = historical_rows(self.destination,"application_events")["items"]
        self.assertIn(old,{row["event_id"] for row in archived})
        self.assertTrue(report["validation"]["archive_unchanged"])

    def test_unproven_reassociation_keeps_manual_review_blocker(self):
        evidence,message,target,old,new,independent = self.repaired_confirmation()
        with connect(self.path) as con:
            con.execute("UPDATE lifecycle_mail_links SET source='suggested' WHERE observation_id=?",(message["observation_id"],))
        report,runtime = self.convert()
        self.assertFalse(report["ready_for_review"])
        self.assertIn("manual_correction_requires_semantic_review",{issue["code"] for issue in report["issues"]})
        rows = runtime.queries.workspace(self.app)["records"]["submissions"]["items"]
        self.assertEqual(next(row for row in rows if row["id"] == old)["status"],"confirmed")

    def test_reassociation_without_accepted_target_confirmation_stays_blocked(self):
        evidence,message,target,old,new,independent = self.repaired_confirmation()
        with connect(self.path) as con:
            con.execute("UPDATE event_proposals SET status='rejected' WHERE applied_event_id=?",(new,))
        report,_ = self.convert()
        self.assertFalse(report["ready_for_review"])
        self.assertIn("manual_correction_requires_semantic_review",{issue["code"] for issue in report["issues"]})

    def test_reassociation_with_conflicting_current_links_stays_blocked(self):
        evidence,message,target,old,new,independent = self.repaired_confirmation()
        with connect(self.path) as con:
            con.execute("INSERT INTO lifecycle_mail_links VALUES(?,?,1,'reviewed_correction',?)",(message["observation_id"],self.app,stamp()))
        report,_ = self.convert()
        self.assertFalse(report["ready_for_review"])
        self.assertIn("manual_correction_requires_semantic_review",{issue["code"] for issue in report["issues"]})
        self.assertIn("ambiguous_message_association",{issue["code"] for issue in report["issues"]})

    def test_other_correction_phase_still_requires_semantic_review(self):
        self.repaired_confirmation(target_phase="preparing")
        report,_ = self.convert()
        self.assertFalse(report["ready_for_review"])
        self.assertIn("manual_correction_requires_semantic_review",{issue["code"] for issue in report["issues"]})

    def test_repair_does_not_retract_confirmation_with_conflicting_support(self):
        evidence,message,target,old,new,independent = self.repaired_confirmation()
        other = self.ledger.record_mail_evidence({"account_id":"account","immutable_message_id":"other-message",
            "sender":"recruiter@example.test","subject":"Another receipt","received_at":stamp(),
            "body_sha256":"a"*64,"excerpt":"Other receipt"},context("other-evidence","system","outlook_sync"))["evidence"]
        with connect(self.path) as con:
            original = dict(con.execute("SELECT * FROM event_proposals WHERE applied_event_id=?",(old,)).fetchone())
            original.update(proposal_id="conflicting-proposal",dedupe_key="conflicting-support",evidence_id=other["evidence_id"])
            con.execute("INSERT INTO event_proposals("+",".join(original)+") VALUES("+",".join("?" for _ in original)+")",tuple(original.values()))
        report,runtime = self.convert()
        self.assertFalse(report["ready_for_review"])
        self.assertIn("conflicting_event_evidence_identity",{issue["code"] for issue in report["issues"]})
        rows = runtime.queries.workspace(self.app)["records"]["submissions"]["items"]
        self.assertEqual(next(row for row in rows if row["id"] == old)["status"],"confirmed")

    def test_multiple_source_corrections_require_semantic_replay(self):
        evidence,message,target,old,new,independent = self.repaired_confirmation()
        self.event(ApplicationEventType.MANUAL_CORRECTION,{"reason":"Another historical correction","target_phase":"active"},key="second-correction")
        report,runtime = self.convert()
        self.assertFalse(report["ready_for_review"])
        issues = [issue for issue in report["issues"] if issue["code"] == "manual_correction_requires_semantic_review"]
        self.assertEqual(len(issues),2)
        rows = runtime.queries.workspace(self.app)["records"]["submissions"]["items"]
        self.assertEqual(next(row for row in rows if row["id"] == old)["status"],"confirmed")

    def test_no_overwrite_no_unbounded_history_no_source_mutation(self):
        with connect(self.path) as con:
            before = [dict(row) for row in con.execute("SELECT * FROM applications")]
        report, _ = self.convert()
        with connect(self.path) as con:
            self.assertEqual([dict(row) for row in con.execute("SELECT * FROM applications")], before)
        with self.assertRaises(DomainError):
            convert_snapshot(self.path, self.destination)
        with self.assertRaises(DomainError):
            convert_snapshot(self.path, self.path.parent)
        with self.assertRaises(DomainError):
            historical_rows(self.destination, "applications; DROP TABLE applications")
        with self.assertRaises(DomainError):
            historical_rows(self.destination, "applications", limit=201)
        page = historical_rows(self.destination, "schema_migrations", limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["next_cursor"], 1)
        self.assertFalse(report["activation_authorized"])


if __name__ == "__main__":
    unittest.main()
