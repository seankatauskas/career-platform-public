"""User-requested collection through the normal, serialized opportunity pipeline."""
from __future__ import annotations

import os
from contextlib import closing
import sqlite3

from .activation import disabled_tasks
from .contracts import ContractError, MutationContext, canonical_json
from .maintenance import draining
from .scheduler import _stable_id, has_real_scraper_contact


def scan_availability(config, con) -> dict:
    reason = None
    if draining():
        reason = "Updates are in progress. Try again when they finish."
    elif "ats.new_only" in disabled_tasks(con):
        reason = "Job collection is paused. Enable it in background activity first."
    elif not has_real_scraper_contact(config.environment(os.environ)):
        reason = "Configure the collector contact before scanning."
    elif not config.board_registry_path or not config.board_registry_path.is_file():
        reason = "Configure the company board list before scanning."
    elif not con.execute("SELECT 1 FROM schedule_specs WHERE task_kind='ats.new_only' AND enabled=1").fetchone():
        reason = "The collection worker is not configured yet."
    return {"available": reason is None, "reason": reason}


def request_scan(config, store, command_id: str) -> dict:
    """Replay requests durably and join an outstanding collection when possible."""
    def operation(con, stamp):
        availability = scan_availability(config, con)
        if not availability["available"]:
            raise ContractError(availability["reason"])
        active = con.execute(
            "SELECT work_id,workflow_id,status FROM work_items "
            "WHERE task_kind IN ('ats.new_only','ats.authoritative') "
            "AND status IN ('queued','running') ORDER BY created_at LIMIT 1"
        ).fetchone()
        if active:
            return {**dict(active), "coalesced": True}
        dedupe = "manual-scan:" + command_id
        work_id, workflow_id = _stable_id("work", dedupe), _stable_id("workflow", dedupe)
        con.execute(
            "INSERT INTO work_items (work_id,task_kind,dedupe_key,payload_json,status,"
            "priority,due_at,attempts,max_attempts,created_at,lane,workflow_id) "
            "VALUES (?,'ats.new_only',?,?,'queued',40,?,0,5,?,'core',?)",
            (work_id, dedupe, canonical_json({"scheduled_for": stamp, "source": "dashboard"}), stamp, stamp, workflow_id),
        )
        con.execute(
            "INSERT INTO workflow_runs (workflow_id,workflow_kind,root_work_id,"
            "trigger_task_kind,scheduled_for,status,started_at,last_error) "
            "VALUES (?,'opportunity_refresh',?,'ats.new_only',?,'running',?,'')",
            (workflow_id, work_id, stamp, stamp),
        )
        return {"work_id": work_id, "workflow_id": workflow_id, "status": "queued", "coalesced": False}

    return store._idempotent("request_job_scan", MutationContext(command_id, "user", "dashboard"),
                             {"task_kind": "ats.new_only"}, operation)


def scan_status(config) -> dict:
    with closing(sqlite3.connect(config.application_db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        result = scan_availability(config, con)
        active = con.execute(
            "SELECT work_id,status FROM work_items WHERE task_kind IN ('ats.new_only','ats.authoritative') "
            "AND status IN ('queued','running') ORDER BY created_at LIMIT 1"
        ).fetchone()
        result["active"] = dict(active) if active else None
        result["next_scan_at"] = con.execute(
            "SELECT MIN(next_due_at) FROM schedule_specs WHERE enabled=1 "
            "AND task_kind IN ('ats.new_only','ats.authoritative')"
        ).fetchone()[0]
        result["last_scan_at"] = con.execute(
            "SELECT MAX(completed_at) FROM work_items WHERE status='succeeded' "
            "AND task_kind IN ('ats.new_only','ats.authoritative')"
        ).fetchone()[0]
        return result
