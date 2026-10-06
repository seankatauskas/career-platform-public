"""Offline enforcement of scoped reviewer identity, leases and replay safety."""
import copy
import hashlib
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from job_search.contracts import ContractError, payload_sha256
from job_search.job_reviews.authority import (MODEL, REASONING_EFFORT, ReviewAuthority,
    ReviewAuthorizationError, ReviewConflictError)
from tests import test_agent_job_reviews as fixtures


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.authority = ReviewAuthority(self.fixture.service)
        self.runtime_sequence = 0

    def start(self, **kwargs):
        kwargs.setdefault('rubric_version', 'job-review-v1')
        return self.authority.start(dict(mode='custom', window_start='2026-10-01T00:00:00Z',
                                         idempotency_key='managed-start', **kwargs))['review_id']

    def issue(self, rid, ordinals=(1,), kind='primary', launched=True):
        self.runtime_sequence += 1
        runtime_id = f'runtime-{self.runtime_sequence}'
        grant = self.authority.issue(rid, list(ordinals), kind,
            {'runtime_id': runtime_id, 'model': MODEL, 'reasoning_effort': REASONING_EFFORT})
        if launched:
            self.authority.mark_launch(grant['grant_id'], runtime_id, self.launch_receipt(runtime_id))
        return grant

    def launch_receipt(self, name):
        return {'container_id': hashlib.sha256(name.encode()).hexdigest(),
                'image_digest': 'sha256:' + '1' * 64, 'config_sha256': '2' * 64}

    def read(self, grant, ordinal=1):
        offset = 0
        while True:
            page = self.authority.scoped_call(grant['token'], 'job', {'ordinal': ordinal, 'offset': offset})
            if page['next_offset'] is None:
                return page
            offset = page['next_offset']

    def assess(self, grant, ordinal=1, value=None):
        self.read(grant, ordinal)
        return self.authority.scoped_call(grant['token'], 'assessment',
            {'ordinal': ordinal, 'assessment': value or self.fixture.assessment()})

    def ledger(self):
        return closing(sqlite3.connect(self.fixture.ledger.store.db_path))

    def test_managed_review_is_server_marked_full_window_and_legacy_remains_compatible(self):
        self.fixture.insert('b')
        rid = self.start()
        status = self.authority.status(rid)
        self.assertEqual(status['metadata']['execution_mode'], 'isolated')
        self.assertEqual(status['total'], 2)
        with self.assertRaises(ContractError):
            self.fixture.command('start', mode='custom', window_start='2026-10-01T00:00:00Z',
                                 execution_mode='isolated')
        legacy = self.fixture.start()
        self.fixture.assess(legacy)
        self.assertNotIn('execution_mode', self.fixture.command('status', review_id=legacy)['metadata'])

    def test_generic_managed_mutations_block_even_with_real_actor(self):
        rid = self.start()
        grant = self.issue(rid)
        mutations = {
            'job': {'ordinal': 1, 'actor': grant['actor']},
            'claim': {'actor': grant['actor']},
            'assess': {'ordinal': 1, 'actor': grant['actor'], 'kind': 'primary', 'expected_revision': 0,
                       'assessment': self.fixture.assessment()},
            'refresh-job': {'ordinal': 1, 'actor': 'isolated-coordinator', 'expected_revision': 0},
            'abandon': {'reason': 'must not abandon'},
            'publish': {'preview_sha256': 'anything'},
        }
        for action, args in mutations.items():
            with self.subTest(action=action), self.assertRaisesRegex(ContractError, 'scoped reviewer'):
                self.fixture.command(action, review_id=rid, **args)
        self.assertEqual(self.authority.status(rid)['counts'], {'pending': 1})

    def test_generic_idempotent_replay_does_not_bypass_managed_guard(self):
        rid = self.start()
        grant = self.issue(rid)
        self.assess(grant)
        with self.assertRaisesRegex(ContractError, 'scoped reviewer'):
            self.fixture.service.call('assess', {'review_id': rid, 'ordinal': 1, 'actor': grant['actor'],
                'kind': 'primary', 'expected_revision': 0, 'assessment': self.fixture.assessment(),
                'idempotency_key': f"grant:{grant['grant_id']}:1"})
        with self.assertRaisesRegex(ContractError, 'scoped reviewer'):
            self.fixture.service.call('start', {'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                               'idempotency_key': 'managed-start'})

    def test_token_hashed_at_rest_and_launch_required(self):
        rid = self.start()
        grant = self.issue(rid, launched=False)
        with self.ledger() as con:
            row = con.execute('SELECT token_sha256,launch_json FROM job_review_grants').fetchone()
        self.assertEqual(row, (hashlib.sha256(grant['token'].encode()).hexdigest(), None))
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(grant['token'], 'assignment')
        self.authority.mark_launch(grant['grant_id'], 'runtime-1', self.launch_receipt('runtime-1'))
        assignment = self.authority.scoped_call(grant['token'], 'assignment')
        self.assertNotIn('token', json.dumps(assignment))
        self.assertNotIn('actor', assignment)

    def test_token_expiry_revocation_and_unknown_capability(self):
        rid = self.start()
        grant = self.issue(rid)
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call('x' * 43, 'assignment')
        self.fixture.now = '2026-10-02T01:30:00.000000Z'
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(grant['token'], 'assignment')
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.renew(grant['grant_id'])
        replacement = self.issue(rid)
        self.authority.revoke(replacement['grant_id'])
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(replacement['token'], 'assignment')

    def test_fields_and_operations_cannot_expand_assignment(self):
        self.fixture.insert('b')
        rid = self.start()
        grant = self.issue(rid)
        for operation in ('publish', 'start', 'claim', 'feedback', 'sql', 'ranking', 'refresh-job'):
            with self.subTest(operation=operation), self.assertRaises(ContractError):
                self.authority.scoped_call(grant['token'], operation, {})
        for field in ('review_id', 'actor', 'kind', 'idempotency_key', 'expected_revision', 'trusted'):
            with self.subTest(field=field), self.assertRaises(ContractError):
                self.authority.scoped_call(grant['token'], 'job', {'ordinal': 1, field: 'injected'})
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(grant['token'], 'job', {'ordinal': 2})
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(grant['token'], 'assessment', {'ordinal': 2, 'assessment': self.fixture.assessment()})

    def test_full_description_and_fact_evidence_required_before_assessment(self):
        with closing(sqlite3.connect(self.fixture.db)) as con, con:
            con.execute('UPDATE jobs SET description=?', ('prefix ' * 2000 + 'Python APIs',))
        rid = self.start()
        grant = self.issue(rid)
        args = {'ordinal': 1, 'assessment': self.fixture.assessment()}
        with self.assertRaisesRegex(ContractError, 'all frozen'):
            self.authority.scoped_call(grant['token'], 'assessment', args)
        self.authority.scoped_call(grant['token'], 'job', {'ordinal': 1, 'offset': 12000})
        with self.assertRaisesRegex(ContractError, 'all frozen'):
            self.authority.scoped_call(grant['token'], 'assessment', args)
        self.read(grant)
        bad = copy.deepcopy(args)
        bad['assessment']['evidence'][0]['fact_id'] = 'invented'
        with self.assertRaisesRegex(ContractError, 'unknown profile'):
            self.authority.scoped_call(grant['token'], 'assessment', bad)
        self.assertEqual(self.authority.scoped_call(grant['token'], 'assessment', args)['revision'], 1)

    def test_blind_projection_no_old_assessment_lists_or_source_diagnostics(self):
        self.fixture.profile['model_score'] = 'MODEL_ONLY_SENTINEL'
        self.fixture.profile['facts'][0]['model_rank'] = 'MODEL_ONLY_SENTINEL'
        note = 'Ranking infrastructure interests me; senior ownership does not.'
        self.fixture.command('feedback', note=note)
        rid = self.start()
        with self.ledger() as con, con:
            row = con.execute('SELECT snapshot_json FROM job_review_items WHERE review_id=?', (rid,)).fetchone()
            job = json.loads(row[0])
            job.update(model_score='MODEL_ONLY_SENTINEL', model_rank='MODEL_ONLY_SENTINEL',
                       department='Infrastructure', team='Developer Experience', workplaceType='Remote')
            con.execute('UPDATE job_review_items SET snapshot_json=? WHERE review_id=?', (json.dumps(job), rid))
        primary = self.issue(rid)
        self.assess(primary)
        check = self.issue(rid, kind='check')
        page = self.read(check)
        self.assertNotIn('MODEL_ONLY_SENTINEL', json.dumps(page))
        self.assertEqual(page['job']['department'], 'Infrastructure')
        self.assertNotIn('assessment', page)
        self.assertNotIn('previous_lists', page)
        context = self.authority.scoped_call(check['token'], 'context', {'section': 'facts'})
        self.assertNotIn('MODEL_ONLY_SENTINEL', json.dumps(context))
        self.assertNotIn('previous_review', context)
        feedback = self.authority.scoped_call(check['token'], 'context', {'section': 'feedback'})
        self.assertEqual(feedback['feedback'][0]['note'], note)

    def test_lost_response_replay_is_exact_after_revision_advanced(self):
        rid = self.start()
        grant = self.issue(rid)
        first = self.assess(grant)
        request = {'ordinal': 1, 'assessment': self.fixture.assessment()}
        self.assertEqual(self.authority.scoped_call(grant['token'], 'assessment', request), first)
        check = self.issue(rid, kind='check')
        self.assess(check)
        self.assertEqual(self.authority.scoped_call(grant['token'], 'assessment', request), first)
        request['assessment']['explanation'] = 'Different response'
        with self.assertRaises(ReviewConflictError):
            self.authority.scoped_call(grant['token'], 'assessment', request)
        with self.ledger() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM job_review_revisions WHERE kind='primary'").fetchone()[0], 1)

    def test_concurrent_identical_submissions_commit_once(self):
        rid = self.start()
        grant = self.issue(rid)
        self.read(grant)
        args = {'ordinal': 1, 'assessment': self.fixture.assessment()}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.authority.scoped_call(grant['token'], 'assessment', args), range(2)))
        self.assertEqual(results[0], results[1])
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_revisions').fetchone()[0], 1)

    def test_transaction_failure_rolls_back_assessment_and_receipt(self):
        rid = self.start()
        grant = self.issue(rid)
        self.read(grant)
        args = {'ordinal': 1, 'assessment': self.fixture.assessment()}
        with patch.object(self.fixture.service, '_touch', side_effect=RuntimeError('injected crash')):
            with self.assertRaises(RuntimeError):
                self.authority.scoped_call(grant['token'], 'assessment', args)
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT revision FROM job_review_items').fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM job_review_commands WHERE operation='assess'").fetchone()[0], 0)
        self.assertEqual(self.authority.scoped_call(grant['token'], 'assessment', args)['revision'], 1)

    def test_renew_only_pending_slots_and_clear_check_claims(self):
        self.fixture.insert('b')
        rid = self.start()
        primary = self.issue(rid, ordinals=[1, 2])
        self.assess(primary, 1)
        self.fixture.now = '2026-10-02T01:15:00.000000Z'
        receipt = self.authority.renew(primary['grant_id'])
        self.assertEqual(receipt['pending_count'], 1)
        self.assertEqual(receipt['expires_at'], '2026-10-02T01:45:00.000000Z')
        check = self.issue(rid, kind='check')
        self.assess(check)
        self.assertEqual(self.authority.renew(check['grant_id'])['pending_count'], 0)
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT claim_owner,claim_until FROM job_review_items WHERE ordinal=1').fetchone(), (None, None))

    def test_conflicting_lease_and_revoke_release(self):
        rid = self.start()
        first = self.issue(rid)
        with self.assertRaises(ReviewConflictError):
            self.issue(rid)
        self.authority.revoke(first['grant_id'])
        replacement = self.issue(rid)
        self.assertNotEqual(first['actor'], replacement['actor'])
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(first['token'], 'job', {'ordinal': 1})

    def test_refresh_revokes_old_grants_and_requires_fresh_snapshot(self):
        rid = self.start()
        grant = self.issue(rid)
        with closing(sqlite3.connect(self.fixture.db)) as con, con:
            con.execute("UPDATE jobs SET description=description || ' Changed scope.'")
        self.authority.refresh(rid, 1, 0)
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(grant['token'], 'assignment')
        replacement = self.issue(rid)
        self.assertIn('Changed scope.', self.read(replacement)['description'])

    def test_stale_revision_and_context_fail_closed(self):
        rid = self.start()
        grant = self.issue(rid)
        with self.ledger() as con, con:
            con.execute('UPDATE job_review_items SET revision=revision+1')
        with self.assertRaises(ReviewConflictError):
            self.read(grant)
        with self.ledger() as con, con:
            con.execute('UPDATE job_review_items SET revision=0')
            con.execute("UPDATE job_reviews SET context_sha256='changed'")
        with self.assertRaises(ReviewConflictError):
            self.authority.scoped_call(grant['token'], 'context', {'section': 'facts'})

    def test_runtime_configuration_launch_receipt_and_independence_enforced(self):
        rid = self.start()
        with self.assertRaises(ContractError):
            self.authority.issue(rid, [1], 'primary', {'runtime_id': 'r', 'model': 'other', 'reasoning_effort': 'high'})
        grant = self.issue(rid, launched=False)
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.mark_launch(grant['grant_id'], 'foreign', self.launch_receipt('r'))
        good = self.launch_receipt('runtime-1')
        receipt = self.authority.mark_launch(grant['grant_id'], 'runtime-1', good)
        self.assertEqual(self.authority.mark_launch(grant['grant_id'], 'runtime-1', good), receipt)
        with self.assertRaises(ReviewConflictError):
            self.authority.mark_launch(grant['grant_id'], 'runtime-1', self.launch_receipt('different'))
        self.assess(grant)
        with self.assertRaises(ReviewConflictError):
            self.authority.issue(rid, [1], 'check', {'runtime_id': 'runtime-1', 'model': MODEL, 'reasoning_effort': 'high'})
        check = self.issue(rid, kind='check', launched=False)
        with self.assertRaises(ReviewConflictError):
            self.authority.mark_launch(check['grant_id'], f'runtime-{self.runtime_sequence}', good)

    def test_publication_keeps_disagreements_blocking_and_no_auto_agreement(self):
        rid = self.start()
        self.assess(self.issue(rid))
        self.assess(self.issue(rid, kind='check'), value=self.fixture.assessment('broad_only'))
        preview = self.authority.preview(rid)
        self.assertIn('reviewer_disagreements', preview['blockers'])
        with self.assertRaises(ContractError):
            self.authority.publish(rid, preview['preview_sha256'], 'publish-disagreed')
        self.assertEqual(self.authority.status(rid)['disagreement_count'], 1)
        self.assertEqual(self.authority.status(rid)['status'], 'active')

    def test_verified_publication_and_plain_generic_publish_replay_denied(self):
        rid = self.start()
        # Whitespace is normalized by assessment validation; provenance follows the
        # immutable revision, not a recomputed hash of the unnormalized request.
        value = self.fixture.assessment(explanation='  Python API fit.  ')
        self.assess(self.issue(rid), value=value)
        self.assess(self.issue(rid, kind='check'))
        preview = self.authority.preview(rid)
        self.assertTrue(preview['ready'])
        receipt = self.authority.publish(rid, preview['preview_sha256'], 'publish-managed')
        self.assertEqual(len(receipt['lists']), 2)
        self.assertEqual(self.authority.publish(rid, preview['preview_sha256'], 'publish-managed'), receipt)
        with self.assertRaisesRegex(ContractError, 'scoped reviewer'):
            self.fixture.service.call('publish', {'review_id': rid, 'preview_sha256': preview['preview_sha256'],
                                                'idempotency_key': 'publish-managed'})

    def test_provenance_tampering_prevents_preview(self):
        rid = self.start()
        grant = self.issue(rid)
        self.assess(grant)
        with self.ledger() as con, con:
            con.execute('UPDATE job_review_grants SET launch_json=NULL')
        with self.assertRaisesRegex(ReviewConflictError, 'container launch'):
            self.authority.preview(rid)

    def test_schema_migration_and_trusted_abandon(self):
        rid = self.start()
        grant = self.issue(rid)
        with self.ledger() as con:
            from job_search.db import MIGRATIONS
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], MIGRATIONS[-1][0])
            self.assertEqual(con.execute('SELECT name FROM schema_migrations WHERE version=16').fetchone()[0], 'isolated_review_authority')
        self.authority.abandon(rid, 'Manual trial stopped', 'abandon-managed')
        self.assertEqual(self.authority.status(rid)['status'], 'abandoned')
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(grant['token'], 'assignment')

    def test_model_choice_is_trusted_configuration_not_a_request_field(self):
        rid = self.start()
        other = ReviewAuthority(self.fixture.service, approved_model='explicitly-selected-model',
                                approved_reasoning_effort='medium')
        with self.assertRaises(ContractError):
            other.issue(rid, [1], 'primary', {'runtime_id': 'bad-model', 'model': MODEL, 'reasoning_effort': 'high'})
        grant = other.issue(rid, [1], 'primary', {'runtime_id': 'selected-runtime',
            'model': 'explicitly-selected-model', 'reasoning_effort': 'medium'})
        other.mark_launch(grant['grant_id'], 'selected-runtime', self.launch_receipt('selected-runtime'))
        with self.assertRaises(ContractError):
            other.scoped_call(grant['token'], 'assignment', {'model': MODEL})

    def test_coordinator_cannot_convert_legacy_start_receipt_into_managed_review(self):
        args = {'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z', 'idempotency_key': 'legacy-start'}
        rid = self.fixture.service.call('start', args)['review_id']
        with self.assertRaisesRegex(ContractError, 'not managed'):
            self.authority.start(args)
        self.assertNotIn('execution_mode', self.fixture.command('status', review_id=rid)['metadata'])

    def test_recovery_revokes_pending_and_prelaunch_grants_and_keeps_coverage_resumable(self):
        self.fixture.insert('b')
        rid = self.start()
        launched = self.issue(rid, [1])
        before_launch = self.issue(rid, [2], launched=False)
        result = self.authority.reconcile_interrupted()
        self.assertEqual(result['revoked_grants'], 2)
        self.assertEqual(result['released_claims'], 2)
        workers = {w['grant_id']: w for w in result['workers']}
        self.assertEqual(workers[launched['grant_id']]['container_id'], self.launch_receipt('runtime-1')['container_id'])
        self.assertIsNone(workers[before_launch['grant_id']]['container_id'])
        self.assertNotIn('token', json.dumps(result))
        self.assertNotIn('Python APIs', json.dumps(result))
        for grant in (launched, before_launch):
            with self.assertRaises(ReviewAuthorizationError):
                self.authority.scoped_call(grant['token'], 'assignment')
        self.assertEqual(self.authority.status(rid)['counts'], {'pending': 2})
        self.assertEqual(self.authority.status(rid)['status'], 'active')
        self.assess(self.issue(rid, [1]))
        self.assertEqual(self.authority.status(rid)['counts'], {'close': 1, 'pending': 1})

    def test_recovery_preserves_committed_receipts_and_independent_check_progress(self):
        self.fixture.insert('b')
        rid = self.start()
        primary = self.issue(rid, [1, 2])
        committed = self.assess(primary, 1)
        check = self.issue(rid, [1], kind='check')
        self.assess(check)
        with self.ledger() as con:
            before = con.execute('SELECT * FROM job_review_commands ORDER BY idempotency_key').fetchall()
            revisions = con.execute('SELECT * FROM job_review_revisions ORDER BY revision').fetchall()
        result = self.authority.reconcile_interrupted(rid)
        self.assertEqual(result['released_claims'], 1)
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT * FROM job_review_commands ORDER BY idempotency_key').fetchall(), before)
            self.assertEqual(con.execute('SELECT * FROM job_review_revisions ORDER BY revision').fetchall(), revisions)
            self.assertEqual(json.loads(con.execute('SELECT response_json FROM job_review_commands WHERE idempotency_key=?',
                (f"grant:{primary['grant_id']}:1",)).fetchone()[0]), committed)
        pending = self.fixture.service.call('batch', {'review_id': rid, 'filter': 'pending'})
        self.assertEqual([j['ordinal'] for j in pending['items']], [2])
        self.assertEqual(self.authority.status(rid)['audit_remaining'], [])
        self.assess(self.issue(rid, [2]), 2)
        self.assess(self.issue(rid, [2], kind='check'), 2)
        self.assertTrue(self.authority.preview(rid)['ready'])

    def test_recovery_inventory_survives_crash_between_revoke_and_container_cleanup(self):
        rid = self.start()
        grant = self.issue(rid)
        first = self.authority.reconcile_interrupted(rid)
        second = self.authority.reconcile_interrupted(rid)
        self.assertEqual(first['workers'], second['workers'])
        self.assertEqual(second['revoked_grants'], 0)
        self.assertEqual(second['released_claims'], 0)
        self.assertEqual(second['workers'][0]['grant_id'], grant['grant_id'])

    def test_recovery_scope_does_not_touch_other_reviews_or_legacy_claims(self):
        first = self.start()
        grant = self.issue(first)
        second = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                      'idempotency_key': 'second-managed-start'})['review_id']
        other = self.issue(second)
        legacy = self.fixture.start()
        self.fixture.command('claim', review_id=legacy, actor='legacy-reviewer')
        result = self.authority.reconcile_interrupted(first)
        self.assertEqual([w['grant_id'] for w in result['workers']], [grant['grant_id']])
        self.assertEqual(self.authority.scoped_call(other['token'], 'assignment')['grant_id'], other['grant_id'])
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT claim_owner FROM job_review_items WHERE review_id=?',
                                         (legacy,)).fetchone()[0], 'legacy-reviewer')
        with self.assertRaises(ContractError):
            self.authority.reconcile_interrupted(legacy)

    def test_recovery_rejects_damaged_runtime_binding_atomically(self):
        self.fixture.insert('b')
        rid = self.start()
        first = self.issue(rid, [1])
        bad = self.issue(rid, [2])
        with self.ledger() as con, con:
            con.execute('UPDATE job_review_grants SET runtime_json=? WHERE grant_id=?',
                        (json.dumps({'runtime_id': 'foreign'}), bad['grant_id']))
        with self.assertRaisesRegex(ReviewConflictError, 'intact runtime'):
            self.authority.reconcile_interrupted()
        # Validation precedes all changes; the operator receives a repairable failure.
        self.assertEqual(self.authority.scoped_call(first['token'], 'assignment')['grant_id'], first['grant_id'])
        with self.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_grants WHERE revoked_at IS NOT NULL').fetchone()[0], 0)

    def test_recovery_includes_expired_grants_for_orphan_cleanup(self):
        rid = self.start()
        grant = self.issue(rid)
        self.fixture.now = '2026-10-02T02:00:00.000000Z'
        result = self.authority.reconcile_interrupted()
        self.assertEqual(result['revoked_grants'], 1)
        self.assertEqual(result['workers'][0]['grant_id'], grant['grant_id'])
        self.assertEqual(result['released_claims'], 1)


if __name__ == '__main__':
    unittest.main()
