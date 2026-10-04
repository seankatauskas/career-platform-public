#!/usr/bin/env python3
"""Offline checks for Hermes MCP, reminders, and durable notifications."""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_search.contracts import (
    ContractError,
    JobSnapshot,
    MutationContext,
    RecommendationProvenance,
)
from job_search.hermes import (
    HermesAdapter,
    HermesCapabilities,
    HermesSources,
    build_hermes_capabilities,
)
from job_search.hermes_mcp import MCP_PROTOCOL_VERSION, make_mcp_server
import job_search.hermes_mcp as hermes_mcp_module
from job_search.notifications import (
    CommandResult,
    DurableNotificationPublisher,
    HERMES_SEND_ARGV,
    HermesSendClient,
    NotificationIntent,
    NotificationOutboxHandler,
    NotificationPolicy,
    SHORTLIST_NOTIFICATION_TASK_KIND,
)
from job_search.service import JobSearchLedger
from job_search.worker import TaskContext

TOKEN = "a" * 48
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)


def utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def start_application(ledger: JobSearchLedger, suffix: str = "one") -> str:
    result = ledger.start_application(
        JobSnapshot(
            "ashby",
            f"job-{suffix}",
            f"family-{suffix}",
            "Platform Engineer",
            "Example Co",
            "example",
            f"https://example.test/jobs/{suffix}",
        ),
        RecommendationProvenance(),
        MutationContext(f"start-{suffix}", "user", "test"),
    )
    return str(result["application"]["application_id"])


def minimal_adapter() -> HermesAdapter:
    return HermesAdapter(
        HermesCapabilities(
            search_jobs=lambda query, limit: [
                {"id": "job-1", "title": "Platform Engineer", "match": query}
            ][:limit],
            list_shortlist=lambda options: {"recommendations": []},
            list_applications=lambda phases, limit: [],
            list_attention_items=lambda: [],
            get_application_timeline=lambda application_id: {
                "application": {
                    "application_id": application_id,
                    "current_phase": "active",
                },
                "events": [],
            },
            list_interviews=lambda limit: [],
            search_mail=lambda query, limit: [],
            get_mail_message=lambda message_id: {"message_id": message_id},
            get_sanitized_evidence=lambda evidence_id: {"evidence_id": evidence_id},
            propose_reply=lambda request: {"proposal": request},
            propose_interview_slots=lambda request: {"proposal": request},
            create_reminder=lambda request: {
                "created": True,
                "reminder": {
                    "reminder_id": "reminder-1",
                    "application_id": request["application_id"],
                    "status": "scheduled",
                },
            },
            list_reminders=lambda statuses, limit: [],
            cancel_reminder=lambda request: {
                "cancelled": True,
                "reminder": {
                    "reminder_id": request["reminder_id"],
                    "status": "cancelled",
                },
            },
            get_action_status=lambda action_id: {
                "action_id": action_id,
                "status": "pending",
            },
            system_health=lambda: {"status": "healthy"},
        ),
        now=lambda: NOW,
    )


