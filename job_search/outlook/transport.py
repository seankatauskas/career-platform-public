"""Small Microsoft Graph transport with explicit operation and scope boundaries."""

from __future__ import annotations

import email.utils
import http.client
import json
import random
import re
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, FrozenSet, Mapping, Optional, Protocol, Sequence
from urllib.parse import urljoin, urlsplit

from job_search.contracts import RetryDecision

from .auth import BASE_SCOPES, DRAFT_SCOPES, HOLD_SCOPES, SEND_SCOPES, TokenProvider


GRAPH_ORIGIN = "https://graph.microsoft.com"
GRAPH_PREFIX = "/v1.0/"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

_ALLOWED_OPERATIONS = {
    "GET": (
        re.compile(r"^/v1\.0/me/mailFolders$"),
        re.compile(r"^/v1\.0/me/mailFolders/[^/]+$"),
        re.compile(r"^/v1\.0/me/mailFolders/[^/]+/childFolders$"),
        re.compile(r"^/v1\.0/me/mailFolders/[^/]+/messages(?:/delta)?$"),
        # Graph returns OData-key URLs in nextLink/deltaLink even when the
        # initial request used slash addressing. Preserve the opaque link.
        re.compile(r"^/v1\.0/me/mailFolders\('[A-Za-z0-9_+=%-]+'\)/(?:messages(?:/delta)?|childFolders)$"),
        re.compile(r"^/v1\.0/me/messages/[^/]+$"),
        re.compile(r"^/v1\.0/me/messages/[^/]+/attachments$"),
        re.compile(r"^/v1\.0/me/messages/[^/]+/attachments/[^/]+$"),
        re.compile(r"^/v1\.0/me/calendar/calendarView$"),
        re.compile(r"^/v1\.0/me/events/[^/]+$"),
    ),
    "POST": (
        re.compile(r"^/v1\.0/me/messages/[^/]+/createReply$"),
        re.compile(r"^/v1\.0/me/messages/[^/]+/send$"),
        re.compile(r"^/v1\.0/me/events$"),
    ),
    "DELETE": (re.compile(r"^/v1\.0/me/events/[^/]+$"),),
    "PATCH": (re.compile(r"^/v1\.0/me/messages/[^/]+$"), re.compile(r"^/v1\.0/me/events/[^/]+$")),
}

_TRANSACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


class GraphLinkError(ValueError):
    """A URL escapes the narrow Microsoft Graph v1.0 boundary."""


class GraphHttpError(RuntimeError):
    def __init__(
        self,
        status: Optional[int],
        error_code: str,
        decision: RetryDecision,
    ) -> None:
        super().__init__(f"Microsoft Graph request failed ({status or 'transport'}: {error_code})")
        self.status = status
        self.error_code = error_code
        self.decision = decision


class GraphOutcomeUnknown(GraphHttpError):
    """A non-idempotent request may have reached Microsoft Graph."""


class RetryClass(str, Enum):
    READ = "read"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"


@dataclass(frozen=True)
class RawHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpAdapter(Protocol):
    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
    ) -> RawHttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        return None


class UrllibHttpAdapter:
    """stdlib adapter that refuses redirects and bounds response bodies."""

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirect())

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
    ) -> RawHttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(response_body) > MAX_RESPONSE_BYTES:
                    raise GraphLinkError("Microsoft Graph response exceeded size limit")
                return RawHttpResponse(
                    int(response.status),
                    {str(key).lower(): str(value) for key, value in response.headers.items()},
                    response_body,
                )
        except urllib.error.HTTPError as exc:
            response_body = exc.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                response_body = b""
            return RawHttpResponse(
                int(exc.code),
                {str(key).lower(): str(value) for key, value in exc.headers.items()},
                response_body,
            )


def validate_graph_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise GraphLinkError("Microsoft Graph URL is required")
    absolute = urljoin(GRAPH_ORIGIN, url)
    parsed = urlsplit(absolute)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GraphLinkError("Microsoft Graph URL has an invalid port") from exc
    lower_path = parsed.path.lower()
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.microsoft.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(GRAPH_PREFIX)
        or parsed.fragment
        or "\\" in parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or "%2e" in lower_path
        or "%5c" in lower_path
    ):
        raise GraphLinkError("URL must remain on Microsoft Graph v1.0")
    return absolute


