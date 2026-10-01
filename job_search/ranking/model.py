#!/usr/bin/env python3
"""Leakage-aware preference modeling for deduplicated job postings.

Modeling opens the operational job database read-only. ``refresh`` first updates only
the regenerable family tables through :mod:`job_dedupe`; embeddings, model manifests,
and scores live in a separate sidecar database and an ignored artifact directory.

Typical usage::

    python3 -m job_search.ranking.model --db job-boards.db embed
    python3 -m job_search.ranking.model --db job-boards.db train
    python3 -m job_search.ranking.model --db job-boards.db score
    python3 -m job_search.ranking.model --db job-boards.db refresh

The default encoder is Sentence Transformers in strict offline mode. An explicitly
selected owner-only inference profile can instead run the exact recorded embedding
revision through an OpenAI-compatible endpoint. Providers and revisions never change
implicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import math
import os
import platform
import pickle
import re
import shlex
import sqlite3
import struct
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence
from urllib.parse import quote

from job_search.inference import (
    EmbeddingProvider,
    InferenceConfigError,
    InferenceTransportError,
    build_embedding_provider,
    configured_inference_path,
    load_inference_config,
)


TEXT_VERSION = "preference-text-v1"
DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"
DEFAULT_SEED = 1729
MAX_DESCRIPTION_CHUNKS = 4
MAX_CHUNK_TOKENS = 384
MODEL_FORMAT_VERSION = 1


class PreferenceModelError(RuntimeError):
    """A user-actionable pipeline error."""


class DependencyError(PreferenceModelError):
    """An optional local-model dependency is unavailable."""


@dataclass(frozen=True)
class FeatureDocument:
    """Only text that is allowed to influence intrinsic-interest predictions."""

    subject_type: str
    subject_id: str
    family_id: str
    title_metadata_text: str
    description_text: str
    description_chunks: tuple[str, ...]
    template_cluster_id: str
    leakage_group_id: str

    @property
    def fingerprint(self) -> str:
        payload = {
            "text_version": TEXT_VERSION,
            "title_metadata": self.title_metadata_text,
            "description_chunks": self.description_chunks,
        }
        return sha256_text(canonical_json(payload))

    @property
    def word_text(self) -> str:
        return "\n".join(
            value for value in (self.title_metadata_text, self.description_text) if value
        )

    @property
    def char_text(self) -> str:
        return self.title_metadata_text


@dataclass(frozen=True)
class TrainingExample:
    example_id: str
    document: FeatureDocument
    target: int
    dataset_version: str = ""
    source_fingerprint: str = ""
    selection_strategy: str = ""
    selection_probability: Optional[float] = None
    sample_weight: float = 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_SPACE = re.compile(r"\s+")
_PARAGRAPH = re.compile(r"(?:\r?\n\s*){2,}")


def clean_text(value: Any) -> str:
    return _SPACE.sub(" ", value if isinstance(value, str) else "").strip()


def title_metadata_text(row: dict[str, Any]) -> str:
    """Build the high-signal title arm from an explicit field allowlist.

    Company, salary, location, ATS, employment constraints, and every unknown field
    are deliberately inaccessible to the feature builder.
    """
    pieces = []
    for label, key in (("title", "title"), ("department", "department"), ("team", "team")):
        value = clean_text(row.get(key))
        if value:
            pieces.append(f"{label}: {value}")
    return "\n".join(pieces)


def _bounded_paragraphs(description: str, max_tokens: int) -> list[str]:
    paragraphs = [clean_text(p) for p in _PARAGRAPH.split(description) if clean_text(p)]
    if not paragraphs and clean_text(description):
        paragraphs = [clean_text(description)]
    bounded: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        for start in range(0, len(words), max_tokens):
            chunk = " ".join(words[start:start + max_tokens])
            if chunk:
                bounded.append(chunk)
    return bounded


def description_chunks(
    description: Any,
    max_chunks: int = MAX_DESCRIPTION_CHUNKS,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> tuple[str, ...]:
    """Return bounded word windows sampled across the entire stored posting."""
    if max_chunks < 1 or max_tokens < 1:
        raise ValueError("chunk limits must be positive")
    chunks = _bounded_paragraphs(description if isinstance(description, str) else "", max_tokens)
    if len(chunks) <= max_chunks:
        return tuple(chunks)
    if max_chunks == 1:
        return (chunks[0],)
    # Preserve the opening, closing, and evenly spaced middle evidence.
    indexes = [round(i * (len(chunks) - 1) / (max_chunks - 1)) for i in range(max_chunks)]
    return tuple(chunks[index] for index in indexes)


def build_feature_document(
    row: dict[str, Any],
    subject_type: str = "family",
    subject_id: Optional[str] = None,
) -> FeatureDocument:
    family_id = str(row.get("family_id") or "").strip()
    if not family_id:
        raise PreferenceModelError("feature row is missing family_id")
    description = row.get("description")
    description = description if isinstance(description, str) else ""
    entity_id = str(subject_id or row.get("subject_id") or family_id)
    return FeatureDocument(
        subject_type=subject_type,
        subject_id=entity_id,
        family_id=family_id,
        title_metadata_text=title_metadata_text(row),
        description_text=clean_text(description),
        description_chunks=description_chunks(description),
        template_cluster_id=str(row.get("template_cluster_id") or family_id),
        leakage_group_id=str(row.get("leakage_group_id") or family_id),
    )


def default_state_db(source_db: Path) -> Path:
    return source_db.with_name(f"{source_db.stem}-preference.db")


def default_artifact_dir(source_db: Path) -> Path:
    return source_db.parent / ".models" / "preference"


def connect_source(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise PreferenceModelError(f"source database does not exist: {db_path}")
    uri = "file:" + quote(str(db_path.resolve())) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def connect_state(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    return con


STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS preference_feature_documents (
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    feature_fingerprint TEXT NOT NULL,
    title_metadata_text TEXT NOT NULL,
    description_text TEXT NOT NULL,
    description_chunks_json TEXT NOT NULL,
    template_cluster_id TEXT NOT NULL,
    leakage_group_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject_type, subject_id)
);
CREATE TABLE IF NOT EXISTS preference_embedding_cache (
    model_revision TEXT NOT NULL,
    text_version TEXT NOT NULL,
    text_fingerprint TEXT NOT NULL,
    vector_blob BLOB NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (model_revision, text_version, text_fingerprint)
);
CREATE TABLE IF NOT EXISTS preference_embedding_refs (
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    text_version TEXT NOT NULL,
    feature_fingerprint TEXT NOT NULL,
    title_fingerprint TEXT NOT NULL,
    chunk_fingerprints_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject_type, subject_id, model_revision, text_version)
);
CREATE TABLE IF NOT EXISTS preference_embedding_provenance (
    model_revision TEXT NOT NULL,
    provenance_fingerprint TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    first_used_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (model_revision, provenance_fingerprint)
);
CREATE TABLE IF NOT EXISTS preference_model_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    text_version TEXT NOT NULL,
    training_examples INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    artifact_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_scores (
    run_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    feature_fingerprint TEXT NOT NULL DEFAULT '',
    dense_linear_score REAL NOT NULL,
    dense_neighbor_score REAL NOT NULL,
    sparse_score REAL NOT NULL,
    final_score REAL NOT NULL,
    explanation_json TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (run_id, family_id),
    FOREIGN KEY (run_id) REFERENCES preference_model_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS preference_scores_rank
    ON preference_scores(run_id, final_score DESC, family_id);
CREATE TABLE IF NOT EXISTS preference_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_champion_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    previous_run_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    forced INTEGER NOT NULL DEFAULT 0,
    projected_incremental_minutes REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_protected_acceptances (
    protected_set_fingerprint TEXT PRIMARY KEY,
    consumed_at TEXT NOT NULL,
    candidate_run_id TEXT NOT NULL,
    previous_run_id TEXT NOT NULL,
    acceptance_slice TEXT NOT NULL
        CHECK (acceptance_slice = 'protected_top_ranked')
);
"""


def prepare_state(db_path: Path) -> None:
    with connect_state(db_path) as con:
        con.executescript(STATE_SCHEMA)
        _migrate_json_embedding_cache(con)
        score_columns = {
            str(row[1]) for row in con.execute("PRAGMA table_info(preference_scores)")
        }
        if "feature_fingerprint" not in score_columns:
            con.execute(
                "ALTER TABLE preference_scores ADD COLUMN feature_fingerprint "
                "TEXT NOT NULL DEFAULT ''"
            )
        # Early development sidecars copied source text here. Keep the additive
        # columns for migration compatibility, but scrub every row permanently.
        con.execute(
            "UPDATE preference_feature_documents SET title_metadata_text='',"
            "description_text='',description_chunks_json='[]' WHERE "
            "title_metadata_text<>'' OR description_text<>'' OR "
            "description_chunks_json<>'[]'"
        )


def _migrate_json_embedding_cache(con: sqlite3.Connection) -> None:
    """Upgrade short-lived development sidecars without retaining JSON vectors."""
    columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(preference_embedding_cache)")
    }
    if "vector_blob" in columns:
        return
    if "vector_json" not in columns:
        raise PreferenceModelError("embedding cache has an unsupported schema")
    con.execute("ALTER TABLE preference_embedding_cache RENAME TO preference_embedding_cache_json")
    con.execute("""
        CREATE TABLE preference_embedding_cache (
            model_revision TEXT NOT NULL,
            text_version TEXT NOT NULL,
            text_fingerprint TEXT NOT NULL,
            vector_blob BLOB NOT NULL,
            dimensions INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (model_revision, text_version, text_fingerprint)
        )
    """)
    cursor = con.execute(
        "SELECT model_revision,text_version,text_fingerprint,vector_json,"
        "dimensions,created_at FROM preference_embedding_cache_json"
    )
    while True:
        rows = cursor.fetchmany(256)
        if not rows:
            break
        converted = []
        for row in rows:
            try:
                vector = json.loads(row["vector_json"])
                blob = encode_vector_blob(vector)
            except (TypeError, ValueError, struct.error) as exc:
                raise PreferenceModelError(
                    f"could not migrate embedding {row['text_fingerprint']} from JSON"
                ) from exc
            if len(vector) != int(row["dimensions"]):
                raise PreferenceModelError(
                    f"embedding {row['text_fingerprint']} has inconsistent dimensions"
                )
            converted.append((
                row["model_revision"], row["text_version"], row["text_fingerprint"],
                sqlite3.Binary(blob), len(vector), row["created_at"],
            ))
        con.executemany(
            "INSERT INTO preference_embedding_cache VALUES (?,?,?,?,?,?)", converted,
        )
    con.execute("DROP TABLE preference_embedding_cache_json")


