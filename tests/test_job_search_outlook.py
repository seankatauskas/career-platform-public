#!/usr/bin/env python3
"""Offline tests for the personal Outlook connector foundation."""

from __future__ import annotations

import http.client
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from job_search.outlook import (
    BASE_SCOPES,
    DRAFT_SCOPES,
    HOLD_SCOPES,
    GraphCalendarClient,
    GraphLinkError,
    GraphMailClient,
    GraphOutcomeUnknown,
    GraphOutlookClient,
    InMemoryCursorStateAdapter,
    MsalTokenProvider,
    OutlookAuthError,
    SecureTokenStorageError,
    retry_decision,
    validate_graph_url,
)
from job_search.outlook.transport import GraphSession, RawHttpResponse, RetryClass


class FakeTokens:
    def __init__(self):
        self.calls = []

    def get_token(self, scopes, *, interactive=False, force_refresh=False):
        self.calls.append((frozenset(scopes), interactive, force_refresh))
        return "secret-token"


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, method, url, headers, body):
        self.requests.append((method, url, dict(headers), body))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def response(status, payload, headers=None):
    return RawHttpResponse(
        status,
        headers or {},
        json.dumps(payload).encode("utf-8") if payload is not None else b"",
    )


def session_with(*responses):
    tokens = FakeTokens()
    http = FakeHttp(*responses)
    return GraphSession(tokens, http), tokens, http


class FakePersistence:
    def __init__(self, encrypted=True):
        self.is_encrypted = encrypted


class FakeExtensions:
    encrypted = True

    @classmethod
    def build_encrypted_persistence(cls, location):
        return FakePersistence(cls.encrypted)

    class PersistedTokenCache:
        def __init__(self, persistence):
            self.persistence = persistence


class FakeApp:
    def __init__(self, client_id, authority, token_cache):
        self.client_id = client_id
        self.authority = authority
        self.cache = token_cache
        self.accounts = []
        self.interactive_calls = []
        self.device_flow_calls = []
        self.device_token_calls = []

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account, force_refresh=False):
        return {"access_token": "silent"}

    def acquire_token_interactive(self, scopes, redirect_uri):
        self.interactive_calls.append((scopes, redirect_uri))
        return {"access_token": "interactive"}

    def initiate_device_flow(self, scopes):
        self.device_flow_calls.append(scopes)
        return {
            "user_code": "ABCD-EFGH",
            "message": "Open https://microsoft.com/devicelogin and enter ABCD-EFGH",
        }

    def acquire_token_by_device_flow(self, flow):
        self.device_token_calls.append(dict(flow))
        return {"access_token": "device-code"}

    def remove_account(self, account):
        self.accounts.remove(account)


class FakeMsal:
    PublicClientApplication = FakeApp


def test_optional_auth_is_personal_public_client_and_secure():
    with tempfile.TemporaryDirectory() as temp:
        provider = MsalTokenProvider(
            "client-id",
            Path(temp) / "cache.bin",
            msal_module=FakeMsal,
            extensions_module=FakeExtensions,
        )
        assert provider._app.authority.endswith("/consumers")
        assert provider.get_token(BASE_SCOPES, interactive=True) == "interactive"
        assert provider._app.interactive_calls[0][1] == "http://localhost"
        try:
            provider.get_token(frozenset({"Files.Read"}), interactive=True)
        except OutlookAuthError:
            pass
        else:
            raise AssertionError("unsupported Microsoft Graph scopes were accepted")
    FakeExtensions.encrypted = False
    try:
        with tempfile.TemporaryDirectory() as temp:
            MsalTokenProvider(
                "client-id",
                Path(temp) / "cache.bin",
                msal_module=FakeMsal,
                extensions_module=FakeExtensions,
            )
    except SecureTokenStorageError:
        pass
    else:
        raise AssertionError("plaintext MSAL persistence was accepted")
    finally:
        FakeExtensions.encrypted = True

    injected = FakePersistence(True)
    with tempfile.TemporaryDirectory() as temp:
        provider = MsalTokenProvider(
            "client-id",
            Path(temp) / "cache.bin",
            msal_module=FakeMsal,
            extensions_module=FakeExtensions,
            persistence=injected,
        )
        assert provider._cache.persistence is injected


