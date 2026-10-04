"""Read-only, bounded runtime measurements from the application work ledger.

Run ``python -m job_search.efficiency --db /private/job-search.db --days 7``.
Only aggregate counts and timestamps/durations leave the reader. No models,
providers, credentials, migrations, or worker handlers are invoked.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

from .activation import GROUPS

KNOWN_TASKS = frozenset(task for tasks in GROUPS.values() for task in tasks) | {
    "system.worker_tick", "outlook.reminders.publish",
}
STATUSES = frozenset({"queued", "running", "succeeded", "dead", "cancelled"})
MEMORY_COUNTERS = {
    "process_rss": ("process_rss_bytes", "maximum", "MAX"),
    "cgroup_anonymous": ("cgroup_anon_bytes", "maximum", "MAX"),
    "cgroup_file": ("cgroup_file_bytes", "maximum", "MAX"),
    "host_available": ("host_available_bytes", "minimum", "MIN"),
}


def _memory_columns():
    # Extract only fixed numeric counters in SQL; never return arbitrary result
    # payloads to the report reader. Legacy/non-JSON results remain unavailable.
    columns = []
    for alias, (counter, statistic, aggregate) in MEMORY_COUNTERS.items():
        path = '$.counters.' + counter + '.' + statistic
        columns.append(
            f"(SELECT {aggregate}(CASE WHEN j.type='object' THEN "
            f"CASE WHEN json_type(j.value,'{path}')='integer' "
            f"THEN json_extract(j.value,'{path}') END END) "
            "FROM json_each(CASE WHEN json_valid(r.result_json) THEN r.result_json ELSE '{}' END,"
            f"'$.command_memory.commands') j) AS memory_{alias}")
    return ','.join(columns)


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "zero_seconds_samples": 0, "p50_seconds": None, "p95_seconds": None, "max_seconds": None}
    return {
        "samples": len(ordered),
        "zero_seconds_samples": sum(value == 0 for value in ordered),
        "p50_seconds": round(ordered[math.ceil(len(ordered) * .50) - 1], 3),
        "p95_seconds": round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
        "max_seconds": round(ordered[-1], 3),
    }


def runtime_report(db: Path, *, days: int = 7, max_rows: int = 100000,
                   now: datetime | None = None) -> dict:
    """Summarize recent activity plus current backlog; reject unsupported schemas.

    Quantiles use nearest rank. Retry history is not reconstructed: job_runs
    holds only the most recent attempt, whereas work_items preserves first claim.
    The current due_at is mutable, so dispatch delay is limited to successful or
    running first attempts. Creation-to-claim includes deliberate scheduling.
    """
    if type(days) is not int or not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366")
    if type(max_rows) is not int or not 1 <= max_rows <= 100000:
        raise ValueError("max_rows must be between 1 and 100000")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(timezone.utc)
    since = now - timedelta(days=days)
    db = Path(db).resolve(strict=True)
    started = time.monotonic()
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        con.execute("PRAGMA query_only=ON")
        con.set_progress_handler(lambda: int(time.monotonic() - started > 5), 10000)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT w.task_kind,w.lane,w.status,w.attempts,w.created_at,"
            "w.started_at,w.completed_at,w.due_at,r.started_at AS attempt_start,"
            "r.completed_at AS attempt_end,r.outcome AS attempt_outcome," + _memory_columns() + " "
            "FROM work_items w LEFT JOIN job_runs r ON r.work_id=w.work_id "
            "WHERE w.status IN ('queued','running') OR "
            "COALESCE(r.completed_at,w.completed_at,r.started_at,w.started_at,w.created_at)>=? "
            "ORDER BY w.created_at DESC,w.work_id DESC LIMIT ?",
            (_stamp(since), max_rows + 1),
        ).fetchall()
    finally:
        con.close()
    truncated = len(rows) > max_rows
    groups = {}
    intervals = []
    invalid_timestamps = 0
    included = 0
    for row in rows[:max_rows]:
        stamps = {key: _date(row[key]) for key in (
            "created_at", "started_at", "completed_at", "due_at", "attempt_start", "attempt_end")}
        invalid_timestamps += sum(row[key] is not None and value is None for key, value in stamps.items())
        # The SQL predicate cheaply narrows canonical stored UTC timestamps;
        # enforce the exact window here, including future-data exclusion.
        activity = stamps["attempt_end"] or stamps["completed_at"] or stamps["attempt_start"] or stamps["started_at"] or stamps["created_at"]
        backlog = row["status"] in {"queued", "running"}
        if not stamps["created_at"] or stamps["created_at"] > now:
            continue
        if not backlog and (activity is None or not since <= activity <= now):
            continue
        task = row["task_kind"] if row["task_kind"] in KNOWN_TASKS else "other"
        lane = row["lane"] if row["lane"] in {"core", "model"} else "other"
        key = (lane, task)
        group = groups.setdefault(key, {"lane": lane, "task_kind": task,
            "statuses": Counter(), "work_items": 0, "retried_work_items": 0,
            "latest_attempt_duration": [], "creation_to_first_claim": [],
            "first_attempt_dispatch_delay": [], "queued_age": [], "queued_overdue": [],
            "command_memory": {name: [] for name in MEMORY_COUNTERS}})
        included += 1
        group["work_items"] += 1
        group["statuses"][row["status"] if row["status"] in STATUSES else "other"] += 1
        attempts = row["attempts"] if type(row["attempts"]) is int else 0
        group["retried_work_items"] += int(attempts > 1)
        for name in MEMORY_COUNTERS:
            value = row['memory_' + name]
            if type(value) is int and value >= 0:
                group['command_memory'][name].append(value)

        def duration(name, end, begin):
            if end is not None and begin is not None and begin <= end <= now:
                group[name].append((end - begin).total_seconds())

        if row["attempt_outcome"] in {"succeeded", "failed", "dead"}:
            duration("latest_attempt_duration", stamps["attempt_end"], stamps["attempt_start"])
            begin, end = stamps["attempt_start"], stamps["attempt_end"]
            if begin is not None and end is not None and begin <= end <= now:
                intervals.append({"task_kind": task, "lane": lane,
                    "started_at": _stamp(begin), "completed_at": _stamp(end),
                    "outcome": row["attempt_outcome"], "duration_seconds": (end - begin).total_seconds()})
        duration("creation_to_first_claim", stamps["started_at"], stamps["created_at"])
        if attempts == 1 and row["status"] in {"succeeded", "running"} and stamps["due_at"]:
            duration("first_attempt_dispatch_delay", stamps["started_at"], max(stamps["created_at"], stamps["due_at"]))
        if row["status"] == "queued":
            duration("queued_age", now, stamps["created_at"])
            if stamps["due_at"]:
                duration("queued_overdue", now, max(stamps["created_at"], stamps["due_at"]))
    for group in groups.values():
        group["statuses"] = dict(sorted(group["statuses"].items()))
        for name in ("latest_attempt_duration", "creation_to_first_claim", "first_attempt_dispatch_delay", "queued_age", "queued_overdue"):
            group[name] = _summary(group[name])
        group['command_memory'] = {
            name: {'recorded_attempts': len(values),
                   'minimum_bytes' if name == 'host_available' else 'maximum_bytes':
                   (min(values) if name == 'host_available' else max(values)) if values else None}
            for name, values in group['command_memory'].items()}
    has_memory = any(counter['recorded_attempts'] for group in groups.values()
                     for counter in group['command_memory'].values())
    return {"schema_version": 1, "observed_at": _stamp(now), "window_start": _stamp(since),
        "scope": "recent work activity plus current queued/running work",
        "included_work_items": included, "row_limit": max_rows, "truncated": truncated,
        "invalid_timestamp_values": invalid_timestamps,
        "historical_memory": {"status": "sampled" if has_memory else "unavailable",
            "reason": "recorded command samples only; older attempts may lack samples"},
        "limitations": [
            "Durations describe the latest recorded attempt only, not cumulative retry time or CPU time.",
            "Second-resolution or historical timestamps can record zero duration; this does not establish that no work occurred.",
            "Creation-to-first-claim includes deliberate scheduling; dispatch delay excludes retrying work.",
            "Queued delays may include paused automation or provider throttling, not only resource contention.",
            "Rows are bounded by newest creation time; a truncated report is not a complete backlog census.",
            "Memory extrema are sampled observations, not guaranteed peaks; process RSS excludes children and cgroup memory includes shared workloads/cache.",
            "No unrecorded historical memory or provider dollar costs are inferred.",
        ], "longest_attempts": sorted(intervals, key=lambda value: (-value["duration_seconds"], value["started_at"], value["task_kind"]))[:10],
        "tasks": [groups[key] for key in sorted(groups)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Application ledger, opened read-only")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max-rows", type=int, default=100000)
    args = parser.parse_args()
    try:
        result = runtime_report(args.db, days=args.days, max_rows=args.max_rows)
    except (OSError, ValueError, sqlite3.Error):
        # Database paths and exception text can contain private values.
        parser.exit(2, "Runtime report unavailable: check the database schema, read access, and limits.\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
