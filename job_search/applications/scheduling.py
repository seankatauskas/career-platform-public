"""Exact one-shot human schedules and private reminder notification handoffs.

No timer infers an obligation. Scheduling a fixed task authorizes only that task
input for one application pursuit. Reminder delivery records never complete tasks.
"""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from uuid import uuid4

from . import _store as db
from . import tasks
from .contracts import CreateTask


def _human(tx):
    if tx.context.principal.kind != "human" or tx.context.origin != "direct":
        db.fail("not_authorized", "An exact schedule requires a direct human decision")


def _worker(tx, origin):
    if tx.context.principal.kind != "worker" or tx.context.origin != origin:
        db.fail("not_authorized", "A bounded trusted worker with the correct origin is required")


def schedule(tx, value):
    _human(tx)
    app = db.application(tx.connection, value.application_id, require_open=True)
    db.nonempty(value.reason, "reason")
    if value.operation != "create_task" or not isinstance(value.input, dict):
        db.fail("invalid_input", "Schedules support one exact create_task operation")
    if db.instant(value.due_at, "schedule due_at") <= db.instant(tx.now):
        db.fail("invalid_input", "The scheduled trigger must be in the future")
    if set(value.input) & {"application_id", "job_source", "origin_ref"}:
        db.fail("invalid_input", "Schedule target and identity are bound by the schedule")
    payload = db.input_as(CreateTask, {"application_id": app["id"], **value.input})
    # Validate through the actual owner inside a savepoint, without accepting work.
    # Rollback includes its history/outbox; no external effects exist in create_task.
    tx.connection.execute("SAVEPOINT validate_schedule")
    try:
        tasks.create(tx, payload)
    finally:
        tx.connection.execute("ROLLBACK TO validate_schedule")
        tx.connection.execute("RELEASE validate_schedule")
    exact = asdict(payload)
    exact.pop("application_id")
    exact.pop("job_source")
    exact.pop("origin_ref")
    return db.insert(tx, "schedules", app, "pending", {
        "operation": value.operation, "input": exact, "input_digest": sha256(db.encode(exact).encode()).hexdigest(),
        "due_at": db.timestamp(db.instant(value.due_at)), "reason": value.reason,
        "issuer_id": tx.context.principal.actor_id, "authorized_application_id": app["id"],
        "authorized_pursuit_no": app["pursuit_no"], "authorized_at": tx.now,
    }, "schedule_operation")


def cancel(tx, value):
    _human(tx)
    before = db.record(tx.connection, "schedules", value.schedule_id)
    db.check_version(before, value.expected_version)
    db.nonempty(value.reason, "reason")
    if before["status"] != "pending":
        db.fail("version_conflict", "Schedule is no longer pending")
    data = db.data_of(before, "schedules")
    data["resolution_reason"] = value.reason
    return db.update(tx, "schedules", before, "cancelled", data, "cancel_schedule")


def cancel_for(tx, application_id, reason):
    app = db.application(tx.connection, application_id)
    items = []
    for row in tx.connection.execute("SELECT id FROM app_schedules WHERE application_id=? AND status='pending'", (app["id"],)).fetchall():
        before = db.record(tx.connection, "schedules", row[0])
        data = db.data_of(before, "schedules")
        data["resolution_reason"] = reason
        items.append(db.update(tx, "schedules", before, "cancelled", data, "invalidate_schedule"))
    return items


def run(tx, schedule_id):
    _worker(tx, "scheduled")
    before = db.record(tx.connection, "schedules", schedule_id)
    if before["status"] != "pending":
        return {"schedule": before, "task": None}
    app = db.application(tx.connection, before["application_id"])
    data = db.data_of(before, "schedules")
    if app["disposition"] != "open" or app["id"] != before["authorized_application_id"] or app["pursuit_no"] != before["authorized_pursuit_no"]:
        data["resolution_reason"] = "Application pursuit changed"
        return {"schedule": db.update(tx, "schedules", before, "cancelled", data, "run_schedule"), "task": None}
    if db.instant(before["due_at"]) > db.instant(tx.now):
        db.fail("invalid_input", "Scheduled operation is not yet due")
    if before["operation"] != "create_task" or sha256(db.encode(before["input"]).encode()).hexdigest() != before["input_digest"]:
        db.fail("invalid_input", "Scheduled authority does not match its exact operation")
    original = tx.context
    try:
        tx.context = replace(original, causation_id=schedule_id)
        task = tasks.create(tx, db.input_as(CreateTask, {"application_id": app["id"], **before["input"], "origin_ref": "schedule:" + schedule_id}))
    finally:
        tx.context = original
    data["task_id"] = task["id"]
    return {"schedule": db.update(tx, "schedules", before, "executed", data, "run_schedule"), "task": task}


