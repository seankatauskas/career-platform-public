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


def install_owner_resolution_schema(con: sqlite3.Connection) -> None:
    """Install additive recovery-owned audit storage inside the core migration lock.

    Predecessors ignore these records while continuing to write their unchanged
    core tables. No core schema version or historical migration checksum changes.
    """
    if not con.in_transaction:
        raise RuntimeError("Owner resolution schema requires the migration transaction")
    con.execute("""CREATE TABLE IF NOT EXISTS work_owner_resolutions (
        work_id TEXT PRIMARY KEY REFERENCES work_items(work_id),
        owner_work_id TEXT NOT NULL, expected_revision INTEGER NOT NULL,
        proof_json TEXT NOT NULL, before_json TEXT NOT NULL, resolved_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS work_owner_resolutions_no_update BEFORE UPDATE ON work_owner_resolutions
        BEGIN SELECT RAISE(ABORT,'owner work resolution is immutable'); END""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS work_owner_resolutions_no_delete BEFORE DELETE ON work_owner_resolutions
        BEGIN SELECT RAISE(ABORT,'owner work resolution is immutable'); END""")


RETRYABLE_TASKS = frozenset({
    "system.worker_tick", "ats.authoritative", "ats.new_only", "ats.refresh_recent",
    "opportunity.location_refresh", "opportunity.preference_refresh",
    "opportunity.salary_drain", "notification.shortlist_evaluate",
    "notification.reminders_due", "outlook.mail.sync",
})


def _retired_tasks(con):
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='application_owner_binding'").fetchone() and con.execute("SELECT 1 FROM application_owner_binding WHERE singleton=1").fetchone():
        from .application_installation import LEGACY_TASKS
        return tuple(sorted(LEGACY_TASKS))
    return ()


def _reject_retired_work(con, work_id):
    retired = _retired_tasks(con)
    if retired:
        row = con.execute('SELECT task_kind FROM work_items WHERE work_id=?', (work_id,)).fetchone()
        if row and row[0] in retired:
            raise ConflictError('retired_application_work; use the Applications owner recovery path')


