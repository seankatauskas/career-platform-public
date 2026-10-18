"""Measure real health/CLI interpreter startup without providers or private state.

Run inside a candidate image, for example:
  docker run --rm -i --network none --read-only --memory 4g --cpus 2 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=64m \
    --entrypoint python IMAGE - < tests/benchmark_python_startup.py
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time


def benchmark(repeats: int = 11) -> dict:
    if type(repeats) is not int or not 3 <= repeats <= 100:
        raise ValueError('repeats must be an integer in 3..100')
    with tempfile.TemporaryDirectory(prefix='python-startup-', dir='/tmp') as directory:
        path = Path(directory) / 'worker-core.json'
        path.write_text(json.dumps({'schema_version': 1, 'state': 'idle',
                                   'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}))
        probes = {
            'worker_health': ['-m', 'job_search.cloud', 'healthcheck', 'core', '--health-dir', directory],
            'tools_cli': ['-m', 'job_search.tool_service', '--help'],
        }
        samples = {name: [] for name in probes}
        # Alternate probes; each sample starts a new interpreter. Child CPU is a
        # delta across this one synchronous process, not cumulative lifetime RSS.
        for _ in range(repeats):
            for name, arguments in probes.items():
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                started = time.monotonic()
                subprocess.run([sys.executable, *arguments], check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
                wall = time.monotonic() - started
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
                samples[name].append({'cpu_ms': round(cpu * 1000, 3), 'wall_ms': round(wall * 1000, 3)})
    return {
        'schema_version': 1,
        'environment': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version(),
                        'runtime_uid': getattr(os, 'geteuid', lambda: None)()},
        'repetitions': repeats,
        'results': {name: {'samples': rows, 'median_cpu_ms': statistics.median(row['cpu_ms'] for row in rows),
                           'median_wall_ms': statistics.median(row['wall_ms'] for row in rows)}
                    for name, rows in samples.items()},
        'limitations': ['No provider calls or application data; worker health uses a temporary fictional health record.',
                       'CLI/import startup only; no document processing or task execution.',
                       'CPU limits, architecture and host load affect results; no performance threshold is enforced.'],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=11)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    encoded = json.dumps(benchmark(args.repeats), indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