def test_device_code_auth_supports_headless_hosts_without_browser_launch():
    with tempfile.TemporaryDirectory() as temp:
        provider = MsalTokenProvider(
            "client-id",
            Path(temp) / "cache.bin",
            msal_module=FakeMsal,
            extensions_module=FakeExtensions,
        )
        messages = []
        token = provider.get_token(
            BASE_SCOPES,
            interactive=True,
            device_code_callback=messages.append,
        )
        assert token == "device-code"
        assert messages == [
            "Open https://microsoft.com/devicelogin and enter ABCD-EFGH"
        ]
        assert provider._app.device_flow_calls == [sorted(BASE_SCOPES)]
        assert len(provider._app.device_token_calls) == 1
        assert provider._app.interactive_calls == []


def test_graph_urls_are_v1_https_only():
    assert validate_graph_url("/v1.0/me/messages").startswith("https://graph.microsoft.com/")
    for bad in (
        "http://graph.microsoft.com/v1.0/me",
        "https://evil.test/v1.0/me",
        "https://graph.microsoft.com/beta/me",
        "https://user@graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com/v1.0/me/messages#fragment",
        "https://graph.microsoft.com/v1.0/me/%2E%2E/beta",
    ):
        try:
            validate_graph_url(bad)
        except GraphLinkError:
            pass
        else:
            raise AssertionError(f"unsafe Graph link accepted: {bad}")


def test_generic_transport_rejects_sending_endpoints():
    session, _, _ = session_with()
    for method, url in (
        ("POST", "/v1.0/me/sendMail"),
        ("POST", "/v1.0/me/messages/message-1/send"),
        ("POST", "/v1.0/me/events/event-1/accept"),
        ("DELETE", "/v1.0/me/events/event-1"),
    ):
        try:
            session.request_json(
                method,
                url,
                scopes=BASE_SCOPES,
                retry_class=RetryClass.NON_IDEMPOTENT_WRITE,
            )
        except GraphLinkError:
            pass
        else:
            raise AssertionError(f"unsafe operation accepted: {method} {url}")


def test_generic_transport_enforces_scopes_and_safe_payloads_below_clients():
    session, tokens, http = session_with()
    safe_hold = {
        "subject": "Private hold",
        "body": {"contentType": "text", "content": ""},
        "start": {"dateTime": "2026-09-02T15:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-02T15:30:00", "timeZone": "UTC"},
        "showAs": "tentative",
        "sensitivity": "private",
        "isReminderOn": False,
        "responseRequested": False,
        "allowNewTimeProposals": False,
        "attendees": [],
        "transactionId": "action-1",
    }
    unsafe_hold = dict(safe_hold)
    unsafe_hold["attendees"] = [
        {"emailAddress": {"address": "victim@example.test"}}
    ]
    cases = (
        ("POST", "/v1.0/me/events", HOLD_SCOPES, unsafe_hold),
        (
            "PATCH",
            "/v1.0/me/messages/draft-1",
            DRAFT_SCOPES,
            {"toRecipients": [{"emailAddress": {"address": "victim@example.test"}}]},
        ),
        ("GET", "/v1.0/me/messages/message-1", BASE_SCOPES | {"Files.Read"}, None),
        ("POST", "/v1.0/me/messages/message-1/createReply", HOLD_SCOPES, None),
    )
    for method, url, scopes, payload in cases:
        try:
            session.request_json(
                method,
                url,
                scopes=frozenset(scopes),
                retry_class=RetryClass.IDEMPOTENT_WRITE,
                payload=payload,
            )
        except GraphLinkError:
            pass
        else:
            raise AssertionError(f"unsafe Graph request accepted: {method} {url}")
    assert tokens.calls == [] and http.requests == []

    import job_search.outlook as outlook

    assert not hasattr(outlook, "GraphSession")


def test_every_request_has_immutable_preference_and_tokens_are_not_logged():
    session, tokens, http = session_with(response(200, {"ok": True}))
    assert session.request_json(
        "GET", "/v1.0/me/messages/message-1", scopes=BASE_SCOPES, retry_class=RetryClass.READ
    ) == {"ok": True}
    headers = http.requests[0][2]
    assert 'IdType="ImmutableId"' in headers["Prefer"]
    assert headers["Authorization"] == "Bearer secret-token"
    assert tokens.calls == [(BASE_SCOPES, False, False)]


