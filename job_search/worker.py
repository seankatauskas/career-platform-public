"""Bounded, crash-safe worker for SQLite-owned schedules and outbox delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import MutationContext, canonical_json
from .db import connect
from .notifications import DurableNotificationPublisher, NotificationIntent
from .scheduler import (
    as_utc,
    automation_health,
    has_real_scraper_contact,
    materialize_due_schedules,
    utc_stamp,
)


WORKER_LEASE_NAME = "job-search-worker"
WORK_LANES = frozenset({"core", "model"})
DEFAULT_LEASE_SECONDS = 240
DEFAULT_MAX_WORK_PER_TICK = 10
DEFAULT_MAX_OUTBOX_PER_TICK = 20
OUTBOX_MAX_ATTEMPTS = 8
SCRAPER_CONCURRENCY = 8
MAX_RETRY_SECONDS = 24 * 60 * 60


class RetryableTaskError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        retry_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.retry_at = retry_at


class PermanentTaskError(RuntimeError):
    pass


@dataclass(frozen=True)
class FollowUpTask:
    task_kind: str
    payload: Mapping[str, Any]
    priority: int = 0
    max_attempts: int = 5
    delay_seconds: int = 0
    lane: str = "core"
    workflow_id: str = ""
    parent_work_id: str = ""
    dedupe_key: str = ""


@dataclass(frozen=True)
class TaskResult:
    result: Mapping[str, Any]
    follow_ups: Sequence[FollowUpTask] = ()


@dataclass(frozen=True)
class TaskContext:
    work_id: str
    task_kind: str
    attempt: int
    scheduled_for: str
    heartbeat: Callable[[], bool]
    lane: str = "core"
    workflow_id: str = ""
    parent_work_id: str = ""
    record_resources: Callable[[Mapping[str, Any]], None] | None = None


@dataclass(frozen=True)
class OutboxContext:
    outbox_id: str
    topic: str
    source_event_id: str
    attempt: int
    heartbeat: Callable[[], bool]


def _safe_error(exc: BaseException) -> str:
    value = f"{type(exc).__name__}: {exc}"
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [redacted]", value)
    value = re.sub(r"(?i)(api[_-]?key|token|secret)=\S+", r"\1=[redacted]", value)
    return " ".join(value.split())[:1000]


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, value).hex}"


def _retry_time(exc: BaseException, now: datetime, attempt: int) -> tuple[bool, datetime]:
    if getattr(exc, "outcome_unknown", False):
        return False, now
    if isinstance(exc, PermanentTaskError):
        return False, now
    if getattr(exc, "retryable", None) is False:
        return False, now
    delay = min(60 * (2 ** max(0, attempt - 1)), 6 * 60 * 60)
    if isinstance(exc, RetryableTaskError):
        if exc.retry_after_seconds is not None:
            delay = max(delay, int(max(0, exc.retry_after_seconds)))
        if exc.retry_at:
            try:
                requested = as_utc(exc.retry_at)
                delay = max(delay, int((requested - now).total_seconds()))
            except ValueError:
                pass
    decision = getattr(exc, "decision", None)
    if decision is not None:
        if getattr(decision, "outcome_unknown", False):
            return False, now
        if not getattr(decision, "retryable", False):
            return False, now
        next_at = getattr(decision, "next_attempt_at", None)
        if next_at:
            try:
                delay = max(delay, int((as_utc(next_at) - now).total_seconds()))
            except ValueError:
                pass
    return True, now + timedelta(seconds=min(max(0, delay), MAX_RETRY_SECONDS))


def _failure_classification(exc: BaseException) -> tuple[str, int | None, bool]:
    """Persist positive evidence; unknown Python failures are not operator-retryable."""
    decision = getattr(exc, "decision", None)
    ambiguous = bool(getattr(exc, "outcome_unknown", False) or getattr(decision, "outcome_unknown", False))
    if ambiguous:
        return "external_reconciliation", 0, True
    if isinstance(exc, PermanentTaskError) or getattr(exc, "retryable", None) is False:
        return "permanent", 0, False
    if isinstance(exc, RetryableTaskError) or getattr(exc, "retryable", None) is True:
        return "retryable", 1, False
    if decision is not None:
        retryable = bool(getattr(decision, "retryable", False))
        return "retryable" if retryable else "permanent", int(retryable), False
    return "unknown", None, False


def run_command_with_heartbeat(
    command: Sequence[str],
    context: TaskContext,
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: int,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    heartbeat_interval_seconds: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv while retaining the worker lease and cancelling on loss."""

    if timeout_seconds < 1 or heartbeat_interval_seconds <= 0:
        raise ValueError("command timeout and heartbeat interval must be positive")
    process = popen_factory(
        tuple(command),
        cwd=cwd,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    started = time.monotonic()

    def stop() -> None:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    sampler = None
    try:
        pid = getattr(process, "pid", None)
        if context.record_resources is not None and type(pid) is int and pid > 0:
            from .resource_usage import MemorySampler
            sampler = MemorySampler(pid=pid, interval_seconds=2).start()
    except Exception:
        # Observability must not prevent the command or lease heartbeats.
        sampler = None
    try:
        while True:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                stop()
                raise RetryableTaskError("ATS command timed out")
            try:
                stdout, stderr = process.communicate(
                    timeout=min(heartbeat_interval_seconds, remaining)
                )
                return subprocess.CompletedProcess(
                    tuple(command), int(process.returncode), stdout, stderr
                )
            except subprocess.TimeoutExpired:
                if not context.heartbeat():
                    stop()
                    raise PermanentTaskError("worker lease was lost during ATS command")
    finally:
        if sampler is not None:
            try:
                context.record_resources(sampler.finish())
            except Exception:
                pass


def acquire_worker_lease(
    db_path: Path,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    lane: str = "core",
) -> str | None:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    if lane not in WORK_LANES:
        raise ValueError("unknown worker lane")
    lease_name = WORKER_LEASE_NAME if lane == "core" else f"{WORKER_LEASE_NAME}:{lane}"
    now = as_utc(now)
    stamp = utc_stamp(now)
    expires = utc_stamp(now + timedelta(seconds=lease_seconds))
    token = uuid.uuid4().hex
    with connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT * FROM worker_leases WHERE lease_name=?", (lease_name,)
        ).fetchone()
        if current and current["expires_at"] > stamp:
            con.rollback()
            return None
        con.execute(
            "INSERT INTO worker_leases "
            "(lease_name,owner,token,acquired_at,heartbeat_at,expires_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(lease_name) DO UPDATE SET "
            "owner=excluded.owner,token=excluded.token,acquired_at=excluded.acquired_at,"
            "heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at",
            (lease_name, owner, token, stamp, stamp, expires),
        )
        con.commit()
    return token


