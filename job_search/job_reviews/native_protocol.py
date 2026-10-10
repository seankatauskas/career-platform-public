"""Validated native Codex HTTP protocol; Docker remains the execution boundary.

This deliberately does not translate tools, conversation items, IDs or SSE data.
It is only for credentialless, network-disabled reviewer containers, never a
replacement authorization boundary for host-side execution.
"""
from __future__ import annotations

import json
import http.client
import ssl
import threading
import time

from . import model_gateway as gateway
from .native_client import native_codex_command, REQUEST_HEADERS, RESPONSE_HEADERS
from . import native_client
from .reviewer_api import encode_json

DECLARED_FUNCTIONS = frozenset(('wait', 'request_user_input', 'request_user_input_async'))
OPAQUE_ITEM_METADATA = ('metadata', 'internal_chat_message_metadata_passthrough')
EVENTS = frozenset((
    'response.created', 'response.in_progress', 'response.completed',
    'response.output_item.added', 'response.output_item.done',
    'response.content_part.added', 'response.content_part.done',
    'response.output_text.delta', 'response.output_text.done',
    'response.function_call_arguments.delta', 'response.function_call_arguments.done',
    'response.custom_tool_call_input.delta', 'response.custom_tool_call_input.done',
    'response.reasoning_summary_part.added', 'response.reasoning_summary_part.done',
    'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done',
    'response.reasoning_text.delta', 'response.reasoning_text.done',
))


def forwarding_headers(headers, *, response=False):
    """Host compatibility adapter; the worker helper raises plain ValueError."""
    try:
        return native_client.routing_headers(headers, response=response)
    except ValueError as error:
        raise gateway.GatewayRejected(str(error)) from None


routing_headers = forwarding_headers


def _content(value, *, output=False):
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        raise gateway.GatewayRejected('inline text is required')
    for chunk in value:
        gateway._exact(chunk, ('type', 'text', 'annotations', 'logprobs'))
        if (chunk.get('type') not in (('output_text',) if output else ('input_text', 'output_text')) or
                not isinstance(chunk.get('text'), str) or chunk.get('annotations') not in (None, []) or
                chunk.get('logprobs') not in (None, [])):
            raise gateway.GatewayRejected('external or nontext content is prohibited')


def _item(item, *, output=False, partial=False):
    if not isinstance(item, dict):
        raise gateway.GatewayRejected('invalid native item')
    for field in OPAQUE_ITEM_METADATA:
        if field in item and not isinstance(item[field], dict):
            raise gateway.GatewayRejected('invalid native opaque metadata')
    # Strict JSON decoding/request encoding already rejects nonfinite values and
    # bounds the whole payload. These observed subscription fields are opaque
    # transport metadata and never authorize a capability or alter item content.
    kind = item.get('type', 'message')
    if kind == 'message':
        gateway._exact(item, ('type', 'id', 'role', 'content', 'status', 'phase') + OPAQUE_ITEM_METADATA)
        gateway._validate_phase(item)
        if item.get('role') not in (('assistant',) if output else ('system', 'developer', 'user', 'assistant')):
            raise gateway.GatewayRejected('invalid message role')
        _content(item.get('content', []), output=output)
    elif kind == 'reasoning':
        gateway._exact(item, ('type', 'id', 'summary', 'content', 'encrypted_content', 'status') + OPAQUE_ITEM_METADATA)
        for name in ('summary', 'content'):
            for chunk in item.get(name) or []:
                gateway._exact(chunk, ('type', 'text'))
                if chunk.get('type') not in ('summary_text', 'reasoning_text') or not isinstance(chunk.get('text'), str):
                    raise gateway.GatewayRejected('invalid reasoning continuation')
        if item.get('encrypted_content') is not None and not isinstance(item['encrypted_content'], str):
            raise gateway.GatewayRejected('invalid encrypted reasoning')
    elif kind in ('custom_tool_call', 'function_call'):
        custom = kind == 'custom_tool_call'
        gateway._exact(item, ('type', 'id', 'call_id', 'name', 'namespace', 'status',
            'async', 'caller', 'created_by', 'input' if custom else 'arguments') + OPAQUE_ITEM_METADATA)
        _tool_metadata(item)
        # The subscription endpoint omits namespace for calls in its sole
        # functions namespace. An explicitly different namespace stays invalid.
        if (('namespace' in item and item['namespace'] != 'functions') or
                item.get('name') != ('exec' if custom else 'wait')):
            raise gateway.GatewayRejected('unapproved native tool', reason='unsupported_tool')
        if ('call_id' in item or not partial) and (not isinstance(item.get('call_id'), str) or not item['call_id']):
            raise gateway.GatewayRejected('missing native call identity')
        input_key = 'input' if custom else 'arguments'
        value = item.get(input_key)
        if (input_key in item or not partial) and not isinstance(value, str):
            raise gateway.GatewayRejected('invalid native tool input')
        if not custom and not partial:
            gateway._json_object(value, gateway.MAX_REQUEST)
    elif not output and kind in ('custom_tool_call_output', 'function_call_output'):
        gateway._exact(item, ('type', 'id', 'call_id', 'output', 'status', 'caller', 'created_by') + OPAQUE_ITEM_METADATA)
        _tool_metadata(item)
        if not isinstance(item.get('call_id'), str) or not item['call_id']:
            raise gateway.GatewayRejected('missing native result identity')
        _content(item.get('output'))
    else:
        raise gateway.GatewayRejected('unsupported native capability', reason='unsupported_capability')
    if output and not partial and item.get('status') not in (None, 'completed'):
        raise gateway.GatewayRejected('incomplete native output', reason='incomplete_stream')


