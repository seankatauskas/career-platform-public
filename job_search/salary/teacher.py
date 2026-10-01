#!/usr/bin/env python3
"""Create and run a bounded, resumable LLM salary-labeling sample.

The raw model predictions in this module are drafts, not authoritative job data.
Human-approved labels live in a separate table and can be edited without losing the
original response from either provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from job_search.salary.enrichment import html_to_text


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "job-boards.db"
DEFAULT_SEED = 20260830
SCHEMA_VERSION = "cash-pay-ranges-v2"
PROMPT_VERSION = "all-currency-pay-v2"

SAMPLE_CATEGORY_WEIGHTS = {
    "usd_annual": 0.50,
    "usd_hourly": 0.20,
    "other_currency": 0.15,
    "no_qualifying_pay": 0.15,
}

PROVIDERS = {
    "fireworks": {
        "env": "FIREWORKS_API_KEY",
        "model": "accounts/fireworks/models/qwen3p8-2p4t-a95b",
        "url": "https://api.fireworks.ai/inference/v1/chat/completions",
        "auth_url": (
            "https://api.fireworks.ai/v1/accounts/fireworks/models"
            "?filter=supports_serverless%3Dtrue&pageSize=1"
        ),
        "input_per_million": 2.00,
        "output_per_million": 6.00,
        "default_max_output_tokens": 2400,
    },
    "openai": {
        "env": "OPENAI_API_KEY",
        "model": "gpt-5.6-sol",
        "url": "https://api.openai.com/v1/responses",
        "input_per_million": 4.00,
        "output_per_million": 20.00,
        "default_max_output_tokens": 1200,
    },
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
                    "currency": {"type": "string"},
                    "period": {
                        "type": "string",
                        "enum": ["year", "hour"],
                    },
                    "min_value": {"type": "number"},
                    "max_value": {"type": "number"},
                    "evidence_text": {"type": "string"},
                },
                "required": [
                    "currency", "period", "min_value", "max_value", "evidence_text",
                ],
            },
        },
    },
    "required": ["ranges"],
}

SYSTEM_PROMPT = """You extract explicit primary cash pay from one job posting into the supplied JSON schema.

Rules:
- Extract every explicit annual or hourly primary cash-pay range offered for this job,
  regardless of currency. Never guess, annualize, or convert units or currencies.
- Treat base salary, base pay, OTE, and target cash compensation as the same kind of pay.
- Preserve each distinct published range, including multiple geographic pay zones.
- For one exact amount, put that same value in both min_value and max_value. Omit
  incomplete minimum-only or maximum-only statements because this task scores only
  complete ranges and exact amounts.
- Use an uppercase ISO currency code such as USD, CAD, EUR, GBP, PLN, or INR when
  explicit or unambiguous from the symbol and applicable location. Use UNKNOWN when
  the currency genuinely cannot be determined.
- Ignore 401(k) matches, insurance values, fundraising, revenue, budgets, years of experience,
  and other numbers that are not compensation for the applicant.
