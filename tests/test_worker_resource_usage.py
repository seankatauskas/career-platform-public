"""Memory telemetry must survive task failure without changing task semantics."""
from datetime import timedelta
import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from job_search.db import connect
from job_search.worker import (PermanentTaskError, RetryableTaskError, TaskContext,
                               Worker, run_command_with_heartbeat)
from tests.test_job_search_automation import NOW, enqueue_work, make_db


SUMMARY = {"version": 1, "samples": 2, "counters": {"process_rss_bytes":
           {"samples": 2, "first": 100, "last": 120, "minimum": 100, "maximum": 120}}}


class WorkerResourceUsageTests(unittest.TestCase):
    def process(self):
        process = Mock(pid=123, returncode=0)
        process.communicate.return_value = ("", "")
        return process

    def invoke(self, context, process=None, **kwargs):
        return run_command_with_heartbeat(("python", "-m", "fixture"), context,
                    cwd="/tmp", env={}, timeout_seconds=60,
                    popen_factory=lambda *a, **k: process or self.process(), **kwargs)

    def context(self, record=None, heartbeat=lambda: True):
        return TaskContext("work", "test.task", 1, "", heartbeat, record_resources=record)

    def sampler(self):
        sampler = Mock()
        sampler.start.return_value = sampler
        sampler.finish.return_value = SUMMARY
        return sampler

    def test_success_and_failure_store_memory_in_latest_attempt(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                db = make_db(directory)
                enqueue_work(db, "memory-task", "test.task")
                def handler(payload, context):
                    self.invoke(context)
                    if failure:
                        raise RetryableTaskError("try later")
                    return {"handled": True}
                worker = Worker(db, task_handlers={"test.task": handler},
                                max_work_per_tick=1, max_outbox_per_tick=0,
                                now_provider=lambda: NOW)
                with patch("job_search.resource_usage.MemorySampler", return_value=self.sampler()):
                    report = worker.tick(now=NOW)
                self.assertEqual(report["work"]["retried" if failure else "succeeded"], 1)
                with connect(db) as con:
                    row = con.execute("SELECT result_json,outcome FROM job_runs").fetchone()
                self.assertEqual(json.loads(row[0])["command_memory"]["commands"], [SUMMARY])
                self.assertEqual(row[1], "failed" if failure else "succeeded")
                if failure:
                    # Next attempt has no measured command: never reuse old peaks.
                    worker.task_handlers["test.task"] = lambda payload, context: {"handled": True}
                    worker.tick(now=NOW + timedelta(minutes=2))
                    with connect(db) as con:
                        result = json.loads(con.execute("SELECT result_json FROM job_runs").fetchone()[0])
                    self.assertNotIn("command_memory", result)

    def test_heartbeat_loss_and_timeout_stop_command_and_finish_telemetry(self):
        for reason in ("lease", "timeout"):
            with self.subTest(reason=reason):
                process = self.process()
                process.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 1), ("", "")] if reason == "lease" else [("", "")]
                recorded = []
                sampler = self.sampler()
                with patch("job_search.resource_usage.MemorySampler", return_value=sampler), \
                     patch("job_search.worker.time.monotonic", side_effect=[0, 61] if reason == "timeout" else [0, 1]):
                    with self.assertRaises(RetryableTaskError if reason == "timeout" else PermanentTaskError):
                        self.invoke(self.context(recorded.append, lambda: False), process)
                process.terminate.assert_called_once()
                sampler.finish.assert_called_once()
                self.assertEqual(recorded, [SUMMARY])

    def test_telemetry_setup_finish_and_callback_errors_cannot_fail_command(self):
        for point in ("setup", "finish", "callback"):
            with self.subTest(point=point):
                sampler = self.sampler()
                if point == "finish":
                    sampler.finish.side_effect = RuntimeError("telemetry error")
                callback = Mock(side_effect=RuntimeError("callback error") if point == "callback" else None)
                with patch("job_search.resource_usage.MemorySampler", return_value=sampler,
                           side_effect=RuntimeError("setup error") if point == "setup" else None):
                    self.assertEqual(self.invoke(self.context(callback)).returncode, 0)

    def test_commands_per_task_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            enqueue_work(db, "memory-task", "test.task")
            def handler(payload, context):
                for _ in range(10):
                    self.invoke(context)
                return {}
            worker = Worker(db, task_handlers={"test.task": handler}, max_work_per_tick=1,
                            max_outbox_per_tick=0, now_provider=lambda: NOW)
            with patch("job_search.resource_usage.MemorySampler", return_value=self.sampler()):
                worker.tick(now=NOW)
            with connect(db) as con:
                result = json.loads(con.execute("SELECT result_json FROM job_runs").fetchone()[0])
            self.assertEqual(len(result["command_memory"]["commands"]), 8)
            self.assertEqual(result["command_memory"]["omitted_commands"], 2)


if __name__ == "__main__":
    unittest.main()
