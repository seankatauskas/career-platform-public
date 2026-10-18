"""Replay an existing review's frozen inputs without collection, inference or publication.

Source databases are read-only. Only a private temporary catalog receives an index.
Output contains counts, timing and digests, never job payloads or individual job IDs.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace

from job_search.collection.discovery_index import CANDIDATE_SQL, INDEX_SQL, candidate_bounds
from job_search.contracts import payload_sha256
from job_search.job_reviews.contracts import REVIEW_JOB_FIELDS, fingerprint, timestamp
from job_search.job_reviews.old_method import WORKFLOW
from job_search.job_reviews.old_method.store import Store
from job_search.job_reviews.old_method.window import DISCOVERY_WINDOW, eligible


def readonly(path):
    con = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def select_in_transaction(con, start, end, *, indexed):
    """Compare selectors using the caller's already-open SQLite read transaction."""
    columns = {r[1] for r in con.execute('PRAGMA table_info(jobs)')}
    fields = ','.join(f for f in REVIEW_JOB_FIELDS if f in columns)
    if not {'ats', 'id', 'posted_at', 'first_seen', 'closed_at', 'description'} <= columns:
        raise ValueError('catalog lacks required discovery fields')
    query, params = ((CANDIDATE_SQL, candidate_bounds(start, end)) if indexed else
                     ('SELECT ats,id,posted_at,first_seen,closed_at FROM jobs', ()))
    keys, candidates = [], 0
    for row in con.execute(query, params):
        candidates += 1
        if eligible(dict(row), start, end):
            keys.append((row['ats'], row['id']))
    jobs = [dict(con.execute('SELECT '+fields+' FROM jobs WHERE ats=? AND id=?', key).fetchone())
            for key in keys]
    jobs.sort(key=lambda j: (timestamp(j['first_seen']), j['ats'], str(j['id'])))
    return jobs, candidates


def saved_inputs(app_db, review_id):
    with closing(readonly(app_db)) as con:
        con.execute('BEGIN')
        run = con.execute('SELECT window_start,window_end,metadata_json FROM job_reviews WHERE review_id=?',
                          (review_id,)).fetchone()
        if run is None:
            raise ValueError('review does not exist')
        metadata = json.loads(run['metadata_json'])
        if metadata.get('workflow') != WORKFLOW or metadata.get('window_policy') != DISCOVERY_WINDOW:
            raise ValueError('replay requires an OLD METHOD discovery-window review')
        rows = con.execute('SELECT ordinal,ats,job_id,snapshot_json,snapshot_sha256 FROM job_review_items '
                           'WHERE review_id=? ORDER BY ordinal', (review_id,)).fetchall()
    jobs = []
    for expected, row in enumerate(rows, 1):
        job = json.loads(row['snapshot_json'])
        if row['ordinal'] != expected or fingerprint(job) != row['snapshot_sha256']:
            raise ValueError('saved snapshot ordinal or fingerprint verification failed')
        if (job['ats'], str(job['id'])) != (row['ats'], row['job_id']):
            raise ValueError('saved snapshot identity verification failed')
        jobs.append(job)
    return run['window_start'], run['window_end'], jobs


def drift_counts(current, frozen):
    current_by_key = {(j['ats'], str(j['id'])): j for j in current}
    frozen_by_key = {(j['ats'], str(j['id'])): j for j in frozen}
    common = current_by_key.keys() & frozen_by_key.keys()
    return {'added': len(current_by_key.keys()-frozen_by_key.keys()),
            'removed': len(frozen_by_key.keys()-current_by_key.keys()),
            'changed': sum(current_by_key[k] != frozen_by_key[k] for k in common),
            'unchanged': sum(current_by_key[k] == frozen_by_key[k] for k in common)}


