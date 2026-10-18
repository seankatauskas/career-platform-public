"""Compare identical full quick_check with ordinary and bounded mapped reads.

Fictional collected-jobs schema, overflow descriptions, updates and deletes only.
Run on a bounded local Linux Docker volume with --evict; never on production.
Each check runs in its own process so peak RSS is attributable to that check.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

from job_search import aws_ops as ops
from job_search.collection import boards
from tests.benchmark_host_preparation import evict, io_counts

# Experimental only: production checks retain ordinary SQLite reads.
MMAP_BYTES = 2 * 1024**3


def seed(path, megabytes):
    rows = megabytes * 1024**2 // 16384
    body = 'Fictional job description. ' * 640
    with sqlite3.connect(path) as con:
        boards._prepare(con)
        con.executemany('INSERT INTO jobs(ats,id,company,description,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                        (('ashby', str(i), f'Fictional company {i % 101}', body,
                          '2026-10-01T00:00:00Z', '2026-10-17T00:00:00Z') for i in range(rows)))
        con.commit()
        # Growing records interleave new overflow pages with existing records.
        con.executemany("UPDATE jobs SET description=? WHERE ats='ashby' AND id=?",
                        ((body * 2, str(i)) for i in range(0, rows, 3)))
        con.executemany("DELETE FROM jobs WHERE ats='ashby' AND id=?",
                        ((str(i),) for i in range(0, rows, 11)))
    ops.sync_tree(path.parent)


def check(path, mapped_bytes):
    before = io_counts(); start = time.monotonic(); cpu = time.process_time()
    con = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        con.execute(f'PRAGMA mmap_size={mapped_bytes}')
        effective = con.execute('PRAGMA mmap_size').fetchone()
        result = con.execute('PRAGMA quick_check').fetchall()
        if result != [('ok',)]: raise RuntimeError('fixture integrity failed')
    finally: con.close()
    after = io_counts()
    return {'seconds': time.monotonic() - start, 'cpu_seconds': time.process_time() - cpu,
            'requested_mmap_bytes': mapped_bytes, 'effective_mmap_bytes': effective[0] if effective else 0,
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'physical_read_bytes': after.get('read_bytes', 0) - before.get('read_bytes', 0), 'result': 'ok'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--megabytes', type=int, default=128)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--evict', action='store_true')
    parser.add_argument('--output', type=Path)
    # Internal subprocess inputs only read the generated independent fixture.
    parser.add_argument('--check', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--mapped-bytes', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(args.check.resolve(), args.mapped_bytes))); return
    if not 16 <= args.megabytes <= 4096 or not 1 <= args.runs <= 5:
        parser.error('use 16–4096 MiB and 1–5 runs')
    measurements = []
    with tempfile.TemporaryDirectory(prefix='sqlite-check-benchmark-') as temporary:
        root = Path(temporary).resolve(); path = root / 'fictional-jobs.db'; seed(path, args.megabytes)
        before = ops.digest(path)
        for trial in range(args.runs):
            order = ('default', 'mapped') if trial % 2 == 0 else ('mapped', 'default')
            for mode in order:
                residency = evict(root) if args.evict else None
                result = subprocess.run([sys.executable, '-m', 'tests.benchmark_sqlite_snapshot_check',
                                         '--check', str(path), '--mapped-bytes',
                                         str(MMAP_BYTES if mode == 'mapped' else 0)],
                                        check=True, capture_output=True, text=True)
                measurements.append({'mode': mode, 'trial': trial, 'initial_residency': residency,
                                     **json.loads(result.stdout)})
        if ops.digest(path) != before: raise RuntimeError('check changed fixture bytes')
        report = {'scope': 'fictional local collected-jobs database; not production downtime',
                  'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
                  'schema': 'collection.boards._prepare; grown overflow descriptions and deleted rows',
                  'file_bytes': path.stat().st_size, 'sha256': before,
                  'cache': 'fixture-only advisory eviction' if args.evict else 'not evicted',
                  'measurements': measurements,
                  'median_seconds': {mode: statistics.median(r['seconds'] for r in measurements if r['mode'] == mode)
                                     for mode in ('default', 'mapped')}}
    cgroup = Path('/sys/fs/cgroup')
    report['cgroup'] = {name: (cgroup / name).read_text().strip()
                        for name in ('memory.max', 'memory.peak', 'memory.events')
                        if (cgroup / name).exists()}
    output = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(output)
    else: print(output)


if __name__ == '__main__':
    main()
