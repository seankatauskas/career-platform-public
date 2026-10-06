"""Checker profiles are resolved, authorized and frozen without changing audit scope."""
import argparse
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError, canonical_json, payload_sha256
from job_search.job_reviews.authority import ReviewAuthority
from job_search.job_reviews.runner import ProductionRuntime, ReviewCoordinator, add_arguments, command
from tests import test_review_runner as runner_fixtures
from tests.test_review_runner import FakeRuntime


class CheckerProfileTests(unittest.TestCase):
    def setUp(self):
        base = runner_fixtures.RunnerTests('runTest')
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.base = base
        self.fixture, self.config, self.authority = base.fixture, base.config, base.authority

    def authority_for(self, config):
        return ReviewAuthority(self.fixture.service, approved_model=config.model,
            approved_reasoning_effort=config.reasoning_effort,
            approved_check_model=config.check_profile()['model'],
            approved_check_reasoning_effort=config.check_profile()['reasoning_effort'],
            approved_screening_model=config.screening_model,
            approved_screening_reasoning_effort=config.screening_reasoning_effort)

    def start(self, key='checker-test'):
        return self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
            'rubric_version': 'job-review-v2', 'idempotency_key': key})['review_id']

    def ledger(self):
        return sqlite3.connect(self.fixture.service.db_path)

    def test_unset_or_partial_settings_resolve_and_invalid_values_fail(self):
        self.assertEqual(self.config.check_profile(), {'model': self.config.model,
                                                     'reasoning_effort': self.config.reasoning_effort})
        self.assertEqual(self.config.execution_policy(), replace(self.config, check_model=self.config.model,
            check_reasoning_effort=self.config.reasoning_effort).execution_policy())
        self.assertEqual(replace(self.config, check_reasoning_effort='low').validate().check_profile(),
                         {'model': self.config.model, 'reasoning_effort': 'low'})
        self.assertEqual(replace(self.config, check_model='checker').validate().check_profile(),
                         {'model': 'checker', 'reasoning_effort': self.config.reasoning_effort})
        for changes in ({'check_model': ''}, {'check_model': 'bad\nmodel'}, {'check_model': True},
                        {'check_reasoning_effort': ''}, {'check_reasoning_effort': 'extreme'},
                        {'check_reasoning_effort': True}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                replace(self.config, **changes).validate()

    def test_independent_profile_binds_checks_and_adjudication_and_is_frozen(self):
        config = replace(self.config, model='primary-model', reasoning_effort='low',
            check_model='checker-model', check_reasoning_effort='high', screening_enabled=True,
            screening_model='screen-model', screening_reasoning_effort='low').validate()
        authority = self.authority_for(config)
        runtime = FakeRuntime(authority, self.fixture.assessment())
        runner = ReviewCoordinator(config, authority, runtime, is_draining=lambda: False)
        result = runner.run(mode='custom', window_start='2026-10-01T00:00:00Z')
        self.assertEqual(result['status'], 'published')
        self.assertEqual(runner._profile('benchmark_check'), ('checker-model', 'high'))
        self.assertEqual(runner._profile('adjudicator'), ('checker-model', 'high'))
        with self.ledger() as con:
            profiles = [(kind, purpose, json.loads(value)) for kind, purpose, value in
                con.execute('SELECT kind,purpose,runtime_json FROM job_review_grants')]
            final = json.loads(con.execute('SELECT runtime_json FROM job_review_finalizer_grants').fetchone()[0])
            policy = json.loads(con.execute('SELECT policy_json FROM job_review_execution_policies').fetchone()[0])
        self.assertEqual({(kind, purpose, value['model'], value['reasoning_effort']) for kind, purpose, value in profiles},
            {('primary', 'screening', 'screen-model', 'low'), ('primary', 'detailed', 'primary-model', 'low'),
             ('check', 'detailed', 'checker-model', 'high')})
        self.assertEqual((final['model'], final['reasoning_effort']), ('primary-model', 'low'))
        self.assertEqual(policy['check'], {'model': 'checker-model', 'reasoning_effort': 'high'})
        self.assertEqual(authority.status(result['review_id'])['audit_required_count'], 1)
        production = ProductionRuntime(config, authority)
        for kind, purpose, model, effort in (('primary', 'screening', 'screen-model', 'low'),
                ('primary', 'detailed', 'primary-model', 'low'), ('check', 'detailed', 'checker-model', 'high'),
                ('finalizer', 'detailed', 'primary-model', 'low'),
                ('adjudicator', 'detailed', 'checker-model', 'high')):
            bound = production._runtime_config('job-review-v2', kind=kind, purpose=purpose)
            self.assertEqual((bound.model, bound.reasoning_effort), (model, effort))
        with self.assertRaises(ContractError):
            production._runtime_config(kind='check', purpose='screening')

    def test_fast_primary_and_stronger_checker_bind_adjudicator_through_publication(self):
        config = replace(self.config, model='primary-fast', reasoning_effort='low',
                         check_model='checker-strong', check_reasoning_effort='high').validate()
        authority = self.authority_for(config)
        runtime = FakeRuntime(authority, self.fixture.assessment(), disagreement=True, resolution='check')
        runner = ReviewCoordinator(config, authority, runtime, is_draining=lambda: False)
        result = runner.run(mode='custom', window_start='2026-10-01T00:00:00Z')
        self.assertEqual(result['status'], 'published')
        self.assertEqual([kind for kind, _ in runtime.calls], ['primary', 'check', 'adjudicator', 'finalizer'])
        with self.ledger() as con:
            profile = json.loads(con.execute('SELECT runtime_json FROM job_review_adjudicator_grants').fetchone()[0])
            policy = json.loads(con.execute('SELECT policy_json FROM job_review_execution_policies').fetchone()[0])
        self.assertEqual({key:profile[key] for key in ('model','reasoning_effort')}, policy['check'])
        self.assertEqual(profile['model'], 'checker-strong')
        self.assertEqual(authority.preview(result['review_id'])['targeted_count'], 0)
        rid = authority.start({'mode':'custom','window_start':'2026-10-01T00:00:00Z',
            'rubric_version':'job-review-v2','idempotency_key':'reject-fast-adjudicator'})['review_id']
        authority.freeze_execution_policy(rid, config.execution_policy())
        with self.assertRaisesRegex(ContractError,'frozen checker profile'):
            authority.issue(rid,[1],'adjudicator',{'runtime_id':'wrong-adjudicator',
                'model':config.model,'reasoning_effort':config.reasoning_effort})
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_adjudicator_items WHERE review_id=?',(rid,)).fetchone()[0],0)

    def test_authority_rejects_model_swapping_between_primary_and_check(self):
        config = replace(self.config, check_model='checker', check_reasoning_effort='medium')
        authority = self.authority_for(config)
        rid = self.start()
        for kind, profile in [('primary', config.check_profile()),
                              ('check', {'model': config.model, 'reasoning_effort': config.reasoning_effort})]:
            with self.subTest(kind=kind), self.assertRaisesRegex(ContractError, 'trusted runtime'):
                authority.issue(rid, [1], kind, dict(profile, runtime_id='wrong-' + kind))
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_grants').fetchone()[0], 0)

    def test_legacy_policy_resolves_original_profile_and_preserves_hash(self):
        rid = self.start()
        legacy = self.config.execution_policy()
        del legacy['check']
        digest = payload_sha256(legacy)
        with self.ledger() as con:
            con.execute('INSERT INTO job_review_execution_policies VALUES(?,?,?,?)',
                        (rid, canonical_json(legacy), digest, self.fixture.now))
        frozen = self.authority.freeze_execution_policy(rid, self.config.execution_policy())
        self.assertEqual(frozen['policy_sha256'], digest)
        self.assertEqual(frozen['policy']['check'], legacy['detailed'])
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT policy_json,policy_sha256 FROM job_review_execution_policies').fetchone(),
                             (canonical_json(legacy), digest))
        changed = replace(self.config, check_model='different-checker')
        with self.assertRaises(ContractError):
            self.authority_for(changed).freeze_execution_policy(rid, changed.execution_policy())
        runtime = FakeRuntime(self.authority, self.fixture.assessment(), disagreement=True, resolution='check')
        result = ReviewCoordinator(self.config, self.authority, runtime, is_draining=lambda:False).run(review_id=rid)
        self.assertEqual(result['status'],'published')
        with self.ledger() as con:
            profile=json.loads(con.execute('SELECT runtime_json FROM job_review_adjudicator_grants').fetchone()[0])
            self.assertEqual({key:profile[key] for key in ('model','reasoning_effort')},legacy['detailed'])
            self.assertEqual(con.execute('SELECT policy_json,policy_sha256 FROM job_review_execution_policies').fetchone(),
                             (canonical_json(legacy),digest))

    def test_legacy_work_without_policy_cannot_adopt_new_checker(self):
        rid = self.start()
        runtime = {'runtime_id': 'historical-worker', 'model': self.config.model,
                   'reasoning_effort': self.config.reasoning_effort}
        with self.ledger() as con:
            context = con.execute('SELECT context_sha256 FROM job_reviews WHERE review_id=?', (rid,)).fetchone()[0]
            con.execute('INSERT INTO job_review_grants '
                '(grant_id,token_sha256,review_id,actor,kind,context_sha256,runtime_id,runtime_json,created_at,expires_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)', ('legacy-grant', 'legacy-token', rid, 'legacy-actor', 'primary',
                context, runtime['runtime_id'], canonical_json(runtime), self.fixture.now, self.fixture.now))
        changed = replace(self.config, check_model='different-checker')
        with self.assertRaisesRegex(ContractError, 'legacy review work'):
            self.authority_for(changed).freeze_execution_policy(rid, changed.execution_policy())
        self.authority.freeze_execution_policy(rid, self.config.execution_policy())

    def test_changed_checker_resume_rejects_before_recovery_or_dispatch(self):
        rid = self.start()
        config = replace(self.config, check_model='checker', check_reasoning_effort='high')
        authority = self.authority_for(config)
        authority.freeze_execution_policy(rid, config.execution_policy())
        grant = authority.issue(rid, [1], 'primary', {'runtime_id': 'pending-worker',
            'model': config.model, 'reasoning_effort': config.reasoning_effort})
        for changed in (replace(config, check_model='other'), replace(config, check_reasoning_effort='low'),
                        replace(config, check_model=None, check_reasoning_effort=None)):
            altered = self.authority_for(changed)
            runtime = FakeRuntime(altered, self.fixture.assessment())
            with self.subTest(profile=changed.check_profile()), self.assertRaises(ContractError):
                ReviewCoordinator(changed, altered, runtime, is_draining=lambda: False).run(review_id=rid)
            self.assertEqual(runtime.calls, [])
            self.assertEqual(runtime.recovered, [])
        with self.ledger() as con:
            self.assertIsNone(con.execute('SELECT revoked_at FROM job_review_grants WHERE grant_id=?',
                                         (grant['grant_id'],)).fetchone()[0])

    def test_benchmark_overrides_execute_with_bound_checker_and_leave_defaults_alone(self):
        source = self.start()
        panel = self.authority.start_benchmark({'review_id': source, 'ordinals': [1],
            'label': 'separate checker profile', 'idempotency_key': 'checker-panel'})['review_id']
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(['run', '--runner-config', str(self.fixture.root / 'runner.json'),
            '--review-id', panel, '--benchmark-model', 'primary-model', '--benchmark-reasoning-effort', 'low',
            '--benchmark-check-model', 'checker-model', '--benchmark-check-reasoning-effort', 'high'])
        with patch('job_search.job_reviews.runner.load_runner_config', return_value=self.config), \
                patch('job_search.job_reviews.runner.require_start'), \
                patch('job_search.job_reviews.runner.build_authority', return_value=self.authority), \
                patch('job_search.job_reviews.runner.ProductionRuntime',
                      side_effect=lambda config, authority: FakeRuntime(authority, self.fixture.assessment())):
            result = command(args)
        self.assertEqual(result['status'], 'benchmark_complete')
        with self.ledger() as con:
            actual = {kind: json.loads(raw)['model'] for kind, raw in con.execute(
                'SELECT kind,runtime_json FROM job_review_grants WHERE review_id=?', (panel,))}
        self.assertEqual(actual, {'primary': 'primary-model', 'check': 'checker-model'})
        self.assertIsNone(self.config.check_model)
        self.assertEqual(self.authority.status(source)['counts'], {'pending': 1})

    def test_check_all_uses_checker_profile_without_changing_normal_audit_sample(self):
        self.base.config = replace(self.config, check_model='checker-model', check_reasoning_effort='high')
        self.base.authority = self.authority_for(self.base.config)
        # The existing 60-job exercise asserts 50 normal audits versus 60 with
        # check-all, separate phase counts, and no repeated assigned ordinals.
        self.base.test_check_all_benchmark_adds_blind_checks_beyond_unchanged_exclusion_sample()
        with self.ledger() as con:
            profiles = [json.loads(row[0]) for row in con.execute(
                "SELECT runtime_json FROM job_review_grants WHERE kind='check'")]
        self.assertTrue(profiles)
        self.assertEqual({(value['model'], value['reasoning_effort']) for value in profiles},
                         {('checker-model', 'high')})


if __name__ == '__main__':
    unittest.main()
