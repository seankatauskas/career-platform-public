"""Passive ranking coverage and queue state; never starts inference or prepares data."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from .refresh import POLICIES, inspect_policies, policy_runs
from ..activation import disabled_tasks


def _read(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5))


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _attempt_progress(progress, work, now):
    """A saved journal is not evidence that the currently claimed attempt wrote it."""
    if not work or not isinstance(progress, dict) or progress.get("sample") is True:
        return None
    revision = _count(progress.get("invocation_revision"))
    if progress.get("invocation_work_id") != work["work_id"] or revision is None:
        return None
    attempt = _timestamp(work["attempt_started_at"])
    started, updated = _timestamp(progress.get("started_at")), _timestamp(progress.get("updated_at"))
    if any(value is None for value in (attempt, started, updated)) or not attempt <= started <= updated <= now:
        return None
    status = progress.get("status")
    if status not in {"checking", "preparing", "scoring", "embedding_and_scoring", "succeeded"}:
        return None
    if work["status"] == "running":
        if (revision != work["recovery_revision"] or work["attempt_completed_at"] is not None
                or work["attempt_outcome"] is not None):
            return None
    elif work["status"] == "succeeded":
        completed = _timestamp(work["attempt_completed_at"])
        # Worker completion stamps have whole-second precision; the journal does not.
        if (revision + 1 != work["recovery_revision"] or status != "succeeded"
                or work["attempt_outcome"] != "succeeded" or completed is None
                or updated >= completed + timedelta(seconds=1)):
            return None
    else:
        return None
    total = _count(progress.get("total_families"))
    checked = _count(progress.get("checked_families", progress.get("processed_families")))
    if total is not None and checked is not None and checked > total:
        return None
    reused = progress.get("reused") is True
    if reused and (status != "succeeded" or checked != 0):
        return None
    policies = {}
    recorded_policies = progress.get("policies")
    if not isinstance(recorded_policies, dict):
        recorded_policies = {}
    for policy in POLICIES:
        counts = recorded_policies.get(policy)
        if not isinstance(counts, dict):
            continue
        recomputed = _count(counts.get("recomputed_families", counts.get("updated_families")))
        cached = _count(counts.get("reused_families"))
        if cached is not None and total is not None and cached > total:
            cached = None
        if recomputed is not None and (total is None or recomputed <= total):
            policies[policy] = {"recomputed_families": recomputed, "reused_families": cached}
    return {"status": status, "started_at": progress["started_at"], "updated_at": progress["updated_at"],
            "checked_families": checked, "total_families": total, "reused": reused,
            "policies": policies}


def ranking_progress(config) -> dict:
    with _read(config.application_db) as con:
        con.row_factory = sqlite3.Row
        paused = "opportunity.preference_refresh" in disabled_tasks(con)
        work = con.execute(
            "SELECT w.work_id,w.recovery_revision,w.status,w.failure_kind,w.due_at,r.started_at AS attempt_started_at,"
            "r.completed_at AS attempt_completed_at,r.outcome AS attempt_outcome FROM work_items w "
            "LEFT JOIN job_runs r ON r.work_id=w.work_id "
            "WHERE w.task_kind='opportunity.preference_refresh' "
            "ORDER BY CASE w.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,w.created_at DESC LIMIT 1"
        ).fetchone()
    state = "paused" if paused else "idle"
    retry_at = None
    if work and not paused:
        state = work["status"] if work["status"] in {"queued", "running", "dead"} else "idle"
        if state == "queued" and work["failure_kind"] == "usage_deferred":
            state, retry_at = "waiting_allowance", work["due_at"]
        elif state == "queued" and work["failure_kind"] == "inference_waiting":
            state, retry_at = "waiting_provider", work["due_at"]
    result = {"state": state, "retry_at": retry_at, "available": False, "current_pass": None, "last_pass": None}
    try:
        with _read(config.jobs_db) as jobs:
            jobs.execute("BEGIN")
            postings = jobs.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            families = {r[0] for r in jobs.execute(
                "SELECT f.family_id FROM job_families f JOIN jobs j "
                "ON j.ats=f.canonical_ats AND j.id=f.canonical_job_id"
            )}
        runs = policy_runs(config.proxy_db)
        with _read(config.preference_db) as scores:
            scores.execute("BEGIN")
            policies = {}
            for policy in POLICIES:
                ranked = {r[0] for r in scores.execute(
                    "SELECT family_id FROM preference_scores WHERE run_id=?", (runs.get(policy),)
                )} & families
                policies[policy] = {"ranked_families": len(ranked), "unranked_families": len(families - ranked)}
            row = scores.execute("SELECT value FROM preference_state WHERE key='policy_refresh_progress'").fetchone()
            try:
                progress = json.loads(row[0]) if row else {}
            except (ValueError, TypeError):
                progress = None  # A damaged journal does not invalidate saved coverage.
        freshness = inspect_policies(config.preference_db, config.proxy_db, config.jobs_db)
        for policy, counts in policies.items():
            counts["freshness"] = freshness[policy]["status"]
        matched = _attempt_progress(progress, work, datetime.now(timezone.utc))
        result.update(available=True, postings=postings, total_families=len(families), policies=policies)
        if state == "running":
            result["current_pass"] = matched
        elif state == "idle":
            result["last_pass"] = matched
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result["reason"] = "Ranking coverage is not available yet."
    return result