- Exclude bonuses, equity, stock values, stipends, benefits, and commission-only amounts.
- Omit monthly, weekly, daily, and one-time amounts; do not convert them.
- evidence_text must be a short exact quote copied from the posting for every range.
- Return an empty ranges array when no qualifying annual or hourly cash pay is present.
"""

DDL = """
CREATE TABLE IF NOT EXISTS salary_labeling_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ats              TEXT NOT NULL,
    job_id           TEXT NOT NULL,
    position         INTEGER NOT NULL,
    stratum          TEXT NOT NULL,
    sampling_weight  REAL NOT NULL,
    seed             INTEGER NOT NULL,
    split            TEXT NOT NULL CHECK (split IN ('calibration','evaluation')),
    created_at       TEXT NOT NULL,
    UNIQUE (ats, job_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS salary_labeling_queue_position
ON salary_labeling_queue(position);

CREATE TABLE IF NOT EXISTS salary_sample_history (
    ats               TEXT NOT NULL,
    job_id            TEXT NOT NULL,
    original_seed     INTEGER NOT NULL,
    original_position INTEGER NOT NULL,
    archived_at       TEXT NOT NULL,
    PRIMARY KEY (ats, job_id)
);

CREATE TABLE IF NOT EXISTS salary_model_predictions (
    queue_id       INTEGER NOT NULL REFERENCES salary_labeling_queue(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status         TEXT NOT NULL,
    result_json    TEXT,
    validation_json TEXT NOT NULL DEFAULT '[]',
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    response_id    TEXT,
    response_meta_json TEXT NOT NULL DEFAULT '{}',
    attempts       INTEGER NOT NULL DEFAULT 0,
    error_code     TEXT,
    error           TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    PRIMARY KEY (queue_id, provider)
);
CREATE INDEX IF NOT EXISTS salary_model_predictions_status
ON salary_model_predictions(provider, status);

CREATE TABLE IF NOT EXISTS salary_gold_labels (
    queue_id      INTEGER PRIMARY KEY REFERENCES salary_labeling_queue(id) ON DELETE CASCADE,
    result_json   TEXT NOT NULL,
    chosen_source TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    reviewed_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS salary_gold_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id               INTEGER NOT NULL,
    previous_result_json   TEXT,
    previous_chosen_source TEXT,
    previous_note          TEXT,
    happened_at            TEXT NOT NULL
);
"""


class ProviderError(Exception):
    def __init__(self, message: str, code: str = "provider_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ResponseDecodeError(ProviderError):
    """A billed provider response arrived but did not contain usable JSON."""

    def __init__(
        self,
        message: str,
        input_tokens: int,
        output_tokens: int,
        response_id: str,
        metadata: dict[str, Any],
    ) -> None:
        super().__init__(message, "invalid_response", False)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.response_id = response_id
        self.metadata = metadata


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def prepare_schema(db_path: Path) -> None:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    with connect(db_path) as con:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone():
            raise ValueError("database does not contain the jobs table")
        con.executescript(DDL)
        prediction_columns = {
            row[1] for row in con.execute("PRAGMA table_info(salary_model_predictions)")
        }
        if "response_meta_json" not in prediction_columns:
            con.execute(
                "ALTER TABLE salary_model_predictions ADD COLUMN "
                "response_meta_json TEXT NOT NULL DEFAULT '{}'"
            )


def _pay_sample_category(description: str, location: str = "") -> str:
    """Assign a coarse sampling category without treating it as a gold label."""
    # This is only a broad sampling hint. Searching the stored representation directly
    # avoids fully rendering hundreds of thousands of descriptions during queue setup.
    text = " ".join(str(description or "").split()).casefold()
    place = str(location or "").casefold()
    hourly = bool(re.search(r"(?:/\s*(?:hr|hour)\b|per\s+hour\b|hourly\b)", text))
    annual = bool(re.search(
        r"(?:/\s*(?:yr|year)\b|per\s+year\b|annually\b|annual\b|salary\b|"
        r"base\s+pay\b|pay\s+range\b|compensation\s+range\b|ote\b)",
        text,
    ))
    explicit_usd = bool(re.search(
        r"(?:\busd\b|\bus\s*dollars?\b|\bu\.s\.\s*dollars?\b|us\s*\$)", text
    ))
    explicit_other = bool(re.search(
        r"(?:\b(?:cad|eur|gbp|inr|aud|nzd|sgd|chf|jpy|pln|sek|nok|dkk)\b|[€£₹])",
        text,
    ))
    dollar_amount = bool(re.search(r"\$\s*\d", text))
    other_dollar_location = bool(re.search(
        r"\b(?:canada|canadian|australia|australian|new zealand|singapore)\b", place
    ))
    likely_usd = explicit_usd or (
        dollar_amount and not explicit_other and not other_dollar_location
    )
    if explicit_other and not explicit_usd and (annual or hourly):
        return "other_currency"
    if likely_usd and hourly:
        return "usd_hourly"
    if likely_usd and annual:
        return "usd_annual"
    return "no_qualifying_pay"


def _rank(seed: int, *parts: str) -> str:
    value = "|".join((str(seed), *parts)).encode()
    return hashlib.sha256(value).hexdigest()


def _allocate(total: int, capacities: dict[str, int]) -> dict[str, int]:
    targets = {key: 0 for key in capacities}
    active = sorted(key for key, cap in capacities.items() if cap)
    while sum(targets.values()) < total and active:
        for key in list(active):
            if targets[key] >= capacities[key]:
                active.remove(key)
                continue
            targets[key] += 1
            if sum(targets.values()) == total:
                break
    return targets


def _weighted_targets(total: int, capacities: dict[str, int]) -> dict[str, int]:
    """Allocate the requested category mix and redistribute capacity shortfalls."""
    raw = {key: total * SAMPLE_CATEGORY_WEIGHTS[key] for key in SAMPLE_CATEGORY_WEIGHTS}
    desired = {key: math.floor(value) for key, value in raw.items()}
    for key in sorted(raw, key=lambda item: (raw[item] - desired[item], item), reverse=True):
        if sum(desired.values()) == total:
            break
        desired[key] += 1
    targets = {key: min(desired[key], capacities.get(key, 0)) for key in desired}
    while sum(targets.values()) < total:
        available = [key for key in targets if targets[key] < capacities.get(key, 0)]
        if not available:
            break
        key = min(
            available,
            key=lambda item: (
                targets[item] / SAMPLE_CATEGORY_WEIGHTS[item],
                list(SAMPLE_CATEGORY_WEIGHTS).index(item),
            ),
        )
        targets[key] += 1
    return targets


def build_sample(
    db_path: Path,
    sample_size: int = 500,
    seed: int = DEFAULT_SEED,
    company_cap: int = 3,
    fresh: bool = False,
    exclude_current: bool = False,
) -> int:
    """Create a deterministic targeted sample balanced within category across ATS."""
    if sample_size < 1 or company_cap < 1:
        raise ValueError("sample size and company cap must be positive")
    if exclude_current and not fresh:
        raise ValueError("exclude_current requires fresh=True")
    prepare_schema(db_path)
    with connect(db_path) as con:
        existing = int(con.execute("SELECT COUNT(*) FROM salary_labeling_queue").fetchone()[0])
        if existing and not fresh:
            return existing
        if fresh:
            if exclude_current:
                con.execute(
                    "INSERT OR IGNORE INTO salary_sample_history "
                    "(ats,job_id,original_seed,original_position,archived_at) "
                    "SELECT ats,job_id,seed,position,? FROM salary_labeling_queue",
                    (now(),),
                )
            # Only the hosted-model batch is replaced. Raw jobs, enrichment, and the
            # separate deterministic-parser audit are intentionally untouched.
            con.execute("DELETE FROM salary_gold_events")
            con.execute("DELETE FROM salary_gold_labels")
            con.execute("DELETE FROM salary_model_predictions")
            con.execute("DELETE FROM salary_labeling_queue")

        excluded = {
            (row[0], row[1])
            for row in con.execute("SELECT ats,job_id FROM salary_sample_history")
        }
        groups: dict[str, dict[tuple[str, str], list[dict[str, str]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in con.execute(
            "SELECT ats,id AS job_id,company,location,description FROM jobs "
            "WHERE description IS NOT NULL AND length(trim(description))>0"
        ):
            item = dict(row)
            if (item["ats"], item["job_id"]) in excluded:
                continue
            category = _pay_sample_category(item["description"], item["location"])
            stratum = f"{item['ats']}|{category}"
            item["category"] = category
            item["stratum"] = stratum
            del item["description"]
            del item["location"]
            company_key = str(item["company"] or "").casefold()
            groups[stratum][(item["ats"], company_key)].append(item)

        stratum_capacities = {
            stratum: sum(min(company_cap, len(rows)) for rows in companies.values())
            for stratum, companies in groups.items()
        }
        category_capacities = {
            category: sum(
                capacity for stratum, capacity in stratum_capacities.items()
                if stratum.endswith("|" + category)
            )
            for category in SAMPLE_CATEGORY_WEIGHTS
        }
        available = sum(category_capacities.values())
        if available < sample_size:
            raise ValueError(f"only {available} jobs available after the company cap")
        category_targets = _weighted_targets(sample_size, category_capacities)
        if sum(category_targets.values()) < sample_size:
            raise ValueError("not enough jobs to satisfy the targeted sample")
        targets: dict[str, int] = {}
        for category, category_target in category_targets.items():
            capacities = {
                stratum: capacity
                for stratum, capacity in stratum_capacities.items()
                if stratum.endswith("|" + category)
            }
            targets.update(_allocate(category_target, capacities))
        selected: list[dict[str, Any]] = []
        company_counts: Counter[tuple[str, str]] = Counter()
        for stratum in sorted(groups):
            if targets.get(stratum, 0) == 0:
                continue
            companies = groups[stratum]
            company_keys = sorted(
                companies, key=lambda key: _rank(seed, stratum, "company", *key)
            )
            for key, rows in companies.items():
                rows.sort(key=lambda item: _rank(seed, stratum, item["ats"], item["job_id"]))
            chosen: list[dict[str, Any]] = []
            for round_number in range(company_cap):
                for key in company_keys:
                    if len(companies[key]) <= round_number:
                        continue
                    if company_counts[key] >= company_cap:
                        continue
                    chosen.append(companies[key][round_number])
                    company_counts[key] += 1
                    if len(chosen) == targets[stratum]:
                        break
                if len(chosen) == targets[stratum]:
                    break
            population = sum(len(rows) for rows in companies.values())
            if len(chosen) != targets[stratum]:
                raise ValueError(
                    f"could only select {len(chosen)} of {targets[stratum]} jobs for {stratum} "
                    "after applying the global company cap"
                )
            weight = population / len(chosen) if chosen else 1.0
            for item in chosen:
                item["sampling_weight"] = weight
            selected.extend(chosen)

        selected.sort(key=lambda item: _rank(seed, "queue", item["ats"], item["job_id"]))
        created = now()
        con.executemany(
            "INSERT INTO salary_labeling_queue "
            "(ats,job_id,position,stratum,sampling_weight,seed,split,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    item["ats"], item["job_id"], position, item["stratum"],
                    item["sampling_weight"], seed,
                    "calibration" if position <= min(100, sample_size) else "evaluation",
                    created,
                )
                for position, item in enumerate(selected, 1)
            ],
        )
        return len(selected)


def _job_prompt(row: sqlite3.Row) -> str:
    return (
        "Extract compensation from this job. The response must match the JSON schema.\n\n"
        f"ATS: {row['ats']}\nCompany: {row['company']}\nTitle: {row['title']}\n"
        f"Location: {row['location']}\nEmployment type: {row['employmentType']}\n\n"
        f"JOB POSTING\n{row['description']}"
    )


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def cost_usd(provider: str, input_tokens: int, output_tokens: int) -> float:
    config = PROVIDERS[provider]
    return (
        input_tokens * config["input_per_million"]
        + output_tokens * config["output_per_million"]
    ) / 1_000_000


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
            # This Qwen3.8 checkpoint requires thinking and defaults to xhigh.
            # Low preserves reasoning while leaving room for the final JSON.
            "reasoning_effort": "low",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "salary_extraction", "schema": RESULT_SCHEMA},
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
                "type": "json_schema",
                "name": "salary_extraction",
                "strict": True,
                "schema": RESULT_SCHEMA,
            },
        },
        "max_output_tokens": max_output_tokens,
        "store": False,
    }


def _classify_http_error(exc: urllib.error.HTTPError) -> ProviderError:
    try:
        raw = exc.read().decode("utf-8", "replace")
        payload = json.loads(raw)
        detail = payload.get("error", payload)
        if isinstance(detail, dict):
            code = str(detail.get("code") or detail.get("type") or f"http_{exc.code}")
            message = str(detail.get("message") or raw)
        else:
            code, message = f"http_{exc.code}", str(detail)
    except Exception:
        code, message = f"http_{exc.code}", str(exc)
    lower = f"{code} {message}".lower()
    quota = any(token in lower for token in ("quota", "billing", "credit", "insufficient"))
    if exc.code == 429 and quota:
        return ProviderError(message, "quota_exhausted", False)
    retryable = exc.code in {408, 409, 429, 500, 502, 503, 504}
    return ProviderError(message, code, retryable)


def post_json(url: str, api_key: str, body: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "job-boards-salary-teacher/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise _classify_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ProviderError(str(exc), "network_error", True) from exc


def credential_report(provider: str) -> dict[str, Any]:
    """Identify the loaded secret without printing any part of the secret itself."""
    config = PROVIDERS[provider]
    raw = os.environ.get(config["env"], "")
    effective = raw.strip()
    if effective.startswith("fw_"):
        key_kind = "standard_fireworks_api_key"
    elif effective.startswith("fpk_"):
        key_kind = "fire_pass_key_not_standard_api_key"
    elif effective.startswith(("sk-", "sk_")):
        key_kind = "third_party_or_stored_secret_not_fireworks_api_key"
    elif effective:
        key_kind = "unrecognized"
    else:
        key_kind = "missing"
    return {
        "provider": provider,
        "environment_variable": config["env"],
        "is_set": bool(effective),
        "characters_sent": len(effective),
        "sha256_fingerprint": (
            hashlib.sha256(effective.encode()).hexdigest()[:16] if effective else None
        ),
        "key_kind": key_kind,
        "surrounding_whitespace_removed": raw != effective,
        "contains_internal_whitespace": any(character.isspace() for character in effective),
        "looks_like_assignment_instead_of_key": effective.startswith(config["env"] + "="),
        "has_literal_outer_quotes": (
            len(effective) >= 2
            and effective[0] in {"'", '"'}
            and effective[-1] == effective[0]
        ),
    }


def check_fireworks_auth(
    transport: Callable[[urllib.request.Request, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Make a free metadata request that proves Fireworks accepts the loaded key."""
    config = PROVIDERS["fireworks"]
    api_key = os.environ.get(config["env"], "").strip()
    if not api_key:
        raise ValueError(f"{config['env']} is not set in this shell")
    request = urllib.request.Request(
        config["auth_url"],
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "job-boards-salary-teacher/1.0",
        },
    )
    if transport is not None:
        payload = transport(request, 30)
    else:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise ProviderError(str(exc), "network_error", True) from exc
    return {
        "authenticated": True,
        "metadata_endpoint": config["auth_url"],
        "models_returned": len(payload.get("models", [])),
        "paid_inference": False,
    }


def _extract_response(
    provider: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], int, int, str, dict[str, Any]]:
    response_id = str(payload.get("id") or "")
    if provider == "fireworks":
        choice = payload["choices"][0]
        message = choice["message"]
        content = message.get("content")
        usage = payload.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        reasoning = message.get("reasoning_content") or ""
        metadata = {
            "finish_reason": choice.get("finish_reason"),
            "content_characters": len(content) if isinstance(content, str) else 0,
            "reasoning_characters": len(reasoning) if isinstance(reasoning, str) else 0,
        }
    else:
        chunks: list[str] = []
        for output in payload.get("output", []):
            if output.get("type") != "message":
                continue
            for part in output.get("content", []):
                if part.get("type") == "output_text":
                    chunks.append(part.get("text", ""))
        content = "".join(chunks)
        usage = payload.get("usage", {})
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        metadata = {
            "status": payload.get("status"),
            "incomplete_details": payload.get("incomplete_details"),
            "content_characters": len(content),
        }
    if not isinstance(content, (str, dict)) or (isinstance(content, str) and not content.strip()):
        raise ResponseDecodeError(
            "provider returned no final JSON content"
            + (
                f" (finish_reason={metadata.get('finish_reason')}, "
                f"output_tokens={output_tokens})"
                if provider == "fireworks" else ""
            ),
            input_tokens, output_tokens, response_id, metadata,
        )
    if isinstance(content, dict):
        result = content
    else:
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ResponseDecodeError(
                f"provider returned invalid JSON: {exc}",
                input_tokens, output_tokens, response_id, metadata,
            ) from exc
    return result, input_tokens, output_tokens, response_id, metadata


