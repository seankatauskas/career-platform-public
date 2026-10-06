"""Offline observations never turn transport failure into a closed posting."""
import copy
import io
import json
import time
import unittest
from http.client import IncompleteRead, RemoteDisconnected
from collections import Counter
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from job_search.job_reviews import availability as av


class Response(io.BytesIO):
    status = 200

    def __init__(self, raw, headers=None):
        super().__init__(raw)
        self.headers = headers or {}


class AvailabilityTests(unittest.TestCase):
    def job(self, ats='ashby', jid='a', company='example'):
        return {'ats': ats, 'id': jid, 'company': company, 'jobUrl': 'http://127.0.0.1/never-fetch'}

    def check(self, jobs, payload):
        return av.check_boards(jobs, contact='operator@example.test', fetcher=lambda *_: payload)

    def test_platform_shapes_and_listed_identity(self):
        cases = [
            ('ashby', {'jobs': [{'id': 'a', 'isListed': True}, {'id': 'b', 'isListed': False}]}),
            ('greenhouse', {'jobs': [{'id': 'a'}], 'meta': {'total': 1}}),
            ('lever', [{'id': 'a'}]),
        ]
        for ats, payload in cases:
            with self.subTest(ats=ats):
                result = self.check([self.job(ats), self.job(ats, 'b')], payload)
                self.assertEqual([r['status'] for r in result], ['open', 'absent'])
                self.assertTrue(all(r['source'].startswith('https://') and r['checked_at'] for r in result))
        self.assertEqual(self.check([self.job('greenhouse', '42')], {'jobs': [{'id': 42}]})[0]['status'], 'open')

    def test_one_request_per_board_and_no_input_mutation(self):
        jobs = [self.job(), self.job(jid='b'), self.job(company='second')]
        original = copy.deepcopy(jobs)
        calls = Counter()
        def fetch(url, *_):
            calls[url] += 1
            return {'jobs': []}
        results = av.check_boards(jobs, contact='operator@example.test', fetcher=fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(calls.values()), [1, 1])
        self.assertEqual(jobs, original)
        self.assertEqual(len(results), 3)
        self.assertTrue(all('127.0.0.1' not in url for url in calls))

    def test_invalid_partial_payloads_never_establish_absence(self):
        for payload in ({}, {'jobs': None}, {'jobs': [None]}, {'jobs': [{'id': None}]},
                        {'jobs': [{'id': True}]}, {'jobs': [], 'hasMore': True},
                        {'jobs': [], 'meta': {'total': 2}}, {'jobs': [{'id': 'a'}]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.check([self.job()], payload)[0]['status'], 'unknown')

    def test_invalid_board_contact_and_platform_skip_network(self):
        def forbidden(*_):
            self.fail('network must not be used')
        for company in ('../other', 'example?x=1', 'example/other', 'https://evil.test', ''):
            r = av.check_boards([self.job(company=company)], contact='x@example.test', fetcher=forbidden)
            self.assertEqual(r[0]['status'], 'unknown')
        self.assertEqual(av.check_boards([self.job('unknown')], contact='x@example.test', fetcher=forbidden)[0]['reason'], 'unsupported_platform')
        for contact in ('', None, 'x\r\nInjected: header'):
            r = av.check_boards([self.job()], contact=contact, fetcher=forbidden)
            self.assertEqual(r[0]['reason'], 'scraper_contact_not_configured')

    def test_transport_failure_is_unknown_after_one_retry(self):
        for error in (URLError('offline'), TimeoutError(), IncompleteRead(b'partial'), RemoteDisconnected(),
                      HTTPError('https://example.test', 429, '', {}, None)):
            calls = []
            def fetch(*_):
                calls.append(1)
                raise error
            r = av.check_boards([self.job()], contact='x@example.test', fetcher=fetch)
            self.assertEqual(r[0]['status'], 'unknown')
            self.assertEqual(len(calls), 2)
        calls = []
        def missing(*_):
            calls.append(1)
            raise HTTPError('https://example.test', 404, '', {}, None)
        self.assertEqual(av.check_boards([self.job()], contact='x@example.test', fetcher=missing)[0]['status'], 'unknown')
        self.assertEqual(len(calls), 1)

    def test_budget_returns_unknown_without_waiting_for_late_success(self):
        def slow(*_):
            time.sleep(.08)
            return {'jobs': []}
        started = time.monotonic()
        r = av.check_boards([self.job()], contact='x@example.test', fetcher=slow, budget_seconds=.01)
        self.assertEqual(r[0]['status'], 'unknown')
        self.assertEqual(r[0]['reason'], 'verification_budget_exhausted')
        self.assertLess(time.monotonic() - started, .07)
        time.sleep(.08)
        self.assertEqual(r[0]['status'], 'unknown')

    def test_reader_limits_redirects_encoding_and_incomplete_bodies(self):
        with self.assertRaises(av.ObservationError):
            av.NoRedirects().redirect_request(None, None, 302, '', {}, 'https://evil.test')
        cases = [(b'x' * 33, {}, 'board_response_too_large'),
                 (b'{}', {'Content-Length': '100'}, 'board_response_too_large'),
                 (b'{}', {'Content-Length': '4'}, 'incomplete_board_response'),
                 (b'{}', {'Content-Encoding': 'gzip'}, 'unsupported_content_encoding'),
                 (b'broken', {}, 'invalid_board_json')]
        for raw, headers, reason in cases:
            with self.subTest(reason=reason), patch.object(av, 'MAX_BYTES', 32), patch.object(av, 'build_opener') as opener:
                opener.return_value.open.return_value = Response(raw, headers)
                with self.assertRaisesRegex(av.ObservationError, reason):
                    av._fetch('https://api.ashbyhq.com/posting-api/job-board/example', 'x@example.test', time.monotonic()+1)
        with patch.object(av, 'build_opener') as opener:
            opener.return_value.open.return_value = Response(b'{"jobs":[]}')
            self.assertEqual(av._fetch('https://api.ashbyhq.com/posting-api/job-board/example', 'x@example.test', time.monotonic()+1), {'jobs': []})
            request = opener.return_value.open.call_args.args[0]
            self.assertNotIn('If-none-match', dict(request.header_items()))


if __name__ == '__main__':
    unittest.main()
