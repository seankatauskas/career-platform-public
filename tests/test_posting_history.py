"""Offline lifecycle, provenance, migration and dashboard history regressions."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_search.collection.boards import FIELDS, _prepare, normalize_greenhouse, save
from job_search.integration import LocalJobCatalog
from tests.test_job_search_dashboard import dashboard, request, start_direct


class PostingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'jobs.db'
        self.row = {field: '' for field in FIELDS}
        self.row.update(ats='ashby', id='job-direct', company='example', title='Engineer',
                        description='Original description', location='Chicago',
                        posted_at='2026-09-01T12:00:00Z', publishedAt='2026-09-01T12:00:00Z')
        self.catalog = LocalJobCatalog(self.db)

    def scan(self, day, rows=None, covered=True):
        return save([self.row] if rows is None else rows, self.db, f'2026-09-{day:02}T12:00:00Z',
                    [('ashby', 'example')] if covered else None)

    def history(self, before=None):
        return self.catalog.posting_history('ashby', 'job-direct', before)

    def test_open_modify_close_reopen_no_duplicate_observations(self):
        self.scan(2); self.scan(3)
        self.row['title'] = 'Software Engineer'
        self.row['description'] = 'Replacement description'
        self.scan(4); self.scan(4)
        self.scan(5, []); self.scan(6, [])
        self.scan(7); self.scan(8)
        events = self.history()['events']
        self.assertEqual([e['event_type'] for e in events], ['reopened', 'closed', 'modified', 'opened'])
        self.assertEqual(events[2]['changes']['title'], {'before': 'Engineer', 'after': 'Software Engineer'})
        self.assertIn('description', events[2]['changes'])
        self.assertIsNone(events[2]['source_at'])
        self.assertEqual(events[-1]['source_at'], self.row['posted_at'])
        self.assertIsNone(events[0]['source_at'])

    def test_filtered_absence_and_other_boards_cannot_close(self):
        self.scan(2)
        self.scan(3, [], covered=False)
        save([], self.db, '2026-09-04T12:00:00Z', [('ashby', 'other')])
        self.assertEqual([e['event_type'] for e in self.history()['events']], ['opened'])

    def test_date_enrichment_is_not_a_modification_and_missing_dates_preserved(self):
        self.row['posted_at'] = ''
        self.scan(2)
        self.row['posted_at'] = '2026-09-01T12:00:00Z'
        self.row['source_updated_at'] = '2026-09-02T12:00:00Z'
        self.scan(3)
        self.assertEqual(len(self.history()['events']), 1)
        self.row['source_updated_at'] = '2026-09-04T12:00:00Z'; self.scan(4)
        self.assertEqual(self.history()['events'][0]['source_at'], self.row['source_updated_at'])
        self.row.pop('source_updated_at'); self.row.pop('posted_at'); self.scan(5)
        self.assertEqual(len(self.history()['events']), 2)
        self.assertEqual(self.history()['job']['source_updated_at'], '2026-09-04T12:00:00Z')

    def test_greenhouse_keeps_publication_and_update_distinct(self):
        row = normalize_greenhouse({'id': 1, 'first_published': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-10T00:00:00Z'})
        self.assertEqual(row['posted_at'], '2026-09-01T00:00:00Z')
        self.assertEqual(row['source_updated_at'], '2026-09-10T00:00:00Z')
        self.assertEqual(row['publishedAt'], row['posted_at'])
        fallback = normalize_greenhouse({'id': 1, 'updated_at': row['source_updated_at']})
        self.assertEqual(fallback['posted_at'], '')
        self.assertEqual(fallback['publishedAt'], row['source_updated_at'])

    def test_legacy_dates_do_not_invent_intermediate_history(self):
        self.scan(2); self.scan(5, [])
        with sqlite3.connect(self.db) as con:
            con.execute('DELETE FROM job_posting_events')
        history = self.history()
        self.assertEqual([e['event_type'] for e in history['events']], ['closed', 'first_seen'])
        self.assertEqual(history['events'][-1]['observed_at'], '2026-09-02T12:00:00Z')
        self.assertIn('unrecorded', history['history_note'])

    def test_transaction_rollback_removes_observation(self):
        self.scan(2)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE jobs SET title='Changed'")
            con.rollback()
        self.assertEqual(len(self.history()['events']), 1)

    def test_same_id_on_another_platform_is_isolated(self):
        self.scan(2)
        other = {**self.row, 'ats': 'greenhouse'}
        save([other], self.db, '2026-09-03T00:00:00Z')
        self.assertEqual(len(self.history()['events']), 1)

    def test_pagination_does_not_duplicate_events(self):
        self.scan(2)
        with sqlite3.connect(self.db) as con:
            for index in range(55):
                con.execute('UPDATE jobs SET title=?', (f'Engineer {index}',))
        first = self.history(); second = self.history(first['next_before'])
        all_events = first['events'] + second['events']
        self.assertEqual(len(all_events), 56)
        self.assertEqual(len({e['event_id'] for e in all_events}), 56)
        self.assertIsNone(second['next_before'])

    def test_read_old_schema_without_mutating_it(self):
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE jobs (ats TEXT,id TEXT,publishedAt TEXT,first_seen TEXT,closed_at TEXT)')
            con.execute("INSERT INTO jobs VALUES ('ashby','job-direct','2026-09-01','2026-09-02',NULL)")
        self.assertEqual(self.history()['events'][0]['event_type'], 'first_seen')
        with sqlite3.connect(self.db) as con:
            self.assertFalse(con.execute("SELECT 1 FROM sqlite_master WHERE name='job_posting_events'").fetchone())

    def test_dashboard_returns_dates_and_application_scoped_history(self):
        self.scan(2)
        with dashboard() as (server, controller, ledger, _):
            controller.jobs = self.catalog
            app_id = start_direct(ledger)
            status, _, body = request(server, 'GET', f'/api/v1/applications/{app_id}/job-history')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['events'][0]['event_type'], 'opened')
            status, _, body = request(server, 'GET', '/api/v1/applications')
            self.assertEqual(json.loads(body)['applications'][0]['job_posting']['posted_at'], self.row['posted_at'])
            status, _, body = request(server, 'GET', f'/api/v1/applications/{app_id}/workspace')
            self.assertIn('job_history', json.loads(body))
            for query in ('?before=-1', '?before=no', '?before=1&before=2', '?ats=lever'):
                status, _, _ = request(server, 'GET', f'/api/v1/applications/{app_id}/job-history{query}')
                self.assertEqual(status, 400)
            status, _, _ = request(server, 'GET', '/api/v1/applications/missing/job-history')
            self.assertNotEqual(status, 200)


if __name__ == '__main__':
    unittest.main()
