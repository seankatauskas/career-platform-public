#!/usr/bin/env python3
"""Canonical all-currency pay plus the primary USD evaluation projection.

Raw model and gold JSON retain every extracted currency. Only the primary correctness
metric projects those values to USD.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TARGET_CURRENCY = "USD"
TARGET_PERIODS = {"year", "hour"}
EXCLUDED_COMPONENTS = {"bonus", "equity", "stipend", "commission"}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def canonical_all_pay(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every distinct annual/hourly range-or-exact primary pay value.

    Component labels are ignored except for explicit non-target components. Therefore
    base_salary, OTE, target, unknown, and other labels all compare by their numbers.
    A complete pair of bounds is required: equal bounds are exact pay and unequal
    bounds are a range. No period or currency conversion is performed.
    """
    canonical: set[tuple[str, str, int | float, int | float]] = set()
    ranges = result.get("ranges", []) if isinstance(result, dict) else []
    if not isinstance(ranges, list):
        return []
    for item in ranges:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "").upper()
        period = str(item.get("period") or "").lower()
        component = str(item.get("component") or "unknown").lower()
        if not currency or period not in TARGET_PERIODS:
            continue
        if component in EXCLUDED_COMPONENTS:
            continue
        minimum = _number(item.get("min_value"))
        maximum = _number(item.get("max_value"))
        if minimum is None or maximum is None or minimum > maximum:
            continue
        canonical.add((currency, period, minimum, maximum))
    return [
        {
            "currency": currency,
            "period": period,
            "min_value": minimum,
            "max_value": maximum,
        }
        for currency, period, minimum, maximum in sorted(canonical)
    ]


