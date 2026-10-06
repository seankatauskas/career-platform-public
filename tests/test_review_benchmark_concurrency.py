"""Higher reviewer capacity is an explicit frozen benchmark experiment only."""
import argparse
from dataclasses import replace
import io
from contextlib import redirect_stderr
import unittest
from unittest.mock import Mock, patch

from job_search.contracts import ContractError
from job_search.job_reviews.authority import ReviewConflictError
from job_search.job_reviews.runner import ReviewCoordinator, add_arguments, command
from tests import test_review_runner as fixtures


class BenchmarkConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RunnerTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config, self.authority = self.fixture.config, self.fixture.authority
        self.source = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
            'idempotency_key': 'concurrency-source'})['review_id']
        self.panel = self.authority.start_benchmark({'review_id': self.source, 'ordinals': [1],
            'label': 'capacity experiment', 'idempotency_key': 'concurrency-panel'})['review_id']
        self.parser = argparse.ArgumentParser()
        add_arguments(self.parser)

    def test_default_and_bounds_remain_explicit(self):
        self.assertEqual(self.config.concurrency, 2)
        self.assertEqual(replace(self.config, concurrency=32).validate().concurrency, 32)
        with self.assertRaises(ContractError):
            replace(self.config, concurrency=33).validate()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(['run', '--benchmark-concurrency', '33'])
        policy = dict(self.config.execution_policy(), concurrency=33)
        with self.assertRaises(ContractError):
            self.authority.freeze_execution_policy(self.panel, policy)
        self.authority.freeze_execution_policy(self.source, replace(self.config, concurrency=16).execution_policy())

    def test_explicit_marked_benchmark_cli_runs_32_with_real_frozen_policy(self):
        args = self.parser.parse_args(['run', '--runner-config', 'unused', '--review-id', self.panel,
                                      '--benchmark-concurrency', '32'])
        with patch('job_search.job_reviews.runner.load_runner_config', return_value=self.config), \
                patch('job_search.job_reviews.runner.require_start'), \
                patch('job_search.job_reviews.runner.build_authority', return_value=self.authority), \
                patch('job_search.job_reviews.runner.ProductionRuntime',
                      side_effect=lambda config, authority: fixtures.FakeRuntime(authority, self.fixture.fixture.assessment())), \
                patch('job_search.job_reviews.runner.ReviewCoordinator',
                      side_effect=lambda config, authority, runtime: ReviewCoordinator(
                          config, authority, runtime, is_draining=lambda: False)):
            result = command(args)
        self.assertEqual(result['status'], 'benchmark_complete')
        self.assertEqual(result['configuration']['concurrency'], 32)
        frozen = self.authority.freeze_execution_policy(self.panel, replace(self.config, concurrency=32).execution_policy())
        self.assertEqual(result['execution_policy_sha256'], frozen['policy_sha256'])
        self.assertEqual(self.config.concurrency, 2)
        self.assertIsNone(self.authority.status(self.panel)['receipt'])

    def test_ordinary_new_scheduled_and_nonrun_cli_cannot_enable_32(self):
        for direct_config in (False, True):
            config = replace(self.config, concurrency=32) if direct_config else self.config
            flags = [] if direct_config else ['--benchmark-concurrency', '32']
            with patch('job_search.job_reviews.runner.load_runner_config', return_value=config), \
                    patch('job_search.job_reviews.runner.require_start'), \
                    patch('job_search.job_reviews.runner.build_authority', return_value=self.authority), \
                    patch('job_search.job_reviews.runner.ProductionRuntime') as runtime:
                for action, extra in [('run', []), ('run', ['--review-id', self.source]),
                        ('run', ['--review-id', self.panel, '--scheduled']),
                        ('status', ['--review-id', self.panel]), ('readiness', ['--review-id', self.panel]),
                        ('login', ['--review-id', self.panel])]:
                    args = self.parser.parse_args([action, '--runner-config', 'unused', *flags, *extra])
                    with self.subTest(direct_config=direct_config, action=action, extra=extra), self.assertRaises(ContractError):
                        command(args)
                runtime.assert_not_called()

    def test_direct_coordinator_and_authority_cannot_bypass_benchmark_scope(self):
        config = replace(self.config, concurrency=32)
        runtime = Mock()
        coordinator = ReviewCoordinator(config, self.authority, runtime, is_draining=lambda: False)
        for args in ({}, {'review_id': self.source}, {'review_id': self.panel, 'scheduled': True}):
            with self.subTest(args=args), self.assertRaises(ContractError):
                coordinator.run(**args)
        runtime.recover.assert_not_called()
        runtime.worker.assert_not_called()
        with self.assertRaisesRegex(ContractError, 'restricted to benchmark'):
            self.authority.freeze_execution_policy(self.source, config.execution_policy())

    def test_frozen_eight_cannot_resume_32_or_revoke_existing_work(self):
        before = self.authority.freeze_execution_policy(self.panel, replace(self.config, concurrency=8).execution_policy())
        grant = self.authority.issue(self.panel, [1], 'primary', {
            'runtime_id': 'capacity-prior-worker', 'model': self.config.model,
            'reasoning_effort': self.config.reasoning_effort})
        runtime = Mock()
        coordinator = ReviewCoordinator(replace(self.config, concurrency=32), self.authority, runtime,
                                        is_draining=lambda: False)
        with self.assertRaisesRegex(ReviewConflictError, 'already frozen'):
            coordinator.run(review_id=self.panel)
        runtime.recover.assert_not_called()
        runtime.worker.assert_not_called()
        after = self.authority.freeze_execution_policy(self.panel, replace(self.config, concurrency=8).execution_policy())
        self.assertEqual(after, before)
        with self.authority._transaction() as con:
            saved = con.execute('SELECT revoked_at FROM job_review_grants WHERE grant_id=?', (grant['grant_id'],)).fetchone()
            self.assertIsNone(saved[0])


if __name__ == '__main__':
    unittest.main()
