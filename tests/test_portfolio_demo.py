"""The populated public demo exercises real services with only synthetic state."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.actions import ActionExecutor
from job_search.availability import AvailabilityPlanner
from job_search.contracts import MutationContext
from job_search.curated import CuratedShortlists
from job_search.integration import LedgerProposalSource, LocalJobCatalog
from job_search.outlook.client import GraphOutlookClient
from job_search.outlook.transport import GraphSession
from job_search.ranking.labeler import policy_recommendations
from job_search.service import JobSearchLedger
from scripts.offline_system_demo import FixtureTokens, enqueue, run
from scripts.portfolio_demo import PortfolioGraph, PortfolioPdfExtractor, write_json_atomic


class PortfolioDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portfolio-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        args = SimpleNamespace(state_dir=self.root, serve=False, interactive=False, port=0, scenario="portfolio")
        # Any attempt to load a real runtime file is an isolation regression.
        with patch("job_search.runtime.load_runtime_config", side_effect=AssertionError("must not load private config")), contextlib.redirect_stdout(io.StringIO()):
            self.report = run(args)
        self.ledger = JobSearchLedger(self.root / "applications.db")
        self.catalog = LocalJobCatalog(self.root / "jobs.db")
        self.hero = self.report["hero_application_id"]

    def test_catalog_saved_lists_lifecycle_and_model_modes(self):
        with sqlite3.connect(self.root / "jobs.db") as con:
            self.assertEqual(dict(con.execute("SELECT ats,count(*) FROM jobs GROUP BY ats")), {"ashby": 8, "greenhouse": 8, "lever": 8})
            self.assertEqual(set(row[0] for row in con.execute("SELECT DISTINCT event_type FROM job_posting_events")), {"opened", "modified", "closed", "reopened"})
        curated = CuratedShortlists(self.root / "applications.db", self.catalog)
        lists = curated.lists()["lists"]
        self.assertEqual(len(lists), 3)
        self.assertEqual([row["job_count"] for row in lists], [8, 8, 8])
        self.assertEqual([row["created_at"] for row in lists], sorted([row["created_at"] for row in lists], reverse=True))
        latest = curated.get(self.report["curated_list_id"])
        self.assertEqual([job["rank"] for job in latest["recommendations"]], list(range(1, 9)))
        self.assertEqual(latest["recommendations"][0]["company"], "Northstar Labs")
        self.assertEqual(latest["recommendations"][0]["application_id"], self.hero)
        for policy in ("champion", "selective", "broad", "compare"):
            result = policy_recommendations(self.root / "jobs.db", self.root / "preference.db", self.root / "proxy.db", {"policy": policy, "days": 30, "limit": 8, "remote_only": False, "salary_floor": None, "max_per_company": 2, "max_per_title": 2})
            self.assertTrue(result["model"]["ready"], policy)
            self.assertGreater(len(result["recommendations"]), 0, policy)
            self.assertTrue(all(job["policy_id"] in {"champion", "selective", "broad"} for job in result["recommendations"]))

    def test_application_states_browser_evidence_and_posting_chronology(self):
        applications = self.ledger.list_applications()
        self.assertEqual(Counter(row["current_phase"] for row in applications), {"active": 4, "awaiting_confirmation": 2, "interviewing": 3, "offer": 1, "terminal": 2, "preparing": 1})
        self.assertEqual(len([row for row in applications if row["submitted_at"]]), 12)
        self.assertEqual([row["current_phase"] for row in applications[:3]], ["active", "interviewing", "offer"])
        self.assertEqual({row["terminal_outcome"] for row in applications if row["current_phase"] == "terminal"}, {"rejected", "withdrawn"})
        hero = self.ledger.get_application_timeline(self.hero)
        self.assertEqual(sum(event["event_type"] == "submission_observed" for event in hero["events"]), 1)
        self.assertEqual(sum(event["event_type"] == "submission_confirmed" for event in hero["events"]), 1)
        self.assertEqual(hero["application"]["current_phase"], "active")
        self.assertEqual(self.ledger.verify_projections(), [])
        with sqlite3.connect(self.root / "applications.db") as con:
            self.assertEqual(con.execute("SELECT count(*) FROM browser_observations").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT count(*) FROM browser_finalizations").fetchone()[0], 1)
        for app in applications:
            job = self.catalog.get_job(app["ats"], app["job_id"])
            self.assertLessEqual(job["posted_at"], app["started_at"])

    def test_review_decisions_are_real_and_repeatable(self):
        reviews = self.ledger.list_attention_items()
        self.assertEqual(len(reviews), 5)
        uncertain = next(row for row in reviews if row["id"] == self.report["ids"]["uncertain_match_review"])
        self.assertEqual(len(uncertain["candidate_application_ids"]), 2)
        self.assertIsNone(uncertain["application_id"])
        decision = (self.report["ids"]["hero_interview_review"], "accepted", self.hero, "Review verified", MutationContext("portfolio-test-accept", "user", "dashboard"))
        first = self.ledger.decide_event_proposal(*decision)
        self.assertEqual(self.ledger.decide_event_proposal(*decision), first)
        timeline = self.ledger.get_application_timeline(self.hero)
        self.assertEqual(timeline["application"]["current_phase"], "interviewing")
        self.assertEqual(sum(item["event_type"] == "interview_requested" for item in timeline["events"]), 1)
        self.ledger.decide_temporal_proposal(self.report["ids"]["interview_time_review"], "accepted", "Correct time", MutationContext("portfolio-test-time", "user", "dashboard"))
        self.assertTrue(any(item["application_id"] == self.hero for item in self.ledger.list_interview_schedules()))

    def test_real_pdf_documents_and_encrypted_correspondence(self):
        from job_search.resume_lab.artifacts import ResumePdfArtifactRepository
        from job_search.resume_lab.gateway import ResumeLabProductionGateway
        from job_search.resume_lab.service import ResumeLabService
        gateway = ResumeLabProductionGateway(ResumeLabService(self.root / "resume.db"), ResumePdfArtifactRepository(self.root / "artifacts"), application_db=self.root / "applications.db")
        hashes = set()
        for version in self.report["ids"]["resume_versions"]:
            document = gateway.get_standard_document(version)
            parsed = PortfolioPdfExtractor().extract(document.content)
            self.assertIn("Sean", parsed.logical_text)
            self.assertIn("sean@example.test", parsed.logical_text)
            self.assertIn("Juniper Systems", parsed.logical_text)
            self.assertEqual(parsed.pages, 1)
            hashes.add(hashlib.sha256(document.content).hexdigest())
        self.assertEqual(len(hashes), 2)
        timeline = self.ledger.get_application_timeline(self.hero)
        submitted = next(item for item in timeline["events"] if item["event_type"] == "submission_observed")
        payload = submitted.get("payload") or json.loads(submitted["payload_json"])
        self.assertEqual(payload["resume"]["decision"], "selected")
        self.assertIn(gateway.get_artifact(payload["resume"]["artifact_id"]).sha256, hashes)
        mail = self.ledger.list_application_mail(self.hero)
        self.assertEqual(len(mail), 3)
        self.assertTrue(all(item["archive_id"] for item in mail))
        with sqlite3.connect(self.root / "applications.db") as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(mail_archive)")}
            self.assertIn("ciphertext", columns)
            self.assertEqual(con.execute("SELECT count(*) FROM mail_archive").fetchone()[0], 5)

    def test_only_approved_exact_draft_and_private_hold_execute_once(self):
        graph = PortfolioGraph()
        action = self.ledger.get_action(self.report["ids"]["hero_reply_action"])
        graph.messages[action["payload"]["message_id"]] = {"id": action["payload"]["message_id"]}
        outlook = GraphOutlookClient(GraphSession(FixtureTokens(), graph))
        availability = AvailabilityPlanner(outlook)
        executor = ActionExecutor(self.ledger, outlook, availability, account_id="outlook-personal")
        self.assertEqual(executor.execute(action["action_id"]).outcome, "permanent_failure")
        self.assertFalse(graph.drafts)
        self.ledger.decide_action(action["action_id"], True, action["payload_sha256"], MutationContext("portfolio-test-draft", "user", "dashboard"))
        self.assertEqual(executor.execute(action["action_id"]).outcome, "succeeded")
        executor.execute(action["action_id"])
        self.assertEqual(len(graph.drafts), 1)
        self.assertEqual(graph.drafts[0]["body"], action["payload"]["body"])
        hold = LedgerProposalSource(self.ledger, account_id="outlook-personal", availability=availability).propose_interview_slots({"application_id": self.hero, "duration_minutes": 30, "idempotency_key": "portfolio-test-hold"})["action"]
        self.ledger.decide_action(hold["action_id"], True, hold["payload_sha256"], MutationContext("portfolio-test-hold-approve", "user", "dashboard"))
        self.assertEqual(executor.execute(hold["action_id"]).outcome, "succeeded")
        executor.execute(hold["action_id"])
        self.assertEqual(len(graph.holds), 1)
        self.assertEqual(graph.holds[0]["attendees"], [])
        self.assertEqual(graph.holds[0]["sensitivity"], "private")
        self.assertFalse(any(path.endswith("/send") for _, path in graph.requests))

    def test_existing_directory_is_refused(self):
        before = (self.root / "portfolio-receipt.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "empty --state-dir"):
            run(SimpleNamespace(scenario="portfolio", state_dir=self.root, serve=False, interactive=False, port=0))
        self.assertEqual((self.root / "portfolio-receipt.json").read_bytes(), before)

    def test_worker_restart_does_not_duplicate_approved_reply(self):
        from job_search.runtime import RuntimeConfigV1, build_runtime
        from job_search.worker import ApprovedActionTaskHandler
        graph = PortfolioGraph()
        action = self.ledger.get_action(self.report["ids"]["hero_reply_action"])
        graph.messages[action["payload"]["message_id"]] = {"id": action["payload"]["message_id"]}
        outlook = GraphOutlookClient(GraphSession(FixtureTokens(), graph))
        handler = ApprovedActionTaskHandler(self.ledger, ActionExecutor(self.ledger, outlook, AvailabilityPlanner(outlook), account_id="outlook-personal"))
        config = replace(RuntimeConfigV1.defaults(self.root), application_db=self.root / "applications.db", jobs_db=self.root / "jobs.db", preference_db=self.root / "preference.db", proxy_db=self.root / "proxy.db", log_dir=self.root / "logs", mcp_token_file=self.root / "token", hermes_data_dir=self.root / "hermes")
        self.ledger.decide_action(action["action_id"], True, action["payload_sha256"], MutationContext("portfolio-worker-approve", "user", "dashboard"))
        for index in range(2):
            enqueue(config, "outlook.actions.execute", "portfolio-restart-" + str(index))
            worker = build_runtime(config, lane="core", base_environment={}, task_overrides={"outlook.actions.execute": handler})
            worker.tick()
        self.assertEqual(self.ledger.get_action(action["action_id"])["status"], "executed")
        self.assertEqual(len(graph.drafts), 1)
        with sqlite3.connect(self.root / "applications.db") as con:
            self.assertEqual(con.execute("SELECT count(*) FROM work_items WHERE work_id LIKE 'fixture-portfolio-restart-%' AND status='succeeded'").fetchone()[0], 2)

    def test_ambient_private_inference_is_never_loaded_and_environment_is_restored(self):
        private_config = "/private/portfolio-must-not-read-inference.json"
        args = SimpleNamespace(scenario="portfolio", state_dir=Path(self.temp.name) / "ambient", serve=False, interactive=False, port=0)
        with patch.dict(os.environ, {"JOB_SEARCH_INFERENCE_CONFIG": private_config, "OPENROUTER_API_KEY": "fixture-must-not-be-inherited"}), \
                patch("job_search.inference.load_inference_config", side_effect=AssertionError("private inference config read")) as config_loader, \
                patch("job_search.inference.config.load_credential", side_effect=AssertionError("private credential read")) as credential_loader, \
                contextlib.redirect_stdout(io.StringIO()):
            report = run(args)
            config_loader.assert_not_called()
            credential_loader.assert_not_called()
            self.assertEqual(os.environ["JOB_SEARCH_INFERENCE_CONFIG"], private_config)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "fixture-must-not-be-inherited")
        self.assertEqual(report["status"], "ready")
        with sqlite3.connect(args.state_dir / "applications.db") as con:
            snapshot = json.loads(con.execute("SELECT capabilities_json FROM runtime_dependency_snapshot").fetchone()[0])
        self.assertEqual(next(item["status"] for item in snapshot if item["id"] == "inference"), "disabled")

    def test_status_replacement_is_atomic(self):
        path = self.root / "atomic-receipt.json"
        old, new = {"stage": "before"}, {"stage": "after", "records": list(range(100))}
        write_json_atomic(path, old)
        replace_file = os.replace
        def inspect_before_replace(source, destination):
            self.assertEqual(json.loads(path.read_text()), old)
            self.assertEqual(json.loads(Path(source).read_text()), new)
            self.assertEqual(Path(source).parent, path.parent)
            replace_file(source, destination)
        with patch("scripts.portfolio_demo.os.replace", side_effect=inspect_before_replace):
            write_json_atomic(path, new)
        self.assertEqual(json.loads(path.read_text()), new)
        self.assertEqual(list(self.root.glob(".atomic-receipt.json-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
