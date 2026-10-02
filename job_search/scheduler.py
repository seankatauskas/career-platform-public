"""SQLite-backed schedule materialization for the local job-search worker."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .contracts import canonical_json, parse_utc
from .db import connect


SCHEDULE_TIMEZONE = "America/Chicago"
CENTRAL = ZoneInfo(SCHEDULE_TIMEZONE)
WORKER_INTERVAL_MINUTES = 5
OPPORTUNITY_ROOT_TASKS = frozenset({"ats.authoritative", "ats.new_only"})
WORKFLOW_WATERMARK_KEYS = (
    "ats_ingested",
    "locations_ready",
    "recommendations_stable",
    "shortlist_evaluated",
    "salary_drained",
)
WORK_STATUSES = ("queued", "running", "succeeded", "dead", "cancelled")

_PLACEHOLDER_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "localhost", "test", "invalid"}
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


@dataclass(frozen=True)
class DefaultSchedule:
    schedule_key: str
    task_kind: str
    schedule: Mapping[str, Any]
    ats_task: bool = False
    outlook_task: bool = False
    notification_task: bool = False
    coalesce: bool = True


DEFAULT_SCHEDULES: tuple[DefaultSchedule, ...] = (
    DefaultSchedule(
        "system.worker.five_minute",
        "system.worker_tick",
        {"kind": "interval", "minutes": 5, "max_attempts": 1, "priority": -100},
    ),
    DefaultSchedule(
        "outlook.mail.five_minute",
        "outlook.mail.sync",
        {"kind": "interval", "minutes": 5, "max_attempts": 5, "priority": 80},
        outlook_task=True,
    ),
    DefaultSchedule(
        "outlook.actions.five_minute",
        "outlook.actions.execute",
        {"kind": "interval", "minutes": 5, "max_attempts": 5, "priority": 90},
        outlook_task=True,
    ),
    DefaultSchedule(
        "notification.delivery.five_minute",
        "notification.deliver",
        {"kind": "interval", "minutes": 5, "max_attempts": 5, "priority": 70},
        notification_task=True,
    ),
    DefaultSchedule(
        "notification.reminders.five_minute",
        "notification.reminders_due",
        {"kind": "interval", "minutes": 5, "max_attempts": 5, "priority": 65},
        notification_task=True,
    ),
    DefaultSchedule(
        "ats.authoritative.0200",
        "ats.authoritative",
        {"kind": "daily", "hour": 2, "minute": 0, "timezone": SCHEDULE_TIMEZONE,
         "max_attempts": 5, "priority": 50},
        True,
    ),
    *tuple(
        DefaultSchedule(
            f"ats.new_only.{hour:02d}00",
            "ats.new_only",
            {"kind": "daily", "hour": hour, "minute": 0,
             "timezone": SCHEDULE_TIMEZONE, "max_attempts": 5, "priority": 40},
            True,
        )
        for hour in (6, 10, 14, 18, 22)
    ),
    *tuple(
        DefaultSchedule(
            f"ats.discovery.recent.{hour:02d}00",
            "ats.refresh_recent",
            {"kind": "daily", "hour": hour, "minute": 0,
             "timezone": SCHEDULE_TIMEZONE, "max_attempts": 3, "priority": 50},
            True,
        )
        for hour in (5, 17)
    ),
)


def utc_stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        return parse_utc(value)
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def has_real_scraper_contact(environment: Mapping[str, str]) -> bool:
    raw = str(environment.get("JOB_SCRAPER_CONTACT") or "").strip()
    address = parseaddr(raw)[1].strip().casefold()
    if not _EMAIL_RE.fullmatch(address) or address.count("@") != 1:
        return False
    domain = address.rsplit("@", 1)[1].rstrip(".")
    if "." not in domain or domain in _PLACEHOLDER_DOMAINS:
        return False
    return not any(domain.endswith("." + item) for item in _PLACEHOLDER_DOMAINS)


def has_outlook_config(environment: Mapping[str, str]) -> bool:
    client_id = str(environment.get("OUTLOOK_CLIENT_ID") or "").strip()
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            client_id,
        )
    )


def has_notification_config(environment: Mapping[str, str]) -> bool:
    return bool(str(environment.get("JOB_SEARCH_NOTIFICATION_TARGET") or "").strip())


def _local_occurrences(day: datetime, hour: int, minute: int) -> list[datetime]:
    naive = datetime.combine(day.date(), time(hour, minute))
    results: dict[str, datetime] = {}
    for fold in (0, 1):
        local = naive.replace(tzinfo=CENTRAL, fold=fold)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(CENTRAL).replace(tzinfo=None) == naive:
            results[utc_stamp(utc)] = utc
    return [results[key] for key in sorted(results)]


def next_occurrence(schedule: Mapping[str, Any], after: datetime | str) -> datetime:
    after_utc = as_utc(after)
    kind = schedule.get("kind")
    if kind == "interval":
        minutes = int(schedule.get("minutes", 0))
        if not 1 <= minutes <= 24 * 60:
            raise ValueError("interval schedule minutes are invalid")
        step = minutes * 60
        epoch = int(after_utc.timestamp())
        return datetime.fromtimestamp((epoch // step + 1) * step, timezone.utc)
    if schedule.get("timezone") != SCHEDULE_TIMEZONE:
        raise ValueError(f"calendar schedules must use {SCHEDULE_TIMEZONE}")
    hour = int(schedule.get("hour", -1))
    minute = int(schedule.get("minute", -1))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("calendar schedule time is invalid")
    weekday = schedule.get("weekday")
    if kind == "weekly" and (not isinstance(weekday, int) or not 0 <= weekday <= 6):
        raise ValueError("weekly schedule weekday is invalid")
    if kind not in {"daily", "weekly"}:
        raise ValueError("unknown schedule kind")
    local_after = after_utc.astimezone(CENTRAL)
    for offset in range(0, 15):
        day = local_after + timedelta(days=offset)
        if kind == "weekly" and day.weekday() != weekday:
            continue
        for candidate in _local_occurrences(day, hour, minute):
            if candidate > after_utc:
                return candidate
    raise ValueError("could not resolve the next schedule occurrence")


def seed_default_schedules(
    db_path: Path,
    now: datetime,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    now_utc = as_utc(now)
    stamp = utc_stamp(now_utc)
    ats_enabled = has_real_scraper_contact(environment)
    outlook_enabled = has_outlook_config(environment)
    notifications_enabled = has_notification_config(environment)
    mail_minutes = environment.get("JOB_SEARCH_OUTLOOK_POLL_INTERVAL_MINUTES", "5")
    if not isinstance(mail_minutes, str) or not mail_minutes.isascii() or not mail_minutes.isdecimal() or not 1 <= int(mail_minutes) <= 1440:
        raise ValueError("Outlook poll interval must be an integer from 1 to 1440")
    with connect(db_path) as con:
        from .activation import disabled_tasks
        paused_tasks = set(disabled_tasks(con))
        # Retire the previous default without deleting its execution history.
        # Discovery and collection share the serialized core lane. Giving an
        # overdue discovery priority 50 puts it ahead of the 06:00/18:00 scans;
        # failed discovery backs off while those scans keep using known boards.
        con.execute(
            "UPDATE schedule_specs SET enabled=0,enabled_since=NULL,updated_at=? "
            "WHERE schedule_key='ats.discovery.recent.sunday_0300'",
            (stamp,),
        )
        con.execute(
            "UPDATE work_items SET status='cancelled',completed_at=?,last_error=? "
            "WHERE schedule_key='ats.discovery.recent.sunday_0300' AND status='queued'",
            (stamp, "Replaced by twice-daily discovery"),
        )
        for spec in DEFAULT_SCHEDULES:
            enabled = 1
            if spec.ats_task and not ats_enabled:
                enabled = 0
            if spec.outlook_task and not outlook_enabled:
                enabled = 0
            if spec.notification_task and not notifications_enabled:
                enabled = 0
            if spec.task_kind in paused_tasks:
                enabled = 0
            schedule = dict(spec.schedule)
            if spec.task_kind == "outlook.mail.sync":
                # Keep the historical schedule key and execution history.
                schedule["minutes"] = int(mail_minutes)
            schedule_json = canonical_json(schedule)
            due = utc_stamp(next_occurrence(schedule, now_utc))
            con.execute(
                "INSERT INTO schedule_specs "
                "(schedule_key,task_kind,schedule_json,enabled,coalesce,next_due_at,updated_at,enabled_since) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(schedule_key) DO UPDATE SET "
                "task_kind=excluded.task_kind,schedule_json=excluded.schedule_json,"
                "enabled_since=CASE WHEN excluded.enabled=0 THEN NULL "
                "WHEN schedule_specs.enabled=0 THEN excluded.enabled_since "
                "ELSE schedule_specs.enabled_since END,"
                "next_due_at=CASE WHEN (schedule_specs.enabled=0 AND excluded.enabled=1) "
                "OR schedule_specs.schedule_json<>excluded.schedule_json "
                "THEN excluded.next_due_at ELSE schedule_specs.next_due_at END,"
                "enabled=excluded.enabled,coalesce=excluded.coalesce,updated_at=excluded.updated_at",
                (
                    spec.schedule_key, spec.task_kind, schedule_json, enabled,
                    int(spec.coalesce), due, stamp, stamp if enabled else None,
                ),
            )
    return {
        "seeded": len(DEFAULT_SCHEDULES),
        "ats_enabled": ats_enabled,
        "outlook_enabled": outlook_enabled,
        "notifications_enabled": notifications_enabled,
        "timezone": SCHEDULE_TIMEZONE,
    }


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, value).hex}"


def _due_occurrences(
    schedule: Mapping[str, Any],
    first_due: datetime,
    now: datetime,
    *,
    coalesce: bool,
    limit: int,
) -> tuple[list[datetime], datetime]:
    if first_due > now:
        return [], first_due
    if schedule.get("kind") == "interval":
        minutes = int(schedule["minutes"])
        step = timedelta(minutes=minutes)
        elapsed = int((now - first_due).total_seconds() // step.total_seconds())
        latest = first_due + elapsed * step
        if coalesce:
            return [latest], latest + step
        count = min(elapsed + 1, limit)
        values = [first_due + index * step for index in range(count)]
        future = first_due + (elapsed + 1) * step
        return values, future
    values: list[datetime] = []
    current = first_due
    steps = 0
    while current <= now:
        if coalesce:
            values[:] = [current]
        elif len(values) < limit:
            values.append(current)
        current = next_occurrence(schedule, current)
        steps += 1
        if steps > 4_000:
            raise ValueError("calendar schedule catch-up exceeded safety bound")
    return values, current


def materialize_due_schedules(
    db_path: Path,
    now: datetime,
    *,
    max_schedules: int = 20,
    max_work_items: int = 50,
) -> dict[str, int]:
    if max_schedules < 1 or max_work_items < 1:
        raise ValueError("materialization bounds must be positive")
    now_utc = as_utc(now)
    stamp = utc_stamp(now_utc)
    created = 0
    coalesced = 0
    with connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT * FROM schedule_specs WHERE enabled=1 AND next_due_at<=? "
            "ORDER BY next_due_at,schedule_key LIMIT ?",
            (stamp, max_schedules),
        ).fetchall()
        for row in rows:
            schedule = json.loads(row["schedule_json"])
            occurrences, future = _due_occurrences(
                schedule,
                as_utc(row["next_due_at"]),
                now_utc,
                coalesce=bool(row["coalesce"]),
                limit=max_work_items - created,
            )
            outstanding = con.execute(
                "SELECT 1 FROM work_items WHERE schedule_key=? "
                "AND status IN ('queued','running') LIMIT 1",
                (row["schedule_key"],),
            ).fetchone()
            if outstanding and row["coalesce"]:
                coalesced += len(occurrences)
                occurrences = []
            for scheduled_for in occurrences:
                if created >= max_work_items:
                    break
                scheduled_stamp = utc_stamp(scheduled_for)
                dedupe_key = f"schedule:{row['schedule_key']}:{scheduled_stamp}"
                work_id = _stable_id("work", dedupe_key)
                payload = {
                    "schedule_key": row["schedule_key"],
                    "scheduled_for": scheduled_stamp,
                    "configuration": schedule.get("payload", {}),
                }
                cursor = con.execute(
                    "INSERT OR IGNORE INTO work_items "
                    "(work_id,schedule_key,task_kind,dedupe_key,payload_json,status,priority,"
                    "due_at,attempts,max_attempts,created_at,lane,workflow_id,parent_work_id) "
                    "VALUES (?,?,?,?,?,'queued',?,?,?,?,?,'core',?,NULL)",
                    (
                        work_id, row["schedule_key"], row["task_kind"], dedupe_key,
                        canonical_json(payload), int(schedule.get("priority", 0)),
                        scheduled_stamp, 0, int(schedule.get("max_attempts", 5)), stamp,
                        _stable_id("workflow", dedupe_key),
                    ),
                )
                created += int(cursor.rowcount > 0)
                if cursor.rowcount > 0 and row["task_kind"] in OPPORTUNITY_ROOT_TASKS:
                    con.execute(
                        "INSERT INTO workflow_runs "
                        "(workflow_id,workflow_kind,root_work_id,trigger_task_kind,"
                        "scheduled_for,status,started_at,last_error) "
                        "VALUES (?,'opportunity_refresh',?,?,?,'running',?,'')",
                        (
                            _stable_id("workflow", dedupe_key),
                            work_id,
                            row["task_kind"],
                            scheduled_stamp,
                            stamp,
                        ),
                    )
            con.execute(
                "UPDATE schedule_specs SET next_due_at=?,updated_at=? WHERE schedule_key=?",
                (utc_stamp(future), stamp, row["schedule_key"]),
            )
        con.commit()
    return {"schedules": len(rows), "created": created, "coalesced": coalesced}


def workflow_health(db_path: Path, now: datetime) -> dict[str, Any]:
    """Return a versioned, privacy-minimized view of pipeline progress."""

    stamp = utc_stamp(as_utc(now))
    with connect(db_path) as con:
        lane_counts = {
            lane: {status: 0 for status in WORK_STATUSES}
            for lane in ("core", "model")
        }
        for row in con.execute(
            "SELECT lane,status,COUNT(*) AS count FROM work_items GROUP BY lane,status"
        ):
            lane_counts[str(row["lane"])][str(row["status"])] = int(row["count"])
        lanes = {}
        for lane, counts in lane_counts.items():
            lease_name = "job-search-worker" if lane == "core" else "job-search-worker:model"
            lease = con.execute(
                "SELECT lease_name,owner,heartbeat_at,expires_at FROM worker_leases "
                "WHERE lease_name=?",
                (lease_name,),
            ).fetchone()
            lanes[lane] = {
                "counts": counts,
                "overdue": int(
                    con.execute(
                        "SELECT COUNT(*) FROM work_items "
                        "WHERE lane=? AND status='queued' AND due_at<?",
                        (lane, stamp),
                    ).fetchone()[0]
                ),
                "worker_lease": dict(lease) if lease else None,
            }

        latest_watermarks: dict[str, Any] = {}
        for key in WORKFLOW_WATERMARK_KEYS:
            row = con.execute(
                "SELECT workflow_id,work_id,reached_at,result_sha256 "
                "FROM workflow_watermarks WHERE watermark_key=? "
                "ORDER BY reached_at DESC,workflow_id DESC LIMIT 1",
                (key,),
            ).fetchone()
            latest_watermarks[key] = dict(row) if row else None

        workflows = []
        for row in con.execute(
            "SELECT * FROM workflow_runs ORDER BY scheduled_for DESC,workflow_id DESC LIMIT 20"
        ):
            item = dict(row)
            item["watermarks"] = {
                key: dict(value)
                for key, value in (
                    (str(value["watermark_key"]), value)
                    for value in con.execute(
                        "SELECT watermark_key,work_id,reached_at,result_sha256 "
                        "FROM workflow_watermarks WHERE workflow_id=? "
                        "ORDER BY reached_at,watermark_key",
                        (row["workflow_id"],),
                    )
                )
            }
            item["work_counts"] = {status: 0 for status in WORK_STATUSES}
            for value in con.execute(
                "SELECT status,COUNT(*) AS count FROM work_items "
                "WHERE workflow_id=? GROUP BY status",
                (row["workflow_id"],),
            ):
                item["work_counts"][str(value["status"])] = int(value["count"])
            workflows.append(item)
        return {
            "schema_version": 1,
            "checked_at": stamp,
            "lanes": lanes,
            "latest_watermarks": latest_watermarks,
            "workflows": workflows,
        }


def automation_health(db_path: Path, now: datetime) -> dict[str, Any]:
    stamp = utc_stamp(as_utc(now))
    with connect(db_path) as con:
        schedules = [dict(row) for row in con.execute(
            "SELECT schedule_key,task_kind,enabled,next_due_at,updated_at "
            "FROM schedule_specs ORDER BY schedule_key"
        )]
        work_counts = {
            str(row["status"]): int(row["count"])
            for row in con.execute(
                "SELECT status,COUNT(*) AS count FROM work_items GROUP BY status"
            )
        }
        outbox_counts = {
            str(row["status"]): int(row["count"])
            for row in con.execute(
                "SELECT status,COUNT(*) AS count FROM outbox_messages GROUP BY status"
            )
        }
        last_runs = []
        task_kinds = [row[0] for row in con.execute(
            "SELECT DISTINCT task_kind FROM work_items ORDER BY task_kind"
        )]
        for task_kind in task_kinds:
            latest = con.execute(
                "SELECT r.*,w.task_kind,w.attempts,w.last_error FROM job_runs r "
                "JOIN work_items w ON w.work_id=r.work_id WHERE w.task_kind=? "
                "ORDER BY r.started_at DESC LIMIT 1",
                (task_kind,),
            ).fetchone()
            success = con.execute(
                "SELECT r.completed_at FROM job_runs r JOIN work_items w ON w.work_id=r.work_id "
                "WHERE w.task_kind=? AND r.outcome='succeeded' "
                "ORDER BY r.completed_at DESC LIMIT 1",
                (task_kind,),
            ).fetchone()
            if latest:
                value = dict(latest)
                value["last_success_at"] = success[0] if success else None
                last_runs.append(value)
        lease = con.execute(
            "SELECT lease_name,owner,heartbeat_at,expires_at FROM worker_leases "
            "WHERE lease_name='job-search-worker'"
        ).fetchone()
        result = {
            "checked_at": stamp,
            "schedules": schedules,
            "work_counts": work_counts,
            "outbox_counts": outbox_counts,
            "overdue_work": int(con.execute(
                "SELECT COUNT(*) FROM work_items WHERE status='queued' AND due_at<?",
                (stamp,),
            ).fetchone()[0]),
            "expired_work_leases": int(con.execute(
                "SELECT COUNT(*) FROM work_items WHERE status='running' AND lease_expires_at<=?",
                (stamp,),
            ).fetchone()[0]),
            "last_runs": last_runs,
            "worker_lease": dict(lease) if lease else None,
        }
    result["workflow"] = workflow_health(db_path, now)
    return result
