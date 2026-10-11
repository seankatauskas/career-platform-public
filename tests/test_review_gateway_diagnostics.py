"""Native production gateway diagnostics survive rejection without private payloads."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from job_search.job_reviews.codex_runtime import UnixHTTPConnection
from job_search.job_reviews.model_gateway import gateway_server, GatewayRejected
from job_search.job_reviews import native_protocol as protocol
from job_search.job_reviews.old_method.runtime import diagnostic_log, worker
from job_search.job_reviews.runner_config import RunnerConfig
from tests.test_codex_review_runtime import FakeAuth
from tests.test_review_native_protocol import request, stream


def events(*values):
    return ''.join('data: ' + json.dumps(v) + '\n\n' for v in values).encode()


def upstream(raw):
    response = Mock(status=200, will_close=False, headers={'x-codex-turn-state': 'PRIVATE_HEADER'})
    response.getheader.return_value = 'text/event-stream'
    response.read.side_effect = [raw[:1], raw[1:]]
    connection = Mock()
    connection.getresponse.return_value = response
    return connection


class GatewayDiagnosticTests(unittest.TestCase):
    def test_native_rejections_are_classified_and_persisted_through_real_gateway(self):
        cases = [
            ({'type': 'response.failed', 'response': {'error': {'code': 'server_error', 'message': 'PRIVATE_ERROR'}}},
             'upstream_failed', 'upstream_failed'),
            ({'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'max_output_tokens'}}},
             'incomplete_stream', 'incomplete_stream'),
            ({'type': 'error', 'code': 'rate_limit_exceeded', 'message': 'PRIVATE_ERROR'},
             'upstream_failed', 'upstream_failed'),
            ({'type': 'response.PRIVATE_EVENT', 'PRIVATE_KEY': 'PRIVATE_VALUE'},
             'unsupported_capability', 'unsupported_event'),
            ({'type': 'response.output_item.added', 'output_index': 0,
              'item': {'type': 'compaction', 'id': 'PRIVATE_ID', 'encrypted_content': 'PRIVATE_CONTENT'}},
             'unsupported_capability', 'unsupported_item'),
        ]
        for event, reason, code in cases:
            with self.subTest(reason=reason, code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); metrics = []
                with diagnostic_log(root) as diagnostics, \
                     patch.object(protocol.http.client, 'HTTPSConnection', return_value=upstream(events(event))), \
                     gateway_server(root/'model.sock', FakeAuth(), kind='old_method', native_codex=True,
                                    diagnostic_callback=diagnostics, telemetry_callback=metrics.append):
                    connection = UnixHTTPConnection(root/'model.sock')
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, 400)
                    self.assertEqual(json.loads(reply.read()), {'error': 'review model request rejected'})
                    connection.close()
                records = [json.loads(line) for line in (root/'gateway.jsonl').read_text().splitlines()]
                self.assertEqual([r['event'] for r in records], ['request_started', 'request_finished'])
                start, end = records
                self.assertEqual(start['request_sequence'], end['request_sequence'])
                self.assertLessEqual(start['timestamp'], end['timestamp'])
                self.assertEqual(end['http_status'], 200)
                self.assertEqual(end['outcome'], 'response_rejected')
                self.assertEqual(end['rejection']['reason'], reason)
                self.assertEqual(end['rejection']['validation_code'], code)
                self.assertIn('body_complete', end['timings_ms'])
                self.assertNotIn('PRIVATE', json.dumps(records))
                self.assertEqual(json.loads((root/'gateway-failure.json').read_text()), end)
                self.assertEqual(metrics[-1]['gateway_response_rejected_' + reason + '_count'], 1)
                for name in ('gateway.jsonl', 'gateway-failure.json'):
                    self.assertEqual((root/name).stat().st_mode & 0o777, 0o600)

    def test_success_and_http_or_transport_failure_have_distinct_records(self):
        for response, outcome, expected_status in ((stream(), 'completed', 200),
                                                   (503, 'upstream_http_error', 503),
                                                   (OSError('PRIVATE_ERROR'), 'transport_failed', 503)):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                records = []
                def transport(*args):
                    if isinstance(response, Exception): raise response
                    return (response, b'') if isinstance(response, int) else (200, response)
                with gateway_server(Path(tmp)/'model.sock', FakeAuth(), kind='old_method', native_codex=True,
                                    transport=transport, diagnostic_callback=records.append):
                    connection = UnixHTTPConnection(Path(tmp)/'model.sock')
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, expected_status)
                    reply.read(); connection.close()
                self.assertEqual(records[-1]['outcome'], outcome)
                self.assertNotIn('PRIVATE', json.dumps(records))
                self.assertNotIn('rejection', records[-1])

    def test_diagnostic_failures_cannot_change_response_or_numeric_telemetry(self):
        for raw, status in ((stream(), 200), (events({'type': 'response.failed'}), 400)):
            with tempfile.TemporaryDirectory() as tmp:
                records = Mock(side_effect=OSError('PRIVATE_DISK_ERROR'))
                metrics = []
                with patch.object(protocol, 'schema_diagnostic', side_effect=ValueError('PRIVATE_DIAG_ERROR')), \
                     gateway_server(Path(tmp)/'model.sock', FakeAuth(), kind='old_method', native_codex=True,
                                    transport=lambda *_: (200, raw), diagnostic_callback=records,
                                    telemetry_callback=metrics.append):
                    connection = UnixHTTPConnection(Path(tmp)/'model.sock')
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, status)
                    reply.read(); connection.close()
                self.assertEqual(records.call_count, 2)
                self.assertEqual(metrics[-1]['request_count'], 1)

    def test_failure_tail_and_unknown_types_remain_diagnosable_without_text(self):
        raw = events(*([{'type': 'response.in_progress'}] * 105),
                     {'type': 'response.failed', 'response': {'error': {'code': 'PRIVATE_CODE', 'message': 'PRIVATE_TEXT'}}})
        with self.assertRaises(GatewayRejected) as caught: protocol.completion(raw)
        d = protocol.schema_diagnostic(raw, caught.exception)
        self.assertEqual(len(d['events']), 100)
        self.assertTrue(d['truncated'])
        self.assertEqual(d['events'][-1]['event'], 'response.failed')
        self.assertEqual(d['events'][-1]['error']['code'], 'unknown')
        self.assertNotIn('PRIVATE', json.dumps(d))
        d = protocol.schema_diagnostic(events({'type': 'PRIVATE_TYPE'}), GatewayRejected('unsupported native event'))
        self.assertEqual(len(d['events'][0]['event_sha256']), 64)
        self.assertNotIn('PRIVATE', json.dumps(d))

    def test_exact_rejected_item_is_retained_beyond_diagnostic_event_limit(self):
        raw = events(*([{'type': 'response.in_progress'}] * 105),
                     {'type': 'response.output_item.added', 'output_index': 0,
                      'item': {'type': 'compaction', 'encrypted_content': 'PRIVATE_CONTENT'}})
        with self.assertRaises(GatewayRejected) as caught: protocol.completion(raw)
        d = protocol.schema_diagnostic(raw, caught.exception)
        self.assertEqual(d['rejected_event_index'], 105)
        self.assertEqual(d['rejected_event']['item']['type'], 'compaction')
        self.assertNotIn('PRIVATE', json.dumps(d))

    def test_log_is_bounded_and_keeps_last_failure_after_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with diagnostic_log(root) as record:
                for _ in range(10): record({'padding': 'x' * (1024 * 1024)})
                failure = {'event': 'request_finished', 'outcome': 'transport_failed'}
                record(failure)
            self.assertLess((root/'gateway.jsonl').stat().st_size, 4 * 1024 * 1024 + 100)
            self.assertEqual((root/'gateway.jsonl').read_text().count('log_limit_reached'), 1)
            self.assertEqual(json.loads((root/'gateway-failure.json').read_text()), failure)

    def test_production_worker_wires_diagnostics_outside_worker_mounts(self):
        from job_search.job_reviews.old_method import runtime
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); attempt = root/'attempt'
            config = RunnerConfig(application_config=root/'app.json', state_dir=root/'state',
                runtime_dir=root/'runtime', auth_home=root/'auth', worker_uid=os.getuid(),
                worker_gid=os.getgid(), model_image='example/app@sha256:'+'a'*64)
            process = Mock(); process.poll.return_value = 0; process.wait.return_value = 0
            with patch.object(runtime, 'readiness', return_value={'ready': True}), \
                 patch.object(runtime, 'cleanup_inventory'), \
                 patch.object(runtime, 'NativeAuthOwner', return_value=FakeAuth()), \
                 patch.object(runtime.subprocess, 'run', return_value=Mock(stdout='1')), \
                 patch.object(runtime.subprocess, 'Popen', return_value=process), \
                 patch.object(protocol.http.client, 'HTTPSConnection', return_value=upstream(events({'type': 'response.failed'}))):
                with worker(config, {}, attempt):
                    sock = next((root/'runtime').glob('om_*/model.sock'))
                    connection = UnixHTTPConnection(sock)
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, 400)
                    reply.read(); connection.close()
                failure = json.loads((attempt/'gateway-failure.json').read_text())
                self.assertEqual(failure['rejection']['reason'], 'upstream_failed')
                self.assertFalse((attempt/'output'/'gateway.jsonl').exists())
                self.assertFalse((attempt/'evidence'/'gateway.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
