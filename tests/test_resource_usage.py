"""Offline checks for scoped, bounded, private memory observations."""
from pathlib import Path
import json
import tempfile
import unittest

from job_search.resource_usage import MemorySampler, memory_snapshot


class ResourceUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / "proc"
        self.group = self.root / "cgroup"
        (self.proc / "123").mkdir(parents=True)
        (self.proc / "self").mkdir()
        (self.group / "service").mkdir(parents=True)

    def put(self, path, contents):
        path.write_text(contents)

    def snapshot(self):
        return memory_snapshot(pid=123, proc_root=self.proc, cgroup_root=self.group)

    def test_allowlisted_scopes_units_and_lifetime_are_distinct(self):
        self.put(self.proc / "meminfo", "MemTotal: 8000 kB\nMemAvailable: 7000 kB\nAnonPages: 500 kB\nCached: 400 kB\nPrivateData: 9876 kB\n")
        self.put(self.proc / "123/status", "Name: private employer\nVmRSS: 30 kB\nRssAnon: 20 kB\nVmHWM: 90 kB\n")
        self.put(self.proc / "123/cgroup", "0::/service\n")
        self.put(self.group / "service/memory.current", "99999\n")
        self.put(self.group / "service/memory.peak", "200000\n")
        self.put(self.group / "service/memory.max", "max\n")
        self.put(self.group / "service/memory.stat", "anon 50000\nfile 45000\nslab 4999\nprivate 123\n")
        self.put(self.group / "service/memory.events", "oom 2\noom_kill 1\n")
        result = self.snapshot()
        self.assertEqual(result["process_rss_bytes"], 30720)
        self.assertEqual(result["process_lifetime_peak_rss_bytes"], 92160)
        self.assertEqual(result["cgroup_current_bytes"], 99999)
        self.assertEqual(result["cgroup_lifetime_peak_bytes"], 200000)
        self.assertEqual(result["cgroup_anon_bytes"], 50000)
        self.assertEqual(result["cgroup_event_oom_kill"], 1)
        self.assertIsNone(result["cgroup_limit_bytes"])
        self.assertEqual(result["host_available_bytes"], 7168000)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("123", json.dumps(result))

    def test_missing_platform_or_exited_process_is_unavailable_not_zero(self):
        self.assertTrue(all(value is None for value in self.snapshot().values()))
        self.put(self.proc / "123/status", "VmRSS: -10 kB\nVmSwap: nope kB\nRssFile: 8 bytes\n")
        self.assertTrue(all(value is None for value in self.snapshot().values()))

    def test_cgroup_namespace_fallback_only_for_same_membership(self):
        self.put(self.proc / "123/cgroup", "0::/outer/service\n")
        self.put(self.group / "memory.current", "1234\n")
        self.assertIsNone(self.snapshot()["cgroup_current_bytes"])
        self.put(self.proc / "self/cgroup", "0::/outer/service\n")
        self.assertEqual(self.snapshot()["cgroup_current_bytes"], 1234)
        self.put(self.proc / "123/cgroup", "0::/../cgroup\n")
        self.assertIsNone(self.snapshot()["cgroup_current_bytes"])

    def test_summary_bounds_samples_omits_private_data_and_preserves_scopes(self):
        values = iter([{"process_rss_bytes": 50, "host_available_bytes": 200, "private": "secret"},
                       {"process_rss_bytes": 80, "host_available_bytes": 100}])
        sampler = MemorySampler(snapshot=lambda: next(values), max_samples=2, interval_seconds=60)
        with sampler:
            sampler._sample()
            sampler._sample()
        result = sampler.finish()
        self.assertEqual(result["samples"], 2)
        self.assertTrue(result["sample_limit_reached"])
        self.assertEqual(result["counters"]["process_rss_bytes"],
                         {"samples": 2, "first": 50, "last": 80, "minimum": 50, "maximum": 80})
        self.assertEqual(result["counters"]["host_available_bytes"]["minimum"], 100)
        self.assertIn("may predate", result["semantics"]["lifetime_peak"])
        self.assertNotIn("private", json.dumps(result))
        self.assertFalse(sampler._thread.is_alive())
        with self.assertRaises(RuntimeError):
            sampler.start()

    def test_sampling_failure_does_not_replace_work_exception(self):
        def fail():
            raise OSError("private process information")
        sampler = MemorySampler(snapshot=fail, max_samples=2)
        with self.assertRaisesRegex(ValueError, "task failed"):
            with sampler:
                raise ValueError("task failed")
        result = sampler.finish()
        self.assertEqual(result["failed_samples"], 2)
        self.assertEqual(result["counters"], {})
        self.assertNotIn("private", json.dumps(result))

    def test_invalid_limits_and_pid_rejected(self):
        for kwargs in ({"max_samples": 0}, {"max_samples": 3601}, {"interval_seconds": .1},
                       {"interval_seconds": 61}, {"interval_seconds": float("nan")},
                       {"max_samples": 1.5}, {"max_samples": True}, {"interval_seconds": True}):
            with self.assertRaises(ValueError):
                MemorySampler(**kwargs)
        for pid in (0, -1, True, "123"):
            with self.assertRaises(ValueError):
                memory_snapshot(pid=pid)
        with self.assertRaises(RuntimeError):
            MemorySampler().finish()


if __name__ == "__main__":
    unittest.main()