def _evidence_text(value: str) -> str:
    """Compare rendered evidence while ignoring markup-only punctuation spacing."""
    rendered = " ".join(html_to_text(value).split()).casefold()
    return re.sub(r"\s*([\-\u2013\u2014])\s*", r"\1", rendered)


def _validate_result(result: dict[str, Any], description: str) -> list[str]:
    problems: list[str] = []
    classifications = {"salaried", "hourly", "other_pay", "no_compensation", "unclear"}
    legacy_shape = "classification" in result
    if legacy_shape and result.get("classification") not in classifications:
        problems.append("invalid classification")
    ranges = result.get("ranges")
    if not isinstance(ranges, list):
        return problems + ["ranges is not an array"]
    normalized_descriptions = {
        _evidence_text(description),
        " ".join(str(description or "").split()).casefold(),
    }
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            problems.append(f"range {index + 1} is not an object")
            continue
        evidence = str(item.get("evidence_text") or "")
        if not evidence:
            problems.append(f"range {index + 1} has no evidence")
        elif not any(_evidence_text(evidence) in candidate for candidate in normalized_descriptions):
            problems.append(f"range {index + 1} evidence is not an exact posting quote")
        minimum, maximum = item.get("min_value"), item.get("max_value")
        if minimum is None and maximum is None:
            problems.append(f"range {index + 1} has no amount")
        if minimum is not None and maximum is not None and minimum > maximum:
            problems.append(f"range {index + 1} minimum exceeds maximum")
        if not legacy_shape:
            if not isinstance(item.get("currency"), str) or not item["currency"].strip():
                problems.append(f"range {index + 1} has no currency")
            if item.get("period") not in {"year", "hour"}:
                problems.append(f"range {index + 1} period is not year or hour")
            if minimum is None or maximum is None:
                problems.append(f"range {index + 1} needs both bounds")
    if legacy_shape and result.get("classification") == "no_compensation" and ranges:
        problems.append("no_compensation result contains ranges")
    if "confidence" in result:
        confidence = result.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            problems.append("confidence is outside 0..1")
    return problems


