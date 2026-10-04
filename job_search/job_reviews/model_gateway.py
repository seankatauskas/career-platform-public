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

from .auth_owner import AuthenticationUnavailable, CODEX_VERSION
from .reviewer_api import decode_json, encode_json

MODEL = 'gpt-6-astra'
EFFORT = 'high'
UPSTREAM_HOST = 'chatgpt.com'
UPSTREAM_PATH = '/backend-api/codex/responses'
MAX_REQUEST = 4 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
REVIEW_TOOLS = frozenset('mcp__review__review_' + name for name in ('assignment', 'context', 'job', 'assessment'))
# These pinned CLI built-ins cannot expose anything outside its empty container,
# but are removed from inference so the model receives review tools exclusively.
OMITTED_TOOLS = frozenset(('view_image', 'get_goal', 'create_goal', 'update_goal', 'request_user_input'))


class GatewayRejected(ValueError):
    pass


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
        raise GatewayRejected('invalid finite JSON object') from None


def _upstream_events(data):
    if not isinstance(data, bytes) or len(data) > MAX_RESPONSE:
        raise GatewayRejected('model response exceeded its size bound')
    for line in data.splitlines():
        if not line.startswith(b'data:'):
            continue
        raw = line[5:].strip()
        if raw != b'[DONE]':
            yield _json_object(raw, MAX_RESPONSE)


def validate_request(value, model=MODEL, effort=EFFORT):
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
            # never reach the model. The gateway supplies just our four schemas.
            continue
        if kind == 'custom_tool_call':
            _exact(item, ('type', 'id', 'call_id', 'name', 'namespace', 'input', 'status'))
            if item.get('name') != 'exec' or item.get('namespace') != 'functions':
                raise GatewayRejected('unapproved native tool continuation')
            match = re.fullmatch(r'text\(await tools\.(mcp__review__review_[a-z]+)\((.*)\)\);', item.get('input', ''), re.DOTALL)
            if not match or match[1] not in REVIEW_TOOLS:
                raise GatewayRejected('arbitrary code execution is prohibited')
            arguments = _json_object(match[2], MAX_REQUEST)
            item = {'type': 'function_call', 'name': match[1], 'arguments': json.dumps(arguments),
                    'call_id': item['call_id'], **({'id': item['id']} if 'id' in item else {})}
            kind = 'function_call'
        elif kind == 'custom_tool_call_output':
            _exact(item, ('type', 'id', 'call_id', 'output', 'status'))
            item = dict(item, type='function_call_output')
            kind = 'function_call_output'
        if kind == 'message':
            _exact(item, ('type', 'role', 'content', 'id', 'status'))
            if item.get('role') not in ('system', 'developer', 'user', 'assistant'):
                raise GatewayRejected('invalid message role')
            content = item.get('content')
            if isinstance(content, str):
                source_input.append(item)
                continue
            if not isinstance(content, list):
                raise GatewayRejected('inline text is required')
            for chunk in content:
                _exact(chunk, ('type', 'text', 'annotations'))
                if chunk.get('type') not in ('input_text', 'output_text') or not isinstance(chunk.get('text'), str):
                    raise GatewayRejected('external or nontext input is prohibited')
                if chunk.get('annotations') not in (None, []):
                    raise GatewayRejected('external annotations are prohibited')
        elif kind == 'function_call':
            _exact(item, ('type', 'name', 'arguments', 'call_id', 'id', 'status'))
            if item.get('name') not in REVIEW_TOOLS:
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
        if tool.get('name') not in REVIEW_TOOLS:
            raise GatewayRejected('unapproved tool')
    if value.get('tool_choice', 'auto') not in ('auto', 'none'):
        raise GatewayRejected('unsupported tool choice')
    # Canonical schemas apply to both native additional_tools and flat registries.
    # Client descriptions and JSON Schema references never reach inference.
    from .reviewer_mcp import TOOLS
    permitted = [{'type': 'function', 'name': 'mcp__review__' + t['name'],
                  'description': t['description'], 'parameters': t['inputSchema'], 'strict': False} for t in TOOLS]
    result = dict(value, tools=permitted, input=source_input)
    # Host/client metadata and cache keys are not needed for evidence assessment.
    result.pop('client_metadata', None)
    result.pop('prompt_cache_key', None)
    return result


