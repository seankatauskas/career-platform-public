"""Dedicated authenticated service ingress. No cookies, browser CORS, or MCP tools."""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler

from ..contracts import ContractError, ConflictError, canonical_json
from ..hermes_mcp import _BoundedThreadingHTTPServer


def make_interaction_server(service, bearer_token, host='127.0.0.1', port=8768, *, allowed_hosts=()):
    if not isinstance(bearer_token,str) or len(bearer_token) < 32 or any(c.isspace() for c in bearer_token):
        raise ValueError('interaction bearer must contain at least 32 nonspace characters')
    hosts = set(allowed_hosts) | {'localhost','127.0.0.1'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'CareerInteractions/1'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def _json(self, status, value):
            body = canonical_json(value).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            supplied = self.headers.get_all('Authorization',[])
            host_headers = self.headers.get_all('Host',[])
            if len(host_headers) != 1 or host_headers[0].split(':')[0].lower() not in hosts or self.headers.get('Origin') or self.headers.get('Transfer-Encoding'):
                self._json(403,{'error':'invalid service origin'})
                return False
            if len(supplied) != 1 or not secrets.compare_digest(supplied[0],'Bearer '+bearer_token):
                self._json(401,{'error':'authentication required'})
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            if self.path == '/health':
                return self._json(200,{'status':'ok','configured':bool(service.identity)})
            if self.path == '/v1/reviews':
                return self._json(200,service.pending_reviews())
            self._json(404,{'error':'unknown endpoint'})

        def do_POST(self):
            if not self._authorized():
                return
            try:
                lengths = self.headers.get_all('Content-Length',[])
                if len(lengths) != 1 or not lengths[0].isdigit() or not 1 <= int(lengths[0]) <= 8192:
                    raise ContractError('invalid request length')
                if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    raise ContractError('JSON required')
                def pairs(items):
                    result = {}
                    for key,value in items:
                        if key in result:
                            raise ContractError('duplicate JSON key')
                        result[key] = value
                    return result
                def finite(value):
                    raise ContractError('non-finite JSON number')
                body = json.loads(self.rfile.read(int(lengths[0])),object_pairs_hook=pairs,parse_constant=finite)
                if not isinstance(body,dict):
                    raise ContractError('object required')
                if self.path == '/v1/interactions':
                    result = service.ingest(body)
                elif self.path == '/v1/reviews/delivered':
                    if set(body) != {'ticket_id','message_id'}:
                        raise ContractError('invalid receipt')
                    result = service.mark_delivered(body['ticket_id'],body['message_id'])
                elif self.path in ('/v1/reviews/claim','/v1/reviews/unknown'):
                    if set(body) != {'ticket_id'}:
                        raise ContractError('invalid delivery claim')
                    result = service.claim_delivery(body['ticket_id']) if self.path.endswith('/claim') else service.delivery_unknown(body['ticket_id'])
                else:
                    return self._json(404,{'error':'unknown endpoint'})
                self._json(200,result)
            except ConflictError as exc:
                self._json(409,{'error':str(exc)[:300]})
            except (ContractError,ValueError,TypeError) as exc:
                self._json(400,{'error':str(exc)[:300]})
            except Exception:
                self._json(503,{'error':'interaction temporarily unavailable; retry this same command'})

    server = _BoundedThreadingHTTPServer((host,int(port)),Handler)
    server.daemon_threads = True
    return server
