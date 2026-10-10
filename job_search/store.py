"""Transactional repository for the deterministic job-search ledger."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    ACTION_APPROVAL_TTL_SECONDS,
    ACTION_EXECUTION_MAX_ATTEMPTS,
    EVENT_SCHEMA_VERSION,
    ActionKind,
    ActionProposalInput,
    ActionStatus,
    ApplicationEventType,
    ApplicationPhase,
    ConflictError,
    ContractError,
    EventInput,
    EventProposalInput,
    JobSnapshot,
    MutationContext,
    RecommendationProvenance,
    MODEL_AUTO_APPLY_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    TerminalOutcome,
    TemporalProposalInput,
    TemporalProposalKind,
    bounded_candidate_ids,
    canonical_json,
    parse_utc,
    payload_sha256,
    utc_now,
    validate_event_payload,
    validate_identifier,
)
from .db import connect, prepare_database
from .reducer import ApplicationState, projection_mismatches, reduce_events


def _new_id() -> str:
    return uuid.uuid4().hex


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


APPLICATION_NOTIFICATION_EVENTS = frozenset(
    {
        ApplicationEventType.RECRUITER_CONTACT,
        ApplicationEventType.ASSESSMENT_REQUESTED,
        ApplicationEventType.INTERVIEW_REQUESTED,
        ApplicationEventType.INTERVIEW_SCHEDULED,
        ApplicationEventType.OFFER_RECEIVED,
        ApplicationEventType.REJECTION_RECEIVED,
    }
)


class LedgerStore:
    """The only writer for job-search application state."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        prepare_database(self.db_path, utc_now())

    def _idempotent(
        self,
        command_name: str,
        context: MutationContext,
        request: Any,
        operation: Callable[[sqlite3.Connection, str], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        context.validate()
        request_hash = payload_sha256(request)
        stamp = utc_now()
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute(
                "SELECT request_sha256,response_json FROM command_results "
                "WHERE command_name=? AND idempotency_key=?",
                (command_name, context.idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_sha256"] != request_hash:
                    raise ConflictError(
                        "idempotency key was already used with a different request"
                    )
                result = json.loads(previous["response_json"])
                con.commit()
                return result
            result = dict(operation(con, stamp))
            response_json = canonical_json(result)
            con.execute(
                "INSERT INTO command_results "
                "(command_name,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    command_name,
                    context.idempotency_key,
                    request_hash,
                    response_json,
                    stamp,
                ),
            )
            con.commit()
            return json.loads(response_json)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _events(
        con: sqlite3.Connection, application_id: str
    ) -> Sequence[sqlite3.Row]:
        return con.execute(
            "SELECT * FROM application_events WHERE application_id=? ORDER BY event_seq",
            (application_id,),
        ).fetchall()

    @staticmethod
    def _project(con: sqlite3.Connection, application_id: str) -> ApplicationState:
        state = reduce_events(LedgerStore._events(con, application_id))
        values = state.projection_values()
        con.execute(
            "UPDATE applications SET current_phase=?,terminal_outcome=?,started_at=?,"
            "submitted_at=?,confirmed_at=?,last_activity_at=?,last_event_seq=?,"
            "projection_sha256=?,updated_at=? WHERE application_id=?",
            (
                values["current_phase"],
                values["terminal_outcome"],
                values["started_at"],
                values["submitted_at"],
                values["confirmed_at"],
                values["last_activity_at"],
                values["last_event_seq"],
                state.projection_sha256,
                values["updated_at"],
                application_id,
            ),
        )
        # A verified confirmation email may arrive before the browser sees the
        # success page. Finalize its tracked attempt under this same ledger lock.
        if state.confirmed_at:
            attempt = con.execute("SELECT * FROM browser_attempts WHERE application_id=? ORDER BY created_at DESC LIMIT 1", (application_id,)).fetchone()
            if attempt:
                fresh = con.execute("INSERT OR IGNORE INTO browser_finalizations VALUES (?,?,?)", (application_id, attempt["attempt_id"], state.confirmed_at)).rowcount
                observed = con.execute("SELECT 1 FROM application_events WHERE application_id=? AND event_type='submission_observed'", (application_id,)).fetchone()
                if fresh and not observed:
                    event, created = LedgerStore._append_event(con, application_id, ApplicationEventType.SUBMISSION_OBSERVED,
                        state.confirmed_at, {"observed_by": "confirmation_email", "resume": json.loads(attempt["resume_json"])},
                        "browser-submission:"+application_id, MutationContext("browser-finalize:"+application_id, "system", "browser_extension", attempt["attempt_id"]), utc_now())
                    if created:
                        state = LedgerStore._project(con, application_id)
                        LedgerStore._insert_feedback_outbox(con, LedgerStore._application(con, application_id), event, utc_now())
        if state.current_phase is ApplicationPhase.TERMINAL:
            from .lifecycle.core import close_application_work
            close_application_work(con, application_id, utc_now())
        return state

    @staticmethod
    def _application(con: sqlite3.Connection, application_id: str) -> Dict[str, Any]:
        row = con.execute(
            "SELECT * FROM applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if not row:
            raise ContractError("application not found")
        return _row(row)

    @staticmethod
    def _event(con: sqlite3.Connection, event_id: str) -> Dict[str, Any]:
        row = con.execute(
            "SELECT * FROM application_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            raise ContractError("application event not found")
        result = _row(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    @staticmethod
    def _append_event(
        con: sqlite3.Connection,
        application_id: str,
        event_type: ApplicationEventType,
        occurred_at: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        context: MutationContext,
        recorded_at: str,
    ) -> Tuple[Dict[str, Any], bool]:
        previous = con.execute(
            "SELECT * FROM application_events WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        encoded_payload = canonical_json(payload)
        if previous:
            same = (
                previous["application_id"] == application_id
                and previous["event_type"] == event_type.value
                and previous["occurred_at"] == occurred_at
                and previous["actor_kind"] == context.actor_kind
                and previous["source_kind"] == context.source_kind
                and previous["source_ref"] == context.source_ref
                and previous["payload_json"] == encoded_payload
            )
            if not same:
                raise ConflictError("event dedupe key conflicts with an existing event")
            return LedgerStore._event(con, previous["event_id"]), False

        event_id = _new_id()
        con.execute(
            "INSERT INTO application_events "
            "(event_id,application_id,event_type,occurred_at,recorded_at,actor_kind,"
            "source_kind,source_ref,dedupe_key,schema_version,payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                application_id,
                event_type.value,
                occurred_at,
                recorded_at,
                context.actor_kind,
                context.source_kind,
                context.source_ref,
                dedupe_key,
                EVENT_SCHEMA_VERSION,
                encoded_payload,
            ),
        )
        from .mail.understanding_store import available
        replay = available(con) and con.execute("SELECT 1 FROM mail_understanding_projections p JOIN mail_understanding_findings f USING(finding_id) JOIN mail_understanding_analyses a USING(analysis_id) WHERE p.target_id=? AND a.mode='replay' LIMIT 1",(context.source_ref,)).fetchone()
        if event_type in APPLICATION_NOTIFICATION_EVENTS and not replay:
            LedgerStore._insert_application_notification_outbox(
                con, application_id, event_type, event_id, recorded_at
            )
        return LedgerStore._event(con, event_id), True

    def start_application(
        self,
        snapshot: JobSnapshot,
        provenance: RecommendationProvenance,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        snapshot.validate()
        provenance.validate()
        request = {
            "snapshot": asdict(snapshot),
            "provenance": asdict(provenance),
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
            "source_ref": context.source_ref,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            return self._start_application(con, snapshot, provenance, context, stamp)

        return self._idempotent("start_application", context, request, operation)

    def _start_application(self, con, snapshot, provenance, context, stamp):
        """Create or reuse a catalog application within the caller's transaction."""
        existing = con.execute(
            "SELECT * FROM applications WHERE ats=? AND job_id=?",
            (snapshot.ats, snapshot.job_id),
        ).fetchone()
        if existing:
            return {"created": False, "application": _row(existing)}

        application_id = _new_id()
        con.execute(
            "INSERT INTO applications "
            "(application_id,ats,job_id,family_id,title_snapshot,employer_snapshot,"
            "company_slug_snapshot,job_url_snapshot,recommendation_session_id,"
            "recommendation_impression_id,recommendation_model_run_id,"
            "recommendation_policy_id,recommendation_rank,semantic_score,ranking_score,"
            "current_phase,terminal_outcome,started_at,submitted_at,confirmed_at,"
            "last_activity_at,last_event_seq,projection_sha256,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                application_id,
                snapshot.ats,
                snapshot.job_id,
                snapshot.family_id,
                snapshot.title,
                snapshot.employer,
                snapshot.company_slug,
                snapshot.job_url,
                provenance.session_id,
                provenance.impression_id,
                provenance.model_run_id,
                provenance.policy_id,
                provenance.rank,
                provenance.semantic_score,
                provenance.ranking_score,
                ApplicationPhase.PREPARING.value,
                None,
                stamp,
                None,
                None,
                stamp,
                0,
                "",
                stamp,
            ),
        )
        start_dedupe = "application-started:" + payload_sha256(
            {"ats": snapshot.ats, "job_id": snapshot.job_id}
        )
        self._append_event(
            con,
            application_id,
            ApplicationEventType.APPLICATION_STARTED,
            stamp,
            {"snapshot": asdict(snapshot), "provenance": asdict(provenance)},
            start_dedupe,
            context,
            stamp,
        )
        self._project(con, application_id)
        return {
            "created": True,
            "application": self._application(con, application_id),
        }

    def record_mail_evidence(
        self,
        evidence: Mapping[str, Any],
        context: MutationContext,
        *,
        _transaction: Optional[Tuple[sqlite3.Connection, str]] = None,
    ) -> Mapping[str, Any]:
        account_id = str(evidence.get("account_id") or "").strip()
        immutable_id = str(evidence.get("immutable_message_id") or "").strip()
        sender = str(evidence.get("sender") or "").strip()[:500]
        subject = str(evidence.get("subject") or "")[:1000]
        received_at = str(evidence.get("received_at") or "")
        body_hash = str(evidence.get("body_sha256") or "").lower()
        excerpt = str(evidence.get("excerpt") or "")
        if not account_id or not immutable_id or not sender:
            raise ContractError("mail evidence requires account, immutable message, and sender")
        parse_utc(received_at)
        if not re.fullmatch(r"[0-9a-f]{64}", body_hash):
            raise ContractError("mail evidence body_sha256 is invalid")
        if len(excerpt) > 2048:
            raise ContractError("mail evidence excerpt exceeds 2048 characters")
        normalized = {
            "account_id": account_id,
            "immutable_message_id": immutable_id,
            "conversation_id": str(evidence.get("conversation_id") or "")[:500],
            "sender": sender,
            "subject": subject,
            "received_at": received_at,
            "body_sha256": body_hash,
            "excerpt": excerpt,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            existing = con.execute(
                "SELECT * FROM mail_evidence WHERE account_id=? "
                "AND immutable_message_id=?",
                (account_id, immutable_id),
            ).fetchone()
            if existing:
                same = all(existing[name] == value for name, value in normalized.items())
                if not same:
                    raise ConflictError(
                        "immutable Outlook message already has different evidence"
                    )
                return {"evidence": _row(existing), "created": False}
            evidence_id = _new_id()
            con.execute(
                "INSERT INTO mail_evidence "
                "(evidence_id,account_id,immutable_message_id,conversation_id,sender,"
                "subject,received_at,body_sha256,excerpt,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    normalized["account_id"],
                    normalized["immutable_message_id"],
                    normalized["conversation_id"],
                    normalized["sender"],
                    normalized["subject"],
                    normalized["received_at"],
                    normalized["body_sha256"],
                    normalized["excerpt"],
                    stamp,
                ),
            )
            return {
                "evidence": _row(con.execute(
                    "SELECT * FROM mail_evidence WHERE evidence_id=?", (evidence_id,)
                ).fetchone()),
                "created": True,
            }

        if _transaction is not None:
            context.validate()
            return operation(*_transaction)
        return self._idempotent("record_mail_evidence", context, normalized, operation)

    def get_mail_evidence(self, evidence_id: str) -> Mapping[str, Any]:
        """Return one persisted sanitized excerpt; never a raw message body."""

        validate_identifier(evidence_id, "evidence_id")
        with connect(self.db_path) as con:
            saved = con.execute(
                "SELECT evidence_id,account_id,immutable_message_id,conversation_id,"
                "sender,subject,received_at,body_sha256,excerpt,created_at "
                "FROM mail_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if not saved:
                raise ContractError("mail evidence not found")
            return _row(saved)

    def resolve_reply_evidence(
        self, evidence_id: str, application_id: str, account_id: str
    ) -> Mapping[str, Any]:
        """Resolve an incoming reply target through reviewed evidence or explicit linkage."""

        validate_identifier(evidence_id, "evidence_id")
        validate_identifier(application_id, "application_id")
        validate_identifier(account_id, "account_id")
        with connect(self.db_path) as con:
            self._application(con, application_id)
            saved = con.execute(
                "SELECT e.evidence_id,e.account_id,e.immutable_message_id "
                "FROM mail_evidence e WHERE e.evidence_id=? AND e.account_id=? "
                "AND NOT EXISTS (SELECT 1 FROM lifecycle_mail_observations m WHERE m.evidence_id=e.evidence_id AND m.direction IN ('outbound','draft')) "
                "AND (EXISTS ("
                "SELECT 1 FROM event_proposals p "
                "JOIN application_events a ON a.event_id=p.applied_event_id "
                "WHERE p.evidence_id=e.evidence_id "
                "AND p.status IN ('accepted','auto_applied') "
                "AND a.application_id=?"
                ") OR EXISTS (SELECT 1 FROM lifecycle_mail_observations m "
                "JOIN lifecycle_mail_links l USING(observation_id) WHERE m.evidence_id=e.evidence_id "
                "AND m.account_id=e.account_id AND m.direction='inbound' AND l.application_id=?))",
                (evidence_id, account_id, application_id, application_id),
            ).fetchone()
            if not saved:
                raise ContractError(
                    "reply evidence does not match the application and Outlook account"
                )
            result = _row(saved)
            result["application_id"] = application_id
            return result

    @staticmethod
    def _archive_binary(value: Any, field: str, *, nonce: bool = False) -> bytes:
        if not isinstance(value, bytes):
            raise ContractError(f"{field} must be bytes")
        if nonce and len(value) != 12:
            raise ContractError("archive nonce must be 12 bytes")
        if not nonce and not 16 <= len(value) <= 5 * 1024 * 1024:
            raise ContractError("archive ciphertext size is invalid")
        return value

    def put_mail_archive(
        self, record: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        """Store only authenticated ciphertext for a sanitized full message."""

        required = {
            "account_id", "immutable_message_id", "key_id", "nonce", "ciphertext",
            "aad_sha256", "sanitized_sha256", "sanitized_chars", "truncated",
        }
        if not isinstance(record, Mapping) or set(record) != required:
            raise ContractError("mail archive record fields do not match schema")
        for name in ("account_id", "key_id"):
            if not isinstance(record[name], str):
                raise ContractError(f"{name} must be text")
            validate_identifier(record[name], name)
        message_id = record["immutable_message_id"]
        if not isinstance(message_id, str) or not message_id or len(message_id) > 2048:
            raise ContractError("immutable message id is invalid")
        nonce = self._archive_binary(record["nonce"], "nonce", nonce=True)
        ciphertext = self._archive_binary(record["ciphertext"], "ciphertext")
        for field in ("aad_sha256", "sanitized_sha256"):
            if not isinstance(record[field], str) or not re.fullmatch(
                r"[0-9a-f]{64}", record[field]
            ):
                raise ContractError(f"{field} must be SHA-256")
        chars = record["sanitized_chars"]
        if isinstance(chars, bool) or not isinstance(chars, int) or not 0 < chars <= 1_000_000:
            raise ContractError("sanitized character count is invalid")
        if not isinstance(record["truncated"], bool):
            raise ContractError("archive truncated flag must be boolean")
        # The authenticated envelope is deliberately randomized. Retries therefore
        # identify the logical plaintext digest and bound metadata, not the fresh
        # nonce/ciphertext produced before the idempotency lookup.
        normalized_request = {
            key: record[key] for key in required - {"nonce", "ciphertext"}
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            existing = con.execute(
                "SELECT * FROM mail_archive WHERE account_id=? AND immutable_message_id=?",
                (str(record["account_id"]), message_id),
            ).fetchone()
            if existing and existing["sanitized_sha256"] == record["sanitized_sha256"]:
                return {"created": False, "archive": self._archive_public(existing)}
            archive_id = str(existing["archive_id"]) if existing else _new_id()
            con.execute(
                "INSERT INTO mail_archive "
                "(archive_id,account_id,immutable_message_id,key_id,nonce,ciphertext,"
                "aad_sha256,sanitized_sha256,sanitized_chars,truncated,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,immutable_message_id) "
                "DO UPDATE SET key_id=excluded.key_id,nonce=excluded.nonce,"
                "ciphertext=excluded.ciphertext,aad_sha256=excluded.aad_sha256,"
                "sanitized_sha256=excluded.sanitized_sha256,"
                "sanitized_chars=excluded.sanitized_chars,truncated=excluded.truncated,"
                "updated_at=excluded.updated_at",
                (
                    archive_id, record["account_id"], message_id, record["key_id"],
                    nonce, ciphertext, record["aad_sha256"], record["sanitized_sha256"],
                    chars, int(bool(record["truncated"])), stamp, stamp,
                ),
            )
            saved = con.execute(
                "SELECT * FROM mail_archive WHERE archive_id=?", (archive_id,)
            ).fetchone()
            return {"created": not bool(existing), "archive": self._archive_public(saved)}

        return self._idempotent("put_mail_archive", context, normalized_request, operation)

    @staticmethod
    def _archive_public(row: sqlite3.Row) -> Mapping[str, Any]:
        return {
            name: row[name]
            for name in (
                "archive_id", "account_id", "immutable_message_id", "key_id",
                "aad_sha256", "sanitized_sha256", "sanitized_chars", "truncated",
                "created_at", "updated_at",
            )
        }

    def get_encrypted_mail_archive(self, archive_id: str) -> Mapping[str, Any]:
        validate_identifier(archive_id, "archive_id")
        with connect(self.db_path) as con:
            row = con.execute(
                "SELECT * FROM mail_archive WHERE archive_id=?", (archive_id,)
            ).fetchone()
            if not row:
                raise ContractError("mail archive was not found")
            return _row(row)

    def list_application_mail(self, application_id: str) -> Sequence[Mapping[str, Any]]:
        """Read message references through explicit proposal/event associations."""
        validate_identifier(application_id, "application_id")
        with connect(self.db_path) as con:
            self._application(con, application_id)
            return tuple(_row(row) for row in con.execute(
                "SELECT DISTINCT e.evidence_id,e.sender,e.received_at,e.excerpt,"
                "m.archive_id FROM mail_evidence e "
                "JOIN event_proposals p ON p.evidence_id=e.evidence_id "
                "LEFT JOIN application_events a ON a.event_id=p.applied_event_id "
                "LEFT JOIN mail_archive m ON m.account_id=e.account_id "
                "AND m.immutable_message_id=e.immutable_message_id "
                "WHERE (a.application_id=? OR (p.proposed_application_id=? "
                "AND p.status IN ('pending','conflict'))) "
                "ORDER BY e.received_at DESC,e.evidence_id LIMIT 50",
                (application_id, application_id),
            ))

    def list_mail_archive_index(
        self, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        """List bounded ciphertext records by opaque archive ID only.

        This is the discovery boundary for read-only archive adapters.  Graph message
        IDs, account IDs, key metadata, ciphertext, and plaintext never leave the
        archive implementation.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ContractError("mail archive index limit must be between 1 and 200")
        with connect(self.db_path) as con:
            return tuple(
                _row(row)
                for row in con.execute(
                    "SELECT archive_id,sanitized_chars,truncated,created_at,updated_at "
                    "FROM mail_archive ORDER BY updated_at DESC,archive_id LIMIT ?",
                    (limit,),
                )
            )

    def put_mail_archive_attachment(
        self, record: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        required = {
            "archive_id", "immutable_attachment_id", "mime_type", "source_size",
            "source_sha256", "extracted_sha256", "extracted_chars", "key_id",
            "nonce", "ciphertext", "aad_sha256",
        }
        if not isinstance(record, Mapping) or set(record) != required:
            raise ContractError("attachment archive fields do not match schema")
        for name in ("archive_id", "key_id"):
            if not isinstance(record[name], str):
                raise ContractError(f"{name} must be text")
            validate_identifier(record[name], name)
        attachment_id = record["immutable_attachment_id"]
        if not isinstance(attachment_id, str) or not attachment_id or len(attachment_id) > 2048:
            raise ContractError("immutable attachment id is invalid")
        allowed_mimes = {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/calendar",
        }
        if not isinstance(record["mime_type"], str) or record["mime_type"] not in allowed_mimes:
            raise ContractError("attachment MIME is not allowed")
        source_size = record["source_size"]
        extracted_chars = record["extracted_chars"]
        if (
            isinstance(source_size, bool) or not isinstance(source_size, int)
            or not 0 < source_size <= 5 * 1024 * 1024
            or isinstance(extracted_chars, bool) or not isinstance(extracted_chars, int)
            or not 0 < extracted_chars <= 256_000
        ):
            raise ContractError("attachment archive sizes are invalid")
        for field in ("source_sha256", "extracted_sha256", "aad_sha256"):
            if not isinstance(record[field], str) or not re.fullmatch(
                r"[0-9a-f]{64}", record[field]
            ):
                raise ContractError(f"{field} must be SHA-256")
        nonce = self._archive_binary(record["nonce"], "nonce", nonce=True)
        ciphertext = self._archive_binary(record["ciphertext"], "ciphertext")
        normalized_request = {
            key: record[key] for key in required - {"nonce", "ciphertext"}
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            if not con.execute(
                "SELECT 1 FROM mail_archive WHERE archive_id=?", (record["archive_id"],)
            ).fetchone():
                raise ContractError("attachment mail archive was not found")
            existing = con.execute(
                "SELECT * FROM mail_archive_attachments WHERE archive_id=? "
                "AND immutable_attachment_id=?",
                (record["archive_id"], attachment_id),
            ).fetchone()
            if existing:
                matches = all(
                    existing[name] == record[name]
                    for name in (
                        "mime_type", "source_size", "source_sha256", "extracted_sha256",
                        "extracted_chars", "key_id", "aad_sha256",
                    )
                )
                if not matches:
                    raise ConflictError("immutable attachment archive changed")
                return {"created": False, "attachment": self._attachment_public(existing)}
            record_id = _new_id()
            con.execute(
                "INSERT INTO mail_archive_attachments "
                "(attachment_record_id,archive_id,immutable_attachment_id,mime_type,"
                "source_size,source_sha256,extracted_sha256,extracted_chars,key_id,nonce,"
                "ciphertext,aad_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id, record["archive_id"], attachment_id, record["mime_type"],
                    source_size, record["source_sha256"], record["extracted_sha256"],
                    extracted_chars, record["key_id"], nonce, ciphertext,
                    record["aad_sha256"], stamp,
                ),
            )
            saved = con.execute(
                "SELECT * FROM mail_archive_attachments WHERE attachment_record_id=?",
                (record_id,),
            ).fetchone()
            return {"created": True, "attachment": self._attachment_public(saved)}

        return self._idempotent(
            "put_mail_archive_attachment", context, normalized_request, operation
        )

    @staticmethod
    def _attachment_public(row: sqlite3.Row) -> Mapping[str, Any]:
        return {
            name: row[name]
            for name in (
                "attachment_record_id", "archive_id", "immutable_attachment_id",
                "mime_type", "source_size", "source_sha256", "extracted_sha256",
                "extracted_chars", "key_id", "aad_sha256", "created_at",
            )
        }

    def get_encrypted_mail_archive_attachment(
        self, attachment_record_id: str
    ) -> Mapping[str, Any]:
        validate_identifier(attachment_record_id, "attachment_record_id")
        with connect(self.db_path) as con:
            row = con.execute(
                "SELECT * FROM mail_archive_attachments WHERE attachment_record_id=?",
                (attachment_record_id,),
            ).fetchone()
            if not row:
                raise ContractError("mail archive attachment was not found")
            return _row(row)

    def create_temporal_proposal(
        self, proposal: TemporalProposalInput, context: MutationContext
    ) -> Mapping[str, Any]:
        context.validate()
        for value, name in (
            (proposal.archive_id, "archive_id"),
            (proposal.application_id, "application_id"),
            (proposal.producer_version, "producer_version"),
            (proposal.dedupe_key, "dedupe_key"),
        ):
            validate_identifier(value, name)
        if proposal.attachment_record_id:
            validate_identifier(proposal.attachment_record_id, "attachment_record_id")
        if (
            not math.isfinite(proposal.confidence)
            or not 0 <= proposal.confidence <= 1
            or not re.fullmatch(r"[0-9a-f]{64}", proposal.source_sha256)
            or not proposal.evidence_quote
            or len(proposal.evidence_quote) > 512
            or proposal.span_start < 0
            or proposal.span_end <= proposal.span_start
        ):
            raise ContractError("temporal proposal evidence is invalid")
        try:
            ZoneInfo(proposal.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ContractError("temporal proposal time zone is unknown") from exc
        for field, value in (
            ("starts_at", proposal.starts_at),
            ("ends_at", proposal.ends_at),
            ("due_at", proposal.due_at),
        ):
            if value is not None:
                parsed = parse_utc(value)
                if value != parsed.isoformat(timespec="seconds").replace("+00:00", "Z"):
                    raise ContractError(f"{field} must be second-precision UTC")
        if proposal.kind is TemporalProposalKind.INTERVIEW:
            if not proposal.starts_at or not proposal.ends_at or proposal.due_at is not None:
                raise ContractError("interview proposal requires only start and end")
            if parse_utc(proposal.ends_at) <= parse_utc(proposal.starts_at):
                raise ContractError("interview proposal interval is invalid")
        elif proposal.kind is TemporalProposalKind.DEADLINE:
            if proposal.starts_at is not None or proposal.ends_at is not None or not proposal.due_at:
                raise ContractError("deadline proposal requires only due_at")
            parse_utc(proposal.due_at)
        else:
            raise ContractError("temporal proposal kind is invalid")
        request = {
            "proposal": asdict(proposal),
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
            "source_ref": context.source_ref,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            self._application(con, proposal.application_id)
            archive = con.execute(
                "SELECT * FROM mail_archive WHERE archive_id=?", (proposal.archive_id,)
            ).fetchone()
            if not archive:
                raise ContractError("temporal proposal archive was not found")
            if proposal.attachment_record_id:
                source = con.execute(
                    "SELECT * FROM mail_archive_attachments WHERE attachment_record_id=?",
                    (proposal.attachment_record_id,),
                ).fetchone()
                if not source or source["archive_id"] != proposal.archive_id:
                    raise ContractError("temporal attachment is outside its mail archive")
                expected_sha = str(source["extracted_sha256"])
                source_chars = int(source["extracted_chars"])
            else:
                expected_sha = str(archive["sanitized_sha256"])
                source_chars = int(archive["sanitized_chars"])
            if expected_sha != proposal.source_sha256 or proposal.span_end > source_chars:
                raise ContractError("temporal evidence is outside the authenticated source")
            previous = con.execute(
                "SELECT * FROM temporal_proposals WHERE dedupe_key=?",
                (proposal.dedupe_key,),
            ).fetchone()
            if previous:
                existing_input = {
                    "archive_id": previous["archive_id"],
                    "attachment_record_id": previous["attachment_record_id"],
                    "application_id": previous["application_id"],
                    "kind": previous["kind"],
                    "starts_at": previous["starts_at"],
                    "ends_at": previous["ends_at"],
                    "due_at": previous["due_at"],
                    "time_zone": previous["time_zone"],
                    "confidence": previous["confidence"],
                    "evidence_quote": previous["evidence_quote"],
                    "span_start": previous["span_start"],
                    "span_end": previous["span_end"],
                    "source_sha256": previous["source_sha256"],
                    "producer_version": previous["producer_version"],
                    "dedupe_key": previous["dedupe_key"],
                }
                if payload_sha256(existing_input) != payload_sha256(asdict(proposal)):
                    raise ConflictError("temporal proposal dedupe key conflicts")
                return {"created": False, "proposal": _row(previous)}
            proposal_id = _new_id()
            con.execute(
                "INSERT INTO temporal_proposals "
                "(temporal_proposal_id,dedupe_key,archive_id,attachment_record_id,"
                "application_id,kind,starts_at,ends_at,due_at,time_zone,confidence,"
                "evidence_quote,span_start,span_end,source_sha256,producer_version,status,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (
                    proposal_id, proposal.dedupe_key, proposal.archive_id,
                    proposal.attachment_record_id, proposal.application_id,
                    proposal.kind.value, proposal.starts_at, proposal.ends_at,
                    proposal.due_at, proposal.time_zone, proposal.confidence,
                    proposal.evidence_quote, proposal.span_start, proposal.span_end,
                    proposal.source_sha256, proposal.producer_version, stamp,
                ),
            )
            saved = con.execute(
                "SELECT * FROM temporal_proposals WHERE temporal_proposal_id=?",
                (proposal_id,),
            ).fetchone()
            return {"created": True, "proposal": _row(saved)}

        return self._idempotent("create_temporal_proposal", context, request, operation)

    def decide_temporal_proposal(
        self,
        temporal_proposal_id: str,
        decision: str,
        reason: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        validate_identifier(temporal_proposal_id, "temporal_proposal_id")
        if context.actor_kind != "user":
            raise ContractError("temporal proposal decisions require actor_kind=user")
        if decision not in {"accepted", "rejected"}:
            raise ContractError("temporal proposal decision is invalid")
        request = {
            "temporal_proposal_id": temporal_proposal_id,
            "decision": decision,
            "reason": str(reason)[:1000],
            "actor_kind": context.actor_kind,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            return self._decide_temporal_proposal(con, stamp, temporal_proposal_id, decision, reason, context)

        return self._idempotent(
            "decide_temporal_proposal", context, request, operation
        )

    def _decide_temporal_proposal(self, con, stamp, temporal_proposal_id, decision, reason, context):
        proposal = con.execute(
            "SELECT * FROM temporal_proposals WHERE temporal_proposal_id=?",
            (temporal_proposal_id,),
        ).fetchone()
        if not proposal:
            raise ContractError("temporal proposal was not found")
        if proposal["status"] != "pending":
            raise ConflictError("temporal proposal has already been decided")
        if decision == "accepted" and self._application(con, proposal["application_id"])["current_phase"] == "terminal":
            raise ConflictError("terminal applications cannot accept new schedules or deadlines")
        schedule = None
        reminders = []
        from .mail.understanding_store import available
        understanding = con.execute('SELECT a.mode FROM mail_understanding_projections p JOIN mail_understanding_findings f USING(finding_id) JOIN mail_understanding_analyses a USING(analysis_id) WHERE p.kind=\'temporal_proposal\' AND p.target_id=?',(temporal_proposal_id,)).fetchone() if available(con) else None
        status = decision
        if decision == "accepted" and proposal["kind"] == "interview":
            overlap = con.execute(
                "SELECT 1 FROM accepted_interview_schedules WHERE status='active' "
                "AND starts_at<? AND ends_at>? LIMIT 1",
                (proposal["ends_at"], proposal["starts_at"]),
            ).fetchone()
            if overlap:
                status = "conflict"
            else:
                schedule_id = _new_id()
                payload = {
                    "interview_schedule_id": schedule_id,
                    "starts_at": proposal["starts_at"],
                    "ends_at": proposal["ends_at"],
                    "time_zone": proposal["time_zone"],
                    "temporal_proposal_id": temporal_proposal_id,
                }
                event_context = MutationContext(
                    context.idempotency_key,
                    context.actor_kind,
                    context.source_kind,
                    temporal_proposal_id,
                )
                event, created = self._append_event(
                    con,
                    str(proposal["application_id"]),
                    ApplicationEventType.INTERVIEW_SCHEDULED,
                    stamp,
                    payload,
                    "temporal-schedule:" + temporal_proposal_id,
                    event_context,
                    stamp,
                )
                if created:
                    self._project(con, str(proposal["application_id"]))
                con.execute(
                    "INSERT INTO accepted_interview_schedules "
                    "(interview_schedule_id,temporal_proposal_id,application_id,"
                    "starts_at,ends_at,time_zone,application_event_id,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,'active',?)",
                    (
                        schedule_id, temporal_proposal_id, proposal["application_id"],
                        proposal["starts_at"], proposal["ends_at"],
                        proposal["time_zone"], event["event_id"], stamp,
                    ),
                )
                schedule = _row(con.execute(
                    "SELECT * FROM accepted_interview_schedules "
                    "WHERE interview_schedule_id=?", (schedule_id,)
                ).fetchone())
                start_time = parse_utc(str(proposal["starts_at"]))
                current = parse_utc(stamp)
                for kind, delta in (
                    ("interview_24h", timedelta(hours=24)),
                    ("interview_1h", timedelta(hours=1)),
                ):
                    if understanding and understanding['mode']=='replay':
                        continue
                    due = max(current, start_time - delta).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z")
                    reminders.append(self._insert_local_reminder(
                        con, proposal, kind, due, stamp, schedule_id
                    ))
        elif decision == "accepted":
            from .lifecycle.core import ensure_deadline_task
            ensure_deadline_task(con, self, proposal, context, stamp)
            if not understanding:
                reminders.append(self._insert_local_reminder(
                    con, proposal, "deadline", str(proposal["due_at"]), stamp, None,
                ))
        con.execute(
            "UPDATE temporal_proposals SET status=?,decided_at=? "
            "WHERE temporal_proposal_id=?",
            (status, stamp, temporal_proposal_id),
        )
        con.execute(
            "INSERT INTO temporal_proposal_decisions "
            "(decision_id,temporal_proposal_id,decision,actor_kind,reason,decided_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                _new_id(), temporal_proposal_id, decision, context.actor_kind,
                str(reason).strip()[:1000], stamp,
            ),
        )
        return {
            "temporal_proposal_id": temporal_proposal_id,
            "decision": decision,
            "status": status,
            "schedule": schedule,
            "reminders": reminders,
        }


    @staticmethod
    def _insert_local_reminder(
        con: sqlite3.Connection,
        proposal: sqlite3.Row,
        kind: str,
        due_at: str,
        stamp: str,
        schedule_id: Optional[str],
    ) -> Mapping[str, Any]:
        due = parse_utc(due_at)
        reminder_id = _new_id()
        # A reviewed historical deadline is useful application evidence, but its
        # overdue alert was never scheduled prospectively. Keep the reminder for
        # audit without enqueueing a notification when the old mail is accepted.
        historical = kind == "deadline" and due < parse_utc(stamp)
        con.execute(
            "INSERT INTO local_reminders "
            "(reminder_id,temporal_proposal_id,interview_schedule_id,application_id,"
            "kind,due_at,status,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                reminder_id, proposal["temporal_proposal_id"], schedule_id,
                proposal["application_id"], kind, due_at,
                "dismissed" if historical else "pending", stamp,
                stamp if historical else None,
            ),
        )
        return _row(con.execute(
            "SELECT * FROM local_reminders WHERE reminder_id=?", (reminder_id,)
        ).fetchone())

    def list_temporal_proposals(
        self, statuses: Optional[Sequence[str]] = None, *, limit: int = 200
    ) -> Sequence[Mapping[str, Any]]:
        allowed = {"pending", "accepted", "rejected", "conflict"}
        selected = tuple(statuses or sorted(allowed))
        if not 1 <= limit <= 1000 or not selected or any(item not in allowed for item in selected):
            raise ContractError("temporal proposal query is invalid")
        marks = ",".join("?" for _ in selected)
        with connect(self.db_path) as con:
            return tuple(_row(row) for row in con.execute(
                f"SELECT * FROM temporal_proposals WHERE status IN ({marks}) "
                "ORDER BY created_at,temporal_proposal_id LIMIT ?",
                (*selected, limit),
            ))

    def list_interview_schedules(
        self, *, limit: int = 200, application_id: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None, starts_after: Optional[str] = None,
        starts_before: Optional[str] = None, offset: int = 0,
    ) -> Sequence[Mapping[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ContractError("interview schedule limit is invalid")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ContractError("interview schedule offset is invalid")
        clauses, args = [], []
        if application_id:
            validate_identifier(application_id, "application_id")
            clauses.append("application_id=?")
            args.append(application_id)
        normalized = tuple(dict.fromkeys(statuses or ()))
        if any(value not in {"active", "cancelled", "completed"} for value in normalized):
            raise ContractError("invalid interview schedule status")
        if normalized:
            clauses.append("status IN (" + ",".join("?" for _ in normalized) + ")")
            args.extend(normalized)
        for column, value in (("starts_at>=?", starts_after), ("starts_at<=?", starts_before)):
            if value:
                parse_utc(value)
                clauses.append(column)
                args.append(value)
        with connect(self.db_path) as con:
            return tuple(_row(row) for row in con.execute(
                "SELECT * FROM accepted_interview_schedules "
                + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
                + "ORDER BY starts_at,interview_schedule_id LIMIT ? OFFSET ?", (*args, limit, offset)
            ))

    def list_due_local_reminders(
        self, now: str, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        parse_utc(now)
        if not 1 <= limit <= 500:
            raise ContractError("local reminder limit is invalid")
        with connect(self.db_path) as con:
            return tuple(_row(row) for row in con.execute(
                "SELECT * FROM local_reminders WHERE status='pending' AND due_at<=? "
                "ORDER BY due_at,reminder_id LIMIT ?", (now, limit)
            ))

    def complete_local_reminder(
        self,
        reminder_id: str,
        resolution: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        validate_identifier(reminder_id, "reminder_id")
        if resolution not in {"completed", "dismissed"}:
            raise ContractError("local reminder resolution is invalid")
        request = {"reminder_id": reminder_id, "resolution": resolution}

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            reminder = con.execute(
                "SELECT * FROM local_reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
            if not reminder:
                raise ContractError("local reminder was not found")
            if reminder["status"] != "pending":
                raise ConflictError("local reminder has already been resolved")
            con.execute(
                "UPDATE local_reminders SET status=?,completed_at=? WHERE reminder_id=?",
                (resolution, stamp, reminder_id),
            )
            return _row(con.execute(
                "SELECT * FROM local_reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone())

        return self._idempotent("complete_local_reminder", context, request, operation)

    @staticmethod
    def _terminal_outcome(event_type: ApplicationEventType) -> Optional[str]:
        return {
            ApplicationEventType.OFFER_ACCEPTED: TerminalOutcome.ACCEPTED.value,
            ApplicationEventType.REJECTION_RECEIVED: TerminalOutcome.REJECTED.value,
            ApplicationEventType.WITHDRAWN: TerminalOutcome.WITHDRAWN.value,
        }.get(event_type)

    @staticmethod
    def _insert_application_notification_outbox(
        con: sqlite3.Connection,
        application_id: str,
        event_type: ApplicationEventType,
        event_id: str,
        stamp: str,
    ) -> None:
        payload = {
            "application_id": application_id,
            "event_type": event_type.value,
            "source_event_id": event_id,
        }
        con.execute(
            "INSERT OR IGNORE INTO outbox_messages "
            "(outbox_id,topic,source_event_id,dedupe_key,payload_json,status,attempts,"
            "available_at,last_error,created_at) VALUES (?,?,?,?,?,'pending',0,?,'',?)",
            (
                _new_id(),
                "notification.application_event",
                event_id,
                "notification.application_event:" + event_id,
                canonical_json(payload),
                stamp,
                stamp,
            ),
        )

    @staticmethod
    def _insert_feedback_outbox(
        con: sqlite3.Connection,
        application: Mapping[str, Any],
        event: Mapping[str, Any],
        stamp: str,
    ) -> None:
        event_id = str(event["event_id"])
        payload = {
            "source_event_id": event_id,
            "application_id": application["application_id"],
            "ats": application["ats"],
            "job_id": application["job_id"],
            "family_id": application["family_id"],
            "action": "applied",
            "model_run_id": application["recommendation_model_run_id"],
            "policy_id": application["recommendation_policy_id"],
            "session_id": application["recommendation_session_id"],
            "impression_id": application["recommendation_impression_id"],
            "recommendation_rank": application["recommendation_rank"],
            "semantic_score": application["semantic_score"],
            "ranking_score": application["ranking_score"],
            "title_snapshot": application["title_snapshot"],
        }
        con.execute(
            "INSERT OR IGNORE INTO outbox_messages "
            "(outbox_id,topic,source_event_id,dedupe_key,payload_json,status,attempts,"
            "available_at,last_error,created_at) VALUES (?,?,?,?,?,'pending',0,?,'',?)",
            (
                _new_id(),
                "recommendation.applied",
                event_id,
                "recommendation.applied:" + event_id,
                canonical_json(payload),
                stamp,
                stamp,
            ),
        )

    def record_event(self, event: EventInput) -> Mapping[str, Any]:
        validate_identifier(event.application_id, "application_id")
        validate_identifier(event.dedupe_key, "dedupe_key")
        event.context.validate()
        parse_utc(event.occurred_at)
        validate_event_payload(event.event_type, event.payload)
        if event.event_type is ApplicationEventType.APPLICATION_STARTED:
            raise ContractError("application_started is created by start_application")
        if (
            event.event_type is ApplicationEventType.MANUAL_CORRECTION
            and event.context.actor_kind != "user"
        ):
            raise ContractError("manual correction is restricted to actor_kind=user")
        if event.event_type in TERMINAL_EVENT_TYPES and event.context.actor_kind != "user":
            raise ContractError("terminal events require a user review decision")
        request = {
            "application_id": event.application_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "payload": event.payload,
            "dedupe_key": event.dedupe_key,
            "actor_kind": event.context.actor_kind,
            "source_kind": event.context.source_kind,
            "source_ref": event.context.source_ref,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            application = self._application(con, event.application_id)
            incoming_outcome = self._terminal_outcome(event.event_type)
            if (
                application["current_phase"] == ApplicationPhase.TERMINAL.value
                and incoming_outcome
                and application["terminal_outcome"] != incoming_outcome
            ):
                raise ConflictError("terminal outcome conflicts with application state")
            stored_event, created = self._append_event(
                con,
                event.application_id,
                event.event_type,
                event.occurred_at,
                event.payload,
                event.dedupe_key,
                event.context,
                stamp,
            )
            if created:
                self._project(con, event.application_id)
                application = self._application(con, event.application_id)
                if event.event_type is ApplicationEventType.SUBMISSION_OBSERVED:
                    self._insert_feedback_outbox(con, application, stored_event, stamp)
            return {
                "created": created,
                "event": stored_event,
                "application": self._application(con, event.application_id),
            }

        return self._idempotent("record_event", event.context, request, operation)

    def record_submission(
        self,
        application_id: str,
        occurred_at: str,
        context: MutationContext,
        dedupe_key: str,
        payload_factory: Callable[[], Mapping[str, Any]],
        request_payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Record submission while resolving cross-store metadata under one app lock."""

        validate_identifier(application_id, "application_id")
        validate_identifier(dedupe_key, "dedupe_key")
        context.validate()
        parse_utc(occurred_at)
        canonical_json(request_payload)
        request = {
            "application_id": application_id,
            "occurred_at": occurred_at,
            "dedupe_key": dedupe_key,
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
            "source_ref": context.source_ref,
            "payload_request": request_payload,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            self._application(con, application_id)
            payload = payload_factory()
            if not isinstance(payload, Mapping):
                raise ContractError("submission payload factory returned invalid data")
            frozen_payload = json.loads(canonical_json(payload))
            validate_event_payload(
                ApplicationEventType.SUBMISSION_OBSERVED, frozen_payload
            )
            stored_event, created = self._append_event(
                con,
                application_id,
                ApplicationEventType.SUBMISSION_OBSERVED,
                occurred_at,
                frozen_payload,
                dedupe_key,
                context,
                stamp,
            )
            if created:
                self._project(con, application_id)
                application = self._application(con, application_id)
                self._insert_feedback_outbox(con, application, stored_event, stamp)
            return {
                "created": created,
                "event": stored_event,
                "application": self._application(con, application_id),
            }

        return self._idempotent(
            "record_submission", context, request, operation
        )

    def create_event_proposal(
        self, proposal: EventProposalInput, context: MutationContext, *,
        _transaction: Optional[Tuple[sqlite3.Connection, str]] = None,
    ) -> Mapping[str, Any]:
        context.validate()
        validate_identifier(proposal.evidence_id, "evidence_id")
        validate_identifier(proposal.dedupe_key, "dedupe_key")
        validate_identifier(proposal.producer_version, "producer_version")
        if not math.isfinite(proposal.confidence) or not 0 <= proposal.confidence <= 1:
            raise ContractError("confidence must be between zero and one")
        candidates = bounded_candidate_ids(proposal.candidate_application_ids)
        if proposal.proposed_application_id:
            validate_identifier(proposal.proposed_application_id, "application_id")
            if candidates and proposal.proposed_application_id not in candidates:
                raise ContractError("proposed application must be among the candidates")
        if proposal.event_type in {
            ApplicationEventType.APPLICATION_STARTED,
            ApplicationEventType.MANUAL_CORRECTION,
        }:
            raise ContractError("producer cannot propose this event type")
        if proposal.span_start is None or proposal.span_end is None:
            raise ContractError("event proposals require an exact evidence span")
        if (
            proposal.span_start < 0
            or proposal.span_end <= proposal.span_start
            or not isinstance(proposal.evidence_quote, str)
            or not proposal.evidence_quote
            or len(proposal.evidence_quote) > 512
        ):
            raise ContractError("evidence span is invalid")
        validate_event_payload(proposal.event_type, proposal.payload)
        request = {
            "proposal": asdict(proposal),
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
            "source_ref": context.source_ref,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            evidence = con.execute(
                "SELECT excerpt FROM mail_evidence WHERE evidence_id=?",
                (proposal.evidence_id,),
            ).fetchone()
            if not evidence:
                raise ContractError("event proposal evidence was not found")
            excerpt = str(evidence["excerpt"])
            if (
                proposal.span_end > len(excerpt)
                or excerpt[proposal.span_start : proposal.span_end]
                != proposal.evidence_quote
            ):
                raise ContractError(
                    "evidence quote and span do not match retained mail evidence"
                )
            if proposal.proposed_application_id:
                self._application(con, proposal.proposed_application_id)
            for candidate in candidates:
                self._application(con, candidate)
            previous = con.execute(
                "SELECT * FROM event_proposals WHERE dedupe_key=?",
                (proposal.dedupe_key,),
            ).fetchone()
            if previous:
                existing_request = {
                    "evidence_id": previous["evidence_id"],
                    "proposed_application_id": previous["proposed_application_id"],
                    "event_type": previous["event_type"],
                    "producer_kind": previous["producer_kind"],
                    "producer_version": previous["producer_version"],
                    "confidence": previous["confidence"],
                    "candidate_application_ids": json.loads(
                        previous["candidate_application_ids_json"]
                    ),
                    "evidence_quote": previous["evidence_quote"],
                    "span_start": previous["span_start"],
                    "span_end": previous["span_end"],
                    "payload": json.loads(previous["payload_json"]),
                    "dedupe_key": previous["dedupe_key"],
                }
                if payload_sha256(existing_request) != payload_sha256(asdict(proposal)):
                    raise ConflictError("proposal dedupe key conflicts with existing input")
                return {"created": False, "proposal": _row(previous)}
            proposal_id = _new_id()
            con.execute(
                "INSERT INTO event_proposals "
                "(proposal_id,dedupe_key,evidence_id,proposed_application_id,event_type,"
                "producer_kind,producer_version,confidence,candidate_application_ids_json,"
                "evidence_quote,span_start,span_end,payload_json,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (
                    proposal_id,
                    proposal.dedupe_key,
                    proposal.evidence_id,
                    proposal.proposed_application_id,
                    proposal.event_type.value,
                    proposal.producer_kind.value,
                    proposal.producer_version,
                    proposal.confidence,
                    canonical_json(candidates),
                    proposal.evidence_quote,
                    proposal.span_start,
                    proposal.span_end,
                    canonical_json(proposal.payload),
                    stamp,
                ),
            )
            saved = con.execute(
                "SELECT * FROM event_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            return {"created": True, "proposal": _row(saved)}

        if _transaction is not None:
            return operation(*_transaction)
        return self._idempotent("create_event_proposal", context, request, operation)

    def decide_event_proposal(
        self,
        proposal_id: str,
        decision: str,
        selected_application_id: Optional[str],
        reason: str,
        context: MutationContext,
        *,
        review_mail_content: Optional[Mapping[str, Any]] = None,
        review_job_snapshot: Optional[JobSnapshot] = None,
    ) -> Mapping[str, Any]:
        validate_identifier(proposal_id, "proposal_id")
        if context.actor_kind != "user":
            raise ContractError("event proposal decisions require actor_kind=user")
        if decision not in {"accepted", "rejected"}:
            raise ContractError("proposal decision must be accepted or rejected")
        if selected_application_id:
            validate_identifier(selected_application_id, "application_id")
        if review_job_snapshot is not None:
            review_job_snapshot.validate()
            if selected_application_id or decision != 'accepted':
                raise ContractError('catalog selection requires acceptance without an application selection')
        request = {
            "proposal_id": proposal_id,
            "decision": decision,
            "selected_application_id": selected_application_id,
            "reason": reason,
            "actor_kind": context.actor_kind,
        }
        if review_job_snapshot is not None:
            request['selected_job'] = {'ats': review_job_snapshot.ats, 'id': review_job_snapshot.job_id}

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            return self._decide_event_proposal(con, stamp, proposal_id, decision, selected_application_id, reason, context,
                review_mail_content=review_mail_content, review_job_snapshot=review_job_snapshot)

        return self._idempotent("decide_event_proposal", context, request, operation)

    def _decide_event_proposal(self, con, stamp, proposal_id, decision, selected_application_id, reason, context, *,
                               create_tasks=True, review_mail_content=None, review_job_snapshot=None):
        proposal = con.execute(
            "SELECT * FROM event_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if not proposal:
            raise ContractError("event proposal not found")
        if proposal["status"] != "pending":
            raise ConflictError("event proposal has already been decided")
        application_id = selected_application_id or proposal["proposed_application_id"]
        event_result = None
        if decision == "accepted":
            if review_job_snapshot is not None:
                from .mail.context import CandidateApplication
                from .mail.identity import review_supported_candidates
                content = review_mail_content or {}
                candidate = CandidateApplication(application_id='catalog-review',
                    ats=review_job_snapshot.ats, job_id=review_job_snapshot.job_id,
                    employer=review_job_snapshot.employer, title=review_job_snapshot.title,
                    company_slug=review_job_snapshot.company_slug)
                if not review_supported_candidates([candidate], content.get('subject', ''), content.get('body', '')):
                    raise ContractError('selected job has no supporting mail identity')
                application_id = self._start_application(con, review_job_snapshot,
                    RecommendationProvenance(), context, stamp)['application']['application_id']
            if not application_id:
                raise ContractError("acceptance requires an application selection")
            candidates = json.loads(proposal["candidate_application_ids_json"])
            if not proposal["proposed_application_id"] and not ('understanding_finding_id' in proposal.keys() and proposal['understanding_finding_id']):
                # The browser may have delivered the application after this
                # email was processed. Resolve review choices from current
                # local evidence, without another provider call or auto-link.
                candidates = self._unassigned_mail_candidates(proposal["evidence_id"], review_mail_content=review_mail_content)
            if review_job_snapshot is None and candidates and application_id not in candidates:
                raise ContractError("selected application is not a proposal candidate")
            if review_job_snapshot is None and not proposal["proposed_application_id"] and application_id not in candidates:
                raise ContractError("selected application has no supporting mail identity")
            application = self._application(con, application_id)
            event_type = ApplicationEventType(proposal["event_type"])
            incoming_outcome = self._terminal_outcome(event_type)
            if (
                application["current_phase"] == ApplicationPhase.TERMINAL.value
                and incoming_outcome
                and application["terminal_outcome"] != incoming_outcome
            ):
                con.execute(
                    "UPDATE event_proposals SET status='conflict',decided_at=? "
                    "WHERE proposal_id=?",
                    (stamp, proposal_id),
                )
                return {"decision": "conflict", "proposal_id": proposal_id}
            event_context = MutationContext(
                idempotency_key=context.idempotency_key,
                actor_kind=context.actor_kind,
                source_kind=context.source_kind,
                source_ref=proposal_id,
            )
            payload = json.loads(proposal["payload_json"])
            evidence = con.execute(
                "SELECT received_at FROM mail_evidence WHERE evidence_id=?",
                (proposal["evidence_id"],),
            ).fetchone()
            occurred_at = payload.get("occurred_at") or (evidence["received_at"] if evidence else stamp)
            parse_utc(occurred_at)
            saved_event, created = self._append_event(
                con,
                application_id,
                event_type,
                occurred_at,
                payload,
                "event-proposal:" + proposal_id,
                event_context,
                stamp,
            )
            if created:
                self._project(con, application_id)
                if event_type is ApplicationEventType.SUBMISSION_OBSERVED:
                    self._insert_feedback_outbox(
                        con, self._application(con, application_id), saved_event, stamp
                    )
            event_result = saved_event
            con.execute(
                "UPDATE event_proposals SET status='accepted',applied_event_id=?,"
                "decided_at=? WHERE proposal_id=?",
                (saved_event["event_id"], stamp, proposal_id),
            )
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_mail_links'").fetchone():
                from .lifecycle.mail import link_accepted_evidence
                link_accepted_evidence(con, proposal["evidence_id"], application_id, context, stamp)
            from .lifecycle.core import ensure_event_task
            if create_tasks:
                ensure_event_task(con, self, saved_event, proposal["evidence_id"], stamp)
        else:
            con.execute(
                "UPDATE event_proposals SET status='rejected',decided_at=? "
                "WHERE proposal_id=?",
                (stamp, proposal_id),
            )
        con.execute(
            "INSERT INTO event_proposal_decisions "
            "(decision_id,proposal_id,decision,selected_application_id,actor_kind,"
            "reason,decided_at) VALUES (?,?,?,?,?,?,?)",
            (
                _new_id(),
                proposal_id,
                decision,
                application_id,
                context.actor_kind,
                reason.strip()[:1000],
                stamp,
            ),
        )
        return {
            "decision": decision,
            "proposal_id": proposal_id,
            "event": event_result,
        }


    def auto_apply_event_proposal(
        self,
        proposal_id: str,
        context: MutationContext,
        automation_policy_id: str = "",
    ) -> Mapping[str, Any]:
        validate_identifier(proposal_id, "proposal_id")
        if context.actor_kind != "system":
            raise ContractError("automatic proposal application requires actor_kind=system")
        request = {
            "proposal_id": proposal_id,
            "automation_policy_id": automation_policy_id,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            return self._auto_apply_event_proposal(con, stamp, proposal_id, context, automation_policy_id)

        return self._idempotent(
            "auto_apply_event_proposal", context, request, operation
        )

    def _auto_apply_event_proposal(self, con, stamp, proposal_id, context, automation_policy_id):
        proposal = con.execute(
            "SELECT p.*,e.received_at FROM event_proposals p "
            "LEFT JOIN mail_evidence e ON e.evidence_id=p.evidence_id "
            "WHERE p.proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if not proposal:
            raise ContractError("event proposal not found")
        if proposal["status"] == "auto_applied":
            return {
                "proposal_id": proposal_id,
                "event": self._event(con, str(proposal["applied_event_id"])),
                "application": self._application(
                    con, str(proposal["proposed_application_id"])
                ),
                "created": False,
            }
        if proposal["status"] != "pending":
            raise ConflictError("event proposal is not pending")
        application_id = proposal["proposed_application_id"]
        if not application_id:
            raise ContractError("ambiguous proposals cannot be auto-applied")
        event_type = ApplicationEventType(str(proposal["event_type"]))
        if event_type not in MODEL_AUTO_APPLY_EVENT_TYPES:
            raise ContractError("event type is not eligible for automatic application")
        producer_kind = str(proposal["producer_kind"])
        if producer_kind == "rule":
            if float(proposal["confidence"]) != 1.0:
                raise ContractError("deterministic rule confidence must be exact")
        elif producer_kind == "model":
            if not automation_policy_id:
                raise ContractError("model proposal requires an automation policy")
            policy = con.execute(
                "SELECT * FROM classifier_automation_policies WHERE policy_id=?",
                (automation_policy_id,),
            ).fetchone()
            if not policy or not int(policy["enabled"]):
                raise ContractError("model automation policy is not enabled")
            if (
                str(policy["event_type"]) != event_type.value
                or str(policy["producer_version"]) != str(proposal["producer_version"])
                or int(policy["example_count"]) < 50
                or float(policy["observed_precision"]) < 0.99
                or int(policy["wrong_application_matches"]) != 0
                or float(proposal["confidence"]) < float(policy["threshold"])
            ):
                raise ContractError("model proposal does not satisfy its safety gate")
        else:
            raise ContractError("unknown proposal producer")
        event, created = self._append_event(
            con,
            str(application_id),
            event_type,
            str(proposal["received_at"] or stamp),
            json.loads(str(proposal["payload_json"])),
            "proposal:" + proposal_id,
            context,
            stamp,
        )
        if created:
            self._project(con, str(application_id))
        con.execute(
            "UPDATE event_proposals SET status='auto_applied',applied_event_id=?,"
            "automation_policy_id=?,decided_at=? WHERE proposal_id=?",
            (event["event_id"], automation_policy_id, stamp, proposal_id),
        )
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_mail_links'").fetchone():
            from .lifecycle.mail import link_accepted_evidence
            link_accepted_evidence(con, proposal["evidence_id"], application_id, context, stamp)
        from .lifecycle.core import ensure_event_task
        ensure_event_task(con, self, event, proposal["evidence_id"], stamp)
        # A newer rule can resolve an older pending match for the same evidence.
        # Keep the old proposal as audit history without leaving duplicate review work.
        con.execute(
            "UPDATE event_proposals SET status='superseded',decided_at=? "
            "WHERE evidence_id=? AND proposed_application_id=? AND event_type=? "
            "AND status='pending' AND proposal_id<>?",
            (stamp, proposal["evidence_id"], application_id, event_type.value, proposal_id),
        )
        return {
            "proposal_id": proposal_id,
            "event": event,
            "application": self._application(con, str(application_id)),
            "created": created,
        }


    def create_action_proposal(
        self, proposal: ActionProposalInput, context: MutationContext
    ) -> Mapping[str, Any]:
        context.validate()
        validate_identifier(proposal.account_id, "account_id")
        expires = parse_utc(proposal.expires_at)
        if proposal.application_id:
            validate_identifier(proposal.application_id, "application_id")
        encoded = canonical_json(proposal.payload)
        request = {
            "proposal": asdict(proposal),
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
            "source_ref": context.source_ref,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            if expires <= parse_utc(stamp):
                raise ContractError("action proposal expiry must be in the future")
            if proposal.application_id:
                self._application(con, proposal.application_id)
            action_id = _new_id()
            con.execute(
                "INSERT INTO action_proposals "
                "(action_id,application_id,account_id,kind,payload_json,payload_sha256,"
                "remote_idempotency_key,status,expires_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,'pending',?,?)",
                (
                    action_id,
                    proposal.application_id,
                    proposal.account_id,
                    proposal.kind.value,
                    encoded,
                    payload_sha256(proposal.payload),
                    _new_id(),
                    proposal.expires_at,
                    stamp,
                ),
            )
            saved = con.execute(
                "SELECT * FROM action_proposals WHERE action_id=?", (action_id,)
            ).fetchone()
            return {"created": True, "action": _row(saved)}

        return self._idempotent("create_action_proposal", context, request, operation)

    def decide_action(
        self,
        action_id: str,
        approve: bool,
        exact_payload_sha256: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        validate_identifier(action_id, "action_id")
        if context.actor_kind != "user":
            raise ContractError("action decisions require actor_kind=user")
        if not re.fullmatch(r"[0-9a-f]{64}", exact_payload_sha256):
            raise ContractError("payload SHA-256 is invalid")
        request = {
            "action_id": action_id,
            "approve": approve,
            "payload_sha256": exact_payload_sha256,
            "actor_kind": context.actor_kind,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            action = con.execute(
                "SELECT * FROM action_proposals WHERE action_id=?", (action_id,)
            ).fetchone()
            if not action:
                raise ContractError("action proposal not found")
            if action["status"] != ActionStatus.PENDING.value:
                raise ConflictError("action proposal has already been decided")
            if action["payload_sha256"] != exact_payload_sha256:
                raise ConflictError("approval payload hash does not match proposal")
            now = parse_utc(stamp)
            if approve and parse_utc(action["expires_at"]) <= now:
                raise ConflictError("action proposal has expired")
            decision_id = _new_id()
            approval_expiry = None
            if approve:
                approval_expiry_dt = min(
                    now + timedelta(seconds=ACTION_APPROVAL_TTL_SECONDS),
                    parse_utc(action["expires_at"]),
                )
                approval_expiry = approval_expiry_dt.isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z")
            decision = "approve" if approve else "reject"
            con.execute(
                "INSERT INTO action_approval_decisions "
                "(decision_id,action_id,decision,payload_sha256,actor_kind,created_at,"
                "expires_at) VALUES (?,?,?,?,?,?,?)",
                (
                    decision_id,
                    action_id,
                    decision,
                    exact_payload_sha256,
                    context.actor_kind,
                    stamp,
                    approval_expiry,
                ),
            )
            status = ActionStatus.APPROVED if approve else ActionStatus.REJECTED
            con.execute(
                "UPDATE action_proposals SET status=? WHERE action_id=?",
                (status.value, action_id),
            )
            return {
                "action_id": action_id,
                "decision_id": decision_id,
                "decision": decision,
                "payload_sha256": exact_payload_sha256,
                "expires_at": approval_expiry,
            }

        return self._idempotent("decide_action", context, request, operation)

    def get_action(self, action_id: str) -> Mapping[str, Any]:
        """Return an action with its immutable payload and execution history."""

        validate_identifier(action_id, "action_id")
        with connect(self.db_path) as con:
            saved = con.execute(
                "SELECT * FROM action_proposals WHERE action_id=?", (action_id,)
            ).fetchone()
            if not saved:
                raise ContractError("action proposal not found")
            action = _row(saved)
            action["payload"] = json.loads(action.pop("payload_json"))
            action["approvals"] = [
                _row(row)
                for row in con.execute(
                    "SELECT * FROM action_approval_decisions WHERE action_id=? "
                    "ORDER BY decision_seq",
                    (action_id,),
                )
            ]
            action["executions"] = [
                _row(row)
                for row in con.execute(
                    "SELECT * FROM action_executions WHERE action_id=? ORDER BY attempt",
                    (action_id,),
                )
            ]
            return action

    def normalize_action_eligibility(self, action_id: str) -> Mapping[str, Any]:
        """Fail an approved action whose approval/expiry/retry budget is exhausted."""

        validate_identifier(action_id, "action_id")
        stamp = utc_now()
        now = parse_utc(stamp)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            action = con.execute(
                "SELECT * FROM action_proposals WHERE action_id=?", (action_id,)
            ).fetchone()
            if not action:
                raise ContractError("action proposal not found")
            if action["status"] != ActionStatus.APPROVED.value:
                return {"action_id": action_id, "eligible": False, "status": action["status"]}
            approval = con.execute(
                "SELECT * FROM action_approval_decisions WHERE action_id=? "
                "ORDER BY decision_seq DESC LIMIT 1",
                (action_id,),
            ).fetchone()
            attempts = int(
                con.execute(
                    "SELECT COUNT(*) FROM action_executions WHERE action_id=?",
                    (action_id,),
                ).fetchone()[0]
            )
            expired = bool(
                not approval
                or approval["decision"] != "approve"
                or not approval["expires_at"]
                or parse_utc(str(approval["expires_at"])) <= now
                or parse_utc(str(action["expires_at"])) <= now
                or attempts >= ACTION_EXECUTION_MAX_ATTEMPTS
            )
            if expired:
                con.execute(
                    "UPDATE action_proposals SET status=? WHERE action_id=?",
                    (ActionStatus.FAILED.value, action_id),
                )
                return {"action_id": action_id, "eligible": False, "status": "failed"}
            return {"action_id": action_id, "eligible": True, "status": "approved"}

    def claim_action(self, action_id: str) -> Mapping[str, Any]:
        """Atomically claim one approved, unexpired exact action for execution."""

        validate_identifier(action_id, "action_id")
        stamp = utc_now()
        now = parse_utc(stamp)
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            saved = con.execute(
                "SELECT * FROM action_proposals WHERE action_id=?", (action_id,)
            ).fetchone()
            if not saved:
                raise ContractError("action proposal not found")
            if saved["status"] != ActionStatus.APPROVED.value:
                raise ConflictError("action is not approved for execution")
            approval = con.execute(
                "SELECT * FROM action_approval_decisions WHERE action_id=? "
                "ORDER BY decision_seq DESC LIMIT 1",
                (action_id,),
            ).fetchone()
            if not approval or approval["decision"] != "approve":
                raise ConflictError("action has no current approval")
            if not approval["expires_at"] or parse_utc(approval["expires_at"]) <= now:
                con.execute(
                    "UPDATE action_proposals SET status=? WHERE action_id=?",
                    (ActionStatus.FAILED.value, action_id),
                )
                con.commit()
                raise ConflictError("action approval has expired")
            if parse_utc(saved["expires_at"]) <= now:
                con.execute(
                    "UPDATE action_proposals SET status=? WHERE action_id=?",
                    (ActionStatus.FAILED.value, action_id),
                )
                con.commit()
                raise ConflictError("action proposal has expired")
            payload = json.loads(saved["payload_json"])
            expected_hash = payload_sha256(payload)
            if (
                saved["payload_sha256"] != expected_hash
                or approval["payload_sha256"] != expected_hash
            ):
                raise ConflictError("approved action payload no longer matches")
            previous = con.execute(
                "SELECT remote_id FROM action_executions WHERE action_id=? "
                "AND remote_id IS NOT NULL ORDER BY attempt DESC LIMIT 1",
                (action_id,),
            ).fetchone()
            attempt = int(
                con.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM action_executions "
                    "WHERE action_id=?",
                    (action_id,),
                ).fetchone()[0]
            )
            if attempt > ACTION_EXECUTION_MAX_ATTEMPTS:
                con.execute(
                    "UPDATE action_proposals SET status=? WHERE action_id=?",
                    (ActionStatus.FAILED.value, action_id),
                )
                con.commit()
                raise ConflictError("action execution attempt limit was reached")
            execution_id = _new_id()
            con.execute(
                "INSERT INTO action_executions "
                "(execution_id,action_id,approval_decision_id,attempt,status,started_at) "
                "VALUES (?,?,?,?, 'claimed', ?)",
                (
                    execution_id,
                    action_id,
                    approval["decision_id"],
                    attempt,
                    stamp,
                ),
            )
            con.execute(
                "UPDATE action_proposals SET status=? WHERE action_id=?",
                (ActionStatus.EXECUTING.value, action_id),
            )
            action = _row(saved)
            action["payload"] = payload
            action.pop("payload_json")
            action["status"] = ActionStatus.EXECUTING.value
            result = {
                "action": action,
                "execution": _row(
                    con.execute(
                        "SELECT * FROM action_executions WHERE execution_id=?",
                        (execution_id,),
                    ).fetchone()
                ),
                "prior_remote_id": previous["remote_id"] if previous else None,
            }
            con.commit()
            return result
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def checkpoint_action_remote_id(
        self, execution_id: str, remote_id: str
    ) -> Mapping[str, Any]:
        """Persist a returned remote id before the next external saga step."""

        validate_identifier(execution_id, "execution_id")
        if not isinstance(remote_id, str) or not remote_id or len(remote_id) > 2048:
            raise ContractError("remote_id is required")
        with connect(self.db_path) as con:
            changed = con.execute(
                "UPDATE action_executions SET remote_id=? "
                "WHERE execution_id=? AND status='claimed'",
                (remote_id, execution_id),
            ).rowcount
            if changed != 1:
                raise ConflictError("action execution is not claimable")
            saved = con.execute(
                "SELECT * FROM action_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            return _row(saved)

    def complete_action(
        self,
        execution_id: str,
        outcome: str,
        *,
        remote_id: str = "",
        error: str = "",
    ) -> Mapping[str, Any]:
        """Complete a claimed action and project its durable proposal status."""

        validate_identifier(execution_id, "execution_id")
        if outcome not in {
            "succeeded", "retryable_failure", "permanent_failure", "uncertain"
        }:
            raise ContractError("invalid action execution outcome")
        if not isinstance(remote_id, str) or len(remote_id) > 2048:
            raise ContractError("remote_id is invalid")
        safe_error = re.sub(r"[^A-Za-z0-9._:-]", "_", str(error))[:500]
        stamp = utc_now()
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            execution = con.execute(
                "SELECT * FROM action_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            if not execution or execution["status"] != "claimed":
                raise ConflictError("action execution is not claimed")
            action = con.execute(
                "SELECT * FROM action_proposals WHERE action_id=?",
                (execution["action_id"],),
            ).fetchone()
            if not action or action["status"] != ActionStatus.EXECUTING.value:
                raise ConflictError("action proposal is not executing")
            proposal_status = {
                "succeeded": ActionStatus.EXECUTED.value,
                "retryable_failure": (
                    ActionStatus.FAILED.value
                    if int(execution["attempt"]) >= ACTION_EXECUTION_MAX_ATTEMPTS
                    else ActionStatus.APPROVED.value
                ),
                "permanent_failure": ActionStatus.FAILED.value,
                "uncertain": ActionStatus.NEEDS_RECONCILIATION.value,
            }[outcome]
            final_remote_id = remote_id or execution["remote_id"]
            con.execute(
                "UPDATE action_executions SET status=?,remote_id=?,completed_at=?,error=? "
                "WHERE execution_id=?",
                (outcome, final_remote_id, stamp, safe_error, execution_id),
            )
            con.execute(
                "UPDATE action_proposals SET status=? WHERE action_id=?",
                (proposal_status, execution["action_id"]),
            )
            result = {
                "action_id": execution["action_id"],
                "execution_id": execution_id,
                "execution_status": outcome,
                "action_status": proposal_status,
                "remote_id": final_remote_id,
                "completed_at": stamp,
                "error": safe_error,
            }
            con.commit()
            return result
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def recover_stale_actions(
        self,
        now: Optional[str] = None,
        *,
        stale_after_seconds: int = 300,
    ) -> Mapping[str, int]:
        """Recover or quarantine action claims left behind by a crashed worker.

        Reply creation without a durable remote id is ambiguous and must be
        reconciled by the user. Holds have a stable Graph transaction id, and reply
        patches with a checkpointed draft id are safe to retry.
        """

        if stale_after_seconds < 60 or stale_after_seconds > 24 * 60 * 60:
            raise ContractError("stale action threshold must be between 60 seconds and one day")
        stamp = now or utc_now()
        current = parse_utc(stamp)
        cutoff = current - timedelta(seconds=stale_after_seconds)
        recovered = reconciliation = failed = 0
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT a.*,x.execution_id,x.remote_id,x.started_at,x.status AS execution_status,"
                "d.expires_at AS approval_expires_at FROM action_proposals a "
                "LEFT JOIN action_executions x ON x.execution_id=("
                "SELECT execution_id FROM action_executions WHERE action_id=a.action_id "
                "ORDER BY attempt DESC LIMIT 1) "
                "LEFT JOIN action_approval_decisions d ON d.decision_id=x.approval_decision_id "
                "WHERE a.status=?",
                (ActionStatus.EXECUTING.value,),
            ).fetchall()
            for row in rows:
                started = row["started_at"]
                if started and parse_utc(str(started)) > cutoff:
                    continue
                execution_id = row["execution_id"]
                if not execution_id or row["execution_status"] != "claimed":
                    con.execute(
                        "UPDATE action_proposals SET status=? WHERE action_id=?",
                        (ActionStatus.NEEDS_RECONCILIATION.value, row["action_id"]),
                    )
                    reconciliation += 1
                    continue
                reply_without_checkpoint = (
                    row["kind"] == ActionKind.OUTLOOK_REPLY_DRAFT.value
                    and not row["remote_id"]
                )
                if reply_without_checkpoint:
                    con.execute(
                        "UPDATE action_executions SET status='uncertain',completed_at=?,"
                        "error='worker_crashed_before_remote_checkpoint' WHERE execution_id=?",
                        (stamp, execution_id),
                    )
                    con.execute(
                        "UPDATE action_proposals SET status=? WHERE action_id=?",
                        (ActionStatus.NEEDS_RECONCILIATION.value, row["action_id"]),
                    )
                    reconciliation += 1
                    continue
                approval_valid = bool(
                    row["approval_expires_at"]
                    and parse_utc(str(row["approval_expires_at"])) > current
                    and parse_utc(str(row["expires_at"])) > current
                )
                next_status = (
                    ActionStatus.APPROVED.value
                    if approval_valid
                    else ActionStatus.FAILED.value
                )
                execution_status = "retryable_failure" if approval_valid else "permanent_failure"
                error = "worker_claim_expired" if approval_valid else "approval_expired_after_crash"
                con.execute(
                    "UPDATE action_executions SET status=?,completed_at=?,error=? "
                    "WHERE execution_id=?",
                    (execution_status, stamp, error, execution_id),
                )
                con.execute(
                    "UPDATE action_proposals SET status=? WHERE action_id=?",
                    (next_status, row["action_id"]),
                )
                if approval_valid:
                    recovered += 1
                else:
                    failed += 1
            con.commit()
        return {"recovered": recovered, "needs_reconciliation": reconciliation, "failed": failed}

    def reconcile_action(
        self,
        action_id: str,
        resolution: str,
        remote_id: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        """Resolve an uncertain remote write with an explicit user assertion."""

        validate_identifier(action_id, "action_id")
        if context.actor_kind != "user":
            raise ContractError("action reconciliation requires actor_kind=user")
        if resolution not in {"completed", "created", "not_created", "abandon"}:
            raise ContractError("invalid action reconciliation resolution")
        if not isinstance(remote_id, str) or len(remote_id) > 2048:
            raise ContractError("remote_id is invalid")
        if resolution == "completed" and not remote_id:
            raise ContractError("completed reconciliation requires remote_id")
        request = {
            "action_id": action_id,
            "resolution": resolution,
            "remote_id": remote_id,
            "actor_kind": context.actor_kind,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            action = con.execute(
                "SELECT * FROM action_proposals WHERE action_id=?", (action_id,)
            ).fetchone()
            if not action:
                raise ContractError("action proposal not found")
            if action["status"] != ActionStatus.NEEDS_RECONCILIATION.value:
                raise ConflictError("action does not require reconciliation")
            execution = con.execute(
                "SELECT * FROM action_executions WHERE action_id=? "
                "ORDER BY attempt DESC LIMIT 1",
                (action_id,),
            ).fetchone()
            if not execution or execution["status"] != "uncertain":
                raise ConflictError("uncertain action execution was not found")
            if resolution in {"completed", "created"}:
                execution_status = "succeeded"
                action_status = ActionStatus.EXECUTED.value
            elif resolution == "not_created":
                execution_status = "permanent_failure"
                action_status = ActionStatus.PENDING.value
            else:
                execution_status = "permanent_failure"
                action_status = ActionStatus.FAILED.value
            con.execute(
                "UPDATE action_executions SET status=?,remote_id=?,completed_at=?,error=? "
                "WHERE execution_id=?",
                (
                    execution_status,
                    remote_id or execution["remote_id"],
                    stamp,
                    "user_reconciled_" + resolution,
                    execution["execution_id"],
                ),
            )
            con.execute(
                "UPDATE action_proposals SET status=? WHERE action_id=?",
                (action_status, action_id),
            )
            return {
                "action_id": action_id,
                "execution_id": execution["execution_id"],
                "resolution": resolution,
                "action_status": action_status,
                "remote_id": remote_id,
            }

        return self._idempotent("reconcile_action", context, request, operation)

    def create_reminder(
        self, reminder: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        application_id = str(reminder.get("application_id") or "")
        due_at = str(reminder.get("due_at") or "")
        note = str(reminder.get("note") or "").strip()
        validate_identifier(application_id, "application_id")
        due = parse_utc(due_at)
        if not note or len(note) > 500 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", note):
            raise ContractError("reminder note must be 1 to 500 plain-text characters")
        request = {
            "application_id": application_id,
            "due_at": due_at,
            "note": note,
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            self._application(con, application_id)
            if due <= parse_utc(stamp):
                raise ContractError("reminder due_at must be in the future")
            reminder_id = _new_id()
            con.execute(
                "INSERT INTO reminders "
                "(reminder_id,application_id,note,due_at,status,idempotency_key,created_at) "
                "VALUES (?,?,?,?,'scheduled',?,?)",
                (
                    reminder_id,
                    application_id,
                    note,
                    due_at,
                    context.idempotency_key,
                    stamp,
                ),
            )
            saved = con.execute(
                "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
            return {"created": True, "reminder": _row(saved)}

        return self._idempotent("create_reminder", context, request, operation)

    def list_reminders(
        self,
        statuses: Optional[Sequence[str]] = None,
        limit: int = 100,
    ) -> Sequence[Mapping[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ContractError("reminder limit must be between 1 and 500")
        allowed = {"scheduled", "cancelled", "completed"}
        normalized = tuple(dict.fromkeys(str(value) for value in (statuses or ())))
        if any(value not in allowed for value in normalized):
            raise ContractError("invalid reminder status")
        where = ""
        parameters: Tuple[Any, ...] = ()
        if normalized:
            marks = ",".join("?" for _ in normalized)
            where = f" WHERE status IN ({marks})"
            parameters = normalized
        with connect(self.db_path) as con:
            return [
                _row(row)
                for row in con.execute(
                    "SELECT * FROM reminders"
                    + where
                    + " ORDER BY due_at,created_at,reminder_id LIMIT ?",
                    (*parameters, limit),
                )
            ]

    def cancel_reminder(
        self, reminder_id: str, context: MutationContext
    ) -> Mapping[str, Any]:
        validate_identifier(reminder_id, "reminder_id")
        request = {
            "reminder_id": reminder_id,
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            saved = con.execute(
                "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
            if not saved:
                raise ContractError("reminder not found")
            queued = con.execute(
                "SELECT notification_id FROM notification_outbox WHERE topic='reminder.due' "
                "AND application_id=? AND json_extract(context_json,'$.reminder_id') IN (?,?) "
                "AND status IN ('pending','delivering')",
                (saved["application_id"], reminder_id, "general:" + reminder_id),
            ).fetchall()
            if saved["status"] == "cancelled" and not queued:
                return {"cancelled": False, "reminder": _row(saved)}
            if saved["status"] == "completed" and not queued:
                raise ConflictError("delivered reminder cannot be cancelled")
            for notification in queued:
                con.execute(
                    "UPDATE notification_outbox SET status='cancelled',lease_owner=NULL,"
                    "lease_token=NULL,lease_expires_at=NULL WHERE notification_id=?",
                    (notification["notification_id"],),
                )
            con.execute(
                "UPDATE reminders SET status='cancelled',cancelled_at=? "
                "WHERE reminder_id=?",
                (stamp, reminder_id),
            )
            updated = con.execute(
                "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
            return {"cancelled": True, "reminder": _row(updated)}

        return self._idempotent("cancel_reminder", context, request, operation)

    def complete_reminder(
        self, reminder_id: str, context: MutationContext
    ) -> Mapping[str, Any]:
        validate_identifier(reminder_id, "reminder_id")
        request = {
            "reminder_id": reminder_id,
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            saved = con.execute(
                "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
            if not saved:
                raise ContractError("reminder not found")
            if saved["status"] == "completed":
                return {"completed": False, "reminder": _row(saved)}
            if saved["status"] != "scheduled":
                raise ConflictError("only a scheduled reminder can be completed")
            con.execute(
                "UPDATE reminders SET status='completed',completed_at=? "
                "WHERE reminder_id=?",
                (stamp, reminder_id),
            )
            updated = con.execute(
                "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
            return {"completed": True, "reminder": _row(updated)}

        return self._idempotent("complete_reminder", context, request, operation)

    def _enqueue_notification(
        self, notification: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        dedupe_key = str(notification.get("dedupe_key") or "")
        topic = str(notification.get("topic") or "")
        policy_id = str(notification.get("policy_id") or "")
        application_id = str(notification.get("application_id") or "") or None
        title = str(notification.get("title") or "").strip()
        body = str(notification.get("body") or "").strip()
        available_at = str(notification.get("available_at") or "")
        context_data = notification.get("context", {})
        max_attempts = notification.get("max_attempts", 5)
        validate_identifier(dedupe_key, "dedupe_key")
        validate_identifier(topic, "topic")
        validate_identifier(policy_id, "policy_id")
        if application_id:
            validate_identifier(application_id, "application_id")
        if available_at:
            parse_utc(available_at)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ContractError("notification max_attempts must be an integer")
        if not 1 <= max_attempts <= 20:
            raise ContractError("notification max_attempts must be between 1 and 20")
        if not title or len(title) > 200:
            raise ContractError("notification title must be 1 to 200 characters")
        if not body or len(body) > 2000:
            raise ContractError("notification body must be 1 to 2000 characters")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", title + body):
            raise ContractError("notification text contains unsafe control characters")
        if not isinstance(context_data, Mapping):
            raise ContractError("notification context must be an object")
        encoded_context = canonical_json(context_data)
        if len(encoded_context.encode("utf-8")) > 8192:
            raise ContractError("notification context exceeds 8192 bytes")
        normalized = {
            "dedupe_key": dedupe_key,
            "topic": topic,
            "policy_id": policy_id,
            "application_id": application_id,
            "title": title,
            "body": body,
            "context": dict(context_data),
            "max_attempts": max_attempts,
            "available_at": available_at,
        }
        # The enqueue clock is not semantic notification content.  A replay with the
        # same source must return the first durable result even when its clock advances.
        request = {
            **{
                key: value
                for key, value in normalized.items()
                if key != "available_at"
            },
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
        }

        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            actual_available_at = available_at or stamp
            if application_id:
                self._application(con, application_id)
            existing = con.execute(
                "SELECT * FROM notification_outbox WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if existing:
                same = (
                    existing["topic"] == topic
                    and existing["policy_id"] == policy_id
                    and existing["application_id"] == application_id
                    and existing["title"] == title
                    and existing["body"] == body
                    and existing["context_json"] == encoded_context
                    and existing["max_attempts"] == max_attempts
                    and (
                        not available_at
                        or existing["available_at"] == actual_available_at
                    )
                )
                if not same:
                    raise ConflictError(
                        "notification dedupe key conflicts with an existing message"
                    )
                item = _row(existing)
                item["context"] = json.loads(item.pop("context_json"))
                return {"created": False, "notification": item}
            notification_id = _new_id()
            con.execute(
                "INSERT INTO notification_outbox "
                "(notification_id,dedupe_key,topic,policy_id,application_id,title,body,"
                "context_json,status,max_attempts,available_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    notification_id,
                    dedupe_key,
                    topic,
                    policy_id,
                    application_id,
                    title,
                    body,
                    encoded_context,
                    max_attempts,
                    actual_available_at,
                    stamp,
                ),
            )
            saved = con.execute(
                "SELECT * FROM notification_outbox WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            item = _row(saved)
            item["context"] = json.loads(item.pop("context_json"))
            return {"created": True, "notification": item}

        return self._idempotent("enqueue_notification", context, request, operation)

    def claim_notification(
        self, worker_id: str, now: str, lease_seconds: int = 60
    ) -> Optional[Mapping[str, Any]]:
        validate_identifier(worker_id, "worker_id")
        current = parse_utc(now)
        if not 1 <= lease_seconds <= 3600:
            raise ContractError("notification lease must be between 1 and 3600 seconds")
        lease_expires_at = (current + timedelta(seconds=lease_seconds)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            control = con.execute(
                "SELECT enabled FROM automation_controls WHERE capability='notifications'"
            ).fetchone()
            if control is not None and not control["enabled"]:
                con.commit()
                return None
            con.execute(
                "UPDATE notification_outbox SET status='dead',lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,"
                "last_error='notification attempt limit reached' "
                "WHERE attempts>=max_attempts AND (status='pending' OR "
                "(status='delivering' AND lease_expires_at<=?))",
                (now,),
            )
            con.execute(
                "UPDATE notification_outbox SET status='pending',lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL "
                "WHERE status='delivering' AND lease_expires_at<=? "
                "AND attempts<max_attempts",
                (now,),
            )
            candidates = con.execute(
                "SELECT * FROM notification_outbox WHERE status='pending' "
                "AND attempts<max_attempts AND available_at<=? "
                "ORDER BY available_at,created_at,notification_id "
                "LIMIT 100",
                (now,),
            ).fetchall()
            from .attention import AttentionService
            saved = next((row for row in candidates
                          if AttentionService.validate_delivery(con, dict(row), now)), None)
            if saved is None:
                con.commit()
                return None
            lease_token = _new_id()
            con.execute(
                "UPDATE notification_outbox SET status='delivering',attempts=attempts+1,"
                "lease_owner=?,lease_token=?,lease_expires_at=? WHERE notification_id=?",
                (worker_id, lease_token, lease_expires_at, saved["notification_id"]),
            )
            claimed = con.execute(
                "SELECT * FROM notification_outbox WHERE notification_id=?",
                (saved["notification_id"],),
            ).fetchone()
            item = _row(claimed)
            item["context"] = json.loads(item.pop("context_json"))
            con.commit()
            return item
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def defer_notification_for_receipt(self, notification_id, lease_token, now, retry_at):
        """A queued Telegram ticket is not a failed send or a delivery receipt."""
        parse_utc(now)
        parse_utc(retry_at)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            saved = con.execute("SELECT status,lease_token FROM notification_outbox WHERE notification_id=?", (notification_id,)).fetchone()
            if saved is None:
                raise ContractError("notification not found")
            if saved['status'] in ('delivered', 'cancelled', 'dead'):
                return saved['status']
            if saved['status'] != 'delivering' or saved['lease_token'] != lease_token:
                raise ConflictError("notification lease changed")
            con.execute("UPDATE notification_outbox SET status='pending',available_at=?,attempts=MAX(0,attempts-1),lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE notification_id=?", (retry_at,notification_id))
            return 'pending'

    def validate_notification_claim(self, notification_id: str, lease_token: str, now: str) -> bool:
        """Recheck authorization and source relevance immediately before external I/O."""
        parse_utc(now)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM notification_outbox WHERE notification_id=?", (notification_id,)).fetchone()
            if row is None or row['status'] != 'delivering' or row['lease_token'] != lease_token or row['lease_expires_at'] <= now:
                return False
            control = con.execute("SELECT enabled FROM automation_controls WHERE capability='notifications'").fetchone()
            if control is not None and not control['enabled']:
                con.execute("UPDATE notification_outbox SET status='pending',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE notification_id=?", (notification_id,))
                return False
            from .attention import AttentionService
            return AttentionService.validate_delivery(con, dict(row), now)

    def complete_notification(
        self,
        notification_id: str,
        lease_token: str,
        outcome: str,
        now: str,
        *,
        retry_at: Optional[str] = None,
        error: str = "",
    ) -> Mapping[str, Any]:
        validate_identifier(notification_id, "notification_id")
        validate_identifier(lease_token, "lease_token")
        parse_utc(now)
        if retry_at:
            parse_utc(retry_at)
        if outcome not in {"succeeded", "retryable_failure", "permanent_failure"}:
            raise ContractError("invalid notification outcome")
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            saved = con.execute(
                "SELECT * FROM notification_outbox WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if not saved:
                raise ContractError("notification not found")
            if saved["status"] != "delivering" or saved["lease_token"] != lease_token:
                raise ConflictError("notification lease is not current")
            safe_error = str(error)[:1000]
            if outcome == "succeeded":
                status = "delivered"
                available_at = saved["available_at"]
                delivered_at = now
            elif outcome == "retryable_failure" and saved["attempts"] < saved["max_attempts"]:
                if not retry_at or parse_utc(retry_at) <= parse_utc(now):
                    raise ContractError("retryable notification requires a future retry_at")
                status = "pending"
                available_at = retry_at
                delivered_at = None
            else:
                status = "dead"
                available_at = saved["available_at"]
                delivered_at = None
            con.execute(
                "UPDATE notification_outbox SET status=?,available_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,last_error=?,delivered_at=? "
                "WHERE notification_id=?",
                (
                    status,
                    available_at,
                    safe_error,
                    delivered_at,
                    notification_id,
                ),
            )
            con.commit()
            return {
                "notification_id": notification_id,
                "status": status,
                "attempts": int(saved["attempts"]),
                "available_at": available_at,
                "delivered_at": delivered_at,
            }
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def list_notification_outbox(
        self, statuses: Optional[Sequence[str]] = None, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ContractError("notification limit must be between 1 and 500")
        allowed = {"pending", "delivering", "delivered", "dead", "cancelled"}
        normalized = tuple(dict.fromkeys(str(value) for value in (statuses or ())))
        if any(value not in allowed for value in normalized):
            raise ContractError("invalid notification status")
        where = ""
        parameters: Tuple[Any, ...] = ()
        if normalized:
            marks = ",".join("?" for _ in normalized)
            where = f" WHERE status IN ({marks})"
            parameters = normalized
        with connect(self.db_path) as con:
            result = []
            for row in con.execute(
                "SELECT * FROM notification_outbox"
                + where
                + " ORDER BY created_at DESC,notification_id LIMIT ?",
                (*parameters, limit),
            ):
                item = _row(row)
                item["context"] = json.loads(item.pop("context_json"))
                result.append(item)
            return result

    def get_shortlist_notification_state(self) -> Mapping[str, Any]:
        """Return only the durable state needed to enforce shortlist alert policy."""

        with connect(self.db_path) as con:
            rows = con.execute(
                "SELECT created_at,context_json FROM notification_outbox "
                "WHERE topic='shortlist.ready' "
                "ORDER BY created_at DESC,notification_id"
            ).fetchall()
        workflow_ids = []
        for row in rows:
            context = json.loads(str(row["context_json"]))
            workflow = context.get("workflow") if isinstance(context, Mapping) else None
            if isinstance(workflow, str) and workflow:
                workflow_ids.append(workflow)
        return {
            "latest_created_at": str(rows[0]["created_at"]) if rows else None,
            "workflow_ids": tuple(dict.fromkeys(workflow_ids)),
        }

    def get_application_timeline(self, application_id: str) -> Mapping[str, Any]:
        validate_identifier(application_id, "application_id")
        with connect(self.db_path) as con:
            application = self._application(con, application_id)
            email_evidence = {
                row["applied_event_id"]: {
                    key: row[key] for key in
                    ("evidence_id", "sender", "subject", "received_at", "evidence_quote")
                }
                for row in con.execute(
                    "SELECT p.applied_event_id,e.evidence_id,e.sender,e.subject,"
                    "e.received_at,p.evidence_quote FROM event_proposals p "
                    "JOIN mail_evidence e ON e.evidence_id=p.evidence_id "
                    "JOIN application_events a ON a.event_id=p.applied_event_id "
                    "WHERE a.application_id=? AND p.status IN ('accepted','auto_applied')",
                    (application_id,),
                )
            }
            events = []
            for saved in self._events(con, application_id):
                item = _row(saved)
                item["payload"] = json.loads(item.pop("payload_json"))
                if item["event_id"] in email_evidence:
                    item["email_evidence"] = email_evidence[item["event_id"]]
                events.append(item)
            return {"application": application, "events": events}

    def list_applications(
        self,
        phases: Optional[Sequence[str]] = None,
        limit: int = 200,
    ) -> Sequence[Mapping[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ContractError("application limit must be between 1 and 1000")
        parameters: Tuple[Any, ...] = ()
        where = ""
        if phases:
            allowed = {phase.value for phase in ApplicationPhase}
            normalized = tuple(dict.fromkeys(str(phase) for phase in phases))
            if any(phase not in allowed for phase in normalized):
                raise ContractError("invalid application phase")
            marks = ",".join("?" for _ in normalized)
            where = f" WHERE current_phase IN ({marks})"
            parameters = normalized
        with connect(self.db_path) as con:
            return [
                _row(row)
                for row in con.execute(
                    "SELECT * FROM applications"
                    + where
                    + " ORDER BY updated_at DESC,application_id LIMIT ?",
                    (*parameters, limit),
                )
            ]

    def list_mail_candidates(
        self, *, received_at: str = "", account_id: str = "",
        conversation_id: str = "", sender: str = "", include_unsubmitted: bool = False,
    ) -> Sequence[Mapping[str, Any]]:
        """Read application history for local retrieval before bounding model context.

        Unsubmitted tracked applications are included only for explicit review.
        Automatic ingestion keeps its submission/evidence eligibility boundary.
        """
        with connect(self.db_path) as con:
            rows = [_row(row) for row in con.execute(
                "SELECT a.*, "
                "(SELECT b.created_at FROM browser_attempts b WHERE b.application_id=a.application_id "
                "AND julianday(b.created_at)<=julianday(?) ORDER BY julianday(b.created_at) DESC LIMIT 1) "
                "AS submission_attempted_at, "
                "EXISTS(SELECT 1 FROM event_proposals p JOIN mail_evidence e USING(evidence_id) "
                "JOIN application_events ae ON ae.event_id=p.applied_event_id "
                "WHERE ae.application_id=a.application_id AND p.status IN ('accepted','auto_applied') "
                "AND NOT EXISTS(SELECT 1 FROM lifecycle_mail_observations o WHERE o.evidence_id=e.evidence_id) "
                "AND e.account_id=? AND e.conversation_id=? AND ?<>'') AS same_conversation, "
                "EXISTS(SELECT 1 FROM event_proposals p JOIN mail_evidence e USING(evidence_id) "
                "JOIN application_events ae ON ae.event_id=p.applied_event_id "
                "WHERE ae.application_id=a.application_id AND p.status IN ('accepted','auto_applied') "
                "AND (NOT EXISTS(SELECT 1 FROM lifecycle_mail_observations o WHERE o.evidence_id=e.evidence_id) "
                "OR EXISTS(SELECT 1 FROM lifecycle_mail_observations o JOIN lifecycle_mail_links l USING(observation_id) "
                "WHERE o.evidence_id=e.evidence_id AND l.application_id=a.application_id)) "
                "AND e.account_id=? AND lower(e.sender)=lower(?) AND ?<>'') AS same_sender "
                "FROM applications a WHERE ? OR a.submitted_at IS NOT NULL OR a.confirmed_at IS NOT NULL "
                "OR a.ats='external' OR EXISTS(SELECT 1 FROM lifecycle_mail_links l WHERE l.application_id=a.application_id) OR EXISTS(SELECT 1 FROM browser_attempts b WHERE b.application_id=a.application_id) "
                "ORDER BY a.updated_at DESC,a.application_id",
                (received_at, account_id, conversation_id, conversation_id, account_id, sender, sender,
                 include_unsubmitted),
            )]

            if conversation_id and account_id:
                thread = payload_sha256({'account': account_id, 'conversation': conversation_id})
                linked = {r[0] for r in con.execute('SELECT DISTINCT application_id FROM lifecycle_mail_links JOIN lifecycle_mail_observations USING(observation_id) WHERE account_id=? AND conversation_ref=?', (account_id, thread))}
                for row in rows:
                    row['same_conversation'] = bool(row['same_conversation'] or row['application_id'] in linked)
            return rows

    def application_job_keys(self) -> Sequence[Tuple[str, str]]:
        with connect(self.db_path) as con:
            return [
                (str(row["ats"]), str(row["job_id"]))
                for row in con.execute(
                    "SELECT ats,job_id FROM applications ORDER BY ats,job_id"
                )
            ]

    def recent_company_applications(self) -> Sequence[Mapping[str, Any]]:
        """Actual submissions in the rolling 180-day window, newest first.

        Keep terminal applications and avoid the paginated application-list limit.
        A later confirmation must not renew an older submission's window.
        """
        now = parse_utc(utc_now())
        cutoff = now - timedelta(days=180)
        with connect(self.db_path) as con:
            return [_row(row) for row in con.execute(
                "SELECT application_id,ats,employer_snapshot,company_slug_snapshot,"
                "COALESCE(submitted_at,confirmed_at) AS applied_at,180 AS window_days "
                "FROM applications WHERE julianday(COALESCE(submitted_at,confirmed_at)) "
                "BETWEEN julianday(?) AND julianday(?) "
                "ORDER BY julianday(COALESCE(submitted_at,confirmed_at)) DESC,application_id",
                (cutoff.isoformat(), now.isoformat()),
            )]

    def list_actions(
        self, statuses: Optional[Sequence[str]] = None
    ) -> Sequence[Mapping[str, Any]]:
        parameters: Tuple[Any, ...] = ()
        where = ""
        if statuses:
            allowed = {status.value for status in ActionStatus}
            normalized = tuple(dict.fromkeys(str(status) for status in statuses))
            if any(status not in allowed for status in normalized):
                raise ContractError("invalid action status")
            marks = ",".join("?" for _ in normalized)
            where = f" WHERE status IN ({marks})"
            parameters = normalized
        with connect(self.db_path) as con:
            actions = []
            for row in con.execute(
                "SELECT * FROM action_proposals"
                + where
                + " ORDER BY created_at DESC,action_id",
                parameters,
            ):
                action = _row(row)
                action["payload"] = json.loads(action.pop("payload_json"))
                actions.append(action)
            return actions

    def system_health(self) -> Mapping[str, Any]:
        with connect(self.db_path) as con:
            application_counts = {
                str(row["current_phase"]): int(row["count"])
                for row in con.execute(
                    "SELECT current_phase,COUNT(*) AS count FROM applications "
                    "GROUP BY current_phase ORDER BY current_phase"
                )
            }
            outbox_counts = {
                str(row["status"]): int(row["count"])
                for row in con.execute(
                    "SELECT status,COUNT(*) AS count FROM outbox_messages "
                    "GROUP BY status ORDER BY status"
                )
            }
            notification_counts = {
                str(row["status"]): int(row["count"])
                for row in con.execute(
                    "SELECT status,COUNT(*) AS count FROM notification_outbox "
                    "GROUP BY status ORDER BY status"
                )
            }
            reminder_counts = {
                str(row["status"]): int(row["count"])
                for row in con.execute(
                    "SELECT status,COUNT(*) AS count FROM reminders "
                    "GROUP BY status ORDER BY status"
                )
            }
            work_counts = {
                str(row["status"]): int(row["count"])
                for row in con.execute(
                    "SELECT status,COUNT(*) AS count FROM work_items "
                    "GROUP BY status ORDER BY status"
                )
            }
            from .recovery import unresolved_work_count
            unresolved_work = unresolved_work_count(con)
            schedules = [
                _row(row)
                for row in con.execute(
                    "SELECT schedule_key,task_kind,enabled,next_due_at,updated_at "
                    "FROM schedule_specs ORDER BY schedule_key"
                )
            ]
            recent_runs = [
                _row(row)
                for row in con.execute(
                    "SELECT run_id,work_id,scheduled_for,started_at,completed_at,outcome,error "
                    "FROM job_runs ORDER BY started_at DESC,run_id LIMIT 20"
                )
            ]
            pending_reviews = int(
                con.execute(
                    "SELECT COUNT(*) FROM event_proposals "
                    "WHERE status IN ('pending','conflict')"
                ).fetchone()[0]
            ) + int(
                con.execute(
                    "SELECT COUNT(*) FROM temporal_proposals "
                    "WHERE status IN ('pending','conflict')"
                ).fetchone()[0]
            ) + int(
                con.execute(
                    "SELECT COUNT(*) FROM action_proposals "
                    "WHERE status IN ('pending','executing','failed','needs_reconciliation')"
                ).fetchone()[0]
            )
            oldest_outbox = con.execute(
                "SELECT MIN(created_at) FROM outbox_messages "
                "WHERE status IN ('pending','delivering')"
            ).fetchone()[0]
            oldest_notification = con.execute(
                "SELECT MIN(created_at) FROM notification_outbox "
                "WHERE status IN ('pending','delivering')"
            ).fetchone()[0]
            migrations = [
                {"version": int(row["version"]), "name": str(row["name"])}
                for row in con.execute(
                    "SELECT version,name FROM schema_migrations ORDER BY version"
                )
            ]
            connectors = [dict(row) for row in con.execute(
                "SELECT * FROM connector_health ORDER BY connector_key"
            )]
            mail_stage_counts = {
                str(row["processing_status"]): int(row["count"])
                for row in con.execute(
                    "SELECT processing_status,COUNT(*) AS count "
                    "FROM outlook_message_stage GROUP BY processing_status"
                )
            }
        projection_failures = self.verify_projections()
        unhealthy = bool(
            projection_failures
            or outbox_counts.get("dead", 0)
            or notification_counts.get("dead", 0)
            or unresolved_work
            or mail_stage_counts.get("failed", 0)
            or any(row["status"] not in {"healthy", "disabled"} for row in connectors)
        )
        return {
            "status": "attention" if unhealthy else "healthy",
            "checked_at": utc_now(),
            "database": str(self.db_path),
            "migrations": migrations,
            "connectors": connectors,
            "mail_stage": {"counts": mail_stage_counts},
            "applications": application_counts,
            "pending_reviews": pending_reviews,
            "outbox": {"counts": outbox_counts, "oldest_pending_at": oldest_outbox},
            "notifications": {
                "counts": notification_counts,
                "oldest_pending_at": oldest_notification,
            },
            "reminders": {"counts": reminder_counts},
            "work": {"counts": work_counts, "unresolved_dead": unresolved_work, "schedules": schedules, "recent_runs": recent_runs},
            "projection_failures": projection_failures,
        }

    def resolve_mail_failure(self, account_id: str, folder_ref: str, message_id: str,
                             query_version: int, action: str,
                             context: MutationContext) -> Mapping[str, Any]:
        if action not in {"retry", "dismiss"}:
            raise ContractError("mail action must be retry or dismiss")
        if type(query_version) is not int or query_version < 1:
            raise ContractError("query_version must be a positive integer")
        for value in (account_id, folder_ref, message_id):
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ContractError("invalid staged message identity")
        request = dict(account_id=account_id, folder_ref=folder_ref, message_id=message_id,
                       query_version=query_version, action=action, actor_kind=context.actor_kind)
        def operation(con: sqlite3.Connection, stamp: str) -> Mapping[str, Any]:
            identity = (account_id, folder_ref, query_version, message_id)
            row = con.execute("SELECT processing_status,removed FROM outlook_message_stage "
                "WHERE account_id=? AND folder_ref=? AND query_version=? AND immutable_message_id=?",
                identity).fetchone()
            if not row:
                raise ContractError("staged message was not found")
            if row["processing_status"] != "failed":
                raise ConflictError("message is no longer awaiting failure review; refresh the page")
            if action == "retry" and row["removed"]:
                raise ContractError("message is no longer in the synced folder; dismiss this item")
            status = "pending" if action == "retry" else "ignored"
            con.execute("UPDATE outlook_message_stage SET processing_status=?,last_error='',updated_at=? "
                "WHERE account_id=? AND folder_ref=? AND query_version=? AND immutable_message_id=?",
                (status, stamp, *identity))
            return {"status":status, "action":action, "message_id":message_id}
        return self._idempotent("resolve_mail_failure", context, request, operation)

    def _unassigned_mail_candidates(
        self, evidence_id: str, *, review_mail_content: Optional[Mapping[str, Any]] = None,
    ) -> list[str]:
        """Revalidate identity using a server-loaded archive, or the saved excerpt.

        The optional content must come from the authenticated archive reader,
        never from a browser request. It remains local to this read and is not
        included in the decision's persisted idempotency payload.
        """
        from .mail.context import CandidateApplication
        from .mail.identity import review_supported_candidates
        from .mail.matching import rank_mail_candidates
        evidence = self.get_mail_evidence(evidence_id)
        subject = evidence['subject']
        body = evidence['excerpt']
        if review_mail_content is not None:
            subject = review_mail_content.get('subject', subject)
            body = review_mail_content.get('body', body)
        rows = rank_mail_candidates(self.list_mail_candidates(
            received_at=evidence['received_at'], account_id=evidence['account_id'],
            conversation_id=evidence['conversation_id'], sender=evidence['sender'],
            include_unsubmitted=True,
        ), subject + '\n' + body)
        candidates = [CandidateApplication(
            application_id=row['application_id'], ats=row['ats'], job_id=row['job_id'],
            employer=row['employer_snapshot'], company_slug=row['company_slug_snapshot'],
            title=row['title_snapshot'], phase=row['current_phase'],
            match_context=row['mail_match_context'],
        ) for row in rows]
        return [c.application_id for c in review_supported_candidates(
            candidates, subject, body)][:20]

    def list_attention_items(self) -> Sequence[Mapping[str, Any]]:
        with connect(self.db_path) as con:
            rows: List[Mapping[str, Any]] = []
            for task in con.execute(
                "SELECT t.*,a.employer_snapshot,a.title_snapshot,e.subject,e.sender "
                "FROM lifecycle_tasks t JOIN applications a USING(application_id) "
                "LEFT JOIN mail_evidence e USING(evidence_id) "
                "WHERE t.status='open' AND t.owner='applicant' "
                "AND t.kind IN ('reply','send_availability') AND a.current_phase<>'terminal' "
                "ORDER BY t.created_at,t.task_id"
            ):
                rows.append({
                    'kind': 'reply_request', 'id': task['task_id'],
                    'application_id': task['application_id'], 'status': 'review',
                    'detail': task['note'], 'task_kind': task['kind'],
                    'revision_no': task['revision_no'], 'evidence_id': task['evidence_id'],
                    'due_at': task['due_at'], 'snoozed_until': task['snoozed_until'],
                    'created_at': task['source_time'] or task['created_at'],
                    'employer': task['employer_snapshot'], 'title': task['title_snapshot'],
                    'subject': task['subject'], 'sender': task['sender'],
                })
            from .mail.understanding_store import available, briefing_analyses
            understood = available(con)
            projected = {(r['kind'],r['target_id']) for r in con.execute('SELECT kind,target_id FROM mail_understanding_projections')} if understood else set()
            for analysis in briefing_analyses(con):
                if analysis['mode']=='shared' and analysis['current'] and any(f['status'] in ('pending','held') for f in analysis['findings']):
                    rows.append({'kind':'mail_analysis','id':analysis['analysis_id'],'application_id':analysis['application_id'],'status':'review','detail':'Email understanding','created_at':analysis['created_at'],'candidate_application_ids':analysis['candidate_application_ids'],'analysis':analysis})
            for attempt in con.execute("SELECT b.*,a.title_snapshot,a.employer_snapshot FROM browser_attempts b JOIN applications a USING(application_id) WHERE a.submitted_at IS NULL AND a.current_phase='preparing' AND b.status IN ('attempted','request_sent','failed') AND datetime(b.updated_at)<datetime('now','-10 minutes') AND b.attempt_id=(SELECT x.attempt_id FROM browser_attempts x WHERE x.application_id=b.application_id ORDER BY x.created_at DESC LIMIT 1)"):
                rows.append({"kind": "browser_submission", "id": attempt["attempt_id"], "application_id": attempt["application_id"],
                    "status": "review", "detail": "Submission has not been confirmed. Check the employer page or wait for a confirmation email.",
                    "created_at": attempt["created_at"], "employer": attempt["employer_snapshot"], "title": attempt["title_snapshot"]})
            for proposal in con.execute(
                "SELECT p.proposal_id,p.evidence_id,p.proposed_application_id,p.event_type,p.confidence,"
                "p.evidence_quote,p.candidate_application_ids_json,p.created_at,e.subject,e.sender "
                "FROM event_proposals p JOIN mail_evidence e USING(evidence_id) WHERE p.status IN ('pending','conflict') "
                "ORDER BY p.created_at,p.proposal_id"
            ):
                if ('event_proposal',proposal['proposal_id']) in projected:
                    continue
                rows.append(
                    {
                        "kind": "event_proposal",
                        "id": proposal["proposal_id"],
                        "application_id": proposal["proposed_application_id"],
                        "status": "review",
                        "detail": proposal["event_type"],
                        "subject": proposal["subject"],
                        "sender": proposal["sender"],
                        "confidence": proposal["confidence"],
                        "evidence_quote": proposal["evidence_quote"],
                        "candidate_application_ids": self._unassigned_mail_candidates(proposal['evidence_id'])
                        if not proposal['proposed_application_id'] else json.loads(
                            proposal["candidate_application_ids_json"]
                        ),
                        "created_at": proposal["created_at"],
                    }
                )
            for proposal in con.execute(
                "SELECT t.temporal_proposal_id,t.application_id,t.kind,t.starts_at,"
                "t.ends_at,t.due_at,t.time_zone,t.confidence,t.evidence_quote,"
                "t.created_at,a.employer_snapshot,a.title_snapshot "
                "FROM temporal_proposals t JOIN applications a "
                "ON a.application_id=t.application_id WHERE t.status='pending' "
                "ORDER BY t.created_at,t.temporal_proposal_id"
            ):
                if ('temporal_proposal',proposal['temporal_proposal_id']) in projected:
                    continue
                rows.append(
                    {
                        "kind": "temporal_proposal",
                        "id": proposal["temporal_proposal_id"],
                        "application_id": proposal["application_id"],
                        "employer": proposal["employer_snapshot"],
                        "title": proposal["title_snapshot"],
                        "status": "review",
                        "detail": proposal["kind"],
                        "starts_at": proposal["starts_at"],
                        "ends_at": proposal["ends_at"],
                        "due_at": proposal["due_at"],
                        "time_zone": proposal["time_zone"],
                        "confidence": proposal["confidence"],
                        "evidence_quote": proposal["evidence_quote"],
                        "created_at": proposal["created_at"],
                    }
                )
            for action in con.execute(
                "SELECT action_id,application_id,kind,status,created_at FROM action_proposals "
                "WHERE status IN ('pending','executing','failed','needs_reconciliation') "
                "ORDER BY created_at,action_id"
            ):
                rows.append(
                    {
                        "kind": "action_proposal",
                        "id": action["action_id"],
                        "application_id": action["application_id"],
                        "status": (
                            "approval" if action["status"] == "pending" else action["status"]
                        ),
                        "detail": action["kind"],
                        "created_at": action["created_at"],
                    }
                )
            for review in con.execute(
                "SELECT r.*,e.subject,e.sender FROM mail_classification_reviews r "
                "JOIN mail_evidence e USING(evidence_id) WHERE r.status='pending' "
                "AND NOT EXISTS(SELECT 1 FROM mail_understanding_ownership o WHERE o.evidence_id=r.evidence_id) "
                "ORDER BY r.created_at,r.review_id"
            ):
                rows.append(dict(kind='mail_classification_review', id=review['review_id'],
                    application_id=None, status='review', detail='Classification needs review',
                    subject=review['subject'], sender=review['sender'], created_at=review['created_at'],
                    reason_code=review['reason_code']))
            for message in con.execute(
                "SELECT account_id,folder_ref,query_version,immutable_message_id,subject,last_error,updated_at,web_link,removed "
                "FROM outlook_message_stage WHERE processing_status='failed' "
                "ORDER BY updated_at,immutable_message_id"
            ):
                rows.append(
                    {
                        "kind": "mail_processing_failure",
                        "id": message["immutable_message_id"],
                        "application_id": None,
                        "status": "failed",
                        "detail": message["subject"],
                        "error": message["last_error"],
                        "account_id": message["account_id"],
                        "folder_ref": message["folder_ref"],
                        "query_version": message["query_version"],
                        "web_link": message["web_link"],
                        "can_retry": not bool(message["removed"]),
                        "created_at": message["updated_at"],
                    }
                )
            return rows

    def verify_projections(self) -> Sequence[Mapping[str, Any]]:
        with connect(self.db_path) as con:
            failures = []
            for application in con.execute(
                "SELECT * FROM applications ORDER BY application_id"
            ):
                expected = reduce_events(self._events(con, application["application_id"]))
                mismatches = projection_mismatches(application, expected)
                if mismatches:
                    failures.append(
                        {
                            "application_id": application["application_id"],
                            "mismatches": mismatches,
                        }
                    )
            return failures

    def rebuild_projections(
        self,
        dry_run: bool = True,
        context: Optional[MutationContext] = None,
    ) -> Sequence[Mapping[str, Any]]:
        if dry_run:
            return self.verify_projections()
        if context is None:
            raise ContractError("a mutation context is required to rebuild projections")

        def operation(con: sqlite3.Connection, _stamp: str) -> Mapping[str, Any]:
            rebuilt = []
            for application in con.execute(
                "SELECT application_id FROM applications ORDER BY application_id"
            ).fetchall():
                application_id = application["application_id"]
                expected = reduce_events(self._events(con, application_id))
                stored = self._application(con, application_id)
                mismatch = projection_mismatches(stored, expected)
                if mismatch:
                    self._project(con, application_id)
                    rebuilt.append(application_id)
            return {"rebuilt_application_ids": rebuilt}

        result = self._idempotent(
            "rebuild_projections",
            context,
            {"dry_run": False, "actor_kind": context.actor_kind},
            operation,
        )
        return result["rebuilt_application_ids"]

    def list_outbox(self, status: str = "pending") -> Sequence[Mapping[str, Any]]:
        if status not in {"pending", "delivering", "delivered", "dead"}:
            raise ContractError("invalid outbox status")
        with connect(self.db_path) as con:
            result = []
            for row in con.execute(
                "SELECT * FROM outbox_messages WHERE status=? ORDER BY created_at,outbox_id",
                (status,),
            ):
                item = _row(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result
