#!/usr/bin/env python3
"""Audit Greenhouse structured pay against already-stored descriptions.

The sample is job-weighted and proportionally stratified by Greenhouse board posting
volume and posting age. Results are append-only and resumable.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_search.collection.boards import NotFound, fetch, plain_text


MONEY_RE = re.compile(
    r"(?:US\$|CA\$|C\$|AU\$|A\$|NZ\$|SG\$|HK\$|R\$|RM|[$£€¥₹₩₱฿₫₦₪₽₺₴₡₲₵₸])"
    r"\s*\d[\d,.]*\s*[kKmMbB]?"
    r"|\b(?:USD|CAD|GBP|EUR|AUD|NZD|JPY|CNY|RMB|INR|CHF|SEK|NOK|DKK|PLN|BRL|"
    r"MXN|SGD|HKD|AED|SAR|QAR|KWD|BHD|OMR|HUF|MYR|ZAR|ILS|TWD|KRW|PHP|THB|"
    r"IDR|VND|NGN|KES|EGP|TRY|RUB|UAH|CZK|RON)\b\s*\d[\d,.]*\s*[kKmMbB]?"
    r"|\d[\d,.]*\s*[kKmMbB]?\s*\b(?:USD|CAD|GBP|EUR|AUD|NZD|JPY|CNY|RMB|INR|"
    r"CHF|SEK|NOK|DKK|PLN|BRL|MXN|SGD|HKD|AED|SAR|QAR|KWD|BHD|OMR|HUF|MYR|"
    r"ZAR|ILS|TWD|KRW|PHP|THB|IDR|VND|NGN|KES|EGP|TRY|RUB|UAH|CZK|RON)\b"
    r"|\d[\d,.]*\s*[kKmMbB]?\s*\b(?:dollars?|euros?|yen|yuan|renminbi|rupees?|"
    r"pesos?|francs?|krona|kronor|kroner|zloty|zlotys|dirhams?|riyals?|shekels?|"
    r"baht|ringgit|rubles?|rands?|reais|lira|forints?|dinars?)\b",
    re.I,
)
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?\s*[kKmMbB]?")
PAY_CONTEXT_RE = re.compile(
    r"[$£€¥₹]|\b(?:pay|salary|compensation|wage|rate|hourly|annual|annually|yearly|"
    r"USD|CAD|GBP|EUR|AUD|NZD|JPY|CNY|INR|CHF|PLN)\b",
    re.I,
)
ZERO_MINOR = {"JPY", "KRW", "VND", "CLP", "PYG", "ISK"}
THREE_MINOR = {"BHD", "JOD", "KWD", "OMR", "TND"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="job-boards.db")
    p.add_argument("--sample-size", type=int, default=2000)
    p.add_argument("--rate", type=float, default=2.0, help="maximum requests/second")
    p.add_argument("--seed", type=int, default=20260830)
    p.add_argument("--prefix", default="greenhouse-pay-sample-2000")
    return p.parse_args()


def parse_date(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (AttributeError, ValueError):
        return None


def volume_bucket(count: int) -> str:
    if count == 1:
        return "1"
    if count <= 4:
        return "2-4"
    if count <= 9:
        return "5-9"
    if count <= 24:
        return "10-24"
    if count <= 49:
        return "25-49"
    if count <= 99:
        return "50-99"
    if count <= 249:
        return "100-249"
    return "250+"


def age_bucket(published: str, now: datetime) -> str:
    parsed = parse_date(published)
    if parsed is None:
        return "unknown"
    days = max(0, (now - parsed.astimezone(timezone.utc)).days)
    if days <= 7:
        return "0-7d"
    if days <= 30:
        return "8-30d"
    if days <= 90:
        return "31-90d"
    if days <= 365:
        return "91-365d"
    return "366d+"


def allocate(groups: dict[str, list[dict]], wanted: int) -> dict[str, int]:
    total = sum(map(len, groups.values()))
    quotas = {key: wanted * len(rows) / total for key, rows in groups.items()}
    out = {key: math.floor(value) for key, value in quotas.items()}
    remaining = wanted - sum(out.values())
    order = sorted(groups, key=lambda key: (quotas[key] - out[key], len(groups[key])), reverse=True)
    for key in order[:remaining]:
        out[key] += 1
    return out


def make_manifest(db_path: str, sample_size: int, seed: int) -> dict:
    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("pragma table_info(jobs)")}
    live_clause = "and closed_at is null" if "closed_at" in columns else ""
    rows = [
        dict(zip(("company", "id", "title", "publishedAt", "description"), row))
        for row in con.execute(
            "select company,id,title,publishedAt,description from jobs "
            f"where ats='greenhouse' {live_clause}"
        )
    ]
    missing = sum(not (row["company"] or "").strip() for row in rows)
    if missing:
        raise SystemExit(f"refusing to stratify: {missing} jobs have no company slug")
    if sample_size > len(rows):
        raise SystemExit("sample exceeds population")

    counts = Counter(row["company"] for row in rows)
    now = datetime.now(timezone.utc)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        cell = f"volume={volume_bucket(counts[row['company']])}|age={age_bucket(row['publishedAt'], now)}"
        row["stratum"] = cell
        groups[cell].append(row)

    allocation = allocate(groups, sample_size)
    rng = random.Random(seed)
    selected = []
    strata = {}
    for cell in sorted(groups):
        population_n = len(groups[cell])
        sample_n = allocation[cell]
        strata[cell] = {
            "population_jobs": population_n,
            "sample_jobs": sample_n,
            "weight": population_n / sample_n if sample_n else None,
        }
        if sample_n:
            for row in rng.sample(groups[cell], sample_n):
                row["weight"] = population_n / sample_n
                selected.append(row)
    rng.shuffle(selected)
    return {
        "created_at": now.isoformat(),
        "population": "all Greenhouse jobs with closed_at IS NULL",
        "population_jobs": len(rows),
        "population_boards": len(counts),
        "missing_company": missing,
        "sample_size": len(selected),
        "seed": seed,
        "stratification": ["live jobs per Greenhouse board", "posting age"],
        "strata": strata,
        "jobs": selected,
    }


def amount_value(token: str) -> Decimal | None:
    cleaned = token.replace(",", "").replace(" ", "")
    multiplier = Decimal(1)
    if cleaned and cleaned[-1:].lower() in {"k", "m", "b"}:
        multiplier = {"k": Decimal(1000), "m": Decimal(1000000), "b": Decimal(1000000000)}[
            cleaned[-1].lower()
        ]
        cleaned = cleaned[:-1]
    try:
        return Decimal(cleaned) * multiplier
    except InvalidOperation:
        return None


def major_value(cents: object, currency: str) -> Decimal | None:
    try:
        raw = Decimal(str(cents))
    except (InvalidOperation, TypeError):
        return None
    digits = 0 if currency in ZERO_MINOR else 3 if currency in THREE_MINOR else 2
    return raw / (Decimal(10) ** digits)


def values_duplicated(description: str, pay_range: dict) -> bool:
    currency = str(pay_range.get("currency_type") or "").upper()
    expected = [
        value
        for value in (
            major_value(pay_range.get("min_cents"), currency),
            major_value(pay_range.get("max_cents"), currency),
        )
        if value is not None and value != 0
    ]
    if not expected:
        return False
    numbers = []
    for match in NUMBER_RE.finditer(description):
        value = amount_value(match.group())
        if value is not None:
            numbers.append((value, match.start(), match.end()))
    positions = []
    for wanted in expected:
        matches = [(start, end) for value, start, end in numbers if value == wanted]
        if not matches:
            return False
        positions.append(matches)
    for first in positions[0]:
        candidates = positions[-1]
        for last in candidates:
            start, end = min(first[0], last[0]), max(first[1], last[1])
            if end - start <= 300:
                context = description[max(0, start - 100) : end + 100]
                if PAY_CONTEXT_RE.search(context) or currency.lower() in context.lower():
                    return True
    return False


def ranges_rendered_in_description(description: str, pay_ranges: list[dict]) -> bool:
    """Verify Greenhouse's rendered pay blocks without assuming US number syntax."""
    if not pay_ranges:
        return False
    decoded = description
    for _ in range(3):
        decoded = html.unescape(decoded)
    rendered = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", decoded)).strip()
    titles = all(
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(item.get("title") or ""))))
        .strip() in rendered
        for item in pay_ranges
    )
    currencies = all(str(item.get("currency_type") or "") in rendered for item in pay_ranges)
    return (
        "content-pay-transparency" in decoded
        and decoded.count('class="pay-range"') >= len(pay_ranges)
        and titles
        and currencies
    )


