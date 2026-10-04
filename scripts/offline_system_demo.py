#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=44,<47", "pypdf>=5,<7", "reportlab>=4,<5"]
# ///
"""Repeatable local acceptance flow; external services are fictional fixtures.

Run with --serve to leave the actual dashboard open for inspection. No production
configuration, credentials, mailbox, model provider, or Telegram target is used.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_search.collection.dedupe import prepare_families
from job_search.ranking.labeler import prepare_preferences
from job_search.ranking.model import HashingEncoder, prepare_state
from job_search.actions import ActionExecutor
from job_search.availability import AvailabilityPlanner
from job_search.dashboard import make_server
from job_search.db import connect
from job_search.hermes_delivery import DeliveryReceiptStore, HermesDispatcher, _HermesServer
from job_search.hermes_mcp import MCP_PROTOCOL_VERSION, make_mcp_server_from_sources
from job_search.mail.archive import EncryptedMailArchive
from job_search.mail.archive_source import EncryptedArchiveMailSource
from job_search.mail.secure_ingest import SecureMailIngestor
from job_search.outlook.client import GraphOutlookClient
from job_search.outlook.mail import GraphMailClient
from job_search.outlook.state import SQLiteOutlookState
from job_search.outlook.transport import GraphSession, RawHttpResponse
from job_search.resume_lab.artifacts import ResumePdfArtifactRepository
from job_search.resume_lab.gateway import ResumeLabProductionGateway, build_resume_lab_read_gateway
from job_search.resume_lab.service import ResumeLabService
from job_search.runtime import RuntimeConfigV1, build_runtime
from job_search.service import JobSearchLedger
from job_search.sync import OutlookMailCoordinator
from job_search.system import build_dashboard_controller, build_hermes_sources_from_config
from job_search.worker import ApprovedActionTaskHandler, OutlookMailTaskHandler
from tests.test_career_resume import PROFILE, CareerModel, Toolchain
from scripts.portfolio_demo import write_json_atomic


class DemoPdfToolchain(Toolchain):
    """A valid, visibly fictional PDF fixture; real TeX is an optional mode."""
    def build(self, content, **kwargs):
        from io import BytesIO
        from textwrap import wrap
        from reportlab.pdfgen import canvas
        build = super().build(content, **kwargs)
        output = BytesIO()
        pdf = canvas.Canvas(output, pagesize=(612, 792))
        pdf.setTitle("Sean - demo resume")
        text = pdf.beginText(40, 750)
        text.setFont("Helvetica", 9)
        text.setLeading(12)
        for line in build.rendered.intended_text.splitlines():
            for part in wrap(line, 100) or [""]:
                text.textLine(part)
        pdf.drawText(text)
        pdf.save()
        data = output.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        return replace(build, compiled=replace(build.compiled, pdf_bytes=data, pdf_sha256=digest), extracted=replace(build.extracted, pdf_sha256=digest, content_stream_bytes=len(data)))


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class FixtureTokens:
    def get_token(self, scopes, *, interactive=False, force_refresh=False):
        assert not interactive and "Mail.Send" not in scopes
        return "offline-fixture-token"


class FixtureGraph:
    """Fake only the HTTP edge; production Graph parsing/policy still runs."""
    def __init__(self):
        self.requests = []
        self.mail_seen = False
        self.throttle_next = False
        self.drafts = []
        self.body = "We would like to schedule a conversation about the Platform Engineer role at Northstar Labs."

    def send(self, method, url, headers, body):
        parsed = urlsplit(url)
        assert parsed.hostname == "graph.microsoft.com"
        self.requests.append((method, parsed.path))
        path = parsed.path
        status = 200
        if self.throttle_next and method == "GET" and path.endswith("/messages/delta"):
            self.throttle_next = False
            return RawHttpResponse(429, {"Retry-After": "1"}, json.dumps({"error": {"code": "TooManyRequests"}}).encode())
        if method == "GET" and path.endswith("/messages/delta"):
            if "skiptoken=" in parsed.query:
                payload = {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=fixture"}
                self.mail_seen = True
            elif self.mail_seen:
                payload = {"value": [], "@odata.deltaLink": url}
            else:
                payload = {"value": [{"id": "fixture-mail-1", "subject": "Northstar Labs interview",
                    "receivedDateTime": stamp(), "lastModifiedDateTime": stamp(),
                    "conversationId": "fixture-thread-1", "from": {"emailAddress": {"address": "recruiter@northstar.example.test"}}}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=second"}
        elif method == "GET" and path.endswith("/mailFolders/inbox/messages"):
            payload = {"value": []}
        elif method == "GET" and path == "/v1.0/me/messages/fixture-mail-1":
            payload = {"id": "fixture-mail-1", "subject": "Northstar Labs interview",
                "body": {"contentType": "text", "content": self.body}, "receivedDateTime": stamp(),
                "conversationId": "fixture-thread-1", "hasAttachments": False,
                "from": {"emailAddress": {"address": "recruiter@northstar.example.test"}}}
        elif method == "GET" and path == "/v1.0/me/calendar/calendarView":
            payload = {"value": []}
        elif method == "POST" and path == "/v1.0/me/messages/fixture-mail-1/createReply":
            self.drafts.append({"id": "fixture-draft-1", "body": ""})
            payload = {"id": "fixture-draft-1", "isDraft": True}
            status = 201
        elif method == "PATCH" and path == "/v1.0/me/messages/fixture-draft-1":
            self.drafts[-1]["body"] = json.loads(body)["body"]["content"]
            payload = {"id": "fixture-draft-1", "isDraft": True}
        else:
            raise AssertionError(f"unimplemented fixture Graph request: {method} {path}")
        return RawHttpResponse(status, {}, json.dumps(payload).encode())


class FixtureClassifier:
    def classify(self, text, candidates):
        quote = "We would like to schedule a conversation"
        start = text.index(quote)
        app = next(c for c in candidates if c["employer"] == "Northstar Labs")
        return {"event_type": "interview_requested", "application_id": app["application_id"],
            "confidence": 0.99, "evidence_quote": quote, "span_start": start,
            "span_end": start + len(quote), "payload": {}}


class FixtureKeys:
    def __init__(self, root):
        self.path = root / "fixture-archive-key"
        self.path.write_bytes(os.urandom(32))
        self.path.chmod(0o600)

    def get_or_create_key(self):
        key = self.path.read_bytes()
        return hashlib.sha256(key).hexdigest(), key


class Browser:
    def __init__(self, server):
        self.port = server.server_address[1]
        self.cookie = ""
        self.csrf = ""
        self.csrf = self.request("GET", "/api/v1/session")["csrf_token"]
        self.sequence = 0

    def request(self, method, path, body=None):
        headers = {"Cookie": self.cookie, "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}", "X-CSRF-Token": self.csrf}
        if method == "POST":
            self.sequence += 1
            body = {**(body or {}), "idempotency_key": f"fixture-browser-{self.sequence}"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        raw = response.read()
        connection.close()
        result = json.loads(raw)
        if not 200 <= response.status < 300:
            raise AssertionError(f"dashboard {method} {path}: {response.status} {result}")
        return result


def seed_jobs(root):
    jobs = root / "jobs.db"
    state = root / "preference.db"
    descriptions = ["Required: Python and Kubernetes production experience. Preferred: PostgreSQL experience.",
        "Build TypeScript APIs and customer reporting systems.", "Analyze business reporting and spreadsheet workflows."]
    with sqlite3.connect(jobs) as connection:
        connection.execute("CREATE TABLE jobs (ats TEXT NOT NULL,id TEXT NOT NULL,company TEXT,title TEXT,department TEXT,team TEXT,employmentType TEXT,location TEXT,isRemote TEXT,workplaceType TEXT,publishedAt TEXT,jobUrl TEXT,description TEXT,matched TEXT,first_seen TEXT,last_seen TEXT,closed_at TEXT,PRIMARY KEY(ats,id))")
        for i, (company, title) in enumerate((("Northstar Labs", "Platform Engineer"), ("Harbor Systems", "Backend Engineer"), ("Fieldwork", "Business Analyst"))):
            connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                ("ashby", f"job-{i+1}", company, title, "Engineering", "", "FullTime", "Chicago, IL", "true", "Remote", stamp(), f"https://example.test/jobs/{i+1}", descriptions[i], "", stamp(), stamp()))
        # Deliberately include untrusted markup to exercise the preview sanitizer.
        connection.execute("ALTER TABLE jobs ADD COLUMN description_html TEXT")
        connection.execute("UPDATE jobs SET description_html=? WHERE id='job-1'", (
            '<h2>Requirements</h2><p>Required: <strong>Python</strong> and Kubernetes production experience.</p>'
            '<ul><li>Preferred: PostgreSQL experience.</li></ul>'
            '<script>window.unsafePreview = true</script><img src="https://example.test/preview-tracker" onerror="window.unsafePreview = true">',))
    prepare_families(jobs)
    prepare_preferences(jobs)
    prepare_state(state)
    encoder = HashingEncoder(64)
    reference = encoder.encode(["Python Kubernetes PostgreSQL backend platform engineer"])[0]
    with sqlite3.connect(jobs) as source, sqlite3.connect(state) as target:
        target.execute("INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)", ("offline-hashing-fixture", stamp(), encoder.model_revision, "fixture-v1", 0, json.dumps({"fixture": True, "trained_model": False}), ""))
        target.execute("INSERT INTO preference_state VALUES ('champion_run_id','offline-hashing-fixture')")
        for family, title, description in source.execute("SELECT m.family_id,j.title,j.description FROM job_family_members m JOIN jobs j ON j.ats=m.ats AND j.id=m.job_id"):
            vector = encoder.encode([title + " " + description])[0]
            score = min(0.95, max(0.05, (1 + sum(a*b for a,b in zip(reference, vector))) / 2))
            target.execute("INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?,?)", ("offline-hashing-fixture", family, "fixture", score, score, score, score, "{}", stamp()))


def enqueue(config, kind, suffix):
    with connect(config.application_db) as connection:
        connection.execute("INSERT INTO work_items (work_id,task_kind,dedupe_key,payload_json,status,priority,due_at,attempts,max_attempts,created_at,lane,workflow_id) VALUES (?,?,?,'{}','queued',50,?,0,5,?,'core','')",
            ("fixture-" + suffix, kind, "fixture-" + suffix, stamp(), stamp()))


def run(args):
    if getattr(args, "scenario", "acceptance") == "portfolio":
        from scripts.portfolio_demo import run_portfolio
        return run_portfolio(args)
    root = args.state_dir.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("use an empty --state-dir so fixture data cannot replace existing state")
    root.chmod(0o700)
    seed_jobs(root)
    # Unix socket path lengths are limited on macOS; keep this runtime mount short.
    with tempfile.TemporaryDirectory(prefix="career-fixture-") as socket_directory:
        socket_root = Path(socket_directory)
        socket_root.chmod(0o700)
        executable = root / "fixture-hermes"
        executable.write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
        executable.chmod(0o700)
        socket_path = socket_root / "delivery.sock"
        dispatcher = HermesDispatcher(executable, target="telegram:offline-fixture", receipts=DeliveryReceiptStore(root / "receipts.db"))
        bridge_server = _HermesServer(socket_path, dispatcher)
        socket_path.chmod(0o600)
        config = replace(RuntimeConfigV1.defaults(root), project_root=Path(__file__).resolve().parents[1],
            application_db=root / "applications.db", jobs_db=root / "jobs.db", preference_db=root / "preference.db",
            proxy_db=root / "proxy.db", resume_lab_db=root / "resume.db", resume_artifact_root=root / "artifacts",
            hermes_notification_socket=socket_path, hermes_telegram_target="telegram:offline-fixture")
        ledger = JobSearchLedger(config.application_db)
        toolchain = DemoPdfToolchain() if getattr(args, "interactive", False) else Toolchain()
        if args.tectonic:
            from job_search.resume_lab.tex import TectonicCompiler
            from job_search.resume_lab.pdf import PypdfExtractor
            from job_search.resume_lab.toolchain import ResumeArtifactToolchain
            toolchain = ResumeArtifactToolchain(TectonicCompiler(args.tectonic.resolve(), args.bundle.resolve(), args.tectonic_version), PypdfExtractor())
        gateway = ResumeLabProductionGateway(ResumeLabService(config.resume_lab_db), ResumePdfArtifactRepository(config.resume_artifact_root),
            application_db=config.application_db, model=CareerModel(), toolchain=toolchain)
        graph = FixtureGraph()
        session = GraphSession(FixtureTokens(), graph)
        mail = GraphMailClient(session)
        outlook = GraphOutlookClient(session)
        availability = AvailabilityPlanner(outlook)
        archive = EncryptedMailArchive(ledger, FixtureKeys(root))
        coordinator = OutlookMailCoordinator(mail, SQLiteOutlookState(config.application_db), ledger,
            classifier=FixtureClassifier(), model_version="offline-classifier-fixture", secure_ingestor=SecureMailIngestor(archive))
        clock = [datetime.now(timezone.utc)]
        core_overrides = {
            "outlook.mail.sync": OutlookMailTaskHandler(coordinator, account_id="outlook-personal"),
            "outlook.actions.execute": ApprovedActionTaskHandler(ledger, ActionExecutor(ledger, outlook, availability, account_id="outlook-personal"))}
        core = build_runtime(config, lane="core", base_environment={}, now_provider=lambda: clock[0], task_overrides=core_overrides)
        model = build_runtime(config, lane="model", base_environment={}, task_overrides={"resume.optimize": gateway.handle_work})
        controller = build_dashboard_controller(config, resume_lab=gateway, mail_source=EncryptedArchiveMailSource(ledger, archive))
        controller.demo_mode = True
        dashboard = make_server(controller, args.port if args.serve else 0)
        sources = build_hermes_sources_from_config(config, mail_source=EncryptedArchiveMailSource(ledger, archive),
            availability=availability, resume_lab=build_resume_lab_read_gateway(config))
        token = "offline-fixture-" + os.urandom(24).hex()
        mcp = make_mcp_server_from_sources(sources, token, 0)
        servers = [bridge_server, dashboard, mcp]
        threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        def mcp_call(name, arguments):
            connection = http.client.HTTPConnection("127.0.0.1", mcp.server_address[1], timeout=15)
            connection.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}),
                {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION})
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            assert response.status == 200 and not value.get("error"), value
            assert not value["result"].get("isError"), value
            return json.loads(value["result"]["content"][0]["text"])
        try:
            browser = Browser(dashboard)
            demo_profile = {**PROFILE, "identity": {**PROFILE["identity"], "name": "Sean", "email": "sean@example.test"}}
            profile = browser.request("POST", "/api/v1/career-profile", {"content": demo_profile})
            browser.request("POST", "/api/v1/career-profile/approve", {"revision_id": profile["draft_revision_id"]})
            shortlist = browser.request("POST", "/api/v1/shortlist", {"options": {"limit": 3, "policy": "champion"}})
            assert len(shortlist["recommendations"]) == 3, shortlist
            if getattr(args, "interactive", False):
                from job_search.contracts import JobSnapshot, RecommendationProvenance, MutationContext
                for suffix, company, title in (("4", "Cedar Health", "Full Stack Engineer"), ("5", "Waypoint", "Backend Engineer")):
                    started = ledger.start_application(JobSnapshot("ashby", "demo-" + suffix, "demo-family-" + suffix, title, company, company.lower().replace(" ", "-"), "https://example.test/background/" + suffix), RecommendationProvenance(), MutationContext("demo-background-" + suffix, "user", "dashboard"))
                    if suffix == "4":
                        browser.request("POST", f"/api/v1/applications/{started['application']['application_id']}/submitted", {"resume_decision": "not_tracked"})
                # Advance external fixture edges separately; all approvals stay in the dashboard.
                report = {"fixture": True, "dashboard_url": f"http://127.0.0.1:{dashboard.server_address[1]}", "status": "ready", "stage": "discovery"}
                write_json_atomic(root / "demo-status.json", report)
                print(json.dumps(report), flush=True)
                sequence = 0
                try:
                    while True:
                        clock[0] = datetime.now(timezone.utc)
                        model.tick()
                        command_path = root / "demo-command.json"
                        if command_path.exists():
                            command = json.loads(command_path.read_text())
                            command_path.unlink()
                            sequence += 1
                            try:
                                applications = ledger.list_applications()
                                app = next(a["application_id"] for a in applications if a["employer_snapshot"] == "Northstar Labs")
                                if command["step"] == "curated":
                                    published = mcp_call("publish_curated_shortlist", {
                                        "title": f"Codex daily picks {sequence}", "idempotency_key": f"demo-curated-{sequence}",
                                        "jobs": [{"ats": "ashby", "job_id": "job-2", "explanation": "Relevant Python experience. <script>unsafe()</script>"},
                                                 {"ats": "ashby", "job_id": "job-1", "explanation": "Production systems experience."}]})
                                    report["curated_list_id"] = published["list_id"]
                                elif command["step"] == "mail":
                                    enqueue(config, "outlook.mail.sync", f"interactive-mail-{sequence}")
                                    # The insert may cross a second boundary after this loop captured its clock.
                                    clock[0] = datetime.now(timezone.utc)
                                    core.tick()
                                elif command["step"] == "reply":
                                    with connect(config.application_db) as connection:
                                        proposal = connection.execute("SELECT evidence_id FROM event_proposals WHERE status IN ('accepted','auto_applied') LIMIT 1").fetchone()
                                    if proposal is None:
                                        raise ValueError("Review and accept the recruiter update first.")
                                    context = mcp_call("get_application_timeline", {"application_id": app})
                                    assert context
                                    mcp_call("propose_reply", {"application_id": app, "evidence_id": proposal["evidence_id"], "body": "Hi Morgan,\n\nThank you for reaching out. I would be happy to discuss the Platform Engineer role at Northstar Labs. I am available Tuesday or Wednesday afternoon. Does either work for your team?\n\nBest,\nSean", "idempotency_key": "interactive-reply"})
                                elif command["step"] == "execute":
                                    enqueue(config, "outlook.actions.execute", f"interactive-action-{sequence}")
                                    clock[0] = datetime.now(timezone.utc)
                                    core.tick()
                                report.update(stage=command["step"], status="ready", command_id=command["id"], application_id=app, drafts_created=len(graph.drafts))
                                report.pop("error", None)
                            except Exception as exc:
                                report.update(status="error", error=str(exc), command_id=command["id"])
                            write_json_atomic(root / "demo-status.json", report)
                        time.sleep(.2)
                except KeyboardInterrupt:
                    return report
            chosen = next(j for j in shortlist["recommendations"] if j["id"] == "job-1")
            prepared = browser.request("POST", "/api/v1/resume-lab/prepare", {"session_id": shortlist["session_id"], "impression_id": chosen["impression_id"]})
            app = prepared["application"]["application_id"]
            run_id = prepared["resume_lab"]["run_id"]
            model.tick()
            result = browser.request("GET", f"/api/v1/resume-lab/runs/{run_id}/result")
            assert result["status"] == "succeeded", result
            candidate = result["comparisons"][0]
            browser.request("POST", f"/api/v1/resume-lab/runs/{run_id}/approve", {"comparison_kind": "grounded_rewrite"})
            browser.request("POST", f"/api/v1/applications/{app}/resume-selection", {"artifact_id": candidate["artifact_id"], "evaluation_id": candidate["evaluation_id"]})
            browser.request("POST", f"/api/v1/applications/{app}/submitted", {"resume_decision": "selected"})
            clock[0] = datetime.now(timezone.utc)
            enqueue(config, "outlook.mail.sync", "mail")
            core.tick()
            with connect(config.application_db) as connection:
                proposals = [dict(row) for row in connection.execute("SELECT * FROM event_proposals")]
            assert len(proposals) == 1, proposals
            proposal = proposals[0]
            browser.request("POST", f"/api/v1/proposals/{proposal['proposal_id']}/decision", {"decision": "accepted", "selected_application_id": app})
            reply = mcp_call("propose_reply", {"application_id": app, "evidence_id": proposal["evidence_id"],
                "body": "Thank you. I would be happy to discuss the Platform Engineer role.", "idempotency_key": "fixture-reply"})
            actions = ledger.list_actions()
            assert len(actions) == 1 and not graph.drafts
            action = actions[0]
            browser.request("POST", f"/api/v1/actions/{action['action_id']}/decision", {"approve": True, "payload_sha256": action["payload_sha256"]})
            enqueue(config, "outlook.actions.execute", "action")
            clock[0] = datetime.now(timezone.utc)
            core.tick()
            assert len(graph.drafts) == 1 and graph.drafts[0]["body"].startswith("Thank you")
            resume = mcp_call("get_application_resume_content", {"application_id": app})
            assert resume["available"] and resume["provenance"]["binding"] == "submitted"
            mail_results = mcp_call("search_mail", {"query": "Northstar", "limit": 5})
            assert "Northstar" in json.dumps(mail_results)
            # Production now routes events through shadow attention. This fixture
            # explicitly activates one current, user-requested reminder to exercise
            # the receipt-aware delivery transport without historical alert replay.
            from job_search.contracts import MutationContext
            attention = core.worker.task_handlers["attention.tick"].ledger.attention
            prefs = attention.preferences()
            attention.update_preferences({"shadow": False}, prefs["revision"], MutationContext("fixture-notifications", "user", "fixture"))
            from job_search.notifications import NotificationIntent
            due = datetime.now(timezone.utc) + timedelta(minutes=1)
            reminder = ledger.create_reminder({"application_id": app, "due_at": due.isoformat(timespec="seconds").replace("+00:00", "Z"), "note": "Review your application."}, MutationContext("fixture-reminder", "user", "fixture"))
            clock[0] = due + timedelta(seconds=1)
            attention.from_notification(NotificationIntent("reminder.due", "fixture-reminder", "Fixture reminder", "Review your application.", application_id=app, context={"reminder_id": "general:" + reminder["reminder"]["reminder_id"]}))
            attention.evaluate()
            enqueue(config, "notification.deliver", "delivery")
            core.tick()
            delivered = ledger.list_notification_outbox(("delivered",))
            assert delivered, ledger.system_health()
            # Replaying a terminal delta page and restarting both workers changes
            # neither event/proposal counts nor the one approved reply draft.
            timeline_before = ledger.get_application_timeline(app)
            enqueue(config, "outlook.mail.sync", "mail-replay")
            clock[0] = datetime.now(timezone.utc)
            core.tick()
            with connect(config.application_db) as connection:
                assert connection.execute("SELECT COUNT(*) FROM event_proposals").fetchone()[0] == 1
            assert ledger.get_application_timeline(app) == timeline_before
            assert len(graph.drafts) == 1
            assert all(not path.endswith("/send") for _, path in graph.requests)
            # A transient Graph response exercises the actual worker retry path.
            graph.throttle_next = True
            enqueue(config, "outlook.mail.sync", "mail-throttled")
            clock[0] = datetime.now(timezone.utc)
            core.tick()
            with connect(config.application_db) as connection:
                delayed = dict(connection.execute("SELECT status,due_at,attempts FROM work_items WHERE work_id='fixture-mail-throttled'").fetchone())
            assert delayed["status"] == "queued" and delayed["attempts"] == 1, delayed
            clock[0] += timedelta(minutes=2)
            restarted_core = build_runtime(config, lane="core", base_environment={}, now_provider=lambda: clock[0], task_overrides=core_overrides)
            restarted_core.tick()
            with connect(config.application_db) as connection:
                recovered = dict(connection.execute("SELECT status,attempts FROM work_items WHERE work_id='fixture-mail-throttled'").fetchone())
            assert recovered == {"status": "succeeded", "attempts": 2}, recovered
            assert len(graph.drafts) == 1
            # Reconstruct the production workers from the same state; no fixture
            # counters substitute for durable completion/receipt records.
            restarted_model = build_runtime(config, lane="model", base_environment={}, task_overrides={"resume.optimize": gateway.handle_work})
            restarted_model.tick()
            assert gateway.get_run_result(run_id)["status"] == "succeeded"
            for notification in delivered:
                assert dispatcher.receipts.status(notification["notification_id"])["state"] == "delivered"
            report = {"status": "passed", "fixture": True, "application_id": app, "run_id": run_id,
                "dashboard_url": f"http://127.0.0.1:{dashboard.server_address[1]}",
                "checks": ["real shortlist policy and impression records", "dashboard CSRF and user approvals", "leased resume worker", "immutable submitted resume context over MCP HTTP", "Graph delta pagination and replay", "Graph throttling and retry after worker restart", "encrypted mail archive", "reviewed recruiter event", "MCP reply proposal", "approved draft creation only", "application feedback outbox", "notification Unix socket and durable receipts", "worker reconstruction"],
                "fixtures": {"ranking": "deterministic hashing similarity scores; real shortlist policy; no trained ranker validation", "model": "fixed CareerModel and mail classifier", "graph": "in-process fake HTTP edge", "telegram": "local successful fake executable", "pdf": "real Tectonic/Pypdf" if args.tectonic else "fixture document tool"},
                "notifications_delivered": len(delivered), "graph_requests": len(graph.requests)}
            (root / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)
            if args.serve:
                print("Fictional local acceptance dashboard. Ctrl-C stops it.", flush=True)
                try:
                    while True:
                        clock[0] = max(clock[0], datetime.now(timezone.utc))
                        core.tick()
                        model.tick()
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    pass
            return report
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--interactive", action="store_true", help="Leave review and approval to the browser")
    parser.add_argument("--scenario", choices=("acceptance", "portfolio"), default="acceptance", help="Choose the original acceptance flow or a populated portfolio workspace")
    parser.add_argument("--advance", choices=("mail", "reply", "execute", "curated"), help="Advance an external fixture in an existing interactive demo")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--tectonic", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--tectonic-version")
    args = parser.parse_args(argv)
    if any((args.tectonic, args.bundle, args.tectonic_version)) and not all((args.tectonic, args.bundle, args.tectonic_version)):
        parser.error("real PDF mode requires --tectonic, --bundle, and --tectonic-version together")
    if args.advance:
        import uuid
        if not (args.state_dir / "demo-status.json").is_file():
            parser.error("No interactive demo exists at this state directory")
        target = args.state_dir / "demo-command.json"
        with target.open("x") as stream:
            json.dump({"id": str(uuid.uuid4()), "step": args.advance}, stream)
        return
    if args.interactive:
        args.serve = True
    run(args)


if __name__ == "__main__":
    main()
