"""Notification scheduling. Delivery never changes an obligation's status."""
from datetime import timedelta
from . import _store as db


def cancel_for(tx, related_id, reason):
    rows = tx.connection.execute("SELECT id FROM app_reminders WHERE status='pending' AND json_extract(data,'$.related_id')=?", (related_id,)).fetchall()
    for row in rows:
        before = db.record(tx.connection, "reminders", row[0])
        data = db.data_of(before, "reminders")
        data["resolution_reason"] = reason
        db.update(tx, "reminders", before, "cancelled", data, "cancel_reminder")


def schedule(tx, app, related_id, at, kind, *, import_quiet=False, description=None):
    parsed = db.instant(at, "reminder time")
    status = "elapsed" if parsed <= db.instant(tx.now) or import_quiet else "pending"
    reminder = db.insert(tx, "reminders", app, status, {"related_id": related_id, "at": at, "next_notification_at": at, "kind": kind, "description": description}, "schedule_reminder")
    if status == "pending":
        tx.enqueue("applications", "reminder", reminder["id"], {"reminder_id": reminder["id"], "version": reminder["version"], "not_before": at})
    return reminder


def for_interview(tx, app, interview):
    cancel_for(tx, interview["id"], "Interview scheduling revised")
    if interview["status"] == "scheduled" and interview["reminders_enabled"]:
        at = db.instant(interview["start_at"])
        for hours in (24, 1):
            schedule(tx, app, interview["id"], db.timestamp(at - timedelta(hours=hours)), "interview")


def snooze(tx, task, until):
    db.instant(until, "snooze time")
    if db.instant(until) <= db.instant(tx.now):
        db.fail("invalid_input", "Snooze must be in the future")
    for row in tx.connection.execute("SELECT id FROM app_reminders WHERE status='pending' AND json_extract(data,'$.related_id')=?", (task["id"],)).fetchall():
        before = db.record(tx.connection, "reminders", row[0])
        data = db.data_of(before, "reminders")
        data["next_notification_at"] = until
        after = db.update(tx, "reminders", before, "pending", data, "snooze_reminder")
        tx.enqueue("applications", "reminder", after["id"] + ":" + str(after["version"]), {"reminder_id": after["id"], "version": after["version"], "not_before": until})


def mark_delivered(tx, reminder_id, expected_version):
    before = db.record(tx.connection, "reminders", reminder_id)
    db.check_version(before, expected_version)
    app = db.application(tx.connection, before["application_id"], require_open=True)
    if before["status"] != "pending" or before["pursuit_no"] != app["pursuit_no"]:
        db.fail("version_conflict", "Reminder is no longer pending")
    if db.instant(before["next_notification_at"]) > db.instant(tx.now):
        db.fail("invalid_input", "Reminder is not yet due")
    return db.update(tx, "reminders", before, "delivered", db.data_of(before, "reminders"), "reminder_delivered")
