"""Bounded offline preparation/feature benchmark; prediction is a deterministic stub.

Run: uv run python -m tests.benchmark_incremental_ranking --families 3000
No catalog network calls, model downloads, or provider inference are performed.
"""
import argparse
import json
import sqlite3
import time

from job_search.collection.dedupe import prepare_families
from job_search.ranking import refresh
from tests.test_policy_refresh import PolicyRefreshTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--families', type=int, default=3000)
    args = parser.parse_args()
    if not 10 <= args.families <= 10000:
        parser.error('--families must be between 10 and 10000')
    fixture = PolicyRefreshTests()
    fixture.setUp()
    try:
        fixture.configure_sparse()
        text = 'Design reliable distributed systems, support production services, and collaborate with engineers. ' * 40
        with sqlite3.connect(fixture.jobs) as con:
            con.executemany('INSERT INTO jobs(ats,id,company,title,description,last_seen) VALUES (?,?,?,?,?,?)',
                            [('lever', 'bench-' + str(i), 'Benchmark', 'Engineer ' + str(i), text, '2026-10-17')
                             for i in range(args.families - 3)])
        result = {'families': args.families, 'prediction': 'deterministic stub, no model inference', 'phases': {}}
        started = time.perf_counter()
        prepare_families(fixture.jobs)
        result['full_preparation_seconds'] = round(time.perf_counter() - started, 4)
        for phase, statement in (
            ('initial', None),
            ('observation_only', "UPDATE jobs SET last_seen='2026-10-18'"),
            ('one_metadata_edit', "UPDATE jobs SET department='New work' WHERE id='bench-0'"),
            ('one_description_edit', "UPDATE jobs SET description=description || ' Added responsibility.' WHERE id='bench-0'"),
        ):
            if statement:
                with sqlite3.connect(fixture.jobs) as con:
                    con.execute(statement)
            started = time.perf_counter()
            refreshed = refresh.refresh_policies(fixture.jobs, fixture.state, fixture.root / 'proxy.db',
                                                 batch_size=256, active_components_only=True, no_embeddings=True)
            result['phases'][phase] = {
                'seconds': round(time.perf_counter() - started, 4),
                'checked_families': refreshed['checked_families'],
                'recomputed_per_policy': {p: value['result']['recomputed_families'] for p, value in refreshed['policies'].items()},
                'dedupe': refreshed['dedupe'], 'phase_seconds': refreshed['phase_seconds'],
            }
        print(json.dumps(result, sort_keys=True, indent=2))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
