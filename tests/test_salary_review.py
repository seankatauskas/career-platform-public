#!/usr/bin/env python3
"""Offline persistence tests for the human salary review layer."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_search.salary import review as salary_review
from job_search.salary import teacher as salary_teacher


class SalaryReviewTests(unittest.TestCase):
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
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "greenhouse", "1", "Example", "Engineer", "US", "Full-time",
                    "2026-08-30", "https://example.test/1",
                    "The annual base salary range is $100,000 to $140,000.",
                ),
            )
        salary_teacher.build_sample(self.db, sample_size=1, fresh=True)
        self.result = {
            "classification": "salaried",
            "ranges": [{
                "component": "base_salary", "value_kind": "range", "currency": "USD",
                "period": "year", "min_value": 100000, "max_value": 140000,
                "location_scope": None,
                "evidence_text": "The annual base salary range is $100,000 to $140,000.",
            }],
            "confidence": 0.98,
            "notes": "",
        }
        with sqlite3.connect(self.db) as con:
            queue_id = con.execute("SELECT id FROM salary_labeling_queue").fetchone()[0]
            con.execute(
                "INSERT INTO salary_model_predictions "
                "(queue_id,provider,model,schema_version,prompt_version,status,result_json) "
                "VALUES (?,?,?,?,?,'complete',?)",
                (
                    queue_id, "fireworks", "test", salary_teacher.SCHEMA_VERSION,
                    salary_teacher.PROMPT_VERSION, json.dumps(self.result),
                ),
            )
        self.queue_id = queue_id

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_accept_prediction_creates_gold_and_undo_restores_queue(self) -> None:
        item = salary_review.get_item(self.db)
        self.assertEqual(item["predictions"]["fireworks"]["result"], self.result)
        salary_review.save_gold(self.db, self.queue_id, "fireworks", None, "looks right")
        self.assertEqual(salary_review.review_stats(self.db)["gold_labels"], 1)
        self.assertIsNone(salary_review.get_item(self.db))
        self.assertEqual(salary_review.undo_last(self.db), self.queue_id)
        self.assertEqual(salary_review.review_stats(self.db)["gold_labels"], 0)

    def test_rejects_invented_evidence(self) -> None:
        bad = json.loads(json.dumps(self.result))
        bad["ranges"][0]["evidence_text"] = "This sentence was invented."
        with self.assertRaisesRegex(salary_review.ApiError, "exact posting quote"):
            salary_review.save_gold(self.db, self.queue_id, "human", bad)


if __name__ == "__main__":
    unittest.main()
