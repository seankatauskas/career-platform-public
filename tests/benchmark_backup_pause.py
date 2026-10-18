"""Bounded synthetic comparison of capture and validation pause boundaries.

Run python3 -m tests.benchmark_backup_pause --megabytes 64 --runs 3.
No services, Docker, AWS, production files, or intentional wait delays are used.
The measured paused interval covers capture only, excluding service stop/start.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import statistics
import tempfile
import time

from job_search import aws_ops as ops


def benchmark(megabytes=64, runs=3):
    if not 1 <= megabytes <= 256 or not 1 <= runs <= 5:
        raise ValueError('use 1–256 MiB and 1–5 runs')
    with tempfile.TemporaryDirectory(prefix='backup-pause-benchmark-') as temporary:
        root = Path(temporary).resolve(); source = root / 'state'; source.mkdir()
        db = sqlite3.connect(source / 'indexed.db')
        try:
            db.execute('CREATE TABLE rows(id INTEGER PRIMARY KEY, text TEXT, payload BLOB)')
            db.executemany('INSERT INTO rows(text,payload) VALUES(?,?)',
                           ((f'fictional job {i}', b'x' * 2048) for i in range(megabytes * 256)))
            db.execute('CREATE INDEX by_text ON rows(text)'); db.commit()
        finally: db.close()
        rows = []
        for trial in range(runs):
            order = ('paused_validation', 'postresume_validation') if trial % 2 == 0 else ('postresume_validation', 'paused_validation')
            for implementation in order:
                target = root / f'{trial}-{implementation}'
                started = time.monotonic()
                ops.copy_snapshot(source, target, validate=implementation == 'paused_validation')
                paused = time.monotonic() - started
                if implementation == 'postresume_validation':
                    for path in target.iterdir(): ops.sqlite_check(path)
                finished = time.monotonic()
                # Independent source-equivalent bytes are checked for both cases.
                original = sqlite3.connect(source / 'indexed.db')
                copied = sqlite3.connect(target / 'indexed.db')
                try:
                    assert original.execute('SELECT COUNT(*), SUM(id), SUM(length(payload)) FROM rows').fetchone() == copied.execute('SELECT COUNT(*), SUM(id), SUM(length(payload)) FROM rows').fetchone()
                finally: original.close(); copied.close()
                rows.append({'implementation': implementation, 'paused_capture_seconds': round(paused, 6),
                             'capture_and_validation_seconds': round(finished - started, 6)})
                shutil.rmtree(target)
        # Model one last worker write during draining. Preparing after that write
        # should reuse the checked bytes; preparing before it must recapture.
        for name in ('hermes', 'toolchain'): (root / name).mkdir()
        ops.write_json(root / 'materialized-secrets.json', {})
        c = {'data_root': str(root), 'secret_arns': {}}
        deployments = []
        for trial in range(runs):
            for implementation in ('prepare_before_drain', 'prepare_after_drain'):
                def final_worker_write():
                    live = sqlite3.connect(source / 'indexed.db')
                    try:
                        live.execute('UPDATE rows SET text=? WHERE id=1', (f'last write {trial} {implementation}',))
                        live.commit()
                    finally: live.close()
                if implementation == 'prepare_after_drain': final_worker_write()
                with ops.prepare_snapshot(c) as prepared:
                    if implementation == 'prepare_before_drain': final_worker_write()
                    started = time.monotonic()
                    receipt = ops.local_snapshot_unlocked(c, release_id='benchmark', prepared=prepared)
                    elapsed = time.monotonic() - started
                snapshot = ops.local_backup_path(c, receipt)
                ops.verify_snapshot(snapshot)
                deployments.append({'implementation': implementation, 'paused_capture_seconds': round(elapsed, 6),
                                    'reuse': receipt['reuse']})
                shutil.rmtree(snapshot)
        return {'scope': 'synthetic local SQLite capture; no service or AWS downtime measured',
                'sqlite_bytes': (source / 'indexed.db').stat().st_size, 'runs': runs, 'measurements': rows,
                'deployment_measurements': deployments,
                'median_deployment_paused_capture_seconds': {
                    name: round(statistics.median(row['paused_capture_seconds'] for row in deployments if row['implementation'] == name), 6)
                    for name in ('prepare_before_drain', 'prepare_after_drain')},
                'median_paused_capture_seconds': {
                    name: round(statistics.median(row['paused_capture_seconds'] for row in rows if row['implementation'] == name), 6)
                    for name in ('paused_validation', 'postresume_validation')}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--megabytes', type=int, default=64)
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.megabytes, args.runs), indent=2))
