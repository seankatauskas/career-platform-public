#!/usr/bin/env python3
"""Offline route and security tests for the loopback job-search dashboard."""

from __future__ import annotations

import http.client
import hashlib
import json
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest.mock import patch

from job_search.contracts import (
    ActionKind,
    ActionProposalInput,
    ApplicationEventType,
    EventProposalInput,
    JobSnapshot,
    MutationContext,
    ProducerKind,
    RecommendationProvenance,
    TemporalProposalInput,
    TemporalProposalKind,
    payload_sha256,
)
from job_search.dashboard import (
    MAX_REQUEST_BYTES,
    DashboardController,
    DashboardSettings,
    make_server,
)
from job_search.notifications import DurableNotificationPublisher, NotificationIntent
from job_search.service import JobSearchLedger


def stamp(seconds: int = 0) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class FakePreferences:
    def __init__(self) -> None:
        self.calls = []

    def create_shortlist(
        self,
        options: Mapping[str, Any],
        *,
        idempotency_key: str,
        actor: str,
        excluded_job_keys=(),
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "options": dict(options),
                "idempotency_key": idempotency_key,
                "actor": actor,
                "excluded": tuple(excluded_job_keys),
            }
        )
        session = "shortlist-session-1"
        return {
            "session_id": session,
            "model": {"ready": True, "score_count": 1, "model_revision": "test"},
            "options": dict(options),
            "recommendations": [
                {
                    "ats": "ashby",
                    "id": "job-1",
                    "family_id": "family-1",
                    "title": "Platform Engineer",
                    "company": "Acme",
                    "location": "Chicago",
                    "employmentType": "FullTime",
                    "jobUrl": "https://example.test/job-1",
                    "rank": 1,
                    "policy_id": options["policy"],
                    "model_run_id": "model-1",
                    "session_id": session,
                    "impression_id": 11,
                    "semantic_score": 0.9,
                    "ranking_score": 0.8,
                }
            ],
        }


@contextmanager
def dashboard():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        preferences = FakePreferences()
        controller = DashboardController(
            ledger,
            preferences,  # type: ignore[arg-type]
            DashboardSettings(
                timezone="America/Chicago",
                outlook_configured=True,
                job_scraper_contact_configured=True,
            ),
        )
        server = make_server(controller, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, controller, ledger, preferences
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def request(
    server,
    method: str,
    path: str,
    body: Optional[Any] = None,
    headers: Optional[Mapping[str, str]] = None,
):
    port = server.server_address[1]
    sent_headers = {"Host": f"127.0.0.1:{port}", **dict(headers or {})}
    encoded = body
    if isinstance(body, (dict, list)):
        encoded = json.dumps(body).encode("utf-8")
        sent_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, body=encoded, headers=sent_headers)
    response = connection.getresponse()
    data = response.read()
    result_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, result_headers, data


def session(server):
    status, headers, body = request(server, "GET", "/api/v1/session")
    assert status == 200
    payload = json.loads(body)
    cookie = headers["set-cookie"].split(";", 1)[0]
    return cookie, payload["csrf_token"]


def post(server, path: str, body: Mapping[str, Any], cookie: str, csrf: str):
    port = server.server_address[1]
    return request(
        server,
        "POST",
        path,
        body,
        {
            "Cookie": cookie,
            "Origin": f"http://127.0.0.1:{port}",
            "X-CSRF-Token": csrf,
        },
    )


def start_direct(ledger: JobSearchLedger, suffix: str = "direct") -> str:
    result = ledger.start_application(
        JobSnapshot(
            "ashby",
            f"job-{suffix}",
            f"family-{suffix}",
            "Product Engineer",
            "Example Co",
            "example",
            f"https://example.test/job-{suffix}",
        ),
        RecommendationProvenance(),
        MutationContext(f"start-{suffix}", "user", "dashboard"),
    )
    return result["application"]["application_id"]