def _tool_metadata(item):
    # Documented Responses metadata is preserved, not interpreted as another
    # tool capability. The actual item type, namespace and name remain gated.
    if 'async' in item and type(item['async']) is not bool:
        raise gateway.GatewayRejected('invalid native async metadata')
    if 'created_by' in item and not isinstance(item['created_by'], str):
        raise gateway.GatewayRejected('invalid native actor metadata')
    caller = item.get('caller')
    if caller is not None:
        gateway._exact(caller, ('type', 'caller_id'))
        if caller.get('type') == 'direct':
            if 'caller_id' in caller:
                raise gateway.GatewayRejected('invalid direct caller metadata')
        elif caller.get('type') == 'program':
            if not isinstance(caller.get('caller_id'), str) or not caller['caller_id']:
                raise gateway.GatewayRejected('invalid program caller metadata')
        else:
            raise gateway.GatewayRejected('invalid native caller metadata')


def _declarations(item):
    gateway._exact(item, ('type', 'id', 'role', 'tools'))
    if item.get('role') != 'developer' or not isinstance(item.get('tools'), list):
        raise gateway.GatewayRejected('invalid native declarations')
    if len(item['tools']) != 1:
        raise gateway.GatewayRejected('only local functions namespace is permitted')
    namespace = item['tools'][0]
    gateway._exact(namespace, ('type', 'name', 'description', 'tools'))
    if namespace.get('type') != 'namespace' or namespace.get('name') != 'functions':
        raise gateway.GatewayRejected('hosted or collaboration tools are prohibited')
    names = set()
    for tool in namespace.get('tools', []):
        gateway._exact(tool, ('type', 'name', 'description', 'format', 'strict', 'parameters'))
        name = tool.get('name')
        if name in names or not ((name == 'exec' and tool.get('type') == 'custom') or
                                (name in DECLARED_FUNCTIONS and tool.get('type') == 'function')):
            raise gateway.GatewayRejected('unapproved native declaration')
        names.add(name)
    if 'exec' not in names:
        raise gateway.GatewayRejected('native execution declaration is required')


def validate_request(value, model=gateway.MODEL, effort='high'):
    """Validate without changing the native append-only conversation prefix."""
    try:
        encode_json(value, gateway.MAX_REQUEST)
    except ValueError:
        raise gateway.GatewayRejected('invalid finite native request') from None
    gateway._exact(value, ('model', 'instructions', 'input', 'tools', 'tool_choice',
        'parallel_tool_calls', 'reasoning', 'store', 'stream', 'include',
        'prompt_cache_key', 'client_metadata', 'text'))
    if model != gateway.MODEL or effort != 'high':
        raise gateway.GatewayRejected('parity requires approved Astra high')
    gateway._exact(value.get('reasoning', {}), ('effort', 'summary', 'context'))
    if value.get('model') != model or value.get('reasoning', {}).get('effort') != effort:
        raise gateway.GatewayRejected('model and reasoning must match approved profile')
    if value.get('store') is not False or value.get('stream') is not True:
        raise gateway.GatewayRejected('nonpersistent streaming is required')
    if value['reasoning'].get('context') not in (None, 'all_turns'):
        raise gateway.GatewayRejected('unsupported reasoning context')
    if value.get('include', []) not in ([], ['reasoning.encrypted_content']):
        raise gateway.GatewayRejected('unsupported native inclusion')
    if value.get('instructions') is not None and not isinstance(value['instructions'], str):
        raise gateway.GatewayRejected('inline instructions are required')
    if not isinstance(value.get('input'), list) or value.get('tools') not in (None, []):
        raise gateway.GatewayRejected('only inline native tools and input are permitted')
    if value.get('tool_choice', 'auto') not in ('auto', 'none'):
        raise gateway.GatewayRejected('unsupported tool choice')
    declarations = 0
    for item in value['input']:
        if isinstance(item, dict) and item.get('type') == 'additional_tools':
            _declarations(item)
            declarations += 1
        else:
            _item(item)
    if declarations != 1:
        raise gateway.GatewayRejected('one native tool registry is required')
    return value


