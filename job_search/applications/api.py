"""Public transaction participants for the Applications owner.

Authorization and commit belong to the command executor. Cross-owner coordination
belongs in workflows; every function here changes only application-owned tables.
"""
from dataclasses import replace
import json
from . import _store as db
from . import identity, tasks, submissions, interviews, assessments, offers, reminders, progress, importing, scheduling
from .contracts import (SaveJob, AddNote, CreateTask, TaskDecision, SnoozeTask,
    BrowserObservation, Submission, AttachSubmissionAnswers, Interview, CancelInterview, Assessment, Offer,
    OfferDecision, ApplicationDecision, ProgressFact, CorrectProgress, CombineJobs,
    CreateReminder, ReminderDecision, ScheduleOperation, ScheduleDecision,
    ReenableReminder, NotificationDelivery)

SCHEMA = db.SCHEMA
SCHEMA_MIGRATIONS = db.SCHEMA_MIGRATIONS


class ApplicationOperations:
    def ensure_application(self, tx, value):
        """Create a targeted observation/proposal container without recording a save."""
        with tx.scope("applications"):
            return identity.ensure(tx, db.input_as(SaveJob, value))

    def save_job(self, tx, value):
        with tx.scope("applications"):
            return identity.save(tx, db.input_as(SaveJob, value))

    def add_note(self, tx, value):
        with tx.scope("applications"):
            return identity.note(tx, db.input_as(AddNote, value))

    def create_task(self, tx, value):
        with tx.scope("applications"):
            return tasks.create(tx, db.input_as(CreateTask, value))

    def record_request(self, tx, value):
        """A reviewed request creates exactly its specified obligation."""
        return self.create_task(tx, value)

    def complete_task(self, tx, value):
        with tx.scope("applications"):
            return tasks.decide(tx, db.input_as(TaskDecision, value), "completed")

    def cancel_task(self, tx, value):
        with tx.scope("applications"):
            return tasks.decide(tx, db.input_as(TaskDecision, value), "cancelled")

    def snooze_task(self, tx, value):
        with tx.scope("applications"):
            return tasks.snooze(tx, db.input_as(SnoozeTask, value))

    def record_browser_observation(self, tx, value):
        with tx.scope("applications"):
            return submissions.observe(tx, db.input_as(BrowserObservation, value))

    def record_submission(self, tx, value):
        with tx.scope("applications"):
            return submissions.record(tx, db.input_as(Submission, value))

    def confirm_submission(self, tx, value):
        with tx.scope("applications"):
            return submissions.record(tx, db.input_as(Submission, value), confirming=True)

    def attach_submission_answers(self, tx, value):
        with tx.scope("applications"):
            return submissions.attach_answers(tx, db.input_as(AttachSubmissionAnswers, value))

    def get_browser_attempt(self, connection, device_id, attempt_ref, *, limit=100):
        result = submissions.browser_attempt(connection, device_id, attempt_ref, limit=limit)
        if result is not None:
            result["job"] = self.get_job(connection, result["job_id"])
        return result

    def schedule_interview(self, tx, value):
        with tx.scope("applications"):
            return interviews.schedule(tx, db.input_as(Interview, value))

    def reschedule_interview(self, tx, value):
        with tx.scope("applications"):
            return interviews.schedule(tx, db.input_as(Interview, value), rescheduling=True)

    def cancel_interview(self, tx, value):
        with tx.scope("applications"):
            return interviews.cancel(tx, db.input_as(CancelInterview, value))

    def record_assessment(self, tx, value):
        with tx.scope("applications"):
            return assessments.record(tx, db.input_as(Assessment, value))

    def update_assessment(self, tx, value):
        with tx.scope("applications"):
            return assessments.record(tx, db.input_as(Assessment, value), updating=True)

    def record_offer(self, tx, value):
        with tx.scope("applications"):
            return offers.record(tx, db.input_as(Offer, value))

    def decide_offer(self, tx, value):
        with tx.scope("applications"):
            return offers.decide(tx, db.input_as(OfferDecision, value))

    def close_application(self, tx, value):
        """Application participant; workflows also invalidate proposals/actions."""
        with tx.scope("applications"):
            value = db.input_as(ApplicationDecision, value)
            preview = self.preview_closure(tx.connection, value.application_id)
            db.check_related_versions(value.expected_records, preview["expected_records"])
            internal = replace(value, expected_records={key: version for key, version in value.expected_records.items() if not key.startswith("schedules:")})
            result = identity.disposition(tx, internal)
            scheduling.cancel_for(tx, result["id"], "Application closed")
            return result

    def reopen_application(self, tx, value):
        with tx.scope("applications"):
            result = identity.disposition(tx, db.input_as(ApplicationDecision, value), reopening=True)
            scheduling.cancel_for(tx, result["id"], "Application reopened in a new pursuit")
            return result

    def record_progress(self, tx, value):
        with tx.scope("applications"):
            return progress.fact(tx, db.input_as(ProgressFact, value))

    def correct_progress(self, tx, value):
        with tx.scope("applications"):
            return progress.correct(tx, db.input_as(CorrectProgress, value))

    def combine_jobs(self, tx, value):
        """Identity participant; caller checks external execution before this call."""
        with tx.scope("applications"):
            result = identity.combine(tx, db.input_as(CombineJobs, value))
            scheduling.cancel_for(tx, result["resolved_application_id"], "Application identity combined")
            return result

    def schedule_operation(self, tx, value):
        with tx.scope("applications"):
            return scheduling.schedule(tx, db.input_as(ScheduleOperation, value))

    def cancel_schedule(self, tx, value):
        with tx.scope("applications"):
            return scheduling.cancel(tx, db.input_as(ScheduleDecision, value))

    def run_schedule(self, tx, schedule_id):
        with tx.scope("applications"):
            return scheduling.run(tx, schedule_id)

    def due_schedules(self, connection, now, limit=100):
        return scheduling.due(connection, now, limit)

    def queue_due_reminders(self, tx, now=None, limit=100):
        with tx.scope("applications"):
            return scheduling.queue_reminders(tx, now, limit)

    def record_delivery(self, tx, value):
        with tx.scope("applications"):
            return scheduling.delivery(tx, db.input_as(NotificationDelivery, value))

    def get_notification_handoff(self, connection, delivery_id):
        return scheduling.get_handoff(connection, delivery_id)

    def notification_delivery_applicable(self, connection, delivery_id):
        return scheduling.delivery_applicable(connection, delivery_id)

    def find_application_by_job(self, connection, source, source_id):
        db.nonempty(source, "source", 100)
        db.nonempty(source_id, "source_id", 500)
        row = connection.execute("SELECT job_id FROM app_job_sources WHERE source=? AND source_id=?", (source, source_id)).fetchone()
        if row is None:
            return None
        job_id = db.alias(connection, row[0], "job")
        row = connection.execute("SELECT id FROM app_applications WHERE job_id=?", (job_id,)).fetchone()
        return db.application(connection, row[0]) if row else None

    def reenable_reminder(self, tx, value):
        with tx.scope("applications"):
            return scheduling.reenable_reminder(tx, db.input_as(ReenableReminder, value))

    def mark_reminder_delivered(self, tx, reminder_id, expected_version):
        with tx.scope("applications"):
            return reminders.mark_delivered(tx, reminder_id, expected_version)

    def create_reminder(self, tx, value):
        with tx.scope("applications"):
            value = db.input_as(CreateReminder, value)
            db.nonempty(value.description, "description")
            app = identity.ensure(tx, value, require_open=True)
            if value.related_id:
                matches = [row for kind in ("tasks", "interviews") for row in tx.connection.execute("SELECT application_id FROM app_" + kind + " WHERE id=?", (value.related_id,)).fetchall()]
                if not matches or matches[0][0] != app["id"]:
                    db.fail("invalid_input", "Reminder target must belong to this application")
            return reminders.schedule(tx, app, value.related_id, value.at, "standalone", description=value.description)

    def cancel_reminder(self, tx, value):
        with tx.scope("applications"):
            value = db.input_as(ReminderDecision, value)
            before = db.record(tx.connection, "reminders", value.reminder_id)
            db.check_version(before, value.expected_version)
            db.nonempty(value.reason, "reason")
            if before["status"] != "pending":
                db.fail("version_conflict", "Reminder is no longer pending")
            data = db.data_of(before, "reminders")
            data["resolution_reason"] = value.reason
            return db.update(tx, "reminders", before, "cancelled", data, "cancel_reminder")

    def get_application(self, connection, application_id):
        return db.application(connection, application_id)

    def submission_summary(self, connection, application_id):
        return submissions.summary(connection, application_id)

    def get_record(self, connection, kind, record_id):
        return db.record(connection, kind, record_id)

    def list_records(self, connection, application_id, kind, **options):
        return db.records(connection, application_id, kind, **options)

    def query_records(self, connection, kind, *, application_id=None, statuses=(), starts_after=None, starts_before=None, limit=50, after_id=None, current_only=False):
        return db.query_records(connection, kind, application_id=application_id, statuses=statuses,
                                starts_after=starts_after, starts_before=starts_before, limit=limit,
                                after_id=after_id, current_only=current_only)

    def stage(self, connection, application_id):
        return progress.stage(connection, application_id)

    def preview_correction(self, connection, application_id, references):
        return progress.preview_correction(connection, application_id, references)

    def preview_interview_change(self, connection, interview_id):
        return interviews.preview(connection, interview_id)

    def preview_combination(self, connection, source_application_id, target_application_id, **options):
        return identity.preview_combination(connection, source_application_id, target_application_id, **options)

    def preview_closure(self, connection, application_id):
        result = identity.preview_closure(connection, application_id)
        schedules = db.query_records(connection, "schedules", application_id=application_id, statuses=["pending"], current_only=True, limit=200)
        if schedules["truncated"]:
            db.fail("dependency_unresolved", "Closure schedule preview exceeds bounded size")
        for record in schedules["items"]:
            result["records"].append({"kind": "schedules", "id": record["id"], "version": record["version"], "record": record})
            result["expected_records"]["schedules:" + record["id"]] = record["version"]
        return result

    def causal_records(self, connection, application_id, causation_id):
        result = self.affected_by_causation(connection, application_id, [causation_id])
        if result["truncated"]:
            db.fail("dependency_unresolved", "Correction context exceeds query limit")
        return result["items"]

    def affected_by_causation(self, connection, application_id, causation_ids, *, limit=200):
        if not isinstance(causation_ids, (tuple, list)) or len(causation_ids) > 200 or any(not isinstance(value, str) for value in causation_ids):
            db.fail("invalid_input", "Bounded causation identities required")
        if not causation_ids:
            db.application(connection, application_id)
            return {"items": [], "truncated": False}
        clause = "json_extract(data,'$.causation_id') IN (" + ",".join("?" for _ in causation_ids) + ")"
        return self._affected(connection, application_id, clause, tuple(causation_ids), limit, set(causation_ids))

    def affected_by_evidence(self, connection, application_id, source_id, *, limit=200):
        db.nonempty(source_id, "source_id", 500)
        clause = "EXISTS (SELECT 1 FROM json_each(json_extract(data,'$.evidence')) AS evidence WHERE json_extract(evidence.value,'$.source_id')=?)"
        return self._affected(connection, application_id, clause, (source_id,), limit, set())

    def _affected(self, connection, application_id, clause, parameters, limit, causes):
        if type(limit) is not int or not 1 <= limit <= 200:
            db.fail("invalid_input", "Invalid correction query limit")
        app = db.application(connection, application_id)
        result = []
        truncated = False
        for kind in ("tasks", "submissions", "interviews", "assessments", "offers", "reminders", "progress"):
            rows = connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=? AND " + clause + " ORDER BY id LIMIT ?", (app["id"], *parameters, limit + 1)).fetchall()
            for row in rows:
                if len(result) == limit:
                    truncated = True
                    break
                record = db.record(connection, kind, row[0])
                result.append({"kind": kind, "id": record["id"], "expected_version": record["version"], "record": record, "independently_edited": record["version"] > 1 and record.get("last_causation_id") not in causes})
        # Later independently authored obligations can depend on a corrected round
        # without sharing its original evidence. They also need explicit review.
        seen = {(item["kind"], item["id"]) for item in result}
        cursor = 0
        while cursor < len(result):
            parent = result[cursor]
            cursor += 1
            child_kinds = []
            if parent["kind"] in {"interviews", "assessments", "offers"}:
                child_kinds.append("tasks")
            if parent["kind"] in {"interviews", "tasks"}:
                child_kinds.append("reminders")
            for kind in child_kinds:
                rows = connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=? AND json_extract(data,'$.related_id')=? ORDER BY id LIMIT ?", (app["id"], parent["id"], limit + 1)).fetchall()
                for row in rows:
                    if (kind, row[0]) in seen:
                        continue
                    if len(result) == limit:
                        truncated = True
                        break
                    record = db.record(connection, kind, row[0])
                    seen.add((kind, row[0]))
                    inherited = record.get("causation_id") == parent["record"].get("causation_id")
                    result.append({"kind": kind, "id": record["id"], "expected_version": record["version"], "record": record, "independently_edited": not inherited or record["version"] > 1 and record.get("last_causation_id") != parent["record"].get("causation_id")})
        return {"items": result, "truncated": truncated}

    def import_application(self, tx, value):
        with tx.scope("applications"):
            return importing.application(tx, value)

    @staticmethod
    def notification_recovery_summary(connection):
        """Outstanding restored reminder uncertainty, excluding verified receipts."""
        return scheduling.recovery_summary(connection)

    def import_record(self, tx, kind, value, provenance=None):
        with tx.scope("applications"):
            return importing.record(tx, kind, value, provenance)

    def list_applications(self, connection, *, limit=100, after_id=None):
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            db.fail("invalid_input", "Invalid application query limit")
        rows = connection.execute("SELECT id FROM app_applications WHERE id>? AND id NOT IN (SELECT source_id FROM app_aliases WHERE kind='application') ORDER BY id LIMIT ?", (after_id or "", limit + 1)).fetchall()
        items = [db.application(connection, row[0]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None, "truncated": len(rows) > limit}

    def get_job(self, connection, job_id):
        canonical = db.alias(connection, job_id, "job")
        row = connection.execute("SELECT * FROM app_jobs WHERE id=?", (canonical,)).fetchone()
        if row is None:
            db.fail("not_found", "Job not found")
        result = dict(row)
        result["sources"] = [dict(item) for item in connection.execute("WITH RECURSIVE identities(id) AS (SELECT ? UNION SELECT a.source_id FROM app_aliases a JOIN identities i ON a.target_id=i.id WHERE a.kind='job') SELECT source,source_id FROM app_job_sources WHERE job_id IN (SELECT id FROM identities) ORDER BY source,source_id", (canonical,)).fetchall()]
        snapshots = connection.execute("WITH RECURSIVE identities(id) AS (SELECT ? UNION SELECT a.source_id FROM app_aliases a JOIN identities i ON a.target_id=i.id WHERE a.kind='job') SELECT * FROM app_job_snapshots WHERE job_id IN (SELECT id FROM identities) ORDER BY CASE WHEN job_id=? THEN 0 ELSE 1 END,created_at,job_id", (canonical, canonical)).fetchall()
        result["recorded_snapshots"] = [{"job_id": item["job_id"], "snapshot": json.loads(item["snapshot_json"]), "recommendation": json.loads(item["provenance_json"]), "created_at": item["created_at"]} for item in snapshots]
        result["recorded_snapshot"] = result["recorded_snapshots"][0]["snapshot"] if snapshots else None
        return result

    def application_identities(self, connection, application_id, *, limit=200):
        """Canonical application and transitive aliases for immutable history reads."""
        if type(limit) is not int or not 1 <= limit <= 200:
            db.fail("invalid_input", "Invalid application identity query limit")
        canonical = db.application(connection, application_id)["id"]
        rows = connection.execute("WITH RECURSIVE identities(id) AS (SELECT ? UNION SELECT a.source_id FROM app_aliases a JOIN identities i ON a.target_id=i.id WHERE a.kind='application') SELECT id FROM identities ORDER BY id LIMIT ?", (canonical, limit + 1)).fetchall()
        return {"items": [row[0] for row in rows[:limit]], "truncated": len(rows) > limit}
