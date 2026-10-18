"""Saved-window replay keeps historical evidence separate from current drift."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.contracts import canonical_json
from job_search.job_reviews.contracts import fingerprint
from job_search.job_reviews.old_method import WORKFLOW
from job_search.job_reviews.old_method.window import DISCOVERY_WINDOW
from tests import probe_review_input_replay as replay


class ReviewInputReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)/'app.db'
        self.catalog = Path(self.tmp.name)/'jobs.db'
        self.jobs = [dict(ats='ashby', id=key, posted_at='2026-10-17T00:00:00Z',
                          first_seen='2026-10-17T01:00:00Z', closed_at=None, description='Private text '+key)
                     for key in ('private-a', 'private-b')]
        with closing(sqlite3.connect(self.app)) as con, con:
            con.execute('CREATE TABLE job_reviews(review_id TEXT,window_start TEXT,window_end TEXT,metadata_json TEXT)')
            con.execute('CREATE TABLE job_review_items(review_id TEXT,ordinal INTEGER,ats TEXT,job_id TEXT,snapshot_json TEXT,snapshot_sha256 TEXT)')
            con.execute('INSERT INTO job_reviews VALUES(?,?,?,?)', ('review', '2026-10-17T00:00:00Z',
                        '2026-10-17T12:00:00Z', canonical_json({'workflow': WORKFLOW, 'window_policy': DISCOVERY_WINDOW})))
            con.executemany('INSERT INTO job_review_items VALUES(?,?,?,?,?,?)',
                            [('review', n, j['ats'], j['id'], canonical_json(j), fingerprint(j)) for n, j in enumerate(self.jobs, 1)])
        with closing(sqlite3.connect(self.catalog)) as con, con:
            con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,posted_at TEXT,first_seen TEXT,closed_at TEXT,description TEXT,PRIMARY KEY(ats,id))')
            con.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?)', [tuple(j.values()) for j in self.jobs])

    def test_actual_store_replays_saved_window_without_source_changes_or_private_output(self):
        before = self.app.read_bytes(), self.catalog.read_bytes()
        report = replay.probe(self.app, self.catalog, 'review')
        self.assertTrue(report['frozen_replay']['actual_store_exercised'])
        self.assertTrue(report['frozen_replay']['indexed_equals_saved'])
        self.assertEqual(report['saved_payload_sha256'], report['frozen_replay']['payload_sha256'])
        self.assertEqual(report['saved_count'], 2)
        self.assertNotIn('private-a', json.dumps(report))
        self.assertNotIn('Private text', json.dumps(report))
        self.assertEqual(before, (self.app.read_bytes(), self.catalog.read_bytes()))
        with closing(replay.readonly(self.app)) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM job_reviews')

    def test_current_catalog_drift_is_distinct_from_algorithm_difference(self):
        with closing(sqlite3.connect(self.catalog)) as con, con:
            con.execute("UPDATE jobs SET description='Changed' WHERE id='private-a'")
            con.execute("UPDATE jobs SET closed_at='closed' WHERE id='private-b'")
            con.execute("INSERT INTO jobs SELECT ats,'private-new',posted_at,first_seen,NULL,description FROM jobs WHERE id='private-a'")
        before = self.catalog.read_bytes()
        report = replay.probe(self.app, self.catalog, 'review', compare_current=True)
        current = report['current_catalog']
        self.assertTrue(current['algorithms_equal'])
        self.assertEqual(current['changes_since_frozen_snapshot'], {'added': 1, 'removed': 1, 'changed': 1, 'unchanged': 0})
        self.assertNotEqual(current['payload_sha256'], report['saved_payload_sha256'])
        self.assertEqual(self.catalog.read_bytes(), before)
        with closing(replay.readonly(self.catalog)) as con:
            self.assertNotIn('jobs_discovery_candidates', [r[1] for r in con.execute('PRAGMA index_list(jobs)')])

    def test_live_comparison_holds_same_transaction_across_concurrent_update(self):
        with closing(sqlite3.connect(self.catalog)) as con:
            con.execute('PRAGMA journal_mode=WAL')
        select = replay.select_in_transaction
        def concurrent(con, start, end, *, indexed):
            value = select(con, start, end, indexed=indexed)
            attached_path = con.execute('PRAGMA database_list').fetchone()[2]
            if not indexed and Path(attached_path) == self.catalog:
                with closing(sqlite3.connect(self.catalog)) as writer, writer:
                    writer.execute("UPDATE jobs SET description='Later write'")
            return value
        with patch.object(replay, 'select_in_transaction', side_effect=concurrent):
            report = replay.probe(self.app, self.catalog, 'review', compare_current=True)
        self.assertTrue(report['current_catalog']['algorithms_equal'])
        self.assertEqual(report['current_catalog']['changes_since_frozen_snapshot']['unchanged'], 2)

    def test_corrupt_fingerprint_and_changed_saved_order_fail(self):
        with closing(sqlite3.connect(self.app)) as con, con:
            con.execute("UPDATE job_review_items SET snapshot_sha256='bad' WHERE ordinal=1")
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            replay.probe(self.app, None, 'review')
        with closing(sqlite3.connect(self.app)) as con, con:
            con.execute('UPDATE job_review_items SET snapshot_sha256=? WHERE ordinal=1', (fingerprint(self.jobs[0]),))
            con.execute('UPDATE job_review_items SET ordinal=3-ordinal')
        with self.assertRaisesRegex(AssertionError, 'ordering'):
            replay.probe(self.app, None, 'review')

    def test_saved_identity_and_ordinal_corruption_fail(self):
        original = self.app.read_bytes()
        for sql, error in (("UPDATE job_review_items SET job_id='different' WHERE ordinal=1", 'identity'),
                           ("UPDATE job_review_items SET ordinal=3 WHERE ordinal=1", 'ordinal')):
            with self.subTest(error=error):
                self.app.write_bytes(original)
                with closing(sqlite3.connect(self.app)) as con, con:
                    con.execute(sql)
                with self.assertRaisesRegex(ValueError, error):
                    replay.probe(self.app, None, 'review')


if __name__ == '__main__':
    unittest.main()