def revalidate_predictions(db_path: Path) -> dict[str, int]:
    """Re-run local safety checks for every stored successful model result."""
    prepare_schema(db_path)
    checked = complete = needs_review = invalid_json = 0
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT p.queue_id,p.provider,p.result_json,j.description "
            "FROM salary_model_predictions p "
            "JOIN salary_labeling_queue q ON q.id=p.queue_id "
            "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
            "WHERE p.result_json IS NOT NULL"
        )
        for row in rows:
            checked += 1
            try:
                result = json.loads(row["result_json"])
                validation = _validate_result(result, row["description"])
            except (json.JSONDecodeError, TypeError) as exc:
                validation = [f"stored result is invalid JSON: {exc}"]
                invalid_json += 1
            prediction_status = "needs_review" if validation else "complete"
            needs_review += prediction_status == "needs_review"
            complete += prediction_status == "complete"
            con.execute(
                "UPDATE salary_model_predictions SET status=?,validation_json=? "
                "WHERE queue_id=? AND provider=?",
                (
                    prediction_status, json.dumps(validation),
                    row["queue_id"], row["provider"],
                ),
            )
    return {
        "checked": checked,
        "complete": complete,
        "needs_review": needs_review,
        "invalid_json": invalid_json,
    }


def _pending_jobs(
    con: sqlite3.Connection, provider: str, limit: int, split: str | None = None
) -> list[sqlite3.Row]:
    split_clause = " AND q.split=?" if split else ""
    parameters: tuple[Any, ...] = (provider, split, limit) if split else (provider, limit)
    return list(con.execute(
        "SELECT q.id AS queue_id,q.position,q.split,q.ats,q.job_id,j.company,j.title,"
        "j.location,j.employmentType,j.description FROM salary_labeling_queue q "
        "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
        "LEFT JOIN salary_model_predictions p ON p.queue_id=q.id AND p.provider=? "
        "WHERE (p.queue_id IS NULL OR p.status IN ('error','retryable_error','running')) "
        + split_clause +
        "ORDER BY q.position LIMIT ?",
        parameters,
    ))


