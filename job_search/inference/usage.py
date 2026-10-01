"""Durable platform inference reservations, without provider billing claims.

Only hashes, identities, counters, and accepted provider job IDs are persisted.
Prompt and result bodies never enter this ledger. Accepted Runpod jobs are resumed
by polling the same ID. A POST with no trustworthy outcome is never repeated.
"""

from __future__ import annotations

from contextlib import contextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterator, Mapping
import uuid

from ..contracts import ContractError, ConflictError, ID_RE, canonical_json
from ..db import connect
from ..scheduler import as_utc, utc_stamp
from .contracts import InferenceTransportError


_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_INFLIGHT = ("reserved", "submitting", "accepted", "unknown")
_SCOPE: ContextVar["InvocationScope | None"] = ContextVar("inference_scope", default=None)


@dataclass(frozen=True)
class UsagePolicy:
    daily_requests: int | None = None
    daily_tokens: int | None = None
    max_inflight: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "UsagePolicy":
        if value is not None and not isinstance(value, Mapping):
            raise ContractError("inference usage limits must be an object")
        raw = dict(value or {})
        if set(raw) - {"daily_requests", "daily_tokens", "max_inflight"}:
            raise ContractError("unknown inference usage limit")
        for name, limit in raw.items():
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10**12):
                raise ContractError(f"inference usage {name} must be a positive integer or null")
        return cls(**raw)

    def mapping(self) -> dict[str, int | None]:
        return {"daily_requests": self.daily_requests, "daily_tokens": self.daily_tokens, "max_inflight": self.max_inflight}


class UsageDeferred(InferenceTransportError):
    """No POST was attempted; the durable queue may wait without burning attempts."""
    def __init__(self, reason: str, retry_at: str):
        super().__init__(reason, retryable=True)
        self.reason_code = reason
        self.retry_at = retry_at
        self.defer_without_attempt = True


class InvocationReconciliationRequired(InferenceTransportError):
    def __init__(self):
        super().__init__("inference_submission_requires_reconciliation", retryable=False)
        self.outcome_unknown = True


class InvocationPending(UsageDeferred):
    def __init__(self):
        scope = current_scope()
        now = scope.clock() if scope else datetime.now(timezone.utc)
        super().__init__("inference_polling_pending", utc_stamp(as_utc(now) + timedelta(minutes=5)))


@dataclass(frozen=True)
class InvocationScope:
    db_path: Path
    work_id: str
    revision: int
    policy: UsagePolicy
    clock: Callable[[], datetime]
    heartbeat: Callable[[], bool] | None = None


@contextmanager
def invocation_scope(
    db_path: Path, work_id: str, revision: int, *, policy: UsagePolicy | None = None,
    clock: Callable[[], datetime] | None = None,
    heartbeat: Callable[[], bool] | None = None,
) -> Iterator[None]:
    token = _SCOPE.set(InvocationScope(Path(db_path), work_id, revision, policy or UsagePolicy(), clock or (lambda: datetime.now(timezone.utc)), heartbeat))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_scope() -> InvocationScope | None:
    scoped = _SCOPE.get()
    if scoped is not None:
        return scoped
    path, work = os.environ.get("JOB_SEARCH_INVOCATION_DB"), os.environ.get("JOB_SEARCH_INVOCATION_WORK")
    if not path and not work:
        return None
    if not path or not work or not Path(path).is_absolute():
        raise ContractError("inference invocation environment is incomplete")
    revision = int(os.environ.get("JOB_SEARCH_INVOCATION_REVISION", "0"))
    policy = UsagePolicy.from_mapping(json.loads(os.environ.get("JOB_SEARCH_INFERENCE_USAGE_LIMITS", "{}")))
    return InvocationScope(Path(path), work, revision, policy, lambda: datetime.now(timezone.utc))


def scope_environment() -> dict[str, str]:
    scope = current_scope()
    if scope is None:
        return {}
    return {
        "JOB_SEARCH_INVOCATION_DB": str(scope.db_path.resolve()),
        "JOB_SEARCH_INVOCATION_WORK": scope.work_id,
        "JOB_SEARCH_INVOCATION_REVISION": str(scope.revision),
        "JOB_SEARCH_INFERENCE_USAGE_LIMITS": canonical_json(scope.policy.mapping()),
    }


def heartbeat_scope() -> None:
    scope = current_scope()
    if scope is not None and scope.heartbeat is not None and not scope.heartbeat():
        raise ContractError("inference work lease was lost")
    if scope is not None:
        with connect(scope.db_path) as con:
            _assert_owned(con, scope)