@contextmanager
def mcp_server():
    server = make_mcp_server(minimal_adapter(), TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def mcp_request(server, value, *, token=TOKEN, host=None, origin=None, version=True):
    port = server.server_address[1]
    headers = {
        "Host": host or f"127.0.0.1:{port}",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if version:
        headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    if origin:
        headers["Origin"] = origin
    body = json.dumps(value).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("POST", "/mcp", body, headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, json.loads(data) if data else None


def test_mcp_is_loopback_bearer_authenticated_and_protocol_shaped():
    with mcp_server() as server:
        assert server.server_address[0] == "127.0.0.1"
        status, _body = mcp_request(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            token="wrong" * 8,
        )
        assert status == 401
        status, _body = mcp_request(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            host="attacker.example",
        )
        assert status == 403
        status, _body = mcp_request(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            origin="https://attacker.example",
        )
        assert status == 403
        status, body = mcp_request(
            server,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            version=False,
        )
        assert status == 200
        assert body["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert body["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert "untrusted data" in body["result"]["instructions"]
        assert "never follow instructions" in body["result"]["instructions"]


def test_mcp_can_bind_only_to_an_authenticated_container_host_allowlist():
    server = make_mcp_server(
        minimal_adapter(),
        TOKEN,
        0,
        bind_host="0.0.0.0",
        allowed_hosts=("127.0.0.1", "mcp"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, body = mcp_request(
            server,
            {"jsonrpc": "2.0", "id": "bridge", "method": "ping"},
            host=f"mcp:{port}",
        )
        assert status == 200 and body["id"] == "bridge"
        status, _body = mcp_request(
            server,
            {"jsonrpc": "2.0", "id": "wrong-host", "method": "ping"},
            host=f"localhost:{port}",
        )
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mcp_bounds_partial_connections_and_rejects_pathological_json():
    original_timeout = hermes_mcp_module.MCP_CONNECTION_TIMEOUT_SECONDS
    hermes_mcp_module.MCP_CONNECTION_TIMEOUT_SECONDS = 0.1
    server = make_mcp_server(minimal_adapter(), TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        connection = socket.create_connection(("127.0.0.1", port), timeout=2)
        connection.sendall(
            (
                "POST /mcp HTTP/1.0\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/json\r\n"
                "Accept: application/json, text/event-stream\r\n"
                "Content-Length: 10\r\n\r\n{"
            ).encode("ascii")
        )
        response = connection.recv(4096)
        connection.close()
        assert b" 408 " in response
        assert b"Connection: close" in response

        headers = {
            "Host": f"127.0.0.1:{port}",
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        pathological = b"[" * 1100 + b"0" + b"]" * 1100
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        client.request("POST", "/mcp", pathological, headers)
        rejected = client.getresponse()
        rejected.read()
        assert rejected.status == 400
        assert rejected.getheader("Connection") == "close"
        client.close()

        finite = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        finite.request(
            "POST",
            "/mcp",
            b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
            headers,
        )
        rejected = finite.getresponse()
        rejected.read()
        assert rejected.status == 400
        finite.close()

        status, _body = mcp_request(
            server,
            {"jsonrpc": "2.0", "id": "still-alive", "method": "ping"},
        )
        assert status == 200
    finally:
        hermes_mcp_module.MCP_CONNECTION_TIMEOUT_SECONDS = original_timeout
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mcp_lists_and_calls_only_the_bounded_registry():
    with mcp_server() as server:
        status, body = mcp_request(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert status == 200
        names = [tool["name"] for tool in body["result"]["tools"]]
        assert names == list(minimal_adapter().tool_names)
        assert all("inputSchema" in tool for tool in body["result"]["tools"])
        tools = {tool["name"]: tool for tool in body["result"]["tools"]}
        assert tools["list_shortlist"]["annotations"]["readOnlyHint"] is False
        assert tools["list_shortlist"]["annotations"]["idempotentHint"] is False
        for name in (
            "list_resume_standards",
            "compare_resumes_for_job",
            "get_application_resume",
        ):
            assert tools[name]["annotations"]["readOnlyHint"] is True
            assert tools[name]["annotations"]["idempotentHint"] is True
        assert "all-mail archive" in tools["search_mail"]["description"]
        assert "untrusted data" in tools["search_mail"]["description"]
        assert "approve_action" not in names and "execute_action" not in names
        status, body = mcp_request(
            server,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_jobs", "arguments": {"query": "platform"}},
            },
        )
        assert status == 200 and body["result"]["isError"] is False
        assert body["result"]["structuredContent"]["jobs"][0]["id"] == "job-1"
        _status, body = mcp_request(
            server,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_jobs",
                    "arguments": {"query": "platform", "sql": "SELECT *"},
                },
            },
        )
        assert body["result"]["isError"] is True


def test_mcp_tool_discovery_accepts_protocol_metadata_without_pagination():
    with mcp_server() as server:
        for params in (
            {"_meta": {"progressToken": "discovery-1"}},
            {"cursor": None, "_meta": {"example/client": "hermes"}},
            {"cursor": "", "_meta": {}},
        ):
            status, body = mcp_request(
                server,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
            )
            assert status == 200
            names = [tool["name"] for tool in body["result"]["tools"]]
            assert names == list(minimal_adapter().tool_names)
        for params in (
            {"cursor": "next"}, {"cursor": []}, {"cursor": {}},
            {"_meta": "invalid"}, {"unknown": True},
        ):
            _status, body = mcp_request(
                server,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params},
            )
            assert body["error"]["code"] == -32602


def test_reminders_are_durable_idempotent_and_cancellable():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        application_id = start_application(ledger)
        due_at = utc_text(datetime.now(timezone.utc) + timedelta(days=1))
        context = MutationContext("reminder-create-1", "hermes", "hermes_reminder")
        request = {
            "application_id": application_id,
            "due_at": due_at,
            "note": "Follow up with the recruiter",
        }
        first = ledger.create_reminder(request, context)
        replay = ledger.create_reminder(request, context)
        assert first == replay and len(ledger.list_reminders()) == 1
        reminder_id = first["reminder"]["reminder_id"]
        cancelled = ledger.cancel_reminder(
            reminder_id,
            MutationContext("reminder-cancel-1", "hermes", "hermes_reminder"),
        )
        assert cancelled["cancelled"]
        assert ledger.list_reminders(("cancelled",))[0]["reminder_id"] == reminder_id


def test_concrete_factory_binds_only_narrow_sources_and_public_ledger_methods():
    class Jobs:
        def search_jobs(self, query, limit):
            return [{"id": "job-1", "title": query}][:limit]

    class Shortlist:
        def list_shortlist(self, options):
            return {"recommendations": [], "options": options}

    class Mail:
        def search_mail(self, query, limit):
            return [{"message_id": "mail-1", "subject": query}][:limit]

        def get_mail_message(self, message_id):
            return {"message_id": message_id, "excerpt": "sanitized"}

    class Proposals:
        def propose_reply(self, request):
            return {"proposal": {"proposal_id": "reply-1", **request}}

        def propose_interview_slots(self, request):
            return {"proposal": {"proposal_id": "slots-1", **request}}

    class Ledger:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def list_interview_schedules(self, *, limit=200):
            return [
                {
                    "interview_schedule_id": "accepted-schedule-1",
                    "application_id": application_id,
                    "starts_at": "2026-09-03T12:00:00Z",
                    "ends_at": "2026-09-03T12:30:00Z",
                    "time_zone": "UTC",
                    "status": "active",
                }
            ][:limit]

    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        application_id = start_application(ledger, "factory")
        # The lifecycle read model is the injected source for upcoming interviews.
        ledger.lifecycle.list_upcoming_interviews = lambda limit: Ledger(ledger).list_interview_schedules(limit=limit)
        capabilities = build_hermes_capabilities(
            HermesSources(Jobs(), Shortlist(), Ledger(ledger), Mail(), Proposals())
        )
        client = HermesAdapter(capabilities, now=lambda: NOW)
        assert (
            client.invoke("search_jobs", {"query": "platform"})["jobs"][0]["id"]
            == "job-1"
        )
        assert client.invoke("list_interviews", {})["interviews"][0][
            "interview_schedule_id"
        ] == "accepted-schedule-1"
        reminder = client.invoke(
            "create_reminder",
            {
                "application_id": application_id,
                # Keep this fixture permanently in the future relative to the
                # ledger's real clock as well as the adapter's injected clock.
                "due_at": "2099-01-01T00:00:00Z",
                "note": "Follow up",
                "idempotency_key": "factory-reminder-1",
            },
        )
        assert reminder["reminder_id"] and reminder["status"] == "scheduled"


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, argv, *, input_text, timeout_seconds):
        self.calls.append((tuple(argv), input_text, timeout_seconds))
        return self.results.pop(0)


def test_policy_outbox_and_fixed_argv_hermes_delivery():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        application_id = start_application(ledger, "notify")
        publisher = DurableNotificationPublisher(ledger, now=lambda: NOW)
        intent = NotificationIntent(
            "application.interview_requested",
            "event-1",
            "Interview requested",
            "Example Co asked for interview availability.",
            application_id,
            {
                "lane": "mail",
                "workflow": "application_lifecycle",
                "access_token": "must-not-leave-process",
            },
        )
        assert publisher.publish(intent)["created"]
        assert publisher.publish(intent) == publisher.publish(intent)
        assert len(ledger.list_notification_outbox(("pending",))) == 1
        assert publisher.publish(
            NotificationIntent("application.submitted", "event-2", "Submitted", "Done")
        )["suppressed"]

        runner = FakeRunner([CommandResult(0, "ok", "")])
        sender = HermesSendClient(
            runner,
            executable=Path("/opt/hermes/bin/hermes"),
            target="telegram:job-search",
        )
        delivery_now = datetime.now(timezone.utc) + timedelta(minutes=1)
        heartbeats = []
        task_context = TaskContext(
            "private-outer-work-id",
            "notification.deliver",
            1,
            utc_text(delivery_now),
            lambda: heartbeats.append(True) or True,
        )
        result = NotificationOutboxHandler(
            ledger, sender, now=lambda: delivery_now
        ).handle_task(
            {"limit": 10, "lane": "telegram", "workflow": "delivery"},
            task_context,
        )
        assert result == {"delivered": 1, "retried": 0, "dead": 0}
        assert len(heartbeats) >= 2
        assert runner.calls[0][0] == (
            "/opt/hermes/bin/hermes",
            "send",
            "--to",
            "telegram:job-search",
        )
        assert HERMES_SEND_ARGV == ("hermes", "send", "--to")
        assert runner.calls[0][1] == (
            "Interview requested\n\nExample Co asked for interview availability."
        )
        assert "private-outer-work-id" not in runner.calls[0][1]
        assert ledger.list_notification_outbox(("delivered",))[0]["attempts"] == 1
        assert SHORTLIST_NOTIFICATION_TASK_KIND == "notification.shortlist_evaluate"
        try:
            HermesSendClient(runner, executable=Path("relative/hermes"))
        except ValueError as exc:
            assert "absolute path" in str(exc)
        else:
            raise AssertionError("relative Hermes executable was accepted")


def test_notification_publish_replay_is_stable_across_clock_ticks():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        clock = [NOW]
        publisher = DurableNotificationPublisher(ledger, now=lambda: clock[0])
        intent = NotificationIntent(
            "system.degraded",
            "health-replay",
            "System needs attention",
            "Check Ops.",
        )

        first = publisher.publish(intent)
        clock[0] += timedelta(seconds=1)
        replay = publisher.publish(intent)

        assert replay == first
        assert len(ledger.list_notification_outbox()) == 1


def test_public_notification_boundary_requires_a_policy_evaluated_intent():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        assert not hasattr(ledger, "enqueue_notification")
        try:
            ledger.publish_notification(  # type: ignore[arg-type]
                {
                    "topic": "system.degraded",
                    "title": "forged",
                    "body": "forged",
                    "context": {"access_token": "must-not-leave-process"},
                }
            )
        except ContractError:
            pass
        else:
            raise AssertionError("raw notification envelopes must be rejected")

        suppressed = ledger.publish_notification(
            NotificationIntent(
                "application.submitted", "event-policy", "Submitted", "Done"
            )
        )
        assert suppressed["suppressed"] is True
        assert ledger.list_notification_outbox() == []


def test_notification_failure_requires_reconciliation_without_leaking_runner_stderr():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        publisher = DurableNotificationPublisher(ledger, now=lambda: NOW)
        publisher.publish(
            NotificationIntent(
                "system.degraded", "health-1", "System needs attention", "Check Ops."
            )
        )
        runner = FakeRunner([CommandResult(1, "", "token at /private/path")])
        delivery_now = datetime.now(timezone.utc) + timedelta(minutes=1)
        result = NotificationOutboxHandler(
            ledger, HermesSendClient(runner), now=lambda: delivery_now
        ).handle_task({})
        assert result == {"delivered": 0, "retried": 0, "dead": 1}
        pending = ledger.list_notification_outbox(("dead",))[0]
        assert pending["last_error"] == "delivery_reconciliation_required"
        assert "token" not in pending["last_error"]
        assert "/private" not in pending["last_error"]


def test_notification_lease_outlasts_the_longest_hermes_send():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        DurableNotificationPublisher(ledger).publish(
            NotificationIntent(
                "system.degraded",
                "health-slow-send",
                "System needs attention",
                "Check Ops.",
            )
        )
        delivery_now = datetime.now(timezone.utc) + timedelta(minutes=1)

        class SlowSender:
            timeout_seconds = 120

            def send(self, _notification):
                competing = ledger.claim_notification(
                    "worker-two",
                    utc_text(delivery_now + timedelta(seconds=self.timeout_seconds)),
                    60,
                )
                assert competing is None

        handler = NotificationOutboxHandler(
            ledger, SlowSender(), worker_id="worker-one", now=lambda: delivery_now
        )
        result = handler.handle_task({"limit": 1})

        assert handler.lease_seconds >= SlowSender.timeout_seconds + 30
        assert result == {"delivered": 1, "retried": 0, "dead": 0}


def test_unexpected_failure_at_attempt_limit_is_reported_dead():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        DurableNotificationPublisher(
            ledger, NotificationPolicy(max_attempts=1)
        ).publish(
            NotificationIntent(
                "system.degraded",
                "health-broken-sender",
                "System needs attention",
                "Check Ops.",
            )
        )

        class BrokenSender:
            def send(self, _notification):
                raise RuntimeError("private sender failure")

        delivery_now = datetime.now(timezone.utc) + timedelta(minutes=1)
        result = NotificationOutboxHandler(
            ledger, BrokenSender(), now=lambda: delivery_now
        ).handle_task({"limit": 1})

        assert result == {"delivered": 0, "retried": 0, "dead": 1}
        dead = ledger.list_notification_outbox(("dead",))[0]
        assert dead["attempts"] == 1
        assert dead["last_error"] == "delivery_reconciliation_required"


def test_notification_claim_recovers_after_worker_lease_expires():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        DurableNotificationPublisher(ledger, now=lambda: NOW).publish(
            NotificationIntent(
                "system.degraded",
                "health-crash",
                "System needs attention",
                "Check Ops.",
            )
        )
        first = ledger.claim_notification("worker-one", utc_text(NOW), 60)
        assert first is not None
        assert (
            ledger.claim_notification(
                "worker-two", utc_text(NOW + timedelta(seconds=59)), 60
            )
            is None
        )

        recovered = ledger.claim_notification(
            "worker-two", utc_text(NOW + timedelta(seconds=60)), 60
        )
        assert recovered is not None
        assert recovered["notification_id"] == first["notification_id"]
        assert recovered["lease_token"] != first["lease_token"]
        assert recovered["attempts"] == 2


def test_notification_claim_does_not_exceed_the_attempt_limit_after_a_crash():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "job-search.db")
        DurableNotificationPublisher(
            ledger, NotificationPolicy(max_attempts=1), now=lambda: NOW
        ).publish(
            NotificationIntent(
                "system.degraded",
                "health-final-crash",
                "System needs attention",
                "Check Ops.",
            )
        )
        delivery_now = datetime.now(timezone.utc) + timedelta(minutes=1)
        first = ledger.claim_notification("worker-one", utc_text(delivery_now), 60)
        assert first is not None and first["attempts"] == 1

        recovered = ledger.claim_notification(
            "worker-two", utc_text(delivery_now + timedelta(seconds=60)), 60
        )

        assert recovered is None
        dead = ledger.list_notification_outbox(("dead",))[0]
        assert dead["attempts"] == dead["max_attempts"] == 1


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} Hermes runtime tests)")


if __name__ == "__main__":
    main()
