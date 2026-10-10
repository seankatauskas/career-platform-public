"""Exact local identity and explicit pursuit disposition."""
from uuid import uuid4
from . import _store as db


def ensure(tx, target, *, require_open=False):
    if target.application_id:
        if target.job_source:
            db.fail("invalid_input", "Provide application_id or job_source, not both")
        return db.application(tx.connection, target.application_id, require_open=require_open)
    source = target.job_source
    if not isinstance(source, dict) or set(source) - {"source", "source_id", "employer", "title", "discovery_id"}:
        db.fail("invalid_input", "An exact job source or application is required")
    for name in ("employer", "title"):
        if source.get(name) is not None:
            db.nonempty(source[name], name, 1000)
    if "discovery_id" in source:
        source_kind, source_id = "discovery", db.nonempty(source["discovery_id"], "discovery_id", 300)
        db.nonempty(source.get("employer"), "employer", 500)
        db.nonempty(source.get("title"), "title", 1000)
    else:
        source_kind = db.nonempty(source.get("source"), "source", 100)
        source_id = db.nonempty(source.get("source_id"), "source_id", 500)
    match = tx.connection.execute("SELECT job_id FROM app_job_sources WHERE source=? AND source_id=?", (source_kind, source_id)).fetchone()
    if match:
        job_id = db.alias(tx.connection, match[0], "job")
    else:
        job_id = "job_" + uuid4().hex
        tx.connection.execute("INSERT INTO app_jobs VALUES(?,?,?,?)", (job_id, source.get("employer"), source.get("title"), tx.now))
        tx.connection.execute("INSERT INTO app_job_sources VALUES(?,?,?)", (source_kind, source_id, job_id))
    found = tx.connection.execute("SELECT id FROM app_applications WHERE job_id=?", (job_id,)).fetchone()
    if found:
        return db.application(tx.connection, found[0], require_open=require_open)
    identifier = "app_" + uuid4().hex
    tx.connection.execute("INSERT INTO app_applications VALUES(?,?,'open',NULL,1,1,?,?)", (identifier, job_id, tx.now, tx.now))
    after = db.application(tx.connection, identifier)
    tx.record("applications", identifier, "create_application", None, after)
    return after


def save(tx, value):
    app = ensure(tx, value)
    if value.job_snapshot is not None:
        if not isinstance(value.job_snapshot, dict) or not isinstance(value.recommendation or {}, dict):
            db.fail("invalid_input", "Job snapshot and recommendation must be objects")
        snapshot = value.job_snapshot
        match = tx.connection.execute("WITH RECURSIVE identities(id) AS (SELECT ? UNION SELECT a.source_id FROM app_aliases a JOIN identities i ON a.target_id=i.id WHERE a.kind='job') SELECT 1 FROM app_job_sources WHERE job_id IN (SELECT id FROM identities) AND source=? AND source_id=?", (app["job_id"], snapshot.get("ats"), snapshot.get("job_id"))).fetchone()
        if not match:
            db.fail("invalid_input", "Job snapshot does not match the application's source identity")
        tx.connection.execute("INSERT OR IGNORE INTO app_job_snapshots VALUES(?,?,?,?)",
                              (app["job_id"], db.encode(snapshot), db.encode(value.recommendation or {}), tx.now))
    tx.record("applications", app["id"], "save_job", app, app)
    return app


def note(tx, value):
    db.nonempty(value.text, "text", 100000)
    return db.insert(tx, "notes", ensure(tx, value), "active", {"text": value.text}, "add_note")


def preview_closure(connection, application_id):
    app = db.application(connection, application_id)
    records = []
    for kind, status in (("tasks", "open"), ("reminders", "pending")):
        for row in connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=? AND pursuit_no=? AND status=? ORDER BY id LIMIT 2001", (app["id"], app["pursuit_no"], status)).fetchall():
            if len(records) == 2000:
                db.fail("dependency_unresolved", "Closure exceeds bounded review size")
            record = db.record(connection, kind, row[0])
            records.append({"kind": kind, "id": record["id"], "version": record["version"], "record": record})
    return {"application": app, "expected_version": app["version"], "records": records, "expected_records": {row["kind"] + ":" + row["id"]: row["version"] for row in records}}


def disposition(tx, value, *, reopening=False):
    before = db.application(tx.connection, value.application_id)
    db.check_version(before, value.expected_version)
    db.nonempty(value.reason, "reason")
    if reopening:
        if before["disposition"] != "closed":
            db.fail("invalid_input", "Only a closed application can reopen")
        tx.connection.execute("UPDATE app_applications SET disposition='open',outcome=NULL,pursuit_no=pursuit_no+1,version=version+1,updated_at=? WHERE id=?", (tx.now, before["id"]))
    else:
        db.check_related_versions(value.expected_records, preview_closure(tx.connection, before["id"])["expected_records"])
        if value.outcome not in {"accepted", "rejected", "withdrawn", "stopped_pursuing"}:
            db.fail("invalid_input", "Unsupported closure outcome")
        if before["disposition"] != "open":
            db.fail("invalid_input", "Application already closed")
        tx.connection.execute("UPDATE app_applications SET disposition='closed',outcome=?,version=version+1,updated_at=? WHERE id=?", (value.outcome, tx.now, before["id"]))
        for kind, status in (("tasks", "open"), ("reminders", "pending")):
            rows = tx.connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=? AND pursuit_no=? AND status=?", (before["id"], before["pursuit_no"], status)).fetchall()
            for row in rows:
                old = db.record(tx.connection, kind, row[0])
                data = db.data_of(old, kind)
                data["resolution_reason"] = value.reason
                db.update(tx, kind, old, "cancelled", data, "close_application")
    after = db.application(tx.connection, before["id"])
    tx.record("applications", before["id"], "reopen_application" if reopening else "close_application", before, {**after, "reason": value.reason})
    return after