def _assert_owned(con: sqlite3.Connection, scope: InvocationScope) -> None:
    work = con.execute("SELECT status,recovery_revision FROM work_items WHERE work_id=?", (scope.work_id,)).fetchone()
    if not work or work[0] != "running" or int(work[1]) != scope.revision:
        raise ConflictError("inference scope no longer owns the running work")


def assert_work_completed(con: sqlite3.Connection, work_id: str) -> None:
    states = {row[0] for row in con.execute("SELECT state FROM inference_invocations WHERE work_id=?", (work_id,))}
    if states & {"unknown", "submitting"}:
        raise InvocationReconciliationRequired()
    if states & {"reserved", "accepted"}:
        raise InvocationPending()


def _update_work_outcome(con: sqlite3.Connection, work_id: str) -> None:
    states = {row[0] for row in con.execute("SELECT state FROM inference_invocations WHERE work_id=?", (work_id,))}
    outcome = "unknown" if states & {"submitting", "unknown"} else "in_flight" if states & {"reserved", "accepted"} else "terminal"
    con.execute("UPDATE work_items SET external_outcome=? WHERE work_id=?", (outcome, work_id))


def can_resume_work(con: sqlite3.Connection, work_id: str) -> bool:
    """Positive proof that re-running a handler cannot repeat an uncertain POST."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='inference_invocations'").fetchone():
        return False
    rows = con.execute("SELECT state,provider_job_id FROM inference_invocations WHERE work_id=?", (work_id,)).fetchall()
    if not rows:
        return False
    return all(state == "reserved" or (state in {"accepted", "completed", "failed", "cancelled"} and bool(job)) for state, job in rows)


def work_can_resume(db_path: Path, work_id: str) -> bool:
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as con:
        if can_resume_work(con, work_id):
            return True
        work = con.execute("SELECT failure_kind,external_outcome FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        return bool(work and work[0] == "usage_deferred" and work[1] in {"none", "terminal"})


def reconciled_resume_can_poll(con: sqlite3.Connection, work_id: str) -> bool:
    """Only an audited review plus entirely retrievable results permits this retry."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='inference_invocations'").fetchone():
        return False
    rows=con.execute("SELECT state,retrieval_kind,provider_job_id FROM inference_invocations WHERE work_id=?", (work_id,)).fetchall()
    if not rows or not all(state == "completed" and kind == "runpod_job" and bool(job) for state,kind,job in rows):
        return False
    return con.execute("SELECT 1 FROM inference_recovery_commands r JOIN inference_invocations i USING(invocation_id) WHERE i.work_id=? AND r.resolution='completed' LIMIT 1", (work_id,)).fetchone() is not None


