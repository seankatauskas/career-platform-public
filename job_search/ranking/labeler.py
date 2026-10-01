#!/usr/bin/env python3
"""Local, dependency-free web UI for labeling jobs in job-boards.db.

The app intentionally makes no network requests. It reads normalized job metadata
already collected by job_search/collection/boards.py and writes human preference labels back to the
same SQLite database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import secrets
import sqlite3
import struct
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = Path(__file__).resolve().parent / "web"
INTERESTS = {"interested", "maybe", "not_interested", "skipped"}
DECISIVE_INTERESTS = {"interested", "not_interested"}
QUALIFICATION_FITS = {"", "strong", "plausible", "stretch", "unlikely", "unclear"}
PRIMARY_REASONS = {
    "",
    "role_work",
    "domain",
    "skills",
    "growth_scope",
    "company",
    "location",
    "employment_terms",
    "compensation",
}
HARD_BLOCKERS = {
    "location",
    "compensation",
    "work_authorization",
    "clearance",
    "travel",
    "schedule",
    "employment_terms",
}
SKIP_REASONS = {
    "other",
    "insufficient_information",
    "duplicate",
    "not_a_job",
    "wrong_language",
}
PROGRAM_STAGES = (
    {
        "id": 1,
        "name": "Calibration",
        "quota": 50,
        "sample_role": "training",
        "description": "Practice one consistent Pursue or Pass judgment on varied recent jobs.",
    },
    {
        "id": 2,
        "name": "Foundation",
        "quota": 150,
        "sample_role": "training",
        "description": "Build the first broad map of the work, domains, and environments you prefer.",
    },
    {
        "id": 3,
        "name": "Expansion",
        "quota": 300,
        "sample_role": "training",
        "description": "Cover more role families and sharpen the boundary between Pursue and Pass.",
    },
    {
        "id": 4,
        "name": "Refinement",
        "quota": 300,
        "sample_role": "training",
        "description": "Add depth and less obvious examples before the preference model is evaluated.",
    },
    {
        "id": 5,
        "name": "Evaluation",
        "quota": 200,
        "sample_role": "evaluation",
        "description": "Create a protected holdout set used to measure ranking quality, not train it.",
    },
)
PROGRAM_TOTAL = sum(stage["quota"] for stage in PROGRAM_STAGES)
MAX_BODY_BYTES = 64 * 1024
DEFAULT_DATASET_VERSION = "preference-v1"
FEEDBACK_ACTIONS = {
    "applied", "saved", "dismissed_preference", "blocked", "duplicate",
}


PREFERENCE_TABLE = """
CREATE TABLE job_preferences (
    ats             TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    interest        TEXT NOT NULL CHECK (
                        interest IN (
                            'interested', 'maybe', 'not_interested', 'skipped'
                        )
                    ),
    skip_reason     TEXT NOT NULL DEFAULT '' CHECK (
                        skip_reason IN (
                            '', 'other', 'insufficient_information', 'duplicate',
                            'not_a_job', 'wrong_language'
                        )
                    ),
    qualification_fit TEXT NOT NULL DEFAULT '' CHECK (
                        qualification_fit IN (
                            '', 'strong', 'plausible', 'stretch', 'unlikely', 'unclear'
                        )
                    ),
    primary_reason  TEXT NOT NULL DEFAULT '' CHECK (
                        primary_reason IN (
                            '', 'role_work', 'domain', 'skills', 'growth_scope',
                            'company', 'location', 'employment_terms', 'compensation'
                        )
                    ),
    hard_blockers   TEXT NOT NULL DEFAULT '[]',
    program_stage   INTEGER NOT NULL CHECK (program_stage BETWEEN 1 AND 5),
    sample_role     TEXT NOT NULL CHECK (sample_role IN ('training', 'evaluation')),
    note            TEXT NOT NULL DEFAULT '',
    dataset_version TEXT NOT NULL DEFAULT 'preference-v1',
    current_example_id INTEGER,
    labeled_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (ats, job_id)
)
"""
PREFERENCE_INDEX = (
    "CREATE INDEX IF NOT EXISTS job_preferences_interest "
    "ON job_preferences(interest, updated_at)"
)
PREFERENCE_STAGE_INDEX = (
    "CREATE INDEX IF NOT EXISTS job_preferences_stage "
    "ON job_preferences(program_stage, interest, updated_at)"
)

PRODUCT_SCHEMA = """
CREATE TABLE IF NOT EXISTS preference_datasets (
    dataset_version TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'collecting' CHECK (
                        status IN ('collecting', 'frozen')
                    ),
    description     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preference_examples (
    example_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_version         TEXT NOT NULL,
    ats                     TEXT NOT NULL,
    job_id                  TEXT NOT NULL,
    interest                TEXT NOT NULL,
    skip_reason             TEXT NOT NULL DEFAULT '',
    qualification_fit       TEXT NOT NULL DEFAULT '',
    primary_reason          TEXT NOT NULL DEFAULT '',
    hard_blockers           TEXT NOT NULL DEFAULT '[]',
    program_stage           INTEGER NOT NULL,
    sample_role             TEXT NOT NULL CHECK (
                                sample_role IN ('training', 'evaluation')
                            ),
    note                    TEXT NOT NULL DEFAULT '',
    title_snapshot          TEXT NOT NULL DEFAULT '',
    description_snapshot    TEXT NOT NULL DEFAULT '',
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    family_id               TEXT NOT NULL DEFAULT '',
    template_cluster_id     TEXT NOT NULL DEFAULT '',
    leakage_group_id        TEXT NOT NULL DEFAULT '',
    source_fingerprint      TEXT NOT NULL,
    selection_strategy      TEXT NOT NULL DEFAULT 'legacy',
    selection_probability  REAL,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (dataset_version) REFERENCES preference_datasets(dataset_version)
);

CREATE INDEX IF NOT EXISTS preference_examples_source
ON preference_examples(ats, job_id, created_at);
CREATE INDEX IF NOT EXISTS preference_examples_split
ON preference_examples(dataset_version, sample_role, interest);

CREATE TABLE IF NOT EXISTS recommendation_feedback (
    feedback_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ats                  TEXT NOT NULL,
    job_id               TEXT NOT NULL,
    family_id            TEXT NOT NULL DEFAULT '',
    action               TEXT NOT NULL CHECK (
                             action IN (
                                 'applied', 'saved', 'dismissed_preference',
                                 'blocked', 'duplicate'
                             )
                         ),
    model_run_id         TEXT NOT NULL DEFAULT '',
    policy_id            TEXT NOT NULL DEFAULT 'champion',
    session_id           TEXT NOT NULL DEFAULT '',
    impression_id        INTEGER,
    recommendation_rank  INTEGER,
    semantic_score       REAL,
    ranking_score        REAL,
    implicit_weight      REAL NOT NULL DEFAULT 0,
    title_snapshot       TEXT NOT NULL DEFAULT '',
    description_snapshot TEXT NOT NULL DEFAULT '',
    metadata_json        TEXT NOT NULL DEFAULT '{}',
    template_cluster_id  TEXT NOT NULL DEFAULT '',
    leakage_group_id     TEXT NOT NULL DEFAULT '',
    source_fingerprint   TEXT NOT NULL DEFAULT '',
    note                 TEXT NOT NULL DEFAULT '',
    source_event_id      TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recommendation_feedback_job
ON recommendation_feedback(ats, job_id, created_at);

CREATE TABLE IF NOT EXISTS recommendation_sessions (
    session_id       TEXT PRIMARY KEY,
    policy_id        TEXT NOT NULL,
    model_runs_json  TEXT NOT NULL,
    options_json     TEXT NOT NULL,
    idempotency_key  TEXT,
    actor            TEXT NOT NULL DEFAULT 'dashboard',
    result_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendation_impressions (
    impression_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES recommendation_sessions(session_id),
    position        INTEGER NOT NULL,
    policy_id       TEXT NOT NULL,
    model_run_id    TEXT NOT NULL,
    ats             TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    family_id       TEXT NOT NULL,
    semantic_score  REAL NOT NULL,
    ranking_score   REAL NOT NULL,
    shown_at        TEXT NOT NULL,
    UNIQUE(session_id,position)
);
CREATE INDEX IF NOT EXISTS recommendation_impressions_job
ON recommendation_impressions(ats,job_id,shown_at);

CREATE TABLE IF NOT EXISTS preference_evaluation_queue (
    dataset_version     TEXT NOT NULL,
    position            INTEGER NOT NULL CHECK (position BETWEEN 1 AND 200),
    family_id           TEXT NOT NULL,
    ats                 TEXT NOT NULL,
    job_id              TEXT NOT NULL,
    template_cluster_id TEXT NOT NULL,
    leakage_group_id    TEXT NOT NULL,
    source_fingerprint  TEXT NOT NULL,
    title_snapshot      TEXT NOT NULL,
    description_snapshot TEXT NOT NULL,
    metadata_json       TEXT NOT NULL,
    selection_strategy  TEXT NOT NULL CHECK (
                            selection_strategy IN (
                                'protected_top_ranked', 'protected_uniform',
                                'protected_company_holdout'
                            )
                        ),
    model_run_id        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (dataset_version, position),
    UNIQUE (dataset_version, family_id)
);

CREATE TABLE IF NOT EXISTS preference_label_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ats             TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    example_id      INTEGER,
    action          TEXT NOT NULL CHECK (action IN ('create','update','undo')),
    dataset_version TEXT NOT NULL,
    sample_role     TEXT NOT NULL,
    interest        TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    occurred_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS preference_label_events_job
ON preference_label_events(ats,job_id,occurred_at);

CREATE TABLE IF NOT EXISTS preference_selection_tokens (
    token                  TEXT PRIMARY KEY,
    dataset_version        TEXT NOT NULL,
    program_stage          INTEGER NOT NULL,
    sample_role            TEXT NOT NULL,
    ats                    TEXT NOT NULL,
    job_id                 TEXT NOT NULL,
    family_id              TEXT NOT NULL,
    template_cluster_id    TEXT NOT NULL,
    leakage_group_id       TEXT NOT NULL,
    source_fingerprint     TEXT NOT NULL,
    selection_strategy     TEXT NOT NULL,
    selection_probability REAL,
    evaluation_position    INTEGER,
    issued_at              TEXT NOT NULL,
    consumed_at            TEXT
);
CREATE INDEX IF NOT EXISTS preference_selection_tokens_job
ON preference_selection_tokens(ats,job_id,consumed_at);

CREATE VIEW IF NOT EXISTS preference_training_examples AS
SELECT e.* FROM preference_examples e
JOIN job_preferences p ON p.current_example_id=e.example_id
WHERE e.sample_role='training'
  AND e.interest IN ('interested', 'not_interested');

CREATE VIEW IF NOT EXISTS preference_evaluation_examples AS
SELECT e.* FROM preference_examples e
JOIN job_preferences p ON p.current_example_id=e.example_id
WHERE e.sample_role='evaluation'
  AND e.interest IN ('interested', 'not_interested');
"""

IMMUTABLE_EXAMPLE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS preference_examples_no_update
BEFORE UPDATE ON preference_examples
BEGIN
    SELECT RAISE(ABORT, 'preference examples are immutable');
END;
CREATE TRIGGER IF NOT EXISTS preference_examples_no_delete
BEFORE DELETE ON preference_examples
BEGIN
    SELECT RAISE(ABORT, 'preference examples are immutable');
END;
CREATE TRIGGER IF NOT EXISTS preference_evaluation_queue_no_update
BEFORE UPDATE ON preference_evaluation_queue
BEGIN
    SELECT RAISE(ABORT, 'preference evaluation queue is immutable');
END;
CREATE TRIGGER IF NOT EXISTS preference_evaluation_queue_no_delete
BEFORE DELETE ON preference_evaluation_queue
BEGIN
    SELECT RAISE(ABORT, 'preference evaluation queue is immutable');
END;
CREATE TRIGGER IF NOT EXISTS preference_label_events_no_update
BEFORE UPDATE ON preference_label_events
BEGIN
    SELECT RAISE(ABORT, 'preference label events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS preference_label_events_no_delete
BEFORE DELETE ON preference_label_events
BEGIN
    SELECT RAISE(ABORT, 'preference label events are immutable');
END;
"""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def _create_preferences(con: sqlite3.Connection) -> None:
    con.execute(PREFERENCE_TABLE)
    con.execute(PREFERENCE_INDEX)
    con.execute(PREFERENCE_STAGE_INDEX)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def _family_context(
    con: sqlite3.Connection, ats: str, job_id: str,
) -> dict[str, str]:
    """Return dedupe identifiers when prepared, otherwise safe singleton values."""
    fallback = f"{ats}:{job_id}"
    result = {
        "family_id": fallback,
        "template_cluster_id": fallback,
        "leakage_group_id": fallback,
        "source_fingerprint": "",
    }
    if not _table_exists(con, "job_family_members"):
        return result
    row = con.execute(
        "SELECT family_id, source_fingerprint FROM job_family_members "
        "WHERE ats=? AND job_id=?", (ats, job_id),
    ).fetchone()
    if not row:
        return result
    result["family_id"] = str(row["family_id"])
    result["template_cluster_id"] = result["family_id"]
    result["leakage_group_id"] = result["family_id"]
    result["source_fingerprint"] = str(row["source_fingerprint"] or "")
    if _table_exists(con, "job_template_clusters"):
        cluster = con.execute(
            "SELECT template_cluster_id, leakage_group_id "
            "FROM job_template_clusters WHERE family_id=?",
            (result["family_id"],),
        ).fetchone()
        if cluster:
            result["template_cluster_id"] = str(
                cluster["template_cluster_id"] or result["family_id"]
            )
            result["leakage_group_id"] = str(
                cluster["leakage_group_id"] or result["family_id"]
            )
    return result


def _source_fingerprint(job: sqlite3.Row | dict[str, Any]) -> str:
    keys = (
        "ats", "id", "company", "title", "department", "team",
        "employmentType", "location", "isRemote", "workplaceType",
        "publishedAt", "jobUrl", "description",
    )
    payload = {key: (job[key] if key in job.keys() else "") or "" for key in keys}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _snapshot_metadata(job: sqlite3.Row | dict[str, Any]) -> str:
    fields = (
        "company", "department", "team", "employmentType", "location",
        "isRemote", "workplaceType", "publishedAt", "jobUrl", "matched",
        "first_seen", "last_seen",
    )
    return json.dumps(
        {key: (job[key] if key in job.keys() else "") or "" for key in fields},
        sort_keys=True, ensure_ascii=False,
    )


def _insert_example(
    con: sqlite3.Connection,
    *,
    job: sqlite3.Row | dict[str, Any] | None,
    ats: str,
    job_id: str,
    interest: str,
    skip_reason: str,
    qualification_fit: str,
    primary_reason: str,
    hard_blockers: str,
    program_stage: int,
    sample_role: str,
    note: str,
    dataset_version: str,
    selection_strategy: str,
    selection_probability: float | None,
    created_at: str,
    frozen_family: dict[str, str] | None = None,
) -> int:
    if job is None:
        job = {"ats": ats, "id": job_id}
    family = frozen_family or _family_context(con, ats, job_id)
    fingerprint = family.get("source_fingerprint", "") or _source_fingerprint(job)
    cursor = con.execute(
        "INSERT INTO preference_examples "
        "(dataset_version,ats,job_id,interest,skip_reason,qualification_fit,"
        "primary_reason,hard_blockers,program_stage,sample_role,note,title_snapshot,"
        "description_snapshot,metadata_json,family_id,template_cluster_id,"
        "leakage_group_id,source_fingerprint,selection_strategy,"
        "selection_probability,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            dataset_version, ats, job_id, interest, skip_reason,
            qualification_fit, primary_reason, hard_blockers, program_stage,
            sample_role, note, (job["title"] if "title" in job.keys() else "") or "",
            (job["description"] if "description" in job.keys() else "") or "",
            _snapshot_metadata(job), family["family_id"],
            family["template_cluster_id"], family["leakage_group_id"],
            fingerprint, selection_strategy, selection_probability, created_at,
        ),
    )
    return int(cursor.lastrowid)


def _prepare_product_schema(con: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con.executescript(PRODUCT_SCHEMA)
    queue_columns = {
        str(row[1]) for row in con.execute(
            "PRAGMA table_info(preference_evaluation_queue)"
        )
    }
    for name in ("title_snapshot", "description_snapshot", "metadata_json"):
        if name not in queue_columns:
            default = "'{}'" if name == "metadata_json" else "''"
            con.execute(
                f"ALTER TABLE preference_evaluation_queue ADD COLUMN {name} "
                f"TEXT NOT NULL DEFAULT {default}"
            )
    feedback_columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(recommendation_feedback)")
    }
    feedback_additions = {
        "policy_id": "TEXT NOT NULL DEFAULT 'champion'",
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "impression_id": "INTEGER",
        "semantic_score": "REAL",
        "ranking_score": "REAL",
        "implicit_weight": "REAL NOT NULL DEFAULT 0",
        "title_snapshot": "TEXT NOT NULL DEFAULT ''",
        "description_snapshot": "TEXT NOT NULL DEFAULT ''",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "template_cluster_id": "TEXT NOT NULL DEFAULT ''",
        "leakage_group_id": "TEXT NOT NULL DEFAULT ''",
        "source_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "source_event_id": "TEXT",
    }
    for name, declaration in feedback_additions.items():
        if name not in feedback_columns:
            con.execute(
                f"ALTER TABLE recommendation_feedback ADD COLUMN {name} {declaration}"
            )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS recommendation_feedback_source_event "
        "ON recommendation_feedback(source_event_id) "
        "WHERE source_event_id IS NOT NULL"
    )
    session_columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(recommendation_sessions)")
    }
    session_additions = {
        "idempotency_key": "TEXT",
        "actor": "TEXT NOT NULL DEFAULT 'dashboard'",
        "result_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for name, declaration in session_additions.items():
        if name not in session_columns:
            con.execute(
                f"ALTER TABLE recommendation_sessions ADD COLUMN {name} {declaration}"
            )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS recommendation_sessions_idempotency "
        "ON recommendation_sessions(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    con.execute(
        "INSERT OR IGNORE INTO preference_datasets "
        "(dataset_version,status,description,created_at) VALUES (?,?,?,?)",
        (
            DEFAULT_DATASET_VERSION, "collecting",
            "Personal interest training and protected evaluation snapshots", now,
        ),
    )
    rows = con.execute(
        "SELECT p.*, j.* FROM job_preferences p LEFT JOIN jobs j "
        "ON j.ats=p.ats AND j.id=p.job_id WHERE p.current_example_id IS NULL"
    ).fetchall()
    for row in rows:
        # Duplicate column names resolve to the preference-side ats/job_id, which is
        # exactly what legacy rows without a surviving source job need.
        example_id = _insert_example(
            con, job=row, ats=row["ats"], job_id=row["job_id"],
            interest=row["interest"], skip_reason=row["skip_reason"],
            qualification_fit=row["qualification_fit"],
            primary_reason=row["primary_reason"], hard_blockers=row["hard_blockers"],
            program_stage=row["program_stage"], sample_role=row["sample_role"],
            note=row["note"], dataset_version=row["dataset_version"],
            selection_strategy="legacy", selection_probability=None,
            created_at=row["labeled_at"] or now,
        )
        con.execute(
            "UPDATE job_preferences SET current_example_id=? WHERE ats=? AND job_id=?",
            (example_id, row["ats"], row["job_id"]),
        )
    con.executescript(IMMUTABLE_EXAMPLE_TRIGGERS)


