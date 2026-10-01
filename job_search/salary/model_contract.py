#!/usr/bin/env python3
"""Shared production/evaluation contract for local salary extraction."""

from __future__ import annotations

import re
from typing import Any, Callable

from job_search.salary.enrichment import html_to_text


PROMPT_VERSION = "nuextract-v3-simple-production-1"
MODEL_NAME = "nuextract3-4b"
MAX_INPUT_TOKENS = 16_000
OVERLAP_TOKENS = 400

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
                    "period": {"type": "string", "enum": ["year", "hour"]},
                    "min_value": {"type": ["number", "null"]},
                    "max_value": {"type": ["number", "null"]},
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

PROMPT = """Extract explicit primary annual or hourly cash pay from this job posting.

Rules:
- Return each useful compensation candidate with currency, period, min_value,
  max_value, and a short exact evidence quote. Do not output a value_kind field.
- For one fixed amount, put the same number in min_value and max_value.
- For a complete range, populate both bounds.
- For "starts at", "from", "at least", or an amount followed by +, populate only
  min_value and set max_value to null.
- For "up to", "maximum", or "not to exceed", set min_value to null and populate
  only max_value.
- Never annualize or convert units or currencies. Omit monthly, weekly, daily,
  per-project, and one-time amounts.
- Treat base salary, base pay, OTE, target cash compensation, and total cash
  compensation as eligible primary pay.
- Exclude bonus-only, commission-only, equity, stock, stipends, relocation amounts,
  benefits, fundraising, revenue, budgets, and other non-compensation numbers.
- Preserve multiple geographic pay candidates. Return an empty ranges array when no
  qualifying annual or hourly cash pay is present.
"""

TEMPLATE = {
    "ranges": [{
        "currency": "currency",
        "period": ["year", "hour"],
        "min_value": "number-or-null",
        "max_value": "number-or-null",
        "evidence_text": "verbatim-string",
    }]
}


def _normalized(value: str) -> str:
    rendered = " ".join(html_to_text(value).split()).casefold()
    rendered = re.sub(r"\s*([-\u2013\u2014])\s*", r"\1", rendered)
    rendered = re.sub(r"\s+([:;,])", r"\1", rendered)
    rendered = re.sub(r"(?<=\d)\s+(?=\d)", "", rendered)
    rendered = re.sub(r"(?<=,)\s+(?=\d{3}\b)", "", rendered)
    return re.sub(r"(?<=[$€£₹])\s+(?=\d)", "", rendered)