def existing_results(path: Path) -> dict[tuple[str, str], dict]:
    out = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            out[(row["company"], row["id"])] = row
    return out


def request_one(job: dict) -> dict:
    company, job_id = job["company"], str(job["id"])
    url = (
        "https://boards-api.greenhouse.io/v1/boards/"
        f"{quote(company, safe='')}/jobs/{quote(job_id, safe='')}?pay_transparency=true"
    )
    base = {
        "company": company,
        "id": job_id,
        "title": job["title"],
        "publishedAt": job["publishedAt"],
        "stratum": job["stratum"],
        "weight": job["weight"],
        "requestUrl": url,
    }
    try:
        payload = json.loads(fetch(url))
        ranges = payload.get("pay_input_ranges") or []
        meaningful = [
            item for item in ranges
            if isinstance(item, dict)
            and ((item.get("min_cents") or 0) != 0 or (item.get("max_cents") or 0) != 0)
        ]
        description = plain_text(job["description"] or "")
        duplicated = [values_duplicated(description, item) for item in meaningful]
        return {
            **base,
            "ok": True,
            "pay_input_ranges": ranges,
            "structured_pay_present": bool(meaningful),
            "description_has_currency_amount": bool(MONEY_RE.search(description)),
            "all_structured_values_in_description": bool(meaningful) and all(duplicated),
            "structured_pay_rendered_in_description": ranges_rendered_in_description(
                job["description"] or "", meaningful
            ),
            "each_structured_range_in_description": duplicated,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            "error": "",
        }
    except Exception as exc:
        return {
            **base,
            "ok": False,
            "pay_input_ranges": [],
            "structured_pay_present": False,
            "description_has_currency_amount": False,
            "all_structured_values_in_description": False,
            "structured_pay_rendered_in_description": False,
            "each_structured_range_in_description": [],
            "description_sha256": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def interval(records: list[dict], field: str) -> dict:
    weighted = [(float(row["weight"]), int(bool(row[field])), row["company"]) for row in records]
    total_w = sum(weight for weight, _, _ in weighted)
    p = sum(weight * value for weight, value, _ in weighted) / total_w
    n = len(weighted)
    population = 187718
    fpc = math.sqrt(max(0, (population - n) / (population - 1)))
    ordinary_se = math.sqrt(max(p * (1 - p) / n, 0)) * fpc
    by_company = defaultdict(float)
    for weight, value, company in weighted:
        by_company[company] += weight * (value - p)
    clusters = len(by_company)
    cluster_se = math.sqrt(
        (clusters / (clusters - 1) if clusters > 1 else 1)
        * sum(value * value for value in by_company.values())
    ) / total_w
    def ci(se: float) -> list[float]:
        return [round(max(0, p - 1.96 * se) * 100, 3), round(min(1, p + 1.96 * se) * 100, 3)]
    def wilson(successes: float, denominator: float) -> list[float]:
        z = 1.96
        observed = successes / denominator
        scale = 1 + z * z / denominator
        center = (observed + z * z / (2 * denominator)) / scale
        half = z * math.sqrt(observed * (1 - observed) / denominator + z * z / (4 * denominator**2)) / scale
        return [round(max(0, center - half) * 100, 3), round(min(1, center + half) * 100, 3)]
    ordinary_ci = wilson(sum(value for _, value, _ in weighted), n)
    clustered_ci = wilson(clusters, clusters) if p == 1 else ci(cluster_se)
    return {
        "estimate_percent": round(p * 100, 3),
        "unweighted_count": sum(value for _, value, _ in weighted),
        "sample_denominator": n,
        "ordinary_95_percent_ci": ordinary_ci,
        "company_clustered_95_percent_ci": clustered_ci,
        "sampled_companies": clusters,
    }


def summarize(manifest: dict, results: dict[tuple[str, str], dict]) -> dict:
    ordered = [results[(job["company"], str(job["id"]))] for job in manifest["jobs"] if (job["company"], str(job["id"])) in results]
    jobs = {(job["company"], str(job["id"])): job for job in manifest["jobs"]}
    for row in ordered:
        if "structured_pay_rendered_in_description" not in row:
            source = jobs[(row["company"], row["id"])]
            meaningful = [
                item for item in row.get("pay_input_ranges", [])
                if isinstance(item, dict)
                and ((item.get("min_cents") or 0) != 0 or (item.get("max_cents") or 0) != 0)
            ]
            row["structured_pay_rendered_in_description"] = ranges_rendered_in_description(
                source.get("description") or "", meaningful
            )
    ok = [row for row in ordered if row["ok"]]
    fields = (
        "structured_pay_present",
        "description_has_currency_amount",
        "structured_pay_rendered_in_description",
    )
    summary = {
        "population_jobs": manifest["population_jobs"],
        "population_boards": manifest["population_boards"],
        "selected_jobs": manifest["sample_size"],
        "completed_requests": len(ordered),
        "successful_requests": len(ok),
        "failed_requests": len(ordered) - len(ok),
        "metrics": {field: interval(ok, field) for field in fields} if ok else {},
        "conditional_duplication": {},
        "errors": Counter(row["error"].split(":", 1)[0] for row in ordered if not row["ok"]),
    }
    structured = [row for row in ok if row["structured_pay_present"]]
    if structured:
        summary["conditional_duplication"] = interval(
            structured, "structured_pay_rendered_in_description"
        )
    summary["errors"] = dict(summary["errors"])
    return summary


def main() -> int:
    args = parse_args()
    if args.rate <= 0:
        raise SystemExit("--rate must be positive")
    if not os.environ.get("JOB_SCRAPER_CONTACT"):
        raise SystemExit("set JOB_SCRAPER_CONTACT before making network requests")
    manifest_path = Path(f"{args.prefix}.manifest.json")
    results_path = Path(f"{args.prefix}.results.jsonl")
    summary_path = Path(f"{args.prefix}.summary.json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = make_manifest(args.db, args.sample_size, args.seed)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    results = existing_results(results_path)
    pending = [job for job in manifest["jobs"] if (job["company"], str(job["id"])) not in results]
    print(
        f"population={manifest['population_jobs']} sample={manifest['sample_size']} "
        f"already_done={len(results)} pending={len(pending)} rate={args.rate}/s",
        flush=True,
    )
    interval_seconds = 1 / args.rate
    with results_path.open("a", encoding="utf-8") as handle:
        for index, job in enumerate(pending, 1):
            row = request_one(job)
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            handle.flush()
            results[(row["company"], row["id"])] = row
            if index % 100 == 0 or index == len(pending):
                os.fsync(handle.fileno())
                done = len(results)
                structured = sum(r["structured_pay_present"] for r in results.values() if r["ok"])
                errors = sum(not r["ok"] for r in results.values())
                print(f"progress={done}/{manifest['sample_size']} structured={structured} errors={errors}", flush=True)
            if index != len(pending):
                time.sleep(interval_seconds)
    summary = summarize(manifest, results)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["completed_requests"] == manifest["sample_size"] else 1


if __name__ == "__main__":
    sys.exit(main())