def _migrate_preferences(con: sqlite3.Connection, columns: set[str]) -> None:
    """Rebuild an older preference schema without losing existing labels."""
    con.execute("DROP VIEW IF EXISTS preference_training_examples")
    con.execute("DROP VIEW IF EXISTS preference_evaluation_examples")
    con.execute("DROP INDEX IF EXISTS job_preferences_interest")
    con.execute("DROP INDEX IF EXISTS job_preferences_stage")
    con.execute("ALTER TABLE job_preferences RENAME TO job_preferences_legacy")
    _create_preferences(con)

    interest = (
        "CASE WHEN interest IN ('interested','maybe','not_interested','skipped') "
        "THEN interest ELSE 'not_interested' END"
        if "interest" in columns else "'not_interested'"
    )
    if "qualification_fit" in columns:
        qualification = (
            "CASE WHEN qualification_fit IN "
            "('','strong','plausible','stretch','unlikely','unclear') "
            "THEN qualification_fit ELSE '' END"
        )
    elif "seniority_fit" in columns:
        qualification = (
            "CASE seniority_fit "
            "WHEN 'right_level' THEN 'strong' "
            "WHEN 'too_junior' THEN 'strong' "
            "WHEN 'stretch' THEN 'stretch' "
            "WHEN 'too_senior' THEN 'unlikely' "
            "WHEN 'unclear' THEN 'unclear' ELSE '' END"
        )
    else:
        qualification = "''"

    reason_source = "primary_reason" if "primary_reason" in columns else (
        "signal" if "signal" in columns else "''"
    )
    reason = (
        f"CASE WHEN {reason_source} IN "
        "('','role_work','domain','skills','growth_scope','company','location',"
        "'employment_terms','compensation') "
        f"THEN {reason_source} ELSE '' END"
    )
    legacy_blocker = (
        "CASE WHEN hard_blocker IN "
        "('location','compensation','work_authorization','clearance',"
        "'travel','schedule','employment_terms') "
        "THEN '[\"' || hard_blocker || '\"]' ELSE '[]' END"
        if "hard_blocker" in columns else "'[]'"
    )
    blockers = "hard_blockers" if "hard_blockers" in columns else legacy_blocker
    skip_reason = "skip_reason" if "skip_reason" in columns else "''"
    program_stage = "program_stage" if "program_stage" in columns else "1"
    sample_role = "sample_role" if "sample_role" in columns else "'training'"
    note = "note" if "note" in columns else "''"
    dataset_version = (
        "dataset_version" if "dataset_version" in columns
        else f"'{DEFAULT_DATASET_VERSION}'"
    )
    current_example_id = "current_example_id" if "current_example_id" in columns else "NULL"
    labeled_at = "labeled_at" if "labeled_at" in columns else "''"
    updated_at = "updated_at" if "updated_at" in columns else labeled_at
    con.execute(
        "INSERT INTO job_preferences "
        "(ats, job_id, interest, skip_reason, qualification_fit, primary_reason, "
        "hard_blockers, program_stage, sample_role, note, dataset_version, "
        "current_example_id, labeled_at, updated_at) "
        f"SELECT ats, job_id, {interest}, {skip_reason}, {qualification}, {reason}, "
        f"{blockers}, {program_stage}, {sample_role}, {note}, {dataset_version}, "
        f"{current_example_id}, {labeled_at}, "
        f"{updated_at} FROM job_preferences_legacy"
    )
    con.execute("DROP TABLE job_preferences_legacy")


def prepare_preferences(db_path: Path) -> None:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    with connect(db_path) as con:
        jobs = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if not jobs:
            raise ValueError(f"database has no jobs table: {db_path}")
        existing = con.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='job_preferences'"
        ).fetchone()
        if not existing:
            _create_preferences(con)
            _prepare_product_schema(con)
            return
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(job_preferences)")
        }
        required = {
            "qualification_fit", "primary_reason", "hard_blockers", "skip_reason",
            "program_stage", "sample_role", "dataset_version", "current_example_id",
        }
        table_sql = existing["sql"] or ""
        if not required <= columns or "'skipped'" not in table_sql:
            _migrate_preferences(con, columns)
        else:
            con.execute(PREFERENCE_INDEX)
            con.execute(PREFERENCE_STAGE_INDEX)
        _prepare_product_schema(con)


def _filters(params: dict[str, list[str]]) -> dict[str, Any]:
    ats = params.get("ats", [""])[0].strip().lower()
    if ats not in {"", "ashby", "greenhouse", "lever"}:
        raise ApiError("unknown ATS filter")

    raw_days = params.get("days", ["30"])[0].strip().lower()
    if raw_days in {"", "all"}:
        days = None
    else:
        try:
            days = int(raw_days)
        except ValueError as exc:
            raise ApiError("days must be 7, 30, 90, 365, or all") from exc
        if days not in {7, 30, 90, 365}:
            raise ApiError("days must be 7, 30, 90, 365, or all")

    remote = params.get("remote", ["false"])[0].lower() in {"1", "true", "yes"}
    search = params.get("search", [""])[0].strip()[:100]
    return {"ats": ats, "days": days, "remote": remote, "search": search}