def _validate_patch_payload(payload: Optional[Mapping[str, Any]]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != {"body"}:
        raise GraphLinkError("message PATCH is limited to an exact text body")
    body = payload.get("body")
    if not isinstance(body, Mapping) or set(body) != {"contentType", "content"}:
        raise GraphLinkError("message PATCH is limited to an exact text body")
    if body.get("contentType") != "text" or not isinstance(body.get("content"), str):
        raise GraphLinkError("message PATCH is limited to an exact text body")


def _utc_graph_time(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"dateTime", "timeZone"}:
        return False
    date_time = value.get("dateTime")
    if not isinstance(date_time, str) or value.get("timeZone") != "UTC":
        return False
    try:
        parsed = datetime.fromisoformat(date_time)
    except ValueError:
        return False
    return parsed.tzinfo is None


def _validate_hold_payload(payload: Optional[Mapping[str, Any]]) -> None:
    expected = {
        "subject", "body", "start", "end", "showAs", "sensitivity",
        "isReminderOn", "responseRequested", "allowNewTimeProposals",
        "attendees", "transactionId",
    }
    if not isinstance(payload, Mapping) or set(payload) not in (expected, expected | {"location"}):
        raise GraphLinkError("calendar event payload exceeds the private-hold boundary")
    if (
        payload.get("subject") not in {"Private hold", "Private career commitment"}
        or not isinstance(payload.get("body"), Mapping)
        or set(payload["body"]) != {"contentType", "content"}
        or payload["body"].get("contentType") != "text"
        or not isinstance(payload["body"].get("content"), str)
        or len(payload["body"]["content"]) > 4000
        or (payload.get("subject") == "Private hold" and payload["body"]["content"] != "")
        or payload.get("showAs") not in {"tentative", "busy", "free"}
        or payload.get("sensitivity") != "private"
        or payload.get("isReminderOn") is not False
        or payload.get("responseRequested") is not False
        or payload.get("allowNewTimeProposals") is not False
        or payload.get("attendees") != []
        or not _utc_graph_time(payload.get("start"))
        or not _utc_graph_time(payload.get("end"))
    ):
        raise GraphLinkError("calendar event payload exceeds the private-hold boundary")
    if "location" in payload and (not isinstance(payload["location"], Mapping) or set(payload["location"]) != {"displayName"} or not isinstance(payload["location"]["displayName"], str) or len(payload["location"]["displayName"])>1000):
        raise GraphLinkError("private event location is invalid")
    transaction_id = payload.get("transactionId")
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise GraphLinkError("calendar event requires a stable transactionId")
    start = datetime.fromisoformat(str(payload["start"]["dateTime"]))
    end = datetime.fromisoformat(str(payload["end"]["dateTime"]))
    if end <= start:
        raise GraphLinkError("calendar hold end must be after start")


def _validate_operation(
    method: str,
    url: str,
    scopes: FrozenSet[str],
    payload: Optional[Mapping[str, Any]],
) -> None:
    path = urlsplit(url).path
    patterns = _ALLOWED_OPERATIONS.get(method.upper(), ())
    if not any(pattern.fullmatch(path) for pattern in patterns):
        raise GraphLinkError("Microsoft Graph operation is outside the Outlook capability boundary")
    method = method.upper()
    if method == "GET":
        expected_scopes = BASE_SCOPES
        if payload is not None:
            raise GraphLinkError("Microsoft Graph reads cannot include a payload")
    elif method == "POST" and path.endswith("/createReply"):
        expected_scopes = DRAFT_SCOPES
        if payload is not None:
            raise GraphLinkError("createReply cannot include caller-controlled content")
    elif method == "POST" and path.endswith("/send"):
        expected_scopes = SEND_SCOPES
        if payload is not None:
            raise GraphLinkError("send must not include content")
    elif method == "DELETE" and "/events/" in path:
        expected_scopes = HOLD_SCOPES
        if payload is not None:
            raise GraphLinkError("owned event deletion cannot include content")
    elif method == "PATCH" and "/events/" in path:
        expected_scopes = HOLD_SCOPES
        if not isinstance(payload, Mapping) or (set(payload)-{"location"}) != {"subject", "body", "start", "end", "showAs", "sensitivity", "isReminderOn", "responseRequested", "attendees"}:
            raise GraphLinkError("event update exceeds private commitment boundary")
        _validate_hold_payload({**payload, "allowNewTimeProposals": False, "transactionId": "conditional-owned-update"})
    elif method == "PATCH":
        expected_scopes = DRAFT_SCOPES
        _validate_patch_payload(payload)
    elif method == "POST" and path == "/v1.0/me/events":
        expected_scopes = HOLD_SCOPES
        _validate_hold_payload(payload)
    else:  # Retained as a fail-closed guard if the route table changes.
        raise GraphLinkError("Microsoft Graph operation has no capability policy")
    if frozenset(scopes) != expected_scopes:
        raise GraphLinkError("Microsoft Graph scopes exceed the operation capability")


def _retry_after_seconds(value: Optional[str], now: datetime) -> Optional[int]:
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((parsed.astimezone(timezone.utc) - now).total_seconds()))


