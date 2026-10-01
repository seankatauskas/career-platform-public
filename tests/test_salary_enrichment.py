#!/usr/bin/env python3
"""Offline tests for salary extraction and persistence."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from job_search.collection.boards import board_url, save, scan_board
from job_search.salary.enrichment import backfill, enrich_job, html_to_text, parse_number, parse_salary_text


def test_recursive_html_decoding() -> None:
    assert html_to_text("&lt;p&gt;Salary &amp;amp; benefits&lt;/p&gt;") == "Salary & benefits"


def test_locale_numbers_and_suffixes() -> None:
    assert parse_number("120,000") == 120000
    assert parse_number("120.000") == 120000
    assert parse_number("120.500,50") == 120500.5
    assert parse_number("125k") == 125000


def test_description_range_and_exact_salary() -> None:
    ranges = parse_salary_text("The annual base salary range is $120,000–$150,000 USD.")
    assert len(ranges) == 1
    assert (ranges[0]["min_value"], ranges[0]["max_value"]) == (120000, 150000)
    assert ranges[0]["currency"] == "USD"
    exact = parse_salary_text("This role pays EUR 9.000 per month as base salary.")
    assert exact[0]["value_kind"] == "minimum"
    assert exact[0]["annual_min_value"] == 108000


def test_hourly_and_unrelated_money_are_excluded() -> None:
    assert parse_salary_text("The hourly pay range is $50-$70 per hour.") == []
    assert parse_salary_text("We raised $120m in funding at a $1b valuation.") == []


def test_single_number_preserves_missing_bound() -> None:
    item = parse_salary_text("Starting base salary is $125,000 annually.")[0]
    assert item["min_value"] == 125000
    assert item["max_value"] is None
    assert item["value_kind"] == "minimum"


def test_one_time_bonus_is_preserved_but_not_annualized() -> None:
    item = parse_salary_text("We provide a one-time signing bonus of $10,000.")[0]
    assert item["component"] == "bonus"
    assert item["period"] == "one_time"
    assert item["annual_min_value"] is None


def test_ashby_structured_is_used_without_generic_description_parsing() -> None:
    raw = {
        "compensation": {
            "interval": "1 YEAR", "minValue": 100000,
            "maxValue": 140000, "currency": "usd",
        }
    }
    result = enrich_job(
        "ashby", "a1", "Base salary is $100,000-$140,000 annually.", raw
    )
    assert result["range_count"] == 1
    assert result["preferred_source"] == "ats_structured"
    assert result["ranges"][0]["is_corroborated"] == 0
    assert len(result["ranges"][0]["evidence"]) == 1


def test_current_ashby_tiers_preserve_location_and_components() -> None:
    raw = {"compensation": {
        "compensationTierSummary": "$200K – $260K • Offers Bonus",
        "compensationTiers": [{
            "id": "tier-1", "title": "Zone A", "tierSummary": "$200K – $260K",
            "components": [
                {
                    "compensationType": "Salary", "interval": "1 YEAR",
                    "currencyCode": "USD", "minValue": 200000, "maxValue": 260000,
                },
                {
                    "compensationType": "Bonus", "interval": "1 YEAR",
                    "currencyCode": "USD", "minValue": 20000, "maxValue": 30000,
                },
                {
                    "compensationType": "EquityCashValue", "interval": "1 YEAR",
                    "currencyCode": "USD", "minValue": None, "maxValue": None,
                },
            ],
        }],
        "summaryComponents": [{
            "compensationType": "Salary", "interval": "1 YEAR",
            "currencyCode": "USD", "minValue": 200000, "maxValue": 260000,
        }],
    }}
    result = enrich_job("ashby", "nested", "", raw)
    assert result["range_count"] == 2  # tiers win; summaryComponents is only fallback
    assert {item["component"] for item in result["ranges"]} == {"base_salary", "bonus"}
    assert {item["location_scope"] for item in result["ranges"]} == {"Zone A"}
    assert all(item["source_type"] == "ats_structured" for item in result["ranges"])


def test_ashby_monthly_is_annualized_and_hourly_is_preserved() -> None:
    monthly = enrich_job("ashby", "1", "", {
        "compensation": {
            "interval": "1 MONTH", "minValue": 10000,
            "maxValue": 12000, "currency": "USD",
        }
    })
    assert monthly["ranges"][0]["annual_max_value"] == 144000
    hourly = enrich_job("ashby", "2", "", {
        "compensation": {
            "interval": "1 HOUR", "minValue": 50,
            "maxValue": 70, "currency": "USD",
        }
    })
    assert hourly["status"] == "complete"
    assert hourly["ranges"][0]["period"] == "hour"
    assert hourly["ranges"][0]["annual_max_value"] is None
    maximum_only = enrich_job("ashby", "3", "", {
        "compensation": {
            "interval": "1 YEAR", "minValue": None,
            "maxValue": 160000, "currency": "USD",
        }
    })
    assert maximum_only["ranges"][0]["value_kind"] == "maximum"


def test_greenhouse_pay_block_preserves_location_label() -> None:
    content = (
        '<div class="content-pay-transparency"><div class="pay-input">'
        '<div class="title">US Pay Range</div><div class="pay-range">'
        '<span>$90,000</span><span>—</span><span>$110,000 USD</span>'
        '</div></div></div>'
    )
    result = enrich_job("greenhouse", "g1", content, {"content": content})
    assert result["range_count"] == 1
    assert result["ranges"][0]["source_type"] == "ats_rendered"
    assert result["ranges"][0]["location_scope"] == "US Pay Range"


def test_greenhouse_unsuffixed_small_pay_block_is_hourly() -> None:
    content = (
        '<div class="content-pay-transparency"><div class="pay-input">'
        '<div class="title">US Pay Range</div><div class="pay-range">'
        '<span>$20</span><span>—</span><span>$24 USD</span>'
        '</div></div></div>'
    )
    result = enrich_job("greenhouse", "hourly", content, {"content": content})
    assert result["ranges"][0]["period"] == "hour"
    assert result["ranges"][0]["annual_min_value"] is None


def _job(ats: str, identifier: str, description: str) -> dict:
    from job_search.collection.boards import FIELDS
    row = {field: "" for field in FIELDS}
    row.update({"ats": ats, "company": "acme", "id": identifier,
                "title": "Engineer", "description": description})
    return row


def test_ranges_are_historical_and_jobs_remain_authoritative() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "jobs.db"
        first = enrich_job("ashby", "1", "", {"compensation": {
            "interval": "1 YEAR", "minValue": 100000,
            "maxValue": 120000, "currency": "USD",
        }})
        save([_job("ashby", "1", "original")], db, "t1", enrichments=[first])
        second = enrich_job("ashby", "1", "", {"compensation": {
            "interval": "1 YEAR", "minValue": 110000,
            "maxValue": 130000, "currency": "USD",
        }})
        save([_job("ashby", "1", "original")], db, "t2", enrichments=[second])
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT description FROM jobs").fetchone()[0] == "original"
            history = con.execute(
                "SELECT min_value,max_value,removed_at FROM job_compensation_ranges "
                "ORDER BY min_value"
            ).fetchall()
        assert history == [(100000.0, 120000.0, "t2"), (110000.0, 130000.0, None)]


def test_deterministic_description_backfill_is_disabled() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "jobs.db"
        save([_job("lever", "1", "Annual salary is EUR 80,000-90,000.")], db, "t1")
        try:
            backfill(db, only_missing=True)
            assert False, "description backfill must be disabled"
        except ValueError as exc:
            assert "disabled" in str(exc)


def test_scan_captures_raw_structured_data_without_changing_rows() -> None:
    from job_search.collection import boards as job_boards
    payload = {"jobs": [{
        "id": "1", "title": "Engineer", "isListed": True,
        "descriptionPlain": "Base salary $100,000-$120,000 annually.",
        "compensation": {
            "interval": "1 YEAR", "minValue": 100000,
            "maxValue": 120000, "currency": "USD",
        },
    }]}
    original = job_boards.fetch
    job_boards.fetch = lambda *args, **kwargs: json.dumps(payload).encode()
    sink = []
    try:
        rows = scan_board("ashby", "acme", None, False, "fuzzy", enrichment_sink=sink)
    finally:
        job_boards.fetch = original
    assert len(rows) == len(sink) == 1
    assert set(rows[0]) == set(job_boards.FIELDS)
    assert sink[0]["preferred_source"] == "ats_structured"


def test_request_shapes_capture_native_salary_without_extra_calls() -> None:
    assert "includeCompensation=true" in board_url("ashby", "acme")
    assert "content=true" in board_url("greenhouse", "acme", want_content=True)
    assert "mode=json" in board_url("lever", "acme", want_content=True)


def test_old_ashby_etag_cannot_skip_first_compensation_response() -> None:
    from job_search.collection.boards import load_etags, save_etags
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "jobs.db"
        with sqlite3.connect(db) as con:
            con.execute(
                "CREATE TABLE board_etag (ats TEXT,company TEXT,etag TEXT,seen_at TEXT,"
                "includes_descriptions INTEGER NOT NULL DEFAULT 1,"
                "representation_version INTEGER NOT NULL DEFAULT 1,"
                "PRIMARY KEY (ats,company))"
            )
            con.execute("INSERT INTO board_etag VALUES ('ashby','acme','old','t',1,1)")
        assert load_etags(db) == {}
        save_etags(db, {("ashby", "acme"): "new"}, "later")
        assert load_etags(db) == {("ashby", "acme"): "new"}


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} salary enrichment tests)")
