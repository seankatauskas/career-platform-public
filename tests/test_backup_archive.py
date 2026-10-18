"""Streaming backup archives keep the existing checksum and recovery contract."""
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import unittest
from unittest.mock import patch

from job_search import aws_ops as ops
from tests import test_snapshot_pause_boundaries as fixtures


class BackupArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SnapshotPauseTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data, self.root = self.fixture.c, self.fixture.data, self.fixture.root
        self.stage = self.root / 'stage'; self.stage.mkdir()
        for name in ops.BACKUP_DIRS: shutil.copytree(self.data / name, self.stage / name)
        (self.stage / 'toolchain/immutable').write_bytes(b'original immutable binary')
        (self.stage / 'toolchain/immutable').chmod(0o400)
        self.bundle = self.root / 'backup.tar.gz'
        self.manifest = {'version': 1, 'backup_id': 'fixture', 'release_id': ops.release_path(self.c).name, 'secrets': {}}

    def build(self, **kwargs):
        return ops.build_backup_archive(self.stage, self.bundle, self.manifest, timings={}, **kwargs)

    def backup_boundaries(self):
        stack = ExitStack()
        stack.enter_context(patch.object(ops, 'running_services', return_value=['dashboard']))
        stack.enter_context(patch.object(ops, 'compose', return_value=''))
        return stack

    def test_streamed_checksums_match_archive_and_restored_payload_without_digest_rereads(self):
        with patch.object(ops, 'digest', side_effect=AssertionError('unexpected full reread')):
            checksum = self.build()
        self.assertEqual(ops.digest(self.bundle), checksum)
        with tarfile.open(self.bundle) as archive:
            self.assertEqual(archive.getnames()[-1], 'backup.json')
        restored = self.root / 'verified'; restored.mkdir()
        ops.safe_extract(self.bundle, restored)
        manifest = ops.verify_snapshot(restored)
        self.assertNotIn('backup.json', manifest['files'])
        for name, entry in manifest['files'].items():
            self.assertEqual(entry['sha256'], ops.digest(self.stage / name))
        self.assertEqual((restored / 'toolchain/immutable').stat().st_mode & 0o777, 0o400)

    def test_existing_restore_reads_manifest_last_and_replaces_data_with_verified_capture(self):
        checksum = self.build()
        self.fixture.update_sources(2)
        with patch.object(ops, 'running_services', return_value=[]), \
                patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'):
            result = ops.restore_unlocked(self.c, self.bundle, checksum, replace=True)
        self.assertEqual(result['status'], 'restored_paused')
        for path in self.fixture.databases:
            con = sqlite3.connect(path)
            try: self.assertEqual(con.execute('SELECT value FROM state').fetchone()[0], 1)
            finally: con.close()

    def test_corrupt_archive_checksum_is_rejected_before_replacing_data(self):
        checksum = self.build()
        with self.bundle.open('r+b') as stream:
            stream.seek(20); stream.write(b'corrupt archive')
        with patch.object(ops, 'running_services', return_value=[]), patch.object(ops, 'materialize_secrets') as secrets:
            with self.assertRaisesRegex(ops.OpsError, 'checksum mismatch'):
                ops.restore_unlocked(self.c, self.bundle, checksum, replace=True)
        secrets.assert_not_called()

    def test_partial_compressed_write_publishes_nothing_and_keeps_last_success(self):
        previous = b'{"backup_id":"previous"}\n'
        (self.data / 'last-backup.json').write_bytes(previous)
        original = ops._DigestWriter.write
        def short_write(writer, data):
            if len(data) > 100:
                class ShortStream:
                    def write(self, block): return len(block) - 1
                writer.stream = ShortStream()
            return original(writer, data)
        with self.backup_boundaries(), patch.object(ops._DigestWriter, 'write', short_write), patch.object(ops, 'aws') as upload:
            with self.assertRaisesRegex(ops.OpsError, 'short backup archive write'):
                ops.backup_unlocked(self.c)
        upload.assert_not_called()
        self.assertEqual((self.data / 'last-backup.json').read_bytes(), previous)

    def test_growing_payload_is_rejected_before_upload(self):
        original = ops._DigestReader.__init__
        def grow(reader, stream, *args):
            original(reader, stream, *args)
            with Path(stream.name).open('ab') as out: out.write(b'new bytes')
        with self.backup_boundaries(), patch.object(ops._DigestReader, '__init__', grow), patch.object(ops, 'aws') as upload:
            with self.assertRaisesRegex(ops.OpsError, 'capture changed'):
                ops.backup_unlocked(self.c)
        upload.assert_not_called()
        self.assertFalse((self.data / 'last-backup.json').exists())

    def test_short_payload_read_or_failed_gzip_finalization_never_publishes_success(self):
        previous = b'{"backup_id":"previous"}\n'
        (self.data / 'last-backup.json').write_bytes(previous)
        close = ops.gzip.GzipFile.close
        def fail_finalize(compressed):
            was_open = compressed.fileobj is not None
            close(compressed)
            if was_open: raise OSError('gzip finalization failed')
        failures = (
            patch.object(ops._DigestReader, 'read', return_value=b''),
            patch.object(ops.gzip.GzipFile, 'close', fail_finalize),
        )
        for failure in failures:
            with self.subTest(failure=failure), self.backup_boundaries(), failure, patch.object(ops, 'aws') as upload:
                with self.assertRaises(OSError): ops.backup_unlocked(self.c)
            upload.assert_not_called()
            self.assertEqual((self.data / 'last-backup.json').read_bytes(), previous)

    def test_failed_archive_flush_never_uploads_or_advances_receipt(self):
        previous = b'{"backup_id":"previous"}\n'
        (self.data / 'last-backup.json').write_bytes(previous)
        with self.backup_boundaries(), patch.object(ops, 'sync_tree', side_effect=OSError('flush failed')), patch.object(ops, 'aws') as upload:
            with self.assertRaisesRegex(OSError, 'flush failed'): ops.backup_unlocked(self.c)
        upload.assert_not_called()
        self.assertEqual((self.data / 'last-backup.json').read_bytes(), previous)

    def test_receipt_upload_failure_retains_previous_success_after_archive_upload(self):
        previous = b'{"backup_id":"previous"}\n'
        (self.data / 'last-backup.json').write_bytes(previous)
        with self.backup_boundaries(), patch.object(ops, 'aws', side_effect=['', ops.OpsError('receipt upload failed')]) as upload:
            with self.assertRaisesRegex(ops.OpsError, 'receipt upload failed'): ops.backup_unlocked(self.c)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual((self.data / 'last-backup.json').read_bytes(), previous)

    def test_success_receipt_records_complete_archive_sync_and_upload_timings(self):
        with self.backup_boundaries(), patch.object(ops, 'aws', return_value=''):
            result = ops.backup_unlocked(self.c)
        receipt = json.loads((self.data / 'last-backup.json').read_text())
        for name in ('archive_build', 'compression', 'archive_hash', 'destination_hash', 'sqlite_check', 'sync', 'archive_upload'):
            self.assertIn(name, receipt['timings_seconds'])
        self.assertIn('receipt_upload', result['timings_seconds'])
        self.assertIn('receipt_upload', receipt['timings_seconds'])
        self.assertEqual(receipt['compression']['level'], ops.BACKUP_COMPRESSION_LEVEL)

    def test_archive_rejects_unsafe_members_and_invalid_compression_before_publication(self):
        (self.stage / 'state/unsafe').symlink_to(self.stage / 'state/catalog.db')
        with self.assertRaisesRegex(ops.OpsError, 'link or special'): self.build()
        for level in (0, 10, True):
            with self.subTest(level=level), self.assertRaisesRegex(ops.OpsError, 'compression level'):
                self.build(compression_level=level)


if __name__ == '__main__': unittest.main()
