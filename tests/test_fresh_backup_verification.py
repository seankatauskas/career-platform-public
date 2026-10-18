"""Routine backups certify fresh exact bytes only after complete publication."""
from contextlib import ExitStack
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
from tests import test_preparation_reference as fixtures


class FreshBackupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreparationReferenceTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data, self.db = self.fixture.c, self.fixture.data, self.fixture.db
        self.anchor, _ = self.fixture.anchor()
        self.proof = self.data / 'operations/last-backup-verification.json'
        self.current = self.data / 'operations/current.json'

    def boundaries(self):
        stack = ExitStack()
        stack.enter_context(patch.object(ops, 'running_services', return_value=[]))
        stack.enter_context(patch.object(ops, 'compose', return_value=''))
        stack.enter_context(patch.object(ops, 'aws', return_value=''))
        return stack

    def backup(self):
        with self.boundaries(): return ops.backup_unlocked(self.c)

    def assert_fallback(self):
        self.assertEqual(ops.preparation_reference(self.c)[1:], ('available', self.anchor['backup_id']))

    def test_changed_catalog_gets_fresh_byte_identical_proof_and_later_changes_miss(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE jobs SET text='new since deployment' WHERE id=1")
        result = self.backup()
        self.assertEqual(result['preparation_proof'], 'published')
        proof = json.loads(self.proof.read_text())
        self.assertEqual(proof['files']['state/jobs.db']['sha256'], ops.digest(self.db))
        self.assertEqual(ops.preparation_reference(self.c)[1:], ('available_backup', result['backup_id']))
        with patch.object(ops, 'sqlite_check', wraps=ops.sqlite_check) as check:
            with ops.prepare_snapshot(self.c) as prepared:
                self.assertEqual(prepared['file_profiles']['state/jobs.db']['validation'], 'reused')
        check.assert_not_called()
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE jobs SET text='changed after backup' WHERE id=1")
        self.fixture.assert_checked()

    def test_repeated_backups_replace_one_proof_without_inheriting_unearned_fresh_proof(self):
        first = self.backup(); original_id = ops.read_operation(self.c)['operation_id']
        second = self.backup(); current = ops.read_operation(self.c)
        self.assertNotEqual(first['backup_id'], second['backup_id'])
        self.assertNotEqual(original_id, current['operation_id'])
        self.assertEqual(current['backup_verification']['operation_id'], current['operation_id'])
        self.assertEqual(len(list(self.proof.parent.glob('last-backup-verification*'))), 1)
        operation = ops.Operation.begin(self.c, 'backup', previous_release=ops.release_path(self.c).name)
        self.assertNotIn('backup_verification', operation.value)
        operation.finish('snapshot_finished')  # Resume alone certifies nothing new.
        self.assert_fallback()

    def test_validation_and_both_upload_failures_never_earn_fresh_proof(self):
        for failure in ('capture', 'check', 'archive', 'receipt'):
            with self.subTest(failure=failure):
                self.backup()  # Existing proof must not become the new backup's proof.
                with self.boundaries(), ExitStack() as stack:
                    if failure == 'capture':
                        stack.enter_context(patch.object(ops, 'copy_snapshot', side_effect=ops.OpsError('capture failed')))
                    elif failure == 'check':
                        stack.enter_context(patch.object(ops, 'sqlite_check', side_effect=sqlite3.DatabaseError('bad copy')))
                    else:
                        effects = [ops.OpsError('upload failed')] if failure == 'archive' else ['', ops.OpsError('upload failed')]
                        stack.enter_context(patch.object(ops, 'aws', side_effect=effects))
                    with self.assertRaises((sqlite3.DatabaseError, ops.OpsError)):
                        ops.backup_unlocked(self.c)
                self.assertNotIn('backup_verification', ops.read_operation(self.c))
                self.assert_fallback()

    def test_optional_proof_write_and_journal_failure_preserve_success_and_deploy_anchor(self):
        for failure in ('proof', 'journal'):
            write, update = ops.write_json, ops.Operation.update
            def writing(path, value, *args, **kwargs):
                if failure == 'proof' and path == self.proof: raise OSError('proof failure')
                return write(path, value, *args, **kwargs)
            def updating(operation, phase, **fields):
                if failure == 'journal' and 'backup_verification' in fields: raise OSError('journal failure')
                return update(operation, phase, **fields)
            with self.subTest(failure=failure), self.boundaries(), patch.object(ops, 'write_json', writing), patch.object(ops.Operation, 'update', updating):
                result = ops.backup_unlocked(self.c)
            self.assertEqual(result['preparation_proof'], 'unavailable')
            self.assertEqual(json.loads((self.data / 'last-backup.json').read_text())['backup_id'], result['backup_id'])
            self.assert_fallback()

    def test_process_death_between_proof_write_and_journal_binding_never_certifies_it(self):
        code = """import json,os,signal,sys
from job_search import aws_ops as ops
c=json.loads(sys.stdin.read())
ops.running_services=lambda c: []
ops.drain_workers=lambda *a: None
ops.project_containers=lambda: []
ops.compose=lambda *a,**kw: ''
ops.aws=lambda *a: ''
update=ops.Operation.update
def interrupted(self, phase, **fields):
    if 'backup_verification' in fields: os.kill(os.getpid(), signal.SIGKILL)
    return update(self, phase, **fields)
ops.Operation.update=interrupted
ops.backup_unlocked(c)
"""
        child = subprocess.run([sys.executable, '-c', code], input=json.dumps(self.c),
                               text=True, capture_output=True, timeout=20)
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        self.assertTrue(self.proof.exists())
        self.assertNotIn('backup_verification', ops.read_operation(self.c))
        self.assert_fallback()

    def test_proof_fields_and_current_operation_identity_are_bound(self):
        self.backup(); original_proof = self.proof.read_bytes(); original_journal = self.current.read_bytes()
        cases = [('backup_id', 'different'), ('operation_id', 'f' * 32), ('release_id', 'different'),
                 ('archive_sha256', 'f' * 64), ('sqlite_verification', {'method': 'unknown'}), ('format', 'unknown')]
        for field, value in cases:
            with self.subTest(field=field):
                proof = json.loads(original_proof); proof[field] = value; ops.write_json(self.proof, proof)
                journal = json.loads(original_journal); journal['backup_verification']['sha256'] = ops.digest(self.proof)
                ops.write_json(self.current, journal); self.assert_fallback()
        self.proof.write_bytes(original_proof); self.current.write_bytes(original_journal)
        journal = json.loads(original_journal); journal['complete'] = False; ops.write_json(self.current, journal)
        self.assertEqual(ops.preparation_reference(self.c)[0], {})

    def test_missing_damaged_linked_public_or_oversized_proof_falls_back(self):
        self.backup(); original = self.proof.read_bytes()
        for damage in ('missing', 'modified', 'symlink', 'hardlink', 'public', 'oversized'):
            with self.subTest(damage=damage):
                self.proof.unlink(missing_ok=True); self.proof.write_bytes(original); self.proof.chmod(0o600)
                other = self.proof.with_name('fictional-proof-target'); other.unlink(missing_ok=True)
                if damage == 'missing': self.proof.unlink()
                elif damage == 'modified': self.proof.write_text('{}')
                elif damage == 'symlink':
                    self.proof.rename(other); self.proof.symlink_to(other)
                elif damage == 'hardlink': os.link(self.proof, other)
                elif damage == 'public': self.proof.chmod(0o644)
                else:
                    with self.proof.open('r+b') as stream: stream.truncate(64 * 1024**2 + 1)
                self.assert_fallback()
                self.proof.unlink(missing_ok=True); other.unlink(missing_ok=True)

    def test_untrusted_current_or_parent_never_certifies_fresh_bytes(self):
        self.backup()
        for path, original in ((self.current, 0o600), (self.current.parent, 0o700)):
            path.chmod(0o755)
            self.assertEqual(ops.preparation_reference(self.c)[0], {})
            path.chmod(original)

    def test_raw_copy_is_explicit_and_wal_commits_use_backup_api(self):
        root = self.data.parent
        ops.copy_snapshot(self.db.parent, root / 'online')
        self.assertNotEqual(ops.digest(root / 'online/jobs.db'), ops.digest(self.db))
        ops.copy_snapshot(self.db.parent, root / 'stopped', quiesced_raw_sqlite=True)
        self.assertEqual(ops.digest(root / 'stopped/jobs.db'), ops.digest(self.db))
        con = sqlite3.connect(self.db)
        try:
            con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA wal_autocheckpoint=0')
            con.execute("UPDATE jobs SET text='committed WAL value' WHERE id=1"); con.commit()
            self.assertFalse(ops.sqlite_sidecars_empty(self.db))
            ops.copy_snapshot(self.db.parent, root / 'wal', quiesced_raw_sqlite=True)
            with sqlite3.connect(root / 'wal/jobs.db') as copied:
                self.assertEqual(copied.execute('SELECT text FROM jobs WHERE id=1').fetchone()[0], 'committed WAL value')
        finally: con.close()

    def test_nonregular_sidecars_fail_before_sqlite_can_open_them(self):
        sidecar = Path(str(self.db) + '-wal')
        for kind in ('directory', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'directory': sidecar.mkdir()
                else: os.mkfifo(sidecar)
                with patch.object(ops.sqlite3, 'connect', side_effect=AssertionError('unsafe sidecar opened')):
                    with self.assertRaisesRegex(ops.OpsError, 'not a regular file'):
                        ops.copy_snapshot(self.db.parent, self.data.parent / kind, quiesced_raw_sqlite=True)
                if kind == 'directory': sidecar.rmdir()
                else: sidecar.unlink()

    def test_raw_copy_rejects_sidecar_symlinks_and_hot_rollback_journal(self):
        sidecar = Path(str(self.db) + '-wal'); sidecar.symlink_to(self.db)
        with self.assertRaises(ops.OpsError):
            ops.copy_snapshot(self.db.parent, self.data.parent / 'bad', quiesced_raw_sqlite=True)
        sidecar.unlink()
        code = '''import os,signal,sqlite3,sys
con=sqlite3.connect(sys.argv[1]);con.execute('PRAGMA cache_size=1');con.execute('BEGIN IMMEDIATE')
con.execute("UPDATE jobs SET text=replace(text,'fictional','different')")
os.kill(os.getpid(),signal.SIGKILL)
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.db)], capture_output=True, timeout=20)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        journal = Path(str(self.db) + '-journal'); self.assertGreater(journal.stat().st_size, 512)
        with self.assertRaises(sqlite3.OperationalError):
            ops.copy_snapshot(self.db.parent, self.data.parent / 'hot', quiesced_raw_sqlite=True)
        self.assertTrue(journal.exists())


if __name__ == '__main__': unittest.main()