def canonical_usd_pay(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Project stored all-currency pay to the primary USD correctness target."""
    return [
        value for value in canonical_all_pay(result)
        if value["currency"] == TARGET_CURRENCY
    ]


def canonical_key(result: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Hashable representation for exact-set comparison in evaluations."""
    return tuple(
        (item["currency"], item["period"], item["min_value"], item["max_value"])
        for item in canonical_usd_pay(result)
    )


def usd_primary_match(prediction: dict[str, Any], gold: dict[str, Any]) -> bool:
    """Return whether a prediction satisfies the primary USD usability rule.

    A positive gold label needs at least one exact predicted range, and every predicted
    USD range must be supported by gold. A gold label with no USD ranges requires an
    empty USD prediction. Missing additional gold ranges affect recall, not this result.
    """
    predicted = set(canonical_key(prediction))
    expected = set(canonical_key(gold))
    if not expected:
        return not predicted
    return bool(predicted & expected) and predicted <= expected


# Retain the old public name as an alias for callers created before the primary rule
# was relaxed from exact-set equality to supported overlap.
usd_exact_match = usd_primary_match


def canonical_all_pay_v3(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical v3 pay, retaining intentional minimum/maximum null bounds.

    This is separate from the frozen v2 projection so the locked evaluation cannot
    change. A one-sided amount matches only the same shape and published bound; it is
    never silently converted to an exact amount.
    """
    canonical: set[tuple[str, str, str, int | float | None, int | float | None]] = set()
    ranges = result.get("ranges", []) if isinstance(result, dict) else []
    if not isinstance(ranges, list):
        return []
    for item in ranges:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "").upper()
        period = str(item.get("period") or "").lower()
        component = str(item.get("component") or "unknown").lower()
        kind = str(item.get("value_kind") or "").lower()
        minimum = _number(item.get("min_value"))
        maximum = _number(item.get("max_value"))
        if not currency or period not in TARGET_PERIODS or component in EXCLUDED_COMPONENTS:
            continue
        if not kind and minimum is not None and maximum is not None:
            kind = "exact" if minimum == maximum else "range"
        valid = (
            (kind == "exact" and minimum is not None and minimum == maximum)
            or (kind == "range" and minimum is not None and maximum is not None and minimum < maximum)
            or (kind == "minimum" and minimum is not None and maximum is None)
            or (kind == "maximum" and minimum is None and maximum is not None)
        )
        if valid:
            canonical.add((currency, period, kind, minimum, maximum))
    return [
        {
            "currency": currency, "period": period, "value_kind": kind,
            "min_value": minimum, "max_value": maximum,
        }
        for currency, period, kind, minimum, maximum in sorted(
            canonical,
            key=lambda value: tuple("" if item is None else str(item) for item in value),
        )
    ]


def canonical_usd_pay_v3(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Project v3 values to the USD annual/hourly model-selection target."""
    return [value for value in canonical_all_pay_v3(result) if value["currency"] == TARGET_CURRENCY]


def canonical_key_v3(result: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Hashable USD representation including intentional one-sided bound shape."""
    return tuple(
        (
            item["currency"], item["period"], item["value_kind"],
            item["min_value"], item["max_value"],
        )
        for item in canonical_usd_pay_v3(result)
    )


def usd_primary_match_v3(prediction: dict[str, Any], gold: dict[str, Any]) -> bool:
    """Apply supported-overlap scoring to exact, range, minimum, and maximum values."""
    predicted, expected = set(canonical_key_v3(prediction)), set(canonical_key_v3(gold))
    if not expected:
        return not predicted
    return bool(predicted & expected) and predicted <= expected


def canonical_all_pay_v3_simple(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical pay without asking the model to classify the bound shape.

    A fixed value has equal bounds, a range has two distinct bounds, and a one-sided
    amount has one null bound. This lets the representation carry the semantics
    without a redundant ``value_kind`` prediction.
    """
    canonical: set[tuple[str, str, int | float | None, int | float | None]] = set()
    ranges = result.get("ranges", []) if isinstance(result, dict) else []
    if not isinstance(ranges, list):
        return []
    for item in ranges:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "").upper()
        period = str(item.get("period") or "").lower()
        component = str(item.get("component") or "unknown").lower()
        minimum = _number(item.get("min_value"))
        maximum = _number(item.get("max_value"))
        if not currency or period not in TARGET_PERIODS or component in EXCLUDED_COMPONENTS:
            continue
        if minimum is None and maximum is None:
            continue
        if minimum is not None and maximum is not None and minimum > maximum:
            continue
        canonical.add((currency, period, minimum, maximum))
    return [
        {
            "currency": currency, "period": period,
            "min_value": minimum, "max_value": maximum,
        }
        for currency, period, minimum, maximum in sorted(
            canonical,
            key=lambda value: tuple("" if item is None else str(item) for item in value),
        )
    ]


def canonical_usd_pay_v3_simple(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        value for value in canonical_all_pay_v3_simple(result)
        if value["currency"] == TARGET_CURRENCY
    ]


def canonical_figure_key_v3_simple(result: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Return distinct USD figures; bound classification is intentionally ignored."""
    figures: set[tuple[str, str, int | float]] = set()
    for item in canonical_usd_pay_v3_simple(result):
        for key in ("min_value", "max_value"):
            if item[key] is not None:
                figures.add((item["currency"], item["period"], item[key]))
    return tuple(sorted(figures))


def usd_primary_match_v3_simple(prediction: dict[str, Any], gold: dict[str, Any]) -> bool:
    """Accept one matching USD figure and tolerate extra candidates on positives.

    Currency and annual/hourly period remain part of the match. A gold label with no
    qualifying USD figure still requires no USD prediction, preventing fabricated pay.
    """
    predicted = set(canonical_figure_key_v3_simple(prediction))
    expected = set(canonical_figure_key_v3_simple(gold))
    if not expected:
        return not predicted
    return bool(predicted & expected)


def gold_stats(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    totals = {
        "gold_labels": 0,
        "labels_with_target_usd_pay": 0,
        "labels_without_target_usd_pay": 0,
        "annual_values": 0,
        "hourly_values": 0,
        "exact_values": 0,
        "range_values": 0,
        "all_currency_values": 0,
        "non_usd_values_stored": 0,
    }
    with sqlite3.connect(str(db_path)) as con:
        rows = con.execute("SELECT result_json FROM salary_gold_labels")
        for (encoded,) in rows:
            totals["gold_labels"] += 1
            result = json.loads(encoded)
            values = canonical_usd_pay(result)
            all_values = canonical_all_pay(result)
            totals["all_currency_values"] += len(all_values)
            totals["non_usd_values_stored"] += sum(
                value["currency"] != TARGET_CURRENCY for value in all_values
            )
            if values:
                totals["labels_with_target_usd_pay"] += 1
            else:
                totals["labels_without_target_usd_pay"] += 1
            for value in values:
                totals[f"{value['period']}ly_values" if value["period"] == "hour" else "annual_values"] += 1
                shape = "exact_values" if value["min_value"] == value["max_value"] else "range_values"
                totals[shape] += 1
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="job-boards.db")
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    try:
        print(json.dumps(gold_stats(db_path), indent=2))
    except (ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