def test_server_is_loopback_only_and_static_page_has_every_view() -> None:
    with dashboard() as (server, _controller, _ledger, _preferences):
        assert server.server_address[0] == "127.0.0.1"
        status, headers, body = request(server, "GET", "/")
        page = body.decode("utf-8")
        assert status == 200
        for asset in ("applications-view", "shortlist-view", "review-view", "settings-view"):
            assert f"/assets/{asset}.js" in page
            asset_status, _, asset_body = request(server, "GET", f"/assets/{asset}.js")
            assert asset_status == 200 and asset_body
        nav = page.split('<nav class="sidebar" aria-label="Dashboard views">', 1)[1].split(
            "</nav>", 1
        )[0]
        for label in ("Shortlist", "Applications", "Review", "Settings"):
            assert label in nav
        assert "Outlook actions" not in nav
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "HttpOnly" in headers["set-cookie"]
        assert "SameSite=Strict" in headers["set-cookie"]
        assert "webbrowser" not in page


def test_host_origin_csrf_and_body_limit_protect_mutations() -> None:
    with dashboard() as (server, _controller, _ledger, _preferences):
        status, _headers, _body = request(
            server, "GET", "/api/v1/session", headers={"Host": "attacker.example"}
        )
        assert status == 400
        cookie, csrf = session(server)
        body = {"idempotency_key": "shortlist-1", "options": {}}
        status, _headers, _body = request(
            server,
            "POST",
            "/api/v1/shortlist",
            body,
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 403
        port = server.server_address[1]
        status, _headers, _body = request(
            server,
            "POST",
            "/api/v1/shortlist",
            body,
            {"Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"},
        )
        assert status == 403
        status, _headers, response = request(
            server,
            "POST",
            "/api/v1/shortlist",
            b"x" * (MAX_REQUEST_BYTES + 1),
            {
                "Cookie": cookie,
                "Origin": f"http://127.0.0.1:{port}",
                "X-CSRF-Token": csrf,
                "Content-Type": "application/json",
            },
        )
        assert status == 413 and json.loads(response)["error"]


def test_shortlist_start_submission_and_timeline_flow() -> None:
    with dashboard() as (server, _controller, ledger, preferences):
        cookie, csrf = session(server)
        status, _headers, body = post(
            server,
            "/api/v1/shortlist",
            {
                "idempotency_key": "shortlist-1",
                "options": {"limit": 12, "policy": "selective", "remote_only": True},
            },
            cookie,
            csrf,
        )
        shortlist = json.loads(body)
        assert status == 200 and shortlist["recommendations"][0]["impression_id"] == 11
        assert preferences.calls[0]["actor"] == "dashboard"
        assert preferences.calls[0]["options"]["max_per_company"] == 2

        start_body = {
            "idempotency_key": "application-start-1",
            "session_id": shortlist["session_id"],
            "impression_id": 11,
        }
        status, _headers, body = post(
            server, "/api/v1/applications/start", start_body, cookie, csrf
        )
        started = json.loads(body)
        assert status == 200 and started["created"] is True
        application_id = started["application"]["application_id"]
        status, _headers, replay_body = post(
            server, "/api/v1/applications/start", start_body, cookie, csrf
        )
        assert status == 200 and json.loads(replay_body) == started

        status, _headers, _body = post(
            server,
            f"/api/v1/applications/{application_id}/submitted",
            {"idempotency_key": "submitted-missing-choice", "occurred_at": stamp()},
            cookie,
            csrf,
        )
        assert status == 400
        assert (
            ledger.get_application_timeline(application_id)["application"][
                "current_phase"
            ]
            == "preparing"
        )

        status, _headers, body = post(
            server,
            f"/api/v1/applications/{application_id}/submitted",
            {
                "idempotency_key": "submitted-1",
                "occurred_at": stamp(),
                "resume_decision": "not_tracked",
            },
            cookie,
            csrf,
        )
        assert status == 200 and json.loads(body)["application"]["current_phase"] == "awaiting_confirmation"
        status, _headers, body = request(
            server,
            "GET",
            f"/api/v1/applications/{application_id}",
            headers={"Cookie": cookie},
        )
        timeline = json.loads(body)
        assert status == 200
        assert [event["event_type"] for event in timeline["events"]] == [
            "application_started",
            "submission_observed",
        ]
        assert len(ledger.list_outbox()) == 1


def test_persisted_submission_envelope_replays_after_server_clock_moves() -> None:
    with dashboard() as (server, _controller, ledger, _preferences):
        cookie, csrf = session(server)
        application_id = start_direct(ledger, "delayed-submit-replay")
        envelope = {
            "idempotency_key": "persisted-submission-command",
            "occurred_at": "2026-09-02T12:34:56Z",
            "resume_decision": "not_tracked",
        }
        first_status, _headers, first_body = post(
            server,
            f"/api/v1/applications/{application_id}/submitted",
            envelope,
            cookie,
            csrf,
        )
        with patch(
            "job_search.dashboard.utc_now",
            return_value="2026-09-03T12:34:56Z",
        ):
            replay_status, _headers, replay_body = post(
                server,
                f"/api/v1/applications/{application_id}/submitted",
                envelope,
                cookie,
                csrf,
            )
        assert first_status == replay_status == 200
        assert json.loads(first_body) == json.loads(replay_body)
        timeline = ledger.get_application_timeline(application_id)
        submissions = [
            event
            for event in timeline["events"]
            if event["event_type"] == "submission_observed"
        ]
        assert len(submissions) == 1
        assert submissions[0]["occurred_at"] == envelope["occurred_at"]
        assert submissions[0]["payload"] == {
            "resume": {"decision": "not_tracked"}
        }


def test_attention_review_interviews_and_outlook_action_approval() -> None:
    with dashboard() as (server, _controller, ledger, _preferences):
        cookie, csrf = session(server)
        application_id = start_direct(ledger, "review")
        evidence_quote = "Please choose an interview time"
        evidence_id = ledger.record_mail_evidence(
            {
                "account_id": "outlook-personal",
                "immutable_message_id": "message-review",
                "sender": "recruiter@example.test",
                "subject": "Interview",
                "received_at": stamp(),
                "body_sha256": "c" * 64,
                "excerpt": evidence_quote,
            },
            MutationContext("create-evidence", "system", "outlook_sync"),
        )["evidence"]["evidence_id"]
        proposal = ledger.create_event_proposal(
            EventProposalInput(
                evidence_id,
                application_id,
                ApplicationEventType.INTERVIEW_REQUESTED,
                ProducerKind.MODEL,
                "model-v1",
                0.99,
                [application_id],
                evidence_quote,
                0,
                len(evidence_quote),
                {"occurred_at": stamp()},
                "proposal-review",
            ),
            MutationContext("create-proposal", "model", "classifier"),
        )["proposal"]
        status, _headers, body = request(
            server, "GET", "/api/v1/attention", headers={"Cookie": cookie}
        )
        items = json.loads(body)["items"]
        assert status == 200 and items[0]["evidence_quote"].startswith("Please")
        status, _headers, body = post(
            server,
            f"/api/v1/proposals/{proposal['proposal_id']}/decision",
            {
                "idempotency_key": "review-accept",
                "decision": "accepted",
                "selected_application_id": application_id,
                "reason": "verified",
            },
            cookie,
            csrf,
        )
        assert status == 200 and json.loads(body)["event"]["event_type"] == "interview_requested"
        status, _headers, body = request(
            server, "GET", "/api/v1/interviews", headers={"Cookie": cookie}
        )
        assert status == 200 and json.loads(body)["applications"][0]["application_id"] == application_id

        action_payload = {"message_id": "mail-1", "body": "Tuesday works."}
        action = ledger.create_action_proposal(
            ActionProposalInput(
                ActionKind.OUTLOOK_REPLY_DRAFT,
                application_id,
                "personal-outlook",
                action_payload,
                stamp(3600),
            ),
            MutationContext("create-action", "system", "scheduling"),
        )["action"]
        status, _headers, body = request(
            server, "GET", "/api/v1/actions", headers={"Cookie": cookie}
        )
        assert status == 200 and json.loads(body)["actions"][0]["payload"] == action_payload
        status, _headers, body = post(
            server,
            f"/api/v1/actions/{action['action_id']}/decision",
            {
                "idempotency_key": "approve-action",
                "approve": True,
                "payload_sha256": payload_sha256(action_payload),
            },
            cookie,
            csrf,
        )
        assert status == 200 and json.loads(body)["decision"] == "approve"


def test_temporal_proposals_are_visible_and_user_can_accept_or_reject() -> None:
    with dashboard() as (server, _controller, ledger, _preferences):
        cookie, csrf = session(server)
        application_id = start_direct(ledger, "temporal-review")
        source = "submit by noon"
        source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
        archive = ledger.put_mail_archive(
            {
                "account_id": "outlook-personal",
                "immutable_message_id": "temporal-dashboard-mail",
                "key_id": "key-1",
                "nonce": b"N" * 12,
                "ciphertext": b"C" * 16,
                "aad_sha256": "a" * 64,
                "sanitized_sha256": source_sha,
                "sanitized_chars": len(source),
                "truncated": False,
            },
            MutationContext("dashboard-temporal-archive", "system", "secure_mail"),
        )["archive"]

        def proposal(suffix: str, due_at: str):
            return ledger.create_temporal_proposal(
                TemporalProposalInput(
                    str(archive["archive_id"]),
                    None,
                    application_id,
                    TemporalProposalKind.DEADLINE,
                    None,
                    None,
                    due_at,
                    "America/Chicago",
                    0.91,
                    source,
                    0,
                    len(source),
                    source_sha,
                    "temporal-v1",
                    "dashboard-temporal-" + suffix,
                ),
                MutationContext(
                    "dashboard-temporal-create-" + suffix,
                    "model",
                    "temporal_extractor",
                ),
            )["proposal"]

        accepted = proposal("accepted", stamp(3600))
        rejected = proposal("rejected", stamp(7200))
        status, _headers, body = request(
            server, "GET", "/api/v1/attention", headers={"Cookie": cookie}
        )
        temporal = [
            item for item in json.loads(body)["items"]
            if item["kind"] == "temporal_proposal"
        ]
        assert status == 200 and len(temporal) == 2
        assert temporal[0]["evidence_quote"] == source
        assert temporal[0]["time_zone"] == "America/Chicago"
        assert temporal[0]["employer"] == "Example Co"

        status, _headers, body = post(
            server,
            f"/api/v1/temporal-proposals/{accepted['temporal_proposal_id']}/decision",
            {
                "idempotency_key": "dashboard-temporal-accept",
                "decision": "accepted",
                "reason": "confirmed",
            },
            cookie,
            csrf,
        )
        accepted_result = json.loads(body)
        assert status == 200 and accepted_result["status"] == "accepted"
        assert accepted_result["reminders"][0]["kind"] == "deadline"

        status, _headers, body = post(
            server,
            f"/api/v1/temporal-proposals/{rejected['temporal_proposal_id']}/decision",
            {
                "idempotency_key": "dashboard-temporal-reject",
                "decision": "rejected",
                "reason": "incorrect extraction",
            },
            cookie,
            csrf,
        )
        assert status == 200 and json.loads(body)["status"] == "rejected"
        status, _headers, body = request(
            server, "GET", "/api/v1/attention", headers={"Cookie": cookie}
        )
        assert status == 200
        assert not any(
            item["kind"] == "temporal_proposal" for item in json.loads(body)["items"]
        )


def test_health_settings_and_read_only_lists_are_safe() -> None:
    with dashboard() as (server, _controller, ledger, _preferences):
        cookie, _csrf = session(server)
        start_direct(ledger, "health")
        status, _headers, body = request(
            server, "GET", "/api/v1/applications", headers={"Cookie": cookie}
        )
        assert status == 200 and len(json.loads(body)["applications"]) == 1
        status, _headers, body = request(
            server, "GET", "/api/v1/health", headers={"Cookie": cookie}
        )
        health = json.loads(body)
        assert status == 200 and health["status"] == "healthy"
        assert health["applications"]["preparing"] == 1
        status, _headers, body = request(
            server, "GET", "/api/v1/settings", headers={"Cookie": cookie}
        )
        settings = json.loads(body)
        assert status == 200
        assert settings == {
        "resume_mode": "tailored",
            "ats_refresh_hours": 4,
            "job_scraper_contact_configured": True,
            "mail_folder": "inbox",
            "outlook_configured": True,
            "timezone": "America/Chicago",
        }
        assert not any("path" in key or "token" in key for key in settings)


def test_ops_api_reports_and_cancels_reminders_without_notification_bodies() -> None:
    with dashboard() as (server, _controller, ledger, _preferences):
        application_id = start_direct(ledger, "ops")
        reminder = ledger.create_reminder(
            {
                "application_id": application_id,
                "due_at": stamp(3600),
                "note": "Follow up",
            },
            MutationContext("reminder-ops", "hermes", "hermes_reminder"),
        )["reminder"]
        DurableNotificationPublisher(ledger).publish(
            NotificationIntent(
                "system.degraded",
                "ops-notification-1",
                "System attention",
                "Private notification body",
                context={"lane": "ops"},
            )
        )
        cookie, csrf = session(server)
        status, _headers, body = request(
            server, "GET", "/api/v1/ops", headers={"Cookie": cookie}
        )
        ops = json.loads(body)
        assert status == 200 and ops["status"] == "healthy"
        assert ops["reminders"]["counts"]["scheduled"] == 1
        assert ops["notifications"]["counts"]["pending"] == 1
        assert "body" not in ops["notifications"]["items"][0]
        status, _headers, body = post(
            server,
            f'/api/v1/reminders/{reminder["reminder_id"]}/cancel',
            {"idempotency_key": "dashboard-cancel-reminder"},
            cookie,
            csrf,
        )
        assert status == 200 and json.loads(body)["cancelled"] is True


def test_user_can_reconcile_an_uncertain_outlook_action() -> None:
    with dashboard() as (server, _controller, ledger, _preferences):
        cookie, csrf = session(server)
        application_id = start_direct(ledger, "reconcile")
        action_payload = {"message_id": "mail-reconcile", "body": "Approved body"}
        action = ledger.create_action_proposal(
            ActionProposalInput(
                ActionKind.OUTLOOK_REPLY_DRAFT,
                application_id,
                "personal-outlook",
                action_payload,
                stamp(3600),
            ),
            MutationContext("create-reconcile-action", "system", "scheduling"),
        )["action"]
        ledger.decide_action(
            action["action_id"],
            True,
            payload_sha256(action_payload),
            MutationContext("approve-reconcile-action", "user", "dashboard"),
        )
        claimed = ledger.claim_action(action["action_id"])
        ledger.complete_action(claimed["execution"]["execution_id"], "uncertain")

        status, _headers, body = post(
            server,
            f"/api/v1/actions/{action['action_id']}/reconcile",
            {
                "idempotency_key": "reconcile-created",
                "resolution": "created",
                "remote_id": "",
            },
            cookie,
            csrf,
        )
        result = json.loads(body)
        assert status == 200 and result["action_status"] == "executed"
        assert ledger.get_action(action["action_id"])["status"] == "executed"


def test_mail_failure_review_is_scoped_idempotent_and_requires_csrf():
    from job_search.outlook.state import SQLiteOutlookState
    from tests.test_job_search_sync import change
    from job_search.db import connect
    with dashboard() as (server, _, ledger, _preferences):
        state = SQLiteOutlookState(ledger.store.db_path)
        for version in (1, 2):
            state.stage_changes("personal", "inbox", [change("failed-mail")], query_version=version)
            state.mark_message("personal", "inbox", "failed-mail", "failed", "evidence span mismatch", query_version=version)
        cookie, csrf = session(server)
        status, _, body = request(server, "GET", "/api/v1/attention", headers={"Cookie":cookie})
        items = json.loads(body)['items']
        assert status == 200 and {item['query_version'] for item in items} == {1,2}
        payload = dict(account_id="personal", folder_ref="inbox", message_id="failed-mail",
                       query_version=2, action="retry", idempotency_key="retry-failure")
        path = "/api/v1/mail/failures/resolve"
        assert post(server, path, payload, cookie, "wrong-csrf")[0] == 403
        first = post(server, path, payload, cookie, csrf)
        assert first[0] == 200 and json.loads(first[2])['status'] == 'pending'
        assert post(server, path, payload, cookie, csrf)[2] == first[2]
        assert post(server, path, {**payload, 'idempotency_key':'retry-new'}, cookie, csrf)[0] == 409
        assert post(server, path, {**payload, 'query_version':1, 'action':'dismiss',
                                  'idempotency_key':'dismiss-failure'}, cookie, csrf)[0] == 200
        with connect(ledger.store.db_path) as con:
            assert [tuple(r) for r in con.execute('SELECT query_version,processing_status FROM outlook_message_stage ORDER BY query_version')] == [(1,'ignored'),(2,'pending')]
            assert con.execute('SELECT count(*) FROM application_events').fetchone()[0] == 0
        assert post(server, path, {**payload,'action':'send','idempotency_key':'invalid'}, cookie, csrf)[0] == 400
        assert ledger.list_attention_items() == []


def test_scan_endpoint_requires_csrf_and_only_accepts_fixed_scan() -> None:
    from dataclasses import replace
    from .test_job_search_scanning import fixture
    from job_search.db import connect
    with dashboard() as (server, controller, ledger, preferences), fixture() as (config, _store):
        controller.automation_config = replace(config, application_db=ledger.store.db_path)
        from job_search.scheduler import seed_default_schedules
        seed_default_schedules(ledger.store.db_path, datetime.now(timezone.utc), config.environment({}))
        cookie, csrf = session(server)
        path = '/api/v1/ops/scan'
        payload = {'idempotency_key': 'manual-scan-http'}
        assert post(server, path, payload, cookie, 'invalid')[0] == 403
        assert post(server, path, {**payload, 'concurrency': 99}, cookie, csrf)[0] == 400
        assert post(server, path, {**payload, 'mode': 'all'}, cookie, csrf)[0] == 400
        first = post(server, path, payload, cookie, csrf)
        assert first[0] == 202
        assert post(server, path, payload, cookie, csrf)[2] == first[2]
        status, _headers, body = request(server, 'GET', '/api/v1/ops', headers={'Cookie': cookie})
        assert status == 200
        report = json.loads(body)
        assert report['collection']['active']['status'] == 'queued'
        assert report['ranking']['available'] is False
        status, _headers, body = request(server, 'GET', '/api/v1/ops/pipeline', headers={'Cookie': cookie})
        assert status == 200 and json.loads(body) == {k: report[k] for k in ('collection', 'ranking')}
        with connect(ledger.store.db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM work_items WHERE task_kind='ats.new_only'").fetchone()[0] == 1
        controller.demo_mode = True
        assert controller.pipeline_view()['collection']['available'] is False
        assert 'local preview' in controller.pipeline_view()['collection']['reason']
        assert post(server, path, {'idempotency_key':'demo-scan'}, cookie, csrf)[0] == 400


def test_pipeline_progress_get_is_passive_and_keeps_real_attempt_counts() -> None:
    from .test_job_search_scanning import progress_fixture, save_progress
    with dashboard() as (server, controller, _ledger, _preferences), progress_fixture() as config:
        controller.automation_config = config
        save_progress(config)
        cookie, _csrf = session(server)
        paths = (config.application_db, config.jobs_db, config.preference_db, config.proxy_db)
        before = {path: path.read_bytes() for path in paths}
        with patch('job_search.ranking.refresh.refresh_policies', side_effect=AssertionError('GET started ranking')), \
                patch('job_search.scanning.request_scan', side_effect=AssertionError('GET started collection')):
            for _ in range(2):
                status, _headers, body = request(server, 'GET', '/api/v1/ops/pipeline', headers={'Cookie': cookie})
                ranking = json.loads(body)['ranking']
                assert status == 200 and ranking['available']
                assert ranking['current_pass']['checked_families'] == 1
                assert ranking['current_pass']['policies']['selective']['recomputed_families'] == 0
        assert {path: path.read_bytes() for path in paths} == before


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search dashboard tests)")


if __name__ == "__main__":
    main()
