"""Private, bounded Unix HTTP transport for isolated reviewer assignments.

The authority is trusted and owns every data access decision. A coordinator keeps
assignment credentials here; the worker receives only its individual socket.
Neither listener exposes a dashboard session, arbitrary URL, or generic service
operation. Socket mounts and the worker's network namespace are the outer boundary.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import socket
import socketserver
import stat
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from ..contracts import ContractError
from .context import review_context
from .contracts import REVIEW_JOB_FIELDS

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 56 * 1024
SOCKET_TIMEOUT = 15
ROUTES = {
    ('GET', '/v1/assignment'): 'assignment',
    ('POST', '/v1/context'): 'context',
    ('POST', '/v1/job'): 'job',
    ('POST', '/v1/assessment'): 'assessment',
}
ARGUMENTS = {
    'assignment': ((), ()),
    'context': (('section', 'offset', 'limit'), ()),
    'job': (('ordinal', 'offset', 'limit'), ('ordinal',)),
    'assessment': (('ordinal', 'assessment'), ('ordinal', 'assessment')),
}


class ReviewerTransportError(ContractError):
    """A bounded error suitable for display to an isolated reviewer."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def encode_json(value, maximum=MAX_RESPONSE_BYTES):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ReviewerTransportError('review message must be finite JSON') from None
    if len(result) > maximum:
        raise ReviewerTransportError('review message exceeds its size limit', 413)
    return result


def decode_json(raw, maximum=MAX_REQUEST_BYTES):
    if not raw or len(raw) > maximum:
        raise ReviewerTransportError('review message exceeds its size limit', 413)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError()

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=invalid)
        # Also reject 1e999, which json.loads otherwise parses as infinity.
        encode_json(value, maximum)
    except (ValueError, UnicodeError, RecursionError):
        raise ReviewerTransportError('review message must be finite JSON') from None
    if not isinstance(value, dict):
        raise ReviewerTransportError('review message must be an object')
    return value


def validate_arguments(operation, args):
    if operation not in ARGUMENTS or not isinstance(args, dict):
        raise ReviewerTransportError('unknown reviewer operation')
    allowed, required = ARGUMENTS[operation]
    if set(args) - set(allowed) or set(required) - set(args):
        raise ReviewerTransportError('missing or unknown reviewer fields')
    for key, maximum in (('ordinal', 1000000), ('offset', 10000000), ('limit', 6000)):
        if key in args and (type(args[key]) is not int or not 0 <= args[key] <= maximum):
            raise ReviewerTransportError('invalid reviewer paging or job identity')
    if args.get('ordinal') == 0 or args.get('limit') == 0:
        raise ReviewerTransportError('invalid reviewer paging or job identity')
    if operation == 'context':
        if args.get('section', 'facts') not in ('facts', 'preferences', 'feedback'):
            raise ReviewerTransportError('unknown review context section')
        if args.get('limit', 20) > 20:
            raise ReviewerTransportError('context page limit must be at most twenty')
    if operation == 'assessment':
        if not isinstance(args['assessment'], dict):
            raise ReviewerTransportError('assessment must be an object')
    encode_json(args, MAX_REQUEST_BYTES)
    return args


def _fields(value, names):
    if not isinstance(value, dict):
        raise ReviewerTransportError('invalid review service response', 502)
    return {name: value[name] for name in names if name in value}


def _scalar_fields(value, names):
    result = _fields(value, names)
    if any(v is not None and not isinstance(v, (str, bool, int, float)) for v in result.values()):
        raise ReviewerTransportError('invalid review service fields', 502)
    return result


def _job(value):
    result = _scalar_fields(value, tuple(field for field in REVIEW_JOB_FIELDS if field != 'description'))
    return result