def due(connection, now, limit=100):
    db.instant(now)
    if type(limit) is not int or not 1 <= limit <= 200:
        db.fail("invalid_input", "Invalid schedule query bound")
    rows = connection.execute("SELECT id FROM app_schedules WHERE status='pending' AND julianday(json_extract(data,'$.due_at'))<=julianday(?) ORDER BY json_extract(data,'$.due_at'),id LIMIT ?", (now, limit + 1)).fetchall()
    return {"items": [db.record(connection, "schedules", row[0]) for row in rows[:limit]], "truncated": len(rows) > limit}


def reenable_reminder(tx, value):
    _human(tx)
    before = db.record(tx.connection, "reminders", value.reminder_id)
    db.check_version(before, value.expected_version)
    app = db.application(tx.connection, before["application_id"], require_open=True)
    if before["status"] != "pending" or before["pursuit_no"] != app["pursuit_no"] or "migration_provenance" not in before:
        db.fail("invalid_input", "Only an inert imported current pending reminder can be reenabled")
    if db.instant(value.due_at) <= db.instant(tx.now):
        db.fail("invalid_input", "Reenabled reminder requires a future notification time")
    db.nonempty(value.reason, "reason")
    data = db.data_of(before, "reminders")
    data.update(delivery_reenabled=True, next_notification_at=value.due_at, reenabled_by=tx.context.principal.actor_id, reenable_reason=value.reason)
    return db.update(tx, "reminders", before, "pending", data, "reenable_reminder")


