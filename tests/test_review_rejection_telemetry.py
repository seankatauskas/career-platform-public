"""Fixed response failure counters never preserve untrusted diagnostic text."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from job_search.job_reviews.codex_runtime import UnixHTTPConnection
from job_search.job_reviews.model_gateway import (
    GatewayRejected, RESPONSE_REJECTION_COUNTERS, RESPONSE_REJECTION_REASONS,
    gateway_server, subscription_transport,
)
from tests.test_codex_review_runtime import FakeAuth, done_stream, request, response, stream


class ResponseRejectionTelemetryTests(unittest.TestCase):
    def test_real_gateway_response_categories_preserve_rejection_and_attempt_counts(self):
        call = {'type': 'function_call', 'name': 'mcp__review__review_job',
                'arguments': '{}', 'id': 'fc1', 'call_id': 'call1'}
        duplicate = done_stream([call, call])
        sparse = done_stream([call])
        sparse[0]['output_index'] = 1
        mismatched = done_stream([call], completed_output=[dict(call, arguments='{"ordinal":2}')])
        cases = [
            ('upstream_failed', stream([{'type': 'response.failed', 'response': {
                'status': 'failed', 'error': {'message': 'PRIVATE_PAYLOAD', 'code': 'PRIVATE_CODE'}}}])),
            ('incomplete_stream', stream([{'type': 'response.incomplete', 'response': {
                'status': 'incomplete', 'incomplete_details': {'reason': 'PRIVATE_DETAIL'}}}])),
            ('incomplete_stream', stream([{'type': 'response.in_progress'}])),
            ('incomplete_stream', stream(done_stream([dict(call, status='in_progress')]))),
            ('framing', b'PRIVATE_NOT_SSE\n\n'),
            ('framing', response()[:-1]),
            ('output_consistency', stream(duplicate)),
            ('output_consistency', stream(sparse)),
            ('output_consistency', stream(mismatched)),
            ('unsupported_tool', response('PRIVATE_UNAPPROVED_TOOL')),
            ('unsupported_capability', stream(done_stream([{'type': 'PRIVATE_CAPABILITY'}]))),
            ('unsupported_capability', stream([{'type': 'response.PRIVATE_NEW_EVENT'}])),
            ('size', b'x' * 2049),
            ('validation', b'data: {PRIVATE_INVALID_JSON}\n\n'),
            ('validation', GatewayRejected('PRIVATE_EXCEPTION')),
            ('validation', ValueError('PRIVATE_VALUE_ERROR')),
        ]
        events, received = [], []

        def transport(*_args):
            value = received.pop(0)
            if isinstance(value, Exception):
                raise value
            return 200, value

        auth = FakeAuth()
        with tempfile.TemporaryDirectory() as tmp, \
                patch('job_search.job_reviews.model_gateway.MAX_RESPONSE', 2048), \
                gateway_server(Path(tmp) / 'model.sock', auth, transport=transport,
                               telemetry_callback=events.append):
            for reason, body in cases:
                with self.subTest(reason=reason, body_type=type(body).__name__):
                    events.clear()
                    received.append(body)
                    connection = UnixHTTPConnection(Path(tmp) / 'model.sock')
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, 400)
                    self.assertNotIn(b'PRIVATE', reply.read())
                    connection.close()
                    self.assertEqual(len(events), 2)
                    self.assertEqual(events[0], {'request_started_count': 1})
                    self.assertEqual(events[1]['request_count'], 1)
                    self.assertEqual(events[1]['gateway_response_rejected_count'], 1)
                    counters = {key: value for key, value in events[1].items()
                                if key in RESPONSE_REJECTION_COUNTERS}
                    self.assertEqual(counters, {'gateway_response_rejected_' + reason + '_count': 1})
                    self.assertTrue(all(type(value) in (int, float) for value in events[1].values()))
                    self.assertNotIn('PRIVATE', json.dumps(events))
                    self.assertEqual(received, [])
        self.assertEqual(auth.refreshes, [False] * len(cases))

    def test_rejection_reason_is_allowlisted_and_native_conversion_is_counted_once(self):
        for raw_reason in ('PRIVATE_REASON', [], None):
            error = GatewayRejected('PRIVATE_EXCEPTION', reason=raw_reason)
            self.assertEqual(error.reason, 'validation')
        error = GatewayRejected('PRIVATE_EXCEPTION', reason='size')
        error.reason = 'PRIVATE_MUTATED_REASON'
        for failure, expected in ((error, 'validation'),
                                  (GatewayRejected('PRIVATE_NATIVE', reason='output_consistency'), 'output_consistency')):
            events = []
            transport = Mock(return_value=(200, response()))
            with tempfile.TemporaryDirectory() as tmp, \
                    patch('job_search.job_reviews.model_gateway.native_response', side_effect=failure), \
                    gateway_server(Path(tmp) / 'model.sock', FakeAuth(), transport=transport,
                                   telemetry_callback=events.append):
                connection = UnixHTTPConnection(Path(tmp) / 'model.sock')
                connection.request('POST', '/v1/responses', json.dumps(request()))
                reply = connection.getresponse()
                self.assertEqual(reply.status, 400)
                reply.read(); connection.close()
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(sum(e.get('gateway_response_rejected_count', 0) for e in events), 1)
            self.assertEqual(sum(e.get('gateway_response_rejected_' + expected + '_count', 0) for e in events), 1)
            self.assertNotIn('PRIVATE', json.dumps(events))

    def test_transport_header_and_size_reasons_do_not_expose_upstream_values(self):
        connection = Mock()
        upstream = connection.getresponse.return_value
        upstream.status = 200
        for header, data, reason in (('PRIVATE_CONTENT_TYPE', response(), 'framing'),
                                     (None, b'x' * 2049, 'size')):
            upstream.getheader.return_value = header
            upstream.read.return_value = data
            with patch('job_search.job_reviews.model_gateway.MAX_RESPONSE', 2048), \
                    patch('job_search.job_reviews.model_gateway.http.client.HTTPSConnection', return_value=connection), \
                    self.assertRaises(GatewayRejected) as caught:
                subscription_transport(b'{}', {})
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(set(RESPONSE_REJECTION_COUNTERS), {
            'gateway_response_rejected_' + reason + '_count' for reason in RESPONSE_REJECTION_REASONS})


if __name__ == '__main__':
    unittest.main()
