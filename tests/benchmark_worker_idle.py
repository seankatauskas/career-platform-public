"""Measure idle ticks over generated history; no live state or providers are used."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import statistics
import tempfile
import time

from job_search.db import connect, prepare_database
from job_search.scheduler import utc_stamp
from job_search.worker import Worker


def benchmark(rows: int = 11000, repeats: int = 7) -> dict:
    if not 0 <= rows <= 1000000 or not 3 <= repeats <= 100:
        raise ValueError('rows must be 0..1000000 and repeats must be 3..100')
    stamp = utc_stamp(datetime.now(timezone.utc))
    with tempfile.TemporaryDirectory(prefix='career-idle-benchmark-') as directory:
        path = Path(directory) / 'synthetic.db'
        prepare_database(path, stamp)
        with connect(path) as con:
            con.executemany(
                "INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,created_at,lane) "
                "VALUES (?,?,?,'{}','succeeded',?,3,?,'core')",
                ((f'w{i}', f'fixture.task{i % 20}', f'd{i}', stamp, stamp) for i in range(rows)),
            )
            con.executemany(
                "INSERT INTO job_runs(run_id,work_id,scheduled_for,started_at,completed_at,outcome,result_json,error) "
                "VALUES(?,?,?,?,?,'succeeded',?,'')",
                ((f'r{i}', f'w{i}', stamp, stamp, stamp, json.dumps({'fixture': 'x' * 1024})) for i in range(rows)),
            )
        worker = Worker(path, lane='core', partition='interactive', max_outbox_per_tick=0)
        samples = {True: [], False: []}
        # Alternate modes to reduce page-cache / warmup ordering bias.
        for _ in range(repeats):
            for diagnostics in (True, False):
                cpu, wall = time.process_time(), time.perf_counter()
                report = worker.tick(include_diagnostics=diagnostics)
                samples[diagnostics].append((time.process_time() - cpu, time.perf_counter() - wall, len(json.dumps(report))))
                assert report['work'] == {'succeeded': 0, 'retried': 0, 'dead': 0}
                assert not report['more_due']
        results = {}
        for diagnostics, values in samples.items():
            results['detailed' if diagnostics else 'compact'] = {
                'median_cpu_ms': round(statistics.median(row[0] for row in values) * 1000, 3),
                'median_wall_ms': round(statistics.median(row[1] for row in values) * 1000, 3),
                'median_report_bytes': statistics.median(row[2] for row in values),
            }
    return {
        'fixture': {'completed_work_items': rows, 'completed_job_runs': rows, 'task_kinds': min(rows, 20),
                    'result_payload_characters': 1024, 'repetitions': repeats},
        'environment': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version()},
        'results': results,
        'limitations': ['Synthetic local SQLite history, not production timing.',
                        'Excludes dependency checks, Docker probes, providers, and active task execution.'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=11000)
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = benchmark(args.rows, args.repeats)
    encoded = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
