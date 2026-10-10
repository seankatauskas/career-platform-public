"""Bounded native Codex protocol qualification without live inference."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from job_search.job_reviews import native_protocol as protocol
from job_search.job_reviews.model_gateway import GatewayRejected


def request():
    return {'model': 'gpt-6-astra', 'reasoning': {'effort': 'high'},
        'store': False, 'stream': True, 'include': ['reasoning.encrypted_content'],
        'prompt_cache_key': 'native-session', 'client_metadata': {'session_id': 'native-session'},
        'input': [{'type': 'additional_tools', 'id': 'at_native', 'role': 'developer', 'tools': [
            {'type': 'namespace', 'name': 'functions', 'description': '', 'tools': [
                {'type': 'custom', 'name': 'exec', 'description': 'Local execution',
                 'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: /.+/'}},
                {'type': 'function', 'name': 'wait', 'parameters': {'type': 'object'}, 'strict': False},
                {'type': 'function', 'name': 'request_user_input', 'parameters': {'type': 'object'}}]}]},
            {'type': 'message', 'id': 'msg_1', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Review frozen jobs.'}]}]}


def call():
    return {'type': 'custom_tool_call', 'id': 'ct_1', 'call_id': 'call_1',
        'name': 'exec', 'namespace': 'functions', 'input': 'text(await tools.exec_command({cmd:"printf probe"}));',
        'status': 'completed'}


def stream(item=None, *, done_item=None):
    item = item or call()
    final = {'id': 'resp_1', 'status': 'completed', 'output': [item],
             'usage': {'input_tokens': 100, 'output_tokens': 10,
                       'input_tokens_details': {'cached_tokens': 50}}}
    events = [{'type': 'response.created', 'response': {'id': 'resp_1', 'output': []}},
              {'type': 'response.output_item.added', 'output_index': 0,
               'item': dict(item, status='in_progress', input='')},
              {'type': 'response.custom_tool_call_input.delta', 'output_index': 0, 'delta': item['input']},
              {'type': 'response.output_item.done', 'output_index': 0, 'item': done_item or item},
              {'type': 'response.completed', 'response': final}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


class ParityProtocolTests(unittest.TestCase):
    def test_native_request_and_continuation_are_not_rewritten(self):
        value = request()
        original = json.dumps(value)
        self.assertIs(protocol.validate_request(value), value)
        self.assertEqual(json.dumps(value), original)
        prefix = deepcopy(value['input'])
        value['input'].extend([call(), {'type': 'custom_tool_call_output', 'id': 'ctco_1',
            'call_id': 'call_1', 'output': [{'type': 'input_text', 'text': 'probe'}]}])
        self.assertIs(protocol.validate_request(value), value)
        self.assertEqual(value['input'][:len(prefix)], prefix)
        self.assertEqual(value['prompt_cache_key'], 'native-session')

    def test_models_hosted_tools_references_and_collaboration_rejected(self):
        variations = []
        for field, change in [('model', 'gpt-6-luna'), ('reasoning', {'effort': 'xhigh'}),
                              ('store', True), ('stream', False), ('previous_response_id', 'resp_old'),
                              ('tools', [{'type': 'web_search'}])]:
            value = request(); value[field] = change; variations.append(value)
        value = request(); value['input'][0]['tools'][0]['name'] = 'collaboration'; variations.append(value)
        value = request(); value['input'].append({'type': 'item_reference', 'id': 'other'}); variations.append(value)
        value = request(); value['input'].append(dict(call(), namespace='collaboration', name='spawn_agent')); variations.append(value)
        value = request(); value['input'].append({'type': 'message', 'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://example.test'}]}); variations.append(value)
        for value in variations:
            with self.subTest(value=value):
                with self.assertRaises(GatewayRejected):
                    protocol.validate_request(value)

    def test_native_response_preserves_bytes_ids_and_usage(self):
        data = stream()
        self.assertIs(protocol.validate_response(data), data)
        final = protocol.completion(data)
        self.assertEqual(final['output'][0], call())
        self.assertEqual(final['usage']['input_tokens_details']['cached_tokens'], 50)

    def test_completion_rejects_inconsistent_or_unapproved_calls(self):
        with self.assertRaises(GatewayRejected):
            protocol.validate_response(stream(done_item=dict(call(), input='changed')))
        with self.assertRaises(GatewayRejected):
            protocol.validate_response(stream(dict(call(), namespace='collaboration', name='spawn_agent')))
        with self.assertRaises(GatewayRejected):
            protocol.validate_response(stream(dict(call(), name='request_user_input')))
        with self.assertRaises(GatewayRejected):
            protocol.validate_response(b'data: {"type":"response.in_progress"}\n\n')
        with self.assertRaises(GatewayRejected):
            protocol.validate_response(stream() + b'data: {"type":"response.in_progress"}\n\n')

    def test_responses_lite_resolves_completed_items_without_rewriting_stream(self):
        events = [json.loads(part[6:]) for part in stream().decode().strip().split('\n\n')]
        events[-1]['response']['output'] = []
        raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        self.assertIs(protocol.validate_response(raw), raw)
        self.assertEqual(protocol.completion(raw)['output'], [call()])
        events[-2]['output_index'] = 1
        broken = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        with self.assertRaises(GatewayRejected):
            protocol.validate_response(broken)

    def test_partial_native_fields_defer_until_strict_completed_validation(self):
        events = [json.loads(part[6:]) for part in stream().decode().strip().split('\n\n')]
        events[1]['item'].pop('input')
        events[1]['item'].pop('call_id')
        raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        self.assertIs(protocol.validate_response(raw), raw)
        for field in ('input', 'call_id'):
            missing = deepcopy(events)
            missing[-2]['item'].pop(field)
            missing[-1]['response']['output'][0].pop(field)
            raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in missing).encode()
            with self.assertRaises(GatewayRejected):
                protocol.validate_response(raw)
        for field, value in (('namespace', 'collaboration'), ('name', 'spawn_agent')):
            unapproved = deepcopy(events)
            unapproved[1]['item'][field] = value
            raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in unapproved).encode()
            with self.assertRaises(GatewayRejected):
                protocol.validate_response(raw)

    def test_schema_diagnostic_never_emits_text_ids_unknown_keys_or_enum_values(self):
        secret = 'sensitive-never-report'
        events = [{'type': 'response.output_item.added', 'output_index': 0,
            'item': dict(call(), input=secret, call_id=secret, id=secret, caller={'type': secret}, **{secret: secret})},
            {'type': secret, 'item': {'type': secret, secret: secret}},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': [call()]}}]
        raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        value = protocol.schema_diagnostic(raw, GatewayRejected(secret))
        self.assertNotIn(secret, json.dumps(value))
        self.assertEqual(value['events'][0]['item']['unknown_key_count'], 1)
        self.assertIn('caller', value['events'][0]['item']['keys'])
        self.assertEqual(value['events'][-1]['output_count'], 1)
        events = [events[0]] * 101 + events[-1:]
        raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        value = protocol.schema_diagnostic(raw, GatewayRejected('invalid'))
        self.assertEqual(len(value['events']), 100)
        self.assertTrue(value['truncated'])
        self.assertEqual(value['events'][-1]['event'], 'response.completed')

    def test_documented_native_tool_metadata_is_preserved_without_new_capabilities(self):
        for caller in (None, {'type': 'direct'}, {'type': 'program', 'caller_id': 'parent_1'}):
            item = dict(call(), async_=True)
            item['async'] = item.pop('async_')
            item.update(caller=caller, created_by='native-actor')
            raw = stream(item)
            self.assertIs(protocol.validate_response(raw), raw)
            self.assertEqual(protocol.completion(raw)['output'][0], item)
            value = request(); value['input'].append(item)
            self.assertIs(protocol.validate_request(value), value)
            with self.assertRaises(GatewayRejected):
                protocol.validate_response(stream(dict(item, namespace='collaboration', name='spawn_agent')))
        for metadata in ({'async': 'true'}, {'created_by': {}}, {'caller': {'type': 'hosted'}},
                         {'caller': {'type': 'program'}}, {'caller': {'type': 'direct', 'caller_id': 'bad'}}):
            with self.assertRaises(GatewayRejected):
                protocol.validate_response(stream(dict(call(), **metadata)))

    def test_observed_subscription_metadata_and_implicit_namespace_are_preserved(self):
        item = call()
        item.pop('namespace')
        item.update(metadata={'opaque': {'nested': [True, None, 1]}},
                    internal_chat_message_metadata_passthrough={'transport': 'opaque'})
        raw = stream(item)
        self.assertIs(protocol.validate_response(raw), raw)
        self.assertEqual(protocol.completion(raw)['output'][0], item)
        value = request()
        value['input'].extend([item, {'type': 'custom_tool_call_output',
            'call_id': item['call_id'], 'output': 'local result', 'metadata': {}}])
        self.assertIs(protocol.validate_request(value), value)
        wait = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'wait_1',
                'name': 'wait', 'arguments': '{"cell_id":"local"}', 'status': 'completed'}
        message = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'Complete.', 'annotations': []}],
            'metadata': {}, 'internal_chat_message_metadata_passthrough': {'transport': 'opaque'}}
        for native in (wait, message):
            data = ('data: ' + json.dumps({'type': 'response.completed', 'response': {
                'status': 'completed', 'output': [native]}}) + '\n\n').encode()
            self.assertIs(protocol.validate_response(data), data)
        for change in ({'namespace': None}, {'namespace': 'collaboration'}, {'name': 'spawn_agent'},
                       {'metadata': []}, {'internal_chat_message_metadata_passthrough': 'not-object'},
                       {'metadata': {'bad': float('nan')}}):
            with self.assertRaises(GatewayRejected):
                protocol.validate_response(stream(dict(item, **change)))
        diagnostic = protocol.schema_diagnostic(raw, GatewayRejected('validation'))
        shape = diagnostic['events'][1]['item']
        self.assertIn('metadata', shape['keys'])
        self.assertEqual(shape['metadata_type'], 'object')
        self.assertNotIn('opaque', json.dumps(diagnostic))

    def test_protocol_header_allowlist_bounds_and_secret_rejection(self):
        self.assertEqual(protocol.routing_headers({'session-id': 'stable', 'Host': 'elsewhere',
            'X-Codex-Turn-State': 'opaque', 'Connection': 'keep-alive'}),
            {'session-id': 'stable', 'x-codex-turn-state': 'opaque'})
        self.assertEqual(protocol.routing_headers({'X-Codex-Turn-State': 'opaque',
            'Set-Cookie': 'never-forward', 'Authorization': 'never-forward'}, response=True),
            {'x-codex-turn-state': 'opaque'})
        for headers in ({'Authorization': 'not-a-real-token'}, {'x-codex-turn-state': 'x\r\ny'},
                        {'session-id': 'x' * 8193}, {'session-id': 'a', 'Session-ID': 'b'}):
            with self.assertRaises(GatewayRejected):
                protocol.routing_headers(headers)

    def test_native_command_has_no_mcp_or_native_delegation_and_retains_http(self):
        command = protocol.native_codex_command(1234)
        settings = {command[i + 1].split('=', 1)[0]: json.loads(command[i + 1].split('=', 1)[1])
                    for i, arg in enumerate(command) if arg == '-c'}
        self.assertFalse(any(k.startswith('mcp_servers.') for k in settings))
        self.assertFalse(settings['agents.enabled'])
        self.assertEqual(settings['agents.max_threads'], 1)
        self.assertTrue(settings['features.unified_exec'])
        self.assertTrue(settings['features.shell_tool'])
        self.assertFalse(settings['model_providers.review.requires_openai_auth'])
        self.assertFalse(settings['model_providers.review.supports_websockets'])
        self.assertEqual(settings['model_reasoning_effort'], 'high')
        self.assertEqual(command[command.index('--sandbox') + 1], 'danger-full-access')
        self.assertIn('--ignore-user-config', command)
        self.assertEqual(command[-1], '-')
        with self.assertRaises(ValueError):
            protocol.native_codex_command(1234, effort='xhigh')

    def test_worker_import_and_native_setup_need_no_host_gateway_modules(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / 'job_search' / 'job_reviews'
            package.mkdir(parents=True)
            (package.parent / '__init__.py').write_text('')
            (package / '__init__.py').write_text('')
            for name in ('codex_runtime.py', 'native_client.py'):
                shutil.copyfile(repository / 'job_search' / 'job_reviews' / name, package / name)
            probe = '''import sys
sys.path.insert(0, sys.argv[1])
from job_search.job_reviews.native_client import native_codex_command, routing_headers
from job_search.job_reviews.codex_runtime import bridge_server
command = native_codex_command(1234)
assert 'agents.enabled=false' in command
assert routing_headers({'session-id': 'native'}) == {'session-id': 'native'}
server = bridge_server('/nonexistent-probe.sock', native_codex=True)
server.server_close()
for suffix in ('model_gateway', 'auth_owner', 'reviewer_api', 'native_protocol'):
    assert 'job_search.job_reviews.' + suffix not in sys.modules
print('worker import ok')
'''
            result = subprocess.run([sys.executable, '-I', '-c', probe, str(root)],
                                    cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('worker import ok', result.stdout)
        overlay = (repository / 'Dockerfile.codex-review').read_text()
        self.assertIn('native_client.py', overlay)
        self.assertNotIn('native_protocol.py', overlay)

    def test_transport_reuses_connection_preserves_native_body_and_records_phases(self):
        raw = stream()
        response = Mock(status=200, will_close=False, headers={'x-codex-turn-state': 'opaque'})
        response.getheader.return_value = 'text/event-stream'
        response.read.side_effect = [raw[:1], raw[1:], raw[:1], raw[1:]]
        connection = Mock(); connection.getresponse.return_value = response
        factory = Mock(return_value=connection)
        transport = protocol.NativeTransport(connection_factory=factory)
        body = json.dumps(request()).encode()
        headers = {'session-id': 'stable', 'Authorization': 'host-only-fake'}
        self.assertEqual(transport(body, headers), (200, raw))
        self.assertFalse(transport.observations['connection_reused'])
        self.assertEqual(transport(body, headers), (200, raw))
        self.assertTrue(transport.observations['connection_reused'])
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(connection.connect.call_count, 1)
        self.assertEqual(connection.request.call_args.kwargs['body'], body)
        self.assertEqual(transport.observations['response_headers'], {'x-codex-turn-state': 'opaque'})
        times = [transport.observations[k] for k in
                 ('started', 'connected', 'headers', 'first_byte', 'body_complete', 'validated')]
        self.assertEqual(times, sorted(times))
        transport.close(); connection.close.assert_called_once()

    def test_failed_http_never_exposes_upstream_error_and_drops_connection(self):
        response = Mock(status=401)
        connection = Mock(); connection.getresponse.return_value = response
        transport = protocol.NativeTransport(connection_factory=Mock(return_value=connection))
        self.assertEqual(transport(b'{}', {}), (401, b''))
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_validation_failure_observer_cannot_change_rejection_or_dispatch(self):
        raw = stream(dict(call(), namespace='collaboration'))
        response = Mock(status=200, will_close=False, headers={})
        response.getheader.return_value = 'text/event-stream'
        response.read.side_effect = [raw[:1], raw[1:]]
        connection = Mock(); connection.getresponse.return_value = response
        observer = Mock(side_effect=RuntimeError('diagnostic failure'))
        transport = protocol.NativeTransport(connection_factory=Mock(return_value=connection),
                                             failure_observer=observer)
        with self.assertRaises(GatewayRejected):
            transport(b'{}', {})
        observer.assert_called_once()
        self.assertEqual(observer.call_args.args[0], raw)
        self.assertIsInstance(observer.call_args.args[1], GatewayRejected)
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
