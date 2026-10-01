#!/usr/bin/env python3
"""Extract salaried compensation into additive, auditable SQLite tables.

``job_search/collection/boards.py`` calls :func:`enrich_job` while a board response is already in
memory, so ATS-native compensation is captured without per-job requests. Generic
description extraction is delegated to the durable local-model queue in
``job_search/salary/llm.py``; this module's former fuzzy description backfill is disabled.

Machine-derived values never overwrite the authoritative ``jobs`` table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any


PARSER_VERSION = "salary-native-v2"
SUCCESS_STATUSES = {"complete", "no_compensation", "non_salary_compensation"}
PERIODS = {"year", "hour", "month", "unknown", "one_time"}
COMPONENTS = {
    "base_salary", "ote", "commission", "bonus", "equity", "stipend", "other", "unknown"
}
VALUE_KINDS = {"range", "exact", "minimum", "maximum", "target", "average"}

ENRICHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_enrichment (
    ats                 TEXT NOT NULL,
    job_id              TEXT NOT NULL,
    parser_version      TEXT NOT NULL,
    status              TEXT NOT NULL,
    preferred_source    TEXT NOT NULL DEFAULT '',
    range_count         INTEGER NOT NULL DEFAULT 0,
    source_fingerprint  TEXT NOT NULL,
    processed_at        TEXT NOT NULL,
    error               TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ats, job_id),
    FOREIGN KEY (ats, job_id) REFERENCES jobs(ats, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS job_enrichment_status
ON job_enrichment(status, preferred_source);

CREATE TABLE IF NOT EXISTS job_compensation_ranges (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ats               TEXT NOT NULL,
    job_id            TEXT NOT NULL,
    range_key         TEXT NOT NULL,
    component         TEXT NOT NULL,
    value_kind        TEXT NOT NULL,
    currency          TEXT NOT NULL,
    period            TEXT NOT NULL,
    min_value         REAL,
    max_value         REAL,
    annual_min_value  REAL,
    annual_max_value  REAL,
    location_scope    TEXT NOT NULL DEFAULT '',
    source_type       TEXT NOT NULL,
    source_field      TEXT NOT NULL,
    evidence_text     TEXT NOT NULL DEFAULT '',
    evidence_json     TEXT NOT NULL DEFAULT '[]',
    parser_rule       TEXT NOT NULL,
    is_preferred      INTEGER NOT NULL DEFAULT 0,
    is_corroborated   INTEGER NOT NULL DEFAULT 0,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    removed_at        TEXT,
    UNIQUE (ats, job_id, range_key),
    FOREIGN KEY (ats, job_id) REFERENCES jobs(ats, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS job_compensation_current
ON job_compensation_ranges(ats, job_id, removed_at);
CREATE INDEX IF NOT EXISTS job_compensation_annual
ON job_compensation_ranges(currency, annual_min_value, annual_max_value);
"""

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_PAY_BLOCK = re.compile(
    r"<div[^>]+class=[\"'][^\"']*pay-input[^\"']*[\"'][^>]*>"
    r"(?P<body>.*?<div[^>]+class=[\"'][^\"']*pay-range[^\"']*[\"'][^>]*>.*?</div>)",
    re.IGNORECASE | re.DOTALL,
)
_TITLE = re.compile(
    r"<div[^>]+class=[\"'][^\"']*title[^\"']*[\"'][^>]*>(?P<title>.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)

_CURRENCY_TOKEN = (
    r"(?:US\$|CA\$|C\$|AU\$|A\$|NZ\$|SG\$|S\$|HK\$|"
    r"USD|CAD|AUD|NZD|EUR|GBP|INR|JPY|CNY|RMB|CHF|SGD|HKD|SEK|NOK|DKK|PLN|"
    r"US dollars?|Canadian dollars?|Australian dollars?|New Zealand dollars?|"
    r"euros?|pounds? sterling|Indian rupees?|Japanese yen|Chinese yuan|\$|€|£|₹|¥)"
)
_AMOUNT = r"(?:\d{1,3}(?:[\s,.\u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*[kKmM]?"
_MONEY = re.compile(
    rf"(?P<prefix>{_CURRENCY_TOKEN})?\s*(?P<first>{_AMOUNT})"
    rf"\s*(?P<suffix1>{_CURRENCY_TOKEN})?"
    rf"(?:\s*(?P<sep>[-–—]|to|through)\s*(?P<prefix2>{_CURRENCY_TOKEN})?\s*"
    rf"(?P<second>{_AMOUNT})\s*(?P<suffix2>{_CURRENCY_TOKEN})?)?",
    re.IGNORECASE,
)

_PAY_CONTEXT = re.compile(
    r"\b(salary|salaried|base pay|base compensation|compensation range|pay range|"
    r"annual compensation|annual pay|annual base|total cash|on-target earnings|OTE|"
    r"commission|bonus|equity|stock grant|RSUs?|stipend)\b",
    re.IGNORECASE,
)
_NON_SALARY_PERIOD = re.compile(
    r"(?:/|per\s+|an?\s+)(?:hour|hr|day|week)\b|\bhourly\b|\bdaily rate\b|\bweekly pay\b",
    re.IGNORECASE,
)
_MONTH = re.compile(r"(?:/|per\s+|a\s+)(?:month|mo)\b|\bmonthly\b", re.IGNORECASE)
_YEAR = re.compile(
    r"(?:/|per\s+|a\s+)(?:year|yr)\b|\b(?:annual|annually|yearly|per annum)\b",
    re.IGNORECASE,
)
_ONE_TIME = re.compile(r"\b(one[- ]time|sign[- ]on|signing)\b", re.IGNORECASE)
_FALSE_CONTEXT = re.compile(
    r"\b(revenue|valuation|funding|budget|sales quota|401\s*\(?k\)?|insurance coverage|"
    r"donation|contract value|assets under management)\b",
    re.IGNORECASE,
)

_CURRENCY_MAP = {
    "€": "EUR", "£": "GBP", "₹": "INR", "¥": "UNKNOWN",
    "us$": "USD", "ca$": "CAD", "c$": "CAD", "au$": "AUD", "a$": "AUD",
    "nz$": "NZD", "sg$": "SGD", "s$": "SGD", "hk$": "HKD",
    "us dollar": "USD", "us dollars": "USD",
    "canadian dollar": "CAD", "canadian dollars": "CAD",
    "australian dollar": "AUD", "australian dollars": "AUD",
    "new zealand dollar": "NZD", "new zealand dollars": "NZD",
    "euro": "EUR", "euros": "EUR", "pound sterling": "GBP",
    "pounds sterling": "GBP", "indian rupee": "INR", "indian rupees": "INR",
    "japanese yen": "JPY", "chinese yuan": "CNY", "rmb": "CNY",
}
_SOURCE_PRIORITY = {
    "ats_structured": 3,
    "ats_rendered": 2,
    "llm_description": 1,
    "description_rule": 0,
}
NATIVE_SOURCES = {"ats_structured", "ats_rendered"}
PRIMARY_COMPONENTS = {"base_salary", "ote", "unknown"}


def html_to_text(value: str) -> str:
    """Decode nested entities before stripping tags.

    Some Greenhouse responses entity-encode the complete HTML body. Stripping
    first turns ``&lt;p&gt;`` into a literal tag after the stripping pass, which is
    how markup ended up in older database descriptions.
    """
    text = str(value or "")
    for _ in range(3):
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    return _SPACE.sub(" ", _TAG.sub(" ", text)).strip()


def prepare_enrichment(con: sqlite3.Connection) -> None:
    con.executescript(ENRICHMENT_SCHEMA)


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_number(value: str) -> float | None:
    """Parse grouped US and European numbers plus ``k``/``m`` suffixes."""
    raw = str(value or "").strip().replace("\u00a0", "").replace(" ", "")
    multiplier = 1.0
    if raw[-1:].lower() == "k":
        multiplier, raw = 1_000.0, raw[:-1]
    elif raw[-1:].lower() == "m":
        multiplier, raw = 1_000_000.0, raw[:-1]
    if not raw:
        return None
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        grouping = "." if decimal == "," else ","
        raw = raw.replace(grouping, "").replace(decimal, ".")
    elif "," in raw or "." in raw:
        separator = "," if "," in raw else "."
        pieces = raw.split(separator)
        if len(pieces) > 2 or (len(pieces) == 2 and len(pieces[1]) == 3):
            raw = "".join(pieces)
        else:
            raw = ".".join(pieces)
    try:
        result = float(raw) * multiplier
    except ValueError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _currency(*tokens: str | None) -> str:
    values = [str(token).strip() for token in tokens if token and str(token).strip()]
    normalized = {
        _CURRENCY_MAP.get(value.lower(), value.upper() if len(value) == 3 else "UNKNOWN")
        for value in values
    }
    normalized.discard("UNKNOWN")
    return next(iter(normalized)) if len(normalized) == 1 else "UNKNOWN"


def _component(context: str) -> str:
    lower = context.lower()
    if re.search(r"\b(on-target earnings|ote)\b", lower):
        return "ote"
    if "commission" in lower:
        return "commission"
    if "bonus" in lower:
        return "bonus"
    if re.search(r"\b(equity|stock|rsu)\b", lower):
        return "equity"
    if "stipend" in lower:
        return "stipend"
    if re.search(r"\b(base salary|base pay|salary|salaried|annual base)\b", lower):
        return "base_salary"
    return "unknown"


def _value_kind(minimum: float | None, maximum: float | None, context: str = "") -> str:
    lower = context.lower()
    if "target" in lower:
        return "target"
    if "average" in lower:
        return "average"
    if minimum is not None and maximum is not None:
        return "exact" if minimum == maximum else "range"
    if re.search(r"\b(up to|maximum|max)\b", lower):
        return "maximum"
    return "minimum"


def _bounds_kind(minimum: float | None, maximum: float | None) -> str:
    """Value shape for ATS fields, where which bound is present is authoritative."""
    if minimum is not None and maximum is not None:
        return "exact" if minimum == maximum else "range"
    return "minimum" if minimum is not None else "maximum"


def _range(
    *, minimum: float | None, maximum: float | None, currency: str, period: str,
    component: str, source_type: str, source_field: str, evidence: str,
    parser_rule: str, location_scope: str = "", value_kind: str | None = None,
) -> dict[str, Any] | None:
    if minimum is None and maximum is None:
        return None
    if period not in PERIODS or component not in COMPONENTS:
        return None
    if minimum is not None and maximum is not None and minimum > maximum:
        minimum, maximum = maximum, minimum
    annual_min = minimum * 12 if minimum is not None and period == "month" else minimum
    annual_max = maximum * 12 if maximum is not None and period == "month" else maximum
    if period != "year" and period != "month":
        annual_min = annual_max = None
    return {
        "component": component,
        "value_kind": value_kind or _value_kind(minimum, maximum, evidence),
        "currency": (currency or "UNKNOWN").upper(),
        "period": period,
        "min_value": minimum,
        "max_value": maximum,
        "annual_min_value": annual_min,
        "annual_max_value": annual_max,
        "location_scope": html_to_text(location_scope),
        "source_type": source_type,
        "source_field": source_field,
        "evidence_text": html_to_text(evidence)[:1200],
        "parser_rule": parser_rule,
    }


def _period(
    context: str, values: tuple[float | None, float | None], *, allow_hourly: bool = False
) -> str | None:
    if re.search(r"(?:/|per\s+|an?\s+)(?:hour|hr)\b|\bhourly\b", context, re.I):
        return "hour" if allow_hourly else None
    if _NON_SALARY_PERIOD.search(context):
        return None
    if _MONTH.search(context):
        return "month"
    if _YEAR.search(context):
        return "year"
    if _ONE_TIME.search(context):
        return "one_time"
    present = [value for value in values if value is not None]
    if allow_hourly and present and max(present) <= 500:
        return "hour"
    # A five-figure unsuffixed pay range in a salary context is overwhelmingly
    # annual. Small ambiguous values are left for the negative audit queue.
    return "year" if present and min(present) >= 10_000 else "unknown"


def parse_salary_text(
    text: str,
    *,
    source_type: str = "description_rule",
    source_field: str = "description",
    structural: bool = False,
    location_scope: str = "",
) -> list[dict[str, Any]]:
    """Extract conservative annual/monthly salaried ranges from text."""
    clean = html_to_text(text)
    found: list[dict[str, Any]] = []
    for match in _MONEY.finditer(clean):
        tokens = (match.group("prefix"), match.group("suffix1"),
                  match.group("prefix2"), match.group("suffix2"))
        if not any(tokens):
            continue
        start, end = max(0, match.start() - 180), min(len(clean), match.end() + 180)
        context = clean[start:end]
        if _FALSE_CONTEXT.search(context):
            continue
        if not structural and not _PAY_CONTEXT.search(context):
            continue
        minimum = parse_number(match.group("first"))
        maximum = parse_number(match.group("second")) if match.group("second") else None
        period = _period(context, (minimum, maximum), allow_hourly=structural)
        if period is None:  # hourly/daily/weekly are outside this pipeline's scope
            continue
        item = _range(
            minimum=minimum,
            maximum=maximum,
            currency=_currency(*tokens),
            period=period,
            component=_component(context),
            source_type=source_type,
            source_field=source_field,
            evidence=context,
            parser_rule="money_range_with_salary_context",
            location_scope=location_scope,
        )
        if item:
            found.append(item)
    return found


def _ashby_component(value: Any) -> str:
    kind = re.sub(r"[^a-z]", "", str(value or "").lower())
    if kind in {"salary", "basesalary", "basepay"}:
        return "base_salary"
    if kind in {"ontargetearnings", "ote", "totalcash"}:
        return "ote"
    if "commission" in kind:
        return "commission"
    if "bonus" in kind:
        return "bonus"
    if any(token in kind for token in ("equity", "stock", "rsu")):
        return "equity"
    if "stipend" in kind:
        return "stipend"
    return "other" if kind else "unknown"


def _ashby_component_range(
    component: dict[str, Any], location_scope: str, source_field: str,
    tier_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    interval = str(component.get("interval") or "1 YEAR").strip().upper()
    if interval in {"1 YEAR", "YEAR", "ANNUAL", "YEARLY"}:
        period = "year"
    elif interval in {"1 HOUR", "HOUR", "HOURLY"}:
        period = "hour"
    elif interval in {"1 MONTH", "MONTH", "MONTHLY"}:
        period = "month"
    else:
        return None, any(unit in interval for unit in ("HOUR", "DAY", "WEEK"))
    minimum = _finite_number(component.get("minValue"))
    maximum = _finite_number(component.get("maxValue"))
    evidence: dict[str, Any] = {"component": component}
    if tier_context:
        evidence["tier"] = tier_context
    item = _range(
        minimum=minimum,
        maximum=maximum,
        currency=str(component.get("currencyCode") or component.get("currency") or "UNKNOWN"),
        period=period,
        component=_ashby_component(component.get("compensationType")),
        source_type="ats_structured",
        source_field=source_field,
        evidence=json.dumps(evidence, sort_keys=True, ensure_ascii=False),
        parser_rule="ashby_compensation_component",
        location_scope=location_scope,
        value_kind=_bounds_kind(minimum, maximum),
    )
    return item, False


def _structured_ashby(job: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    comp = job.get("compensation")
    if not isinstance(comp, dict):
        return [], False

    items: list[dict[str, Any]] = []
    ignored_non_salary = False
    tiers = comp.get("compensationTiers")
    if isinstance(tiers, list) and tiers:
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            location_scope = str(tier.get("title") or "")
            tier_context = {
                key: tier.get(key)
                for key in ("id", "title", "additionalInformation", "tierSummary")
                if tier.get(key) not in (None, "")
            }
            for component in tier.get("components") or []:
                if not isinstance(component, dict):
                    continue
                item, ignored = _ashby_component_range(
                    component,
                    location_scope,
                    "compensation.compensationTiers[].components[]",
                    tier_context,
                )
                ignored_non_salary = ignored_non_salary or ignored
                if item:
                    items.append(item)
    else:
        components = comp.get("summaryComponents")
        if isinstance(components, list) and components:
            for component in components:
                if not isinstance(component, dict):
                    continue
                item, ignored = _ashby_component_range(
                    component, "", "compensation.summaryComponents[]"
                )
                ignored_non_salary = ignored_non_salary or ignored
                if item:
                    items.append(item)
        elif any(key in comp for key in ("minValue", "maxValue")):
            # Compatibility with the older flat shape.
            legacy = dict(comp)
            legacy.setdefault("compensationType", "Salary")
            item, ignored_non_salary = _ashby_component_range(
                legacy, "", "compensation"
            )
            if item:
                items.append(item)
    return items, ignored_non_salary


def _structured_lever(job: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    comp = job.get("salaryRange")
    if not isinstance(comp, dict):
        return [], False
    interval = str(comp.get("interval") or "").lower().replace("_", "-")
    if "year" in interval or "annual" in interval:
        period = "year"
    elif "hour" in interval:
        period = "hour"
    elif "month" in interval:
        period = "month"
    else:
        return [], any(unit in interval for unit in ("hour", "day", "week"))
    minimum = _finite_number(comp.get("min"))
    maximum = _finite_number(comp.get("max"))
    item = _range(
        minimum=minimum,
        maximum=maximum,
        currency=str(comp.get("currency") or "UNKNOWN"),
        period=period,
        component="base_salary",
        source_type="ats_structured",
        source_field="salaryRange",
        evidence=json.dumps(comp, sort_keys=True, ensure_ascii=False),
        parser_rule="lever_salary_range",
        value_kind=_bounds_kind(minimum, maximum),
    )
    return ([item] if item else []), False


def _greenhouse_rendered(content: str) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for match in _PAY_BLOCK.finditer(str(content or "")):
        body = match.group("body")
        title_match = _TITLE.search(body)
        title = html_to_text(title_match.group("title")) if title_match else ""
        ranges.extend(parse_salary_text(
            body,
            source_type="ats_rendered",
            source_field="content-pay-transparency",
            structural=True,
            location_scope=title,
        ))
    return ranges


def _semantic_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(item.get(key) for key in (
        "component", "value_kind", "currency", "period", "min_value", "max_value",
        "location_scope",
    ))


def _merge_ranges(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse corroborating sources while retaining conflicting ranges."""
    # A bare "$" is intentionally stored as UNKNOWN when it stands alone. When
    # the ATS supplies the same numerical range with an ISO currency, however,
    # that authoritative field resolves the symbol without guessing a locale.
    structured = [item for item in items if item["source_type"] == "ats_structured"]
    for item in items:
        if item["currency"] != "UNKNOWN":
            continue
        matches = [candidate for candidate in structured if all(
            item.get(key) == candidate.get(key)
            for key in ("component", "value_kind", "period", "min_value", "max_value")
        )]
        currencies = {candidate["currency"] for candidate in matches}
        if len(currencies) == 1:
            item["currency"] = currencies.pop()

    # Generic parsing sees the text inside a Greenhouse pay block a second time
    # after HTML normalization. Attach it to the structurally labeled range so it
    # becomes corroborating evidence, not a duplicate with a blank location label.
    higher_priority = [item for item in items if item["source_type"] != "description_rule"]
    for item in items:
        if item["source_type"] != "description_rule" or item["location_scope"]:
            continue
        matches = [candidate for candidate in higher_priority if all(
            item.get(key) == candidate.get(key)
            for key in (
                "component", "value_kind", "currency", "period", "min_value", "max_value"
            )
        )]
        scopes = {candidate["location_scope"] for candidate in matches}
        if len(scopes) == 1:
            item["location_scope"] = scopes.pop()

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        key = _semantic_key(item)
        evidence = {
            "source_type": item["source_type"],
            "source_field": item["source_field"],
            "text": item["evidence_text"],
            "parser_rule": item["parser_rule"],
        }
        existing = merged.get(key)
        if existing is None:
            item = dict(item)
            item["evidence"] = [evidence]
            merged[key] = item
            continue
        existing["evidence"].append(evidence)
        if _SOURCE_PRIORITY[item["source_type"]] > _SOURCE_PRIORITY[existing["source_type"]]:
            evidence_list = existing["evidence"]
            existing.update(item)
            existing["evidence"] = evidence_list
    result = list(merged.values())
    best = max((_SOURCE_PRIORITY[r["source_type"]] for r in result), default=0)
    for item in result:
        item["is_preferred"] = int(_SOURCE_PRIORITY[item["source_type"]] == best)
        item["is_corroborated"] = int(len({e["source_type"] for e in item["evidence"]}) > 1)
        # Source belongs in the persistence identity even when the normalized values
        # match. This preserves provenance and lets native data supersede an LLM row
        # without rewriting its historical record in place.
        key_json = json.dumps(
            (*_semantic_key(item), item["source_type"]),
            separators=(",", ":"), ensure_ascii=True,
        )
        item["range_key"] = hashlib.sha256(key_json.encode()).hexdigest()[:32]
    return result


def has_usable_native_compensation(result: dict[str, Any]) -> bool:
    """Whether native data is sufficient to bypass description inference."""
    return any(
        item.get("source_type") in NATIVE_SOURCES
        and item.get("component") in PRIMARY_COMPONENTS
        and item.get("currency") not in {None, "", "UNKNOWN"}
        and item.get("period") in {"year", "hour"}
        and (item.get("min_value") is not None or item.get("max_value") is not None)
        for item in result.get("ranges", [])
        if isinstance(item, dict)
    )


def llm_result_ranges(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Serialize validated v3-simple output into the shared compensation model."""
    items: list[dict[str, Any]] = []
    for value in result.get("ranges", []) if isinstance(result, dict) else []:
        if not isinstance(value, dict):
            continue
        minimum = _finite_number(value.get("min_value"))
        maximum = _finite_number(value.get("max_value"))
        item = _range(
            minimum=minimum,
            maximum=maximum,
            currency=str(value.get("currency") or "UNKNOWN"),
            period=str(value.get("period") or "unknown"),
            component="unknown",
            source_type="llm_description",
            source_field="description",
            evidence=str(value.get("evidence_text") or ""),
            parser_rule="nuextract-v3-simple",
            value_kind=_bounds_kind(minimum, maximum),
        )
        if item:
            items.append(item)
    return _merge_ranges(items)


def enrich_job(
    ats: str,
    job_id: str,
    description: str,
    raw_job: dict[str, Any] | None = None,
    processed_at: str | None = None,
) -> dict[str, Any]:
    """Build one job-level result and zero or more compensation ranges."""
    when = processed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    raw = raw_job if isinstance(raw_job, dict) else {}
    fingerprint_payload = {
        "description": description or "",
        "structured": raw.get("compensation") if ats == "ashby" else raw.get("salaryRange"),
        "rendered": (
            raw.get("content") if ats == "greenhouse"
            else raw.get("salaryDescriptionPlain") or raw.get("salaryDescription")
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    try:
        items: list[dict[str, Any]] = []
        ignored_non_salary = False
        if ats == "ashby":
            structured, ignored_non_salary = _structured_ashby(raw)
            items.extend(structured)
        elif ats == "lever":
            structured, ignored_non_salary = _structured_lever(raw)
            items.extend(structured)
            rendered = raw.get("salaryDescriptionPlain") or raw.get("salaryDescription") or ""
            items.extend(parse_salary_text(
                str(rendered), source_type="ats_rendered", source_field="salaryDescription",
                structural=True,
            ))
        elif ats == "greenhouse":
            items.extend(_greenhouse_rendered(str(raw.get("content") or description or "")))

        ranges = _merge_ranges(items)
        preferred = max(
            (r["source_type"] for r in ranges),
            key=lambda value: _SOURCE_PRIORITY[value],
            default="",
        )
        status = "complete" if ranges else (
            "non_salary_compensation" if ignored_non_salary else "no_compensation"
        )
        return {
            "ats": ats,
            "job_id": str(job_id),
            "parser_version": PARSER_VERSION,
            "status": status,
            "preferred_source": preferred,
            "range_count": len(ranges),
            "source_fingerprint": fingerprint,
            "processed_at": when,
            "error": "",
            "ranges": ranges,
        }
    except Exception as exc:  # one malformed posting must not fail its entire board
        return {
            "ats": ats,
            "job_id": str(job_id),
            "parser_version": PARSER_VERSION,
            "status": "error",
            "preferred_source": "",
            "range_count": 0,
            "source_fingerprint": fingerprint,
            "processed_at": when,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "ranges": [],
        }


def save_enrichments(
    con: sqlite3.Connection,
    enrichments: list[dict[str, Any]],
    observed_at: str,
) -> tuple[int, int]:
    """Upsert native ATS enrichment without modifying LLM-owned rows."""
    prepare_enrichment(con)
    saved_ranges = 0
    errors = 0
    for result in enrichments:
        ats, job_id = result["ats"], result["job_id"]
        con.execute(
            "INSERT INTO job_enrichment "
            "(ats,job_id,parser_version,status,preferred_source,range_count,"
            "source_fingerprint,processed_at,error) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ats,job_id) DO UPDATE SET "
            "parser_version=excluded.parser_version,status=excluded.status,"
            "preferred_source=excluded.preferred_source,range_count=excluded.range_count,"
            "source_fingerprint=excluded.source_fingerprint,"
            "processed_at=excluded.processed_at,error=excluded.error",
            (
                ats, job_id, result["parser_version"], result["status"],
                result.get("preferred_source", ""), result.get("range_count", 0),
                result["source_fingerprint"], result["processed_at"], result.get("error", ""),
            ),
        )
        errors += result["status"] == "error"
        if result["status"] in SUCCESS_STATUSES:
            saved_ranges += save_source_ranges(
                con, ats, job_id, result.get("ranges", []), NATIVE_SOURCES, observed_at
            )
        refresh_enrichment_summary(con, ats, job_id, fallback_status=result["status"])
    return saved_ranges, errors


def save_source_ranges(
    con: sqlite3.Connection,
    ats: str,
    job_id: str,
    ranges: list[dict[str, Any]],
    owned_sources: set[str],
    observed_at: str,
) -> int:
    """Replace only one producer's current rows while retaining source history."""
    current_keys: list[str] = []
    saved = 0
    for item in ranges:
        if item.get("source_type") not in owned_sources:
            continue
        current_keys.append(item["range_key"])
        con.execute(
            "INSERT INTO job_compensation_ranges "
            "(ats,job_id,range_key,component,value_kind,currency,period,min_value,"
            "max_value,annual_min_value,annual_max_value,location_scope,source_type,"
            "source_field,evidence_text,evidence_json,parser_rule,is_preferred,"
            "is_corroborated,first_seen,last_seen,removed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT(ats,job_id,range_key) DO UPDATE SET "
            "source_type=excluded.source_type,source_field=excluded.source_field,"
            "evidence_text=excluded.evidence_text,evidence_json=excluded.evidence_json,"
            "parser_rule=excluded.parser_rule,is_preferred=excluded.is_preferred,"
            "is_corroborated=excluded.is_corroborated,last_seen=excluded.last_seen,"
            "removed_at=NULL",
            (
                ats, job_id, item["range_key"], item["component"], item["value_kind"],
                item["currency"], item["period"], item["min_value"], item["max_value"],
                item["annual_min_value"], item["annual_max_value"], item["location_scope"],
                item["source_type"], item["source_field"], item["evidence_text"],
                json.dumps(item["evidence"], ensure_ascii=False), item["parser_rule"],
                item["is_preferred"], item["is_corroborated"], observed_at, observed_at,
            ),
        )
        saved += 1
    source_marks = ",".join("?" for _ in owned_sources)
    params: list[Any] = [observed_at, ats, job_id, *sorted(owned_sources)]
    sql = (
        "UPDATE job_compensation_ranges SET removed_at=COALESCE(removed_at, ?) "
        f"WHERE ats=? AND job_id=? AND removed_at IS NULL AND source_type IN ({source_marks})"
    )
    if current_keys:
        sql += " AND range_key NOT IN (" + ",".join("?" for _ in current_keys) + ")"
        params.extend(current_keys)
    con.execute(sql, params)
    return saved


def refresh_enrichment_summary(
    con: sqlite3.Connection,
    ats: str,
    job_id: str,
    fallback_status: str = "no_compensation",
) -> None:
    """Recompute job-level status and preference across all current producers."""
    current = con.execute(
        "SELECT id,source_type,component,currency,period,min_value,max_value "
        "FROM job_compensation_ranges "
        "WHERE ats=? AND job_id=? AND removed_at IS NULL",
        (ats, job_id),
    ).fetchall()
    def effective_priority(row: tuple[Any, ...]) -> int:
        _, source, component, currency, period, minimum, maximum = row
        usable_primary = (
            component in PRIMARY_COMPONENTS
            and currency not in {None, "", "UNKNOWN"}
            and period in {"year", "hour"}
            and (minimum is not None or maximum is not None)
        )
        if source == "ats_structured" and usable_primary:
            return 30
        if source == "ats_rendered" and usable_primary:
            return 20
        if source == "llm_description" and usable_primary:
            return 10
        return 0

    best = max((effective_priority(row) for row in current), default=-1)
    preferred_ids = [row[0] for row in current if effective_priority(row) == best]
    con.execute(
        "UPDATE job_compensation_ranges SET is_preferred=0 WHERE ats=? AND job_id=?",
        (ats, job_id),
    )
    if preferred_ids:
        con.execute(
            "UPDATE job_compensation_ranges SET is_preferred=1 WHERE id IN ("
            + ",".join("?" for _ in preferred_ids) + ")",
            preferred_ids,
        )
    status = "complete" if current else fallback_status
    preferred = next((row[1] for row in current if row[0] in preferred_ids), "")
    con.execute(
        "UPDATE job_enrichment SET status=?,preferred_source=?,range_count=? "
        "WHERE ats=? AND job_id=?",
        (status, preferred, len(current), ats, job_id),
    )


def backfill(db_path: Path, ats: str = "all", only_missing: bool = False) -> tuple[int, int, int]:
    """The old fuzzy description backfill is intentionally disabled."""
    raise ValueError(
        "deterministic description backfill is disabled; native ATS values are "
        "captured during scans and future missing values use job_search/salary/llm.py"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Legacy deterministic description backfill (disabled; use job_search/salary/llm.py "
            "for future new/changed jobs)."
        )
    )
    parser.add_argument("--db", default="job-boards.db")
    parser.add_argument("--ats", choices=("all", "ashby", "greenhouse", "lever"), default="all")
    parser.add_argument(
        "--only-missing", action="store_true",
        help="legacy option retained for a clear migration error",
    )
    args = parser.parse_args()
    db = Path(args.db)
    if not db.is_absolute():
        db = Path(__file__).resolve().parents[2] / db
    try:
        processed, ranges, errors = backfill(db, args.ats, args.only_missing)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f"complete: {processed:,} jobs, {ranges:,} ranges, {errors:,} errors -> {db}")


if __name__ == "__main__":
    main()
