"""Cache validation and startup benchmark contracts without Docker or providers."""
from pathlib import Path
import py_compile
import tempfile
import unittest

from tests.benchmark_python_startup import benchmark
from tests.probe_python_bytecode import check_cache, stale_source_probe


class PythonBytecodeTests(unittest.TestCase):
    def test_checked_hash_cache_matches_source_and_rejects_stale_or_timestamp_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.py'
            source.write_text('VALUE = 1\n')
            py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
            check_cache(source, require_immutable=False)
            source.write_text('VALUE = 2\n')
            with self.assertRaisesRegex(AssertionError, 'cache source hash differs'):
                check_cache(source, require_immutable=False)
            py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
            with self.assertRaisesRegex(AssertionError, 'not checked-hash'):
                check_cache(source, require_immutable=False)

    def test_changed_same_size_same_mtime_source_rejects_stale_bytecode(self):
        self.assertEqual(stale_source_probe(), {'stale_source_rejected': True,
                                               'size_and_mtime_preserved': True, 'cache_not_rewritten': True})

    def test_benchmark_invokes_real_fixture_health_and_cli_without_thresholds(self):
        report = benchmark(3)
        self.assertEqual(report['repetitions'], 3)
        self.assertEqual(set(report['results']), {'worker_health', 'tools_cli'})
        for result in report['results'].values():
            self.assertEqual(len(result['samples']), 3)
            self.assertGreaterEqual(result['median_cpu_ms'], 0)
            self.assertGreater(result['median_wall_ms'], 0)
            self.assertTrue(all(set(row) == {'cpu_ms', 'wall_ms'} for row in result['samples']))
        for invalid in (True, 2, 101):
            with self.assertRaises(ValueError):
                benchmark(invalid)


if __name__ == '__main__':
    unittest.main()
