"""Measure detailed/routine readiness against an isolated fictional catalog."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time
from unittest.mock import patch

from job_search.dependency_health import dependency_health
from job_search.runtime import RuntimeConfigV1
from job_search.runtime_readiness import runtime_readiness
from job_search.service import JobSearchLedger


def benchmark(rows: int, description_bytes: int) -> dict:
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        root = Path(directory)
        config = replace(RuntimeConfigV1.defaults(root), shortlist_policy='compare')
        JobSearchLedger(config.application_db)
        with closing(sqlite3.connect(config.application_db)) as con, con:
            con.execute("INSERT INTO automation_controls VALUES ('ranking',0,1,'2026-01-01T00:00:00Z')")
        with closing(sqlite3.connect(config.jobs_db)) as con, con:
            con.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,last_seen TEXT,description TEXT)')
            con.execute('CREATE TABLE job_families(family_id INTEGER PRIMARY KEY)')
            con.executemany('INSERT INTO jobs VALUES (?,?,?)', (
                (index, '2026-01-01T00:00:00Z', 'x' * description_bytes) for index in range(rows)))
            con.executemany('INSERT INTO job_families VALUES (?)', ((index,) for index in range(rows)))
        with closing(sqlite3.connect(config.preference_db)) as con, con:
            con.execute('CREATE TABLE preference_model_runs(run_id TEXT,artifact_path TEXT,manifest_json TEXT)')
            con.execute('CREATE TABLE preference_scores(run_id TEXT,scored_at TEXT)')
            con.execute('CREATE TABLE preference_state(key TEXT,value TEXT)')
        with closing(sqlite3.connect(config.proxy_db)) as con, con:
            con.execute('CREATE TABLE proxy_students(policy_id TEXT,model_run_id TEXT,run_id TEXT,trained_at TEXT)')
            con.execute('CREATE TABLE proxy_runs(run_id TEXT,created_at TEXT)')
        samples = {name: [] for name in ('detailed', 'routine', 'missing_local_bridge')}
        bridge = replace(config, hermes_telegram_target='fixture-target', hermes_notification_socket=root/'absent.sock')
        for iteration in range(8):
            for detailed in (True, False) if iteration % 2 == 0 else (False, True):
                wall, cpu = time.perf_counter(), time.process_time()
                report = runtime_readiness(config, use_snapshot=True, include_ranking_details=detailed)
                elapsed = {'wall_ms': (time.perf_counter()-wall)*1000, 'cpu_ms': (time.process_time()-cpu)*1000}
                assert ('ranking_policies' in report) is detailed
                if iteration:
                    samples['detailed' if detailed else 'routine'].append(elapsed)
            wall, cpu = time.perf_counter(), time.process_time()
            dependencies = dependency_health(bridge)
            elapsed = {'wall_ms': (time.perf_counter()-wall)*1000, 'cpu_ms': (time.process_time()-cpu)*1000}
            assert dependencies['notifications']['status'] == 'blocked_setup'
            if iteration:
                samples['missing_local_bridge'].append(elapsed)
        return {
            'scope': 'fictional local SQLite fixture; warm cache; no provider calls; not production latency',
            'rows': rows, 'description_bytes_per_row': description_bytes,
            'catalog_bytes': config.jobs_db.stat().st_size,
            'iterations': 7,
            'median': {name: {key: round(statistics.median(row[key] for row in values), 3)
                              for key in ('wall_ms', 'cpu_ms')} for name, values in samples.items()},
            'probe_limit': 'At most three early recovery observations before returning to 300-second cadence. '
                           'Missing local socket only; this does not measure timeout or configured resume/inference cost.',
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=100000)
    parser.add_argument('--description-bytes', type=int, default=256)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.rows <= 1000000 or not 0 <= args.description_bytes <= 8192:
        parser.error('fixture bounds exceeded')
    result = benchmark(args.rows, args.description_bytes)
    encoded = json.dumps(result, indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
