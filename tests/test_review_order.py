"""Complete ordering keeps existing evidence, scope and provenance gates."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.authority import ReviewAuthorizationError, ReviewConflictError
from job_search.job_reviews.ordering import order_entries
from job_search.job_reviews.reviewer_api import (MAX_REQUEST_BYTES, ReviewerClient, ReviewerTransportError,
    assignment_proxy, encode_json, validate_arguments)
from job_search.job_reviews.reviewer_mcp import serve_stdio, tools_for
from tests import test_review_bulk_tools as fixtures
from tests import test_reviewer_api as transport_fixtures


class CompleteOrderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BulkAuthorityTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.authority = self.fixture.authority
        self.rid = self.fixture.rid
        self.scope = self.fixture.scope
        for kind in ('primary', 'check'):
            self.reviewer = self.fixture.grant(kind)
            self.authority.scoped_call(self.reviewer['token'], 'assessments', self.fixture.args())
        self.final = self.scope.issue(self.rid, (), 'finalizer')
        self.args = {'ordinals': [2, 1], 'groups': [{'id': 'siblings', 'label': 'Related roles', 'ordinals': [1, 2]}]}

    def submit(self, args=None):
        return self.authority.scoped_call(self.final['token'], 'order', self.args if args is None else args)

    def test_atomic_order_roundtrip_preserves_whole_cards_and_provenance(self):
        with self.scope.ledger() as con:
            before = [tuple(row) for row in con.execute('SELECT ordinal,assessment_json,check_json FROM job_review_items ORDER BY ordinal')]
        path = self.scope.fixture.root / 'order.sock'
        with assignment_proxy(path, self.authority, self.final['token']):
            client = ReviewerClient(path)
            result = client.call('order', self.args)
            self.assertTrue(result['complete'])
            self.assertEqual(result, client.call('order', self.args))
        with self.scope.ledger() as con:
            after = [tuple(row) for row in con.execute('SELECT ordinal,assessment_json,check_json FROM job_review_items ORDER BY ordinal')]
            self.assertEqual(before, after)
            self.assertEqual([tuple(row) for row in con.execute('SELECT ordinal,position FROM job_review_calibration_entries ORDER BY position')], [(2, 1), (1, 2)])
        with self.authority._transaction() as con:
            run = self.authority.service._run(con, self.rid)
            self.authority._validate_finalizer_provenance(con, run)
        with self.assertRaises(ContractError):
            self.submit({'ordinals': [1, 2]})

    def test_non_finalizers_incomplete_membership_duplicates_and_groups_cannot_write(self):
        with self.assertRaises(ReviewAuthorizationError):
            self.authority.scoped_call(self.reviewer['token'], 'order', self.args)
        for args in ({'ordinals': [1]}, {'ordinals': [1, 3]}, {'ordinals': [1, 1]},
                     {'ordinals': [True, 2]}, {'ordinals': [1, 2], 'groups': [{'id': 'a', 'label': 'A', 'ordinals': [3]}]},
                     {'ordinals': [1, 2], 'groups': [{'id': 'a', 'label': 'A', 'ordinals': [1, 1]}]}):
            with self.subTest(args=args), self.assertRaises(ContractError):
                self.submit(args)
        self.assertEqual(self.authority.status(self.rid)['calibration']['staged_count'], 0)

    def test_late_seal_failure_rolls_back_entries_and_commands(self):
        with patch.object(self.authority.service, '_finalize', side_effect=ContractError('injected invalid visible card')):
            with self.assertRaises(ContractError):
                self.submit()
        with self.scope.ledger() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_calibration_entries').fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM job_review_commands WHERE operation IN ('calibrate','finalize')").fetchone()[0], 0)
        self.assertTrue(self.submit()['complete'])

    def test_revocation_and_stale_basis_reject_replay(self):
        self.submit()
        with self.scope.ledger() as con:
            con.execute('UPDATE job_review_items SET revision=revision+1 WHERE review_id=? AND ordinal=2', (self.rid,))
            con.commit()
        with self.assertRaises(ReviewConflictError):
            self.submit()
        self.assertFalse(self.authority.status(self.rid)['calibration']['complete'])
        self.authority.revoke(self.final['grant_id'])
        with self.assertRaises(ReviewAuthorizationError):
            self.submit()

    def test_basis_and_status_cost_is_constant_not_per_position(self):
        with patch.object(self.authority.service, '_calibration_basis', wraps=self.authority.service._calibration_basis) as basis, \
                patch.object(self.authority.service, '_status', wraps=self.authority.service._status) as status:
            self.submit()
            self.assertEqual(basis.call_count, 3)
            self.assertEqual(status.call_count, 2)

    def test_failed_complete_order_preserves_previously_staged_work(self):
        self.authority.scoped_call(self.final['token'], 'calibrate', {'ordinal': 1, 'position': 2})
        with patch.object(self.authority.service, '_finalize', side_effect=ContractError('injected seal failure')):
            with self.assertRaises(ContractError):
                self.submit({'ordinals': [2, 1]})
        with self.scope.ledger() as con:
            self.assertEqual([tuple(row) for row in con.execute('SELECT ordinal,position FROM job_review_calibration_entries')], [(1, 2)])
        self.assertTrue(self.submit({'ordinals': [2, 1]})['complete'])

    def test_more_than_twenty_selections_seal_in_one_submission(self):
        for n in range(3, 41):
            self.fixture.fixture.insert('order-' + str(n))
        rid = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
            'rubric_version': 'job-review-v2', 'idempotency_key': 'forty-order'})['review_id']
        for kind in ('primary', 'check'):
            for first in (1, 21):
                ordinals = list(range(first, first + 20))
                grant = self.scope.issue(rid, ordinals, kind)
                for ordinal in ordinals:
                    self.scope.read(grant, ordinal)
                self.authority.scoped_call(grant['token'], 'assessments', {'assessments': [
                    {'ordinal': ordinal, 'assessment': self.fixture.value} for ordinal in ordinals]})
        final = self.scope.issue(rid, (), 'finalizer')
        result = self.authority.scoped_call(final['token'], 'order', {'ordinals': list(range(40, 0, -1))})
        self.assertTrue(result['complete'])
        with self.authority._transaction() as con:
            run = self.authority.service._run(con, rid)
            self.authority._validate_finalizer_provenance(con, run)
        self.assertEqual(self.authority.status(rid)['calibration']['staged_count'], 40)


class OrderContractTests(unittest.TestCase):
    def test_near_limit_order_mcp_envelope_and_oversized_body_recover_over_private_socket(self):
        fixture = transport_fixtures.ReviewerAPITests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        args = {'ordinals': list(range(1, 6001)), 'groups': [
            {'id': str(n), 'label': 'x' * 200, 'ordinals': [n]} for n in range(1, 154)]}
        excess = len(encode_json(args, MAX_REQUEST_BYTES + 4096)) - (MAX_REQUEST_BYTES - 1)
        self.assertTrue(0 < excess < 200)
        args['groups'][-1]['label'] = args['groups'][-1]['label'][:-excess]
        self.assertEqual(len(encode_json(args, MAX_REQUEST_BYTES + 4096)), MAX_REQUEST_BYTES - 1)
        validate_arguments('order', args)
        oversized = copy.deepcopy(args)
        oversized['groups'][-1]['label'] += 'xx'
        # Both bodies obey the order schema; only the second exceeds the byte cap.
        order_entries(oversized)
        self.assertEqual(len(encode_json(oversized, MAX_REQUEST_BYTES + 4096)), MAX_REQUEST_BYTES + 1)
        received = []
        original = fixture.authority.scoped_call

        def finalizer(token, operation, arguments=None):
            result = original(token, operation, arguments)
            if operation == 'assignment':
                result.update(kind='finalizer', jobs=[], calibration={})
            elif operation == 'order':
                received.append(copy.deepcopy(arguments))
                result = {'complete': True, 'selected_count': 6000, 'staged_count': 6000}
            return result

        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {}}}]
        for request_id, arguments in ((2, args), (3, oversized), (4, args)):
            requests.append({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                             'params': {'name': 'review_order', 'arguments': arguments}})
        requests.append({'jsonrpc': '2.0', 'id': 5, 'method': 'ping'})
        frames = [encode_json(request, MAX_REQUEST_BYTES + 4096) + b'\n' for request in requests]
        for frame in frames[1:4]:
            self.assertGreater(len(frame), MAX_REQUEST_BYTES)
            self.assertLessEqual(len(frame), MAX_REQUEST_BYTES + 4096)
        output = io.BytesIO()
        with patch.object(fixture.authority, 'scoped_call', side_effect=finalizer), \
                assignment_proxy(fixture.path, fixture.authority, transport_fixtures.TOKEN):
            self.assertEqual(serve_stdio(fixture.path, input_stream=io.BytesIO(b''.join(frames)),
                                        output_stream=output), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([response['id'] for response in responses], [1, 2, 3, 4, 5])
        self.assertEqual([response['result']['isError'] for response in responses[1:4]],
                         [False, True, False])
        self.assertEqual(responses[-1]['result'], {})
        self.assertEqual(received, [args, args])

    def test_six_thousand_positions_fit_transport_without_dropping_items(self):
        args = {'ordinals': list(range(6000, 0, -1))}
        validate_arguments('order', args)
        entries = order_entries(args)
        self.assertEqual(len(entries), 6000)
        self.assertEqual(entries[-1], {'ordinal': 1, 'position': 6000, 'related_group': None})
        with self.assertRaises(ReviewerTransportError):
            validate_arguments('order', {'ordinals': list(range(1, 6002))})
        oversized = copy.deepcopy(args)
        oversized['groups'] = [{'id': str(n), 'label': 'x' * 200, 'ordinals': [n]} for n in range(1, 1000)]
        with self.assertRaises(ReviewerTransportError):
            validate_arguments('order', oversized)

    def test_tool_is_available_only_to_finalizers(self):
        self.assertIn('review_order', {tool['name'] for tool in tools_for('finalizer', 'job-review-v2')})
        for kind in ('primary', 'check'):
            self.assertNotIn('review_order', {tool['name'] for tool in tools_for(kind, 'job-review-v2')})


if __name__ == '__main__':
    unittest.main()
