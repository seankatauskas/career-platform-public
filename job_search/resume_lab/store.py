"""Private SQLite sidecar for immutable resume-lab state."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .contracts import (
    ArtifactInput,
    AtsProxyEvaluation,
    JobSnapshot,
    NORMALIZATION_VALIDATOR_REVISION,
    RequirementGraph,
    ResumeBoundaryError,
    ResumeConflictError,
    ResumeLabError,
    ResumeNotFoundError,
    ResumePurpose,
    ResumeRunStatus,
    RunItemStatus,
    RUN_VARIANTS,
    StandardVersionInput,
    VariantKind,
    canonical_json,
    claims_from_json,
    content_sha256,
    sha256_text,
    validate_identifier,
    validate_sha256,
)
from .career_runs import (
    execute_schema, migrate_sources, SOURCE_TRIGGERS, artifact_grounding_current,
    get_composition,
)
from .grounding import (
    GROUNDING_EQUIVALENCE_REVISION,
    GROUNDING_VALIDATOR_REVISION,
    validate_grounded_artifact_boundary,
)


MAX_ACTIVE_STANDARDS = 25
RUNPOD_RECONCILIATION_ERROR = "runpod_reconciliation_required"


def requires_runpod_reconciliation(value: Any) -> bool:
    return isinstance(value, str) and (
        value == RUNPOD_RECONCILIATION_ERROR
        or value.startswith(RUNPOD_RECONCILIATION_ERROR + ":")
    )

_RANKING_COMMON_KEYS = frozenset(
    {
        "standard_id",
        "standard_version_id",
        "active_version_id",
        "name",
        "manual_rank",
        "status",
        "normalized",
        "parse_safe",
        "rank",
        "primary",
    }
)
_RANKING_SCORE_KEYS = frozenset(
    {
        "artifact_id",
        "evaluation_id",
        "score",
        "parsed_fit",
        "requirement_evidence",
        "search_visibility",
        "screening_readiness",
        "eligibility_status",
        "scorer_revision",
        "requirement_graph_fingerprint",
        "criteria",
    }
)
_RANKING_ERROR_KEYS = frozenset({"score", "error_code"})


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS resume_lab_schema (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_standards (
    standard_id        TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    manual_rank        INTEGER NOT NULL CHECK (manual_rank > 0),
    active             INTEGER NOT NULL CHECK (active IN (0, 1)),
    active_version_id  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS resume_standards_active_rank
ON resume_standards(manual_rank) WHERE active = 1;

CREATE TABLE IF NOT EXISTS resume_standard_versions (
    version_id       TEXT PRIMARY KEY,
    standard_id      TEXT NOT NULL,
    version_number   INTEGER NOT NULL CHECK (version_number > 0),
    tex_source       TEXT NOT NULL,
    plain_text       TEXT NOT NULL,
    claims_json      TEXT NOT NULL,
    normalized_content_json TEXT,
    import_metadata_json TEXT,
    content_sha256   TEXT NOT NULL CHECK (length(content_sha256) = 64),
    authored_by      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (standard_id, version_number),
    UNIQUE (standard_id, content_sha256),
    FOREIGN KEY (standard_id) REFERENCES resume_standards(standard_id)
);

CREATE TRIGGER IF NOT EXISTS resume_standard_versions_no_update
BEFORE UPDATE ON resume_standard_versions
BEGIN
    SELECT RAISE(ABORT, 'standard resume versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_standard_versions_no_delete
BEFORE DELETE ON resume_standard_versions
BEGIN
    SELECT RAISE(ABORT, 'standard resume versions are immutable');
END;

CREATE TABLE IF NOT EXISTS resume_artifacts (
    artifact_id            TEXT PRIMARY KEY,
    purpose                TEXT NOT NULL CHECK (
        purpose IN ('real_application', 'synthetic_research')
    ),
    variant_kind           TEXT NOT NULL CHECK (
        variant_kind IN (
            'standard', 'grounded_rewrite', 'standard_exaggerated',
            'market_ideal', 'keyword_adversarial'
        )
    ),
    ats                    TEXT NOT NULL,
    job_id                 TEXT NOT NULL,
    job_fingerprint        TEXT NOT NULL CHECK (length(job_fingerprint) = 64),
    base_version_id        TEXT,
    tex_source             TEXT NOT NULL,
    intended_text          TEXT NOT NULL,
    parsed_text            TEXT NOT NULL,
    claims_json            TEXT NOT NULL,
    managed_relative_path  TEXT NOT NULL,
    pdf_sha256             TEXT NOT NULL CHECK (length(pdf_sha256) = 64),
    content_sha256         TEXT NOT NULL CHECK (length(content_sha256) = 64),
    parse_fidelity         REAL NOT NULL CHECK (parse_fidelity BETWEEN 0 AND 1),
    parse_safe             INTEGER NOT NULL CHECK (parse_safe IN (0, 1)),
    generator_revision     TEXT NOT NULL,
    study_id               TEXT,
    pair_id                TEXT,
    treatment              TEXT NOT NULL DEFAULT '',
    generation_seed        INTEGER,
    metadata_json          TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    source_mode TEXT NOT NULL DEFAULT 'standard' CHECK(source_mode IN ('standard','career_profile')),
    composition_id TEXT REFERENCES career_compositions(composition_id),
    UNIQUE (purpose, content_sha256),
    FOREIGN KEY (base_version_id) REFERENCES resume_standard_versions(version_id),
    CHECK (
        (purpose = 'real_application' AND artifact_id LIKE 'real\_%' ESCAPE '\'
         AND study_id IS NULL AND pair_id IS NULL AND generation_seed IS NULL)
        OR
        (purpose = 'synthetic_research' AND artifact_id LIKE 'syn\_%' ESCAPE '\'
         AND study_id IS NOT NULL AND pair_id IS NOT NULL AND generation_seed IS NOT NULL)
    )
);

CREATE TRIGGER IF NOT EXISTS resume_artifacts_no_update
BEFORE UPDATE ON resume_artifacts
BEGIN
    SELECT RAISE(ABORT, 'resume artifacts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifacts_no_delete
BEFORE DELETE ON resume_artifacts
BEGIN
    SELECT RAISE(ABORT, 'resume artifacts are immutable');
END;

CREATE TABLE IF NOT EXISTS resume_evaluation_cache (
    cache_key                       TEXT PRIMARY KEY,
    artifact_id                     TEXT NOT NULL,
    job_fingerprint                 TEXT NOT NULL CHECK (length(job_fingerprint) = 64),
    requirement_graph_fingerprint   TEXT NOT NULL CHECK (length(requirement_graph_fingerprint) = 64),
    scorer_revision                 TEXT NOT NULL,
    result_json                     TEXT NOT NULL,
    created_at                      TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);

CREATE TRIGGER IF NOT EXISTS resume_evaluation_cache_no_update
BEFORE UPDATE ON resume_evaluation_cache
BEGIN
    SELECT RAISE(ABORT, 'resume evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_evaluation_cache_no_delete
BEFORE DELETE ON resume_evaluation_cache
BEGIN
    SELECT RAISE(ABORT, 'resume evaluations are immutable');
END;

-- One content-addressed evaluation may validly apply to multiple artifacts with
-- identical parsed text.  Persist every artifact/evaluation binding explicitly so a
-- caller cannot attach a score from another job or artifact during selection.
CREATE TABLE IF NOT EXISTS resume_artifact_evaluations (
    artifact_id   TEXT NOT NULL,
    cache_key     TEXT NOT NULL,
    linked_at     TEXT NOT NULL,
    PRIMARY KEY (artifact_id, cache_key),
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id),
    FOREIGN KEY (cache_key) REFERENCES resume_evaluation_cache(cache_key)
);
CREATE TRIGGER IF NOT EXISTS resume_artifact_evaluations_no_update
BEFORE UPDATE ON resume_artifact_evaluations
BEGIN
    SELECT RAISE(ABORT, 'resume artifact evaluation links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifact_evaluations_no_delete
BEFORE DELETE ON resume_artifact_evaluations
BEGIN
    SELECT RAISE(ABORT, 'resume artifact evaluation links are immutable');
END;

CREATE TABLE IF NOT EXISTS application_resume_selections (
    selection_seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_id     TEXT NOT NULL UNIQUE,
    application_id   TEXT NOT NULL,
    artifact_id      TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    selected_by      TEXT NOT NULL CHECK (selected_by = 'user'),
    selected_at      TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);
CREATE INDEX IF NOT EXISTS application_resume_selections_current
ON application_resume_selections(application_id, selection_seq DESC);

CREATE TRIGGER IF NOT EXISTS application_resume_selections_real_only
BEFORE INSERT ON application_resume_selections
WHEN NOT EXISTS (
    SELECT 1 FROM resume_artifacts a
    WHERE a.artifact_id = NEW.artifact_id
      AND a.purpose = 'real_application'
      AND a.parse_safe = 1
      AND a.variant_kind IN ('standard', 'grounded_rewrite')
)
BEGIN
    SELECT RAISE(ABORT, 'only parse-safe real artifacts may be selected');
END;
CREATE TRIGGER IF NOT EXISTS application_resume_selections_no_update
BEFORE UPDATE ON application_resume_selections
BEGIN
    SELECT RAISE(ABORT, 'application resume selections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS application_resume_selections_no_delete
BEFORE DELETE ON application_resume_selections
BEGIN
    SELECT RAISE(ABORT, 'application resume selections are append-only');
END;

CREATE TABLE IF NOT EXISTS resume_runs (
    run_id              TEXT PRIMARY KEY,
    application_id      TEXT NOT NULL,
    ats                 TEXT NOT NULL,
    job_id              TEXT NOT NULL,
    job_snapshot_json   TEXT NOT NULL,
    job_fingerprint     TEXT NOT NULL CHECK (length(job_fingerprint) = 64),
    selected_standard_id TEXT,
    base_version_id     TEXT,
    base_fingerprint    TEXT NOT NULL CHECK (length(base_fingerprint) = 64),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed')
    ),
    attempt             INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    error               TEXT NOT NULL DEFAULT '',
    result_fingerprint  TEXT,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    updated_at          TEXT NOT NULL,
    source_mode TEXT NOT NULL DEFAULT 'standard' CHECK(source_mode IN ('standard','career_profile')),
    composition_id TEXT REFERENCES career_compositions(composition_id),
    run_role TEXT NOT NULL DEFAULT 'primary' CHECK(run_role IN ('primary','research')),
    parent_run_id TEXT REFERENCES resume_runs(run_id),
    FOREIGN KEY (selected_standard_id) REFERENCES resume_standards(standard_id),
    FOREIGN KEY (base_version_id) REFERENCES resume_standard_versions(version_id)
);
CREATE INDEX IF NOT EXISTS resume_runs_status
ON resume_runs(status, created_at DESC);

-- SQLite timestamps are intentionally human-readable and only precise to a second.
-- Keep a separate insertion sequence so two runs created in that same second still
-- have one durable, unambiguous order.  A separate table permits additive upgrades of
-- existing sidecars because SQLite cannot add an AUTOINCREMENT column in place.
CREATE TABLE IF NOT EXISTS resume_run_order (
    run_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL UNIQUE,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_order_no_update
BEFORE UPDATE ON resume_run_order
BEGIN
    SELECT RAISE(ABORT, 'resume run order is immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_order_no_delete
BEFORE DELETE ON resume_run_order
BEGIN
    SELECT RAISE(ABORT, 'resume run order is immutable');
END;

-- The exact requirement analysis used to rank the winning standard belongs to the
-- run.  Looking up "the latest" analysis at worker time would make an already queued
-- experiment change underneath us.
CREATE TABLE IF NOT EXISTS resume_run_analyses (
    run_id                         TEXT PRIMARY KEY,
    requirement_graph_fingerprint TEXT NOT NULL CHECK (
        length(requirement_graph_fingerprint) = 64
    ),
    extraction_source             TEXT NOT NULL,
    clauses_json                  TEXT NOT NULL,
    created_at                    TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_analyses_no_update
BEFORE UPDATE ON resume_run_analyses
BEGIN
    SELECT RAISE(ABORT, 'resume run analyses are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_analyses_no_delete
BEFORE DELETE ON resume_run_analyses
BEGIN
    SELECT RAISE(ABORT, 'resume run analyses are immutable');
END;

-- Preserve the exact standard-resume leaderboard that selected the run base.  Product
-- views must never rebuild historical rankings from today's active standards.
CREATE TABLE IF NOT EXISTS resume_run_ranking_snapshots (
    run_id           TEXT PRIMARY KEY,
    rankings_json    TEXT NOT NULL,
    rankings_sha256  TEXT NOT NULL CHECK (length(rankings_sha256) = 64),
    created_at       TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_ranking_snapshots_no_update
BEFORE UPDATE ON resume_run_ranking_snapshots
BEGIN
    SELECT RAISE(ABORT, 'resume run ranking snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_ranking_snapshots_no_delete
BEFORE DELETE ON resume_run_ranking_snapshots
BEGIN
    SELECT RAISE(ABORT, 'resume run ranking snapshots are immutable');
END;

-- Retrying is a user command, not merely a status mutation.  Recording its key first
-- makes an exact replay safe even after the retried generation has completed.
CREATE TABLE IF NOT EXISTS resume_run_retry_commands (
    idempotency_key  TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    result_run_id    TEXT,
    run_attempt      INTEGER NOT NULL CHECK (run_attempt >= 0),
    requested_by     TEXT NOT NULL CHECK (requested_by = 'user'),
    reconciliation_acknowledged INTEGER NOT NULL DEFAULT 0
        CHECK (reconciliation_acknowledged IN (0, 1)),
    requested_at     TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id),
    FOREIGN KEY (result_run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_retry_commands_no_update
BEFORE UPDATE ON resume_run_retry_commands
BEGIN
    SELECT RAISE(ABORT, 'resume retry commands are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_retry_commands_no_delete
BEFORE DELETE ON resume_run_retry_commands
BEGIN
    SELECT RAISE(ABORT, 'resume retry commands are immutable');
END;

-- A work-item delivery claims a resume run with an opaque owner token.  A recovered
-- delivery replaces this row and increments resume_runs.attempt, so a stale worker can
-- no longer attach an artifact to the run after losing its lease.
CREATE TABLE IF NOT EXISTS resume_run_owners (
    run_id       TEXT PRIMARY KEY,
    run_attempt  INTEGER NOT NULL CHECK (run_attempt > 0),
    owner_token  TEXT NOT NULL,
    claimed_at   TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);

CREATE TABLE IF NOT EXISTS resume_run_items (
    run_item_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    variant_kind      TEXT NOT NULL CHECK (
        variant_kind IN (
            'grounded_rewrite', 'standard_exaggerated',
            'market_ideal', 'keyword_adversarial'
        )
    ),
    purpose           TEXT NOT NULL CHECK (
        (variant_kind = 'grounded_rewrite' AND purpose = 'real_application')
        OR
        (variant_kind IN ('standard_exaggerated','market_ideal','keyword_adversarial')
         AND purpose = 'synthetic_research')
    ),
    base_version_id  TEXT,
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    status            TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed')),
    artifact_id       TEXT,
    output_fingerprint TEXT,
    error             TEXT NOT NULL DEFAULT '',
    attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    completed_at      TEXT,
    UNIQUE (run_id, variant_kind),
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id),
    FOREIGN KEY (base_version_id) REFERENCES resume_standard_versions(version_id),
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);

CREATE TRIGGER IF NOT EXISTS resume_runs_lineage_is_immutable
BEFORE UPDATE ON resume_runs
WHEN OLD.application_id IS NOT NEW.application_id
  OR OLD.ats IS NOT NEW.ats
  OR OLD.job_id IS NOT NEW.job_id
  OR OLD.job_snapshot_json IS NOT NEW.job_snapshot_json
  OR OLD.job_fingerprint IS NOT NEW.job_fingerprint
  OR OLD.selected_standard_id IS NOT NEW.selected_standard_id
  OR OLD.base_version_id IS NOT NEW.base_version_id
  OR OLD.base_fingerprint IS NOT NEW.base_fingerprint
  OR OLD.request_fingerprint IS NOT NEW.request_fingerprint
  OR OLD.idempotency_key IS NOT NEW.idempotency_key
BEGIN
    SELECT RAISE(ABORT, 'resume run lineage is immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_runs_no_delete
BEFORE DELETE ON resume_runs
BEGIN
    SELECT RAISE(ABORT, 'resume runs are append-only');
END;

CREATE TRIGGER IF NOT EXISTS resume_run_items_lineage_is_immutable
BEFORE UPDATE ON resume_run_items
WHEN OLD.run_id IS NOT NEW.run_id
  OR OLD.variant_kind IS NOT NEW.variant_kind
  OR OLD.purpose IS NOT NEW.purpose
  OR OLD.base_version_id IS NOT NEW.base_version_id
  OR OLD.input_fingerprint IS NOT NEW.input_fingerprint
BEGIN
    SELECT RAISE(ABORT, 'resume run item lineage is immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_items_succeeded_are_immutable
BEFORE UPDATE ON resume_run_items
WHEN OLD.status = 'succeeded'
BEGIN
    SELECT RAISE(ABORT, 'succeeded resume run items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_items_terminal_are_immutable
BEFORE UPDATE ON resume_run_items
WHEN OLD.status IN ('succeeded', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'terminal resume run items are immutable');
END;
DROP TRIGGER IF EXISTS resume_run_items_parent_terminal_are_immutable;
CREATE TRIGGER resume_run_items_parent_terminal_are_immutable
BEFORE UPDATE ON resume_run_items
WHEN EXISTS (
    SELECT 1 FROM resume_runs r
    WHERE r.run_id = OLD.run_id AND r.status IN ('succeeded', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'terminal resume run items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_items_no_delete
BEFORE DELETE ON resume_run_items
BEGIN
    SELECT RAISE(ABORT, 'resume run items are append-only');
END;
CREATE TRIGGER IF NOT EXISTS resume_runs_succeeded_are_immutable
BEFORE UPDATE ON resume_runs
WHEN OLD.status = 'succeeded'
BEGIN
    SELECT RAISE(ABORT, 'succeeded resume runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_runs_terminal_are_immutable
BEFORE UPDATE ON resume_runs
WHEN OLD.status IN ('succeeded', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'terminal resume runs are immutable');
END;

CREATE TABLE IF NOT EXISTS resume_artifact_approvals (
    approval_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    artifact_id       TEXT NOT NULL,
    pdf_sha256        TEXT NOT NULL CHECK (length(pdf_sha256) = 64),
    content_sha256    TEXT NOT NULL CHECK (length(content_sha256) = 64),
    idempotency_key   TEXT NOT NULL UNIQUE,
    approved_by       TEXT NOT NULL CHECK (approved_by = 'user'),
    approved_at       TEXT NOT NULL,
    UNIQUE (run_id, artifact_id),
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id),
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);

DROP TRIGGER IF EXISTS resume_artifact_approvals_exact_grounded;
CREATE TRIGGER resume_artifact_approvals_exact_grounded
BEFORE INSERT ON resume_artifact_approvals
WHEN NOT EXISTS (
    SELECT 1
    FROM resume_runs r
    JOIN resume_run_items i
      ON i.run_id = r.run_id
     AND i.variant_kind = 'grounded_rewrite'
     AND i.purpose = 'real_application'
     AND i.status = 'succeeded'
     AND i.artifact_id = NEW.artifact_id
    JOIN resume_artifacts a
      ON a.artifact_id = i.artifact_id
     AND a.variant_kind = 'grounded_rewrite'
     AND a.purpose = 'real_application'
     AND a.parse_safe = 1
     AND a.pdf_sha256 = NEW.pdf_sha256
     AND a.content_sha256 = NEW.content_sha256
     AND a.source_mode = r.source_mode
     AND a.composition_id IS r.composition_id
     AND ((a.source_mode='standard'
       AND json_extract(a.metadata_json, '$.grounding_validator_revision')='grounding-boundary-v5-relational'
       AND json_extract(a.metadata_json, '$.grounding_equivalence_revision')='grounding-equivalences-v3')
       OR (a.source_mode='career_profile' AND a.composition_id IS NOT NULL
       AND json_extract(a.metadata_json, '$.grounding_validator_revision')='career-grounding-v1'))
    WHERE r.run_id = NEW.run_id
      AND r.status IN ('succeeded', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'approval must bind a succeeded run grounded artifact');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifact_approvals_no_update
BEFORE UPDATE ON resume_artifact_approvals
BEGIN
    SELECT RAISE(ABORT, 'resume artifact approvals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifact_approvals_no_delete
BEFORE DELETE ON resume_artifact_approvals
BEGIN
    SELECT RAISE(ABORT, 'resume artifact approvals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS application_resume_selections_grounded_approved
BEFORE INSERT ON application_resume_selections
WHEN EXISTS (
    SELECT 1 FROM resume_artifacts a
    WHERE a.artifact_id = NEW.artifact_id
      AND a.variant_kind = 'grounded_rewrite'
)
AND NOT EXISTS (
    SELECT 1
    FROM resume_artifact_approvals p
    JOIN resume_runs r ON r.run_id = p.run_id
    WHERE p.artifact_id = NEW.artifact_id
      AND r.application_id = NEW.application_id
)
BEGIN
    SELECT RAISE(ABORT, 'grounded artifact requires exact run approval');
END;
DROP TRIGGER IF EXISTS application_resume_selections_current_grounding;
CREATE TRIGGER application_resume_selections_current_grounding
BEFORE INSERT ON application_resume_selections
WHEN EXISTS (
    SELECT 1 FROM resume_artifacts a
    WHERE a.artifact_id = NEW.artifact_id
      AND a.variant_kind = 'grounded_rewrite'
      AND NOT (
        (a.source_mode='career_profile' AND a.composition_id IS NOT NULL
          AND json_extract(a.metadata_json, '$.grounding_validator_revision') IS 'career-grounding-v1')
        OR (a.source_mode='standard' AND a.composition_id IS NULL
          AND json_extract(a.metadata_json, '$.grounding_validator_revision') IS 'grounding-boundary-v5-relational'
          AND json_extract(a.metadata_json, '$.grounding_equivalence_revision') IS 'grounding-equivalences-v3')
      )
)
BEGIN
    SELECT RAISE(ABORT, 'grounded artifact safety revision is stale');
END;
"""


