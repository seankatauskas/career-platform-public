"""Exact-content validation reuse never substitutes for a recovery snapshot."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from job_search import aws_ops as ops
from tests import test_prepared_snapshots as fixtures


class PreparationReferenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreparedSnapshotTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data, self.db = self.fixture.c, self.fixture.data, self.fixture.db

    def anchor(self):
        with ops.prepare_snapshot(self.c) as prepared:
            receipt, stage, _ = self.fixture.capture(prepared)
        op = ops.Operation.begin(self.c, 'deploy', backup=receipt)
        op.finish('deployed')
        return receipt, stage

    def edit_manifest(self, stage, edit):
        path = stage / 'backup.json'; value = json.loads(path.read_text()); edit(value)
        ops.write_json(path, value)
        op = ops.Operation(self.c, ops.read_operation(self.c))
        op.value['backup']['sha256'] = ops.digest(path)
        op.update('deployed')

    def assert_checked(self):
        with patch.object(ops, 'sqlite_check', wraps=ops.sqlite_check) as check:
            with ops.prepare_snapshot(self.c) as prepared:
                self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'checked')
                self.assertEqual(check.call_count, 1)

    def test_exact_bytes_reuse_checks_but_copies_remain_independent_and_restore_rechecks(self):
        _, prior = self.anchor()
        with patch.object(ops, 'sqlite_check', wraps=ops.sqlite_check) as check:
            with ops.prepare_snapshot(self.c) as prepared:
                check.assert_not_called()
                self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'reused')
                candidate = prepared['root'] / 'state/jobs.db'
                self.assertEqual(len({candidate.stat().st_ino, self.db.stat().st_ino,
                                     (prior / 'state/jobs.db').stat().st_ino}), 3)
                receipt = ops.local_snapshot_unlocked(self.c, prepared=prepared)
        restored = ops.local_backup_path(self.c, receipt)
        with patch.object(ops, 'sqlite_check', wraps=ops.sqlite_check) as check:
            manifest = ops.verify_snapshot(restored)
            self.assertTrue(any(call.args[0].name == 'jobs.db' for call in check.call_args_list))
        self.assertEqual(manifest['preparation']['files']['state/jobs.db']['validation'], 'reused')
        self.assertEqual(manifest['preparation']['files']['state/jobs.db']['cutover'], 'reused')
        self.assertEqual(receipt['preparation_validation']['reused'], 1)
        self.assertNotIn('jobs.db', json.dumps(receipt))  # names stay private
        with (restored / 'state/jobs.db').open('r+b') as stream:
            stream.seek(4096); stream.write(b'corrupt after publication')
        with self.assertRaisesRegex(ops.OpsError, 'checksum mismatch'):
            ops.verify_snapshot(restored)

    def test_stream_digest_matches_destination_without_a_second_digest_read(self):
        with patch.object(ops, 'digest', side_effect=AssertionError('extra destination read')):
            with ops.prepare_snapshot(self.c) as prepared:
                for name, expected in prepared['files'].items():
                    self.assertEqual(hashlib.sha256((prepared['root'] / name).read_bytes()).hexdigest(), expected['sha256'])
                    self.assertEqual((prepared['root'] / name).stat().st_size, expected['size'])

    def test_same_size_and_mtime_changed_database_is_checked(self):
        self.anchor(); before = self.db.stat()
        db = sqlite3.connect(self.db)
        db.execute("UPDATE jobs SET text=replace(text,'fictional','different') WHERE id=1")
        db.commit(); db.close()
        self.assertEqual(self.db.stat().st_size, before.st_size)
        os.utime(self.db, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assert_checked()

    def test_changed_after_cached_preparation_still_uses_sqlite_backup_and_checks(self):
        self.anchor()
        with ops.prepare_snapshot(self.c) as prepared:
            self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'reused')
            db = sqlite3.connect(self.db); db.execute("UPDATE jobs SET text='new committed work' WHERE id=1")
            db.commit(); db.close()
            with patch.object(ops, 'sqlite_check', wraps=ops.sqlite_check) as check:
                receipt, stage, _ = self.fixture.capture(prepared)
            self.assertTrue(any(call.args[0].name == 'jobs.db' for call in check.call_args_list))
            self.assertEqual(receipt['reuse']['reused_files'], 1)
            self.assertEqual(prepared['file_profiles']['state/jobs.db']['cutover'], 'recaptured')
            db = sqlite3.connect(stage / 'state/jobs.db')
            self.assertEqual(db.execute('SELECT text FROM jobs WHERE id=1').fetchone()[0], 'new committed work')
            db.close()

    def test_wal_commits_are_preserved_when_main_file_validation_hits(self):
        db = sqlite3.connect(self.db); db.execute('PRAGMA journal_mode=WAL'); db.close()
        self.anchor()
        db = sqlite3.connect(self.db)
        try:
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("UPDATE jobs SET text='committed in WAL' WHERE id=1"); db.commit()
            with ops.prepare_snapshot(self.c) as prepared:
                self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'reused')
                receipt, stage, _ = self.fixture.capture(prepared)
            self.assertEqual(receipt['reuse']['reused_files'], 1)
            restored = sqlite3.connect(stage / 'state/jobs.db')
            self.assertEqual(restored.execute('SELECT text FROM jobs WHERE id=1').fetchone()[0], 'committed in WAL')
            restored.close()
        finally: db.close()

    def test_persisted_rollback_journal_forces_final_backup_despite_cache_hit(self):
        db = sqlite3.connect(self.db); db.execute('PRAGMA journal_mode=PERSIST')
        db.execute("UPDATE jobs SET text='committed persistent journal' WHERE id=1"); db.commit(); db.close()
        journal = Path(str(self.db) + '-journal')
        self.assertGreater(journal.stat().st_size, 0)
        # Prime the proof with these exact main-file bytes, then leave the real
        # nonhot PERSIST journal beside the source for both later phases.
        saved = journal.read_bytes(); journal.unlink()
        self.anchor(); journal.write_bytes(saved)
        with ops.prepare_snapshot(self.c) as prepared:
            self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'reused')
            receipt, stage, _ = self.fixture.capture(prepared)
        self.assertEqual(receipt['reuse']['reused_files'], 1)
        self.assertFalse((stage / 'state/jobs.db-journal').exists())
        db = sqlite3.connect(stage / 'state/jobs.db')
        self.assertEqual(db.execute('SELECT text FROM jobs WHERE id=1').fetchone()[0], 'committed persistent journal')
        db.close()

    def test_unanchored_or_incomplete_snapshot_is_not_a_verification_cache(self):
        self.anchor()
        path = self.data / 'operations/current.json'
        original = path.read_bytes(); path.unlink(); self.assert_checked()
        path.write_bytes(original); path.chmod(0o600)
        value = json.loads(original); value['complete'] = False; ops.write_json(path, value)
        self.assert_checked()

    def test_hot_rollback_journal_cannot_authorize_a_snapshot_from_cached_main_bytes(self):
        self.anchor()
        before = (self.data / 'operations/current.json').read_bytes()
        code = '''import os,signal,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=1')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE jobs SET text=replace(text,'fictional','different')")
os.kill(os.getpid(),signal.SIGKILL)
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.db)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        journal = Path(str(self.db) + '-journal')
        self.assertGreater(journal.stat().st_size, 512)
        with ops.prepare_snapshot(self.c) as prepared:
            with self.assertRaises(sqlite3.OperationalError):
                ops.local_snapshot_unlocked(self.c, prepared=prepared)
        self.assertEqual((self.data / 'operations/current.json').read_bytes(), before)
        self.assertTrue(journal.exists())
        self.assertEqual(len(list((self.data / 'backups').glob('*.snapshot'))), 1)

    def test_manifest_must_match_journal_digest(self):
        _, stage = self.anchor()
        (stage / 'backup.json').write_text('{"not":"the committed manifest"}')
        self.assert_checked()

    def test_missing_legacy_or_wrong_engine_contract_is_a_cache_miss(self):
        for contract in (None, {'method': 'sqlite-quick-check-v1', 'sqlite_version': 'old'},
                         {'method': 'unknown', 'sqlite_version': sqlite3.sqlite_version}):
            with self.subTest(contract=contract):
                _, stage = self.anchor()
                self.edit_manifest(stage, lambda value: value.update(sqlite_verification=contract))
                self.assert_checked()

    def test_wrong_file_digest_size_or_shape_is_a_cache_miss(self):
        for metadata in ({'sha256': 'a'*64, 'size': self.db.stat().st_size},
                         {'sha256': ops.digest(self.db), 'size': 1}, None, []):
            with self.subTest(metadata=metadata):
                _, stage = self.anchor()
                self.edit_manifest(stage, lambda value: value['files'].update({'state/jobs.db': metadata}))
                self.assert_checked()

    def test_private_metadata_permissions_links_and_owner_are_required(self):
        _, stage = self.anchor()
        for path, mode in ((stage, 0o700), (stage / 'backup.json', 0o600),
                           (self.data / 'operations', 0o700), (self.data / 'operations/current.json', 0o600)):
            with self.subTest(path=path):
                path.chmod(mode | 0o044); self.assert_checked(); path.chmod(mode)
        manifest = stage / 'backup.json'; original = manifest.read_bytes()
        link = stage / 'duplicate'; os.link(manifest, link)
        self.assert_checked(); link.unlink()
        other = self.data / 'other-manifest'; manifest.rename(other); manifest.symlink_to(other)
        self.assert_checked(); manifest.unlink(); other.rename(manifest)
        fstat = os.fstat
        def different_owner(fd):
            values = list(fstat(fd)); values[4] += 1
            return os.stat_result(values)
        with patch.object(ops.os, 'fstat', side_effect=different_owner):
            self.assertEqual(ops.preparation_reference(self.c)[0], {})
        self.assertEqual(manifest.read_bytes(), original)

    def test_truncated_or_corrupt_candidate_never_gains_a_cache_proof(self):
        self.anchor()
        with self.db.open('r+b') as stream:
            stream.seek(4096); stream.write(b'\xff' * 4096)
        with ops.prepare_snapshot(self.c) as prepared:
            self.assertNotIn('state/jobs.db', prepared['files'])
            self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'discarded')

    def test_sigkill_during_warm_preparation_preserves_prior_proof_and_never_publishes(self):
        _, stage = self.anchor()
        journal = (self.data / 'operations/current.json').read_bytes()
        manifest = (stage / 'backup.json').read_bytes()
        code = '''import json,os,signal,sys
from job_search import aws_ops as ops
c=json.loads(sys.stdin.read())
def die(path): os.kill(os.getpid(),signal.SIGKILL)
ops.sync_tree=die
with ops.prepare_snapshot(c): raise AssertionError('must not yield')
'''
        result = subprocess.run([sys.executable, '-c', code], input=json.dumps(self.c), text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertEqual((self.data / 'operations/current.json').read_bytes(), journal)
        self.assertEqual((stage / 'backup.json').read_bytes(), manifest)
        self.assertEqual(len(list((self.data / 'backups').glob('*.snapshot'))), 1)
        self.assertFalse((self.data / 'maintenance/gate.json').exists())
        with ops.prepare_snapshot(self.c) as prepared:
            self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'reused')

    def test_cached_validation_does_not_bypass_copy_io_failure_or_stop_services(self):
        self.fixture.fixture.install_candidate()
        self.anchor()
        before = (self.data / 'operations/current.json').read_bytes()
        opened = ops.os.open
        def fail_source(path, *args, **kwargs):
            if Path(path) == self.db: raise OSError('fictional source I/O failure')
            return opened(path, *args, **kwargs)
        with patch.object(ops, 'compose', return_value='') as compose, \
                patch.object(ops, 'preflight', return_value={'issues': []}), \
                patch.object(ops.os, 'open', side_effect=fail_source):
            with self.assertRaisesRegex(OSError, 'source I/O failure'):
                ops.deploy(self.c, 'b' * 40 + '-2')
        self.assertFalse(any('stop' in call.args or 'initialize' in call.args for call in compose.call_args_list))
        self.assertEqual((self.data / 'operations/current.json').read_bytes(), before)


if __name__ == '__main__': unittest.main()
