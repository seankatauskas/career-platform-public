"""Fixed ChatGPT Responses gateway. No arbitrary URLs, hosted tools or API fallback."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import http.client
import json
import os
from pathlib import Path
import re
import socketserver
import ssl
import threading
import time

from .auth_owner import AuthenticationUnavailable, CODEX_VERSION
from .reviewer_api import decode_json, encode_json
from .reviewer_mcp import tools_for

MODEL = 'gpt-6-astra'
EFFORT = 'high'
UPSTREAM_HOST = 'chatgpt.com'
UPSTREAM_PATH = '/backend-api/codex/responses'
MAX_REQUEST = 4 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
REVIEW_TOOLS = frozenset('mcp__review__' + tool['name'] for tool in tools_for())
# These pinned CLI built-ins cannot expose anything outside its empty container,
# but are removed from inference so the model receives review tools exclusively.
OMITTED_TOOLS = frozenset(('view_image', 'get_goal', 'create_goal', 'update_goal', 'request_user_input'))


RESPONSE_REJECTION_REASONS = frozenset((
    'upstream_failed', 'incomplete_stream', 'framing', 'output_consistency',
    'unsupported_tool', 'unsupported_capability', 'size', 'validation',
))
RESPONSE_REJECTION_COUNTERS = tuple(
    'gateway_response_rejected_' + reason + '_count' for reason in sorted(RESPONSE_REJECTION_REASONS))


class GatewayRejected(ValueError):
    def __init__(self, message, *, reason='validation'):
        super().__init__(message)
        self.reason = reason if type(reason) is str and reason in RESPONSE_REJECTION_REASONS else 'validation'


def _response_rejection_metrics(error):
    # Never classify from exception text or upstream content. Recheck the fixed
    # enum at the telemetry boundary even if an exception attribute was changed.
    reason = getattr(error, 'reason', None) if isinstance(error, GatewayRejected) else None
    if type(reason) is not str or reason not in RESPONSE_REJECTION_REASONS:
        reason = 'validation'
    return {'gateway_response_rejected_count': 1,
            'gateway_response_rejected_' + reason + '_count': 1}


def _exact(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise GatewayRejected('unsupported model request fields')


def _json_object(raw, maximum):
    """Strict shared JSON grammar with the gateway's separate byte budgets."""
    try:
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        if not isinstance(raw, bytes):
            raise ValueError()
        return decode_json(raw, maximum)
    except (ValueError, UnicodeError, RecursionError):
        raise GatewayRejected('invalid finite JSON object',
            reason='size' if isinstance(raw, bytes) and len(raw) > maximum else 'validation') from None


def _upstream_events(data):
    if not isinstance(data, bytes) or len(data) > MAX_RESPONSE:
        raise GatewayRejected('model response exceeded its size bound',
                              reason='size' if isinstance(data, bytes) else 'validation')
    name, payload, ended = None, [], False
    lines = data.replace(b'\r\n', b'\n').split(b'\n')
    for line in lines[:-1] if data.endswith(b'\n') else lines:
        if line.startswith(b':'):
            continue
        if not line:
            if name is None and not payload:
                continue
            if ended or not payload:
                raise GatewayRejected('invalid model event framing', reason='framing')
            raw = b'\n'.join(payload)
            if raw == b'[DONE]':
                if name is not None:
                    raise GatewayRejected('invalid model stream terminator', reason='framing')
                ended = True
            else:
                event = _json_object(raw, MAX_RESPONSE)
                if name is not None and name != str(event.get('type')).encode():
                    raise GatewayRejected('model event name does not match its payload', reason='framing')
                yield event
            name, payload = None, []
        elif line.startswith(b'event:') and name is None and not payload:
            name = line[6:].strip()
        elif line.startswith(b'data:'):
            payload.append(line[5:].lstrip(b' '))
        else:
            raise GatewayRejected('invalid model event framing', reason='framing')
    if name is not None or payload:
        raise GatewayRejected('incomplete model event framing', reason='framing')


