"""Transactional state machine; contains no provider I/O or application SQL."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone

from job_search.commands import DomainError


SCHEMA = """
CREATE TABLE IF NOT EXISTS action_revisions (
 action_id TEXT PRIMARY KEY, envelope_json TEXT NOT NULL, digest TEXT NOT NULL,
 application_id TEXT NOT NULL, account_id TEXT NOT NULL, kind TEXT NOT NULL,
 operation_id TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL,
 authorization TEXT NOT NULL DEFAULT 'pending', authorized_by TEXT,
 approved_until TEXT, execution TEXT NOT NULL DEFAULT 'not_started',
 fence INTEGER NOT NULL DEFAULT 0, worker_id TEXT, lease_until TEXT,
 attempt_count INTEGER NOT NULL DEFAULT 0, write_intent INTEGER NOT NULL DEFAULT 0,
 retry_at TEXT,
 checkpoint_json TEXT NOT NULL DEFAULT '{}', cancellation_requested INTEGER NOT NULL DEFAULT 0,
 error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS action_application ON action_revisions(application_id,action_id);
CREATE TABLE IF NOT EXISTS action_attempts (
 action_id TEXT NOT NULL REFERENCES action_revisions(action_id), fence INTEGER NOT NULL,
 worker_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
 outcome TEXT, PRIMARY KEY(action_id,fence)
);
CREATE TABLE IF NOT EXISTS action_checkpoints (
 checkpoint_id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES action_revisions(action_id),
 fence INTEGER NOT NULL, phase TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_results (
 result_id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES action_revisions(action_id),
 application_id TEXT NOT NULL, envelope_json TEXT NOT NULL, observation_json TEXT NOT NULL,
 delivery TEXT NOT NULL, conflict_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS action_imports (
 source_id TEXT PRIMARY KEY, source_json TEXT NOT NULL, application_id TEXT,
 unresolved INTEGER NOT NULL DEFAULT 0, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_owned_events (
 account_id TEXT NOT NULL, remote_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
 etag TEXT NOT NULL, application_id TEXT NOT NULL, active INTEGER NOT NULL,
 PRIMARY KEY(account_id,remote_id)
);
CREATE TABLE IF NOT EXISTS action_activation_fences (
 action_id TEXT NOT NULL REFERENCES action_revisions(action_id), fence INTEGER NOT NULL,
 activation_revision INTEGER NOT NULL, PRIMARY KEY(action_id,fence)
);
"""

# The command schema registry applies this only to the exact prior owner checksum.
SCHEMA_MIGRATIONS = {("external_actions", "ccf98e063defdc633d289454dcab958837c33c92a05dde0398580bd6ef1767b9", hashlib.sha256(SCHEMA.encode()).hexdigest()): ("""
CREATE TABLE action_activation_fences (
 action_id TEXT NOT NULL REFERENCES action_revisions(action_id), fence INTEGER NOT NULL,
 activation_revision INTEGER NOT NULL, PRIMARY KEY(action_id,fence)
);
""",)}

KINDS = frozenset({"create_reply_draft", "send_reply", "create_calendar_entry",
                   "update_calendar_entry", "cancel_calendar_entry"})
UNRESOLVED = frozenset({"executing", "awaiting_confirmation", "uncertain"})


def encode(value):
    """Version 1 preserves exact Unicode and whitespace, including decomposed text."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def instant(text):
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise DomainError("invalid_input", "Expected a timestamp with timezone") from None


def stamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_worker(tx):
    if tx.context.principal.kind != "worker":
        raise DomainError("not_authorized", "External execution requires trusted worker authority")


def require_human(tx):
    if tx.context.principal.kind != "human" or tx.context.origin != "direct":
        raise DomainError("not_authorized", "Exact external approval requires a direct human decision")


def bounded_text(value, name, limit=2000):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise DomainError("invalid_input", "Invalid " + name)


def validate_envelope(envelope):
    if is_dataclass(envelope):
        envelope = asdict(envelope)
    envelope = json.loads(encode(dict(envelope)))  # Snapshot nested caller-owned values.
    allowed = {"kind", "account_id", "application_id", "pursuit_no", "target", "payload",
               "context_versions", "consequence", "allow_closed"}
    if set(envelope) - allowed:
        raise DomainError("invalid_input", "Unknown action envelope field")
    if envelope.get("kind") not in KINDS:
        raise DomainError("invalid_input", "Unsupported external effect")
    for name in ("account_id", "application_id"):
        bounded_text(envelope.get(name), name)
    if type(envelope.get("pursuit_no")) is not int or envelope["pursuit_no"] < 1:
        raise DomainError("invalid_input", "Invalid pursuit")
    for name in ("target", "payload", "context_versions"):
        if not isinstance(envelope.get(name), dict):
            raise DomainError("invalid_input", "Invalid " + name)
    if any(not isinstance(k, str) or type(v) is not int or v < 1
           for k, v in envelope["context_versions"].items()):
        raise DomainError("invalid_input", "Invalid context versions")
    if type(envelope.get("allow_closed", False)) is not bool:
        raise DomainError("invalid_input", "Invalid closed-context choice")
    envelope.setdefault("allow_closed", False)
    envelope.setdefault("consequence", None)
    payload, target, kind = envelope["payload"], envelope["target"], envelope["kind"]
    if kind in {"send_reply", "create_reply_draft"}:
        if set(target) != {"message_id", "provider_message_id", "source_hash", "provider_source_hash"}:
            raise DomainError("invalid_input", "Reply target fields must match the exact supported effect")
        bounded_text(target.get("message_id"), "message identity")
        bounded_text(target.get("provider_message_id"), "provider message identity")
        for name in ("source_hash", "provider_source_hash"):
            bounded_text(target.get(name), name, 64)
            if len(target[name]) != 64 or any(c not in "0123456789abcdef" for c in target[name]):
                raise DomainError("invalid_input", "Invalid source hash")
        recipients = payload.get("recipients")
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 10:
            raise DomainError("invalid_input", "Expected one to ten exact recipients")
        for address in recipients:
            bounded_text(address, "recipient", 320)
            if "@" not in address or any(c.isspace() for c in address):
                raise DomainError("invalid_input", "Invalid recipient")
        if len(set(a.casefold() for a in recipients)) != len(recipients):
            raise DomainError("invalid_input", "Duplicate recipient")
        bounded_text(payload.get("subject"), "subject", 1000)
        bounded_text(payload.get("body"), "body", 100_000)
        if set(payload) - {"recipients", "subject", "body"}:
            raise DomainError("invalid_input", "Unsupported reply fields")
    else:
        expected_target = set() if kind == "create_calendar_entry" else {"remote_id", "transaction_id", "etag"}
        if set(target) != expected_target:
            raise DomainError("invalid_input", "Calendar effects support only the default calendar and exact owned-item targets")
        if set(payload) - {"starts_at", "ends_at", "location", "join_url"}:
            raise DomainError("invalid_input", "Only owned private calendar fields are supported")
        if kind != "cancel_calendar_entry":
            if instant(payload.get("ends_at")) <= instant(payload.get("starts_at")):
                raise DomainError("invalid_input", "Invalid calendar interval")
            for name in ("location", "join_url"):
                if not isinstance(payload.get(name, ""), str) or len(payload.get(name, "")) > 2000:
                    raise DomainError("invalid_input", "Invalid calendar details")
        if kind != "create_calendar_entry":
            for name in ("remote_id", "transaction_id", "etag"):
                bounded_text(target.get(name), name)
        if kind == "cancel_calendar_entry" and payload:
            raise DomainError("invalid_input", "Calendar deletion has no editable fields")
    consequence = envelope["consequence"]
    if consequence is not None:
        if kind != "send_reply" or not isinstance(consequence, dict):
            raise DomainError("invalid_input", "Only verified sending can complete the linked reply task")
        if set(consequence) != {"operation", "task_id", "expected_version", "completion_rule"}:
            raise DomainError("invalid_input", "Invalid exact internal consequence")
        if consequence["operation"] != "complete_task" or consequence["completion_rule"] != "verified_send":
            raise DomainError("invalid_input", "Unsupported internal consequence")
        bounded_text(consequence["task_id"], "task identity")
        if type(consequence["expected_version"]) is not int or consequence["expected_version"] < 1:
            raise DomainError("invalid_input", "Invalid task version")
    return envelope


class ExternalActionOperations:
    """Public participants run inside the caller's shared transaction."""

    def prepare_reply(self, tx, envelope=None, *, expires_at=None, **fields):
        value = validate_envelope(envelope if envelope is not None else fields)
        if value["kind"] not in {"send_reply", "create_reply_draft"}:
            raise DomainError("invalid_input", "Expected a reply effect")
        return self._prepare(tx, value, expires_at)

    def prepare_calendar_change(self, tx, envelope=None, *, expires_at=None, **fields):
        value = validate_envelope(envelope if envelope is not None else fields)
        if value["kind"] in {"send_reply", "create_reply_draft"}:
            raise DomainError("invalid_input", "Expected a calendar effect")
        if value["kind"] != "create_calendar_entry":
            owned = self.get_owned_event(tx.connection, value["account_id"], value["target"]["remote_id"])
            if not owned or not owned["active"] or owned["application_id"] != value["application_id"] or owned["transaction_id"] != value["target"]["transaction_id"] or owned["etag"] != value["target"]["etag"]:
                raise DomainError("version_conflict", "Calendar target has no matching platform ownership receipt")
        return self._prepare(tx, value, expires_at)

    def _prepare(self, tx, envelope, expires_at):
        if expires_at is None:
            expires_at = stamp(instant(tx.now) + timedelta(days=7))
        if instant(expires_at) <= instant(tx.now):
            raise DomainError("invalid_input", "Proposal expiry must be in the future")
        expires_at = stamp(instant(expires_at))
        action_id = uuid.uuid4().hex
        envelope = {**envelope, "encoding_version": 1, "operation_id": "career-" + action_id,
                    "expires_at": expires_at}
        hashed = digest(envelope)
        with tx.scope("external_actions"):
            tx.connection.execute("""INSERT INTO action_revisions
                (action_id,envelope_json,digest,application_id,account_id,kind,operation_id,expires_at,
                 created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (action_id, encode(envelope), hashed, envelope["application_id"], envelope["account_id"],
                 envelope["kind"], envelope["operation_id"], expires_at, tx.now, tx.now))
            result = self.get(tx.connection, action_id)
            tx.record("external_actions", action_id, "prepare_action", None, result)
        return result

    @staticmethod
    def get(connection, action_id):
        row = connection.execute("SELECT * FROM action_revisions WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "External action not found")
        result = dict(row)
        result["envelope"] = json.loads(result.pop("envelope_json"))
        result["checkpoint"] = json.loads(result.pop("checkpoint_json"))
        return result

    @staticmethod
    def readiness_summary(connection):
        """Aggregate execution health without reading retired provider records."""
        counts = dict(connection.execute("SELECT execution,COUNT(*) FROM action_revisions GROUP BY execution"))
        historical = connection.execute("SELECT COUNT(*) FROM action_imports WHERE unresolved=1").fetchone()[0]
        restored = dict(connection.execute("""SELECT a.execution,COUNT(*)
            FROM installation_restore_quarantine q JOIN action_revisions a ON a.action_id=q.record_id
            WHERE q.kind='external_action' AND a.execution!='succeeded' GROUP BY a.execution"""))
        # A local cancellation after restore is not proof that the provider did
        # nothing after the snapshot. Preserve that unresolved historical fact.
        quarantine = sum(restored.values())
        return {"execution_counts": counts,
                "uncertain": counts.get("uncertain", 0) + historical + quarantine - restored.get("uncertain", 0),
                "historical_uncertain": historical, "restore_quarantined_actions": quarantine}

    @staticmethod
    def list_actions(connection, application_id=None, *, application_ids=None, after="", limit=100):
        """Bounded installation-wide read, or an explicitly scoped application history."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError("invalid_input", "Invalid action query bound")
        clauses, params = ["action_id>?"], [after]
        if application_id is not None or application_ids is not None:
            identities = [application_id] if application_ids is None else application_ids
            if not isinstance(identities,(list,tuple)) or not 1<=len(identities)<=200 or any(not isinstance(item,str) or not item for item in identities):
                raise DomainError("invalid_input", "Invalid application identity set")
            clauses.append("application_id IN ("+",".join("?" for _ in identities)+")")
            params.extend(identities)
        rows = connection.execute("SELECT action_id FROM action_revisions WHERE " + " AND ".join(clauses) + " ORDER BY action_id LIMIT ?",
                                  (*params, limit + 1)).fetchall()
        items = [ExternalActionOperations.get(connection, row[0]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["action_id"] if len(rows) > limit else None}

    @staticmethod
    def list_review_actions(connection, *, application_ids=None, after="", limit=50):
        """Pending approvals and execution needing visibility, filtered before paging."""
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after, str):
            raise DomainError("invalid_input", "Invalid action review page")
        where = ["action_id>?", "(authorization='pending' OR execution IN ('queued','executing','awaiting_confirmation','uncertain') OR (execution='failed' AND authorization='approved'))"]
        values = [after]
        if application_ids is not None:
            if not isinstance(application_ids, (list, tuple)) or not 1 <= len(application_ids) <= 200 or any(not isinstance(i, str) or not i for i in application_ids):
                raise DomainError("invalid_input", "Invalid application identity set")
            where.append("application_id IN (" + ",".join("?" for _ in application_ids) + ")")
            values.extend(application_ids)
        rows = connection.execute("SELECT action_id FROM action_revisions WHERE " + " AND ".join(where) + " ORDER BY action_id LIMIT ?", (*values, limit + 1)).fetchall()
        items = [ExternalActionOperations.get(connection, row[0]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["action_id"] if len(rows) > limit else None, "truncated": len(rows) > limit}

    def authorize_action(self, tx, action_id, expected_digest, *, applicability):
        require_human(tx)
        with tx.scope("external_actions"):
            before = self.get(tx.connection, action_id)
            if expected_digest != before["digest"] or digest(before["envelope"]) != expected_digest:
                raise DomainError("version_conflict", "Action content or context changed")
            if before["execution"] not in {"not_started", "failed", "cancelled"} or before["authorization"] == "rejected":
                raise DomainError("needs_reconciliation", "Action cannot be authorized in its current state")
            if before["cancellation_requested"]:
                raise DomainError("version_conflict", "Context was invalidated; prepare a new action")
            if instant(before["expires_at"]) <= instant(tx.now):
                raise DomainError("version_conflict", "Action proposal expired")
            if not applicability(tx, before["envelope"]):
                raise DomainError("version_conflict", "Action context changed")
            until = stamp(min(instant(before["expires_at"]), instant(tx.now) + timedelta(minutes=15)))
            tx.connection.execute("UPDATE action_revisions SET authorization='approved',authorized_by=?,approved_until=?,execution='queued',updated_at=? WHERE action_id=?",
                                  (tx.context.principal.actor_id, until, tx.now, action_id))
            tx.enqueue("external_actions", "execute_action", action_id + ":" + until, {"action_id": action_id})
            return self._record(tx, before, "authorize_action")

    def reject_action(self, tx, action_id, expected_digest):
        return self._decide(tx, action_id, expected_digest, "rejected")

    def revoke_action(self, tx, action_id, expected_digest):
        return self._decide(tx, action_id, expected_digest, "revoked")

    def _decide(self, tx, action_id, expected_digest, decision):
        require_human(tx)
        with tx.scope("external_actions"):
            before = self.get(tx.connection, action_id)
            if expected_digest != before["digest"]:
                raise DomainError("version_conflict", "Action changed")
            execution = before["execution"] if before["execution"] in UNRESOLVED | {"succeeded"} else "cancelled"
            tx.connection.execute("UPDATE action_revisions SET authorization=?,execution=?,cancellation_requested=1,updated_at=? WHERE action_id=?",
                                  (decision, execution, tx.now, action_id))
            return self._record(tx, before, decision)

    def claim_action(self, tx, action_id, worker_id, *, lease_seconds=60,
                     activation_revision=None, activation_guard=None):
        require_worker(tx)
        if tx.restore_quarantined("external_action", action_id):
            raise DomainError("needs_reconciliation", "Restored action cannot execute from a historical approval")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise DomainError("invalid_input", "Invalid execution lease")
        bounded_text(worker_id, "worker identity")
        with tx.scope("external_actions"):
            before = self.get(tx.connection, action_id)
            if activation_revision is not None:
                if type(activation_revision) is not int or activation_revision < 0 or activation_guard is None:
                    raise DomainError("not_authorized", "Production claim requires its activation fence")
                if not activation_guard(tx, activation_revision):
                    return {**before, "dispatch_paused": True}
            if before["execution"] != "queued":
                raise DomainError("needs_reconciliation", "Action is not queued for a safe new attempt")
            if before["retry_at"] and instant(before["retry_at"]) > instant(tx.now):
                return before
            if not self._authorized(before, tx.now):
                tx.connection.execute("UPDATE action_revisions SET authorization='expired',execution='cancelled',updated_at=? WHERE action_id=?", (tx.now, action_id))
                return self._record(tx, before, "approval_expired")
            if before["attempt_count"] >= 5:
                tx.connection.execute("UPDATE action_revisions SET execution='failed',error_code='attempt_limit',updated_at=? WHERE action_id=?", (tx.now, action_id))
                return self._record(tx, before, "attempt_limit")
            fence = before["fence"] + 1
            tx.connection.execute("UPDATE action_revisions SET execution='executing',fence=?,worker_id=?,lease_until=?,attempt_count=attempt_count+1,write_intent=0,retry_at=NULL,updated_at=? WHERE action_id=?",
                                  (fence, worker_id, stamp(instant(tx.now) + timedelta(seconds=lease_seconds)), tx.now, action_id))
            tx.connection.execute("INSERT INTO action_attempts(action_id,fence,worker_id,started_at) VALUES(?,?,?,?)", (action_id, fence, worker_id, tx.now))
            if activation_revision is not None:
                tx.connection.execute("INSERT INTO action_activation_fences VALUES(?,?,?)", (action_id, fence, activation_revision))
            return self._record(tx, before, "claim_action")

    def _activation_allows(self, tx, before, activation_guard):
        row = tx.connection.execute("SELECT activation_revision FROM action_activation_fences WHERE action_id=? AND fence=?", (before["action_id"], before["fence"])).fetchone()
        if row is None:
            return True  # Explicit test dispatch has no production activation fence.
        if activation_guard is None:
            raise DomainError("not_authorized", "Production write requires its activation guard")
        return bool(activation_guard(tx, row[0]))

    def _park(self, tx, before):
        tx.connection.execute("UPDATE action_revisions SET execution='queued',fence=fence+1,lease_until=NULL,write_intent=0,error_code='activation_changed',updated_at=? WHERE action_id=?", (tx.now, before["action_id"]))
        tx.connection.execute("UPDATE action_attempts SET ended_at=?,outcome='paused' WHERE action_id=? AND fence=?", (tx.now, before["action_id"], before["fence"]))
        return {**self._record(tx, before, "pause_execution"), "dispatch_paused": True}

    def renew_claim(self, tx, action_id, fence, *, lease_seconds=180, activation_guard=None):
        require_worker(tx)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise DomainError("invalid_input", "Invalid execution lease")
        with tx.scope("external_actions"):
            before = self.get(tx.connection, action_id)
            self._fenced(before, fence, tx.now)
            if before["write_intent"]:
                raise DomainError("needs_reconciliation", "Cannot renew across an unfinished provider write")
            if not self._activation_allows(tx, before, activation_guard):
                return self._park(tx, before)
            tx.connection.execute("UPDATE action_revisions SET lease_until=?,updated_at=? WHERE action_id=?", (stamp(instant(tx.now) + timedelta(seconds=lease_seconds)), tx.now, action_id))
            return self.get(tx.connection, action_id)

    def begin_write(self, tx, action_id, fence, *, applicability, activation_guard=None):
        require_worker(tx)
        if tx.restore_quarantined("external_action", action_id):
            raise DomainError("needs_reconciliation", "Restored action cannot resume a historical provider write")
        with tx.scope("external_actions"):
            before = self.get(tx.connection, action_id)
            self._fenced(before, fence, tx.now)
            if before["write_intent"]:
                raise DomainError("needs_reconciliation", "A recorded write intent cannot be replayed")
            if not self._activation_allows(tx, before, activation_guard):
                return self._park(tx, before)
            error_code = "context_invalid"
            applicable = False
            if self._authorized(before, tx.now):
                try:
                    applicable = bool(applicability(tx, before["envelope"]))
                except DomainError as exc:
                    # A relevant owner can reject with a typed conflict instead
                    # of returning False. Persist the blocked attempt rather than
                    # leaving an executing lease with no provider write.
                    error_code = "context_invalid:" + exc.code
            if not applicable:
                tx.connection.execute("UPDATE action_revisions SET execution='cancelled',authorization='revoked',cancellation_requested=1,error_code=?,updated_at=? WHERE action_id=?", (error_code, tx.now, action_id))
                tx.connection.execute("UPDATE action_attempts SET ended_at=?,outcome='cancelled' WHERE action_id=? AND fence=?", (tx.now, action_id, fence))
                return self._record(tx, before, "blocked_write")
            tx.connection.execute("UPDATE action_revisions SET write_intent=1,updated_at=? WHERE action_id=?", (tx.now, action_id))
            self._checkpoint(tx, before, "intent", before["checkpoint"])
            return self._record(tx, before, "begin_write")

    def finish_attempt(self, tx, action_id, fence, outcome):
        require_worker(tx)
        with tx.scope("external_actions"):
            before = self.get(tx.connection, action_id)
            # Preserve late observations for recovery, but a stale claimant can
            # never publish success or a task consequence through the old fence.
            try:
                self._fenced(before, fence, tx.now)
            except DomainError:
                attempt = tx.connection.execute("SELECT 1 FROM action_attempts WHERE action_id=? AND fence=?", (action_id, fence)).fetchone()
                if not attempt:
                    raise
                value = asdict(outcome) if is_dataclass(outcome) else dict(outcome)
                tx.connection.execute("INSERT INTO action_checkpoints VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, action_id, fence, "late_observation", encode(value), tx.now))
                if before["execution"] == "executing" and instant(before["lease_until"]) <= instant(tx.now):
                    tx.connection.execute("UPDATE action_revisions SET execution='uncertain',fence=fence+1,error_code='lease_expired',updated_at=? WHERE action_id=?", (tx.now, action_id))
                return self._record(tx, before, "late_observation")
            return self._outcome(tx, before, outcome, reconciliation=False)

    def reconcile_result(self, tx, action_id, outcome, *, expected_fence=None):
        require_worker(tx)
        with tx.scope("external_actions"):
            before = self.get(tx.connection, action_id)
            if expected_fence is not None and expected_fence != before["fence"]:
                raise DomainError("version_conflict", "Reconciliation snapshot is stale")
            if before["execution"] == "succeeded":
                return before
            if before["execution"] not in {"uncertain", "awaiting_confirmation"}:
                raise DomainError("needs_reconciliation", "No uncertain effect to reconcile")
            return self._outcome(tx, before, outcome, reconciliation=True)

    def _outcome(self, tx, before, outcome, *, reconciliation):
        value = asdict(outcome) if is_dataclass(outcome) else dict(outcome)
        status = value.get("status")
        allowed = {"progress", "succeeded", "accepted", "uncertain", "mismatch", "not_executed", "pre_effect_failure", "failed"}
        if status not in allowed:
            raise DomainError("invalid_input", "Invalid provider outcome")
        if status in {"progress", "succeeded", "accepted"} and not (before["write_intent"] or reconciliation):
            raise DomainError("invalid_input", "No durable intent exists for this effect")
        if status == "not_executed" and not reconciliation:
            raise DomainError("invalid_input", "Nonexecution proof must pass through reconciliation")
        if status == "pre_effect_failure" and before["write_intent"]:
            raise DomainError("needs_reconciliation", "Recorded write requires reconciliation")
        checkpoint = value.get("checkpoint") or before["checkpoint"]
        observation = value.get("observation") or {}
        if not isinstance(checkpoint, dict) or not isinstance(observation, dict):
            raise DomainError("invalid_input", "Invalid provider evidence")
        state = {"progress": "executing", "succeeded": "succeeded", "accepted": "awaiting_confirmation",
                 "uncertain": "uncertain", "mismatch": "uncertain", "failed": "failed"}.get(status)
        if status in {"not_executed", "pre_effect_failure"}:
            state = "queued" if self._authorized(before, tx.now) and before["attempt_count"] < 5 else "failed"
        if reconciliation and status == "progress":
            # Resuming from proven intermediate state needs a new authorized claim.
            state = "queued" if self._authorized(before, tx.now) and before["attempt_count"] < 5 else "failed"
        if status == "failed" and before["write_intent"]:
            state = "uncertain"
        self._checkpoint(tx, before, status, {"checkpoint": checkpoint, "observation": observation})
        tx.connection.execute("UPDATE action_revisions SET execution=?,checkpoint_json=?,write_intent=?,error_code=?,updated_at=? WHERE action_id=?",
                              (state, encode(checkpoint), int(state in {"uncertain", "awaiting_confirmation"}), value.get("error_code", ""), tx.now, before["action_id"]))
        if state != "executing":
            tx.connection.execute("UPDATE action_attempts SET ended_at=?,outcome=? WHERE action_id=? AND fence=?", (tx.now, state, before["action_id"], before["fence"]))
        if state == "queued":
            retry_at = stamp(instant(tx.now) + timedelta(seconds=min(300, 30 * 2 ** max(0, before["attempt_count"] - 1))))
            tx.connection.execute("UPDATE action_revisions SET retry_at=? WHERE action_id=?", (retry_at, before["action_id"]))
            tx.enqueue("external_actions", "execute_action", before["action_id"] + ":retry:" + str(before["fence"]), {"action_id": before["action_id"], "available_at": retry_at})
        if state == "succeeded":
            self._result(tx, before, observation)
        return self._record(tx, before, "reconcile_action" if reconciliation else "finish_attempt")

    def _result(self, tx, before, observation):
        result_id = "result-" + before["action_id"]
        pending = before["envelope"].get("consequence") is not None
        tx.connection.execute("INSERT OR IGNORE INTO action_results(result_id,action_id,application_id,envelope_json,observation_json,delivery,created_at) VALUES(?,?,?,?,?,?,?)",
                              (result_id, before["action_id"], before["application_id"], encode(before["envelope"]), encode(observation), "pending" if pending else "applied", tx.now))
        if pending:
            tx.enqueue("external_actions", "apply_action_result", result_id, {"result_id": result_id})
        envelope = before["envelope"]
        if envelope["kind"] in {"create_calendar_entry", "update_calendar_entry"}:
            remote, etag = observation.get("remote_id"), observation.get("etag")
            if not remote or not etag:
                raise DomainError("invalid_input", "Confirmed calendar result requires identity and version")
            transaction_id = envelope["operation_id"] if envelope["kind"] == "create_calendar_entry" else envelope["target"]["transaction_id"]
            tx.connection.execute("INSERT INTO action_owned_events VALUES(?,?,?,?,?,1) ON CONFLICT(account_id,remote_id) DO UPDATE SET etag=excluded.etag,active=1",
                                  (envelope["account_id"], remote, transaction_id, etag, envelope["application_id"]))
        elif envelope["kind"] == "cancel_calendar_entry":
            tx.connection.execute("UPDATE action_owned_events SET active=0 WHERE account_id=? AND remote_id=?", (envelope["account_id"], envelope["target"]["remote_id"]))

    @staticmethod
    def get_owned_event(connection, account_id, remote_id):
        row = connection.execute("SELECT * FROM action_owned_events WHERE account_id=? AND remote_id=?", (account_id, remote_id)).fetchone()
        return dict(row) if row else None

    def expire_leases(self, tx):
        require_worker(tx)
        results = []
        with tx.scope("external_actions"):
            for row in tx.connection.execute("SELECT action_id FROM action_revisions WHERE execution='executing'").fetchall():
                before = self.get(tx.connection, row[0])
                if instant(before["lease_until"]) > instant(tx.now):
                    continue
                state = "uncertain" if before["write_intent"] else "queued" if self._authorized(before, tx.now) else "cancelled"
                tx.connection.execute("UPDATE action_revisions SET execution=?,fence=fence+1,error_code='lease_expired',updated_at=? WHERE action_id=?", (state, tx.now, row[0]))
                tx.connection.execute("UPDATE action_attempts SET ended_at=?,outcome=? WHERE action_id=? AND fence=?", (tx.now, state, row[0], before["fence"]))
                if state == "queued":
                    tx.enqueue("external_actions", "execute_action", row[0] + ":recovery:" + str(before["fence"]), {"action_id": row[0]})
                results.append(self._record(tx, before, "expire_lease"))
        return {"items": results}

    @staticmethod
    def eligible_reconciliation(connection, now, *, limit=100, after=""):
        instant(now)
        if type(limit) is not int or not 1 <= limit <= 200 or not isinstance(after, str):
            raise DomainError("invalid_input", "Invalid reconciliation query bound")
        rows = connection.execute("SELECT action_id FROM action_revisions WHERE action_id>? AND execution IN ('uncertain','awaiting_confirmation') ORDER BY action_id LIMIT ?", (after, limit + 1)).fetchall()
        items = [ExternalActionOperations.get(connection, row[0]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["action_id"] if len(rows) > limit else None}

    def invalidate_context(self, tx, application_id, reason, *, target_ids=None):
        bounded_text(reason, "invalidation reason")
        items = []
        with tx.scope("external_actions"):
            for row in tx.connection.execute("SELECT action_id FROM action_revisions WHERE application_id=? AND execution<>'succeeded'", (application_id,)).fetchall():
                before = self.get(tx.connection, row[0])
                references = set(before["envelope"]["context_versions"])
                if before["envelope"].get("consequence"):
                    references.add(before["envelope"]["consequence"]["task_id"])
                if target_ids is not None and not references.intersection(target_ids):
                    continue
                state = before["execution"] if before["execution"] in UNRESOLVED else "cancelled"
                tx.connection.execute("UPDATE action_revisions SET authorization='revoked',execution=?,cancellation_requested=1,error_code=?,updated_at=? WHERE action_id=?", (state, reason, tx.now, row[0]))
                items.append(self._record(tx, before, "invalidate_context"))
        return {"items": items}

    @staticmethod
    def has_unresolved(connection, application_id):
        return (connection.execute("SELECT 1 FROM action_revisions WHERE application_id=? AND execution IN ('executing','awaiting_confirmation','uncertain') LIMIT 1", (application_id,)).fetchone() is not None
                or connection.execute("SELECT 1 FROM action_imports WHERE application_id=? AND unresolved=1 LIMIT 1", (application_id,)).fetchone() is not None)

    @staticmethod
    def get_result(connection, result_id):
        row = connection.execute("SELECT * FROM action_results WHERE result_id=?", (result_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "Action result not found")
        result = dict(row)
        result["envelope"] = json.loads(result.pop("envelope_json"))
        result["observation"] = json.loads(result.pop("observation_json"))
        return result

    get_action = get

    @staticmethod
    def list_results(connection, *, application_id=None, application_ids=None, delivery=None, after="", limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError("invalid_input", "Invalid result query bound")
        if delivery not in {None, "pending", "applied", "conflict"}:
            raise DomainError("invalid_input", "Invalid delivery filter")
        clauses, params = ["result_id>?"], [after]
        if application_ids is not None:
            if application_id is not None or not isinstance(application_ids,(list,tuple)) or not 1<=len(application_ids)<=200 or any(not isinstance(item,str) or not item for item in application_ids):
                raise DomainError("invalid_input", "Invalid application identity set")
            clauses.append("application_id IN ("+",".join("?" for _ in application_ids)+")")
            params.extend(application_ids)
        elif application_id is not None:
            clauses.append("application_id=?")
            params.append(application_id)
        if delivery is not None:
            clauses.append("delivery=?")
            params.append(delivery)
        rows = connection.execute("SELECT result_id FROM action_results WHERE " + " AND ".join(clauses)
                                  + " ORDER BY result_id LIMIT ?", (*params, limit + 1)).fetchall()
        items = [ExternalActionOperations.get_result(connection, row[0]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["result_id"] if len(rows) > limit else None}

    def acknowledge_result(self, tx, result_id, status, *, reason=""):
        require_worker(tx)
        if status not in {"applied", "conflict"}:
            raise DomainError("invalid_input", "Invalid result delivery status")
        with tx.scope("external_actions"):
            before = self.get_result(tx.connection, result_id)
            if before["delivery"] != "pending":
                return before
            tx.connection.execute("UPDATE action_results SET delivery=?,conflict_reason=?,delivered_at=? WHERE result_id=?", (status, reason, tx.now, result_id))
            after = self.get_result(tx.connection, result_id)
            tx.record("external_actions", result_id, "acknowledge_result", before, after)
            return after

    def import_history(self, tx, source_id, record):
        if tx.context.origin != "migration":
            raise DomainError("not_authorized", "Historical action import requires migration authority")
        with tx.scope("external_actions"):
            previous = tx.connection.execute("SELECT source_json FROM action_imports WHERE source_id=?", (source_id,)).fetchone()
            encoded = encode(record)
            if previous and previous[0] != encoded:
                raise DomainError("idempotency_conflict", "Historical action changed")
            status = record.get("execution") or record.get("status")
            unresolved = status in {"executing", "accepted", "awaiting_confirmation", "uncertain"} or record.get("uncertain") is True
            tx.connection.execute("INSERT OR IGNORE INTO action_imports VALUES(?,?,?,?,?)", (source_id, encoded, record.get("application_id"), int(unresolved), tx.now))
            # Historical approvals remain source records, never executable proposals.
            return {"source_id": source_id, "dispatch_enabled": False, "historical": True}

    @staticmethod
    def list_history(connection, application_id, *, after="", limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError("invalid_input", "Invalid history query bound")
        rows = connection.execute("SELECT * FROM action_imports WHERE application_id=? AND source_id>? ORDER BY source_id LIMIT ?", (application_id, after, limit + 1)).fetchall()
        items = [{"source_id": r["source_id"], "record": json.loads(r["source_json"]), "unresolved": bool(r["unresolved"])} for r in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["source_id"] if len(rows) > limit else None}

    @staticmethod
    def _authorized(action, now):
        return (action["authorization"] == "approved" and not action["cancellation_requested"]
                and action["approved_until"] is not None and instant(action["approved_until"]) > instant(now)
                and instant(action["expires_at"]) > instant(now))

    @staticmethod
    def _fenced(action, fence, now):
        if action["fence"] != fence or action["execution"] != "executing" or instant(action["lease_until"]) <= instant(now):
            raise DomainError("version_conflict", "Execution claim is stale; reconcile the effect")

    @staticmethod
    def _checkpoint(tx, before, phase, details):
        tx.connection.execute("INSERT INTO action_checkpoints VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, before["action_id"], before["fence"], phase, encode(details), tx.now))

    def _record(self, tx, before, operation):
        after = self.get(tx.connection, before["action_id"])
        tx.record("external_actions", before["action_id"], operation, before, after)
        return after
