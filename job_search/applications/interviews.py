"""Interview round changes own attendance scheduling and reminders atomically."""
from dataclasses import asdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit
from . import _store as db
from . import identity, reminders, tasks
from .contracts import CreateTask


def preview(connection, interview_id):
    interview = db.record(connection, "interviews", interview_id)
    references = []
    tasks_rows = connection.execute("SELECT id FROM app_tasks WHERE status='open' AND json_extract(data,'$.related_id')=?", (interview_id,)).fetchall()
    related_ids = [interview_id] + [row[0] for row in tasks_rows]
    for row in tasks_rows:
        task = db.record(connection, "tasks", row[0])
        references.append({"kind": "tasks", "id": task["id"], "version": task["version"], "record": task})
    for row in connection.execute("SELECT id FROM app_reminders WHERE status='pending' AND json_extract(data,'$.related_id') IN (" + ",".join("?" for _ in related_ids) + ")", related_ids).fetchall():
        reminder = db.record(connection, "reminders", row[0])
        references.append({"kind": "reminders", "id": reminder["id"], "version": reminder["version"], "record": reminder})
    return {"interview": interview, "records": references, "expected_related_versions": {row["kind"] + ":" + row["id"]: row["version"] for row in references}}


def schedule(tx, value, *, rescheduling=False):
    if rescheduling and value.status != "scheduled":
        db.fail("invalid_input", "Rescheduling requires a complete scheduled interval")
    if value.status not in {"requested", "scheduled", "completed"}:
        db.fail("invalid_input", "Unsupported interview status; cancellation is explicit")
    db.nonempty(value.title, "title", 500)
    db.nonempty(value.round_kind, "round_kind", 100)
    if not isinstance(value.participants, (tuple, list)) or len(value.participants) > 100:
        db.fail("invalid_input", "Interview participants must be a bounded list")
    for person in value.participants:
        db.nonempty(person, "participant", 500)
    for name in ("location", "organizer", "note"):
        if getattr(value, name) is not None:
            db.nonempty(getattr(value, name), name, 10000 if name == "note" else 2000)
    if value.join_url is not None:
        db.nonempty(value.join_url, "join_url", 2000)
        parsed = urlsplit(value.join_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            db.fail("invalid_input", "Invalid interview joining URL")
    if value.status == "scheduled":
        start, end = db.instant(value.start_at, "start_at"), db.instant(value.end_at, "end_at")
        try:
            ZoneInfo(value.timezone)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            db.fail("invalid_input", "Scheduled interview requires a valid time zone")
        if end <= start:
            db.fail("invalid_input", "Interview end must follow its start")
    elif value.start_at is not None or value.end_at is not None:
        if value.start_at is None or value.end_at is None:
            db.fail("invalid_input", "Interview interval is incomplete")
        if db.instant(value.end_at) <= db.instant(value.start_at):
            db.fail("invalid_input", "Interview end must follow start")
    data = asdict(value)
    for key in ("application_id", "job_source", "interview_id", "expected_version", "status", "create_task", "expected_related_versions"):
        data.pop(key)
    if value.interview_id:
        before = db.record(tx.connection, "interviews", value.interview_id)
        db.check_version(before, value.expected_version)
        db.check_related_versions(value.expected_related_versions, preview(tx.connection, value.interview_id)["expected_related_versions"])
        app = db.application(tx.connection, before["application_id"], require_open=True)
        if before["pursuit_no"] != app["pursuit_no"]:
            db.fail("version_conflict", "Interview is historical")
        if value.application_id and db.alias(tx.connection, value.application_id) != app["id"]:
            db.fail("invalid_input", "Interview belongs to another application")
        after = db.update(tx, "interviews", before, value.status, data, "reschedule_interview" if rescheduling else "schedule_interview")
        tasks.update_related(tx, before["id"], value.start_at)
    else:
        if rescheduling:
            db.fail("invalid_input", "Rescheduling requires an existing round")
        app = identity.ensure(tx, value, require_open=True)
        after = db.insert(tx, "interviews", app, value.status, data, "schedule_interview")
    if value.create_task:
        tasks.create(tx, CreateTask(application_id=app["id"], kind="attend_interview", description=value.title, due_at=value.start_at, origin_ref="interview:" + after["id"], related_id=after["id"], evidence=value.evidence))
    reminders.for_interview(tx, app, after)
    return after


def cancel(tx, value):
    before = db.record(tx.connection, "interviews", value.interview_id)
    db.check_version(before, value.expected_version)
    db.check_related_versions(value.expected_related_versions, preview(tx.connection, value.interview_id)["expected_related_versions"])
    db.nonempty(value.reason, "reason")
    if value.cancellation_kind not in {"user_not_attending", "employer_cancelled", "correction"}:
        db.fail("invalid_input", "Explicit cancellation kind required")
    data = db.data_of(before, "interviews")
    data.update({"resolution_reason": value.reason, "cancellation_kind": value.cancellation_kind})
    after = db.update(tx, "interviews", before, "cancelled", data, "cancel_interview")
    tasks.update_related(tx, before["id"], cancel=True)
    reminders.cancel_for(tx, before["id"], value.reason)
    return after