def _validate_phase(item):
    if 'phase' in item and (item.get('role') != 'assistant' or
                            item['phase'] not in (None, 'commentary', 'final_answer')):
        raise GatewayRejected('invalid assistant message phase')


def _scoped_tools(kind, rubric_version, purpose):
    if purpose not in ('detailed', 'screening') or purpose == 'screening' and kind != 'primary':
        raise GatewayRejected('invalid review inference purpose')
    return tools_for(kind, rubric_version, purpose)


def validate_request(value, model=MODEL, effort=EFFORT, *, kind='primary', rubric_version='job-review-v1', purpose='detailed'):
    schemas = _scoped_tools(kind, rubric_version, purpose)
    allowed_tools = frozenset('mcp__review__' + tool['name'] for tool in schemas)
    try:
        encode_json(value, MAX_REQUEST)
    except ValueError:
        raise GatewayRejected('invalid finite model request') from None
    _exact(value, ('model', 'instructions', 'input', 'tools', 'tool_choice', 'parallel_tool_calls',
                   'reasoning', 'store', 'stream', 'include', 'prompt_cache_key',
                   'client_metadata', 'text'))
    _exact(value.get('reasoning', {}), ('effort', 'summary', 'context'))
    if value.get('model') != model or value.get('reasoning', {}).get('effort') != effort:
        raise GatewayRejected('model and reasoning must match the approved configuration')
    if value.get('store') is not False or value.get('stream') is not True:
        raise GatewayRejected('review inference requires nonpersistent streaming responses')
    _exact(value.get('reasoning', {}), ('effort', 'summary', 'context'))
    if value['reasoning'].get('context') not in (None, 'all_turns'):
        raise GatewayRejected('unsupported reasoning context')
    if value.get('include', []) not in ([], ['reasoning.encrypted_content']):
        raise GatewayRejected('unsupported response inclusion')
    if value.get('instructions') is not None and not isinstance(value['instructions'], str) or not isinstance(value.get('input'), list):
        raise GatewayRejected('inline instructions and input are required')
    source_input = []
    for original in value['input']:
        item = original
        if not isinstance(item, dict):
            raise GatewayRejected('invalid model input')
        kind = item.get('type', 'message')
        if kind == 'additional_tools':
            # Pinned Codex presents Code Mode and other native tools here. They
            # never reach the model. The gateway supplies the scoped schemas.
            continue
        if kind == 'custom_tool_call':
            _exact(item, ('type', 'id', 'call_id', 'name', 'namespace', 'input', 'status'))
            if item.get('name') != 'exec' or item.get('namespace') != 'functions':
                raise GatewayRejected('unapproved native tool continuation')
            match = re.fullmatch(r'text\(await tools\.(mcp__review__review_[a-z]+)\((.*)\)\);', item.get('input', ''), re.DOTALL)
            if not match or match[1] not in allowed_tools:
                raise GatewayRejected('arbitrary code execution is prohibited')
            arguments = _json_object(match[2], MAX_REQUEST)
            item = {'type': 'function_call', 'name': match[1], 'arguments': json.dumps(arguments),
                    'call_id': item['call_id'], **({'id': item['id']} if 'id' in item else {})}
            kind = 'function_call'
        elif kind == 'custom_tool_call_output':
            _exact(item, ('type', 'id', 'call_id', 'output', 'status'))
            item = dict(item, type='function_call_output')
            # Codex's local ctco_ item ID belongs to the native custom-tool type.
            # The upstream function result is linked by call_id, not that ID.
            item.pop('id', None)
            kind = 'function_call_output'
        if kind == 'message':
            _exact(item, ('type', 'role', 'content', 'id', 'status', 'phase'))
            _validate_phase(item)
            if item.get('role') not in ('system', 'developer', 'user', 'assistant'):
                raise GatewayRejected('invalid message role')
            content = item.get('content')
            if isinstance(content, str):
                source_input.append(item)
                continue
            if not isinstance(content, list):
                raise GatewayRejected('inline text is required')
            for chunk in content:
                _exact(chunk, ('type', 'text', 'annotations', 'logprobs'))
                if chunk.get('type') not in ('input_text', 'output_text') or not isinstance(chunk.get('text'), str):
                    raise GatewayRejected('external or nontext input is prohibited')
                if chunk.get('annotations') not in (None, []) or chunk.get('logprobs') not in (None, []):
                    raise GatewayRejected('external annotations are prohibited')
        elif kind == 'function_call':
            _exact(item, ('type', 'name', 'arguments', 'call_id', 'id', 'status'))
            if item.get('name') not in allowed_tools:
                raise GatewayRejected('only scoped review calls are permitted')
            _json_object(item.get('arguments'), MAX_REQUEST)
        elif kind == 'function_call_output':
            _exact(item, ('type', 'call_id', 'output', 'id', 'status'))
            output = item.get('output')
            if isinstance(output, list):
                for chunk in output:
                    _exact(chunk, ('type', 'text'))
                    if chunk.get('type') not in ('input_text', 'output_text') or not isinstance(chunk.get('text'), str):
                        raise GatewayRejected('review tool output must be inline text')
                item = dict(item, output='\n'.join(c['text'] for c in output))
            elif not isinstance(output, str):
                raise GatewayRejected('review tool output must be inline text')
        elif kind == 'reasoning':
            _exact(item, ('type', 'id', 'summary', 'encrypted_content', 'content'))
            for field in ('summary', 'content'):
                for chunk in item.get(field) or []:
                    _exact(chunk, ('type', 'text'))
                    if chunk.get('type') not in ('summary_text', 'reasoning_text') or not isinstance(chunk.get('text'), str):
                        raise GatewayRejected('invalid reasoning continuation')
        else:
            raise GatewayRejected('references and hosted tool inputs are prohibited')
        source_input.append(item)
    tools = value.get('tools', [])
    if not isinstance(tools, list):
        raise GatewayRejected('invalid review tools')
    for tool in tools:
        _exact(tool, ('type', 'name', 'description', 'parameters', 'strict'))
        if tool.get('type') != 'function':
            raise GatewayRejected('hosted and custom tools are prohibited')
        if tool.get('name') in OMITTED_TOOLS:
            continue
        if tool.get('name') not in allowed_tools:
            raise GatewayRejected('unapproved tool')
    if value.get('tool_choice', 'auto') not in ('auto', 'none'):
        raise GatewayRejected('unsupported tool choice')
    # Canonical schemas apply to both native additional_tools and flat registries.
    # Client descriptions and JSON Schema references never reach inference.
    permitted = [{'type': 'function', 'name': 'mcp__review__' + t['name'],
                  'description': t['description'], 'parameters': t['inputSchema'], 'strict': False} for t in schemas]
    result = dict(value, tools=permitted, input=source_input)
    # Host/client metadata and cache keys are not needed for evidence assessment.
    result.pop('client_metadata', None)
    result.pop('prompt_cache_key', None)
    return result


