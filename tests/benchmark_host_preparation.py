"""Bounded fictional preparation benchmark; never use a production data directory.

Use a Linux local volume with --evict for advisory eviction of ONLY fixture files.
No global cache dropping, AWS calls, service stops or real databases are involved.
"""
from __future__ import annotations
import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import json
import mmap
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import tempfile
import threading
import time
from unittest.mock import patch

from job_search import aws_ops as ops
from tests.benchmark_deployment_phases import seed


def io_counts():
    path = Path('/proc/self/io')
    return {k: int(v) for k, v in (line.split(':') for line in path.read_text().splitlines())} if path.exists() else {}


def evict(root):
    if not hasattr(os, 'posix_fadvise'):
        raise RuntimeError('--evict requires Linux posix_fadvise; never substitute global drop_caches')
    for path in root.rglob('*'):
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    # Check residency without touching pages. This measures the coldness of our
    # files, never evicts unrelated applications' caches or reads their memory.
    resident = pages = 0
    libc = ctypes.CDLL(None, use_errno=True)
    for path in root.rglob('*'):
        if not path.is_file() or not path.stat().st_size: continue
        with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_COPY) as memory:
            count = (len(memory) + mmap.PAGESIZE - 1) // mmap.PAGESIZE
            vector = (ctypes.c_ubyte * count)()
            buffer = ctypes.c_char.from_buffer(memory)
            rc = libc.mincore(ctypes.c_void_p(ctypes.addressof(buffer)), ctypes.c_size_t(len(memory)), vector)
            del buffer
            if rc: raise OSError(ctypes.get_errno(), 'mincore failed')
            resident += sum(bool(value & 1) for value in vector); pages += count
    return {'resident_pages': resident, 'total_pages': pages}


@contextmanager
def probe(path):
    """A separate fictional SQLite request every 20 ms on the same volume."""
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, value BLOB)')
    con.execute('INSERT OR IGNORE INTO requests VALUES(1, zeroblob(8192))')
    con.commit(); con.close()
    stop = threading.Event(); latencies = []; failures = []
    def read():
        db = sqlite3.connect(path)
        try:
            while not stop.is_set():
                started = time.monotonic()
                assert len(db.execute('SELECT value FROM requests WHERE id=1').fetchone()[0]) == 8192
                latencies.append(time.monotonic() - started)
                stop.wait(.02)
        except Exception as error: failures.append(error)
        finally: db.close()
    thread = threading.Thread(target=read); thread.start()
    try: yield latencies
    finally: stop.set(); thread.join()
    if failures: raise failures[0]


@contextmanager
def checks(workers):
    """Benchmark-only parallel hypothesis; join all checks before final capture."""
    records = []; futures = []; check = ops.sqlite_check
    def timed(path):
        start = time.monotonic(); cpu = time.thread_time()
        try: return check(path)
        finally: records.append({'file': str(path).split('snapshot-preparing-', 1)[-1].split('/', 1)[-1],
            'bytes': path.stat().st_size, 'wall_seconds': time.monotonic() - start,
            'cpu_seconds': time.thread_time() - cpu})
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        def submit(path):
            if not workers: return timed(path)
            futures.append(pool.submit(timed, path))
        with patch.object(ops, 'sqlite_check', side_effect=submit):
            yield records, futures
        for future in futures: future.result()


