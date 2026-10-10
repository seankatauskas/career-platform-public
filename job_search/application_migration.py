"""Offline legacy snapshot conversion into a separate, paused application candidate.

No provider/configuration discovery occurs here. The input is an explicitly supplied
SQLite snapshot. Original SQL rows, BLOBs, receipts and historical encodings remain
in a read-only predecessor database; current state crosses public owner imports.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .commands import CommandContext, DomainError, Principal, encode, digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_manifest (
 id INTEGER PRIMARY KEY CHECK(id=1), report_json TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_mappings (
 source_table TEXT NOT NULL, source_id TEXT NOT NULL, target_owner TEXT NOT NULL,
 target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
 PRIMARY KEY(source_table,source_id,target_owner,target_kind,target_id)
);
CREATE TABLE IF NOT EXISTS migration_issues (
 id INTEGER PRIMARY KEY, source_table TEXT NOT NULL, source_id TEXT NOT NULL,
 code TEXT NOT NULL, severity TEXT NOT NULL
);
"""

ARCHIVE_NAME = "predecessor.sqlite"
CANDIDATE_NAME = "candidate.sqlite"
REPORT_NAME = "conversion-report.json"


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def _readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def _json(value):
    """Lossless JSON representation for archive queries, including binary fields."""
    if isinstance(value, bytes):
        return {"$sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _tables(connection):
    return [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _inventory(connection):
    counts, hashes = {}, {}
    for table in _tables(connection):
        columns = [row[1] for row in connection.execute("PRAGMA table_info(" + _identifier(table) + ")")]
        hashed = hashlib.sha256()
        count = 0
        order = ",".join(_identifier(column) for column in columns)
        for row in connection.execute("SELECT * FROM " + _identifier(table) + " ORDER BY " + order):
            hashed.update(encode(_json(dict(row))).encode("utf-8") + b"\n")
            count += 1
        counts[table], hashes[table] = count, hashed.hexdigest()
    return {"counts": counts, "table_sha256": hashes, "logical_sha256": digest({"counts": counts, "hashes": hashes})}


def historical_rows(destination_directory, table, *, after=0, limit=100):
    """Bounded exact predecessor rows. No arbitrary SQL or executable old writer.

    Binary values are explicit base64 objects, never decoded as text. Table names
    are checked against the preserved schema. The read-only file remains the
    authoritative historical archive; this query changes no candidate records.
    """
    if type(limit) is not int or not 1 <= limit <= 200 or type(after) is not int or after < 0:
        raise DomainError("invalid_input", "Invalid historical query bound")
    with _readonly(Path(destination_directory) / ARCHIVE_NAME) as con:
        if table not in _tables(con):
            raise DomainError("not_found", "Historical table not found")
        columns = [row[1] for row in con.execute("PRAGMA table_info(" + _identifier(table) + ")")]
        order = ",".join(_identifier(column) for column in columns)
        rows = con.execute("SELECT * FROM " + _identifier(table) + " ORDER BY " + order + " LIMIT ? OFFSET ?", (limit + 1, after)).fetchall()
        return {"table": table, "items": [_json(dict(row)) for row in rows[:limit]],
                "next_cursor": after + limit if len(rows) > limit else None, "read_only": True}


class _Conversion:
    def __init__(self, source, runtime, source_hash):
        self.source, self.runtime, self.source_hash = source, runtime, source_hash
        self.tables = set(_tables(source))
        self.cache, self.mapping, self.issues = {}, [], []
        self.apps, self.imported = {}, defaultdict(set)
        self.now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def rows(self, table):
        if table not in self.cache:
            self.cache[table] = [dict(row) for row in self.source.execute("SELECT * FROM " + _identifier(table))] if table in self.tables else []
        return self.cache[table]

    def issue(self, table, identity, code, severity="blocker"):
        value = {"source_table": table, "source_id": str(identity), "code": code, "severity": severity}
        if value not in self.issues:
            self.issues.append(value)

    def mapped(self, table, identity, owner, kind, target):
        self.mapping.append({"source_table": table, "source_id": str(identity), "target_owner": owner,
                             "target_kind": kind, "target_id": str(target)})

    def attempt(self, tx, table, identity, callback):
        tx.connection.execute("SAVEPOINT conversion_record")
        try:
            result = callback()
        except (DomainError, ValueError, TypeError, KeyError, sqlite3.DatabaseError) as exc:
            tx.connection.execute("ROLLBACK TO conversion_record")
            self.issue(table, identity, getattr(exc, "code", "invalid_legacy_record"))
            result = None
        finally:
            tx.connection.execute("RELEASE conversion_record")
        return result

    def record(self, tx, table, row, kind, identity, status, data, *, application_id=None, version=None):
        app_id = application_id or row.get("application_id")
        if app_id not in self.apps:
            self.issue(table, identity, "missing_application")
            return None
        if identity in self.imported[kind]:
            self.issue(table, identity, "duplicate_target_identity")
            return None
        value = {**data, "id": identity, "application_id": app_id, "pursuit_no": 1,
                 "status": status, "version": version or max(1, row.get("revision_no", 1)),
                 "created_at": row.get("created_at") or row.get("recorded_at") or self.apps[app_id]["started_at"],
                 "updated_at": row.get("updated_at") or row.get("recorded_at") or row.get("created_at") or self.apps[app_id]["updated_at"]}
        result = self.attempt(tx, table, identity, lambda: self.runtime.applications.import_record(tx, kind, value,
            {"snapshot": self.source_hash, "table": table, "source_id": identity, "historically_accepted": True}))
        if result is not None:
            self.imported[kind].add(identity)
            self.mapped(table, identity, "applications", kind, result["id"])
        return result

    def applications(self, tx):
        identities = set()
        for row in self.rows("applications"):
            identity = row["application_id"]
            source = (row.get("ats"), row.get("job_id"))
            if not all(source) or source in identities:
                self.issue("applications", identity, "ambiguous_job_identity")
                continue
            identities.add(source)
            phase = row.get("current_phase")
            if phase not in {"preparing", "awaiting_confirmation", "active", "interviewing", "offer", "terminal"}:
                self.issue("applications", identity, "unknown_application_phase")
                continue
            value = {"id": identity, "job_id": "legacy-job-" + digest(list(source))[:32],
                "disposition": "closed" if phase == "terminal" else "open",
                "outcome": row.get("terminal_outcome") if phase == "terminal" else None,
                "pursuit_no": 1, "version": max(1, row.get("last_event_seq", 1)),
                "created_at": row["started_at"], "updated_at": row["updated_at"],
                "job": {"employer": row.get("employer_snapshot"), "title": row.get("title_snapshot"),
                        "sources": [{"source": source[0], "source_id": source[1]}],
                        "snapshot": {"ats": source[0], "job_id": source[1], "family_id": row.get("family_id"),
                            "title": row.get("title_snapshot"), "employer": row.get("employer_snapshot"),
                            "company_slug": row.get("company_slug_snapshot"), "job_url": row.get("job_url_snapshot")},
                        "recommendation": {key: row.get("recommendation_" + key) for key in ("session_id", "impression_id", "model_run_id", "policy_id", "rank")} | {key: row.get(key) for key in ("semantic_score", "ranking_score")}},
                "provenance": {"source_snapshot": self.source_hash, "legacy_projection_sha256": row.get("projection_sha256")}}
            result = self.attempt(tx, "applications", identity, lambda: self.runtime.applications.import_application(tx, value))
            if result:
                self.apps[identity] = row
                self.mapped("applications", identity, "applications", "applications", identity)

    def lifecycle(self, tx):
        related_tasks = {row["task_id"]: row["round_id"] for row in self.rows("interview_rounds") if row.get("task_id")}
        related_tasks.update({row["task_id"]: row["detail_id"] for row in self.rows("lifecycle_details") if row.get("task_id")})
        evidence_ids = {row["evidence_id"] for row in self.rows("mail_evidence")}
        for row in self.rows("lifecycle_tasks"):
            if row.get("evidence_id") and row["evidence_id"] not in evidence_ids:
                self.issue("lifecycle_tasks", row["task_id"], "missing_evidence")
            self.record(tx, "lifecycle_tasks", row, "tasks", row["task_id"], row["status"], {
                "kind": row["kind"], "description": row["note"], "responsible_party": row["owner"],
                "due_at": row.get("due_at"), "snoozed_until": row.get("snoozed_until"),
                "completion_rule": "verified_send" if row["kind"] in {"reply", "send_availability"} else "human_decision",
                "related_id": related_tasks.get(row["task_id"]), "origin_ref": row.get("evidence_id"),
                "evidence": [{"legacy_evidence_id": row["evidence_id"]}] if row.get("evidence_id") else [],
                "legacy_completed_evidence_id": row.get("completed_evidence_id"), "reminders_enabled": False})
        for row in self.rows("lifecycle_details"):
            kind = "assessments" if row["kind"] == "assessment" else "offers"
            details = json.loads(row["details_json"])
            data = {"evidence": [{"legacy_evidence_id": row["evidence_id"]}] if row.get("evidence_id") else [],
                    "task_id": row.get("task_id"), "legacy_details": details}
            if kind == "assessments":
                data.update(description=details.get("description") or details.get("instructions") or "",
                            channel=details.get("channel", "unknown"), due_at=details.get("due_at"), deadline_text=details.get("deadline_text"))
            else:
                data["terms"] = details
            self.record(tx, "lifecycle_details", row, kind, row["detail_id"], row["status"], data)
        for row in self.rows("interview_rounds"):
            if row["status"] == "proposed":
                continue  # An unaccepted round is not an accepted interview fact.
            state = {"confirmed": "scheduled", "rescheduled": "scheduled", "completed": "completed", "cancelled": "cancelled"}.get(row["status"])
            if state is None or (state == "scheduled" and not (row.get("starts_at") and row.get("ends_at"))):
                self.issue("interview_rounds", row["round_id"], "incomplete_interview")
                continue
            self.record(tx, "interview_rounds", row, "interviews", row["round_id"], state,
                        {"title": row["round_kind"], "start_at": row.get("starts_at") or None, "end_at": row.get("ends_at") or None,
                         "timezone": row.get("time_zone"), "employer_confirmed": None,
                         "reminders_enabled": False, "evidence": [], "legacy_details": json.loads(row["details_json"]),
                         "calendar_account_id": row.get("calendar_account_id"), "calendar_event_id": row.get("calendar_event_id"),
                         "calendar_change_key": row.get("calendar_change_key"), "task_id": row.get("task_id")})
        converted_schedules = {row.get("legacy_schedule_id") for row in self.rows("interview_rounds") if row["round_id"] in self.imported["interviews"]}
        for row in self.rows("accepted_interview_schedules"):
            if row["interview_schedule_id"] in converted_schedules:
                continue
            self.record(tx, "accepted_interview_schedules", row, "interviews", row["interview_schedule_id"],
                        {"active": "scheduled", "completed": "completed", "cancelled": "cancelled"}.get(row["status"], row["status"]),
                        {"title": "Interview", "start_at": row["starts_at"], "end_at": row["ends_at"], "timezone": row["time_zone"],
                         "employer_confirmed": None, "reminders_enabled": False, "evidence": [{"legacy_event_id": row["application_event_id"]}]})
        for row in self.rows("application_notes"):
            identity = row.get("note_id") or row.get("id")
            self.record(tx, "application_notes", row, "notes", identity, "active", {"text": row.get("text", row.get("note", ""))})
        for table in ("reminders", "local_reminders", "interview_reminders"):
            for row in self.rows(table):
                state = {"scheduled": "pending", "pending": "pending", "completed": "delivered", "cancelled": "cancelled", "dismissed": "cancelled"}.get(row["status"])
                related = row.get("round_id") or row.get("interview_schedule_id")
                if related in converted_schedules:
                    related = next(r["round_id"] for r in self.rows("interview_rounds") if r.get("legacy_schedule_id") == related)
                if related and related not in self.imported["interviews"]:
                    self.issue(table, row["reminder_id"], "missing_reminder_target")
                if row["reminder_id"] in self.imported["reminders"]:
                    self.issue(table, row["reminder_id"], "duplicate_reminder_requires_review")
                    continue
                self.record(tx, table, row, "reminders", row["reminder_id"], state,
                            {"at": row["due_at"], "next_notification_at": row["due_at"], "related_id": related,
                             "kind": row.get("kind", "manual"), "description": row.get("note"), "migration_delivery_paused": True})

    def _submission_repairs(self, events):
        """Recognize one proven historical reassociation, never infer a repair."""
        repairs, recognized = {}, set()
        by_id = {row["event_id"]: row for rows in events.values() for row in rows}
        evidence = {row["evidence_id"]: row for row in self.rows("mail_evidence")}
        proposals = [row for row in self.rows("event_proposals") if row["status"] == "accepted" and row.get("applied_event_id")]
        for correction in by_id.values():
            if correction["event_type"] != "manual_correction":
                continue
            payload = json.loads(correction["payload_json"])
            if (set(payload) != {"reason", "reassigned_evidence_id", "target_application_id", "target_phase"}
                    or correction["source_kind"] != "authorized_association_repair" or correction["actor_kind"] != "user"
                    or payload["target_phase"] != "active" or not isinstance(payload["reason"], str) or not payload["reason"].strip()):
                continue
            source, target, evidence_id = correction["application_id"], payload["target_application_id"], payload["reassigned_evidence_id"]
            if source not in self.apps or target not in self.apps or target == source or evidence_id not in evidence:
                continue
            # Multiple corrections need a semantic replay: one correction's
            # independent support might itself be retracted by another.
            if sum(e["event_type"] == "manual_correction" for e in events[source]) != 1:
                continue
            related = [p for p in proposals if p["evidence_id"] == evidence_id]
            supported = [by_id.get(p["applied_event_id"]) for p in related]
            if (not supported or any(not event or event["event_type"] != "submission_confirmed"
                    or event["application_id"] not in {source, target} or event["source_ref"] != proposal["proposal_id"]
                    or proposal["event_type"] != event["event_type"] for proposal, event in zip(related, supported))
                    or len({event["event_id"] for event in supported}) != len(supported)):
                continue
            old = [e for e in supported if e["application_id"] == source]
            new = [e for e in supported if e["application_id"] == target]
            if (not old or not new or any(e["event_seq"] >= correction["event_seq"] for e in old)
                    or any(e["event_seq"] <= correction["event_seq"] for e in new)):
                continue
            supported_ids = {e["event_id"] for e in supported}
            if any(p["applied_event_id"] in supported_ids and p["evidence_id"] != evidence_id for p in proposals):
                continue
            # The specified active source phase needs separate, unreassigned support.
            old_ids = {e["event_id"] for e in old}
            independent = [e for e in events[source] if e["event_type"] == "submission_confirmed"
                           and e["event_id"] not in old_ids and e["event_seq"] < correction["event_seq"]]
            observations = [o for o in self.rows("lifecycle_mail_observations") if o.get("evidence_id") == evidence_id]
            if not independent or len(observations) != 1:
                continue
            observation = observations[0]
            links = [link for link in self.rows("lifecycle_mail_links") if link["observation_id"] == observation["observation_id"]]
            if (len(links) != 1 or links[0]["application_id"] != target or links[0]["confidence"] != 1
                    or links[0]["source"] != "reviewed_correction"
                    or any(observation[key] != evidence[evidence_id][key] for key in ("account_id", "immutable_message_id"))
                    or any(e["event_id"] in repairs for e in old)):
                continue
            provenance = {"correction_event_id": correction["event_id"], "legacy_evidence_id": evidence_id,
                "source_application_id": source, "target_application_id": target, "reason": payload["reason"],
                "target_confirmation_event_ids": [e["event_id"] for e in new], "observation_id": observation["observation_id"]}
            for event in old:
                repairs[event["event_id"]] = provenance
            recognized.add(correction["event_id"])
        return repairs, recognized

    def submissions(self, tx):
        events = defaultdict(list)
        attempts = defaultdict(list)
        for event in self.rows("application_events"):
            events[event["application_id"]].append(event)
        repairs, recognized_corrections = self._submission_repairs(events)
        accepted_proposals = defaultdict(list)
        for proposal in self.rows("event_proposals"):
            if proposal["status"] == "accepted" and proposal.get("applied_event_id"):
                accepted_proposals[proposal["applied_event_id"]].append(proposal)
        for attempt in self.rows("browser_attempts"):
            attempts[attempt["application_id"]].append(attempt)
        snapshots = defaultdict(list)
        for snapshot in self.rows("application_answer_snapshots"):
            snapshots[snapshot["attempt_id"]].append(snapshot)
        attempt_owners = {row["attempt_id"]: row["application_id"] for rows in attempts.values() for row in rows}
        for app_id, app in self.apps.items():
            accepted = sorted((e for e in events[app_id] if e["event_type"] in {"submission_observed", "submission_confirmed"}), key=lambda e: e["event_seq"])
            records = {}
            for row in attempts[app_id]:
                identity = row["attempt_id"]
                captures = sorted(snapshots[identity], key=lambda s: (s["captured_at"], s["capture_id"]))
                exact = [{**s, "snapshot": json.loads(s["snapshot_json"])} for s in captures]
                answers = {f["field_key"]: f["value"] for f in exact[-1]["snapshot"].get("fields", []) if isinstance(f.get("value"), str)} if exact else {}
                documents = [json.loads(row["resume_json"])] if row.get("resume_sha256") or row.get("resume_json", "{}") != "{}" else []
                records[identity] = (row, "unreviewed", {"occurred_at": row["created_at"], "answers": answers, "documents": documents,
                    "evidence": [], "captured_answer_snapshots": exact, "legacy_attempt_status": row["status"]})
            for event in accepted:
                repair = repairs.get(event["event_id"])
                payload = json.loads(event["payload_json"])
                inferred_attempt = (event["event_type"] == "submission_observed" and event["source_kind"] == "browser_extension"
                    and event["actor_kind"] == "system" and payload.get("observed_by") == "confirmation_email")
                attempt_ids = [row["attempt_id"] for row in attempts[app_id]]
                explicit = event.get("source_ref") if event.get("source_ref") in attempt_ids and not repair and not inferred_attempt else None
                if event.get("source_ref") in attempt_owners and attempt_owners[event["source_ref"]] != app_id:
                    self.issue("application_events", event["event_id"], "conflicting_submission_attempt_identity")
                if event["source_kind"] == "browser_extension" and event.get("source_ref") not in attempt_owners:
                    self.issue("application_events", event["event_id"], "missing_submission_attempt_identity")
                if not explicit:
                    explicit = event["event_id"]
                    if explicit in records:
                        self.issue("application_events", explicit, "conflicting_submission_record_identity")
                        continue
                    click_known = event["event_type"] == "submission_observed" and not inferred_attempt
                    warnings = (["legacy_confirmation_attempt_link_unverified"] if inferred_attempt else
                                ["submission_attempt_link_unresolved"] if attempt_ids and not repair else [])
                    records[explicit] = (event, "unreviewed", {"occurred_at": event["occurred_at"], "answers": {},
                        "documents": [payload["resume"]] if click_known and isinstance(payload.get("resume"), dict) else [],
                        "evidence": [], "click_time_known": click_known,
                        "attempt_link": {"status": "unresolved", "candidate_attempt_ids": sorted(attempt_ids)},
                        "migration_warnings": warnings})
                    for warning in warnings:
                        self.issue("application_events", event["event_id"], warning, "warning")
                row, state, data = records[explicit]
                if repair:
                    state = "retracted"
                    data["correction_provenance"] = repair
                    data["superseded_evidence"] = [{"legacy_event_id": event["event_id"], "legacy_evidence_id": repair["legacy_evidence_id"]}]
                elif event["event_type"] == "submission_confirmed":
                    state = "confirmed"
                elif state != "confirmed" and not inferred_attempt:
                    state = "attempted"
                if not repair:
                    support = {"legacy_event_id": event["event_id"]}
                    evidence_ids = {p["evidence_id"] for p in accepted_proposals[event["event_id"]]}
                    if len(evidence_ids) > 1:
                        self.issue("application_events", event["event_id"], "conflicting_event_evidence_identity")
                    elif evidence_ids:
                        support["legacy_evidence_id"] = next(iter(evidence_ids))
                    data["evidence"].append(support)
                    if explicit in attempt_ids:
                        data["attempt_link"] = {"status": "explicit", "attempt_id": explicit}
                        data["click_time_known"] = True
                records[explicit] = (row, state, data)
                self.mapped("application_events", event["event_id"], "applications", "submissions", explicit)
            if (app.get("submitted_at") or app.get("confirmed_at")) and not accepted:
                self.issue("applications", app_id, "submission_projection_without_event")
            for identity, (row, state, data) in records.items():
                table = "browser_attempts" if "attempt_id" in row else "application_events"
                self.record(tx, table, row, "submissions", identity, state, data, application_id=app_id)
        by_attempt = {row["attempt_id"]: row for rows in attempts.values() for row in rows}
        for row in self.rows("browser_observations"):
            attempt = by_attempt.get(row["attempt_id"])
            if attempt is None:
                self.issue("browser_observations", row["observation_id"], "missing_browser_attempt")
                continue
            self.record(tx, "browser_observations", row, "observations", row["observation_id"], "recorded",
                        {"device_id": attempt["device_id"], "source_observation_id": row["observation_id"], "source_digest": row["payload_sha256"],
                         "attempt_ref": row["attempt_id"], "occurred_at": row["occurred_at"], "activity": row["kind"],
                         "legacy_metadata": json.loads(row["metadata_json"])}, application_id=attempt["application_id"])
        for app_id, rows in events.items():
            for row in rows:
                if row["event_type"] in {"recruiter_contact", "interview_requested"}:
                    self.record(tx, "application_events", row, "progress", row["event_id"], "active",
                                {"kind": "interview_request" if row["event_type"] == "interview_requested" else "recruiter_contact",
                                 "evidence": [{"legacy_event_id": row["event_id"]}]})
                if row["event_type"] == "manual_correction":
                    if row["event_id"] not in recognized_corrections:
                        self.issue("application_events", row["event_id"], "manual_correction_requires_semantic_review")
                    else:
                        self.issue("application_events", row["event_id"], "historical_association_repair_preserved", "warning")
                        for event_id, repair in repairs.items():
                            if repair["correction_event_id"] == row["event_id"]:
                                self.mapped("application_events", row["event_id"], "applications", "submissions", event_id)
            # Some older accepted facts predate the richer lifecycle tables.
            # Preserve these as explicit historical records without inventing
            # unavailable deadlines, meeting times, or relationships between rounds.
            for kind, event_kinds in (("assessments", {"assessment_requested", "assessment_completed"}),
                                      ("offers", {"offer_received"}),
                                      ("interviews", {"interview_scheduled", "interview_completed"})):
                if app_id not in self.apps:
                    continue
                existing = self.runtime.applications.list_records(tx.connection, app_id, kind, limit=1)["items"]
                if existing:
                    continue
                selected = [row for row in rows if row["event_type"] in event_kinds]
                if len(selected) > 1:
                    self.issue("applications", app_id, kind + "_identity_requires_review")
                for row in selected:
                    payload = json.loads(row["payload_json"])
                    data = {"evidence": [{"legacy_event_id": row["event_id"]}], "legacy_payload": payload}
                    if kind == "assessments":
                        status = "completed" if row["event_type"] == "assessment_completed" else "requested"
                        data.update(description=payload.get("description", ""), channel="unknown", due_at=None, deadline_text=None)
                    elif kind == "offers":
                        status = "offered"
                        data["terms"] = payload
                    else:
                        status = "completed" if row["event_type"] == "interview_completed" else "scheduled"
                        data.update(title="Interview", start_at=payload.get("starts_at"), end_at=payload.get("ends_at"),
                                    timezone=payload.get("time_zone"), employer_confirmed=None, reminders_enabled=False)
                        if status == "scheduled" and not (data["start_at"] and data["end_at"] and data["timezone"]):
                            self.issue("application_events", row["event_id"], "missing_interview_interval")
                            continue
                    self.record(tx, "application_events", row, kind, row["event_id"], status, data)

    def correspondence(self, tx):
        evidence = {row["evidence_id"]: row for row in self.rows("mail_evidence")}
        archives = {row["archive_id"]: row for row in self.rows("mail_archive")}
        links = defaultdict(list)
        for link in self.rows("lifecycle_mail_links"):
            links[link["observation_id"]].append(link)
        for row in self.rows("lifecycle_mail_observations"):
            identity = row["observation_id"]
            direction = {"inbound": "incoming", "outbound": "outgoing", "draft": "draft", "unknown": "unknown"}.get(row["direction"])
            if direction is None:
                self.issue("lifecycle_mail_observations", identity, "unknown_message_direction")
                continue
            item, archive = evidence.get(row.get("evidence_id")), archives.get(row.get("archive_id"))
            source_hash = archive.get("sanitized_sha256") if archive else item.get("body_sha256") if item else None
            if not source_hash:
                self.issue("lifecycle_mail_observations", identity, "missing_message_evidence_hash")
                continue
            revision_id = "legacy-revision-" + digest({"id": identity, "modified_at": row["modified_at"]})[:32]
            prior = [r for r in self.rows("lifecycle_mail_revisions") if r["observation_id"] == identity]
            for revision in prior:
                values = json.loads(revision["payload_json"])
                values = values.get("observation", values)
                if values.get("modified_at") == row["modified_at"] and values.get("direction") == row["direction"]:
                    revision_id = revision["revision_id"]
            records = {"messages": [{"id": identity, "account_id": row["account_id"], "provider_message_id": row["immutable_message_id"],
                        "thread_id": row.get("conversation_ref"), "latest_revision": revision_id, "version": max(1, len(prior))}],
                "revisions": [{"id": revision_id, "message_id": identity, "source_version": row["modified_at"], "direction": direction,
                    "occurred_at": row["source_at"], "received_at": row["created_at"], "source_sha256": source_hash,
                    "archive_ref": "predecessor:mail_archive:" + archive["archive_id"] if archive else None,
                    "content_chars": archive["sanitized_chars"] if archive else 0,
                    "metadata": {"coverage": {"complete": bool(archive and not archive["truncated"]), "reason": "historical_archive_requires_reader" if archive else "archive_missing"}}}],
                "associations": []}
            if len(links[identity]) > 1:
                self.issue("lifecycle_mail_links", identity, "ambiguous_message_association")
            elif links[identity]:
                link = links[identity][0]
                if link["application_id"] not in self.apps:
                    self.issue("lifecycle_mail_links", identity, "missing_application")
                else:
                    records["associations"].append({"id": "legacy-association-" + identity, "message_id": identity,
                        "application_id": link["application_id"], "version": 1, "reason": "Preserved historical association"})
            result = self.attempt(tx, "lifecycle_mail_observations", identity,
                lambda: self.runtime.correspondence.import_records(tx, records=records, source_snapshot=self.source_hash))
            if result:
                self.mapped("lifecycle_mail_observations", identity, "correspondence", "messages", identity)
                if item:
                    self.mapped("mail_evidence", item["evidence_id"], "correspondence", "messages", identity)

    def proposals(self, tx):
        operations = {"submission_confirmed": "confirm_submission", "recruiter_contact": "record_progress", "assessment_requested": "record_assessment",
            "assessment_completed": "update_assessment", "interview_requested": "record_progress", "interview_scheduled": "schedule_interview",
            "interview_completed": "schedule_interview", "offer_received": "record_offer", "offer_accepted": "close_application",
            "rejection_received": "close_application", "withdrawn": "close_application"}
        for table, id_field in (("event_proposals", "proposal_id"), ("temporal_proposals", "temporal_proposal_id"),
                                ("interview_revisions", "revision_id"), ("lifecycle_correction_proposals", "proposal_id")):
            for row in self.rows(table):
                identity = row[id_field]
                operation = operations.get(row.get("event_type")) if table == "event_proposals" else "schedule_interview" if table in {"temporal_proposals", "interview_revisions"} else "correct_progress"
                if table == "temporal_proposals" and row["kind"] == "deadline":
                    operation = "create_task"
                if not operation:
                    self.issue(table, identity, "unsupported_proposal_operation")
                    continue
                app_id = row.get("application_id", row.get("proposed_application_id"))
                state = {"pending": "pending", "conflict": "stale", "accepted": "applied", "auto_applied": "applied", "rejected": "rejected", "superseded": "superseded", "stale": "stale"}.get(row["status"])
                value = {"id": identity, "version": 1, "status": state, "operation": operation,
                    "input": {"application_id": app_id, "legacy_payload": json.loads(row.get("payload_json") or row.get("details_json") or "{}")},
                    "application_id": app_id, "evidence": [{"legacy_table": table, "legacy_id": identity}], "expected_versions": {}, "dependencies": [],
                    "blockers": ["migration_revalidation_required"], "analysis_id": None, "finding_id": None,
                    "lineage_key": "legacy:" + table + ":" + identity, "recorded_at": row["created_at"], "reason": "Original decision history remains in predecessor archive"}
                result = self.attempt(tx, table, identity, lambda: self.runtime.understanding.import_records(tx, records={"proposals": [value]}, source_snapshot=self.source_hash))
                if result:
                    self.mapped(table, identity, "understanding", "proposals", identity)
                if app_id is not None and app_id not in self.apps:
                    self.issue(table, identity, "missing_application")

    def actions(self, tx):
        executions = defaultdict(list)
        for row in self.rows("action_executions"):
            executions[row["action_id"]].append(row)
        for table, key in (("career_send_proposals", "proposal_id"), ("action_proposals", "action_id"), ("career_commitments", "commitment_id")):
            for row in self.rows(table):
                identity = row[key]
                value = dict(row)
                if table == "action_proposals" and identity in executions:
                    attempts = sorted(executions[identity], key=lambda item: item["attempt"])
                    value["historical_executions"] = attempts
                    value["execution"] = "uncertain" if any(item["status"] in {"claimed", "uncertain"} for item in attempts) else attempts[-1]["status"]
                if row.get("status") == "needs_reconciliation":
                    value["execution"] = "uncertain"
                if table == "career_commitments":
                    linked = next((p for p in self.rows("career_send_proposals") if p["proposal_id"] == row["proposal_id"]), None)
                    value["application_id"] = linked["application_id"] if linked else None
                result = self.attempt(tx, table, identity, lambda: self.runtime.actions.import_history(tx, table + ":" + identity, value))
                if result:
                    self.mapped(table, identity, "external_actions", "historical_actions", table + ":" + identity)
                if value.get("execution", value.get("status")) in {"executing", "accepted", "awaiting_confirmation", "uncertain"}:
                    self.issue(table, identity, "external_outcome_requires_reconciliation")
                if value.get("application_id") not in self.apps:
                    self.issue(table, identity, "missing_application")

    def run(self, tx):
        self.applications(tx)
        self.lifecycle(tx)
        self.submissions(tx)
        self.correspondence(tx)
        self.proposals(tx)
        self.actions(tx)
        for identity, original in self.apps.items():
            stage = self.runtime.applications.stage(tx.connection, identity)
            if stage["legacy_phase"] != original["current_phase"] or stage["outcome"] != original.get("terminal_outcome"):
                self.issue("applications", identity, "derived_progress_differs_requires_review")
        for row in self.source.execute("PRAGMA foreign_key_check"):
            self.issue(str(row[0]), str(row[1]), "source_foreign_key_missing")
        with tx.scope("migration"):
            for mapping in self.mapping:
                tx.connection.execute("INSERT OR IGNORE INTO migration_mappings VALUES(:source_table,:source_id,:target_owner,:target_kind,:target_id)", mapping)
            for issue in self.issues:
                tx.connection.execute("INSERT INTO migration_issues(source_table,source_id,code,severity) VALUES(:source_table,:source_id,:code,:severity)", issue)
            result = {"mapping": self.mapping, "issues": self.issues, "imported_applications": len(self.apps), "dispatch_created": False}
            tx.record("migration", self.source_hash, "import_snapshot", None, result)
            return result


def convert_snapshot(source_path, destination_directory):
    """Produce an isolated reviewable candidate, never activate or overwrite one."""
    from .application_runtime import ApplicationRuntime

    source, destination = Path(source_path).resolve(), Path(destination_directory).resolve()
    if not source.is_file() or destination.exists() or destination == source.parent or source == destination:
        raise DomainError("invalid_input", "Use an existing snapshot and a new separate destination directory")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    archive, candidate = destination / ARCHIVE_NAME, destination / CANDIDATE_NAME
    source_file_hash = _file_hash(source)
    with _readonly(source) as original:
        with sqlite3.connect(archive) as copied:
            original.backup(copied)
            # Materialize a self-contained immutable snapshot without WAL sidecars.
            copied.execute("PRAGMA journal_mode=DELETE")
    os.chmod(archive, 0o400)
    archive_hash = _file_hash(archive)
    with _readonly(archive) as preserved:
        integrity = preserved.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise DomainError("invalid_input", "Source snapshot failed SQLite integrity verification")
        inventory = _inventory(preserved)
        if "applications" not in inventory["counts"] or "app_applications" in inventory["counts"]:
            raise DomainError("invalid_input", "Expected a predecessor application snapshot")
        runtime = ApplicationRuntime(candidate, extra_schemas={"migration": SCHEMA})
        conversion = _Conversion(preserved, runtime, inventory["logical_sha256"])
        context = CommandContext(Principal("offline-snapshot-converter", "worker", {"import_snapshot"}), inventory["logical_sha256"], "migration")
        result = runtime.executor.run(context, "import_snapshot", {"source_snapshot": inventory["logical_sha256"]}, conversion.run)
    with runtime.executor.read() as con:
        candidate_inventory = _inventory(con)
        queued = con.execute("SELECT count(*) FROM command_work").fetchone()[0]
        paused = con.execute("SELECT paused FROM command_installation WHERE singleton=1").fetchone()[0] == 1
        valid_fk = not con.execute("PRAGMA foreign_key_check").fetchall()
    domain_tables = {table for table in candidate_inventory["counts"] if table.startswith(("app_", "corr_", "understand_", "action_"))}
    domain_counts = {table: candidate_inventory["counts"][table] for table in sorted(domain_tables)}
    domain_hashes = {table: candidate_inventory["table_sha256"][table] for table in sorted(domain_tables)}
    preserved_exact = _file_hash(archive) == archive_hash
    mapped_tables = {item["source_table"] for item in result["mapping"]}
    report = {"format_version": 1, "source_path": str(source), "source_file_sha256": source_file_hash,
        "source_snapshot_sha256": inventory["logical_sha256"], "archive_path": str(archive), "archive_sha256": archive_hash,
        "candidate_path": str(candidate), "source_counts": inventory["counts"], "source_table_sha256": inventory["table_sha256"],
        "candidate_counts": domain_counts, "candidate_counts_scope": "domain_owner_tables",
        "candidate_domain_sha256": digest({"counts": domain_counts, "hashes": domain_hashes}),
        "mapping": result["mapping"], "issues": result["issues"], "paused": paused,
        "archive_only_tables": {table: count for table, count in inventory["counts"].items() if table not in mapped_tables and count},
        "validation": {"source_integrity": integrity, "archive_unchanged": preserved_exact, "candidate_foreign_keys_valid": valid_fk,
                       "pending_dispatch_count": queued, "no_runtime_effects": queued == 0},
        "ready_for_review": paused and preserved_exact and valid_fk and queued == 0 and not any(i["severity"] == "blocker" for i in result["issues"]),
        "activation_authorized": False}
    report_context = CommandContext(Principal("offline-snapshot-converter", "worker", {"import_snapshot"}), "report:" + inventory["logical_sha256"], "migration")
    def save_report(tx):
        with tx.scope("migration"):
            tx.connection.execute("INSERT INTO migration_manifest VALUES(1,?,?)", (encode(report), tx.now))
        return {"report_recorded": True}
    runtime.executor.run(report_context, "import_snapshot", {"report": report}, save_report)
    (destination / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(candidate, 0o600)
    os.chmod(destination / REPORT_NAME, 0o600)
    return report
