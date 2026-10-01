"""Narrow operator recovery for durable work, never for remote action approval.

This service only requeues positively classified retryable work. Outlook actions,
resume runs and notification sends retain their domain-specific reconciliation
flows. Error text is deliberately neither returned nor used as a policy decision.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import ConflictError, ContractError, ID_RE, canonical_json
from .db import connect
from .scheduler import as_utc, utc_stamp


RETRYABLE_TASKS = frozenset({
    "system.worker_tick", "ats.authoritative", "ats.new_only", "ats.refresh_recent",
    "opportunity.location_refresh", "opportunity.preference_refresh",
    "opportunity.salary_drain", "notification.shortlist_evaluate",
    "notification.reminders_due", "outlook.mail.sync",
})


def unresolved_work_count(con: sqlite3.Connection) -> int:
    """Count current failures without discarding historical evidence."""
    return int(con.execute(
        "SELECT COUNT(*) FROM work_items w WHERE status='dead' AND (external_outcome IN ('unknown','in_flight') "
        "OR ((w.schedule_key IS NULL OR EXISTS (SELECT 1 FROM schedule_specs s WHERE s.schedule_key=w.schedule_key AND s.enabled=1)) "
        "AND NOT EXISTS (SELECT 1 FROM work_items n WHERE n.status='succeeded' AND n.task_kind=w.task_kind "
        "AND n.created_at>w.created_at AND ((w.schedule_key IS NOT NULL AND n.schedule_key=w.schedule_key) "
        "OR (w.workflow_id!='' AND n.workflow_id!='')))))"
    ).fetchone()[0])


def recovery_reason(item: Mapping[str, Any], *, superseded: bool = False) -> str:
    if item["status"] != "dead":
        return "work_not_failed"
    if item.get("external_outcome") in {"in_flight", "unknown"}:
        return "external_reconciliation_required"
    if item["task_kind"] not in RETRYABLE_TASKS:
        return "use_domain_recovery"
    if item.get("failure_retryable") != 1:
        return "inspect_failure" if item.get("failure_retryable") is None else "failure_not_retryable"
    if superseded:
        return "superseded_by_success"
    return "retry_available"


def _superseded(con: sqlite3.Connection, item: Mapping[str, Any]) -> bool:
    # A newer successful scheduled occurrence replaces an old failed refresh.
    # Independent, unscheduled jobs must not suppress each other accidentally.
    schedule = item.get("schedule_key")
    if not schedule:
        if not item.get("workflow_id"):
            return False
        return con.execute(
            "SELECT 1 FROM work_items newer JOIN workflow_runs n ON n.workflow_id=newer.workflow_id "
            "JOIN workflow_runs old ON old.workflow_id=? WHERE newer.task_kind=? "
            "AND newer.status='succeeded' AND n.scheduled_for>old.scheduled_for LIMIT 1",
            (item["workflow_id"], item["task_kind"]),
        ).fetchone() is not None
    return con.execute(
        "SELECT 1 FROM work_items WHERE schedule_key=? AND status='succeeded' "
        "AND created_at>? LIMIT 1", (schedule, item["created_at"]),
    ).fetchone() is not None


def _summary(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    reason = _policy_reason(con, item)
    return {
        "work_id": item["work_id"], "task_kind": item["task_kind"],
        "status": item["status"], "attempts": item["attempts"],
        "revision": item["recovery_revision"], "failure_kind": item["failure_kind"],
        "external_outcome": item["external_outcome"],
        "retry_allowed": reason == "retry_available", "reason_code": reason,
    }


def _policy_reason(con: sqlite3.Connection, item: Mapping[str, Any]) -> str:
    reason = recovery_reason(item, superseded=_superseded(con, item))
    if reason == "use_domain_recovery" and item["task_kind"] == "resume.optimize" and item.get("failure_retryable") == 1:
        from .inference.usage import reconciled_resume_can_poll
        if not item.get("lease_token") and reconciled_resume_can_poll(con, str(item["work_id"])):
            reason = "retry_available"
    if reason == "retry_available" and item.get("schedule_key"):
        schedule = con.execute("SELECT enabled FROM schedule_specs WHERE schedule_key=?", (item["schedule_key"],)).fetchone()
        if not schedule or not schedule[0]:
            return "schedule_disabled"
    return reason


class RecoveryService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def list_work(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ContractError("recovery limit must be between 1 and 500")
        # Inspection cannot initialize, migrate, chmod, or create a database.
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            return [_summary(con, row) for row in con.execute(
                "SELECT * FROM work_items WHERE status='dead' "
                "ORDER BY completed_at DESC,work_id LIMIT ?", (limit,),
            )]

    def retry(
        self, work_id: str, *, expected_revision: int, command_id: str,
        actor_kind: str = "user", now: datetime | None = None,
    ) -> dict[str, Any]:
        if actor_kind != "user":
            raise ContractError("work recovery requires a user decision")
        if not isinstance(work_id, str) or not ID_RE.fullmatch(work_id):
            raise ContractError("work id is invalid")
        if not isinstance(command_id, str) or not ID_RE.fullmatch(command_id):
            raise ContractError("recovery command id is invalid")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ContractError("recovery revision is invalid")
        if not self.db_path.is_file():
            raise ContractError("application state is not initialized")
        stamp = utc_stamp(as_utc(now or datetime.now(timezone.utc)))
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute(
                "SELECT * FROM work_recovery_commands WHERE command_id=?", (command_id,),
            ).fetchone()
            if previous:
                if previous["work_id"] != work_id or previous["expected_revision"] != expected_revision:
                    raise ConflictError("recovery command was already used for a different decision")
                return dict(json.loads(previous["result_json"]))
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                raise ContractError("work item does not exist")
            item = dict(row)
            if item["recovery_revision"] != expected_revision:
                raise ConflictError("work changed; refresh its recovery state")
            reason = _policy_reason(con, item)
            if reason != "retry_available":
                raise ConflictError(reason)
            result = {
                "schema_version": 1, "command_id": command_id, "work_id": work_id,
                "status": "queued", "revision": expected_revision + 1,
                "requested_at": stamp,
            }
            before = {key: item[key] for key in (
                "status", "attempts", "max_attempts", "failure_kind", "failure_retryable",
                "external_outcome", "recovery_revision", "completed_at",
            )}
            con.execute(
                "UPDATE work_items SET status='queued',attempts=0,due_at=?,completed_at=NULL,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error='',"
                "failure_kind='',failure_retryable=NULL,recovery_revision=recovery_revision+1 "
                "WHERE work_id=?", (stamp, work_id),
            )
            if item["workflow_id"]:
                con.execute(
                    "UPDATE workflow_runs SET status=CASE WHEN stable_at IS NULL THEN 'running' "
                    "ELSE 'stable' END,completed_at=NULL,last_error='' "
                    "WHERE workflow_id=? AND status='failed'", (item["workflow_id"],),
                )
            con.execute(
                "INSERT INTO work_recovery_commands "
                "(command_id,work_id,expected_revision,actor_kind,requested_at,before_json,result_json) "
                "VALUES (?,?,?,'user',?,?,?)",
                (command_id, work_id, expected_revision, stamp, canonical_json(before), canonical_json(result)),
            )
            con.commit()
            return result
