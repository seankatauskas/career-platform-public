"""Candidate-image cache acceptance; standalone stdlib code sent through Docker."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile


REPRESENTATIVE_MODULES = ('argparse', 'pathlib', 'json.decoder', 'job_search.cloud', 'job_search.worker', 'job_search.tool_service')


def check_cache(source: Path, *, require_immutable: bool = True) -> None:
    cached = Path(importlib.util.cache_from_source(str(source)))
    header = cached.read_bytes()[:16]
    assert header[:4] == importlib.util.MAGIC_NUMBER, f'{source.name}: wrong bytecode magic'
    assert int.from_bytes(header[4:8], 'little') == 3, f'{source.name}: cache is not checked-hash bytecode'
    assert header[8:16] == importlib.util.source_hash(source.read_bytes()), f'{source.name}: cache source hash differs'
    if require_immutable:
        assert not os.access(cached, os.W_OK), f'{source.name}: runtime can write cache'
        assert not os.access(cached.parent, os.W_OK), f'{source.name}: runtime can replace cache'
        try:
            with cached.open('r+b'):
                pass
        except OSError:
            pass
        else:
            raise AssertionError(f'{source.name}: runtime opened cache for writing')


def stale_source_probe() -> dict:
    with tempfile.TemporaryDirectory(prefix='checked-pyc-', dir='/tmp') as directory:
        source = Path(directory) / 'fixture_cached_source.py'
        source.write_text("VALUE = 'before'\n")
        cache = Path(py_compile.compile(str(source), doraise=True,
                        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH))
        cached_bytes = cache.read_bytes()
        metadata = source.stat()
        command = [sys.executable, '-I', '-B', '-c',
                   'import sys; sys.path.insert(0,sys.argv[1]); import fixture_cached_source; print(fixture_cached_source.VALUE)',
                   directory]
        assert subprocess.check_output(command, text=True).strip() == 'before'
        source.write_text("VALUE = 'after!'\n")
        os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        assert source.stat().st_size == metadata.st_size
        assert source.stat().st_mtime_ns == metadata.st_mtime_ns
        # Timestamp-based bytecode would still look valid. Checked hashes must
        # load the changed source even when runtime bytecode writes are disabled.
        assert subprocess.check_output(command, text=True).strip() == 'after!'
        assert cache.read_bytes() == cached_bytes
    return {'stale_source_rejected': True, 'size_and_mtime_preserved': True, 'cache_not_rewritten': True}


def main() -> None:
    assert os.geteuid() != 0, 'image probe must run as its unprivileged runtime user'
    for name in REPRESENTATIVE_MODULES:
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin, f'{name}: missing source module'
        check_cache(Path(spec.origin))
    print(json.dumps({'checked_hash_modules': list(REPRESENTATIVE_MODULES),
                      'runtime_cache_read_only': True, **stale_source_probe()}))


if __name__ == '__main__':
    main()
