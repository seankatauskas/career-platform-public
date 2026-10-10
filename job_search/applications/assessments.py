"""Assessment requirements and progress remain distinct from their tasks."""
from . import _store as db
from . import identity, tasks
from .contracts import CreateTask


def record(tx, value, *, updating=False):
    if value.status not in {"requested", "submitted", "completed", "cancelled"}:
        db.fail("invalid_input", "Unsupported assessment status")
    db.nonempty(value.description, "description")
    db.nonempty(value.channel, "channel", 100)
    db.instant(value.due_at, "due_at", optional=True)
    data = {"description": value.description, "channel": value.channel, "due_at": value.due_at, "deadline_text": value.deadline_text, "evidence": list(value.evidence)}
    if value.assessment_id:
        before = db.record(tx.connection, "assessments", value.assessment_id)
        db.check_version(before, value.expected_version)
        app = db.application(tx.connection, before["application_id"], require_open=True)
        if before["pursuit_no"] != app["pursuit_no"]:
            db.fail("version_conflict", "Assessment is historical")
        if value.application_id and db.alias(tx.connection, value.application_id) != app["id"]:
            db.fail("invalid_input", "Assessment belongs to another application")
        after = db.update(tx, "assessments", before, value.status, data, "update_assessment")
        tasks.update_related(tx, before["id"], value.due_at, value.status == "cancelled")
    else:
        if updating:
            db.fail("invalid_input", "Assessment update requires an existing assessment")
        app = identity.ensure(tx, value, require_open=True)
        after = db.insert(tx, "assessments", app, value.status, data, "record_assessment")
    if value.create_task:
        tasks.create(tx, CreateTask(application_id=app["id"], kind="complete_assessment", description=value.description, due_at=value.due_at, origin_ref="assessment:" + after["id"], related_id=after["id"], evidence=value.evidence))
    return after
