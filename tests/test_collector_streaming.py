"""Bounded collection persists observations without premature absence claims."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from job_search.collection import boards as jb


class StreamingTests(unittest.TestCase):
    def test_pending_results_are_bounded_even_when_consumer_is_slow(self):
        class Future:
            def __init__(self, value): self.value = value
            def result(self): return self.value
        class Pool:
            submitted = 0
            def submit(self, function, item):
                self.submitted += 1
                return Future(function(item))
        pool = Pool()
        results = jb.bounded_results(pool, lambda n: n, range(100), 8)
        for consumed, value in enumerate(results, 1):
            self.assertEqual(value, consumed - 1)
            self.assertLessEqual(pool.submitted - consumed, 7)
        self.assertEqual(pool.submitted, 100)

    def run_scan(self, root, *, fail_save=False, new_only=False):
        board_file, db, out = root/'boards.json', root/'jobs.db', root/'run'
        board_file.write_text(json.dumps({'ashby': ['a', 'b', 'bad']}))
        base = {field: '' for field in jb.FIELDS}
        old = [dict(base, ats='ashby', id=slug+'-old', company=slug, title='Old')
               for slug in ('a', 'b', 'bad', 'untouched')]
        jb.save(old, db, '2000-01-01T00:00:00+00:00')
        def scan(ats, slug, *args, **kwargs):
            if slug == 'bad': raise ValueError('fixture failed board')
            if kwargs.get('meta') is not None: kwargs['meta']['etag'] = slug+'-etag'
            return [dict(base, ats=ats, id=slug, company=slug, title='New',
                         description='Full description'), old[0]] if slug == 'a' else [dict(base, ats=ats, id=slug, company=slug, title='New')]
        original = jb.save
        writes = []
        def save(rows, *args, **kwargs):
            writes.append(len(rows))
            if fail_save and len(writes) == 2: raise RuntimeError('interrupted persistence')
            return original(rows, *args, **kwargs)
        argv = ['boards', '--ats', 'ashby', '--all', '--no-export', '--boards-from',
                str(board_file), '--db', str(db), '--out', str(out)]
        if new_only: argv.append('--new-only')
        with patch.object(sys, 'argv', argv), patch.object(jb, 'scan_board', side_effect=scan), \
             patch.object(jb, 'save', side_effect=save), patch.object(jb, 'COLLECTION_BATCH_ROWS', 1), \
             contextlib.redirect_stderr(io.StringIO()):
            if fail_save:
                with self.assertRaisesRegex(RuntimeError, 'interrupted'): jb.main()
            else: jb.main()
        return db, out, writes

    def test_completed_scan_closes_only_observed_boards_and_preserves_counts(self):
        with tempfile.TemporaryDirectory() as d:
            db, out, writes = self.run_scan(Path(d))
            self.assertGreater(len(writes), 2)
            with sqlite3.connect(db) as con:
                rows = dict(con.execute('SELECT id,closed_at FROM jobs'))
                self.assertIsNotNone(rows['b-old'])
                for key in ('a-old', 'bad-old', 'untouched-old', 'a', 'b'):
                    self.assertIsNone(rows[key])
                self.assertEqual(con.execute("SELECT description FROM jobs WHERE id='a'").fetchone()[0], 'Full description')
            receipt = json.loads(Path(str(out)+'.collection.json').read_text())
            self.assertEqual(receipt['postings_returned'], 3)
            self.assertEqual(receipt['boards_failed'], 1)
            self.assertEqual(receipt['status'], 'partial')

    def test_interrupted_scan_cannot_close_postings_or_publish_etags(self):
        with tempfile.TemporaryDirectory() as d:
            db, out, _ = self.run_scan(Path(d), fail_save=True)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM jobs WHERE closed_at IS NOT NULL').fetchone()[0], 0)
                self.assertTrue(con.execute("SELECT 1 FROM jobs WHERE id='a'").fetchone())
            self.assertEqual(jb.load_etags(db), {})
            self.assertFalse(Path(str(out)+'.collection.json').exists())

    def test_new_only_counts_only_new_jobs_and_never_closes_missing_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            db, out, _ = self.run_scan(Path(d), new_only=True)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM jobs WHERE closed_at IS NOT NULL').fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT last_seen FROM jobs WHERE id='a-old'").fetchone()[0], '2000-01-01T00:00:00+00:00')
            self.assertEqual(json.loads(Path(str(out)+'.collection.json').read_text())['postings_returned'], 2)
            self.assertEqual(len(jb.load_etags(db)), 2)


if __name__ == '__main__':
    unittest.main()