def completion(data):
    """Validate native event identities and return the unchanged final object."""
    final, done, added = None, {}, {}
    for event in gateway._upstream_events(data):
        event_type = event.get('type')
        if final is not None:
            raise gateway.GatewayRejected('events followed completion', reason='output_consistency')
        if event_type not in EVENTS:
            raise gateway.GatewayRejected('unsupported native event', reason='unsupported_capability')
        response = event.get('response', {})
        if not isinstance(response, dict) or not isinstance(response.get('output', []), list):
            raise gateway.GatewayRejected('invalid native response')
        for item in response.get('output', []):
            _item(item, output=True, partial=event_type != 'response.completed')
        if event_type in ('response.output_item.added', 'response.output_item.done'):
            index, item = event.get('output_index'), event.get('item')
            target = added if event_type.endswith('added') else done
            if type(index) is not int or index < 0 or index in target:
                raise gateway.GatewayRejected('invalid native output index', reason='output_consistency')
            _item(item, output=True, partial=target is added)
            target[index] = item
        if event_type == 'response.completed':
            if response.get('status') != 'completed' or 'output' not in response:
                raise gateway.GatewayRejected('invalid native completion', reason='incomplete_stream')
            final = response
    if final is None:
        raise gateway.GatewayRejected('native stream did not complete', reason='incomplete_stream')
    output = final['output']
    # Codex Responses Lite can omit the duplicated final output array while
    # retaining every completed item in the preceding SSE events. Resolve that
    # representation only for validation/telemetry; forward the original bytes.
    if not output and done:
        if sorted(done) != list(range(len(done))):
            raise gateway.GatewayRejected('native completed indexes are not contiguous', reason='output_consistency')
        output = [done[index] for index in range(len(done))]
        final = dict(final, output=output)
    if any(index >= len(output) or output[index] != item for index, item in done.items()):
        raise gateway.GatewayRejected('native completed outputs disagree', reason='output_consistency')
    for index, item in added.items():
        if index >= len(output) or any(item[k] != output[index].get(k)
                for k in ('type', 'id', 'call_id', 'name', 'namespace') if k in item):
            raise gateway.GatewayRejected('native added output differs', reason='output_consistency')
    for field in ('id', 'call_id'):
        ids = [item[field] for item in output if field in item]
        if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
            raise gateway.GatewayRejected('duplicate native output identity', reason='output_consistency')
    return final


def validate_response(data):
    completion(data)
    return data