def test_401_refreshes_silently_once():
    session, tokens, _ = session_with(
        response(401, {"error": {"code": "InvalidAuthenticationToken"}}),
        response(200, {"ok": True}),
    )
    session.request_json(
        "GET", "/v1.0/me/messages/message-1", scopes=BASE_SCOPES, retry_class=RetryClass.READ
    )
    assert tokens.calls[-1] == (BASE_SCOPES, False, True)


def test_retry_after_and_non_idempotent_uncertainty():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    throttled = retry_decision(
        status=429,
        headers={"Retry-After": "10"},
        attempt=1,
        retry_class=RetryClass.READ,
        now=now,
    )
    assert throttled.retryable
    assert throttled.next_attempt_at == "2026-09-01T12:00:10Z"
    dated = retry_decision(
        status=503,
        headers={"retry-after": "Tue, 01 Sep 2026 12:00:20 GMT"},
        attempt=1,
        retry_class=RetryClass.READ,
        now=now,
    )
    assert dated.next_attempt_at == "2026-09-01T12:00:20Z"
    unknown = retry_decision(
        status=503,
        headers={},
        attempt=1,
        retry_class=RetryClass.NON_IDEMPOTENT_WRITE,
        now=now,
    )
    assert unknown.outcome_unknown and not unknown.retryable


def test_transport_failure_on_create_reply_is_unknown():
    for failure in (
        TimeoutError("secret detail"),
        http.client.IncompleteRead(b'{"id":', 20),
    ):
        session, _, _ = session_with(failure)
        client = GraphOutlookClient(session)
        try:
            client.create_reply_draft("message-id")
        except GraphOutcomeUnknown as exc:
            assert exc.decision.outcome_unknown
            assert "secret detail" not in str(exc)
        else:
            raise AssertionError("ambiguous createReply was treated as safe")


def test_unusable_success_response_on_create_reply_is_unknown():
    responses = (
        response(201, None),
        RawHttpResponse(201, {}, b'{"id":'),
        response(201, ["unexpected"]),
    )
    for unusable in responses:
        session, _, _ = session_with(unusable)
        try:
            GraphOutlookClient(session).create_reply_draft("message-id")
        except GraphOutcomeUnknown as exc:
            assert exc.status == 201
            assert exc.decision.outcome_unknown
            assert not exc.decision.retryable
        else:
            raise AssertionError("unusable createReply success was treated as final")


def test_mail_backfill_is_bounded_ordered_and_privacy_minimized():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    url = GraphMailClient.backfill_url(now=now)
    assert "receivedDateTime+ge+2026-06-03T00%3A00%3A00Z" in url
    assert "%24orderby=receivedDateTime+desc" in url
    assert "body%2C" not in url and "attachments" not in url


