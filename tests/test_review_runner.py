"""Coordinator recovery tests: no model, credentials, Docker, or network."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.runner import ReviewCoordinator, runner_lock, runner_status
from job_search.job_reviews.runner_config import RunnerConfig, load_runner_config
from tests import test_agent_job_reviews as review_fixtures


class FinishedWorker:
    def __init__(self, code=0):
        self.code = code
    def poll(self):
        return self.code
    def wait(self):
        return self.code
    def terminate(self):
        self.code = -15


class FakeRuntime:
    def __init__(self, authority, assessment, *, partial=False, empty=False, disagreement=False):
        self.authority, self.assessment = authority, assessment
        self.partial, self.empty, self.disagreement = partial, empty, disagreement
        self.calls = []
        self.recovered = []
    def recover(self, workers):
        self.recovered = workers

    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    @contextmanager
    def worker(self, grant, *, runtime_id):
        self.authority.mark_launch(grant['grant_id'], runtime_id, {
            'container_id': hashlib.sha256(runtime_id.encode()).hexdigest(),
            'image_digest': 'sha256:' + '1' * 64, 'config_sha256': '2' * 64,
        })
        api = lambda op, args=None: self.authority.scoped_call(grant['token'], op, args)
        assignment = api('assignment')
        self.calls.append((assignment['kind'], [j['ordinal'] for j in assignment['jobs']]))
        jobs = assignment['jobs'][:1] if self.partial else assignment['jobs']
        if not self.empty:
            for job in jobs:
                offset = 0
                while True:
                    page = api('job', {'ordinal': job['ordinal'], 'offset': offset})
                    offset = page['next_offset']
                    if offset is None:
                        break
                value = copy.deepcopy(self.assessment)
                if assignment['kind'] == 'check' and self.disagreement:
                    value.update(decision='broad_only')
                api('assessment', {'ordinal': job['ordinal'], 'assessment': value})
        yield FinishedWorker(75 if self.partial else 0)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        from job_search.job_reviews.authority import ReviewAuthority
        self.fixture = review_fixtures.ReviewTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.authority = ReviewAuthority(self.fixture.service)
        self.config = RunnerConfig(application_config=self.root / 'app.json',
            state_dir=self.root / 'runner', runtime_dir=self.root / 'runtime',
            auth_home=self.root / 'auth', model_image='example/reviewer@sha256:' + '1' * 64)

    def coordinator(self, **kwargs):
        runtime = FakeRuntime(self.authority, self.fixture.assessment(), **kwargs)
        runner = ReviewCoordinator(self.config, self.authority, runtime, is_draining=lambda: False)
        return runner, runtime

    def run_custom(self, runner, **kwargs):
        return runner.run(mode='custom', window_start='2026-10-01T00:00:00Z', **kwargs)

    def test_primary_and_independent_check_publish_without_scores(self):
        runner, runtime = self.coordinator()
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        self.assertEqual([c[0] for c in runtime.calls], ['primary', 'check'])
        self.assertEqual(len(result['receipt']['lists']), 2)
        self.assertFalse(runner_status(self.config)['schedule_enabled'])
        serialized = (self.config.state_dir / 'status.json').read_text()
        self.assertNotIn('token', serialized)
        self.assertNotIn('Python APIs', serialized)

    def test_schedule_remains_disabled_without_creating_state(self):
        runner, runtime = self.coordinator()
        self.assertEqual(runner.run(scheduled=True)['status'], 'disabled')
        self.assertFalse(self.config.state_dir.exists())
        self.assertEqual(runtime.calls, [])

    def test_maintenance_prevents_dispatch(self):
        runner, runtime = self.coordinator()
        runner.is_draining = lambda: True
        self.assertEqual(runner.run()['status'], 'maintenance')
        self.assertEqual(runtime.calls, [])

    def test_disagreements_preserve_both_judgments_without_publication(self):
        runner, _ = self.coordinator(disagreement=True)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'needs_review')
        status = self.authority.status(result['review_id'])
        self.assertEqual(status['disagreement_count'], 1)
        self.assertIsNone(status['receipt'])

    def test_partial_worker_progress_is_not_repeated(self):
        self.fixture.insert('b')
        runner, runtime = self.coordinator(partial=True)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        primary = [jobs for kind, jobs in runtime.calls if kind == 'primary']
        self.assertEqual(primary, [[1, 2], [2]])

    def test_incomplete_run_resumes_ledger_with_fresh_workers(self):
        runner, runtime = self.coordinator(empty=True)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(len(runtime.calls), 3)
        replacement, new_runtime = self.coordinator()
        final = replacement.run(review_id=result['review_id'])
        self.assertEqual(final['status'], 'published')
        self.assertEqual(final['review_id'], result['review_id'])
        self.assertEqual(len(new_runtime.calls), 2)

    def test_fatal_worker_exit_stops_without_retry(self):
        runner, runtime = self.coordinator(empty=True)
        with patch.object(FinishedWorker, 'wait', return_value=1):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'worker_failed')
        self.assertEqual(len(runtime.calls), 1)
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_stop_after_preview_prevents_publication(self):
        runner, runtime = self.coordinator()
        preview = self.authority.preview
        def stopped_preview(rid):
            result = preview(rid)
            runner.stop.set()
            return result
        with patch.object(self.authority, 'preview', side_effect=stopped_preview):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'interrupted')
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_crash_reconciliation_happens_before_replacement_dispatch(self):
        current = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                        'idempotency_key': 'crashed-run'})
        grant = self.authority.issue(current['review_id'], [1], 'primary', {
            'runtime_id': 'worker_' + '3' * 32, 'model': self.config.model,
            'reasoning_effort': self.config.reasoning_effort})
        runner, runtime = self.coordinator()
        result = runner.run(review_id=current['review_id'])
        self.assertEqual(result['status'], 'published')
        self.assertEqual(runtime.recovered[0]['grant_id'], grant['grant_id'])
        self.assertEqual(result['recovered_grants'], 1)
        with self.assertRaises(ContractError):
            self.authority.scoped_call(grant['token'], 'assignment')

    def test_trial_limit_never_silently_truncates_review(self):
        self.fixture.insert('b')
        runner, runtime = self.coordinator()
        with self.assertRaisesRegex(ContractError, 'trial limit'):
            self.run_custom(runner, max_jobs=1)
        result = runner_status(self.config)['last_run']
        self.assertEqual(self.authority.status(result['review_id'])['total'], 2)
        self.assertEqual(runtime.calls, [])

    def test_old_unisolated_review_cannot_be_adopted(self):
        rid = self.fixture.start()
        runner, runtime = self.coordinator()
        with self.assertRaisesRegex(ContractError, 'not managed|not isolated'):
            runner.run(review_id=rid)
        self.assertEqual(runtime.calls, [])

    def test_lock_excludes_overlapping_manual_or_timer_runs(self):
        with runner_lock(self.config.state_dir):
            with self.assertRaisesRegex(ContractError, 'already running'):
                with runner_lock(self.config.state_dir):
                    pass

    def test_config_requires_pinned_image_and_explicit_schedule(self):
        with self.assertRaisesRegex(ContractError, 'sha256'):
            replace(self.config, model_image='reviewer:latest').validate()
        with self.assertRaisesRegex(ContractError, 'calendar'):
            replace(self.config, schedule_enabled=True).validate()
        value = dict(self.config.__dict__)
        for key, item in value.items():
            if isinstance(item, Path):
                value[key] = str(item)
        path = self.root / 'runner.json'
        path.write_text(json.dumps(value))
        self.assertEqual(load_runner_config(path), self.config)
        value['token'] = 'not-allowed'
        path.write_text(json.dumps(value))
        with self.assertRaises(ContractError):
            load_runner_config(path)


if __name__ == '__main__':
    unittest.main()