def _eligible_where(filters: dict[str, Any], unlabeled: bool) -> tuple[str, list[Any]]:
    clauses = [
        "j.closed_at IS NULL",
        "LENGTH(TRIM(COALESCE(j.description,''))) > 0",
    ]
    values: list[Any] = []
    if unlabeled:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM job_preferences p "
            "WHERE p.ats=j.ats AND p.job_id=j.id)"
        )
    if filters["ats"]:
        clauses.append("LOWER(j.ats)=?")
        values.append(filters["ats"])
    if filters["days"] is not None:
        clauses.append("datetime(j.publishedAt) >= datetime('now', ?)")
        values.append(f"-{filters['days']} days")
    if filters["remote"]:
        clauses.append(
            "(LOWER(COALESCE(j.isRemote,'')) IN ('true','1','yes') "
            "OR LOWER(COALESCE(j.workplaceType,''))='remote' "
            "OR LOWER(COALESCE(j.location,'')) LIKE '%remote%')"
        )
    if filters["search"]:
        clauses.append(
            "LOWER(COALESCE(j.title,'') || ' ' || COALESCE(j.company,'') || ' ' || "
            "COALESCE(j.department,'') || ' ' || COALESCE(j.team,'')) LIKE ?"
        )
        values.append(f"%{filters['search'].lower()}%")
    return " AND ".join(clauses), values


def _program_status(con: sqlite3.Connection) -> dict[str, Any]:
    counts = con.execute(
        "SELECT COUNT(*) AS labeled, "
        "SUM(CASE WHEN interest='interested' THEN 1 ELSE 0 END) AS interested, "
        "SUM(CASE WHEN interest='maybe' THEN 1 ELSE 0 END) AS maybe, "
        "SUM(CASE WHEN interest='not_interested' THEN 1 ELSE 0 END) AS not_interested, "
        "SUM(CASE WHEN interest='skipped' THEN 1 ELSE 0 END) AS skipped "
        "FROM job_preferences"
    ).fetchone()
    values = {key: counts[key] or 0 for key in counts.keys()}
    decisive = values["interested"] + values["not_interested"]
    before = 0
    active: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = []
    for definition in PROGRAM_STAGES:
        completed = min(max(decisive - before, 0), definition["quota"])
        stage = {
            **definition,
            "number": definition["id"],
            "completed": completed,
            "remaining": definition["quota"] - completed,
            "complete": completed == definition["quota"],
        }
        stages.append(stage)
        if active is None and not stage["complete"]:
            active = stage
        before += definition["quota"]

    complete = decisive >= PROGRAM_TOTAL
    return {
        "complete": complete,
        "decisive": decisive,
        "overall_total": PROGRAM_TOTAL,
        "overall_remaining": max(PROGRAM_TOTAL - decisive, 0),
        "overall_percent": min(round(decisive * 100 / PROGRAM_TOTAL, 1), 100.0),
        "stage": None if complete else active,
        "stage_count": len(PROGRAM_STAGES),
        "stages": stages,
        "counts": values,
        "class_targets": {
            "interested": {"target": 200, "met": values["interested"] >= 200},
            "not_interested": {
                "target": 400,
                "met": values["not_interested"] >= 400,
            },
        },
    }


