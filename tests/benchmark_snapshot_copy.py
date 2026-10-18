"""Bounded synthetic preparation-copy benchmark; never use production files.

Compare the previous serial copy/hash with the actual bounded worker helper.
Linux --evict affects only fixture files; both paths flush and compare hashes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import tempfile
import time

from job_search import aws_ops as ops
from tests.benchmark_host_preparation import evict, io_counts


def serial(source, target):
    hasher = hashlib.sha256(); copied = 0; signature = b''
    with source.open('rb') as stream, target.open('xb') as out:
        while block := stream.read(1024**2):
            out.write(block); hasher.update(block)
            if not copied: signature = block[:16]
            copied += len(block)
    return hasher.hexdigest(), copied, signature


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--megabytes', type=int, default=128)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--evict', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 32 <= args.megabytes <= 4096 or not 1 <= args.runs <= 5:
        parser.error('use 32–4096 MiB and 1–5 runs')
    measurements = []
    with tempfile.TemporaryDirectory(prefix='snapshot-copy-benchmark-') as temporary:
        root = Path(temporary).resolve(); source, target = root / 'source', root / 'target'
        with source.open('wb') as out:
            block = bytes(range(256)) * 4096
            for _ in range(args.megabytes): out.write(block)
        ops.sync_tree(root); expected = ops.digest(source)
        for trial in range(args.runs):
            order = ('serial', 'overlap') if trial % 2 == 0 else ('overlap', 'serial')
            for mode in order:
                residency = evict(root) if args.evict else None
                before = io_counts(); start = time.monotonic(); cpu = time.process_time()
                result = (serial(source, target) if mode == 'serial' else
                          ops.copy_preparation_file(source, target, source.stat().st_size, {}))
                with target.open('rb') as stream: os.fsync(stream.fileno())
                elapsed = time.monotonic() - start; cpu = time.process_time() - cpu; after = io_counts()
                if result != (expected, args.megabytes * 1024**2, block[:16]) or ops.digest(target) != expected:
                    raise RuntimeError('copy/hash mismatch')
                measurements.append({'mode': mode, 'trial': trial, 'seconds': elapsed,
                    'cpu_seconds': cpu, 'initial_residency': residency, 'sha256': result[0],
                    'physical_read_bytes': after.get('read_bytes', 0) - before.get('read_bytes', 0),
                    'physical_write_bytes': after.get('write_bytes', 0) - before.get('write_bytes', 0)})
                target.unlink()
        report = {'scope': 'fictional local copy/hash; not EC2 deployment timings',
                  'platform': platform.platform(), 'bytes': source.stat().st_size,
                  'cache': 'fixture-only advisory eviction' if args.evict else 'not evicted',
                  'measurements': measurements,
                  'median_seconds': {mode: statistics.median(r['seconds'] for r in measurements if r['mode'] == mode)
                                     for mode in ('serial', 'overlap')}}
    output = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(output)
    else: print(output)


if __name__ == '__main__': main()