def retry_decision(
    *,
    status: Optional[int],
    headers: Mapping[str, str],
    attempt: int,
    retry_class: RetryClass,
    now: Optional[datetime] = None,
    transport_failure: bool = False,
    jitter: bool = False,
) -> RetryDecision:
    """Classify a failure without sleeping or retrying inline."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    non_idempotent_ambiguous = retry_class is RetryClass.NON_IDEMPOTENT_WRITE and (
        transport_failure or status in {408, 500, 502, 503, 504}
    )
    if non_idempotent_ambiguous:
        return RetryDecision(False, None, True, "outcome_unknown")
    if status not in RETRYABLE_STATUSES and not transport_failure:
        return RetryDecision(False, None, False, f"http_{status}")
    if attempt >= 5:
        return RetryDecision(False, None, False, "retry_exhausted")
    supplied = _retry_after_seconds(normalized_headers.get("retry-after"), now)
    if supplied is None:
        delay = min(300, 2 ** max(0, attempt - 1))
        if jitter:
            delay = max(1, int(random.uniform(0, delay)))
    else:
        delay = supplied
    next_at = (now + timedelta(seconds=delay)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    return RetryDecision(True, next_at, False, f"retry_http_{status or 'transport'}")


def _error_code(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
        code = payload.get("error", {}).get("code")
        if isinstance(code, str) and code:
            return code[:128]
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass
    return "unknown_error"


class GraphSession:
    """Authenticated JSON session used only by the narrow Outlook clients."""

    def __init__(self, token_provider: TokenProvider, http: HttpAdapter) -> None:
        self._tokens = token_provider
        self._http = http

    def ensure_authorized(self, scopes: FrozenSet[str]) -> None:
        """Verify silent consent without returning token material or making a request."""

        self._tokens.get_token(scopes)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        scopes: FrozenSet[str],
        retry_class: RetryClass,
        payload: Optional[Mapping[str, Any]] = None,
        preferences: Sequence[str] = (),
        attempt: int = 1,
        expected_statuses: Sequence[int] = (200,),
        if_match: str = "",
    ) -> Mapping[str, Any]:
        absolute = validate_graph_url(url)
        method = method.upper()
        _validate_operation(method, absolute, scopes, payload)
        if if_match and (method not in {"PATCH", "DELETE"} or "/me/events/" not in absolute):
            raise GraphLinkError("conditional updates only supported for owned events")
        if method in {"PATCH", "DELETE"} and "/me/events/" in absolute and not if_match:
            raise GraphLinkError("event updates require a version condition")
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(body) > MAX_REQUEST_BYTES:
                raise ValueError("Microsoft Graph request exceeded size limit")
        token = self._tokens.get_token(scopes)
        response = self._send(method, absolute, token, body, preferences, retry_class, attempt, if_match)
        if response.status == 401:
            token = self._tokens.get_token(scopes, force_refresh=True)
            response = self._send(method, absolute, token, body, preferences, retry_class, attempt, if_match)
        if response.status not in set(expected_statuses):
            decision = retry_decision(
                status=response.status,
                headers=response.headers,
                attempt=attempt,
                retry_class=retry_class,
            )
            error_type = GraphOutcomeUnknown if decision.outcome_unknown else GraphHttpError
            raise error_type(response.status, _error_code(response.body), decision)
        if method == "POST" and urlsplit(absolute).path.endswith("/send"):
            if response.status != 202 or response.body:
                raise GraphOutcomeUnknown(response.status, "unexpected_send_response", RetryDecision(False, None, True, "outcome_unknown"))
            return {"accepted": True}
        if not response.body:
            if retry_class is RetryClass.NON_IDEMPOTENT_WRITE:
                decision = RetryDecision(False, None, True, "outcome_unknown")
                raise GraphOutcomeUnknown(
                    response.status, "empty_success_response", decision
                )
            return {}
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            unknown = retry_class is RetryClass.NON_IDEMPOTENT_WRITE
            decision = RetryDecision(
                False, None, unknown, "outcome_unknown" if unknown else "invalid_json"
            )
            error_type = GraphOutcomeUnknown if unknown else GraphHttpError
            raise error_type(response.status, "invalid_json", decision) from exc
        if not isinstance(parsed, dict):
            unknown = retry_class is RetryClass.NON_IDEMPOTENT_WRITE
            decision = RetryDecision(
                False,
                None,
                unknown,
                "outcome_unknown" if unknown else "invalid_json_shape",
            )
            error_type = GraphOutcomeUnknown if unknown else GraphHttpError
            raise error_type(response.status, "invalid_json_shape", decision)
        return parsed

    def _send(
        self,
        method: str,
        url: str,
        token: str,
        body: Optional[bytes],
        preferences: Sequence[str],
        retry_class: RetryClass,
        attempt: int,
        if_match: str = "",
    ) -> RawHttpResponse:
        all_preferences = list(dict.fromkeys(("IdType=\"ImmutableId\"", *preferences)))
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "client-request-id": str(uuid.uuid4()),
            "return-client-request-id": "true",
            "Prefer": ", ".join(all_preferences),
        }
        if if_match:
            headers["If-Match"] = if_match
        if method == "POST" and urlsplit(url).path.endswith("/send"):
            headers["Content-Length"] = "0"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self._http.send(method, url, headers, body)
        except (
            http.client.HTTPException,
            OSError,
            TimeoutError,
            socket.timeout,
        ) as exc:
            decision = retry_decision(
                status=None,
                headers={},
                attempt=attempt,
                retry_class=retry_class,
                transport_failure=True,
            )
            error_type = GraphOutcomeUnknown if decision.outcome_unknown else GraphHttpError
            raise error_type(None, "transport_failure", decision) from exc
