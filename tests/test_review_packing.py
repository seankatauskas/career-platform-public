"""Complete frozen evidence packing and safe oversized singleton fallback."""
import sqlite3
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.authority import ReviewAuthority
from job_search.job_reviews.packing import pack_assignment
from tests import test_agent_job_reviews as fixtures


class PackingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.authority = ReviewAuthority(self.fixture.service)

    def start(self):
        return self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                     'idempotency_key': 'packing'})['review_id']

    def test_packing_shrinks_deterministically_without_truncating_or_reading_new_catalog(self):
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET description=?', ('A' * 70_000,))
        self.fixture.insert('b', description='B' * 70_000)
        self.fixture.insert('c', description='C' * 70_000)
        rid = self.start()
        expected = {'ordinals': [1, 2], 'preload_enabled': True}
        self.assertEqual(pack_assignment(self.authority, rid, [1, 2, 3]), expected)
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET description=?', ('CHANGED',))
        self.assertEqual(pack_assignment(self.authority, rid, [1, 2, 3]), expected)
        self.assertEqual(pack_assignment(self.authority, rid, [3])['ordinals'], [3])

    def test_utf8_oversize_screen_routes_detail_and_detail_keeps_paged_full_evidence(self):
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET description=?', ('\U0001f680' * 50_000,))
        rid = self.start()
        with patch.object(self.authority, 'route_oversize') as route:
            self.assertEqual(pack_assignment(self.authority, rid, [1], purpose='screening'),
                             {'ordinals': [], 'preload_enabled': True})
            route.assert_called_once_with(rid, 1)
        self.assertEqual(pack_assignment(self.authority, rid, [1]),
                         {'ordinals': [1], 'preload_enabled': False})

    def test_metadata_response_limit_is_separate_from_packet_budget(self):
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET title=?', ('T' * 19_000,))
        self.fixture.insert('b', title='B' * 19_000)
        self.fixture.insert('c', title='C' * 19_000)
        rid = self.start()
        self.assertEqual(pack_assignment(self.authority, rid, [1, 2, 3])['ordinals'], [1, 2])

    def test_single_metadata_cannot_be_silently_truncated(self):
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET title=?', ('T' * 60_000,))
        rid = self.start()
        with self.assertRaisesRegex(ContractError, 'single job metadata'):
            pack_assignment(self.authority, rid, [1])

    def test_large_screening_pack_fits_actual_complete_worker_packet(self):
        import json
        from job_search.job_reviews.codex_runtime import preload_evidence
        from job_search.job_reviews.packing import MAX_PACKET_BYTES
        for index in range(1, 200):
            self.fixture.insert('job-' + str(index))
        self.authority = ReviewAuthority(self.fixture.service, approved_screening_model='screen-model',
                                         approved_screening_reasoning_effort='low')
        rid = self.start()
        self.authority.freeze_execution_policy(rid, {
            'version': 1, 'detailed': {'model': 'gpt-6-astra', 'reasoning_effort': 'high'},
            'screening': {'model': 'screen-model', 'reasoning_effort': 'low'},
            'concurrency': 2, 'batch_size': 20, 'screening_batch_size': 200})
        packed = pack_assignment(self.authority, rid, list(range(1, 201)), purpose='screening')
        self.assertGreater(len(packed['ordinals']), 20)
        self.assertLess(len(packed['ordinals']), 200)
        grant = self.authority.issue(rid, packed['ordinals'], 'primary', {
            'runtime_id': 'packing-worker', 'model': 'screen-model', 'reasoning_effort': 'low'}, purpose='screening')
        self.authority.mark_launch(grant['grant_id'], 'packing-worker', {
            'container_id': '1' * 64, 'image_digest': 'sha256:' + '2' * 64, 'config_sha256': '3' * 64})
        authority = self.authority

        class Client:
            def call(self, operation, args):
                return authority.scoped_call(grant['token'], operation, args)

        packet = preload_evidence(Client(), expected_purpose='screening')
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False).encode('utf-8')), MAX_PACKET_BYTES)
        self.assertEqual([job['ordinal'] for job in packet['jobs']], packed['ordinals'])
        self.assertTrue(all(job['job']['description'] == 'Build Python APIs and SQL systems.' for job in packet['jobs']))


if __name__ == '__main__':
    unittest.main()
