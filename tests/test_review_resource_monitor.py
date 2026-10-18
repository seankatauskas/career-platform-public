"""Resource attribution, persistence, privacy and fail-open sampling contracts."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.job_reviews.resource_monitor import (
    AttemptResources, LinuxReader, assessment, cgroup_snapshot, host_snapshot, running_tasks,
)


class ReviewResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, cpu=1000000, worker='a', available=3000):
        return {'host': {'host_total_bytes': 8192, 'host_available_bytes': available,
                         'cpu_total_ticks': cpu // 1000, 'cpu_busy_ticks': cpu // 4000,
                         'swap_in_pages': cpu // 1000000, 'swap_out_pages': 0,
                         'prompt': 'PRIVATE'},
                'coordinator': {'cpu_usec': cpu // 2, 'rss_bytes': 20},
                'containers': {worker * 64: {'role': 'review-worker', 'memory_limit_bytes': 3072,
                    'cpu_limit_nanocpus': 1000000000, 'counters': {
                        'cpu_usage_usec': cpu, 'memory_peak_bytes': 200, 'memory_anon_bytes': 120,
                        'memory_file_bytes': 80, 'event_oom_kill': 0, 'token': 'PRIVATE'}}},
                'running_tasks': {'counts': {'opportunity.preference_refresh': 1, 'SECRET_TASK': 3}, 'truncated': False}}

    def test_private_receipt_cpu_attribution_and_overlap(self):
        rows = iter([self.fixture(), self.fixture(3000000, available=2500)])
        with AttemptResources(self.root, self.root/'unused', reader=lambda: next(rows),
                              identity={'model': 'test', 'secret': 'PRIVATE'}) as monitor:
            monitor.count('input_jobs', 200)
            monitor.count('selected_jobs', 4)
            monitor.outcome('published')
        report = json.loads((self.root/'resources.json').read_text())
        value = assessment(report)
        self.assertEqual(value['worker_cpu_seconds_observed_lower_bound'], 3)
        self.assertEqual(value['coordinator_cpu_seconds'], 1)
        self.assertEqual(value['host_average_cpu_percent'], 25)
        self.assertEqual(value['host_min_available_bytes'], 2500)
        self.assertEqual(value['host_swap_in_pages'], 2)
        self.assertEqual(value['worker_sampled_anon_peak_bytes'], 120)
        self.assertEqual(report['review_phase'], 'published')
        self.assertEqual(report['input_jobs'], 200)
        self.assertEqual(report['overlap_max_running'], {'opportunity.preference_refresh': 1})
        for filename in ('resources.json', 'resources.jsonl'):
            text = (self.root/filename).read_text()
            self.assertNotIn('PRIVATE', text)
            self.assertNotIn('SECRET_TASK', text)
            self.assertEqual((self.root/filename).stat().st_mode & 0o777, 0o600)

    def test_failure_persists_and_preserves_original_exception(self):
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            with AttemptResources(self.root, self.root/'unused', reader=lambda: self.fixture()) as monitor:
                monitor.outcome('failed')
                raise RuntimeError('original failure')
        report = json.loads((self.root/'resources.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertIsNotNone(report['finished_at'])

    def test_sample_and_disk_errors_do_not_fail_work(self):
        def unavailable():
            raise OSError('do not retain error detail')
        with patch('job_search.job_reviews.resource_monitor._write_json', side_effect=OSError('disk full')):
            with AttemptResources(self.root, self.root/'unused', reader=unavailable) as monitor:
                pass
        self.assertGreater(monitor.report['sample_errors'], 0)
        self.assertGreater(monitor.report['write_errors'], 0)
        self.assertIsNone(assessment(monitor.report)['worker_cpu_seconds_observed_lower_bound'])

    def test_crash_checkpoint_is_in_progress_and_timeline_cap_keeps_summary(self):
        monitor = AttemptResources(self.root, self.root/'unused', reader=lambda: self.fixture())
        with patch('job_search.job_reviews.resource_monitor.MAX_TIMELINE_BYTES', 1):
            monitor.sample()
            monitor._save()
        report = json.loads((self.root/'resources.json').read_text())
        self.assertEqual(report['status'], 'in_progress')
        self.assertIsNone(report['finished_at'])
        self.assertTrue(report['timeline_truncated'])
        self.assertEqual(report['samples'], 1)
        self.assertEqual(assessment(report)['worker_cpu_seconds_observed_lower_bound'], 1)

    def test_restart_identity_and_counter_regression_are_not_combined(self):
        rows = iter([self.fixture(3000000), self.fixture(1000000, worker='b')])
        with AttemptResources(self.root, self.root/'unused', reader=lambda: next(rows)) as monitor:
            pass
        self.assertEqual(len(monitor.report['containers']), 2)
        self.assertEqual(assessment(monitor.report)['worker_cpu_seconds_observed_lower_bound'], 4)
        self.assertIsNone(assessment(monitor.report)['host_average_cpu_percent'])

    def test_cgroup_descendant_cpu_io_memory_and_missing_counters(self):
        (self.root/'cpu.stat').write_text('usage_usec 6000000\nuser_usec 5000000\nsystem_usec 1000000\nnr_throttled 3\nthrottled_usec 55\n')
        (self.root/'memory.current').write_text('1000')
        (self.root/'memory.peak').write_text('1500')
        (self.root/'memory.stat').write_text('anon 400\nfile 600\ninactive_file 300\n')
        (self.root/'memory.events').write_text('oom 2\noom_kill 1\n')
        (self.root/'io.stat').write_text('259:0 rbytes=100 wbytes=200 rios=1 wios=2\n259:1 rbytes=300 wbytes=400 rios=3 wios=4\n')
        result = cgroup_snapshot(self.root)
        self.assertEqual(result['cpu_usage_usec'], 6000000)
        self.assertEqual(result['memory_current_bytes'], 1000)
        self.assertEqual(result['memory_file_bytes'], 600)
        self.assertEqual(result['io_rbytes'], 400)
        self.assertEqual(result['event_oom_kill'], 1)
        self.assertNotIn('memory_swap_bytes', result)

    def test_host_cpu_does_not_double_count_guest_and_swap_is_io(self):
        (self.root/'stat').write_text('cpu 100 0 100 500 100 0 0 40 20 10\n')
        (self.root/'meminfo').write_text('MemTotal: 8000 kB\nMemAvailable: 3000 kB\nSwapFree: 1000 kB\n')
        (self.root/'vmstat').write_text('pswpin 10\npswpout 20\n')
        (self.root/'pressure').mkdir()
        (self.root/'pressure/memory').write_text('some avg10=0.00 avg60=0.00 avg300=0.00 total=123\nfull avg10=0.00 total=45\n')
        result = host_snapshot(self.root)
        self.assertEqual(result['cpu_total_ticks'], 840)
        self.assertEqual(result['cpu_steal_ticks'], 40)
        self.assertEqual(result['cpu_busy_ticks'], 200)
        self.assertEqual(result['swap_in_pages'], 10)
        self.assertEqual(result['memory_full_stall_usec'], 45)
        self.assertEqual(result['host_available_bytes'], 3000 * 1024)

    def test_readonly_overlap_query_does_not_fetch_payload(self):
        db = self.root/'ledger.db'
        with sqlite3.connect(db) as con:
            con.execute('CREATE TABLE work_items(task_kind,status,payload)')
            con.executemany('INSERT INTO work_items VALUES(?,?,?)', [
                ('opportunity.preference_refresh', 'running', 'PRIVATE'),
                ('ats.authoritative', 'queued', 'PRIVATE'), ('private-task', 'running', 'PRIVATE')])
        result = running_tasks(db)
        self.assertEqual(result['counts'], {'opportunity.preference_refresh': 1, 'other': 1})
        self.assertFalse(result['truncated'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_discovery_requests_only_allowed_docker_fields(self):
        reader = LinuxReader(self.root/'unused')
        reader.worker_name = 'career-review-old-' + 'f' * 16
        class Response:
            returncode = 0
            stdout = '/job-search-model-1 ' + 'a' * 64 + ' 123 0 0 false\n'
        with patch('job_search.job_reviews.resource_monitor.subprocess.run', return_value=Response()) as run, \
             patch('job_search.job_reviews.resource_monitor._cgroup_path', return_value=self.root):
            reader.discover()
        args = run.call_args.args[0]
        self.assertNotIn('Config.Env', ' '.join(args))
        self.assertIn('--format', args)
        self.assertEqual(reader.containers['a' * 64][0], 'model')


if __name__ == '__main__':
    unittest.main()
