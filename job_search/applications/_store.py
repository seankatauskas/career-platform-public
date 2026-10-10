"""Private persistence helpers. Only this owner may mutate app_* domain tables."""
import json
from hashlib import sha256
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from uuid import uuid4
from job_search.commands import DomainError


KINDS = ("notes", "tasks", "submissions", "observations", "interviews", "assessments", "offers", "reminders", "progress", "schedules")
ID_FIELDS = {"notes": "note_id", "tasks": "task_id", "submissions": "submission_id", "observations": "observation_id", "interviews": "interview_id", "assessments": "assessment_id", "offers": "offer_id", "reminders": "reminder_id", "progress": "progress_id"}
ID_FIELDS["schedules"] = "schedule_id"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_jobs (
    id TEXT PRIMARY KEY, employer TEXT, title TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_job_sources (
    source TEXT NOT NULL, source_id TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES app_jobs(id),
    PRIMARY KEY(source, source_id)
);
CREATE TABLE IF NOT EXISTS app_applications (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES app_jobs(id),
    disposition TEXT NOT NULL CHECK(disposition IN ('open','closed')),
    outcome TEXT, pursuit_no INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_aliases (
    source_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_browser_keys (
    device_id TEXT NOT NULL, observation_id TEXT NOT NULL, record_id TEXT NOT NULL,
    digest TEXT NOT NULL, PRIMARY KEY(device_id, observation_id)
);
CREATE TABLE IF NOT EXISTS app_feedback (
    application_id TEXT NOT NULL, pursuit_no INTEGER NOT NULL, submission_id TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(application_id, pursuit_no)
);
CREATE TABLE IF NOT EXISTS app_notification_handoffs (
    id TEXT PRIMARY KEY, reminder_id TEXT NOT NULL, reminder_version INTEGER NOT NULL,
    application_id TEXT NOT NULL, pursuit_no INTEGER NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(reminder_id,reminder_version)
);
CREATE TABLE IF NOT EXISTS app_notification_receipts (
    receipt_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES app_notification_handoffs(id),
    payload_digest TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
);
""" + "\n".join("""
CREATE TABLE IF NOT EXISTS app_%s (
    id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES app_applications(id),
    pursuit_no INTEGER NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
    data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS app_%s_application ON app_%s(application_id, pursuit_no, status);
""" % (kind, kind, kind) for kind in KINDS)

_PRE_SNAPSHOT_SCHEMA = SCHEMA
_JOB_SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_job_snapshots (
    job_id TEXT PRIMARY KEY REFERENCES app_jobs(id),
    snapshot_json TEXT NOT NULL, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
SCHEMA += _JOB_SNAPSHOTS_SCHEMA
SCHEMA_MIGRATIONS = {("applications", sha256(_PRE_SNAPSHOT_SCHEMA.encode()).hexdigest(),
                     sha256(SCHEMA.encode()).hexdigest()): (_JOB_SNAPSHOTS_SCHEMA,)}


def fail(code, message):
    raise DomainError(code, message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def input_as(cls, value):
    try:
        parsed = value if isinstance(value, cls) else cls(**value)
    except (TypeError, ValueError):
        fail("invalid_input", "Invalid " + cls.__name__ + " input")
    if hasattr(parsed, "evidence"):
        if not isinstance(parsed.evidence, (tuple, list)) or len(parsed.evidence) > 100 or any(not isinstance(item, dict) for item in parsed.evidence):
            fail("invalid_input", "Evidence must contain bounded source references")
    return parsed


def nonempty(value, field, maximum=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail("invalid_input", field + " must be nonempty bounded text")
    return value


def instant(value, field="time", optional=False):
    if value is None and optional:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        fail("invalid_input", field + " requires an explicit UTC offset")


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unpack(row):
    if row is None:
        return None
    result = dict(row)
    if "data" in result:
        data = json.loads(result.pop("data"))
        result.update(data)
    return result


def alias(connection, identifier, kind="application"):
    seen = set()
    while identifier not in seen:
        seen.add(identifier)
        row = connection.execute("SELECT target_id FROM app_aliases WHERE source_id=? AND kind=?", (identifier, kind)).fetchone()
        if row is None:
            return identifier
        identifier = row[0]
    fail("invalid_input", "Identity alias cycle")


def application(connection, identifier, *, require_open=False):
    identifier = alias(connection, identifier)
    row = unpack(connection.execute("SELECT * FROM app_applications WHERE id=?", (identifier,)).fetchone())
    if row is None:
        fail("not_found", "Application not found")
    row["application_id"] = row["id"]
    if require_open and row["disposition"] != "open":
        fail("invalid_input", "Reopen application before adding active work")
    return row


def record(connection, kind, identifier):
    if kind not in KINDS:
        fail("invalid_input", "Unsupported application record kind")
    row = unpack(connection.execute("SELECT * FROM app_" + kind + " WHERE id=?", (identifier,)).fetchone())
    if row is None:
        fail("not_found", "Application record not found")
    row[ID_FIELDS[kind]] = row["id"]
    return row


def check_version(row, expected):
    if not isinstance(expected, int) or isinstance(expected, bool) or row["version"] != expected:
        fail("version_conflict", "Record changed since review")


def check_related_versions(expected, actual):
    if expected is None and not actual:
        return
    if not isinstance(expected, dict) or expected != actual or any(type(version) is not int for version in expected.values()):
        fail("version_conflict", "Dependent records changed or were not included in review")


def insert(tx, kind, app, status, data, operation):
    identifier = kind.rstrip("s") + "_" + uuid4().hex
    data = {**data, "causation_id": tx.context.causation_id}
    tx.connection.execute("INSERT INTO app_" + kind + " VALUES(?,?,?,?,?,?,?,?)", (identifier, app["id"], app["pursuit_no"], 1, status, encode(data), tx.now, tx.now))
    after = record(tx.connection, kind, identifier)
    tx.record("applications", identifier, operation, None, after)
    return after


def update(tx, kind, before, status, data, operation):
    data = {**data, "last_causation_id": tx.context.causation_id}
    tx.connection.execute("UPDATE app_" + kind + " SET version=version+1,status=?,data=?,updated_at=? WHERE id=?", (status, encode(data), tx.now, before["id"]))
    after = record(tx.connection, kind, before["id"])
    tx.record("applications", before["id"], operation, before, after)
    return after


def data_of(row, kind):
    return {key: value for key, value in row.items() if key not in {"id", "application_id", "pursuit_no", "version", "status", "created_at", "updated_at", ID_FIELDS[kind]}}


def records(connection, application_id, kind, *, current_only=False, limit=200, after_id=None):
    return query_records(connection, kind, application_id=application_id, current_only=current_only, limit=limit, after_id=after_id)


def query_records(connection, kind, *, application_id=None, statuses=(), starts_after=None, starts_before=None, limit=50, after_id=None, current_only=False):
    if kind not in KINDS or not isinstance(limit, int) or not 1 <= limit <= 200:
        fail("invalid_input", "Invalid record query")
    if not isinstance(statuses, (tuple, list)) or len(statuses) > 10 or any(not isinstance(status, str) or len(status) > 100 for status in statuses):
        fail("invalid_input", "Invalid status filter")
    where = "1=1"
    params = []
    if application_id:
        app = application(connection, application_id)
        where += " AND application_id=?"
        params.append(app["id"])
    if current_only:
        where += " AND pursuit_no=(SELECT pursuit_no FROM app_applications WHERE id=app_" + kind + ".application_id)"
    if statuses:
        where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
        params.extend(statuses)
    if starts_after is not None or starts_before is not None:
        if kind != "interviews":
            fail("invalid_input", "Start filters require interview records")
        instant(starts_after, "starts_after", optional=True)
        instant(starts_before, "starts_before", optional=True)
        if starts_after is not None:
            where += " AND julianday(json_extract(data,'$.start_at'))>=julianday(?)"
            params.append(starts_after)
        if starts_before is not None:
            where += " AND julianday(json_extract(data,'$.start_at'))<julianday(?)"
            params.append(starts_before)
    if after_id is not None:
        where += " AND id>?"
        params.append(after_id)
    params.append(limit + 1)
    rows = connection.execute("SELECT * FROM app_" + kind + " WHERE " + where + " ORDER BY id LIMIT ?", params).fetchall()
    items = [record(connection, kind, row["id"]) for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None, "truncated": len(rows) > limit}
