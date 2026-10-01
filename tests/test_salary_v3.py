#!/usr/bin/env python3
"""Offline tests for versioned one-sided salary labeling."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search.salary import v3 as salary_v3
from job_search.salary import v3_review as salary_v3_review


class SalaryV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "jobs.db"
        with sqlite3.connect(self.db) as con:
            con.execute("""
                CREATE TABLE jobs (
                    ats TEXT NOT NULL,id TEXT NOT NULL,company TEXT,title TEXT,
                    location TEXT,employmentType TEXT,publishedAt TEXT,jobUrl TEXT,
                    description TEXT NOT NULL DEFAULT '',PRIMARY KEY(ats,id)
                )
            """)
            con.execute("""
                CREATE TABLE job_enrichment (
                    ats TEXT NOT NULL,job_id TEXT NOT NULL,range_count INTEGER NOT NULL,
                    PRIMARY KEY(ats,job_id)
                )
            """)
            con.execute("""
                CREATE TABLE job_compensation_ranges (
                    ats TEXT NOT NULL,job_id TEXT NOT NULL,value_kind TEXT NOT NULL,
                    period TEXT NOT NULL,removed_at TEXT,evidence_text TEXT NOT NULL
                )
            """)
            descriptions = {
                "minimum": "Compensation starts at $120,000 per year.",
                "maximum": "Compensation is up to $150,000 per year.",
                "exact": "Salary is $135,000 per year.",
                "range": "Salary is $100,000-$140,000 per year.",
                "hard_negative": "Competitive salary and excellent benefits in the United States.",
            }
            for ats in ("ashby", "greenhouse", "lever"):
                for kind, description in descriptions.items():
                    job_id = f"{ats}-{kind}"
                    con.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            ats, job_id, f"{ats}-{kind}", "Engineer", "United States",
                            "Full-time", "2026-08-31", "https://example.test", description,
                        ),
                    )
                    count = 0 if kind == "hard_negative" else 1
                    con.execute("INSERT INTO job_enrichment VALUES (?,?,?)", (ats, job_id, count))
                    if count:
                        con.execute(
                            "INSERT INTO job_compensation_ranges VALUES (?,?,?,?,NULL,?)",
                            (ats, job_id, kind, "year", description),
                        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_validator_enforces_each_bound_shape(self) -> None:
        description = (
            "Starts at $120,000; up to $150,000; exact $130,000; "
            "range $100,000-$140,000 per year."
        )
        result = {"ranges": [
            {"value_kind": "minimum", "currency": "USD", "period": "year",
             "min_value": 120000, "max_value": None, "evidence_text": "Starts at $120,000"},
            {"value_kind": "maximum", "currency": "USD", "period": "year",
             "min_value": None, "max_value": 150000, "evidence_text": "up to $150,000"},
            {"value_kind": "exact", "currency": "USD", "period": "year",
             "min_value": 130000, "max_value": 130000, "evidence_text": "exact $130,000"},
            {"value_kind": "range", "currency": "USD", "period": "year",
             "min_value": 100000, "max_value": 140000,
             "evidence_text": "range $100,000-$140,000 per year"},
        ]}
        self.assertEqual(salary_v3.validate_result(result, description), [])
        result["ranges"][0]["max_value"] = 120000
        self.assertIn("value_kind=minimum", " ".join(salary_v3.validate_result(result, description)))

    def test_evidence_ignores_html_created_space_before_colon(self) -> None:
        description = (
            "<p><strong>Zone 1 (New York, California, Washington)</strong>: "
            "$101,200– $151,800</p>"
        )
        result = {"ranges": [{
            "value_kind": "range", "currency": "USD", "period": "year",
            "min_value": 101200, "max_value": 151800,
            "evidence_text": "Zone 1 (New York, California, Washington): $101,200– $151,800",
        }]}
        self.assertEqual(salary_v3.validate_result(result, description), [])

    def test_evidence_joins_digits_split_across_html_tags(self) -> None:
        description = (
            "<p>The base salary range for this position is<strong>&nbsp;$1</strong>"
            "<strong>5</strong><strong>0,000 – $2</strong><strong>2</strong>"
            "<strong>0,000.&nbsp;</strong></p>"
        )
        result = {"ranges": [{
            "value_kind": "range", "currency": "USD", "period": "year",
            "min_value": 150000, "max_value": 220000,
            "evidence_text": "The base salary range for this position is $150,000 – $220,000.",
        }]}
        self.assertEqual(salary_v3.validate_result(result, description), [])

    def test_evidence_joins_thousands_group_split_after_comma(self) -> None:
        description = (
            "<p>The California annual base salary for this role is currently&nbsp;"
            "<span>$137</span><span>,000</span><span>-</span><span>$193,</span>"
            "<span>000</span><span> Pay Grades</span></p>"
        )
        result = {"ranges": [{
            "value_kind": "range", "currency": "USD", "period": "year",
            "min_value": 137000, "max_value": 193000,
            "evidence_text": (
                "The California annual base salary for this role is currently "
                "$137,000-$193,000"
            ),
        }]}
        self.assertEqual(salary_v3.validate_result(result, description), [])

    def test_targeted_sample_is_versioned_and_balanced(self) -> None:
        targets = {kind: 1 for kind in salary_v3.TARGETS}
        with patch.object(salary_v3, "TARGETS", targets):
            report = salary_v3.build_targeted_sample(
                self.db, batch="test-v3", seed=42, company_cap=1
            )
        self.assertEqual(report["queued"], 5)
        self.assertEqual(report["candidate_kinds"], targets)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM salary_v3_queue WHERE batch='test-v3'"
            ).fetchone()[0], 5)

    def test_prediction_and_review_use_only_v3_tables(self) -> None:
        targets = {kind: 1 for kind in salary_v3.TARGETS}
        with patch.object(salary_v3, "TARGETS", targets):
            salary_v3.build_targeted_sample(self.db, batch="test-v3", seed=7)

        def transport(url, api_key, body, timeout):
            result = {"ranges": []}
            return {
                "id": "fw-v3", "choices": [{"message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            }

        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "test-key"}):
            outcome = salary_v3.run_predictions(
                self.db, "fireworks", 1, 1.0, batch="test-v3", delay=0,
                transport=transport,
            )
        self.assertEqual(outcome["completed"], 1)
        item = salary_v3_review.get_item(self.db, "test-v3")
        salary_v3_review.save_gold(
            self.db, "test-v3", item["id"], "fireworks", None, "v3 test"
        )
        self.assertEqual(salary_v3_review.review_stats(self.db, "test-v3")["gold_labels"], 1)

    def test_timeout_is_retried_then_batch_continues(self) -> None:
        targets = {kind: 1 for kind in salary_v3.TARGETS}
        with patch.object(salary_v3, "TARGETS", targets):
            salary_v3.build_targeted_sample(self.db, batch="test-v3", seed=9)
        calls = []

        def transport(url, api_key, body, timeout):
            calls.append(timeout)
            raise salary_v3.ProviderError(str(socket.timeout("timed out")), "network_error", True)

        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "test-key"}), patch(
            "job_search.salary.v3.time.sleep", return_value=None
        ):
            outcome = salary_v3.run_predictions(
                self.db, "fireworks", 1, 1.0, batch="test-v3", retries=2,
                request_timeout=30, transport=transport,
            )
        self.assertEqual(calls, [30, 30])
        self.assertEqual(outcome["errors"], 1)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                "SELECT status FROM salary_v3_predictions"
            ).fetchone()[0], "retryable_error")


if __name__ == "__main__":
    unittest.main()
