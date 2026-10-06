"""Offline adversarial checks for isolated review HTTP, Unix sockets and stdio MCP."""
import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from job_search.contracts import ContractError
from job_search.job_reviews.reviewer_api import (
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, ReviewerClient, ReviewerTransportError,
    assignment_proxy, decode_json, encode_json, make_api_server, make_assignment_server,
    project_response, safe_socket, validate_arguments,
)
from job_search.job_reviews.reviewer_mcp import ReviewerMCP, serve_stdio

SENTINEL = 'DO-NOT-EXPOSE-RANKING-SENTINEL'
TOKEN = 'test-only-assignment-credential'


class ReviewAuthorizationError(ContractError):
    pass


class ReviewConflictError(ContractError):
    pass


class FakeAuthority:
    def __init__(self):
        self.calls = []
        self.raise_error = None
        self.job = {'title': 'Software Engineer', 'description': 'Build APIs.',
                    'id': 'job-a', 'ats': 'ashby', 'ranking_score': SENTINEL}

    def scoped_call(self, token, operation, args=None):
        if token != TOKEN:
            raise ReviewAuthorizationError(TOKEN + SENTINEL)
        self.calls.append((token, operation, copy.deepcopy(args)))
        if self.raise_error:
            raise self.raise_error(TOKEN + SENTINEL)
        if operation == 'assignment':
            return {'grant_id': 'grant-1', 'kind': 'primary', 'expires_at': '2026-10-04T01:00:00Z',
                    'context_fingerprint': 'a' * 64, 'rubric_version': 'job-review-v1',
                    'jobs': [{'ordinal': 1, 'expected_revision': 0, 'snapshot_sha256': 'b' * 64, 'submitted': True,
                              'job': self.job, 'assessment': SENTINEL}], 'token': TOKEN,
                    'model_score': SENTINEL}
        if operation == 'context':
            section = args.get('section', 'facts')
            sections = {
                'facts': [{'fact_id': 'fact-1', 'source': 'experience',
                           'text': {'company': 'Example', 'role': 'Engineer', 'ranking_score': SENTINEL},
                           'embedding_score': SENTINEL}],
                'preferences': ['Backend roles'],
                'feedback': [{'sequence': 1, 'note': 'Prefer junior roles', 'created_at': '2026-10-01',
                              'model_explanation': SENTINEL}],
            }
            return {'section': section, section: sections[section], 'total': 1, 'next_offset': None,
                    'fingerprint': 'a' * 64, 'profile_revision': 'profile-1', 'resume_versions': [],
                    'previous_review': SENTINEL, 'ranking_score': SENTINEL}
        if operation == 'job':
            return {'ordinal': 1, 'revision': 0, 'snapshot_sha256': 'b' * 64, 'job': self.job,
                    'description': 'Build APIs.', 'offset': 0, 'next_offset': None, 'description_chars': 11,
                    'assessment': SENTINEL, 'previous_assessments': SENTINEL, 'previous_lists': SENTINEL,
                    'application': {'ranking_score': SENTINEL}}
        return {'review_id': 'review-1', 'ordinal': 1, 'revision': 1, 'assessment': SENTINEL}


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def raw_request(path, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(request)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = b''.join(chunks)
    head, body = response.split(b'\r\n\r\n', 1)
    return int(head.split(b' ')[1]), body


def http_request(path, method='GET', route='/v1/assignment', args=None, headers=()):
    body = b'' if args is None else encode_json(args, MAX_REQUEST_BYTES)
    lines = [f'{method} {route} HTTP/1.1', 'Host: reviewer', 'Connection: close']
    if args is not None:
        lines += ['Content-Type: application/json', 'Content-Length: ' + str(len(body))]
    lines.extend(headers)
    return raw_request(path, ('\r\n'.join(lines) + '\r\n\r\n').encode() + body)


class ReviewerAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rv-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'review.sock'
        self.authority = FakeAuthority()

    def test_api_telemetry_is_numeric_and_uses_fixed_operation_labels_only(self):
        events, responses = [], []
        with assignment_proxy(self.path, self.authority, TOKEN, telemetry_callback=events.append):
            responses.append(http_request(self.path))
            responses.append(http_request(self.path, 'POST', '/v1/context', {'section': 'facts'}))
            responses.append(http_request(self.path, 'GET', '/private/' + SENTINEL,
                                          headers=('X-Private: ' + TOKEN,)))
            responses.append(http_request(self.path, 'PATCH', '/' + TOKEN))
            self.authority.raise_error = ContractError
            responses.append(http_request(self.path, 'POST', '/v1/assessment',
                                          {'ordinal': 1, 'assessment': {'private': SENTINEL}}))
        self.assertEqual([event['operation'] for event in events],
                         ['assignment', 'context', 'invalid', 'invalid', 'assessment'])
        self.assertEqual([event['status'] for event in events], [200, 200, 404, 501, 400])
        for event, (_, body) in zip(events, responses):
            self.assertEqual(set(event), {'operation', 'elapsed_seconds', 'status', 'response_bytes'})
            self.assertIs(type(event['elapsed_seconds']), float)
            self.assertGreaterEqual(event['elapsed_seconds'], 0)
            self.assertEqual(event['response_bytes'], len(body))
        encoded = json.dumps(events)
        self.assertNotIn(TOKEN, encoded)
        self.assertNotIn(SENTINEL, encoded)

    def test_api_telemetry_failure_cannot_change_response_or_backend_call(self):
        observed = []
        def fail(event):
            observed.append(event)
            raise RuntimeError(TOKEN + SENTINEL)
        with assignment_proxy(self.path, self.authority, TOKEN, telemetry_callback=fail):
            result = ReviewerClient(self.path).call('assessment', {'ordinal': 1, 'assessment': {}})
            assignment = ReviewerClient(self.path).call('assignment')
        self.assertEqual(result, {'review_id': 'review-1', 'ordinal': 1, 'revision': 1})
        self.assertEqual(assignment['grant_id'], 'grant-1')
        self.assertEqual([event['operation'] for event in observed], ['assessment', 'assignment'])
        self.assertEqual([call[1] for call in self.authority.calls], ['assessment', 'assignment'])

    def test_assignment_preserves_durable_submission_receipt_flag(self):
        with assignment_proxy(self.path, self.authority, TOKEN):
            value = ReviewerClient(self.path).call('assignment')
        self.assertIs(value['jobs'][0]['submitted'], True)

    def test_four_operations_round_trip_and_strip_all_forbidden_fields(self):
        with assignment_proxy(self.path, self.authority, TOKEN):
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            client = ReviewerClient(self.path)
            results = [client.call('assignment'), client.call('job', {'ordinal': 1}),
                       client.call('assessment', {'ordinal': 1, 'assessment': {}})]
            results += [client.call('context', {'section': section})
                        for section in ('facts', 'preferences', 'feedback')]
        encoded = json.dumps(results)
        self.assertNotIn(SENTINEL, encoded)
        self.assertNotIn(TOKEN, encoded)
        self.assertEqual(results[1]['description'], 'Build APIs.')
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.authority.calls), 6)
        self.assertTrue(all(call[0] == TOKEN for call in self.authority.calls))

    def test_api_requires_bearer_and_bound_proxy_rejects_override(self):
        with serving(make_api_server(self.path, self.authority)):
            for headers in ((), ('Authorization: Bearer wrong',),
                            ('Authorization: Bearer ' + TOKEN, 'Authorization: Bearer ' + TOKEN)):
                status, body = http_request(self.path, headers=headers)
                self.assertEqual(status, 401)
                self.assertNotIn(TOKEN.encode(), body)
            status, body = http_request(self.path, headers=('Authorization: Bearer ' + TOKEN,))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['grant_id'], 'grant-1')
        with assignment_proxy(self.path, self.authority, TOKEN):
            self.assertEqual(http_request(self.path, headers=('Authorization: Bearer ' + TOKEN,))[0], 401)

    def test_unknown_routes_and_methods_never_reach_authority(self):
        with assignment_proxy(self.path, self.authority, TOKEN):
            for method, path in [('GET', '/api/v1/session'), ('GET', '/mcp'),
                                 ('GET', '/v1/assignment?token=anything'), ('GET', '/v1/assignment/'),
                                 ('POST', '/v1/assignment'), ('GET', '/v1/context'),
                                 ('POST', '/v1/status'), ('GET', 'http://host/v1/assignment'),
                                 ('CONNECT', 'example.test:443'), ('PUT', '/v1/job')]:
                with self.subTest(method=method, path=path):
                    status, _ = http_request(self.path, method, path)
                    self.assertIn(status, (404, 501))
        self.assertEqual(self.authority.calls, [])

    def test_actor_review_kind_revision_and_command_cannot_be_overridden(self):
        with assignment_proxy(self.path, self.authority, TOKEN):
            for field in ('actor', 'review_id', 'kind', 'expected_revision', 'idempotency_key', 'token'):
                status, _ = http_request(self.path, 'POST', '/v1/assessment',
                                         {'ordinal': 1, 'assessment': {}, field: 'forged'})
                self.assertEqual(status, 400)
        self.assertEqual(self.authority.calls, [])

    def test_invalid_http_frames_are_rejected_without_backend_call(self):
        cases = [
            b'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}',
            b'Content-Length: +2\r\n\r\n{}',
            b'Content-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}',
            b'Content-Length: 2\r\n\r\n{}',  # no JSON content type
            b'Content-Type: application/json\r\n\r\n',  # no content length
            b'Content-Type: application/json\r\nContent-Length: 3\r\n\r\nNaN',
            b'Content-Type: application/json\r\nContent-Length: 2\r\n\r\n[]',
            b'Content-Type: application/json\r\nContent-Length: 999999\r\n\r\n',
        ]
        with assignment_proxy(self.path, self.authority, TOKEN):
            for suffix in cases:
                status, _ = raw_request(self.path, b'POST /v1/context HTTP/1.0\r\n' + suffix)
                self.assertIn(status, (400, 413))
        self.assertEqual(self.authority.calls, [])

    def test_finite_json_duplicate_keys_and_body_bounds(self):
        for raw in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":1,"x":2}',
                    b'[]', b'null', b'{"x":"\xff"}', b' ' * (MAX_REQUEST_BYTES + 1)):
            with self.subTest(raw=raw[:30]), self.assertRaises(ReviewerTransportError):
                decode_json(raw)
        for value in ({'x': float('inf')}, {'x': float('nan')}, {'x': '\ud800'}):
            with self.assertRaises(ReviewerTransportError):
                encode_json(value)
        with self.assertRaises(ReviewerTransportError):
            encode_json({'x': 'a' * MAX_RESPONSE_BYTES})

    def test_errors_never_reflect_source_or_credentials(self):
        with assignment_proxy(self.path, self.authority, TOKEN):
            for error, expected in ((ReviewAuthorizationError, 401), (ReviewConflictError, 409),
                                    (ContractError, 400), (ReviewerTransportError, 400), (RuntimeError, 503)):
                self.authority.raise_error = error
                status, body = http_request(self.path)
                self.assertEqual(status, expected)
                self.assertNotIn(SENTINEL.encode(), body)
                self.assertNotIn(TOKEN.encode(), body)
                with self.assertRaises(ReviewerTransportError) as caught:
                    ReviewerClient(self.path).call('assignment')
                self.assertEqual(caught.exception.status, expected)
                self.assertNotIn(SENTINEL, str(caught.exception))

    def test_launch_barrier_waits_and_fails_closed_without_authority_access(self):
        ready = Mock()
        ready.wait.return_value = False
        with assignment_proxy(self.path, self.authority, TOKEN, ready=ready):
            self.assertEqual(http_request(self.path)[0], 503)
            ready.wait.assert_called_once_with(timeout=10)
            self.assertEqual(self.authority.calls, [])
            ready.wait.return_value = True
            self.assertEqual(http_request(self.path)[0], 200)

    def test_unsafe_sockets_and_collisions_are_rejected(self):
        with self.assertRaises(ReviewerTransportError):
            safe_socket(Path('relative.sock'), exists=False)
        os.chmod(self.root, 0o755)
        with self.assertRaises(ReviewerTransportError):
            make_api_server(self.path, self.authority)
        os.chmod(self.root, 0o700)
        self.path.write_text('not a socket')
        with self.assertRaises(ReviewerTransportError):
            make_api_server(self.path, self.authority)
        self.path.unlink()
        with assignment_proxy(self.path, self.authority, TOKEN):
            with self.assertRaises(ReviewerTransportError):
                make_api_server(self.path, self.authority)
            link = self.root / 'link.sock'
            link.symlink_to(self.path)
            with self.assertRaises(ReviewerTransportError):
                ReviewerClient(link).call('assignment')
            os.chmod(self.path, 0o666)
            with self.assertRaises(ReviewerTransportError):
                ReviewerClient(self.path).call('assignment')

    def test_backend_cannot_hide_objects_in_scalar_job_fields(self):
        self.authority.job['title'] = {'ranking_score': SENTINEL}
        with assignment_proxy(self.path, self.authority, TOKEN):
            status, body = http_request(self.path)
            self.assertEqual(status, 502)
            self.assertNotIn(SENTINEL.encode(), body)

    def test_client_does_not_follow_redirects(self):
        class RedirectHandler:
            def __call__(self, request, *_args):
                request.recv(65536)
                request.sendall(b'HTTP/1.0 302 Found\r\nLocation: http://127.0.0.1/admin\r\nContent-Length: 0\r\n\r\n')
        from job_search.job_reviews.reviewer_api import _UnixServer
        with serving(_UnixServer(self.path, RedirectHandler())):
            with self.assertRaises(ReviewerTransportError) as caught:
                ReviewerClient(self.path).call('assignment')
            self.assertEqual(caught.exception.status, 302)

    def test_argument_validation_blocks_unscoped_operations(self):
        for operation, args in [('list_shortlist', {}), ('job', {'ordinal': True}),
                                ('job', {'ordinal': 0}), ('job', {'ordinal': 1, 'limit': 0}),
                                ('context', {'limit': 21}), ('context', {'section': 'rankings'}),
                                ('assignment', {'review_id': 'other'}),
                                ('assessment', {'ordinal': 1, 'assessment': []})]:
            with self.assertRaises(ReviewerTransportError):
                validate_arguments(operation, args)

    def test_stdio_subprocess_uses_only_bound_assignment_socket(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                        'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'review_assignment', 'arguments': {}}},
        ]
        with assignment_proxy(self.path, self.authority, TOKEN):
            result = subprocess.run([sys.executable, '-m', 'job_search.job_reviews.reviewer_mcp',
                                     '--socket', str(self.path)],
                                    input=b'\n'.join(encode_json(r) for r in requests) + b'\n',
                                    capture_output=True, timeout=10, check=True)
        outputs = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['id'] for r in outputs], [1, 2, 3])
        self.assertEqual(len(outputs[1]['result']['tools']), 5)
        self.assertNotIn(SENTINEL.encode(), result.stdout)
        self.assertNotIn(TOKEN.encode(), result.stdout)
        self.assertEqual(result.stderr, b'')

    def test_stdio_rejects_oversized_or_non_finite_frames_and_stops(self):
        for raw in (b'{"value":NaN}\n', b'x' * (MAX_REQUEST_BYTES + 2), b'{}'):
            output = io.BytesIO()
            status = serve_stdio(self.path, input_stream=io.BytesIO(raw), output_stream=output)
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue())['error']['code'], -32700)

    def test_mcp_exposes_no_resource_prompt_history_or_arbitrary_tool_methods(self):
        client = ReviewerClient(self.path)
        server = ReviewerMCP(client)
        init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {}}}
        self.assertIn('result', server.handle(init))
        for method in ('resources/list', 'resources/read', 'prompts/list', 'sampling/createMessage', 'exec'):
            reply = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': method})
            self.assertEqual(reply['error']['code'], -32601)
        for name in ('list_shortlist', 'review_history', 'review_status', 'get_application_timeline'):
            reply = server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                   'params': {'name': name}})
            self.assertEqual(reply['error']['code'], -32602)
        self.assertIsNone(server.handle({'jsonrpc': '2.0', 'method': 'tools/call',
                                         'params': {'name': 'review_assignment'}}))
        self.assertEqual(self.authority.calls, [])


if __name__ == '__main__':
    unittest.main()
