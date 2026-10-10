"""Outstanding obligations and explicit completion."""
from dataclasses import asdict
from . import _store as db
from . import identity, reminders

KINDS = frozenset(("reply", "send_availability", "complete_assessment", "attend_interview", "send_document", "offer_decision", "follow_up", "book_interview", "other"))


def create(tx, value):
    if value.kind not in KINDS or value.responsible_party not in {"applicant", "employer", "unknown"}:
        db.fail("invalid_input", "Unsupported task kind or responsible party")
    db.nonempty(value.description, "description")
    db.instant(value.due_at, "due_at", optional=True)
    if value.completion_rule not in {"human_decision", "verified_send"}:
        db.fail("invalid_input", "Unsupported task completion rule")
    if value.completion_rule == "verified_send" and value.kind not in {"reply", "send_availability", "send_document"}:
        db.fail("invalid_input", "Sending cannot complete a non-communication obligation")
    app = identity.ensure(tx, value, require_open=True)
    # A finding/request identity, not its containing email, deduplicates obligations.
    if value.origin_ref:
        row = tx.connection.execute("SELECT id FROM app_tasks WHERE application_id=? AND pursuit_no=? AND json_extract(data,'$.origin_ref')=?", (app["id"], app["pursuit_no"], value.origin_ref)).fetchone()
        if row:
            existing = db.record(tx.connection, "tasks", row[0])
            if any(existing.get(field) != getattr(value, field) for field in ("kind", "description", "responsible_party", "due_at", "completion_rule", "related_id")):
                db.fail("idempotency_conflict", "Request identity already has different task content")
            return existing
    data = asdict(value)
    data.pop("application_id")
    data.pop("job_source")
    data["snoozed_until"] = None
    result = db.insert(tx, "tasks", app, "open", data, "create_task")
    if value.reminders_enabled and value.due_at:
        reminders.schedule(tx, app, result["id"], value.due_at, "task")
    return result


def decide(tx, value, status):
    before = db.record(tx.connection, "tasks", value.task_id)
    db.check_version(before, value.expected_version)
    app = db.application(tx.connection, before["application_id"])
    if before["status"] != "open" or before["pursuit_no"] != app["pursuit_no"]:
        db.fail("version_conflict", "Task is no longer open in the current pursuit")
    db.nonempty(value.reason, "reason")
    data = db.data_of(before, "tasks")
    data["resolution_reason"] = value.reason
    if value.result_id:
        if tx.context.origin != "result" or before["completion_rule"] != "verified_send" or before["kind"] not in {"reply", "send_availability", "send_document"}:
            db.fail("not_authorized", "Result completion requires the authorized task rule")
        data["result_id"] = value.result_id
    after = db.update(tx, "tasks", before, status, data, "complete_task" if status == "completed" else "cancel_task")
    reminders.cancel_for(tx, before["id"], value.reason)
    return after


def snooze(tx, value):
    before = db.record(tx.connection, "tasks", value.task_id)
    db.check_version(before, value.expected_version)
    app = db.application(tx.connection, before["application_id"], require_open=True)
    if before["status"] != "open" or before["pursuit_no"] != app["pursuit_no"]:
        db.fail("version_conflict", "Task is no longer open")
    reminders.snooze(tx, before, value.until)
    data = db.data_of(before, "tasks")
    data["snoozed_until"] = value.until
    return db.update(tx, "tasks", before, "open", data, "snooze_task")


def update_related(tx, related_id, due_at=None, cancel=False):
    for row in tx.connection.execute("SELECT id FROM app_tasks WHERE status='open' AND json_extract(data,'$.related_id')=?", (related_id,)).fetchall():
        before = db.record(tx.connection, "tasks", row[0])
        data = db.data_of(before, "tasks")
        data["due_at"] = due_at
        after = db.update(tx, "tasks", before, "cancelled" if cancel else "open", data, "revise_related_task")
        reminders.cancel_for(tx, before["id"], "Related scheduling changed")
        if not cancel and due_at and before.get("reminders_enabled"):
            reminders.schedule(tx, db.application(tx.connection, before["application_id"]), before["id"], due_at, "task")
