"""Profile real rollback capture on fictional SQLite and file state (no services).

python -m tests.benchmark_deployment_phases --megabytes 11264 --runs 3
No AWS, credentials, containers or real application state are used. Filesystem
cache is not evicted. This measures the snapshot part of downtime, not startup.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack, contextmanager
import json
import platform
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import time
from unittest.mock import patch
from job_search import aws_ops as ops


@contextmanager
def profile():
    seconds = {}
    def timed(name, fn):
        def call(*args, **kwargs):
            start = time.monotonic()
            try: return fn(*args, **kwargs)
            finally: seconds[name] = seconds.get(name, 0) + time.monotonic() - start
        return call
    class Connection(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            return timed('sqlite_backup', super().backup)(*args, **kwargs)
    connect = sqlite3.connect
    with ExitStack() as stack:
        stack.enter_context(patch.object(sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, factory=Connection, **kw)))
        for owner, attr, name in ((ops.shutil, 'copy2', 'file_copy'), (ops, 'digest', 'hash'),
                                   (ops, 'sqlite_check', 'sqlite_check'), (ops, 'sync_tree', 'sync_tree')):
            stack.enter_context(patch.object(owner, attr, timed(name, getattr(owner, attr))))
        yield seconds


def seed(root, megabytes):
    for name in ops.BACKUP_DIRS: (root / name).mkdir()
    ops.write_json(root / 'materialized-secrets.json', {})
    block = random.Random(17).randbytes(15000)
    # Indexed fictional job text spread over three DBs; a separate immutable
    # predecessor is a material part of the payload, as in the production layout.
    for name, share in (('jobs.db', .45), ('owners.db', .10), ('history.db', .05),
                         ('archive/predecessor.sqlite', .15)):
        path = root / 'state' / name; path.parent.mkdir(exist_ok=True)
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, company TEXT, body BLOB)')
        rows = max(1, int(megabytes * share * 1024**2 / 16384))
        for start in range(0, rows, 512):
            db.executemany('INSERT INTO records VALUES(?,?,?)',
                           ((i, 'Fictional Company ' + str(i % 101), block) for i in range(start, min(start+512, rows))))
            db.commit()
        db.execute('CREATE INDEX company_idx ON records(company)'); db.commit(); db.close()
        if name.endswith('.sqlite'):
            ops.write_json(path.parent / 'conversion-report.json', {'archive_sha256': ops.digest(path)})
            path.chmod(0o400)
    block = random.Random(23).randbytes(1024**2)
    for name, share in (('state/models.bin', .10), ('hermes/history.bin', .05), ('toolchain/bundle.bin', .10)):
        with (root / name).open('wb') as stream:
            for _ in range(max(1, int(megabytes * share))): stream.write(block)
    for i in range(100): (root / 'state' / ('resume-' + str(i) + '.txt')).write_text('Fictional resume\n' * 64)
    ops.sync_tree(root)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--megabytes', type=int, default=128)
    p.add_argument('--runs', type=int, default=1)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    if not 16 <= args.megabytes <= 16384 or not 1 <= args.runs <= 5: p.error('use 16–16384 MiB and 1–5 runs')
    report = {'scope': 'fictional local snapshot; no measured service downtime', 'platform': platform.platform(),
              'sqlite': sqlite3.sqlite_version, 'cache': 'not evicted; same seeded source', 'measurements': []}
    with tempfile.TemporaryDirectory(prefix='career-phase-benchmark-') as tmp:
        root = Path(tmp).resolve(); seed(root, args.megabytes)
        report['payload_bytes'] = sum(x.stat().st_size for x in root.rglob('*') if x.is_file())
        config = {'data_root': str(root), 'secret_arns': {}}
        modes = ['paused'] + (['prepared', 'changed'] if hasattr(ops, 'prepare_snapshot') else [])
        for trial in range(args.runs):
            for mode in modes if trial % 2 == 0 else reversed(modes):
                start = time.monotonic()
                with ExitStack() as stack, profile() as phases:
                    prepared = stack.enter_context(ops.prepare_snapshot(config)) if mode != 'paused' else None
                    preparation = time.monotonic() - start
                    if mode == 'changed':
                        with sqlite3.connect(root / 'state/owners.db') as db:
                            db.execute("UPDATE records SET company='Fictional Changed Company' WHERE id=0")
                    paused = time.monotonic()
                    receipt = ops.local_snapshot_unlocked(config, release_id='benchmark', **({'prepared': prepared} if prepared else {}))
                    stopped = time.monotonic() - paused
                verification_start = time.monotonic()
                snapshot = ops.local_backup_path(config, receipt)
                ops.verify_snapshot(snapshot)
                verification = time.monotonic() - verification_start
                report['measurements'].append({'mode': mode, 'trial': trial, 'preparation_seconds': preparation,
                    'paused_snapshot_seconds': stopped, 'total_snapshot_seconds': preparation + stopped,
                    'restore_verification_seconds': verification, 'profile_seconds': phases, 'receipt': receipt})
                shutil.rmtree(snapshot)
    result = json.dumps(report, indent=2) + '\n'
    if args.output: args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(result)
    print(result)


if __name__ == '__main__': main()