def queue_reminders(tx, now=None, limit=100):
    _worker(tx, "scheduled")
    now = now or tx.now
    if db.instant(now) > db.instant(tx.now):
        db.fail("invalid_input", "A caller cannot advance the scheduler clock")
    if type(limit) is not int or not 1 <= limit <= 200:
        db.fail("invalid_input", "Invalid reminder query bound")
    # Absence of migration_provenance identifies reminders created under the new
    # explicit opt-in contract. Importing always adds that field, even if empty.
    rows = tx.connection.execute("""SELECT r.id FROM app_reminders r JOIN app_applications a ON a.id=r.application_id
        WHERE r.status='pending' AND a.disposition='open' AND r.pursuit_no=a.pursuit_no
        AND julianday(json_extract(r.data,'$.next_notification_at'))<=julianday(?)
        AND (json_type(r.data,'$.migration_provenance') IS NULL OR json_extract(r.data,'$.delivery_reenabled')=1)
        AND NOT EXISTS(SELECT 1 FROM app_notification_handoffs h WHERE h.reminder_id=r.id AND h.reminder_version=r.version)
        ORDER BY json_extract(r.data,'$.next_notification_at'),r.id LIMIT ?""", (now, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        reminder = db.record(tx.connection, "reminders", row[0])
        if not _reminder_relevant(tx.connection, reminder):
            data = db.data_of(reminder, "reminders")
            data["resolution_reason"] = "Related obligation is no longer active"
            db.update(tx, "reminders", reminder, "cancelled", data, "invalidate_reminder")
            continue
        delivery_id = "notification_" + uuid4().hex
        tx.connection.execute("INSERT INTO app_notification_handoffs VALUES(?,?,?,?,?,'pending',?,?)", (delivery_id, reminder["id"], reminder["version"], reminder["application_id"], reminder["pursuit_no"], tx.now, tx.now))
        item = {"delivery_id": delivery_id, "reminder_id": reminder["id"], "expected_version": reminder["version"], "application_id": reminder["application_id"], "pursuit_no": reminder["pursuit_no"], "destination": "owner", "not_before": reminder["next_notification_at"], "description": reminder.get("description")}
        tx.record("applications", delivery_id, "queue_due_reminder", None, item)
        tx.enqueue("applications", "deliver_owner_notification", delivery_id, item)
        items.append(item)
    return {"items": items, "truncated": len(rows) > limit}


def delivery(tx, value):
    _worker(tx, "result")
    db.nonempty(value.receipt_id, "receipt_id", 300)
    if type(value.expected_version) is not int or value.expected_version < 1:
        db.fail("invalid_input", "Notification result requires an exact reminder version")
    if value.outcome != "delivered" or db.instant(value.observed_at) > db.instant(tx.now):
        db.fail("invalid_input", "Only an observed successful notification receipt is accepted")
    payload_digest = sha256(db.encode(asdict(value)).encode()).hexdigest()
    existing = tx.connection.execute("SELECT payload_digest,result FROM app_notification_receipts WHERE receipt_id=?", (value.receipt_id,)).fetchone()
    if existing:
        if existing["payload_digest"] != payload_digest:
            db.fail("idempotency_conflict", "Notification receipt identity changed")
        return json.loads(existing["result"])
    handoff = tx.connection.execute("SELECT * FROM app_notification_handoffs WHERE id=?", (value.delivery_id,)).fetchone()
    if handoff is None or handoff["reminder_id"] != value.reminder_id or handoff["reminder_version"] != value.expected_version:
        db.fail("invalid_input", "Notification result must bind its exact durable handoff")
    if db.instant(value.observed_at) < db.instant(handoff["created_at"]):
        db.fail("invalid_input", "Notification observation predates its authorized handoff")
    reminder = db.record(tx.connection, "reminders", value.reminder_id)
    app = db.application(tx.connection, reminder["application_id"])
    conflict = reminder["version"] != value.expected_version or reminder["status"] != "pending" or app["disposition"] != "open" or app["pursuit_no"] != handoff["pursuit_no"] or app["id"] != handoff["application_id"] or not _reminder_relevant(tx.connection, reminder)
    if handoff["status"] != "pending":
        status = handoff["status"]
    elif conflict:
        status = "conflict"
    else:
        data = db.data_of(reminder, "reminders")
        data.update(delivery_id=value.delivery_id, receipt_id=value.receipt_id, delivered_at=value.observed_at)
        db.update(tx, "reminders", reminder, "delivered", data, "record_delivery")
        status = "delivered"
    tx.connection.execute("UPDATE app_notification_handoffs SET status=?,updated_at=? WHERE id=?", (status, tx.now, value.delivery_id))
    result = {"delivery_id": value.delivery_id, "reminder_id": value.reminder_id, "receipt_id": value.receipt_id, "observed_outcome": "delivered", "status": status, "observed_at": value.observed_at}
    tx.connection.execute("INSERT INTO app_notification_receipts VALUES(?,?,?,?,?)", (value.receipt_id, value.delivery_id, payload_digest, db.encode(result), tx.now))
    tx.record("applications", value.delivery_id, "record_notification_receipt", dict(handoff), result)
    return result


def _reminder_relevant(connection, reminder):
    related = reminder.get("related_id")
    if not related:
        return True
    for kind, active in (("tasks", {"open"}), ("interviews", {"requested", "scheduled"})):
        row = connection.execute("SELECT application_id,pursuit_no,status FROM app_" + kind + " WHERE id=?", (related,)).fetchone()
        if row:
            return row["application_id"] == reminder["application_id"] and row["pursuit_no"] == reminder["pursuit_no"] and row["status"] in active
    return False


def get_handoff(connection, delivery_id):
    db.nonempty(delivery_id, "delivery_id", 300)
    row = connection.execute("SELECT * FROM app_notification_handoffs WHERE id=?", (delivery_id,)).fetchone()
    if row is None:
        db.fail("not_found", "Notification handoff does not exist")
    return dict(row)


def delivery_applicable(connection, delivery_id):
    handoff = get_handoff(connection, delivery_id)
    reminder = db.record(connection, "reminders", handoff["reminder_id"])
    app = db.application(connection, reminder["application_id"])
    return (handoff["status"] == "pending" and reminder["status"] == "pending"
            and reminder["version"] == handoff["reminder_version"]
            and app["id"] == handoff["application_id"] and app["disposition"] == "open"
            and app["pursuit_no"] == handoff["pursuit_no"]
            and _reminder_relevant(connection, reminder))


def recovery_summary(connection):
    count = connection.execute("""SELECT COUNT(*) FROM installation_restore_quarantine q
        JOIN app_reminders r ON r.id=q.record_id
        WHERE q.kind='reminder' AND r.status!='delivered' AND NOT EXISTS (
          SELECT 1 FROM app_notification_handoffs h
          JOIN app_notification_receipts receipt ON receipt.delivery_id=h.id
          WHERE h.reminder_id=r.id AND h.reminder_version=r.version)""").fetchone()[0]
    return {"restore_quarantined_reminders": count}