def project_response(operation, value):
    """A second allowlist independent of the authority's own projections."""
    if operation == 'assignment':
        result = _scalar_fields(value, ('grant_id', 'kind', 'expires_at', 'context_fingerprint', 'rubric_version'))
        jobs = value.get('jobs')
        if not isinstance(jobs, list) or len(jobs) > 20:
            raise ReviewerTransportError('invalid review assignment', 502)
        result['jobs'] = []
        for job in jobs:
            item = _scalar_fields(job, ('ordinal', 'expected_revision', 'snapshot_sha256', 'submitted'))
            item['job'] = _job(job.get('job'))
            result['jobs'].append(item)
    elif operation == 'context':
        result = _scalar_fields(value, ('rubric_version', 'profile_revision',
                                       'fingerprint', 'section', 'total', 'next_offset'))
        if 'resume_versions' in value:
            if not isinstance(value['resume_versions'], list) or not all(isinstance(v, str) for v in value['resume_versions']):
                raise ReviewerTransportError('invalid review context', 502)
            result['resume_versions'] = value['resume_versions']
        section = value.get('section')
        if section not in ('facts', 'preferences', 'feedback') or not isinstance(value.get(section), list):
            raise ReviewerTransportError('invalid review context', 502)
        projected = review_context(value).get(section, [])
        if section == 'preferences':
            if not all(isinstance(item, str) for item in projected):
                raise ReviewerTransportError('invalid review preferences', 502)
        elif section == 'facts':
            for item in projected:
                _scalar_fields(item, ('fact_id', 'source', 'entry_id'))
                text = item.get('text')
                if isinstance(text, dict):
                    if not all(v is None or isinstance(v, str) for v in text.values()):
                        raise ReviewerTransportError('invalid approved fact fields', 502)
                elif not isinstance(text, str):
                    raise ReviewerTransportError('invalid approved fact text', 502)
        else:
            for item in projected:
                _scalar_fields(item, ('sequence', 'note', 'created_at'))
        result[section] = projected
    elif operation == 'job':
        result = _scalar_fields(value, ('ordinal', 'revision', 'snapshot_sha256', 'description',
                                'offset', 'next_offset', 'description_chars'))
        result['job'] = _job(value.get('job'))
    elif operation == 'assessment':
        result = _scalar_fields(value, ('review_id', 'ordinal', 'revision'))
    else:
        raise ReviewerTransportError('unknown reviewer operation')
    encode_json(result)
    return result


