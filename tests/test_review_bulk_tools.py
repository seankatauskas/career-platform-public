"""Bulk tools retain scoped provenance, item validation and bounded transport."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.authority import ReviewAuthorizationError, ReviewConflictError
from job_search.job_reviews.reviewer_api import (MAX_BULK_REQUEST_BYTES, MAX_REQUEST_BYTES,
    ReviewerClient, ReviewerTransportError, assignment_proxy, encode_json,
    project_response, validate_arguments)
from job_search.job_reviews.reviewer_mcp import serve_stdio, tools_for
from tests import test_review_authority as authority_fixtures
from tests import test_reviewer_api as transport_fixtures
from tests.test_reviewer_api import FakeAuthority, TOKEN, SENTINEL


class BulkAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.scope = authority_fixtures.AuthorityTests('runTest')
        self.scope.setUp()
        self.addCleanup(self.scope.doCleanups)
        self.fixture, self.authority = self.scope.fixture, self.scope.authority
        self.fixture.insert('b')
        self.rid = self.scope.start(rubric_version='job-review-v2')
        self.value = self.fixture.assessment(eligibility='no_known_barrier',
            eligibility_condition='', next_step='apply', category='core')

    def grant(self, kind='primary'):
        grant = self.scope.issue(self.rid, (1, 2), kind)
        for ordinal in (1, 2):
            self.scope.read(grant, ordinal)
        return grant

    def args(self):
        return {'assessments': [{'ordinal': ordinal, 'assessment': copy.deepcopy(self.value)}
                                for ordinal in (1, 2)]}

    def test_partial_validation_preserves_valid_cards_and_exact_retries(self):
        grant, args = self.grant(), self.args()
        args['assessments'][1]['assessment']['evidence'][0]['quote'] = 'not in description'
        result = self.authority.scoped_call(grant['token'], 'assessments', args)
        self.assertEqual([v['status'] for v in result['results']], ['saved', 'error'])
        self.assertEqual(result['results'][1], {'ordinal': 2, 'status': 'error', 'error': 'validation_failed'})
        self.assertEqual(self.authority.scoped_call(grant['token'], 'assessments', args), result)
        self.assertEqual(self.authority.status(self.rid)['counts'], {'close': 1, 'pending': 1})
        args['assessments'][1]['assessment'] = self.value
        fixed = self.authority.scoped_call(grant['token'], 'assessments', args)
        self.assertEqual([v['status'] for v in fixed['results']], ['saved', 'saved'])
        with self.scope.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_revisions').fetchone()[0], 2)
        checker = self.grant('check')
        self.authority.scoped_call(checker['token'], 'assessments', args)
        self.assertEqual(self.authority.status(self.rid)['audit_remaining_count'], 0)

    def test_out_of_scope_and_duplicate_ordinals_never_save_first_item(self):
        grant = self.grant()
        for ordinal in (3, 1):
            args = self.args()
            args['assessments'][1]['ordinal'] = ordinal
            with self.assertRaises(ContractError):
                self.authority.scoped_call(grant['token'], 'assessments', args)
            self.assertEqual(self.authority.status(self.rid)['counts'], {'pending': 2})

    def test_late_authorization_conflict_rolls_back_entire_batch(self):
        grant = self.grant()
        original = self.authority._assess
        def assess(con, owner, args, principal):
            if args['ordinal'] == 2:
                raise ReviewConflictError('injected stale scope')
            return original(con, owner, args, principal)
        with patch.object(self.authority, '_assess', side_effect=assess):
            with self.assertRaises(ReviewConflictError):
                self.authority.scoped_call(grant['token'], 'assessments', self.args())
        self.assertEqual(self.authority.status(self.rid)['counts'], {'pending': 2})

    def test_stale_second_snapshot_rejects_before_any_submission(self):
        grant = self.grant()
        with self.scope.ledger() as con:
            con.execute('UPDATE job_review_items SET revision=revision+1 WHERE review_id=? AND ordinal=2', (self.rid,))
            con.commit()
        with self.assertRaises(ReviewConflictError):
            self.authority.scoped_call(grant['token'], 'assessments', self.args())
        self.assertEqual(self.authority.status(self.rid)['counts'], {'pending': 2})

    def test_bulk_shape_and_authority_byte_limits_are_enforced(self):
        grant = self.grant()
        for entries in ([], self.args()['assessments'] * 11):
            with self.assertRaises(ContractError):
                self.authority.scoped_call(grant['token'], 'assessments', {'assessments': entries})
        args = self.args()
        args['assessments'][0]['assessment']['explanation'] = 'x' * MAX_BULK_REQUEST_BYTES
        with self.assertRaises(ContractError):
            self.authority.scoped_call(grant['token'], 'assessments', args)
        self.assertEqual(self.authority.status(self.rid)['counts'], {'pending': 2})

    def test_full_description_still_required_and_prelaunch_is_blocked(self):
        grant = self.scope.issue(self.rid, (1, 2), launched=False)
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(grant['token'], 'assessments', self.args())
        self.authority.mark_launch(grant['grant_id'], 'runtime-1', self.scope.launch_receipt('runtime-1'))
        result = self.authority.scoped_call(grant['token'], 'assessments', self.args())
        self.assertEqual([v['status'] for v in result['results']], ['error', 'error'])
        self.assertEqual(self.authority.status(self.rid)['counts'], {'pending': 2})

    def test_finalizer_bulk_scope_partial_validation_replay_and_seal(self):
        for kind in ('primary', 'check'):
            reviewer = self.grant(kind)
            self.authority.scoped_call(reviewer['token'], 'assessments', self.args())
        final = self.scope.issue(self.rid, (), 'finalizer')
        args = {'calibrations': [{'ordinal': 1, 'position': 1}, {'ordinal': 2, 'position': 3}]}
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(reviewer['token'], 'calibrations', args)
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(final['token'], 'assessments', self.args())
        out_of_scope = copy.deepcopy(args)
        out_of_scope['calibrations'][1]['ordinal'] = 3
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(final['token'], 'calibrations', out_of_scope)
        self.assertEqual(self.authority.status(self.rid)['calibration']['staged_count'], 0)
        with patch.object(self.fixture.service, '_calibration_basis', wraps=self.fixture.service._calibration_basis) as basis, \
                patch.object(self.fixture.service, '_status', wraps=self.fixture.service._status) as status:
            result = self.authority.scoped_call(final['token'], 'calibrations', args)
            self.assertEqual(basis.call_count, 1)
            self.assertEqual(status.call_count, 1)
        self.assertEqual([v['status'] for v in result['results']], ['saved', 'error'])
        self.assertEqual(self.authority.scoped_call(final['token'], 'calibrations', args), result)
        args['calibrations'][1]['position'] = 2
        fixed = self.authority.scoped_call(final['token'], 'calibrations', args)
        self.assertEqual([v['status'] for v in fixed['results']], ['saved', 'saved'])
        self.assertTrue(self.authority.scoped_call(final['token'], 'finalize')['complete'])

    def test_finalizer_bulk_revalidates_each_transaction_and_rolls_back_stale_basis(self):
        for kind in ('primary', 'check'):
            self.authority.scoped_call(self.grant(kind)['token'], 'assessments', self.args())
        final = self.scope.issue(self.rid, (), 'finalizer')
        args = {'calibrations': [{'ordinal': 1, 'position': 1}, {'ordinal': 2, 'position': 2}]}
        with patch.object(self.fixture.service, '_calibration_basis', wraps=self.fixture.service._calibration_basis) as basis:
            first = self.authority.scoped_call(final['token'], 'calibrations', args)
            self.assertEqual(basis.call_count, 1)
            self.assertEqual(first, self.authority.scoped_call(final['token'], 'calibrations', args))
            self.assertEqual(basis.call_count, 2)
        with self.scope.ledger() as con:
            con.execute('UPDATE job_review_items SET revision=revision+1 WHERE review_id=? AND ordinal=2', (self.rid,))
            con.commit()
        with self.assertRaises(ReviewConflictError):
            self.authority.scoped_call(final['token'], 'calibrations', args)
        # Prior receipts survive, but they cannot validate the changed basis.
        self.assertEqual(self.authority.status(self.rid)['calibration']['staged_count'], 0)
        self.assertFalse(self.authority.status(self.rid)['calibration']['complete'])

    def test_twenty_calibrations_compute_basis_and_status_only_once(self):
        for number in range(3, 21):
            self.fixture.insert('job-' + str(number))
        rid = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
            'rubric_version': 'job-review-v2', 'idempotency_key': 'twenty-calibrations'})['review_id']
        ordinals = list(range(1, 21))
        for kind in ('primary', 'check'):
            grant = self.scope.issue(rid, ordinals, kind)
            for ordinal in ordinals:
                self.scope.read(grant, ordinal)
            self.authority.scoped_call(grant['token'], 'assessments', {'assessments': [
                {'ordinal': ordinal, 'assessment': self.value} for ordinal in ordinals]})
        final = self.scope.issue(rid, (), 'finalizer')
        with patch.object(self.fixture.service, '_calibration_basis', wraps=self.fixture.service._calibration_basis) as basis, \
                patch.object(self.fixture.service, '_status', wraps=self.fixture.service._status) as status:
            result = self.authority.scoped_call(final['token'], 'calibrations', {'calibrations': [
                {'ordinal': ordinal, 'position': ordinal} for ordinal in ordinals]})
            self.assertEqual(basis.call_count, 1)
            self.assertEqual(status.call_count, 1)
        self.assertEqual([item['status'] for item in result['results']], ['saved'] * 20)
        self.assertTrue(self.authority.scoped_call(final['token'], 'finalize')['complete'])

    def test_finalizer_bulk_late_scope_failure_rolls_back_positions_and_receipts(self):
        for kind in ('primary', 'check'):
            self.authority.scoped_call(self.grant(kind)['token'], 'assessments', self.args())
        final = self.scope.issue(self.rid, (), 'finalizer')
        args = {'calibrations': [{'ordinal': 1, 'position': 1}, {'ordinal': 2, 'position': 2}]}
        original = self.authority._finalizer_call
        def call(con, grant, operation, entry, **kwargs):
            if entry['ordinal'] == 2:
                raise ReviewAuthorizationError('injected lost scope')
            return original(con, grant, operation, entry, **kwargs)
        with patch.object(self.authority, '_finalizer_call', side_effect=call):
            with self.assertRaises(ReviewAuthorizationError):
                self.authority.scoped_call(final['token'], 'calibrations', args)
        with self.scope.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_calibration_entries').fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM job_review_commands WHERE operation='calibrate'").fetchone()[0], 0)
        result = self.authority.scoped_call(final['token'], 'calibrations', args)
        self.assertEqual([item['status'] for item in result['results']], ['saved', 'saved'])

    def test_bulk_calibration_cannot_bypass_global_position_uniqueness(self):
        for kind in ('primary', 'check'):
            grant = self.grant(kind)
            self.authority.scoped_call(grant['token'], 'assessments', self.args())
        final = self.scope.issue(self.rid, (), 'finalizer')
        self.authority.scoped_call(final['token'], 'calibrations', {'calibrations': [
            {'ordinal': 1, 'position': 1}, {'ordinal': 2, 'position': 1}]})
        with self.assertRaisesRegex(ContractError, 'unique complete order'):
            self.authority.scoped_call(final['token'], 'finalize')
        self.assertFalse(self.authority.status(self.rid)['calibration']['complete'])


class BulkTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = transport_fixtures.ReviewerAPITests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path
        self.authority = FakeAuthority()
        self.original = self.authority.scoped_call
        def call(token, operation, args=None):
            if operation in ('assessments', 'calibrations'):
                return {'results': [{'ordinal': item['ordinal'], 'status': 'saved',
                    'receipt': {'review_id': 'review-1', 'ordinal': item['ordinal'], 'revision': 1,
                                'forbidden': SENTINEL}, 'forbidden': SENTINEL} for item in args[operation]]}
            return self.original(token, operation, args)
        self.authority.scoped_call = call
        self.args = {'assessments': [{'ordinal': n, 'assessment': {'explanation': 'x' * 3500}}
                                     for n in range(1, 21)]}

    def test_bulk_body_over_64k_roundtrips_but_single_and_over_256k_reject(self):
        self.assertGreater(len(encode_json(self.args, MAX_BULK_REQUEST_BYTES)), MAX_REQUEST_BYTES)
        with assignment_proxy(self.path, self.authority, TOKEN):
            result = ReviewerClient(self.path).call('assessments', self.args)
        self.assertEqual(len(result['results']), 20)
        self.assertNotIn(SENTINEL, json.dumps(result))
        with self.assertRaises(ReviewerTransportError):
            validate_arguments('assessment', {'ordinal': 1, 'assessment': {'x': 'x' * MAX_REQUEST_BYTES}})
        large = copy.deepcopy(self.args)
        for item in large['assessments']:
            item['assessment']['explanation'] *= 4
        with self.assertRaises(ReviewerTransportError):
            validate_arguments('assessments', large)

    def test_bulk_stdio_frame_and_scoped_tool_schemas(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': 'review_assessments', 'arguments': self.args}}]
        source = io.BytesIO(b'\n'.join(encode_json(r, MAX_BULK_REQUEST_BYTES + 4096) for r in requests) + b'\n')
        output = io.BytesIO()
        with assignment_proxy(self.path, self.authority, TOKEN):
            self.assertEqual(serve_stdio(self.path, input_stream=source, output_stream=output), 0)
        response = json.loads(output.getvalue().splitlines()[-1])['result']
        self.assertFalse(response['isError'])
        self.assertEqual(len(json.loads(response['content'][0]['text'])['results']), 20)
        for rubric in ('job-review-v1', 'job-review-v2'):
            primary = {t['name']: t for t in tools_for('primary', rubric)}
            self.assertIn('review_assessments', primary)
            self.assertNotIn('review_calibrations', primary)
            schema = primary['review_assessments']['inputSchema']['properties']['assessments']['items']
            self.assertEqual(schema, primary['review_assessment']['inputSchema'])
        final = {t['name'] for t in tools_for('finalizer', 'job-review-v2')}
        self.assertIn('review_calibrations', final)
        self.assertNotIn('review_assessments', final)

    def test_response_projection_allows_only_typed_safe_item_errors(self):
        result = project_response('assessments', {'results': [
            {'ordinal': 1, 'status': 'error', 'error': 'validation_failed', 'detail': SENTINEL}]})
        self.assertNotIn(SENTINEL, json.dumps(result))
        with self.assertRaises(ReviewerTransportError):
            project_response('assessments', {'results': [{'ordinal': 1, 'status': 'error', 'error': SENTINEL}]})


if __name__ == '__main__':
    unittest.main()
