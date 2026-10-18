"""Compare gzip levels and archive read passes using bounded fictional state.

Run: python -m tests.benchmark_backup_archive --megabytes 64 --runs 3 \
    --output .cache/backup-archive.json
Each measurement uses a fresh subprocess and independent captured fixture files.
No real database, credentials, Docker, network or AWS is accessed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import resource
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from unittest.mock import patch

from job_search import aws_ops as ops


def fixture(root: Path, megabytes: int) -> None:
    for name in ops.BACKUP_DIRS: (root / name).mkdir(parents=True)
    randomizer = random.Random(20261017)
    titles = ['Software Engineer', 'Infrastructure Engineer', 'Product Designer', 'Data Analyst']
    skills = ['Python', 'JavaScript', 'SQL', 'Linux', 'customer support', 'project planning']
    def rows():
        for i in range(megabytes * 160):
            title = randomizer.choice(titles)
            text = (f'Fictional employer {i % 307}. {title}. Build reliable services with '
                    f'{randomizer.choice(skills)} and {randomizer.choice(skills)}. '
                    'Collaborate with colleagues, review changes, document decisions and investigate failures. '
                    'This fictional posting is used only for an offline storage benchmark. ')
            body = text * 9 + f'Requisition {i}: ' + randomizer.randbytes(48).hex()
            yield i, title, body
    db = sqlite3.connect(root / 'state/catalog.db')
    try:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, title TEXT, description TEXT)')
        db.executemany('INSERT INTO jobs VALUES(?,?,?)', rows())
        db.execute('CREATE INDEX by_title ON jobs(title)'); db.commit()
    finally: db.close()
    # Already compressed/encrypted-like binary assets complement compressible
    # database/text state; no fixture is claimed to match production ratios.
    with (root / 'toolchain/assets.bin').open('wb') as stream:
        for _ in range(max(1, megabytes // 5)):
            stream.write(randomizer.randbytes(1024**2))
    with (root / 'hermes/history.txt').open('w') as stream:
        for i in range(megabytes * 80):
            stream.write(f'Fictional conversation {i}: ' + 'Plan tasks and preserve decisions. ' * 40 + '\n')


def legacy_archive(stage: Path, bundle: Path, manifest: dict, timings: dict) -> str:
    files = {}
    for path in stage.rglob('*'):
        if not path.is_file(): continue
        with ops.measured(timings, 'sqlite_check'): ops.sqlite_check(path)
        with ops.measured(timings, 'destination_hash'):
            files[str(path.relative_to(stage))] = {'sha256': ops.digest(path), 'size': path.stat().st_size}
    manifest['files'] = files
    ops.write_json(stage / 'backup.json', manifest)
    with ops.measured(timings, 'compression'):
        with tarfile.open(bundle, 'w:gz', compresslevel=9) as archive:
            for path in sorted(stage.rglob('*')):
                archive.add(path, arcname=str(path.relative_to(stage)), recursive=False)
    with ops.measured(timings, 'archive_hash'): return ops.digest(bundle)


def run_case(source: Path, implementation: str, level: int, evict: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix='archive-case-') as temp:
        root = Path(temp).resolve(); stage = root / 'payload'; shutil.copytree(source, stage)
        data_bytes = sum(path.stat().st_size for path in stage.rglob('*') if path.is_file())
        evicted = evict and hasattr(os, 'posix_fadvise')
        if evicted:
            ops.sync_tree(stage)
            for path in stage.rglob('*'):
                if path.is_file():
                    with path.open('rb') as stream:
                        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        manifest = {'version': 1, 'backup_id': 'fictional-benchmark', 'release_id': 'benchmark', 'secrets': {}}
        bundle = root / 'backup.tar.gz'; timings = {}; reread_bytes = 0; reread_calls = 0
        original_digest = ops.digest
        def counted_digest(path):
            nonlocal reread_bytes, reread_calls
            reread_bytes += path.stat().st_size; reread_calls += 1
            return original_digest(path)
        started = time.monotonic(); cpu = time.process_time()
        with patch.object(ops, 'digest', side_effect=counted_digest):
            if implementation == 'legacy': checksum = legacy_archive(stage, bundle, manifest, timings)
            else: checksum = ops.build_backup_archive(stage, bundle, manifest, timings=timings, compression_level=level)
            with ops.measured(timings, 'sync'): ops.sync_tree(root)
        elapsed = time.monotonic() - started; cpu_seconds = time.process_time() - cpu
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == 'darwin' else peak * 1024
        # Validation is deliberately outside measured archive work.
        assert ops.digest(bundle) == checksum
        restored = root / 'restored'; restored.mkdir()
        ops.safe_extract(bundle, restored); result = ops.verify_snapshot(restored)
        for name, metadata in result['files'].items():
            assert metadata['sha256'] == ops.digest(source / name)
        return {'implementation': implementation, 'gzip_level': level, 'payload_bytes': data_bytes,
                'archive_bytes': bundle.stat().st_size, 'seconds': round(elapsed, 6),
                'cpu_seconds': round(cpu_seconds, 6), 'process_peak_rss_bytes': peak_bytes,
                'extra_digest_read_bytes': reread_bytes, 'extra_digest_calls': reread_calls,
                'file_cache_eviction_requested': evict, 'file_cache_eviction_supported': bool(evicted),
                'timings_seconds': {k: round(v, 6) for k, v in timings.items()},
                'full_restore_verification_passed': True}


def benchmark(megabytes: int, runs: int, evict: bool) -> dict:
    if not 4 <= megabytes <= 256 or not 1 <= runs <= 5:
        raise ValueError('use 4–256 MiB and 1–5 runs')
    with tempfile.TemporaryDirectory(prefix='archive-fixture-') as temp:
        source = Path(temp).resolve(); fixture(source, megabytes); measurements = []
        cases = [('legacy', 9), *[('streaming', n) for n in (1, 3, 6, 9)]]
        for trial in range(runs):
            order = cases if trial % 2 == 0 else list(reversed(cases))
            for implementation, level in order:
                command = [sys.executable, '-m', 'tests.benchmark_backup_archive', '--source', str(source), '--case', implementation, '--level', str(level)]
                if evict: command.append('--evict')
                result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=300)
                row = json.loads(result.stdout); row['trial'] = trial; measurements.append(row)
                print(json.dumps({'trial': trial, 'implementation': implementation, 'gzip_level': level,
                                  'seconds': row['seconds'], 'archive_bytes': row['archive_bytes']}),
                      file=sys.stderr, flush=True)
        medians = []
        for implementation, level in cases:
            rows = [r for r in measurements if r['implementation'] == implementation and r['gzip_level'] == level]
            medians.append({'implementation': implementation, 'gzip_level': level,
                            **{key: statistics.median(r[key] for r in rows) for key in
                               ('seconds', 'cpu_seconds', 'archive_bytes', 'process_peak_rss_bytes', 'extra_digest_read_bytes')}})
        return {'scope': 'fictional SQLite, binary and text; local archive work, not production or AWS downtime',
                'platform': sys.platform, 'python': sys.version.split()[0], 'runs': runs,
                'requested_fixture_megabytes': megabytes, 'measurements': measurements, 'medians': medians}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--megabytes', type=int, default=64)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--evict', action='store_true')
    parser.add_argument('--case', choices=('legacy', 'streaming'), help=argparse.SUPPRESS)
    parser.add_argument('--level', type=int, default=9, help=argparse.SUPPRESS)
    parser.add_argument('--source', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case: report = run_case(args.source, args.case, args.level, args.evict)
    else: report = benchmark(args.megabytes, args.runs, args.evict)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
