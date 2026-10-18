"""Offline exact-window, index migration and frozen-input regressions."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from job_search.collection.boards import _prepare
from job_search.collection.discovery_index import CANDIDATE_SQL, candidate_bounds, prepare_existing
from job_search.job_reviews.contracts import REVIEW_JOB_FIELDS, timestamp
from job_search.job_reviews.old_method.store import Store
from job_search.job_reviews.old_method.window import eligible


def create_catalog(path):
    with closing(sqlite3.connect(path)) as con:
        con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,posted_at TEXT,first_seen TEXT,closed_at TEXT,description TEXT,PRIMARY KEY(ats,id))')


def old_snapshot(path, start, end):
    """The previous full-catalog algorithm, retained only as a parity oracle."""
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        con.execute('BEGIN')
        keys = [(r['ats'], r['id']) for r in con.execute(
            'SELECT ats,id,posted_at,first_seen,closed_at FROM jobs') if eligible(dict(r), start, end)]
        columns = {r[1] for r in con.execute('PRAGMA table_info(jobs)')}
        fields = ','.join(f for f in REVIEW_JOB_FIELDS if f in columns)
        jobs = [dict(con.execute('SELECT '+fields+' FROM jobs WHERE ats=? AND id=?', key).fetchone()) for key in keys]
    return sorted(jobs, key=lambda j: (timestamp(j['first_seen']), j['ats'], str(j['id'])))


def store_for(path):
    return Store(SimpleNamespace(db_path=None, catalog=SimpleNamespace(jobs_db=path)))


class ReviewInputIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'jobs.db'
        create_catalog(self.path)
        self.start, self.end = map(timestamp, ('2026-10-08T12:00:00Z', '2026-10-09T12:00:00Z'))

    def insert(self, rows):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?)', rows)

    def test_exact_parity_for_offsets_alternate_iso_invalid_and_boundary_dates(self):
        dates = [None, '', 'invalid', '2026-10-09T10:00:00',
                 '2026-10-08T12:00:00Z', '2026-10-08T12:00:00.000001Z',
                 '2026-10-09T12:00:00Z', '2026-10-09T12:00:00.000001Z',
                 '2026-10-10T11:59:00+23:59', '2026-10-07T12:01:00-23:59',
                 '20261009T120000Z', '2026-W41-5T12:00:00Z',
                 '2026-10-09X12:00:00Z', '2026-10-09T12:00:00,000000Z',
                 '2026-10-09T12:00:00+00:00:00.1', b'2026-10-09T12:00:00Z',
                 '2026-10-09T12:00:00.0000009Z', '2026-10-09T99:00:00Z']
        rows = [('ashby', str(n), posted, seen, closed, 'Description '+str(n))
                for n, (seen, posted, closed) in enumerate(
                    (s, p, c) for s in dates for p in dates for c in (None, '', 'closed'))]
        rng = random.Random(42)
        for n in range(500):
            dt = self.start + timedelta(seconds=rng.randrange(-172800, 259200), microseconds=n)
            seen = dt.astimezone(timezone(timedelta(minutes=rng.randrange(-1439, 1440))))
            posted = dt - timedelta(hours=rng.choice((-1, 0, 12, 24, 25)))
            rows.append(('lever', str(n), posted.isoformat(), seen.isoformat(), None, 'Offset row'))
        self.insert(rows)
        expected = old_snapshot(self.path, self.start, self.end)
        # Old catalogs remain readable before the additive migration.
        self.assertEqual(store_for(self.path)._snapshot(self.start, self.end)[0], expected)
        prepare_existing(self.path)
        actual, counts = store_for(self.path)._snapshot(self.start, self.end)
        self.assertEqual(actual, expected)
        self.assertEqual(counts['total_eligible'], len(expected))
        self.assertEqual(sum(counts['by_platform'].values()), len(expected))

    def test_covering_range_and_fallback_avoid_description_table_reads(self):
        self.insert([('ashby', 'old', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', None, 'large')])
        prepare_existing(self.path)
        with closing(sqlite3.connect(self.path)) as con:
            plan = [r[3] for r in con.execute('EXPLAIN QUERY PLAN '+CANDIDATE_SQL, candidate_bounds(self.start, self.end))]
            searches = [p for p in plan if 'SEARCH jobs' in p]
            self.assertEqual(len(searches), 2, plan)
            self.assertTrue(all('COVERING INDEX jobs_discovery_candidates' in p for p in searches), plan)
            self.assertFalse(any('SCAN jobs' in p for p in plan), plan)
            self.assertEqual(con.execute(CANDIDATE_SQL, candidate_bounds(self.start, self.end)).fetchall(), [])

    def test_index_install_is_idempotent_and_collector_maintains_updates(self):
        self.insert([('ashby', 'a', '2026-10-09T00:00:00Z', '2020-01-01T00:00:00Z', None, 'Original')])
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("ALTER TABLE jobs ADD COLUMN last_seen TEXT")
            _prepare(con)
        prepare_existing(self.path)
        prepare_existing(self.path)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE jobs SET first_seen='2026-10-09T12:00:00Z' WHERE id='a'")
        self.assertEqual(len(store_for(self.path)._snapshot(self.start, self.end)[0]), 1)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE jobs SET closed_at='closed' WHERE id='a'")
        self.assertEqual(store_for(self.path)._snapshot(self.start, self.end)[0], [])

    def test_absent_and_pre_provenance_catalogs_are_left_for_collector_migration(self):
        absent = self.path.parent/'absent.db'
        prepare_existing(absent)
        self.assertFalse(absent.exists())
        with closing(sqlite3.connect(self.path)) as con:
            con.execute('DROP TABLE jobs')
            con.execute('CREATE TABLE jobs(id TEXT,first_seen TEXT)')
        prepare_existing(self.path)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(list(con.execute('PRAGMA index_list(jobs)')), [])

    def test_description_reads_share_candidate_transaction_during_catalog_write(self):
        self.insert([('ashby', 'a', '2026-10-09T00:00:00Z', '2026-10-09T12:00:00Z', None, 'Original')])
        prepare_existing(self.path)
        with closing(sqlite3.connect(self.path)) as con:
            con.execute('PRAGMA journal_mode=WAL')
        def concurrent_update(row, start, end):
            with closing(sqlite3.connect(self.path)) as con, con:
                con.execute("UPDATE jobs SET description='Changed',closed_at='closed'")
            return eligible(row, start, end)
        with patch('job_search.job_reviews.old_method.store.eligible', side_effect=concurrent_update):
            jobs, _ = store_for(self.path)._snapshot(self.start, self.end)
        self.assertEqual(jobs[0]['description'], 'Original')
        self.assertIsNone(jobs[0]['closed_at'])
        self.assertEqual(store_for(self.path)._snapshot(self.start, self.end)[0], [])

    def test_supported_calendar_extremes_do_not_overflow_range_expansion(self):
        start = datetime.min.replace(tzinfo=timezone.utc)
        end = datetime.max.replace(tzinfo=timezone.utc)
        self.assertEqual(candidate_bounds(start, end), ('0001-01-01', '9999-12-31'))


if __name__ == '__main__':
    unittest.main()