def probe(app_db, jobs_db, review_id, *, compare_current=False):
    raw_start, raw_end, frozen = saved_inputs(app_db, review_id)
    start, end = timestamp(raw_start), timestamp(raw_end)
    if not start < end:
        raise ValueError('saved review window is invalid')
    report = {'review_id': review_id, 'window_start': raw_start, 'window_end': raw_end,
              'saved_count': len(frozen), 'saved_payload_sha256': payload_sha256(frozen),
              'saved_fingerprints_verified': True}
    # Snapshots contain the original catalog columns, including null values. Use
    # untyped SQLite columns to retain their exact stored value types in replay.
    required = {'ats', 'id', 'posted_at', 'first_seen', 'closed_at', 'description'}
    columns = set(frozen[0]) if frozen else required
    if not required <= columns or columns-set(REVIEW_JOB_FIELDS) or any(set(j) != columns for j in frozen):
        raise ValueError('saved snapshot fields are inconsistent')
    ordered_columns = [f for f in REVIEW_JOB_FIELDS if f in columns]
    with tempfile.TemporaryDirectory(prefix='review-input-replay-') as directory:
        path = Path(directory)/'jobs.db'
        with closing(sqlite3.connect(path)) as con, con:
            con.execute('CREATE TABLE jobs('+','.join(ordered_columns)+',PRIMARY KEY(ats,id))')
            con.executemany('INSERT INTO jobs VALUES('+','.join('?' for _ in ordered_columns)+')',
                            [tuple(j[f] for f in ordered_columns) for j in frozen])
        started = time.perf_counter()
        with closing(readonly(path)) as con:
            con.execute('BEGIN')
            original, candidates = select_in_transaction(con, start, end, indexed=False)
        original_seconds = time.perf_counter()-started
        with closing(sqlite3.connect(path)) as con, con:
            con.execute(INDEX_SQL)
        started = time.perf_counter()
        # Exercise the actual application implementation, not only probe SQL.
        store = Store(SimpleNamespace(db_path=None, catalog=SimpleNamespace(jobs_db=path)))
        actual, counts = store._snapshot(start, end)
        indexed_seconds = time.perf_counter()-started
        if (original != frozen or actual != frozen or
                payload_sha256(original) != payload_sha256(frozen) or
                payload_sha256(actual) != payload_sha256(frozen)):
            raise AssertionError('frozen replay differs from saved payloads or ordering')
        report['frozen_replay'] = {'original_equals_saved': True, 'indexed_equals_saved': True,
                                   'actual_store_exercised': True, 'count': counts['total_eligible'],
                                   'original_candidates': candidates,
                                   'original_seconds': original_seconds, 'indexed_seconds': indexed_seconds,
                                   'payload_sha256': payload_sha256(actual)}
    if compare_current:
        with closing(readonly(jobs_db)) as con:
            con.execute('BEGIN')
            # Both algorithms and description reads observe this one transaction.
            started = time.perf_counter()
            original, original_candidates = select_in_transaction(con, start, end, indexed=False)
            original_seconds = time.perf_counter()-started
            started = time.perf_counter()
            indexed, indexed_candidates = select_in_transaction(con, start, end, indexed=True)
            indexed_seconds = time.perf_counter()-started
            plan = [r[3] for r in con.execute('EXPLAIN QUERY PLAN '+CANDIDATE_SQL, candidate_bounds(start, end))]
        if original != indexed or payload_sha256(original) != payload_sha256(indexed):
            raise AssertionError('live selectors differ within the same read transaction')
        report['current_catalog'] = {'algorithms_equal': True, 'same_read_transaction': True,
                                     'eligible_count': len(indexed), 'original_candidates': original_candidates,
                                     'indexed_candidates': indexed_candidates,
                                     'original_seconds': original_seconds, 'indexed_seconds': indexed_seconds,
                                     'payload_sha256': payload_sha256(indexed), 'query_plan': plan,
                                     'changes_since_frozen_snapshot': drift_counts(indexed, frozen)}
    report['limitations'] = ('Frozen replay contains only the original saved inputs; it verifies exact historical '
                             'payload/order parity, not historical catalog scan time. Current-catalog drift is '
                             'reported separately from same-transaction algorithm parity. No model or publication ran.')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-db', type=Path, required=True)
    parser.add_argument('--jobs-db', type=Path)
    parser.add_argument('--review-id', required=True)
    parser.add_argument('--compare-current', action='store_true', help='also scan the current catalog read-only')
    args = parser.parse_args()
    if args.compare_current and args.jobs_db is None:
        parser.error('--compare-current requires --jobs-db')
    print(json.dumps(probe(args.app_db, args.jobs_db, args.review_id, compare_current=args.compare_current), indent=2))


if __name__ == '__main__':
    main()
