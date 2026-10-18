"""Bounded overlapping hashes must describe independent copied bytes or fail."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from job_search import aws_ops as ops


class PreparationCopyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.source, self.target = root / 'source', root / 'target'
        with self.source.open('wb') as stream:
            for i in range(32): stream.write(bytes([i]) * 1024**2)

    def test_large_copy_hashes_exact_bytes_and_returns_complete_metadata(self):
        timings = {}
        result = ops.copy_preparation_file(self.source, self.target, self.source.stat().st_size, timings)
        self.assertEqual(result, (ops.digest(self.target), self.source.stat().st_size, bytes(16)))
        self.assertEqual(result[0], ops.digest(self.source))
        self.assertNotEqual(self.source.stat().st_ino, self.target.stat().st_ino)
        self.assertGreater(timings['hash_in_copy'], 0)

    def test_early_source_eof_never_claims_uncopied_bytes(self):
        self.source.write_bytes(b'fictional short candidate')
        result = ops.copy_preparation_file(self.source, self.target, 32 * 1024**2, {})
        self.assertEqual(result, (ops.digest(self.target), self.source.stat().st_size,
                                  self.source.read_bytes()[:16]))

    def test_hash_failure_is_propagated_and_worker_is_joined(self):
        workers = []
        class BrokenHasher:
            def update(self, block):
                workers.append(threading.current_thread())
                raise ValueError('fictional hasher failure')
        with patch.object(ops.hashlib, 'sha256', return_value=BrokenHasher()):
            with self.assertRaisesRegex(ValueError, 'hasher failure'):
                ops.copy_preparation_file(self.source, self.target, self.source.stat().st_size, {})
        self.assertTrue(workers)
        self.assertTrue(all(not thread.is_alive() for thread in workers))

    def test_short_write_cannot_produce_successful_metadata(self):
        original_open = Path.open
        class ShortWriter:
            def __enter__(self):
                self.stream = original_open(self_target, 'xb'); return self
            def __exit__(self, *args): self.stream.close()
            def write(self, block): return self.stream.write(block[:1])
        self_target = self.target
        def opening(path, *args, **kwargs):
            if path == self_target: return ShortWriter()
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', opening):
            with self.assertRaisesRegex(OSError, 'short preparation copy'):
                ops.copy_preparation_file(self.source, self.target, self.source.stat().st_size, {})
        self.assertEqual(self.target.stat().st_size, 1)


if __name__ == '__main__': unittest.main()
