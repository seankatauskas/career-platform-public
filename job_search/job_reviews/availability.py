"""Bounded official-board observations, independent of collector lifecycle state.

Only trusted application code calls this module. It never follows employer-supplied
URLs and never mutates the catalog, ETags, or collection state.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..collection.boards import board_url

MAX_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT = 10
BUDGET_SECONDS = 90
CONCURRENCY = 8

SCHEMA = """
CREATE TABLE job_review_availability (
 review_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL,
 snapshot_sha256 TEXT NOT NULL,
 result_json TEXT NOT NULL,
 PRIMARY KEY(review_id,ordinal),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
"""


class ObservationError(Exception):
    """A bounded reason safe to include in a review observation."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ObservationError('redirect_not_followed')


def official_url(ats, company):
    # The catalog's company field is the board slug. Reject path/query injection,
    # including dot-only segments, rather than trusting a posting's jobUrl.
    if ats not in ('ashby', 'greenhouse', 'lever'):
        raise ObservationError('unsupported_platform')
    if not isinstance(company, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,254}', company):
        raise ObservationError('invalid_board_identity')
    return board_url(ats, company, want_content=False)


def listed_ids(ats, payload):
    """Reject incomplete/unrecognized bodies; they cannot establish absence."""
    rows = payload if ats == 'lever' else payload.get('jobs') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ObservationError('invalid_board_response')
    if isinstance(payload, dict):
        if any(payload.get(key) for key in ('hasMore', 'has_more', 'next', 'nextPage', 'next_page', 'nextCursor')):
            raise ObservationError('incomplete_board_response')
        total = (payload.get('meta') or {}).get('total') if isinstance(payload.get('meta', {}), dict) else None
        if total is not None and (type(total) is not int or total != len(rows)):
            raise ObservationError('incomplete_board_response')
    result = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get('id')) not in (str, int) or not str(row['id']).strip():
            raise ObservationError('invalid_posting_identity')
        if ats == 'ashby':
            if type(row.get('isListed')) is not bool:
                raise ObservationError('invalid_listing_status')
            if not row['isListed']:
                continue
        result.add(str(row['id']))
    return result


def _fetch(url, contact, deadline, *, clock=time.monotonic):
    remaining = deadline - clock()
    if remaining <= 0:
        raise ObservationError('verification_budget_exhausted')
    request = Request(url, headers={
        'Accept': 'application/json', 'Accept-Encoding': 'identity',
        'User-Agent': 'CareerPlatform-availability/1.0 (' + contact + ')',
    })
    with build_opener(NoRedirects()).open(request, timeout=min(REQUEST_TIMEOUT, remaining)) as response:
        if response.status != 200:
            raise ObservationError('unexpected_http_status')
        headers = {key.lower(): value for key, value in response.headers.items()}
        if headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
            raise ObservationError('unsupported_content_encoding')
        if 'content-length' in headers:
            try:
                length = int(headers['content-length'])
            except ValueError:
                raise ObservationError('invalid_content_length') from None
            if length < 0 or length > MAX_BYTES:
                raise ObservationError('board_response_too_large')
        chunks, size = [], 0
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise ObservationError('verification_budget_exhausted')
            # read1 returns after one buffered read, avoiding a slow stream holding
            # an unbounded read open. Each socket read retains a bounded timeout.
            chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise ObservationError('board_response_too_large')
        if headers.get('content-length') is not None and size != int(headers['content-length']):
            raise ObservationError('incomplete_board_response')
        try:
            return json.loads(b''.join(chunks))
        except (ValueError, UnicodeError, RecursionError):
            raise ObservationError('invalid_board_json') from None


def _observe(ats, url, contact, deadline, fetcher):
    for attempt in range(2):
        try:
            return listed_ids(ats, fetcher(url, contact, deadline)), ''
        except ObservationError as exc:
            return None, str(exc)
        except HTTPError as exc:
            # An HTTP 404 for a board is not a complete board response.
            if attempt == 0 and exc.code in (408, 429, 500, 502, 503, 504):
                continue
            return None, 'http_' + str(exc.code)
        except (URLError, TimeoutError, OSError, HTTPException):
            if attempt == 0:
                continue
            return None, 'network_unavailable'
        except (ValueError, TypeError):
            return None, 'invalid_board_response'
    return None, 'network_unavailable'


def check_boards(jobs, *, contact='', fetcher=None, budget_seconds=BUDGET_SECONDS,
                 clock=time.monotonic, now=None):
    """Return one result per requested posting, without changing supplied jobs.

    The injected fetcher is trusted code for offline tests, not a reviewer option.
    Pending work is canceled at the overall deadline; no late result is persisted.
    """
    fetcher = fetcher or _fetch
    now = now or (lambda: datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
    deadline = clock() + max(0, min(float(budget_seconds), BUDGET_SECONDS))
    contact_ok = isinstance(contact, str) and bool(contact.strip()) and len(contact) <= 255 and all(32 <= ord(c) < 127 for c in contact)
    boards, prepared = {}, []
    for job in jobs:
        ats, jid = job.get('ats'), str(job.get('id', job.get('job_id', '')))
        try:
            url = official_url(ats, job.get('company'))
            reason = '' if contact_ok else 'scraper_contact_not_configured'
        except ObservationError as exc:
            url, reason = '', str(exc)
        prepared.append((ats, jid, url, reason))
        if not reason:
            boards[(ats, url)] = None
    pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix='review-availability')
    futures = {}
    try:
        for ats, url in boards:
            futures[pool.submit(_observe, ats, url, contact, deadline, fetcher)] = (ats, url)
        if futures:
            completed, pending = wait(futures, timeout=max(0, deadline - clock()))
            for future in completed:
                boards[futures[future]] = (*future.result(), now())
            for future in pending:
                future.cancel()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    output = []
    for ats, jid, url, reason in prepared:
        observed = boards.get((ats, url))
        ids, failure, checked = observed or (None, reason or 'verification_budget_exhausted', now())
        output.append({'ats': ats, 'job_id': jid,
                       'status': 'unknown' if ids is None else 'open' if jid in ids else 'absent',
                       'checked_at': checked, 'source': url,
                       'reason': failure if ids is None else 'listed_on_official_board' if jid in ids else 'not_in_complete_official_board'})
    return output