def native_response(data, *, kind='primary', rubric_version='job-review-v1', purpose='detailed'):
    """Translate only validated scoped calls into fixed native Code Mode calls.

    The model cannot supply JavaScript. Arguments are parsed JSON and re-encoded
    inside one generated expression; only assignment-scoped MCP names exist.
    """
    completed = _validated_completion(data, kind=kind, rubric_version=rubric_version, purpose=purpose)

    def convert(item):
        if item['type'] != 'function_call':
            return item
        args = _json_object(item['arguments'], MAX_REQUEST)
        code = 'text(await tools.' + item['name'] + '(' + json.dumps(args, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '));'
        return {k: v for k, v in dict(item, type='custom_tool_call', name='exec', namespace='functions', input=code).items()
                if k != 'arguments'}

    # Reconstruct the entire stream. No upstream delta or custom event can alter
    # the generated native call after validation, including unknown future events.
    final = {'id': completed.get('id', 'review_response'), 'object': 'response',
             'status': 'completed', 'output': [convert(item) for item in completed['output']]}
    events = [{'type': 'response.created', 'response': dict(final, status='in_progress', output=[])}]
    events.extend({'type': 'response.output_item.done', 'output_index': i, 'item': item}
                  for i, item in enumerate(final['output']))
    events.append({'type': 'response.completed', 'response': final})
    return ''.join('data: ' + json.dumps(event, allow_nan=False, separators=(',', ':')) + '\n\n' for event in events).encode()


def validate_response(data, *, kind='primary', rubric_version='job-review-v1', purpose='detailed'):
    """Validate the complete bounded event stream before allowing CLI dispatch.

    Buffering prevents an early malicious tool call from executing before a later
    completion event is checked. This deliberately trades token streaming for a
    small, auditable execution boundary.
    """
    _validated_completion(data, kind=kind, rubric_version=rubric_version, purpose=purpose)
    return data


def _validate_output_item(item, allowed_tools):
    if not isinstance(item, dict):
        raise GatewayRejected('invalid completion item')
    kind = item.get('type')
    if item.get('status') not in (None, 'completed'):
        raise GatewayRejected('incomplete model output item', reason='incomplete_stream')
    if kind == 'function_call':
        _exact(item, ('type', 'id', 'call_id', 'name', 'arguments', 'status'))
        if item.get('name') not in allowed_tools:
            raise GatewayRejected('model requested an unapproved tool', reason='unsupported_tool')
        if not isinstance(item.get('call_id'), str) or not item['call_id']:
            raise GatewayRejected('invalid model tool call identifier', reason='output_consistency')
        _json_object(item.get('arguments'), MAX_REQUEST)
    elif kind == 'message':
        _exact(item, ('type', 'id', 'role', 'status', 'content', 'phase'))
        _validate_phase(item)
        if item.get('role') != 'assistant' or not isinstance(item.get('content'), list):
            raise GatewayRejected('invalid assistant message')
        for chunk in item['content']:
            _exact(chunk, ('type', 'text', 'annotations', 'logprobs'))
            if chunk.get('type') != 'output_text' or not isinstance(chunk.get('text'), str):
                raise GatewayRejected('nontext model output', reason='unsupported_capability')
            if chunk.get('annotations') not in (None, []) or chunk.get('logprobs') not in (None, []):
                raise GatewayRejected('unsupported output references', reason='unsupported_capability')
    elif kind == 'reasoning':
        _exact(item, ('type', 'id', 'summary', 'content', 'encrypted_content', 'status'))
    else:
        raise GatewayRejected('model requested an unsupported capability', reason='unsupported_capability')


def _validated_completion(data, *, kind, rubric_version, purpose='detailed'):
    allowed_tools = frozenset('mcp__review__' + tool['name'] for tool in _scoped_tools(kind, rubric_version, purpose))
    completed, done, added = None, {}, {}
    allowed_events = {'response.created', 'response.in_progress', 'response.completed',
        'response.output_item.added', 'response.output_item.done', 'response.content_part.added',
        'response.content_part.done', 'response.output_text.delta', 'response.output_text.done',
        'response.function_call_arguments.delta', 'response.function_call_arguments.done',
        'response.reasoning_summary_part.added', 'response.reasoning_summary_part.done',
        'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done',
        'response.reasoning_text.delta', 'response.reasoning_text.done'}
    for event in _upstream_events(data):
        if completed is not None:
            raise GatewayRejected('model events followed completion', reason='output_consistency')
        if event.get('type') not in allowed_events:
            reason = ('upstream_failed' if event.get('type') in ('response.failed', 'error') else
                      'incomplete_stream' if event.get('type') == 'response.incomplete' else 'unsupported_capability')
            raise GatewayRejected('model response failed', reason=reason)
        items = [event.get('item')]
        response = event.get('response') or {}
        if not isinstance(response, dict) or not isinstance(response.get('output', []), list):
            raise GatewayRejected('invalid model response object')
        items.extend(response.get('output') or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get('type')
            if kind == 'function_call':
                if item.get('name') not in allowed_tools:
                    raise GatewayRejected('model requested an unapproved tool', reason='unsupported_tool')
            elif kind not in ('message', 'reasoning'):
                raise GatewayRejected('model requested an unsupported capability', reason='unsupported_capability')
        if event.get('type') == 'response.output_item.done':
            index = event.get('output_index')
            if type(index) is not int or index < 0 or index in done:
                raise GatewayRejected('invalid or duplicate completed output index', reason='output_consistency')
            _validate_output_item(event.get('item'), allowed_tools)
            done[index] = event['item']
        elif event.get('type') == 'response.output_item.added':
            index = event.get('output_index')
            if type(index) is not int or index < 0 or index in added or not isinstance(event.get('item'), dict):
                raise GatewayRejected('invalid or duplicate added output index', reason='output_consistency')
            added[index] = event['item']
        if event.get('type') == 'response.completed':
            if response.get('status') != 'completed' or not isinstance(response.get('output'), list):
                raise GatewayRejected('invalid model completion')
            for item in response['output']:
                _validate_output_item(item, allowed_tools)
            completed = response
    if completed is None:
        raise GatewayRejected('model stream did not complete', reason='incomplete_stream')
    output = completed['output']
    if output:
        if any(index >= len(output) or output[index] != item for index, item in done.items()):
            raise GatewayRejected('completed model outputs disagree', reason='output_consistency')
    elif done:
        if sorted(done) != list(range(len(done))):
            raise GatewayRejected('completed output indexes are not contiguous', reason='output_consistency')
        output = [done[index] for index in range(len(done))]
    for index, item in added.items():
        if index >= len(output) or any(item.get(key) != output[index].get(key)
                                       for key in ('type', 'id', 'call_id', 'name')):
            raise GatewayRejected('added model output did not complete consistently', reason='output_consistency')
    for field in ('id', 'call_id'):
        identifiers = [item[field] for item in output if field in item]
        if any(not isinstance(value, str) or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
            raise GatewayRejected('invalid or duplicate model output identifier', reason='output_consistency')
    return dict(completed, output=output)


def subscription_transport(body, headers, *, timeout=600, kind='primary', rubric_version='job-review-v1', purpose='detailed'):
    connection = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=timeout, context=ssl.create_default_context())
    try:
        connection.request('POST', UPSTREAM_PATH, body=body, headers={
            **headers, 'Content-Type': 'application/json', 'Accept': 'text/event-stream',
            'OpenAI-Beta': 'responses=experimental', 'originator': 'codex_cli_rs',
            'User-Agent': 'codex_cli_rs/' + CODEX_VERSION})
        response = connection.getresponse()
        if response.status != 200:
            # Never forward account/error payloads or redirects to the reviewer.
            return response.status, b''
        content_type = response.getheader('Content-Type')
        if content_type is not None and content_type.split(';')[0].strip().lower() != 'text/event-stream':
            raise GatewayRejected('upstream did not return an event stream', reason='framing')
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise GatewayRejected('model response exceeded its size bound', reason='size')
        # The subscription endpoint can omit Content-Type. Never sniff a prefix:
        # accept only a completely framed, validated, completed response stream.
        validate_response(data, kind=kind, rubric_version=rubric_version, purpose=purpose)
        return 200, data
    finally:
        connection.close()


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 4


@contextmanager
def gateway_server(socket_path, auth_owner, *, model=MODEL, reasoning_effort=EFFORT,
                   uid=None, gid=None, transport=None, kind='primary', rubric_version='job-review-v1',
                   telemetry_callback=None, purpose='detailed'):
    _scoped_tools(kind, rubric_version, purpose)
    if transport is None:
        transport = lambda body, headers: subscription_transport(body, headers, kind=kind, rubric_version=rubric_version, purpose=purpose)
    path = Path(socket_path)
    if path.exists() or path.is_symlink():
        raise ValueError('gateway socket path already exists')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    slots = threading.BoundedSemaphore(4)

    def emit_telemetry(event):
        if telemetry_callback is not None:
            try:
                telemetry_callback(event)
            except Exception:
                # Observability must never affect authorization or dispatch.
                pass

    def request_upstream(body, headers):
        # Only explicit numeric counters cross this callback. Neither arbitrary
        # usage metadata nor response/error text is allowed into host telemetry.
        event = {'request_count': 1}
        started = time.monotonic()
        emit_telemetry({'request_started_count': 1})
        try:
            status, data = transport(body, headers)
            if type(status) is int and 100 <= status <= 599:
                event['upstream_status'] = status
            event['upstream_duration_ms'] = round((time.monotonic() - started) * 1000, 3)
            if status == 200:
                completed = _validated_completion(data, kind=kind, rubric_version=rubric_version, purpose=purpose)
                usage = completed.get('usage')
                if isinstance(usage, dict):
                    counts = {key: usage.get(key) for key in ('input_tokens', 'output_tokens', 'total_tokens')}
                    for group, field, label in (('input_tokens_details', 'cached_tokens', 'cached_input_tokens'),
                                                ('output_tokens_details', 'reasoning_tokens', 'reasoning_tokens')):
                        details = usage.get(group)
                        if isinstance(details, dict):
                            counts[label] = details.get(field)
                    event.update({key: value for key, value in counts.items()
                                  if type(value) is int and 0 <= value < 2**63})
            return status, data
        except (GatewayRejected, ValueError, TypeError, KeyError, UnicodeError) as error:
            event.update(_response_rejection_metrics(error))
            raise
        except (OSError, http.client.HTTPException):
            event['upstream_transport_failed_count'] = 1
            raise
        finally:
            event.setdefault('upstream_duration_ms', round((time.monotonic() - started) * 1000, 3))
            emit_telemetry(event)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(610)

        def send_result(self, status, body, content='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not slots.acquire(blocking=False):
                emit_telemetry({'gateway_busy_count': 1})
                self.send_result(503, b'{"error":"review gateway busy"}')
                return
            phase = 'request'
            try:
                if self.path != '/v1/responses' or self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise GatewayRejected('invalid gateway request')
                if self.headers.get('Authorization') or self.headers.get('ChatGPT-Account-ID'):
                    raise GatewayRejected('reviewers must not supply credentials')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_REQUEST:
                    raise GatewayRejected('request size is invalid')
                body = self.rfile.read(length)
                if len(body) != length:
                    raise GatewayRejected('incomplete request')
                value = validate_request(_json_object(body, MAX_REQUEST), model, reasoning_effort, kind=kind, rubric_version=rubric_version, purpose=purpose)
                body = encode_json(value, MAX_REQUEST)
                phase = 'upstream'
                status, data = request_upstream(body, auth_owner._request_headers())
                if status == 401:
                    status, data = request_upstream(body, auth_owner._request_headers(refresh=True))
                if status != 200:
                    self.send_result(503, b'{"error":"subscription inference unavailable"}')
                    return
                phase = 'response'
                data = native_response(data, kind=kind, rubric_version=rubric_version, purpose=purpose)
                phase = 'delivery'
                self.send_result(200, data, 'text/event-stream')
            except (GatewayRejected, ValueError, TypeError, KeyError, UnicodeError) as error:
                if phase == 'request':
                    emit_telemetry({'gateway_request_rejected_count': 1})
                elif phase == 'response':
                    emit_telemetry(_response_rejection_metrics(error))
                self.send_result(400, b'{"error":"review model request rejected"}')
            except AuthenticationUnavailable:
                emit_telemetry({'authentication_failed_count': 1})
                self.send_result(503, b'{"error":"subscription inference unavailable"}')
            except (OSError, http.client.HTTPException):
                self.send_result(503, b'{"error":"subscription inference unavailable"}')
            finally:
                slots.release()

        def do_GET(self):
            self.send_result(404, b'{"error":"unknown review model endpoint"}')

        do_CONNECT = do_GET

    server = _UnixServer(str(path), Handler)
    os.chmod(path, 0o600)
    if uid is not None and os.getuid() == 0:
        os.chown(path, uid, gid if gid is not None else uid)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'socket': str(path), 'model': model, 'reasoning_effort': reasoning_effort,
               'auth_mode': 'chatgpt', 'protocol_version': CODEX_VERSION}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)
