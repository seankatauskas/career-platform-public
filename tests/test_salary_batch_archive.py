#!/usr/bin/env python3
"""Offline tests for immutable salary batch archives."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_search.salary import batch_archive as salary_batch_archive
from job_search.salary import teacher as salary_teacher


class SalaryBatchArchiveTests(unittest.TestCase):
    def test_archive_is_self_contained_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            output = Path(directory) / "archive.jsonl"
            with sqlite3.connect(db) as con:
                con.execute("""
                    CREATE TABLE jobs (
                        ats TEXT,id TEXT,company TEXT,title TEXT,location TEXT,
                        employmentType TEXT,description TEXT NOT NULL,PRIMARY KEY(ats,id)
                    )
                """)
                con.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                    ("ashby", "1", "Example", "Engineer", "US", "Full-time", "Pay is $100."),
                )
            salary_teacher.build_sample(db, sample_size=1, fresh=True)
            result = {
                "classification": "salaried", "ranges": [],
                "confidence": 1, "notes": "",
            }
            with sqlite3.connect(db) as con:
                queue_id = con.execute("SELECT id FROM salary_labeling_queue").fetchone()[0]
                con.execute(
                    "INSERT INTO salary_model_predictions "
                    "(queue_id,provider,model,schema_version,prompt_version,status,result_json) "
                    "VALUES (?,?,?,?,?,'complete',?)",
                    (queue_id, "fireworks", "model", "v1", "v1", json.dumps(result)),
                )
                con.execute(
                    "INSERT INTO salary_gold_labels VALUES (?,?,?,?,?,?)",
                    (queue_id, json.dumps(result), "fireworks", "ok", "now", "now"),
                )
            summary = salary_batch_archive.archive_batch(db, output)
            lines = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(summary["queue_count"], 1)
            self.assertEqual(summary["prediction_count"], 1)
            self.assertEqual(summary["gold_count"], 1)
            self.assertEqual(lines[0]["type"], "salary_batch_manifest")
            self.assertEqual(lines[1]["job"]["description"], "Pay is $100.")
            self.assertEqual(lines[1]["gold"]["result"], result)
            with self.assertRaisesRegex(ValueError, "will not be overwritten"):
                salary_batch_archive.archive_batch(db, output)


if __name__ == "__main__":
    unittest.main()
