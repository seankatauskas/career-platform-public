"""Immutable browser observations and explicitly accepted submission attempts."""
import json
from dataclasses import asdict
from hashlib import sha256
from . import _store as db
from . import identity


_ATTEMPT_WHERE = "json_extract(data,'$.device_id')=? AND json_extract(data,'$.attempt_ref')=?"


def summary(connection, application_id):
    """Keep confirmation time distinct from an explicitly unknown submission time."""
    app = db.application(connection, application_id)
    row = connection.execute("""SELECT
        MIN(CASE WHEN json_extract(data,'$.click_time_known')=1 OR
            (json_extract(data,'$.click_time_known') IS NOT 0
             AND COALESCE(json_extract(data,'$.attempt_link.status'),'')!='unresolved')
            THEN COALESCE(json_extract(data,'$.occurred_at'),created_at) END) AS submitted_at,
        MIN(CASE WHEN status='confirmed' THEN COALESCE(json_extract(data,'$.occurred_at'),created_at) END) AS confirmed_at,
        SUM(CASE WHEN json_extract(data,'$.attempt_link.status')='unresolved' THEN 1 ELSE 0 END) AS unresolved_attempts
        FROM app_submissions WHERE application_id=? AND pursuit_no=? AND status IN ('attempted','confirmed')""",
        (app["id"], app["pursuit_no"])).fetchone()
    unresolved = row["unresolved_attempts"] or 0
    return {"submitted_at": row["submitted_at"], "confirmed_at": row["confirmed_at"], "unresolved_attempts": unresolved,
            "warnings": [{"code": "unresolved_submission_attempt", "message":
                "A historical submission record has no verified link to a browser attempt. Browser captures remain separate until that link is reviewed."}] if unresolved else []}


def browser_attempt(connection, device_id, attempt_ref, *, limit=100):
    """Bounded arrival-ordered evidence; latest capture is independent of the page."""
    db.nonempty(device_id, "device_id", 300)
    db.nonempty(attempt_ref, "attempt_ref", 300)
    if type(limit) is not int or not 1 <= limit <= 200:
        db.fail("invalid_input", "Invalid browser attempt query limit")
    rows = connection.execute("SELECT id FROM app_observations WHERE " + _ATTEMPT_WHERE + " ORDER BY rowid LIMIT ?", (device_id, attempt_ref, limit + 1)).fetchall()
    if not rows:
        return None
    observations = [db.record(connection, "observations", row[0]) for row in rows[:limit]]
    app = db.application(connection, observations[0]["application_id"])
    latest = connection.execute("SELECT id FROM app_observations WHERE " + _ATTEMPT_WHERE + " AND json_extract(data,'$.activity')='answer_capture' AND json_type(data,'$.source.answer_snapshot') IS NOT NULL ORDER BY rowid DESC LIMIT 1", (device_id, attempt_ref)).fetchone()
    captured = db.record(connection, "observations", latest[0]) if latest else None
    return {"device_id": device_id, "attempt_ref": attempt_ref,
            "application_id": app["id"], "job_id": app["job_id"],
            "observations": observations, "coverage": {"truncated": len(rows) > limit},
            "has_answer_snapshot": captured is not None,
            "latest_answer_snapshot": captured["source"]["answer_snapshot"] if captured else None,
            "latest_answer_observation_id": captured["id"] if captured else None}


def _check_attempt_target(connection, value, app):
    first = connection.execute("SELECT id FROM app_observations WHERE " + _ATTEMPT_WHERE + " ORDER BY rowid LIMIT 1", (value.device_id, value.attempt_ref)).fetchone()
    previous = db.record(connection, "observations", first[0]) if first else None
    if previous and db.alias(connection, previous["application_id"]) != app["id"]:
        db.fail("invalid_input", "Browser attempt belongs to another application")
    paired = value.source.get("adapter") == "paired_extension_v1"
    if paired or previous and previous.get("source", {}).get("adapter") == "paired_extension_v1":
        target = value.source.get("identity")
        if not paired or not isinstance(target, dict):
            db.fail("invalid_input", "Paired browser attempt requires its original identity")
        for key in ("ats", "job_id", "board"):
            db.nonempty(target.get(key), "identity." + key, 1000)
        if value.job_source and (target["ats"] != value.job_source.get("source") or target["job_id"] != value.job_source.get("source_id")):
            db.fail("invalid_input", "Browser identity does not match its job target")
        matched = connection.execute("WITH RECURSIVE identities(id) AS (SELECT ? UNION SELECT a.source_id FROM app_aliases a JOIN identities i ON a.target_id=i.id WHERE a.kind='job') SELECT 1 FROM app_job_sources WHERE job_id IN (SELECT id FROM identities) AND source=? AND source_id=? LIMIT 1", (app["job_id"], target["ats"], target["job_id"])).fetchone()
        if not matched:
            db.fail("invalid_input", "Browser identity does not identify this application's job")
        if previous:
            prior_target = previous.get("source", {}).get("identity", {})
            if any(target[key] != prior_target.get(key) for key in ("ats", "job_id", "board")):
                db.fail("invalid_input", "Browser attempt identity changed")
        elif value.activity != "submission_attempt":
            db.fail("dependency_unresolved", "Paired browser attempt must begin with attempted activity")


