"""Personal-account calendar availability through paged calendarView."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

from job_search.contracts import CalendarBlock, parse_utc

from .auth import BASE_SCOPES
from .transport import GraphSession, RetryClass, validate_graph_url


CALENDAR_SELECT = (
    "id,changeKey,start,end,isAllDay,isCancelled,showAs,type,"
    "seriesMasterId,lastModifiedDateTime"
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
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _calendar_block(item: Mapping[str, Any]) -> CalendarBlock:
    remote_id = item.get("id")
    if not isinstance(remote_id, str) or not remote_id:
        raise ValueError("calendar event is missing an immutable id")
    show_as = str(item.get("showAs") or "unknown")
    if show_as not in {"free", "tentative", "busy", "oof", "workingElsewhere", "unknown"}:
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
            if not isinstance(values, list) or any(not isinstance(item, Mapping) for item in values):
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
