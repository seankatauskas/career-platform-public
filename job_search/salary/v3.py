#!/usr/bin/env python3
"""Versioned salary-labeling pipeline with one-sided bound support.

This module deliberately uses its own tables.  The v2 queue, predictions, and 500
human labels remain immutable comparison data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from job_search.salary.enrichment import html_to_text
from job_search.salary.teacher import (
    PROVIDERS,
    ProviderError,
    ResponseDecodeError,
    _classify_http_error,
    _estimate_tokens,
    _extract_response,
    connect,
    cost_usd,
    now,
    post_json,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "job-boards.db"
DEFAULT_SEED = 20260831
SCHEMA_VERSION = "cash-pay-bounds-v3"
PROMPT_VERSION = "primary-cash-bounds-v3"
DEFAULT_BATCH = "targeted-v3-20260831"
AUDIT_BATCH = "v2-one-sided-audit"

TARGETS = {
    "minimum": 200,
    "maximum": 150,
    "exact": 50,
    "range": 50,
    "hard_negative": 50,
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ranges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "value_kind": {
                        "type": "string",
                        "enum": ["exact", "range", "minimum", "maximum"],
                    },
                    "currency": {"type": "string"},
                    "period": {"type": "string", "enum": ["year", "hour"]},
                    "min_value": {"type": ["number", "null"]},
                    "max_value": {"type": ["number", "null"]},
                    "evidence_text": {"type": "string"},
                },
                "required": [
                    "value_kind", "currency", "period", "min_value", "max_value",
                    "evidence_text",
                ],
            },
        },
    },
    "required": ["ranges"],
}

SYSTEM_PROMPT = """You extract explicit primary cash pay from one job posting into the supplied JSON schema.

Rules:
- Extract every explicit annual or hourly primary cash-pay amount offered for this job,
  regardless of currency. Never guess, annualize, or convert units or currencies.
- Treat base salary, base pay, OTE, target cash compensation, and total cash
  compensation as equivalent for this task. Do not try to classify among them.
- Preserve each distinct published amount, including multiple geographic pay zones.
- Use value_kind=range for a complete lower-to-upper range and populate both bounds.
- Use value_kind=exact for one fixed amount and put it in both bounds.
- Use value_kind=minimum for phrases such as "starts at", "from", or "at least";
  populate min_value and set max_value to null.
- Use value_kind=maximum for phrases such as "up to", "maximum", or "not to exceed";
  set min_value to null and populate max_value.
- A plus sign after an amount (for example "$200k+") means minimum, not exact.
- Do not turn a minimum or maximum into an exact amount. Do not infer an unstated bound.
- Infer year only when normal job-posting language makes an unsuffixed five- or
  six-figure salary/OTE amount unambiguous. Otherwise do not infer a period.
- Use an uppercase ISO currency code such as USD, CAD, EUR, GBP, PLN, or INR when
  explicit or unambiguous from the symbol and applicable location. Use UNKNOWN only
  when the currency genuinely cannot be determined.
- Ignore benefits, fundraising, revenue, budgets, years of experience, and other
  numbers that are not applicant compensation.
- Exclude bonus-only, equity, stock values, stipends, and commission-only amounts.
- Omit monthly, weekly, daily, per-project, and one-time amounts; do not convert them.
- evidence_text must be a short exact quote copied from the posting for every item.
- Return an empty ranges array when no qualifying annual or hourly cash pay is present.
"""

DDL = """
CREATE TABLE IF NOT EXISTS salary_v3_queue (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    batch              TEXT NOT NULL,
    ats                TEXT NOT NULL,
    job_id             TEXT NOT NULL,
    position           INTEGER NOT NULL,
    candidate_kind     TEXT NOT NULL,
    sampling_weight    REAL NOT NULL,
    seed               INTEGER NOT NULL,
    split              TEXT NOT NULL CHECK (split IN ('audit','training','calibration','evaluation')),
    legacy_result_json TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (batch, ats, job_id),
    UNIQUE (batch, position)
);
CREATE INDEX IF NOT EXISTS salary_v3_queue_split
ON salary_v3_queue(batch, split, position);

CREATE TABLE IF NOT EXISTS salary_v3_exclusions (
    ats         TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    excluded_at TEXT NOT NULL,
    PRIMARY KEY (ats, job_id)
);

