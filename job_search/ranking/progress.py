"""Passive ranking coverage and queue state; never starts inference or prepares data."""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from .refresh import POLICIES, inspect_policies, policy_runs
from ..activation import disabled_tasks


def _read(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5))


def ranking_progress(config) -> dict:
    with _read(config.application_db) as con:
        con.row_factory = sqlite3.Row
        paused = "opportunity.preference_refresh" in disabled_tasks(con)
        work = con.execute(
            "SELECT status,failure_kind,due_at FROM work_items "
            "WHERE task_kind='opportunity.preference_refresh' "
            "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,created_at DESC LIMIT 1"
        ).fetchone()
    state = "paused" if paused else "idle"
    retry_at = None
    if work and not paused:
        state = work["status"] if work["status"] in {"queued", "running", "dead"} else "idle"
        if state == "queued" and work["failure_kind"] == "usage_deferred":
            state, retry_at = "waiting_allowance", work["due_at"]
        elif state == "queued" and work["failure_kind"] == "inference_waiting":
            state, retry_at = "waiting_provider", work["due_at"]
    result = {"state": state, "retry_at": retry_at, "available": False}
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
            progress = json.loads(row[0]) if row else {}
            if not isinstance(progress, dict):
                raise ValueError("invalid ranking progress")
        freshness = inspect_policies(config.preference_db, config.proxy_db, config.jobs_db)
        for policy, counts in policies.items():
            counts["freshness"] = freshness[policy]["status"]
        result.update(available=True, postings=postings, total_families=len(families), policies=policies,
                      current_pass={k: progress.get(k) for k in ("processed_families", "total_families", "updated_at")})
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result["reason"] = "Ranking coverage is not available yet."
    return result
