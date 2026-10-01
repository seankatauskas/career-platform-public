"""Deterministic, privacy-minimized interview availability planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .contracts import CalendarBlock, OutlookTransport, parse_utc


BLOCKING_SHOW_AS = frozenset({"busy", "tentative", "oof", "unknown"})
NONBLOCKING_SHOW_AS = frozenset({"free", "workingElsewhere"})


class AvailabilityConflict(RuntimeError):
    """A previously proposed slot is no longer available."""


@dataclass(frozen=True)
class AvailabilityPolicy:
    timezone_name: str = "America/Chicago"
    workday_start_hour: int = 8
    workday_end_hour: int = 18
    minimum_notice_minutes: int = 24 * 60
    preparation_minutes: int = 30
    recovery_minutes: int = 15
    default_duration_minutes: int = 30
    horizon_days: int = 14
    start_increment_minutes: int = 30
    max_slots: int = 3
    max_slots_per_day: int = 2

    def validate(self) -> None:
        ZoneInfo(self.timezone_name)
        if not 0 <= self.workday_start_hour < self.workday_end_hour <= 24:
            raise ValueError("working hours are invalid")
        for value in (
            self.minimum_notice_minutes,
            self.preparation_minutes,
            self.recovery_minutes,
            self.default_duration_minutes,
            self.start_increment_minutes,
            self.max_slots,
            self.max_slots_per_day,
        ):
            if value <= 0:
                raise ValueError("availability policy values must be positive")
        if self.horizon_days != 14:
            raise ValueError("v1 calendar horizon is fixed at 14 days")


@dataclass(frozen=True)
class InterviewSlot:
    starts_at: str
    ends_at: str
    timezone_name: str = "America/Chicago"

    @property
    def start(self) -> datetime:
        return parse_utc(self.starts_at)

    @property
    def end(self) -> datetime:
        return parse_utc(self.ends_at)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _round_up_local(value: datetime, increment_minutes: int) -> datetime:
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((value - midnight).total_seconds() // 60)
    rounded = ((elapsed + increment_minutes - 1) // increment_minutes) * increment_minutes
    if value.second or value.microsecond:
        rounded = ((elapsed + increment_minutes) // increment_minutes) * increment_minutes
    return midnight + timedelta(minutes=rounded)


def is_blocking(block: CalendarBlock) -> bool:
    if block.is_cancelled:
        return False
    if block.show_as in NONBLOCKING_SHOW_AS:
        return False
    return block.show_as in BLOCKING_SHOW_AS or block.show_as not in NONBLOCKING_SHOW_AS


def _blocking_intervals(
    blocks: Iterable[CalendarBlock], ignore_remote_ids: Iterable[str] = ()
) -> Tuple[Tuple[datetime, datetime], ...]:
    ignored = frozenset(ignore_remote_ids)
    intervals = []
    for block in blocks:
        if block.remote_id in ignored or not is_blocking(block):
            continue
        start = parse_utc(block.starts_at)
        end = parse_utc(block.ends_at)
        if end > start:
            intervals.append((start, end))
    return tuple(sorted(intervals))


def slot_is_available(
    starts_at: datetime,
    ends_at: datetime,
    blocks: Iterable[CalendarBlock],
    policy: AvailabilityPolicy,
    *,
    ignore_remote_ids: Iterable[str] = (),
) -> bool:
    protected_start = starts_at - timedelta(minutes=policy.preparation_minutes)
    protected_end = ends_at + timedelta(minutes=policy.recovery_minutes)
    return not any(
        protected_start < block_end and protected_end > block_start
        for block_start, block_end in _blocking_intervals(blocks, ignore_remote_ids)
    )


class AvailabilityPlanner:
    """Reads only CalendarBlock values and never retains private calendar fields."""

    def __init__(
        self,
        outlook: OutlookTransport,
        policy: AvailabilityPolicy = AvailabilityPolicy(),
    ) -> None:
        policy.validate()
        self._outlook = outlook
        self.policy = policy
        self.timezone = ZoneInfo(policy.timezone_name)

    def propose_slots(
        self,
        *,
        now: Optional[datetime] = None,
        duration_minutes: Optional[int] = None,
    ) -> Sequence[InterviewSlot]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        duration = duration_minutes or self.policy.default_duration_minutes
        if duration < 15 or duration > 240 or duration % 15:
            raise ValueError("interview duration must be a 15-minute multiple from 15 to 240")
        horizon = current + timedelta(days=self.policy.horizon_days)
        blocks = self._outlook.read_calendar_view(_utc_text(current), _utc_text(horizon))
        earliest = current + timedelta(minutes=self.policy.minimum_notice_minutes)
        slots = []
        per_day = {}
        day = current.astimezone(self.timezone).date()
        last_day = horizon.astimezone(self.timezone).date()
        while day <= last_day and len(slots) < self.policy.max_slots:
            if day.weekday() < 5:
                self._collect_day(
                    day,
                    earliest,
                    horizon,
                    duration,
                    blocks,
                    slots,
                    per_day,
                )
            day += timedelta(days=1)
        return tuple(slots)

    def _collect_day(
        self,
        day: date,
        earliest: datetime,
        horizon: datetime,
        duration_minutes: int,
        blocks: Sequence[CalendarBlock],
        slots: list,
        per_day: dict,
    ) -> None:
        local_start = datetime.combine(
            day, time(self.policy.workday_start_hour), tzinfo=self.timezone
        )
        local_end = datetime.combine(
            day, time(self.policy.workday_end_hour), tzinfo=self.timezone
        )
        candidate = local_start
        earliest_local = earliest.astimezone(self.timezone)
        if candidate < earliest_local:
            candidate = _round_up_local(
                earliest_local, self.policy.start_increment_minutes
            )
        duration = timedelta(minutes=duration_minutes)
        increment = timedelta(minutes=self.policy.start_increment_minutes)
        while (
            candidate + duration <= local_end
            and len(slots) < self.policy.max_slots
            and per_day.get(day, 0) < self.policy.max_slots_per_day
        ):
            start_utc = candidate.astimezone(timezone.utc)
            end_utc = (candidate + duration).astimezone(timezone.utc)
            if (
                start_utc >= earliest
                and end_utc + timedelta(minutes=self.policy.recovery_minutes) <= horizon
                and slot_is_available(start_utc, end_utc, blocks, self.policy)
            ):
                slots.append(
                    InterviewSlot(
                        _utc_text(start_utc),
                        _utc_text(end_utc),
                        self.policy.timezone_name,
                    )
                )
                per_day[day] = per_day.get(day, 0) + 1
            candidate += increment

    def revalidate(
        self,
        starts_at: str,
        ends_at: str,
        *,
        ignore_remote_ids: Iterable[str] = (),
        now: Optional[datetime] = None,
    ) -> bool:
        start = parse_utc(starts_at)
        end = parse_utc(ends_at)
        self.validate_slot(start, end, now=now)
        query_start = start - timedelta(minutes=self.policy.preparation_minutes)
        query_end = end + timedelta(minutes=self.policy.recovery_minutes)
        blocks = self._outlook.read_calendar_view(
            _utc_text(query_start), _utc_text(query_end)
        )
        return slot_is_available(
            start,
            end,
            blocks,
            self.policy,
            ignore_remote_ids=ignore_remote_ids,
        )

    def validate_slot(
        self,
        start: datetime,
        end: datetime,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        if end <= start:
            raise ValueError("slot end must be after start")
        duration_seconds = (end - start).total_seconds()
        if (
            duration_seconds < 15 * 60
            or duration_seconds > 240 * 60
            or duration_seconds % (15 * 60)
        ):
            raise ValueError("slot duration must be a 15-minute multiple from 15 to 240")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        if start < current + timedelta(minutes=self.policy.minimum_notice_minutes):
            raise ValueError("slot violates minimum notice")
        if end + timedelta(minutes=self.policy.recovery_minutes) > current + timedelta(
            days=self.policy.horizon_days
        ):
            raise ValueError("slot exceeds the calendar horizon")
        local_start = start.astimezone(self.timezone)
        local_end = end.astimezone(self.timezone)
        if local_start.date() != local_end.date() or local_start.weekday() >= 5:
            raise ValueError("slot must remain within one weekday")
        working_start = datetime.combine(
            local_start.date(), time(self.policy.workday_start_hour), tzinfo=self.timezone
        )
        working_end = datetime.combine(
            local_start.date(), time(self.policy.workday_end_hour), tzinfo=self.timezone
        )
        if local_start < working_start or local_end > working_end:
            raise ValueError("slot is outside working hours")

    def require_fresh(
        self, starts_at: str, ends_at: str, *, now: Optional[datetime] = None
    ) -> None:
        if not self.revalidate(starts_at, ends_at, now=now):
            raise AvailabilityConflict("calendar changed after the slot was proposed")
