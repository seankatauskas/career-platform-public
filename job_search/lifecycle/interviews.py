"""Reviewed interview rounds and read-only Outlook reconciliation.

Only reviewed identity links may receive automatic calendar revisions. Calendar
change keys are opaque equality tokens, never sequence numbers. Older updates and
same-timestamp divergent updates remain recorded without replacing newer facts.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..contracts import (
    ApplicationEventType,
    ContractError,
    MutationContext,
    canonical_json,
    parse_utc,
    payload_sha256,
    utc_now,
)
from ..db import connect

STATES = frozenset({"proposed", "confirmed", "rescheduled", "cancelled", "completed"})
ACTIVE = ("confirmed", "rescheduled")


def _id():
    return uuid.uuid4().hex


def _stamp(value, *, subsecond=False):
    return (
        parse_utc(str(value))
        .isoformat(timespec="microseconds" if subsecond else "seconds")
        .replace("+00:00", "Z")
    )


def _row(row):
    result = dict(row)
    if "details_json" in result:
        result["details"] = json.loads(result.pop("details_json"))
    return result


def _details(value: Mapping[str, Any], *, partial=False) -> dict:
    if not isinstance(value, Mapping):
        raise ContractError("interview details must be an object")
    allowed = {
        "round_id",
        "round_kind",
        "status",
        "starts_at",
        "ends_at",
        "time_zone",
        "participants",
        "location",
        "join_url",
        "source_at",
        "evidence_id",
        "calendar_account_id",
        "calendar_event_id",
        "calendar_uid",
        "calendar_modified_at",
        "calendar_change_key",
        "organizer",
        "employer_confirmed",
        "availability",
        "availability_conflicts",
        "note",
    }
    if set(value) - allowed:
        raise ContractError("unknown interview detail fields")
    out = dict(value)
    state = out.setdefault("status", "proposed")
    if state not in STATES:
        raise ContractError("invalid interview status")
    for key in (
        "round_id",
        "round_kind",
        "location",
        "evidence_id",
        "calendar_account_id",
        "calendar_event_id",
        "calendar_uid",
        "calendar_change_key",
        "organizer",
        "note",
    ):
        if key in out and (not isinstance(out[key], str) or len(out[key]) > 2000):
            raise ContractError("invalid interview text field: " + key)
    out.setdefault("round_kind", "interview")
    out.setdefault("time_zone", "UTC")
    try:
        ZoneInfo(out["time_zone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ContractError("invalid interview time zone") from exc
    if not partial and (state in ACTIVE or state == "proposed"):
        if not out.get("starts_at") or not out.get("ends_at"):
            raise ContractError("scheduled interview requires start and end")
    for key in ("starts_at", "ends_at", "source_at", "calendar_modified_at"):
        if out.get(key):
            out[key] = _stamp(out[key], subsecond=key == "calendar_modified_at")
    if out.get("starts_at") and out.get("ends_at"):
        if parse_utc(out["ends_at"]) <= parse_utc(out["starts_at"]):
            raise ContractError("interview end must follow start")
        if parse_utc(out["ends_at"]) - parse_utc(out["starts_at"]) > timedelta(days=14):
            raise ContractError("interview duration exceeds calendar read bound")
    people = out.setdefault("participants", [])
    if (
        not isinstance(people, list)
        or len(people) > 100
        or any(not isinstance(p, str) or len(p) > 500 for p in people)
    ):
        raise ContractError("invalid interview participants")
    if out.get("join_url"):
        parsed = urlsplit(out["join_url"])
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ContractError("invalid interview join URL")
    if "employer_confirmed" in out and not isinstance(out["employer_confirmed"], bool):
        raise ContractError("employer confirmation must be a boolean")
    # These values are computed at decision time and cannot be supplied as proof.
    out.pop("availability", None)
    out.pop("availability_conflicts", None)
    return out


class InterviewMixin:
    def propose_interview_revision(
        self, application_id: str, details: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        if not isinstance(details, Mapping):
            raise ContractError("interview details must be an object")
        values = _details(details, partial=bool(details.get("round_id")))
        request = {
            "application_id": application_id,
            "details": values,
            "actor_kind": context.actor_kind,
            "source_kind": context.source_kind,
            "source_ref": context.source_ref,
        }

        def operation(con, stamp):
            self.store._application(con, application_id)
            round_id = values.get("round_id") or _id()
            current = con.execute(
                "SELECT * FROM interview_rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            if current and current["application_id"] != application_id:
                raise ContractError("interview belongs to another application")
            if values.get("round_id") and not current:
                raise ContractError("interview round was not found")
            full_details = _details(
                {
                    **(json.loads(current["details_json"]) if current else {}),
                    **dict(details),
                    "source_at": details.get("source_at") or stamp,
                }
            )
            self._validate_interview_evidence(
                con, application_id, full_details.get("evidence_id")
            )
            if not current:
                con.execute(
                    "INSERT INTO interview_rounds (round_id,application_id,round_kind,status,created_at,updated_at) VALUES (?,?,?,'proposed',?,?)",
                    (round_id, application_id, values["round_kind"], stamp, stamp),
                )
            revision = self._insert_interview_revision(
                con, round_id, application_id, full_details, context, stamp
            )
            return {"revision": revision, "round_id": round_id}

        return self.store._idempotent(
            "propose_interview_revision", context, request, operation
        )

    def _insert_interview_revision(
        self, con, round_id, application_id, details, context, stamp
    ):
        base = con.execute(
            "SELECT current_revision_id,status FROM interview_rounds WHERE round_id=?",
            (round_id,),
        ).fetchone()
        base_revision_id = base["current_revision_id"]
        dedupe = payload_sha256(
            {
                "round_id": round_id,
                "base_revision_id": base_revision_id,
                "base_round_status": base["status"],
                "details": details,
                "source_kind": context.source_kind,
                "source_ref": context.source_ref,
            }
        )
        previous = con.execute(
            "SELECT * FROM interview_revisions WHERE dedupe_key=?", (dedupe,)
        ).fetchone()
        if previous:
            return _row(previous)
        revision_id = _id()
        con.execute(
            "INSERT INTO interview_revisions (revision_id,round_id,application_id,dedupe_key,details_json,base_revision_id,base_round_status,status,source_kind,source_ref,source_at,created_at) VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?)",
            (
                revision_id,
                round_id,
                application_id,
                dedupe,
                canonical_json(details),
                base_revision_id,
                base["status"],
                context.source_kind,
                context.source_ref,
                details.get("source_at") or stamp,
                stamp,
            ),
        )
        return _row(
            con.execute(
                "SELECT * FROM interview_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
        )

    def _validate_interview_evidence(self, con, application_id, evidence_id):
        if not evidence_id:
            return
        if hasattr(self, "_evidence"):
            self._evidence(con, application_id, evidence_id)
            return
        # Supports isolated interview integration without the task composition.
        associated = con.execute(
            "SELECT 1 FROM event_proposals WHERE evidence_id=? AND proposed_application_id=?",
            (evidence_id, application_id),
        ).fetchone()
        if not associated:
            raise ContractError(
                "interview evidence is not associated with this application"
            )

    def _interview_availability(self, details):
        calendar = getattr(self, "calendar", None)
        if calendar is None:
            return {"status": "not_checked", "conflicts": []}
        blocks = calendar.read_calendar_view(details["starts_at"], details["ends_at"])
        conflicts = [
            b.remote_id
            for b in blocks
            if not b.is_cancelled
            and b.show_as != "free"
            and b.remote_id != details.get("calendar_event_id")
            and parse_utc(b.starts_at) < parse_utc(details["ends_at"])
            and parse_utc(b.ends_at) > parse_utc(details["starts_at"])
        ]
        return {"status": "checked", "conflicts": conflicts, "checked_at": utc_now()}

    def decide_interview_revision(
        self, revision_id: str, decision: str, reason: str, context: MutationContext
    ) -> Mapping[str, Any]:
        if context.actor_kind != "user":
            raise ContractError("interview decisions require actor_kind=user")
        if decision not in ("accepted", "rejected"):
            raise ContractError("invalid interview decision")
        # Network reads stay outside the SQLite transaction. A failed read fails
        # the decision, preserving its pending review and current reminders.
        with connect(self.store.db_path) as con:
            row = con.execute(
                "SELECT * FROM interview_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if not row:
                raise ContractError("interview revision was not found")
            details = json.loads(row["details_json"])
        availability = (
            self._interview_availability(details)
            if decision == "accepted" and details["status"] in ACTIVE
            else {"status": "not_applicable", "conflicts": []}
        )

        def operation(con, stamp):
            return self._decide_interview_revision(
                con, revision_id, decision, reason, context, stamp, availability
            )

        return self.store._idempotent(
            "decide_interview_revision",
            context,
            {
                "revision_id": revision_id,
                "decision": decision,
                "reason": str(reason)[:1000],
            },
            operation,
        )

    def _decide_interview_revision(
        self,
        con,
        revision_id,
        decision,
        reason,
        context,
        stamp,
        availability,
        *,
        version_conflict=False,
        record_confirmed_conflicts=False,
    ):
        revision = con.execute(
            "SELECT * FROM interview_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if not revision:
            raise ContractError("interview revision was not found")
        if revision["status"] not in ("pending", "conflict"):
            return {"revision": _row(revision), "applied": False}
        current = con.execute(
            "SELECT * FROM interview_rounds WHERE round_id=?", (revision["round_id"],)
        ).fetchone()
        details = json.loads(revision["details_json"])
        result_status = decision
        if decision == "accepted":
            self._validate_interview_evidence(
                con, current["application_id"], details.get("evidence_id")
            )
            application = self.store._application(con, current["application_id"])
            if (
                application["current_phase"] == "terminal"
                and details["status"] in ACTIVE
            ):
                result_status = "conflict"
            modified = details.get("calendar_modified_at", "")
            previous_modified = current["calendar_modified_at"]
            if modified and previous_modified:
                if parse_utc(modified) < parse_utc(previous_modified):
                    result_status = "stale"
                elif (
                    parse_utc(modified) == parse_utc(previous_modified)
                    and details.get("calendar_change_key")
                    != current["calendar_change_key"]
                    and context.actor_kind != "user"
                ):
                    result_status = "conflict"
            if details.get("calendar_account_id") and details.get("calendar_event_id"):
                duplicate = con.execute(
                    "SELECT 1 FROM interview_rounds WHERE calendar_account_id=? "
                    "AND calendar_event_id=? AND round_id<>?",
                    (
                        details["calendar_account_id"],
                        details["calendar_event_id"],
                        current["round_id"],
                    ),
                ).fetchone()
                if duplicate:
                    result_status = "conflict"
            if version_conflict:
                result_status = "conflict"
            if context.actor_kind == "user" and (
                revision["base_revision_id"] != current["current_revision_id"]
                or revision["base_round_status"] != current["status"]
            ):
                result_status = "stale"
            if result_status == "accepted" and details["status"] in ACTIVE:
                overlap = con.execute(
                    "SELECT 1 FROM interview_rounds WHERE status IN ('confirmed','rescheduled') AND round_id<>? AND julianday(starts_at)<julianday(?) AND julianday(ends_at)>julianday(?) LIMIT 1",
                    (current["round_id"], details["ends_at"], details["starts_at"]),
                ).fetchone()
                legacy_overlap = con.execute(
                    "SELECT 1 FROM accepted_interview_schedules WHERE status='active' AND interview_schedule_id<>? AND julianday(starts_at)<julianday(?) AND julianday(ends_at)>julianday(?) LIMIT 1",
                    (
                        current["legacy_schedule_id"] or "",
                        details["ends_at"],
                        details["starts_at"],
                    ),
                ).fetchone()
                if (overlap or legacy_overlap or availability["conflicts"]) and not record_confirmed_conflicts:
                    result_status = "conflict"
        con.execute(
            "UPDATE interview_revisions SET status=?,decided_at=?,decision_reason=? WHERE revision_id=?",
            (result_status, stamp, str(reason)[:1000], revision_id),
        )
        con.execute(
            "INSERT INTO interview_revision_decisions (decision_id,revision_id,decision,resulting_status,actor_kind,reason,availability_json,decided_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                _id(),
                revision_id,
                decision,
                result_status,
                context.actor_kind,
                str(reason)[:1000],
                canonical_json(availability),
                stamp,
            ),
        )
        if result_status == "rejected" and not current["current_revision_id"]:
            con.execute(
                "UPDATE interview_rounds SET status='cancelled',updated_at=? WHERE round_id=?",
                (stamp, current["round_id"]),
            )
        if result_status == "accepted":
            if details["status"] in ACTIVE:
                self._supersede_interview_availability(
                    con,
                    current["application_id"],
                    details.get("evidence_id"),
                    context,
                    stamp,
                )
            same_schedule = (
                current["status"] in ACTIVE
                and details["status"] in ACTIVE
                and parse_utc(current["starts_at"]) == parse_utc(details["starts_at"])
                and parse_utc(current["ends_at"]) == parse_utc(details["ends_at"])
            )
            # Replacing time, cancelling, or completing is one transaction with
            # dismissing superseded reminders and resolving the attendance task.
            # Merely linking the calendar or updating its location must not
            # issue another scheduled notification or reset the attendance task.
            if not same_schedule:
                self._retire_interview_prompts(
                    con, current, details["status"], context, stamp
                )
            task_id = current["task_id"] if same_schedule else None
            if details["status"] in ACTIVE and not same_schedule:
                self.store._append_event(
                    con,
                    current["application_id"],
                    ApplicationEventType.INTERVIEW_SCHEDULED,
                    revision["source_at"],
                    {
                        "round_id": current["round_id"],
                        "revision_id": revision_id,
                        "starts_at": details["starts_at"],
                        "ends_at": details["ends_at"],
                        "time_zone": details["time_zone"],
                    },
                    "interview-revision:" + revision_id,
                    context,
                    stamp,
                )
                self.store._project(con, current["application_id"])
                if hasattr(self, "_create_task"):
                    task = self._create_task(
                        con,
                        current["application_id"],
                        {
                            "kind": "attend_interview",
                            "owner": "applicant",
                            "note": details.get("note", "Interview"),
                            "due_at": details["starts_at"],
                            "source_time": revision["source_at"],
                            "evidence_id": details.get("evidence_id", ""),
                        },
                        context,
                        stamp,
                    )
                    task_id = task["task_id"]
                # Historical imports never produce immediately overdue alerts.
                for kind, delta in (
                    ("interview_24h", timedelta(hours=24)),
                    ("interview_1h", timedelta(hours=1)),
                ):
                    due = parse_utc(details["starts_at"]) - delta
                    if due > parse_utc(stamp):
                        con.execute(
                            "INSERT INTO interview_reminders (reminder_id,round_id,revision_id,application_id,kind,due_at,status,created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                            (
                                _id(),
                                current["round_id"],
                                revision_id,
                                current["application_id"],
                                kind,
                                due.isoformat(timespec="seconds").replace(
                                    "+00:00", "Z"
                                ),
                                stamp,
                            ),
                        )
            merged = {**json.loads(current["details_json"]), **details}
            con.execute(
                "UPDATE interview_rounds SET round_kind=?,status=?,starts_at=?,ends_at=?,time_zone=?,details_json=?,calendar_account_id=?,calendar_event_id=?,calendar_uid=?,calendar_modified_at=?,calendar_change_key=?,current_revision_id=?,task_id=?,updated_at=? WHERE round_id=?",
                (
                    merged["round_kind"],
                    merged["status"],
                    merged.get("starts_at", current["starts_at"]),
                    merged.get("ends_at", current["ends_at"]),
                    merged["time_zone"],
                    canonical_json(merged),
                    merged.get("calendar_account_id", current["calendar_account_id"]),
                    merged.get("calendar_event_id", current["calendar_event_id"]),
                    merged.get("calendar_uid", current["calendar_uid"]),
                    merged.get("calendar_modified_at", current["calendar_modified_at"]),
                    merged.get("calendar_change_key", current["calendar_change_key"]),
                    revision_id,
                    task_id,
                    stamp,
                    current["round_id"],
                ),
            )
        saved = _row(
            con.execute(
                "SELECT * FROM interview_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
        )
        return {
            "revision": saved,
            "applied": result_status == "accepted",
            "availability": availability,
            "round": _row(
                con.execute(
                    "SELECT * FROM interview_rounds WHERE round_id=?",
                    (current["round_id"],),
                ).fetchone()
            ),
        }

    def _supersede_interview_availability(
        self, con, application_id, evidence_id, context, stamp
    ):
        if not evidence_id or not hasattr(self, "_transition_task"):
            return
        candidates = con.execute(
            "SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND kind='send_availability' "
            "AND status='open' AND evidence_id=?",
            (application_id, evidence_id),
        ).fetchall()
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='lifecycle_mail_observations'"
        ).fetchone():
            candidates = (
                con.execute(
                    "SELECT DISTINCT t.task_id FROM lifecycle_tasks t "
                    "JOIN lifecycle_mail_observations old ON old.evidence_id=t.evidence_id "
                    "JOIN lifecycle_mail_observations new ON new.evidence_id=? "
                    "AND new.account_id=old.account_id AND new.conversation_ref=old.conversation_ref "
                    "WHERE t.application_id=? AND t.kind='send_availability' AND t.status='open' "
                    "AND old.conversation_ref<>''",
                    (evidence_id, application_id),
                ).fetchall()
                or candidates
            )
        if len(candidates) == 1:
            self._transition_task(
                con,
                candidates[0]["task_id"],
                "supersede",
                {
                    "reason": "A reviewed interview now records the time",
                    "evidence_id": evidence_id,
                },
                context,
                stamp,
            )

    def _retire_interview_prompts(self, con, current, new_state, context, stamp):
        # A completed reminder can still be waiting in the durable outbox. The
        # same transaction cancels those queued deliveries and invalidates any
        # claimed lease; already delivered notifications remain historical facts.
        con.execute(
            "UPDATE notification_outbox SET status='cancelled',lease_owner=NULL,"
            "lease_token=NULL,lease_expires_at=NULL WHERE topic='reminder.due' "
            "AND status IN ('pending','delivering') AND json_extract(context_json,'$.reminder_id') IN "
            "(SELECT 'interview:'||reminder_id FROM interview_reminders WHERE round_id=? "
            "UNION SELECT reminder_id FROM local_reminders WHERE interview_schedule_id=?)",
            (current["round_id"], current["legacy_schedule_id"] or ""),
        )
        con.execute(
            "UPDATE interview_reminders SET status='dismissed',completed_at=? WHERE round_id=? AND status='pending'",
            (stamp, current["round_id"]),
        )
        if current["legacy_schedule_id"]:
            con.execute(
                "UPDATE local_reminders SET status='dismissed',completed_at=? WHERE interview_schedule_id=? AND status='pending'",
                (stamp, current["legacy_schedule_id"]),
            )
            con.execute(
                "UPDATE accepted_interview_schedules SET status=? WHERE interview_schedule_id=?",
                (
                    "completed" if new_state == "completed" else "cancelled",
                    current["legacy_schedule_id"],
                ),
            )
        if current["task_id"] and hasattr(self, "_transition_task"):
            task = con.execute(
                "SELECT status FROM lifecycle_tasks WHERE task_id=?",
                (current["task_id"],),
            ).fetchone()
            if task and task["status"] not in ("completed", "cancelled", "superseded"):
                self._transition_task(
                    con,
                    current["task_id"],
                    "complete" if new_state == "completed" else "cancel",
                    {"reason": "Interview " + new_state},
                    context,
                    stamp,
                )

    def list_interview_rounds(
        self,
        *,
        application_id=None,
        statuses=None,
        starts_after=None,
        starts_before=None,
        limit=100,
        offset=0,
    ):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 100000
        ):
            raise ContractError("invalid interview page")
        selected = tuple(STATES if statuses is None else statuses)
        if not selected or any(s not in STATES for s in selected):
            raise ContractError("invalid interview statuses")
        clauses = ["status IN (" + ",".join("?" for _ in selected) + ")"]
        params = list(selected)
        if application_id:
            clauses.append("application_id=?")
            params.append(application_id)
        for value, op in ((starts_after, ">="), (starts_before, "<")):
            if value:
                clauses.append("julianday(starts_at)" + op + "julianday(?)")
                params.append(_stamp(value))
        with connect(self.store.db_path) as con:
            rows = con.execute(
                "SELECT * FROM interview_rounds WHERE "
                + " AND ".join(clauses)
                + " ORDER BY starts_at,round_id LIMIT ? OFFSET ?",
                (*params, limit + 1, offset),
            ).fetchall()
        return {
            "rounds": [_row(r) for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def list_interview_revisions(
        self,
        *,
        application_id=None,
        statuses=("pending", "conflict"),
        limit=100,
        offset=0,
    ):
        if (
            not 1 <= limit <= 500
            or not 0 <= offset <= 100000
            or not statuses
            or any(
                s not in ("pending", "accepted", "rejected", "stale", "conflict")
                for s in statuses
            )
        ):
            raise ContractError("invalid interview revision query")
        clauses = ["status IN (" + ",".join("?" for _ in statuses) + ")"]
        params = list(statuses)
        if application_id:
            clauses.append("application_id=?")
            params.append(application_id)
        with connect(self.store.db_path) as con:
            rows = con.execute(
                "SELECT * FROM interview_revisions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at,revision_id LIMIT ? OFFSET ?",
                (*params, limit + 1, offset),
            ).fetchall()
        return {
            "revisions": [_row(r) for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def list_due_interview_reminders(self, now, *, limit=100):
        if not 1 <= limit <= 500:
            raise ContractError("invalid interview reminder limit")
        with connect(self.store.db_path) as con:
            return tuple(
                dict(r)
                for r in con.execute(
                    "SELECT * FROM interview_reminders WHERE status='pending' AND julianday(due_at)<=julianday(?) ORDER BY due_at,reminder_id LIMIT ?",
                    (_stamp(now), limit),
                )
            )

    def complete_interview_reminder(self, reminder_id, resolution, context):
        if resolution not in ("completed", "dismissed"):
            raise ContractError("invalid interview reminder resolution")

        def operation(con, stamp):
            row = con.execute(
                "SELECT * FROM interview_reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
            if not row:
                raise ContractError("interview reminder was not found")
            if resolution == "dismissed":
                con.execute(
                    "UPDATE notification_outbox SET status='cancelled',lease_owner=NULL,"
                    "lease_token=NULL,lease_expires_at=NULL WHERE topic='reminder.due' "
                    "AND status IN ('pending','delivering') "
                    "AND json_extract(context_json,'$.reminder_id')=?",
                    ("interview:" + reminder_id,),
                )
            if row["status"] == "pending" or resolution == "dismissed":
                con.execute(
                    "UPDATE interview_reminders SET status=?,completed_at=? WHERE reminder_id=?",
                    (resolution, stamp, reminder_id),
                )
            return dict(
                con.execute(
                    "SELECT * FROM interview_reminders WHERE reminder_id=?",
                    (reminder_id,),
                ).fetchone()
            )

        return self.store._idempotent(
            "complete_interview_reminder",
            context,
            {"reminder_id": reminder_id, "resolution": resolution},
            operation,
        )

    def import_accepted_schedule(self, schedule_id, context):
        def operation(con, stamp):
            prior = con.execute(
                "SELECT * FROM interview_rounds WHERE legacy_schedule_id=?",
                (schedule_id,),
            ).fetchone()
            if prior:
                return {"round": _row(prior), "created": False}
            row = con.execute(
                "SELECT * FROM accepted_interview_schedules WHERE interview_schedule_id=?",
                (schedule_id,),
            ).fetchone()
            if not row:
                raise ContractError("accepted schedule was not found")
            round_id = _id()
            state = {
                "active": "confirmed",
                "cancelled": "cancelled",
                "completed": "completed",
            }[row["status"]]
            details = {
                "round_kind": "interview",
                "status": state,
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "time_zone": row["time_zone"],
                "source_at": row["created_at"],
                "note": "Imported reviewed email schedule; calendar invitation not yet linked",
            }
            con.execute(
                "INSERT INTO interview_rounds (round_id,application_id,status,starts_at,ends_at,time_zone,details_json,legacy_schedule_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    round_id,
                    row["application_id"],
                    state,
                    row["starts_at"],
                    row["ends_at"],
                    row["time_zone"],
                    canonical_json(details),
                    schedule_id,
                    row["created_at"],
                    stamp,
                ),
            )
            revision = self._insert_interview_revision(
                con, round_id, row["application_id"], details, context, stamp
            )
            con.execute(
                "UPDATE interview_revisions SET status='accepted',decided_at=?,decision_reason='Imported existing user decision' WHERE revision_id=?",
                (stamp, revision["revision_id"]),
            )
            con.execute(
                "UPDATE interview_rounds SET current_revision_id=? WHERE round_id=?",
                (revision["revision_id"], round_id),
            )
            if (
                state in ACTIVE
                and parse_utc(row["starts_at"]) > parse_utc(stamp)
                and hasattr(self, "_create_task")
            ):
                application = self.store._application(con, row["application_id"])
                if application["current_phase"] != "terminal":
                    task = self._create_task(
                        con,
                        row["application_id"],
                        {
                            "kind": "attend_interview",
                            "owner": "applicant",
                            "note": "Attend reviewed interview",
                            "due_at": row["starts_at"],
                            "source_time": row["created_at"],
                        },
                        context,
                        stamp,
                    )
                    con.execute(
                        "UPDATE interview_rounds SET task_id=? WHERE round_id=?",
                        (task["task_id"], round_id),
                    )
            # Existing local reminders remain authoritative until a later revision.
            return {
                "round": _row(
                    con.execute(
                        "SELECT * FROM interview_rounds WHERE round_id=?", (round_id,)
                    ).fetchone()
                ),
                "created": True,
            }

        return self.store._idempotent(
            "import_accepted_schedule", context, {"schedule_id": schedule_id}, operation
        )

    def reconcile_calendar_events(
        self,
        account_id: str,
        events: Sequence[Mapping[str, Any]],
        context: MutationContext,
    ):
        if context.actor_kind != "system" or context.source_kind != "outlook_calendar":
            raise ContractError("calendar reconciliation requires the calendar worker")
        if not account_id or len(events) > 500:
            raise ContractError("invalid calendar reconciliation batch")
        results = []
        unmatched = 0
        # Each event commits separately and has a stable version key so retries
        # are safe even when the enclosing worker crashes partway through.
        for event in events:
            event_id = event.get("remote_id")
            uid = event.get("ical_uid", "")
            if (
                not event_id
                or not event.get("modified_at")
                or not event.get("change_key")
            ):
                results.append({"status": "insufficient_identity"})
                continue
            with connect(self.store.db_path) as con:
                rows = con.execute(
                    "SELECT * FROM interview_rounds WHERE calendar_account_id=? AND (calendar_event_id=? OR (calendar_uid<>'' AND calendar_uid=?))",
                    (account_id, event_id, uid),
                ).fetchall()
            if len(rows) != 1:
                unmatched += 1
                continue
            current = rows[0]
            details = json.loads(current["details_json"])
            # An organizer or UID change is never silently attached to a round.
            identity_matches = (
                current["calendar_event_id"] == event_id
                and (not current["calendar_uid"] or current["calendar_uid"] == uid)
                and details.get("organizer", "").casefold()
                == str(event.get("organizer", "")).casefold()
            )
            state = (
                "cancelled"
                if event.get("is_cancelled")
                else (
                    "rescheduled"
                    if current["starts_at"]
                    and parse_utc(current["starts_at"]) != parse_utc(event["starts_at"])
                    else "confirmed"
                )
            )
            incoming = _details(
                {
                    **details,
                    "round_id": current["round_id"],
                    "status": state,
                    "starts_at": event["starts_at"],
                    "ends_at": event["ends_at"],
                    "time_zone": "UTC",
                    "calendar_account_id": account_id,
                    "calendar_event_id": event_id,
                    "calendar_uid": uid,
                    "calendar_modified_at": event["modified_at"],
                    "calendar_change_key": event["change_key"],
                    "source_at": event["modified_at"],
                    "organizer": event.get("organizer", ""),
                    "participants": event.get("participants", []),
                    "location": event.get("location", ""),
                    "join_url": event.get("join_url", ""),
                    "employer_confirmed": event.get("is_organizer") is False,
                }
            )
            same_key = current["calendar_change_key"] == event["change_key"]
            same_content = all(
                incoming.get(key, default) == details.get(key, default)
                for key, default in (
                    ("starts_at", ""),
                    ("ends_at", ""),
                    ("organizer", ""),
                    ("participants", []),
                    ("location", ""),
                    ("join_url", ""),
                )
            ) and bool(event.get("is_cancelled")) == (current["status"] == "cancelled")
            if same_key and same_content and identity_matches:
                results.append({"round_id": current["round_id"], "status": "unchanged"})
                continue
            key = "calendar-revision:" + payload_sha256(
                {
                    "account": account_id,
                    "id": event_id,
                    "version": event["change_key"],
                    "modified": event["modified_at"],
                    "payload": dict(event),
                }
            )
            event_context = MutationContext(key, "system", "outlook_calendar", event_id)
            availability = (
                self._interview_availability(incoming)
                if state in ACTIVE
                else {"status": "not_applicable", "conflicts": []}
            )

            def operation(con, stamp):
                # Identity may have been corrected while the network read ran.
                # Recheck the accepted link inside the mutation transaction.
                latest = con.execute(
                    "SELECT * FROM interview_rounds WHERE round_id=?",
                    (current["round_id"],),
                ).fetchone()
                latest_details = json.loads(latest["details_json"])
                link_still_matches = (
                    latest["calendar_account_id"] == account_id
                    and latest["calendar_event_id"] == event_id
                    and (not latest["calendar_uid"] or latest["calendar_uid"] == uid)
                    and latest_details.get("organizer", "").casefold()
                    == str(event.get("organizer", "")).casefold()
                )
                revision = self._insert_interview_revision(
                    con,
                    current["round_id"],
                    current["application_id"],
                    incoming,
                    event_context,
                    stamp,
                )
                older = bool(latest["calendar_modified_at"]) and parse_utc(
                    incoming["calendar_modified_at"]
                ) < parse_utc(latest["calendar_modified_at"])
                if (
                    not identity_matches
                    or not link_still_matches
                    or event.get("is_organizer") is not False
                    or not event.get("organizer")
                    or (
                        latest["status"] in ("cancelled", "completed")
                        and state != "cancelled"
                        and not older
                    )
                ):
                    return {
                        "revision": revision,
                        "applied": False,
                        "reason": "identity_or_reopening_requires_review",
                    }
                return self._decide_interview_revision(
                    con,
                    revision["revision_id"],
                    "accepted",
                    "Observed update to reviewed calendar identity",
                    event_context,
                    stamp,
                    availability,
                    version_conflict=same_key and not same_content,
                )

            result = self.store._idempotent(
                "reconcile_interview_calendar",
                event_context,
                {"event": dict(event)},
                operation,
            )
            results.append(result)
        return {"processed": len(events), "unmatched": unmatched, "results": results}

    def list_calendar_interview_links(self, account_id, *, after="", limit=100):
        if not 1 <= limit <= 100:
            raise ContractError("invalid calendar link page")
        with connect(self.store.db_path) as con:
            rows = con.execute(
                "SELECT round_id,calendar_event_id FROM interview_rounds WHERE calendar_account_id=? AND calendar_event_id<>'' AND status<>'completed' AND round_id>? ORDER BY round_id LIMIT ?",
                (account_id, after, limit + 1),
            ).fetchall()
        return {
            "links": [dict(r) for r in rows[:limit]],
            "next_after": rows[limit - 1]["round_id"] if len(rows) > limit else None,
        }

    def discover_calendar_interviews(self, account_id, events, context):
        """Propose initial identity links using exact time; never auto-enroll.

        An exact organizer match is required when the round already records one.
        Ambiguous equal-time candidates are separate reviews; accepting one makes
        other pending revisions stale through their recorded base revision.
        """
        if context.actor_kind != "system" or context.source_kind != "outlook_calendar":
            raise ContractError("calendar discovery requires the calendar worker")
        if not account_id or len(events) > 500:
            raise ContractError("invalid calendar discovery batch")
        proposals = []
        for event in events:
            if (
                event.get("is_organizer") is not False
                or event.get("is_cancelled")
                or not event.get("organizer")
            ):
                continue
            if not all(
                event.get(k)
                for k in (
                    "remote_id",
                    "change_key",
                    "modified_at",
                    "starts_at",
                    "ends_at",
                )
            ):
                continue
            with connect(self.store.db_path) as con:
                rounds = con.execute(
                    "SELECT * FROM interview_rounds WHERE calendar_event_id='' AND status IN ('proposed','confirmed','rescheduled') AND julianday(starts_at)=julianday(?) AND julianday(ends_at)=julianday(?) ORDER BY round_id LIMIT 101",
                    (event["starts_at"], event["ends_at"]),
                ).fetchall()
            if len(rounds) > 100:
                raise ContractError("calendar discovery candidate bound exceeded")
            for row in rounds:
                current = json.loads(row["details_json"])
                if (
                    current.get("organizer")
                    and current["organizer"].casefold() != event["organizer"].casefold()
                ):
                    continue
                incoming = {
                    **current,
                    "round_id": row["round_id"],
                    "status": "confirmed",
                    "starts_at": event["starts_at"],
                    "ends_at": event["ends_at"],
                    "calendar_account_id": account_id,
                    "calendar_event_id": event["remote_id"],
                    "calendar_uid": event.get("ical_uid", ""),
                    "calendar_modified_at": event["modified_at"],
                    "calendar_change_key": event["change_key"],
                    "organizer": event["organizer"],
                    "participants": event.get("participants", []),
                    "location": event.get("location", ""),
                    "join_url": event.get("join_url", ""),
                    "employer_confirmed": True,
                    "source_at": event["modified_at"],
                    "note": "Review calendar identity: exact interview time match; no invitation response will be sent.",
                }
                key = "calendar-link:" + payload_sha256(
                    {
                        "account": account_id,
                        "round": row["round_id"],
                        "base": row["current_revision_id"],
                        "event": dict(event),
                    }
                )
                proposal = self.propose_interview_revision(
                    row["application_id"],
                    incoming,
                    MutationContext(
                        key, "system", "outlook_calendar", event["remote_id"]
                    ),
                )
                proposals.append(proposal)
        return {
            "proposals": proposals,
            "count": len(proposals),
            "review_required": True,
        }

    def import_pending_legacy_interviews(self, context, *, limit=50):
        """Bring previously reviewed active schedules into round reconciliation."""
        if not 1 <= limit <= 100:
            raise ContractError("invalid legacy interview import bound")
        with connect(self.store.db_path) as con:
            rows = con.execute(
                "SELECT s.interview_schedule_id FROM accepted_interview_schedules s LEFT JOIN interview_rounds r ON r.legacy_schedule_id=s.interview_schedule_id WHERE s.status='active' AND r.round_id IS NULL ORDER BY s.starts_at,s.interview_schedule_id LIMIT ?",
                (limit + 1,),
            ).fetchall()
        imported = []
        for row in rows[:limit]:
            schedule_id = row["interview_schedule_id"]
            imported.append(
                self.import_accepted_schedule(
                    schedule_id,
                    MutationContext(
                        "import-calendar-schedule:" + schedule_id,
                        context.actor_kind,
                        context.source_kind,
                        schedule_id,
                    ),
                )
            )
        return {"imported": len(imported), "has_more": len(rows) > limit}
