"""Private, bounded per-attempt Linux resource evidence; never reads task payloads.

Run ``python -m job_search.job_reviews.resource_monitor /private/attempt`` to
print a saved assessment. Sampling and persistence failures never fail a review.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import resource
import sqlite3
import subprocess
import threading
import time

from ..resource_usage import memory_snapshot, _read, _pairs, _number, _cgroup_path
from ..activation import GROUPS
from .runner import _write_json

SERVICES = frozenset(('core', 'model', 'interactive-core', 'interactive-model',
                      'dashboard', 'hermes', 'mcp', 'interactions', 'tools'))
TASKS = frozenset(task for tasks in GROUPS.values() for task in tasks)
MEMORY = ('current', 'peak', 'swap')
STAT = ('anon', 'file', 'kernel', 'inactive_file', 'shmem')
CPU = ('usage_usec', 'user_usec', 'system_usec', 'nr_periods', 'nr_throttled', 'throttled_usec')
EVENTS = ('high', 'max', 'oom', 'oom_kill')
IO = ('rbytes', 'wbytes', 'rios', 'wios')
CG_KEYS = ({'memory_' + x + '_bytes' for x in MEMORY + STAT} |
           {'cpu_' + x for x in CPU} | {'event_' + x for x in EVENTS} |
           {'io_' + x for x in IO})
HOST_KEYS = {'host_' + x + '_bytes' for x in ('total', 'available', 'anonymous', 'file_cache',
             'swap_total', 'swap_free', 'slab', 'reclaimable_slab')} | {
             'cpu_busy_ticks', 'cpu_total_ticks', 'cpu_iowait_ticks', 'cpu_steal_ticks', 'swap_in_pages', 'swap_out_pages'} | {
             kind + '_' + level + '_stall_usec' for kind in ('memory', 'io', 'cpu') for level in ('some', 'full')}
COORD_KEYS = {'rss_bytes', 'cpu_usec'}
MAX_TIMELINE_BYTES = 16 * 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat()


def numeric(values, allowed):
    return {k: v for k, v in values.items() if k in allowed and type(v) is int and v >= 0}


def cgroup_snapshot(path):
    result = {}
    for key, name in (('current', 'memory.current'), ('peak', 'memory.peak'), ('swap', 'memory.swap.current')):
        result['memory_' + key + '_bytes'] = _number(_read(path / name).strip())
    for filename, keys, prefix, suffix in (('memory.stat', STAT, 'memory_', '_bytes'),
                                          ('cpu.stat', CPU, 'cpu_', ''),
                                          ('memory.events', EVENTS, 'event_', '')):
        pairs = _pairs(_read(path / filename))
        result.update({prefix + key + suffix: pairs.get(key) for key in keys})
    text = _read(path / 'io.stat')
    if text.strip():
        for key in IO:
            numbers = [_number(part.split('=', 1)[1]) for line in text.splitlines()
                       for part in line.split()[1:] if part.startswith(key + '=')]
            if numbers and all(n is not None for n in numbers):
                result['io_' + key] = sum(numbers)
    return numeric(result, CG_KEYS)


def host_snapshot(proc=Path('/proc')):
    result = numeric(memory_snapshot(proc_root=proc), HOST_KEYS)
    first = _read(proc / 'stat').splitlines()
    if first and first[0].startswith('cpu '):
        ticks = [_number(n) for n in first[0].split()[1:9]]  # exclude double-counted guest ticks
        if len(ticks) == 8 and all(n is not None for n in ticks):
            result.update(cpu_total_ticks=sum(ticks), cpu_busy_ticks=sum(ticks) - ticks[3] - ticks[4] - ticks[7],
                          cpu_iowait_ticks=ticks[4], cpu_steal_ticks=ticks[7])
    vm = _pairs(_read(proc / 'vmstat'))
    result.update(swap_in_pages=vm.get('pswpin'), swap_out_pages=vm.get('pswpout'))
    for kind in ('memory', 'io', 'cpu'):
        for line in _read(proc / 'pressure' / kind).splitlines():
            parts = line.split()
            if parts and parts[0] in ('some', 'full'):
                for part in parts[1:]:
                    if part.startswith('total='):
                        result[kind + '_' + parts[0] + '_stall_usec'] = _number(part[6:])
    return numeric(result, HOST_KEYS)


def running_tasks(db):
    started = time.monotonic()
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as con:
        con.execute('PRAGMA query_only=ON')
        con.set_progress_handler(lambda: int(time.monotonic() - started > .1), 1000)
        rows = con.execute("SELECT task_kind FROM work_items WHERE status='running' LIMIT 101").fetchall()
    result = {}
    for (task,) in rows[:100]:
        task = task if task in TASKS else 'other'
        result[task] = result.get(task, 0) + 1
    return {'counts': result, 'truncated': len(rows) > 100}


class LinuxReader:
    """Discover fixed Compose services and the exact launched worker, then read cgroups.

    Docker inspection requests only numeric state and opaque container identity,
    never environment, arguments, mounts, labels or logs.
    """
    def __init__(self, db):
        self.db = db
        self.worker_name = None
        self.containers = {}
        self.last_discovery = -math.inf
        self.tasks = None
        self.errors = 0

    def discover(self):
        names = ['job-search-' + name + '-1' for name in sorted(SERVICES)]
        if self.worker_name:
            names.append(self.worker_name)
        fmt = '{{.Name}} {{.Id}} {{.State.Pid}} {{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.State.OOMKilled}}'
        run = subprocess.run(['docker', 'inspect', '--format', fmt, *names],
                             capture_output=True, text=True, timeout=2)
        self.containers = {}
        for line in run.stdout.splitlines()[:32]:
            parts = line.split()
            if len(parts) != 6:
                continue
            name, identity, pid, memory, cpu, oom = parts
            name = name.lstrip('/')
            if name not in names or not re.fullmatch('[a-f0-9]{64}', identity):
                continue
            if not pid.isdecimal() or int(pid) < 1:
                continue
            group = _cgroup_path(Path('/proc'), int(pid), Path('/sys/fs/cgroup'))
            if group:
                role = 'review-worker' if name == self.worker_name else name[11:-2]
                self.containers[identity] = (role, group, _number(memory), _number(cpu))
        if run.returncode:
            self.errors += 1

    def __call__(self):
        if not Path('/proc/stat').is_file():
            return {'host': {}, 'coordinator': {}, 'containers': {}, 'running_tasks': None, 'read_errors': 1}
        worker_missing = self.worker_name and not any(row[0] == 'review-worker' for row in self.containers.values())
        if time.monotonic() - self.last_discovery >= (1 if worker_missing else 10):
            self.last_discovery = time.monotonic()
            try:
                self.discover()
            except Exception:
                self.errors += 1
            try:
                self.tasks = running_tasks(self.db)
            except Exception:
                self.errors += 1
                self.tasks = None
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = memory_snapshot().get('process_rss_bytes')
        containers = {}
        for identity, (role, path, memory, cpu) in self.containers.items():
            counters = cgroup_snapshot(path)
            if counters:
                containers[identity] = {'role': role, 'memory_limit_bytes': memory,
                                        'cpu_limit_nanocpus': cpu, 'counters': counters}
        return {'host': host_snapshot(), 'coordinator': numeric({
                    'rss_bytes': rss, 'cpu_usec': int((usage.ru_utime + usage.ru_stime) * 1000000)}, COORD_KEYS),
                'containers': containers, 'running_tasks': self.tasks, 'read_errors': self.errors}


def accumulate(target, values):
    for key, value in values.items():
        row = target.setdefault(key, {'samples': 0, 'first': value, 'last': value,
                                     'minimum': value, 'maximum': value, 'decreased': False})
        row.update(samples=row['samples'] + 1, last=value, minimum=min(row['minimum'], value),
                   maximum=max(row['maximum'], value), decreased=row['decreased'] or value < row['last'])


def delta(counters, key):
    row = counters.get(key)
    return row['last'] - row['first'] if row and not row['decreased'] else None


def assessment(report):
    host = report['counters'].get('host', {})
    def extreme(key, field):
        return host.get(key, {}).get(field)
    busy, total = delta(host, 'cpu_busy_ticks'), delta(host, 'cpu_total_ticks')
    workers = [v for v in report['containers'].values() if v['role'] == 'review-worker']
    cpu = [v['counters'].get('cpu_usage_usec', {}).get('maximum') for v in workers]
    def worker_max(key):
        values = [v['counters'].get(key, {}).get('maximum') for v in workers]
        return max(values) if values and all(v is not None for v in values) else None
    def worker_sum(key):
        values = [v['counters'].get(key, {}).get('maximum') for v in workers]
        return sum(values) if values and all(v is not None for v in values) else None
    peaks = [v['counters'].get('memory_peak_bytes', {}).get('maximum') for v in workers]
    return {'host_average_cpu_percent': round(100 * busy / total, 3) if busy is not None and total else None,
            'host_iowait_ticks': delta(host, 'cpu_iowait_ticks'),
            'host_steal_ticks': delta(host, 'cpu_steal_ticks'),
            'host_min_available_bytes': extreme('host_available_bytes', 'minimum'),
            'host_total_bytes': extreme('host_total_bytes', 'last'),
            'host_memory_full_stall_usec': delta(host, 'memory_full_stall_usec'),
            'host_io_full_stall_usec': delta(host, 'io_full_stall_usec'),
            'coordinator_cpu_seconds': (delta(report['counters'].get('coordinator', {}), 'cpu_usec') / 1000000
                if delta(report['counters'].get('coordinator', {}), 'cpu_usec') is not None else None),
            'worker_sampled_anon_peak_bytes': worker_max('memory_anon_bytes'),
            'worker_sampled_file_cache_peak_bytes': worker_max('memory_file_bytes'),
            'worker_sampled_swap_peak_bytes': worker_max('memory_swap_bytes'),
            'worker_observed_oom_kills': worker_sum('event_oom_kill'),
            'worker_observed_throttled_usec': worker_sum('cpu_throttled_usec'),
            'worker_observed_read_bytes': worker_sum('io_rbytes'),
            'worker_observed_write_bytes': worker_sum('io_wbytes'),
            'host_swap_in_pages': delta(host, 'swap_in_pages'),
            'host_swap_out_pages': delta(host, 'swap_out_pages'),
            'worker_cpu_seconds_observed_lower_bound': sum(cpu) / 1000000 if cpu and all(v is not None for v in cpu) else None,
            'worker_observed_lifetime_peak_bytes': max(peaks) if peaks and all(v is not None for v in peaks) else None,
            'sizing_verdict': 'requires workload and overlap review; not an automatic resize approval'}


class AttemptResources:
    def __init__(self, root, db, *, timeout_seconds=3600, reader=None, identity=None):
        self.root = Path(root)
        self.reader = reader or LinuxReader(db)
        self.interval = max(2, min(60, math.ceil(timeout_seconds / 3600)))
        self.stop = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        self.phase = 'freezing_inputs'
        self.timeline_bytes = 0
        self.started = time.monotonic()
        self.report = {'version': 1, 'started_at': stamp(), 'finished_at': None, 'status': 'in_progress',
                       'interval_seconds': self.interval, 'samples': 0, 'sample_errors': 0, 'write_errors': 0,
                       'timeline_truncated': False, 'counters': {}, 'containers': {}, 'phases': [],
                       'input_jobs': None, 'selected_jobs': None, 'overlap_max_running': {},
                       'identity': {k: v for k, v in (identity or {}).items() if k in ('image', 'model', 'reasoning_effort', 'codex_version')},
                       'semantics': {'worker_cpu': 'last observed new-container lifetime CPU; exit tail may be missing',
                           'container_cpu': 'CPU counters include descendants; use per-identity deltas for existing services',
                           'memory': 'current includes reclaimable file cache; peaks from different counters need not coincide',
                           'coverage': 'sampling can miss short workers, peaks and final OOM events; missing is unknown',
                           'host': 'whole-host counters include all workloads; busy CPU ticks exclude iowait and steal',
                           'coordinator': 'process CPU/RSS excludes child processes; includes monitor thread',
                           'overlap': 'running task counts refreshed every 10s; absence does not prove no overlap',
                           'privacy': 'no prompts, descriptions, credentials, model responses or task payloads'}}

    def mark(self, phase):
        if phase not in ('freezing_inputs', 'loading_inputs', 'worker', 'validating', 'publishing'):
            return
        with self.lock:
            self.phase = phase
            self.report['phases'].append({'phase': phase, 'elapsed_seconds': round(time.monotonic() - self.started, 3)})

    def worker(self, name):
        if isinstance(self.reader, LinuxReader) and isinstance(name, str) and re.fullmatch('career-review-old-[a-f0-9]{16}', name):
            self.reader.worker_name = name
            self.reader.last_discovery = -math.inf
        self.mark('worker')

    def outcome(self, value):
        if value in ('awaiting_inputs', 'queued', 'running', 'validating', 'qualified', 'published', 'failed', 'interrupted'):
            with self.lock:
                self.report['review_phase'] = value

    def count(self, key, value):
        if key in ('input_jobs', 'selected_jobs') and type(value) is int and value >= 0:
            with self.lock:
                self.report[key] = value

    def _save(self):
        try:
            self.report['elapsed_seconds'] = round(time.monotonic() - self.started, 3)
            self.report['assessment'] = assessment(self.report)
            _write_json(self.root / 'resources.json', self.report)
        except Exception:
            self.report['write_errors'] += 1

    def sample(self):
        with self.lock:
            try:
                value = self.reader()
                sample = {'at': stamp(), 'elapsed_seconds': round(time.monotonic() - self.started, 3), 'phase': self.phase}
                for scope, keys in (('host', HOST_KEYS), ('coordinator', COORD_KEYS)):
                    sample[scope] = numeric(value.get(scope, {}), keys)
                    accumulate(self.report['counters'].setdefault(scope, {}), sample[scope])
                sample['containers'] = {}
                for identity, row in list(value.get('containers', {}).items())[:32]:
                    if not re.fullmatch('[a-f0-9]{64}', identity) or row.get('role') not in SERVICES | {'review-worker'}:
                        continue
                    if identity not in self.report['containers'] and len(self.report['containers']) >= 64:
                        self.report['sample_errors'] += 1
                        continue
                    counters = numeric(row.get('counters', {}), CG_KEYS)
                    target = self.report['containers'].setdefault(identity, {'role': row['role'], 'counters': {},
                        'first_observed_at': sample['at'], 'memory_limit_bytes': row.get('memory_limit_bytes') if type(row.get('memory_limit_bytes')) is int else None,
                        'cpu_limit_nanocpus': row.get('cpu_limit_nanocpus') if type(row.get('cpu_limit_nanocpus')) is int else None})
                    target['last_observed_at'] = sample['at']
                    accumulate(target['counters'], counters)
                    sample['containers'][identity] = {'role': row['role'], 'counters': counters}
                tasks = value.get('running_tasks')
                sample['running_tasks'] = None if tasks is None else {
                    'counts': numeric(tasks.get('counts', {}), TASKS | {'other'}), 'truncated': tasks.get('truncated') is True}
                if sample['running_tasks']:
                    for task, count in sample['running_tasks']['counts'].items():
                        self.report['overlap_max_running'][task] = max(count, self.report['overlap_max_running'].get(task, 0))
                self.report['read_errors'] = value.get('read_errors', 0)
                self.report['samples'] += 1
                self.report['last_sample_at'] = sample['at']
                encoded = json.dumps(sample, separators=(',', ':'), allow_nan=False) + '\n'
                if self.timeline_bytes + len(encoded.encode()) <= MAX_TIMELINE_BYTES:
                    try:
                        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
                        fd = os.open(self.root / 'resources.jsonl', flags, 0o600)
                        with os.fdopen(fd, 'a') as stream:
                            stream.write(encoded)
                        self.timeline_bytes += len(encoded.encode())
                    except Exception:
                        self.report['write_errors'] += 1
                else:
                    self.report['timeline_truncated'] = True
            except Exception:
                self.report['sample_errors'] += 1
            if self.report['samples'] % max(1, int(30 / self.interval)) == 0:
                self._save()

    def _run(self):
        while not self.stop.wait(self.interval):
            self.sample()

    def __enter__(self):
        try:
            self.mark('freezing_inputs')
            self.sample()
            self._save()
            self.thread = threading.Thread(target=self._run, name='review-resources', daemon=True)
            self.thread.start()
        except Exception:
            self.report['sample_errors'] += 1
        return self

    def __exit__(self, exc_type, *_):
        self.stop.set()
        if self.thread and self.thread.ident is not None:
            self.thread.join()
        self.sample()
        self.report['finished_at'] = stamp()
        self.report['status'] = 'failed' if exc_type else 'finished'
        self._save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('attempt', type=Path)
    args = parser.parse_args(argv)
    path = args.attempt / 'resources.json'
    with path.open() as stream:
        report = json.loads(stream.read(2 * 1024 * 1024))
    print(json.dumps({'started_at': report['started_at'], 'finished_at': report['finished_at'],
        'status': report['status'], 'review_phase': report.get('review_phase'), 'elapsed_seconds': report.get('elapsed_seconds'),
        'samples': report['samples'], 'sample_errors': report['sample_errors'], 'write_errors': report['write_errors'],
        'read_errors': report.get('read_errors'), 'timeline_truncated': report['timeline_truncated'],
        'input_jobs': report['input_jobs'], 'selected_jobs': report['selected_jobs'],
        'assessment': assessment(report), 'overlap_max_running': report['overlap_max_running'],
        'phases': report['phases'], 'semantics': report['semantics']}, indent=2))


if __name__ == '__main__':
    main()
