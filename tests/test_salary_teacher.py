#!/usr/bin/env python3
"""Offline tests for the bounded hosted-model salary teacher."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search.salary import teacher as salary_teacher


JOBS_DDL = """
CREATE TABLE jobs (
    ats TEXT NOT NULL,
    id TEXT NOT NULL,
    company TEXT,
    title TEXT,
    location TEXT,
    employmentType TEXT,
    publishedAt TEXT,
    jobUrl TEXT,
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ats,id)
)
"""


class SalaryTeacherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "jobs.db"
        with sqlite3.connect(self.db) as con:
            con.execute(JOBS_DDL)
            rows = []
            for ats in ("ashby", "greenhouse", "lever"):
                for index in range(8):
                    if index % 3 == 0:
                        description = "The base salary range is $100,000 to $140,000 per year."
                    elif index % 3 == 1:
                        description = "Benefits include health insurance and meaningful work."
                    else:
                        description = "Compensation is competitive and includes equity."
                    rows.append((
                        ats, f"{ats}-{index}", f"company-{index}", "Engineer", "US",
                        "Full-time", "2026-08-30", "https://example.test/job", description,
                    ))
            con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sample_is_deterministic_balanced_and_idempotent(self) -> None:
        count = salary_teacher.build_sample(
            self.db, sample_size=12, seed=42, company_cap=1, fresh=True
        )
        self.assertEqual(count, 12)
        with sqlite3.connect(self.db) as con:
            first = con.execute(
                "SELECT ats,job_id,stratum,position FROM salary_labeling_queue ORDER BY position"
            ).fetchall()
        self.assertEqual(salary_teacher.build_sample(self.db, sample_size=4), 12)
        salary_teacher.build_sample(
            self.db, sample_size=12, seed=42, company_cap=1, fresh=True
        )
        with sqlite3.connect(self.db) as con:
            second = con.execute(
                "SELECT ats,job_id,stratum,position FROM salary_labeling_queue ORDER BY position"
            ).fetchall()
            per_company = con.execute(
                "SELECT j.ats,j.company,COUNT(*) FROM salary_labeling_queue q "
                "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
                "GROUP BY j.ats,j.company HAVING COUNT(*)>1"
            ).fetchall()
        self.assertEqual(first, second)
        self.assertEqual(per_company, [])
        self.assertEqual({row[0] for row in first}, {"ashby", "greenhouse", "lever"})

    def test_replacement_sample_can_exclude_every_current_job(self) -> None:
        salary_teacher.build_sample(self.db, sample_size=6, seed=42, fresh=True)
        with sqlite3.connect(self.db) as con:
            first = set(con.execute(
                "SELECT ats,job_id FROM salary_labeling_queue"
            ).fetchall())
        salary_teacher.build_sample(
            self.db, sample_size=6, seed=43, fresh=True, exclude_current=True
        )
        with sqlite3.connect(self.db) as con:
            second = set(con.execute(
                "SELECT ats,job_id FROM salary_labeling_queue"
            ).fetchall())
            history = set(con.execute(
                "SELECT ats,job_id FROM salary_sample_history"
            ).fetchall())
        self.assertFalse(first & second)
        self.assertEqual(history, first)

    def test_v2_schema_retains_non_usd_annual_and_hourly_ranges(self) -> None:
        result = {"ranges": [
            {
                "currency": "CAD", "period": "year", "min_value": 100000,
                "max_value": 130000, "evidence_text": "CAD 100,000-130,000 per year",
            },
            {
                "currency": "EUR", "period": "hour", "min_value": 50,
                "max_value": 65, "evidence_text": "EUR 50-65 per hour",
            },
        ]}
        description = "CAD 100,000-130,000 per year; EUR 50-65 per hour"
        self.assertEqual(salary_teacher._validate_result(result, description), [])
        self.assertEqual(salary_teacher.RESULT_SCHEMA["required"], ["ranges"])

    def test_prediction_selection_can_be_locked_to_a_split(self) -> None:
        salary_teacher.build_sample(self.db, sample_size=3, seed=8, fresh=True)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE salary_labeling_queue SET split='evaluation' WHERE position=3"
            )
        calibration = salary_teacher.prediction_estimate(
            self.db, "fireworks", 10, split="calibration"
        )
        evaluation = salary_teacher.prediction_estimate(
            self.db, "fireworks", 10, split="evaluation"
        )
        self.assertEqual(calibration["jobs"], 2)
        self.assertEqual(evaluation["jobs"], 1)

    def test_fireworks_prediction_is_saved_and_resume_skips_it(self) -> None:
        salary_teacher.build_sample(self.db, sample_size=3, seed=1, fresh=True)
        calls = []

        def transport(url, api_key, body, timeout):
            calls.append((url, api_key, body, timeout))
            result = {"ranges": []}
            return {
                "id": "fw-response",
                "choices": [{"message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 100},
            }

        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "test-key"}):
            outcome = salary_teacher.run_predictions(
                self.db, "fireworks", 1, 1.0, delay=0, transport=transport
            )
            self.assertEqual(outcome["completed"], 1)
            salary_teacher.run_predictions(
                self.db, "fireworks", 1, 1.0, delay=0, transport=transport
            )
        self.assertEqual(len(calls), 2)  # second run advances to the next pending job
        self.assertEqual(calls[0][2]["reasoning_effort"], "low")
        self.assertEqual(calls[0][2]["max_tokens"], 2400)
        with sqlite3.connect(self.db) as con:
            row = con.execute(
                "SELECT status,response_id,input_tokens,output_tokens,cost_usd "
                "FROM salary_model_predictions ORDER BY queue_id LIMIT 1"
            ).fetchone()
        self.assertEqual(row[:4], ("complete", "fw-response", 500, 100))
        self.assertAlmostEqual(row[4], 0.0016)

    def test_openai_response_and_bad_evidence_needs_review(self) -> None:
        salary_teacher.build_sample(self.db, sample_size=1, seed=4, fresh=True)
        result = {
            "classification": "salaried",
            "ranges": [{
                "component": "base_salary", "value_kind": "exact", "currency": "USD",
                "period": "year", "min_value": 120000, "max_value": 120000,
                "location_scope": None, "evidence_text": "invented evidence",
            }],
            "confidence": 0.7,
            "notes": "",
        }

        def transport(url, api_key, body, timeout):
            return {
                "id": "resp_1",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(result)}],
                }],
                "usage": {"input_tokens": 400, "output_tokens": 80},
            }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            salary_teacher.run_predictions(
                self.db, "openai", 1, 1.0, delay=0, transport=transport
            )
        with sqlite3.connect(self.db) as con:
            row = con.execute(
                "SELECT status,validation_json FROM salary_model_predictions"
            ).fetchone()
        self.assertEqual(row[0], "needs_review")
        self.assertIn("exact posting quote", row[1])

    def test_empty_billed_response_is_not_retried_and_records_usage(self) -> None:
        salary_teacher.build_sample(self.db, sample_size=1, seed=7, fresh=True)
        calls = []

        def transport(url, api_key, body, timeout):
            calls.append(body)
            return {
                "id": "empty-response",
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "still thinking"},
                }],
                "usage": {"prompt_tokens": 400, "completion_tokens": 2400},
            }

        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "test-key"}):
            outcome = salary_teacher.run_predictions(
                self.db, "fireworks", 1, 1.0, delay=0, retries=3,
                transport=transport,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome["errors"], 1)
        self.assertAlmostEqual(outcome["spent_usd"], 0.0152)
        with sqlite3.connect(self.db) as con:
            row = con.execute(
                "SELECT error_code,input_tokens,output_tokens,cost_usd,response_meta_json "
                "FROM salary_model_predictions"
            ).fetchone()
        self.assertEqual(row[:3], ("invalid_response", 400, 2400))
        self.assertAlmostEqual(row[3], 0.0152)
        self.assertEqual(json.loads(row[4])["finish_reason"], "length")

    def test_budget_guard_runs_before_key_or_transport(self) -> None:
        salary_teacher.build_sample(self.db, sample_size=1, fresh=True)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            salary_teacher.run_predictions(self.db, "openai", 1, 0.000001, delay=0)

    def test_socket_read_timeout_becomes_retryable_provider_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=socket.timeout("read timed out")):
            with self.assertRaises(salary_teacher.ProviderError) as raised:
                salary_teacher.post_json("https://example.test", "key", {})
        self.assertEqual(raised.exception.code, "network_error")
        self.assertTrue(raised.exception.retryable)

    def test_evidence_validation_uses_rendered_html_and_dash_spacing(self) -> None:
        description = (
            '<div class="title">New York Base Pay Range</div>'
            '<span>$100,000</span><span>&mdash;</span><span>$140,000 USD</span>'
        )
        result = {
            "classification": "salaried",
            "ranges": [{
                "component": "base_salary", "value_kind": "range", "currency": "USD",
                "period": "year", "min_value": 100000, "max_value": 140000,
                "location_scope": "New York",
                "evidence_text": "New York Base Pay Range $100,000—$140,000 USD",
            }],
            "confidence": 0.99,
            "notes": "",
        }
        self.assertEqual(salary_teacher._validate_result(result, description), [])

    def test_credential_report_and_auth_check_do_not_reveal_key(self) -> None:
        secret = "fw_test_secret_value"
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": f"  {secret}\n"}):
            report = salary_teacher.credential_report("fireworks")
            self.assertEqual(report["characters_sent"], len(secret))
            self.assertEqual(report["key_kind"], "standard_fireworks_api_key")
            self.assertTrue(report["surrounding_whitespace_removed"])
            self.assertNotIn(secret, json.dumps(report))

            def transport(request, timeout):
                self.assertEqual(request.get_header("Authorization"), f"Bearer {secret}")
                return {"models": [{"name": "example"}]}

            checked = salary_teacher.check_fireworks_auth(transport)
        self.assertTrue(checked["authenticated"])
        self.assertFalse(checked["paid_inference"])


if __name__ == "__main__":
    unittest.main()