def main():
    global ops
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--megabytes', type=int, default=128)
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--evict', action='store_true')
    parser.add_argument('--check-workers', type=int, choices=(0, 2), default=0)
    parser.add_argument('--ops-source', type=Path)
    parser.add_argument('--modes', nargs='+', choices=('cold', 'reusable', 'changed', 'changed_largest', 'changed_after', 'wal', 'rollback', 'invalid'),
                        default=['cold', 'reusable', 'changed', 'changed_after', 'wal', 'invalid'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 32 <= args.megabytes <= 12288 or not 1 <= args.runs <= 5:
        parser.error('use 32–12288 MiB and 1–5 trials')
    if args.modes[0] != 'cold': parser.error('start with cold to create the first trusted snapshot')
    if args.ops_source:
        spec = importlib.util.spec_from_file_location('job_search.benchmark_ops', args.ops_source)
        ops = importlib.util.module_from_spec(spec); spec.loader.exec_module(ops)
    checks_original = ops.sqlite_check
    report = {'scope': 'fictional local files; no production downtime measurement',
              'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
              'source_sha256': ops.digest(Path(ops.__file__)),
              'fixture_mib': args.megabytes, 'check_workers': args.check_workers,
              'os_cache': 'fixture-only fsync + POSIX_FADV_DONTNEED (advisory)' if args.evict else 'not evicted',
              'measurements': []}
    with tempfile.TemporaryDirectory(prefix='career-preparation-bench-') as temporary:
        root = Path(temporary).resolve(); seed(root, args.megabytes)
        c = {'data_root': str(root), 'secret_arns': {}}
        with probe(root / 'probe.db') as idle:
            time.sleep(1)
        idle.sort()
        report['idle_probe'] = {'count': len(idle), 'p95_ms': idle[int(.95 * (len(idle)-1))] * 1000,
                                'max_ms': max(idle) * 1000}
        previous = None
        for trial in range(args.runs):
            for mode in args.modes:
                if mode == 'cold':
                    shutil.rmtree(root / 'operations', ignore_errors=True)
                elif mode in ('changed', 'changed_largest'):
                    db = sqlite3.connect(root / ('state/jobs.db' if mode == 'changed_largest' else 'state/owners.db'))
                    db.execute('UPDATE records SET company=? WHERE id=0', (f'Fictional changed {trial}',))
                    db.commit(); db.close()
                elif mode == 'invalid':
                    (previous / 'backup.json').write_text('{"invalid":true}')
                wal = None
                if mode == 'wal':
                    wal = sqlite3.connect(root / 'state/history.db')
                    wal.execute('PRAGMA journal_mode=WAL'); wal.execute('PRAGMA wal_autocheckpoint=0')
                    wal.execute('UPDATE records SET company=? WHERE id=0', (f'Fictional WAL {trial}',)); wal.commit()
                if mode == 'rollback':
                    db = sqlite3.connect(root / 'state/owners.db')
                    db.execute('PRAGMA journal_mode=PERSIST')
                    db.execute('UPDATE records SET company=? WHERE id=0', (f'Fictional journal {trial}',)); db.commit(); db.close()
                residency = evict(root) if args.evict else None
                begin_io = io_counts(); begin_cpu = time.process_time(); begin = time.monotonic()
                with probe(root / 'probe.db') as latencies, checks(args.check_workers) as (validation, futures):
                    with ops.prepare_snapshot(c) as prepared:
                        for future in futures: future.result()
                        seconds = time.monotonic() - begin
                        cpu = time.process_time() - begin_cpu
                        end_io = io_counts()
                        preparation = dict(prepared['timings_seconds'])
                        preparation_latencies = list(latencies)
                        if mode == 'changed_after':
                            db = sqlite3.connect(root / 'state/owners.db')
                            db.execute('UPDATE records SET company=? WHERE id=0', (f'Fictional later {trial}',))
                            db.commit(); db.close()
                        # Remove the probe/check patches before the paused path.
                        with patch.object(ops, 'sqlite_check', new=checks_original):
                            paused = time.monotonic()
                            receipt = ops.local_snapshot_unlocked(c, release_id='benchmark', prepared=prepared)
                            paused_seconds = time.monotonic() - paused
                if wal: wal.close()
                snapshot = ops.local_backup_path(c, receipt)
                print(json.dumps({'event': 'capture_complete', 'trial': trial, 'mode': mode,
                                  'preparation_seconds': seconds, 'paused_snapshot_seconds': paused_seconds}), flush=True)
                verification_started = time.monotonic()
                ops.verify_snapshot(snapshot)
                verification_seconds = time.monotonic() - verification_started
                operation = ops.Operation.begin(c, 'deploy', backup=receipt)
                operation.finish('deployed')
                if previous: shutil.rmtree(previous)
                previous = snapshot
                ordered = sorted(preparation_latencies)
                row = {'mode': mode, 'trial': trial, 'preparation_seconds': seconds, 'cpu_seconds': cpu,
                       'source_residency_before': residency,
                       'io': {k: end_io[k] - begin_io[k] for k in end_io},
                       'paused_snapshot_seconds': paused_seconds, 'preparation_timings': preparation,
                       'restore_verification_seconds': verification_seconds,
                       'per_file_checks': validation, 'receipt': receipt,
                       'probe': {'count': len(ordered), 'p95_ms': ordered[int(.95 * (len(ordered)-1))] * 1000,
                                 'max_ms': max(ordered) * 1000}}
                report['measurements'].append(row)
                print(json.dumps({k: row[k] for k in ('trial', 'mode', 'preparation_seconds', 'paused_snapshot_seconds')}), flush=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
