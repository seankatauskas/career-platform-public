"""Explicit inert conversion participants; never enqueue runtime work."""
from . import _store as db


def authorize(tx):
    if tx.context.origin != "migration" or tx.context.principal.kind != "worker":
        db.fail("not_authorized", "Historical import requires migration worker authority")


def application(tx, value):
    authorize(tx)
    identifier = db.nonempty(value.get("id"), "application id", 500)
    job_id = db.nonempty(value.get("job_id"), "job id", 500)
    pursuit = value.get("pursuit_no", 1)
    version = value.get("version", 1)
    if type(pursuit) is not int or pursuit < 1 or type(version) is not int or version < 1:
        db.fail("invalid_input", "Historical versions and pursuits must be positive")
    disposition = value.get("disposition", "open")
    outcome = value.get("outcome")
    if disposition not in {"open", "closed"} or (disposition == "closed" and outcome not in {"accepted", "rejected", "withdrawn", "stopped_pursuing"}) or (disposition == "open" and outcome is not None):
        db.fail("invalid_input", "Invalid historical disposition")
    created = value.get("created_at", tx.now)
    updated = value.get("updated_at", created)
    db.instant(created)
    db.instant(updated)
    if tx.connection.execute("SELECT 1 FROM app_applications WHERE id=?", (identifier,)).fetchone():
        db.fail("idempotency_conflict", "Historical application already imported")
    job = value.get("job", {})
    tx.connection.execute("INSERT INTO app_jobs VALUES(?,?,?,?)", (job_id, job.get("employer"), job.get("title"), created))
    for source in job.get("sources", []):
        tx.connection.execute("INSERT INTO app_job_sources VALUES(?,?,?)", (db.nonempty(source.get("source"), "source", 100), db.nonempty(source.get("source_id"), "source_id", 500), job_id))
    if job.get("snapshot") is not None:
        snapshot = job["snapshot"]
        if not isinstance(snapshot, dict) or {"source": snapshot.get("ats"), "source_id": snapshot.get("job_id")} not in job.get("sources", []):
            db.fail("invalid_input", "Imported snapshot does not match its source identity")
        tx.connection.execute("INSERT INTO app_job_snapshots VALUES(?,?,?,?)", (job_id, db.encode(snapshot), db.encode(job.get("recommendation") or {}), created))
    tx.connection.execute("INSERT INTO app_applications VALUES(?,?,?,?,?,?,?,?)", (identifier, job_id, disposition, outcome, pursuit, version, created, updated))
    after = db.application(tx.connection, identifier)
    tx.record("applications", identifier, "import_application", None, {**after, "migration_provenance": value.get("provenance", {})})
    return after


def record(tx, kind, value, provenance=None):
    authorize(tx)
    if kind not in db.KINDS:
        db.fail("invalid_input", "Unsupported historical record kind")
    identifier = db.nonempty(value.get("id"), "record id", 500)
    app = db.application(tx.connection, value.get("application_id"))
    pursuit = value.get("pursuit_no", app["pursuit_no"])
    version = value.get("version", 1)
    if type(pursuit) is not int or not 1 <= pursuit <= app["pursuit_no"] or type(version) is not int or version < 1:
        db.fail("invalid_input", "Invalid historical record pursuit/version")
    statuses = {
        "notes": {"active"}, "tasks": {"open", "completed", "cancelled", "superseded"},
        "submissions": {"unreviewed", "attempted", "confirmed", "failed", "retracted"},
        "observations": {"recorded"}, "interviews": {"requested", "scheduled", "completed", "cancelled"},
        "assessments": {"requested", "submitted", "completed", "cancelled"},
        "offers": {"offered", "negotiating", "accepted", "declined", "expired", "employer_withdrawn", "retracted"},
        "reminders": {"pending", "delivered", "cancelled", "elapsed"}, "progress": {"active", "retracted"},
    }
    status = value.get("status")
    if status not in statuses[kind]:
        db.fail("invalid_input", "Invalid historical record status")
    created = value.get("created_at", tx.now)
    updated = value.get("updated_at", created)
    db.instant(created)
    db.instant(updated)
    data = db.data_of(value, kind)
    data["migration_provenance"] = provenance or {}
    tx.connection.execute("INSERT INTO app_" + kind + " VALUES(?,?,?,?,?,?,?,?)", (identifier, app["id"], pursuit, version, status, db.encode(data), created, updated))
    after = db.record(tx.connection, kind, identifier)
    tx.record("applications", identifier, "import_" + kind, None, after)
    if kind == "submissions" and status in {"attempted", "confirmed"}:
        tx.connection.execute("INSERT OR IGNORE INTO app_feedback VALUES(?,?,?,?)", (app["id"], pursuit, identifier, created))
    if kind == "observations" and all(key in data for key in ("device_id", "source_observation_id", "source_digest")):
        tx.connection.execute("INSERT INTO app_browser_keys VALUES(?,?,?,?)", (data["device_id"], data["source_observation_id"], identifier, data["source_digest"]))
    return after
