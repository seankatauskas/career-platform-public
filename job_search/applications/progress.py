"""Derived stage and reviewed corrections to accepted facts."""
from . import _store as db
from . import identity, reminders, tasks


def fact(tx, value):
    if value.kind not in {"recruiter_contact", "interview_request"}:
        db.fail("invalid_input", "Unsupported progress fact")
    return db.insert(tx, "progress", identity.ensure(tx, value, require_open=True), "active", {"kind": value.kind, "evidence": list(value.evidence)}, "record_progress")


def stage(connection, application_id):
    app = db.application(connection, application_id)
    if app["disposition"] == "closed":
        return {"stage": "closed", "legacy_phase": "terminal", "outcome": app["outcome"]}
    def has(kind, states, extra="", params=()):
        return connection.execute("SELECT 1 FROM app_" + kind + " WHERE application_id=? AND pursuit_no=? AND status IN (" + ",".join("?" for _ in states) + ") " + extra + " LIMIT 1", (app["id"], app["pursuit_no"], *states, *params)).fetchone() is not None
    if has("offers", ("offered", "negotiating")):
        value, legacy = "offer", "offer"
    elif has("interviews", ("requested", "scheduled", "completed")) or has("progress", ("active",), "AND json_extract(data,'$.kind')='interview_request'"):
        value, legacy = "interviewing", "interviewing"
    elif has("submissions", ("confirmed",)) or has("assessments", ("requested", "submitted", "completed")) or has("progress", ("active",), "AND json_extract(data,'$.kind')='recruiter_contact'"):
        value, legacy = "active", "active"
    elif has("submissions", ("attempted",)):
        value, legacy = "submitted", "awaiting_confirmation"
    else:
        value, legacy = "tracking", "preparing"
    return {"stage": value, "legacy_phase": legacy, "outcome": None}


def preview_correction(connection, application_id, references):
    app = db.application(connection, application_id)
    result = []
    for ref in references:
        kind = ref.get("kind")
        if kind not in {"submissions", "tasks", "interviews", "assessments", "offers", "progress", "reminders"}:
            db.fail("invalid_input", "Unsupported correction target")
        before = db.record(connection, kind, ref.get("id"))
        if before["application_id"] != app["id"]:
            db.fail("invalid_input", "Correction target belongs to another application")
        result.append({"kind": kind, "id": before["id"], "expected_version": before["version"], "before": before})
    return {"application_id": app["id"], "records": result}


def correct(tx, value):
    app = db.application(tx.connection, value.application_id)
    db.nonempty(value.reason, "reason")
    if not value.corrections:
        db.fail("invalid_input", "Explicit correction resolutions required")
    checked = []
    seen = set()
    for item in value.corrections:
        if set(item) - {"kind", "id", "expected_version", "resolution"}:
            db.fail("invalid_input", "Unknown correction field")
        preview = preview_correction(tx.connection, app["id"], [item])["records"][0]
        if (item["kind"], item["id"]) in seen:
            db.fail("invalid_input", "Duplicate correction target")
        seen.add((item["kind"], item["id"]))
        before = preview["before"]
        db.check_version(before, item.get("expected_version"))
        if item.get("resolution") not in {"keep", "retract"}:
            db.fail("invalid_input", "Each correction needs an explicit keep/retract decision")
        checked.append((item, before))
    for item, before in checked:
        if item["resolution"] == "retract" and item["kind"] in {"interviews", "assessments", "offers"}:
            related = tx.connection.execute("SELECT id FROM app_tasks WHERE json_extract(data,'$.related_id')=?", (before["id"],)).fetchall()
            if any(("tasks", row[0]) not in seen for row in related):
                db.fail("dependency_unresolved", "Correction requires explicit resolution for every dependent task")
    results = []
    for item, before in checked:
        if item["resolution"] == "keep":
            tx.record("applications", before["id"], "correct_progress_keep", before, {**before, "reason": value.reason})
            results.append(before)
            continue
        kind = item["kind"]
        data = db.data_of(before, kind)
        data["correction_reason"] = value.reason
        status = "retracted" if kind in {"submissions", "progress", "offers"} else "cancelled"
        if kind == "tasks" and before["status"] == "completed":
            # Explicitly preserve completion history while retracting its relevance.
            status = "superseded"
        if kind == "reminders" and before["status"] == "delivered":
            status = "delivered"
        results.append(db.update(tx, kind, before, status, data, "correct_progress"))
        reminders.cancel_for(tx, before["id"], value.reason)
    return {"application_id": app["id"], "records": results, **stage(tx.connection, app["id"])}
