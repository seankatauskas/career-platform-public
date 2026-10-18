"""Cloud diagnostics stay bounded without weakening work or dependency checks."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from job_search.cloud import run_worker_loop
from job_search.runtime import Runtime, RuntimeConfigV1
from job_search.worker import PermanentTaskError, Worker
from tests.test_job_search_automation import NOW, enqueue_work, make_db
from tests.test_worker_catch_up import Clock


class IdleEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = make_db(self.temp.name)
        self.config = RuntimeConfigV1.defaults(self.root)

    def test_compact_tick_still_claims_completes_and_reports_due_work(self):
        for name in ('a', 'b'):
            enqueue_work(self.db, name, 'fixture')
        worker = Worker(self.db, partition='interactive', max_work_per_tick=1,
                        now_provider=lambda: NOW, task_handlers={'fixture': lambda *_: {'done': True}})
        with patch('job_search.worker.automation_health', side_effect=AssertionError('historical diagnostics')):
            report = worker.tick(include_diagnostics=False)
        self.assertEqual(report['work']['succeeded'], 1)
        self.assertTrue(report['more_due'])
        self.assertNotIn('health', report)
        self.assertEqual(report['recovered']['work'], 0)
        # Direct callers keep the historical report by default.
        detailed = worker.tick()
        self.assertIn('health', detailed)
        self.assertEqual(detailed['work']['succeeded'], 1)
        self.assertFalse(detailed['more_due'])

    def test_compact_tick_preserves_task_dependency_failure(self):
        enqueue_work(self.db, 'missing-provider', 'fixture')
        def handler(*_):
            raise PermanentTaskError('fixture provider unavailable')
        worker = Worker(self.db, partition='interactive', now_provider=lambda: NOW,
                        task_handlers={'fixture': handler})
        with patch('job_search.worker.automation_health', side_effect=AssertionError('historical diagnostics')):
            report = worker.tick(include_diagnostics=False)
        self.assertEqual(report['work']['dead'], 1)
        self.assertEqual(report['work']['succeeded'], 0)

    def test_runtime_refresh_switch_is_observational_and_defaults_to_refresh(self):
        worker = Mock()
        worker.tick.return_value = {'acquired': True}
        runtime = Runtime(self.config, 'core', worker, {})
        with patch('job_search.dependency_health.dependency_health', return_value={'status': 'fixture'}) as dependencies, \
             patch('job_search.dependency_snapshot.publish_snapshot') as publish:
            runtime.tick(refresh_dependencies=False, include_diagnostics=False)
            dependencies.assert_not_called()
            publish.assert_not_called()
            self.assertFalse(worker.tick.call_args.kwargs['include_diagnostics'])
            runtime.tick()
            dependencies.assert_called_once_with(self.config)
            publish.assert_called_once_with(self.config.application_db, {'status': 'fixture'})
            self.assertTrue(worker.tick.call_args.kwargs['include_diagnostics'])
            runtime.tick(should_stop=lambda: True)
            self.assertEqual(dependencies.call_count, 1)
            self.assertEqual(worker.tick.call_count, 2)

    def loop(self, *, lane='core', partition='interactive', once=False, ticks=22, observations=None):
        clock, calls = Clock(), []
        observations = iter(observations or [])
        def tick(**kwargs):
            calls.append((clock.seconds, kwargs['refresh_dependencies'], kwargs['include_diagnostics']))
            self.assertFalse(kwargs['should_stop']())
            report = {'acquired': True, 'more_due': False}
            if kwargs['refresh_dependencies']:
                retry = next(observations, False)
                report['dependency_snapshot'] = {'notification_retry': retry}
            return report
        def emit(raw):
            self.assertEqual(json.loads(raw)['partition'], partition)
            if len(calls) == ticks:
                clock.set()
        with patch('job_search.cloud.time.monotonic', lambda: clock.seconds):
            result = run_worker_loop(
                self.config, lane=lane, partition=partition, interval_seconds=15,
                runtime_factory=lambda *_a, **_kw: SimpleNamespace(tick=tick),
                stop=clock, now_provider=clock.now, emit=emit, once=once,
                health_dir=self.root / 'health',
            )
        self.assertEqual(result, 0)
        return calls

    def test_interactive_refresh_every_five_minutes_keeps_fifteen_second_poll(self):
        calls = self.loop()
        self.assertEqual([time for time, refresh, _ in calls if refresh], [0, 300])
        self.assertEqual([time for time, _, _ in calls], list(range(0, 330, 15)))
        self.assertFalse(any(detail for _, _, detail in calls))
        # A replacement process always gets a first observation, regardless of
        # the previous process's in-memory cadence.
        self.assertEqual(self.loop(ticks=1), [(0, True, False)])

    def test_batch_does_not_duplicate_snapshots_but_unsplit_core_refreshes(self):
        self.assertFalse(any(refresh for _, refresh, _ in self.loop(partition='batch')))
        self.assertFalse(any(refresh for _, refresh, _ in self.loop(lane='model')))
        self.assertEqual([time for time, refresh, _ in self.loop(partition='all') if refresh], [0, 300])


    def test_notification_startup_failure_recovers_on_bounded_early_observation(self):
        calls = self.loop(ticks=25, observations=[True, False])
        self.assertEqual([time for time, refresh, _ in calls if refresh], [0, 30, 330])
        # The ordinary 15-second work poll stays unchanged; no health check per poll.
        self.assertEqual([time for time, _, _ in calls], list(range(0, 375, 15)))

    def test_persistent_failure_backs_off_and_success_resets_retry_budget(self):
        calls = self.loop(ticks=60, observations=[True] * 5 + [False, True])
        self.assertEqual([time for time, refresh, _ in calls if refresh], [0, 30, 90, 210, 510, 810])
        calls = self.loop(ticks=46, observations=[True, False, True, False])
        self.assertEqual([time for time, refresh, _ in calls if refresh], [0, 30, 330, 360, 660])

    def test_notification_retry_hint_is_sanitized_and_reflects_only_observed_failure(self):
        worker = Mock()
        worker.tick.return_value = {'acquired': False, 'reason': 'lease unavailable'}
        runtime = Runtime(self.config, 'core', worker, {})
        for state, retry in [('blocked_setup', True), ('ready', False), ('disabled', False)]:
            with self.subTest(state=state), \
                 patch('job_search.dependency_health.dependency_health', return_value={
                     'notifications': {'status': state, 'path': '/private/secret', 'token': 'NEVER_EXPOSE'}}), \
                 patch('job_search.dependency_snapshot.publish_snapshot'):
                report = runtime.tick(refresh_dependencies=True, include_diagnostics=False)
            self.assertEqual(report['dependency_snapshot'], {'notification_retry': retry})
            self.assertNotIn('private', json.dumps(report))
            self.assertNotIn('NEVER_EXPOSE', json.dumps(report))
            self.assertFalse(report['acquired'])
        with patch('job_search.dependency_health.dependency_health', side_effect=AssertionError('probe')):
            self.assertNotIn('dependency_snapshot', runtime.tick(refresh_dependencies=False))

    def test_explicit_once_retains_diagnostics_and_dependency_refresh(self):
        self.assertEqual(self.loop(partition='batch', once=True), [(0, True, True)])
        self.assertEqual(self.loop(lane='model', once=True), [(0, False, True)])


if __name__ == '__main__':
    unittest.main()
