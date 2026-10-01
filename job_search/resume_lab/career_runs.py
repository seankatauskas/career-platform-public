"""Persisted career-source composition and run contracts.

Source discrimination is stored in columns, never inferred from model metadata.
The ordinary resume worker, ownership fences, approvals, and selection ledger remain
the authority for both source types.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import (
    JobSnapshot, ResumeBoundaryError, ResumeConflictError, ResumeNotFoundError,
    VariantKind, canonical_json, content_sha256, validate_identifier, validate_sha256,
)

CAREER_GROUNDING_REVISION = "career-grounding-v1"
CAREER_SCHEMA = """
CREATE TABLE IF NOT EXISTS career_compositions (
    composition_id TEXT PRIMARY KEY,
    profile_revision_id TEXT NOT NULL REFERENCES career_profile_revisions(revision_id),
    profile_sha256 TEXT NOT NULL,
    job_fingerprint TEXT NOT NULL,
    template_version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS career_compositions_no_update
BEFORE UPDATE ON career_compositions BEGIN
 SELECT RAISE(ABORT, 'career compositions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS career_compositions_no_delete
BEFORE DELETE ON career_compositions BEGIN
 SELECT RAISE(ABORT, 'career compositions are immutable'); END;
CREATE TABLE IF NOT EXISTS career_import_jobs (
    import_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    source_bytes BLOB NOT NULL,
    expected_revision_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    result_json TEXT,
    error TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    owner_token TEXT,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS career_import_sources_immutable
BEFORE UPDATE ON career_import_jobs
WHEN OLD.import_id IS NOT NEW.import_id OR OLD.idempotency_key IS NOT NEW.idempotency_key
 OR OLD.request_sha256 IS NOT NEW.request_sha256 OR OLD.filename IS NOT NEW.filename
 OR OLD.content_type IS NOT NEW.content_type OR OLD.source_bytes IS NOT NEW.source_bytes
 OR OLD.expected_revision_id IS NOT NEW.expected_revision_id
BEGIN SELECT RAISE(ABORT, 'career import sources are immutable'); END;
"""


def execute_schema(connection: sqlite3.Connection, source: str) -> None:
    """Execute DDL without executescript's implicit pre-transaction commit."""
    pending = ""
    for line in source.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():
        raise RuntimeError("incomplete resume migration")


def migrate_sources(connection: sqlite3.Connection) -> None:
    """Called under one IMMEDIATE transaction with foreign keys temporarily off."""
    execute_schema(connection, CAREER_SCHEMA)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "resume_runs" in tables:
        columns = {r[1]: r for r in connection.execute("PRAGMA table_info(resume_runs)")}
        if columns["selected_standard_id"][3]:
            # Rebuild only the parent table, preserving rowids and every child FK.
            # Temporarily remove triggers so SQLite cannot resolve a half-renamed
            # parent in another table's trigger during ALTER TABLE.
            triggers = list(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
            for name, _ in triggers:
                connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            old_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='resume_runs'").fetchone()[0]
            new_sql = old_sql.replace("resume_runs", "resume_runs_new", 1)
            new_sql = new_sql.replace("selected_standard_id TEXT NOT NULL", "selected_standard_id TEXT")
            new_sql = new_sql.replace("base_version_id     TEXT NOT NULL", "base_version_id     TEXT")
            connection.execute(new_sql)
            names = ",".join('"' + name + '"' for name in columns)
            connection.execute(f"INSERT INTO resume_runs_new(rowid,{names}) SELECT rowid,{names} FROM resume_runs")
            connection.execute("DROP TABLE resume_runs")
            connection.execute("ALTER TABLE resume_runs_new RENAME TO resume_runs")
            for _, sql in triggers:
                connection.execute(sql)
        for name, declaration in (
            ("source_mode", "TEXT NOT NULL DEFAULT 'standard' CHECK(source_mode IN ('standard','career_profile'))"),
            ("composition_id", "TEXT REFERENCES career_compositions(composition_id)"),
            ("run_role", "TEXT NOT NULL DEFAULT 'primary' CHECK(run_role IN ('primary','research'))"),
            ("parent_run_id", "TEXT REFERENCES resume_runs(run_id)"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE resume_runs ADD COLUMN {name} {declaration}")
    if "resume_artifacts" in tables:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(resume_artifacts)")}
        for name, declaration in (
            ("source_mode", "TEXT NOT NULL DEFAULT 'standard' CHECK(source_mode IN ('standard','career_profile'))"),
            ("composition_id", "TEXT REFERENCES career_compositions(composition_id)"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE resume_artifacts ADD COLUMN {name} {declaration}")


SOURCE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS resume_runs_source_union
BEFORE INSERT ON resume_runs WHEN NOT (
 (NEW.source_mode='standard' AND NEW.selected_standard_id IS NOT NULL AND NEW.base_version_id IS NOT NULL AND NEW.composition_id IS NULL AND NEW.run_role='primary' AND NEW.parent_run_id IS NULL)
 OR (NEW.source_mode='career_profile' AND NEW.selected_standard_id IS NULL AND NEW.base_version_id IS NULL AND NEW.composition_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM career_compositions c WHERE c.composition_id=NEW.composition_id AND c.job_fingerprint=NEW.job_fingerprint)
 AND ((NEW.run_role='primary' AND NEW.parent_run_id IS NULL) OR (NEW.run_role='research' AND EXISTS(SELECT 1 FROM resume_runs p WHERE p.run_id=NEW.parent_run_id AND p.run_role='primary' AND p.source_mode='career_profile' AND p.composition_id=NEW.composition_id AND p.application_id=NEW.application_id))))
) BEGIN SELECT RAISE(ABORT, 'invalid resume run source'); END;
CREATE TRIGGER IF NOT EXISTS resume_runs_source_immutable
BEFORE UPDATE ON resume_runs WHEN OLD.source_mode IS NOT NEW.source_mode OR OLD.composition_id IS NOT NEW.composition_id OR OLD.run_role IS NOT NEW.run_role OR OLD.parent_run_id IS NOT NEW.parent_run_id
BEGIN SELECT RAISE(ABORT, 'resume run source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS resume_artifacts_source_union
BEFORE INSERT ON resume_artifacts WHEN NOT (
 (NEW.source_mode='standard' AND NEW.composition_id IS NULL)
 OR (NEW.source_mode='career_profile' AND NEW.base_version_id IS NULL AND NEW.variant_kind!='standard' AND NEW.composition_id IS NOT NULL
 AND EXISTS(SELECT 1 FROM career_compositions c WHERE c.composition_id=NEW.composition_id AND c.job_fingerprint=NEW.job_fingerprint))
) BEGIN SELECT RAISE(ABORT, 'invalid resume artifact source'); END;
"""


def get_composition(connection: sqlite3.Connection, composition_id: str) -> Mapping[str, Any]:
    row = connection.execute("SELECT * FROM career_compositions WHERE composition_id=?", (composition_id,)).fetchone()
    if row is None:
        raise ResumeNotFoundError("career composition was not found")
    value = dict(row)
    value["snapshot"] = json.loads(value.pop("snapshot_json"))
    if (content_sha256(value["snapshot"]) != value["content_sha256"]
            or value["composition_id"] != "composition_" + value["content_sha256"]):
        raise ResumeConflictError("career composition identity changed")
    snapshot = value["snapshot"]
    if not isinstance(snapshot, Mapping) or any(value[key] != snapshot.get(key) for key in
            ("profile_revision_id", "profile_sha256", "job_fingerprint", "template_version")):
        raise ResumeBoundaryError("career composition columns differ from its immutable snapshot")
    _validate_composition_snapshot(connection, snapshot)
    return value


def _validate_composition_snapshot(connection: sqlite3.Connection, snapshot: Mapping[str, Any]) -> None:
    """Revalidate persisted source authority independently of the creating caller."""
    from .career_composition import source_rows
    from .tex import TEMPLATE_VERSIONS
    required = {"profile_revision_id", "profile_sha256", "profile_content", "job_fingerprint",
        "template_version", "pinned_fact_ids", "excluded_fact_ids", "selected", "omitted",
        "page_target", "selection_revision"}
    if (not isinstance(snapshot, Mapping) or not required <= set(snapshot)
            or set(snapshot) - required - {"model_provenance"}):
        raise ResumeBoundaryError("career composition snapshot has an invalid shape")
    validate_identifier(snapshot["profile_revision_id"], "profile_revision_id")
    for field in ("profile_sha256", "job_fingerprint"):
        validate_sha256(snapshot[field], field)
    if (snapshot["template_version"] not in TEMPLATE_VERSIONS
            or type(snapshot["page_target"]) is not int or snapshot["page_target"] != 1
            or snapshot["selection_revision"] != "career-selection-v1"):
        raise ResumeBoundaryError("career composition format or selection revision is invalid")
    if "model_provenance" in snapshot and (not isinstance(snapshot["model_provenance"], Mapping)
            or len(canonical_json(snapshot["model_provenance"]).encode()) > 32_000):
        raise ResumeBoundaryError("career composition model provenance is invalid")
    revision = connection.execute(
        "SELECT r.content_json,r.content_sha256 FROM career_profile_revisions r "
        "JOIN career_profile_approvals a ON a.revision_id=r.revision_id AND a.actor='user' "
        "WHERE r.revision_id=?", (snapshot["profile_revision_id"],)).fetchone()
    if revision is None:
        raise ResumeBoundaryError("composition requires an approved immutable career revision")
    content = json.loads(revision["content_json"])
    if (revision["content_sha256"] != snapshot["profile_sha256"]
            or content_sha256(content) != revision["content_sha256"]
            or canonical_json(content) != canonical_json(snapshot["profile_content"])):
        raise ResumeBoundaryError("composition source differs from the approved career revision")
    source = {r["fact_id"]: r for r in source_rows(content)}
    choices = {}
    for field in ("pinned_fact_ids", "excluded_fact_ids"):
        ids = snapshot[field]
        if (not isinstance(ids, list) or len(ids) > 2000 or any(not isinstance(fid, str) for fid in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= set(source)):
            raise ResumeBoundaryError("career composition choices reference invalid source IDs")
        choices[field] = set(ids)
    pins, exclusions = choices["pinned_fact_ids"], choices["excluded_fact_ids"]
    pinned_entries = {fid for fid in pins if source[fid]["kind"] == "entry"}
    excluded_entries = {fid for fid in exclusions if source[fid]["kind"] == "entry"}
    pins |= {fid for fid, row in source.items() if row["entry_id"] in pinned_entries}
    exclusions |= {fid for fid, row in source.items() if row["entry_id"] in excluded_entries}
    if pins & exclusions:
        raise ResumeBoundaryError("career composition has conflicting pin and exclusion choices")
    seen = set()
    partitions = {}
    for field in ("selected", "omitted"):
        rows = snapshot[field]
        if not isinstance(rows, list) or len(rows) > 2000:
            raise ResumeBoundaryError("career composition facts must be a bounded array")
        partitions[field] = set()
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("fact_id"), str):
                raise ResumeBoundaryError("career composition fact is invalid")
            fid = row["fact_id"]
            if fid not in source or fid in seen or any(row.get(k) != v for k, v in source[fid].items()):
                raise ResumeBoundaryError("composition facts differ from the approved career bank")
            expected = set(source[fid]) | {"score", "pinned", "reason", "matched_requirements"}
            score = row.get("score")
            matches = row.get("matched_requirements")
            if (set(row) != expected or isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score) or not 0 <= score <= 1_000_000
                    or type(row.get("pinned")) is not bool or row["pinned"] != (fid in pins)
                    or not isinstance(row.get("reason"), str) or len(row["reason"]) > 4000
                    or not isinstance(matches, list) or len(matches) > 6
                    or any(not isinstance(m, str) or len(m) > 20_000 for m in matches)):
                raise ResumeBoundaryError("career composition selection metadata is invalid")
            seen.add(fid)
            partitions[field].add(fid)
    selected = partitions["selected"]
    if seen != set(source):
        raise ResumeBoundaryError("composition must account for every eligible career fact")
    if (not selected or not pins <= selected or selected & exclusions
            or any(source[fid]["entry_id"] not in selected for fid in selected)):
        raise ResumeBoundaryError("career composition selection violates its source associations or choices")


def create_career_run(store: Any, application_id: str, job: JobSnapshot,
                      composition: Mapping[str, Any], idempotency_key: str,
                      *, parent_run_id: str | None = None) -> Mapping[str, Any]:
    from .store import connect, _now, _new_id
    validate_identifier(application_id, "application_id")
    validate_identifier(idempotency_key, "idempotency_key")
    job.validate()
    role = "research" if parent_run_id else "primary"
    fingerprint = content_sha256({"application_id": application_id, "job": job,
        "composition_id": composition["composition_id"], "role": role, "parent": parent_run_id})
    with connect(store.db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        previous = con.execute("SELECT * FROM resume_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if previous:
            if previous["request_fingerprint"] != fingerprint:
                raise ResumeConflictError("resume request key belongs to different content")
            return store._run(con, previous)
        persisted = get_composition(con, str(composition["composition_id"]))
        if persisted["job_fingerprint"] != job.fingerprint:
            raise ResumeBoundaryError("career composition belongs to another job")
        if parent_run_id is not None:
            validate_identifier(parent_run_id, "parent_run_id")
            parent = con.execute("SELECT * FROM resume_runs WHERE run_id=?", (parent_run_id,)).fetchone()
            if (parent is None or parent["status"] != "succeeded" or parent["source_mode"] != "career_profile"
                    or parent["run_role"] != "primary" or parent["composition_id"] != persisted["composition_id"]
                    or parent["application_id"] != application_id or parent["job_fingerprint"] != job.fingerprint):
                raise ResumeBoundaryError("research requires a succeeded factual run for the same application and composition")
        run_id, stamp = _new_id("run_"), _now()
        con.execute("INSERT INTO resume_runs (run_id,application_id,ats,job_id,job_snapshot_json,job_fingerprint,selected_standard_id,base_version_id,base_fingerprint,request_fingerprint,idempotency_key,status,attempt,error,created_at,updated_at,source_mode,composition_id,run_role,parent_run_id) VALUES (?,?,?,?,?,?,NULL,NULL,?,?,?,'queued',0,'',?,?,'career_profile',?,?,?)",
            (run_id,application_id,job.ats,job.job_id,canonical_json(job),job.fingerprint,persisted["content_sha256"],fingerprint,idempotency_key,stamp,stamp,persisted["composition_id"],role,parent_run_id))
        con.execute("INSERT INTO resume_run_order(run_id) VALUES (?)", (run_id,))
        from .requirements import extract_requirement_graph
        graph = extract_requirement_graph(job)
        con.execute("INSERT INTO resume_run_analyses(run_id,requirement_graph_fingerprint,extraction_source,clauses_json,created_at) VALUES (?,?,'deterministic_fallback','[]',?)", (run_id,graph.fingerprint,stamp))
        kinds = (VariantKind.STANDARD_EXAGGERATED,VariantKind.MARKET_IDEAL,VariantKind.KEYWORD_ADVERSARIAL) if parent_run_id else (VariantKind.GROUNDED_REWRITE,)
        for kind in kinds:
            purpose = "synthetic_research" if parent_run_id else "real_application"
            con.execute("INSERT INTO resume_run_items(run_item_id,run_id,variant_kind,purpose,base_version_id,input_fingerprint,status,error,attempts) VALUES (?,?,?,?,NULL,?,'pending','',0)",
                (_new_id("item_"),run_id,kind.value,purpose,content_sha256({"request":fingerprint,"kind":kind})))
        return store._run(con, con.execute("SELECT * FROM resume_runs WHERE run_id=?", (run_id,)).fetchone())


def save_composition(store: Any, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    from .store import connect, _now
    from .career_store import CareerStore
    CareerStore(store.db_path)
    digest = content_sha256(snapshot)
    composition_id = "composition_" + digest
    with connect(store.db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        _validate_composition_snapshot(con, snapshot)
        con.execute("INSERT OR IGNORE INTO career_compositions VALUES (?,?,?,?,?,?,?,?)",
            (composition_id,snapshot["profile_revision_id"],snapshot["profile_sha256"],snapshot["job_fingerprint"],snapshot["template_version"],canonical_json(snapshot),digest,_now()))
        return get_composition(con, composition_id)


def artifact_grounding_current(artifact: Mapping[str, Any], metadata: Mapping[str, Any]) -> bool:
    from .grounding import GROUNDING_VALIDATOR_REVISION, GROUNDING_EQUIVALENCE_REVISION
    if artifact["source_mode"] == "career_profile":
        return bool(artifact["composition_id"] and metadata.get("grounding_validator_revision") == CAREER_GROUNDING_REVISION)
    return (metadata.get("grounding_validator_revision") == GROUNDING_VALIDATOR_REVISION
            and metadata.get("grounding_equivalence_revision") == GROUNDING_EQUIVALENCE_REVISION)