def _output_limit(provider: str, requested: int | None) -> int:
    value = requested or int(PROVIDERS[provider]["default_max_output_tokens"])
    if value < 1:
        raise ValueError("--max-output-tokens must be positive")
    return value


def prediction_estimate(
    db_path: Path, provider: str, limit: int, max_output_tokens: int | None = None,
    split: str | None = None,
) -> dict[str, Any]:
    max_output_tokens = _output_limit(provider, max_output_tokens)
    if split not in {None, "calibration", "evaluation"}:
        raise ValueError("split must be calibration or evaluation")
    prepare_schema(db_path)
    with connect(db_path) as con:
        rows = _pending_jobs(con, provider, limit, split)
    estimates = []
    for row in rows:
        prompt = _job_prompt(row)
        input_tokens = _estimate_tokens(SYSTEM_PROMPT + prompt + json.dumps(RESULT_SCHEMA))
        estimates.append({
            "queue_id": row["queue_id"],
            "position": row["position"],
            "input_tokens": input_tokens,
            "max_output_tokens": max_output_tokens,
            "max_cost_usd": cost_usd(provider, input_tokens, max_output_tokens),
        })
    return {
        "provider": provider,
        "split": split or "all",
        "model": PROVIDERS[provider]["model"],
        "jobs": len(estimates),
        "estimated_input_tokens": sum(item["input_tokens"] for item in estimates),
        "maximum_output_tokens": len(estimates) * max_output_tokens,
        "maximum_cost_usd": sum(item["max_cost_usd"] for item in estimates),
        "items": estimates,
    }


