"""Full-pass equivalence and failure boundaries for incremental family preparation."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.collection import dedupe, dedupe_incremental


class IncrementalFamiliesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.paths = [Path(self.directory.name) / name for name in ('incremental.db', 'full.db')]
        self.description = 'Design reliable distributed systems and support production services. ' * 12
        for path in self.paths:
            with closing(sqlite3.connect(path)) as con, con:
                con.execute('CREATE TABLE jobs (ats TEXT,id TEXT,company TEXT,title TEXT,description TEXT,department TEXT,team TEXT,location TEXT,closed_at TEXT,last_seen TEXT,PRIMARY KEY(ats,id))')
                con.executemany('INSERT INTO jobs(ats,id,company,title,description,last_seen) VALUES (?,?,?,?,?,?)', [
                    ('ashby', 'a', 'Acme', 'Engineer', self.description, 'first'),
                    ('ashby', 'b', 'Acme', 'Engineer', self.description, 'first'),
                    ('ashby', 'c', 'Acme', 'Engineer', self.description + ' Great opportunity.', 'first'),
                    ('ashby', 'd', 'Acme', 'Manager', self.description, 'first'),
                    ('lever', 'e', 'Other', 'Engineer', 'Thin text', 'first'),
                ])
            dedupe.prepare_families(path)

    def snapshot(self, path):
        with closing(sqlite3.connect(path)) as con:
            return {table: con.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()
                    for table in dedupe_incremental.DERIVED}

    def mutate(self, sql, params=()):
        for path in self.paths:
            with closing(sqlite3.connect(path)) as con, con:
                con.execute(sql, params)

    def compare(self):
        delta = dedupe.prepare_families(self.paths[0], incremental=True)
        full = dedupe.prepare_families(self.paths[1])
        self.assertEqual({key: delta[key] for key in full}, full)
        self.assertEqual(self.snapshot(self.paths[0]), self.snapshot(self.paths[1]))
        self.assertTrue(dedupe.prepared_families_are_current(self.paths[0]))

    def test_inserts_deletes_moves_edits_and_historical_lineages_match_full_pass(self):
        mutations = [
            ('INSERT INTO jobs(ats,id,company,title,description) VALUES (?,?,?,?,?)', ('ashby', '0', 'Acme', 'Engineer', self.description)),
            ("UPDATE jobs SET description=? WHERE id='c'", (self.description + ' Different technologies and architecture.',)),
            ("UPDATE jobs SET title='Manager' WHERE id='a'", ()),
            ("UPDATE jobs SET company='Other' WHERE id='b'", ()),
            ("UPDATE jobs SET ats='lever' WHERE id='b'", ()),
            ("UPDATE jobs SET title='Engineer',company='Other' WHERE id='d'", ()),
            ("UPDATE jobs SET title='Engineer',company='Acme',ats='ashby' WHERE id='b'", ()),
            ("UPDATE jobs SET description='' WHERE id='0'", ()),
            ("UPDATE jobs SET id='changed-key' WHERE id='c'", ()),
            ("UPDATE jobs SET closed_at='closed',department='Architecture',last_seen='second' WHERE id='d'", ()),
            ("DELETE FROM jobs WHERE id IN ('a','b','changed-key')", ()),
            ("DELETE FROM jobs", ()),
        ]
        for sql, params in mutations:
            with self.subTest(sql=sql):
                self.mutate(sql, params)
                self.compare()

    def test_unrelated_descriptions_are_never_normalized(self):
        path = self.paths[0]
        with closing(sqlite3.connect(path)) as con, con:
            con.executemany('INSERT INTO jobs(ats,id,company,title,description) VALUES (?,?,?,?,?)',
                            [('lever', str(i), 'Unrelated', str(i), self.description) for i in range(100)])
        dedupe.prepare_families(path)
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("UPDATE jobs SET description=? WHERE id='a'", (self.description + ' Added work.',))
        build = dedupe._build_rows
        visited = []
        def record(rows):
            rows = list(rows)
            visited.extend(row['id'] for row in rows)
            return build(rows)
        with patch.object(dedupe, '_build_rows', side_effect=record):
            result = dedupe.prepare_families(path, incremental=True)
        self.assertEqual(result["changed_blocks"], 1)
        self.assertEqual(result["examined_jobs"], 3)
        self.assertEqual(set(visited), {'a', 'b', 'c'})
        self.assertTrue(dedupe.prepared_families_are_current(path))

    def test_observation_only_update_needs_no_rebuild(self):
        self.mutate("UPDATE jobs SET last_seen='new observation'")
        with patch.object(dedupe, '_build_rows', side_effect=AssertionError('unexpected normalization')):
            self.assertTrue(dedupe.prepared_families_are_current(self.paths[0]))
            dedupe.prepare_families(self.paths[0], incremental=True)

    def test_rollback_keeps_previous_metadata_and_pending_dirty_keys(self):
        path = self.paths[0]
        before = self.snapshot(path)
        self.mutate("UPDATE jobs SET title='Moved' WHERE id='a'")
        original = dedupe._upsert_derived_rows
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('interrupted publication')
        with patch.object(dedupe, '_upsert_derived_rows', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                dedupe.prepare_families(path, incremental=True)
        self.assertEqual(before, self.snapshot(path))
        self.assertFalse(dedupe.prepared_families_are_current(path))
        self.compare()

    def test_concurrent_source_commit_aborts_incremental_publication(self):
        path = self.paths[0]
        with closing(sqlite3.connect(path)) as con, con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute("UPDATE jobs SET title='Moved' WHERE id='a'")
        before = self.snapshot(path)
        build = dedupe._build_rows
        wrote = False
        def concurrent(rows):
            nonlocal wrote
            values = build(rows)
            if not wrote:
                wrote = True
                with closing(sqlite3.connect(path, timeout=0)) as writer, writer:
                    writer.execute("UPDATE jobs SET description='Concurrent edit' WHERE id='b'")
            return values
        with patch.object(dedupe, '_build_rows', side_effect=concurrent):
            with self.assertRaises(sqlite3.OperationalError):
                dedupe.prepare_families(path, incremental=True)
        self.assertTrue(wrote)
        self.assertEqual(before, self.snapshot(path))
        self.assertFalse(dedupe.prepared_families_are_current(path))
        dedupe.prepare_families(path, incremental=True)
        self.assertTrue(dedupe.prepared_families_are_current(path))

    def test_source_rollback_does_not_leave_dirty_keys(self):
        with closing(sqlite3.connect(self.paths[0])) as con:
            con.execute("UPDATE jobs SET description='rolled back'")
            con.rollback()
        self.assertTrue(dedupe.prepared_families_are_current(self.paths[0]))

    def test_dropped_trigger_and_damaged_block_index_force_full_repair(self):
        for damage in ('DROP TRIGGER family_preparation_jobs_update',
                       "UPDATE family_preparation_blocks SET block_title='wrong' WHERE job_id='a'",
                       'DELETE FROM family_preparation_state',
                       'DROP INDEX family_preparation_block_lookup'):
            with self.subTest(damage=damage):
                path = self.paths[0]
                with closing(sqlite3.connect(path)) as con, con:
                    con.execute(damage)
                    con.execute("UPDATE jobs SET description=description || ' changed' WHERE id='a'")
                self.assertFalse(dedupe.prepared_families_are_current(path))
                with patch.object(dedupe_incremental, 'seal_full', wraps=dedupe_incremental.seal_full) as seal:
                    dedupe.prepare_families(path, incremental=True)
                seal.assert_called_once()
                self.assertTrue(dedupe.prepared_families_are_current(path))

    def test_missing_dirty_event_forces_full_repair(self):
        path = self.paths[0]
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("UPDATE jobs SET description='New description' WHERE id='a'")
            con.execute('DELETE FROM family_preparation_dirty')
        self.assertFalse(dedupe.prepared_families_are_current(path))
        with patch.object(dedupe_incremental, 'seal_full', wraps=dedupe_incremental.seal_full) as seal:
            dedupe.prepare_families(path, incremental=True)
        seal.assert_called_once()
        self.assertTrue(dedupe.prepared_families_are_current(path))

    def test_changed_dirty_event_key_forces_full_repair(self):
        path = self.paths[0]
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("UPDATE jobs SET description='New description' WHERE id='a'")
            con.execute("UPDATE family_preparation_dirty SET job_id='e'")
        self.assertFalse(dedupe.prepared_families_are_current(path))
        with patch.object(dedupe_incremental, 'seal_full', wraps=dedupe_incremental.seal_full) as seal:
            dedupe.prepare_families(path, incremental=True)
        seal.assert_called_once()
        self.assertTrue(dedupe.prepared_families_are_current(path))

    def test_derived_mutation_and_normalizer_change_force_full_repair(self):
        path = self.paths[0]
        with closing(sqlite3.connect(path)) as con, con:
            con.execute('UPDATE job_families SET member_count=99')
        self.assertFalse(dedupe.prepared_families_are_current(path))
        dedupe.prepare_families(path, incremental=True)
        self.assertEqual(self.snapshot(path), self.snapshot(self.paths[1]))
        with patch.object(dedupe, 'NORMALIZATION_VERSION', 2):
            self.assertFalse(dedupe.prepared_families_are_current(path))
            dedupe.prepare_families(path, incremental=True)
            self.assertTrue(dedupe.prepared_families_are_current(path))


if __name__ == '__main__':
    unittest.main()
