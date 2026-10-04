"""Personal-account calendar availability through paged calendarView."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode

from job_search.contracts import CalendarBlock, parse_utc

from .auth import BASE_SCOPES
from .transport import GraphSession, RetryClass, validate_graph_url


CALENDAR_SELECT = (
    "id,changeKey,start,end,isAllDay,isCancelled,showAs,type," "seriesMasterId"
)
MAX_CALENDAR_PAGES = 32
MAX_CALENDAR_BLOCKS = 10_000


def _graph_time(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("calendar event time must be an object")
    raw = value.get("dateTime")
    zone = value.get("timeZone")
    if not isinstance(raw, str):
        raise ValueError("calendar event time is missing dateTime")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    # Exchange commonly emits seven fractional digits while Python's ISO parser
    # accepts at most microseconds on the oldest supported Python versions.
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", normalized, count=1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("calendar event returned an invalid dateTime") from exc
    if parsed.tzinfo is None:
        if str(zone).upper() not in {"UTC", "ETC/UTC"}:
            raise ValueError("calendar event did not honor the UTC preference")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _calendar_block(item: Mapping[str, Any]) -> CalendarBlock:
    remote_id = item.get("id")
    if not isinstance(remote_id, str) or not remote_id:
        raise ValueError("calendar event is missing an immutable id")
    show_as = str(item.get("showAs") or "unknown")
    if show_as not in {
        "free",
        "tentative",
        "busy",
        "oof",
        "workingElsewhere",
        "unknown",
    }:
        show_as = "unknown"
    return CalendarBlock(
        remote_id=remote_id,
        starts_at=_graph_time(item.get("start")),
        ends_at=_graph_time(item.get("end")),
        show_as=show_as,
        is_all_day=bool(item.get("isAllDay", False)),
        is_cancelled=bool(item.get("isCancelled", False)),
        change_key=str(item.get("changeKey") or ""),
    )


class GraphCalendarClient:
    def __init__(self, session: GraphSession) -> None:
        self._session = session

    def read_interview_event(self, remote_id: str) -> Mapping[str, Any]:
        """Read a previously reviewed immutable identity, including cancellations.

        A 404 is not interpreted as cancellation: it can also mean access loss or
        deletion of the local copy. The worker surfaces the failure for recovery.
        """
        if not isinstance(remote_id, str) or not remote_id or len(remote_id) > 2000:
            raise ValueError("invalid calendar identity")
        payload = self._session.request_json(
            "GET",
            "/v1.0/me/events/" + quote(remote_id, safe=""),
            scopes=BASE_SCOPES,
            retry_class=RetryClass.READ,
            preferences=('outlook.timezone="UTC"',),
        )
        return _interview_event(payload)

    def read_interview_events(
        self,
        starts_at: str,
        ends_at: str,
        *,
        max_pages: int = MAX_CALENDAR_PAGES,
        max_events: int = 500,
    ) -> Sequence[Mapping[str, Any]]:
        """Read a bounded scheduling window without assuming absence is removal.

        Graph calendarView does not support selecting lastModifiedDateTime. This
        reader intentionally omits $select and immediately strips private content.
        See https://learn.microsoft.com/graph/api/calendar-list-calendarview
        and https://learn.microsoft.com/graph/api/resources/event.
        """
        start, end = parse_utc(starts_at), parse_utc(ends_at)
        if end <= start or end - start > timedelta(days=14):
            raise ValueError("interview calendar window must be at most 14 days")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= MAX_CALENDAR_PAGES
        ):
            raise ValueError("invalid interview calendar page bound")
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= 500
        ):
            raise ValueError("invalid interview calendar event bound")
        params = urlencode(
            {"startDateTime": starts_at, "endDateTime": ends_at, "$top": "100"}
        )
        next_url = validate_graph_url("/v1.0/me/calendar/calendarView?" + params)
        seen = set()
        events = []
        while next_url:
            if next_url in seen or len(seen) >= max_pages:
                raise ValueError(
                    "interview calendar paging exceeded its bound or cycled"
                )
            seen.add(next_url)
            payload = self._session.request_json(
                "GET",
                next_url,
                scopes=BASE_SCOPES,
                retry_class=RetryClass.READ,
                preferences=('outlook.timezone="UTC"',),
            )
            values = payload.get("value", [])
            if not isinstance(values, list) or any(
                not isinstance(v, Mapping) for v in values
            ):
                raise ValueError("calendarView value must be an array of objects")
            if len(events) + len(values) > max_events:
                raise ValueError("interview calendar event count exceeded its bound")
            events.extend(_interview_event(v) for v in values)
            candidate = payload.get("@odata.nextLink")
            if candidate is None:
                next_url = ""
            elif isinstance(candidate, str):
                next_url = validate_graph_url(candidate)
            else:
                raise ValueError("calendarView nextLink must be a URL")
        return tuple(events)

    def read_agenda(self, starts_at: str, ends_at: str):
        """Complete primary-calendar occurrences; private text is never retained."""
        start, end = parse_utc(starts_at), parse_utc(ends_at)
        if end <= start or end-start > timedelta(days=14, hours=1):
            raise ValueError("agenda window must be at most 14 local days")
        next_url = validate_graph_url("/v1.0/me/calendar/calendarView?" + urlencode({
            "startDateTime": starts_at, "endDateTime": ends_at, "$top": "1000",
            "$select": "id,subject,start,end,isAllDay,isCancelled,isDraft,showAs,sensitivity,responseStatus,type,seriesMasterId,originalStartTimeZone,originalEndTimeZone",
        }))
        seen, items, identities = set(), [], set()
        while next_url:
            if next_url in seen or len(seen) >= MAX_CALENDAR_PAGES:
                raise ValueError("agenda paging incomplete")
            seen.add(next_url)
            page = self._session.request_json("GET", next_url, scopes=BASE_SCOPES,
                retry_class=RetryClass.READ, preferences=('outlook.timezone="UTC"',))
            values = page.get("value")
            if not isinstance(values, list) or len(items)+len(values) > MAX_CALENDAR_BLOCKS:
                raise ValueError("agenda exceeds complete snapshot bounds")
            for event in values:
                block = _calendar_block(event)
                if block.remote_id in identities:
                    raise ValueError("agenda repeated occurrence identity")
                identities.add(block.remote_id)
                private = event.get("sensitivity") in {"private", "confidential", "personal"}
                title = "Private commitment" if private else str(event.get("subject") or "Calendar commitment")[:300]
                title = "".join(c for c in title if ord(c) >= 32)
                items.append({"id":block.remote_id, "source_ref":block.remote_id,
                    "starts_at":block.starts_at,"ends_at":block.ends_at,"title":title,
                    "status":"cancelled" if block.is_cancelled else "draft" if event.get("isDraft") else str((event.get("responseStatus") or {}).get("response") or "unknown"),
                    "show_as":block.show_as,"is_all_day":block.is_all_day,"private":private,
                    "type":str(event.get("type") or "singleInstance"),
                    "series_master_id":str(event.get("seriesMasterId") or ""),
                    "original_start_time_zone":str(event.get("originalStartTimeZone") or "UTC"),
                    "original_end_time_zone":str(event.get("originalEndTimeZone") or "UTC")})
            candidate = page.get("@odata.nextLink")
            next_url = validate_graph_url(candidate) if candidate else ""
        return items

    def read_calendar_view(
        self,
        starts_at: str,
        ends_at: str,
        *,
        max_pages: int = MAX_CALENDAR_PAGES,
        max_blocks: int = MAX_CALENDAR_BLOCKS,
    ) -> Sequence[CalendarBlock]:
        start = parse_utc(starts_at)
        end = parse_utc(ends_at)
        if end <= start:
            raise ValueError("calendar view end must be after start")
        if end - start > timedelta(days=14):
            raise ValueError("calendar view is limited to 14 days")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= MAX_CALENDAR_PAGES
        ):
            raise ValueError("calendar page bound is invalid")
        if (
            isinstance(max_blocks, bool)
            or not isinstance(max_blocks, int)
            or not 1 <= max_blocks <= MAX_CALENDAR_BLOCKS
        ):
            raise ValueError("calendar block bound is invalid")
        params = urlencode(
            {
                "startDateTime": starts_at,
                "endDateTime": ends_at,
                "$select": CALENDAR_SELECT,
                "$top": "1000",
            }
        )
        next_url = validate_graph_url(f"/v1.0/me/calendar/calendarView?{params}")
        blocks = []
        seen_urls: set[str] = set()
        while next_url:
            if next_url in seen_urls:
                raise ValueError("calendarView paging cycle detected")
            if len(seen_urls) >= max_pages:
                raise ValueError("calendarView paging exceeded its bound")
            seen_urls.add(next_url)
            payload = self._session.request_json(
                "GET",
                next_url,
                scopes=BASE_SCOPES,
                retry_class=RetryClass.READ,
                preferences=('outlook.timezone="UTC"',),
            )
            values = payload.get("value", [])
            if not isinstance(values, list) or any(
                not isinstance(item, Mapping) for item in values
            ):
                raise ValueError("calendarView value must be an array of objects")
            if len(blocks) + len(values) > max_blocks:
                raise ValueError("calendarView event count exceeded its bound")
            blocks.extend(_calendar_block(item) for item in values)
            candidate = payload.get("@odata.nextLink")
            if candidate is None:
                next_url = ""
            elif isinstance(candidate, str):
                next_url = validate_graph_url(candidate)
            else:
                raise ValueError("calendarView nextLink must be a URL")
        return tuple(blocks)


def _interview_event(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """Keep only scheduling evidence; never retain event body/subject."""
    block = _calendar_block(item)
    organizer = item.get("organizer") or {}
    organizer_email = (
        organizer.get("emailAddress") or {} if isinstance(organizer, Mapping) else {}
    )
    participants = []
    for attendee in (item.get("attendees") or [])[:100]:
        if isinstance(attendee, Mapping):
            address = attendee.get("emailAddress") or {}
            if isinstance(address, Mapping) and isinstance(address.get("address"), str):
                participants.append(address["address"][:500])
    location = item.get("location") or {}
    meeting = item.get("onlineMeeting") or {}
    modified = item.get("lastModifiedDateTime")
    if not isinstance(modified, str):
        raise ValueError("calendar event is missing modification time")
    parse_utc(modified)
    return {
        "remote_id": block.remote_id,
        "ical_uid": str(item.get("iCalUId") or ""),
        "change_key": block.change_key,
        "modified_at": modified,
        "starts_at": block.starts_at,
        "ends_at": block.ends_at,
        "is_cancelled": block.is_cancelled,
        "is_organizer": item.get("isOrganizer")
        if isinstance(item.get("isOrganizer"), bool)
        else None,
        "organizer": str(organizer_email.get("address") or ""),
        "participants": participants,
        "location": str(location.get("displayName") or "")[:2000]
        if isinstance(location, Mapping)
        else "",
        "join_url": str(meeting.get("joinUrl") or "")
        if isinstance(meeting, Mapping)
        else "",
    }