def preview_combination(connection, source_id, target_id, *, limit=2000):
    if type(limit) is not int or not 1 <= limit <= 2000:
        db.fail("invalid_input", "Invalid combination preview limit")
    source = db.application(connection, source_id)
    target = db.application(connection, target_id)
    if source["id"] == target["id"]:
        db.fail("invalid_input", "Choose two distinct applications")
    records = []
    truncated = False
    for kind in db.KINDS:
        # Source ownership moves in full; target current pursuit is revised.
        for row in connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=? OR (application_id=? AND pursuit_no=?) ORDER BY id LIMIT ?", (source["id"], target["id"], target["pursuit_no"], limit + 1)).fetchall():
            if len(records) == limit:
                truncated = True
                break
            record = db.record(connection, kind, row[0])
            records.append({"kind": kind, "id": record["id"], "version": record["version"], "record": record})
    return {"source": source, "target": target, "records": records, "expected_records": {row["kind"] + ":" + row["id"]: row["version"] for row in records}, "truncated": truncated}


def combine(tx, value):
    source = db.application(tx.connection, value.source_application_id)
    target = db.application(tx.connection, value.target_application_id)
    if source["id"] == target["id"]:
        db.fail("invalid_input", "Choose two distinct applications")
    db.check_version(source, value.source_version)
    db.check_version(target, value.target_version)
    preview = preview_combination(tx.connection, source["id"], target["id"])
    if preview["truncated"]:
        db.fail("dependency_unresolved", "Combination exceeds bounded review limit")
    db.check_related_versions(value.expected_records, preview["expected_records"])
    db.nonempty(value.reason, "reason")
    if value.disposition not in {"open", "closed"} or (value.disposition == "closed" and value.outcome not in {"accepted", "rejected", "withdrawn", "stopped_pursuing"}) or (value.disposition == "open" and value.outcome is not None):
        db.fail("invalid_input", "Explicit combined disposition/outcome required")
    # Give source historical pursuits distinct positive numbers. Both current
    # pursuits join a fresh combined current pursuit; no historical number collides.
    target_pursuit = target["pursuit_no"] + source["pursuit_no"]
    for kind in db.KINDS:
        for row in tx.connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=? AND pursuit_no=?", (target["id"], target["pursuit_no"])).fetchall():
            before = db.record(tx.connection, kind, row[0])
            tx.connection.execute("UPDATE app_" + kind + " SET pursuit_no=?,version=version+1,updated_at=? WHERE id=?", (target_pursuit, tx.now, row[0]))
            tx.record("applications", row[0], "combine_jobs", before, db.record(tx.connection, kind, row[0]))
    tx.connection.execute("UPDATE app_feedback SET pursuit_no=? WHERE application_id=? AND pursuit_no=?", (target_pursuit, target["id"], target["pursuit_no"]))
    for kind in db.KINDS:
        rows = tx.connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=?", (source["id"],)).fetchall()
        for row in rows:
            before = db.record(tx.connection, kind, row[0])
            pursuit = target_pursuit if before["pursuit_no"] == source["pursuit_no"] else target["pursuit_no"] + before["pursuit_no"]
            tx.connection.execute("UPDATE app_" + kind + " SET application_id=?,pursuit_no=?,version=version+1,updated_at=? WHERE id=?", (target["id"], pursuit, tx.now, row[0]))
            tx.record("applications", row[0], "combine_jobs", before, db.record(tx.connection, kind, row[0]))
    for feedback in tx.connection.execute("SELECT * FROM app_feedback WHERE application_id=?", (source["id"],)).fetchall():
        pursuit = target_pursuit if feedback["pursuit_no"] == source["pursuit_no"] else target["pursuit_no"] + feedback["pursuit_no"]
        tx.connection.execute("INSERT OR IGNORE INTO app_feedback VALUES(?,?,?,?)", (target["id"], pursuit, feedback["submission_id"], feedback["created_at"]))
    tx.connection.execute("INSERT INTO app_aliases VALUES(?,?,'application')", (source["id"], target["id"]))
    tx.connection.execute("INSERT INTO app_aliases VALUES(?,?,'job')", (source["job_id"], target["job_id"]))
    tx.connection.execute("UPDATE app_applications SET disposition=?,outcome=?,pursuit_no=?,version=version+1,updated_at=? WHERE id=?", (value.disposition, value.outcome, target_pursuit, tx.now, target["id"]))
    # Historical source row remains addressable only through its canonical alias.
    tx.connection.execute("UPDATE app_applications SET disposition='closed',outcome='stopped_pursuing',version=version+1,updated_at=? WHERE id=?", (tx.now, source["id"]))
    after = db.application(tx.connection, target["id"])
    if value.disposition == "closed":
        for kind, status in (("tasks", "open"), ("reminders", "pending")):
            for row in tx.connection.execute("SELECT id FROM app_" + kind + " WHERE application_id=? AND pursuit_no=? AND status=?", (target["id"], target_pursuit, status)).fetchall():
                before = db.record(tx.connection, kind, row[0])
                db.update(tx, kind, before, "cancelled", db.data_of(before, kind), "combine_jobs")
    tx.record("applications", target["id"], "combine_jobs", {"source": source, "target": target}, {**after, "reason": value.reason})
    return {"application": after, "source_application_id": source["id"], "resolved_application_id": target["id"]}
