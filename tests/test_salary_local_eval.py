#!/usr/bin/env python3
"""Offline tests for local salary model output parsing and scoring."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_search.salary.local_eval import (
    _complete_model_path,
    _json_object,
    _normalize_nuextract_result,
    score,
)


class SalaryLocalEvalTests(unittest.TestCase):
    def test_json_object_accepts_fenced_and_thinking_output(self) -> None:
        value = _json_object('<think>private</think>\n```json\n{"ranges": []}\n```')
        self.assertEqual(value, {"ranges": []})

    def test_normalize_nuextract_singleton_period_enum(self) -> None:
        value = {"ranges": [{"period": ["year"]}, {"period": "hour"}]}
        normalized, changes = _normalize_nuextract_result(value)
        self.assertEqual(normalized["ranges"][0]["period"], "year")
        self.assertEqual(
            changes, ["ranges[0].period: singleton enum list to scalar"]
        )

    def test_normalize_nuextract_does_not_guess_ambiguous_period(self) -> None:
        value = {"ranges": [{"period": ["year", "hour"]}]}
        normalized, changes = _normalize_nuextract_result(value)
        self.assertEqual(normalized["ranges"][0]["period"], ["year", "hour"])
        self.assertEqual(changes, [])

    def test_normalize_v3_singleton_value_kind_enum(self) -> None:
        value = {"ranges": [{"value_kind": ["minimum"], "period": ["year"]}]}
        normalized, changes = _normalize_nuextract_result(value, "v3")
        self.assertEqual(normalized["ranges"][0]["value_kind"], "minimum")
        self.assertEqual(normalized["ranges"][0]["period"], "year")
        self.assertEqual(len(changes), 2)

    def test_normalize_v3_repairs_value_kind_from_bound_shape(self) -> None:
        value = {"ranges": [
            {"value_kind": "range", "min_value": 150000, "max_value": 150000},
            {"value_kind": "exact", "min_value": 100000, "max_value": 120000},
        ]}
        normalized, changes = _normalize_nuextract_result(value, "v3")
        self.assertEqual(normalized["ranges"][0]["value_kind"], "exact")
        self.assertEqual(normalized["ranges"][1]["value_kind"], "range")
        self.assertEqual(len(changes), 2)

    def test_normalize_v3_simple_applies_only_narrow_deterministic_rules(self) -> None:
        value = {"ranges": [
            {"currency": "USD", "period": "hour", "min_value": None, "max_value": None},
            {
                "currency": "USD", "period": "year", "min_value": 20,
                "max_value": 24, "evidence_text": "$20 - $24 USD",
            },
        ]}
        normalized, changes = _normalize_nuextract_result(
            value, "v3-simple", '<div class="content-pay-transparency">pay</div>'
        )
        self.assertEqual(len(normalized["ranges"]), 1)
        self.assertEqual(normalized["ranges"][0]["period"], "hour")
        self.assertEqual(len(changes), 2)

    def test_normalize_v3_simple_removes_explicit_commission_only_pay(self) -> None:
        value = {"ranges": [{
            "currency": "USD", "period": "year",
            "min_value": 150000, "max_value": 200000,
        }]}
        normalized, changes = _normalize_nuextract_result(
            value, "v3-simple", "This is a commission-only position."
        )
        self.assertEqual(normalized, {"ranges": []})
        self.assertEqual(len(changes), 1)

    def test_complete_model_path_requires_every_indexed_shard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)
            (model / "config.json").write_text("{}")
            (model / "model.safetensors.index.json").write_text(json.dumps({
                "weight_map": {"a": "one.safetensors", "b": "two.safetensors"}
            }))
            (model / "one.safetensors").touch()
            self.assertFalse(_complete_model_path(model))
            (model / "two.safetensors").touch()
            self.assertTrue(_complete_model_path(model))

    def test_score_uses_supported_overlap_and_rejects_extra_usd_guess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            output = root / "results.jsonl"
            with sqlite3.connect(database) as con:
                con.executescript("""
                    CREATE TABLE salary_labeling_queue (
                        id INTEGER PRIMARY KEY, position INTEGER, ats TEXT, job_id TEXT,
                        split TEXT
                    );
                    CREATE TABLE jobs (
                        ats TEXT,id TEXT,company TEXT,title TEXT,location TEXT,
                        employmentType TEXT,description TEXT,PRIMARY KEY(ats,id)
                    );
                    CREATE TABLE salary_gold_labels (queue_id INTEGER,result_json TEXT);
                """)
                for queue_id in (1, 2):
                    con.execute(
                        "INSERT INTO salary_labeling_queue VALUES (?,?,?,?,?)",
                        (queue_id, queue_id, "ashby", str(queue_id), "calibration"),
                    )
                    con.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                        ("ashby", str(queue_id), "Co", "Role", "US", "Full", "posting"),
                    )
                gold = {"ranges": [
                    {"currency": "USD", "period": "year", "min_value": 60, "max_value": 80},
                    {"currency": "USD", "period": "year", "min_value": 90, "max_value": 115},
                ]}
                con.execute(
                    "INSERT INTO salary_gold_labels VALUES (?,?)", (1, json.dumps(gold))
                )
                con.execute(
                    "INSERT INTO salary_gold_labels VALUES (?,?)",
                    (2, json.dumps({"ranges": []})),
                )
            records = [
                {
                    "split": "calibration", "queue_id": 1,
                    "error": "TypeError: malformed non-target field",
                    "validation": ["example validation failure"],
                    "prediction": {"ranges": [
                        {"currency": "USD", "period": "year", "min_value": 90, "max_value": 115}
                    ]},
                },
                {
                    "split": "calibration", "queue_id": 2,
                    "prediction": {"ranges": [
                        {"currency": "USD", "period": "year", "min_value": 1, "max_value": 2}
                    ]},
                },
            ]
            output.write_text("".join(json.dumps(row) + "\n" for row in records))
            result = score(database, "calibration", output)
            self.assertEqual(result["primary_accuracy"], 0.5)
            self.assertEqual(result["usd_values"], {"tp": 1, "fp": 1, "fn": 1})
            self.assertEqual(result["errors"], 1)
            self.assertEqual(result["validation_failures"], 1)
            self.assertEqual(result["strict_output_success_rate"], 0.5)

    def test_v3_score_distinguishes_minimum_from_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            output = root / "v3.jsonl"
            with sqlite3.connect(database) as con:
                con.executescript("""
                    CREATE TABLE salary_v3_queue (
                        id INTEGER PRIMARY KEY, batch TEXT, position INTEGER,
                        ats TEXT, job_id TEXT, split TEXT
                    );
                    CREATE TABLE jobs (
                        ats TEXT,id TEXT,company TEXT,title TEXT,location TEXT,
                        employmentType TEXT,description TEXT,PRIMARY KEY(ats,id)
                    );
                    CREATE TABLE salary_v3_gold_labels (queue_id INTEGER,result_json TEXT);
                """)
                con.execute(
                    "INSERT INTO salary_v3_queue VALUES (1,?,?,?,?,?)",
                    ("targeted-v3-20260831", 1, "ashby", "1", "calibration"),
                )
                con.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                    ("ashby", "1", "Co", "Role", "US", "Full", "starts at 175000"),
                )
                gold = {"ranges": [{
                    "value_kind": "minimum", "currency": "USD", "period": "year",
                    "min_value": 175000, "max_value": None,
                }]}
                con.execute(
                    "INSERT INTO salary_v3_gold_labels VALUES (?,?)", (1, json.dumps(gold))
                )
            prediction = {"ranges": [{
                "value_kind": "exact", "currency": "USD", "period": "year",
                "min_value": 175000, "max_value": 175000,
            }]}
            output.write_text(json.dumps({
                "dataset": "v3", "split": "calibration", "queue_id": 1,
                "prediction": prediction, "validation": [], "error": None,
            }) + "\n")
            result = score(database, "calibration", output, "v3")
            self.assertEqual(result["primary_accuracy"], 0.0)
            self.assertEqual(result["usd_values"], {"tp": 0, "fp": 1, "fn": 1})
            self.assertEqual(
                result["by_value_kind"]["minimum"], {"tp": 0, "fp": 0, "fn": 1}
            )
            self.assertEqual(
                result["by_value_kind"]["exact"], {"tp": 0, "fp": 1, "fn": 0}
            )


if __name__ == "__main__":
    unittest.main()
