"""Offline mixed-work resource fixture; synthetic scores are not production inference.

Run python -m tests.benchmark_medium_host --families 3000. Every database is
generated in a temporary directory. There is no user-database or provider option.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import platform
import resource
import queue
import socket
import sqlite3
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

from job_search import aws_ops
from job_search.collection import boards
from job_search.collection.dedupe import prepare_families
from job_search.ranking import model, refresh
from job_search.resource_usage import MemorySampler

PHASES = ('setup', 'interactive_idle_baseline', 'streaming_collection_initial', 'ranking_full',
          'ranking_unchanged', 'snapshot_fixture_setup', 'mixed_collection_ranking_snapshot',
          'full_rebuild_parity_oracle')


@contextmanager
def offline():
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('network forbidden in the offline benchmark')
        return original(sock, address)
    with patch.object(socket.socket, 'connect', connect):
        yield


def usage():
    value = resource.getrusage(resource.RUSAGE_SELF)
    return value.ru_utime, value.ru_stime, int(value.ru_maxrss * (1 if sys.platform == 'darwin' else 1024))


@contextmanager
def measured(results, name):
    before = usage()
    started = time.monotonic()
    sampler = MemorySampler(interval_seconds=1).start()
    entry = results[name] = {'started_monotonic_seconds': started}
    try:
        yield entry
    finally:
        after = usage()
        finished = time.monotonic()
        entry.update(wall_seconds=finished - started, finished_monotonic_seconds=finished,
                     user_cpu_seconds=after[0] - before[0], system_cpu_seconds=after[1] - before[1],
                     process_lifetime_peak_rss_bytes=after[2], memory=sampler.finish())
        for key in ('cgroup_event_oom', 'cgroup_event_oom_kill', 'cgroup_event_oom_group_kill'):
            counter = entry['memory']['counters'].get(key)
            if counter and counter['last'] > counter['first']:
                raise RuntimeError(f'resource failure: {key} increased')


def digest_rows(path, query):
    digest = hashlib.sha256()
    with sqlite3.connect(path) as con:
        for row in con.execute(query):
            digest.update(json.dumps(row, separators=(',', ':'), ensure_ascii=False).encode())
            digest.update(b'\n')
    return digest.hexdigest()


def score_digest(path):
    return digest_rows(path, 'SELECT run_id,family_id,feature_fingerprint,dense_linear_score,'
                       'dense_neighbor_score,sparse_score,final_score,explanation_json '
                       'FROM preference_scores ORDER BY run_id,family_id')


def family_digest(path):
    return {table: digest_rows(path, f'SELECT * FROM {table} ORDER BY 1,2') for table in
            ('job_families', 'job_family_members', 'job_template_clusters')}


def seed_state(path):
    model.prepare_state(path)
    with sqlite3.connect(path) as con:
        con.executemany('INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)',
                        ((run, '2026-10-17', 'synthetic-sparse', model.TEXT_VERSION, 1, '{}', '')
                         for run in ('run-broad', 'run-selective')))


@contextmanager
def synthetic_models():
    # Same sparse fixture contract as test_policy_refresh, without Mock call
    # histories retaining every document in a hundred-thousand-family run.
    def artifact(db, run, **kwargs):
        return ({'weights': {'sparse': 1.0}, 'model_revision': 'synthetic-sparse'},
                {'run_id': run, 'model_revision': 'synthetic-sparse',
                 'manifest_json': json.dumps({'artifacts': {'model.pkl': 'a' * 64}})})
    def predict(state, documents, artifact, modules, *, active_components_only=False):
        if not active_components_only:
            raise AssertionError('only synthetic sparse predictions are supported')
        # Input-dependent deterministic values make parity checks meaningful.
        return [dict(dense_linear=None, dense_neighbor=None,
                     sparse=int(d.fingerprint[:8], 16) / 2**32,
                     final=int(d.fingerprint[:8], 16) / 2**32,
                     similar_liked_family_ids=[], positive_sparse_phrases=[],
                     computed_components=['sparse']) for d in documents]
    def forbidden(*args, **kwargs):
        raise AssertionError('training/embedding inference is forbidden')
    with patch.object(refresh, 'policy_runs', new=lambda _: {'broad': 'run-broad', 'selective': 'run-selective'}), \
         patch.object(model, '_load_artifact', new=artifact), \
         patch.object(model, '_optional_ml_modules', new=lambda: {}), \
         patch.object(model, '_score_document_batch', new=predict), \
         patch.object(model, 'encoder_for_recorded_revision', new=forbidden), \
         patch.object(model, 'train_model', new=forbidden):
        yield


def rank(jobs, state, root):
    return refresh.refresh_policies(jobs, state, root / 'unused-proxy.db', batch_size=256,
                                    active_components_only=True, no_embeddings=True)


def ranking_counts(result):
    return {'checked_families': result['checked_families'], 'dedupe': result['dedupe'],
            'phase_seconds': result['phase_seconds'],
            'recomputed_per_policy': {p: v['result']['recomputed_families'] for p, v in result['policies'].items()}}


def collect(jobs, count, description_bytes, *, stamp='2026-10-17', changed=False):
    text = ('Design reliable distributed services and support engineers. ' * (description_bytes // 60 + 2))[:description_bytes]
    writes = 0
    for start in range(0, count, boards.COLLECTION_BATCH_ROWS):
        batch = [dict(ats='ashby', id=str(i), company=f'Fictional company {i % 100}',
                      title=f'Engineer {i}', description=f'{text} Role identifier {i}.',
                      location='Chicago, IL', department='Changed' if changed and i == 0 else 'Engineering')
                 for i in range(start, min(start + boards.COLLECTION_BATCH_ROWS, count))]
        boards.save(batch, jobs, stamp)
        writes += 1
    with sqlite3.connect(jobs) as con:
        if con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] != count:
            raise AssertionError('collector lost or duplicated postings')
    return {'batches': writes, 'batch_limit': boards.COLLECTION_BATCH_ROWS, 'postings': count}


def latency_summary(values):
    ordered = sorted(values)
    return {'requests': len(ordered), 'p50_seconds': ordered[len(ordered)//2],
            'p95_seconds': ordered[min(len(ordered)-1, int(len(ordered)*.95))],
            'max_seconds': ordered[-1]}


def overlap_seconds(left, right):
    return max(0, min(left[1], right[1]) - max(left[0], right[0]))


def probe_worker(path, stop, ready, output, activity):
    results = {}
    try:
        with offline(), measured(results, 'interactive'):
            with sqlite3.connect(path, timeout=1) as con:
                con.execute('PRAGMA journal_mode=WAL')
                con.execute('CREATE TABLE requests(id INTEGER PRIMARY KEY, state TEXT NOT NULL)')
                con.executemany("INSERT INTO requests VALUES (?,'queued')", ((i,) for i in range(1000)))
                con.commit()
                latencies = []
                by_phase = {}
                ready.set()
                due = time.monotonic()
                while not stop.is_set():
                    stop.wait(max(0, due - time.monotonic()))
                    if stop.is_set():
                        break
                    phase = PHASES[activity.value]
                    state = f'done:{len(latencies)}'
                    con.execute('UPDATE requests SET state=? WHERE id=?', (state, len(latencies) % 1000))
                    if con.execute('SELECT state FROM requests WHERE id=?', (len(latencies) % 1000,)).fetchone() != (state,):
                        raise AssertionError('interactive read/write mismatch')
                    con.commit()
                    latencies.append(time.monotonic() - due)
                    by_phase.setdefault(phase, []).append(latencies[-1])
                    # No catch-up burst after contention; include wake-up delay.
                    due = time.monotonic() + .1
                if not latencies:
                    raise AssertionError('interactive fixture produced no observations')
                results['latency'] = {**latency_summary(latencies), 'cadence_seconds': .1,
                                      'includes_scheduling_delay': True,
                                      'by_phase': {name: latency_summary(values) for name, values in by_phase.items()}}
        output.put({'ok': True, **results})
    except BaseException as exc:
        ready.set()
        output.put({'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc(), **results})


def snapshot_worker(source, target, output):
    results = {}
    try:
        with offline(), measured(results, 'snapshot'):
            aws_ops.copy_snapshot(Path(source), Path(target), validate=False)
            aws_ops.sqlite_check(Path(target) / 'jobs.db')
            query = 'SELECT ats,id,title,description FROM jobs ORDER BY ats,id'
            if digest_rows(Path(source)/'jobs.db', query) != digest_rows(Path(target)/'jobs.db', query):
                raise AssertionError('snapshot logical contents changed')
            if aws_ops.digest(Path(source)/'payload.bin') != aws_ops.digest(Path(target)/'payload.bin'):
                raise AssertionError('snapshot payload changed')
        output.put({'ok': True, **results})
    except BaseException as exc:
        output.put({'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc(), **results})


def child_result(process, output):
    # Read before join so Queue feeder threads cannot block shutdown on a full pipe.
    deadline = time.monotonic() + 3600
    while process.is_alive():
        if time.monotonic() >= deadline:
            raise RuntimeError('child exceeded one-hour completion deadline')
        try:
            result = output.get(timeout=.2)
            break
        except queue.Empty:
            pass
    else:
        try:
            result = output.get(timeout=1)
        except queue.Empty as exc:
            raise RuntimeError(f'child exited without result (exit={process.exitcode}; possible resource kill)') from exc
    process.join(timeout=10)
    if process.is_alive() or process.exitcode != 0 or not result['ok']:
        raise RuntimeError(f'child failure: exit={process.exitcode}, result={result}')
    return result


def benchmark(families=3000, description_bytes=4096, snapshot_mib=16):
    if not 10 <= families <= 400000 or not 128 <= description_bytes <= 65536 or not 1 <= snapshot_mib <= 1024:
        raise ValueError('families 10..400000; description-bytes 128..65536; snapshot-mib 1..1024')
    phases = {}
    result = {'version': 1, 'fixture': {'families': families, 'description_bytes': description_bytes,
              'snapshot_payload_mib': snapshot_mib, 'prediction': 'input-dependent deterministic stub; no ML inference'},
              'environment': {'os': platform.system(), 'machine': platform.machine(),
                              'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version},
              'phases': phases, 'scope': 'synthetic shared-host contention; not an EC2 capacity certification'}
    context = mp.get_context('spawn')
    activity = context.Value('i', 0)
    @contextmanager
    def phase(name):
        activity.value = PHASES.index(name)
        try:
            with measured(phases, name) as entry:
                yield entry
        finally:
            activity.value = 0
    children = []
    with tempfile.TemporaryDirectory(prefix='career-medium-benchmark-') as directory, offline(), synthetic_models():
        root = Path(directory).resolve()
        jobs, state = root/'jobs.db', root/'scores.db'
        stop, ready, output = context.Event(), context.Event(), context.Queue()
        probe = context.Process(target=probe_worker, args=(root/'interactive.db', stop, ready, output, activity))
        try:
            with measured(phases, 'whole_run'):
                probe.start(); children.append(probe)
                if not ready.wait(10):
                    raise RuntimeError('interactive fixture failed to start')
                with phase('interactive_idle_baseline'):
                    stop.wait(1)
                with phase('streaming_collection_initial') as entry:
                    entry['collection'] = collect(jobs, families, description_bytes)
                seed_state(state)
                with phase('ranking_full') as entry:
                    entry['ranking'] = ranking_counts(rank(jobs, state, root))
                if entry['ranking']['checked_families'] != families:
                    raise AssertionError('full fixture did not cover every family')
                original = score_digest(state)
                with phase('ranking_unchanged') as entry:
                    entry['ranking'] = ranking_counts(rank(jobs, state, root))
                if entry['ranking']['checked_families'] != 0 or score_digest(state) != original:
                    raise AssertionError('unchanged refresh altered scores or checked families')
                source, target = root/'snapshot-source', root/'snapshot-target'
                with phase('snapshot_fixture_setup'):
                    source.mkdir()
                    with closing(sqlite3.connect(jobs)) as src, closing(sqlite3.connect(source/'jobs.db')) as dst:
                        src.backup(dst)
                        dst.execute('PRAGMA journal_mode=DELETE')
                    with (source/'payload.bin').open('wb') as stream:
                        block = bytes(range(256)) * 4096
                        for _ in range(snapshot_mib):
                            stream.write(block)
                snap_output = context.Queue()
                snapshot = context.Process(target=snapshot_worker, args=(source, target, snap_output))
                with phase('mixed_collection_ranking_snapshot') as entry:
                    snapshot.start(); children.append(snapshot)
                    collection_started = time.monotonic()
                    entry['collection'] = collect(jobs, families, description_bytes, stamp='2026-10-18', changed=True)
                    with sqlite3.connect(jobs) as con:
                        con.execute("UPDATE jobs SET description=description || ' New responsibility.' WHERE id='1'")
                    collection_finished = time.monotonic()
                    ranking_started = time.monotonic()
                    entry['ranking'] = ranking_counts(rank(jobs, state, root))
                    ranking_finished = time.monotonic()
                    entry['snapshot_child'] = child_result(snapshot, snap_output)
                    snapshot_profile = entry['snapshot_child']['snapshot']
                    snapshot_interval = (snapshot_profile['started_monotonic_seconds'], snapshot_profile['finished_monotonic_seconds'])
                    entry['stage_intervals_monotonic_seconds'] = {
                        'collection': [collection_started, collection_finished],
                        'ranking': [ranking_started, ranking_finished], 'snapshot': list(snapshot_interval)}
                    entry['snapshot_overlap_seconds'] = {
                        name: overlap_seconds(interval, snapshot_interval) for name, interval in
                        entry['stage_intervals_monotonic_seconds'].items() if name != 'snapshot'}
                    if entry['ranking']['checked_families'] != 2:
                        raise AssertionError('two sparse edits did not check exactly two families')
                incremental_scores, incremental_families = score_digest(state), family_digest(jobs)
                if incremental_scores == original:
                    raise AssertionError('changed inputs did not change deterministic predictions')
                # Independent full preparation and fresh score state is the oracle.
                oracle = root/'full-oracle.db'
                with phase('full_rebuild_parity_oracle') as entry:
                    prepare_families(jobs)
                    seed_state(oracle)
                    entry['ranking'] = ranking_counts(rank(jobs, oracle, root))
                    if family_digest(jobs) != incremental_families or score_digest(oracle) != incremental_scores:
                        raise AssertionError('incremental result differs from full rebuild')
                stop.set()
                result['interactive_child'] = child_result(probe, output)
                result['parity'] = {'incremental_vs_full': True, 'snapshot': True, 'unchanged': True}
                result['fixture_disk_bytes'] = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
            profiles = (phases['whole_run'], result['interactive_child']['interactive'],
                        phases['mixed_collection_ranking_snapshot']['snapshot_child']['snapshot'])
            result['total_measured_cpu_seconds'] = sum(p['user_cpu_seconds'] + p['system_cpu_seconds'] for p in profiles)
            result['two_vcpu_average_percent'] = 50 * result['total_measured_cpu_seconds'] / phases['whole_run']['wall_seconds']
            result['ok'] = True
        finally:
            stop.set()
            for process in children:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=10)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--families', type=int, default=3000)
    parser.add_argument('--description-bytes', type=int, default=4096)
    parser.add_argument('--snapshot-mib', type=int, default=16)
    args = parser.parse_args()
    try:
        result = benchmark(args.families, args.description_bytes, args.snapshot_mib)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}), flush=True)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