def release_worker_lease(db_path: Path, token: str, *, lane: str = "core") -> None:
    if lane not in WORK_LANES:
        raise ValueError("unknown worker lane")
    lease_name = WORKER_LEASE_NAME if lane == "core" else f"{WORKER_LEASE_NAME}:{lane}"
    with connect(db_path) as con:
        con.execute(
            "DELETE FROM worker_leases WHERE lease_name=? AND token=?",
            (lease_name, token),
        )


class Worker:
    def __init__(
        self,
        db_path: Path,
        *,
        task_handlers: Mapping[str, Callable[[Mapping[str, Any], TaskContext], Mapping[str, Any] | TaskResult]] | None = None,
        outbox_handlers: Mapping[str, Callable[[Mapping[str, Any], OutboxContext], Mapping[str, Any]]] | None = None,
        owner: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_work_per_tick: int = DEFAULT_MAX_WORK_PER_TICK,
        max_outbox_per_tick: int = DEFAULT_MAX_OUTBOX_PER_TICK,
        lane: str = "core",
        deferred_task_kinds: Sequence[str] = (),
        inference_usage_limits: Mapping[str, Any] | None = None,
    ) -> None:
        if max_work_per_tick < 0 or max_outbox_per_tick < 0:
            raise ValueError("per-tick bounds cannot be negative")
        if lane not in WORK_LANES:
            raise ValueError("unknown worker lane")
        self.db_path = Path(db_path)
        self.task_handlers = dict(task_handlers or {})
        self.outbox_handlers = dict(outbox_handlers or {})
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.max_work_per_tick = max_work_per_tick
        self.max_outbox_per_tick = max_outbox_per_tick if lane == "core" else 0
        self.lane = lane
        self.deferred_task_kinds = tuple(sorted(set(deferred_task_kinds)))
        from .inference.usage import UsagePolicy
        self.inference_usage_policy = UsagePolicy.from_mapping(inference_usage_limits)
        self._worker_token = ""

    @property
    def _lease_name(self) -> str:
        return (
            WORKER_LEASE_NAME
            if self.lane == "core"
            else f"{WORKER_LEASE_NAME}:{self.lane}"
        )

    def _now(self) -> datetime:
        return as_utc(self.now_provider())

    def _heartbeat(self, *, work_id: str | None = None, lease_token: str | None = None) -> bool:
        now = self._now()
        stamp = utc_stamp(now)
        expires = utc_stamp(now + timedelta(seconds=self.lease_seconds))
        with connect(self.db_path) as con:
            worker = con.execute(
                "UPDATE worker_leases SET heartbeat_at=?,expires_at=? "
                "WHERE lease_name=? AND token=?",
                (stamp, expires, self._lease_name, self._worker_token),
            )
            owned = worker.rowcount == 1
            if work_id and lease_token:
                item = con.execute(
                    "UPDATE work_items SET lease_expires_at=? "
                    "WHERE work_id=? AND status='running' AND lease_token=?",
                    (expires, work_id, lease_token),
                )
                owned = owned and item.rowcount == 1
        return owned

    def _recover_expired(self, now: datetime) -> dict[str, int]:
        stamp = utc_stamp(now)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            # A remote invocation records its outcome before any submission. A lost
            # worker lease cannot authorize another POST while that outcome is unknown.
            uncertain = [row[0] for row in con.execute(
                "SELECT work_id FROM work_items WHERE status='running' AND lane=? "
                "AND lease_expires_at<=? AND external_outcome IN ('in_flight','unknown')",
                (self.lane, stamp),
            )]
            from .inference.usage import can_resume_work
            # Accepted IDs (and durable pre-POST reservations) can restart through
            # the invocation ledger. Its adapter will poll, never blindly resubmit.
            resumable = [work_id for work_id in uncertain if can_resume_work(con, work_id)]
            uncertain = [work_id for work_id in uncertain if work_id not in resumable]
            if uncertain:
                marks = ",".join("?" for _ in uncertain)
                con.execute(
                    f"UPDATE work_items SET status='dead',completed_at=?,lease_owner=NULL,"
                    f"lease_token=NULL,lease_expires_at=NULL,last_error='external outcome requires reconciliation',"
                    f"failure_kind='external_reconciliation',failure_retryable=0,"
                    f"recovery_revision=recovery_revision+1 WHERE work_id IN ({marks})",
                    (stamp, *uncertain),
                )
                con.execute(
                    f"UPDATE job_runs SET completed_at=?,outcome='dead',error='external outcome requires reconciliation' "
                    f"WHERE work_id IN ({marks}) AND outcome IS NULL", (stamp, *uncertain),
                )
            exhausted = [row[0] for row in con.execute(
                "SELECT work_id FROM work_items WHERE status='running' AND lane=? "
                "AND lease_expires_at<=? AND attempts>=max_attempts", (self.lane, stamp),
            )]
            if exhausted:
                marks = ",".join("?" for _ in exhausted)
                con.execute(
                    f"UPDATE work_items SET status='dead',completed_at=?,lease_owner=NULL,lease_token=NULL,"
                    f"lease_expires_at=NULL,last_error='lease expired at attempt limit',failure_kind='lease_expired',"
                    f"failure_retryable=1,recovery_revision=recovery_revision+1 WHERE work_id IN ({marks})",
                    (stamp, *exhausted),
                )
                con.execute(
                    f"UPDATE job_runs SET completed_at=?,outcome='dead',error='lease expired at attempt limit' "
                    f"WHERE work_id IN ({marks}) AND outcome IS NULL", (stamp, *exhausted),
                )
            work_ids = [row[0] for row in con.execute(
                "SELECT work_id FROM work_items WHERE status='running' "
                "AND lane=? AND lease_expires_at<=?", (self.lane, stamp),
            )]
            if work_ids:
                marks = ",".join("?" for _ in work_ids)
                con.execute(
                    f"UPDATE work_items SET status='queued',due_at=?,lease_owner=NULL,"
                    f"lease_token=NULL,lease_expires_at=NULL,last_error='lease expired' "
                    f",failure_kind='lease_expired',failure_retryable=1,recovery_revision=recovery_revision+1 "
                    f"WHERE work_id IN ({marks})",
                    (stamp, *work_ids),
                )
                con.execute(
                    f"UPDATE job_runs SET completed_at=?,outcome='failed',error='lease expired' "
                    f"WHERE work_id IN ({marks}) AND outcome IS NULL",
                    (stamp, *work_ids),
                )
            outbox = 0
            if self.lane == "core":
                outbox = con.execute(
                    "UPDATE outbox_messages SET status='pending',available_at=?,"
                    "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                    "last_error='lease expired' "
                    "WHERE status='delivering' AND lease_expires_at<=?",
                    (stamp, stamp),
                ).rowcount
            con.commit()
        return {"work": len(work_ids), "outbox": int(outbox), "reconciliation": len(uncertain), "exhausted": len(exhausted)}

    def _next_due_work(self, con, stamp: str):
        from .activation import disabled_tasks

        deferred = tuple(sorted(set(self.deferred_task_kinds) | set(disabled_tasks(con))))
        deferred_sql = ""
        parameters: list[Any] = [self.lane, stamp]
        if deferred:
            marks = ",".join("?" for _ in deferred)
            deferred_sql = f" AND task_kind NOT IN ({marks})"
            parameters.extend(deferred)
        return con.execute(
            "SELECT * FROM work_items WHERE lane=? AND status='queued' AND due_at<=?"
            + deferred_sql
            + " ORDER BY priority DESC,due_at,created_at,work_id LIMIT 1",
            tuple(parameters),
        ).fetchone()

    def _more_due(self, now: datetime) -> bool:
        """Observe claimable work without reserving it across worker ticks."""
        stamp = utc_stamp(now)
        with connect(self.db_path) as con:
            return bool(
                (self.max_work_per_tick and self._next_due_work(con, stamp))
                or (self.max_outbox_per_tick and self._next_due_outbox(con, stamp))
            )

    def _claim_work(self, now: datetime) -> tuple[dict[str, Any], str] | None:
        stamp = utc_stamp(now)
        expires = utc_stamp(now + timedelta(seconds=self.lease_seconds))
        token = uuid.uuid4().hex
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._next_due_work(con, stamp)
            if not row:
                con.rollback()
                return None
            changed = con.execute(
                "UPDATE work_items SET status='running',attempts=attempts+1,lease_owner=?,"
                "lease_token=?,lease_expires_at=?,started_at=COALESCE(started_at,?) "
                ",recovery_revision=recovery_revision+1 "
                "WHERE work_id=? AND status='queued'",
                (self.owner, token, expires, stamp, row["work_id"]),
            )
            if changed.rowcount != 1:
                con.rollback()
                return None
            payload = json.loads(row["payload_json"])
            scheduled_for = str(payload.get("scheduled_for") or row["due_at"])
            run_id = _stable_id("run", str(row["work_id"]))
            con.execute(
                "INSERT OR IGNORE INTO job_runs "
                "(run_id,work_id,scheduled_for,started_at,result_json,error) "
                "VALUES (?,?,?,?,?,'')",
                (run_id, row["work_id"], scheduled_for, stamp, "{}"),
            )
            con.execute(
                "UPDATE job_runs SET started_at=?,completed_at=NULL,outcome=NULL,error='',result_json='{}' WHERE work_id=?",
                (stamp, row["work_id"]),
            )
            claimed = dict(con.execute(
                "SELECT * FROM work_items WHERE work_id=?", (row["work_id"],)
            ).fetchone())
            con.commit()
        claimed["payload"] = payload
        return claimed, token

    def _complete_work(
        self,
        item: Mapping[str, Any],
        token: str,
        result: TaskResult,
        now: datetime,
    ) -> None:
        stamp = utc_stamp(now)
        canonical_json(result.result)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            owned = con.execute(
                "SELECT 1 FROM work_items WHERE work_id=? AND status='running' AND lease_token=?",
                (item["work_id"], token),
            ).fetchone()
            if not owned:
                con.rollback()
                raise RuntimeError("work lease was lost before completion")
            from .inference.usage import assert_work_completed
            assert_work_completed(con, str(item["work_id"]))
            for index, follow_up in enumerate(result.follow_ups):
                if not follow_up.task_kind or not 1 <= follow_up.max_attempts <= 20:
                    raise PermanentTaskError("follow-up task specification is invalid")
                if follow_up.lane not in WORK_LANES:
                    raise PermanentTaskError("follow-up task lane is invalid")
                workflow_id = follow_up.workflow_id or str(item["workflow_id"])
                parent_work_id = follow_up.parent_work_id or str(item["work_id"])
                if item["workflow_id"] and workflow_id != item["workflow_id"]:
                    raise PermanentTaskError("follow-up crossed a workflow boundary")
                if parent_work_id != item["work_id"]:
                    raise PermanentTaskError("follow-up parent must be the current work item")
                due = now + timedelta(seconds=max(0, follow_up.delay_seconds))
                dedupe = f"follow-up:{item['work_id']}:{index}:{follow_up.task_kind}"
                if follow_up.dedupe_key:
                    if not isinstance(follow_up.dedupe_key, str) or len(follow_up.dedupe_key) > 256 or any(ord(c) < 32 for c in follow_up.dedupe_key):
                        raise PermanentTaskError("follow-up dedupe key is invalid")
                    dedupe = f"semantic-follow-up:{follow_up.task_kind}:{follow_up.dedupe_key}"
                    existing = con.execute("SELECT payload_json,lane FROM work_items WHERE dedupe_key=?", (dedupe,)).fetchone()
                    if existing is not None and (existing['payload_json'] != canonical_json(dict(follow_up.payload)) or existing['lane'] != follow_up.lane):
                        raise PermanentTaskError("follow-up dedupe key conflicts with existing work")
                con.execute(
                    "INSERT OR IGNORE INTO work_items "
                    "(work_id,schedule_key,task_kind,dedupe_key,payload_json,status,priority,"
                    "due_at,attempts,max_attempts,created_at,lane,workflow_id,parent_work_id) "
                    "VALUES (?,NULL,?,?,?,'queued',?,?,?,?,?,?,?,?)",
                    (
                        _stable_id("work", dedupe), follow_up.task_kind, dedupe,
                        canonical_json(dict(follow_up.payload)), follow_up.priority,
                        utc_stamp(due), 0, follow_up.max_attempts, stamp,
                        follow_up.lane, workflow_id, parent_work_id,
                    ),
                )
            con.execute(
                "UPDATE work_items SET status='succeeded',completed_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,last_error='',failure_kind='',failure_retryable=NULL,"
                "external_outcome=CASE WHEN external_outcome='none' THEN 'none' ELSE 'terminal' END,"
                "recovery_revision=recovery_revision+1 WHERE work_id=?",
                (stamp, item["work_id"]),
            )
            con.execute(
                "UPDATE job_runs SET completed_at=?,outcome='succeeded',result_json=?,error='' "
                "WHERE work_id=?",
                (stamp, canonical_json(result.result), item["work_id"]),
            )
            self._record_workflow_success(con, item, result.result, stamp)
            con.commit()

    @staticmethod
    def _record_workflow_success(
        con: Any,
        item: Mapping[str, Any],
        result: Mapping[str, Any],
        stamp: str,
    ) -> None:
        workflow_id = str(item.get("workflow_id") or "")
        if not workflow_id:
            return
        watermark = {
            "ats.authoritative": "ats_ingested",
            "ats.new_only": "ats_ingested",
            "opportunity.location_refresh": "locations_ready",
            "opportunity.preference_refresh": "recommendations_stable",
            "notification.shortlist_evaluate": "shortlist_evaluated",
        }.get(str(item["task_kind"]))
        if (
            item["task_kind"] == "notification.shortlist_evaluate"
            and item.get("payload", {}).get("reason") != "recommendations_stable"
        ):
            watermark = None
        if item["task_kind"] == "opportunity.salary_drain" and result.get("queue_empty"):
            watermark = "salary_drained"
        if not watermark:
            return
        digest = hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()
        con.execute(
            "INSERT OR IGNORE INTO workflow_watermarks "
            "(workflow_id,watermark_key,work_id,reached_at,result_sha256) "
            "VALUES (?,?,?,?,?)",
            (workflow_id, watermark, item["work_id"], stamp, digest),
        )
        if watermark == "recommendations_stable":
            con.execute(
                "UPDATE workflow_runs SET status='stable',stable_at=COALESCE(stable_at,?),"
                "last_error='' WHERE workflow_id=? AND status='running'",
                (stamp, workflow_id),
            )
        elif watermark == "shortlist_evaluated":
            con.execute(
                "UPDATE workflow_runs SET status='completed',completed_at=?,last_error='' "
                "WHERE workflow_id=? AND status IN ('running','stable')",
                (stamp, workflow_id),
            )

    def _fail_work(
        self,
        item: Mapping[str, Any],
        token: str,
        exc: BaseException,
        now: datetime,
        resources: Mapping[str, Any] | None = None,
    ) -> str:
        retryable, due = _retry_time(exc, now, int(item["attempts"]))
        failure_kind, classified_retryable, ambiguous = _failure_classification(exc)
        deferred = bool(getattr(exc, "defer_without_attempt", False))
        if deferred:
            due = as_utc(str(exc.retry_at))
            failure_kind, classified_retryable = ("inference_waiting" if getattr(exc, "reason_code", "") == "inference_polling_pending" else "usage_deferred"), 1
        error = _safe_error(exc)
        stamp = utc_stamp(now)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT external_outcome FROM work_items WHERE work_id=?", (item["work_id"],)).fetchone()
            from .inference.usage import can_resume_work
            safe_resume = bool(current and current[0] == "in_flight" and can_resume_work(con, str(item["work_id"])))
            unresolved = ambiguous or bool(current and current[0] in {"in_flight", "unknown"} and not safe_resume)
            dead = unresolved or not retryable or (not deferred and int(item["attempts"]) >= int(item["max_attempts"]))
            status = "dead" if dead else "queued"
            outcome = "dead" if dead else "failed"
            if unresolved:
                failure_kind, classified_retryable = "external_reconciliation", 0
            changed = con.execute(
                "UPDATE work_items SET status=?,due_at=?,completed_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,last_error=?,failure_kind=?,failure_retryable=?,"
                "external_outcome=CASE WHEN ? THEN 'unknown' ELSE external_outcome END,"
                "attempts=MAX(0,attempts-?),"
                "recovery_revision=recovery_revision+1 "
                "WHERE work_id=? AND status='running' AND lease_token=?",
                (
                    status, utc_stamp(due), stamp if dead else None, error,
                    failure_kind, classified_retryable, int(ambiguous), int(deferred),
                    item["work_id"], token,
                ),
            )
            if changed.rowcount != 1:
                con.rollback()
                raise RuntimeError("work lease was lost before failure recording")
            con.execute(
                "UPDATE job_runs SET completed_at=?,outcome=?,error=?,result_json=? WHERE work_id=?",
                (stamp, outcome, error, canonical_json({"command_memory": resources} if resources else {}), item["work_id"]),
            )
            critical_failure = item["task_kind"] in {
                "ats.authoritative",
                "ats.new_only",
                "opportunity.location_refresh",
                "opportunity.preference_refresh",
                "notification.shortlist_evaluate",
            }
            if (
                item["task_kind"] == "notification.shortlist_evaluate"
                and item.get("payload", {}).get("reason") != "recommendations_stable"
            ):
                critical_failure = False
            if dead and critical_failure:
                con.execute(
                    "UPDATE workflow_runs SET status='failed',completed_at=?,last_error=? "
                    "WHERE workflow_id=? AND status IN ('running','stable')",
                    (stamp, error, item["workflow_id"]),
                )
            con.commit()
        return status

    @staticmethod
    def _next_due_outbox(con, stamp: str):
        return con.execute(
            "SELECT * FROM outbox_messages WHERE status='pending' AND available_at<=? "
            "ORDER BY available_at,created_at,outbox_id LIMIT 1",
            (stamp,),
        ).fetchone()

    def _claim_outbox(self, now: datetime) -> tuple[dict[str, Any], str] | None:
        stamp = utc_stamp(now)
        expires = utc_stamp(now + timedelta(seconds=self.lease_seconds))
        token = uuid.uuid4().hex
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._next_due_outbox(con, stamp)
            if not row:
                con.rollback()
                return None
            changed = con.execute(
                "UPDATE outbox_messages SET status='delivering',attempts=attempts+1,"
                "lease_owner=?,lease_token=?,lease_expires_at=? "
                "WHERE outbox_id=? AND status='pending'",
                (self.owner, token, expires, row["outbox_id"]),
            )
            if changed.rowcount != 1:
                con.rollback()
                return None
            claimed = dict(con.execute(
                "SELECT * FROM outbox_messages WHERE outbox_id=?", (row["outbox_id"],)
            ).fetchone())
            con.commit()
        claimed["payload"] = json.loads(claimed["payload_json"])
        return claimed, token

    def _complete_outbox(self, item: Mapping[str, Any], token: str, now: datetime) -> None:
        stamp = utc_stamp(now)
        with connect(self.db_path) as con:
            changed = con.execute(
                "UPDATE outbox_messages SET status='delivered',delivered_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,last_error='' "
                "WHERE outbox_id=? AND status='delivering' AND lease_token=?",
                (stamp, item["outbox_id"], token),
            )
            if changed.rowcount != 1:
                raise RuntimeError("outbox lease was lost before completion")

    def _fail_outbox(
        self,
        item: Mapping[str, Any],
        token: str,
        exc: BaseException,
        now: datetime,
    ) -> str:
        retryable, due = _retry_time(exc, now, int(item["attempts"]))
        dead = not retryable or int(item["attempts"]) >= OUTBOX_MAX_ATTEMPTS
        status = "dead" if dead else "pending"
        error = _safe_error(exc)
        with connect(self.db_path) as con:
            con.execute(
                "UPDATE outbox_messages SET status=?,available_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,last_error=? "
                "WHERE outbox_id=? AND status='delivering' AND lease_token=?",
                (status, utc_stamp(due), error, item["outbox_id"], token),
            )
        return status

    def tick(self, *, now: datetime | None = None, should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        if should_stop():
            return {"acquired": False, "reason": "worker draining"}
        tick_now = as_utc(now or self._now())
        tick_started = time.monotonic()
        # Include task duration even with an explicit fixture clock.
        def current_time() -> datetime:
            return tick_now + timedelta(seconds=max(0, time.monotonic() - tick_started))
        token = acquire_worker_lease(
            self.db_path,
            owner=self.owner,
            now=tick_now,
            lease_seconds=self.lease_seconds,
            lane=self.lane,
        )
        if token is None:
            return {"acquired": False, "reason": "worker lease is held"}
        self._worker_token = token
        try:
            report: dict[str, Any] = {
                "acquired": True,
                "recovered": self._recover_expired(tick_now),
                "lane": self.lane,
                "materialized": (
                    materialize_due_schedules(self.db_path, tick_now)
                    if self.lane == "core"
                    else {"schedules": 0, "created": 0, "coalesced": 0}
                ),
                "outbox": {"delivered": 0, "retried": 0, "dead": 0},
                "work": {"succeeded": 0, "retried": 0, "dead": 0},
            }
            for _ in range(self.max_outbox_per_tick):
                if should_stop():
                    break
                claimed = self._claim_outbox(current_time())
                if not claimed:
                    break
                item, item_token = claimed
                handler = self.outbox_handlers.get(str(item["topic"]))
                context = OutboxContext(
                    outbox_id=str(item["outbox_id"]),
                    topic=str(item["topic"]),
                    source_event_id=str(item["source_event_id"]),
                    attempt=int(item["attempts"]),
                    heartbeat=lambda: self._heartbeat(),
                )
                try:
                    if handler is None:
                        raise PermanentTaskError(f"no outbox handler for {item['topic']}")
                    result = handler(item["payload"], context)
                    canonical_json(result)
                    self._complete_outbox(item, item_token, current_time())
                    report["outbox"]["delivered"] += 1
                except Exception as exc:
                    status = self._fail_outbox(item, item_token, exc, current_time())
                    report["outbox"]["dead" if status == "dead" else "retried"] += 1

            for _ in range(self.max_work_per_tick):
                if should_stop():
                    break
                claimed = self._claim_work(current_time())
                if not claimed:
                    break
                item, item_token = claimed
                handler = self.task_handlers.get(str(item["task_kind"]))
                resources: dict[str, Any] = {"commands": [], "omitted_commands": 0}

                def record_resources(summary: Mapping[str, Any]) -> None:
                    encoded = canonical_json(summary)
                    if len(encoded) > 16384:
                        return
                    if len(resources["commands"]) < 8:
                        # The sampler owns a fixed counter allowlist and stores no
                        # argv, environment, PID, filenames, or command output.
                        resources["commands"].append(json.loads(encoded))
                    else:
                        resources["omitted_commands"] += 1

                context = TaskContext(
                    work_id=str(item["work_id"]),
                    task_kind=str(item["task_kind"]),
                    attempt=int(item["attempts"]),
                    scheduled_for=str(item["payload"].get("scheduled_for") or item["due_at"]),
                    heartbeat=lambda work_id=str(item["work_id"]), lease=item_token: self._heartbeat(
                        work_id=work_id, lease_token=lease,
                    ),
                    lane=str(item["lane"]),
                    workflow_id=str(item["workflow_id"]),
                    parent_work_id=str(item["parent_work_id"] or ""),
                    record_resources=record_resources,
                )
                try:
                    if item["task_kind"] == "system.worker_tick" and handler is None:
                        raw_result: Mapping[str, Any] | TaskResult = {"tick_at": utc_stamp(tick_now)}
                    elif handler is None:
                        raise PermanentTaskError(f"no task handler for {item['task_kind']}")
                    else:
                        from .inference.usage import invocation_scope
                        with invocation_scope(self.db_path, context.work_id, int(item["recovery_revision"]),
                                              policy=self.inference_usage_policy, clock=self._now, heartbeat=context.heartbeat):
                            raw_result = handler(item["payload"], context)
                    result = raw_result if isinstance(raw_result, TaskResult) else TaskResult(raw_result)
                    if not isinstance(result.result, Mapping):
                        raise PermanentTaskError("task handler result must be an object")
                    if resources["commands"]:
                        result = TaskResult({**result.result, "command_memory": resources}, result.follow_ups)
                    self._complete_work(item, item_token, result, current_time())
                    report["work"]["succeeded"] += 1
                except Exception as exc:
                    status = self._fail_work(item, item_token, exc, current_time(),
                                             resources if resources["commands"] else None)
                    report["work"]["dead" if status == "dead" else "retried"] += 1
            report["health"] = automation_health(self.db_path, current_time())
            report["more_due"] = self._more_due(current_time())
            return report
        finally:
            release_worker_lease(self.db_path, token, lane=self.lane)
            self._worker_token = ""


class ATSCommandHandler:
    """Fixed scraper command; task payload cannot raise concurrency or inject flags."""

    _ARGS = {
        "authoritative": ("--all", "--no-export"),
        "new_only": ("--all", "--new-only", "--no-export"),
        "refresh_recent": ("--refresh-recent", "--discover-only"),
    }

    def __init__(
        self,
        mode: str,
        *,
        project_root: Path,
        jobs_db: Path,
        board_registry_path: Path | None = None,
        environment_provider: Callable[[], Mapping[str, str]] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        follow_up_task_kinds: Sequence[str] = (),
        follow_up_factory: Callable[[TaskContext], Sequence[FollowUpTask]] | None = None,
        timeout_seconds: int = 60 * 60,
    ) -> None:
        if mode not in self._ARGS:
            raise ValueError("unknown ATS command mode")
        self.mode = mode
        self.project_root = Path(project_root)
        self.jobs_db = Path(jobs_db)
        self.board_registry_path = board_registry_path
        self.environment_provider = environment_provider or (lambda: dict(os.environ))
        self.runner = runner
        self.follow_up_task_kinds = tuple(follow_up_task_kinds)
        self.follow_up_factory = follow_up_factory
        self.timeout_seconds = timeout_seconds

    def __call__(self, payload: Mapping[str, Any], context: TaskContext) -> TaskResult:
        del payload
        environment = dict(self.environment_provider())
        if not has_real_scraper_contact(environment):
            raise RetryableTaskError(
                "ATS task disabled until JOB_SCRAPER_CONTACT is a real address",
                retry_after_seconds=4 * 60 * 60,
            )
        command = (
            sys.executable,
            "-m", "job_search.collection.boards",
            *self._ARGS[self.mode],
            "--concurrency", str(SCRAPER_CONCURRENCY),
            "--db", str(self.jobs_db),
            "--out", str(self.jobs_db.parent / "job-boards"),
        )
        if self.board_registry_path is not None and self.mode != "refresh_recent":
            command += ("--boards-from", str(self.board_registry_path))
        if self.runner is None:
            completed = run_command_with_heartbeat(
                command,
                context,
                cwd=str(self.project_root),
                env=environment,
                timeout_seconds=self.timeout_seconds,
            )
        else:
            completed = self.runner(
                command,
                cwd=str(self.project_root),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        if completed.returncode != 0:
            # Exceptions are normally at the END, after progress. Redact first
            # so truncation cannot cut away a credential's identifying prefix.
            diagnostic = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [redacted]", str(completed.stderr or "scraper failed"))
            diagnostic = re.sub(r"(?i)(api[_-]?key|token|secret)=\S+", r"\1=[redacted]", diagnostic)
            message = f"ATS {self.mode} exit {completed.returncode}: " + " ".join(diagnostic.split())[-800:]
            if completed.returncode == 2:
                raise PermanentTaskError(message)
            raise RetryableTaskError(message)
        if self.follow_up_factory is not None:
            follow_ups = tuple(self.follow_up_factory(context))
        else:
            follow_ups = tuple(
                FollowUpTask(
                    task_kind,
                    {
                        "source_work_id": context.work_id,
                        "source_task": f"ats.{self.mode}",
                        "scraper_completed_at": utc_stamp(datetime.now(timezone.utc)),
                    },
                    workflow_id=context.workflow_id,
                    parent_work_id=context.work_id,
                )
                for task_kind in self.follow_up_task_kinds
            )
        return TaskResult(
            {
                "mode": self.mode,
                "returncode": int(completed.returncode),
                "stdout_tail": str(completed.stdout or "")[-4_096:],
                "stderr_tail": str(completed.stderr or "")[-4_096:],
                "concurrency": SCRAPER_CONCURRENCY,
            },
            follow_ups,
        )


def preference_outbox_handler(gateway: Any) -> Callable[[Mapping[str, Any], OutboxContext], Mapping[str, Any]]:
    def deliver(payload: Mapping[str, Any], context: OutboxContext) -> Mapping[str, Any]:
        return gateway.deliver_applied_feedback(
            payload,
            source_event_id=context.source_event_id,
        )

    return deliver


class OutlookMailTaskHandler:
    """Run one bounded Outlook delta pass and process its staged messages.

    Production enables ``all_history`` so the same scheduled task discovers the
    mailbox folder tree and processes query-version-2 rows from every eligible
    folder.  The legacy single-folder mode remains available for callers that have
    not opted into encrypted full-mail retention.
    """

    def __init__(
        self,
        coordinator: Any,
        *,
        account_id: str,
        folder_ref: str = "inbox",
        folder_refs: Sequence[str] | None = None,
        all_history: bool = False,
        max_folders: int = 4096,
        max_pages_per_folder: int = 100,
        max_messages: int = 100,
        activation_start: Callable[[], str | None] | None = None,
    ) -> None:
        if not 1 <= max_folders <= 10_000:
            raise ValueError("mail folder bound must be between 1 and 10000")
        if not 1 <= max_pages_per_folder <= 10_000:
            raise ValueError("mail page bound must be between 1 and 10000")
        if not 1 <= max_messages <= 500:
            raise ValueError("mail processing bound must be between 1 and 500")
        self.coordinator = coordinator
        self.account_id = account_id
        values = tuple(folder_refs) if folder_refs is not None else (folder_ref,)
        if not values or any(not str(value).strip() for value in values):
            raise ValueError("at least one nonempty Outlook folder is required")
        self.folder_refs = tuple(dict.fromkeys(str(value).strip() for value in values))
        self.folder_ref = self.folder_refs[0]
        self.all_history = bool(all_history)
        self.max_folders = max_folders
        self.max_pages_per_folder = max_pages_per_folder
        self.max_messages = max_messages
        self.activation_start = activation_start

    def __call__(self, payload: Mapping[str, Any], context: TaskContext) -> Mapping[str, Any]:
        del payload
        if self.activation_start is not None:
            start = self.activation_start()
            if start is None:
                raise ValueError("activate mail before synchronizing new messages")
            from .contracts import parse_utc
            parse_utc(start)
            self.coordinator.received_since = start
        if self.all_history:
            from .sync import ALL_HISTORY_QUERY_VERSION

            synced = self.coordinator.sync_all_history(
                self.account_id,
                max_folders=self.max_folders,
                max_pages_per_folder=self.max_pages_per_folder,
                heartbeat=context.heartbeat,
            )
            query_version = ALL_HISTORY_QUERY_VERSION
        else:
            folder_syncs = {}
            for folder_ref in self.folder_refs:
                folder_sync = self.coordinator.sync_folder(
                    self.account_id,
                    folder_ref,
                    max_pages=self.max_pages_per_folder,
                    heartbeat=context.heartbeat,
                )
                folder_syncs[folder_ref] = asdict(folder_sync)
        processing_options = {
            "limit": self.max_messages,
            "transient_attempt": context.attempt,
            "transient_limit": 5,
            "heartbeat": context.heartbeat,
        }
        if self.all_history:
            processing_options["query_version"] = query_version
        processed = self.coordinator.process_pending(**processing_options)
        result = {"processing": asdict(processed)}
        if self.all_history:
            result["sync"] = asdict(synced)
        else:
            result["sync"] = folder_syncs[self.folder_ref]
            result["folder_syncs"] = folder_syncs
        return result


class DueLocalReminderTaskHandler:
    """Publish accepted temporal reminders, then mark only durable rows complete.

    Publishing and completion are intentionally two replay-safe operations.  A crash
    after the outbox insert leaves the reminder pending; the next pass observes the
    notification dedupe key and can finish the reminder without sending a duplicate.
    """

    _TITLES = {
        "interview_24h": "Interview in 24 hours",
        "interview_1h": "Interview in 1 hour",
        "deadline": "Application deadline due",
    }

    def __init__(
        self,
        service: Any,
        publisher: DurableNotificationPublisher,
        *,
        now_provider: Callable[[], datetime] | None = None,
        limit: int = 50,
    ) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("local reminder bound must be between 1 and 100")
        self.service = service
        self.publisher = publisher
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.limit = limit

    @staticmethod
    def _one_line(value: Any, maximum: int) -> str:
        return " ".join(str(value or "").split())[:maximum]

    def _intent(self, reminder: Mapping[str, Any]) -> NotificationIntent:
        application_id = str(reminder["application_id"])
        application = self.service.get_application_timeline(application_id)["application"]
        employer = self._one_line(application.get("employer_snapshot"), 200)
        role = self._one_line(application.get("title_snapshot"), 200)
        kind = str(reminder["kind"])
        title = self._TITLES.get(kind, "Job-search reminder")
        body = " — ".join(item for item in (employer, role) if item)
        if not body:
            body = "A job-search reminder is due."
        if kind == "deadline":
            body += f"\nDeadline: {reminder['due_at']}"
        return NotificationIntent(
            topic="reminder.due",
            source_id=str(reminder["reminder_id"]),
            title=title,
            body=body,
            application_id=application_id,
            context={
                "application_id": application_id,
                "reminder_id": str(reminder["reminder_id"]),
            },
        )

    def __call__(self, payload: Mapping[str, Any], context: TaskContext) -> Mapping[str, Any]:
        del payload
        current = self.now_provider()
        if current.tzinfo is None:
            raise ValueError("local reminder clock must be timezone-aware")
        due = self.service.list_due_local_reminders(
            utc_stamp(current), limit=self.limit
        )
        published = completed = suppressed = 0
        for reminder in due:
            result = self.publisher.publish(self._intent(reminder))
            if result.get("suppressed"):
                suppressed += 1
            else:
                published += int(bool(result.get("created")))
                self.service.complete_local_reminder(
                    str(reminder["reminder_id"]),
                    "completed",
                    MutationContext(
                        "reminder-published:" + str(reminder["reminder_id"]),
                        "system",
                        "notification_policy",
                        str(reminder["reminder_id"]),
                    ),
                )
                completed += 1
            if not context.heartbeat():
                raise RuntimeError("local reminder worker lease was lost")
        return {
            "due": len(due),
            "published": published,
            "completed": completed,
            "suppressed": suppressed,
        }


class ApprovedActionTaskHandler:
    """Recover stale claims and execute a bounded set of still-approved actions."""

    def __init__(
        self,
        service: Any,
        executor: Any,
        *,
        max_actions: int = 10,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= max_actions <= 50:
            raise ValueError("action bound must be between 1 and 50")
        self.service = service
        self.executor = executor
        self.max_actions = max_actions
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def __call__(self, payload: Mapping[str, Any], context: TaskContext) -> Mapping[str, Any]:
        del payload
        recovered = self.service.recover_stale_actions(
            utc_stamp(self.now_provider()), stale_after_seconds=300
        )
        results = []
        for action in self.service.list_actions(("approved",))[: self.max_actions]:
            result = self.executor.execute(str(action["action_id"]))
            results.append(asdict(result))
            context.heartbeat()
            if result.outcome == "retryable_failure":
                current = self.service.get_action(str(action["action_id"]))
                if current["status"] == "approved":
                    raise RetryableTaskError(
                        "approved Outlook action needs retry",
                        retry_at=result.retry_at or None,
                    )
        return {"recovery": recovered, "executions": results}


def _unavailable_outlook_handler(message: str) -> Callable[[Mapping[str, Any], TaskContext], Mapping[str, Any]]:
    def unavailable(payload: Mapping[str, Any], context: TaskContext) -> Mapping[str, Any]:
        del payload, context
        raise PermanentTaskError(message)

    return unavailable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one bounded job-search worker tick")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--lane", choices=tuple(sorted(WORK_LANES)), default="core")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--jobs-db", type=Path)
    parser.add_argument("--preference-db", type=Path)
    parser.add_argument("--proxy-db", type=Path)
    parser.add_argument(
        "--mail-classifier-config",
        type=Path,
        help="owner-only JSON config for an isolated local mail classifier",
    )
    parser.add_argument(
        "--inference-config",
        type=Path,
        help="owner-only portable inference provider configuration",
    )
    parser.add_argument(
        "--tool-service-socket",
        type=Path,
        help="private cloud document-tool Unix socket",
    )
    parser.add_argument(
        "--hermes-notification-socket",
        type=Path,
        help="private cloud Hermes delivery Unix socket",
    )
    parser.add_argument("--max-work", type=int, default=DEFAULT_MAX_WORK_PER_TICK)
    parser.add_argument("--max-outbox", type=int, default=DEFAULT_MAX_OUTBOX_PER_TICK)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from .runtime import (
        DEFAULT_CONFIG_PATH,
        build_runtime,
        load_runtime_config,
        override_runtime_config,
    )

    config_path = args.config.expanduser() if args.config else DEFAULT_CONFIG_PATH
    default_root = args.project_root.expanduser() if args.project_root else Path.cwd()
    try:
        config = load_runtime_config(
            config_path,
            required=args.config is not None,
            default_root=default_root,
        )
        config = override_runtime_config(
            config,
            project_root=args.project_root,
            application_db=args.db,
            jobs_db=args.jobs_db,
            preference_db=args.preference_db,
            proxy_db=args.proxy_db,
            mail_classifier_config=args.mail_classifier_config,
            inference_config=args.inference_config,
            tool_service_socket=args.tool_service_socket,
            hermes_notification_socket=args.hermes_notification_socket,
        )
        runtime = build_runtime(
            config,
            lane=args.lane,
            max_work_per_tick=args.max_work,
            max_outbox_per_tick=args.max_outbox,
        )
        result = runtime.tick()
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