def observe(tx, value):
    db.nonempty(value.device_id, "device_id", 300)
    db.nonempty(value.observation_id, "observation_id", 300)
    db.nonempty(value.attempt_ref, "attempt_ref", 300)
    if value.activity not in {"submission_attempt", "website_acknowledgment", "document_selection", "answer_capture", "submission_signal"}:
        db.fail("invalid_input", "Only application activity creates browser evidence")
    db.instant(value.occurred_at, "occurred_at", optional=True)
    if not isinstance(value.source, dict):
        db.fail("invalid_input", "Browser source must be a JSON object")
    data = asdict(value)
    digest = sha256(db.encode(data).encode("utf-8")).hexdigest()
    existing = tx.connection.execute("SELECT record_id,digest FROM app_browser_keys WHERE device_id=? AND observation_id=?", (value.device_id, value.observation_id)).fetchone()
    if existing:
        if existing["digest"] != digest:
            db.fail("idempotency_conflict", "Observation identity reused with different bytes")
        return db.record(tx.connection, "observations", existing["record_id"])
    app = identity.ensure(tx, value)
    _check_attempt_target(tx.connection, value, app)
    data.pop("application_id")
    data.pop("job_source")
    data["source_observation_id"] = data.pop("observation_id")
    data["source_digest"] = digest
    after = db.insert(tx, "observations", app, "recorded", data, "record_browser_observation")
    tx.connection.execute("INSERT INTO app_browser_keys VALUES(?,?,?,?)", (value.device_id, value.observation_id, after["id"], digest))
    tx.enqueue("applications", "analyze_browser", after["id"], {"observation_id": after["id"], "source_digest": digest})
    return after


def _browser_pairs(connection, evidence, application_id):
    pairs = set()
    for item in evidence:
        if item.get("owner") != "applications":
            continue
        row = connection.execute("SELECT id FROM app_observations WHERE id=?", (item.get("source_id"),)).fetchone()
        if row is None:
            continue  # Other application-owned evidence need not be browser activity.
        observation = db.record(connection, "observations", row[0])
        if db.alias(connection, observation["application_id"]) != application_id:
            db.fail("invalid_input", "Submission evidence belongs to another application")
        for key in ("device_id", "attempt_ref"):
            if key in item and item[key] != observation[key]:
                db.fail("invalid_input", "Submission evidence does not match preserved browser identity")
        pairs.add((observation["device_id"], observation["attempt_ref"]))
    return pairs


def _check_unique_attempt(connection, app, evidence, *, excluding=None):
    pairs = _browser_pairs(connection, evidence, app["id"])
    if not pairs:
        return
    # The command transaction serializes writers. Scan current submissions, not a
    # paginated projection, including source-only references from earlier reviews.
    for row in connection.execute("SELECT id FROM app_submissions WHERE application_id=? AND pursuit_no=? AND status!='retracted'", (app["id"], app["pursuit_no"])):
        if row[0] == excluding:
            continue
        other = db.record(connection, "submissions", row[0])
        if pairs & _browser_pairs(connection, other.get("evidence", []), app["id"]):
            db.fail("version_conflict", "Browser attempt already has a current submission; explicitly update that record")


