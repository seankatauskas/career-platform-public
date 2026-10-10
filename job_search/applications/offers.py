"""Offer facts and explicit decisions; closing the application is a workflow."""
from . import _store as db
from . import identity, tasks
from .contracts import CreateTask


STATUSES = {"offered", "negotiating", "accepted", "declined", "expired", "employer_withdrawn"}


def record(tx, value):
    if value.status not in STATUSES or not isinstance(value.terms, dict):
        db.fail("invalid_input", "Unsupported offer status or terms")
    data = {"terms": value.terms, "evidence": list(value.evidence)}
    if value.offer_id:
        before = db.record(tx.connection, "offers", value.offer_id)
        db.check_version(before, value.expected_version)
        app = db.application(tx.connection, before["application_id"], require_open=True)
        if before["pursuit_no"] != app["pursuit_no"]:
            db.fail("version_conflict", "Offer is historical")
        if value.application_id and db.alias(tx.connection, value.application_id) != app["id"]:
            db.fail("invalid_input", "Offer belongs to another application")
        after = db.update(tx, "offers", before, value.status, data, "record_offer")
    else:
        app = identity.ensure(tx, value, require_open=True)
        after = db.insert(tx, "offers", app, value.status, data, "record_offer")
    if value.create_task:
        tasks.create(tx, CreateTask(application_id=app["id"], kind="offer_decision", description="Review offer", related_id=after["id"], origin_ref="offer:" + after["id"], evidence=value.evidence))
    return after


def decide(tx, value):
    before = db.record(tx.connection, "offers", value.offer_id)
    db.check_version(before, value.expected_version)
    app = db.application(tx.connection, before["application_id"], require_open=True)
    if before["pursuit_no"] != app["pursuit_no"]:
        db.fail("version_conflict", "Offer is historical")
    if value.status not in STATUSES:
        db.fail("invalid_input", "Unsupported offer decision")
    db.nonempty(value.reason, "reason")
    data = db.data_of(before, "offers")
    data["decision_reason"] = value.reason
    return db.update(tx, "offers", before, value.status, data, "decide_offer")