def program_status(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as con:
        return _program_status(con)


def _candidate_scores(
    preference_db_path: Path | None,
    family_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if (
        preference_db_path is None
        or not preference_db_path.exists()
        or not family_ids
    ):
        return {}
    try:
        with connect(preference_db_path) as con:
            if not _table_exists(con, "preference_state") or not _table_exists(
                con, "preference_scores"
            ):
                return {}
            champion = con.execute(
                "SELECT value FROM preference_state WHERE key='champion_run_id'"
            ).fetchone()
            if not champion:
                return {}
            scores: dict[str, dict[str, Any]] = {}
            for index in range(0, len(family_ids), 400):
                group = family_ids[index:index + 400]
                marks = ",".join("?" for _ in group)
                rows = con.execute(
                    "SELECT family_id,final_score,dense_linear_score,"
                    "dense_neighbor_score,sparse_score FROM preference_scores "
                    f"WHERE run_id=? AND family_id IN ({marks})",
                    (champion["value"], *group),
                ).fetchall()
                scores.update({str(row["family_id"]): dict(row) for row in rows})
            return scores
    except sqlite3.Error:
        return {}


def _active_title_vectors(
    preference_db_path: Path | None,
    family_ids: list[str],
) -> dict[str, list[float]]:
    if preference_db_path is None or not preference_db_path.exists() or not family_ids:
        return {}
    try:
        with connect(preference_db_path) as con:
            if not _table_exists(con, "preference_state"):
                return {}
            state = {
                str(row["key"]): str(row["value"])
                for row in con.execute(
                    "SELECT key,value FROM preference_state "
                    "WHERE key IN ('embedding_model_revision','text_version')"
                )
            }
            if not state.get("embedding_model_revision") or not state.get("text_version"):
                return {}
            return _title_vectors(
                con, list(dict.fromkeys(family_ids)),
                state["embedding_model_revision"], state["text_version"],
            )
    except sqlite3.Error:
        return {}


def _cosine_similarity(
    left: list[float] | None, right: list[float] | None,
) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def _selection_policy(decisive: int, has_scores: bool) -> tuple[str, float]:
    if decisive < 200:
        slot = decisive % 10
        return ("diversity", 0.70) if slot < 7 else ("uniform", 0.30)
    if not has_scores:
        slot = decisive % 10
        return (
            ("diversity_fallback", 0.70) if slot < 7
            else ("uniform_fallback", 0.30)
        )
    slot = (decisive - 200) % 20
    if slot < 7:
        return "uncertainty", 0.35
    if slot < 12:
        return "dense_sparse_disagreement", 0.25
    if slot < 16:
        return "high_score", 0.20
    return "uniform_audit", 0.20


def _choose_protected_evaluation(
    con: sqlite3.Connection,
    preference_db_path: Path | None,
    where: str,
    values: list[Any],
    columns: str,
) -> dict[str, Any] | None:
    queue_count = con.execute(
        "SELECT COUNT(*) FROM preference_evaluation_queue WHERE dataset_version=?",
        (DEFAULT_DATASET_VERSION,),
    ).fetchone()[0]
    if queue_count not in {0, 200}:
        raise ApiError("protected evaluation queue is incomplete; restore its database", 409)
    model: dict[str, Any] | None = None
    if queue_count == 0:
        if preference_db_path is None:
            raise ApiError("protected evaluation requires a champion model", 409)
        model = _model_status(preference_db_path)
        if not model["ready"]:
            raise ApiError(
                "protected evaluation requires a scored champion model; train, score, "
                "and promote one before continuing",
                409,
            )
        if not _table_exists(con, "job_family_members") or not _table_exists(
            con, "job_template_clusters"
        ):
            raise ApiError("protected evaluation requires prepared job families", 409)
    lineage_join = ""
    labeled_template = (
        "COALESCE(labeled_t.template_cluster_id,labeled_e.template_cluster_id)"
    )
    if _table_exists(con, "job_template_cluster_lineage"):
        lineage_join = (
            "LEFT JOIN job_template_cluster_lineage labeled_lineage ON "
            "labeled_lineage.template_cluster_id="
            "COALESCE(labeled_t.template_cluster_id,labeled_e.template_cluster_id) "
        )
        labeled_template = (
            "COALESCE(labeled_lineage.lineage_id,labeled_t.template_cluster_id,"
            "labeled_e.template_cluster_id)"
        )
    leakage_exclusion = (
        "AND NOT EXISTS ("
        "SELECT 1 FROM job_preferences labeled_p "
        "JOIN preference_examples labeled_e "
        "ON labeled_p.current_example_id=labeled_e.example_id "
        "LEFT JOIN job_family_members labeled_m "
        "ON labeled_m.ats=labeled_p.ats AND labeled_m.job_id=labeled_p.job_id "
        "LEFT JOIN job_template_clusters labeled_t "
        "ON labeled_t.family_id=labeled_m.family_id "
        f"{lineage_join}"
        "WHERE COALESCE(labeled_t.leakage_group_id,labeled_e.leakage_group_id)="
        "t.leakage_group_id OR "
        f"{labeled_template}="
        "t.template_cluster_id) "
    )
    if queue_count == 0:
        assert model is not None and preference_db_path is not None
        company_seen = (
            "EXISTS (SELECT 1 FROM job_preferences training_p "
            "JOIN preference_examples training_e "
            "ON training_p.current_example_id=training_e.example_id "
            "WHERE training_p.sample_role='training' "
            "AND training_p.interest IN ('interested','not_interested') "
            "AND LOWER(COALESCE(json_extract(training_e.metadata_json,'$.company'),''))="
            "LOWER(COALESCE(j.company,''))) AS company_seen_training"
        )
        base = (
            "WITH eligible AS (SELECT "
            f"{columns},m.family_id,m.source_fingerprint,"
            "t.template_cluster_id,t.leakage_group_id,s.final_score,"
            f"{company_seen},ROW_NUMBER() OVER (PARTITION BY m.family_id "
            "ORDER BY j.ats,j.id) AS member_rank "
            "FROM jobs j JOIN job_family_members m "
            "ON m.ats=j.ats AND m.job_id=j.id "
            "JOIN job_template_clusters t ON t.family_id=m.family_id "
            "JOIN preference_sidecar.preference_scores s ON s.family_id=m.family_id "
            "AND s.run_id=? "
            f"WHERE {where} {leakage_exclusion}) "
            "SELECT * FROM eligible WHERE member_rank=1 "
        )
        used_families: set[str] = set()
        used_templates: set[str] = set()
        used_leakage: set[str] = set()
        frozen: list[tuple[sqlite3.Row, str]] = []

        def take(query: str, strategy: str, quota: int) -> None:
            for row in con.execute(
                query, (model["champion_run_id"], *values),
            ):
                if len([item for item in frozen if item[1] == strategy]) >= quota:
                    break
                family_id = str(row["family_id"])
                template_id = str(row["template_cluster_id"])
                leakage_id = str(row["leakage_group_id"])
                if (
                    family_id in used_families
                    or template_id in used_templates
                    or leakage_id in used_leakage
                ):
                    continue
                used_families.add(family_id)
                used_templates.add(template_id)
                used_leakage.add(leakage_id)
                frozen.append((row, strategy))

        con.execute("ATTACH DATABASE ? AS preference_sidecar", (str(preference_db_path),))
        try:
            take(
                base + "ORDER BY final_score DESC,family_id,ats,id",
                "protected_top_ranked", 100,
            )
            take(
                base + "ORDER BY family_id,ats,id",
                "protected_uniform", 50,
            )
            take(
                base + "AND company_seen_training=0 ORDER BY family_id,ats,id",
                "protected_company_holdout", 50,
            )
        finally:
            con.execute("DETACH DATABASE preference_sidecar")
        counts = {
            strategy: sum(1 for _, value in frozen if value == strategy)
            for strategy in (
                "protected_top_ranked", "protected_uniform",
                "protected_company_holdout",
            )
        }
        if counts != {
            "protected_top_ranked": 100,
            "protected_uniform": 50,
            "protected_company_holdout": 50,
        }:
            raise ApiError(
                "cannot freeze protected evaluation queue: need 100 ranked, "
                "50 uniform, and 50 company-holdout leakage-safe families; "
                f"found {counts}",
                409,
            )
        now = datetime.now(timezone.utc).isoformat()
        con.executemany(
            "INSERT INTO preference_evaluation_queue "
            "(dataset_version,position,family_id,ats,job_id,template_cluster_id,"
            "leakage_group_id,source_fingerprint,title_snapshot,description_snapshot,"
            "metadata_json,selection_strategy,model_run_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    DEFAULT_DATASET_VERSION, position, row["family_id"], row["ats"],
                    row["id"], row["template_cluster_id"], row["leakage_group_id"],
                    row["source_fingerprint"], row["title"] or "",
                    row["description"] or "", _snapshot_metadata(row), strategy,
                    model["champion_run_id"], now,
                )
                for position, (row, strategy) in enumerate(frozen, 1)
            ],
        )
        con.execute(
            "UPDATE preference_datasets SET status='frozen' WHERE dataset_version=?",
            (DEFAULT_DATASET_VERSION,),
        )

    queued = con.execute(
        "SELECT q.* FROM preference_evaluation_queue q "
        "WHERE q.dataset_version=? AND NOT EXISTS ("
        "SELECT 1 FROM job_preferences p JOIN preference_examples e "
        "ON p.current_example_id=e.example_id WHERE e.family_id=q.family_id) "
        "ORDER BY q.position LIMIT 1",
        (DEFAULT_DATASET_VERSION,),
    ).fetchone()
    if not queued:
        return None
    try:
        metadata = json.loads(queued["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    selected = {
        "ats": queued["ats"], "id": queued["job_id"],
        "title": queued["title_snapshot"],
        "description": queued["description_snapshot"],
        **(metadata if isinstance(metadata, dict) else {}),
    }
    selected.update({
        "family_id": queued["family_id"],
        "template_cluster_id": queued["template_cluster_id"],
        "leakage_group_id": queued["leakage_group_id"],
        "source_fingerprint": queued["source_fingerprint"],
        "selection_strategy": queued["selection_strategy"],
        "evaluation_position": queued["position"],
        "model_run_id": queued["model_run_id"],
    })
    selected["selection_probability"] = 1.0
    selected["dataset_version"] = DEFAULT_DATASET_VERSION
    return selected


def _issue_selection_token(
    con: sqlite3.Connection,
    selected: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    stage = status["stage"]
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    dataset_version = selected.get("dataset_version", DEFAULT_DATASET_VERSION)
    # A newly served card is the sole authoritative assignment.  This avoids
    # two browser tabs recording different answers against the same program
    # position (especially the head of the protected evaluation queue).
    con.execute(
        "UPDATE preference_selection_tokens SET consumed_at=? "
        "WHERE dataset_version=? AND consumed_at IS NULL",
        (now, dataset_version),
    )
    con.execute(
        "INSERT INTO preference_selection_tokens "
        "(token,dataset_version,program_stage,sample_role,ats,job_id,family_id,"
        "template_cluster_id,leakage_group_id,source_fingerprint,selection_strategy,"
        "selection_probability,evaluation_position,issued_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            token, dataset_version,
            stage["id"], stage["sample_role"], selected["ats"], selected["id"],
            selected.get("family_id", f"{selected['ats']}:{selected['id']}"),
            selected.get("template_cluster_id", ""),
            selected.get("leakage_group_id", ""),
            selected.get("source_fingerprint", ""),
            selected.get("selection_strategy", "manual"),
            selected.get("selection_probability"),
            selected.get("evaluation_position"), now,
        ),
    )
    result = dict(selected)
    result["selection_token"] = token
    result["selection_sample_role"] = stage["sample_role"]
    result["selection_program_stage"] = stage["id"]
    return result


def choose_job(
    db_path: Path,
    filters: dict[str, Any],
    preference_db_path: Path | None = None,
) -> dict[str, Any] | None:
    where, values = _eligible_where(filters, unlabeled=True)
    columns = (
        "j.ats, j.id, j.company, j.title, j.department, j.team, "
        "j.employmentType, j.location, j.isRemote, j.workplaceType, "
        "j.publishedAt, j.jobUrl, j.description, j.matched, j.first_seen, j.last_seen"
    )
    with connect(db_path) as con:
        status = _program_status(con)
        if status["complete"]:
            return None
        stage = status["stage"]
        if stage["sample_role"] == "evaluation":
            evaluation_where, evaluation_values = _eligible_where(
                {"ats": "", "days": 30, "remote": False, "search": ""},
                unlabeled=True,
            )
            selected = _choose_protected_evaluation(
                con, preference_db_path, evaluation_where, evaluation_values, columns,
            )
            return None if selected is None else _issue_selection_token(
                con, selected, status,
            )
        ordering = "RANDOM()"
        raw_candidates = con.execute(
            f"SELECT {columns} FROM jobs j WHERE {where} ORDER BY {ordering} LIMIT 512",
            values,
        ).fetchall()
        if not raw_candidates:
            return None
        if _table_exists(con, "job_family_members"):
            labeled_query = (
                "SELECT DISTINCT COALESCE(m.family_id,e.family_id) "
                "FROM job_preferences p "
                "JOIN preference_examples e ON p.current_example_id=e.example_id "
                "LEFT JOIN job_family_members m ON m.ats=p.ats AND m.job_id=p.job_id"
            )
        else:
            labeled_query = (
                "SELECT DISTINCT e.family_id FROM preference_examples e "
                "JOIN job_preferences p ON p.current_example_id=e.example_id"
            )
        labeled_families = {row[0] for row in con.execute(labeled_query) if row[0]}
        if _table_exists(con, "job_family_members") and _table_exists(
            con, "job_template_clusters"
        ):
            lineage_select = "COALESCE(t.template_cluster_id,e.template_cluster_id)"
            lineage_join = ""
            if _table_exists(con, "job_template_cluster_lineage"):
                lineage_select = (
                    "COALESCE(lineage.lineage_id,t.template_cluster_id,"
                    "e.template_cluster_id)"
                )
                lineage_join = (
                    " LEFT JOIN job_template_cluster_lineage lineage ON "
                    "lineage.template_cluster_id="
                    "COALESCE(t.template_cluster_id,e.template_cluster_id)"
                )
            split_query = (
                "SELECT DISTINCT COALESCE(t.leakage_group_id,e.leakage_group_id),"
                f"{lineage_select} "
                "FROM job_preferences p "
                "JOIN preference_examples e ON p.current_example_id=e.example_id "
                "LEFT JOIN job_family_members m ON m.ats=p.ats AND m.job_id=p.job_id "
                "LEFT JOIN job_template_clusters t ON t.family_id=m.family_id"
                f"{lineage_join}"
            )
        else:
            split_query = (
                "SELECT DISTINCT e.leakage_group_id,e.template_cluster_id "
                "FROM preference_examples e "
                "JOIN job_preferences p ON p.current_example_id=e.example_id"
            )
        labeled_splits = con.execute(split_query).fetchall()
        labeled_leakage_groups = {row[0] for row in labeled_splits if row[0]}
        labeled_template_groups = {row[1] for row in labeled_splits if row[1]}
        candidates: list[dict[str, Any]] = []
        seen_families: set[str] = set()
        for raw in raw_candidates:
            family = _family_context(con, raw["ats"], raw["id"])
            family_id = family["family_id"]
            if (
                family_id in labeled_families
                or family_id in seen_families
                or family["leakage_group_id"] in labeled_leakage_groups
                or family["template_cluster_id"] in labeled_template_groups
            ):
                continue
            candidate = dict(raw)
            candidate.update(family)
            candidates.append(candidate)
            seen_families.add(family_id)
        if not candidates:
            return None

        used = con.execute(
            "SELECT LOWER(COALESCE(j.company,'')) AS company, "
            "LOWER(COALESCE(j.title,'')) AS title "
            "FROM job_preferences p JOIN jobs j ON j.ats=p.ats AND j.id=p.job_id "
            "WHERE p.program_stage=?",
            (stage["id"],),
        ).fetchall()
        used_companies = {row["company"] for row in used if row["company"]}
        used_titles = {row["title"] for row in used if row["title"]}

        def repetition(row: dict[str, Any]) -> int:
            return (
                int((row["company"] or "").lower() in used_companies)
                + int((row["title"] or "").lower() in used_titles)
            )

        scores = _candidate_scores(
            preference_db_path, [row["family_id"] for row in candidates]
        )
        available = {
            row["family_id"]: scores[row["family_id"]]
            for row in candidates if row["family_id"] in scores
        }
        strategy, mass = _selection_policy(status["decisive"], bool(available))

        def value(row: dict[str, Any], key: str, default: float = 0.0) -> float:
            raw = available.get(row["family_id"], {}).get(key)
            try:
                return float(raw) if raw is not None else default
            except (TypeError, ValueError):
                return default

        scored = [row for row in candidates if row["family_id"] in available]
        if strategy == "uncertainty":
            selected = min(scored, key=lambda row: abs(value(row, "final_score") - 0.5))
        elif strategy == "dense_sparse_disagreement":
            def disagreement(row: dict[str, Any]) -> float:
                parts = [
                    value(row, "dense_linear_score"),
                    value(row, "dense_neighbor_score"),
                    value(row, "sparse_score"),
                ]
                return max(parts) - min(parts)
            selected = max(scored, key=disagreement)
        elif strategy == "high_score":
            selected = max(scored, key=lambda row: value(row, "final_score"))
        elif strategy in {"diversity", "diversity_fallback"}:
            vectors = _active_title_vectors(
                preference_db_path,
                [row["family_id"] for row in candidates] + list(labeled_families),
            )
            labeled_vectors = [
                vectors[family_id] for family_id in labeled_families
                if family_id in vectors
            ]
            vector_candidates = [
                row for row in candidates if row["family_id"] in vectors
            ]
            if labeled_vectors and vector_candidates:
                selected = min(vector_candidates, key=lambda row: (
                    max(
                        _cosine_similarity(vectors[row["family_id"]], labeled)
                        for labeled in labeled_vectors
                    ),
                    repetition(row),
                ))
            else:
                selected = min(candidates, key=repetition)
                if strategy == "diversity":
                    strategy = "diversity_fallback"
        else:
            selected = candidates[0]

        selected["selection_strategy"] = strategy
        # This is the randomized strategy-allocation probability, not an
        # unsupported item-level propensity for the deterministic selector.
        selected["selection_probability"] = mass
        selected["dataset_version"] = DEFAULT_DATASET_VERSION
        return _issue_selection_token(con, selected, status)


def label_stats(db_path: Path, filters: dict[str, Any]) -> dict[str, Any]:
    where, values = _eligible_where(filters, unlabeled=False)
    with connect(db_path) as con:
        status = _program_status(con)
        eligible = con.execute(
            f"SELECT COUNT(*) FROM jobs j WHERE {where}", values
        ).fetchone()[0]
        unlabeled = con.execute(
            f"SELECT COUNT(*) FROM jobs j WHERE {where} AND NOT EXISTS "
            "(SELECT 1 FROM job_preferences p WHERE p.ats=j.ats AND p.job_id=j.id)",
            values,
        ).fetchone()[0]
    return {
        **status["counts"],
        "eligible": eligible,
        "unlabeled": unlabeled,
        "program": status,
    }


def save_label(
    db_path: Path,
    payload: dict[str, Any],
    preference_db_path: Path | None = None,
) -> dict[str, Any]:
    ats = str(payload.get("ats", "")).strip().lower()
    job_id = str(payload.get("id", "")).strip()
    interest = str(payload.get("interest", "")).strip()
    skip_reason = str(payload.get("skip_reason", "")).strip()
    qualification = str(payload.get("qualification_fit", "")).strip()
    reason = str(payload.get("primary_reason", "")).strip()
    raw_blockers = payload.get("hard_blockers", payload.get("hard_blocker", []))
    if isinstance(raw_blockers, str):
        raw_blockers = [] if raw_blockers in {"", "none"} else [raw_blockers]
    if not isinstance(raw_blockers, list):
        raise ApiError("hard blockers must be a list")
    blockers = list(dict.fromkeys(str(value).strip() for value in raw_blockers if value))
    note = str(payload.get("note", "")).strip()[:1000]
    selection_token = str(payload.get("selection_token", "")).strip()
    if not ats or not job_id:
        raise ApiError("ats and id are required")
    if not selection_token:
        raise ApiError("a valid selection token is required", 409)
    if interest not in INTERESTS:
        raise ApiError("interest must be interested, maybe, not_interested, or skipped")
    if interest == "skipped":
        skip_reason = skip_reason or "other"
        if skip_reason not in SKIP_REASONS:
            raise ApiError("unknown skip reason")
    else:
        skip_reason = ""
    if qualification not in QUALIFICATION_FITS:
        raise ApiError("unknown qualification fit")
    if reason not in PRIMARY_REASONS:
        raise ApiError("unknown primary reason")
    if any(blocker not in HARD_BLOCKERS for blocker in blockers):
        raise ApiError("unknown hard blocker")

    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        # Serialize stage/head validation with token consumption and the label
        # write.  A second request must observe the first request's decision.
        con.execute("BEGIN IMMEDIATE")
        before = _program_status(con)
        if before["complete"]:
            raise ApiError("the labeling program is already complete")
        token = con.execute(
            "SELECT * FROM preference_selection_tokens "
            "WHERE token=? AND consumed_at IS NULL", (selection_token,),
        ).fetchone()
        if not token:
            raise ApiError("selection token is invalid, expired, or already used", 409)
        if token["ats"] != ats or token["job_id"] != job_id:
            raise ApiError("selection token does not match this job", 409)
        if token["dataset_version"] != DEFAULT_DATASET_VERSION:
            raise ApiError("selection token belongs to a different dataset", 409)
        active_stage = before["stage"]
        if (
            token["program_stage"] != active_stage["id"]
            or token["sample_role"] != active_stage["sample_role"]
        ):
            raise ApiError("selection token is stale because the program stage advanced", 409)

        queued: sqlite3.Row | None = None
        if active_stage["sample_role"] == "evaluation":
            queued = con.execute(
                "SELECT q.* FROM preference_evaluation_queue q "
                "WHERE q.dataset_version=? AND NOT EXISTS ("
                "SELECT 1 FROM job_preferences p JOIN preference_examples e "
                "ON p.current_example_id=e.example_id WHERE e.family_id=q.family_id) "
                "ORDER BY q.position LIMIT 1",
                (DEFAULT_DATASET_VERSION,),
            ).fetchone()
            if not queued:
                raise ApiError("protected evaluation queue has no available item", 409)
            if (
                token["evaluation_position"] != queued["position"]
                or token["family_id"] != queued["family_id"]
                or ats != queued["ats"] or job_id != queued["job_id"]
            ):
                raise ApiError("selection token is not the next protected evaluation item", 409)
            try:
                frozen_metadata = json.loads(queued["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                frozen_metadata = {}
            job: sqlite3.Row | dict[str, Any] = {
                "ats": ats, "id": job_id,
                "title": queued["title_snapshot"],
                "description": queued["description_snapshot"],
                **(frozen_metadata if isinstance(frozen_metadata, dict) else {}),
            }
            frozen_family = {
                "family_id": str(queued["family_id"]),
                "template_cluster_id": str(queued["template_cluster_id"]),
                "leakage_group_id": str(queued["leakage_group_id"]),
                "source_fingerprint": str(queued["source_fingerprint"]),
            }
        else:
            job = con.execute(
                "SELECT * FROM jobs WHERE ats=? AND id=?", (ats, job_id)
            ).fetchone()
            if not job:
                raise ApiError("job not found", 404)
            frozen_family = {
                "family_id": str(token["family_id"]),
                "template_cluster_id": str(token["template_cluster_id"]),
                "leakage_group_id": str(token["leakage_group_id"]),
                "source_fingerprint": str(token["source_fingerprint"]),
            }
        selection_strategy = str(token["selection_strategy"])
        selection_probability = token["selection_probability"]
        consumed = con.execute(
            "UPDATE preference_selection_tokens SET consumed_at=? "
            "WHERE token=? AND consumed_at IS NULL",
            (now, selection_token),
        )
        if consumed.rowcount != 1:
            raise ApiError("selection token was already used", 409)
        existing = con.execute(
            "SELECT program_stage, sample_role FROM job_preferences "
            "WHERE ats=? AND job_id=?", (ats, job_id)
        ).fetchone()
        if existing:
            program_stage = existing["program_stage"]
            sample_role = existing["sample_role"]
        else:
            program_stage = before["stage"]["id"]
            sample_role = before["stage"]["sample_role"]
        dataset_version = DEFAULT_DATASET_VERSION
        example_id = _insert_example(
            con, job=job, ats=ats, job_id=job_id, interest=interest,
            skip_reason=skip_reason, qualification_fit=qualification,
            primary_reason=reason, hard_blockers=json.dumps(blockers),
            program_stage=program_stage, sample_role=sample_role, note=note,
            dataset_version=dataset_version,
            selection_strategy=selection_strategy or "manual",
            selection_probability=selection_probability, created_at=now,
            frozen_family=frozen_family,
        )
        con.execute(
            "INSERT INTO job_preferences "
            "(ats, job_id, interest, skip_reason, qualification_fit, primary_reason, "
            "hard_blockers, program_stage, sample_role, note, dataset_version, "
            "current_example_id, labeled_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ats, job_id) DO UPDATE SET "
            "interest=excluded.interest, "
            "skip_reason=excluded.skip_reason, "
            "qualification_fit=excluded.qualification_fit, "
            "primary_reason=excluded.primary_reason, "
            "hard_blockers=excluded.hard_blockers, note=excluded.note, "
            "dataset_version=excluded.dataset_version, "
            "current_example_id=excluded.current_example_id, "
            "updated_at=excluded.updated_at",
            (
                ats, job_id, interest, skip_reason, qualification, reason,
                json.dumps(blockers), program_stage, sample_role, note,
                dataset_version, example_id, now, now,
            ),
        )
        event_payload = {
            "interest": interest, "skip_reason": skip_reason,
            "qualification_fit": qualification, "primary_reason": reason,
            "hard_blockers": blockers, "note": note,
            "selection_strategy": selection_strategy,
            "selection_probability": selection_probability,
            "evaluation_position": token["evaluation_position"],
        }
        con.execute(
            "INSERT INTO preference_label_events "
            "(ats,job_id,example_id,action,dataset_version,sample_role,interest,"
            "payload_json,occurred_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ats, job_id, example_id, "update" if existing else "create",
                dataset_version, sample_role, interest,
                json.dumps(event_payload, sort_keys=True), now,
            ),
        )
        after = _program_status(con)
    before_stage = before["stage"]["id"] if before["stage"] else None
    after_stage = after["stage"]["id"] if after["stage"] else None
    return {
        "ok": True,
        "ats": ats,
        "id": job_id,
        "program": after,
        "stage_transition": before_stage != after_stage,
    }


def undo_last(db_path: Path) -> dict[str, Any] | None:
    with connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM job_preferences "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "interest": row["interest"],
            "skip_reason": row["skip_reason"],
            "qualification_fit": row["qualification_fit"],
            "primary_reason": row["primary_reason"],
            "hard_blockers": json.loads(row["hard_blockers"] or "[]"),
            "note": row["note"],
            "labeled_at": row["labeled_at"],
            "updated_at": row["updated_at"],
        }
        con.execute(
            "INSERT INTO preference_label_events "
            "(ats,job_id,example_id,action,dataset_version,sample_role,interest,"
            "payload_json,occurred_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["ats"], row["job_id"], row["current_example_id"], "undo",
                row["dataset_version"], row["sample_role"], row["interest"],
                json.dumps(payload, sort_keys=True), now,
            ),
        )
        con.execute(
            "DELETE FROM job_preferences WHERE ats=? AND job_id=?",
            (row["ats"], row["job_id"]),
        )
    return {
        "ats": row["ats"], "job_id": row["job_id"],
        "interest": row["interest"],
    }