def normalize_result(
    result: dict[str, Any], description: str = ""
) -> tuple[dict[str, Any], list[str]]:
    """Apply only the narrow deterministic rules frozen during calibration."""
    changes: list[str] = []
    ranges = result.get("ranges")
    if not isinstance(ranges, list):
        return result, changes
    if re.search(r"\bcommission[- ]only\b", description, re.IGNORECASE):
        if ranges:
            result["ranges"] = []
            changes.append("ranges: removed because posting explicitly says commission-only")
        return result, changes
    kept: list[Any] = []
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            kept.append(item)
            continue
        numeric = [
            value for value in (item.get("min_value"), item.get("max_value"))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if not numeric:
            changes.append(f"ranges[{index}]: removed because it has no numeric bound")
            continue
        period = item.get("period")
        if isinstance(period, list) and len(period) == 1 and period[0] in {"year", "hour"}:
            item["period"] = period[0]
            changes.append(f"ranges[{index}].period: singleton enum list to scalar")
        evidence = str(item.get("evidence_text") or "")
        has_explicit_unit = re.search(
            r"(?:/\s*(?:hr|hour|yr|year)|per\s+(?:hour|year)|hourly|annually|annual)",
            evidence,
            re.IGNORECASE,
        )
        if (
            item.get("period") == "year"
            and max(numeric) <= 500
            and not has_explicit_unit
            and "content-pay-transparency" in description
        ):
            item["period"] = "hour"
            changes.append(
                f"ranges[{index}].period: unsuffixed ATS pay field under 500 implies hour"
            )
        kept.append(item)
    result["ranges"] = kept
    return result, changes


def validate_result(result: dict[str, Any], description: str) -> list[str]:
    ranges = result.get("ranges") if isinstance(result, dict) else None
    if not isinstance(ranges, list):
        return ["ranges is not an array"]
    problems: list[str] = []
    posting = _normalized(description)
    allowed = {"currency", "period", "min_value", "max_value", "evidence_text"}
    for index, item in enumerate(ranges, 1):
        if not isinstance(item, dict):
            problems.append(f"range {index} is not an object")
            continue
        extra = set(item) - allowed
        if extra:
            problems.append(f"range {index} has unsupported fields: {', '.join(sorted(extra))}")
        minimum, maximum = item.get("min_value"), item.get("max_value")
        min_number = isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
        max_number = isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
        if minimum is not None and not min_number:
            problems.append(f"range {index} min_value is not numeric or null")
        if maximum is not None and not max_number:
            problems.append(f"range {index} max_value is not numeric or null")
        if not min_number and not max_number:
            problems.append(f"range {index} has no numeric bound")
        if min_number and max_number and minimum > maximum:
            problems.append(f"range {index} has reversed bounds")
        currency = item.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            problems.append(f"range {index} has no currency")
        if item.get("period") not in {"year", "hour"}:
            problems.append(f"range {index} period is not year or hour")
        evidence = item.get("evidence_text")
        if not isinstance(evidence, str) or not evidence.strip():
            problems.append(f"range {index} has no evidence")
        elif _normalized(evidence) not in posting:
            problems.append(f"range {index} evidence is not an exact posting quote")
    return list(dict.fromkeys(problems))


def chunk_text(
    description: str,
    count_tokens: Callable[[str], int],
    max_tokens: int = MAX_INPUT_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[str]:
    """Split long text on paragraphs, retaining bounded context overlap."""
    def hard_split(value: str) -> list[str]:
        pieces: list[str] = []
        remaining = value
        while remaining and count_tokens(remaining) > max_tokens:
            low, high, best = 1, len(remaining), 1
            while low <= high:
                middle = (low + high) // 2
                if count_tokens(remaining[:middle]) <= max_tokens:
                    best, low = middle, middle + 1
                else:
                    high = middle - 1
            piece = remaining[:best]
            pieces.append(piece)
            overlap_start = len(piece)
            low, high = 0, len(piece)
            while low <= high:
                middle = (low + high) // 2
                if count_tokens(piece[middle:]) <= overlap_tokens:
                    overlap_start, high = middle, middle - 1
                else:
                    low = middle + 1
            consumed = best
            remaining = piece[overlap_start:] + remaining[consumed:]
            if len(remaining) >= len(value):  # defensive progress for unusual tokenizers
                remaining = remaining[max(1, consumed):]
            value = remaining
        if remaining:
            pieces.append(remaining)
        return pieces

    text = str(description or "").strip()
    if not text or count_tokens(text) <= max_tokens:
        return [text] if text else []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|(?=<h[1-6]\b)", text) if part.strip()]
    if len(paragraphs) == 1:
        # Stored descriptions are usually flattened. Sentences provide a safe second
        # boundary before the final hard character fallback.
        paragraphs = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    for part in paragraphs:
        candidate = "\n\n".join([*current, part])
        if current and count_tokens(candidate) > max_tokens:
            chunks.append("\n\n".join(current))
            overlap: list[str] = []
            for previous in reversed(current):
                proposed = [previous, *overlap]
                if count_tokens("\n\n".join(proposed)) > overlap_tokens:
                    break
                overlap = proposed
            current = [*overlap, part]
        else:
            current.append(part)
        if count_tokens("\n\n".join(current)) > max_tokens:
            # An indivisible paragraph/sentence is split by exact token counts while
            # retaining a bounded overlap.
            chunks.extend(hard_split("\n\n".join(current)))
            current = []
    if current:
        chunks.append("\n\n".join(current))
    return [chunk for chunk in chunks if chunk]
