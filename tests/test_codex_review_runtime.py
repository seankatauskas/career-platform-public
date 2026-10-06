"""Offline adversarial checks for the independent reviewer execution boundary."""
import base64
import copy
from contextlib import contextmanager
import http.client
import json
import io
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from job_search.job_reviews.auth_owner import NativeAuthOwner, AuthenticationUnavailable
from job_search.job_reviews.codex_runtime import RuntimeConfig, UnixHTTPConnection, docker_command, codex_command, readiness, WorkerHandle, recover_workers
from job_search.job_reviews.codex_runtime import preload_evidence, review_prompt, PreloadTooLarge, main as worker_main
from job_search.job_reviews.contracts import fingerprint
from job_search.job_reviews.model_gateway import (GatewayRejected, REVIEW_TOOLS, gateway_server,
                                                 validate_request, validate_response, native_response,
                                                 subscription_transport)


def request():
    return {'model': 'gpt-6-astra', 'reasoning': {'effort': 'high'},
            'instructions': 'Assess source evidence.', 'input': [{'role': 'user', 'content': 'A job.'}],
            'tools': [], 'stream': True, 'store': False}


def response(tool=None):
    item = {'type': 'function_call', 'name': tool, 'arguments': '{}', 'call_id': 'c1'} if tool else {
        'type': 'message', 'id': 'msg1', 'role': 'assistant', 'status': 'completed',
        'content': [{'type': 'output_text', 'text': 'Complete.', 'annotations': []}]}
    return ('data: ' + json.dumps({'type': 'response.completed', 'response': {
        'id': 'resp1', 'object': 'response', 'status': 'completed', 'output': [item],
        'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}) + '\n\n').encode()


def stream(events):
    return ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n'
                   for event in events).encode()


def done_stream(items, *, completed_output=None):
    events = [{'type': 'response.output_item.done', 'output_index': index, 'item': item,
               'sequence_number': index} for index, item in enumerate(items)]
    events.append({'type': 'response.completed', 'sequence_number': len(items),
                   'response': {'id': 'resp1', 'status': 'completed',
                                'output': completed_output or []}})
    return events


class FakeAuth:
    def __init__(self):
        self.refreshes = []

    def _request_headers(self, *, refresh=False):
        self.refreshes.append(refresh)
        return {'Authorization': 'Bearer SYNTHETIC_TEST_ONLY'}


class ReviewRuntimeTests(unittest.TestCase):
    def preload_client(self, kind='check', description=None):
        description = description or ('Complete original evidence. ' * 6000 + 'FINAL SOURCE SENTINEL')
        job = {'ats': 'ashby', 'id': 'fixture', 'title': 'Engineer', 'description': description}
        slot = {'ordinal': 7, 'expected_revision': 2, 'snapshot_sha256': fingerprint(job),
                'submitted': False, 'job': {k: v for k, v in job.items() if k != 'description'}}
        assignment = {'kind': kind, 'grant_id': 'synthetic', 'context_fingerprint': 'context-fixed',
                      'rubric_version': 'job-review-v2', 'jobs': [] if kind == 'finalizer' else [slot]}
        if kind == 'finalizer':
            assignment['calibration'] = {'basis_sha256': 'b' * 64, 'selected_count': 2,
                                         'staged_count': 0, 'complete': False}
        values = {'facts': [{'fact_id': 'one', 'source': 'projects', 'text': 'Built a system'},
                            {'fact_id': 'two', 'source': 'experience', 'text': 'Shipped APIs'}],
                  'preferences': ['Software building'], 'feedback': []}
        def call(operation, args):
            if operation == 'assignment':
                return copy.deepcopy(assignment)
            if operation == 'context':
                section, offset = args['section'], args['offset']
                chunk = values[section][offset:offset + 1]
                return {'fingerprint': 'context-fixed', 'rubric_version': 'job-review-v2',
                        'section': section, section: copy.deepcopy(chunk), 'total': len(values[section]),
                        'next_offset': offset + 1 if offset + 1 < len(values[section]) else None}
            if operation == 'job':
                offset = args['offset']; part = description[offset:offset + args['limit']]
                return {'ordinal': 7, 'revision': 2, 'snapshot_sha256': slot['snapshot_sha256'],
                        'job': copy.deepcopy(slot['job']), 'description': part, 'offset': offset,
                        'description_chars': len(description),
                        'next_offset': offset + len(part) if offset + len(part) < len(description) else None}
            if operation == 'calibration':
                ordinal = 7 if args['after'] == 0 else 12
                return dict(assignment['calibration'], items=[{'ordinal': ordinal, 'assessment': {'decision': 'close'}}],
                            next_after=7 if ordinal == 7 else None)
            raise AssertionError('unexpected bootstrap operation')
        return Mock(call=Mock(side_effect=call))

    def test_preload_reads_complete_original_evidence_and_hashes_before_stdin(self):
        client = self.preload_client()
        packet = preload_evidence(client)
        self.assertEqual(packet['assignment']['kind'], 'check')
        self.assertEqual(len(packet['context']['facts']), 2)
        self.assertTrue(packet['jobs'][0]['job']['description'].endswith('FINAL SOURCE SENTINEL'))
        self.assertGreater(len(review_prompt(packet).encode()), 128 * 1024)
        self.assertEqual(codex_command(1234)[-1], '-')
        self.assertLess(sum(map(len, codex_command(1234))), 10000)
        self.assertEqual({c.args[0] for c in client.call.call_args_list}, {'assignment', 'context', 'job'})
        self.assertNotIn('assessment', packet['jobs'][0])
        self.assertNotIn('calibration', packet)
        server = Mock()
        with patch('job_search.job_reviews.codex_runtime.bridge_server', return_value=server), \
             patch('job_search.job_reviews.codex_runtime.Path.mkdir'), \
             patch('job_search.job_reviews.codex_runtime.preload_evidence', return_value=packet), \
             patch('job_search.job_reviews.codex_runtime.subprocess.run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(worker_main([]), 0)
        self.assertEqual(run.call_args.args[0][-1], '-')
        self.assertEqual(run.call_args.kwargs['input'], review_prompt(packet))
        self.assertTrue(run.call_args.kwargs['text'])

    def test_preload_finalizer_reads_all_calibration_without_job_access(self):
        client = self.preload_client('finalizer')
        packet = preload_evidence(client)
        self.assertEqual([x['ordinal'] for x in packet['calibration']['items']], [7, 12])
        self.assertEqual(packet['jobs'], [])
        self.assertNotIn('job', {c.args[0] for c in client.call.call_args_list})

    def test_prompt_reuses_complete_context_prefix_without_changing_packet(self):
        first = preload_evidence(self.preload_client(description='Complete description including its final sentence.'))
        first['context']['facts'][0]['text'] = 'Shared approved fact. ' * 2000
        second = copy.deepcopy(first)
        second['assignment']['grant_id'] = 'another-grant'
        second['assignment']['jobs'][0]['ordinal'] = 9
        second['jobs'][0]['ordinal'] = 9
        second['packet_sha256'] = 'b' * 64
        originals = copy.deepcopy([first, second])
        prompts = [review_prompt(packet) for packet in (first, second)]
        context_json = json.dumps(first['context'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.assertIn(context_json, os.path.commonprefix(prompts))
        for packet, prompt in zip(originals, prompts):
            rendered = prompt.split('BEGIN UNTRUSTED EVIDENCE JSON\n', 1)[1].rsplit('\nEND UNTRUSTED EVIDENCE JSON', 1)[0]
            self.assertTrue(rendered.startswith('{"version":1,"context":'))
            self.assertEqual(json.loads(rendered), packet)
            self.assertEqual(len(rendered.encode()), len(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()))
        self.assertEqual([first, second], originals)

    def test_preload_allows_renewed_expiry_but_rejects_changed_assignment_revision(self):
        for changed_revision in (False, True):
            with self.subTest(changed_revision=changed_revision):
                client = self.preload_client(description='complete original evidence')
                original = client.call.side_effect
                assignment_reads = []
                def renew(operation, args):
                    value = original(operation, args)
                    if operation == 'assignment':
                        assignment_reads.append(1)
                        value['expires_at'] = ('2026-10-05T12:00:00Z' if len(assignment_reads) == 1
                                               else '2026-10-05T12:01:00Z')
                        if changed_revision and len(assignment_reads) > 1:
                            value['jobs'][0]['expected_revision'] += 1
                    return value
                client.call.side_effect = renew
                if changed_revision:
                    with self.assertRaisesRegex(ValueError, 'assignment changed'):
                        preload_evidence(client)
                else:
                    packet = preload_evidence(client)
                    self.assertEqual(packet['jobs'][0]['job']['description'], 'complete original evidence')
                self.assertEqual(len(assignment_reads), 2)

    def test_preload_rejects_changed_sources_and_missing_pages(self):
        for corruption in ('snapshot_sha256', 'description', 'fingerprint', 'next_offset', 'basis_sha256'):
            with self.subTest(corruption=corruption):
                client = self.preload_client('finalizer' if corruption == 'basis_sha256' else 'check', 'complete')
                original = client.call.side_effect
                def corrupt(operation, args):
                    value = original(operation, args)
                    if operation == 'job' and corruption in ('snapshot_sha256', 'description'):
                        value[corruption] = 'altered'
                    if operation == 'context' and corruption in ('fingerprint', 'next_offset'):
                        value[corruption] = 'changed' if corruption == 'fingerprint' else None
                    if operation == 'calibration' and corruption == 'basis_sha256':
                        value[corruption] = 'changed'
                    return value
                client.call.side_effect = corrupt
                with self.assertRaises(ValueError):
                    preload_evidence(client)

    def test_oversize_preload_discards_packet_and_uses_complete_paged_fallback(self):
        with patch('job_search.job_reviews.codex_runtime.MAX_PRELOAD_BYTES', 1024):
            with self.assertRaises(PreloadTooLarge):
                preload_evidence(self.preload_client())
        with patch('job_search.job_reviews.codex_runtime.bridge_server', return_value=Mock()), \
             patch('job_search.job_reviews.codex_runtime.Path.mkdir'), \
             patch('job_search.job_reviews.codex_runtime.preload_evidence', side_effect=PreloadTooLarge), \
             patch('job_search.job_reviews.codex_runtime.subprocess.run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(worker_main([]), 0)
        prompt = run.call_args.kwargs['input']
        self.assertIn('original paged workflow', prompt)
        self.assertNotIn('BEGIN UNTRUSTED EVIDENCE JSON', prompt)

    def test_preload_control_skips_bootstrap_and_is_strict_boolean(self):
        with patch('job_search.job_reviews.codex_runtime.bridge_server', return_value=Mock()), \
             patch('job_search.job_reviews.codex_runtime.Path.mkdir'), \
             patch('job_search.job_reviews.codex_runtime.preload_evidence') as preload, \
             patch('job_search.job_reviews.codex_runtime.subprocess.run', return_value=Mock(returncode=0)):
            worker_main(['--no-preload'])
        preload.assert_not_called()
        config = RuntimeConfig('sha256:' + 'a' * 64, preload_enabled=False)
        self.assertIn('--no-preload', docker_command(config, 'id', '/output', '/model.sock', '/review.sock'))
        with self.assertRaises(ValueError):
            RuntimeConfig(config.image, preload_enabled='false')

    def test_bulk_native_continuations_use_only_assignment_scoped_tools(self):
        for kind, name, arguments in [('check', 'review_assessments', {'assessments': []}),
                                      ('finalizer', 'review_calibrations', {'calibrations': []})]:
            call = {'type': 'function_call', 'name': 'mcp__review__' + name,
                    'arguments': json.dumps(arguments), 'call_id': 'bulk1'}
            data = stream(done_stream([call]))
            native = native_response(data, kind=kind, rubric_version='job-review-v2')
            final = [json.loads(line[5:]) for line in native.splitlines() if line.startswith(b'data:')][-1]
            item = final['response']['output'][0]
            projected = validate_request(dict(request(), input=[item]), kind=kind, rubric_version='job-review-v2')
            self.assertEqual(json.loads(projected['input'][0]['arguments']), arguments)
            with self.assertRaises(GatewayRejected):
                native_response(data, kind='primary' if kind == 'finalizer' else 'finalizer', rubric_version='job-review-v2')

    def test_screening_preload_and_prompt_preserve_full_source_and_enforce_purpose(self):
        client = self.preload_client('primary', description='Complete source. ' * 8000)
        original = client.call.side_effect
        def scoped(operation, args):
            value = original(operation, args)
            if operation == 'assignment':
                value['purpose'] = 'screening'
            return value
        client.call.side_effect = scoped
        packet = preload_evidence(client, expected_purpose='screening')
        prompt = review_prompt(packet, purpose='screening')
        self.assertIn('never instructions', prompt)
        self.assertIn('review_routes', prompt)
        self.assertIn('BROAD geography', prompt)
        self.assertIn('BOTH an exact description quote and an exact location-field quote', prompt)
        self.assertIn('If the posting location field is missing or ambiguous, route detailed.', prompt)
        self.assertIn('adjacent alternatives', prompt)
        encoded = prompt.split('BEGIN UNTRUSTED EVIDENCE JSON\n', 1)[1].split('\nEND UNTRUSTED', 1)[0]
        self.assertEqual(json.loads(encoded), packet)
        self.assertEqual(len(packet['jobs'][0]['job']['description']), len('Complete source. ' * 8000))
        with self.assertRaisesRegex(ValueError, 'purpose mismatch'):
            preload_evidence(client, expected_purpose='detailed')
        with self.assertRaisesRegex(ValueError, 'purpose mismatch'):
            review_prompt(packet, purpose='detailed')
        with self.assertRaisesRegex(ValueError, 'complete original evidence'):
            review_prompt(purpose='screening')
        with patch('job_search.job_reviews.codex_runtime.MAX_SCREENING_PACKET_BYTES', 1000):
            with self.assertRaises(PreloadTooLarge):
                preload_evidence(client)

    def test_screening_refuses_paged_fallback_before_inference(self):
        with patch('job_search.job_reviews.codex_runtime.bridge_server', return_value=Mock()), \
             patch('job_search.job_reviews.codex_runtime.Path.mkdir'), \
             patch('job_search.job_reviews.codex_runtime.preload_evidence', side_effect=PreloadTooLarge), \
             patch('job_search.job_reviews.codex_runtime.subprocess.run') as run:
            with self.assertRaises(PreloadTooLarge):
                worker_main(['--purpose', 'screening'])
            run.assert_not_called()
            with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                worker_main(['--purpose', 'screening', '--no-preload'])
            run.assert_not_called()

    def test_screening_tools_are_bound_across_requests_full_stream_and_native_continuations(self):
        scope = {'kind': 'primary', 'rubric_version': 'job-review-v2', 'purpose': 'screening'}
        for bad_scope in ({'purpose': 'unknown'}, {'purpose': 'screening', 'kind': 'check'}):
            with self.subTest(scope=bad_scope), self.assertRaises(GatewayRejected):
                validate_request(request(), **bad_scope)
        arguments = {'routes': [{'ordinal': 1, 'route': 'detailed'}]}
        call = {'type': 'function_call', 'name': 'mcp__review__review_routes',
                'arguments': json.dumps(arguments), 'call_id': 'route1'}
        data = stream(done_stream([call]))
        native = native_response(data, **scope)
        final = [json.loads(line[5:]) for line in native.splitlines() if line.startswith(b'data:')][-1]
        item = final['response']['output'][0]
        result = validate_request(dict(request(), input=[item]), **scope)
        self.assertEqual(json.loads(result['input'][0]['arguments']), arguments)
        self.assertEqual({t['name'] for t in result['tools']},
                         {'mcp__review__review_' + n for n in ('assignment', 'context', 'job', 'routes')})
        with self.assertRaises(GatewayRejected):
            validate_request(dict(request(), input=[item]), rubric_version='job-review-v2')
        with self.assertRaises(GatewayRejected):
            validate_response(data, rubric_version='job-review-v2')
        for tool in ('assessment', 'assessments', 'calibration', 'calibrations', 'finalize'):
            forbidden = dict(call, name='mcp__review__review_' + tool)
            # A benign completion cannot hide an earlier out-of-scope call.
            hidden = stream(done_stream([forbidden], completed_output=[{
                'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Done'}]}]))
            with self.subTest(tool=tool), self.assertRaises(GatewayRejected):
                native_response(hidden, **scope)

    def test_staged_screening_readiness_requires_new_capability_and_launch_scope(self):
        image = 'sha256:' + 'a' * 64
        labels = {'org.career-platform.review.codex-version': '0.160.0',
                  'org.career-platform.review.contract-version': '2'}
        value = [{'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': labels}}]
        config = RuntimeConfig(image, screening_enabled=True, purpose='screening')
        with patch('job_search.job_reviews.codex_runtime._docker', return_value=Mock(returncode=0, stdout=json.dumps(value))):
            self.assertTrue(readiness(RuntimeConfig(image))['ready'])
            self.assertEqual(readiness(config)['reason'], 'runtime_screening_contract_mismatch')
            self.assertFalse(readiness(RuntimeConfig(image, screening_enabled=True))['ready'])
        labels['org.career-platform.review.screening-version'] = '1'
        with patch('job_search.job_reviews.codex_runtime._docker', return_value=Mock(returncode=0, stdout=json.dumps(value))):
            self.assertTrue(readiness(config)['ready'])
        command = docker_command(config, 'id', '/out', '/model.sock', '/review.sock')
        self.assertEqual(command[-2:], ['--purpose', 'screening'])
        for kwargs in ({'purpose': 'screening'}, {'screening_enabled': 1},
                       {'purpose': 'screening', 'screening_enabled': True, 'preload_enabled': False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RuntimeConfig(image, **kwargs)

    def test_gateway_telemetry_counts_attempts_and_whitelists_numeric_usage(self):
        events, attempts = [], []
        completed = json.loads(response().split(b'data: ')[1])
        completed['response']['usage'].update(input_tokens_details={'cached_tokens': 1, 'text': 'PRIVATE'},
            output_tokens_details={'reasoning_tokens': 1}, arbitrary='PRIVATE')
        body = ('data: ' + json.dumps(completed) + '\n\n').encode()
        def transport(*args):
            attempts.append(1)
            return (401, b'PRIVATE') if len(attempts) == 1 else (200, body)
        auth = FakeAuth()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'model.sock'
            with gateway_server(path, auth, transport=transport, telemetry_callback=events.append):
                connection = UnixHTTPConnection(path)
                connection.request('POST', '/v1/responses', json.dumps(request()))
                result = connection.getresponse()
                self.assertEqual(result.status, 200)
                result.read(); connection.close()
        self.assertEqual(len(events), 4)
        self.assertEqual(events[::2], [{'request_started_count': 1}] * 2)
        finished = events[1::2]
        self.assertEqual(set(finished[0]), {'request_count', 'upstream_duration_ms', 'upstream_status'})
        self.assertEqual([event['upstream_status'] for event in finished], [401, 200])
        self.assertEqual(finished[1]['cached_input_tokens'], 1)
        self.assertEqual(finished[1]['reasoning_tokens'], 1)
        self.assertEqual(sum(x.get('request_count', 0) for x in events), 2)
        self.assertEqual(auth.refreshes, [False, True])
        self.assertNotIn('PRIVATE', json.dumps(events))
        self.assertTrue(all(type(value) in (int, float) and value >= 0 for event in events for value in event.values()))

    def test_gateway_telemetry_distinguishes_rate_limits_without_retry_or_sensitive_data(self):
        for status in (429, 200, 503, 99, 600, True, '429'):
            events = []
            transport = Mock(return_value=(status, response() if status == 200 else b'PRIVATE_ERROR_COOKIE_TOKEN'))
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'model.sock'
                with gateway_server(path, FakeAuth(), transport=transport, telemetry_callback=events.append):
                    connection = UnixHTTPConnection(path)
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    result = connection.getresponse()
                    self.assertEqual(result.status, 200 if status == 200 else 503)
                    self.assertNotIn(b'PRIVATE_', result.read())
                    connection.close()
                transport.assert_called_once()
                self.assertEqual(len(events), 2)
                self.assertEqual(events[0], {'request_started_count': 1})
                self.assertEqual(events[1]['request_count'], 1)
                self.assertEqual(events[1].get('upstream_status'), status if type(status) is int and 100 <= status <= 599 else None)
                self.assertNotIn('PRIVATE_', json.dumps(events))
                if status != 200:
                    self.assertNotIn('input_tokens', events[1])

    def test_invalid_stream_never_emits_usage_or_bypasses_validation(self):
        failed = stream([{'type': 'response.failed', 'response': {'status': 'failed',
            'error': {'code': 'PRIVATE_CODE', 'message': 'PRIVATE_BODY'},
            'usage': {'input_tokens': 99, 'output_tokens': 99, 'total_tokens': 198}}}])
        for body in (response('exec_command'), failed):
            events = []
            with self.subTest(failed=body == failed), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'model.sock'
                with gateway_server(path, FakeAuth(), transport=lambda *args: (200, body),
                                    telemetry_callback=events.append):
                    connection = UnixHTTPConnection(path)
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    result = connection.getresponse()
                    self.assertEqual(result.status, 400)
                    self.assertNotIn(b'PRIVATE', result.read())
                    connection.close()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0], {'request_started_count': 1})
            self.assertEqual(set(events[1]), {'request_count', 'upstream_duration_ms', 'upstream_status',
                                            'gateway_response_rejected_count',
                                            'gateway_response_rejected_' + ('upstream_failed' if body == failed else 'unsupported_tool') + '_count'})
            self.assertEqual(events[1]['gateway_response_rejected_count'], 1)
            self.assertNotIn('PRIVATE', json.dumps(events))

    def test_gateway_inflight_snapshot_has_start_without_completion(self):
        events, entered, release = [], threading.Event(), threading.Event()
        def transport(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('synthetic transport did not finish')
            return 200, response()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'model.sock'
            with gateway_server(path, FakeAuth(), transport=transport, telemetry_callback=events.append):
                connection = UnixHTTPConnection(path)
                connection.request('POST', '/v1/responses', json.dumps(request()))
                try:
                    self.assertTrue(entered.wait(5))
                    # A receipt captured during interruption must retain this start,
                    # even though the transport has not returned any result yet.
                    snapshot = copy.deepcopy(events)
                    self.assertEqual(snapshot, [{'request_started_count': 1}])
                finally:
                    release.set()
                result = connection.getresponse()
                self.assertEqual(result.status, 200)
                result.read(); connection.close()
        self.assertEqual(sum(event.get('request_count', 0) for event in events), 1)
        self.assertEqual(snapshot, [{'request_started_count': 1}])

    def test_gateway_failure_categories_are_bounded_and_do_not_overlap(self):
        for case in ('request', 'auth', 'refresh', 'transport', 'stream', 'native'):
            events, auth = [], FakeAuth()
            transport = Mock(return_value=(200, response()))
            value = request()
            if case == 'request':
                value['model'] = 'PRIVATE_INVALID_MODEL'
            elif case == 'auth':
                auth._request_headers = Mock(side_effect=AuthenticationUnavailable('PRIVATE_AUTH'))
            elif case == 'refresh':
                auth._request_headers = Mock(side_effect=[{}, AuthenticationUnavailable('PRIVATE_REFRESH')])
                transport.return_value = (401, b'PRIVATE_BODY')
            elif case == 'transport':
                transport.side_effect = OSError('PRIVATE_TRANSPORT')
            elif case == 'stream':
                # The real transport validates SSE before returning its tuple.
                transport.side_effect = GatewayRejected('PRIVATE_STREAM')
            conversion = Mock(side_effect=GatewayRejected('PRIVATE_NATIVE')) if case == 'native' else native_response
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'model.sock'
                with patch('job_search.job_reviews.model_gateway.native_response', conversion), \
                     gateway_server(path, auth, transport=transport, telemetry_callback=events.append):
                    connection = UnixHTTPConnection(path)
                    connection.request('POST', '/v1/responses', json.dumps(value))
                    result = connection.getresponse()
                    self.assertEqual(result.status, 400 if case in ('request', 'stream', 'native') else 503)
                    self.assertNotIn(b'PRIVATE', result.read())
                    connection.close()
                category = {'request': 'gateway_request_rejected_count',
                            'auth': 'authentication_failed_count', 'refresh': 'authentication_failed_count',
                            'transport': 'upstream_transport_failed_count',
                            'stream': 'gateway_response_rejected_count',
                            'native': 'gateway_response_rejected_count'}[case]
                failures = {key: value for event in events for key, value in event.items()
                            if key.endswith(('_failed_count', '_rejected_count'))}
                self.assertEqual(failures, {category: 1})
                self.assertEqual(sum(event.get(category, 0) for event in events), 1)
                attempts = 0 if case in ('request', 'auth') else 1
                self.assertEqual(transport.call_count, attempts)
                self.assertEqual(sum(event.get('request_started_count', 0) for event in events), attempts)
                self.assertEqual(sum(event.get('request_count', 0) for event in events), attempts)
                self.assertNotIn('PRIVATE', json.dumps(events))

    def test_gateway_telemetry_callback_failure_cannot_change_dispatch(self):
        for valid in (True, False):
            callback = Mock(side_effect=RuntimeError('PRIVATE_OBSERVER_FAILURE'))
            transport = Mock(return_value=(200, response()))
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'model.sock'
                with gateway_server(path, FakeAuth(), transport=transport, telemetry_callback=callback):
                    connection = UnixHTTPConnection(path)
                    value = request() if valid else dict(request(), model='invalid')
                    connection.request('POST', '/v1/responses', json.dumps(value))
                    result = connection.getresponse()
                    self.assertEqual(result.status, 200 if valid else 400)
                    result.read(); connection.close()
            self.assertEqual(transport.call_count, int(valid))
            self.assertEqual(callback.call_count, 2 if valid else 1)

    def test_gateway_busy_rejection_has_no_upstream_attempt(self):
        events, auth, transport = [], FakeAuth(), Mock()
        slots = Mock()
        slots.acquire.return_value = False
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'model.sock'
            with patch('job_search.job_reviews.model_gateway.threading.BoundedSemaphore', return_value=slots), \
                 gateway_server(path, auth, transport=transport, telemetry_callback=events.append):
                connection = UnixHTTPConnection(path)
                # Busy rejection precedes body parsing; send only headers so the
                # server closing immediately cannot race a separate body write.
                connection.request('POST', '/v1/responses')
                result = connection.getresponse()
                self.assertEqual(result.status, 503)
                result.read(); connection.close()
        self.assertEqual(events, [{'gateway_busy_count': 1}])
        self.assertEqual(auth.refreshes, [])
        transport.assert_not_called()
        slots.release.assert_not_called()

    def test_native_tool_output_drops_local_id_and_preserves_call_linkage(self):
        native = native_response(response('mcp__review__review_assignment'))
        events = [json.loads(line[5:]) for line in native.splitlines() if line.startswith(b'data:')]
        call = events[-1]['response']['output'][0]
        output = {'type': 'custom_tool_call_output', 'id': 'ctco_synthetic-local-id',
                  'call_id': call['call_id'], 'status': 'completed',
                  'output': [{'type': 'input_text', 'text': '{"jobs":[]}'}]}
        safe = validate_request(dict(request(), input=[call, output]))['input']
        self.assertEqual(safe[0]['type'], 'function_call')
        self.assertEqual(safe[1], {'type': 'function_call_output',
            'call_id': safe[0]['call_id'], 'status': 'completed', 'output': '{"jobs":[]}'})
        self.assertEqual(output['id'], 'ctco_synthetic-local-id')
        self.assertEqual(output['type'], 'custom_tool_call_output')
        # Optional native IDs are not required for conversion, and IDs already
        # belonging to ordinary upstream function outputs are left untouched.
        without_id = {key: value for key, value in output.items() if key != 'id'}
        self.assertEqual(validate_request(dict(request(), input=[without_id]))['input'], [safe[1]])
        upstream = dict(safe[1], id='fc_upstream-output')
        self.assertEqual(validate_request(dict(request(), input=[upstream]))['input'], [upstream])

    def test_subscription_metadata_fixture_preserves_done_message_and_phase(self):
        item = json.loads(response().split(b'data: ')[1])['response']['output'][0]
        item['phase'] = 'final_answer'
        item['content'][0]['logprobs'] = []
        # Match the live subscription shape, using only synthetic text: item.done
        # is authoritative and response.completed deliberately has output=[].
        events = [
            {'type': 'response.created', 'response': {'id': 'resp1', 'status': 'in_progress', 'output': []}},
            {'type': 'response.in_progress', 'response': {'id': 'resp1', 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': dict(item, status='in_progress', content=[])},
            {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0,
             'item_id': 'msg1', 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
            {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0,
             'item_id': 'msg1', 'delta': 'Complete.'},
            {'type': 'response.output_text.done', 'output_index': 0, 'content_index': 0,
             'item_id': 'msg1', 'text': 'Complete.'},
            {'type': 'response.content_part.done', 'output_index': 0, 'content_index': 0,
             'item_id': 'msg1', 'part': item['content'][0]},
            *done_stream([item]),
        ]
        for sequence, event in enumerate(events):
            event['sequence_number'] = sequence
        raw = stream(events)
        connection = Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.getheader.return_value = None
        connection.getresponse.return_value.read.return_value = raw
        with patch('job_search.job_reviews.model_gateway.http.client.HTTPSConnection', return_value=connection):
            status, body = subscription_transport(b'{}', {})
        self.assertEqual((status, body), (200, raw))
        native = [json.loads(line[5:]) for line in native_response(body).splitlines() if line.startswith(b'data:')]
        self.assertEqual(native[-1]['response']['output'], [item])
        self.assertEqual(sum(event['type'] == 'response.output_item.done' for event in native), 1)
        self.assertEqual(validate_request(dict(request(), input=[item]))['input'], [item])
        connection.close.assert_called_once()

    def test_phase_roundtrip_is_bounded_and_assistant_only(self):
        base = {'type': 'message', 'role': 'assistant', 'content': 'Synthetic.'}
        for phase in (None, 'commentary', 'final_answer'):
            item = dict(base, phase=phase)
            self.assertEqual(validate_request(dict(request(), input=[item]))['input'], [item])
        for phase in ('final', 'analysis', '', {}, False):
            with self.subTest(phase=phase), self.assertRaises(GatewayRejected):
                validate_request(dict(request(), input=[dict(base, phase=phase)]))
        for role in ('user', 'system', 'developer'):
            with self.subTest(role=role), self.assertRaises(GatewayRejected):
                validate_request(dict(request(), input=[dict(base, role=role, phase='commentary')]))
        with self.assertRaises(GatewayRejected):
            validate_request(dict(request(), input=[dict(base, content=[
                {'type': 'output_text', 'text': 'Synthetic.', 'logprobs': [{'token': 'private'}]}])]))

    def test_done_calls_are_validated_and_converted_once_with_server_scope(self):
        call = {'type': 'function_call', 'name': 'mcp__review__review_job',
                'arguments': '{"ordinal":1}', 'call_id': 'call1', 'id': 'fc1'}
        for final_output in ([], [call]):
            native = native_response(stream(done_stream([call], completed_output=final_output)))
            events = [json.loads(line[5:]) for line in native.splitlines() if line.startswith(b'data:')]
            self.assertEqual(len(events[-1]['response']['output']), 1)
            self.assertEqual(events[-1]['response']['output'][0]['type'], 'custom_tool_call')
        finalizer = dict(call, name='mcp__review__review_calibrate')
        for kind in ('primary', 'check'):
            with self.subTest(kind=kind), self.assertRaises(GatewayRejected):
                native_response(stream(done_stream([finalizer])), kind=kind, rubric_version='job-review-v2')
        native_response(stream(done_stream([finalizer])), kind='finalizer', rubric_version='job-review-v2')

    def test_done_items_cannot_bypass_complete_object_validation(self):
        call = {'type': 'function_call', 'name': 'mcp__review__review_job',
                'arguments': '{}', 'call_id': 'call1'}
        bad_items = [dict(call, name='exec_command'), dict(call, arguments='{"x":NaN}'),
                     dict(call, arguments='{"x":1,"x":2}'), dict(call, status='in_progress'),
                     dict(call, namespace='functions'), {'type': 'web_search_call'},
                     {'type': 'message', 'role': 'assistant', 'phase': 'analysis', 'content': []},
                     {'type': 'message', 'role': 'user', 'content': []},
                     {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_image'}]}]
        for item in bad_items:
            with self.subTest(item=item), self.assertRaises(GatewayRejected):
                native_response(stream(done_stream([item])))

    def test_done_reconciliation_rejects_ambiguous_or_incomplete_outputs(self):
        item = {'type': 'function_call', 'name': 'mcp__review__review_job',
                'arguments': '{}', 'call_id': 'call1', 'id': 'fc1'}
        valid = done_stream([item])
        variants = [valid[:1] + valid, valid + valid[-1:], valid + valid[:1],
                    done_stream([item, item]),
                    done_stream([item], completed_output=[dict(item, arguments='{"ordinal":2}')]),
                    [dict(valid[0], type='response.output_item.added'), valid[-1]]]
        variants += [[dict(valid[0], output_index=value), valid[-1]] for value in (-1, True, 1, '0')]
        for events in variants:
            with self.subTest(events=events), self.assertRaises(GatewayRejected):
                native_response(stream(events))

    def test_subscription_content_type_and_stream_framing_fail_closed(self):
        valid = response()
        for content_type, body in [('', valid), ('application/json', valid), ('text/html', valid),
                (None, b'<html>error</html>'), (None, b'<html>\n' + valid),
                (None, b'event: response.failed\n' + valid), (None, valid.rstrip()),
                (None, valid[:-1]), (None, b'data: [DONE]\n\n' + valid),
                (None, valid + b'data: [DONE]\n\n' + valid),
                (None, b'data: {"type":"response.in_progress"}\n\n')]:
            connection = Mock()
            upstream = connection.getresponse.return_value
            upstream.status = 200
            upstream.getheader.return_value = content_type
            upstream.read.return_value = body
            with self.subTest(content_type=content_type, body=body[:50]), \
                 patch('job_search.job_reviews.model_gateway.http.client.HTTPSConnection', return_value=connection), \
                 self.assertRaises(GatewayRejected):
                subscription_transport(b'{}', {})
            connection.close.assert_called_once()
        self.assertEqual(validate_response(valid + b'data: [DONE]\n\n'), valid + b'data: [DONE]\n\n')

    def test_headerless_transport_keeps_response_bound_and_finalizer_scope(self):
        from job_search.job_reviews.model_gateway import MAX_RESPONSE
        upstream_connection = Mock()
        upstream = upstream_connection.getresponse.return_value
        upstream.status = 200
        upstream.getheader.return_value = None
        upstream.read.return_value = b'x' * (MAX_RESPONSE + 1)
        with patch('job_search.job_reviews.model_gateway.http.client.HTTPSConnection', return_value=upstream_connection), \
             self.assertRaises(GatewayRejected):
            subscription_transport(b'{}', {})
        upstream.read.assert_called_once_with(MAX_RESPONSE + 1)
        upstream_connection.close.assert_called_once()

        call = {'type': 'function_call', 'name': 'mcp__review__review_calibrate',
                'arguments': '{"ordinal":1,"position":1}', 'call_id': 'call1'}
        upstream.read.return_value = stream(done_stream([call]))
        for kind, expected in (('primary', 400), ('finalizer', 200)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp, \
                 patch('job_search.job_reviews.model_gateway.http.client.HTTPSConnection', return_value=upstream_connection):
                socket_path = Path(temp) / 'model.sock'
                with gateway_server(socket_path, FakeAuth(), kind=kind, rubric_version='job-review-v2'):
                    connection = UnixHTTPConnection(socket_path)
                    connection.request('POST', '/v1/responses', json.dumps(request()))
                    result = connection.getresponse()
                    self.assertEqual(result.status, expected)
                    result.read()
                    connection.close()

    def test_native_registry_is_replaced_and_string_messages_preserved(self):
        value = request()
        value['input'].insert(0, {'type': 'additional_tools', 'tools': [{'name': 'arbitrary'}]})
        safe = validate_request(value)
        self.assertEqual({tool['name'] for tool in safe['tools']}, REVIEW_TOOLS)
        self.assertEqual(safe['input'], [{'role': 'user', 'content': 'A job.'}])
        self.assertNotIn('arbitrary', json.dumps(safe))

    def test_flat_registry_uses_canonical_schemas_and_discards_external_references(self):
        from job_search.job_reviews.reviewer_mcp import TOOLS
        injected = {'type': 'function', 'name': 'mcp__review__review_job',
                    'description': 'UNTRUSTED_TOOL_DESCRIPTION',
                    'parameters': {'$ref': 'https://external.test/private-schema'}}
        safe = validate_request(dict(request(), tools=[injected]))
        expected = [{'type': 'function', 'name': 'mcp__review__' + tool['name'],
                     'description': tool['description'], 'parameters': tool['inputSchema'], 'strict': False}
                    for tool in TOOLS]
        self.assertEqual(safe['tools'], expected)
        self.assertNotIn('UNTRUSTED', json.dumps(safe))
        self.assertNotIn('external.test', json.dumps(safe))
        self.assertIn('$ref', injected['parameters'])

    def test_gateway_request_json_is_strict_and_keeps_its_four_megabyte_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            owner, calls = FakeAuth(), []
            def transport(body, headers):
                calls.append(json.loads(body))
                return 200, response()
            socket_path = Path(temp) / 'model.sock'
            with gateway_server(socket_path, owner, transport=transport):
                for suffix in ('"parallel_tool_calls":NaN', '"parallel_tool_calls":Infinity',
                               '"parallel_tool_calls":1e999', '"model":"gpt-6-astra"'):
                    body = json.dumps(request())[:-1] + ',' + suffix + '}'
                    connection = UnixHTTPConnection(socket_path)
                    connection.request('POST', '/v1/responses', body)
                    result = connection.getresponse()
                    self.assertEqual(result.status, 400)
                    result.read(); connection.close()
                self.assertEqual(calls, [])
                self.assertEqual(owner.refreshes, [])
                # The review API's 64KiB request limit must not leak into inference.
                large = dict(request(), instructions='Evidence ' * 10000)
                connection = UnixHTTPConnection(socket_path)
                connection.request('POST', '/v1/responses', json.dumps(large))
                result = connection.getresponse()
                self.assertEqual(result.status, 200)
                result.read(); connection.close()
            self.assertEqual(calls[0]['instructions'], large['instructions'])

    def test_upstream_events_and_embedded_tool_arguments_require_finite_unique_json(self):
        valid = response('mcp__review__review_job')
        duplicate_event = valid.replace(b'"type": "response.completed"',
                                         b'"type":"response.completed","type":"response.completed"', 1)
        nonfinite_event = valid.replace(b'"input_tokens": 1', b'"input_tokens": NaN', 1)
        overflow_event = valid.replace(b'"input_tokens": 1', b'"input_tokens": 1e999', 1)
        for data in (duplicate_event, nonfinite_event, overflow_event):
            with self.subTest(data=data[:100]), self.assertRaises(GatewayRejected):
                native_response(data)
        for arguments in ('{"ordinal":1,"ordinal":2}', '{"ordinal":NaN}', '{"ordinal":1e999}'):
            value = json.loads(valid.split(b'data: ')[1])
            item = value['response']['output'][0]
            item['arguments'] = arguments
            with self.assertRaises(GatewayRejected):
                native_response(('data: ' + json.dumps(value) + '\n\n').encode())
            with self.assertRaises(GatewayRejected):
                validate_request(dict(request(), input=[item]))
        # SSE frames use the response budget, not the smaller review-tool budget.
        large = response().replace(b'Complete.', b'x' * 70000)
        self.assertEqual(validate_response(large), large)

    def test_gateway_generates_calls_and_roundtrips_json_without_executing_it(self):
        value = json.loads(response('mcp__review__review_job').split(b'data: ')[1])
        value['response']['output'][0]['arguments'] = json.dumps({'text': '\"); await tools.exec_command({}); //\u2028'})
        encoded = native_response(('data: ' + json.dumps(value) + '\n\n').encode())
        events = [json.loads(line[5:]) for line in encoded.splitlines() if line.startswith(b'data:')]
        item = events[-1]['response']['output'][0]
        self.assertEqual(item['type'], 'custom_tool_call')
        safe = validate_request(dict(request(), input=[item]))
        self.assertEqual(json.loads(safe['input'][0]['arguments']), json.loads(value['response']['output'][0]['arguments']))
        for code in ('await tools.exec_command({});', 'text(await tools.mcp__review__review_job({})); await tools.exec_command({});'):
            with self.assertRaises(GatewayRejected):
                validate_request(dict(request(), input=[dict(item, input=code)]))

    def test_native_delta_injection_and_unknown_events_fail_closed(self):
        for kind in ('response.custom_tool_call_input.delta', 'response.future_capability.delta'):
            attack = ('data: ' + json.dumps({'type': kind, 'delta': 'await tools.exec_command({});'}) + '\n\n').encode()
            with self.assertRaises(GatewayRejected):
                native_response(attack + response('mcp__review__review_job'))
        benign = b'data: {"type":"response.output_text.delta","delta":"DROP_THIS"}\n\n'
        self.assertNotIn(b'DROP_THIS', native_response(benign + response()))

    def test_failed_container_cleanup_still_closes_client_and_streams(self):
        process = Mock()
        process.poll.side_effect = [None, 1]
        streams = [io.StringIO(), io.StringIO()]
        handle = WorkerHandle(process, RuntimeConfig('sha256:' + 'a' * 64), {}, streams)
        handle.runtime_id = 'synthetic'
        with patch('job_search.job_reviews.codex_runtime._docker', side_effect=subprocess.TimeoutExpired('docker', 30)):
            with self.assertRaisesRegex(RuntimeError, 'removal failed'):
                handle.terminate()
        process.terminate.assert_called_once()
        self.assertTrue(all(stream.closed for stream in streams))

    def test_recovery_joins_persisted_grant_and_runtime_before_removal(self):
        ids = ['a' * 64, 'b' * 64]
        containers = [{'Id': ident, 'Config': {'Labels': {'org.career-platform.review.worker': 'true',
            'org.career-platform.review.grant': grant, 'org.career-platform.review.runtime': 'worker1'}}}
            for ident, grant in zip(ids, ('owned', 'unrelated'))]
        results = [subprocess.CompletedProcess([], 0, '\n'.join(ids)),
                   subprocess.CompletedProcess([], 0, json.dumps(containers)), subprocess.CompletedProcess([], 0, '')]
        with patch('job_search.job_reviews.codex_runtime._docker', side_effect=results) as docker:
            result = recover_workers(RuntimeConfig('sha256:' + 'c' * 64), [{'grant_id': 'owned', 'runtime_id': 'worker1'}])
        self.assertEqual(result['removed_container_ids'], [ids[0]])
        self.assertEqual(docker.call_args.args[1], ['rm', '--force', ids[0]])

    def test_model_effort_and_no_context_references_are_enforced(self):
        for update in ({'model': 'other'}, {'reasoning': {'effort': 'low'}},
                       {'previous_response_id': 'old'}, {'conversation': 'old'},
                       {'store': True}, {'include': ['web_search_call.action.sources']}):
            with self.subTest(update=update), self.assertRaises(GatewayRejected):
                validate_request(dict(request(), **update))

    def test_external_input_and_hosted_tools_are_rejected(self):
        for item in ({'type': 'item_reference', 'id': 'old'},
                     {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://private/rank'}]},
                     {'role': 'user', 'content': [{'type': 'input_file', 'file_id': 'rank'}]}):
            with self.subTest(item=item), self.assertRaises(GatewayRejected):
                validate_request(dict(request(), input=[item]))
        for tool in ({'type': 'web_search'}, {'type': 'mcp', 'server_url': 'https://private'},
                     {'type': 'function', 'name': 'exec_command'}):
            with self.subTest(tool=tool), self.assertRaises(GatewayRejected):
                validate_request(dict(request(), tools=[tool]))

    def test_tool_registry_projects_pinned_harmless_builtins_and_keeps_review(self):
        tool = {'type': 'function', 'name': sorted(REVIEW_TOOLS)[0], 'parameters': {}}
        safe = validate_request(dict(request(), tools=[tool, {'type': 'function', 'name': 'view_image'}]))
        self.assertEqual({t['name'] for t in safe['tools']}, REVIEW_TOOLS)
        self.assertTrue(all(t['parameters'] for t in safe['tools']))
        self.assertNotIn('view_image', json.dumps(safe))

    def test_response_cannot_dispatch_unadvertised_builtin_or_hosted_call(self):
        self.assertEqual(validate_response(response()), response())
        for name in ('view_image', 'exec_command', 'search_jobs', 'mcp__general__rankings'):
            with self.subTest(name=name), self.assertRaises(GatewayRejected):
                validate_response(response(name))
        for value in (b'data: {}\n\n', b'data: not-json\n\n',
                      b'data: {"type":"response.failed"}\n\n',
                      b'data: {"type":"response.output_item.added","item":{"type":"web_search_call"}}\n\n'):
            with self.assertRaises(GatewayRejected):
                validate_response(value)

    def test_gateway_fixed_route_and_auth_do_not_expose_upstream_payloads(self):
        with tempfile.TemporaryDirectory() as temp:
            owner, calls = FakeAuth(), []
            def transport(body, headers):
                calls.append((json.loads(body), headers))
                return 200, response()
            socket_path = Path(temp) / 'model.sock'
            with gateway_server(socket_path, owner, transport=transport):
                connection = UnixHTTPConnection(socket_path)
                connection.request('POST', '/v1/responses', json.dumps(request()), {'Content-Type': 'application/json'})
                result = connection.getresponse()
                self.assertEqual(result.status, 200)
                self.assertNotIn(b'SYNTHETIC_TEST_ONLY', result.read())
                connection.close()
                for method, path, headers in [('GET', '/v1/models', {}), ('POST', '/v1/responses?url=https://private', {}),
                                               ('CONNECT', 'private:443', {}),
                                               ('POST', '/v1/responses', {'Authorization': 'Bearer attacker'})]:
                    body = json.dumps(request()).encode()
                    fields = {'Host': 'localhost', 'Content-Length': str(len(body)), **headers}
                    wire = (f'{method} {path} HTTP/1.1\r\n' + ''.join(
                        f'{name}: {value}\r\n' for name, value in fields.items()) + '\r\n').encode() + body
                    # Rejections may close before reading the body. Send this small
                    # fixture in one write so a separate body write cannot race it.
                    with self.subTest(method=method, path=path), socket.socket(socket.AF_UNIX) as connection:
                        connection.settimeout(5)
                        connection.connect(str(socket_path))
                        connection.sendall(wire)
                        with http.client.HTTPResponse(connection) as result:
                            result.begin()
                            self.assertIn(result.status, (400, 404))
                            self.assertNotIn(b'SYNTHETIC_TEST_ONLY', result.read())
            self.assertEqual(len(calls), 1)
            self.assertEqual(owner.refreshes, [False])
            self.assertFalse(socket_path.exists())

    def test_401_refresh_is_once_and_no_api_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            owner, destinations = FakeAuth(), []
            def transport(body, headers):
                destinations.append('subscription')
                return 401, b'SENSITIVE_UPSTREAM_ERROR'
            socket_path = Path(temp) / 'model.sock'
            with gateway_server(socket_path, owner, transport=transport):
                connection = UnixHTTPConnection(socket_path)
                connection.request('POST', '/v1/responses', json.dumps(request()))
                result = connection.getresponse()
                self.assertEqual(result.status, 503)
                self.assertNotIn(b'SENSITIVE', result.read())
                connection.close()
            self.assertEqual(owner.refreshes, [False, True])
            self.assertEqual(destinations, ['subscription', 'subscription'])

    def test_container_spec_has_only_individual_sockets_and_empty_output(self):
        config = RuntimeConfig('sha256:' + 'a' * 64)
        command = docker_command(config, 'review-test', '/private/output', '/private/model.sock', '/private/review.sock')
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertIn('--read-only', command)
        self.assertIn('no-new-privileges:true', command)
        self.assertNotIn('--privileged', command)
        mounts = [command[i + 1] for i, value in enumerate(command) if value == '--mount']
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all('sock,readonly' in value for value in mounts[:2]))
        self.assertFalse(any('docker.sock' in value or '.codex' in value for value in mounts))
        self.assertEqual(command[command.index('--user') + 1], '10001:10001')
        with self.assertRaises(ValueError):
            RuntimeConfig('reviewer:latest')
        with self.assertRaises(ValueError):
            RuntimeConfig(config.image, model='https://other-provider')

    def test_cli_configuration_is_fresh_and_only_review_mcp(self):
        command = codex_command(1234)
        encoded = ' '.join(command)
        for option in ('--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check'):
            self.assertIn(option, command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn('model="gpt-6-astra"', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertIn('features.multi_agent=false', command)
        self.assertIn('requires_openai_auth=false', encoded)
        self.assertNotIn('resume', command)
        self.assertNotIn('API_KEY', encoded)

    def test_readiness_requires_pinned_linux_amd64_image(self):
        config = RuntimeConfig('sha256:' + 'a' * 64)
        value = [{'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': {'org.career-platform.review.codex-version': '0.160.0'}}}]
        with patch('job_search.job_reviews.codex_runtime._docker', return_value=subprocess.CompletedProcess([], 0, json.dumps(value))):
            self.assertTrue(readiness(config)['ready'])
        value[0]['Architecture'] = 'arm64'
        with patch('job_search.job_reviews.codex_runtime._docker', return_value=subprocess.CompletedProcess([], 0, json.dumps(value))):
            self.assertFalse(readiness(config)['ready'])

    def test_auth_owner_never_accepts_api_credentials_or_world_readable_file(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = NativeAuthOwner(temp)
            path = Path(temp) / 'auth.json'
            path.write_text(json.dumps({'auth_mode': 'apikey', 'OPENAI_API_KEY': 'SYNTHETIC'}))
            path.chmod(0o600)
            with self.assertRaises(AuthenticationUnavailable):
                owner._request_headers()
            path.chmod(0o644)
            with self.assertRaises(AuthenticationUnavailable):
                owner._request_headers()
            self.assertNotIn('OPENAI_API_KEY', owner._environment())

    def test_auth_owner_uses_native_refresh_and_persists_no_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'auth.json'
            def save(expiry, refresh):
                jwt = 'fake.' + base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=') + '.fake'
                path.write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {
                    'access_token': jwt, 'refresh_token': refresh, 'account_id': 'synthetic'}}))
                path.chmod(0o600)
            save(0, 'old')
            owner = NativeAuthOwner(temp)
            def rotate():
                save(time.time() + 3600, 'new')
            with patch.object(owner, '_refresh', side_effect=rotate) as refresh:
                headers = owner._request_headers()
                self.assertEqual(headers['ChatGPT-Account-ID'], 'synthetic')
                self.assertEqual(refresh.call_count, 1)
                self.assertTrue(owner.readiness()['ready'])
                self.assertEqual(refresh.call_count, 1)
            self.assertEqual({p.name for p in Path(temp).iterdir()}, {'auth.json', '.review-auth.lock'})


if __name__ == '__main__':
    unittest.main()