def _recommendation_options(
    params: dict[str, list[str]],
    default_limit: int = 20,
    default_salary_floor: float | None = None,
) -> dict[str, Any]:
    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = params.get(name, [str(default)])[0]
        try:
            value = int(raw)
        except ValueError as exc:
            raise ApiError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ApiError(f"{name} must be between {minimum} and {maximum}")
        return value

    limit = integer("limit", default_limit, 1, 100)
    days = integer("days", 30, 1, 3650)
    max_company = integer("max_per_company", min(2, limit), 1, limit)
    max_title = integer("max_per_title", min(2, limit), 1, limit)
    raw_floor = params.get(
        "salary_floor", ["" if default_salary_floor is None else str(default_salary_floor)]
    )[0].strip()
    try:
        salary_floor = None if not raw_floor else float(raw_floor)
    except ValueError as exc:
        raise ApiError("salary_floor must be a non-negative annual USD amount") from exc
    if salary_floor is not None and salary_floor < 0:
        raise ApiError("salary_floor must be a non-negative annual USD amount")
    remote_only = params.get("remote_only", ["false"])[0].lower() in {
        "1", "true", "yes",
    }
    policy = params.get("policy", ["champion"])[0].strip().lower()
    if policy not in {"champion", "selective", "broad", "compare"}:
        raise ApiError("policy must be champion, selective, broad, or compare")
    return {
        "limit": limit,
        "days": days,
        "salary_floor": salary_floor,
        "remote_only": remote_only,
        "max_per_company": max_company,
        "max_per_title": max_title,
        "policy": policy,
    }