def run_predictions(
    db_path: Path,
    provider: str,
    limit: int,
    max_usd: float,
    max_output_tokens: int | None = None,
    delay: float = 1.0,
    retries: int = 3,
    split: str | None = None,
    transport: Callable[[str, str, dict[str, Any], int], dict[str, Any]] = post_json,
) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if max_usd <= 0:
        raise ValueError("--max-usd must be positive")
    if retries < 1 or retries > 10:
        raise ValueError("--retries must be between 1 and 10")
    max_output_tokens = _output_limit(provider, max_output_tokens)
    estimate = prediction_estimate(db_path, provider, limit, max_output_tokens, split)
    if estimate["maximum_cost_usd"] > max_usd:
        raise ValueError(
            f"estimated maximum ${estimate['maximum_cost_usd']:.4f} exceeds "
            f"--max-usd ${max_usd:.4f}"
        )
    config = PROVIDERS[provider]
    api_key = os.environ.get(config["env"], "").strip()
    if not api_key:
        raise ValueError(f"{config['env']} is not set in this shell")

    completed = errors = 0
    spent = 0.0
    halted = False
    with connect(db_path) as con:
        rows = _pending_jobs(con, provider, limit, split)
        for job_number, row in enumerate(rows):
            prompt = _job_prompt(row)
            predicted_input = _estimate_tokens(SYSTEM_PROMPT + prompt + json.dumps(RESULT_SCHEMA))
            predicted_cost = cost_usd(provider, predicted_input, max_output_tokens)
            if spent + predicted_cost > max_usd:
                break
            started = now()
            con.execute(
                "INSERT INTO salary_model_predictions "
                "(queue_id,provider,model,schema_version,prompt_version,status,attempts,started_at) "
                "VALUES (?,?,?,?,?,'running',0,?) "
                "ON CONFLICT(queue_id,provider) DO UPDATE SET status='running',error=NULL,"
                "error_code=NULL,started_at=excluded.started_at",
                (
                    row["queue_id"], provider, config["model"], SCHEMA_VERSION,
                    PROMPT_VERSION, started,
                ),
            )
            con.commit()
            body = _request_body(provider, prompt, max_output_tokens)
            last_error: ProviderError | None = None
            failed_usage: tuple[int, int, float, str, dict[str, Any]] | None = None
            for attempt in range(1, retries + 1):
                try:
                    payload = transport(config["url"], api_key, body, 120)
                    result, input_tokens, output_tokens, response_id, response_meta = _extract_response(
                        provider, payload
                    )
                    validation = _validate_result(result, row["description"])
                    actual_cost = cost_usd(provider, input_tokens, output_tokens)
                    spent += actual_cost
                    status = "needs_review" if validation else "complete"
                    con.execute(
                        "UPDATE salary_model_predictions SET status=?,result_json=?,"
                        "validation_json=?,input_tokens=?,output_tokens=?,cost_usd=?,response_id=?,"
                        "response_meta_json=?,attempts=?,completed_at=?,error=NULL,error_code=NULL "
                        "WHERE queue_id=? AND provider=?",
                        (
                            status, json.dumps(result, separators=(",", ":")),
                            json.dumps(validation), input_tokens, output_tokens, actual_cost,
                            response_id, json.dumps(response_meta, separators=(",", ":")),
                            attempt, now(), row["queue_id"], provider,
                        ),
                    )
                    con.commit()
                    completed += 1
                    last_error = None
                    print(
                        f"{provider} {job_number + 1}/{len(rows)} queue={row['queue_id']} "
                        f"{status} ${actual_cost:.4f}",
                        flush=True,
                    )
                    break
                except ResponseDecodeError as exc:
                    last_error = exc
                    failed_cost = cost_usd(provider, exc.input_tokens, exc.output_tokens)
                    spent += failed_cost
                    failed_usage = (
                        exc.input_tokens, exc.output_tokens, failed_cost,
                        exc.response_id, exc.metadata,
                    )
                except ProviderError as exc:
                    last_error = exc
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    last_error = ProviderError(str(exc), "invalid_response", False)
                if last_error and last_error.retryable and attempt < retries:
                    time.sleep(min(20, (2 ** (attempt - 1)) + random.random()))
                    continue
                break
            if last_error:
                errors += 1
                status = "retryable_error" if last_error.retryable else "error"
                failed_input, failed_output, failed_cost, failed_response_id, failed_meta = (
                    failed_usage or (None, None, None, "", {})
                )
                con.execute(
                    "UPDATE salary_model_predictions SET status=?,attempts=?,error_code=?,error=?,"
                    "input_tokens=COALESCE(?,input_tokens),"
                    "output_tokens=COALESCE(?,output_tokens),cost_usd=COALESCE(?,cost_usd),"
                    "response_id=CASE WHEN ?<>'' THEN ? ELSE response_id END,"
                    "response_meta_json=?,completed_at=? WHERE queue_id=? AND provider=?",
                    (
                        status, attempt, last_error.code, str(last_error)[:2000],
                        failed_input, failed_output, failed_cost,
                        failed_response_id, failed_response_id,
                        json.dumps(failed_meta, separators=(",", ":")), now(),
                        row["queue_id"], provider,
                    ),
                )
                con.commit()
                print(
                    f"{provider} queue={row['queue_id']} {status}: {last_error}",
                    file=sys.stderr,
                    flush=True,
                )
                if last_error.code == "quota_exhausted":
                    halted = True
                    break
            if delay > 0 and job_number + 1 < len(rows):
                time.sleep(delay)
    return {
        "provider": provider,
        "completed": completed,
        "errors": errors,
        "spent_usd": spent,
        "halted_for_quota": halted,
    }


