#!/usr/bin/env python3
"""Offline tests for the salary validation queue and labels."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from job_search.collection.boards import FIELDS, save
from job_search.salary.enrichment import enrich_job
from job_search.salary.labeler import build_queue, get_item, label_item, stats, undo_last


def _row(ats: str, identifier: str, company: str, description: str) -> dict:
    row = {field: "" for field in FIELDS}
    row.update({
        "ats": ats, "id": identifier, "company": company, "title": f"Role {identifier}",
        "description": description, "jobUrl": f"https://example.com/{identifier}",
    })
    return row


def make_db(directory: str) -> Path:
    db = Path(directory) / "jobs.db"
    rows, enrichments = [], []
    descriptions = [
        "Base salary is $100,000-$120,000 annually.",
        "The annual pay range is USD 90,000 to 110,000.",
        "No compensation is listed.",
        "Competitive compensation and benefits.",
        "A mysterious $140,000 annual salary that should parse.",
        "No pay information here.",
    ]
    for index, description in enumerate(descriptions, 1):
        ats = ("ashby", "greenhouse", "lever")[index % 3]
        rows.append(_row(ats, str(index), f"Company {index}", description))
        raw_job = {}
        if index in {1, 2}:
            ats = "greenhouse"
            rows[-1]["ats"] = ats
            raw_job = {
                "content": (
                    '<div class="content-pay-transparency"><div class="pay-input">'
                    '<div class="title">US Pay Range</div><div class="pay-range">'
                    f'<span>${90 + index * 10},000</span><span>—</span>'
                    f'<span>${110 + index * 10},000 USD</span>'
                    '</div></div></div>'
                )
            }
        enrichments.append(enrich_job(ats, str(index), description, raw_job))
    save(rows, db, "now", enrichments=enrichments)
    return db


def test_queue_contains_both_audits_and_is_stable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_db(directory)
        count = build_queue(db, positive_quota=2, negative_quota=2, company_cap=2)
        assert count == 4
        assert build_queue(db, positive_quota=1, negative_quota=1) == 4
        with sqlite3.connect(db) as con:
            audits = dict(con.execute(
                "SELECT audit_type,COUNT(*) FROM salary_validation_queue GROUP BY audit_type"
            ))
        assert audits == {"negative": 2, "positive": 2}


def test_verdict_rules_stats_and_undo() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_db(directory)
        build_queue(db, positive_quota=2, negative_quota=2)
        item = get_item(db)
        assert item and "description" in item and "ranges" in item
        verdict = "correct" if item["audit_type"] == "positive" else "no_salary"
        label_item(db, item["id"], verdict, "checked")
        assert stats(db)["labeled"] == 1
        undone = undo_last(db)
        assert undone and undone["queue_id"] == item["id"]
        assert stats(db)["labeled"] == 0


def test_non_salaried_pay_is_valid_for_both_audit_types() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_db(directory)
        build_queue(db, positive_quota=2, negative_quota=2)
        with sqlite3.connect(db) as con:
            items = con.execute(
                "SELECT id,audit_type FROM salary_validation_queue "
                "GROUP BY audit_type ORDER BY audit_type"
            ).fetchall()
        assert {audit for _, audit in items} == {"positive", "negative"}
        for queue_id, _ in items:
            label_item(db, queue_id, "non_salaried_pay")
        result = stats(db)
        assert result["verdicts"]["non_salaried_pay"] == 2
        assert result["estimates"]["positive_precision"] is None
        assert result["estimates"]["negative_miss_rate"] is None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} salary labeler tests)")