def unresolved_work_count(con: sqlite3.Connection, *, excluded_tasks=()) -> int:
    """Count current failures without discarding historical evidence."""
    excluded = tuple(excluded_tasks)
    restriction = " AND w.task_kind NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
    return int(con.execute(
        "SELECT COUNT(*) FROM work_items w WHERE status='dead' AND (external_outcome IN ('unknown','in_flight') "
        "OR ((w.schedule_key IS NULL OR EXISTS (SELECT 1 FROM schedule_specs s WHERE s.schedule_key=w.schedule_key AND s.enabled=1)) "
        "AND NOT EXISTS (SELECT 1 FROM work_items n WHERE n.status='succeeded' AND n.task_kind=w.task_kind "
        "AND n.created_at>w.created_at AND w.schedule_key IS NOT NULL AND n.schedule_key=w.schedule_key)))" + restriction, excluded
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
        return False
    return con.execute(
        "SELECT 1 FROM work_items WHERE schedule_key=? AND status='succeeded' "
        "AND created_at>? LIMIT 1", (schedule, item["created_at"]),
    ).fetchone() is not None


def _summary(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    reason = _policy_reason(con, item)
    result = {
        "work_id": item["work_id"], "task_kind": item["task_kind"],
        "status": item["status"], "attempts": item["attempts"],
        "revision": item["recovery_revision"], "failure_kind": item["failure_kind"],
        "external_outcome": item["external_outcome"],
        "retry_allowed": reason == "retry_available", "reason_code": reason,
    }
    if item['task_kind'] == 'mail.understanding':
        from .mail.recovery import RESOLUTION, resolution_evidence
        resolution, _ = resolution_evidence(con, item)
        result.update(resolution_allowed=resolution == RESOLUTION, resolution_reason=resolution)
    return result


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


def _owner_mail_dispatch_lineage(con, item):
    """Prove an inherited dispatch label, not a separately managed workflow.

    All scheduled roots receive a workflow ID, including mail dispatch which
    has no workflow_runs state machine. Follow-up work inherits that label.
    Only its succeeded dispatch ancestry may support cancelling an obsolete
    mail child; real workflows and incomplete/malformed ancestry stay blocked.
    """
    workflow_id = item["workflow_id"]
    if not workflow_id:
        return []
    if (con.execute("SELECT 1 FROM workflow_runs WHERE workflow_id=?", (workflow_id,)).fetchone()
            or con.execute("SELECT 1 FROM workflow_watermarks WHERE workflow_id=? LIMIT 1", (workflow_id,)).fetchone()):
        raise ConflictError("Managed workflow requires its own recovery")
    parent_id = item["parent_work_id"]
    seen = {item["work_id"]}
    lineage = []
    for _ in range(100):
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        parent = con.execute("SELECT * FROM work_items WHERE work_id=?", (parent_id,)).fetchone()
        if (parent is None or parent["workflow_id"] != workflow_id
                or parent["task_kind"] != "applications.mail.dispatch" or parent["status"] != "succeeded"
                or parent["lease_token"] or parent["lease_owner"] or parent["lease_expires_at"]
                or parent["external_outcome"] != "none"):
            break
        lineage.append({key:parent[key] for key in (
            "work_id","workflow_id","parent_work_id","schedule_key","status","recovery_revision")})
        if parent["schedule_key"]:
            if parent["schedule_key"] == "applications.mail.dispatch" and not parent["parent_work_id"]:
                return lineage
            break
        parent_id = parent["parent_work_id"]
    raise ConflictError("Mail dispatch ancestry is not safely completed")


class RecoveryService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def list_work(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ContractError("recovery limit must be between 1 and 500")
        # Inspection cannot initialize, migrate, chmod, or create a database.
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            retired = _retired_tasks(con)
            scope = " AND task_kind NOT IN (" + ",".join("?" for _ in retired) + ")" if retired else ""
            return [_summary(con, row) for row in con.execute(
                "SELECT * FROM work_items WHERE status='dead'" + scope +
                " ORDER BY completed_at DESC,work_id LIMIT ?", (*retired, limit),
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
            _reject_retired_work(con, work_id)
            previous = con.execute(
                "SELECT * FROM work_recovery_commands WHERE command_id=?", (command_id,),
            ).fetchone()
            if previous:
                if (previous["work_id"] != work_id or previous["expected_revision"] != expected_revision
                        or json.loads(previous['result_json']).get('status') != 'queued'):
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

    def owner_mail_candidates(self, *, after="", limit=100):
        """Bounded operational rows; domain applicability belongs to their owners."""
        if not isinstance(after, str) or type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError("Invalid owner recovery page")
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM work_items WHERE status='dead' AND work_id>? "
                "AND task_kind IN ('applications.mail.understand','applications.mail.project') ORDER BY work_id LIMIT ?",
                (after, limit + 1)).fetchall()
            items = [dict(row) for row in rows[:limit]]
            return {"items":items, "next_cursor":items[-1]["work_id"] if len(rows) > limit else None}

    def resolve_owner_mail_work(self, work_id, *, expected_revision, expected_payload, proof, now):
        """Cancel one proven obsolete owner reference, retaining all attempt records.

        Called by trusted owner composition while its owner transaction is locked.
        This method is not exposed as a user/tool command accepting arbitrary proof.
        """
        required = {"owner_work_id", "owner", "kind", "key", "reason", "source_id", "revision", "issue_id", "issue_version", "analysis_id"}
        if (set(proof) != required or proof["reason"] not in {"new_source_revision", "owner_resolved", "new_analysis", "explicit_retry", "retry_finished"}
                or type(expected_revision) is not int or expected_revision < 0):
            raise ContractError("Invalid owner recovery proof")
        route = {("correspondence","understand_message"):"applications.mail.understand",
                 ("understanding","retry_processing"):"applications.mail.understand",
                 ("understanding","project_analysis"):"applications.mail.project"}.get((proof["owner"],proof["kind"]))
        reference = {"owner":proof["owner"], "kind":proof["kind"], "key":proof["key"], "work_id":proof["owner_work_id"]}
        if not route or expected_payload != reference:
            raise ContractError("Operational payload does not identify the proven owner work")
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT * FROM work_owner_resolutions WHERE work_id=?", (work_id,)).fetchone()
            if prior:
                if prior["owner_work_id"] != proof["owner_work_id"]:
                    raise ConflictError("Operational resolution belongs to another owner work item")
                return {"status":"cancelled", "work_id":work_id, "owner_work_id":proof["owner_work_id"]}
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                raise ConflictError("Operational work no longer exists")
            item = dict(row)
            if (item["status"] != "dead" or item["recovery_revision"] != expected_revision
                    or item["task_kind"] != route or json.loads(item["payload_json"]) != reference):
                raise ConflictError("Operational work changed after its owner proof")
            if item["lease_token"] or item["lease_owner"] or item["lease_expires_at"]:
                raise ConflictError("Operational work is still leased")
            if item["schedule_key"]:
                raise ConflictError("Scheduled work requires its own recovery")
            lineage = _owner_mail_dispatch_lineage(con, item)
            if item["external_outcome"] not in {"none","terminal"} or con.execute(
                    "SELECT 1 FROM inference_invocations WHERE work_id=? AND state NOT IN ('completed','failed','cancelled') LIMIT 1", (work_id,)).fetchone():
                raise ConflictError("Provider outcome still requires reconciliation")
            receipt_proof = {**proof, "dispatch_lineage":lineage} if lineage else proof
            con.execute("INSERT INTO work_owner_resolutions VALUES(?,?,?,?,?,?)", (
                work_id,proof["owner_work_id"],expected_revision,canonical_json(receipt_proof),canonical_json(item),now))
            con.execute("UPDATE work_items SET status='cancelled',recovery_revision=recovery_revision+1 WHERE work_id=?", (work_id,))
            return {"status":"cancelled", "work_id":work_id, "owner_work_id":proof["owner_work_id"]}

    def resolve_mail_review(
        self, work_id: str, *, expected_revision: int, command_id: str,
        actor_kind: str = 'user', now: datetime | None = None,
    ) -> dict[str, Any]:
        """Audit cancellation of a failed owner whose email was already processed."""
        from .mail.recovery import RESOLUTION, resolution_evidence
        if actor_kind != 'user':
            raise ContractError('mail work resolution requires a user decision')
        if not isinstance(work_id, str) or not ID_RE.fullmatch(work_id):
            raise ContractError('work id is invalid')
        if not isinstance(command_id, str) or not ID_RE.fullmatch(command_id):
            raise ContractError('recovery command id is invalid')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ContractError('recovery revision is invalid')
        if not self.db_path.is_file():
            raise ContractError('application state is not initialized')
        stamp = utc_stamp(as_utc(now or datetime.now(timezone.utc)))
        with connect(self.db_path) as con:
            con.execute('BEGIN IMMEDIATE')
            _reject_retired_work(con, work_id)
            prior = con.execute('SELECT * FROM work_recovery_commands WHERE command_id=?', (command_id,)).fetchone()
            if prior:
                result = json.loads(prior['result_json'])
                if (prior['work_id'] != work_id or prior['expected_revision'] != expected_revision
                        or result.get('resolution') != RESOLUTION):
                    raise ConflictError('recovery command was already used for a different decision')
                return result
            row = con.execute('SELECT * FROM work_items WHERE work_id=?', (work_id,)).fetchone()
            if row is None:
                raise ContractError('work item does not exist')
            item = dict(row)
            if item['recovery_revision'] != expected_revision:
                raise ConflictError('work changed; refresh its recovery state')
            reason, evidence = resolution_evidence(con, item)
            if reason != RESOLUTION:
                raise ConflictError(reason)
            result = dict(schema_version=1, command_id=command_id, work_id=work_id,
                          status='cancelled', resolution=RESOLUTION, revision=expected_revision + 1,
                          requested_at=stamp, evidence=evidence)
            before = {key: item[key] for key in (
                'status', 'attempts', 'max_attempts', 'failure_kind', 'failure_retryable',
                'external_outcome', 'recovery_revision', 'completed_at', 'last_error')}
            # Cancellation describes superseded work, not a successful model run.
            # Preserve attempts, original completion/error and every provider receipt.
            con.execute("UPDATE work_items SET status='cancelled',recovery_revision=recovery_revision+1 WHERE work_id=?", (work_id,))
            con.execute(
                'INSERT INTO work_recovery_commands '
                '(command_id,work_id,expected_revision,actor_kind,requested_at,before_json,result_json) '
                "VALUES (?,?,?,'user',?,?,?)",
                (command_id, work_id, expected_revision, stamp, canonical_json(before), canonical_json(result)))
            return result
