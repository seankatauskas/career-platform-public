"""Small private-dashboard client; no model, ranking, or selection logic."""
from __future__ import annotations

import http.cookiejar
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from ..contracts import ContractError
from .contracts import bounded, canonical_json
from .service import FIELDS


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ContractError('review endpoint redirected; check the configured private dashboard URL')


def endpoint(value):
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ContractError('dashboard URL must be an origin without credentials or a path')
    local = parsed.hostname in ('127.0.0.1', 'localhost', '::1') and parsed.scheme == 'http'
    private = parsed.scheme == 'https' and (parsed.hostname or '').endswith('.ts.net')
    if not (local or private):
        raise ContractError('use the private Tailscale HTTPS origin or a localhost HTTP preview')
    return value.rstrip('/')


class ReviewClient:
    def __init__(self, origin):
        self.origin = endpoint(origin)
        # Session and CSRF values remain in memory, never in CLI output or files.
        self.opener = build_opener(NoRedirects(), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _request(self, path, body=None, csrf=None):
        headers = {'Accept': 'application/json', 'Origin': self.origin}
        data = None
        if body is not None:
            data = canonical_json(body).encode()
            if len(data) > 65536:
                raise ContractError('review request exceeds 64 KiB')
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': csrf})
        request = Request(self.origin + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read(65537)
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read(65536)).get('error', 'request failed')
            except (ValueError, UnicodeError):
                detail = 'request failed'
            raise ContractError(f'review request failed ({exc.code}): {detail}') from None
        except URLError:
            raise ContractError('private dashboard is unreachable; check Tailscale and the dashboard URL') from None
        if len(raw) > 65536:
            raise ContractError('review response is too large; request a smaller page')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ContractError('dashboard returned an invalid review response') from None

    def call(self, action, args):
        if action not in FIELDS:
            raise ContractError('unknown review action')
        csrf = self._request('/api/v1/session')['csrf_token']
        result = bounded(self._request('/api/v1/job-reviews/' + action, args, csrf))
        if action == 'publish':
            for item in result.get('lists', []):
                item['dashboard_url'] = self.origin + '/' + item['dashboard_path']
        return result
