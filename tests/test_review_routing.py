"""Conservative screening preserves pending coverage, source binding and phase scope."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.authority import ReviewAuthority, ReviewAuthorizationError, ReviewConflictError
from job_search.job_reviews.brief import suggested_search_brief
from job_search.job_reviews.reviewer_api import ReviewerClient, assignment_proxy, project_response
from job_search.job_reviews.reviewer_mcp import tools_for
from tests import test_review_authority as fixtures


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.AuthorityTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.fixture.insert('b', title='Preschool Teacher', description='Teach children in preschool classrooms.')
        self.authority = ReviewAuthority(self.fixture.service, approved_model='gpt-6-sol',
            approved_reasoning_effort='high', approved_screening_model='gpt-6-luna',
            approved_screening_reasoning_effort='low')
        self.policy = {'version': 1, 'detailed': {'model': 'gpt-6-sol', 'reasoning_effort': 'high'},
                       'check': {'model': 'gpt-6-sol', 'reasoning_effort': 'high'},
                       'screening': {'model': 'gpt-6-luna', 'reasoning_effort': 'low'},
                       'concurrency': 2, 'batch_size': 20, 'screening_batch_size': 100}
        self.sequence = 0

    def start(self, rubric='job-review-v2', policy=True):
        rid = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                   'rubric_version': rubric, 'idempotency_key': 'routing-start'})['review_id']
        if policy:
            self.authority.freeze_execution_policy(rid, self.policy)
        return rid

    def issue(self, rid, ordinals=(1, 2), kind='primary', purpose='screening', launched=True):
        self.sequence += 1
        runtime_id = 'route-worker-' + str(self.sequence)
        profile = self.policy[purpose]
        grant = self.authority.issue(rid, ordinals, kind, dict(profile, runtime_id=runtime_id), purpose=purpose)
        if launched:
            self.authority.mark_launch(grant['grant_id'], runtime_id, self.base.launch_receipt(runtime_id))
        return grant

    def route(self, grant, entries):
        return self.authority.scoped_call(grant['token'], 'routes', {'routes': entries})

    def read(self, grant, ordinal):
        offset = 0
        while True:
            value = self.authority.scoped_call(grant['token'], 'job', {'ordinal': ordinal, 'offset': offset})
            if value['next_offset'] is None:
                return value
            offset = value['next_offset']

    def exclusion(self):
        return {'ordinal': 2, 'route': 'exclude', 'reason_code': 'non_technical',
                'explanation': 'PRIVATE_SCREEN_REASON: preschool teaching does not involve engineering.',
                'evidence': [{'field': 'description', 'quote': 'Teach children in preschool classrooms.'}]}

    def test_routes_are_durable_pending_and_blind_to_later_reviewers(self):
        rid = self.start()
        screen = self.issue(rid)
        self.read(screen, 2)
        entries = [{'ordinal': 1, 'route': 'detailed'}, self.exclusion()]
        result = self.route(screen, entries)
        self.assertEqual([v['status'] for v in result['results']], ['saved', 'saved'])
        self.assertEqual(result, self.route(screen, entries))
        self.assertEqual(self.authority.status(rid)['counts'], {'pending': 1, 'exclude': 1})
        self.assertEqual(self.authority.pending_routes(rid)['ordinals'], [])
        self.assertEqual(self.authority.pending_detailed(rid)['ordinals'], [1])
        self.authority.revoke(screen['grant_id'])
        detail = self.issue(rid, (1,), purpose='detailed')
        check = self.issue(rid, (2,), kind='check', purpose='detailed')
        for grant, ordinal in ((detail, 1), (check, 2)):
            packet = [self.authority.scoped_call(grant['token'], 'assignment'), self.read(grant, ordinal),
                      self.authority.scoped_call(grant['token'], 'context')]
            self.assertNotIn('PRIVATE_SCREEN_REASON', json.dumps(packet))
            self.assertNotIn('routes', json.dumps(packet))
        with self.base.ledger() as con:
            value = json.loads(con.execute('SELECT assessment_json FROM job_review_items WHERE review_id=? AND ordinal=2', (rid,)).fetchone()[0])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_routes').fetchone()[0], 2)
        self.authority.scoped_call(check['token'], 'assessment', {'ordinal': 2, 'assessment': value})
        self.assertIn('unassessed_jobs', self.authority.preview(rid)['blockers'])

    def test_screening_cannot_submit_cards_and_detailed_cannot_submit_routes(self):
        rid = self.start()
        screen = self.issue(rid)
        for operation, args in [('assessment', {'ordinal': 1, 'assessment': self.fixture.assessment()}),
                                ('assessments', {'assessments': [{'ordinal': 1, 'assessment': {}}]}),
                                ('calibrations', {'calibrations': [{'ordinal': 1, 'position': 1}]})]:
            with self.assertRaises(ReviewAuthorizationError):
                self.authority.scoped_call(screen['token'], operation, args)
        self.route(screen, [{'ordinal': 1, 'route': 'detailed'}])
        detail = self.issue(rid, (1,), purpose='detailed')
        with self.assertRaises(ReviewAuthorizationError):
            self.route(detail, [{'ordinal': 1, 'route': 'detailed'}])
        with self.assertRaises(ContractError):
            self.issue(rid, (2,), kind='check', purpose='screening')
        self.assertEqual({t['name'] for t in tools_for('primary', 'job-review-v2', 'screening')},
                         {'review_assignment', 'review_context', 'review_job', 'review_routes'})
        self.assertNotIn('review_routes', {t['name'] for t in tools_for('check', 'job-review-v2')})

    def test_no_screening_qualification_duplicate_needs_info_or_partial_source_exclusion(self):
        rid = self.start(); grant = self.issue(rid)
        self.assertEqual(self.route(grant, [self.exclusion()])['results'][0]['status'], 'error')
        self.read(grant, 2)
        for changes in ({'reason_code': 'qualification_gap'}, {'reason_code': 'duplicate'},
                        {'route': 'needs_info'}, {'route': 'close'},
                        {'evidence': [{'field': 'title', 'quote': 'Preschool Teacher'}]},
                        {'evidence': [{'field': 'description', 'quote': 'fabricated quote'}]}):
            entry = dict(self.exclusion(), **changes)
            self.assertEqual(self.route(grant, [entry])['results'][0]['status'], 'error')
        self.assertEqual(self.authority.status(rid)['counts'], {'pending': 2})
        self.assertEqual(self.authority.pending_routes(rid)['ordinals'], [1, 2])

    def test_location_requires_saved_scope_and_preserves_alignment(self):
        with sqlite3.connect(self.fixture.db) as con:
            con.execute("UPDATE jobs SET location='London, United Kingdom' WHERE id='a'")
        brief = suggested_search_brief()
        self.fixture.command('save-brief', expected_revision=0, brief=brief)
        rid = self.start(); grant = self.issue(rid)
        self.read(grant, 1)
        entry = {'ordinal': 1, 'route': 'exclude', 'reason_code': 'location', 'brief_revision': 2,
                 'family': 'backend', 'alignment': 'core', 'explanation': 'Location outside saved scope.',
                 'evidence': [{'field': 'description', 'quote': 'Python APIs'},
                              {'field': 'location', 'quote': 'London, United Kingdom'}]}
        self.assertEqual(self.route(grant, [entry])['results'][0]['status'], 'error')
        entry['brief_revision'] = 1
        self.assertEqual(self.route(grant, [entry])['results'][0]['status'], 'saved')
        with self.base.ledger() as con:
            value = json.loads(con.execute('SELECT assessment_json FROM job_review_items WHERE review_id=? AND ordinal=1', (rid,)).fetchone()[0])
        self.assertEqual(value['alignment'], 'core')

    def test_migration_preserves_legacy_grants_and_defaults_only_the_new_purpose(self):
        from job_search.db import MIGRATIONS
        with sqlite3.connect(':memory:') as con:
            for version, _, sql in MIGRATIONS:
                if version < 22:
                    con.executescript(sql)
            con.execute("INSERT INTO job_reviews(review_id,mode,window_start,window_end,created_at,updated_at,status,context_json,context_sha256,metadata_json) VALUES('legacy','custom','a','b','c','c','active','{}','context','{}')")
            con.execute("INSERT INTO job_review_grants VALUES('grant','token','legacy','actor','primary','context','runtime','{}',NULL,'now','later',NULL)")
            con.execute("INSERT INTO job_review_grants VALUES('check','check-token','legacy','checker','check','context','check-runtime','{}',NULL,'now','later',NULL)")
            before = con.execute('SELECT * FROM job_review_grants ORDER BY grant_id').fetchall()
            con.executescript(next(sql for version, _, sql in MIGRATIONS if version == 22))
            after = con.execute('SELECT * FROM job_review_grants ORDER BY grant_id').fetchall()
            self.assertEqual([row[:-1] for row in after], before)
            self.assertEqual([row[-1] for row in after], ['detailed', 'detailed'])
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO job_review_grants VALUES('invalid','invalid-token','legacy','invalid-actor','check','context','invalid-runtime','{}',NULL,'now','later',NULL,'screening')")
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 22)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_routes').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_execution_policies').fetchone()[0], 0)

    def test_unsaved_us_suggestion_does_not_authorize_location_exclusion(self):
        rid = self.start(); grant = self.issue(rid); self.read(grant, 1)
        entry = dict(self.exclusion(), ordinal=1, reason_code='location', brief_revision=1,
                     family='backend', alignment='core', evidence=[{'field': 'description', 'quote': 'Python APIs'}])
        self.assertEqual(self.route(grant, [entry])['results'][0]['status'], 'error')

    def test_interrupted_partial_routes_resume_only_unrouted_items(self):
        rid = self.start(); grant = self.issue(rid)
        self.route(grant, [{'ordinal': 1, 'route': 'detailed'}])
        self.assertEqual(self.authority.renew(grant['grant_id'])['pending_count'], 1)
        self.authority.reconcile_interrupted(rid)
        self.assertEqual(self.authority.pending_routes(rid)['ordinals'], [2])
        self.assertEqual(self.authority.pending_detailed(rid)['ordinals'], [1])
        replacement = self.issue(rid, (2,))
        with self.assertRaises(ReviewAuthorizationError):
            self.route(grant, [{'ordinal': 1, 'route': 'detailed'}])
        with self.assertRaises(ReviewConflictError):
            self.issue(rid, (1,))
        self.assertEqual(self.route(replacement, [{'ordinal': 2, 'route': 'detailed'}])['results'][0]['status'], 'saved')

    def test_scope_and_stale_second_item_roll_back_all_routes(self):
        rid = self.start(); grant = self.issue(rid)
        for second in (1, 999):
            with self.assertRaises(ContractError):
                self.route(grant, [{'ordinal': 1, 'route': 'detailed'}, {'ordinal': second, 'route': 'detailed'}])
        with self.base.ledger() as con:
            con.execute('UPDATE job_review_items SET revision=revision+1 WHERE review_id=? AND ordinal=2', (rid,)); con.commit()
        with self.assertRaises(ReviewConflictError):
            self.route(grant, [{'ordinal': 1, 'route': 'detailed'}, {'ordinal': 2, 'route': 'detailed'}])
        with self.base.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_routes').fetchone()[0], 0)

    def test_policy_purpose_routes_and_profile_cannot_change_silently(self):
        rid = self.start(); grant = self.issue(rid)
        self.assertEqual(self.authority.freeze_execution_policy(rid, self.policy)['policy'], self.policy)
        changed = dict(self.policy, batch_size=10)
        with self.assertRaises(ReviewConflictError):
            self.authority.freeze_execution_policy(rid, changed)
        with self.assertRaises(ContractError):
            self.authority.issue(rid, [1], 'primary', dict(self.policy['screening'], runtime_id='wrongphase'))
        with self.base.ledger() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE job_review_grants SET purpose='detailed' WHERE grant_id=?", (grant['grant_id'],))
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('DELETE FROM job_review_execution_policies WHERE review_id=?', (rid,))
        self.route(grant, [{'ordinal': 1, 'route': 'detailed'}])
        with self.assertRaises(ReviewConflictError):
            self.route(grant, [dict(self.exclusion(), ordinal=1)])
        with self.base.ledger() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('DELETE FROM job_review_routes WHERE review_id=?', (rid,))

    def test_oversize_route_only_defers_and_is_bound_to_snapshot_revision(self):
        rid = self.start()
        receipt = self.authority.route_oversize(rid, 1)
        self.assertEqual(receipt, self.authority.route_oversize(rid, 1))
        self.assertEqual(self.authority.status(rid)['counts'], {'pending': 2})
        self.assertEqual(self.authority.pending_detailed(rid)['ordinals'], [1])
        with self.base.ledger() as con:
            con.execute('UPDATE job_review_items SET revision=revision+1 WHERE review_id=? AND ordinal=1', (rid,)); con.commit()
        self.assertEqual(self.authority.pending_routes(rid)['ordinals'], [1, 2])
        self.assertEqual(self.authority.pending_detailed(rid)['ordinals'], [])

    def test_extra_check_policy_is_benchmark_only_frozen_and_default_compatible(self):
        source = self.start()
        original = self.authority.freeze_execution_policy(source, self.policy)
        self.assertEqual(original, self.authority.freeze_execution_policy(source, dict(self.policy, extra_check_all=False)))
        for value in (True, 1, 'true'):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.authority.freeze_execution_policy(source, dict(self.policy, extra_check_all=value))
        rid = self.authority.start_benchmark({'review_id': source, 'ordinals': [1, 2],
            'label': 'independent full audit', 'idempotency_key': 'full-audit'})['review_id']
        policy = dict(self.policy, extra_check_all=True)
        frozen = self.authority.freeze_execution_policy(rid, policy)
        self.assertEqual(frozen['policy'], policy)
        self.assertEqual(frozen, self.authority.freeze_execution_policy(rid, policy))
        with self.assertRaises(ReviewConflictError):
            self.authority.freeze_execution_policy(rid, self.policy)
        self.issue(rid, (1,))

    def test_screen_assignment_width_does_not_expand_bulk_or_detailed_width(self):
        for n in range(3, 23):
            self.fixture.insert('job-' + str(n))
        rid = self.start(); ordinals = list(range(1, 23))
        with self.assertRaises(ContractError):
            self.issue(rid, ordinals, purpose='detailed')
        grant = self.issue(rid, ordinals)
        assignment = self.authority.scoped_call(grant['token'], 'assignment')
        self.assertEqual(len(project_response('assignment', assignment)['jobs']), 22)
        with self.assertRaises(ContractError):
            self.route(grant, [{'ordinal': n, 'route': 'detailed'} for n in ordinals])
        self.assertEqual(self.authority.pending_routes(rid)['ordinals'], ordinals)

    def test_grant_size_failure_leaves_no_claim_or_grant(self):
        rid = self.start()
        with patch.object(self.authority, '_assignment', return_value={'oversize': 'x' * 60000}):
            with self.assertRaises(ContractError):
                self.issue(rid)
        with self.base.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_grants').fetchone()[0], 0)
            self.assertFalse(con.execute('SELECT 1 FROM job_review_items WHERE claim_owner IS NOT NULL').fetchone())

    def test_routes_transport_roundtrip_is_scoped_and_safe(self):
        rid = self.start(); grant = self.issue(rid)
        socket = self.fixture.root / 'screen.sock'
        with assignment_proxy(socket, self.authority, grant['token']):
            client = ReviewerClient(socket)
            self.assertEqual(client.call('assignment')['purpose'], 'screening')
            result = client.call('routes', {'routes': [{'ordinal': 1, 'route': 'detailed'}]})
        self.assertEqual(result['results'][0]['receipt']['route'], 'detailed')
        self.assertNotIn(grant['token'], json.dumps(result))

    def test_legacy_v1_remains_detailed_and_screening_cannot_relabel_work(self):
        rid = self.start(rubric='job-review-v1', policy=False)
        with self.assertRaises(ContractError):
            self.authority.freeze_execution_policy(rid, self.policy)
        grant = self.issue(rid, (1,), purpose='detailed')
        self.assertEqual(grant['purpose'], 'detailed')
        with self.assertRaises(ReviewConflictError):
            self.authority.freeze_execution_policy(rid, self.policy)


if __name__ == '__main__':
    unittest.main()