def schema_diagnostic(data, error):
    """Bounded structural diagnostics, with no text, identifiers or credentials.

    Field names and enum values are allowlisted so malicious source text cannot
    enter diagnostic records by becoming an object key or an event type.
    """
    keys = frozenset(('type', 'id', 'call_id', 'name', 'namespace', 'status', 'input',
        'arguments', 'role', 'content', 'phase', 'summary', 'encrypted_content',
        'async', 'caller', 'created_by', 'text', 'annotations', 'logprobs', 'output',
        'usage', 'object', 'sequence_number', 'item', 'output_index', 'response',
        'delta', 'item_id', 'content_index', 'summary_index', 'part', 'agent',
        'obfuscation', 'signature', 'end_turn', 'refusal', 'metadata',
        'internal_chat_message_metadata_passthrough'))
    item_types = frozenset(('message', 'reasoning', 'custom_tool_call', 'function_call',
        'custom_tool_call_output', 'function_call_output', 'output_text', 'input_text',
        'summary_text', 'reasoning_text', 'refusal'))

    def shape(value):
        if not isinstance(value, dict):
            return {'object': False}
        result = {'keys': sorted(key for key in value if key in keys),
                  'unknown_key_count': sum(key not in keys for key in value)}
        kind = value.get('type')
        if isinstance(kind, str):
            result['type'] = kind if kind in item_types else 'unknown'
        for key in ('input', 'arguments', 'call_id', 'encrypted_content',
                    'metadata', 'internal_chat_message_metadata_passthrough'):
            if key in value:
                result[key + '_type'] = ('null' if value[key] is None else
                    'string' if isinstance(value[key], str) else
                    'list' if isinstance(value[key], list) else
                    'object' if isinstance(value[key], dict) else 'other')
        return result

    reason = getattr(error, 'reason', None)
    result = {'reason': reason if reason in gateway.RESPONSE_REJECTION_REASONS else 'validation',
              'validation_code': {
                  'unsupported model request fields': 'unexpected_fields',
                  'invalid native tool input': 'invalid_tool_input',
                  'missing native call identity': 'missing_call_identity',
                  'invalid message role': 'invalid_message_role',
                  'external or nontext content is prohibited': 'nontext_content',
                  'invalid reasoning continuation': 'invalid_reasoning',
                  'invalid encrypted reasoning': 'invalid_encrypted_reasoning',
                  'invalid native response': 'invalid_response',
                  'invalid native completion': 'invalid_completion',
                  'unapproved native tool': 'unapproved_tool',
              }.get(str(error), 'other'),
              'response_bytes': len(data) if isinstance(data, bytes) else 0,
              'events': [], 'truncated': False, 'event_count': 0, 'delta_count': 0}
    try:
        for index, event in enumerate(gateway._upstream_events(data)):
            result['event_count'] += 1
            kind = event.get('type')
            if kind in EVENTS and kind.endswith('.delta'):
                result['delta_count'] += 1
                continue
            entry = {'event': kind if kind in EVENTS else 'unknown', 'shape': shape(event)}
            if 'item' in event:
                entry['item'] = shape(event['item'])
                item = event['item']
                if isinstance(item, dict):
                    for field in ('content', 'summary'):
                        if isinstance(item.get(field), list):
                            entry[field] = [shape(chunk) for chunk in item[field][:10]]
            response = event.get('response')
            if isinstance(response, dict):
                entry['response'] = shape(response)
                if isinstance(response.get('output'), list):
                    entry['output_count'] = len(response['output'])
                    entry['output'] = [shape(item) for item in response['output'][:20]]
            if len(result['events']) < 100:
                result['events'].append(entry)
            else:
                result['truncated'] = True
                if kind == 'response.completed':
                    result['events'][-1] = entry
    except (ValueError, TypeError, AttributeError):
        result['framing_error'] = True
    return result



class NativeTransport:
    """Bounded HTTP-only connection reuse, with per-caller phase observations.

    Streaming is still buffered at the outer existing gateway boundary at this
    boundary. These timings explicitly measure first byte separately.
    """

    def __init__(self, *, timeout=600, connection_factory=None, failure_observer=None):
        self.timeout = timeout
        self._factory = connection_factory or http.client.HTTPSConnection
        self._failure_observer = failure_observer
        self._connection = None
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def observations(self):
        return getattr(self._local, 'observations', {})

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __call__(self, body, headers):
        observations = {'started': time.monotonic_ns()}
        self._local.observations = observations
        with self._lock:
            try:
                reused = self._connection is not None
                if not reused:
                    self._connection = self._factory(gateway.UPSTREAM_HOST,
                        timeout=self.timeout, context=ssl.create_default_context())
                    self._connection.connect()
                observations['connection_reused'] = reused
                observations['connected'] = time.monotonic_ns()
                self._connection.request('POST', gateway.UPSTREAM_PATH, body=body,
                    headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                             'OpenAI-Beta': 'responses=experimental', **headers})
                response = self._connection.getresponse()
                observations['headers'] = time.monotonic_ns()
                if response.status != 200:
                    self._close()
                    return response.status, b''
                observations['response_headers'] = forwarding_headers(response.headers, response=True)
                content_type = response.getheader('Content-Type')
                if content_type is not None and content_type.split(';')[0].strip().lower() != 'text/event-stream':
                    raise gateway.GatewayRejected('native response is not SSE', reason='framing')
                first = response.read(1)
                observations['first_byte'] = time.monotonic_ns()
                data = first + response.read(gateway.MAX_RESPONSE + 1)
                observations['body_complete'] = time.monotonic_ns()
                if len(data) > gateway.MAX_RESPONSE:
                    raise gateway.GatewayRejected('native response exceeds bound', reason='size')
                try:
                    validate_response(data)
                except gateway.GatewayRejected as error:
                    if self._failure_observer is not None:
                        try:
                            self._failure_observer(data, error)
                        except Exception:
                            pass
                    raise
                observations['validated'] = time.monotonic_ns()
                if response.will_close:
                    self._close()
                return 200, data
            except BaseException:
                self._close()
                raise