def test_mail_delta_parses_removals_and_opaque_links():
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=x"
    session, _, http = session_with(
        response(
            200,
            {
                "value": [
                    {"id": "immutable-1", "@removed": {"reason": "deleted"}},
                    {
                        "id": "immutable-2",
                        "sender": {"emailAddress": {"address": "recruiter@example.test"}},
                        "subject": "Interview",
                    },
                ],
                "@odata.nextLink": next_link,
            },
        )
    )
    page = GraphMailClient(session).read_delta_page(
        GraphMailClient.initial_delta_url(now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    assert page.changes[0].removed
    assert page.changes[1].sender_address == "recruiter@example.test"
    assert page.next_link == next_link and page.delta_link is None
    assert 'IdType="ImmutableId"' in http.requests[0][2]["Prefer"]


def test_cursor_checkpoint_does_not_replace_committed_delta():
    store = InMemoryCursorStateAdapter()
    state = store.load("account", "inbox", 1)
    state = store.commit(state, "https://graph.microsoft.com/v1.0/delta-one")
    state = store.checkpoint(state, "https://graph.microsoft.com/v1.0/next-page")
    assert state.committed_delta_link.endswith("delta-one")
    assert state.in_flight_next_link.endswith("next-page")
    reset = store.reset(state)
    assert reset.needs_backfill and reset.committed_delta_link is None


def test_calendar_view_is_paged_and_excludes_private_fields_from_blocks():
    next_link = "https://graph.microsoft.com/v1.0/me/calendar/calendarView?$skiptoken=two"
    event = {
        "id": "event-1",
        "changeKey": "change-1",
        "start": {"dateTime": "2026-09-02T15:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-02T15:30:00.0000000", "timeZone": "UTC"},
        "showAs": "busy",
        "subject": "Sensitive appointment",
        "body": {"content": "private"},
    }
    session, tokens, http = session_with(
        response(200, {"value": [event], "@odata.nextLink": next_link}),
        response(200, {"value": []}),
    )
    blocks = GraphCalendarClient(session).read_calendar_view(
        "2026-09-01T12:00:00Z", "2026-09-10T12:00:00Z"
    )
    assert len(blocks) == 1 and blocks[0].remote_id == "event-1"
    assert not hasattr(blocks[0], "subject") and not hasattr(blocks[0], "body")
    assert "subject" not in http.requests[0][1]
    assert "body" not in http.requests[0][1]
    assert "Calendars.Read" in tokens.calls[0][0]
    assert not {"Mail.Send", "Mail.ReadWrite", "Calendars.ReadWrite"} & tokens.calls[0][0]
    assert 'outlook.timezone="UTC"' in http.requests[0][2]["Prefer"]


def test_calendar_and_folder_paging_fail_closed_on_cycles_or_page_exhaustion():
    calendar_link = (
        "https://graph.microsoft.com/v1.0/me/calendar/calendarView?$skiptoken=cycle"
    )
    session, _, http = session_with(
        response(200, {"value": [], "@odata.nextLink": calendar_link}),
        response(200, {"value": [], "@odata.nextLink": calendar_link}),
    )
    try:
        GraphCalendarClient(session).read_calendar_view(
            "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"
        )
    except ValueError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("calendar paging cycle was followed indefinitely")
    assert len(http.requests) == 2

    next_folder = (
        "https://graph.microsoft.com/v1.0/me/mailFolders?$skiptoken=second"
    )
    session, _, http = session_with(
        response(200, {"value": [], "@odata.nextLink": next_folder})
    )
    try:
        GraphMailClient(session).list_folder_tree(max_pages=1)
    except ValueError as exc:
        assert "exceeded" in str(exc)
    else:
        raise AssertionError("distinct empty folder pages exceeded no request bound")
    assert len(http.requests) == 1


def test_reply_draft_never_sends_and_patch_is_exact():
    session, _, http = session_with(
        response(201, {"id": "draft-1", "webLink": "https://outlook.live.test/draft"}),
        response(200, {"id": "draft-1", "isDraft": True}),
    )
    client = GraphOutlookClient(session)
    assert client.create_reply_draft("message-1")["id"] == "draft-1"
    client.update_reply_draft("draft-1", "Exact approved body")
    assert http.requests[0][0:2] == (
        "POST",
        "https://graph.microsoft.com/v1.0/me/messages/message-1/createReply",
    )
    patch = json.loads(http.requests[1][3].decode("utf-8"))
    assert patch == {"body": {"contentType": "text", "content": "Exact approved body"}}
    assert "send" not in http.requests[0][1].lower()
    assert http.requests[0][2]["Authorization"] == "Bearer secret-token"


def test_private_hold_overrides_unsafe_caller_fields():
    session, tokens, http = session_with(
        response(201, {"id": "event-1", "transactionId": "action-1"})
    )
    result = GraphOutlookClient(session).create_private_tentative_hold(
        {
            "start": {"dateTime": "2026-09-02T15:00:00Z"},
            "end": {"dateTime": "2026-09-02T15:30:00Z"},
            "transactionId": "action-1",
            "attendees": [{"emailAddress": {"address": "victim@example.test"}}],
            "showAs": "busy",
            "sensitivity": "normal",
        }
    )
    assert result["id"] == "event-1"
    sent = json.loads(http.requests[0][3].decode("utf-8"))
    assert sent["attendees"] == []
    assert sent["showAs"] == "tentative"
    assert sent["sensitivity"] == "private"
    assert sent["transactionId"] == "action-1"
    assert tokens.calls[0][0] != DRAFT_SCOPES
    assert "Mail.Send" not in tokens.calls[0][0]


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} Outlook tests)")


if __name__ == "__main__":
    main()