def native_response(data):
    """Translate only validated scoped calls into fixed native Code Mode calls.

    The model cannot supply JavaScript. Arguments are parsed JSON and re-encoded
    inside one generated expression; only four fixed MCP function names exist.
    """
    validate_response(data)
    completed = next(event['response'] for event in _upstream_events(data)
                     if event.get('type') == 'response.completed')

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


def validate_response(data):
    """Validate the complete bounded event stream before allowing CLI dispatch.

    Buffering prevents an early malicious tool call from executing before a later
    completion event is checked. This deliberately trades token streaming for a
    small, auditable execution boundary.
    """
    completed = False
    allowed_events = {'response.created', 'response.in_progress', 'response.completed',
        'response.output_item.added', 'response.output_item.done', 'response.content_part.added',
        'response.content_part.done', 'response.output_text.delta', 'response.output_text.done',
        'response.function_call_arguments.delta', 'response.function_call_arguments.done',
        'response.reasoning_summary_part.added', 'response.reasoning_summary_part.done',
        'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done',
        'response.reasoning_text.delta', 'response.reasoning_text.done'}
    for event in _upstream_events(data):
        if event.get('type') not in allowed_events:
            raise GatewayRejected('model response failed')
        items = [event.get('item')]
        response = event.get('response') or {}
        items.extend(response.get('output') or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get('type')
            if kind == 'function_call':
                if item.get('name') not in REVIEW_TOOLS:
                    raise GatewayRejected('model requested an unapproved tool')
            elif kind not in ('message', 'reasoning'):
                raise GatewayRejected('model requested an unsupported capability')
        if event.get('type') == 'response.completed':
            if completed or response.get('status') != 'completed' or not isinstance(response.get('output'), list):
                raise GatewayRejected('invalid model completion')
            # Validate complete objects before converting them to native events.
            for item in response['output']:
                if not isinstance(item, dict):
                    raise GatewayRejected('invalid completion item')
                kind = item.get('type')
                if kind == 'function_call':
                    _exact(item, ('type', 'id', 'call_id', 'name', 'arguments', 'status'))
                    _json_object(item.get('arguments'), MAX_REQUEST)
                elif kind == 'message':
                    _exact(item, ('type', 'id', 'role', 'status', 'content'))
                    if item.get('role') != 'assistant' or not isinstance(item.get('content'), list):
                        raise GatewayRejected('invalid assistant message')
                    for chunk in item['content']:
                        _exact(chunk, ('type', 'text', 'annotations', 'logprobs'))
                        if chunk.get('type') != 'output_text' or not isinstance(chunk.get('text'), str):
                            raise GatewayRejected('nontext model output')
                        if chunk.get('annotations') not in (None, []) or chunk.get('logprobs') not in (None, []):
                            raise GatewayRejected('unsupported output references')
                elif kind == 'reasoning':
                    _exact(item, ('type', 'id', 'summary', 'content', 'encrypted_content', 'status'))
            completed = True
    if not completed:
        raise GatewayRejected('model stream did not complete')
    return data


def subscription_transport(body, headers, *, timeout=600):
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
        if response.getheader('Content-Type', '').split(';')[0] != 'text/event-stream':
            raise GatewayRejected('upstream did not return an event stream')
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise GatewayRejected('model response exceeded its size bound')
        return 200, data
    finally:
        connection.close()


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 4


@contextmanager
def gateway_server(socket_path, auth_owner, *, model=MODEL, reasoning_effort=EFFORT,
                   uid=None, gid=None, transport=subscription_transport):
    path = Path(socket_path)
    if path.exists() or path.is_symlink():
        raise ValueError('gateway socket path already exists')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    slots = threading.BoundedSemaphore(4)

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
                self.send_result(503, b'{"error":"review gateway busy"}')
                return
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
                value = validate_request(_json_object(body, MAX_REQUEST), model, reasoning_effort)
                body = encode_json(value, MAX_REQUEST)
                status, data = transport(body, auth_owner._request_headers())
                if status == 401:
                    status, data = transport(body, auth_owner._request_headers(refresh=True))
                if status != 200:
                    self.send_result(503, b'{"error":"subscription inference unavailable"}')
                    return
                self.send_result(200, native_response(data), 'text/event-stream')
            except (GatewayRejected, ValueError, TypeError, KeyError, UnicodeError):
                self.send_result(400, b'{"error":"review model request rejected"}')
            except (AuthenticationUnavailable, OSError, http.client.HTTPException):
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
