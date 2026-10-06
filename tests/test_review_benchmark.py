"""Experimental panels must preserve production evidence without prior judgments."""
from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.benchmark import main
from job_search.job_reviews.runner_config import RunnerConfig
from tests import test_review_authority as authority_fixtures


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        base = authority_fixtures.AuthorityTests('runTest')
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.fixture, self.authority = base.fixture, base.authority
        for name in ('start', 'issue', 'assess', 'ledger'):
            setattr(self, name, getattr(base, name))

    def panel(self, source, **changes):
        args = dict(review_id=source, ordinals=[1], label='bounded timing panel',
                    idempotency_key='benchmark-one')
        args.update(changes)
        return self.authority.start_benchmark(args)

    def test_panel_copies_only_frozen_inputs_and_is_idempotent(self):
        self.fixture.insert('b')
        source = self.start()
        grant = self.issue(source)
        self.assess(grant)
        self.authority.revoke(grant['grant_id'])
        with self.ledger() as con:
            before = con.execute('SELECT * FROM job_reviews WHERE review_id=?', (source,)).fetchone()
            context = con.execute('SELECT context_json,context_sha256 FROM job_reviews WHERE review_id=?', (source,)).fetchone()
            snapshot = con.execute('SELECT snapshot_json,snapshot_sha256 FROM job_review_items WHERE review_id=? AND ordinal=1', (source,)).fetchone()
        result = self.panel(source)
        rid = result['review_id']
        self.assertNotEqual(source, rid)
        self.assertEqual(result, self.panel(source))
        self.assertEqual(result['counts'], {'pending': 1})
        self.assertEqual(result['metadata']['execution_mode'], 'isolated')
        self.assertEqual(result['metadata']['benchmark']['source_total'], 2)
        with self.ledger() as con:
            self.assertEqual(context, con.execute('SELECT context_json,context_sha256 FROM job_reviews WHERE review_id=?', (rid,)).fetchone())
            self.assertEqual(snapshot, con.execute('SELECT snapshot_json,snapshot_sha256 FROM job_review_items WHERE review_id=?', (rid,)).fetchone())
            for table in ('job_review_revisions', 'job_review_reads', 'job_review_grants', 'job_review_calibrations', 'job_review_publications'):
                self.assertEqual(con.execute('SELECT COUNT(*) FROM '+table+' WHERE review_id=?', (rid,)).fetchone()[0], 0)
            self.assertEqual(before, con.execute('SELECT * FROM job_reviews WHERE review_id=?', (source,)).fetchone())

    def test_panel_requires_coordinator_and_valid_explicit_scope(self):
        source = self.start()
        with self.assertRaisesRegex(ContractError, 'trusted coordinator'):
            self.fixture.service.call('benchmark-start', dict(review_id=source, ordinals=[1], label='bad', idempotency_key='bad'))
        for ordinals in ([], [1, 1], [1, 999], [True], list(range(1, 82))):
            with self.subTest(ordinals=ordinals), self.assertRaises(ContractError):
                self.panel(source, ordinals=ordinals)
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_reviews').fetchone()[0], 1)

    def test_completed_panel_cannot_publish_or_advance_cutoff(self):
        source = self.start()
        rid = self.panel(source)['review_id']
        self.assess(self.issue(rid))
        self.assess(self.issue(rid, kind='check'))
        preview = self.authority.preview(rid)
        self.assertEqual(preview['blockers'], ['benchmark_not_publishable'])
        self.assertFalse(preview['ready'])
        with self.assertRaisesRegex(ContractError, 'benchmark_not_publishable'):
            self.authority.publish(rid, preview['preview_sha256'], 'do-not-publish')
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM curated_shortlists').fetchone()[0], 0)
            self.assertIsNone(self.fixture.service._recurring_cutoff(con))

    def test_wrapper_rejects_start_overrides_before_host_access(self):
        for flag, value in [('model', 'benchmark-model'), ('reasoning-effort', 'medium'),
                            ('batch-size', '5'), ('concurrency', '32'),
                            ('screening-model', 'screen-model'), ('screening-reasoning-effort', 'low'),
                            ('screening-batch-size', '200'), ('check-all', None)]:
            with self.subTest(flag=flag), redirect_stdout(io.StringIO()), \
                 patch('job_search.job_reviews.benchmark.host_config') as host:
                self.assertEqual(main(['start', '--benchmark-' + flag, *([] if value is None else [value])]), 2)
                host.assert_not_called()

    def test_wrapper_forwards_effective_settings_only_for_marked_panel(self):
        source = self.start()
        panel = self.panel(source)['review_id']
        root = self.fixture.root
        path = root / 'runner.json'
        config = RunnerConfig(application_config=root / 'app.json', state_dir=root / 'runner',
                              runtime_dir=root / 'runtime', auth_home=root / 'auth',
                              model_image='example/reviewer@sha256:' + '1' * 64)
        with redirect_stdout(io.StringIO()), \
             patch('job_search.job_reviews.benchmark.host_config', return_value=path), \
             patch('job_search.job_reviews.benchmark.load_runner_config', return_value=config), \
             patch('job_search.job_reviews.benchmark.require_start'), \
             patch('job_search.job_reviews.benchmark.build_authority', return_value=self.authority), \
             patch('job_search.job_reviews.benchmark.runner_main', return_value=0) as runner:
            flags = ['--benchmark-model', 'benchmark-model', '--benchmark-reasoning-effort', 'medium',
                     '--benchmark-check-model', 'checker-model', '--benchmark-check-reasoning-effort', 'high',
                     '--benchmark-batch-size', '5', '--benchmark-concurrency', '32',
                     '--benchmark-screening-model', 'screen-model',
                     '--benchmark-screening-reasoning-effort', 'low', '--benchmark-screening-batch-size', '200',
                     '--benchmark-check-all']
            self.assertEqual(main(['run', '--review-id', source, *flags]), 2)
            self.assertEqual(main(['run', *flags]), 2)
            runner.assert_not_called()
            self.assertEqual(main(['run', '--review-id', panel, *flags]), 0)
            runner.assert_called_once_with(['run', '--runner-config', str(path), '--review-id', panel,
                '--max-jobs', '320', '--benchmark-concurrency', '32', '--benchmark-batch-size', '5',
                '--benchmark-model', 'benchmark-model', '--benchmark-reasoning-effort', 'medium',
                '--benchmark-check-model', 'checker-model', '--benchmark-check-reasoning-effort', 'high',
                '--benchmark-screening-model', 'screen-model', '--benchmark-screening-reasoning-effort', 'low',
                '--benchmark-screening-batch-size', '200', '--benchmark-check-all'])

    def test_deployment_gate_allows_benchmark_but_draining_blocks_creation_and_run(self):
        from job_search.aws_ops import set_gate
        source = self.start()
        root = self.fixture.root.resolve()
        release_root = root / 'release-root'
        release = release_root / 'releases' / 'test-release'
        release.mkdir(parents=True)
        (release_root / 'current').symlink_to(release)
        image = 'example/app@sha256:' + '1' * 64
        (release / 'release.json').write_text(json.dumps({
            'version': 1, 'release_id': 'test-release', 'app_image': image,
            'hermes_image': image, 'hermes_base_image': image,
            'reviewer_image': image, 'tectonic_version': 'test'}))
        operations = {'data_root': str(root), 'release_root': str(release_root)}
        path = root / 'runner.json'
        config = RunnerConfig(application_config=root / 'app.json', state_dir=root / 'runner',
            runtime_dir=root / 'runtime', auth_home=root / 'auth', model_image=image)
        output = io.StringIO()
        with patch.dict(os.environ, {'JOB_SEARCH_MAINTENANCE_GATE': str(root / 'maintenance/gate.json')}), \
             redirect_stdout(output), \
             patch('job_search.job_reviews.benchmark.host_config', return_value=path), \
             patch('job_search.job_reviews.benchmark.load_runner_config', return_value=config), \
             patch('job_search.job_reviews.benchmark.build_authority', return_value=self.authority) as authority, \
             patch('job_search.job_reviews.benchmark.runner_main', return_value=0) as runner:
            set_gate(operations, ['dashboard'])
            start = ['start', '--source-review-id', source, '--ordinals', '1',
                     '--label', 'deployment gate regression', '--idempotency-key', 'gate-panel']
            self.assertEqual(main(start), 0, output.getvalue())
            panel = json.loads(output.getvalue())['review_id']
            self.assertEqual(main(['run', '--review-id', panel]), 0)
            self.assertEqual(runner.call_count, 1)
            set_gate(operations, ['dashboard'], draining=True)
            self.assertEqual(main(['run', '--review-id', panel]), 2)
            self.assertEqual(main([*start[:-1], 'blocked-panel']), 2)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(authority.call_count, 2)
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_reviews').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