CREATE TABLE IF NOT EXISTS salary_v3_predictions (
    queue_id          INTEGER NOT NULL REFERENCES salary_v3_queue(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    schema_version    TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    status            TEXT NOT NULL,
    result_json       TEXT,
    validation_json   TEXT NOT NULL DEFAULT '[]',
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cost_usd          REAL,
    response_id       TEXT,
    response_meta_json TEXT NOT NULL DEFAULT '{}',
    attempts          INTEGER NOT NULL DEFAULT 0,
    error_code        TEXT,
    error             TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    PRIMARY KEY (queue_id, provider)
);
CREATE INDEX IF NOT EXISTS salary_v3_predictions_status
ON salary_v3_predictions(provider, status);

CREATE TABLE IF NOT EXISTS salary_v3_gold_labels (
    queue_id      INTEGER PRIMARY KEY REFERENCES salary_v3_queue(id) ON DELETE CASCADE,
    result_json   TEXT NOT NULL,
    chosen_source TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    reviewed_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS salary_v3_gold_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id               INTEGER NOT NULL,
    previous_result_json   TEXT,
    previous_chosen_source TEXT,
    previous_note          TEXT,
    happened_at            TEXT NOT NULL
);
"""


def prepare_schema(db_path: Path) -> None:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    with connect(db_path) as con:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone():
            raise ValueError("database does not contain the jobs table")
        con.executescript(DDL)


def _normalized(value: str) -> str:
    rendered = " ".join(html_to_text(value).split()).casefold()
    rendered = re.sub(r"\s*([-\u2013\u2014])\s*", r"\1", rendered)
    # Removing inline HTML such as ``<strong>Zone 1</strong>:`` may introduce an
    # artificial space before punctuation. It is not a difference in the quoted text.
    rendered = re.sub(r"\s+([:;,])", r"\1", rendered)
    # Some ATS HTML wraps individual digits in separate tags, rendering $150,000 as
    # "$1 5 0,000" after safe tag removal. Compare the human-visible number instead.
    rendered = re.sub(r"(?<=\d)\s+(?=\d)", "", rendered)
    rendered = re.sub(r"(?<=,)\s+(?=\d{3}\b)", "", rendered)
    return re.sub(r"(?<=[$€£₹])\s+(?=\d)", "", rendered)


def validate_result(result: dict[str, Any], description: str) -> list[str]:
    problems: list[str] = []
    ranges = result.get("ranges")
    if not isinstance(ranges, list):
        return ["ranges is not an array"]
    posting = _normalized(description)
    raw_posting = " ".join(str(description or "").split()).casefold()
    expected_bounds = {
        "exact": (True, True),
        "range": (True, True),
        "minimum": (True, False),
        "maximum": (False, True),
    }
    for index, item in enumerate(ranges, 1):
        if not isinstance(item, dict):
            problems.append(f"range {index} is not an object")
            continue
        kind = item.get("value_kind")
        if kind not in expected_bounds:
            problems.append(f"range {index} has invalid value_kind")
            continue
        minimum, maximum = item.get("min_value"), item.get("max_value")
        has_min, has_max = minimum is not None, maximum is not None
        if (has_min, has_max) != expected_bounds[kind]:
            problems.append(f"range {index} bounds do not match value_kind={kind}")
        if kind == "exact" and has_min and has_max and minimum != maximum:
            problems.append(f"range {index} exact values do not match")
        if kind == "range" and has_min and has_max and minimum >= maximum:
            problems.append(f"range {index} range must have min_value < max_value")
        if has_min and not isinstance(minimum, (int, float)):
            problems.append(f"range {index} min_value is not numeric or null")
        if has_max and not isinstance(maximum, (int, float)):
            problems.append(f"range {index} max_value is not numeric or null")
        if not isinstance(item.get("currency"), str) or not item["currency"].strip():
            problems.append(f"range {index} has no currency")
        if item.get("period") not in {"year", "hour"}:
            problems.append(f"range {index} period is not year or hour")
        evidence = str(item.get("evidence_text") or "")
        if not evidence:
            problems.append(f"range {index} has no evidence")
        elif _normalized(evidence) not in posting and _normalized(evidence) not in raw_posting:
            problems.append(f"range {index} evidence is not an exact posting quote")
    return problems


_AMOUNT = r"(?:(?:US\s*)?\$|USD\s*)\s*\d[\d,]*(?:\.\d+)?\s*[kKmM]?"
_PERIOD = r"(?:/\s*(?:hr|hour|yr|year)|per\s+(?:hour|year)|hourly|annually|annual)"
_LOWER = re.compile(
    rf"\b(?:starting(?:\s+(?:salary|pay))?\s+at|starts?\s+at|at\s+least|"
    rf"minimum(?:\s+(?:salary|pay|compensation))?(?:\s+of)?|from)\s*{_AMOUNT}", re.I,
)
_LOWER_PLUS = re.compile(rf"{_AMOUNT}\s*\+", re.I)
_UPPER = re.compile(
    rf"\b(?:up\s+to|maximum(?:\s+(?:salary|pay|compensation|rate))?(?:\s+of)?|"
    rf"max(?:imum)?\.?)\s*{_AMOUNT}", re.I,
)
_RANGE = re.compile(
    rf"{_AMOUNT}\s*(?:-|\u2013|\u2014|to|through)\s*{_AMOUNT}", re.I,
)
_EXACT = re.compile(rf"{_AMOUNT}(?:\s*(?:\+\s*)?(?:a\s+)?{_PERIOD})", re.I)
_PAY_WORD = re.compile(r"\b(?:salary|compensation|base pay|base salary|pay range|ote|wage)\b", re.I)
_OTHER_CURRENCY = re.compile(r"\b(?:CAD|AUD|NZD|SGD)\b|[€£₹]|\b(?:EUR|GBP|INR|PLN)\b", re.I)
_US_MARKER = re.compile(
    r"\b(?:united states|u\.s\.|usa|us-based|remote[- ]us|new york|california|texas|"
    r"washington|massachusetts|illinois|colorado|florida|georgia|virginia|oregon|"
    r"pennsylvania|north carolina|san francisco|los angeles|chicago|boston|seattle|"
    r"austin|denver|atlanta)\b", re.I,
)


def candidate_kind(description: str) -> str:
    """High-recall sampling hint only; it is never used as a label."""
    # Sampling scans a very large corpus.  Stripping tags is sufficient for regex
    # hints and avoids fully parsing hundreds of thousands of HTML documents.
    text = " ".join(re.sub(r"<[^>]+>", " ", str(description or "")).split())
    lower = bool(_LOWER.search(text) or _LOWER_PLUS.search(text))
    upper = bool(_UPPER.search(text))
    if lower and not upper:
        return "minimum"
    if upper and not lower:
        return "maximum"
    if _RANGE.search(text):
        return "range"
    if _EXACT.search(text):
        return "exact"
    return "hard_negative"


def _us_focused(location: str, description: str) -> bool:
    combined = f"{location}\n{description}"
    if _US_MARKER.search(combined):
        return True
    # Dollar-denominated remote postings without an explicit foreign-currency marker
    # are useful high-precision US candidates even when location is only "Remote".
    return bool("$" in combined and not _OTHER_CURRENCY.search(combined))


def _rank(seed: int, *values: str) -> str:
    return hashlib.sha256("|".join((str(seed), *values)).encode()).hexdigest()


def _select_balanced(
    candidates: dict[str, list[dict[str, Any]]], targets: dict[str, int],
    seed: int, company_cap: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    company_counts: Counter[str] = Counter()
    selected_jobs: set[tuple[str, str]] = set()
    for kind, target in targets.items():
        pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in candidates.get(kind, []):
            pools[item["ats"]].append(item)
        for ats, rows in pools.items():
            rows.sort(key=lambda item: _rank(seed, kind, ats, item["job_id"]))
        ats_order = sorted(pools, key=lambda ats: _rank(seed, kind, ats))
        chosen: list[dict[str, Any]] = []
        while len(chosen) < target:
            progressed = False
            for ats in ats_order:
                pool = pools[ats]
                while pool:
                    item = pool.pop(0)
                    job_key = (item["ats"], item["job_id"])
                    if job_key in selected_jobs:
                        continue
                    company_key = str(item["company"] or "").casefold()
                    if company_counts[company_key] >= company_cap:
                        continue
                    company_counts[company_key] += 1
                    selected_jobs.add(job_key)
                    chosen.append(item)
                    progressed = True
                    break
                if len(chosen) == target:
                    break
            if not progressed:
                raise ValueError(f"only found {len(chosen)} of {target} candidates for {kind}")
        population = len(candidates.get(kind, []))
        for item in chosen:
            item["sampling_weight"] = population / len(chosen)
        selected.extend(chosen)
    return selected


def build_targeted_sample(
    db_path: Path, batch: str = DEFAULT_BATCH, seed: int = DEFAULT_SEED,
    company_cap: int = 3, fresh: bool = False,
) -> dict[str, Any]:
    if company_cap < 1:
        raise ValueError("company cap must be positive")
    prepare_schema(db_path)
    with connect(db_path) as con:
        existing = int(con.execute(
            "SELECT COUNT(*) FROM salary_v3_queue WHERE batch=?", (batch,)
        ).fetchone()[0])
        if existing and not fresh:
            return sample_report(con, batch)
        if existing and fresh:
            reviewed = int(con.execute(
                "SELECT COUNT(*) FROM salary_v3_gold_labels g JOIN salary_v3_queue q "
                "ON q.id=g.queue_id WHERE q.batch=?", (batch,),
            ).fetchone()[0])
            predicted = int(con.execute(
                "SELECT COUNT(*) FROM salary_v3_predictions p JOIN salary_v3_queue q "
                "ON q.id=p.queue_id WHERE q.batch=?", (batch,),
            ).fetchone()[0])
            if reviewed or predicted:
                raise ValueError("refusing --fresh because this v3 batch has predictions or labels")
            con.execute("DELETE FROM salary_v3_queue WHERE batch=?", (batch,))

        existing_tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        exclusion_queries = [
            "SELECT ats,job_id FROM salary_v3_queue",
            "SELECT ats,job_id FROM salary_v3_exclusions",
        ]
        if "salary_labeling_queue" in existing_tables:
            exclusion_queries.append("SELECT ats,job_id FROM salary_labeling_queue")
        if "salary_sample_history" in existing_tables:
            exclusion_queries.append("SELECT ats,job_id FROM salary_sample_history")
        excluded = {
            (row[0], row[1]) for row in con.execute(" UNION ".join(exclusion_queries))
        }
        candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)

        def collect(query: str, intended_kind: str) -> None:
            seen = {(item["ats"], item["job_id"]) for item in candidates[intended_kind]}
            for row in con.execute(query):
                item = dict(row)
                key = (item["ats"], item["job_id"])
                if key in excluded or key in seen:
                    continue
                if not _us_focused(item["location"] or "", item["description"]):
                    continue
                item.pop("sampling_text", None)
                item["candidate_kind"] = intended_kind
                del item["description"]
                del item["location"]
                candidates[intended_kind].append(item)
                seen.add(key)

        job_columns = "j.ats,j.id AS job_id,j.company,j.location,j.description "
        # One-sided candidates are intentionally over-selected with cheap SQL and
        # then checked by the stricter regex sampler.  This avoids materializing the
        # full multi-gigabyte description corpus in Python.
        for kind in ("minimum", "maximum", "exact", "range"):
            collect(
                "SELECT DISTINCT " + job_columns + ",r.evidence_text AS sampling_text "
                "FROM jobs j JOIN job_compensation_ranges r "
                "ON r.ats=j.ats AND r.job_id=j.id WHERE r.removed_at IS NULL AND "
                f"r.value_kind='{kind}' AND r.period IN ('year','hour')",
                kind,
            )
        for ats in ("ashby", "greenhouse", "lever"):
            collect(
                "SELECT " + job_columns + ",'' AS sampling_text FROM job_enrichment e "
                "JOIN jobs j ON j.ats=e.ats AND j.id=e.job_id WHERE e.range_count=0 "
                f"AND e.ats='{ats}' LIMIT 10000",
                "hard_negative",
            )

        selected = _select_balanced(candidates, TARGETS, seed, company_cap)
        selected.sort(key=lambda item: _rank(seed, "queue", item["ats"], item["job_id"]))
        created = now()
        rows = []
        for position, item in enumerate(selected, 1):
            split = "training" if position <= 300 else "calibration" if position <= 400 else "evaluation"
            rows.append((
                batch, item["ats"], item["job_id"], position, item["candidate_kind"],
                item["sampling_weight"], seed, split, None, created,
            ))
        con.executemany(
            "INSERT INTO salary_v3_queue "
            "(batch,ats,job_id,position,candidate_kind,sampling_weight,seed,split,"
            "legacy_result_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", rows,
        )
        return sample_report(con, batch)


def _has_one_sided_language(description: str) -> bool:
    text = " ".join(re.sub(r"<[^>]+>", " ", str(description or "")).split())
    return bool(_LOWER.search(text) or _LOWER_PLUS.search(text) or _UPPER.search(text))


def build_v2_audit(db_path: Path, fresh: bool = False) -> dict[str, Any]:
    """Copy v2 jobs with one-sided language into a non-destructive v3 audit batch."""
    prepare_schema(db_path)
    with connect(db_path) as con:
        existing = int(con.execute(
            "SELECT COUNT(*) FROM salary_v3_queue WHERE batch=?", (AUDIT_BATCH,)
        ).fetchone()[0])
        if existing and not fresh:
            return sample_report(con, AUDIT_BATCH)
        if existing and fresh:
            reviewed = int(con.execute(
                "SELECT COUNT(*) FROM salary_v3_gold_labels g JOIN salary_v3_queue q "
                "ON q.id=g.queue_id WHERE q.batch=?", (AUDIT_BATCH,),
            ).fetchone()[0])
            if reviewed:
                raise ValueError("refusing --fresh because the audit has v3 labels")
            con.execute("DELETE FROM salary_v3_predictions WHERE queue_id IN "
                        "(SELECT id FROM salary_v3_queue WHERE batch=?)", (AUDIT_BATCH,))
            con.execute("DELETE FROM salary_v3_queue WHERE batch=?", (AUDIT_BATCH,))
        source = list(con.execute(
            "SELECT q.ats,q.job_id,q.position,g.result_json,j.description "
            "FROM salary_labeling_queue q JOIN salary_gold_labels g ON g.queue_id=q.id "
            "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id ORDER BY q.position"
        ))
        selected = [row for row in source if _has_one_sided_language(row["description"])]
        created = now()
        con.executemany(
            "INSERT INTO salary_v3_queue "
            "(batch,ats,job_id,position,candidate_kind,sampling_weight,seed,split,"
            "legacy_result_json,created_at) VALUES (?,?,?,?,?,1,?,'audit',?,?)",
            [
                (
                    AUDIT_BATCH, row["ats"], row["job_id"], position,
                    candidate_kind(row["description"]), DEFAULT_SEED,
                    row["result_json"], created,
                )
                for position, row in enumerate(selected, 1)
            ],
        )
        return sample_report(con, AUDIT_BATCH)


def sample_report(con: sqlite3.Connection, batch: str) -> dict[str, Any]:
    return {
        "batch": batch,
        "queued": int(con.execute(
            "SELECT COUNT(*) FROM salary_v3_queue WHERE batch=?", (batch,)
        ).fetchone()[0]),
        "candidate_kinds": dict(con.execute(
            "SELECT candidate_kind,COUNT(*) FROM salary_v3_queue WHERE batch=? "
            "GROUP BY candidate_kind ORDER BY candidate_kind", (batch,),
        ).fetchall()),
        "splits": dict(con.execute(
            "SELECT split,COUNT(*) FROM salary_v3_queue WHERE batch=? GROUP BY split", (batch,),
        ).fetchall()),
        "ats": dict(con.execute(
            "SELECT ats,COUNT(*) FROM salary_v3_queue WHERE batch=? GROUP BY ats", (batch,),
        ).fetchall()),
        "companies": int(con.execute(
            "SELECT COUNT(DISTINCT lower(j.company)) FROM salary_v3_queue q JOIN jobs j "
            "ON j.ats=q.ats AND j.id=q.job_id WHERE q.batch=?", (batch,),
        ).fetchone()[0]),
    }


def _job_prompt(row: sqlite3.Row) -> str:
    return (
        "Extract compensation from this job. The response must match the JSON schema.\n\n"
        f"ATS: {row['ats']}\nCompany: {row['company']}\nTitle: {row['title']}\n"
        f"Location: {row['location']}\nEmployment type: {row['employmentType']}\n\n"
        f"JOB POSTING\n{row['description']}"
    )


def _request_body(provider: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
    config = PROVIDERS[provider]
    if provider == "fireworks":
        return {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "reasoning_effort": "low",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "salary_bounds_v3", "schema": RESULT_SCHEMA},
            },
        }
    return {
        "model": config["model"],
        "instructions": SYSTEM_PROMPT,
        "input": prompt,
        "reasoning": {"effort": "medium"},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema", "name": "salary_bounds_v3", "strict": True,
                "schema": RESULT_SCHEMA,
            },
        },
        "max_output_tokens": max_output_tokens,
        "store": False,
    }


def _pending(
    con: sqlite3.Connection, provider: str, limit: int, batch: str,
    split: str | None,
) -> list[sqlite3.Row]:
    parameters: list[Any] = [provider, batch]
    split_sql = ""
    if split:
        split_sql = " AND q.split=?"
        parameters.append(split)
    parameters.append(limit)
    return list(con.execute(
        "SELECT q.id AS queue_id,q.position,q.split,q.ats,q.job_id,j.company,j.title,"
        "j.location,j.employmentType,j.description FROM salary_v3_queue q "
        "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id LEFT JOIN salary_v3_predictions p "
        "ON p.queue_id=q.id AND p.provider=? WHERE q.batch=? AND q.split<>'audit' "
        "AND (p.queue_id IS NULL OR p.status IN ('error','retryable_error','running'))"
        + split_sql + " ORDER BY q.position LIMIT ?", tuple(parameters),
    ))


def prediction_estimate(
    db_path: Path, provider: str, limit: int, batch: str = DEFAULT_BATCH,
    split: str | None = None, max_output_tokens: int | None = None,
) -> dict[str, Any]:
    prepare_schema(db_path)
    output_limit = max_output_tokens or int(PROVIDERS[provider]["default_max_output_tokens"])
    with connect(db_path) as con:
        rows = _pending(con, provider, limit, batch, split)
    inputs = [
        _estimate_tokens(SYSTEM_PROMPT + _job_prompt(row) + json.dumps(RESULT_SCHEMA))
        for row in rows
    ]
    return {
        "provider": provider,
        "batch": batch,
        "split": split or "all",
        "jobs": len(rows),
        "estimated_input_tokens": sum(inputs),
        "maximum_output_tokens": len(rows) * output_limit,
        "maximum_cost_usd": sum(cost_usd(provider, value, output_limit) for value in inputs),
    }


def revalidate_predictions(db_path: Path, batch: str = DEFAULT_BATCH) -> dict[str, int]:
    """Re-run v3 structural/evidence checks without making provider requests."""
    prepare_schema(db_path)
    checked = complete = needs_review = invalid_json = 0
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT p.queue_id,p.provider,p.result_json,j.description "
            "FROM salary_v3_predictions p JOIN salary_v3_queue q ON q.id=p.queue_id "
            "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id WHERE q.batch=? "
            "AND p.result_json IS NOT NULL", (batch,),
        )
        for row in rows:
            checked += 1
            try:
                validation = validate_result(json.loads(row["result_json"]), row["description"])
            except (json.JSONDecodeError, TypeError) as exc:
                validation = [f"stored result is invalid JSON: {exc}"]
                invalid_json += 1
            prediction_status = "needs_review" if validation else "complete"
            complete += prediction_status == "complete"
            needs_review += prediction_status == "needs_review"
            con.execute(
                "UPDATE salary_v3_predictions SET status=?,validation_json=? "
                "WHERE queue_id=? AND provider=?",
                (
                    prediction_status, json.dumps(validation),
                    row["queue_id"], row["provider"],
                ),
            )
    return {
        "checked": checked, "complete": complete, "needs_review": needs_review,
        "invalid_json": invalid_json,
    }


def run_predictions(
    db_path: Path, provider: str, limit: int, max_usd: float,
    batch: str = DEFAULT_BATCH, split: str | None = None,
    max_output_tokens: int | None = None, delay: float = 1.0, retries: int = 3,
    request_timeout: int = 120,
    transport: Callable[[str, str, dict[str, Any], int], dict[str, Any]] = post_json,
) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if max_usd <= 0:
        raise ValueError("--max-usd must be positive")
    if not 10 <= request_timeout <= 600:
        raise ValueError("--timeout must be between 10 and 600 seconds")
    output_limit = max_output_tokens or int(PROVIDERS[provider]["default_max_output_tokens"])
    estimate = prediction_estimate(db_path, provider, limit, batch, split, output_limit)
    if estimate["maximum_cost_usd"] > max_usd:
        raise ValueError(
            f"estimated maximum ${estimate['maximum_cost_usd']:.4f} exceeds --max-usd ${max_usd:.4f}"
        )
    config = PROVIDERS[provider]
    api_key = os.environ.get(config["env"], "").strip()
    if not api_key:
        raise ValueError(f"{config['env']} is not set in this shell")
    completed = errors = 0
    spent = 0.0
    with connect(db_path) as con:
        rows = _pending(con, provider, limit, batch, split)
        for job_number, row in enumerate(rows, 1):
            prompt = _job_prompt(row)
            con.execute(
                "INSERT INTO salary_v3_predictions "
                "(queue_id,provider,model,schema_version,prompt_version,status,attempts,started_at) "
                "VALUES (?,?,?,?,?,'running',0,?) ON CONFLICT(queue_id,provider) DO UPDATE SET "
                "status='running',error=NULL,error_code=NULL,started_at=excluded.started_at",
                (row["queue_id"], provider, config["model"], SCHEMA_VERSION, PROMPT_VERSION, now()),
            )
            con.commit()
            last_error: ProviderError | None = None
            failed_usage: tuple[int, int, float, str, dict[str, Any]] | None = None
            attempt = 0
            for attempt in range(1, retries + 1):
                try:
                    payload = transport(
                        config["url"], api_key, _request_body(provider, prompt, output_limit),
                        request_timeout,
                    )
                    result, input_tokens, output_tokens, response_id, meta = _extract_response(
                        provider, payload
                    )
                    validation = validate_result(result, row["description"])
                    actual_cost = cost_usd(provider, input_tokens, output_tokens)
                    spent += actual_cost
                    result_status = "needs_review" if validation else "complete"
                    con.execute(
                        "UPDATE salary_v3_predictions SET status=?,result_json=?,validation_json=?,"
                        "input_tokens=?,output_tokens=?,cost_usd=?,response_id=?,response_meta_json=?,"
                        "attempts=?,completed_at=?,error=NULL,error_code=NULL WHERE queue_id=? AND provider=?",
                        (
                            result_status, json.dumps(result, separators=(",", ":")),
                            json.dumps(validation), input_tokens, output_tokens, actual_cost,
                            response_id, json.dumps(meta, separators=(",", ":")), attempt, now(),
                            row["queue_id"], provider,
                        ),
                    )
                    con.commit()
                    completed += 1
                    last_error = None
                    print(
                        f"{provider} v3 {job_number}/{len(rows)} queue={row['queue_id']} "
                        f"{result_status} ${actual_cost:.4f}", flush=True,
                    )
                    break
                except ResponseDecodeError as exc:
                    last_error = exc
                    failed_cost = cost_usd(provider, exc.input_tokens, exc.output_tokens)
                    spent += failed_cost
                    failed_usage = (
                        exc.input_tokens, exc.output_tokens, failed_cost, exc.response_id, exc.metadata,
                    )
                except ProviderError as exc:
                    last_error = exc
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    last_error = ProviderError(str(exc), "invalid_response", False)
                if last_error.retryable and attempt < retries:
                    print(
                        f"{provider} v3 queue={row['queue_id']} attempt {attempt}/{retries} "
                        f"{last_error.code}: {last_error}; retrying",
                        flush=True,
                    )
                    time.sleep(min(20, 2 ** (attempt - 1) + random.random()))
                    continue
                break
            if last_error:
                errors += 1
                failed_input, failed_output, failed_cost, response_id, meta = (
                    failed_usage or (None, None, None, "", {})
                )
                con.execute(
                    "UPDATE salary_v3_predictions SET status=?,attempts=?,error_code=?,error=?,"
                    "input_tokens=COALESCE(?,input_tokens),output_tokens=COALESCE(?,output_tokens),"
                    "cost_usd=COALESCE(?,cost_usd),response_id=?,response_meta_json=?,completed_at=? "
                    "WHERE queue_id=? AND provider=?",
                    (
                        "retryable_error" if last_error.retryable else "error", attempt,
                        last_error.code, str(last_error)[:2000], failed_input, failed_output,
                        failed_cost, response_id, json.dumps(meta), now(), row["queue_id"], provider,
                    ),
                )
                con.commit()
                print(f"{provider} v3 queue={row['queue_id']} error: {last_error}", flush=True)
            if job_number < len(rows) and delay > 0:
                time.sleep(delay)
    return {"provider": provider, "completed": completed, "errors": errors, "spent_usd": spent}


def status(db_path: Path, batch: str = DEFAULT_BATCH) -> dict[str, Any]:
    prepare_schema(db_path)
    with connect(db_path) as con:
        report = sample_report(con, batch)
        report["predictions"] = dict(con.execute(
            "SELECT p.provider||':'||p.status,COUNT(*) FROM salary_v3_predictions p "
            "JOIN salary_v3_queue q ON q.id=p.queue_id WHERE q.batch=? GROUP BY 1", (batch,),
        ).fetchall())
        report["spend_usd"] = dict(con.execute(
            "SELECT p.provider,COALESCE(SUM(p.cost_usd),0) FROM salary_v3_predictions p "
            "JOIN salary_v3_queue q ON q.id=p.queue_id WHERE q.batch=? GROUP BY p.provider", (batch,),
        ).fetchall())
        report["gold_labels"] = int(con.execute(
            "SELECT COUNT(*) FROM salary_v3_gold_labels g JOIN salary_v3_queue q "
            "ON q.id=g.queue_id WHERE q.batch=?", (batch,),
        ).fetchone()[0])
        return report


def _db_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="job-boards.db")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="build the new targeted 500-job v3 batch")
    prepare.add_argument("--batch", default=DEFAULT_BATCH)
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prepare.add_argument("--company-cap", type=int, default=3)
    prepare.add_argument("--fresh", action="store_true")
    audit = sub.add_parser("audit-v2", help="copy v2 one-sided candidates into an audit batch")
    audit.add_argument("--fresh", action="store_true")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--batch", default=DEFAULT_BATCH)
    revalidate_parser = sub.add_parser(
        "revalidate", help="re-run local validation without provider requests"
    )
    revalidate_parser.add_argument("--batch", default=DEFAULT_BATCH)
    for name in ("estimate", "predict"):
        command = sub.add_parser(name)
        command.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
        command.add_argument("--limit", type=int, default=1)
        command.add_argument("--batch", default=DEFAULT_BATCH)
        command.add_argument("--split", choices=("training", "calibration", "evaluation"))
        command.add_argument("--max-output-tokens", type=int)
        if name == "predict":
            command.add_argument("--execute", action="store_true")
            command.add_argument("--max-usd", type=float)
            command.add_argument("--delay", type=float, default=1.0)
            command.add_argument("--retries", type=int, default=3)
            command.add_argument(
                "--timeout", type=int, default=60,
                help="seconds to wait for one provider response (default: 60)",
            )
    args = parser.parse_args()
    db_path = _db_path(args.db)
    try:
        if args.command == "prepare":
            print(json.dumps(build_targeted_sample(
                db_path, args.batch, args.seed, args.company_cap, args.fresh
            ), indent=2))
        elif args.command == "audit-v2":
            print(json.dumps(build_v2_audit(db_path, args.fresh), indent=2))
        elif args.command == "status":
            print(json.dumps(status(db_path, args.batch), indent=2))
        elif args.command == "revalidate":
            print(json.dumps(revalidate_predictions(db_path, args.batch), indent=2))
        elif args.command == "estimate":
            print(json.dumps(prediction_estimate(
                db_path, args.provider, args.limit, args.batch, args.split,
                args.max_output_tokens,
            ), indent=2))
        elif not args.execute:
            print(json.dumps(prediction_estimate(
                db_path, args.provider, args.limit, args.batch, args.split,
                args.max_output_tokens,
            ), indent=2))
            print("dry run only; add --execute and --max-usd to make paid requests")
        elif args.max_usd is None:
            raise ValueError("--max-usd is required with --execute")
        else:
            print(json.dumps(run_predictions(
                db_path, args.provider, args.limit, args.max_usd, args.batch, args.split,
                args.max_output_tokens, args.delay, args.retries, args.timeout,
            ), indent=2))
    except (ValueError, ProviderError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