def _source_tables(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


def validate_family_schema(con: sqlite3.Connection, *, require_complete: bool = True) -> None:
    tables = _source_tables(con)
    missing = {"jobs", "job_families", "job_family_members"} - tables
    if missing:
        names = ", ".join(sorted(missing))
        raise PreferenceModelError(
            f"source database is missing {names}; run `python3 -m job_search.collection.dedupe --db ... prepare` first"
        )
    if not require_complete:
        return
    unmatched = con.execute(
        "SELECT COUNT(*) FROM jobs j LEFT JOIN job_family_members m "
        "ON m.ats=j.ats AND m.job_id=j.id WHERE m.family_id IS NULL"
    ).fetchone()[0]
    if unmatched:
        raise PreferenceModelError(
            f"{unmatched} jobs have no current opportunity family; run job_dedupe prepare or refresh"
        )


def iter_family_document_batches(
    source_db: Path, batch_size: int = 1000,
    *, connection: Optional[sqlite3.Connection] = None,
    family_ids: Optional[Sequence[str]] = None,
) -> Iterator[list[FeatureDocument]]:
    """Stream canonical families without materializing the description corpus."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    con = connection if connection is not None else connect_source(source_db)
    try:
        if not con.in_transaction:
            con.execute("BEGIN")
        validate_family_schema(con, require_complete=family_ids is None)
        if family_ids is not None and not family_ids:
            return
        has_templates = "job_template_clusters" in _source_tables(con)
        template_join = (
            "LEFT JOIN job_template_clusters tc ON tc.family_id=f.family_id"
            if has_templates else ""
        )
        template_columns = (
            "COALESCE(tc.template_cluster_id,f.family_id) AS template_cluster_id, "
            "COALESCE(tc.leakage_group_id,f.family_id) AS leakage_group_id"
            if has_templates else
            "f.family_id AS template_cluster_id, f.family_id AS leakage_group_id"
        )
        cursor = con.execute(
            "SELECT f.family_id, j.title, j.department, j.team, j.description, "
            + template_columns
            + " FROM job_families f "
            "JOIN jobs j ON j.ats=f.canonical_ats AND j.id=f.canonical_job_id "
            + template_join
            + (" WHERE f.family_id IN (" + ",".join("?" for _ in family_ids) + ")" if family_ids is not None else "")
            + " ORDER BY f.family_id",
            tuple(family_ids) if family_ids is not None else (),
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield [build_feature_document(dict(row)) for row in rows]
    finally:
        if connection is None:
            con.close()


def load_family_documents(source_db: Path) -> list[FeatureDocument]:
    return [
        document
        for batch in iter_family_document_batches(source_db)
        for document in batch
    ]


def _parse_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _template_lineage_aliases(con: sqlite3.Connection) -> dict[str, str]:
    """Resolve persisted template-cluster aliases to their stable lineage roots."""
    if "job_template_cluster_lineage" not in _source_tables(con):
        return {}
    aliases = {
        str(row[0]): str(row[1])
        for row in con.execute(
            "SELECT template_cluster_id,lineage_id FROM job_template_cluster_lineage"
        )
    }
    for alias in list(aliases):
        current = alias
        seen: set[str] = set()
        while aliases.get(current, current) != current:
            if current in seen:
                raise PreferenceModelError("template lineage contains a cycle")
            seen.add(current)
            current = aliases[current]
        aliases[alias] = current
    return aliases


def load_snapshot_documents(source_db: Path) -> list[FeatureDocument]:
    """Load immutable label-time text when the family-aware labeler provides it."""
    with connect_source(source_db) as con:
        if "preference_examples" not in _source_tables(con):
            return []
        template_lineages = _template_lineage_aliases(con)
        rows = con.execute(
            "SELECT e.example_id, "
            "COALESCE(NULLIF(e.family_id,''),m.family_id) AS family_id, "
            "e.title_snapshot,e.description_snapshot,e.metadata_json, "
            "COALESCE(NULLIF(e.template_cluster_id,''),tc.template_cluster_id,m.family_id) "
            "AS template_cluster_id, "
            "COALESCE(NULLIF(e.leakage_group_id,''),tc.leakage_group_id,m.family_id) "
            "AS leakage_group_id "
            "FROM preference_examples e "
            "LEFT JOIN job_family_members m ON m.ats=e.ats AND m.job_id=e.job_id "
            "LEFT JOIN job_template_clusters tc ON tc.family_id=m.family_id "
            "ORDER BY e.example_id"
        ).fetchall()
    documents = []
    for raw in rows:
        row = dict(raw)
        metadata = _parse_metadata(row.get("metadata_json"))
        if not row.get("family_id"):
            continue
        documents.append(build_feature_document({
            "family_id": row["family_id"],
            "title": row.get("title_snapshot") or "",
            "department": metadata.get("department") or "",
            "team": metadata.get("team") or "",
            "description": row.get("description_snapshot") or "",
            "template_cluster_id": template_lineages.get(
                str(row.get("template_cluster_id") or ""),
                row.get("template_cluster_id"),
            ),
            "leakage_group_id": row.get("leakage_group_id"),
        }, subject_type="example", subject_id=str(row["example_id"])))
    return documents


def _examples_from_snapshot_view(
    con: sqlite3.Connection, view_name: str,
) -> list[TrainingExample]:
    if view_name not in {"preference_training_examples", "preference_evaluation_examples"}:
        raise ValueError("unknown preference example view")
    template_lineages = _template_lineage_aliases(con)
    rows = con.execute(
        "SELECT e.example_id, "
        "COALESCE(NULLIF(e.family_id,''),m.family_id) AS family_id, "
        "e.interest,e.title_snapshot,e.description_snapshot,e.metadata_json, "
        "e.dataset_version,e.source_fingerprint,e.selection_strategy,"
        "e.selection_probability, "
        "COALESCE(NULLIF(e.template_cluster_id,''),tc.template_cluster_id,m.family_id) "
        "AS template_cluster_id, "
        "COALESCE(NULLIF(e.leakage_group_id,''),tc.leakage_group_id,m.family_id) "
        "AS leakage_group_id "
        f"FROM {view_name} e "
        "LEFT JOIN job_family_members m ON m.ats=e.ats AND m.job_id=e.job_id "
        "LEFT JOIN job_template_clusters tc ON tc.family_id=m.family_id "
        "ORDER BY e.example_id"
    ).fetchall()
    examples = []
    for raw in rows:
        row = dict(raw)
        if row.get("interest") not in {"interested", "not_interested"}:
            continue
        if not row.get("family_id"):
            continue
        metadata = _parse_metadata(row.get("metadata_json"))
        document = build_feature_document({
            "family_id": row["family_id"],
            "title": row.get("title_snapshot") or "",
            "department": metadata.get("department") or "",
            "team": metadata.get("team") or "",
            "description": row.get("description_snapshot") or "",
            "template_cluster_id": template_lineages.get(
                str(row.get("template_cluster_id") or ""),
                row.get("template_cluster_id"),
            ),
            "leakage_group_id": row.get("leakage_group_id"),
        }, subject_type="example", subject_id=str(row["example_id"]))
        examples.append(TrainingExample(
            str(row["example_id"]), document,
            1 if row["interest"] == "interested" else 0,
            str(row.get("dataset_version") or ""),
            str(row.get("source_fingerprint") or ""),
            str(row.get("selection_strategy") or ""),
            (
                float(row["selection_probability"])
                if row.get("selection_probability") is not None else None
            ),
        ))
    return _deduplicate_training_families(examples)


def _training_from_snapshot_view(con: sqlite3.Connection) -> list[TrainingExample]:
    return _examples_from_snapshot_view(con, "preference_training_examples")


def _deduplicate_training_families(examples: Sequence[TrainingExample]) -> list[TrainingExample]:
    """Use one immutable example per family and drop contradictory family labels."""
    by_family: dict[str, list[TrainingExample]] = {}
    for example in examples:
        by_family.setdefault(example.document.family_id, []).append(example)
    result = []
    for family_id in sorted(by_family):
        family_examples = by_family[family_id]
        targets = {example.target for example in family_examples}
        if len(targets) != 1:
            continue
        # IDs are immutable and monotonically sortable in the labeler; choose the
        # latest deterministic snapshot if the same judgment was recorded twice.
        def example_order(item: TrainingExample) -> tuple[int, Any]:
            return (1, int(item.example_id)) if item.example_id.isdigit() else (0, item.example_id)
        result.append(sorted(family_examples, key=example_order)[-1])
    return result


def _training_from_live_labels(con: sqlite3.Connection) -> list[TrainingExample]:
    """Compatibility path for databases created before immutable examples existed."""
    tables = _source_tables(con)
    if "job_preferences" not in tables:
        raise PreferenceModelError(
            "source database has no labels; use job_search/ranking/labeler.py to collect training labels"
        )
    rows = con.execute(
        "SELECT p.ats, p.job_id, p.interest, p.sample_role, m.family_id, "
        "j.title, j.department, j.team, j.description, "
        "COALESCE(tc.template_cluster_id,m.family_id) AS template_cluster_id, "
        "COALESCE(tc.leakage_group_id,m.family_id) AS leakage_group_id "
        "FROM job_preferences p "
        "JOIN job_family_members m ON m.ats=p.ats AND m.job_id=p.job_id "
        "JOIN jobs j ON j.ats=p.ats AND j.id=p.job_id "
        "LEFT JOIN job_template_clusters tc ON tc.family_id=m.family_id "
        "WHERE p.sample_role='training' "
        "AND p.interest IN ('interested','not_interested') "
        "ORDER BY m.family_id,p.updated_at,p.ats,p.job_id"
    ).fetchall()
    examples = []
    for index, raw in enumerate(rows):
        row = dict(raw)
        subject_id = f"legacy-{row['ats']}-{row['job_id']}"
        document = build_feature_document(row, "example", subject_id)
        examples.append(TrainingExample(
            f"{index:09d}-{subject_id}", document,
            1 if row["interest"] == "interested" else 0,
        ))
    return _deduplicate_training_families(examples)


def load_training_examples(source_db: Path) -> list[TrainingExample]:
    with connect_source(source_db) as con:
        validate_family_schema(con)
        if "preference_training_examples" in _source_tables(con):
            return _training_from_snapshot_view(con)
        return _training_from_live_labels(con)


def load_evaluation_examples(source_db: Path) -> list[TrainingExample]:
    with connect_source(source_db) as con:
        validate_family_schema(con)
        if "preference_evaluation_examples" not in _source_tables(con):
            raise PreferenceModelError(
                "source database has no protected evaluation snapshots; finish labeling first"
            )
        return _examples_from_snapshot_view(con, "preference_evaluation_examples")


EVALUATION_SLICE_SIZES = {
    "protected_top_ranked": 100,
    "protected_uniform": 50,
    "protected_company_holdout": 50,
}
PROMOTION_ACCEPTANCE_SLICE = "protected_top_ranked"
POST_SELECTION_AUDIT_SLICES = (
    "protected_uniform",
    "protected_company_holdout",
)


def group_evaluation_slices(
    examples: Sequence[TrainingExample],
) -> dict[str, list[TrainingExample]]:
    grouped = {name: [] for name in EVALUATION_SLICE_SIZES}
    for example in examples:
        if example.selection_strategy not in grouped:
            raise PreferenceModelError(
                f"unknown protected evaluation slice {example.selection_strategy!r}"
            )
        grouped[example.selection_strategy].append(example)
    return grouped


def protected_example_set_fingerprint(report: dict[str, Any]) -> str:
    """Identify a completed protected set independent of evaluation row order."""
    example_ids = report.get("example_ids")
    if not isinstance(example_ids, list) or len(example_ids) != 200:
        raise PreferenceModelError("protected evaluation needs exactly 200 example IDs")
    normalized = [str(example_id) for example_id in example_ids]
    if len(set(normalized)) != 200:
        raise PreferenceModelError("protected evaluation example IDs must be unique")
    return sha256_text(canonical_json(sorted(normalized)))


def text_fingerprint(text: str) -> str:
    return sha256_text(TEXT_VERSION + "\0" + text)


def encode_vector_blob(vector: Sequence[float]) -> bytes:
    """Encode a vector as deterministic little-endian IEEE-754 float32 bytes."""
    values = [float(value) for value in vector]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("embedding vectors must contain only finite numbers")
    return struct.pack(f"<{len(values)}f", *values)


def decode_vector_blob(blob: Any, dimensions: int) -> list[float]:
    """Decode and validate a little-endian float32 embedding without NumPy."""
    try:
        size = int(dimensions)
    except (TypeError, ValueError) as exc:
        raise ValueError("embedding dimensions must be an integer") from exc
    if size < 0:
        raise ValueError("embedding dimensions cannot be negative")
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("embedding vector must be stored as a BLOB")
    raw = bytes(blob)
    expected = size * 4
    if len(raw) != expected:
        raise ValueError(
            f"embedding BLOB is {len(raw)} bytes; expected {expected} for {size} dimensions"
        )
    return list(struct.unpack(f"<{size}f", raw))


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) ** 2 for value in vector))
    if norm == 0:
        return [0.0 for _ in vector]
    return [float(value) / norm for value in vector]


def mean_vectors(vectors: Sequence[Sequence[float]]) -> list[float]:
    if not vectors:
        raise ValueError("cannot average no vectors")
    dimensions = len(vectors[0])
    if any(len(vector) != dimensions for vector in vectors):
        raise ValueError("embedding dimensions disagree")
    return l2_normalize([
        sum(float(vector[index]) for vector in vectors) / len(vectors)
        for index in range(dimensions)
    ])


def combined_vector(title_vector: Sequence[float], chunk_vectors: Sequence[Sequence[float]]) -> list[float]:
    description_vector = (
        mean_vectors(chunk_vectors) if chunk_vectors else [0.0 for _ in title_vector]
    )
    return l2_normalize(list(title_vector) + description_vector)


class HashingEncoder:
    """Deterministic dependency-free encoder for tests and pipeline smoke checks."""

    def __init__(self, dimensions: int = 32) -> None:
        self.dimensions = dimensions
        self.model_revision = f"hashing-test-v1-{dimensions}"
        self.provenance = {
            "provider": "local-hashing-test",
            "protocol": "in-process",
            "model_revision": self.model_revision,
        }

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        output = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in re.findall(r"\w+", text.casefold()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            output.append(l2_normalize(vector))
        return output


class SentenceTransformerEncoder:
    def __init__(self, model_name: str, revision: Optional[str], device: str = "auto") -> None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        try:
            hub = importlib.import_module("huggingface_hub")
            sentence_transformers = importlib.import_module("sentence_transformers")
        except ImportError as exc:
            raise DependencyError(
                "embed requires sentence-transformers and huggingface-hub; install "
                "`python3 -m pip install -r requirements/preference.txt`, then place "
                "BAAI/bge-base-en-v1.5 in the local Hugging Face cache"
            ) from exc
        try:
            if Path(model_name).exists():
                resolved = Path(model_name).resolve()
                cached_commit = (
                    resolved.name
                    if resolved.parent.name == "snapshots"
                    and re.fullmatch(r"[0-9a-f]{20,64}", resolved.name)
                    else None
                )
                exact_revision = revision or cached_commit
                if not exact_revision:
                    raise DependencyError(
                        "a local model path requires --model-revision with an immutable "
                        "revision or content identifier"
                    )
            else:
                resolved = Path(hub.snapshot_download(
                    repo_id=model_name,
                    revision=revision or "main",
                    local_files_only=True,
                ))
                exact_revision = resolved.name
        except DependencyError:
            raise
        except Exception as exc:
            raise DependencyError(
                f"model {model_name!r} is not available locally; download it once "
                "outside this offline pipeline, then rerun embed"
            ) from exc
        chosen_device = None if device == "auto" else device
        try:
            self._model = sentence_transformers.SentenceTransformer(
                str(resolved), device=chosen_device, local_files_only=True,
            )
        except TypeError:
            self._model = sentence_transformers.SentenceTransformer(
                str(resolved), device=chosen_device,
            )
        self.model_revision = f"{model_name}@{exact_revision}"
        self.provenance = {
            "provider": "local-sentence-transformers",
            "protocol": "in-process",
            "model": model_name,
            "model_revision": self.model_revision,
        }

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        values = self._model.encode(
            list(texts), batch_size=32, show_progress_bar=False,
            normalize_embeddings=True, convert_to_numpy=True,
        )
        return [[float(value) for value in row] for row in values]


def _remote_encoder(
    inference_config: Path | str | None,
    remote_provider: EmbeddingProvider | None,
) -> EmbeddingProvider:
    if inference_config is not None and remote_provider is not None:
        raise PreferenceModelError(
            "inference_config and remote embedding provider are mutually exclusive"
        )
    if remote_provider is not None:
        return remote_provider
    if inference_config is None:
        raise PreferenceModelError("remote encoder requires --inference-config")
    try:
        return build_embedding_provider(load_inference_config(inference_config))
    except InferenceConfigError as exc:
        raise PreferenceModelError(str(exc)) from exc


def make_encoder(
    kind: str,
    model: str,
    revision: Optional[str],
    device: str,
    inference_config: Path | str | None = None,
    remote_provider: EmbeddingProvider | None = None,
) -> Any:
    if kind == "hashing":
        return HashingEncoder()
    if kind == "remote":
        return _remote_encoder(inference_config, remote_provider)
    return SentenceTransformerEncoder(model, revision, device)


def encoder_for_recorded_revision(
    model_revision: str,
    device: str = "auto",
    inference_config: Path | str | None = None,
    remote_provider: EmbeddingProvider | None = None,
) -> Any:
    """Recreate exactly the encoder named by a model artifact."""
    hashing = re.fullmatch(r"hashing-test-v1-(\d+)", model_revision)
    if hashing:
        return HashingEncoder(int(hashing.group(1)))
    # A shared profile can configure hosted mail generation without embeddings.
    # Its presence alone must not change the encoder of an existing ranker.
    if inference_config is not None and remote_provider is None:
        from job_search.inference import load_inference_config
        if load_inference_config(inference_config).embeddings is None:
            inference_config = None
    if inference_config is not None or remote_provider is not None:
        encoder = _remote_encoder(inference_config, remote_provider)
        if str(encoder.model_revision) != model_revision:
            raise PreferenceModelError(
                f"remote encoder {encoder.model_revision!r} does not match artifact "
                f"revision {model_revision!r}"
            )
        return encoder
    if "@" not in model_revision:
        raise PreferenceModelError(
            f"recorded embedding revision {model_revision!r} cannot be resolved exactly"
        )
    model_name, revision = model_revision.rsplit("@", 1)
    if not model_name or not revision:
        raise PreferenceModelError(
            f"recorded embedding revision {model_revision!r} cannot be resolved exactly"
        )
    encoder = SentenceTransformerEncoder(model_name, revision, device)
    if encoder.model_revision != model_revision:
        raise PreferenceModelError(
            f"resolved encoder {encoder.model_revision!r} does not match artifact "
            f"revision {model_revision!r}"
        )
    return encoder


def _state_value(con: sqlite3.Connection, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM preference_state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def _set_state(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO preference_state(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _write_feature_document(con: sqlite3.Connection, document: FeatureDocument, now: str) -> None:
    con.execute(
        "INSERT INTO preference_feature_documents "
        "(subject_type,subject_id,family_id,feature_fingerprint,title_metadata_text,"
        "description_text,description_chunks_json,template_cluster_id,leakage_group_id,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(subject_type,subject_id) DO UPDATE SET "
        "family_id=excluded.family_id,feature_fingerprint=excluded.feature_fingerprint,"
        "title_metadata_text=excluded.title_metadata_text,description_text=excluded.description_text,"
        "description_chunks_json=excluded.description_chunks_json,"
        "template_cluster_id=excluded.template_cluster_id,leakage_group_id=excluded.leakage_group_id,"
        "updated_at=excluded.updated_at WHERE "
        "preference_feature_documents.family_id<>excluded.family_id OR "
        "preference_feature_documents.feature_fingerprint<>excluded.feature_fingerprint OR "
        "preference_feature_documents.template_cluster_id<>excluded.template_cluster_id OR "
        "preference_feature_documents.leakage_group_id<>excluded.leakage_group_id OR "
        "preference_feature_documents.title_metadata_text<>'' OR "
        "preference_feature_documents.description_text<>'' OR "
        "preference_feature_documents.description_chunks_json<>'[]'",
        (
            document.subject_type, document.subject_id, document.family_id,
            document.fingerprint, "", "", "[]", document.template_cluster_id,
            document.leakage_group_id, now,
        ),
    )


def iter_batches(values: Iterable[Any], batch_size: int) -> Iterator[list[Any]]:
    """Yield bounded lists from any iterable."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    batch = []
    for value in values:
        batch.append(value)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _flush_embedding_texts(
    con: sqlite3.Connection,
    pending: dict[str, str],
    encoder: Any,
    model_revision: str,
    now: str,
) -> int:
    if not pending:
        return 0
    ordered = sorted(pending.items())
    fingerprints = [fingerprint for fingerprint, _ in ordered]
    existing = {
        str(row[0])
        for row in con.execute(
            "SELECT text_fingerprint FROM preference_embedding_cache "
            "WHERE model_revision=? AND text_version=? AND text_fingerprint IN ("
            + ",".join("?" for _ in fingerprints) + ")",
            (model_revision, TEXT_VERSION, *fingerprints),
        )
    }
    missing = [(fingerprint, text) for fingerprint, text in ordered if fingerprint not in existing]
    if missing:
        texts = [text for _, text in missing]
        stream = getattr(encoder, "iter_encode_batches", None)
        batches = stream(texts) if callable(stream) else [(texts, encoder.encode(texts))]
        offset = 0
        for batch_texts, vectors in batches:
            # Preserve exact input order and cache binding across provider-sized
            # batches; never associate a response with another text fingerprint.
            batch_texts = list(batch_texts)
            if not batch_texts or batch_texts != texts[offset:offset + len(batch_texts)] or len(vectors) != len(batch_texts):
                raise PreferenceModelError("encoder returned an invalid embedding batch")
            encoded = []
            for (fingerprint, _), vector in zip(missing[offset:offset + len(batch_texts)], vectors):
                normalized = l2_normalize(vector)
                encoded.append((
                    model_revision, TEXT_VERSION, fingerprint,
                    sqlite3.Binary(encode_vector_blob(normalized)), len(normalized), now,
                ))
            con.executemany(
                "INSERT OR IGNORE INTO preference_embedding_cache "
                "(model_revision,text_version,text_fingerprint,vector_blob,dimensions,created_at) "
                "VALUES (?,?,?,?,?,?)",
                encoded,
            )
            # A quota deferral or later provider failure must not discard vectors
            # already purchased. Resume asks only for the still-missing texts.
            con.commit()
            offset += len(batch_texts)
        if offset != len(missing):
            raise PreferenceModelError("encoder returned the wrong number of vectors")
    # An interrupted backfill resumes after its most recently completed encoder batch.
    con.commit()
    return len(missing)


def embed_documents(
    state_db: Path,
    documents: Iterable[FeatureDocument],
    encoder: Any,
    batch_size: int = 256,
    activate_revision: bool = True,
    prepare_schema: bool = True,
) -> dict[str, int]:
    """Incrementally embed changed documents, sharing identical text vectors."""
    if prepare_schema:
        prepare_state(state_db)
    model_revision = str(encoder.model_revision)
    now = utc_now()
    if batch_size < 1 or batch_size > 900:
        raise ValueError("embedding batch_size must be between 1 and 900")
    pending: dict[str, str] = {}
    document_count = 0
    checked_texts = 0
    embedded_texts = 0
    with connect_state(state_db) as con:
        provenance = getattr(encoder, "provenance", None)
        if provenance is not None:
            provenance_json = canonical_json(dict(provenance))
            con.execute(
                "INSERT INTO preference_embedding_provenance "
                "(model_revision,provenance_fingerprint,provenance_json,first_used_at,last_used_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(model_revision,provenance_fingerprint) "
                "DO UPDATE SET last_used_at=excluded.last_used_at",
                (
                    model_revision, sha256_text(provenance_json), provenance_json,
                    now, now,
                ),
            )
        for document in documents:
            document_count += 1
            _write_feature_document(con, document, now)
            title_fp = text_fingerprint(document.title_metadata_text)
            chunk_fps = [text_fingerprint(chunk) for chunk in document.description_chunks]
            for fingerprint, text in (
                [(title_fp, document.title_metadata_text)]
                + list(zip(chunk_fps, document.description_chunks))
            ):
                if fingerprint in pending:
                    continue
                if len(pending) == batch_size:
                    checked_texts += len(pending)
                    embedded_texts += _flush_embedding_texts(
                        con, pending, encoder, model_revision, now,
                    )
                    pending.clear()
                pending[fingerprint] = text
            con.execute(
                "INSERT INTO preference_embedding_refs "
                "(subject_type,subject_id,model_revision,text_version,feature_fingerprint,"
                "title_fingerprint,chunk_fingerprints_json,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(subject_type,subject_id,model_revision,text_version) DO UPDATE SET "
                "feature_fingerprint=excluded.feature_fingerprint,"
                "title_fingerprint=excluded.title_fingerprint,"
                "chunk_fingerprints_json=excluded.chunk_fingerprints_json,updated_at=excluded.updated_at "
                "WHERE preference_embedding_refs.feature_fingerprint<>excluded.feature_fingerprint OR "
                "preference_embedding_refs.title_fingerprint<>excluded.title_fingerprint OR "
                "preference_embedding_refs.chunk_fingerprints_json<>excluded.chunk_fingerprints_json",
                (
                    document.subject_type, document.subject_id, model_revision, TEXT_VERSION,
                    document.fingerprint, title_fp, canonical_json(chunk_fps), now,
                ),
            )
        if pending:
            checked_texts += len(pending)
            embedded_texts += _flush_embedding_texts(
                con, pending, encoder, model_revision, now,
            )
        if activate_revision:
            _set_state(con, "embedding_model_revision", model_revision)
            _set_state(con, "text_version", TEXT_VERSION)
            provenance = getattr(encoder, "provenance", None)
            if provenance is not None:
                _set_state(
                    con,
                    "embedding_provider_provenance",
                    canonical_json(dict(provenance)),
                )
    return {
        "documents": document_count,
        "unique_texts": checked_texts,
        "embedded_texts": embedded_texts,
    }


def load_combined_vectors(
    state_db: Path,
    documents: Sequence[FeatureDocument],
    model_revision: str,
) -> list[list[float]]:
    vectors: list[list[float]] = []
    missing: list[str] = []
    with connect_state(state_db) as con:
        for document_batch in iter_batches(documents, 400):
            refs: dict[tuple[str, str], sqlite3.Row] = {}
            by_type: dict[str, list[str]] = {}
            for document in document_batch:
                by_type.setdefault(document.subject_type, []).append(document.subject_id)
            for subject_type, subject_ids in by_type.items():
                for id_batch in iter_batches(subject_ids, 800):
                    rows = con.execute(
                        "SELECT subject_type,subject_id,feature_fingerprint,title_fingerprint,"
                        "chunk_fingerprints_json FROM preference_embedding_refs "
                        "WHERE model_revision=? AND text_version=? AND subject_type=? "
                        "AND subject_id IN (" + ",".join("?" for _ in id_batch) + ")",
                        (model_revision, TEXT_VERSION, subject_type, *id_batch),
                    ).fetchall()
                    refs.update({(str(row[0]), str(row[1])): row for row in rows})

            fingerprints: set[str] = set()
            fingerprints_by_document: dict[tuple[str, str], list[str]] = {}
            for document in document_batch:
                key = (document.subject_type, document.subject_id)
                ref = refs.get(key)
                if not ref or ref["feature_fingerprint"] != document.fingerprint:
                    missing.append(f"{document.subject_type}:{document.subject_id}")
                    continue
                values = [str(ref["title_fingerprint"])] + list(
                    json.loads(ref["chunk_fingerprints_json"])
                )
                fingerprints_by_document[key] = values
                fingerprints.update(values)
            cache: dict[str, list[float]] = {}
            for fingerprint_batch in iter_batches(sorted(fingerprints), 800):
                cache_rows = con.execute(
                    "SELECT text_fingerprint,vector_blob,dimensions "
                    "FROM preference_embedding_cache WHERE model_revision=? "
                    "AND text_version=? AND text_fingerprint IN ("
                    + ",".join("?" for _ in fingerprint_batch) + ")",
                    (model_revision, TEXT_VERSION, *fingerprint_batch),
                ).fetchall()
                cache.update({
                    str(row["text_fingerprint"]): decode_vector_blob(
                        row["vector_blob"], row["dimensions"],
                    )
                    for row in cache_rows
                })
            for document in document_batch:
                key = (document.subject_type, document.subject_id)
                document_fingerprints = fingerprints_by_document.get(key)
                if not document_fingerprints:
                    continue
                if any(fingerprint not in cache for fingerprint in document_fingerprints):
                    missing.append(f"{document.subject_type}:{document.subject_id}")
                    continue
                vectors.append(combined_vector(
                    cache[document_fingerprints[0]],
                    [cache[fingerprint] for fingerprint in document_fingerprints[1:]],
                ))
    if missing:
        sample = ", ".join(missing[:3])
        raise PreferenceModelError(
            f"{len(missing)} documents lack current embeddings ({sample}); run `embed` first"
        )
    return vectors


def require_module(
    name: str,
    purpose: str,
    importer: Callable[[str], Any] = importlib.import_module,
) -> Any:
    try:
        return importer(name)
    except ImportError as exc:
        raise DependencyError(
            f"{purpose} requires optional dependency {name!r}; install "
            "`python3 -m pip install -r requirements/preference.txt`"
        ) from exc


def ndcg_at_k(targets: Sequence[int], predictions: Sequence[float], k: int = 20) -> float:
    order = sorted(range(len(targets)), key=lambda i: (-float(predictions[i]), i))[:k]
    ideal = sorted((int(target) for target in targets), reverse=True)[:k]
    dcg = sum((2 ** int(targets[index]) - 1) / math.log2(rank + 2) for rank, index in enumerate(order))
    idcg = sum((2 ** target - 1) / math.log2(rank + 2) for rank, target in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def precision_at_k(targets: Sequence[int], predictions: Sequence[float], k: int) -> float:
    if not targets:
        return 0.0
    order = sorted(range(len(targets)), key=lambda i: (-float(predictions[i]), i))[:k]
    return sum(int(targets[index]) for index in order) / max(1, len(order))


def average_precision(targets: Sequence[int], predictions: Sequence[float]) -> float:
    positives = sum(int(target) for target in targets)
    if positives == 0:
        return 0.0
    order = sorted(range(len(targets)), key=lambda i: (-float(predictions[i]), i))
    found = 0
    total = 0.0
    for rank, index in enumerate(order, 1):
        if targets[index]:
            found += 1
            total += found / rank
    return total / positives


def metric_summary(targets: Sequence[int], predictions: Sequence[float]) -> dict[str, float]:
    order = sorted(range(len(targets)), key=lambda i: (-float(predictions[i]), i))[:20]
    positives = sum(int(target) for target in targets)
    return {
        "ndcg_at_20": ndcg_at_k(targets, predictions, 20),
        "precision_at_10": precision_at_k(targets, predictions, 10),
        "precision_at_20": precision_at_k(targets, predictions, 20),
        "recall_at_20": (
            sum(int(targets[index]) for index in order) / positives if positives else 0.0
        ),
        "average_precision": average_precision(targets, predictions),
    }


def combined_cv_groups(documents: Sequence[FeatureDocument]) -> list[str]:
    """Connect examples sharing either a fuzzy template or exact-text leakage id."""
    parent = list(range(len(documents)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_template: dict[str, int] = {}
    by_leakage: dict[str, int] = {}
    for index, document in enumerate(documents):
        for value, seen in (
            (document.template_cluster_id, by_template),
            (document.leakage_group_id, by_leakage),
        ):
            if value in seen:
                union(index, seen[value])
            else:
                seen[value] = index
    components: dict[int, list[str]] = {}
    for index, document in enumerate(documents):
        components.setdefault(find(index), []).append(document.family_id)
    identifiers = {
        root: "cv_" + sha256_text(canonical_json(sorted(families)))
        for root, families in components.items()
    }
    return [identifiers[find(index)] for index in range(len(documents))]


def validate_group_splits(splits: Sequence[tuple[Sequence[int], Sequence[int]]], groups: Sequence[str]) -> None:
    for train, test in splits:
        train_groups = {groups[index] for index in train}
        test_groups = {groups[index] for index in test}
        overlap = train_groups & test_groups
        if overlap:
            raise PreferenceModelError(f"leakage groups cross a fold: {sorted(overlap)[:3]}")


def choose_fold_count(targets: Sequence[int], groups: Sequence[str]) -> int:
    class_counts = [sum(target == value for target in targets) for value in (0, 1)]
    unique_groups = len(set(groups))
    if min(class_counts) >= 5 and unique_groups >= 5:
        return 5
    if min(class_counts) >= 3 and unique_groups >= 3:
        return 3
    raise PreferenceModelError(
        "training needs at least three interested and three not_interested labels "
        "across at least three leakage groups"
    )


def select_simplest_within(
    candidates: Sequence[dict[str, Any]], tolerance: float = 0.01,
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("no model candidates")
    best = max(float(candidate["ndcg_at_20"]) for candidate in candidates)
    eligible = [candidate for candidate in candidates if float(candidate["ndcg_at_20"]) >= best - tolerance]
    return min(
        eligible,
        key=lambda candidate: (
            int(candidate.get("complexity", 999)),
            -float(candidate["ndcg_at_20"]),
            str(candidate.get("name", "")),
        ),
    )


def _baseline_clearance_reason(
    selected: dict[str, Any], baselines: dict[str, dict[str, float]],
) -> Optional[str]:
    """Keep strict NDCG improvement except at the perfect-score ceiling."""
    def metric(row: dict[str, Any], name: str) -> float:
        value = float(row[name])
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("invalid ranking metric")
        return value

    try:
        title, random = baselines["title_only"], baselines["random"]
        selected_ndcg = metric(selected, "ndcg_at_20")
        title_ndcg, random_ndcg = (metric(row, "ndcg_at_20") for row in (title, random))
        if selected_ndcg > max(title_ndcg, random_ndcg):
            return "out_of_fold_winner"
        if (
            selected_ndcg == title_ndcg == 1.0
            and random_ndcg < 1.0
            and metric(selected, "precision_at_20") == 1.0
            and metric(title, "precision_at_20") == 1.0
            and metric(selected, "average_precision") > max(
                metric(title, "average_precision"), metric(random, "average_precision")
            )
        ):
            return "out_of_fold_perfect_score_tiebreak"
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def apply_baseline_selection_guard(
    selected: dict[str, Any],
    candidates: Sequence[dict[str, Any]],
    baselines: dict[str, dict[str, float]],
) -> tuple[dict[str, Any], str]:
    """Avoid deploying an ensemble that fails basic title/random sanity checks."""
    reason = _baseline_clearance_reason(selected, baselines)
    if reason:
        return selected, reason
    single_components = [
        candidate for candidate in candidates
        if candidate.get("name") in {"dense_linear", "dense_neighbor", "sparse"}
    ]
    if not single_components:
        raise ValueError("baseline fallback requires component candidates")
    strongest = max(
        single_components,
        key=lambda candidate: (
            float(candidate["ndcg_at_20"]), str(candidate["name"]),
        ),
    )
    return strongest, "baseline_fallback"


def batched_neighbor_scores_and_liked(
    train_vectors: Sequence[Sequence[float]],
    train_targets: Sequence[int],
    train_family_ids: Sequence[str],
    test_vectors: Iterable[Sequence[float]],
    k: int,
    batch_size: int = 1000,
    liked_limit: int = 3,
) -> tuple[list[float], list[list[str]]]:
    """Dependency-free reference scorer that never builds a corpus similarity matrix."""
    if not (len(train_vectors) == len(train_targets) == len(train_family_ids)):
        raise ValueError("neighbor training arrays must have equal lengths")
    if not train_vectors:
        raise ValueError("neighbor scoring needs training examples")
    scores: list[float] = []
    liked: list[list[str]] = []
    for test_batch in iter_batches(test_vectors, batch_size):
        for test_vector in test_batch:
            similarities = [
                sum(float(left) * float(right) for left, right in zip(test_vector, train_vector))
                for train_vector in train_vectors
            ]
            order = sorted(range(len(similarities)), key=lambda index: (-similarities[index], index))
            neighbors = order[:min(k, len(order))]
            weights = [max(similarities[index], 0.0) for index in neighbors]
            if sum(weights) == 0.0:
                score = sum(int(value) for value in train_targets) / len(train_targets)
            else:
                weighted = sum(
                    weight * int(train_targets[index])
                    for weight, index in zip(weights, neighbors)
                )
                score = (weighted + 0.5) / (sum(weights) + 1.0)
            scores.append(float(score))
            liked.append([
                str(train_family_ids[index]) for index in order
                if int(train_targets[index]) == 1
            ][:liked_limit])
    return scores, liked


def _neighbor_predict_details(
    train_x: Any,
    train_y: Any,
    train_family_ids: Sequence[str],
    test_x: Any,
    k: int,
    numpy: Any,
    liked_limit: int = 3,
    train_sample_weights: Any = None,
) -> tuple[Any, list[list[str]]]:
    similarities = test_x @ train_x.T
    count = min(k, train_x.shape[0])
    order = numpy.argsort(-similarities, axis=1, kind="stable")
    indexes = order[:, :count]
    predictions = []
    liked = []
    for row_index, neighbors in enumerate(indexes):
        weights = numpy.maximum(similarities[row_index, neighbors], 0.0)
        if train_sample_weights is not None:
            weights = weights * train_sample_weights[neighbors]
        if float(weights.sum()) == 0.0:
            if train_sample_weights is None:
                predictions.append(float(train_y.mean()))
            else:
                predictions.append(float(numpy.average(train_y, weights=train_sample_weights)))
        else:
            # Small prior prevents a single near-duplicate from producing 0 or 1.
            predictions.append(float((weights @ train_y[neighbors] + 0.5) / (weights.sum() + 1.0)))
        liked.append(([
            str(train_family_ids[index]) for index in order[row_index]
            if int(train_y[index]) == 1
        ][:liked_limit]) if liked_limit else [])
    return numpy.asarray(predictions), liked


def _neighbor_predict(
    train_x: Any, train_y: Any, test_x: Any, k: int, numpy: Any,
    train_sample_weights: Any = None,
) -> Any:
    predictions, _ = _neighbor_predict_details(
        train_x, train_y, [str(index) for index in range(train_x.shape[0])],
        test_x, k, numpy, liked_limit=0, train_sample_weights=train_sample_weights,
    )
    return predictions


def _model_candidates(component_predictions: dict[str, Sequence[float]], targets: Sequence[int]) -> list[dict[str, Any]]:
    candidates = []
    names = ("dense_linear", "dense_neighbor", "sparse")
    for name in names:
        metrics = metric_summary(targets, component_predictions[name])
        candidates.append({"name": name, "weights": {name: 1.0}, "complexity": 1, **metrics})
    steps = (0.0, 0.25, 0.5, 0.75, 1.0)
    for dense_weight in steps:
        for neighbor_weight in steps:
            sparse_weight = round(1.0 - dense_weight - neighbor_weight, 10)
            if sparse_weight < 0 or sparse_weight not in steps:
                continue
            nonzero = sum(weight > 0 for weight in (dense_weight, neighbor_weight, sparse_weight))
            if nonzero < 2:
                continue
            predictions = [
                dense_weight * component_predictions["dense_linear"][index]
                + neighbor_weight * component_predictions["dense_neighbor"][index]
                + sparse_weight * component_predictions["sparse"][index]
                for index in range(len(targets))
            ]
            metrics = metric_summary(targets, predictions)
            candidates.append({
                "name": f"ensemble-{dense_weight:g}-{neighbor_weight:g}-{sparse_weight:g}",
                "weights": {
                    "dense_linear": dense_weight,
                    "dense_neighbor": neighbor_weight,
                    "sparse": sparse_weight,
                },
                "complexity": nonzero,
                **metrics,
            })
    return candidates


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fit_sparse(
    vectorizers: Any, documents: Sequence[FeatureDocument], targets: Any,
    modules: dict[str, Any], sample_weights: Any = None,
) -> Any:
    word = vectorizers["word"]
    char = vectorizers["char"]
    word_x = word.fit_transform([document.word_text for document in documents])
    char_x = char.fit_transform([document.char_text for document in documents])
    matrix = modules["scipy_sparse"].hstack((word_x, char_x), format="csr")
    classifier = modules["LogisticRegression"](
        C=vectorizers["C"], class_weight="balanced", max_iter=2000,
        random_state=DEFAULT_SEED, solver="liblinear",
    )
    classifier.fit(matrix, targets, sample_weight=sample_weights)
    return {"word": word, "char": char, "classifier": classifier}


def _new_vectorizers(C: float, modules: dict[str, Any]) -> dict[str, Any]:
    vectorizer = modules["TfidfVectorizer"]
    return {
        "C": C,
        "word": vectorizer(
            ngram_range=(1, 2), sublinear_tf=True, max_features=75000,
            strip_accents="unicode", dtype=modules["numpy"].float64,
        ),
        "char": vectorizer(
            analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True,
            max_features=50000, dtype=modules["numpy"].float64,
        ),
    }


def _sparse_matrix(sparse_model: dict[str, Any], documents: Sequence[FeatureDocument], scipy_sparse: Any) -> Any:
    word_x = sparse_model["word"].transform([document.word_text for document in documents])
    char_x = sparse_model["char"].transform([document.char_text for document in documents])
    return scipy_sparse.hstack((word_x, char_x), format="csr")


def _optional_ml_modules() -> dict[str, Any]:
    numpy = require_module("numpy", "train and score")
    sklearn = require_module("sklearn", "train and score")
    scipy = require_module("scipy", "train and score")
    scipy_sparse = require_module("scipy.sparse", "train and score")
    linear = require_module("sklearn.linear_model", "train and score")
    feature = require_module("sklearn.feature_extraction.text", "train and score")
    selection = require_module("sklearn.model_selection", "train and score")
    return {
        "numpy": numpy,
        "dependency_versions": {
            "numpy": str(numpy.__version__),
            "python": platform.python_version(),
            "scikit_learn": str(sklearn.__version__),
            "scipy": str(scipy.__version__),
        },
        "scipy_sparse": scipy_sparse,
        "LogisticRegression": linear.LogisticRegression,
        "TfidfVectorizer": feature.TfidfVectorizer,
        "StratifiedGroupKFold": selection.StratifiedGroupKFold,
    }


def _best_hyperparameter(results: dict[Any, Sequence[float]], targets: Sequence[int], preferred: Any) -> Any:
    return max(
        results,
        key=lambda value: (
            ndcg_at_k(targets, results[value], 20),
            value == preferred,
            -abs(float(value) - float(preferred)),
        ),
    )


def training_label_spec(examples: Sequence[TrainingExample]) -> list[dict[str, Any]]:
    return [
        {
            "example_id": example.example_id,
            "family_id": example.document.family_id,
            "target": example.target,
            "feature_fingerprint": example.document.fingerprint,
            "template_cluster_id": example.document.template_cluster_id,
            "leakage_group_id": example.document.leakage_group_id,
            "dataset_version": example.dataset_version,
            "source_fingerprint": example.source_fingerprint,
            "selection_strategy": example.selection_strategy,
            "selection_probability": example.selection_probability,
            "sample_weight": example.sample_weight,
        }
        for example in examples
    ]


def train_model(
    source_db: Path,
    state_db: Path,
    artifact_dir: Path,
    min_training_labels: int = 200,
    training_examples: Optional[Sequence[TrainingExample]] = None,
    training_source: str = "human",
    auto_promote: bool = True,
) -> dict[str, Any]:
    if min_training_labels < 1:
        raise ValueError("min_training_labels must be positive")
    examples = list(training_examples) if training_examples is not None else load_training_examples(source_db)
    if len(examples) < min_training_labels:
        raise PreferenceModelError(
            f"training needs {min_training_labels} decisive training labels; found {len(examples)}"
        )
    modules = _optional_ml_modules()
    numpy = modules["numpy"]
    targets = [example.target for example in examples]
    if set(targets) != {0, 1}:
        raise PreferenceModelError("training requires both interested and not_interested labels")
    documents = [example.document for example in examples]
    if not training_source.strip():
        raise PreferenceModelError("training_source cannot be empty")
    if any(
        not math.isfinite(float(example.sample_weight)) or float(example.sample_weight) <= 0
        for example in examples
    ):
        raise PreferenceModelError("training sample weights must be finite and positive")
    groups = combined_cv_groups(documents)
    fold_count = choose_fold_count(targets, groups)
    prepare_state(state_db)
    with connect_state(state_db) as con:
        model_revision = _state_value(con, "embedding_model_revision")
    if not model_revision:
        raise PreferenceModelError("no embedding revision is active; run `embed` first")
    dense = numpy.asarray(load_combined_vectors(state_db, documents, model_revision), dtype=float)
    y = numpy.asarray(targets, dtype=int)
    sample_weights = numpy.asarray([example.sample_weight for example in examples], dtype=float)
    splitter = modules["StratifiedGroupKFold"](
        n_splits=fold_count, shuffle=True, random_state=DEFAULT_SEED,
    )
    splits = list(splitter.split(dense, y, groups))
    validate_group_splits(splits, groups)
    if any(set(y[train].tolist()) != {0, 1} for train, _ in splits):
        raise PreferenceModelError("a grouped training fold lacks one preference class; collect more diverse labels")

    dense_results = {C: numpy.zeros(len(y), dtype=float) for C in (0.1, 1.0, 10.0)}
    title_results = {C: numpy.zeros(len(y), dtype=float) for C in (0.1, 1.0, 10.0)}
    neighbor_results = {k: numpy.zeros(len(y), dtype=float) for k in (5, 10, 20)}
    sparse_results = {C: numpy.zeros(len(y), dtype=float) for C in (0.1, 1.0, 10.0)}
    for train, test in splits:
        for C, predictions in dense_results.items():
            model = modules["LogisticRegression"](
                C=C, class_weight="balanced", max_iter=2000,
                random_state=DEFAULT_SEED, solver="liblinear",
            )
            model.fit(dense[train], y[train], sample_weight=sample_weights[train])
            predictions[test] = model.predict_proba(dense[test])[:, 1]
        title_dimensions = dense.shape[1] // 2
        for C, predictions in title_results.items():
            model = modules["LogisticRegression"](
                C=C, class_weight="balanced", max_iter=2000,
                random_state=DEFAULT_SEED, solver="liblinear",
            )
            model.fit(
                dense[train, :title_dimensions], y[train],
                sample_weight=sample_weights[train],
            )
            predictions[test] = model.predict_proba(dense[test, :title_dimensions])[:, 1]
        for k, predictions in neighbor_results.items():
            predictions[test] = _neighbor_predict(
                dense[train], y[train], dense[test], k, numpy, sample_weights[train],
            )
        train_documents = [documents[index] for index in train]
        test_documents = [documents[index] for index in test]
        for C, predictions in sparse_results.items():
            sparse_model = _fit_sparse(
                _new_vectorizers(C, modules), train_documents, y[train], modules,
                sample_weights[train],
            )
            matrix = _sparse_matrix(sparse_model, test_documents, modules["scipy_sparse"])
            predictions[test] = sparse_model["classifier"].predict_proba(matrix)[:, 1]

    dense_C = _best_hyperparameter(dense_results, targets, 1.0)
    title_C = _best_hyperparameter(title_results, targets, 1.0)
    neighbor_k = _best_hyperparameter(neighbor_results, targets, 10)
    sparse_C = _best_hyperparameter(sparse_results, targets, 1.0)
    component_predictions = {
        "dense_linear": dense_results[dense_C].tolist(),
        "dense_neighbor": neighbor_results[neighbor_k].tolist(),
        "sparse": sparse_results[sparse_C].tolist(),
    }
    candidates = _model_candidates(component_predictions, targets)
    selected = select_simplest_within(candidates)
    random_predictions = [
        int(sha256_text(example.document.family_id)[:13], 16) / float(16 ** 13 - 1)
        for example in examples
    ]
    baselines = {
        "random": metric_summary(targets, random_predictions),
        "title_only": metric_summary(targets, title_results[title_C].tolist()),
    }
    selected, selection_reason = apply_baseline_selection_guard(
        selected, candidates, baselines,
    )

    dense_model = modules["LogisticRegression"](
        C=dense_C, class_weight="balanced", max_iter=2000,
        random_state=DEFAULT_SEED, solver="liblinear",
    )
    dense_model.fit(dense, y, sample_weight=sample_weights)
    sparse_model = _fit_sparse(
        _new_vectorizers(sparse_C, modules), documents, y, modules, sample_weights,
    )
    label_spec = training_label_spec(examples)
    run_spec = {
        "format_version": MODEL_FORMAT_VERSION,
        "text_version": TEXT_VERSION,
        "model_revision": model_revision,
        "seed": DEFAULT_SEED,
        "dependency_versions": modules["dependency_versions"],
        "fold_count": fold_count,
        "minimum_training_labels": min_training_labels,
        "training_source": training_source,
        "labels": label_spec,
        "hyperparameters": {
            "dense_C": dense_C,
            "title_baseline_C": title_C,
            "neighbor_k": neighbor_k,
            "sparse_C": sparse_C,
        },
        "selection": {
            "name": selected["name"], "weights": selected["weights"],
            "selection_reason": selection_reason,
        },
    }
    run_id = "run_" + sha256_text(canonical_json(run_spec))[:24]
    run_dir = artifact_dir / "runs" / run_id
    model_path = run_dir / "model.pkl"
    manifest_path = run_dir / "manifest.json"
    existing_created = None
    existing_manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_created = existing_manifest.get("created_at")
        except (OSError, ValueError):
            existing_created = None
            existing_manifest = {}
    created_at = existing_created or utc_now()
    manifest = {
        **run_spec,
        "run_id": run_id,
        "created_at": created_at,
        "training_examples": len(examples),
        "class_counts": {"interested": sum(targets), "not_interested": len(targets) - sum(targets)},
        "out_of_fold": {
            "components": {
                name: metric_summary(targets, predictions)
                for name, predictions in component_predictions.items()
            },
            "baselines": baselines,
            "candidates": candidates,
            "selected": {**selected, "selection_reason": selection_reason},
        },
    }
    model_artifact = {
        "format_version": MODEL_FORMAT_VERSION,
        "run_id": run_id,
        "model_revision": model_revision,
        "text_version": TEXT_VERSION,
        "dense_model": dense_model,
        "neighbor_vectors": dense,
        "neighbor_targets": y,
        "neighbor_sample_weights": sample_weights,
        "neighbor_family_ids": [document.family_id for document in documents],
        "neighbor_k": neighbor_k,
        "sparse_model": sparse_model,
        "weights": selected["weights"],
    }
    model_bytes = pickle.dumps(model_artifact, protocol=4)
    manifest["artifacts"] = {"model.pkl": sha256_bytes(model_bytes)}
    if "protected_evaluation" in existing_manifest:
        manifest["protected_evaluation"] = existing_manifest["protected_evaluation"]
        evaluation_path = run_dir / "evaluation.json"
        if evaluation_path.exists():
            manifest["artifacts"]["evaluation.json"] = sha256_bytes(
                evaluation_path.read_bytes()
            )
    _atomic_write(model_path, model_bytes)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_write(manifest_path, manifest_bytes)
    with connect_state(state_db) as con:
        con.execute(
            "INSERT INTO preference_model_runs "
            "(run_id,created_at,model_revision,text_version,training_examples,manifest_json,artifact_path) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "manifest_json=excluded.manifest_json,artifact_path=excluded.artifact_path",
            (
                run_id, created_at, model_revision, TEXT_VERSION, len(examples),
                canonical_json(manifest), str(run_dir.resolve()),
            ),
        )
    with connect_state(state_db) as con:
        current_champion = _state_value(con, "champion_run_id")
    promotion_status = "candidate"
    if auto_promote and not current_champion and _clears_training_baselines(manifest):
        promote_run(
            state_db, artifact_dir, run_id,
            reason="first qualifying preference model",
        )
        promotion_status = "auto_promoted_initial"
    elif current_champion == run_id:
        _write_champion_mirror(
            artifact_dir, run_id, sha256_bytes(manifest_bytes),
        )
        promotion_status = "champion_unchanged"
    return {**manifest, "promotion_status": promotion_status}


def _write_champion_mirror(
    artifact_dir: Path, run_id: str, manifest_sha256: str,
) -> None:
    pointer = {"manifest_sha256": manifest_sha256, "run_id": run_id}
    _atomic_write(
        artifact_dir / "champion.json",
        (json.dumps(pointer, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def promote_champion(
    state_db: Path,
    artifact_dir: Path,
    run_id: str,
    manifest_sha256: str,
    reason: str = "initial qualifying model",
    action: str = "initial",
    forced: bool = False,
    projected_incremental_minutes: Optional[float] = None,
    protected_acceptance_fingerprint: Optional[str] = None,
) -> None:
    """Update the canonical DB pointer, record history, then refresh its file mirror."""
    with connect_state(state_db) as con:
        exists = con.execute(
            "SELECT 1 FROM preference_model_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not exists:
            raise PreferenceModelError(f"cannot promote unknown model run {run_id}")
        previous = _state_value(con, "champion_run_id") or ""
        if protected_acceptance_fingerprint:
            consumed = con.execute(
                "SELECT candidate_run_id,consumed_at FROM preference_protected_acceptances "
                "WHERE protected_set_fingerprint=?",
                (protected_acceptance_fingerprint,),
            ).fetchone()
            if consumed:
                raise PreferenceModelError(
                    "protected evaluation set was already consumed by cross-encoder "
                    f"promotion {consumed['candidate_run_id']} at {consumed['consumed_at']}; "
                    "collect a new protected set, or use --force only for documented recovery"
                )
            con.execute(
                "INSERT INTO preference_protected_acceptances "
                "(protected_set_fingerprint,consumed_at,candidate_run_id,previous_run_id,"
                "acceptance_slice) VALUES (?,?,?,?,?)",
                (
                    protected_acceptance_fingerprint, utc_now(), run_id, previous,
                    PROMOTION_ACCEPTANCE_SLICE,
                ),
            )
        _set_state(con, "champion_run_id", run_id)
        con.execute(
            "INSERT INTO preference_champion_history "
            "(run_id,previous_run_id,action,reason,forced,projected_incremental_minutes,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                run_id, previous, action, reason, int(forced),
                projected_incremental_minutes, utc_now(),
            ),
        )
    _write_champion_mirror(artifact_dir, run_id, manifest_sha256)


def _clears_training_baselines(manifest: dict[str, Any]) -> bool:
    try:
        selected = manifest["out_of_fold"]["selected"]
        baselines = manifest["out_of_fold"]["baselines"]
        return _baseline_clearance_reason(selected, baselines) is not None
    except (KeyError, TypeError, ValueError):
        return False


def embedding_switch_gate(
    current_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    projected_incremental_minutes: Optional[float],
) -> list[str]:
    """Check a new encoder using only the frozen top-ranked acceptance slice.

    Uniform and company-holdout slices remain visible as post-selection audits, but
    deliberately cannot compensate for a failed top-ranked product acceptance gate.
    """
    failures = []
    current_eval = current_manifest.get("protected_evaluation") or {}
    candidate_eval = candidate_manifest.get("protected_evaluation") or {}
    try:
        current_fingerprint = protected_example_set_fingerprint(current_eval)
        candidate_fingerprint = protected_example_set_fingerprint(candidate_eval)
    except PreferenceModelError:
        failures.append("candidate and champion need the identical completed protected evaluation")
        return failures
    if current_fingerprint != candidate_fingerprint:
        failures.append("candidate and champion need the identical completed protected evaluation")
        return failures
    try:
        current_metrics = current_eval["slices"]["protected_top_ranked"]["metrics"]
        candidate_metrics = candidate_eval["slices"]["protected_top_ranked"]["metrics"]
        ndcg_gain = (
            float(candidate_metrics["ndcg_at_20"])
            - float(current_metrics["ndcg_at_20"])
        )
        precision_change = (
            float(candidate_metrics["precision_at_20"])
            - float(current_metrics["precision_at_20"])
        )
    except (KeyError, TypeError, ValueError):
        failures.append("protected top-ranked metrics are missing")
        return failures
    if ndcg_gain + 1e-12 < 0.02:
        failures.append(f"NDCG@20 gain is {ndcg_gain:.4f}; need at least 0.0200")
    if precision_change + 1e-12 < -0.02:
        failures.append(
            f"Precision@20 change is {precision_change:.4f}; cannot be below -0.0200"
        )
    if projected_incremental_minutes is None:
        failures.append("projected incremental embedding minutes are required")
    elif projected_incremental_minutes > 30:
        failures.append("projected incremental embedding time exceeds 30 minutes")
    elif projected_incremental_minutes < 0:
        failures.append("projected incremental embedding minutes cannot be negative")
    return failures


def promote_run(
    state_db: Path,
    artifact_dir: Path,
    run_id: str,
    reason: str,
    projected_incremental_minutes: Optional[float] = None,
    force: bool = False,
) -> None:
    if force and not reason.strip():
        raise PreferenceModelError("--force requires a recorded operational reason")
    _, candidate_row = _load_artifact(state_db, run_id)
    candidate_manifest = json.loads(candidate_row["manifest_json"])
    with connect_state(state_db) as con:
        current_id = _state_value(con, "champion_run_id")
    if current_id == run_id:
        raise PreferenceModelError(f"model run {run_id} is already champion")
    if not force and not _clears_training_baselines(candidate_manifest):
        raise PreferenceModelError(
            "candidate does not clear the title-only and random out-of-fold baseline gates"
        )
    protected_acceptance_fingerprint = None
    if current_id and not force:
        _, current_row = _load_artifact(state_db, current_id)
        if current_row["model_revision"] != candidate_row["model_revision"]:
            failures = embedding_switch_gate(
                json.loads(current_row["manifest_json"]), candidate_manifest,
                projected_incremental_minutes,
            )
            if failures:
                raise PreferenceModelError("embedding promotion gates failed: " + "; ".join(failures))
            protected_acceptance_fingerprint = protected_example_set_fingerprint(
                candidate_manifest["protected_evaluation"]
            )
    manifest_path = Path(candidate_row["artifact_path"]) / "manifest.json"
    manifest_hash = sha256_bytes(manifest_path.read_bytes())
    promote_champion(
        state_db, artifact_dir, run_id, manifest_hash,
        reason=reason or "explicit candidate promotion",
        action=("forced_recovery" if force else ("initial" if not current_id else "promote")),
        forced=force,
        projected_incremental_minutes=projected_incremental_minutes,
        protected_acceptance_fingerprint=protected_acceptance_fingerprint,
    )


def rollback_run(
    state_db: Path,
    artifact_dir: Path,
    run_id: str,
    reason: str,
    force: bool = False,
) -> None:
    _, target_row = _load_artifact(state_db, run_id)
    with connect_state(state_db) as con:
        current_id = _state_value(con, "champion_run_id")
        was_champion = con.execute(
            "SELECT 1 FROM preference_champion_history WHERE run_id=? LIMIT 1", (run_id,)
        ).fetchone()
    if not current_id:
        raise PreferenceModelError("there is no current champion to roll back")
    if current_id == run_id:
        raise PreferenceModelError(f"model run {run_id} is already champion")
    if not was_champion and not force:
        raise PreferenceModelError(
            "rollback target was never champion; use promote, or --force for recovery"
        )
    if force and not reason.strip():
        raise PreferenceModelError("--force requires a recorded operational reason")
    manifest_path = Path(target_row["artifact_path"]) / "manifest.json"
    promote_champion(
        state_db, artifact_dir, run_id, sha256_bytes(manifest_path.read_bytes()),
        reason=reason or "operator rollback", action="rollback", forced=force,
    )


def _load_artifact(state_db: Path, run_id: Optional[str]) -> tuple[dict[str, Any], sqlite3.Row]:
    prepare_state(state_db)
    with connect_state(state_db) as con:
        selected = run_id or _state_value(con, "champion_run_id")
        if not selected:
            raise PreferenceModelError("no champion model exists; run `train` first")
        row = con.execute(
            "SELECT * FROM preference_model_runs WHERE run_id=?", (selected,)
        ).fetchone()
    if not row:
        raise PreferenceModelError(f"unknown model run {selected}")
    model_path = Path(row["artifact_path"]) / "model.pkl"
    if not model_path.exists():
        raise PreferenceModelError(f"model artifact is missing: {model_path}")
    try:
        model_bytes = model_path.read_bytes()
        manifest = json.loads(row["manifest_json"])
        expected_hash = manifest.get("artifacts", {}).get("model.pkl")
        if not expected_hash or sha256_bytes(model_bytes) != expected_hash:
            raise PreferenceModelError(f"model artifact hash verification failed: {model_path}")
        artifact = pickle.loads(model_bytes)
    except PreferenceModelError:
        raise
    except (
        OSError, pickle.UnpicklingError, EOFError, AttributeError,
        ImportError, TypeError, ValueError,
    ) as exc:
        raise PreferenceModelError(f"could not load model artifact: {model_path}") from exc
    return artifact, row


def _sparse_explanations(sparse_model: dict[str, Any], matrix: Any, limit: int = 5) -> list[list[dict[str, Any]]]:
    word_count = len(sparse_model["word"].get_feature_names_out())
    names = sparse_model["word"].get_feature_names_out()
    coefficients = sparse_model["classifier"].coef_[0]
    explanations = []
    for row in matrix:
        contributions = []
        for index, value in zip(row.indices, row.data):
            if index >= word_count:
                continue
            contribution = float(value * coefficients[index])
            if contribution > 0:
                contributions.append((contribution, str(names[index])))
        contributions.sort(key=lambda item: (-item[0], item[1]))
        explanations.append([
            {"phrase": phrase, "contribution": round(value, 6)}
            for value, phrase in contributions[:limit]
        ])
    return explanations


def _score_document_batch(
    state_db: Path,
    documents: Sequence[FeatureDocument],
    artifact: dict[str, Any],
    modules: dict[str, Any],
) -> list[dict[str, Any]]:
    numpy = modules["numpy"]
    dense = numpy.asarray(
        load_combined_vectors(state_db, documents, artifact["model_revision"]),
        dtype=float,
    )
    dense_scores = artifact["dense_model"].predict_proba(dense)[:, 1]
    neighbor_scores, liked_neighbors = _neighbor_predict_details(
        artifact["neighbor_vectors"], artifact["neighbor_targets"],
        artifact["neighbor_family_ids"], dense,
        int(artifact["neighbor_k"]), numpy,
        train_sample_weights=artifact.get("neighbor_sample_weights"),
    )
    sparse_matrix = _sparse_matrix(
        artifact["sparse_model"], documents, modules["scipy_sparse"],
    )
    sparse_scores = artifact["sparse_model"]["classifier"].predict_proba(
        sparse_matrix,
    )[:, 1]
    sparse_phrases = _sparse_explanations(artifact["sparse_model"], sparse_matrix)
    weights = artifact["weights"]
    results = []
    for index in range(len(documents)):
        dense_score = float(dense_scores[index])
        neighbor_score = float(neighbor_scores[index])
        sparse_score = float(sparse_scores[index])
        final = (
            float(weights.get("dense_linear", 0.0)) * dense_score
            + float(weights.get("dense_neighbor", 0.0)) * neighbor_score
            + float(weights.get("sparse", 0.0)) * sparse_score
        )
        results.append({
            "dense_linear": dense_score,
            "dense_neighbor": neighbor_score,
            "sparse": sparse_score,
            "final": final,
            "similar_liked_family_ids": liked_neighbors[index],
            "positive_sparse_phrases": sparse_phrases[index],
        })
    return results


def predict_documents(
    state_db: Path,
    run_id: str,
    documents: Sequence[FeatureDocument],
) -> list[dict[str, Any]]:
    """Score already embedded immutable documents with one explicit model run."""
    artifact, _ = _load_artifact(state_db, run_id)
    return _score_document_batch(
        state_db, documents, artifact, _optional_ml_modules(),
    )


def score_and_store_batch(
    con: sqlite3.Connection,
    state_db: Path,
    documents: Sequence[FeatureDocument],
    artifact: dict[str, Any],
    run: Any,
    modules: dict[str, Any],
) -> int:
    """Store one batch; the caller controls the transaction boundary.

    This never prunes old families or certifies a whole-catalog refresh.
    """
    weights = artifact["weights"]
    now = utc_now()
    family_ids = [document.family_id for document in documents]
    existing_rows = con.execute(
        "SELECT family_id,feature_fingerprint FROM preference_scores "
        "WHERE run_id=? AND family_id IN ("
        + ",".join("?" for _ in family_ids) + ")",
        (run["run_id"], *family_ids),
    ).fetchall()
    existing = {str(row[0]): str(row[1]) for row in existing_rows}
    changed = [
        document for document in documents
        if existing.get(document.family_id) != document.fingerprint
    ]
    if not changed:
        return 0
    predictions = _score_document_batch(
        state_db, changed, artifact, modules,
    )
    rows = []
    for index, document in enumerate(changed):
        prediction = predictions[index]
        explanation = {
            "components": {
                "dense_linear": round(prediction["dense_linear"], 6),
                "dense_neighbor": round(prediction["dense_neighbor"], 6),
                "sparse": round(prediction["sparse"], 6),
            },
            "similar_liked_family_ids": prediction["similar_liked_family_ids"],
            "positive_sparse_phrases": prediction["positive_sparse_phrases"],
            "weights": weights,
        }
        rows.append((
            run["run_id"], document.family_id, document.fingerprint,
            prediction["dense_linear"],
            prediction["dense_neighbor"], prediction["sparse"], prediction["final"],
            canonical_json(explanation), now,
        ))
    con.executemany(
        "INSERT INTO preference_scores "
        "(run_id,family_id,feature_fingerprint,dense_linear_score,"
        "dense_neighbor_score,sparse_score,final_score,explanation_json,scored_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,family_id) DO UPDATE SET "
        "feature_fingerprint=excluded.feature_fingerprint,"
        "dense_linear_score=excluded.dense_linear_score,"
        "dense_neighbor_score=excluded.dense_neighbor_score,"
        "sparse_score=excluded.sparse_score,final_score=excluded.final_score,"
        "explanation_json=excluded.explanation_json,scored_at=excluded.scored_at",
        rows,
    )
    return len(rows)


def score_model(
    source_db: Path,
    state_db: Path,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    modules = _optional_ml_modules()
    artifact, run = _load_artifact(state_db, run_id)
    updated = 0
    with connect_state(state_db) as con:
        con.execute(
            "CREATE TEMP TABLE active_score_families (family_id TEXT PRIMARY KEY)"
        )
        for documents in iter_family_document_batches(source_db, batch_size=800):
            con.executemany(
                "INSERT INTO active_score_families VALUES (?)",
                [(document.family_id,) for document in documents],
            )
            updated += score_and_store_batch(con, state_db, documents, artifact, run, modules)
        removed = con.execute(
            "DELETE FROM preference_scores WHERE run_id=? AND family_id NOT IN "
            "(SELECT family_id FROM active_score_families)",
            (run["run_id"],),
        ).rowcount
        total = con.execute(
            "SELECT COUNT(*) FROM preference_scores WHERE run_id=?", (run["run_id"],)
        ).fetchone()[0]
    return {
        "run_id": run["run_id"], "scored_families": total,
        "updated_families": updated, "removed_families": removed,
    }


def ensure_evaluation_embeddings(
    source_db: Path,
    state_db: Path,
    documents: Sequence[FeatureDocument],
    model_revision: str,
    device: str = "auto",
    inference_config: Path | str | None = None,
    remote_provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Embed only missing protected snapshots with the artifact's exact encoder."""
    prepare_state(state_db)
    try:
        load_combined_vectors(state_db, documents, model_revision)
        return {"model_revision": model_revision, "embedded_texts": 0}
    except PreferenceModelError as exc:
        if "lack current embeddings" not in str(exc):
            raise
    try:
        encoder = encoder_for_recorded_revision(
            model_revision, device, inference_config, remote_provider,
        )
    except PreferenceModelError as exc:
        if inference_config is not None:
            command = (
                "python3 -m job_search.ranking.model embed --db "
                f"{shlex.quote(str(source_db))} --encoder remote --inference-config "
                f"{shlex.quote(str(inference_config))}"
            )
        elif "@" in model_revision:
            model_name, revision = model_revision.rsplit("@", 1)
            command = (
                "python3 -m job_search.ranking.model embed --db "
                f"{shlex.quote(str(source_db))} --model {shlex.quote(model_name)} "
                f"--model-revision {shlex.quote(revision)}"
            )
        else:
            command = (
                "python3 -m job_search.ranking.model embed --db "
                f"{shlex.quote(str(source_db))} --encoder hashing"
            )
        raise PreferenceModelError(
            f"evaluation snapshots need exact {model_revision} embeddings; run `{command}`"
        ) from exc
    if str(encoder.model_revision) != model_revision:
        raise PreferenceModelError(
            f"evaluation encoder {encoder.model_revision!r} does not match "
            f"artifact revision {model_revision!r}"
        )
    result = embed_documents(
        state_db, documents, encoder, activate_revision=False,
    )
    # Fail loudly if a partial encoder/cache write somehow left a missing snapshot.
    load_combined_vectors(state_db, documents, model_revision)
    return {"model_revision": model_revision, **result}


def evaluate_model(
    source_db: Path,
    state_db: Path,
    artifact_dir: Path,
    run_id: Optional[str] = None,
    device: str = "auto",
    inference_config: Path | str | None = None,
    remote_provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Score the completed protected set once, without fitting or selecting anything."""
    examples = load_evaluation_examples(source_db)
    grouped = group_evaluation_slices(examples)
    counts = {name: len(values) for name, values in grouped.items()}
    if counts != EVALUATION_SLICE_SIZES:
        expected = ", ".join(
            f"{name}={count}" for name, count in EVALUATION_SLICE_SIZES.items()
        )
        found = ", ".join(f"{name}={counts[name]}" for name in EVALUATION_SLICE_SIZES)
        raise PreferenceModelError(
            f"protected evaluation requires all 200 labels ({expected}); found {found}"
        )
    artifact, run = _load_artifact(state_db, run_id)
    documents = [example.document for example in examples]
    embedding_preflight = ensure_evaluation_embeddings(
        source_db, state_db, documents, str(artifact["model_revision"]), device,
        inference_config, remote_provider,
    )
    modules = _optional_ml_modules()
    predictions = _score_document_batch(
        state_db, documents, artifact, modules,
    )
    prediction_by_id = {
        example.example_id: prediction
        for example, prediction in zip(examples, predictions)
    }
    slices = {}
    for name, slice_examples in grouped.items():
        targets = [example.target for example in slice_examples]
        scores = [prediction_by_id[example.example_id]["final"] for example in slice_examples]
        slices[name] = {
            "count": len(slice_examples),
            "class_counts": {
                "interested": sum(targets),
                "not_interested": len(targets) - sum(targets),
            },
            "metrics": metric_summary(targets, scores),
        }
    report = {
        "run_id": str(run["run_id"]),
        "evaluated_at": utc_now(),
        "counts": {
            "total": len(examples),
            "by_selection_strategy": counts,
        },
        "embedding_preflight": embedding_preflight,
        # These sampling distributions answer different questions; intentionally
        # do not produce a misleading combined headline metric.
        "slices": slices,
        "example_ids": [example.example_id for example in examples],
        "promotion_acceptance_slice": PROMOTION_ACCEPTANCE_SLICE,
        "post_selection_audit_slices": list(POST_SELECTION_AUDIT_SLICES),
    }
    report["protected_example_set_fingerprint"] = protected_example_set_fingerprint(report)
    run_dir = Path(run["artifact_path"])
    report_bytes = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write(run_dir / "evaluation.json", report_bytes)
    try:
        manifest = json.loads(run["manifest_json"])
    except (TypeError, ValueError) as exc:
        raise PreferenceModelError(f"model run {run['run_id']} has an invalid manifest") from exc
    manifest["protected_evaluation"] = report
    manifest.setdefault("artifacts", {})["evaluation.json"] = sha256_bytes(report_bytes)
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write(run_dir / "manifest.json", manifest_bytes)
    with connect_state(state_db) as con:
        con.execute(
            "UPDATE preference_model_runs SET manifest_json=? WHERE run_id=?",
            (canonical_json(manifest), run["run_id"]),
        )
        champion = _state_value(con, "champion_run_id")
    if champion == run["run_id"]:
        _write_champion_mirror(
            artifact_dir, str(run["run_id"]), sha256_bytes(manifest_bytes),
        )
    return report


def source_documents(source_db: Path) -> Iterable[FeatureDocument]:
    snapshots = load_snapshot_documents(source_db)
    if not snapshots:
        try:
            snapshots = [example.document for example in load_training_examples(source_db)]
        except PreferenceModelError as exc:
            if "has no labels" not in str(exc):
                raise
    families = (
        document
        for batch in iter_family_document_batches(source_db, batch_size=1000)
        for document in batch
    )
    return itertools.chain(families, snapshots)


def estimate_embedding_work(
    source_db: Path,
    dimensions: int = 768,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Estimate first-pass family embedding volume without loading an encoder."""
    if dimensions < 1:
        raise PreferenceModelError("--dimensions must be positive")
    if limit is not None and limit < 1:
        raise PreferenceModelError("--limit must be positive")
    families = 0
    title_inputs = 0
    chunk_inputs = 0
    for batch in iter_family_document_batches(source_db, batch_size=1000):
        for document in batch:
            if limit is not None and families >= limit:
                break
            families += 1
            # embed_documents always emits one title/metadata vector, even when
            # the allowlisted title fields happen to be empty.
            title_inputs += 1
            chunk_inputs += len(document.description_chunks)
        if limit is not None and families >= limit:
            break
    total_inputs = title_inputs + chunk_inputs
    raw_vector_bytes = total_inputs * dimensions * 4
    return {
        "families": families,
        "title_inputs": title_inputs,
        "chunk_inputs": chunk_inputs,
        "total_encoder_inputs": total_inputs,
        "dimensions": dimensions,
        "bytes_per_vector": dimensions * 4,
        "raw_vector_bytes": raw_vector_bytes,
        "raw_vector_mib": round(raw_vector_bytes / (1024 * 1024), 2),
        "limited": limit is not None,
    }


def embed_source_documents(
    source_db: Path, state_db: Path, encoder: Any,
) -> dict[str, Any]:
    result = embed_documents(state_db, source_documents(source_db), encoder)
    result["model_revision"] = encoder.model_revision
    return result


def run_embed(args: argparse.Namespace, state_db: Path) -> dict[str, Any]:
    encoder = make_encoder(
        args.encoder, args.model, args.model_revision, args.device,
        getattr(args, "inference_config", None),
    )
    result = embed_source_documents(args.db, state_db, encoder)
    provenance = getattr(encoder, "provenance", None)
    if provenance is not None:
        result["inference_provenance"] = dict(provenance)
    return result


def _run_has_training_labels(manifest_json: str, label_fingerprint: str) -> bool:
    try:
        labels = json.loads(manifest_json).get("labels", [])
    except (TypeError, ValueError):
        return False
    return sha256_text(canonical_json(labels)) == label_fingerprint


def refresh_pipeline(
    args: argparse.Namespace,
    state_db: Path,
    artifact_dir: Path,
) -> dict[str, Any]:
    """Keep the champion current, then optionally build a non-promoted candidate."""
    from job_search.collection.dedupe import prepare_families

    dedupe = prepare_families(args.db)
    with connect_state(state_db) as con:
        champion_id = _state_value(con, "champion_run_id")
        champion_row = (
            con.execute(
                "SELECT * FROM preference_model_runs WHERE run_id=?", (champion_id,)
            ).fetchone()
            if champion_id else None
        )
    if not champion_row:
        embedded = run_embed(args, state_db)
        trained = train_model(
            args.db, state_db, artifact_dir, args.min_training_labels,
        )
        candidate_score = score_model(args.db, state_db, trained["run_id"])
        return {
            "dedupe": dedupe, "embed": embedded, "candidate": trained,
            "candidate_score": candidate_score,
            "promotion_required": trained["promotion_status"] == "candidate",
        }

    # Production freshness always follows the canonical champion's exact encoder,
    # never parser defaults or the most recently benchmarked candidate.
    champion_encoder = encoder_for_recorded_revision(
        str(champion_row["model_revision"]), args.device,
        getattr(args, "inference_config", None),
    )
    embedded = embed_source_documents(args.db, state_db, champion_encoder)
    champion_score = score_model(args.db, state_db, str(champion_id))

    examples = load_training_examples(args.db)
    label_spec = training_label_spec(examples)
    label_fingerprint = sha256_text(canonical_json(label_spec))
    with connect_state(state_db) as con:
        same_encoder_runs = con.execute(
            "SELECT training_examples,manifest_json FROM preference_model_runs "
            "WHERE model_revision=? ORDER BY training_examples DESC,created_at DESC",
            (champion_row["model_revision"],),
        ).fetchall()
    already_trained = any(
        _run_has_training_labels(row["manifest_json"], label_fingerprint)
        for row in same_encoder_runs
    )
    previous_count = max(
        (int(row["training_examples"]) for row in same_encoder_runs), default=0,
    )
    label_delta = len(examples) - previous_count
    cadence_due = (
        len(examples) >= args.min_training_labels
        and not already_trained
        and (label_delta >= args.retrain_every_labels or label_delta <= 0)
    )
    candidate = None
    candidate_score = None
    if cadence_due:
        candidate = train_model(
            args.db, state_db, artifact_dir, args.min_training_labels,
        )
        if candidate["run_id"] != champion_id:
            candidate_score = score_model(args.db, state_db, candidate["run_id"])
    return {
        "dedupe": dedupe,
        "embed": embedded,
        "champion_run_id": champion_id,
        "champion_score": champion_score,
        "candidate": candidate,
        "candidate_score": candidate_score,
        "candidate_cadence": {
            "decisive_training_labels": len(examples),
            "last_trained_labels": previous_count,
            "new_labels": max(0, label_delta),
            "retrain_every": args.retrain_every_labels,
            "status": (
                "candidate_ready_for_explicit_promotion" if candidate
                else ("already_trained" if already_trained else "awaiting_more_labels")
            ),
        },
        "promotion_required": bool(candidate),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "refresh is authoritative after every scrape or content edit: it rebuilds "
            "opportunity families, refreshes champion embeddings/scores, and creates "
            "a candidate only when its label cadence is due"
        ),
    )
    parser.add_argument(
        "command",
        choices=(
            "estimate", "embed", "train", "score", "evaluate", "refresh",
            "promote", "rollback",
        ),
    )
    parser.add_argument(
        "--db", type=Path, default=Path("job-boards.db"),
        help="source jobs database (model reads are read-only; refresh maintains derived families)",
    )
    parser.add_argument("--state-db", type=Path, help="model sidecar (default: <db-stem>-preference.db)")
    parser.add_argument("--artifacts", type=Path, help="model artifacts (default: <db-dir>/.models/preference)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="local path or Hugging Face model id")
    parser.add_argument("--model-revision", help="local cached revision or commit")
    parser.add_argument(
        "--inference-config", type=Path,
        help=(
            "owner-only portable inference profile; defaults to "
            "JOB_SEARCH_INFERENCE_CONFIG"
        ),
    )
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "mps", "cuda"))
    parser.add_argument(
        "--encoder",
        choices=("sentence-transformers", "hashing", "remote"),
        help=(
            "defaults to remote when an inference profile is selected, otherwise "
            "sentence-transformers; hashing is a smoke-test baseline"
        ),
    )
    parser.add_argument("--run-id", help="specific model run to score instead of the champion")
    parser.add_argument(
        "--min-training-labels", type=int, default=200,
        help="minimum decisive training examples required for promotion (default: 200)",
    )
    parser.add_argument(
        "--retrain-every-labels", type=int, default=25,
        help="create a candidate after this many new decisive labels (default: 25)",
    )
    parser.add_argument(
        "--limit", type=int,
        help="estimate at most this many canonical job families",
    )
    parser.add_argument(
        "--dimensions", type=int, default=768,
        help="embedding dimensions used by estimate (default: 768)",
    )
    parser.add_argument(
        "--projected-incremental-minutes", type=float,
        help="measured/projected daily embedding minutes for a cross-encoder promotion",
    )
    parser.add_argument("--reason", default="", help="reason recorded for promotion or rollback")
    parser.add_argument(
        "--force", action="store_true",
        help="bypass promotion history/quality gates only for documented operational recovery",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.inference_config = configured_inference_path(args.inference_config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.encoder = args.encoder or (
        "remote" if args.inference_config is not None else "sentence-transformers"
    )
    args.db = args.db.resolve()
    state_db = (args.state_db or default_state_db(args.db)).resolve()
    artifact_dir = (args.artifacts or default_artifact_dir(args.db)).resolve()
    try:
        if state_db == args.db:
            raise PreferenceModelError(
                "--state-db must differ from --db; the operational jobs database is read-only"
            )
        source_commands = {"estimate", "embed", "train", "score", "evaluate", "refresh"}
        if args.command in source_commands and not args.db.exists():
            raise PreferenceModelError(f"source database does not exist: {args.db}")
        if args.command != "estimate":
            prepare_state(state_db)
        if args.command == "estimate":
            result = estimate_embedding_work(args.db, args.dimensions, args.limit)
        elif args.command == "embed":
            result = run_embed(args, state_db)
        elif args.command == "train":
            result = train_model(
                args.db, state_db, artifact_dir, args.min_training_labels,
            )
        elif args.command == "score":
            result = score_model(args.db, state_db, args.run_id)
        elif args.command == "evaluate":
            result = evaluate_model(
                args.db, state_db, artifact_dir, args.run_id, args.device,
                args.inference_config,
            )
        elif args.command == "promote":
            if not args.run_id:
                raise PreferenceModelError("promote requires --run-id")
            promote_run(
                state_db, artifact_dir, args.run_id, args.reason,
                args.projected_incremental_minutes, args.force,
            )
            result = {"champion_run_id": args.run_id, "action": "promote"}
        elif args.command == "rollback":
            if not args.run_id:
                raise PreferenceModelError("rollback requires --run-id")
            rollback_run(
                state_db, artifact_dir, args.run_id,
                args.reason or "operator rollback", args.force,
            )
            result = {"champion_run_id": args.run_id, "action": "rollback"}
        else:
            if args.retrain_every_labels < 1:
                raise PreferenceModelError("--retrain-every-labels must be positive")
            result = refresh_pipeline(args, state_db, artifact_dir)
    except InferenceTransportError as exc:
        if getattr(exc, "defer_without_attempt", False):
            print(json.dumps({"type": "inference_usage_deferred", "reason": exc.reason_code, "retry_at": exc.retry_at}), file=sys.stderr)
            return 76
        print(f"error: {exc}", file=sys.stderr)
        # EX_TEMPFAIL for a positively terminal/retryable remote job; EX_CONFIG for
        # ambiguous or invalid work that must not be resubmitted by the outer queue.
        return 75 if exc.retryable else 78
    except (PreferenceModelError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