def _model_status(
    preference_db_path: Path, requested_run_id: str | None = None,
) -> dict[str, Any]:
    empty = {
        "ready": False,
        "champion_run_id": None,
        "score_count": 0,
        "model_revision": None,
        "text_version": None,
        "training_examples": 0,
        "created_at": None,
        "manifest": {},
    }
    if not preference_db_path.exists():
        return empty
    try:
        with connect(preference_db_path) as con:
            if not all(_table_exists(con, name) for name in (
                "preference_state", "preference_scores", "preference_model_runs",
            )):
                return empty
            if requested_run_id:
                run_id = requested_run_id
            else:
                state = con.execute(
                    "SELECT value FROM preference_state WHERE key='champion_run_id'"
                ).fetchone()
                if not state:
                    return empty
                run_id = str(state["value"])
            run = con.execute(
                "SELECT * FROM preference_model_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not run:
                return empty
            count = con.execute(
                "SELECT COUNT(*) FROM preference_scores WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            try:
                manifest = json.loads(run["manifest_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                manifest = {}
            return {
                "ready": count > 0,
                "champion_run_id": run_id,
                "score_count": count,
                "model_revision": run["model_revision"],
                "text_version": run["text_version"],
                "training_examples": run["training_examples"],
                "created_at": run["created_at"],
                "manifest": manifest,
            }
    except sqlite3.Error:
        return empty


def model_status(preference_db_path: Path) -> dict[str, Any]:
    return _model_status(preference_db_path)


def _salary_status(
    con: sqlite3.Connection,
    ats: str,
    job_id: str,
    floor: float | None,
) -> dict[str, Any]:
    if not _table_exists(con, "job_compensation_ranges"):
        return {"status": "unknown", "ranges": []}
    rows = con.execute(
        "SELECT component,value_kind,annual_min_value,annual_max_value,"
        "location_scope FROM job_compensation_ranges "
        "WHERE ats=? AND job_id=? AND removed_at IS NULL AND is_preferred=1 "
        "AND UPPER(currency)='USD' "
        "AND component IN ('base_salary','ote','unknown') "
        "AND (annual_min_value IS NOT NULL OR annual_max_value IS NOT NULL)",
        (ats, job_id),
    ).fetchall()
    return _salary_result(rows, floor)


def _salary_result(
    rows: list[sqlite3.Row] | list[dict[str, Any]],
    floor: float | None,
) -> dict[str, Any]:
    ranges = [{
        "component": row["component"],
        "kind": row["value_kind"],
        "min": row["annual_min_value"],
        "max": row["annual_max_value"],
        "location_scope": row["location_scope"],
    } for row in rows]
    if not rows or floor is None:
        return {"status": "unknown" if not rows else "available", "ranges": ranges}
    if any(
        (row["annual_min_value"] is not None and row["annual_min_value"] >= floor)
        or (row["annual_max_value"] is not None and row["annual_max_value"] >= floor)
        for row in rows
    ):
        return {"status": "meets", "ranges": ranges}
    definitive_ceilings = all(
        row["annual_max_value"] is not None
        and row["value_kind"] not in {"minimum"}
        for row in rows
    )
    return {
        "status": "below" if definitive_ceilings else "unknown",
        "ranges": ranges,
    }


def _chunks(values: list[str], size: int = 400) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _family_members(
    con: sqlite3.Connection,
    family_ids: list[str],
    days: int,
) -> dict[str, list[sqlite3.Row]]:
    result: dict[str, list[sqlite3.Row]] = {}
    for group in _chunks(family_ids):
        marks = ",".join("?" for _ in group)
        # Keep this bounded by the requested families. SQLite otherwise chooses
        # jobs_closed_at first and rereads almost every description for EACH page
        # of scores when most catalog postings are open. CROSS JOIN fixes the
        # lookup order: family index, then exact (ats,id) job lookup.
        rows = con.execute(
            "SELECT m.family_id,j.* FROM job_family_members m CROSS JOIN jobs j "
            "ON j.ats=m.ats AND j.id=m.job_id "
            f"WHERE m.family_id IN ({marks}) AND j.closed_at IS NULL "
            "AND LENGTH(TRIM(COALESCE(j.description,'')))>0 "
            "AND datetime(j.publishedAt)>=datetime('now',?) "
            "ORDER BY datetime(j.publishedAt) DESC, j.ats, j.id",
            (*group, f"-{days} days"),
        ).fetchall()
        for row in rows:
            result.setdefault(str(row["family_id"]), []).append(row)
    return result


def _family_salary_rows(
    con: sqlite3.Connection,
    family_ids: list[str],
) -> dict[tuple[str, str], list[sqlite3.Row]]:
    result: dict[tuple[str, str], list[sqlite3.Row]] = {}
    if not _table_exists(con, "job_compensation_ranges"):
        return result
    for group in _chunks(family_ids):
        marks = ",".join("?" for _ in group)
        rows = con.execute(
            "SELECT r.* FROM job_family_members m JOIN job_compensation_ranges r "
            "ON r.ats=m.ats AND r.job_id=m.job_id "
            f"WHERE m.family_id IN ({marks}) AND r.removed_at IS NULL "
            "AND r.is_preferred=1 AND UPPER(r.currency)='USD' "
            "AND r.component IN ('base_salary','ote','unknown') "
            "AND (r.annual_min_value IS NOT NULL OR r.annual_max_value IS NOT NULL)",
            group,
        ).fetchall()
        for row in rows:
            result.setdefault((str(row["ats"]), str(row["job_id"])), []).append(row)
    return result


def _family_location_rows(
    con: sqlite3.Connection,
    family_ids: list[str],
) -> dict[tuple[str, str], sqlite3.Row]:
    result: dict[tuple[str, str], sqlite3.Row] = {}
    if not _table_exists(con, "job_location_enrichment"):
        return result
    for group in _chunks(family_ids):
        marks = ",".join("?" for _ in group)
        rows = con.execute(
            "SELECT l.* FROM job_family_members m JOIN job_location_enrichment l "
            "ON l.ats=m.ats AND l.job_id=m.job_id "
            f"WHERE m.family_id IN ({marks})",
            group,
        ).fetchall()
        result.update({(str(row["ats"]), str(row["job_id"])): row for row in rows})
    return result


def _location_score(row: sqlite3.Row | None) -> float | None:
    if row is None or str(row["us_eligibility"]) != "eligible":
        return None
    if row["preferred_metro_code"]:
        return 1.0
    if str(row["arrangement"]) in {"remote", "mixed"}:
        return 0.8
    return 0.55


def _salary_signal(
    salary: dict[str, Any], low_usd: float = 120_000.0, high_usd: float = 220_000.0,
) -> float:
    """Map disclosed annual USD to a bounded signal; missing pay is neutral."""
    values = [
        float(value)
        for row in salary.get("ranges", [])
        for value in (row.get("min"), row.get("max"))
        if value is not None
    ]
    if not values:
        return 0.5
    representative = max(values)
    width = max(1.0, high_usd - low_usd)
    return max(0.0, min(1.0, (representative - low_usd) / width))


def _title_vectors(
    con: sqlite3.Connection,
    family_ids: list[str],
    model_revision: str,
    text_version: str,
) -> dict[str, list[float]]:
    if not all(_table_exists(con, name) for name in (
        "preference_embedding_refs", "preference_embedding_cache",
    )):
        return {}
    cache_columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(preference_embedding_cache)")
    }
    uses_blob = {"vector_blob", "dimensions"} <= cache_columns
    if not uses_blob and "vector_json" not in cache_columns:
        return {}
    fingerprints: dict[str, str] = {}
    for group in _chunks(family_ids):
        marks = ",".join("?" for _ in group)
        rows = con.execute(
            "SELECT subject_id,title_fingerprint FROM preference_embedding_refs "
            "WHERE subject_type='family' AND model_revision=? AND text_version=? "
            f"AND subject_id IN ({marks})",
            (model_revision, text_version, *group),
        ).fetchall()
        fingerprints.update({
            str(row["subject_id"]): str(row["title_fingerprint"]) for row in rows
        })
    vectors_by_fingerprint: dict[str, list[float]] = {}
    unique_fingerprints = list(dict.fromkeys(fingerprints.values()))
    for group in _chunks(unique_fingerprints):
        marks = ",".join("?" for _ in group)
        vector_columns = "vector_blob,dimensions" if uses_blob else "vector_json"
        rows = con.execute(
            f"SELECT text_fingerprint,{vector_columns} FROM preference_embedding_cache "
            "WHERE model_revision=? AND text_version=? "
            f"AND text_fingerprint IN ({marks})",
            (model_revision, text_version, *group),
        ).fetchall()
        for row in rows:
            try:
                if uses_blob:
                    dimensions = int(row["dimensions"])
                    blob = bytes(row["vector_blob"])
                    if dimensions <= 0 or len(blob) != dimensions * 4:
                        continue
                    vector = list(struct.unpack(f"<{dimensions}f", blob))
                else:
                    vector = [float(value) for value in json.loads(row["vector_json"])]
            except (TypeError, ValueError, struct.error, json.JSONDecodeError):
                continue
            if vector:
                vectors_by_fingerprint[str(row["text_fingerprint"])] = vector
    return {
        family_id: vectors_by_fingerprint[fingerprint]
        for family_id, fingerprint in fingerprints.items()
        if fingerprint in vectors_by_fingerprint
    }


def _is_remote(job: sqlite3.Row | dict[str, Any]) -> bool:
    return (
        str(job["isRemote"] or "").lower() in {"true", "1", "yes"}
        or str(job["workplaceType"] or "").lower() == "remote"
        or "remote" in str(job["location"] or "").lower()
    )


def _family_display_labels(
    con: sqlite3.Connection,
    family_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Resolve a bounded family set to stable, human-readable labels."""
    unique_ids = list(dict.fromkeys(family_ids))
    if not unique_ids:
        return {}
    labels: dict[str, dict[str, str]] = {}
    if _table_exists(con, "job_family_members"):
        has_families = _table_exists(con, "job_families")
        for group in _chunks(unique_ids):
            marks = ",".join("?" for _ in group)
            family_join = (
                "LEFT JOIN job_families f ON f.family_id=m.family_id "
                if has_families else ""
            )
            canonical_order = (
                "CASE WHEN j.ats=f.canonical_ats AND j.id=f.canonical_job_id "
                "THEN 0 ELSE 1 END, "
                if has_families else ""
            )
            rows = con.execute(
                "SELECT m.family_id,j.ats,j.id,j.company,j.title "
                "FROM job_family_members m JOIN jobs j "
                "ON j.ats=m.ats AND j.id=m.job_id "
                f"{family_join}WHERE m.family_id IN ({marks}) "
                f"ORDER BY m.family_id,{canonical_order}"
                "CASE WHEN j.closed_at IS NULL THEN 0 ELSE 1 END,j.ats,j.id",
                group,
            ).fetchall()
            for row in rows:
                family_id = str(row["family_id"])
                if family_id in labels:
                    continue
                title = str(row["title"] or "").strip()
                company = str(row["company"] or "").strip()
                if title or company:
                    labels[family_id] = {
                        "family_id": family_id,
                        "title": title,
                        "company": company,
                    }

    # A liked family can disappear from the live membership tables.  Immutable
    # label-time snapshots still provide a safe explanation without joining it
    # to an unrelated current family.
    unresolved = [family_id for family_id in unique_ids if family_id not in labels]
    if unresolved and _table_exists(con, "preference_examples"):
        for group in _chunks(unresolved):
            marks = ",".join("?" for _ in group)
            rows = con.execute(
                "SELECT family_id,title_snapshot,metadata_json "
                "FROM preference_examples "
                f"WHERE family_id IN ({marks}) AND interest='interested' "
                "ORDER BY created_at DESC,example_id DESC",
                group,
            ).fetchall()
            for row in rows:
                family_id = str(row["family_id"])
                if family_id in labels:
                    continue
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                title = str(row["title_snapshot"] or "").strip()
                company = str(
                    metadata.get("company", "") if isinstance(metadata, dict) else ""
                ).strip()
                if title or company:
                    labels[family_id] = {
                        "family_id": family_id,
                        "title": title,
                        "company": company,
                    }
    return labels


def _resolved_explanations(
    con: sqlite3.Connection,
    score_rows: list[sqlite3.Row],
) -> dict[str, dict[str, Any]]:
    """Parse explanations and replace opaque neighbor IDs with display objects."""
    parsed: dict[str, dict[str, Any]] = {}
    neighbor_ids: list[str] = []
    for score in score_rows:
        family_id = str(score["family_id"])
        try:
            raw = json.loads(score["explanation_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        explanation = dict(raw) if isinstance(raw, dict) else {}
        parsed[family_id] = explanation
        neighbors = explanation.get("similar_liked_family_ids", [])
        if not isinstance(neighbors, list):
            continue
        for item in neighbors[:5]:
            neighbor_id = (
                item if isinstance(item, str)
                else item.get("family_id", "") if isinstance(item, dict)
                else ""
            )
            if neighbor_id:
                neighbor_ids.append(str(neighbor_id))

    labels = _family_display_labels(con, neighbor_ids)
    for explanation in parsed.values():
        neighbors = explanation.get("similar_liked_family_ids")
        if not isinstance(neighbors, list):
            continue
        resolved: list[dict[str, str]] = []
        for item in neighbors[:5]:
            neighbor_id = (
                item if isinstance(item, str)
                else item.get("family_id", "") if isinstance(item, dict)
                else ""
            )
            label = labels.get(str(neighbor_id))
            if label:
                resolved.append(dict(label))
            elif isinstance(item, dict) and (item.get("title") or item.get("company")):
                resolved.append({
                    "family_id": str(neighbor_id),
                    "title": str(item.get("title", "")).strip(),
                    "company": str(item.get("company", "")).strip(),
                })
        explanation["similar_liked_family_ids"] = resolved
    return parsed


def _recommendation_candidates(
    con: sqlite3.Connection,
    score_rows: list[sqlite3.Row],
    options: dict[str, Any],
    excluded_job_keys: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    has_families = _table_exists(con, "job_family_members")
    family_ids = [str(row["family_id"]) for row in score_rows]
    suppressed: set[str] = set()
    for ats, job_id in excluded_job_keys or set():
        if has_families:
            row = con.execute(
                "SELECT family_id FROM job_family_members WHERE ats=? AND job_id=?",
                (ats, job_id),
            ).fetchone()
            if row:
                suppressed.add(str(row[0]))
        else:
            suppressed.add(f"{ats}:{job_id}")
    if _table_exists(con, "recommendation_feedback"):
        for group in _chunks(family_ids):
            marks = ",".join("?" for _ in group)
            suppressed.update(
                str(row[0]) for row in con.execute(
                    "SELECT DISTINCT family_id FROM recommendation_feedback "
                    "WHERE action IN ('applied','dismissed_preference','blocked','duplicate') "
                    f"AND family_id IN ({marks})",
                    group,
                )
            )
    score_rows = [
        row for row in score_rows if str(row["family_id"]) not in suppressed
    ]
    family_ids = [str(row["family_id"]) for row in score_rows]
    member_map = (
        _family_members(con, family_ids, options["days"]) if has_families else {}
    )
    salary_map = _family_salary_rows(con, family_ids) if has_families else {}
    location_map = _family_location_rows(con, family_ids) if has_families else {}
    explanations = _resolved_explanations(con, score_rows)
    candidates: list[dict[str, Any]] = []
    for score in score_rows:
        family_id = str(score["family_id"])
        if has_families:
            members = member_map.get(family_id, [])
        else:
            parts = family_id.split(":", 1)
            members = [] if len(parts) != 2 else con.execute(
                "SELECT * FROM jobs WHERE ats=? AND id=? AND closed_at IS NULL "
                "AND LENGTH(TRIM(COALESCE(description,'')))>0 "
                "AND datetime(publishedAt)>=datetime('now',?)",
                (parts[0], parts[1], f"-{options['days']} days"),
            ).fetchall()
        if not members:
            continue
        eligible_members: list[tuple[tuple[float, int], sqlite3.Row, dict[str, Any], float | None]] = []
        for member in members:
            if options["remote_only"] and not _is_remote(member):
                continue
            if has_families:
                salary = _salary_result(
                    salary_map.get((str(member["ats"]), str(member["id"])), []),
                    options["salary_floor"],
                )
            else:
                salary = _salary_status(
                    con, member["ats"], member["id"], options["salary_floor"]
                )
            priority = {"meets": 0, "available": 0, "unknown": 1, "below": 2}[
                salary["status"]
            ]
            location = location_map.get((str(member["ats"]), str(member["id"])))
            location_score = _location_score(location)
            if options.get("us_only") and location_score is None:
                continue
            eligible_members.append(((-(location_score or 0.0), priority), member, salary, location_score))
        if not eligible_members or all(
            item[2]["status"] == "below" for item in eligible_members
        ):
            continue
        _, member, salary, location_score = min(
            (item for item in eligible_members if item[2]["status"] != "below"),
            key=lambda item: item[0],
        )
        semantic_score = float(score["final_score"] or 0.0)
        salary_signal = _salary_signal(
            salary,
            float(options.get("compensation_low_usd", 120_000.0)),
            float(options.get("compensation_high_usd", 220_000.0)),
        )
        ranking_score = (
            0.70 * semantic_score
            + 0.15 * (location_score if location_score is not None else 0.5)
            + 0.15 * salary_signal
        )
        candidates.append({
            "family_id": family_id,
            "variant_count": len(members),
            "ats": member["ats"],
            "id": member["id"],
            "company": member["company"],
            "title": member["title"],
            "department": member["department"],
            "team": member["team"],
            "employmentType": member["employmentType"],
            "location": member["location"],
            "isRemote": member["isRemote"],
            "workplaceType": member["workplaceType"],
            "publishedAt": member["publishedAt"],
            "posted_at": member["posted_at"] if "posted_at" in member.keys() else None,
            "source_updated_at": member["source_updated_at"] if "source_updated_at" in member.keys() else None,
            "first_seen": member["first_seen"],
            "jobUrl": member["jobUrl"],
            "salary": salary,
            "final_score": semantic_score,
            "semantic_score": semantic_score,
            "location_score": location_score,
            "salary_signal": salary_signal,
            "ranking_score": ranking_score,
            "score_components": {
                "dense_linear": score["dense_linear_score"],
                "dense_neighbor": score["dense_neighbor_score"],
                "sparse": score["sparse_score"],
            },
            "explanation": explanations.get(family_id, {}),
            "model_run_id": score["run_id"],
        })
    return candidates


def _candidate_capacity(
    candidates: list[dict[str, Any]], options: dict[str, Any],
) -> int:
    companies: dict[str, int] = {}
    titles: dict[str, int] = {}
    count = 0
    for row in sorted(
        candidates,
        key=lambda item: float(item.get("ranking_score", item["final_score"]) or 0.0),
        reverse=True,
    ):
        fallback = row["family_id"]
        company = " ".join(str(row["company"] or "").casefold().split()) or fallback
        title = " ".join(str(row["title"] or "").casefold().split()) or fallback
        if (
            companies.get(company, 0) >= options["max_per_company"]
            or titles.get(title, 0) >= options["max_per_title"]
        ):
            continue
        companies[company] = companies.get(company, 0) + 1
        titles[title] = titles.get(title, 0) + 1
        count += 1
    return count


def recommendations(
    db_path: Path,
    preference_db_path: Path,
    options: dict[str, Any],
    model_run_id: str | None = None,
    excluded_job_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    status = _model_status(preference_db_path, model_run_id)
    if not status["ready"]:
        return {
            "recommendations": [], "model": status, "options": options,
            "candidate_pool_examined": 0, "candidate_pool_truncated": False,
        }

    page_size = min(max(500, options["limit"] * 25), 2500)
    uncertainty_pool = min(max(100, options["limit"] * 10), 1000)
    scan_ceiling = 10_000
    desired_viable = max(options["limit"] * 2, options["limit"] + 10)
    score_by_family: dict[str, sqlite3.Row] = {}
    candidate_by_family: dict[str, dict[str, Any]] = {}
    top_examined = 0
    exhausted = False
    with connect(preference_db_path) as model_con, connect(db_path) as con:
        while top_examined < scan_ceiling:
            count = min(page_size, scan_ceiling - top_examined)
            page = model_con.execute(
                "SELECT * FROM preference_scores WHERE run_id=? "
                "ORDER BY final_score DESC LIMIT ? OFFSET ?",
                (status["champion_run_id"], count, top_examined),
            ).fetchall()
            if not page:
                exhausted = True
                break
            new_rows = [
                row for row in page if str(row["family_id"]) not in score_by_family
            ]
            score_by_family.update({str(row["family_id"]): row for row in new_rows})
            candidate_by_family.update({
                row["family_id"]: row
                for row in _recommendation_candidates(
                    con, new_rows, options, excluded_job_keys
                )
            })
            top_examined += len(page)
            if (
                len(candidate_by_family) >= desired_viable
                and _candidate_capacity(list(candidate_by_family.values()), options)
                >= options["limit"]
            ):
                break
            if len(page) < count:
                exhausted = True
                break

        uncertainty_rows = model_con.execute(
            "SELECT * FROM preference_scores WHERE run_id=? "
            "ORDER BY ABS(final_score - 0.5), final_score DESC LIMIT ?",
            (status["champion_run_id"], uncertainty_pool),
        ).fetchall()
        new_uncertainty = [
            row for row in uncertainty_rows
            if str(row["family_id"]) not in score_by_family
        ]
        score_by_family.update({
            str(row["family_id"]): row for row in new_uncertainty
        })
        candidate_by_family.update({
            row["family_id"]: row
            for row in _recommendation_candidates(
                con, new_uncertainty, options, excluded_job_keys
            )
        })
        title_vectors = _title_vectors(
            model_con,
            list(candidate_by_family),
            str(status["model_revision"] or ""),
            str(status["text_version"] or ""),
        )
    candidates = list(candidate_by_family.values())
    scan_truncated = (
        not exhausted
        and top_examined >= scan_ceiling
        and (
            len(candidate_by_family) < desired_viable
            or _candidate_capacity(list(candidate_by_family.values()), options)
            < options["limit"]
        )
        and status["score_count"] > top_examined
    )

    candidates.sort(
        key=lambda row: (
            float(row.get("ranking_score", row["final_score"]) or 0.0),
            str(row["publishedAt"] or ""),
        ),
        reverse=True,
    )

    def normalized(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())

    company_counts: dict[str, int] = {}
    title_counts: dict[str, int] = {}

    def allowed(row: dict[str, Any]) -> bool:
        company = normalized(row["company"]) or row["family_id"]
        title = normalized(row["title"]) or row["family_id"]
        return (
            company_counts.get(company, 0) < options["max_per_company"]
            and title_counts.get(title, 0) < options["max_per_title"]
        )

    def add(row: dict[str, Any], segment: str, output: list[dict[str, Any]]) -> None:
        row = dict(row)
        row["segment"] = segment
        output.append(row)
        company = normalized(row["company"]) or row["family_id"]
        title = normalized(row["title"]) or row["family_id"]
        company_counts[company] = company_counts.get(company, 0) + 1
        title_counts[title] = title_counts.get(title, 0) + 1

    explore_slots = 2 if options["limit"] >= 20 else (
        1 if options["limit"] >= 10 else 0
    )
    exploit_slots = options["limit"] - explore_slots
    selected: list[dict[str, Any]] = []
    selected_families: set[str] = set()

    while len(selected) < exploit_slots:
        available = [
            row for row in candidates
            if row["family_id"] not in selected_families and allowed(row)
        ]
        if not available:
            break

        def mmr(row: dict[str, Any]) -> tuple[float, float, str]:
            vector = title_vectors.get(row["family_id"])
            similarity = max((
                _cosine_similarity(vector, title_vectors.get(chosen["family_id"]))
                for chosen in selected
            ), default=0.0)
            preference = float(row.get("ranking_score", row["final_score"]) or 0.0)
            return (
                0.85 * preference - 0.15 * similarity,
                preference,
                row["family_id"],
            )

        chosen = max(available, key=mmr)
        add(chosen, "exploit", selected)
        selected_families.add(chosen["family_id"])

    remainder = [row for row in candidates if row["family_id"] not in selected_families]
    remainder.sort(key=lambda row: (
        abs(float(row["final_score"] or 0.0) - 0.5),
        hashlib.sha256(row["family_id"].encode()).hexdigest(),
    ))
    for row in remainder:
        if len(selected) >= options["limit"]:
            break
        if allowed(row):
            add(row, "explore", selected)
            selected_families.add(row["family_id"])

    for rank, row in enumerate(selected, 1):
        row["rank"] = rank
    return {
        "recommendations": selected,
        "model": status,
        "options": options,
        "candidate_pool_examined": len(score_by_family),
        "candidate_pool_truncated": scan_truncated,
    }


def _proxy_student_runs(proxy_db_path: Path) -> dict[str, str]:
    if not proxy_db_path.exists():
        return {}
    try:
        with connect(proxy_db_path) as con:
            if not all(_table_exists(con, name) for name in ("proxy_runs", "proxy_students")):
                return {}
            rows = con.execute(
                "SELECT s.policy_id,s.model_run_id FROM proxy_students s "
                "JOIN proxy_runs r ON r.run_id=s.run_id "
                "ORDER BY r.created_at DESC,s.trained_at DESC"
            ).fetchall()
    except sqlite3.Error:
        return {}
    result: dict[str, str] = {}
    for row in rows:
        result.setdefault(str(row["policy_id"]), str(row["model_run_id"]))
    return result


def _proxy_profile(proxy_db_path: Path) -> dict[str, Any]:
    if not proxy_db_path.exists():
        return {}
    try:
        with connect(proxy_db_path) as con:
            if not all(_table_exists(con, name) for name in (
                "proxy_runs", "proxy_profiles", "proxy_students",
            )):
                return {}
            row = con.execute(
                "SELECT p.profile_json FROM proxy_runs r JOIN proxy_profiles p "
                "ON p.profile_fingerprint=r.profile_fingerprint "
                "WHERE EXISTS (SELECT 1 FROM proxy_students s WHERE s.run_id=r.run_id) "
                "ORDER BY r.created_at DESC LIMIT 1"
            ).fetchone()
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (sqlite3.Error, TypeError, json.JSONDecodeError):
        return {}


def policy_recommendations(
    db_path: Path,
    preference_db_path: Path,
    proxy_db_path: Path,
    options: dict[str, Any],
    excluded_job_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    policy = str(options.get("policy") or "champion")
    if policy == "champion":
        result = recommendations(
            db_path, preference_db_path, options,
            excluded_job_keys=excluded_job_keys,
        )
        for row in result["recommendations"]:
            row["policy_id"] = "champion"
        return result
    runs = _proxy_student_runs(proxy_db_path)
    requested = ("selective", "broad") if policy == "compare" else (policy,)
    missing = [name for name in requested if name not in runs]
    if missing:
        model = _model_status(preference_db_path)
        model.update({"ready": False, "requested_policy": policy, "missing_policies": missing})
        return {
            "recommendations": [], "model": model, "options": options,
            "candidate_pool_examined": 0, "candidate_pool_truncated": False,
        }
    profile = _proxy_profile(proxy_db_path)
    compensation = profile.get("compensation") if isinstance(profile, dict) else {}
    compensation = compensation if isinstance(compensation, dict) else {}
    constrained = dict(
        options,
        us_only=True,
        compensation_low_usd=float(compensation.get("low_usd", 120_000.0)),
        compensation_high_usd=float(compensation.get("high_usd", 220_000.0)),
    )
    results = {
        name: recommendations(
            db_path, preference_db_path, constrained, runs[name], excluded_job_keys
        )
        for name in requested
    }
    for name, result in results.items():
        for row in result["recommendations"]:
            row["policy_id"] = name
    if policy != "compare":
        result = results[policy]
        result["options"] = options
        result["model"]["policy_id"] = policy
        return result

    # Alternate the independent rankings. Scores from separately fit policies are
    # deliberately not compared as though they were calibrated on one scale.
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    indexes = {name: 0 for name in requested}
    while len(selected) < options["limit"]:
        changed = False
        for name in requested:
            rows = results[name]["recommendations"]
            while indexes[name] < len(rows):
                row = rows[indexes[name]]
                indexes[name] += 1
                if row["family_id"] in seen:
                    continue
                selected.append(dict(row))
                seen.add(row["family_id"])
                changed = True
                break
            if len(selected) >= options["limit"]:
                break
        if not changed:
            break
    for rank, row in enumerate(selected, 1):
        row["rank"] = rank
        row["segment"] = f"compare_{row['policy_id']}"
    return {
        "recommendations": selected,
        "model": {
            "ready": all(result["model"]["ready"] for result in results.values()),
            "champion_run_id": "compare",
            "score_count": min(result["model"]["score_count"] for result in results.values()),
            "model_revision": "selective + broad",
            "policies": {name: result["model"] for name, result in results.items()},
        },
        "options": options,
        "candidate_pool_examined": sum(
            result["candidate_pool_examined"] for result in results.values()
        ),
        "candidate_pool_truncated": any(
            result["candidate_pool_truncated"] for result in results.values()
        ),
    }


def record_recommendation_impressions(
    db_path: Path,
    result: dict[str, Any],
    policy_id: str,
    *,
    idempotency_key: str | None = None,
    actor: str = "dashboard",
) -> dict[str, Any]:
    rows = result.get("recommendations") or []
    key = str(idempotency_key or "").strip() or None
    actor = str(actor or "dashboard").strip()[:64]
    if key is not None and len(key) > 255:
        raise ApiError("idempotency key is too long")
    if not actor:
        raise ApiError("actor is required")
    options_json = json.dumps(result.get("options") or {}, sort_keys=True)
    # Application provenance requires an alphanumeric first character; URL-safe
    # entropy can begin with a hyphen or underscore. Keep a stable namespace.
    session_id = "shortlist_" + secrets.token_urlsafe(18)
    stamp = datetime.now(timezone.utc).isoformat()
    model_runs = sorted({str(row.get("model_run_id") or "") for row in rows})
    with connect(db_path) as con:
        if key is not None:
            existing = con.execute(
                "SELECT policy_id,options_json,result_json FROM recommendation_sessions "
                "WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing:
                if (
                    str(existing["policy_id"]) != policy_id
                    or str(existing["options_json"]) != options_json
                ):
                    raise ApiError(
                        "idempotency key was reused for a different shortlist", 409
                    )
                return json.loads(str(existing["result_json"]))
        con.execute(
            "INSERT INTO recommendation_sessions "
            "(session_id,policy_id,model_runs_json,options_json,idempotency_key,actor,"
            "result_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id, policy_id, json.dumps(model_runs, sort_keys=True),
                options_json, key, actor, "{}", stamp,
            ),
        )
        for position, row in enumerate(rows, 1):
            row["session_id"] = session_id
            row["policy_id"] = str(row.get("policy_id") or policy_id)
            cursor = con.execute(
                "INSERT INTO recommendation_impressions "
                "(session_id,position,policy_id,model_run_id,ats,job_id,family_id,"
                "semantic_score,ranking_score,shown_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, position, row["policy_id"], row.get("model_run_id") or "",
                    row["ats"], row["id"], row["family_id"],
                    float(row.get("semantic_score", row.get("final_score", 0.0))),
                    float(row.get("ranking_score", row.get("final_score", 0.0))), stamp,
                ),
            )
            row["impression_id"] = int(cursor.lastrowid)
        result["session_id"] = session_id
        con.execute(
            "UPDATE recommendation_sessions SET result_json=? WHERE session_id=?",
            (json.dumps(result, sort_keys=True), session_id),
        )
    return result


def create_shortlist_session(
    db_path: Path,
    preference_db_path: Path,
    proxy_db_path: Path,
    options: dict[str, Any],
    *,
    idempotency_key: str,
    actor: str,
    excluded_job_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Create or replay one explicit, auditable shortlist exposure."""

    prepare_preferences(db_path)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ApiError("idempotency key is required")
    options_json = json.dumps(options, sort_keys=True)
    with connect(db_path) as con:
        existing = con.execute(
            "SELECT policy_id,options_json,result_json FROM recommendation_sessions "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
    if existing:
        if (
            str(existing["policy_id"]) != str(options.get("policy") or "champion")
            or str(existing["options_json"]) != options_json
        ):
            raise ApiError("idempotency key was reused for a different shortlist", 409)
        return json.loads(str(existing["result_json"]))
    result = policy_recommendations(
        db_path, preference_db_path, proxy_db_path, options,
        excluded_job_keys=excluded_job_keys,
    )
    return record_recommendation_impressions(
        db_path, result, str(options.get("policy") or "champion"),
        idempotency_key=key, actor=actor,
    )


def save_recommendation_feedback(
    db_path: Path,
    payload: dict[str, Any],
    *,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    ats = str(payload.get("ats", "")).strip().lower()
    job_id = str(payload.get("id", "")).strip()
    action = str(payload.get("action", "")).strip()
    if not ats or not job_id:
        raise ApiError("ats and id are required")
    if action not in FEEDBACK_ACTIONS:
        raise ApiError("unknown recommendation feedback action")
    source_event_id = str(source_event_id or "").strip() or None
    if source_event_id is not None and len(source_event_id) > 255:
        raise ApiError("source_event_id is too long")
    raw_rank = payload.get("rank")
    try:
        rank = None if raw_rank in {None, ""} else int(raw_rank)
    except (TypeError, ValueError) as exc:
        raise ApiError("rank must be an integer") from exc
    if rank is not None and rank < 1:
        raise ApiError("rank must be positive")
    policy_id = str(payload.get("policy_id", "champion")).strip().lower()
    if policy_id not in {"champion", "selective", "broad"}:
        raise ApiError("unknown recommendation policy")
    session_id = str(payload.get("session_id", "")).strip()[:128]
    raw_impression = payload.get("impression_id")
    try:
        impression_id = None if raw_impression in {None, ""} else int(raw_impression)
    except (TypeError, ValueError) as exc:
        raise ApiError("impression_id must be an integer") from exc
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        if source_event_id is not None:
            existing = con.execute(
                "SELECT feedback_id,ats,job_id,action FROM recommendation_feedback "
                "WHERE source_event_id=?",
                (source_event_id,),
            ).fetchone()
            if existing:
                if (
                    str(existing["ats"]) != ats
                    or str(existing["job_id"]) != job_id
                    or str(existing["action"]) != action
                ):
                    raise ApiError(
                        "source event already belongs to different feedback", 409
                    )
                return {
                    "ok": True, "feedback_id": int(existing["feedback_id"]),
                    "action": action, "preference_label_changed": False,
                    "created": False,
                }
        job = con.execute(
            "SELECT * FROM jobs WHERE ats=? AND id=?", (ats, job_id)
        ).fetchone()
        if not job:
            raise ApiError("job not found", 404)
        family = _family_context(con, ats, job_id)
        semantic_score = payload.get("semantic_score")
        ranking_score = payload.get("ranking_score")
        if impression_id is not None:
            impression = con.execute(
                "SELECT * FROM recommendation_impressions WHERE impression_id=? "
                "AND ats=? AND job_id=?",
                (impression_id, ats, job_id),
            ).fetchone()
            if not impression:
                raise ApiError("recommendation impression not found", 404)
            session_id = str(impression["session_id"])
            policy_id = str(impression["policy_id"])
            rank = int(impression["position"])
            model_run_id = str(impression["model_run_id"])
            semantic_score = impression["semantic_score"]
            ranking_score = impression["ranking_score"]
        else:
            model_run_id = str(payload.get("model_run_id", ""))[:128]
        implicit_weight = {
            "applied": 2.0,
            "saved": 0.5,
            "dismissed_preference": -2.0,
            "blocked": 0.0,
            "duplicate": 0.0,
        }[action]
        cursor = con.execute(
            "INSERT INTO recommendation_feedback "
            "(ats,job_id,family_id,action,model_run_id,policy_id,session_id,impression_id,"
            "recommendation_rank,semantic_score,ranking_score,implicit_weight,title_snapshot,"
            "description_snapshot,metadata_json,template_cluster_id,leakage_group_id,"
            "source_fingerprint,note,source_event_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ats, job_id, family["family_id"], action,
                model_run_id, policy_id, session_id,
                impression_id, rank, semantic_score, ranking_score, implicit_weight,
                str(job["title"] or ""), str(job["description"] or ""),
                _snapshot_metadata(job), family["template_cluster_id"],
                family["leakage_group_id"], family["source_fingerprint"],
                str(payload.get("note", "")).strip()[:1000], source_event_id, now,
            ),
        )
    return {
        "ok": True, "feedback_id": cursor.lastrowid, "action": action,
        "preference_label_changed": False, "created": True,
    }


def make_handler(
    db_path: Path,
    preference_db_path: Path | None = None,
    proxy_db_path: Path | None = None,
    recommendation_limit: int = 20,
    salary_floor: float | None = None,
) -> type[BaseHTTPRequestHandler]:
    model_db = preference_db_path or db_path.with_name(f"{db_path.stem}-preference.db")
    proxy_db = proxy_db_path or db_path.with_name(f"{db_path.stem}-proxy.db")

    class LabelHandler(BaseHTTPRequestHandler):
        server_version = "JobLabeler/1.0"

        def _json(self, value: Any, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _asset(self, name: str) -> None:
            allowed = {"index.html", "app.js", "styles.css"}
            if name not in allowed:
                raise ApiError("not found", 404)
            path = ASSET_DIR / name
            if not path.exists():
                raise ApiError("UI asset not found", 500)
            body = path.read_bytes()
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", f"{mime}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ApiError("invalid content length") from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ApiError("request body is empty or too large")
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError("invalid JSON") from exc
            if not isinstance(value, dict):
                raise ApiError("JSON body must be an object")
            return value

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            try:
                parsed = urlparse(self.path)
                if parsed.path in {"/", "/index.html"}:
                    self._asset("index.html")
                elif parsed.path == "/app.js":
                    self._asset("app.js")
                elif parsed.path == "/styles.css":
                    self._asset("styles.css")
                elif parsed.path == "/api/job":
                    filters = _filters(parse_qs(parsed.query))
                    self._json({"job": choose_job(db_path, filters, model_db)})
                elif parsed.path == "/api/stats":
                    filters = _filters(parse_qs(parsed.query))
                    self._json(label_stats(db_path, filters))
                elif parsed.path == "/api/recommendations":
                    options = _recommendation_options(
                        parse_qs(parsed.query), recommendation_limit, salary_floor,
                    )
                    result = policy_recommendations(db_path, model_db, proxy_db, options)
                    self._json(record_recommendation_impressions(
                        db_path, result, str(options["policy"]),
                    ))
                elif parsed.path == "/api/model-status":
                    self._json(model_status(model_db))
                else:
                    raise ApiError("not found", 404)
            except ApiError as exc:
                self._json({"error": str(exc)}, exc.status)
            except Exception as exc:  # keep server alive and give the UI a useful error
                self._json({"error": str(exc)}, 500)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/label":
                    self._json(save_label(db_path, self._payload(), model_db))
                elif parsed.path == "/api/undo":
                    self._json({"undone": undo_last(db_path)})
                elif parsed.path == "/api/recommendation-feedback":
                    self._json(save_recommendation_feedback(db_path, self._payload()))
                else:
                    raise ApiError("not found", 404)
            except ApiError as exc:
                self._json({"error": str(exc)}, exc.status)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def log_message(self, format: str, *args: Any) -> None:
            # Keep useful request/error logs, but omit reverse-DNS and timestamps.
            print(f"labeler: {format % args}")

    return LabelHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=ROOT / "job-boards.db",
        help="job database to label (default: ./job-boards.db)",
    )
    parser.add_argument("--port", type=int, default=8765, help="local port (default: 8765)")
    parser.add_argument(
        "--preference-db", type=Path,
        help="model sidecar (default: <db stem>-preference.db beside --db)",
    )
    parser.add_argument(
        "--proxy-db", type=Path,
        help="LLM proxy sidecar (default: <db stem>-proxy.db beside --db)",
    )
    parser.add_argument(
        "--recommendation-limit", type=int, default=20,
        help="default shortlist size (default: 20; API limit can override)",
    )
    parser.add_argument(
        "--salary-floor", type=float,
        help="optional annual USD floor for recommendations",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = args.db.expanduser().resolve()
    preference_db_path = (
        args.preference_db.expanduser().resolve() if args.preference_db
        else db_path.with_name(f"{db_path.stem}-preference.db")
    )
    proxy_db_path = (
        args.proxy_db.expanduser().resolve() if args.proxy_db
        else db_path.with_name(f"{db_path.stem}-proxy.db")
    )
    if not 1 <= args.recommendation_limit <= 100:
        raise SystemExit("--recommendation-limit must be between 1 and 100")
    if args.salary_floor is not None and args.salary_floor < 0:
        raise SystemExit("--salary-floor must be non-negative")
    prepare_preferences(db_path)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(
            db_path, preference_db_path, proxy_db_path,
            args.recommendation_limit, args.salary_floor,
        ),
    )
    server.daemon_threads = True
    print(f"Job labeler: http://127.0.0.1:{args.port}")
    print(f"Database: {db_path}")
    print(f"Preference model: {preference_db_path}")
    print("This server is local-only and makes no ATS requests. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
