"""Production dashboard ownership, compatibility consumers, and resume serialization."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from types import SimpleNamespace

from job_search.application_gateway import ApplicationGateway
from job_search.application_runtime import ApplicationRuntime
from job_search.commands import DomainError
from job_search.contracts import JobSnapshot, MutationContext, RecommendationProvenance, utc_now
from job_search.dashboard import DashboardController, make_server
from job_search.service import JobSearchLedger
from tests.test_job_search_dashboard import FakePreferences
from tests.test_browser_tracking import Catalog, URL, observation
from tests.test_job_search_autofill import EXTENSION_ORIGIN


class ApplicationGatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = JobSearchLedger(self.root / "operations.db")
        self.runtime = ApplicationRuntime(self.root / "owners.db")
        self.gateway = ApplicationGateway(self.runtime, self.ledger, catalog=Catalog())
        self.controller = DashboardController(self.ledger, FakePreferences(), jobs=Catalog(), application_gateway=self.gateway)
        self.server = make_server(self.controller, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        status, headers, session = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        self.assertEqual(session["application_backend"], "owners")
        self.csrf = session["csrf_token"]
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def request(self, method, path, body=None, *, trusted=False, extra=None):
        headers = {"Content-Type": "application/json"}
        if trusted:
            headers.update({"Origin": self.origin, "Cookie": self.cookie, "X-CSRF-Token": self.csrf, "Idempotency-Key": uuid.uuid4().hex})
        headers.update(extra or {})
        con = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        con.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = con.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), json.loads(data) if "application/json" in response.getheader("Content-Type", "") else data
        con.close()
        return result

    def save(self):
        return self.gateway.start_application(JobSnapshot(ats="greenhouse", job_id="12345", family_id="", company_slug="acme", title="Engineer", employer="Acme", job_url=URL), RecommendationProvenance(), MutationContext("save", "user", "dashboard", "owner"))["application"]

    def test_real_dashboard_commands_keep_auth_and_never_touch_legacy_lifecycle(self):
        path = "/api/v1/application-commands/save_job"
        payload = {"job_source": {"source": "greenhouse", "source_id": "12345"}}
        self.assertEqual(self.request("POST", path, payload)[0], 403)
        status, _, result = self.request("POST", path, payload, trusted=True)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.ledger.list_applications(), [])
        status, _, value = self.request("GET", "/api/v1/application-owner/workspace?application_id=" + result["id"])
        self.assertEqual(status, 200, value)
        self.assertEqual(value["application"]["id"], result["id"])
        status, headers, _ = self.request("GET", "/applications")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/#applications")
        for legacy in ("/api/v1/mail-review/resolve", "/api/v1/mail-analyses/old/decisions", "/api/v1/proposals/old/decision", "/api/v1/chief/actions/approve"):
            status, _, error = self.request("POST", legacy, {}, trusted=True)
            self.assertEqual(status, 400, (legacy, error))
        status, _, result = self.request("POST", path, {**payload, "actor_kind": "human"}, trusted=True)
        self.assertEqual(status, 403, result)

    def test_shortlist_application_resolution_and_submission_snapshot_are_owned(self):
        app = self.save()
        self.assertEqual(self.gateway.lookup_job("greenhouse", "12345")["application_id"], app["application_id"])
        self.assertEqual(self.gateway.application_keys(), [])
        self.assertIs(self.controller.curated.application_gateway, self.gateway)
        self.assertIs(self.controller.job_reviews.application_gateway, self.gateway)
        snapshot = {"decision": "selected", "artifact_id": "exact-artifact", "sha256": "a" * 64}
        context = MutationContext("submit", "user", "dashboard", "owner")
        result = self.gateway.record_submission(app["application_id"], utc_now(), context, {"resume": snapshot})
        self.assertEqual(result["application"]["current_phase"], "awaiting_confirmation")
        self.assertEqual(self.gateway.get_application_timeline(app["application_id"])["events"][0]["payload"]["resume"], snapshot)
        self.assertEqual(self.ledger.list_applications(), [])

    def test_application_list_has_reachable_pages_with_bound_cursors(self):
        from urllib.parse import urlencode
        expected = {self.save()["application_id"]}
        for index in range(3):
            expected.add(self.gateway.command("save_job", {"job_source": {"source": "fixture", "source_id": str(index)}},
                "list:" + str(index), "owner")["id"])
        status, _, first = self.request("GET", "/api/v1/applications?limit=2")
        self.assertEqual(status, 200, first)
        self.assertTrue(first["truncated"])
        status, _, last = self.request("GET", "/api/v1/applications?" + urlencode({"limit": 2, "cursor": first["next_cursor"]}))
        self.assertEqual(status, 200, last)
        self.assertFalse(last["truncated"])
        self.assertEqual({a["application_id"] for a in first["applications"] + last["applications"]}, expected)
        self.assertEqual(self.request("GET", "/api/v1/application-owner/review?" + urlencode({
            "group": "proposals", "cursor": first["next_cursor"]}))[0], 400)

    def test_owner_dashboard_enriches_dates_without_replacing_recorded_identity(self):
        app = self.save()
        self.gateway.record_submission(app["application_id"], "2026-10-01T12:00:00Z",
            MutationContext("dated-submit", "user", "dashboard", "owner"), {})
        calls = []
        def posting_summaries(identities):
            calls.append(identities)
            return {("greenhouse", "12345"): {"ats": "greenhouse", "posted_at": "2026-09-15T12:00:00Z",
                "title": "Changed catalog title", "company": "Changed catalog employer"}}
        self.controller.jobs.posting_summaries = posting_summaries
        status, _, page = self.request("GET", "/api/v1/applications?limit=1")
        self.assertEqual(status, 200)
        row = page["applications"][0]
        self.assertEqual(row["job_posting"]["posted_at"], "2026-09-15T12:00:00Z")
        self.assertEqual(row["submitted_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(row["title_snapshot"], "Engineer")
        self.assertEqual(row["employer_snapshot"], "Acme")
        self.assertEqual(calls, [[("greenhouse", "12345")]])
        status, _, workspace = self.request("GET", "/api/v1/applications/" + app["application_id"] + "/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(workspace["application"]["job_posting"], row["job_posting"])
        self.controller.jobs.posting_summaries = lambda identities: {}
        status, _, page = self.request("GET", "/api/v1/applications")
        self.assertIsNone(page["applications"][0]["job_posting"]["posted_at"])

    def test_compatible_workspace_keeps_exact_unreviewed_answer_captures(self):
        from job_search.commands import CommandContext, Principal
        app = self.save()["application_id"]
        worker = Principal("converter", "worker", {"import_snapshot"})
        historical = {"capture_id": "legacy-capture", "captured_at": "2026-10-01T00:00:00Z",
                      "snapshot": {"fields": [{"field_key": "work_authorized", "value": False}]}}
        value = {"id": "old-attempt", "application_id": app, "status": "unreviewed",
                 "answers": {}, "documents": [], "evidence": [], "captured_answer_snapshots": [historical]}
        self.runtime.executor.run(CommandContext(worker, "capture-import", "migration"), "import_snapshot", value,
            lambda tx: self.runtime.applications.import_record(tx, "submissions", value))
        exact = [False, "  original e\u0301  ", {"multi": ["one", "two"]}]
        observation = self.gateway.command("record_browser_observation", {"application_id": app,
            "device_id": "device", "observation_id": "capture", "attempt_ref": "attempt", "activity": "answer_capture",
            "source": {"answer_snapshot": exact}}, "capture", "owner")
        workspace = self.gateway.application_workspace(app)
        captures = {row["capture_id"]: row for row in workspace["answer_snapshots"]}
        self.assertEqual(captures["legacy-capture"]["snapshot"], historical["snapshot"])
        current = next(row for row in captures.values() if row["capture_id"] != "legacy-capture")
        self.assertEqual(workspace["answer_snapshots"][0]["capture_id"], current["capture_id"])
        self.assertEqual(current["snapshot"], exact)
        self.assertEqual(current["review_status"], "unreviewed")
        self.assertIsNone(current["submission_id"])
        self.assertIsNone(workspace["application"]["submitted_at"])
        self.assertEqual(workspace["events"], [])
        self.assertEqual(self.ledger.list_applications(), [])

    def test_http_workspace_continuation_and_installation_action_list(self):
        from urllib.parse import urlencode
        app = self.save()["application_id"]
        tasks = set()
        for i in range(26):
            tasks.add(self.gateway.command("create_task", {"application_id": app, "kind": "other", "description": str(i)}, "task:" + str(i), "owner")["id"])
        status, _, view = self.request("GET", "/api/v1/application-owner/workspace?application_id=" + app)
        self.assertEqual(status, 200, view)
        first = view["records"]["tasks"]
        self.assertEqual(len(first["items"]), 25)
        query = urlencode({"application_id": app, "group": "tasks", "cursor": first["next_cursor"]})
        status, _, last = self.request("GET", "/api/v1/application-owner/workspace-page?" + query)
        self.assertEqual(status, 200, last)
        self.assertIsNone(last["next_cursor"])
        self.assertEqual({t["id"] for t in first["items"] + last["items"]}, tasks)
        status, _, actions = self.request("GET", "/api/v1/actions")
        self.assertEqual(status, 200, actions)
        self.assertEqual(actions["actions"], [])

    def test_unlinked_confirmation_preserves_receipt_without_inventing_submission_or_resume(self):
        from job_search.commands import CommandContext, Principal
        from job_search.resume_lab.agent_context import application_resume_content
        from unittest.mock import Mock
        app = self.save()["application_id"]
        worker = Principal("converter", "worker", {"import_snapshot"})
        stamp = "2026-10-01T12:00:00Z"
        def imported(identity, status, **fields):
            value = {"id": identity, "application_id": app, "status": status, "answers": {}, "documents": [], "evidence": [], **fields}
            return self.runtime.executor.run(CommandContext(worker, identity, "migration"), "import_snapshot", value,
                lambda tx: self.runtime.applications.import_record(tx, "submissions", value))
        for i in range(26):
            imported("attempt:" + str(i).zfill(2), "unreviewed", occurred_at="2026-09-29T10:00:00Z",
                     answers={"private_answer": "Preserved only"}, documents=[{"decision": "selected", "artifact_id": "unverified-resume"}])
        imported("zz-confirmation", "confirmed", occurred_at=stamp, click_time_known=False,
                 attempt_link={"status": "unresolved", "candidate_attempt_ids": ["attempt:00", "attempt:01"]})
        timeline = self.gateway.get_application_timeline(app)
        self.assertEqual(timeline["application"]["current_phase"], "active")
        self.assertIsNone(timeline["application"]["submitted_at"])
        self.assertEqual(timeline["application"]["confirmed_at"], stamp)
        self.assertEqual(len(timeline["events"]), 1)
        self.assertEqual(timeline["events"][0]["payload"]["resume"], {})
        resume_service = Mock()
        resume = application_resume_content(SimpleNamespace(application_db=self.ledger.store.db_path, application_gateway=self.gateway, service=resume_service), app)
        self.assertFalse(resume["available"])
        self.assertEqual(resume["reason"], "submission_resume_not_recorded")
        self.assertEqual(resume_service.mock_calls, [])
        status, _, workspace = self.request("GET", "/api/v1/application-owner/workspace?application_id=" + app)
        self.assertEqual(status, 200, workspace)
        self.assertNotIn("zz-confirmation", [s["id"] for s in workspace["records"]["submissions"]["items"]])
        self.assertEqual(workspace["application"]["submission_summary"]["warnings"][0]["code"], "unresolved_submission_attempt")
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.executor.pending_work(con, owner="applications"), [])
        known = "2026-09-30T10:00:00Z"
        document = {"decision": "not_tracked", "reason": "Explicitly recorded by the user"}
        imported("known-attempt", "attempted", occurred_at=known, click_time_known=True, documents=[document],
                 attempt_link={"status": "unresolved", "candidate_attempt_ids": ["attempt:00", "attempt:01"]})
        updated = self.gateway.get_application_timeline(app)["application"]
        self.assertEqual(updated["submitted_at"], known)
        self.assertEqual(updated["confirmed_at"], stamp)
        self.assertEqual(updated["submission_summary"]["unresolved_attempts"], 2)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.applications.get_record(con, "submissions", "known-attempt")["documents"], [document])
        self.assertNotIn("submission time", updated["submission_summary"]["warnings"][0]["message"])

    def test_message_body_requires_current_association_before_calling_archive_reader(self):
        from unittest.mock import Mock
        from job_search.commands import CommandContext, Principal
        app = self.save()["application_id"]
        other = self.gateway.command("save_job", {"job_source": {"source": "test", "source_id": "other"}}, "other", "owner")["id"]
        human = Principal("owner", "human", frozenset({"*"}))
        message = self.runtime.executor.run(CommandContext(human, "message"), "record_message", {},
            lambda tx: self.runtime.correspondence.record_message(tx, account_id="private-account", provider_message_id="private-provider-id",
                source_version="revision-token", direction="incoming", authored_text="Preserved body", archive_ref="opaque-archive"))
        reader = Mock()
        reader.read_message.return_value = {"message_id": message["id"], "revision": message["revision"],
            "text": "Preserved body", "coverage": {"complete": True}}
        self.runtime.mail_reader = reader
        path = "/api/v1/applications/" + app + "/conversation/" + message["id"]
        self.assertEqual(self.request("GET", path)[0], 404)
        reader.read_message.assert_not_called()
        association = self.runtime.executor.run(CommandContext(human, "link"), "link_message", {},
            lambda tx: self.runtime.correspondence.link_message(tx, message_id=message["id"], application_id=app))
        status, _, body = self.request("GET", path)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["excerpt"], "Preserved body")
        self.assertTrue(body["available"])
        self.assertEqual(body["coverage"], {"complete": True})
        reader.read_message.assert_called_once_with(app, message["id"])
        self.assertNotIn("private-account", json.dumps(body))
        self.assertNotIn("private-provider-id", json.dumps(body))
        self.runtime.executor.run(CommandContext(human, "correct"), "correct_association", {},
            lambda tx: self.runtime.correspondence.correct_association(tx, association_id=association["id"], application_id=other,
                expected_version=1, reason="Reviewed correction"))
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(reader.read_message.call_count, 1)
        reader.read_message.return_value = {"message_id": message["id"], "revision": message["revision"],
            "text": None, "coverage": {"complete": False, "reason": "archive_missing"}}
        status, _, body = self.request("GET", path.replace(app, other))
        self.assertEqual(status, 200, body)
        self.assertFalse(body["available"])
        self.assertEqual(body["excerpt"], "")
        self.assertEqual(body["coverage"]["reason"], "archive_missing")
        del self.runtime.mail_reader
        status, _, body = self.request("GET", path.replace(app, other))
        self.assertEqual(status, 200, body)
        self.assertFalse(body["available"])
        self.assertEqual(body["reason"], "archive_content_requires_authorized_reader")

    def test_recorded_job_snapshot_is_immutable_and_old_schema_upgrade_preserves_identity(self):
        from job_search.applications.api import ApplicationOperations, SCHEMA, SCHEMA_MIGRATIONS
        from job_search.applications._store import _PRE_SNAPSHOT_SCHEMA
        from job_search.commands import CommandExecutor, CommandContext, Principal
        old = CommandExecutor(self.root / "older-owner.db", {"applications": _PRE_SNAPSHOT_SCHEMA})
        api = ApplicationOperations()
        context = CommandContext(Principal("owner", "human", frozenset({"*"})), "legacy-owner-save")
        payload = {"job_source": {"source": "greenhouse", "source_id": "12345"}}
        original = old.run(context, "save_job", payload, lambda tx: api.save_job(tx, payload))
        upgraded = CommandExecutor(old.path, {"applications": SCHEMA}, schema_migrations=SCHEMA_MIGRATIONS)
        with upgraded.read() as con:
            self.assertEqual(api.get_application(con, original["id"]), original)
            self.assertIsNone(api.get_job(con, original["job_id"])["recorded_snapshot"])
        app = self.save()
        self.assertEqual(app["job_url_snapshot"], URL)
        self.assertEqual(app["job_metadata_source"], "recorded_snapshot")
        self.gateway.start_application(JobSnapshot(ats="greenhouse", job_id="12345", family_id="", company_slug="changed", title="Changed", employer="New", job_url="https://example.invalid/new"), RecommendationProvenance(), MutationContext("save-again", "user", "dashboard", "owner"))
        app = self.gateway.lookup_job("greenhouse", "12345")
        self.assertEqual(app["company_slug_snapshot"], "acme")
        self.assertEqual(app["title_snapshot"], "Engineer")

    def test_conversion_keeps_original_job_snapshot_and_configured_gateway_requires_binding(self):
        from job_search.application_gateway import build_application_gateway
        from job_search.application_installation import freeze_legacy
        from job_search.application_migration import convert_snapshot
        original = self.ledger.start_application(JobSnapshot(ats="greenhouse", job_id="12345", family_id="family", company_slug="original-board", title="Original role", employer="Original employer", job_url=URL), RecommendationProvenance(session_id="original-review", policy_id="reviewed"), MutationContext("old-save", "user", "dashboard", "owner"))["application"]
        report = convert_snapshot(self.ledger.store.db_path, self.root / "converted")
        self.assertTrue(report["ready_for_review"], report["issues"])
        converted = ApplicationRuntime(Path(report["candidate_path"]))
        config = SimpleNamespace(application_backend="owners", application_db=self.ledger.store.db_path,
            application_owner_db=converted.executor.path, jobs_db=self.root / "catalog-not-required.db")
        with self.assertRaises(DomainError):
            build_application_gateway(config, self.ledger)
        freeze_legacy(self.ledger.store.db_path, converted, operator="offline-test", report=report)
        gateway = build_application_gateway(config, self.ledger)
        app = gateway.get_application_timeline(original["application_id"])["application"]
        self.assertEqual(app["company_slug_snapshot"], "original-board")
        self.assertEqual(app["title_snapshot"], "Original role")
        self.assertEqual(app["job_url_snapshot"], URL)
        with converted.executor.read() as con:
            job = converted.applications.get_job(con, app["owner_job_id"])
            self.assertEqual(job["recorded_snapshots"][0]["recommendation"]["session_id"], "original-review")
        with self.assertRaises(Exception):
            self.ledger.record_submission(original["application_id"], utc_now(), MutationContext("old-submit", "user", "dashboard"))

    def test_resume_selection_lock_serializes_submission_snapshot_without_freezing_open_application(self):
        from job_search.resume_lab.gateway import ResumeLabProductionGateway
        from job_search.resume_lab.contracts import JobSnapshot as ResumeJob, ResumeConflictError
        app = self.save()
        resume = ResumeLabProductionGateway(None, None, application_db=self.ledger.store.db_path, application_gateway=self.gateway)
        entered, release, submit_started = threading.Event(), threading.Event(), threading.Event()
        selection = {"artifact_id": "old"}
        job = ResumeJob(ats="greenhouse", job_id="12345", title="Engineer", description="Job", employer="Acme")
        def select():
            def change(con):
                entered.set()
                self.assertTrue(release.wait(5))
                selection["artifact_id"] = "new"
                return True
            return resume._while_application_preparing(app["application_id"], job, change)
        def submit():
            submit_started.set()
            return self.gateway.record_submission(app["application_id"], utc_now(), MutationContext("race-submit", "user", "dashboard", "owner"),
                payload_factory=lambda: {"resume": {"decision": "selected", **selection}}, request_payload={"resume_decision": "selected"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            chosen = pool.submit(select)
            self.assertTrue(entered.wait(5))
            submitted = pool.submit(submit)
            self.assertTrue(submit_started.wait(5))
            self.assertFalse(submitted.done())
            release.set()
            self.assertTrue(chosen.result(timeout=5))
            result = submitted.result(timeout=5)
        self.assertEqual(result["submission"]["documents"][0]["artifact_id"], "new")
        self.assertTrue(resume._application_authority(app["application_id"])["selection_editable"])
        resume._while_application_preparing(app["application_id"], job, lambda _: selection.update(artifact_id="later"))
        second = self.gateway.record_submission(app["application_id"], utc_now(), MutationContext("second-submit", "user", "dashboard", "owner"),
            payload_factory=lambda: {"resume": {"decision": "selected", **selection}}, request_payload={"resume_decision": "selected"})
        self.assertEqual(second["submission"]["documents"][0]["artifact_id"], "later")
        with self.runtime.executor.read() as con:
            original = self.runtime.applications.get_record(con, "submissions", result["submission"]["id"])
            preview = self.runtime.applications.preview_closure(con, app["application_id"])
        self.assertEqual(original["documents"][0]["artifact_id"], "new")
        self.gateway.command("close_application", {"application_id": app["application_id"], "expected_version": preview["expected_version"],
            "expected_records": preview["expected_records"], "outcome": "stopped_pursuing", "reason": "Stopped"}, "close", "owner")
        self.assertFalse(resume._application_authority(app["application_id"])["selection_editable"])
        with self.assertRaises(ResumeConflictError):
            resume._while_application_preparing(app["application_id"], job, lambda _: self.fail("Edit after closure"))

    def test_owner_interview_preparation_exposes_selected_resume_without_inventing_submission(self):
        from tests.test_career_resume import gateway_at, JOB, Context
        resume, _ = gateway_at(self.root)
        resume.application_gateway = self.gateway
        app = self.gateway.command("save_job", {"job_source": {"source": JOB["ats"], "source_id": JOB["id"]}}, "resume-job", "owner")["id"]
        self.gateway.command("record_progress", {"application_id": app, "kind": "interview_request"}, "resume-interview", "owner")
        prepared = resume.prepare(JOB, application_id=app, idempotency_key="owner-prepare")
        self.assertEqual(resume.handle_work({"run_id": prepared["run_id"]}, Context())["status"], "succeeded")
        candidate = resume.get_run_result(prepared["run_id"])["comparisons"][0]
        resume.approve_run(prepared["run_id"], comparison_kind="grounded_rewrite", idempotency_key="owner-approve")
        resume.select_resume(app, job=JOB, artifact_id=candidate["artifact_id"], evaluation_id=candidate["evaluation_id"], idempotency_key="owner-select")
        content = resume.get_application_resume_content(app)
        self.assertTrue(content["available"], content)
        self.assertEqual(content["provenance"]["binding"], "selected")
        self.assertEqual(self.gateway.get_application_timeline(app)["events"], [])

    def test_health_uses_owner_state_without_frozen_legacy_reducers_or_queues(self):
        from unittest.mock import patch
        app = self.save()["application_id"]
        self.gateway.command("create_reminder", {"application_id": app, "at": "2030-01-01T12:00:00Z", "description": "Prepare for interview"}, "reminder", "owner")
        with patch.object(self.ledger, "system_health", side_effect=AssertionError("Retired reducer must not run")):
            status, _, health = self.request("GET", "/api/v1/health")
            self.assertEqual(status, 200, health)
            self.assertEqual(health["status"], "healthy")
            self.assertEqual(health["database"], str(self.runtime.executor.path))
            self.assertEqual(health["applications"], {"preparing": 1})
            self.assertEqual(health["reminders"]["counts"], {"pending": 1})
            for retired in ("projection_failures", "outbox", "notifications", "connectors", "mail_stage"):
                self.assertNotIn(retired, health)
            self.assertEqual(self.controller.ops_view()["status"], "healthy")
        from job_search.application_agent_host import ProductionAgentTools
        from unittest.mock import Mock
        host = object.__new__(ProductionAgentTools)
        host.runtime, host.sources = self.runtime, SimpleNamespace(ledger=self.ledger)
        host.existing = Mock()
        host.existing.invoke.side_effect = AssertionError("Retired agent health must not run")
        self.assertEqual(host.invoke("system_health")["application_backend"], "owners")
        host.existing.invoke.assert_not_called()
        from job_search.hermes import HermesValidationError
        with self.assertRaises(HermesValidationError):
            host.invoke("system_health", {"legacy": True})
        reminder = self.controller.reminder_summaries()[0]
        self.assertEqual(reminder["note"], "Prepare for interview")
        self.assertEqual(reminder["due_at"], "2030-01-01T12:00:00Z")
        self.assertTrue(reminder["reminder_id"])

    def test_health_reports_uncertain_imports_without_current_action_proposals(self):
        from job_search.commands import CommandContext, Principal
        app = self.save()["application_id"]
        imported = {"application_id": app, "execution": "uncertain", "provider_request_id": "historical-request"}
        self.runtime.executor.run(CommandContext(Principal("converter", "worker", {"import_snapshot"}), "history", "migration"),
            "import_snapshot", imported, lambda tx: self.runtime.actions.import_history(tx, "old-action", imported))
        health = self.gateway.system_health()
        self.assertEqual(health["actions"]["counts"], {})
        self.assertEqual(health["actions"]["readiness"]["historical_uncertain"], 1)
        self.assertEqual(health["status"], "attention")
        self.assertNotIn("historical-request", json.dumps(health))

    def test_health_reports_required_restore_review_even_without_quarantined_work(self):
        from tests.test_redesign_restore_execution import mark_restored
        mark_restored(self.runtime.executor.path, utc_now(), [])
        health = self.gateway.system_health()
        self.assertEqual(health["status"], "attention")
        self.assertTrue(health["application_restore"]["required"])
        self.runtime.executor.acknowledge_restore(expected_restore_revision=health["application_restore"]["revision"],
            operator="owner", reason="Reviewed empty restore")
        self.assertEqual(self.gateway.system_health()["status"], "healthy")

    def test_health_keeps_restored_action_attention_after_restore_review(self):
        from job_search.commands import CommandContext, Principal
        from tests.test_redesign_restore_execution import mark_restored
        app = self.save()["application_id"]
        envelope = {"kind": "send_reply", "account_id": "fixture", "application_id": app, "pursuit_no": 1,
            "target": {"message_id": "fixture-message", "provider_message_id": "private-provider-id", "source_hash": "a" * 64, "provider_source_hash": "b" * 64},
            "payload": {"recipients": ["person@example.test"], "subject": "Re: Role", "body": "Exact reply"},
            "context_versions": {"application:" + app: 1}}
        action = self.runtime.executor.run(CommandContext(Principal("owner", "human", {"*"}), "prepare-history"),
            "prepare_reply", envelope, lambda tx: self.runtime.actions.prepare_reply(tx, envelope))
        self.runtime.executor.run(CommandContext(Principal("owner", "human", {"*"}), "approve-history"),
            "authorize_action", {}, lambda tx: self.runtime.actions.authorize_action(tx, action["action_id"], action["digest"], applicability=lambda tx, env: True))
        mark_restored(self.runtime.executor.path, utc_now(), [("external_action", action["action_id"])])
        restored = self.runtime.executor.restore_status()
        self.runtime.executor.acknowledge_restore(expected_restore_revision=restored["revision"], operator="owner", reason="Reviewed restored history")
        health = self.gateway.system_health()
        self.assertFalse(health["application_restore"]["required"])
        self.assertEqual(health["actions"]["readiness"]["restore_quarantined_actions"], 1)
        self.assertEqual(health["status"], "attention")
        self.assertNotIn("private-provider-id", json.dumps(health))

    def test_health_keeps_restored_reminder_attention_after_restore_review(self):
        from tests.test_redesign_restore_execution import mark_restored
        app = self.save()["application_id"]
        reminder = self.gateway.command("create_reminder", {"application_id": app, "at": "2030-01-01T12:00:00Z", "description": "Future notification"}, "restored-reminder", "owner")
        mark_restored(self.runtime.executor.path, utc_now(), [("reminder", reminder["id"])])
        restored = self.runtime.executor.restore_status()
        self.runtime.executor.acknowledge_restore(expected_restore_revision=restored["revision"], operator="owner", reason="Reviewed notification uncertainty")
        health = self.gateway.system_health()
        self.assertFalse(health["application_restore"]["required"])
        self.assertEqual(health["actions"]["readiness"]["uncertain"], 0)
        self.assertEqual(health["reminders"]["recovery"]["restore_quarantined_reminders"], 1)
        self.assertEqual(health["status"], "attention")

    def test_owner_shortlist_eligibility_depends_on_submission_not_tracking_or_progress(self):
        app = self.gateway.command("add_note", {"job_source": {"source": "greenhouse", "source_id": "12345"}, "text": "Investigate"}, "note", "owner")["application_id"]
        self.assertEqual(self.gateway.application_keys(), [])
        self.gateway.command("record_progress", {"application_id": app, "kind": "interview_request"}, "contact", "owner")
        self.assertEqual(self.gateway.lookup_job("greenhouse", "12345")["current_phase"], "interviewing")
        self.assertEqual(self.gateway.application_keys(), [])
        self.gateway.record_submission(app, utc_now(), MutationContext("submit", "user", "dashboard"))
        self.assertEqual(self.gateway.application_keys(), [("greenhouse", "12345")])
        with self.runtime.executor.read() as con:
            preview = self.runtime.applications.preview_closure(con, app)
        closed = self.gateway.command("close_application", {"application_id": app, "expected_version": preview["expected_version"],
            "expected_records": preview["expected_records"], "outcome": "stopped_pursuing", "reason": "Stopped"}, "close", "owner")
        self.assertEqual(self.gateway.application_keys(), [("greenhouse", "12345")])
        self.gateway.command("reopen_application", {"application_id": app, "expected_version": closed["version"], "reason": "Try again"}, "reopen", "owner")
        self.assertEqual(self.gateway.application_keys(), [])

    def test_review_publication_consumes_owner_eligibility_and_refreshes_after_submission(self):
        from tests.test_agent_job_reviews import ReviewTests
        fixture = ReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.service.application_gateway = self.gateway
        app = self.gateway.command("add_note", {"job_source": {"source": "ashby", "source_id": "a"}, "text": "Research"}, "note-review", "owner")["application_id"]
        review_id = fixture.start()
        fixture.assess(review_id)
        fixture.assess(review_id, kind="check")
        preview = fixture.command("preview", review_id=review_id)
        self.assertTrue(preview["ready"], preview)
        self.assertEqual(preview["broad_count"], 1)
        self.gateway.record_submission(app, utc_now(), MutationContext("review-submit", "user", "dashboard"))
        updated = fixture.command("preview", review_id=review_id)
        self.assertEqual(updated["broad_count"], 0)
        self.assertEqual(updated["omitted"], [{"ordinal": 1, "reason": "already_applied"}])
        self.assertNotEqual(preview["preview_sha256"], updated["preview_sha256"])
        with self.assertRaisesRegex(Exception, "fresh preview"):
            fixture.command("publish", review_id=review_id, preview_sha256=preview["preview_sha256"])

    def test_accepted_submission_does_not_promote_unreviewed_autofill_captures(self):
        from unittest.mock import Mock
        app = self.save()["application_id"]
        self.gateway.record_submission(app, utc_now(), MutationContext("accepted-submit", "user", "dashboard"))
        vault = Mock()
        vault.pending_captures = {"later-attempt": {"application_id": app, "answers": ["unreviewed fact"]}}
        self.controller.browser_tracking.autofill._vault = vault
        self.controller.browser_tracking.maintain_captures()
        vault.finish_browser_captures.assert_not_called()
        self.assertEqual(vault.pending_captures, {"later-attempt": {"application_id": app, "answers": ["unreviewed fact"]}})

    def test_paired_extension_http_preserves_evidence_without_legacy_submission(self):
        tracker = self.controller.browser_tracking
        pairing = tracker.issue_pairing("local")
        enrollment = tracker.enroll(pairing["pairing_code"], EXTENSION_ORIGIN, "local")
        first = observation()
        body = {**first, "device_token": enrollment["device_token"]}
        status, _, result = self.request("POST", "/api/v1/extension/observations", body, extra={"Origin": EXTENSION_ORIGIN})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.ledger.list_applications(), [])
        self.assertEqual(self.gateway.get_application_timeline(result["application_id"])["application"]["current_phase"], "preparing")
        self.assertEqual(tracker.attempt_status(enrollment["device_id"], first["attempt_id"])["application_id"], result["application_id"])


if __name__ == "__main__":
    unittest.main()
