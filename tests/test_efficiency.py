"""Offline checks for bounded, private runtime measurements and retry semantics."""
from contextlib import redirect_stderr
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.efficiency import main, runtime_report

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


class RuntimeEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "private.db"
        with sqlite3.connect(self.db) as con:
            con.executescript("""
                CREATE TABLE work_items (work_id TEXT PRIMARY KEY, task_kind TEXT,
                    lane TEXT, status TEXT, attempts INTEGER, created_at TEXT,
                    started_at TEXT, completed_at TEXT, due_at TEXT,
                    payload_json TEXT, last_error TEXT);
                CREATE TABLE job_runs (work_id TEXT UNIQUE, started_at TEXT,
                    completed_at TEXT, outcome TEXT, result_json TEXT, error TEXT);
            """)

    def add(self, identity="private-work-id", task="opportunity.preference_refresh",
            status="succeeded", attempts=1, created="2026-10-03T10:00:00Z",
            first="2026-10-03T10:01:00Z", due="2026-10-03T10:00:30Z",
            begin="2026-10-03T10:01:00Z", end="2026-10-03T10:03:00Z", outcome="succeeded", lane="model"):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, task, lane, status, attempts, created, first, end,
                         due, "private resume body", "private error message"))
            if begin is not None:
                con.execute("INSERT INTO job_runs VALUES (?,?,?,?,?,?)",
                            (identity, begin, end, outcome, "private model result", "private mail text"))

    def test_first_attempt_measurements_and_read_only(self):
        self.add()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        report = runtime_report(self.db, now=NOW)
        group = report["tasks"][0]
        self.assertEqual(group["latest_attempt_duration"]["p95_seconds"], 120)
        self.assertEqual(group["creation_to_first_claim"]["p50_seconds"], 60)
        self.assertEqual(group["first_attempt_dispatch_delay"]["p50_seconds"], 30)
        self.assertEqual(report["longest_attempts"][0]["started_at"], "2026-10-03T10:01:00Z")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertEqual(report["historical_memory"]["status"], "unavailable")
        for private in ("private-work-id", "resume body", "error message", "model result", "mail text", str(self.db)):
            self.assertNotIn(private, json.dumps(report))

    def test_retry_uses_latest_attempt_not_elapsed_lifetime(self):
        self.add(attempts=3, begin="2026-10-03T11:00:00Z", end="2026-10-03T11:00:10Z",
                 due="2026-10-03T10:59:00Z")
        group = runtime_report(self.db, now=NOW)["tasks"][0]
        self.assertEqual(group["retried_work_items"], 1)
        self.assertEqual(group["latest_attempt_duration"]["max_seconds"], 10)
        self.assertEqual(group["creation_to_first_claim"]["max_seconds"], 60)
        self.assertEqual(group["first_attempt_dispatch_delay"]["samples"], 0)

    def test_zero_recorded_durations_are_counted_and_intervals_bounded(self):
        for index in range(12):
            self.add(identity=str(index), begin="2026-10-03T10:03:00Z")
        report = runtime_report(self.db, now=NOW)
        self.assertEqual(report["tasks"][0]["latest_attempt_duration"]["zero_seconds_samples"], 12)
        self.assertEqual(len(report["longest_attempts"]), 10)

    def test_old_backlog_future_due_and_recent_window(self):
        self.add(identity="old-queued", created="2026-01-01T00:00:00Z", status="queued",
                 first=None, begin=None, end=None, due="2026-10-03T11:00:00Z")
        self.add(identity="scheduled", status="queued", first=None, begin=None, end=None,
                 due="2026-10-04T00:00:00Z")
        self.add(identity="old-complete", created="2026-01-01T00:00:00Z",
                 first="2026-01-01T01:00:00Z", begin="2026-01-01T01:00:00Z", end="2026-01-01T02:00:00Z")
        report = runtime_report(self.db, now=NOW)
        self.assertEqual(report["included_work_items"], 2)
        group = report["tasks"][0]
        self.assertEqual(group["queued_age"]["samples"], 2)
        self.assertEqual(group["queued_overdue"]["samples"], 1)
        self.assertEqual(group["queued_overdue"]["max_seconds"], 3600)
        self.assertEqual(group["latest_attempt_duration"]["samples"], 0)

    def test_unknown_fields_redacted_and_rows_bounded(self):
        self.add(identity="one", task="private.company.role", lane="private lane")
        self.add(identity="two", task="private.company.role", lane="private lane")
        report = runtime_report(self.db, now=NOW, max_rows=1)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["included_work_items"], 1)
        self.assertEqual(report["tasks"][0]["task_kind"], "other")
        self.assertEqual(report["tasks"][0]["lane"], "other")
        self.assertNotIn("private", json.dumps(report))

    def test_invalid_or_negative_timing_is_not_zero(self):
        self.add(first="malformed", begin="2026-10-03T11:00:00Z", end="2026-10-03T10:03:00Z")
        report = runtime_report(self.db, now=NOW)
        self.assertEqual(report["invalid_timestamp_values"], 1)
        group = report["tasks"][0]
        self.assertIsNone(group["latest_attempt_duration"]["max_seconds"])
        self.assertIsNone(group["creation_to_first_claim"]["max_seconds"])
        self.assertEqual(report["longest_attempts"], [])

    def test_future_work_excluded_and_empty_results_explicit(self):
        self.add(created="2026-10-04T00:00:00Z")
        report = runtime_report(self.db, now=NOW)
        self.assertEqual(report["included_work_items"], 0)
        self.assertEqual(report["tasks"], [])

    def test_missing_database_is_not_created_and_error_is_private(self):
        missing = Path(self.temporary.name) / "secret-company.db"
        with self.assertRaises(FileNotFoundError):
            runtime_report(missing, now=NOW)
        self.assertFalse(missing.exists())
        error = StringIO()
        with patch("sys.argv", ["efficiency", "--db", str(missing)]), redirect_stderr(error):
            with self.assertRaises(SystemExit) as exited:
                main()
        self.assertEqual(exited.exception.code, 2)
        self.assertNotIn("secret-company", error.getvalue())

    def test_limits_rejected(self):
        for kwargs in ({"days": 0}, {"days": 367}, {"max_rows": 0}, {"max_rows": 100001}):
            with self.assertRaises(ValueError):
                runtime_report(self.db, now=NOW, **kwargs)

    def test_memory_report_extracts_only_recorded_numeric_counters(self):
        self.add()
        payload = {'private_payload': 'private resume content', 'command_memory': {
            'commands': [{'counters': {
                'process_rss_bytes': {'maximum': 1024},
                'cgroup_anon_bytes': {'maximum': 2048},
                'cgroup_file_bytes': {'maximum': 'not a counter'},
                'host_available_bytes': {'minimum': 4096},
                'private_data': 'private company name'}}, 'private string']}}
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE job_runs SET result_json=?', (json.dumps(payload),))
        report = runtime_report(self.db, now=NOW)
        self.assertEqual(report['historical_memory']['status'], 'sampled')
        memory = report['tasks'][0]['command_memory']
        self.assertEqual(memory['process_rss'], {'recorded_attempts': 1, 'maximum_bytes': 1024})
        self.assertEqual(memory['host_available']['minimum_bytes'], 4096)
        self.assertIsNone(memory['cgroup_file']['maximum_bytes'])
        self.assertNotIn('private', json.dumps(report))


if __name__ == "__main__":
    unittest.main()