def _now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    return {name: row[name] for name in row.keys()}


_DATABASE_IDENTITIES: dict[str, tuple[int, int]] = {}
_DATABASE_IDENTITIES_LOCK = threading.Lock()


def _private_file(path: Path) -> tuple[int, tuple[int, int]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and stat.S_ISLNK(os.lstat(path).st_mode):
        raise ResumeBoundaryError("resume-lab database must not be a symbolic link")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ResumeBoundaryError("resume-lab database must be a regular file")
    os.fchmod(descriptor, 0o600)
    return descriptor, (int(info.st_dev), int(info.st_ino))


def connect(path: Path) -> sqlite3.Connection:
    supplied = Path(path).expanduser()
    if supplied.is_symlink():
        raise ResumeBoundaryError("resume-lab database must not be a symbolic link")
    target = supplied.resolve()
    descriptor, identity = _private_file(target)
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(str(target), timeout=10)
        current = os.stat(target, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (
            int(current.st_dev),
            int(current.st_ino),
        ) != identity:
            raise ResumeBoundaryError("resume-lab database changed while opening")
        key = str(target)
        with _DATABASE_IDENTITIES_LOCK:
            pinned = _DATABASE_IDENTITIES.setdefault(key, identity)
            if pinned != identity:
                raise ResumeBoundaryError("resume-lab database identity changed")
    except Exception:
        if connection is not None:
            connection.close()
        raise
    finally:
        os.close(descriptor)
    assert connection is not None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        deadline = time.monotonic() + 10
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as exc:
                # WAL conversion precedes BEGIN IMMEDIATE. Concurrent cold
                # starts can return SQLITE_BUSY here without honoring SQLite's
                # busy_timeout, before the migration writer lock is available.
                code = getattr(exc, "sqlite_errorcode", None)
                busy = (code & 0xff) == 5 if code is not None else str(exc).casefold() == "database is locked"
                if not busy or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        connection.execute("PRAGMA synchronous = FULL")
    except Exception:
        connection.close()
        raise
    return connection


class ResumeLabStore:
    """Single writer for one private resume-lab sidecar."""

    def __init__(self, db_path: Path) -> None:
        supplied = Path(db_path).expanduser()
        if supplied.is_symlink():
            raise ResumeBoundaryError("resume-lab database must not be a symbolic link")
        self.db_path = supplied.resolve()
        with connect(self.db_path) as connection:
            # Schema creation, compatibility ALTERs, and backfill are one serialized
            # migration.  Several cloud processes can cold-start after the one-shot
            # initializer; none may observe the columns and then race another ALTER.
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            migrate_sources(connection)
            execute_schema(connection, SCHEMA_SQL)
            execute_schema(connection, SOURCE_TRIGGERS)
            retry_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(resume_run_retry_commands)"
                ).fetchall()
            }
            if "result_run_id" not in retry_columns:
                connection.execute(
                    "ALTER TABLE resume_run_retry_commands "
                    "ADD COLUMN result_run_id TEXT REFERENCES resume_runs(run_id)"
                )
            if "reconciliation_acknowledged" not in retry_columns:
                connection.execute(
                    "ALTER TABLE resume_run_retry_commands "
                    "ADD COLUMN reconciliation_acknowledged INTEGER NOT NULL "
                    "DEFAULT 0 CHECK (reconciliation_acknowledged IN (0, 1))"
                )
            # Older sidecars have insertion-ordered ``resume_runs.rowid`` values but no
            # explicit sequence.  Backfill only missing rows, oldest first, so reopening
            # an upgraded application preserves the best chronology still available.
            connection.execute(
                "INSERT INTO resume_run_order(run_id) "
                "SELECT r.run_id FROM resume_runs AS r "
                "LEFT JOIN resume_run_order AS o ON o.run_id=r.run_id "
                "WHERE o.run_id IS NULL ORDER BY r.rowid ASC"
            )
            connection.execute(
                "INSERT OR IGNORE INTO resume_lab_schema(version,applied_at) VALUES (1,?)",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO resume_lab_schema(version,applied_at) VALUES (2,?)",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO resume_lab_schema(version,applied_at) VALUES (3,?)",
                (_now(),),
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ResumeConflictError("resume migration violated source references")
            connection.execute("INSERT OR IGNORE INTO resume_lab_schema VALUES (4,?)", (_now(),))
        os.chmod(self.db_path, 0o600)

    def create_standard(
        self, name: str, manual_rank: int, *, actor_kind: str, active: bool = True
    ) -> Mapping[str, Any]:
        if actor_kind != "user":
            raise ResumeBoundaryError("hand-written standards require a user actor")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            raise ResumeLabError("standard name is invalid")
        if (
            isinstance(manual_rank, bool)
            or not isinstance(manual_rank, int)
            or manual_rank < 1
        ):
            raise ResumeLabError("manual_rank must be a positive integer")
        if not isinstance(active, bool):
            raise ResumeLabError("active must be a boolean")
        standard_id = _new_id("std_")
        stamp = _now()
        try:
            with connect(self.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if active:
                    active_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM resume_standards WHERE active=1"
                        ).fetchone()[0]
                    )
                    if active_count >= MAX_ACTIVE_STANDARDS:
                        raise ResumeConflictError(
                            f"at most {MAX_ACTIVE_STANDARDS} hand-written standards may be active"
                        )
                connection.execute(
                    "INSERT INTO resume_standards "
                    "(standard_id,name,manual_rank,active,active_version_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,NULL,?,?)",
                    (standard_id, name.strip(), manual_rank, int(active), stamp, stamp),
                )
                row = connection.execute(
                    "SELECT * FROM resume_standards WHERE standard_id=?", (standard_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            if "resume_standards.manual_rank" in str(exc):
                raise ResumeConflictError(
                    "an active standard already has that manual rank"
                ) from None
            raise
        return self._standard(row)

    @staticmethod
    def _standard(row: sqlite3.Row) -> Mapping[str, Any]:
        value = _row(row)
        value["active"] = bool(value["active"])
        return value

    def get_standard(self, standard_id: str) -> Mapping[str, Any]:
        validate_identifier(standard_id, "standard_id")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM resume_standards WHERE standard_id=?", (standard_id,)
            ).fetchone()
        if row is None:
            raise ResumeNotFoundError("resume standard was not found")
        return self._standard(row)

    def list_active_standards(self) -> Sequence[Mapping[str, Any]]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM resume_standards WHERE active=1 AND active_version_id IS NOT NULL "
                "ORDER BY manual_rank ASC,created_at ASC,standard_id ASC"
            ).fetchall()
        return tuple(self._standard(row) for row in rows)

    def add_standard_version(
        self,
        standard_id: str,
        value: StandardVersionInput,
        *,
        actor_kind: str,
        activate: bool = True,
    ) -> Mapping[str, Any]:
        if actor_kind != "user":
            raise ResumeBoundaryError(
                "hand-written standard versions require a user actor"
            )
        validate_identifier(standard_id, "standard_id")
        value.validate()
        if not isinstance(activate, bool):
            raise ResumeLabError("activate must be a boolean")
        stamp = _now()
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            standard = connection.execute(
                "SELECT * FROM resume_standards WHERE standard_id=?", (standard_id,)
            ).fetchone()
            if standard is None:
                raise ResumeNotFoundError("resume standard was not found")
            previous = connection.execute(
                "SELECT * FROM resume_standard_versions WHERE standard_id=? AND content_sha256=?",
                (standard_id, value.fingerprint),
            ).fetchone()
            if previous is not None:
                return self._version(previous)
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version_number),0)+1 FROM resume_standard_versions "
                    "WHERE standard_id=?",
                    (standard_id,),
                ).fetchone()[0]
            )
            version_id = _new_id("stdv_")
            connection.execute(
                "INSERT INTO resume_standard_versions "
                "(version_id,standard_id,version_number,tex_source,plain_text,claims_json,"
                "normalized_content_json,import_metadata_json,content_sha256,authored_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    standard_id,
                    number,
                    value.tex_source,
                    value.plain_text,
                    canonical_json(value.claims),
                    (
                        canonical_json(value.normalized_content)
                        if value.normalized_content is not None
                        else None
                    ),
                    (
                        canonical_json(value.import_metadata)
                        if value.import_metadata is not None
                        else None
                    ),
                    value.fingerprint,
                    value.authored_by,
                    stamp,
                ),
            )
            if activate or standard["active_version_id"] is None:
                connection.execute(
                    "UPDATE resume_standards SET active_version_id=?,updated_at=? WHERE standard_id=?",
                    (version_id, stamp, standard_id),
                )
            row = connection.execute(
                "SELECT * FROM resume_standard_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
            return self._version(row)

    @staticmethod
    def _version(row: sqlite3.Row) -> Mapping[str, Any]:
        value = _row(row)
        normalized = value.pop("normalized_content_json")
        value["normalized_content"] = json.loads(normalized) if normalized else None
        imported = value.pop("import_metadata_json")
        value["import_metadata"] = json.loads(imported) if imported else None
        value["claims"] = [
            asdict(claim) for claim in claims_from_json(value.pop("claims_json"))
        ]
        for claim in value["claims"]:
            claim["origin"] = claim["origin"].value
            claim["source_fact_ids"] = list(claim["source_fact_ids"])
        return value

    def get_standard_version(self, version_id: str) -> Mapping[str, Any]:
        validate_identifier(version_id, "version_id")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM resume_standard_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        if row is None:
            raise ResumeNotFoundError("standard resume version was not found")
        return self._version(row)

    def activate_standard_version(
        self, standard_id: str, version_id: str, *, actor_kind: str
    ) -> Mapping[str, Any]:
        if actor_kind != "user":
            raise ResumeBoundaryError("standard version selection must be manual")
        validate_identifier(standard_id, "standard_id")
        validate_identifier(version_id, "version_id")
        stamp = _now()
        with connect(self.db_path) as connection:
            version = connection.execute(
                "SELECT 1 FROM resume_standard_versions WHERE standard_id=? AND version_id=?",
                (standard_id, version_id),
            ).fetchone()
            if version is None:
                raise ResumeNotFoundError("version does not belong to the standard")
            connection.execute(
                "UPDATE resume_standards SET active_version_id=?,updated_at=? WHERE standard_id=?",
                (version_id, stamp, standard_id),
            )
        return self.get_standard(standard_id)

    def set_standard_rank(
        self, standard_id: str, manual_rank: int, *, actor_kind: str
    ) -> Mapping[str, Any]:
        if actor_kind != "user":
            raise ResumeBoundaryError("standard ranking must be manual")
        if (
            isinstance(manual_rank, bool)
            or not isinstance(manual_rank, int)
            or manual_rank < 1
        ):
            raise ResumeLabError("manual_rank must be a positive integer")
        standard = self.get_standard(standard_id)
        try:
            with connect(self.db_path) as connection:
                connection.execute(
                    "UPDATE resume_standards SET manual_rank=?,updated_at=? WHERE standard_id=?",
                    (manual_rank, _now(), standard_id),
                )
        except sqlite3.IntegrityError:
            raise ResumeConflictError(
                "an active standard already has that manual rank"
            ) from None
        del standard
        return self.get_standard(standard_id)

    def set_standard_active(
        self, standard_id: str, active: bool, *, actor_kind: str
    ) -> Mapping[str, Any]:
        if actor_kind != "user":
            raise ResumeBoundaryError("standard activation must be manual")
        if not isinstance(active, bool):
            raise ResumeLabError("active must be a boolean")
        try:
            with connect(self.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                standard = connection.execute(
                    "SELECT * FROM resume_standards WHERE standard_id=?",
                    (standard_id,),
                ).fetchone()
                if standard is None:
                    raise ResumeNotFoundError("resume standard was not found")
                if active and not standard["active_version_id"]:
                    raise ResumeConflictError(
                        "a standard needs a version before activation"
                    )
                if active and not bool(standard["active"]):
                    active_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM resume_standards WHERE active=1"
                        ).fetchone()[0]
                    )
                    if active_count >= MAX_ACTIVE_STANDARDS:
                        raise ResumeConflictError(
                            f"at most {MAX_ACTIVE_STANDARDS} hand-written standards may be active"
                        )
                connection.execute(
                    "UPDATE resume_standards SET active=?,updated_at=? WHERE standard_id=?",
                    (int(active), _now(), standard_id),
                )
        except sqlite3.IntegrityError:
            raise ResumeConflictError(
                "an active standard already has that manual rank"
            ) from None
        return self.get_standard(standard_id)

    def register_artifact(self, value: ArtifactInput) -> Mapping[str, Any]:
        value.validate()
        prefix = "real_" if value.purpose is ResumePurpose.REAL_APPLICATION else "syn_"
        required_path_prefix = (
            "real/" if value.purpose is ResumePurpose.REAL_APPLICATION else "research/"
        )
        if not value.managed_relative_path.startswith(required_path_prefix):
            raise ResumeBoundaryError(
                f"{value.purpose.value} artifact path must start with {required_path_prefix}"
            )
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            base = None
            if value.base_version_id:
                base = connection.execute(
                    "SELECT * FROM resume_standard_versions WHERE version_id=?",
                    (value.base_version_id,),
                ).fetchone()
                if base is None:
                    raise ResumeNotFoundError("artifact base version was not found")
            if value.variant_kind is VariantKind.STANDARD:
                if base is None:
                    raise ResumeLabError(
                        "standard artifact requires its immutable version"
                    )
                if (
                    value.tex_source != base["tex_source"]
                    or value.intended_text != base["plain_text"]
                    or canonical_json(value.claims) != base["claims_json"]
                ):
                    raise ResumeBoundaryError(
                        "standard artifact must exactly reproduce its immutable version"
                    )
            if value.source_mode == "career_profile":
                from .career_composition import validate_career_artifact
                composition = get_composition(connection, str(value.composition_id))
                validate_career_artifact(value, composition)
            if value.variant_kind is VariantKind.GROUNDED_REWRITE and value.source_mode == "standard":
                if base is None:
                    raise ResumeBoundaryError(
                        "grounded rewrite requires an immutable standard version"
                    )
                base_content = (
                    json.loads(base["normalized_content_json"])
                    if base["normalized_content_json"] is not None
                    else None
                )
                grounding_output = (value.metadata or {}).get("grounding_output")
                imported = (
                    json.loads(base["import_metadata_json"])
                    if base["import_metadata_json"] is not None
                    else None
                )
                normalization_claims = (
                    imported.get("normalization_claims")
                    if isinstance(imported, Mapping)
                    else None
                )
                if not isinstance(base_content, Mapping) or not isinstance(
                    grounding_output, Mapping
                ):
                    raise ResumeBoundaryError(
                        "grounded artifacts require a validated structured bundle"
                    )
                if (
                    (value.metadata or {}).get("grounding_validator_revision")
                    != GROUNDING_VALIDATOR_REVISION
                    or (value.metadata or {}).get("grounding_equivalence_revision")
                    != GROUNDING_EQUIVALENCE_REVISION
                ):
                    raise ResumeBoundaryError(
                        "grounded artifact validation revision is missing or changed"
                    )
                if not isinstance(normalization_claims, list) or not normalization_claims:
                    raise ResumeBoundaryError(
                        "grounded artifacts require immutable normalization claims"
                    )
                base_claims = {
                    item.claim_id: item for item in claims_from_json(base["claims_json"])
                }
                normalized_ids: set[str] = set()
                for source in normalization_claims:
                    if not isinstance(source, Mapping):
                        raise ResumeBoundaryError(
                            "immutable normalization claims are invalid"
                        )
                    source_id = source.get("claim_id")
                    source_text = source.get("text")
                    source_path = source.get("path")
                    if (
                        not isinstance(source_id, str)
                        or source_id in normalized_ids
                        or source_id not in base_claims
                        or source_text != base_claims[source_id].text
                        or not isinstance(source_path, str)
                        or not source_path.startswith("/")
                    ):
                        raise ResumeBoundaryError(
                            "immutable normalization claims differ from the base version"
                        )
                    normalized_ids.add(source_id)
                if normalized_ids != set(base_claims):
                    raise ResumeBoundaryError(
                        "immutable normalization claims do not cover the base claims"
                    )
                validate_grounded_artifact_boundary(
                    grounding_output,
                    {
                        "standard_id": str(base["standard_id"]),
                        "content": base_content,
                    },
                    normalization_claims,
                    value.claims,
                    value.intended_text,
                    template_version=(value.metadata or {}).get("template_version"),
                )
            previous = connection.execute(
                "SELECT * FROM resume_artifacts WHERE purpose=? AND content_sha256=?",
                (value.purpose.value, value.content_fingerprint),
            ).fetchone()
            if previous is not None:
                return self._artifact(previous, include_content=True)
            artifact_id = _new_id(prefix)
            connection.execute(
                "INSERT INTO resume_artifacts "
                "(artifact_id,purpose,variant_kind,ats,job_id,job_fingerprint,base_version_id,"
                "tex_source,intended_text,parsed_text,claims_json,managed_relative_path,pdf_sha256,"
                "content_sha256,parse_fidelity,parse_safe,generator_revision,study_id,pair_id,"
                "treatment,generation_seed,metadata_json,created_at,source_mode,composition_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    value.purpose.value,
                    value.variant_kind.value,
                    value.job.ats,
                    value.job.job_id,
                    value.job.fingerprint,
                    value.base_version_id,
                    value.tex_source,
                    value.intended_text,
                    value.parsed_text,
                    canonical_json(value.claims),
                    value.managed_relative_path,
                    value.pdf_sha256,
                    value.content_fingerprint,
                    float(value.parse_fidelity),
                    int(value.parse_safe),
                    value.generator_revision,
                    value.study_id,
                    value.pair_id,
                    value.treatment,
                    value.generation_seed,
                    canonical_json(value.metadata or {}),
                    _now(),
                    value.source_mode,
                    value.composition_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM resume_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            return self._artifact(row, include_content=True)

    @staticmethod
    def _artifact(row: sqlite3.Row, *, include_content: bool) -> Mapping[str, Any]:
        value = _row(row)
        value["parse_safe"] = bool(value["parse_safe"])
        value["metadata"] = json.loads(value.pop("metadata_json"))
        claims = claims_from_json(value.pop("claims_json"))
        value["claims"] = [
            {
                "claim_id": claim.claim_id,
                "text": claim.text,
                "origin": claim.origin.value,
                "source_fact_ids": list(claim.source_fact_ids),
            }
            for claim in claims
        ]
        if not include_content:
            value.pop("tex_source", None)
            value.pop("intended_text", None)
            value.pop("parsed_text", None)
            value.pop("claims", None)
        return value

    def get_artifact(
        self, artifact_id: str, *, include_content: bool = False
    ) -> Mapping[str, Any]:
        validate_identifier(artifact_id, "artifact_id")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM resume_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise ResumeNotFoundError("resume artifact was not found")
        return self._artifact(row, include_content=include_content)

    def find_standard_artifact(
        self,
        base_version_id: str,
        job_fingerprint: str,
        requirement_graph_fingerprint: str,
    ) -> Optional[Mapping[str, Any]]:
        """Read the cached score artifact for one exact standard/job analysis.

        This is deliberately narrower than a general artifact listing interface.  It
        lets read-only product views reconstruct a run's handwritten ranking without
        invoking the model or exposing resume content.
        """

        validate_identifier(base_version_id, "base_version_id")
        validate_sha256(job_fingerprint, "job_fingerprint")
        validate_sha256(
            requirement_graph_fingerprint, "requirement_graph_fingerprint"
        )
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM resume_artifacts WHERE variant_kind='standard' "
                "AND purpose='real_application' AND base_version_id=? "
                "AND job_fingerprint=? ORDER BY created_at DESC,artifact_id DESC "
                "LIMIT 200",
                (base_version_id, job_fingerprint),
            ).fetchall()
        for row in rows:
            artifact = self._artifact(row, include_content=False)
            if (
                artifact.get("metadata") or {}
            ).get("requirement_graph_fingerprint") == requirement_graph_fingerprint:
                return artifact
        return None

    def get_cached_evaluation(self, cache_key: str) -> Optional[Mapping[str, Any]]:
        validate_identifier(cache_key, "cache_key")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT result_json FROM resume_evaluation_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def has_artifact_evaluation(self, artifact_id: str, cache_key: str) -> bool:
        validate_identifier(artifact_id, "artifact_id")
        validate_identifier(cache_key, "cache_key")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT 1 FROM resume_artifact_evaluations "
                "WHERE artifact_id=? AND cache_key=?",
                (artifact_id, cache_key),
            ).fetchone()
        return row is not None

    def put_evaluation(
        self,
        artifact_id: str,
        graph: RequirementGraph,
        result: AtsProxyEvaluation,
    ) -> Mapping[str, Any]:
        validate_identifier(artifact_id, "artifact_id")
        expected_graph_fingerprint = content_sha256(
            {
                "revision": graph.graph_revision,
                "job_fingerprint": graph.job_fingerprint,
                "requirements": graph.requirements,
            }
        )
        if expected_graph_fingerprint != graph.fingerprint:
            raise ResumeConflictError("requirement graph fingerprint is invalid")
        if result.requirement_graph_fingerprint != graph.fingerprint:
            raise ResumeConflictError("evaluation used a different requirement graph")
        encoded = canonical_json(result)
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            artifact = connection.execute(
                "SELECT job_fingerprint,parsed_text FROM resume_artifacts "
                "WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if artifact is None:
                raise ResumeNotFoundError("resume artifact was not found")
            if artifact["job_fingerprint"] != graph.job_fingerprint:
                raise ResumeConflictError("evaluation job does not match the artifact")
            if result.artifact_text_sha256 != sha256_text(
                str(artifact["parsed_text"])
            ):
                raise ResumeConflictError("evaluation text does not match the artifact")
            previous = connection.execute(
                "SELECT result_json,job_fingerprint FROM resume_evaluation_cache "
                "WHERE cache_key=?",
                (result.cache_key,),
            ).fetchone()
            if previous:
                if (
                    previous["result_json"] != encoded
                    or previous["job_fingerprint"] != graph.job_fingerprint
                ):
                    raise ResumeConflictError("evaluation cache key changed content")
            else:
                connection.execute(
                    "INSERT INTO resume_evaluation_cache "
                    "(cache_key,artifact_id,job_fingerprint,requirement_graph_fingerprint,"
                    "scorer_revision,result_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        result.cache_key,
                        artifact_id,
                        graph.job_fingerprint,
                        result.requirement_graph_fingerprint,
                        result.scorer_revision,
                        encoded,
                        _now(),
                    ),
                )
            connection.execute(
                "INSERT OR IGNORE INTO resume_artifact_evaluations "
                "(artifact_id,cache_key,linked_at) VALUES (?,?,?)",
                (artifact_id, result.cache_key, _now()),
            )
        return json.loads(encoded)

    def approve_run(
        self,
        run_id: str,
        grounded_artifact_id: str,
        idempotency_key: str,
        *,
        actor_kind: str,
    ) -> Mapping[str, Any]:
        """Bind explicit user approval to the run's exact grounded PDF and content."""

        validate_identifier(run_id, "run_id")
        validate_identifier(grounded_artifact_id, "grounded_artifact_id")
        validate_identifier(idempotency_key, "idempotency_key")
        if actor_kind != "user":
            raise ResumeBoundaryError("grounded resume approval requires a user actor")
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM resume_artifact_approvals WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous is not None:
                if (
                    previous["run_id"] != run_id
                    or previous["artifact_id"] != grounded_artifact_id
                    or previous["approved_by"] != actor_kind
                ):
                    raise ResumeConflictError(
                        "approval idempotency key belongs to different content"
                    )
                return _row(previous)
            run = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ResumeNotFoundError("resume run was not found")
            artifact = connection.execute(
                "SELECT * FROM resume_artifacts WHERE artifact_id=?",
                (grounded_artifact_id,),
            ).fetchone()
            if artifact is None:
                raise ResumeNotFoundError("grounded resume artifact was not found")
            if (
                artifact["variant_kind"] != VariantKind.GROUNDED_REWRITE.value
                or artifact["purpose"] != ResumePurpose.REAL_APPLICATION.value
                or not int(artifact["parse_safe"])
                or not grounded_artifact_id.startswith("real_")
            ):
                raise ResumeBoundaryError(
                    "only the parse-safe grounded rewrite can be approved"
                )
            grounding_metadata = json.loads(artifact["metadata_json"])
            if not artifact_grounding_current(artifact, grounding_metadata):
                raise ResumeBoundaryError(
                    "grounded artifact must pass the current safety boundary"
                )
            if run["status"] not in {
                ResumeRunStatus.SUCCEEDED.value,
                ResumeRunStatus.FAILED.value,
            }:
                raise ResumeConflictError(
                    "resume run must finish before grounded approval"
                )
            item = connection.execute(
                "SELECT * FROM resume_run_items WHERE run_id=? AND variant_kind='grounded_rewrite'",
                (run_id,),
            ).fetchone()
            if (
                item is None
                or item["status"] != RunItemStatus.SUCCEEDED.value
                or item["artifact_id"] != grounded_artifact_id
                or item["output_fingerprint"] != artifact["content_sha256"]
            ):
                raise ResumeBoundaryError(
                    "approval artifact is not the run's exact grounded rewrite"
                )
            existing = connection.execute(
                "SELECT * FROM resume_artifact_approvals WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                raise ResumeConflictError("resume run was already approved")
            approval_id = _new_id("approval_")
            try:
                connection.execute(
                    "INSERT INTO resume_artifact_approvals "
                    "(approval_id,run_id,artifact_id,pdf_sha256,content_sha256,"
                    "idempotency_key,approved_by,approved_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        approval_id,
                        run_id,
                        grounded_artifact_id,
                        artifact["pdf_sha256"],
                        artifact["content_sha256"],
                        idempotency_key,
                        actor_kind,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "approval must bind" in str(exc):
                    raise ResumeBoundaryError(str(exc)) from None
                raise
            saved = connection.execute(
                "SELECT * FROM resume_artifact_approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            return _row(saved)

    def get_run_approval(self, run_id: str) -> Optional[Mapping[str, Any]]:
        validate_identifier(run_id, "run_id")
        with connect(self.db_path) as connection:
            run = connection.execute(
                "SELECT 1 FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ResumeNotFoundError("resume run was not found")
            row = connection.execute(
                "SELECT * FROM resume_artifact_approvals WHERE run_id=?", (run_id,)
            ).fetchone()
        return _row(row) if row else None

    def get_approval_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        validate_identifier(idempotency_key, "idempotency_key")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM resume_artifact_approvals WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return _row(row) if row else None

    def select_for_application(
        self,
        application_id: str,
        artifact_id: str,
        idempotency_key: str,
        *,
        actor_kind: str,
    ) -> Mapping[str, Any]:
        validate_identifier(application_id, "application_id")
        validate_identifier(artifact_id, "artifact_id")
        validate_identifier(idempotency_key, "idempotency_key")
        if actor_kind != "user":
            raise ResumeBoundaryError("application resume selection must be manual")
        artifact = self.get_artifact(artifact_id)
        if (
            artifact["purpose"] != ResumePurpose.REAL_APPLICATION.value
            or artifact["variant_kind"]
            not in {VariantKind.STANDARD.value, VariantKind.GROUNDED_REWRITE.value}
            or not artifact["parse_safe"]
            or not artifact_id.startswith("real_")
        ):
            raise ResumeBoundaryError(
                "only parse-safe real standard or grounded artifacts may be selected"
            )
        if artifact["variant_kind"] == VariantKind.GROUNDED_REWRITE.value:
            metadata = artifact.get("metadata") or {}
            if not artifact_grounding_current(artifact, metadata):
                raise ResumeBoundaryError(
                    "grounded artifact must pass the current safety boundary"
                )
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM application_resume_selections WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                if (
                    previous["application_id"] != application_id
                    or previous["artifact_id"] != artifact_id
                ):
                    raise ResumeConflictError(
                        "selection idempotency key belongs to different content"
                    )
                return _row(previous)
            if artifact["variant_kind"] == VariantKind.GROUNDED_REWRITE.value:
                approval = connection.execute(
                    "SELECT 1 FROM resume_artifact_approvals p "
                    "JOIN resume_runs r ON r.run_id=p.run_id "
                    "WHERE p.artifact_id=? AND r.application_id=?",
                    (artifact_id, application_id),
                ).fetchone()
                if approval is None:
                    raise ResumeBoundaryError(
                        "grounded artifact requires approval for this application run"
                    )
            selection_id = _new_id("sel_")
            try:
                connection.execute(
                    "INSERT INTO application_resume_selections "
                    "(selection_id,application_id,artifact_id,idempotency_key,selected_by,selected_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        selection_id,
                        application_id,
                        artifact_id,
                        idempotency_key,
                        actor_kind,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "only parse-safe real artifacts" in str(
                    exc
                ) or "requires exact run approval" in str(exc):
                    raise ResumeBoundaryError(str(exc)) from None
                raise
            row = connection.execute(
                "SELECT * FROM application_resume_selections WHERE selection_id=?",
                (selection_id,),
            ).fetchone()
            return _row(row)

    def current_application_selection(
        self, application_id: str
    ) -> Optional[Mapping[str, Any]]:
        validate_identifier(application_id, "application_id")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM application_resume_selections WHERE application_id=? "
                "ORDER BY selection_seq DESC LIMIT 1",
                (application_id,),
            ).fetchone()
        return _row(row) if row else None

    def get_selection_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        validate_identifier(idempotency_key, "idempotency_key")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM application_resume_selections WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return _row(row) if row else None

    @staticmethod
    def _safe_run_error(value: str) -> str:
        if not isinstance(value, str):
            raise ResumeLabError("run error must be text")
        return re.sub(r"[^A-Za-z0-9 ._:-]", "_", value).strip()[:500]

    @staticmethod
    def _ranking_snapshot(
        ranked_standards: Sequence[Mapping[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], str, str]:
        if isinstance(ranked_standards, (str, bytes)) or not isinstance(
            ranked_standards, (list, tuple)
        ):
            raise ResumeLabError("ranked standards must be a bounded array")
        if len(ranked_standards) > MAX_ACTIVE_STANDARDS:
            raise ResumeLabError("ranked standards exceed the configured limit")
        try:
            frozen = json.loads(canonical_json(ranked_standards))
        except (TypeError, ValueError) as exc:
            raise ResumeLabError("ranked standards are not canonical JSON") from exc
        if not isinstance(frozen, list) or any(
            not isinstance(item, dict) for item in frozen
        ):
            raise ResumeLabError("ranked standards must contain objects")
        encoded = canonical_json(frozen)
        if len(encoded.encode("utf-8")) > 2_000_000:
            raise ResumeLabError("ranked standard snapshot is too large")
        standard_ids: set[str] = set()
        version_ids: set[str] = set()
        for index, item in enumerate(frozen, 1):
            status = item.get("status")
            expected_keys = (
                _RANKING_COMMON_KEYS | _RANKING_SCORE_KEYS
                if status == "ready"
                else _RANKING_COMMON_KEYS | _RANKING_ERROR_KEYS
                if status == "error"
                else frozenset()
            )
            if not expected_keys or set(item) != expected_keys:
                raise ResumeLabError(
                    "ranked standard entry does not match the public snapshot schema"
                )
            validate_identifier(str(item.get("standard_id") or ""), "standard_id")
            validate_identifier(
                str(item.get("standard_version_id") or ""),
                "standard_version_id",
            )
            standard_id = str(item["standard_id"])
            version_id = str(item["standard_version_id"])
            if standard_id in standard_ids or version_id in version_ids:
                raise ResumeLabError(
                    "ranked standards must contain each active version exactly once"
                )
            standard_ids.add(standard_id)
            version_ids.add(version_id)
            if item.get("active_version_id") != version_id:
                raise ResumeLabError(
                    "ranked standard active version does not match its snapshot version"
                )
            if (
                not isinstance(item.get("name"), str)
                or not str(item["name"]).strip()
                or not isinstance(item.get("normalized"), bool)
                or not isinstance(item.get("parse_safe"), bool)
                or not isinstance(item.get("primary"), bool)
            ):
                raise ResumeLabError("ranked standard public fields are invalid")
            if item.get("rank") != index:
                raise ResumeLabError("ranked standards must have sequential ranks")
            if not isinstance(item.get("manual_rank"), int) or isinstance(
                item.get("manual_rank"), bool
            ):
                raise ResumeLabError("ranked standard manual rank is invalid")
            if status == "error" and (
                item.get("score") is not None
                or not isinstance(item.get("error_code"), str)
                or not str(item["error_code"]).strip()
            ):
                raise ResumeLabError("ranked standard error entry is invalid")
        return frozen, encoded, sha256_text(encoded)

    @staticmethod
    def _public_evaluation_snapshot(
        evaluation_id: str, raw: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Reconstruct the only score fields a ranking snapshot may persist."""

        try:
            raw_criteria = raw["criteria"]
            if not isinstance(raw_criteria, list):
                raise TypeError
            criteria = sorted(
                raw_criteria,
                key=lambda item: (
                    -float(item["weight"]),
                    str(item["requirement_id"]),
                ),
            )[:100]
            public_criteria = []
            for item in criteria:
                evidence = item["evidence"]
                if not isinstance(evidence, list):
                    raise TypeError
                public_criteria.append(
                    {
                        "requirement_id": item["requirement_id"],
                        "priority": item["priority"],
                        "kind": item["kind"],
                        "source_text": str(item["source_text"])[:1_000],
                        "status": item["status"],
                        "matched_terms": list(item["matched_terms"]),
                        "missing_term_groups": [
                            list(group) for group in item["missing_term_groups"]
                        ],
                        "weight": item["weight"],
                        "value": item["value"],
                        "confidence": item["confidence"],
                        "match_method": item["match_method"],
                        "evidence_count": len(evidence),
                    }
                )
            return {
                "evaluation_id": evaluation_id,
                "score": raw["parsed_fit"],
                "parsed_fit": raw["parsed_fit"],
                "requirement_evidence": raw["requirement_evidence"],
                "search_visibility": raw["search_visibility"],
                "screening_readiness": raw["screening_readiness"],
                "eligibility_status": raw["eligibility_status"],
                "scorer_revision": raw["scorer_revision"],
                "requirement_graph_fingerprint": raw[
                    "requirement_graph_fingerprint"
                ],
                "criteria": public_criteria,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ResumeConflictError(
                "ranked standard evaluation is invalid"
            ) from exc

    def create_run(
        self,
        application_id: str,
        job: JobSnapshot,
        selected_standard_id: str,
        base_version_id: str,
        idempotency_key: str,
        *,
        ranked_standards: Sequence[Mapping[str, Any]] = (),
        requirement_graph_fingerprint: str,
        requirement_clauses: Sequence[Mapping[str, Any]],
        requirement_extraction: str,
    ) -> Mapping[str, Any]:
        """Create one run and its four fixed derived-comparison items atomically."""

        validate_identifier(application_id, "application_id")
        validate_identifier(selected_standard_id, "selected_standard_id")
        validate_identifier(base_version_id, "base_version_id")
        validate_identifier(idempotency_key, "idempotency_key")
        validate_sha256(
            requirement_graph_fingerprint, "requirement_graph_fingerprint"
        )
        validate_identifier(requirement_extraction, "requirement_extraction")
        if not isinstance(requirement_clauses, (list, tuple)) or len(
            requirement_clauses
        ) > 1_000:
            raise ResumeLabError("requirement clauses must be a bounded array")
        clauses_json = canonical_json(requirement_clauses)
        if len(clauses_json.encode("utf-8")) > 500_000:
            raise ResumeLabError("requirement clauses are too large")
        ranking_snapshot, rankings_json, rankings_sha256 = self._ranking_snapshot(
            ranked_standards
        )
        job.validate()
        stamp = _now()
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            standard = connection.execute(
                "SELECT * FROM resume_standards WHERE standard_id=? AND active=1",
                (selected_standard_id,),
            ).fetchone()
            version = connection.execute(
                "SELECT * FROM resume_standard_versions WHERE version_id=? AND standard_id=?",
                (base_version_id, selected_standard_id),
            ).fetchone()
            if standard is None or version is None:
                raise ResumeNotFoundError("run standard or base version is unavailable")
            if standard["active_version_id"] != base_version_id:
                raise ResumeConflictError(
                    "run must snapshot the selected standard's active version"
                )
            if version["normalized_content_json"] is None:
                raise ResumeConflictError(
                    "selected standard version has no normalized_content for derived generation"
                )
            active_versions = {
                (str(row["standard_id"]), str(row["active_version_id"]))
                for row in connection.execute(
                    "SELECT standard_id,active_version_id FROM resume_standards "
                    "WHERE active=1 AND active_version_id IS NOT NULL"
                ).fetchall()
            }
            snapshot_versions = {
                (
                    str(ranked.get("standard_id") or ""),
                    str(ranked.get("standard_version_id") or ""),
                )
                for ranked in ranking_snapshot
            }
            if ranking_snapshot and snapshot_versions != active_versions:
                raise ResumeConflictError(
                    "active standard set changed while the ranking was prepared"
                )
            for ranked in ranking_snapshot:
                ranked_standard = connection.execute(
                    "SELECT name,manual_rank,active,active_version_id "
                    "FROM resume_standards WHERE standard_id=?",
                    (ranked["standard_id"],),
                ).fetchone()
                ranked_version = connection.execute(
                    "SELECT standard_id,normalized_content_json,import_metadata_json "
                    "FROM resume_standard_versions WHERE version_id=?",
                    (ranked["standard_version_id"],),
                ).fetchone()
                try:
                    import_metadata = (
                        json.loads(ranked_version["import_metadata_json"])
                        if ranked_version is not None
                        and ranked_version["import_metadata_json"] is not None
                        else {}
                    )
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ResumeConflictError(
                        "ranked standard import metadata is invalid"
                    ) from exc
                expected_normalized = bool(
                    ranked_version is not None
                    and ranked_version["normalized_content_json"] is not None
                    and isinstance(import_metadata, Mapping)
                    and import_metadata.get("normalization_validator_revision")
                    == NORMALIZATION_VALIDATOR_REVISION
                )
                expected_parse_safe = bool(
                    isinstance(import_metadata, Mapping)
                    and import_metadata.get("parse_safe")
                )
                if (
                    ranked_standard is None
                    or not int(ranked_standard["active"])
                    or ranked_standard["active_version_id"]
                    != ranked["standard_version_id"]
                    or ranked_standard["name"] != ranked.get("name")
                    or int(ranked_standard["manual_rank"])
                    != int(ranked["manual_rank"])
                    or ranked_version is None
                    or str(ranked_version["standard_id"]) != str(ranked["standard_id"])
                    or ranked.get("active_version_id")
                    != ranked_standard["active_version_id"]
                    or ranked.get("normalized") is not expected_normalized
                    or ranked.get("parse_safe") is not expected_parse_safe
                ):
                    raise ResumeConflictError(
                        "ranked standard snapshot references an invalid version"
                    )
                artifact_id = ranked.get("artifact_id")
                evaluation_id = ranked.get("evaluation_id")
                if artifact_id is None and evaluation_id is None:
                    if ranked.get("score") is not None or ranked.get("status") != "error":
                        raise ResumeConflictError(
                            "unscored ranked standard has invalid status"
                        )
                    continue
                if not isinstance(artifact_id, str) or not isinstance(
                    evaluation_id, str
                ):
                    raise ResumeConflictError(
                        "ranked standard artifact and evaluation must be paired"
                    )
                artifact = connection.execute(
                    "SELECT purpose,variant_kind,job_fingerprint,base_version_id,metadata_json "
                    "FROM resume_artifacts WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                evaluation = connection.execute(
                    "SELECT e.result_json,e.job_fingerprint,"
                    "e.requirement_graph_fingerprint,e.scorer_revision "
                    "FROM resume_artifact_evaluations AS l "
                    "JOIN resume_evaluation_cache AS e ON e.cache_key=l.cache_key "
                    "WHERE l.artifact_id=? AND l.cache_key=?",
                    (artifact_id, evaluation_id),
                ).fetchone()
                if (
                    artifact is None
                    or evaluation is None
                    or artifact["purpose"] != ResumePurpose.REAL_APPLICATION.value
                    or artifact["variant_kind"] != VariantKind.STANDARD.value
                    or artifact["job_fingerprint"] != job.fingerprint
                    or artifact["base_version_id"] != ranked["standard_version_id"]
                    or evaluation["job_fingerprint"] != job.fingerprint
                    or evaluation["requirement_graph_fingerprint"]
                    != requirement_graph_fingerprint
                    or (json.loads(artifact["metadata_json"])).get("evaluation_id")
                    != evaluation_id
                ):
                    raise ResumeConflictError(
                        "ranked standard snapshot has an invalid score binding"
                    )
                try:
                    raw_evaluation = json.loads(evaluation["result_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ResumeConflictError(
                        "ranked standard evaluation is invalid"
                    ) from exc
                if not isinstance(raw_evaluation, Mapping):
                    raise ResumeConflictError(
                        "ranked standard evaluation is invalid"
                    )
                expected_score = self._public_evaluation_snapshot(
                    evaluation_id, raw_evaluation
                )
                actual_score = {
                    key: ranked.get(key) for key in expected_score
                }
                if canonical_json(actual_score) != canonical_json(expected_score):
                    raise ResumeConflictError(
                        "ranked standard snapshot score does not match its evaluation"
                    )
                if ranked.get("status") != "ready":
                    raise ResumeConflictError(
                        "scored ranked standard has invalid status"
                    )
            expected_order = sorted(
                ranking_snapshot,
                key=lambda item: (
                    item.get("score") is None,
                    -float(item.get("score") or 0),
                    int(item["manual_rank"]),
                    str(item.get("name") or "").casefold(),
                    str(item["standard_version_id"]),
                ),
            )
            if [item["standard_id"] for item in ranking_snapshot] != [
                item["standard_id"] for item in expected_order
            ]:
                raise ResumeConflictError(
                    "ranked standard snapshot is not in evaluated score order"
                )
            if any(
                bool(item.get("primary"))
                != (index == 0 and item.get("score") is not None)
                for index, item in enumerate(ranking_snapshot)
            ):
                raise ResumeConflictError(
                    "ranked standard snapshot primary marker is invalid"
                )
            viable = [
                item
                for item in ranking_snapshot
                if item.get("score") is not None and item.get("artifact_id")
            ]
            if ranking_snapshot and (
                not viable
                or str(viable[0]["standard_id"]) != selected_standard_id
                or str(viable[0]["standard_version_id"]) != base_version_id
            ):
                raise ResumeConflictError(
                    "run base does not match the frozen ranking winner"
                )
            request_fingerprint = content_sha256(
                {
                    "application_id": application_id,
                    "job_fingerprint": job.fingerprint,
                    "selected_standard_id": selected_standard_id,
                    "base_version_id": base_version_id,
                    "base_fingerprint": version["content_sha256"],
                    "requirement_graph_fingerprint": requirement_graph_fingerprint,
                    "requirement_clauses": requirement_clauses,
                    "requirement_extraction": requirement_extraction,
                    "rankings_sha256": rankings_sha256,
                }
            )
            previous = connection.execute(
                "SELECT * FROM resume_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                if previous["request_fingerprint"] != request_fingerprint:
                    raise ResumeConflictError(
                        "run idempotency key belongs to different input"
                    )
                return self._run(connection, previous)
            run_id = _new_id("run_")
            connection.execute(
                "INSERT INTO resume_runs "
                "(run_id,application_id,ats,job_id,job_snapshot_json,job_fingerprint,"
                "selected_standard_id,base_version_id,base_fingerprint,request_fingerprint,"
                "idempotency_key,status,attempt,error,result_fingerprint,created_at,started_at,"
                "completed_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',0,'',NULL,?,NULL,NULL,?)",
                (
                    run_id,
                    application_id,
                    job.ats,
                    job.job_id,
                    canonical_json(job),
                    job.fingerprint,
                    selected_standard_id,
                    base_version_id,
                    version["content_sha256"],
                    request_fingerprint,
                    idempotency_key,
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                "INSERT INTO resume_run_order(run_id) VALUES (?)", (run_id,)
            )
            connection.execute(
                "INSERT INTO resume_run_analyses "
                "(run_id,requirement_graph_fingerprint,extraction_source,clauses_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    run_id,
                    requirement_graph_fingerprint,
                    requirement_extraction,
                    clauses_json,
                    stamp,
                ),
            )
            if ranking_snapshot:
                connection.execute(
                    "INSERT INTO resume_run_ranking_snapshots "
                    "(run_id,rankings_json,rankings_sha256,created_at) VALUES (?,?,?,?)",
                    (run_id, rankings_json, rankings_sha256, stamp),
                )
            for kind in RUN_VARIANTS:
                purpose = (
                    ResumePurpose.REAL_APPLICATION
                    if kind is VariantKind.GROUNDED_REWRITE
                    else ResumePurpose.SYNTHETIC_RESEARCH
                )
                item_base = (
                    base_version_id
                    if kind
                    in {VariantKind.GROUNDED_REWRITE, VariantKind.STANDARD_EXAGGERATED}
                    else None
                )
                item_fingerprint = content_sha256(
                    {
                        "request_fingerprint": request_fingerprint,
                        "variant_kind": kind,
                        "purpose": purpose,
                        "base_version_id": item_base,
                    }
                )
                connection.execute(
                    "INSERT INTO resume_run_items "
                    "(run_item_id,run_id,variant_kind,purpose,base_version_id,input_fingerprint,"
                    "status,artifact_id,output_fingerprint,error,attempts,completed_at) "
                    "VALUES (?,?,?,?,?,?,'pending',NULL,NULL,'',0,NULL)",
                    (
                        _new_id("item_"),
                        run_id,
                        kind.value,
                        purpose.value,
                        item_base,
                        item_fingerprint,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return self._run(connection, row)

    @staticmethod
    def _validate_run_items(
        connection: sqlite3.Connection,
        run: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
    ) -> None:
        for item in items:
            status = str(item["status"])
            if status == RunItemStatus.SUCCEEDED.value:
                if (
                    not item.get("artifact_id")
                    or not item.get("output_fingerprint")
                    or item.get("error")
                    or not item.get("completed_at")
                ):
                    raise ResumeConflictError(
                        "succeeded resume run item has invalid result fields"
                    )
                artifact = connection.execute(
                    "SELECT variant_kind,purpose,job_fingerprint,base_version_id,"
                    "content_sha256,parse_safe,study_id,pair_id,treatment,generation_seed,source_mode,composition_id "
                    "FROM resume_artifacts WHERE artifact_id=?",
                    (item["artifact_id"],),
                ).fetchone()
                if (
                    artifact is None
                    or artifact["source_mode"] != run["source_mode"]
                    or artifact["composition_id"] != run["composition_id"]
                    or artifact["variant_kind"] != item["variant_kind"]
                    or artifact["purpose"] != item["purpose"]
                    or artifact["job_fingerprint"] != run["job_fingerprint"]
                    or (artifact["base_version_id"] or None)
                    != (item["base_version_id"] or None)
                    or artifact["content_sha256"] != item["output_fingerprint"]
                    or not int(artifact["parse_safe"])
                ):
                    raise ResumeConflictError(
                        "resume run item artifact binding is invalid"
                    )
                if item["purpose"] == ResumePurpose.SYNTHETIC_RESEARCH.value and (
                    artifact["pair_id"] != "pair_" + str(run["run_id"])
                    or artifact["study_id"]
                    != "study_" + str(run["job_fingerprint"])[:24]
                    or artifact["treatment"] != item["variant_kind"]
                    or artifact["generation_seed"] is None
                ):
                    raise ResumeConflictError(
                        "resume run synthetic study binding is invalid"
                    )
            elif status == RunItemStatus.FAILED.value:
                if (
                    item.get("artifact_id") is not None
                    or item.get("output_fingerprint") is not None
                    or not item.get("error")
                    or not item.get("completed_at")
                ):
                    raise ResumeConflictError(
                        "failed resume run item has invalid result fields"
                    )
            elif status == RunItemStatus.PENDING.value:
                if (
                    item.get("artifact_id") is not None
                    or item.get("output_fingerprint") is not None
                    or item.get("error")
                    or item.get("completed_at") is not None
                ):
                    raise ResumeConflictError(
                        "pending resume run item has invalid result fields"
                    )
            else:  # pragma: no cover - table CHECK is the primary guard
                raise ResumeConflictError("resume run item status is invalid")

        if run["status"] == ResumeRunStatus.SUCCEEDED.value:
            if any(
                item["status"] != RunItemStatus.SUCCEEDED.value for item in items
            ):
                raise ResumeConflictError(
                    "succeeded resume run contains an incomplete comparison"
                )
            expected = content_sha256(
                sorted(str(item["output_fingerprint"]) for item in items)
            )
            if run.get("result_fingerprint") != expected:
                raise ResumeConflictError("resume run result fingerprint is invalid")

    @staticmethod
    def _run(connection: sqlite3.Connection, row: sqlite3.Row) -> Mapping[str, Any]:
        value = _row(row)
        value["job_snapshot"] = json.loads(value.pop("job_snapshot_json"))
        analysis = connection.execute(
            "SELECT requirement_graph_fingerprint,extraction_source,clauses_json "
            "FROM resume_run_analyses WHERE run_id=?",
            (row["run_id"],),
        ).fetchone()
        value["requirement_analysis"] = (
            {
                "requirement_graph_fingerprint": analysis[
                    "requirement_graph_fingerprint"
                ],
                "extraction_source": analysis["extraction_source"],
                "clauses": json.loads(analysis["clauses_json"]),
            }
            if analysis is not None
            else None
        )
        ranking = connection.execute(
            "SELECT rankings_json,rankings_sha256 FROM resume_run_ranking_snapshots "
            "WHERE run_id=?",
            (row["run_id"],),
        ).fetchone()
        if ranking is None:
            value["ranked_standards"] = None
        else:
            encoded_rankings = str(ranking["rankings_json"])
            if sha256_text(encoded_rankings) != ranking["rankings_sha256"]:
                raise ResumeConflictError("resume run ranking snapshot changed")
            decoded_rankings = json.loads(encoded_rankings)
            if not isinstance(decoded_rankings, list):
                raise ResumeConflictError("resume run ranking snapshot is invalid")
            value["ranked_standards"] = decoded_rankings
        items = connection.execute(
            "SELECT * FROM resume_run_items WHERE run_id=? ORDER BY CASE variant_kind "
            "WHEN 'grounded_rewrite' THEN 1 WHEN 'standard_exaggerated' THEN 2 "
            "WHEN 'market_ideal' THEN 3 WHEN 'keyword_adversarial' THEN 4 END",
            (row["run_id"],),
        ).fetchall()
        value["items"] = [_row(item) for item in items]
        expected = ([kind.value for kind in RUN_VARIANTS] if value["source_mode"] == "standard"
                    else [kind.value for kind in RUN_VARIANTS[1:]] if value["run_role"] == "research"
                    else [VariantKind.GROUNDED_REWRITE.value])
        if [item["variant_kind"] for item in value["items"]] != expected:
            raise ResumeConflictError(
                "resume run does not contain the exact comparison items"
            )
        ResumeLabStore._validate_run_items(connection, value, value["items"])
        approval = connection.execute(
            "SELECT * FROM resume_artifact_approvals WHERE run_id=?",
            (row["run_id"],),
        ).fetchone()
        value["grounded_approval"] = _row(approval) if approval else None
        return value

    def get_run(self, run_id: str) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise ResumeNotFoundError("resume run was not found")
            return self._run(connection, row)

    def get_run_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        validate_identifier(idempotency_key, "idempotency_key")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM resume_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            return self._run(connection, row) if row is not None else None

    def get_latest_application_run(
        self, application_id: str
    ) -> Optional[Mapping[str, Any]]:
        validate_identifier(application_id, "application_id")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT r.* FROM resume_runs AS r "
                "JOIN resume_run_order AS o ON o.run_id=r.run_id "
                "WHERE r.application_id=? AND r.run_role='primary' ORDER BY o.run_seq DESC LIMIT 1",
                (application_id,),
            ).fetchone()
            return self._run(connection, row) if row is not None else None

    def list_runs(
        self, statuses: Optional[Sequence[str]] = None, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ResumeLabError("run list limit must be between 1 and 200")
        allowed = {status.value for status in ResumeRunStatus}
        selected = tuple(dict.fromkeys(str(value) for value in (statuses or ())))
        if any(value not in allowed for value in selected):
            raise ResumeLabError("run status filter is invalid")
        with connect(self.db_path) as connection:
            if selected:
                placeholders = ",".join("?" for _ in selected)
                rows = connection.execute(
                    "SELECT r.* FROM resume_runs AS r "
                    "JOIN resume_run_order AS o ON o.run_id=r.run_id "
                    "WHERE r.status IN (" + placeholders + ") "
                    "ORDER BY o.run_seq DESC LIMIT ?",
                    (*selected, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT r.* FROM resume_runs AS r "
                    "JOIN resume_run_order AS o ON o.run_id=r.run_id "
                    "ORDER BY o.run_seq DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return tuple(self._run(connection, row) for row in rows)

    def list_runs_for_job(
        self, ats: str, job_id: str, *, limit: int = 10
    ) -> Sequence[Mapping[str, Any]]:
        validate_identifier(ats, "ats")
        validate_identifier(job_id, "job_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ResumeLabError("job run list limit must be between 1 and 50")
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT r.* FROM resume_runs AS r "
                "JOIN resume_run_order AS o ON o.run_id=r.run_id "
                "WHERE r.ats=? AND r.job_id=? ORDER BY o.run_seq DESC LIMIT ?",
                (ats.lower(), job_id, limit),
            ).fetchall()
            return tuple(self._run(connection, row) for row in rows)

    def start_run(
        self, run_id: str, *, owner_token: Optional[str] = None
    ) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        if owner_token is not None:
            validate_identifier(owner_token, "owner_token")
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise ResumeNotFoundError("resume run was not found")
            owner = connection.execute(
                "SELECT * FROM resume_run_owners WHERE run_id=?", (run_id,)
            ).fetchone()
            if (
                row["status"] == ResumeRunStatus.RUNNING.value
                and owner_token is not None
                and owner is not None
                and owner["owner_token"] == owner_token
                and int(owner["run_attempt"]) == int(row["attempt"])
            ):
                return self._run(connection, row)
            if row["status"] == ResumeRunStatus.RUNNING.value and owner_token is None:
                return self._run(connection, row)
            if row["status"] != ResumeRunStatus.QUEUED.value:
                if not (
                    row["status"] == ResumeRunStatus.RUNNING.value
                    and owner_token is not None
                ):
                    raise ResumeConflictError("only a queued resume run can start")
            stamp = _now()
            next_attempt = int(row["attempt"]) + 1
            connection.execute(
                "UPDATE resume_runs SET status='running',attempt=?,started_at=?,"
                "updated_at=? WHERE run_id=?",
                (next_attempt, stamp, stamp, run_id),
            )
            if owner_token is not None:
                connection.execute(
                    "INSERT INTO resume_run_owners "
                    "(run_id,run_attempt,owner_token,claimed_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET "
                    "run_attempt=excluded.run_attempt,owner_token=excluded.owner_token,"
                    "claimed_at=excluded.claimed_at",
                    (run_id, next_attempt, owner_token, stamp),
                )
            saved = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return self._run(connection, saved)

    @staticmethod
    def _require_run_owner(
        connection: sqlite3.Connection,
        run_id: str,
        run_attempt: Optional[int],
        owner_token: Optional[str],
    ) -> None:
        if (run_attempt is None) != (owner_token is None):
            raise ResumeLabError("run owner and attempt must be supplied together")
        if run_attempt is None:
            owner = connection.execute(
                "SELECT 1 FROM resume_run_owners WHERE run_id=?", (run_id,)
            ).fetchone()
            if owner is not None:
                raise ResumeConflictError(
                    "owned resume run requires its attempt and owner token"
                )
            return
        if isinstance(run_attempt, bool) or not isinstance(run_attempt, int):
            raise ResumeLabError("run attempt must be an integer")
        validate_identifier(str(owner_token), "owner_token")
        owner = connection.execute(
            "SELECT 1 FROM resume_run_owners o JOIN resume_runs r ON r.run_id=o.run_id "
            "WHERE o.run_id=? AND o.run_attempt=? AND o.owner_token=? "
            "AND r.status='running' AND r.attempt=o.run_attempt",
            (run_id, run_attempt, owner_token),
        ).fetchone()
        if owner is None:
            raise ResumeConflictError("resume run ownership was lost")

    def assert_run_owner(
        self, run_id: str, run_attempt: int, owner_token: str
    ) -> None:
        validate_identifier(run_id, "run_id")
        with connect(self.db_path) as connection:
            self._require_run_owner(connection, run_id, run_attempt, owner_token)

    def complete_run_item(
        self,
        run_id: str,
        variant_kind: VariantKind,
        outcome: str,
        *,
        artifact_id: Optional[str] = None,
        error: str = "",
        run_attempt: Optional[int] = None,
        owner_token: Optional[str] = None,
    ) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        if variant_kind not in RUN_VARIANTS:
            raise ResumeLabError("variant is not a derived comparison item")
        if outcome not in {RunItemStatus.SUCCEEDED.value, RunItemStatus.FAILED.value}:
            raise ResumeLabError("run item outcome must be succeeded or failed")
        safe_error = self._safe_run_error(error)
        if outcome == RunItemStatus.SUCCEEDED.value and not artifact_id:
            raise ResumeLabError("successful run item requires an artifact")
        if outcome == RunItemStatus.FAILED.value and not safe_error:
            raise ResumeLabError("failed run item requires a safe error code")
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ResumeNotFoundError("resume run was not found")
            self._require_run_owner(
                connection, run_id, run_attempt, owner_token
            )
            if run["status"] != ResumeRunStatus.RUNNING.value:
                raise ResumeConflictError(
                    "run items can complete only while the run is running"
                )
            item = connection.execute(
                "SELECT * FROM resume_run_items WHERE run_id=? AND variant_kind=?",
                (run_id, variant_kind.value),
            ).fetchone()
            if item is None:
                raise ResumeConflictError("resume run comparison item is missing")
            output_fingerprint = None
            if outcome == RunItemStatus.SUCCEEDED.value:
                validate_identifier(str(artifact_id), "artifact_id")
                artifact = connection.execute(
                    "SELECT * FROM resume_artifacts WHERE artifact_id=?", (artifact_id,)
                ).fetchone()
                if artifact is None:
                    raise ResumeNotFoundError("run item artifact was not found")
                if (
                    artifact["source_mode"] != run["source_mode"]
                    or artifact["composition_id"] != run["composition_id"]
                    or artifact["variant_kind"] != variant_kind.value
                    or artifact["purpose"] != item["purpose"]
                    or artifact["job_fingerprint"] != run["job_fingerprint"]
                    or not int(artifact["parse_safe"])
                    or (
                        item["base_version_id"] is not None
                        and artifact["base_version_id"] != item["base_version_id"]
                    )
                ):
                    raise ResumeBoundaryError(
                        "artifact does not satisfy the run item boundary"
                    )
                if item["purpose"] == ResumePurpose.SYNTHETIC_RESEARCH.value and (
                    artifact["pair_id"] != "pair_" + run_id
                    or artifact["study_id"]
                    != "study_" + str(run["job_fingerprint"])[:24]
                    or artifact["treatment"] != variant_kind.value
                    or artifact["generation_seed"] is None
                ):
                    raise ResumeBoundaryError(
                        "synthetic artifact does not satisfy run study lineage"
                    )
                output_fingerprint = artifact["content_sha256"]
                safe_error = ""
            if item["status"] != RunItemStatus.PENDING.value:
                same = (
                    item["status"] == outcome
                    and (item["artifact_id"] or None) == artifact_id
                    and item["error"] == safe_error
                )
                if same:
                    return _row(item)
                raise ResumeConflictError("run item already completed differently")
            stamp = _now()
            connection.execute(
                "UPDATE resume_run_items SET status=?,artifact_id=?,output_fingerprint=?,"
                "error=?,attempts=attempts+1,completed_at=? WHERE run_item_id=?",
                (
                    outcome,
                    artifact_id,
                    output_fingerprint,
                    safe_error,
                    stamp,
                    item["run_item_id"],
                ),
            )
            saved = connection.execute(
                "SELECT * FROM resume_run_items WHERE run_item_id=?",
                (item["run_item_id"],),
            ).fetchone()
            return _row(saved)

    def complete_run(
        self,
        run_id: str,
        outcome: str,
        *,
        error: str = "",
        run_attempt: Optional[int] = None,
        owner_token: Optional[str] = None,
    ) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        if outcome not in {
            ResumeRunStatus.SUCCEEDED.value,
            ResumeRunStatus.FAILED.value,
        }:
            raise ResumeLabError("run outcome must be succeeded or failed")
        safe_error = self._safe_run_error(error)
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ResumeNotFoundError("resume run was not found")
            self._require_run_owner(
                connection, run_id, run_attempt, owner_token
            )
            if run["status"] == outcome:
                if run["error"] != safe_error:
                    raise ResumeConflictError(
                        "resume run already completed differently"
                    )
                return self._run(connection, run)
            if run["status"] != ResumeRunStatus.RUNNING.value:
                raise ResumeConflictError("only a running resume run can complete")
            items = connection.execute(
                "SELECT * FROM resume_run_items WHERE run_id=?", (run_id,)
            ).fetchall()
            expected_count = (len(RUN_VARIANTS) if run["source_mode"] == "standard"
                              else 3 if run["run_role"] == "research" else 1)
            if len(items) != expected_count:
                raise ResumeConflictError("resume run comparison is incomplete")
            if outcome == ResumeRunStatus.SUCCEEDED.value:
                if any(
                    item["status"] != RunItemStatus.SUCCEEDED.value for item in items
                ):
                    raise ResumeConflictError(
                        "all requested comparison items must succeed first"
                    )
                safe_error = ""
                result_fingerprint = content_sha256(
                    sorted(str(item["output_fingerprint"]) for item in items)
                )
            else:
                if not safe_error:
                    raise ResumeLabError("failed run requires a safe error code")
                result_fingerprint = None
            stamp = _now()
            connection.execute(
                "UPDATE resume_runs SET status=?,error=?,result_fingerprint=?,completed_at=?,"
                "updated_at=? WHERE run_id=?",
                (outcome, safe_error, result_fingerprint, stamp, stamp, run_id),
            )
            saved = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return self._run(connection, saved)

    def fail_queued_run(self, run_id: str, *, error: str) -> Mapping[str, Any]:
        """Fail a run whose durable work item could not be enqueued."""

        validate_identifier(run_id, "run_id")
        safe_error = self._safe_run_error(error)
        if not safe_error:
            raise ResumeLabError("failed run requires a safe error code")
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ResumeNotFoundError("resume run was not found")
            if run["status"] == ResumeRunStatus.FAILED.value:
                return self._run(connection, run)
            if run["status"] != ResumeRunStatus.QUEUED.value:
                raise ResumeConflictError("only a queued resume run can fail enqueue")
            stamp = _now()
            connection.execute(
                "UPDATE resume_runs SET status='failed',error=?,completed_at=?,updated_at=? "
                "WHERE run_id=?",
                (safe_error, stamp, stamp, run_id),
            )
            saved = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return self._run(connection, saved)

    def cancel_active_run_for_application_phase(
        self, run_id: str, *, error: str = "application_not_preparing"
    ) -> Mapping[str, Any]:
        """Fence and terminalize queued/running work after its application closes.

        This is intentionally owner-independent: an authoritative application-phase
        check may discover an orphaned running run whose worker no longer exists.  The
        owner row is deleted in the same writer transaction, so any live stale worker
        loses authority before the failed state becomes visible.
        """

        validate_identifier(run_id, "run_id")
        safe_error = self._safe_run_error(error)
        if not safe_error:
            raise ResumeLabError("cancelled run requires a safe error code")
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ResumeNotFoundError("resume run was not found")
            if run["status"] in {
                ResumeRunStatus.SUCCEEDED.value,
                ResumeRunStatus.FAILED.value,
            }:
                return self._run(connection, run)
            connection.execute(
                "DELETE FROM resume_run_owners WHERE run_id=?", (run_id,)
            )
            stamp = _now()
            connection.execute(
                "UPDATE resume_runs SET status='failed',error=?,completed_at=?,"
                "updated_at=? WHERE run_id=? AND status IN ('queued','running')",
                (safe_error, stamp, stamp, run_id),
            )
            saved = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return self._run(connection, saved)

    def get_retry_command(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        validate_identifier(idempotency_key, "idempotency_key")
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM resume_run_retry_commands WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return _row(row) if row else None

    def retry_run(
        self,
        run_id: str,
        idempotency_key: str,
        *,
        actor_kind: str,
        reconciliation_acknowledged: bool = False,
    ) -> Mapping[str, Any]:
        """Record a retry command without rewriting terminal experiment history.

        A queued/running run only needs another idempotent queue delivery, so its retry
        command points back to that run.  A failed run is terminal: retrying creates a
        successor with the same frozen inputs and four fresh pending items.  In
        particular, synthetic artifacts cannot be reused because their pair identity is
        deliberately bound to one run.
        """

        validate_identifier(run_id, "run_id")
        validate_identifier(idempotency_key, "idempotency_key")
        if actor_kind != "user":
            raise ResumeBoundaryError("resume retry requires a user actor")
        if not isinstance(reconciliation_acknowledged, bool):
            raise ResumeBoundaryError(
                "reconciliation acknowledgment must be a boolean"
            )
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM resume_run_retry_commands WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous is not None:
                if (
                    previous["run_id"] != run_id
                    or previous["requested_by"] != actor_kind
                    or bool(previous["reconciliation_acknowledged"])
                    != reconciliation_acknowledged
                ):
                    raise ResumeConflictError(
                        "retry idempotency key belongs to a different command"
                    )
                result_run_id = str(previous["result_run_id"] or run_id)
                replayed = connection.execute(
                    "SELECT * FROM resume_runs WHERE run_id=?", (result_run_id,)
                ).fetchone()
                if replayed is None:
                    raise ResumeNotFoundError("resume run was not found")
                return {**self._run(connection, replayed), "retry_replayed": True}
            run = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ResumeNotFoundError("resume run was not found")
            item_errors = [
                str(row["error"] or "")
                for row in connection.execute(
                    "SELECT error FROM resume_run_items WHERE run_id=?",
                    (run_id,),
                ).fetchall()
            ]
            reconciliation_required = requires_runpod_reconciliation(
                run["error"]
            ) or any(requires_runpod_reconciliation(value) for value in item_errors)
            if reconciliation_required and not reconciliation_acknowledged:
                raise ResumeConflictError(
                    "inspect the accepted Runpod job before acknowledging this retry"
                )
            if reconciliation_acknowledged and not reconciliation_required:
                raise ResumeConflictError(
                    "reconciliation acknowledgment is valid only for an ambiguous Runpod job"
                )
            stamp = _now()
            if run["status"] == ResumeRunStatus.FAILED.value:
                result_run_id = _new_id("run_")
                retry_request_fingerprint = content_sha256(
                    {
                        "retry_of_run_id": run_id,
                        "source_request_fingerprint": run["request_fingerprint"],
                        "retry_command_idempotency_key": idempotency_key,
                    }
                )
                internal_idempotency_key = "retry_" + content_sha256(
                    {
                        "source_run_id": run_id,
                        "retry_command_idempotency_key": idempotency_key,
                    }
                )
                connection.execute(
                    "INSERT INTO resume_runs "
                    "(run_id,application_id,ats,job_id,job_snapshot_json,job_fingerprint,"
                    "selected_standard_id,base_version_id,base_fingerprint,request_fingerprint,"
                    "idempotency_key,status,attempt,error,result_fingerprint,created_at,started_at,"
                    "completed_at,updated_at,source_mode,composition_id,run_role,parent_run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',0,'',NULL,?,NULL,NULL,?,?,?,?,?)",
                    (
                        result_run_id,
                        run["application_id"],
                        run["ats"],
                        run["job_id"],
                        run["job_snapshot_json"],
                        run["job_fingerprint"],
                        run["selected_standard_id"],
                        run["base_version_id"],
                        run["base_fingerprint"],
                        retry_request_fingerprint,
                        internal_idempotency_key,
                        stamp,
                        stamp,
                        run["source_mode"], run["composition_id"], run["run_role"], run["parent_run_id"],
                    ),
                )
                connection.execute(
                    "INSERT INTO resume_run_order(run_id) VALUES (?)",
                    (result_run_id,),
                )
                analysis = connection.execute(
                    "SELECT requirement_graph_fingerprint,extraction_source,clauses_json "
                    "FROM resume_run_analyses WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if analysis is None:
                    raise ResumeConflictError(
                        "failed resume run has no frozen requirement analysis"
                    )
                connection.execute(
                    "INSERT INTO resume_run_analyses "
                    "(run_id,requirement_graph_fingerprint,extraction_source,clauses_json,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        result_run_id,
                        analysis["requirement_graph_fingerprint"],
                        analysis["extraction_source"],
                        analysis["clauses_json"],
                        stamp,
                    ),
                )
                ranking = connection.execute(
                    "SELECT rankings_json,rankings_sha256 FROM resume_run_ranking_snapshots "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if ranking is not None:
                    connection.execute(
                        "INSERT INTO resume_run_ranking_snapshots "
                        "(run_id,rankings_json,rankings_sha256,created_at) VALUES (?,?,?,?)",
                        (
                            result_run_id,
                            ranking["rankings_json"],
                            ranking["rankings_sha256"],
                            stamp,
                        ),
                    )
                retry_kinds = (RUN_VARIANTS if run["source_mode"] == "standard" else RUN_VARIANTS[1:] if run["run_role"] == "research" else (VariantKind.GROUNDED_REWRITE,))
                for kind in retry_kinds:
                    purpose = (
                        ResumePurpose.REAL_APPLICATION
                        if kind is VariantKind.GROUNDED_REWRITE
                        else ResumePurpose.SYNTHETIC_RESEARCH
                    )
                    item_base = (
                        run["base_version_id"]
                        if kind
                        in {
                            VariantKind.GROUNDED_REWRITE,
                            VariantKind.STANDARD_EXAGGERATED,
                        }
                        else None
                    )
                    item_fingerprint = content_sha256(
                        {
                            "request_fingerprint": retry_request_fingerprint,
                            "variant_kind": kind,
                            "purpose": purpose,
                            "base_version_id": item_base,
                        }
                    )
                    connection.execute(
                        "INSERT INTO resume_run_items "
                        "(run_item_id,run_id,variant_kind,purpose,base_version_id,"
                        "input_fingerprint,status,artifact_id,output_fingerprint,error,"
                        "attempts,completed_at) "
                        "VALUES (?,?,?,?,?,?,'pending',NULL,NULL,'',0,NULL)",
                        (
                            _new_id("item_"),
                            result_run_id,
                            kind.value,
                            purpose.value,
                            item_base,
                            item_fingerprint,
                        ),
                    )
            elif run["status"] not in {
                ResumeRunStatus.QUEUED.value,
                ResumeRunStatus.RUNNING.value,
            }:
                raise ResumeConflictError(
                    "only a failed, queued, or running resume run can be retried"
                )
            else:
                result_run_id = run_id
            connection.execute(
                "INSERT INTO resume_run_retry_commands "
                "(idempotency_key,run_id,result_run_id,run_attempt,requested_by,"
                "reconciliation_acknowledged,requested_at) VALUES (?,?,?,?,?,?,?)",
                (
                    idempotency_key,
                    run_id,
                    result_run_id,
                    int(run["attempt"]),
                    actor_kind,
                    int(reconciliation_acknowledged),
                    stamp,
                ),
            )
            saved = connection.execute(
                "SELECT * FROM resume_runs WHERE run_id=?", (result_run_id,)
            ).fetchone()
            return {**self._run(connection, saved), "retry_replayed": False}


__all__ = ["ResumeLabStore", "connect"]
