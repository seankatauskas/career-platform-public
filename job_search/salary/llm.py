#!/usr/bin/env python3
"""Durable salary inference for jobs without usable native compensation.

NuExtract on local MLX remains the default. An explicitly selected portable inference
profile can run the same validated extraction contract on a hosted model.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from job_search.inference import (
    InferenceTransportError,
    StructuredGenerationProvider,
    build_structured_provider,
    configured_inference_path,
    load_inference_config,
)

from job_search.salary.enrichment import (
    PARSER_VERSION,
    has_usable_native_compensation,
    llm_result_ranges,
    prepare_enrichment,
    refresh_enrichment_summary,
    save_source_ranges,
)
from job_search.salary.model_contract import (
    MAX_INPUT_TOKENS,
    MODEL_NAME,
    PROMPT,
    PROMPT_VERSION,
    RESULT_SCHEMA,
    TEMPLATE,
    chunk_text,
    normalize_result,
    validate_result,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "job-boards.db"
LLM_SOURCE = {"llm_description"}
TERMINAL_STATUSES = {
    "complete", "no_compensation", "needs_review", "failed", "skipped_empty",
    "skipped_closed", "superseded",
}
STALE_MINUTES = 30
MAX_ATTEMPTS = 3
MEASURED_SECONDS_PER_JOB = 3.03

DDL = """
CREATE TABLE IF NOT EXISTS salary_llm_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS salary_llm_queue (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ats                TEXT NOT NULL,
    job_id             TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    status             TEXT NOT NULL,
    priority           INTEGER NOT NULL DEFAULT 0,
    attempts           INTEGER NOT NULL DEFAULT 0,
    queued_at          TEXT NOT NULL,
    started_at         TEXT,
    completed_at       TEXT,
    updated_at         TEXT NOT NULL,
    next_attempt_at    TEXT,
    lease_token        TEXT,
    model              TEXT NOT NULL DEFAULT '',
    raw_output         TEXT,
    result_json        TEXT,
    validation_json    TEXT NOT NULL DEFAULT '[]',
    normalization_json TEXT NOT NULL DEFAULT '[]',
    usage_json         TEXT NOT NULL DEFAULT '{}',
    latency_seconds    REAL,
    error              TEXT NOT NULL DEFAULT '',
    UNIQUE (ats, job_id, source_fingerprint, prompt_version),
    FOREIGN KEY (ats, job_id) REFERENCES jobs(ats, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS salary_llm_queue_status
ON salary_llm_queue(status, priority DESC, queued_at, id);
CREATE INDEX IF NOT EXISTS salary_llm_queue_job
ON salary_llm_queue(ats, job_id, updated_at);

CREATE TABLE IF NOT EXISTS salary_llm_worker_lease (
    singleton    INTEGER PRIMARY KEY CHECK (singleton=1),
    owner        TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def prepare_schema(con: sqlite3.Connection) -> None:
    prepare_enrichment(con)
    con.executescript(DDL)
    columns = {row[1] for row in con.execute("PRAGMA table_info(salary_llm_queue)")}
    if "usage_json" not in columns:
        con.execute("ALTER TABLE salary_llm_queue ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'")


def _setting(con: sqlite3.Connection, key: str) -> str | None:
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='salary_llm_settings'"
    ).fetchone():
        return None
    row = con.execute("SELECT value FROM salary_llm_settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def is_activated(con: sqlite3.Connection) -> bool:
    return _setting(con, "activated_at") is not None


def activate(db: Path, no_backfill: bool) -> dict[str, Any]:
    if not no_backfill:
        raise ValueError("activation requires --no-backfill")
    if not db.exists():
        raise ValueError(f"database does not exist: {db}")
    when = now()
    with _connect(db) as con:
        prepare_schema(con)
        existing = _setting(con, "activated_at")
        if existing:
            return {
                "activated_at": existing, "already_active": True,
                "retired_description_ranges": 0, "queued": 0,
            }
        affected = list(con.execute(
            "SELECT DISTINCT ats,job_id FROM job_compensation_ranges "
            "WHERE removed_at IS NULL AND source_type='description_rule'"
        ))
        retired = con.execute(
            "UPDATE job_compensation_ranges SET removed_at=? "
            "WHERE removed_at IS NULL AND source_type='description_rule'",
            (when,),
        ).rowcount
        for ats, job_id in affected:
            refresh_enrichment_summary(con, ats, job_id)
        con.execute(
            "INSERT INTO salary_llm_settings VALUES ('activated_at',?,?)", (when, when)
        )
        con.execute(
            "INSERT INTO salary_llm_settings VALUES ('prompt_version',?,?)",
            (PROMPT_VERSION, when),
        )
    return {
        "activated_at": when, "already_active": False,
        "retired_description_ranges": retired, "affected_jobs": len(affected),
        "queued": 0,
    }


def _retire_llm_ranges(
    con: sqlite3.Connection, ats: str, job_id: str, observed_at: str
) -> int:
    changed = con.execute(
        "UPDATE job_compensation_ranges SET removed_at=COALESCE(removed_at, ?) "
        "WHERE ats=? AND job_id=? AND source_type='llm_description' "
        "AND removed_at IS NULL",
        (observed_at, ats, job_id),
    ).rowcount
    if changed:
        refresh_enrichment_summary(con, ats, job_id)
    return changed


def sync_scan_enrichments(
    con: sqlite3.Connection,
    enrichments: list[dict[str, Any]],
    new_keys: set[tuple[str, str]],
    observed_at: str,
) -> dict[str, int]:
    """Queue only post-activation new/changed jobs; never seed the baseline."""
    if not is_activated(con):
        return {"queued": 0, "superseded": 0, "skipped_empty": 0}
    counts = {"queued": 0, "superseded": 0, "skipped_empty": 0}
    for result in enrichments:
        ats, job_id = str(result["ats"]), str(result["job_id"])
        key = (ats, job_id)
        previous = con.execute(
            "SELECT source_fingerprint,parser_version FROM job_enrichment "
            "WHERE ats=? AND job_id=?",
            key,
        ).fetchone()
        # Rows written by the former deterministic parser may have a fingerprint
        # built from a less complete payload.  Treat their first scan under the
        # native-only parser as baseline synchronization, not as a changed job.
        # save_enrichments() writes PARSER_VERSION immediately after this hook,
        # so every later fingerprint change is actionable.
        baseline_transition = (
            key not in new_keys
            and (previous is None or previous[1] != PARSER_VERSION)
        )
        changed = (
            previous is not None
            and not baseline_transition
            and previous[0] != result["source_fingerprint"]
        )
        usable_native = has_usable_native_compensation(result)
        if usable_native or key in new_keys or changed:
            counts["superseded"] += con.execute(
                "UPDATE salary_llm_queue SET status='superseded',completed_at=?,updated_at=?,"
                "lease_token=NULL WHERE ats=? AND job_id=? "
                "AND status IN ('queued','processing','retryable')",
                (observed_at, observed_at, ats, job_id),
            ).rowcount
            _retire_llm_ranges(con, ats, job_id, observed_at)
        if usable_native or not (key in new_keys or changed):
            continue
        job = con.execute(
            "SELECT description FROM jobs WHERE ats=? AND id=?", key
        ).fetchone()
        description = str(job[0] or "") if job else ""
        status = "queued" if description.strip() else "skipped_empty"
        cursor = con.execute(
            "INSERT INTO salary_llm_queue "
            "(ats,job_id,source_fingerprint,prompt_version,status,priority,queued_at,updated_at) "
            "VALUES (?,?,?,?,?,0,?,?) "
            "ON CONFLICT(ats,job_id,source_fingerprint,prompt_version) DO UPDATE SET "
            "status=excluded.status,priority=excluded.priority,attempts=0,"
            "queued_at=excluded.queued_at,started_at=NULL,completed_at=NULL,"
            "updated_at=excluded.updated_at,next_attempt_at=NULL,lease_token=NULL,"
            "model='',raw_output=NULL,result_json=NULL,validation_json='[]',"
            "normalization_json='[]',usage_json='{}',latency_seconds=NULL,error=''",
            (ats, job_id, result["source_fingerprint"], PROMPT_VERSION,
             status, observed_at, observed_at),
        )
        if cursor.rowcount:
            counts["queued" if status == "queued" else "skipped_empty"] += 1
    return counts


def _acquire_worker(con: sqlite3.Connection, owner: str) -> None:
    current = datetime.now(timezone.utc)
    expires = (current + timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")
    stamp = current.isoformat(timespec="seconds")
    con.execute("BEGIN IMMEDIATE")
    row = con.execute("SELECT owner,expires_at FROM salary_llm_worker_lease WHERE singleton=1").fetchone()
    if row and row[1] > stamp:
        con.rollback()
        raise ValueError(f"another salary worker holds the lease: {row[0]}")
    con.execute(
        "INSERT INTO salary_llm_worker_lease VALUES (1,?,?,?,?) "
        "ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner,"
        "acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,"
        "expires_at=excluded.expires_at",
        (owner, stamp, stamp, expires),
    )
    stale_before = (current - timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")
    con.execute(
        "UPDATE salary_llm_queue SET status='retryable',lease_token=NULL,updated_at=?,"
        "error='recovered stale processing lease' "
        "WHERE status='processing' AND updated_at<?",
        (stamp, stale_before),
    )
    con.commit()


def _heartbeat(con: sqlite3.Connection, owner: str) -> None:
    stamp = now()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=STALE_MINUTES)).isoformat(
        timespec="seconds"
    )
    con.execute(
        "UPDATE salary_llm_worker_lease SET heartbeat_at=?,expires_at=? "
        "WHERE singleton=1 AND owner=?",
        (stamp, expires, owner),
    )
    con.commit()


def _release_worker(con: sqlite3.Connection, owner: str) -> None:
    con.execute("DELETE FROM salary_llm_worker_lease WHERE singleton=1 AND owner=?", (owner,))
    con.commit()


def _claim(con: sqlite3.Connection) -> sqlite3.Row | None:
    while True:
        stamp = now()
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT q.*,j.company,j.title,j.location,j.employmentType,j.description,j.closed_at,"
            "e.source_fingerprint AS current_fingerprint "
            "FROM salary_llm_queue q JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
            "LEFT JOIN job_enrichment e ON e.ats=q.ats AND e.job_id=q.job_id "
            "WHERE q.status IN ('queued','retryable') "
            "AND (q.next_attempt_at IS NULL OR q.next_attempt_at<=?) "
            "ORDER BY q.priority DESC,q.queued_at,q.id LIMIT 1",
            (stamp,),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        skip: str | None = None
        if row["closed_at"]:
            skip = "skipped_closed"
        elif not str(row["description"] or "").strip():
            skip = "skipped_empty"
        elif row["current_fingerprint"] != row["source_fingerprint"]:
            skip = "superseded"
        if skip:
            con.execute(
                "UPDATE salary_llm_queue SET status=?,completed_at=?,updated_at=? WHERE id=?",
                (skip, stamp, stamp, row["id"]),
            )
            con.commit()
            continue
        lease = uuid.uuid4().hex
        con.execute(
            "UPDATE salary_llm_queue SET status='processing',attempts=attempts+1,"
            "started_at=COALESCE(started_at,?),updated_at=?,lease_token=?,error='' WHERE id=?",
            (stamp, stamp, lease, row["id"]),
        )
        con.commit()
        return con.execute(
            "SELECT q.*,j.company,j.title,j.location,j.employmentType,j.description "
            "FROM salary_llm_queue q JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
            "WHERE q.id=?",
            (row["id"],),
        ).fetchone()


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1].strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        cleaned = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("model output did not contain a JSON object")


class LocalNuExtract:
    def __init__(self) -> None:
        try:
            from mlx_vlm import apply_chat_template, generate, load
            from job_search.salary.local_eval import MODELS, resolve_model_path
        except ImportError as exc:
            raise ValueError(
                "run with .venv-local-mlx/bin/python -m job_search.salary.llm"
            ) from exc
        config = MODELS[MODEL_NAME]
        model_path = resolve_model_path(config)
        if model_path is None:
            raise ValueError(f"model is not downloaded: {config['repo']}")
        self.model, self.processor = load(str(model_path))
        self.apply_chat_template = apply_chat_template
        self.generate = generate
        self.model_revision = f"{config['repo']}@{model_path.name}"
        self.provenance = {
            "provider": "local-mlx",
            "protocol": "in-process",
            "model": config["repo"],
            "model_revision": self.model_revision,
        }

    def count_tokens(self, text: str) -> int:
        tokenizer = getattr(self.processor, "tokenizer", self.processor)
        encoded = tokenizer.encode(text, add_special_tokens=False)
        return len(encoded)

    def __call__(self, job_text: str) -> tuple[str, dict[str, Any]]:
        messages = [{"role": "user", "content": job_text}]
        prompt = self.apply_chat_template(
            self.processor,
            self.model.config,
            messages,
            template=json.dumps(TEMPLATE, separators=(",", ":")),
            instructions=PROMPT,
            enable_thinking=False,
        )
        response = self.generate(
            self.model, self.processor, prompt,
            max_tokens=512, temperature=0.0, verbose=False,
        )
        return response.text, {
            "prompt_tokens": response.prompt_tokens,
            "generation_tokens": response.generation_tokens,
            "peak_memory_gb": response.peak_memory,
            "finish_reason": response.finish_reason,
            "inference_provenance": self.provenance,
        }


class HostedSalaryExtractor:
    """Adapt a structured-generation provider to the frozen salary contract."""

    def __init__(self, provider: StructuredGenerationProvider) -> None:
        self.provider = provider
        self.model_revision = str(provider.model_revision)
        self.max_input_tokens = int(provider.max_input_tokens)
        self._nuextract = provider.provenance.get("model") == "numind/NuExtract3"
        self._system_prompt = (
            PROMPT
            + "\nReturn only one JSON object matching this schema:\n"
            + json.dumps(RESULT_SCHEMA, ensure_ascii=False, separators=(",", ":"))
        )
        self.reserved_input_tokens = (
            provider.count_tokens_upper_bound(
                json.dumps({"template": json.dumps(TEMPLATE, separators=(",", ":")),
                            "instructions": PROMPT, "enable_thinking": False})
                if self._nuextract else json.dumps(self._system_prompt)
                + json.dumps(RESULT_SCHEMA)
            ) + 512 + 512
        )

    def count_tokens(self, text: str) -> int:
        # Queued OpenAI requests encode text as a JSON string. Escaped quotes,
        # backslashes and line breaks must fit too, before a job is submitted.
        return self.provider.count_tokens_upper_bound(json.dumps(text, ensure_ascii=False))

    def __call__(self, job_text: str) -> tuple[str, dict[str, Any]]:
        messages = [{"role": "user", "content": job_text}]
        options = {}
        if self._nuextract:
            options = {"extraction_template": TEMPLATE, "extraction_instructions": PROMPT}
        else:
            messages.insert(0, {"role": "system", "content": self._system_prompt})
        result = self.provider.generate(
            messages,
            json_schema=RESULT_SCHEMA,
            schema_name="salary_extraction",
            max_output_tokens=512,
            temperature=0.0,
            **options,
        )
        usage = dict(result.usage)
        usage["inference_provenance"] = dict(result.provenance)
        return result.text, usage


def _infer_job(
    row: sqlite3.Row,
    generator: Callable[[str], tuple[str, dict[str, Any]]],
    count_tokens: Callable[[str], int],
    max_input_tokens: int = MAX_INPUT_TOKENS,
    reserved_input_tokens: int = 0,
) -> tuple[dict[str, Any], list[str], list[str], str, dict[str, Any]]:
    description = str(row["description"] or "")
    prefix = (
        f"Company: {row['company']}\nTitle: {row['title']}\nLocation: {row['location']}\n"
        f"Employment type: {row['employmentType']}\n\nJOB POSTING\n"
    )
    description_budget = max_input_tokens - reserved_input_tokens - count_tokens(prefix)
    if description_budget < 1_000:
        raise ValueError("configured inference context leaves too little room for the posting")
    chunks = chunk_text(description, count_tokens, max_tokens=description_budget)
    all_ranges: list[Any] = []
    raw_outputs: list[str] = []
    usage: dict[str, Any] = {
        "chunks": len(chunks), "prompt_tokens": 0, "generation_tokens": 0,
    }
    normalizations: list[str] = []
    for chunk in chunks:
        raw, chunk_usage = generator(prefix + chunk)
        raw_outputs.append(raw)
        parsed = _json_object(raw)
        chunk_result, changes = normalize_result(parsed, description)
        normalizations.extend(changes)
        values = chunk_result.get("ranges")
        if not isinstance(values, list):
            raise ValueError("ranges is not an array")
        all_ranges.extend(values)
        for key in ("prompt_tokens", "generation_tokens"):
            usage[key] += float(chunk_usage.get(key) or 0)
        if chunk_usage.get("peak_memory_gb") is not None:
            usage["peak_memory_gb"] = max(
                usage.get("peak_memory_gb", 0), float(chunk_usage["peak_memory_gb"])
            )
        provenance = chunk_usage.get("inference_provenance")
        if provenance is not None:
            if "inference_provenance" in usage and usage["inference_provenance"] != provenance:
                raise ValueError("inference provider provenance changed between chunks")
            usage["inference_provenance"] = provenance
    unique_ranges: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    for item in all_ranges:
        if not isinstance(item, dict):
            unique_ranges.append(item)
            continue
        key = tuple(item.get(field) for field in (
            "currency", "period", "min_value", "max_value"
        ))
        if key not in seen:
            seen.add(key)
            unique_ranges.append(item)
    result, final_changes = normalize_result({"ranges": unique_ranges}, description)
    normalizations.extend(final_changes)
    validation = validate_result(result, description)
    return result, validation, normalizations, json.dumps(raw_outputs, ensure_ascii=False), usage


def _finish_success(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    result: dict[str, Any],
    validation: list[str],
    normalizations: list[str],
    raw_output: str,
    usage: dict[str, Any],
    elapsed: float,
    model_revision: str = MODEL_NAME,
) -> bool:
    stamp = now()
    con.execute("BEGIN IMMEDIATE")
    current = con.execute(
        "SELECT status,lease_token FROM salary_llm_queue WHERE id=?", (row["id"],)
    ).fetchone()
    if not current or current[0] != "processing" or current[1] != row["lease_token"]:
        con.rollback()
        return False
    status = "needs_review" if validation else (
        "complete" if result.get("ranges") else "no_compensation"
    )
    if not validation:
        ranges = llm_result_ranges(result)
        save_source_ranges(
            con, row["ats"], row["job_id"], ranges, LLM_SOURCE, stamp
        )
        refresh_enrichment_summary(con, row["ats"], row["job_id"])
    con.execute(
        "UPDATE salary_llm_queue SET status=?,completed_at=?,updated_at=?,lease_token=NULL,"
        "model=?,raw_output=?,result_json=?,validation_json=?,normalization_json=?,"
        "usage_json=?,latency_seconds=?,error='' WHERE id=?",
        (
            status, stamp, stamp, model_revision, raw_output,
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            json.dumps(validation, ensure_ascii=False),
            json.dumps(normalizations, ensure_ascii=False),
            json.dumps(usage, ensure_ascii=False), elapsed, row["id"],
        ),
    )
    con.commit()
    return True


def _finish_error(con: sqlite3.Connection, row: sqlite3.Row, exc: Exception) -> str:
    stamp = now()
    attempts = int(row["attempts"])
    if getattr(exc, "defer_without_attempt", False):
        con.execute("UPDATE salary_llm_queue SET status='retryable',attempts=MAX(0,attempts-1),updated_at=?,next_attempt_at=?,lease_token=NULL,error=? WHERE id=? AND status='processing' AND lease_token=?",
                    (stamp, exc.retry_at, exc.reason_code, row["id"], row["lease_token"]))
        con.commit()
        raise exc
    nonretryable_transport = (
        isinstance(exc, InferenceTransportError) and not exc.retryable
    )
    status = "failed" if attempts >= MAX_ATTEMPTS or nonretryable_transport else "retryable"
    retry_at = None if status == "failed" else (
        datetime.now(timezone.utc) + timedelta(seconds=30 * 2 ** (attempts - 1))
    ).isoformat(timespec="seconds")
    con.execute(
        "UPDATE salary_llm_queue SET status=?,updated_at=?,completed_at=?,next_attempt_at=?,"
        "lease_token=NULL,error=? WHERE id=? AND status='processing' AND lease_token=?",
        (
            status, stamp, stamp if status == "failed" else None, retry_at,
            f"{type(exc).__name__}: {exc}"[:2000], row["id"], row["lease_token"],
        ),
    )
    con.commit()
    return status


def run_worker(
    db: Path,
    limit: int | None = 100,
    generator: Callable[[str], tuple[str, dict[str, Any]]] | None = None,
    count_tokens: Callable[[str], int] | None = None,
    inference_config: Path | None = None,
    inference_provider: StructuredGenerationProvider | None = None,
) -> dict[str, Any]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if generator is not None and (inference_config is not None or inference_provider is not None):
        raise ValueError("generator and remote inference provider are mutually exclusive")
    if inference_config is not None and inference_provider is not None:
        raise ValueError("inference_config and inference_provider are mutually exclusive")
    hosted_model: HostedSalaryExtractor | None = None
    if inference_config is not None:
        inference_provider = build_structured_provider(load_inference_config(inference_config))
    if inference_provider is not None:
        hosted_model = HostedSalaryExtractor(inference_provider)
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    processed = errors = review = 0
    started = time.perf_counter()
    with _connect(db) as con:
        prepare_schema(con)
        if not is_activated(con):
            raise ValueError("salary LLM pipeline is not active; run activate --no-backfill")
        _acquire_worker(con, owner)
        local_model: LocalNuExtract | None = None
        try:
            while limit is None or processed < limit:
                row = _claim(con)
                if row is None:
                    break
                if hosted_model is not None:
                    active_generator = hosted_model
                    active_counter = hosted_model.count_tokens
                    active_limit = hosted_model.max_input_tokens
                    reserved_tokens = hosted_model.reserved_input_tokens
                elif generator is None:
                    if local_model is None:
                        try:
                            local_model = LocalNuExtract()
                        except Exception as exc:
                            _finish_error(con, row, exc)
                            errors += 1
                            processed += 1
                            raise
                    active_generator = local_model
                    active_counter = local_model.count_tokens
                    active_limit = MAX_INPUT_TOKENS
                    reserved_tokens = 0
                else:
                    active_generator = generator
                    active_counter = count_tokens or (lambda text: max(1, len(text) // 4))
                    active_limit = MAX_INPUT_TOKENS
                    reserved_tokens = 0
                item_started = time.perf_counter()
                try:
                    result, validation, normalizations, raw, usage = _infer_job(
                        row, active_generator, active_counter,
                        active_limit, reserved_tokens,
                    )
                    saved = _finish_success(
                        con, row, result, validation, normalizations, raw, usage,
                        time.perf_counter() - item_started,
                        str(getattr(active_generator, "model_revision", MODEL_NAME)),
                    )
                    state = "superseded" if not saved else (
                        "needs_review" if validation else (
                            "complete" if result.get("ranges") else "no_compensation"
                        )
                    )
                    review += bool(validation)
                except Exception as exc:
                    state = _finish_error(con, row, exc)
                    errors += 1
                processed += 1
                _heartbeat(con, owner)
                print(
                    f"{processed} queue={row['id']} {row['ats']}/{row['job_id']} {state}",
                    flush=True,
                )
        finally:
            _release_worker(con, owner)
    return {
        "processed": processed, "errors": errors, "needs_review": review,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "remaining": status(db)["actionable"],
    }


def status(db: Path) -> dict[str, Any]:
    with _connect(db) as con:
        prepare_schema(con)
        activated = _setting(con, "activated_at")
        counts = {
            row[0]: row[1] for row in con.execute(
                "SELECT status,COUNT(*) FROM salary_llm_queue GROUP BY status"
            )
        }
        actionable = con.execute(
            "SELECT COUNT(*) FROM salary_llm_queue WHERE status IN ('queued','retryable')"
        ).fetchone()[0]
        oldest = con.execute(
            "SELECT MIN(queued_at) FROM salary_llm_queue "
            "WHERE status IN ('queued','retryable','processing')"
        ).fetchone()[0]
    oldest_hours = None
    if oldest:
        try:
            oldest_hours = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds()
                / 3600,
                3,
            )
        except ValueError:
            oldest_hours = None
    return {
        "activated_at": activated,
        "counts": counts,
        "actionable": actionable,
        "oldest_queue_hours": oldest_hours,
        "queue_age_warning": bool(oldest_hours is not None and oldest_hours >= 1),
        "estimated_seconds": round(actionable * MEASURED_SECONDS_PER_JOB, 2),
    }


def retry_failed(db: Path) -> int:
    stamp = now()
    with _connect(db) as con:
        prepare_schema(con)
        changed = con.execute(
            "UPDATE salary_llm_queue SET status='queued',attempts=0,next_attempt_at=NULL,"
            "completed_at=NULL,updated_at=?,error='' WHERE status='failed'",
            (stamp,),
        ).rowcount
    return changed


def _db_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    commands = parser.add_subparsers(dest="command", required=True)
    activation = commands.add_parser("activate")
    activation.add_argument("--no-backfill", action="store_true", required=True)
    commands.add_parser("status")
    run = commands.add_parser("run")
    run.add_argument(
        "--inference-config", type=Path,
        help=(
            "owner-only portable inference profile; defaults to "
            "JOB_SEARCH_INFERENCE_CONFIG, then local MLX"
        ),
    )
    group = run.add_mutually_exclusive_group()
    group.add_argument("--limit", type=int, default=100)
    group.add_argument("--until-empty", action="store_true")
    retry = commands.add_parser("retry")
    retry.add_argument("--failed", action="store_true", required=True)
    args = parser.parse_args()
    db = _db_path(args.db)
    try:
        if args.command == "activate":
            output = activate(db, args.no_backfill)
        elif args.command == "status":
            output = status(db)
        elif args.command == "retry":
            output = {"retried": retry_failed(db)}
        else:
            output = run_worker(
                db,
                None if args.until_empty else args.limit,
                inference_config=configured_inference_path(args.inference_config),
            )
        print(json.dumps(output, indent=2))
    except InferenceTransportError as exc:
        if getattr(exc, "defer_without_attempt", False):
            print(json.dumps({"type": "inference_usage_deferred", "reason": exc.reason_code, "retry_at": exc.retry_at}), file=sys.stderr)
            raise SystemExit(76)
        raise SystemExit(75 if exc.retryable else 78)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
