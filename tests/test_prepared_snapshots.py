"""Speculative copies must prove equality to the stopped cross-file state."""
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from job_search import aws_ops as ops
from tests import test_job_search_aws_ops as fixtures


class PreparedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OperationsTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data = self.fixture.c, self.fixture.data
        ops.write_json(self.data / 'materialized-secrets.json', {})
        self.asset = self.data / 'toolchain/bundle'
        self.asset.write_bytes(b'fictional asset\n' * 100000)
        self.db = self.data / 'state/jobs.db'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, text BLOB)')
            db.executemany('INSERT INTO jobs(text) VALUES(?)', [(b'fictional job\n' * 1000,)] * 100)

    def capture(self, prepared):
        receipt = ops.local_snapshot_unlocked(self.c, prepared=prepared)
        stage = ops.local_backup_path(self.c, receipt)
        return receipt, stage, ops.verify_snapshot(stage)

    def test_unchanged_large_files_are_independent_and_checked_before_pause(self):
        with ops.prepare_snapshot(self.c) as prepared:
            with patch.object(ops, 'sqlite_check', wraps=ops.sqlite_check) as check:
                receipt = ops.local_snapshot_unlocked(self.c, prepared=prepared)
                check.assert_not_called()
            stage = ops.local_backup_path(self.c, receipt)
            self.assertEqual(receipt['reuse']['reused_files'], 2)
            self.assertEqual((stage / 'state/jobs.db').read_bytes(), self.db.read_bytes())
            self.assertNotEqual(self.db.stat().st_ino, (stage / 'state/jobs.db').stat().st_ino)
            self.asset.write_bytes(b'new user work')
            ops.verify_snapshot(stage)
        self.assertFalse(prepared['root'].exists())

    def test_same_size_same_mtime_change_cannot_reuse_old_asset(self):
        before = self.asset.stat()
        with ops.prepare_snapshot(self.c) as prepared:
            self.asset.write_bytes(b'X' * before.st_size)
            os.utime(self.asset, ns=(before.st_atime_ns, before.st_mtime_ns))
            receipt, stage, _ = self.capture(prepared)
        self.assertEqual((stage / 'toolchain/bundle').read_bytes(), b'X' * before.st_size)
        self.assertEqual(receipt['reuse']['reused_files'], 1)

    def test_committed_wal_record_is_retained_even_when_main_file_matches(self):
        with sqlite3.connect(self.db) as db: db.execute('PRAGMA journal_mode=WAL')
        db.close()
        with ops.prepare_snapshot(self.c) as prepared:
            con = sqlite3.connect(self.db)
            try:
                con.execute('PRAGMA wal_autocheckpoint=0')
                con.execute("UPDATE jobs SET text='committed after preparation' WHERE id=1"); con.commit()
                self.assertEqual(ops.digest(self.db), prepared['files']['state/jobs.db']['sha256'])
                receipt, stage, _ = self.capture(prepared)
                with sqlite3.connect(stage / 'state/jobs.db') as restored:
                    self.assertEqual(restored.execute('SELECT text FROM jobs WHERE id=1').fetchone()[0], 'committed after preparation')
                self.assertEqual(receipt['reuse']['reused_files'], 1)
            finally: con.close()

    def test_new_deleted_renamed_and_empty_directories_follow_final_state(self):
        with ops.prepare_snapshot(self.c) as prepared:
            self.asset.unlink()
            self.db.rename(self.db.with_name('renamed.db'))
            (self.data / 'state/empty').mkdir()
            (self.data / 'hermes/new.txt').write_text('new')
            _, stage, manifest = self.capture(prepared)
        self.assertNotIn('toolchain/bundle', manifest['files'])
        self.assertNotIn('state/jobs.db', manifest['files'])
        self.assertIn('state/renamed.db', manifest['files'])
        self.assertIn('state/empty', manifest['directories'])
        self.assertEqual((stage / 'hermes/new.txt').read_text(), 'new')

    def test_torn_speculative_database_is_discarded_and_recaptured(self):
        check = ops.sqlite_check
        def reject_candidate(path):
            if 'snapshot-preparing-' in str(path) and path.name == 'jobs.db':
                raise sqlite3.DatabaseError('fictional torn live copy')
            return check(path)
        with patch.object(ops, 'sqlite_check', side_effect=reject_candidate):
            with ops.prepare_snapshot(self.c) as prepared:
                self.assertNotIn('state/jobs.db', prepared['files'])
                receipt, _, _ = self.capture(prepared)
                self.assertEqual(receipt['reuse']['reused_files'], 1)

    def test_nonempty_rollback_journal_uses_sqlite_even_if_main_matches(self):
        with ops.prepare_snapshot(self.c) as prepared:
            journal = Path(str(self.db) + '-journal'); journal.write_bytes(bytes(512))
            entries = ops.runtime_entries
            def journal_first(*args, **kwargs):
                return iter(sorted(entries(*args, **kwargs), key=lambda pair: not pair[0].name.endswith('-journal')))
            with patch.object(ops, 'runtime_entries', side_effect=journal_first):
                receipt, stage, manifest = self.capture(prepared)
            self.assertEqual(receipt['reuse']['reused_files'], 1)
            self.assertNotIn('state/jobs.db-journal', manifest['files'])
            self.assertFalse((stage / 'state/jobs.db-journal').exists())

    def test_unrelated_journal_named_files_are_retained(self):
        (self.data / 'state/notes-journal').write_text('fictional durable notes')
        (self.data / 'state/notes').write_text('not a SQLite database')
        (self.data / 'state/orphan-journal').write_text('fictional standalone file')
        with ops.prepare_snapshot(self.c) as prepared:
            _, stage, _ = self.capture(prepared)
        self.assertEqual((stage / 'state/notes-journal').read_text(), 'fictional durable notes')
        self.assertEqual((stage / 'state/orphan-journal').read_text(), 'fictional standalone file')

    def test_immutable_predecessor_requires_original_report_hash(self):
        self.db.rename(self.db.with_name('predecessor.sqlite'))
        self.db = self.db.with_name('predecessor.sqlite'); self.db.chmod(0o400)
        ops.write_json(self.db.parent / 'conversion-report.json', {'archive_sha256': ops.digest(self.db)})
        with ops.prepare_snapshot(self.c) as prepared:
            _, stage, _ = self.capture(prepared)
            self.assertEqual(ops.digest(stage / 'state/predecessor.sqlite'), ops.digest(self.db))
            self.assertEqual((stage / 'state/predecessor.sqlite').stat().st_mode & 0o777, 0o400)
        with ops.prepare_snapshot(self.c) as prepared:
            ops.write_json(self.db.parent / 'conversion-report.json', {'archive_sha256': 'a' * 64})
            with self.assertRaisesRegex(ops.OpsError, 'immutable'):
                self.capture(prepared)

    def test_preparation_io_failure_does_not_stop_services_or_begin_operation(self):
        self.fixture.install_candidate()
        with patch.object(ops, 'compose', return_value='') as compose, patch.object(ops, 'preflight', return_value={'issues': []}), patch.object(ops, 'sync_tree', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): ops.deploy(self.c, 'b' * 40 + '-2')
        self.assertFalse(any(call.args[1] == 'stop' for call in compose.call_args_list))
        self.assertIsNone(ops.read_operation(self.c))
        self.assertFalse(list((self.data / 'backups').iterdir()))

    def test_sync_failure_after_reuse_prevents_initialization(self):
        self.fixture.install_candidate()
        sync = ops.sync_tree
        def fail_final(path):
            if path.name.startswith('snapshot-pending-'): raise OSError('flush failed')
            sync(path)
        with patch.object(ops, 'compose', return_value='') as compose, patch.object(ops, 'preflight', return_value={'issues': []}), patch.object(ops, 'sync_tree', side_effect=fail_final):
            with self.assertRaises(OSError): ops.deploy(self.c, 'b' * 40 + '-2')
        self.assertFalse(any('initialize' in call.args for call in compose.call_args_list))
        self.assertIsNone(ops.read_operation(self.c)['backup'])
        self.assertEqual(ops.release_path(self.c).name, 'a' * 40 + '-1')

    def test_reuse_directory_sync_failure_cannot_authorize_initialization(self):
        self.fixture.install_candidate()
        sync = ops.sync_directory
        def fail_unlink(path):
            if path.parent.name.startswith('snapshot-preparing-'):
                raise OSError('directory flush failed')
            sync(path)
        with patch.object(ops, 'compose', return_value='') as compose, patch.object(ops, 'preflight', return_value={'issues': []}), patch.object(ops, 'sync_directory', side_effect=fail_unlink):
            with self.assertRaises(OSError): ops.deploy(self.c, 'b' * 40 + '-2')
        self.assertFalse(any('initialize' in call.args for call in compose.call_args_list))
        self.assertIsNone(ops.read_operation(self.c)['backup'])
        self.assertEqual(ops.release_path(self.c).name, 'a' * 40 + '-1')

    def test_downtime_counts_stopping_to_health_but_not_drain_or_preparation(self):
        value = {'phase_times': {'draining': '2026-10-09T12:00:00+00:00',
                                 'stopping': '2026-10-09T12:05:00+00:00'},
                 'active_services': ['dashboard'],
                 'downtime_started_at': '2026-10-09T12:05:00+00:00',
                 'downtime_finished_at': '2026-10-09T12:06:30+00:00'}
        op = ops.Operation(self.c, value)
        self.assertEqual(op.durations()['drain'], 300)
        self.assertEqual(op.durations()['downtime'], 90)
        value['active_services'] = []
        self.assertEqual(op.durations()['downtime'], 0)
        self.assertEqual(op.durations()['maintenance_window'], 90)

    def test_upgrade_rollback_and_failed_initialize_restore_prepared_bytes(self):
        self.fixture.install_candidate()
        original = ops.digest(self.db)
        with patch.object(ops, 'compose', return_value=''), patch.object(ops, 'preflight', return_value={'issues': []}), patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'):
            upgrade = ops.deploy(self.c, 'b' * 40 + '-2')
            rollback = ops.deploy(self.c, 'a' * 40 + '-1', rollback=True)
            self.assertEqual(upgrade['backup']['reuse']['reused_files'], 2)
            self.assertEqual(rollback['backup']['reuse']['reused_files'], 2)
            def initialize(c, *args, **kwargs):
                if 'initialize' in args:
                    self.db.write_bytes(b'failed migration')
                    self.asset.write_bytes(b'failed file migration')
                    raise ops.OpsError('initialization failed')
                return ''
            with patch.object(ops, 'compose', side_effect=initialize):
                with self.assertRaisesRegex(ops.OpsError, 'initialization failed'):
                    ops.deploy(self.c, 'b' * 40 + '-2')
        self.assertEqual(ops.digest(self.db), original)
        self.assertTrue(self.asset.read_bytes().startswith(b'fictional asset'))
        self.assertEqual(ops.release_path(self.c).name, 'a' * 40 + '-1')
        for key in ('initialize', 'service_startup', 'downtime', 'total_including_cleanup'):
            self.assertIn(key, upgrade['timings_seconds'])


if __name__ == '__main__': unittest.main()
