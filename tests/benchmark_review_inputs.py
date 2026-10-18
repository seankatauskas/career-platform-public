"""Bounded fictional catalog benchmark; no network or production DB access."""
import argparse
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time

from job_search.collection.discovery_index import CANDIDATE_SQL, candidate_bounds, prepare_existing
from job_search.job_reviews.contracts import timestamp
from tests.test_review_input_index import create_catalog, old_snapshot, store_for


def unchanged_date_updates(path, rows):
    """Bounded proxy for repeated collector writes of indexed provenance fields."""
    started = time.perf_counter()
    with closing(sqlite3.connect(path)) as con, con:
        con.executemany('UPDATE jobs SET posted_at=posted_at,closed_at=closed_at WHERE ats=? AND id=?',
                        (('ashby', str(n)) for n in range(rows)))
    return time.perf_counter()-started


def run(rows, description_bytes, repeats):
    with tempfile.TemporaryDirectory(prefix='review-input-benchmark-') as directory:
        path = Path(directory)/'jobs.db'
        create_catalog(path)
        start, end = map(timestamp, ('2026-10-17T00:00:00Z', '2026-10-17T12:00:00Z'))
        with closing(sqlite3.connect(path)) as con, con:
            con.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (
                ('ashby', str(n), '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', None, 'x'*description_bytes)
                for n in range(rows)))
            con.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (
                ('lever', str(n), start.isoformat() if n < 122 else '2020-01-01T00:00:00Z',
                 (start+timedelta(seconds=n+1)).isoformat(), None, 'Recent '+('x'*description_bytes))
                for n in range(1000)))
        before = path.stat().st_size
        expected = old_snapshot(path, start, end)
        original_times = []
        for _ in range(repeats):
            started = time.perf_counter()
            assert old_snapshot(path, start, end) == expected
            original_times.append(time.perf_counter()-started)
        update_rows = min(rows, 10000)
        before_updates = unchanged_date_updates(path, update_rows)
        started = time.perf_counter()
        prepare_existing(path)
        build = time.perf_counter()-started
        index_bytes = path.stat().st_size-before
        after_updates = unchanged_date_updates(path, update_rows)
        timings = {'original_without_index_seconds': original_times,
                   'legacy_scan_with_index_seconds': [], 'indexed_seconds': []}
        for _ in range(repeats):
            for label, read in (
                ('legacy_scan_with_index_seconds', lambda: old_snapshot(path, start, end)),
                ('indexed_seconds', lambda: store_for(path)._snapshot(start, end)[0]),
            ):
                started = time.perf_counter()
                actual = read()
                timings[label].append(time.perf_counter()-started)
                assert actual == expected
        with closing(sqlite3.connect(path)) as con:
            plan = [r[3] for r in con.execute('EXPLAIN QUERY PLAN '+CANDIDATE_SQL, candidate_bounds(start, end))]
            candidates = len(con.execute(CANDIDATE_SQL, candidate_bounds(start, end)).fetchall())
        return {'fixture': {'historical_rows': rows, 'recent_rows': 1000, 'description_bytes': description_bytes,
                            'database_bytes_before_index': before}, 'eligible': len(expected),
                'candidates': candidates, 'index_build_seconds': build, 'index_bytes': index_bytes,
                'unchanged_date_update_proxy': {'rows': update_rows, 'before_index_seconds': before_updates,
                                              'after_index_seconds': after_updates},
                'query_plan': plan, **timings,
                'median_seconds': {key: statistics.median(values) for key, values in timings.items()},
                'limitations': 'Warm local repeated reads; no OS cache eviction, production I/O or model calls. Original measured before index installation; legacy scan and indexed candidates measured afterward.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=100000)
    parser.add_argument('--description-bytes', type=int, default=1024)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.rows < 1 or args.description_bytes < 0 or args.repeats < 1:
        parser.error('rows/repeats must be positive; description bytes must be nonnegative')
    print(json.dumps(run(args.rows, args.description_bytes, args.repeats), indent=2))


if __name__ == '__main__':
    main()