def resume_restart_was_reconciled(con: sqlite3.Connection, run_id: str) -> bool:
    """Permit the existing explicit resume retry after a reviewed failed outcome."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='inference_invocations'").fetchone():
        return False
    work=con.execute("SELECT work_id,status,lease_token FROM work_items WHERE task_kind='resume.optimize' AND json_extract(payload_json,'$.run_id')=? ORDER BY created_at DESC,work_id DESC LIMIT 1", (run_id,)).fetchone()
    if not work or work[1] != "dead" or work[2]:
        return False
    rows=con.execute("SELECT state FROM inference_invocations WHERE work_id=?", (work[0],)).fetchall()
    if not rows or any(row[0] not in {"completed","failed","cancelled"} for row in rows):
        return False
    return con.execute("SELECT 1 FROM inference_recovery_commands r JOIN inference_invocations i USING(invocation_id) WHERE i.work_id=? AND r.resolution IN ('absent','failed') LIMIT 1", (work[0],)).fetchone() is not None


@dataclass
class Invocation:
    scope: InvocationScope
    invocation_id: str
    state: str
    job_id: str

    def _set(self, state: str, *, job_id: str | None = None, observed_tokens: int | None = None, reason: str = "") -> None:
        with connect(self.scope.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            _assert_owned(con, self.scope)
            allowed = {"submitting": {"accepted", "unknown", "completed", "failed"},
                       "accepted": {"completed", "failed", "cancelled", "unknown"},
                       "completed": {"completed", "unknown"}}
            if state not in allowed.get(self.state, set()):
                raise ConflictError("inference state transition is invalid")
            changed = con.execute(
                "UPDATE inference_invocations SET state=?,provider_job_id=COALESCE(?,provider_job_id),"
                "observed_tokens=COALESCE(?,observed_tokens),reconciliation_reason=?,updated_at=? WHERE invocation_id=? AND state=? AND provider_job_id=?",
                (state, job_id, observed_tokens, reason, utc_stamp(as_utc(self.scope.clock())), self.invocation_id, self.state, self.job_id),
            )
            if changed.rowcount != 1:
                raise ConflictError("inference state changed; stale result rejected")
            _update_work_outcome(con, self.scope.work_id)
            con.commit()
        self.state = state
        if job_id is not None:
            self.job_id = job_id

    def submitting(self) -> None:
        with connect(self.scope.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            _assert_owned(con, self.scope)
            if con.execute("SELECT 1 FROM inference_invocations WHERE work_id=? AND state IN ('submitting','unknown') LIMIT 1", (self.scope.work_id,)).fetchone():
                raise InvocationReconciliationRequired()
            row = con.execute("SELECT state,budget_day,reserved_tokens FROM inference_invocations WHERE invocation_id=?", (self.invocation_id,)).fetchone()
            if not row or row[0] != "reserved":
                raise InvocationReconciliationRequired()
            now = as_utc(self.scope.clock())
            # A reservation from yesterday is not permission to spend today's
            # quota. Rebook it atomically before the first POST; accepted work
            # remains charged to its original submission day.
            _check_limits(con, self.scope.policy, now, int(row[2]), exclude=self.invocation_id)
            changed = con.execute(
                "UPDATE inference_invocations SET state='submitting',budget_day=?,updated_at=? WHERE invocation_id=? AND state='reserved'",
                (now.date().isoformat(), utc_stamp(now), self.invocation_id),
            )
            if changed.rowcount != 1:
                raise InvocationReconciliationRequired()
            _update_work_outcome(con, self.scope.work_id)
            con.commit()
        self.state = "submitting"

    def accepted(self, job_id: str) -> None:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            self.unknown()
            raise InvocationReconciliationRequired()
        self._set("accepted", job_id=job_id)

    def unknown(self) -> None:
        self._set("unknown")

    def unavailable(self) -> None:
        """A saved remote ID can no longer provide a trustworthy result."""
        self._set("unknown", reason="inference_result_unavailable")
        raise InvocationReconciliationRequired()

    def terminal(self, status: str, *, observed_tokens: int | None = None) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            raise ContractError("inference terminal outcome is invalid")
        if observed_tokens is not None and (isinstance(observed_tokens, bool) or not isinstance(observed_tokens, int) or observed_tokens < 0):
            observed_tokens = None
        self._set(status, observed_tokens=observed_tokens)


def begin_invocation(
    provider_identity: str, capability: str, request: bytes, *, reserved_tokens: int, retrieval_kind: str = "none",
) -> Invocation | None:
    scope = current_scope()
    if scope is None:
        return None  # Standalone, unmanaged callers retain their existing contract.
    if retrieval_kind not in {"none", "runpod_job"} or not isinstance(request, bytes) or isinstance(reserved_tokens, bool) or not isinstance(reserved_tokens, int) or reserved_tokens < 0:
        raise ContractError("inference reservation is invalid")
    now = as_utc(scope.clock())
    stamp, day = utc_stamp(now), now.date().isoformat()
    fingerprint = hashlib.sha256(provider_identity.encode("utf-8")).hexdigest()
    request_hash = hashlib.sha256(request).hexdigest()
    with connect(scope.db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        _assert_owned(con, scope)
        latest = con.execute(
            "SELECT * FROM inference_invocations WHERE work_id=? AND request_sha256=? AND provider_fingerprint=? "
            "ORDER BY work_revision DESC,created_at DESC LIMIT 1",
            (scope.work_id, request_hash, fingerprint),
        ).fetchone()
        if latest:
            item = dict(latest)
            if item["state"] in {"submitting", "unknown"}:
                raise InvocationReconciliationRequired()
            if item["state"] in {"reserved", "accepted", "completed"}:
                if item["state"] == "completed" and not item["provider_job_id"]:
                    # Synchronous APIs expose no portable replay/retrieve contract.
                    con.execute("UPDATE inference_invocations SET state='unknown',reconciliation_reason='inference_result_not_checkpointed',updated_at=? WHERE invocation_id=?", (stamp,item["invocation_id"]))
                    _update_work_outcome(con, scope.work_id)
                    con.commit()
                    raise InvocationReconciliationRequired()
                return Invocation(scope, item["invocation_id"], item["state"], item["provider_job_id"])
            if scope.revision <= item["work_revision"]:
                raise InferenceTransportError("inference_job_terminal_failure", retryable=item["state"] == "failed")
        # Inputs/model identity can change between attempts. Never create a new
        # request while a previous attempt may still be executing different work.
        uncertain = con.execute(
            "SELECT 1 FROM inference_invocations WHERE work_id=? AND "
            "(state IN ('submitting','unknown') OR (work_revision<? AND state IN ('reserved','accepted'))) LIMIT 1",
            (scope.work_id, scope.revision),
        ).fetchone()
        if uncertain:
            raise InvocationReconciliationRequired()
        con.execute("INSERT INTO inference_usage_policy(singleton,policy_json,updated_at) VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET policy_json=excluded.policy_json,updated_at=excluded.updated_at",
                    (canonical_json(scope.policy.mapping()), stamp))
        try:
            _check_limits(con, scope.policy, now, reserved_tokens)
        except UsageDeferred:
            con.commit()  # Publish configured limits even before a first reservation.
            raise
        identity = "inv_" + uuid.uuid4().hex
        con.execute(
            "INSERT INTO inference_invocations(invocation_id,work_id,request_sha256,provider_fingerprint,capability,retrieval_kind,work_revision,state,reserved_tokens,budget_day,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'reserved',?,?,?,?)",
            (identity, scope.work_id, request_hash, fingerprint, capability, retrieval_kind, scope.revision, reserved_tokens, day, stamp, stamp),
        )
        _update_work_outcome(con, scope.work_id)
        con.commit()
    return Invocation(scope, identity, "reserved", "")


def _check_limits(con: sqlite3.Connection, policy: UsagePolicy, now: datetime, tokens: int, *, exclude: str = "") -> None:
    count, total = con.execute(
        "SELECT COUNT(*),COALESCE(SUM(MAX(reserved_tokens,COALESCE(observed_tokens,0))),0) "
        "FROM inference_invocations WHERE budget_day=? AND invocation_id!=?", (now.date().isoformat(), exclude),
    ).fetchone()
    inflight = con.execute("SELECT COUNT(*) FROM inference_invocations WHERE state IN ('reserved','submitting','accepted','unknown') AND invocation_id!=?", (exclude,)).fetchone()[0]
    tomorrow = utc_stamp(datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    for reason, denied, retry in (
        ("inference_daily_request_limit", policy.daily_requests is not None and count + 1 > policy.daily_requests, tomorrow),
        ("inference_daily_token_limit", policy.daily_tokens is not None and total + tokens > policy.daily_tokens, tomorrow),
        ("inference_inflight_limit", policy.max_inflight is not None and inflight + 1 > policy.max_inflight, utc_stamp(now + timedelta(minutes=5))),
    ):
        if denied:
            raise UsageDeferred(reason, retry)


def usage_report(db_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    current = as_utc(now or datetime.now(timezone.utc))
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+"?mode=ro", uri=True)) as con:
        day = current.date().isoformat()
        count, tokens = con.execute("SELECT COUNT(*),COALESCE(SUM(MAX(reserved_tokens,COALESCE(observed_tokens,0))),0) FROM inference_invocations WHERE budget_day=?", (day,)).fetchone()
        states = dict(con.execute("SELECT state,COUNT(*) FROM inference_invocations GROUP BY state"))
        row = con.execute("SELECT policy_json FROM inference_usage_policy WHERE singleton=1").fetchone()
        deferred = con.execute("SELECT COUNT(*),MIN(due_at) FROM work_items WHERE status='queued' AND failure_kind='usage_deferred'").fetchone()
    policy = json.loads(row[0]) if row else UsagePolicy().mapping()
    return {"schema_version": 1, "budget_day": day, "limits": policy,
            "configured": any(value is not None for value in policy.values()),
            "reserved_requests": count, "reserved_tokens": tokens,
            "inflight": sum(states.get(key, 0) for key in _INFLIGHT),
            "uncertain": states.get("unknown", 0) + states.get("submitting", 0),
            "deferred_work": deferred[0], "next_retry_at": deferred[1],
            "billing": "unpriced", "coverage": "platform_managed_inference"}


class InvocationRecoveryService:
    """Audited user reconciliation of provider outcomes; never contacts a provider."""
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def list_invocations(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ContractError("inference recovery limit must be between 1 and 500")
        with closing(sqlite3.connect(self.db_path.resolve().as_uri()+"?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(
                "SELECT invocation_id,work_id,capability,state,provider_job_id,provider_fingerprint,retrieval_kind,reconciliation_reason,"
                "reserved_tokens,budget_day,updated_at FROM inference_invocations "
                "WHERE state IN ('reserved','submitting','accepted','unknown') ORDER BY created_at,invocation_id LIMIT ?", (limit,),
            )]

    def reconcile(self, invocation_id: str, *, expected_updated_at: str, command_id: str,
                  resolution: str, provider_job_id: str = "", actor_kind: str = "user",
                  now: datetime | None = None) -> dict[str, Any]:
        if actor_kind != "user":
            raise ContractError("inference reconciliation requires a user decision")
        for value in (invocation_id, command_id):
            if not isinstance(value, str) or not ID_RE.fullmatch(value):
                raise ContractError("inference recovery identifier is invalid")
        if resolution not in {"absent", "failed", "completed"}:
            raise ContractError("inference reconciliation resolution is invalid")
        if provider_job_id and (not isinstance(provider_job_id, str) or not _JOB_ID.fullmatch(provider_job_id)):
            raise ContractError("provider job id is invalid")
        as_utc(expected_updated_at)
        if not self.db_path.is_file():
            raise ContractError("application state is not initialized")
        stamp = utc_stamp(as_utc(now or datetime.now(timezone.utc)))
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT * FROM inference_recovery_commands WHERE command_id=?", (command_id,)).fetchone()
            if prior:
                if (prior["invocation_id"], prior["expected_updated_at"], prior["resolution"], prior["provider_job_id"]) != (invocation_id, expected_updated_at, resolution, provider_job_id):
                    raise ConflictError("inference recovery command already describes a different decision")
                return dict(json.loads(prior["result_json"]))
            row = con.execute("SELECT * FROM inference_invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
            if not row:
                raise ContractError("inference invocation does not exist")
            item = dict(row)
            if item["updated_at"] != expected_updated_at or item["state"] not in _INFLIGHT:
                raise ConflictError("inference state changed; review its current outcome")
            work = con.execute("SELECT status,lease_expires_at,task_kind,payload_json FROM work_items WHERE work_id=?", (item["work_id"],)).fetchone()
            if work and work[0] == "running" and (not work[1] or work[1] > stamp):
                raise ConflictError("inference work still owns an active lease")
            if provider_job_id and item["provider_job_id"] and provider_job_id != item["provider_job_id"]:
                raise ConflictError("accepted provider job identity cannot change")
            job_id = provider_job_id or item["provider_job_id"]
            if resolution == "completed" and not job_id:
                raise ContractError("completed reconciliation requires a provider job id")
            if resolution == "completed" and item["retrieval_kind"] != "runpod_job":
                raise ContractError("synchronous inference cannot retrieve completed results; review the outcome and use failed to authorize new work")
            if resolution == "absent" and job_id:
                raise ContractError("an accepted provider job needs a terminal outcome")
            state = "completed" if resolution == "completed" else "failed"
            con.execute("UPDATE inference_invocations SET state=?,provider_job_id=?,updated_at=? WHERE invocation_id=?",
                        (state, job_id, stamp, invocation_id))
            _update_work_outcome(con, item["work_id"])
            con.execute("UPDATE work_items SET status='dead',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,recovery_revision=recovery_revision+1,failure_kind='retryable',failure_retryable=1 WHERE work_id=? AND status IN ('dead','running','queued')",
                        (item["work_id"],))
            result = {"schema_version": 1, "command_id": command_id, "invocation_id": invocation_id,
                      "work_id": item["work_id"], "state": state, "resolution": resolution,
                      "daily_reservation_retained": True, "next_action": "review_work_recovery"}
            remaining = con.execute("SELECT COUNT(*) FROM inference_invocations WHERE work_id=? AND state IN ('reserved','submitting','accepted','unknown')", (item["work_id"],)).fetchone()[0]
            if remaining:
                result["next_action"] = "inspect_inference_recovery"
            elif work and work[2] == "resume.optimize":
                retrievable = con.execute("SELECT COUNT(*) FROM inference_invocations WHERE work_id=? AND (state!='completed' OR retrieval_kind!='runpod_job' OR provider_job_id='')", (item["work_id"],)).fetchone()[0] == 0
                result["next_action"] = "resume_same_work" if retrievable else "retry_resume_after_reconciliation"
                if not retrievable and str(json.loads(work[3]).get("run_id", "")).startswith("import_"):
                    result["next_action"] = "retry_career_import"
            before = {key: item[key] for key in ("state", "provider_job_id", "updated_at", "reserved_tokens", "budget_day")}
            con.execute("INSERT INTO inference_recovery_commands(command_id,invocation_id,expected_updated_at,resolution,provider_job_id,actor_kind,requested_at,before_json,result_json) VALUES (?,?,?,?,?,'user',?,?,?)",
                        (command_id, invocation_id, expected_updated_at, resolution, provider_job_id, stamp, canonical_json(before), canonical_json(result)))
            con.commit()
            return result
