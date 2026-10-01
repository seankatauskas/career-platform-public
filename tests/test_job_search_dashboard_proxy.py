#!/usr/bin/env python3
"""Offline security checks for the optional Tailscale Serve dashboard boundary."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from job_search.dashboard import DashboardController, make_server
from job_search.dashboard_access import DashboardAccess
from job_search.runtime import RuntimeConfigV1
from job_search.service import JobSearchLedger
from job_search.system import make_dashboard_host
from tests.test_job_search_dashboard import FakePreferences, request


ORIGIN = "https://career.example-tailnet.ts.net"
LOGIN = "owner@example.test"
PROXY_HEADERS = {
    "Host": "career.example-tailnet.ts.net",
    "Tailscale-User-Login": LOGIN,
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": "100.100.100.100",
}


@contextmanager
def proxy_dashboard():
    with tempfile.TemporaryDirectory() as directory:
        controller = DashboardController(
            JobSearchLedger(Path(directory) / "applications.db"), FakePreferences()
        )
        server = make_server(
            controller, 0, https_origin=ORIGIN, allowed_tailscale_login=LOGIN
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_runtime_requires_paired_strict_tailscale_origin_and_owner() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = RuntimeConfigV1.defaults(Path(directory))
        assert base.dashboard_https_origin == ""
        assert base.dashboard_allowed_tailscale_login == ""
        configured = RuntimeConfigV1.from_mapping(
            {"dashboard_https_origin": ORIGIN, "dashboard_allowed_tailscale_login": LOGIN},
            default_root=Path(directory),
        )
        configured.validate()
        assert DashboardAccess(ORIGIN + ":443", LOGIN).https_origin == ORIGIN
        for origin, login in (
            (ORIGIN, ""), ("", LOGIN), ("http://career.example-tailnet.ts.net", LOGIN),
            (ORIGIN + "/", LOGIN), (ORIGIN + "?a=b", LOGIN),
            (ORIGIN + "#fragment", LOGIN), (ORIGIN + ":8080", LOGIN),
            ("https://owner@career.example-tailnet.ts.net", LOGIN),
            ("https://*.example-tailnet.ts.net", LOGIN),
            ("https://example.test", LOGIN), ("https://localhost", LOGIN),
            (ORIGIN, LOGIN + ",attacker@example.test"), (ORIGIN, "owner\nforged"),
        ):
            try:
                replace(base, dashboard_https_origin=origin,
                        dashboard_allowed_tailscale_login=login).validate()
            except ValueError:
                pass
            else:
                raise AssertionError("invalid dashboard proxy configuration accepted")


def test_proxy_requires_exact_host_owner_and_rejects_forwarding_spoofs() -> None:
    with proxy_dashboard() as server:
        assert server.server_address[0] == "127.0.0.1"
        for changes in (
            {"Tailscale-User-Login": "other@example.test"},
            {"Tailscale-User-Login": ""},
            {"Host": "another.example-tailnet.ts.net"},
            {"Host": f"localhost:{server.server_port}"},
            {"Host": f"127.0.0.1:{server.server_port}"},
            {"X-Forwarded-Host": "attacker.example"},
            {"X-Forwarded-Proto": "http"},
            {"Forwarded": "host=career.example-tailnet.ts.net;proto=https"},
        ):
            status, headers, _ = request(
                server, "GET", "/api/v1/session", headers={**PROXY_HEADERS, **changes}
            )
            assert status == 400, changes
            assert "set-cookie" not in headers
        # Tagged devices and Funnel have no owner identity. An external Host or
        # forwarding headers must not let them fall back to trusted local mode.
        for headers in (
            {"Host": PROXY_HEADERS["Host"]},
            {"X-Forwarded-For": "100.100.100.100"},
            {"Tailscale-App-Capabilities": "{}"},
        ):
            assert request(server, "GET", "/api/v1/session", headers=headers)[0] == 400


def test_https_cookies_and_csrf_are_bound_to_proxy_audience() -> None:
    with proxy_dashboard() as server:
        status, headers, body = request(server, "GET", "/api/v1/session", headers=PROXY_HEADERS)
        assert status == 200
        cookie = headers["set-cookie"].split(";", 1)[0]
        csrf = json.loads(body)["csrf_token"]
        for attr in ("HttpOnly", "SameSite=Strict", "Secure", "Max-Age=28800"):
            assert attr in headers["set-cookie"]
        assert "access-control-allow-origin" not in headers
        # Ordinary local health checks, dashboard access, and owner-only local
        # tunnels still work, but cannot reuse a proxy session or CSRF token.
        status, local_headers, local_body = request(
            server, "GET", "/api/v1/session", headers={"Cookie": cookie}
        )
        assert status == 200
        local_cookie = local_headers["set-cookie"].split(";", 1)[0]
        assert local_cookie != cookie and "Secure" not in local_headers["set-cookie"]
        local_csrf = json.loads(local_body)["csrf_token"]
        assert local_csrf != csrf
        body = {"idempotency_key": "proxy-shortlist", "options": {}}
        valid = {**PROXY_HEADERS, "Cookie": cookie, "X-CSRF-Token": csrf, "Origin": ORIGIN}
        for changes in (
            {"Origin": "https://attacker.example"},
            {"Origin": f"http://127.0.0.1:{server.server_port}"},
            {"Origin": ""}, {"X-CSRF-Token": "incorrect"},
            {"Cookie": local_cookie, "X-CSRF-Token": local_csrf},
        ):
            status, _, _ = request(server, "POST", "/api/v1/shortlist", body,
                                   {**valid, **changes})
            assert status == 403, changes
        status, _, _ = request(server, "POST", "/api/v1/shortlist", body, valid)
        assert status == 200
        assert request(server, "POST", "/api/v1/shortlist", body, {
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "Cookie": cookie, "X-CSRF-Token": csrf,
        })[0] == 403
        assert request(server, "GET", "/api/v1/health")[0] == 200
        assert request(server, "GET", "/api/v1/session", headers={
            **PROXY_HEADERS, "Origin": "https://attacker.example"
        })[0] == 403


def test_duplicate_headers_cannot_bypass_proxy_validation() -> None:
    with proxy_dashboard() as server:
        for duplicated, value in (
            ("Host", PROXY_HEADERS["Host"]), ("Tailscale-User-Login", LOGIN),
            ("Origin", ORIGIN), ("X-Forwarded-Proto", "https"),
        ):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.putrequest("GET", "/api/v1/session", skip_host=True)
            for key, header_value in {**PROXY_HEADERS, "Origin": ORIGIN}.items():
                connection.putheader(key, header_value)
            connection.putheader(duplicated, value)
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 400, duplicated
            response.read()
            connection.close()


def test_local_extension_preflight_and_tokens_still_required() -> None:
    with proxy_dashboard() as server:
        origin = "chrome-extension://" + "a" * 32
        path = "/api/v1/autofill/exchange"
        status, headers, _ = request(server, "OPTIONS", path, headers={"Origin": origin})
        assert status == 204
        assert headers["access-control-allow-origin"] == origin
        status, _, _ = request(server, "POST", path, {}, {"Origin": origin})
        assert status == 400  # Existing extension contract still requires a pairing token.
        status, _, _ = request(server, "OPTIONS", path, headers={"Origin": "https://attacker.example"})
        assert status == 403


def test_system_host_passes_runtime_policy_to_loopback_listener() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = replace(RuntimeConfigV1.defaults(Path(directory)), dashboard_port=0,
                         dashboard_https_origin=ORIGIN,
                         dashboard_allowed_tailscale_login=LOGIN)
        controller = DashboardController(JobSearchLedger(config.application_db), FakePreferences())
        with patch("job_search.system.build_dashboard_controller", return_value=controller):
            server = make_dashboard_host(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert request(server, "GET", "/api/v1/session", headers=PROXY_HEADERS)[0] == 200
            assert request(server, "GET", "/api/v1/session", headers={
                **PROXY_HEADERS, "Tailscale-User-Login": "other@example.test"
            })[0] == 400
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} dashboard proxy tests)")
