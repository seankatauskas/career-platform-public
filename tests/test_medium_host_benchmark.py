"""Resource harness must fail closed and compare actual derived outputs."""
import socket
import unittest
from unittest.mock import patch

from tests import benchmark_medium_host as benchmark


class MediumHostBenchmarkTests(unittest.TestCase):
    def test_small_mixed_fixture_has_full_incremental_and_snapshot_parity(self):
        result = benchmark.benchmark(families=20, description_bytes=128, snapshot_mib=1)
        self.assertTrue(result['ok'])
        self.assertEqual(result['parity'], {'incremental_vs_full': True, 'snapshot': True, 'unchanged': True})
        phases = result['phases']
        self.assertEqual(phases['ranking_full']['ranking']['checked_families'], 20)
        self.assertEqual(phases['ranking_unchanged']['ranking']['checked_families'], 0)
        self.assertEqual(phases['mixed_collection_ranking_snapshot']['ranking']['checked_families'], 2)
        self.assertGreater(result['interactive_child']['latency']['requests'], 0)
        self.assertGreater(result['total_measured_cpu_seconds'], 0)
        self.assertEqual(set(phases['mixed_collection_ranking_snapshot']['snapshot_overlap_seconds']), {'collection', 'ranking'})

    def test_overlap_reports_zero_for_sequential_work(self):
        self.assertEqual(benchmark.overlap_seconds((1, 3), (4, 5)), 0)
        self.assertEqual(benchmark.overlap_seconds((1, 4), (3, 5)), 1)

    def test_resource_event_increase_is_a_failure(self):
        class Sampler:
            def __init__(self, **kwargs): pass
            def start(self): return self
            def finish(self):
                return {'counters': {'cgroup_event_oom_kill': {'first': 0, 'last': 1}}}
        with patch.object(benchmark, 'MemorySampler', Sampler):
            with self.assertRaisesRegex(RuntimeError, 'oom_kill'):
                with benchmark.measured({}, 'fixture'):
                    pass

    def test_network_attempt_is_rejected_before_connect(self):
        with benchmark.offline(), socket.socket() as sock:
            with self.assertRaisesRegex(AssertionError, 'network forbidden'):
                sock.connect(('127.0.0.1', 1))

    def test_catalog_bounds_are_enforced_before_fixture_creation(self):
        for size in (0, 400001):
            with self.subTest(size=size), self.assertRaises(ValueError):
                benchmark.benchmark(families=size)


if __name__ == '__main__':
    unittest.main()