def safe_socket(path, *, exists):
    target = Path(path)
    if not target.is_absolute():
        raise ReviewerTransportError('review socket path must be absolute')
    try:
        parent = target.parent.resolve(strict=True)
        parent_info = parent.stat()
    except OSError:
        raise ReviewerTransportError('review socket directory is unavailable') from None
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid()
            or parent_info.st_mode & 0o077):
        raise ReviewerTransportError('review socket directory must be owner-only')
    normalized = parent / target.name
    try:
        info = normalized.lstat()
    except FileNotFoundError:
        if exists:
            raise ReviewerTransportError('review socket is unavailable') from None
        return normalized
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ReviewerTransportError('review socket is unsafe')
    if not exists:
        # Never remove a socket that might belong to an active assignment.
        raise ReviewerTransportError('review socket already exists')
    return normalized


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__('reviewer', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


class ReviewerClient:
    """Credentialless client: connects only to the assignment's private socket."""

    def __init__(self, socket_path, *, timeout=SOCKET_TIMEOUT):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ReviewerTransportError('review timeout must be between one and 120 seconds')
        self.socket_path, self.timeout = Path(socket_path), timeout

    def call(self, operation, args=None):
        args = validate_arguments(operation, {} if args is None else args)
        target = safe_socket(self.socket_path, exists=True)
        method, path = next(key for key, action in ROUTES.items() if action == operation)
        body = None if method == 'GET' else encode_json(args, MAX_REQUEST_BYTES)
        connection = _UnixHTTPConnection(target, self.timeout)
        try:
            connection.request(method, path, body=body,
                               headers={'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200:
                # Do not relay backend error bodies, redirects or HTTP headers.
                raise ReviewerTransportError('review request rejected or unavailable', response.status)
            if response.getheader('Content-Type') != 'application/json':
                raise ReviewerTransportError('invalid review response', 502)
            return project_response(operation, decode_json(raw, MAX_RESPONSE_BYTES))
        except (OSError, http.client.HTTPException):
            raise ReviewerTransportError('review service unavailable', 503) from None
        finally:
            connection.close()


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, path, handler):
        self._slots = threading.BoundedSemaphore(8)
        self.socket_path = safe_socket(path, exists=False)
        super().__init__(str(self.socket_path), handler)
        os.chmod(self.socket_path, 0o600)
        self._socket_inode = self.socket_path.stat().st_ino

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(SOCKET_TIMEOUT)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Exceptions may contain private source values or credentials.
        pass

    def server_close(self):
        super().server_close()
        try:
            if self.socket_path.lstat().st_ino == self._socket_inode:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass


def _handler(authority, bound_token=None, ready=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'ReviewAPI'
        sys_version = ''
        protocol_version = 'HTTP/1.0'

        def log_message(self, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self._respond(code, {'error': 'invalid review request'})

        def _respond(self, status, value):
            payload = encode_json(value)
            self.close_connection = True
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def _dispatch(self):
            try:
                operation = ROUTES.get((self.command, self.path))
                if operation is None:
                    raise ReviewerTransportError('unknown review route', 404)
                if self.headers.get_all('Transfer-Encoding'):
                    raise ReviewerTransportError('chunked requests are not supported')
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) > 1 or (lengths and (not lengths[0].isascii() or not lengths[0].isdigit())):
                    raise ReviewerTransportError('invalid content length')
                length = int(lengths[0]) if lengths else 0
                if length > MAX_REQUEST_BYTES:
                    raise ReviewerTransportError('review request exceeds its size limit', 413)
                auth = self.headers.get_all('Authorization', [])
                if bound_token is not None:
                    if auth:
                        raise ReviewerTransportError('credentials cannot be overridden', 401)
                    token = bound_token
                else:
                    if len(auth) != 1 or not auth[0].startswith('Bearer ') or not auth[0][7:]:
                        raise ReviewerTransportError('review authorization required', 401)
                    token = auth[0][7:]
                if self.command == 'GET':
                    if length:
                        raise ReviewerTransportError('assignment does not accept a request body')
                    args = {}
                else:
                    types = self.headers.get_all('Content-Type', [])
                    if types != ['application/json'] or not lengths:
                        raise ReviewerTransportError('JSON content type and length are required')
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ReviewerTransportError('incomplete review request')
                    args = decode_json(raw)
                validate_arguments(operation, args)
                if ready is not None and not ready.wait(timeout=10):
                    raise ReviewerTransportError('review launch is not ready', 503)
                try:
                    value = authority.scoped_call(token, operation, args)
                except ContractError as exc:
                    # Never reflect backend exception text, including transport errors.
                    error_type = type(exc).__name__
                    status = 401 if error_type == 'ReviewAuthorizationError' else 409 if error_type == 'ReviewConflictError' else 400
                    raise ReviewerTransportError('review request rejected by assignment authority', status) from None
                self._respond(200, project_response(operation, value))
            except ReviewerTransportError as exc:
                self._respond(exc.status, {'error': str(exc)})
            except Exception:
                self._respond(503, {'error': 'review service unavailable'})
    return Handler


def make_api_server(socket_path, authority):
    """Create a bearer-authenticated private Unix HTTP server (not started)."""
    return _UnixServer(socket_path, _handler(authority))


def make_assignment_server(socket_path, authority, token, *, ready=None):
    """Create a coordinator-held server bound to one grant (not started)."""
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ReviewerTransportError('assignment credential is invalid')
    return _UnixServer(socket_path, _handler(authority, token, ready))


@contextmanager
def assignment_proxy(socket_path, authority, token, *, ready=None):
    """Serve one assignment until exit; never expose its credential to the worker."""
    server = make_assignment_server(socket_path, authority, token, ready=ready)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=SOCKET_TIMEOUT + 1)
