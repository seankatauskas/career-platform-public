"""Fictional, fully stopped SQLite capture: backup API versus exact raw copy.

Never use production files. Validation runs after the measured capture in both
cases, matching routine backup pause boundaries. Linux --evict touches fixtures only.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import tempfile
import time
from job_search import aws_ops as ops
from tests.benchmark_host_preparation import evict, io_counts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--megabytes', type=int, default=64)
    p.add_argument('--runs', type=int, default=3)
    p.add_argument('--evict', action='store_true')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    if not 16 <= args.megabytes <= 2048 or not 1 <= args.runs <= 5:
        p.error('use 16–2048 MiB and 1–5 runs')
    measurements = []
    with tempfile.TemporaryDirectory(prefix='backup-raw-capture-') as temporary:
        root = Path(temporary).resolve(); source = root / 'state'; source.mkdir(); path = source / 'jobs.db'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, description TEXT NOT NULL)')
            con.executemany('INSERT INTO jobs VALUES(?,?)',
                            ((i, 'fictional description ' * 720) for i in range(args.megabytes * 64)))
            con.execute('CREATE INDEX job_ids ON jobs(id)')
        ops.sync_tree(root); expected = ops.digest(path)
        for trial in range(args.runs):
            for mode in (('sqlite_backup', 'quiesced_raw') if trial % 2 == 0 else ('quiesced_raw', 'sqlite_backup')):
                residency = evict(root) if args.evict else None
                target = root / 'capture'; before = io_counts(); cpu = time.process_time(); start = time.monotonic()
                ops.copy_snapshot(source, target, validate=False, quiesced_raw_sqlite=mode == 'quiesced_raw')
                ops.sync_tree(target)
                elapsed = time.monotonic() - start; cpu = time.process_time() - cpu; after = io_counts()
                captured = target / 'jobs.db'; ops.sqlite_check(captured)
                copied_hash = ops.digest(captured)
                with sqlite3.connect(captured) as con:
                    assert con.execute('SELECT COUNT(*),SUM(length(description)) FROM jobs').fetchone() == (args.megabytes * 64, args.megabytes * 64 * len('fictional description ' * 720))
                if mode == 'quiesced_raw' and copied_hash != expected: raise RuntimeError('raw copy changed bytes')
                measurements.append({'mode': mode, 'trial': trial, 'capture_seconds': elapsed,
                    'cpu_seconds': cpu, 'initial_residency': residency, 'same_bytes': copied_hash == expected,
                    'full_check': 'ok', 'physical_read_bytes': after.get('read_bytes', 0) - before.get('read_bytes', 0),
                    'physical_write_bytes': after.get('write_bytes', 0) - before.get('write_bytes', 0)})
                shutil.rmtree(target)
        report = {'scope': 'fictional stopped SQLite capture only; no production pause measured',
                  'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
                  'file_bytes': path.stat().st_size, 'source_sha256': expected,
                  'measurements': measurements, 'median_capture_seconds': {
                    mode: statistics.median(r['capture_seconds'] for r in measurements if r['mode'] == mode)
                    for mode in ('sqlite_backup', 'quiesced_raw')}}
    output = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(output)
    else: print(output)


if __name__ == '__main__': main()
