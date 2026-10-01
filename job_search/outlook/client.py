"""OutlookTransport implementation without send or invitation capabilities."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from job_search.contracts import CalendarBlock, MailDeltaPage, parse_utc

from .auth import DRAFT_SCOPES, HOLD_SCOPES
from .calendar import GraphCalendarClient
from .mail import GraphMailClient
from .transport import GraphSession, RetryClass


class UnsafeOutlookAction(ValueError):
    """A proposed write would exceed the approved v1 capability boundary."""


_TRANSACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


def _minimal_result(payload: Mapping[str, Any], fields: Sequence[str]) -> Mapping[str, Any]:
    return {field: payload[field] for field in fields if field in payload}


class GraphOutlookClient:
    def __init__(self, session: GraphSession) -> None:
        self._session = session
        self._mail = GraphMailClient(session)
        self._calendar = GraphCalendarClient(session)

    def preflight_action(self, kind: str) -> None:
        """Fail before a durable action claim if interactive consent is required."""

        if kind == "outlook_reply_draft":
            self._session.ensure_authorized(DRAFT_SCOPES)
            return
        if kind == "calendar_tentative_hold":
            self._session.ensure_authorized(HOLD_SCOPES)
            return
        raise UnsafeOutlookAction("unsupported action kind")

    def read_delta_page(self, opaque_url: str) -> MailDeltaPage:
        return self._mail.read_delta_page(opaque_url)

    def read_message_body(self, immutable_message_id: str) -> Mapping[str, Any]:
        return self._mail.read_message_body(immutable_message_id)

    def read_calendar_view(self, starts_at: str, ends_at: str) -> Sequence[CalendarBlock]:
        return self._calendar.read_calendar_view(starts_at, ends_at)

    def create_reply_draft(self, immutable_message_id: str) -> Mapping[str, Any]:
        if not immutable_message_id:
            raise UnsafeOutlookAction("reply target is required")
        result = self._session.request_json(
            "POST",
            f"/v1.0/me/messages/{quote(immutable_message_id, safe='')}/createReply",
            scopes=DRAFT_SCOPES,
            retry_class=RetryClass.NON_IDEMPOTENT_WRITE,
            expected_statuses=(200, 201),
        )
        return _minimal_result(result, ("id", "conversationId", "webLink"))

    def update_reply_draft(self, draft_id: str, body: str) -> Mapping[str, Any]:
        if not draft_id or not isinstance(body, str):
            raise UnsafeOutlookAction("draft id and exact body are required")
        result = self._session.request_json(
            "PATCH",
            f"/v1.0/me/messages/{quote(draft_id, safe='')}",
            scopes=DRAFT_SCOPES,
            retry_class=RetryClass.IDEMPOTENT_WRITE,
            payload={"body": {"contentType": "text", "content": body}},
            expected_statuses=(200,),
        )
        return _minimal_result(result, ("id", "conversationId", "webLink", "isDraft"))

    def create_private_tentative_hold(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        start = payload.get("start")
        end = payload.get("end")
        transaction_id = payload.get("transactionId")
        if not isinstance(start, Mapping) or not isinstance(end, Mapping):
            raise UnsafeOutlookAction("hold requires start and end")
        if not isinstance(transaction_id, str) or not _TRANSACTION_ID_RE.fullmatch(transaction_id):
            raise UnsafeOutlookAction("hold requires a stable transactionId")
        start_text = start.get("dateTime")
        end_text = end.get("dateTime")
        if not isinstance(start_text, str) or not isinstance(end_text, str):
            raise UnsafeOutlookAction("hold times must be UTC timestamps")
        start_dt = parse_utc(start_text)
        end_dt = parse_utc(end_text)
        if end_dt <= start_dt:
            raise UnsafeOutlookAction("hold end must be after start")
        safe_payload = {
            "subject": "Private hold",
            "body": {"contentType": "text", "content": ""},
            "start": {"dateTime": start_text[:-1], "timeZone": "UTC"},
            "end": {"dateTime": end_text[:-1], "timeZone": "UTC"},
            "showAs": "tentative",
            "sensitivity": "private",
            "isReminderOn": False,
            "responseRequested": False,
            "allowNewTimeProposals": False,
            "attendees": [],
            "transactionId": transaction_id,
        }
        result = self._session.request_json(
            "POST",
            "/v1.0/me/events",
            scopes=HOLD_SCOPES,
            retry_class=RetryClass.IDEMPOTENT_WRITE,
            payload=safe_payload,
            preferences=('outlook.timezone="UTC"',),
            expected_statuses=(201,),
        )
        return _minimal_result(result, ("id", "changeKey", "transactionId", "webLink"))
