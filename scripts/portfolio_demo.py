"""Populated, isolated portfolio scenario using production service boundaries.

Only data and external Graph/model responses are fixtures. No production config,
mailbox, secrets, or saved resume is opened. Start through offline_system_demo.py.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from job_search.collection.boards import save
from job_search.collection.dedupe import prepare_families
from job_search.contracts import (ApplicationEventType, EventInput, EventProposalInput,
    JobSnapshot, MutationContext, ProducerKind, RecommendationProvenance,
    TemporalProposalInput, TemporalProposalKind)
from job_search.curated import CuratedShortlists
from job_search.db import connect
from job_search.ranking.labeler import prepare_preferences
from job_search.ranking.model import HashingEncoder, prepare_state


PROFILE = {
    "identity": {"name": "Sean", "email": "sean@example.test", "contact_line": "Chicago, IL | 312-555-0142"},
    "summary": "Software engineer building useful interfaces and dependable services.",
    "education": [{"institution": "Lakefront University", "degree": "BS Computer Science", "dates": "May 2023", "location": "Chicago, IL"}],
    "experience": [
        {"company": "Juniper Systems", "role": "Software Engineer", "dates": "July 2023 - Present", "location": "Chicago, IL", "bullets": [
            "Built TypeScript and Python APIs backed by PostgreSQL for partner configuration tools.",
            "Developed React interfaces that surface validation errors before configuration changes are published.",
            "Added idempotent workers and integration tests for asynchronous document processing."]},
        {"company": "Lakeshore Analytics", "role": "Engineering Intern", "dates": "June 2022 - August 2022", "location": "Remote", "bullets": [
            "Developed a Python retrieval prototype with citations for internal research documents."]}],
    "projects": [
        {"name": "Neighborhood Data", "context": "TypeScript, React, PostgreSQL", "dates": "2025", "bullets": ["Built a searchable community dashboard with saved filters and scheduled data imports."]},
        {"name": "Document Queue", "context": "Python, Docker, AWS", "dates": "2026", "bullets": ["Implemented resumable document ingestion with retries, audit records, and deployment health checks."]}],
    "skills": [
        {"category": "Languages", "items": ["TypeScript", "JavaScript", "Python", "Kotlin", "SQL"]},
        {"category": "Frameworks", "items": ["React", "Node.js", "Express", "FastAPI"]},
        {"category": "Data and infrastructure", "items": ["PostgreSQL", "SQLite", "Docker", "AWS", "Terraform"]},
        {"category": "Testing and tools", "items": ["Git", "Playwright", "JUnit", "GitHub Actions"]}],
}

ROLES = [
    ("Northstar Labs", "Platform Engineer", "Chicago, IL", "Python, TypeScript, PostgreSQL"),
    ("Harbor Systems", "Backend Engineer", "Remote, United States", "Python, PostgreSQL, Docker"),
    ("Cedar Health", "Full Stack Engineer", "Boston, MA", "React, TypeScript, Node.js"),
    ("Morrowfield", "Product Engineer", "Chicago, IL", "React, TypeScript, PostgreSQL"),
    ("Ternwell AI", "AI Engineer", "Austin, TX", "Python, retrieval, evaluation"),
    ("Alderlight", "Software Engineer, Integrations", "Denver, CO", "TypeScript, APIs, OAuth"),
    ("Clearwater Data", "Software Engineer, Data Platform", "Remote, United States", "Python, SQL, AWS"),
    ("Waypoint Cloud", "Backend Software Engineer", "Raleigh, NC", "Python, queues, PostgreSQL"),
    ("Bramble Studio", "Full Stack Software Engineer", "Seattle, WA", "React, TypeScript, APIs"),
    ("Hearthstone Robotics", "Developer Tools Engineer", "Pittsburgh, PA", "Python, Docker, CI"),
    ("Brookline Metrics", "Software Engineer", "Minneapolis, MN", "TypeScript, PostgreSQL, testing"),
    ("Redwood Commons", "Product Engineer", "Remote, United States", "React, Node.js, SQL"),
    ("Sundial Works", "Software Engineer, Web", "Chicago, IL", "React, accessibility, TypeScript"),
    ("Northstar Labs", "Software Engineer, Internal Tools", "Chicago, IL", "React, TypeScript, Python"),
    ("Larch Financial", "Backend Engineer", "Charlotte, NC", "Python, PostgreSQL, APIs"),
    ("Mariner Search", "Applied AI Engineer", "Remote, United States", "Python, retrieval, evaluation"),
    ("Fable Transit", "Full Stack Engineer", "Philadelphia, PA", "React, Node.js, PostgreSQL"),
    ("Pinewell Energy", "Software Engineer, Platform", "Madison, WI", "Python, AWS, Terraform"),
    ("Aster Dispatch", "Product Engineer", "Denver, CO", "TypeScript, React, APIs"),
    ("Copperleaf Labs", "Forward Deployed Engineer", "Austin, TX", "Python, TypeScript, SQL"),
    ("Stillwater Systems", "Backend Engineer", "Remote, United States", "Python, Docker, PostgreSQL"),
    ("Brightmere", "Software Engineer, Customer Platform", "Boston, MA", "React, Node.js, OAuth"),
    ("Ashford Research", "AI Product Engineer", "Chicago, IL", "Python, React, retrieval"),
    ("Orchard Lane", "Software Engineer, APIs", "Raleigh, NC", "TypeScript, PostgreSQL, testing"),
]


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ctx(key, actor="system", source="portfolio_fixture"):
    return MutationContext("portfolio-" + key, actor, source)


def write_json_atomic(path, value):
    """Readers must see a complete receipt even while a stage finishes."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def seed_catalog(root, now):
    """Use collector saves to produce real posting lifecycle observations."""
    rows = []
    for index, (company, title, location, skills) in enumerate(ROLES):
        ats = ("greenhouse", "ashby", "lever")[index % 3]
        job_id = str(9700000 + index) if ats == "greenhouse" else str(uuid.uuid5(uuid.NAMESPACE_URL, "portfolio-job-" + str(index)))
        board = "demo-" + company.lower().replace(" ", "-")
        host = {"greenhouse": "job-boards.greenhouse.io", "ashby": "jobs.ashbyhq.com", "lever": "jobs.lever.co"}[ats]
        url = f"https://{host}/{board}/" + ("jobs/" if ats == "greenhouse" else "") + job_id
        posted = now - timedelta(days=(3 + index if index < 13 else index % 6), hours=index % 4 + 1)
        description = (f"Join {company} as a {title}. Work with a small product team to build reliable software used every day. "
            f"Responsibilities: Design and ship APIs and user interfaces; improve reliability through testing and observability; "
            f"collaborate with product and design on clear, maintainable solutions. "
            f"Requirements: 1-3 years of software engineering experience. Experience with {skills}. "
            "Comfortable working across a codebase, reviewing changes, and explaining engineering tradeoffs. "
            "Preferred: Experience with background jobs, CI, and production debugging. Benefits: Flexible work, learning budget, and health coverage.")
        html = f"<h2>About the role</h2><p>{description.split('Responsibilities:')[0]}</p><h2>What you will build</h2><ul><li>APIs and interfaces that support real customer work.</li><li>Reliable background processing with tests and observable failures.</li><li>Tools that make the rest of the engineering team more effective.</li></ul><h2>What you bring</h2><p>1-3 years of software engineering experience with {skills}.</p><h2>Working together</h2><p>{location}. A small product team, thoughtful code review, and time to learn.</p>"
        row = dict(ats=ats, id=job_id, company=company, title=title, department="Engineering", team="Product engineering", employmentType="FullTime", location=location,
            isRemote=str(location.startswith("Remote")).lower(), workplaceType="Remote" if location.startswith("Remote") else "Hybrid", publishedAt=iso(posted), posted_at=iso(posted), source_updated_at=iso(posted),
            jobUrl=url, description=description, description_html=html, matched="")
        save([row], root / "jobs.db", iso(posted + timedelta(minutes=12)))
        rows.append(row)
    # The hero changed its description and location after opening, without a new application.
    rows[0] = {**rows[0], "location": "Chicago, IL · Hybrid", "description": rows[0]["description"] + " Updated: This role now supports two remote days per week.", "source_updated_at": iso(now - timedelta(hours=2))}
    save([rows[0]], root / "jobs.db", iso(now - timedelta(hours=1)))
    # Keep closed and reopened examples in the durable catalog, not fabricated UI fields.
    save([], root / "jobs.db", iso(now - timedelta(hours=3)), covered=[(rows[10]["ats"], rows[10]["company"])])
    save([], root / "jobs.db", iso(now - timedelta(hours=3)), covered=[(rows[11]["ats"], rows[11]["company"])])
    save([rows[11]], root / "jobs.db", iso(now - timedelta(hours=1)), covered=[(rows[11]["ats"], rows[11]["company"])])
    prepare_families(root / "jobs.db")
    prepare_preferences(root / "jobs.db")
    from job_search.collection.locations import backfill
    backfill(root / "jobs.db")
    prepare_state(root / "preference.db")
    encoder = HashingEncoder(64)
    reference = encoder.encode(["Python TypeScript React PostgreSQL full stack engineer"])[0]
    with sqlite3.connect(root / "jobs.db") as source, sqlite3.connect(root / "preference.db") as state:
        state.execute("INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)", ("portfolio-hashing-fixture", iso(now), encoder.model_revision, "fixture-v1", 0, json.dumps({"fixture": True, "trained_model": False}), ""))
        state.execute("INSERT INTO preference_state VALUES ('champion_run_id','portfolio-hashing-fixture')")
        for family, title, description in source.execute("SELECT m.family_id,j.title,j.description FROM job_family_members m JOIN jobs j ON j.ats=m.ats AND j.id=m.job_id"):
            vector = encoder.encode([title + " " + description])[0]
            score = min(.95, max(.05, (1 + sum(a*b for a, b in zip(reference, vector))) / 2))
            state.execute("INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?,?)", ("portfolio-hashing-fixture", family, "fixture", score, score, score, score, "{}", iso(now)))
    # Policy controls share deterministic fixture scores, never invented training claims.
    from job_search.ranking.proxy import prepare_schema
    prepare_schema(root / "proxy.db")
    with sqlite3.connect(root / "proxy.db") as proxy:
        proxy.execute("INSERT INTO proxy_profiles VALUES (?,?,?,?)", ("portfolio-fixture", "fixture-v1", "{}", iso(now)))
        proxy.execute("INSERT INTO proxy_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("portfolio-fixture", "portfolio-fixture", "fixture-v1", "fixture-v1", "none-fixture", 0, 0, 0, "portfolio-fixture", "complete", iso(now), iso(now)))
        for policy in ("selective", "broad"):
            proxy.execute("INSERT INTO proxy_students VALUES (?,?,?,?,?,?,?,?,?)", ("portfolio-fixture", policy, "portfolio-hashing-fixture", encoder.model_revision, 0, str(root / "preference.db"), "", json.dumps({"fixture": True, "trained_model": False}), iso(now)))
    return rows


class PortfolioGraph:
    """Local HTTP edge with durable application services above it; no network I/O."""
    def __init__(self):
        self.requests, self.drafts, self.holds = [], [], []
        self.messages = {}

    def send(self, method, url, headers, body):
        from job_search.outlook.transport import RawHttpResponse
        parsed = urlsplit(url)
        assert parsed.hostname == "graph.microsoft.com"
        path = parsed.path
        self.requests.append((method, path))
        status = 200
        if method == "GET" and path.endswith("/messages/delta"):
            payload = {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=portfolio"}
        elif method == "GET" and path.endswith("/mailFolders/inbox/messages"):
            payload = {"value": []}
        elif method == "GET" and path == "/v1.0/me/calendar/calendarView":
            payload = {"value": []}
        elif method == "GET" and path.startswith("/v1.0/me/messages/"):
            payload = self.messages[path.rsplit("/", 1)[1]]
        elif method == "POST" and path.endswith("/createReply"):
            assert path.split("/")[-2] in self.messages
            payload = {"id": "portfolio-draft-" + str(len(self.drafts) + 1), "isDraft": True, "body": ""}
            self.drafts.append(payload)
            status = 201
        elif method == "PATCH" and path.startswith("/v1.0/me/messages/portfolio-draft-"):
            payload = next(item for item in self.drafts if item["id"] == path.rsplit("/", 1)[1])
            payload["body"] = json.loads(body)["body"]["content"]
        elif method == "POST" and path == "/v1.0/me/events":
            value = json.loads(body)
            assert value["attendees"] == [] and value["sensitivity"] == "private" and value["showAs"] == "tentative"
            existing = next((item for item in self.holds if item["transactionId"] == value["transactionId"]), None)
            payload = existing or {**value, "id": "portfolio-hold-" + str(len(self.holds) + 1)}
            if not existing:
                self.holds.append(payload)
            status = 201
        else:
            raise AssertionError(f"Unsupported portfolio Graph call: {method} {path}")
        return RawHttpResponse(status, {}, json.dumps(payload).encode())


class PortfolioPdfExtractor:
    """Read only PDFs just generated in this fixture; never accepts an uploaded file."""
    def extract(self, pdf):
        from io import BytesIO
        from pypdf import PdfReader, __version__
        from job_search.resume_lab.pdf import PdfExtraction
        reader = PdfReader(BytesIO(pdf), strict=True)
        text = "\n".join(page.extract_text() for page in reader.pages)
        layout = "\n".join(page.extract_text(extraction_mode="layout") for page in reader.pages)
        return PdfExtraction(hashlib.sha256(pdf).hexdigest(), "pypdf", __version__, len(reader.pages),
            sum(len(page.get_contents().get_data()) for page in reader.pages), text, layout)


def seed_workspace(config, now, rows, ledger, gateway, controller, archive, graph, browser, mcp_call):
    from job_search.mail.sanitizer import sanitize_mail
    from scripts.offline_system_demo import DemoPdfToolchain

    profile = browser.request("POST", "/api/v1/career-profile", {"content": copy.deepcopy(PROFILE)})
    browser.request("POST", "/api/v1/career-profile/approve", {"revision_id": profile["draft_revision_id"]})
    lists, publications = [], []
    orders = [list(range(8, 16)), list(range(4, 12)), [0, 16, 4, 18, 1, 19, 22, 7]]
    for days, order in zip((2, 1, 0), orders):
        at = now - timedelta(days=days)
        payload = {"title": f"Daily picks · {at.strftime('%B %d').replace(' 0', ' ')}", "idempotency_key": f"portfolio-list-{days}", "window_start": iso(at - timedelta(days=1)), "window_end": iso(at),
            "jobs": [{"ats": rows[i]["ats"], "job_id": rows[i]["id"], "explanation": [
                "Strong match for hands-on API development and reliable background services.",
                "Builds on React and TypeScript experience with ownership across the product.",
                "Relevant Python and data experience; room to grow in applied AI.",
                "A small engineering team with scope across customer-facing features and infrastructure."][rank % 4]} for rank, i in enumerate(order)]}
        # Time is controlled only while creating synthetic historical records.
        with patch("job_search.curated.utc_now", return_value=iso(at)):
            saved = mcp_call("publish_curated_shortlist", payload)
        lists.append(saved["list_id"])
        publications.append(payload)

    # Real, readable PDFs from fictional facts; no personal resume is loaded.
    documents = []
    for rank, (name, summary) in enumerate((("Sean · Software engineering", "Software engineer building reliable APIs and useful product interfaces."), ("Sean · Applied AI", "Software engineer combining product development with practical retrieval systems.")), 1):
        content = copy.deepcopy(PROFILE)
        content["summary"] = summary
        built = DemoPdfToolchain().build(content)
        document = gateway.import_existing_standard(name, rank, pdf=built.compiled.pdf_bytes,
            tex_source=built.rendered.tex_source, intended_text=built.rendered.intended_text, content=content,
            provenance={"kind": "portfolio_fixture"}, extractor=PortfolioPdfExtractor())
        documents.append(document)

    applications = {}
    # Start 13 real records; only the first 12 become submitted applications.
    for index in range(13):
        row = rows[index]
        at = now - timedelta(days=2 + index, minutes=35)
        with patch("job_search.store.utc_now", return_value=iso(at)):
            result = ledger.start_application(JobSnapshot(row["ats"], row["id"], "", row["title"], row["company"], row["company"], row["jobUrl"]),
                RecommendationProvenance(session_id=lists[-1], policy_id="curated", rank=1) if index == 0 else RecommendationProvenance(), ctx(f"start-{index}", "user"))
        applications[str(index)] = result["application"]["application_id"]
        if index == 12:
            continue
        app = applications[str(index)]
        if index in (0, 1):
            gateway.use_standard(row, application_id=app, idempotency_key=f"portfolio-resume-{index}")
        if index != 0:
            payload = {"resume": {"decision": "not_tracked", "reason": "fixture_not_attached"}}
            with patch("job_search.store.utc_now", return_value=iso(at + timedelta(minutes=5))):
                ledger.record_submission(app, iso(at + timedelta(minutes=5)), ctx(f"submit-{index}", "user", "browser_extension"), payload)

    # The hero has actual browser evidence and a pinned uploaded document fingerprint.
    tracking = controller.browser_tracking
    paired = tracking.enroll(tracking.issue_pairing("local")["pairing_code"], "chrome-extension://" + "a" * 32, "local")
    hero = applications["0"]
    digest = gateway.get_standard_document(documents[0]["standard_version_id"]).sha256
    observations = []
    for offset, kind in enumerate(("attempted", "request_sent", "site_acknowledged")):
        observation = {"observation_id": f"portfolio-hero-observation-{offset}", "attempt_id": "portfolio-hero-attempt-001", "page_url": rows[0]["jobUrl"], "kind": kind,
            "occurred_at": iso(now - timedelta(days=2, minutes=30 - offset)), "resume_sha256": digest,
            "metadata": {"signal": "success_dom" if kind == "site_acknowledged" else "submit_button", "adapter_version": "portfolio-v1"}}
        tracking.observe(paired["device_id"], observation)
        observations.append(observation)

    def event(index, kind, hours=24):
        at = iso(now - timedelta(hours=hours))
        actor = "user" if kind in (ApplicationEventType.REJECTION_RECEIVED, ApplicationEventType.WITHDRAWN) else "system"
        with patch("job_search.store.utc_now", return_value=at):
            return ledger.record_event(EventInput(applications[str(index)], kind, at, {}, f"portfolio-event-{index}-{kind.value}", ctx(f"event-{index}-{kind.value}", actor, "dashboard" if actor == "user" else "outlook_sync")))

    # 2 awaiting, 4 active (including hero), 3 interviewing, 1 offer, 2 terminal.
    for index in (1, 2, 3, 4, 5, 6, 7, 8, 9):
        event(index, ApplicationEventType.SUBMISSION_CONFIRMED, 48 + index)
    for index in (4, 5, 6):
        event(index, ApplicationEventType.INTERVIEW_REQUESTED, 20 + index)
        event(index, ApplicationEventType.INTERVIEW_SCHEDULED, 2 if index == 4 else 10 + index)
    event(7, ApplicationEventType.INTERVIEW_COMPLETED, 15)
    event(7, ApplicationEventType.OFFER_RECEIVED, 3)
    event(8, ApplicationEventType.REJECTION_RECEIVED, 8)
    event(9, ApplicationEventType.WITHDRAWN, 6)

    def message(key, subject, body, index, kind, *, accepted=False, candidates=None, confidence=.97, quote=None, hours=1):
        app = applications[str(index)] if index is not None else None
        at = iso(now - timedelta(hours=hours))
        sender = "morgan@northstar.example.test" if index == 0 else "recruiting@teams.example.test"
        sanitized = sanitize_mail(subject, body, body_kind="text")
        record = archive.archive_message(account_id="outlook-personal", immutable_message_id=key, sanitized_text=sanitized.text, truncated=False, context=ctx("archive-" + key))
        evidence = ledger.record_mail_evidence({"account_id": "outlook-personal", "immutable_message_id": key, "sender": sender,
            "subject": subject, "received_at": at, "body_sha256": sanitized.content_sha256, "excerpt": sanitized.text}, ctx("evidence-" + key))["evidence"]
        graph.messages[key] = {"id": key, "subject": subject, "body": {"contentType": "text", "content": body}, "receivedDateTime": at,
            "conversationId": "portfolio-thread-" + str(index), "hasAttachments": False, "from": {"emailAddress": {"address": sender}}}
        quote = quote or body.split("\n")[0]
        start = sanitized.text.index(quote)
        proposal = ledger.create_event_proposal(EventProposalInput(evidence["evidence_id"], app, kind, ProducerKind.MODEL,
            "portfolio-fixture-v1", confidence, candidates or ([app] if app else []), quote, start, start + len(quote), {"occurred_at": at}, "portfolio-proposal-" + key), ctx("proposal-" + key, "model", "classifier"))["proposal"]
        if accepted:
            with patch("job_search.store.utc_now", return_value=at):
                ledger.decide_event_proposal(proposal["proposal_id"], "accepted", app, "Previously reviewed fixture message", ctx("accept-" + key, "user", "dashboard"))
        return {"evidence": evidence, "archive": record["archive"], "text": sanitized.text, "proposal": proposal}

    confirmed = message("portfolio-confirmation", "Application received · Northstar Labs", "Thank you for applying for Platform Engineer. We have received your application and will be in touch after reviewing it.", 0,
        ApplicationEventType.SUBMISSION_CONFIRMED, accepted=True, hours=47)
    contact = message("portfolio-contact", "Your application at Northstar Labs", "Hi Sean,\n\nYour experience building APIs and internal tools stood out to our engineering team. I will follow up with a few options for an introductory conversation.\n\nMorgan", 0,
        ApplicationEventType.RECRUITER_CONTACT, accepted=True, hours=.4)
    interview = message("portfolio-interview", "Let's find a time to talk", "Hi Sean,\n\nWe would like to schedule a conversation about the Platform Engineer role. Would Wednesday at 2:00 PM Central work for a 30-minute call?\n\nMorgan", 0,
        ApplicationEventType.INTERVIEW_REQUESTED, quote="We would like to schedule a conversation", hours=.25)
    uncertain = message("portfolio-uncertain", "Next steps with the engineering team", "Hi Sean,\n\nI coordinate recruiting for Cedar Health and Morrowfield. We enjoyed reviewing your application and would like to schedule a conversation with the engineering team. Can you share your availability this week?\n\nTaylor", None,
        ApplicationEventType.INTERVIEW_REQUESTED, candidates=[applications["2"], applications["3"]], confidence=.62, quote="would like to schedule a conversation", hours=.8)
    deadline = message("portfolio-assessment", "A short technical exercise · Harbor Systems", "Hi Sean,\n\nPlease submit the technical exercise by Friday at 5:00 PM Central. We expect it to take about one hour.\n\nThe Harbor Systems team", 1,
        ApplicationEventType.ASSESSMENT_REQUESTED, accepted=True, quote="Please submit the technical exercise", hours=5)

    def temporal(key, source, app, kind, quote, **times):
        start = source["text"].index(quote)
        return ledger.create_temporal_proposal(TemporalProposalInput(source["archive"]["archive_id"], None, app, kind,
            times.get("starts_at"), times.get("ends_at"), times.get("due_at"), "America/Chicago", .88, quote, start, start + len(quote),
            hashlib.sha256(source["text"].encode()).hexdigest(), "portfolio-temporal-fixture", "portfolio-temporal-" + key), ctx("temporal-" + key, "model", "temporal_extractor"))["proposal"]

    local_now = now.astimezone(ZoneInfo("America/Chicago"))
    def next_weekday(weekday, hour):
        result = (local_now + timedelta(days=(weekday - local_now.weekday()) % 7)).replace(hour=hour, minute=0, second=0)
        return result if result > local_now else result + timedelta(days=7)
    next_day = next_weekday(2, 14)
    time_proposal = temporal("interview", interview, hero, TemporalProposalKind.INTERVIEW,
        "Wednesday at 2:00 PM Central", starts_at=iso(next_day), ends_at=iso(next_day + timedelta(minutes=30)))
    due_proposal = temporal("deadline", deadline, applications["1"], TemporalProposalKind.DEADLINE,
        "Friday at 5:00 PM Central", due_at=iso(next_weekday(4, 17)))
    reply_body = "Hi Morgan,\n\nThank you for reaching out. I would be glad to learn more about the Platform Engineer role and the team. Wednesday at 2:00 PM Central works for me.\n\nBest,\nSean"
    reply = mcp_call("propose_reply", {"application_id": hero, "evidence_id": contact["evidence"]["evidence_id"], "body": reply_body, "idempotency_key": "portfolio-reply"})
    action = ledger.list_actions()[0]
    assert action["status"] == "pending" and not graph.drafts
    assert ledger.verify_projections() == []
    return {"scenario": "portfolio", "fixture": True, "fixture_now": iso(now), "status": "ready", "stage": "populated",
        "hero_application_id": hero, "application_id": hero, "curated_list_id": lists[-1],
        "ids": {"applications": applications, "lists": lists, "hero_reply_action": action["action_id"], "hero_interview_review": interview["proposal"]["proposal_id"],
            "uncertain_match_review": uncertain["proposal"]["proposal_id"], "interview_time_review": time_proposal["temporal_proposal_id"], "deadline_review": due_proposal["temporal_proposal_id"],
            "resume_versions": [doc["standard_version_id"] for doc in documents]},
        "counts": {"catalog_jobs": 24, "saved_lists": 3, "jobs_per_list": 8, "submitted_applications": 12, "stored_drafts": 1, "pending_review_items": 5, "saved_resume_pdfs": 2},
        "phases": dict(Counter(app["current_phase"] for app in ledger.list_applications())),
        "fixtures": {"ranking": "deterministic hashing similarity; real saved-score and shortlist services, not trained personal models", "graph": "in-process HTTP fixtures", "pdf": "ReportLab, fictional career facts", "telegram": "local delivery sink"},
        "_replay": {"device_id": paired["device_id"], "observation": observations[-1], "publications": publications}, "drafts_created": 0, "holds_created": 0}


def run_portfolio(args):
    # Some diagnostic helpers consult os.environ rather than the worker's explicit
    # base_environment. Keep the entire server/worker lifetime free of inherited
    # application configuration and credentials, including interactive advances.
    # Restore the caller's environment only after all local servers have stopped.
    environment = {name: os.environ[name] for name in ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT") if name in os.environ}
    with patch.dict(os.environ, environment, clear=True):
        return _run_portfolio_isolated(args)


def _run_portfolio_isolated(args):
    from scripts.offline_system_demo import (Browser, DemoPdfToolchain, FixtureClassifier, FixtureKeys, FixtureTokens, enqueue)
    from tests.test_career_resume import CareerModel
    from job_search.actions import ActionExecutor
    from job_search.availability import AvailabilityPlanner
    from job_search.dashboard import make_server
    from job_search.hermes_delivery import DeliveryReceiptStore, HermesDispatcher, _HermesServer
    from job_search.hermes_mcp import MCP_PROTOCOL_VERSION, make_mcp_server_from_sources
    from job_search.mail.archive import EncryptedMailArchive
    from job_search.mail.archive_source import EncryptedArchiveMailSource
    from job_search.mail.secure_ingest import SecureMailIngestor
    from job_search.outlook.client import GraphOutlookClient
    from job_search.outlook.mail import GraphMailClient
    from job_search.outlook.state import SQLiteOutlookState
    from job_search.outlook.transport import GraphSession
    from job_search.resume_lab.artifacts import ResumePdfArtifactRepository
    from job_search.resume_lab.gateway import ResumeLabProductionGateway, build_resume_lab_read_gateway
    from job_search.resume_lab.service import ResumeLabService
    from job_search.runtime import RuntimeConfigV1, build_runtime
    from job_search.service import JobSearchLedger
    from job_search.sync import OutlookMailCoordinator
    from job_search.system import build_dashboard_controller, build_hermes_sources_from_config
    from job_search.worker import ApprovedActionTaskHandler, OutlookMailTaskHandler

    root = args.state_dir.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("use an empty --state-dir so fixture data cannot replace existing state")
    root.chmod(0o700)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = seed_catalog(root, now)
    with tempfile.TemporaryDirectory(prefix="career-portfolio-") as socket_directory:
        executable = root / "fixture-hermes"
        executable.write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
        executable.chmod(0o700)
        socket_path = Path(socket_directory) / "delivery.sock"
        dispatcher = HermesDispatcher(executable, target="telegram:offline-fixture", receipts=DeliveryReceiptStore(root / "receipts.db"))
        bridge = _HermesServer(socket_path, dispatcher)
        socket_path.chmod(0o600)
        config = replace(RuntimeConfigV1.defaults(root), project_root=Path(__file__).resolve().parents[1], application_db=root / "applications.db", jobs_db=root / "jobs.db", preference_db=root / "preference.db",
            proxy_db=root / "proxy.db", resume_lab_db=root / "resume.db", resume_artifact_root=root / "artifacts", resume_mode="standard",
            log_dir=root / "logs", mcp_token_file=root / "fixture-mcp-token", hermes_data_dir=root / "hermes", hermes_workspace_dir=root / "hermes-workspace",
            hermes_notification_socket=socket_path, hermes_telegram_target="telegram:offline-fixture")
        ledger = JobSearchLedger(config.application_db)
        gateway = ResumeLabProductionGateway(ResumeLabService(config.resume_lab_db), ResumePdfArtifactRepository(config.resume_artifact_root), application_db=config.application_db, model=CareerModel(), toolchain=DemoPdfToolchain())
        graph = PortfolioGraph()
        graph_session = GraphSession(FixtureTokens(), graph)
        outlook = GraphOutlookClient(graph_session)
        availability = AvailabilityPlanner(outlook)
        archive = EncryptedMailArchive(ledger, FixtureKeys(root))
        coordinator = OutlookMailCoordinator(GraphMailClient(graph_session), SQLiteOutlookState(config.application_db), ledger, classifier=FixtureClassifier(), model_version="portfolio-fixture", secure_ingestor=SecureMailIngestor(archive))
        clock = [datetime.now(timezone.utc)]
        core = build_runtime(config, lane="core", base_environment={}, now_provider=lambda: clock[0], task_overrides={
            "outlook.mail.sync": OutlookMailTaskHandler(coordinator, account_id="outlook-personal"),
            "outlook.actions.execute": ApprovedActionTaskHandler(ledger, ActionExecutor(ledger, outlook, availability, account_id="outlook-personal"))})
        controller = build_dashboard_controller(config, resume_lab=gateway, mail_source=EncryptedArchiveMailSource(ledger, archive))
        controller.demo_mode = True
        dashboard = make_server(controller, args.port if args.serve else 0)
        token = "portfolio-fixture-" + os.urandom(24).hex()
        mcp = make_mcp_server_from_sources(build_hermes_sources_from_config(config, mail_source=EncryptedArchiveMailSource(ledger, archive), availability=availability, resume_lab=build_resume_lab_read_gateway(config)), token, 0)
        servers = [bridge, dashboard, mcp]
        threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True) for server in servers]
        for thread in threads:
            thread.start()

        def mcp_call(name, arguments):
            connection = http.client.HTTPConnection("127.0.0.1", mcp.server_address[1], timeout=15)
            connection.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}),
                {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION})
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
            assert response.status == 200 and not result.get("error"), result
            assert not result["result"].get("isError"), result
            return json.loads(result["result"]["content"][0]["text"])

        def work(kind, suffix):
            enqueue(config, kind, "portfolio-" + suffix)
            clock[0] = datetime.now(timezone.utc)
            core.tick()

        try:
            browser = Browser(dashboard)
            report = seed_workspace(config, now, rows, ledger, gateway, controller, archive, graph, browser, mcp_call)
            replay = report.pop("_replay")
            report["dashboard_url"] = f"http://127.0.0.1:{dashboard.server_address[1]}"
            # Verify retry behavior without approving or executing pending user decisions.
            before = ledger.get_application_timeline(report["hero_application_id"])
            controller.browser_tracking.observe(replay["device_id"], replay["observation"])
            for publication in replay["publications"]:
                mcp_call("publish_curated_shortlist", publication)
            work("outlook.mail.sync", "mail-replay")
            work("notification.deliver", "deliver-seeded-notifications")
            assert ledger.get_application_timeline(report["hero_application_id"]) == before
            assert not graph.drafts and not graph.holds
            report["checks"] = ["24 synthetic catalog jobs across three ATS", "three durable ordered shortlists", "real event-reduced application phases", "browser evidence replay", "encrypted linked correspondence", "pending exact-action approval", "two readable private fixture PDFs", "collector-generated posting history", "empty mail delta replay"]
            write_json_atomic(root / "portfolio-receipt.json", report)
            write_json_atomic(root / "demo-status.json", report)
            print(json.dumps(report), flush=True)
            if not getattr(args, "interactive", False) and not args.serve:
                return report
            sequence = 0
            try:
                while True:
                    command_path = root / "demo-command.json"
                    if command_path.exists():
                        command = json.loads(command_path.read_text())
                        command_path.unlink()
                        sequence += 1
                        try:
                            if command["step"] == "mail":
                                work("outlook.mail.sync", "mail-" + str(sequence))
                            elif command["step"] == "execute":
                                work("outlook.actions.execute", "execute-" + str(sequence))
                            elif command["step"] == "curated":
                                mcp_call("publish_curated_shortlist", replay["publications"][-1])
                            elif command["step"] == "reply":
                                # The initial reply is already pending, so replay is deliberately a no-op.
                                assert ledger.get_action(report["ids"]["hero_reply_action"])
                            report.update(status="ready", stage=command["step"], command_id=command["id"], drafts_created=len(graph.drafts), holds_created=len(graph.holds))
                            report.pop("error", None)
                        except Exception as exc:
                            report.update(status="error", error=str(exc), command_id=command["id"])
                        write_json_atomic(root / "demo-status.json", report)
                    time.sleep(.1)
            except KeyboardInterrupt:
                return report
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)
