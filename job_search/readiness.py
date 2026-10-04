"""Read-only domain readiness, deliberately separate from process liveness.

Inspecting this module never creates a database, repairs projections, refreshes an
OAuth token, issues inference, or contacts a connector. Dependency observations are
supplied by the composition root. No credentials, paths, raw errors or mail bodies
are part of the public report.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .scheduler import as_utc, next_occurrence, utc_stamp
from .recovery import unresolved_work_count


STATUSES = frozenset({"disabled", "paused", "configured_unverified", "ready", "stale", "blocked"})
_SEVERITY = {"disabled": 0, "paused": 0, "ready": 1, "configured_unverified": 2, "stale": 3, "blocked": 4}
_SCHEDULE_GROUPS = (
    ("automation", ("system.worker_tick",), 15 * 60),
    ("ats.ingestion", ("ats.authoritative", "ats.new_only"), 2 * 60 * 60),
    ("ats.discovery", ("ats.refresh_recent",), 2 * 60 * 60),
    ("outlook", ("outlook.mail.sync",), 15 * 60),
    ("notifications", ("notification.deliver", "notification.reminders_due"), 15 * 60),
)


def _capability(
    identity: str, status: str, *, configured: bool = True, enabled: bool = True,
    attempt: str | None = None, success: str | None = None,
    reason: str = "", action: str = "none",
) -> dict[str, Any]:
    return {
        "id": identity, "status": status, "configured": bool(configured), "enabled": bool(enabled),
        "last_attempt_at": attempt, "last_success_at": success,
        "reason_code": reason, "next_action": action,
    }


def _latest(values: Sequence[str | None]) -> str | None:
    return max((value for value in values if value), default=None)


def _scheduled_capability(
    con: sqlite3.Connection, identity: str, schedules: Sequence[Mapping[str, Any]],
    grace: int, now: datetime, automation_enabled: bool,
) -> dict[str, Any]:
    if not schedules:
        return _capability(identity, "configured_unverified", configured=False, enabled=False,
                           reason="schedules_not_initialized", action="initialize_runtime")
    enabled = [item for item in schedules if item["enabled"]]
    if not enabled:
        return _capability(identity, "disabled", configured=False, enabled=False, reason="not_enabled", action="review_configuration")
    if not automation_enabled:
        return _capability(identity, "paused", reason="automation_paused", action="review_activation")
    parts = []
    for schedule in enabled:
        key = schedule["schedule_key"]
        attempt = con.execute(
            "SELECT MAX(r.started_at) FROM job_runs r JOIN work_items w USING(work_id) WHERE w.schedule_key=?", (key,),
        ).fetchone()[0]
        success = con.execute(
            "SELECT MAX(completed_at) FROM work_items WHERE schedule_key=? AND status='succeeded'", (key,),
        ).fetchone()[0]
        # enabled_since is stable across process restarts and moves only on an
        # actual disabled→enabled transition. updated_at changes on every seed.
        since = schedule.get("enabled_since") or schedule["updated_at"]
        baseline = max(since, success or since)
        failed = con.execute(
            "SELECT 1 FROM work_items WHERE schedule_key=? AND status='dead' "
            "AND COALESCE(completed_at,created_at)>=? LIMIT 1", (key, baseline),
        ).fetchone()
        if failed:
            status, reason, action = "blocked", "latest_work_failed", "inspect_failed_work"
        else:
            due = next_occurrence(json.loads(schedule["schedule_json"]), as_utc(baseline))
            if now > due + timedelta(seconds=grace):
                status, reason, action = "stale", "scheduled_success_overdue", "inspect_workflow"
            elif success and success >= since:
                status, reason, action = "ready", "recent_success", "none"
            else:
                status, reason, action = "configured_unverified", "awaiting_first_scheduled_run", "none"
        parts.append(_capability(identity, status, attempt=attempt, success=success, reason=reason, action=action))
    worst = max(parts, key=lambda item: _SEVERITY[item["status"]]).copy()
    worst["last_attempt_at"] = _latest([item["last_attempt_at"] for item in parts])
    worst["last_success_at"] = _latest([item["last_success_at"] for item in parts])
    return worst


def _workflow_capabilities(
    con: sqlite3.Connection, *, enabled: bool, automation_enabled: bool, now: datetime,
) -> list[dict[str, Any]]:
    latest = con.execute("SELECT * FROM workflow_runs ORDER BY scheduled_for DESC,workflow_id DESC LIMIT 1").fetchone()
    output = []
    for identity, mark in (("ranking", "recommendations_stable"), ("shortlist", "shortlist_evaluated")):
        success = con.execute("SELECT MAX(reached_at) FROM workflow_watermarks WHERE watermark_key=?", (mark,)).fetchone()[0]
        attempt = latest["started_at"] if latest else None
        if not enabled:
            status, reason, action = "disabled", "ingestion_not_enabled", "review_configuration"
        elif not automation_enabled:
            status, reason, action = "paused", "automation_paused", "review_activation"
        elif not latest:
            status, reason, action = "configured_unverified", "awaiting_ingestion", "none"
        elif con.execute("SELECT 1 FROM workflow_watermarks WHERE workflow_id=? AND watermark_key=?", (latest["workflow_id"], mark)).fetchone():
            status, reason, action = "ready", "latest_workflow_succeeded", "none"
        elif latest["status"] == "failed":
            status, reason, action = "blocked", "latest_workflow_failed", "inspect_failed_work"
        elif now > as_utc(latest["started_at"] or latest["scheduled_for"]) + timedelta(hours=2):
            status, reason, action = "stale", "workflow_progress_overdue", "inspect_workflow"
        else:
            status, reason, action = "configured_unverified", "workflow_in_progress", "none"
        output.append(_capability(identity, status, enabled=enabled, configured=enabled,
                                  attempt=attempt, success=success, reason=reason, action=action))
    return output


def _dependency_capabilities(dependencies: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if dependencies is None:
        return []
    output = []
    for key, identity in (("inference", "inference"), ("resume_lab", "resume"), ("notifications", "notification_transport")):
        raw = dependencies.get(key)
        if not isinstance(raw, Mapping):
            continue
        configured = bool(raw.get("configured", raw.get("configuration_ready", False)))
        observed = str(raw.get("status") or "")
        if observed == "disabled":
            status, reason, action = "disabled", "not_configured", "review_configuration"
        elif observed in {"attention", "blocked_setup", "blocked", "failed"}:
            status, reason, action = "blocked", "dependency_not_ready", "review_configuration"
        elif observed == "ready" and identity != "notification_transport":
            status, reason, action = "ready", "local_dependency_ready", "none"
        else:
            # A reachable delivery socket does not prove Telegram delivery, and a
            # valid inference profile does not prove its external endpoint works.
            status, reason, action = "configured_unverified", "external_operation_unverified", "none"
        output.append(_capability(identity, status, configured=configured,
                                  enabled=observed != "disabled", reason=reason, action=action))
        if key == "inference":
            embeddings = raw.get("preference_embeddings", {})
            if embeddings.get("status") in {"migration_required", "blocked_preflight", "no_champion"}:
                output.append(_capability("ranking_model", "blocked", reason=str(embeddings["status"]), action="review_model_readiness"))
    return output


def readiness_report(
    db_path: Path, *, now: datetime | None = None,
    dependencies: Mapping[str, Any] | None = None, automation_enabled: bool = True,
) -> dict[str, Any]:
    current = as_utc(now or datetime.now(timezone.utc))
    stamp = utc_stamp(current)
    capabilities: list[dict[str, Any]] = []
    metrics = {"blocked_capabilities": 0, "stale_capabilities": 0, "unresolved_work": 0,
               "expired_work_leases": 0, "overdue_work": 0, "pending_reconciliation": 0}
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            schedules = [dict(row) for row in con.execute("SELECT * FROM schedule_specs")]
            # Older releases require migration; never mutate state from a health read.
            columns = {row[1] for row in con.execute("PRAGMA table_info(work_items)")}
            if "external_outcome" not in columns:
                return _report(stamp, [_capability("database", "blocked", reason="schema_upgrade_required", action="upgrade_release")], metrics)
            capabilities.append(_capability("database", "ready", reason="readable_schema"))
            for identity, kinds, grace in _SCHEDULE_GROUPS:
                group = [item for item in schedules if item["task_kind"] in kinds]
                capabilities.append(_scheduled_capability(con, identity, group, grace, current, automation_enabled))
            ats_enabled = any(row["enabled"] and row["task_kind"] in {"ats.authoritative", "ats.new_only"} for row in schedules)
            capabilities.extend(_workflow_capabilities(con, enabled=ats_enabled, automation_enabled=automation_enabled, now=current))
            # Activation is an independent user decision, not missing setup.
            from .activation import GROUPS
            switches = {r[0]: bool(r[1]) for r in con.execute("SELECT capability,enabled FROM automation_controls")}
            capability_groups = {"ats.ingestion": "collection", "ats.discovery": "collection",
                                 "outlook": "mail", "notifications": "notifications",
                                 "ranking": "ranking", "shortlist": "notifications"}
            for item in capabilities:
                group = capability_groups.get(item["id"])
                if group in switches and not switches[group]:
                    item.update(status="paused", enabled=False,
                                reason_code="explicitly_paused", next_action="review_activation")
            worker_cap = next(item for item in capabilities if item["id"] == "automation")
            if automation_enabled and set(GROUPS) <= switches.keys() and not any(switches.values()) and not worker_cap["last_attempt_at"]:
                worker_cap.update(status="configured_unverified", reason_code="worker_not_started",
                                  next_action="start_local_services")
            for connector in con.execute("SELECT * FROM connector_health"):
                if connector["status"] not in {"healthy", "disabled"}:
                    for item in capabilities:
                        if item["id"] == "outlook" and item["enabled"] and automation_enabled:
                            item.update(status="blocked", reason_code="reauth_required" if connector["status"] == "reauth_required" else "connector_failed",
                                        next_action="reconnect_outlook" if connector["status"] == "reauth_required" else "inspect_connector",
                                        last_attempt_at=connector["last_attempt_at"], last_success_at=connector["last_success_at"])
            metrics["expired_work_leases"] = con.execute("SELECT COUNT(*) FROM work_items WHERE status='running' AND lease_expires_at<=?", (stamp,)).fetchone()[0]
            metrics["overdue_work"] = con.execute("SELECT COUNT(*) FROM work_items WHERE status='queued' AND due_at<?", (utc_stamp(current - timedelta(minutes=15)),)).fetchone()[0]
            paused_tasks = tuple(task for group, active in switches.items() if not active for task in GROUPS.get(group, ()))
            if paused_tasks:
                placeholders = ','.join('?' for _ in paused_tasks)
                metrics["overdue_work"] = con.execute(
                    f"SELECT COUNT(*) FROM work_items WHERE status='queued' AND due_at<? AND task_kind NOT IN ({placeholders})",
                    (utc_stamp(current - timedelta(minutes=15)), *paused_tasks),
                ).fetchone()[0]
            metrics["pending_reconciliation"] = con.execute("SELECT COUNT(*) FROM work_items WHERE status!='succeeded' AND external_outcome IN ('unknown','in_flight') AND (status='dead' OR lease_expires_at<=?)", (stamp,)).fetchone()[0]
            # A successful newer occurrence resolves operational freshness, while
            # historical failure rows remain available in the recovery inspector.
            metrics["unresolved_work"] = unresolved_work_count(con)
            pending_actions = con.execute("SELECT COUNT(*) FROM action_proposals WHERE status='needs_reconciliation'").fetchone()[0]
            metrics["pending_reconciliation"] += pending_actions
            queue_status, reason, action = "ready", "no_stalled_work", "none"
            if metrics["pending_reconciliation"]:
                queue_status, reason, action = "blocked", "external_reconciliation_required", "inspect_reconciliation"
            elif not automation_enabled:
                queue_status, reason, action = "paused", "automation_paused", "review_activation"
            elif metrics["unresolved_work"]:
                queue_status, reason, action = "blocked", "unresolved_work_failed", "inspect_failed_work"
            elif metrics["expired_work_leases"] or metrics["overdue_work"]:
                queue_status, reason, action = "stale", "work_progress_overdue", "inspect_workflow"
            capabilities.append(_capability("work_queue", queue_status, reason=reason, action=action))
            for table, identity in (("outbox_messages", "application_outbox"), ("notification_outbox", "notification_outbox")):
                rows = con.execute(f"SELECT status,COUNT(*) FROM {table} GROUP BY status").fetchall()
                counts = dict(rows)
                overdue = con.execute(f"SELECT COUNT(*) FROM {table} WHERE status IN ('pending','delivering') AND available_at<?", (utc_stamp(current - timedelta(minutes=15)),)).fetchone()[0]
                state, why, act = ("blocked", "delivery_failed", "inspect_delivery") if counts.get("dead") else (("stale", "delivery_overdue", "inspect_delivery") if overdue else ("ready", "no_failed_delivery", "none"))
                if table == "notification_outbox":
                    uncertain_delivery = con.execute("SELECT COUNT(*) FROM notification_outbox WHERE status='dead' AND last_error='delivery_reconciliation_required'").fetchone()[0]
                    if uncertain_delivery:
                        metrics["pending_reconciliation"] += uncertain_delivery
                        state, why, act = "blocked", "external_reconciliation_required", "inspect_reconciliation"
                if not automation_enabled:
                    if why != "external_reconciliation_required":
                        state, why, act = "paused", "automation_paused", "review_activation"
                capabilities.append(_capability(identity, state, reason=why, action=act))
            failed_mail = con.execute("SELECT COUNT(*) FROM outlook_message_stage WHERE processing_status='failed'").fetchone()[0]
            if failed_mail:
                capabilities.append(_capability("mail_processing", "blocked", reason="staged_mail_failed", action="inspect_mail_processing"))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        capabilities = [_capability("database", "blocked", configured=Path(db_path).is_file(), reason="state_unavailable", action="inspect_database")]
    capabilities.extend(_dependency_capabilities(dependencies))
    return _report(stamp, capabilities, metrics)


def _report(stamp: str, capabilities: list[dict[str, Any]], metrics: dict[str, int]) -> dict[str, Any]:
    metrics["blocked_capabilities"] = sum(item["status"] == "blocked" for item in capabilities)
    metrics["stale_capabilities"] = sum(item["status"] == "stale" for item in capabilities)
    states = {item["status"] for item in capabilities}
    status = next((value for value in ("blocked", "stale", "paused", "configured_unverified", "ready") if value in states), "disabled")
    return {"schema_version": 1, "checked_at": stamp, "status": status, "capabilities": capabilities, "metrics": metrics}