def status(db_path: Path) -> dict[str, Any]:
    prepare_schema(db_path)
    with connect(db_path) as con:
        queued = int(con.execute("SELECT COUNT(*) FROM salary_labeling_queue").fetchone()[0])
        splits = dict(con.execute(
            "SELECT split,COUNT(*) FROM salary_labeling_queue GROUP BY split"
        ).fetchall())
        predictions = {
            f"{row['provider']}:{row['status']}": row["count"]
            for row in con.execute(
                "SELECT provider,status,COUNT(*) AS count FROM salary_model_predictions "
                "GROUP BY provider,status"
            )
        }
        spend = {
            row["provider"]: round(float(row["cost"] or 0), 6)
            for row in con.execute(
                "SELECT provider,SUM(cost_usd) AS cost FROM salary_model_predictions GROUP BY provider"
            )
        }
        gold = int(con.execute("SELECT COUNT(*) FROM salary_gold_labels").fetchone()[0])
    return {
        "queued": queued,
        "splits": splits,
        "predictions": predictions,
        "spend_usd": spend,
        "gold_labels": gold,
    }


def _db_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="build the deterministic review sample")
    prepare.add_argument("--sample-size", type=int, default=500)
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prepare.add_argument("--company-cap", type=int, default=3)
    prepare.add_argument(
        "--fresh", action="store_true",
        help="discard the old parser review and any existing LLM predictions/gold labels",
    )
    prepare.add_argument(
        "--exclude-current", action="store_true",
        help="record current queue IDs in sample history so the replacement cannot reuse them",
    )

    subparsers.add_parser("status", help="show queue, prediction, spend, and review counts")
    subparsers.add_parser(
        "revalidate", help="re-run local validation for stored successful predictions"
    )
    subparsers.add_parser(
        "auth-check",
        help="fingerprint the Fireworks environment value and test it without inference",
    )

    for name in ("estimate", "predict"):
        command = subparsers.add_parser(name)
        command.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
        command.add_argument("--limit", type=int, default=1)
        command.add_argument(
            "--split", choices=("calibration", "evaluation"),
            help="restrict work to one locked sample split",
        )
        command.add_argument(
            "--max-output-tokens", type=int,
            help="override provider default (Fireworks 2400; OpenAI 1200)",
        )
        if name == "predict":
            command.add_argument("--execute", action="store_true")
            command.add_argument("--max-usd", type=float)
            command.add_argument("--delay", type=float, default=1.0)
            command.add_argument("--retries", type=int, default=3)

    args = parser.parse_args()
    db_path = _db_path(args.db)
    try:
        if args.command == "prepare":
            count = build_sample(
                db_path, args.sample_size, args.seed, args.company_cap, args.fresh,
                args.exclude_current,
            )
            print(json.dumps({"queued": count, "status": status(db_path)}, indent=2))
        elif args.command == "status":
            print(json.dumps(status(db_path), indent=2))
        elif args.command == "revalidate":
            print(json.dumps(revalidate_predictions(db_path), indent=2))
        elif args.command == "auth-check":
            print(json.dumps(credential_report("fireworks"), indent=2))
            print(json.dumps(check_fireworks_auth(), indent=2))
        elif args.command == "estimate":
            print(json.dumps(prediction_estimate(
                db_path, args.provider, args.limit, args.max_output_tokens, args.split
            ), indent=2))
        else:
            estimate = prediction_estimate(
                db_path, args.provider, args.limit, args.max_output_tokens, args.split
            )
            if not args.execute:
                print(json.dumps(estimate, indent=2))
                print("dry run only; add --execute and --max-usd to make paid requests")
            elif args.max_usd is None:
                raise ValueError("--max-usd is required with --execute")
            else:
                print(json.dumps(run_predictions(
                    db_path, args.provider, args.limit, args.max_usd,
                    args.max_output_tokens, args.delay, args.retries, args.split,
                ), indent=2))
    except (ValueError, ProviderError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
