#!/usr/bin/env python3
"""Offline cloud autofill contracts through the existing private proxy boundary."""
from __future__ import annotations

import json
import tempfile
import threading
from contextlib import contextmanager

from job_search.autofill import AutofillBroker, SUBMISSION_TTL_SECONDS
from job_search.dashboard import DashboardController, make_server
from tests.test_job_search_autofill import EXTENSION_ORIGIN, descriptors, make_ledger, profile, start_application
from tests.test_job_search_dashboard import FakePreferences, request
from tests.test_job_search_dashboard_proxy import LOGIN, ORIGIN, PROXY_HEADERS

PAGE = "https://job-boards.greenhouse.io/acme/jobs/job-1"
EXTENSION_HEADERS = {**PROXY_HEADERS, "Origin": EXTENSION_ORIGIN}


@contextmanager
def cloud():
    with tempfile.TemporaryDirectory() as directory:
        ledger = make_ledger(directory)
        application = start_application(ledger)
        now = [1000.0]
        broker = AutofillBroker(ledger, profile(), clock=lambda: now[0])
        server = make_server(DashboardController(ledger, FakePreferences(), autofill=broker), 0,
                             https_origin=ORIGIN, allowed_tailscale_login=LOGIN)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, headers, body = request(server, "GET", "/api/v1/session", headers=PROXY_HEADERS)
            assert status == 200
            session = {**PROXY_HEADERS, "Cookie": headers["set-cookie"].split(";", 1)[0],
                       "Origin": ORIGIN, "X-CSRF-Token": json.loads(body)["csrf_token"]}
            status, _, body = request(server, "POST", "/api/v1/autofill/handoffs",
                                      {"application_id": application, "idempotency_key": "issue-cloud"}, session)
            assert status == 200
            yield server, ledger, application, now, json.loads(body)["pairing_code"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def exchange(server, code, headers=None, **changes):
    return request(server, "POST", "/api/v1/autofill/exchange",
                   {"pairing_code": code, "ats": "greenhouse", "page_url": PAGE,
                    "fields": descriptors(), **changes}, headers or EXTENSION_HEADERS)


def test_cloud_pair_capture_manual_mark_and_duplicate_retry():
    with cloud() as (server, ledger, application, _now, code):
        status, headers, _ = request(server, "OPTIONS", "/api/v1/autofill/exchange",
                                      headers={**EXTENSION_HEADERS, "Access-Control-Request-Method": "POST"})
        assert status == 204 and headers["access-control-allow-origin"] == EXTENSION_ORIGIN
        status, headers, body = exchange(server, code)
        assert status == 200 and "set-cookie" not in headers
        assert headers["access-control-allow-origin"] == EXTENSION_ORIGIN
        receipt = json.loads(body)["submission_token"]
        assert exchange(server, code)[0] == 400
        payload = {"submission_token": receipt, "ats": "greenhouse", "page_url": PAGE}
        assert request(server, "POST", "/api/v1/autofill/capture",
                       {**payload, "answers": [{"field_id": "why", "value": "Synthetic answer"}]}, EXTENSION_HEADERS)[0] == 200
        assert ledger.get_application_timeline(application)["application"]["current_phase"] == "preparing"
        submission = {**payload, "idempotency_key": "manual-submission", "resume_decision": "not_tracked"}
        first = request(server, "POST", "/api/v1/autofill/submitted", submission, EXTENSION_HEADERS)
        second = request(server, "POST", "/api/v1/autofill/submitted", submission, EXTENSION_HEADERS)
        assert first[0] == second[0] == 200 and first[2] == second[2]
        timeline = ledger.get_application_timeline(application)
        assert timeline["application"]["current_phase"] == "awaiting_confirmation"
        assert sum(event["payload"].get("observed_by") == "manual_extension_action" for event in timeline["events"]) == 1


def test_cloud_identity_and_origin_cannot_be_bypassed():
    with cloud() as (server, _ledger, _application, _now, code):
        for changes, expected in (
            ({"Tailscale-User-Login": "other@example.test"}, 400),
            ({"Tailscale-User-Login": ""}, 400),
            ({"Host": f"127.0.0.1:{server.server_port}"}, 400),
            ({"X-Forwarded-Proto": "http"}, 400),
            ({"Forwarded": "host=career.example-tailnet.ts.net"}, 400),
            ({"Origin": "https://attacker.example"}, 403),
        ):
            assert exchange(server, code, {**EXTENSION_HEADERS, **changes})[0] == expected
        assert exchange(server, code)[0] == 200  # Refused requests did not consume the code.


def test_cloud_receipts_remain_scoped_and_expire():
    with cloud() as (server, _ledger, _application, now, code):
        assert exchange(server, code, page_url=PAGE.replace("job-1", "job-2"))[0] == 400
        assert exchange(server, code, ats="ashby")[0] == 400
        status, _, body = exchange(server, code)
        assert status == 200
        payload = {"submission_token": json.loads(body)["submission_token"], "ats": "greenhouse", "page_url": PAGE, "answers": []}
        for changes in ({"Origin": "chrome-extension://" + "b" * 32}, {"Tailscale-User-Login": "other@example.test"}):
            assert request(server, "POST", "/api/v1/autofill/capture", payload, {**EXTENSION_HEADERS, **changes})[0] == 400
        now[0] += SUBMISSION_TTL_SECONDS + 1
        status, _, body = request(server, "POST", "/api/v1/autofill/capture", payload, EXTENSION_HEADERS)
        assert status == 400 and "expired" in json.loads(body)["error"]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} private HTTPS extension tests)")