def attach_answers(tx, value):
    before = db.record(tx.connection, "submissions", value.submission_id)
    observation = db.record(tx.connection, "observations", value.observation_id)
    source = observation.get("source", {})
    if observation.get("activity") != "answer_capture" or "answer_snapshot" not in source:
        db.fail("invalid_input", "Answer attachment requires preserved answer-capture evidence")
    appid = db.alias(tx.connection, before["application_id"])
    if db.alias(tx.connection, observation["application_id"]) != appid or (observation["device_id"], observation["attempt_ref"]) not in _browser_pairs(tx.connection, before.get("evidence", []), appid):
        db.fail("invalid_input", "Answer capture does not belong to this submission attempt")
    data = db.data_of(before, "submissions")
    snapshots = list(data.get("answer_snapshots", []))
    if any(item["observation_id"] == observation["id"] for item in snapshots):
        return before
    db.check_version(before, value.expected_version)
    snapshots.append({"observation_id": observation["id"], "snapshot": source["answer_snapshot"]})
    data["answer_snapshots"] = snapshots
    evidence = list(data.get("evidence", []))
    evidence.append({"owner": "applications", "source_id": observation["id"],
                     "revision": str(observation["version"]), "sha256": observation["source_digest"],
                     "device_id": observation["device_id"], "attempt_ref": observation["attempt_ref"]})
    data["evidence"] = evidence
    return db.update(tx, "submissions", before, before["status"], data, "attach_submission_answers")


def record(tx, value, *, confirming=False):
    status = "confirmed" if confirming else value.status
    if status not in {"unreviewed", "attempted", "confirmed", "failed", "retracted"}:
        db.fail("invalid_input", "Unsupported submission status")
    db.instant(value.occurred_at, "occurred_at", optional=True)
    if any(not isinstance(key, str) or not isinstance(text, str) for key, text in value.answers.items()):
        db.fail("invalid_input", "Submission answers must be exact strings")
    if value.submission_id:
        before = db.record(tx.connection, "submissions", value.submission_id)
        db.check_version(before, value.expected_version)
        app = db.application(tx.connection, before["application_id"], require_open=True)
        if value.application_id and db.alias(tx.connection, value.application_id) != app["id"]:
            db.fail("invalid_input", "Submission belongs to another application")
        if before["pursuit_no"] != app["pursuit_no"]:
            db.fail("version_conflict", "Submission belongs to a historical pursuit")
        data = db.data_of(before, "submissions")
        # Confirmation corroborates, never replaces historical answers/documents.
        if value.answers and value.answers != data["answers"] or value.documents and list(value.documents) != data["documents"]:
            db.fail("invalid_input", "Submitted answer/document snapshots are immutable")
        evidence = list(data["evidence"])
        for item in value.evidence:
            if item not in evidence:
                evidence.append(item)
        data["evidence"] = evidence
        _check_unique_attempt(tx.connection, app, evidence, excluding=before["id"])
        after = db.update(tx, "submissions", before, status, data, "confirm_submission" if confirming else "record_submission")
    else:
        app = identity.ensure(tx, value, require_open=True)
        _check_unique_attempt(tx.connection, app, value.evidence)
        after = db.insert(tx, "submissions", app, status, {"occurred_at": value.occurred_at, "answers": value.answers, "documents": list(value.documents), "evidence": list(value.evidence)}, "confirm_submission" if confirming else "record_submission")
    if status in {"attempted", "confirmed"}:
        inserted = tx.connection.execute("INSERT OR IGNORE INTO app_feedback VALUES(?,?,?,?)", (app["id"], app["pursuit_no"], after["id"], tx.now)).rowcount
        if inserted:
            snapshot=tx.connection.execute("SELECT snapshot_json,provenance_json FROM app_job_snapshots WHERE job_id=?",(app["job_id"],)).fetchone()
            if snapshot:
                job,provenance=json.loads(snapshot[0]),json.loads(snapshot[1])
                source={"ats":job["ats"],"job_id":job["job_id"]}
            else:
                rows=tx.connection.execute("SELECT source,source_id FROM app_job_sources WHERE job_id=? ORDER BY source,source_id",(app["job_id"],)).fetchall()
                source={"ats":rows[0]["source"],"job_id":rows[0]["source_id"]} if len(rows)==1 else {}
                provenance={}
            feedback={**source,"policy_id":provenance.get("policy_id","")}
            feedback.update({("recommendation_"+k if k in {"session_id","impression_id","model_run_id","policy_id","rank"} else k):v for k,v in provenance.items()})
            tx.enqueue("applications", "submission_feedback", app["id"] + ":" + str(app["pursuit_no"]), {"application_id": app["id"], "pursuit_no": app["pursuit_no"], "submission_id": after["id"], "feedback":feedback})
    return after
