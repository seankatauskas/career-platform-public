"""Trusted host benchmark using frozen production evidence and the normal runner."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..contracts import ContractError
from ..maintenance import require_start
from .runner import (BENCHMARK_OVERRIDES, add_benchmark_arguments, benchmark_overrides,
                     build_authority, main as runner_main, runner_lock)
from .runner_config import load_runner_config


def host_config(path):
    from ..aws_ops import load_config, manifest, release_path, verify_mount
    if os.geteuid() != 0:
        raise ContractError('benchmark launch requires the trusted root coordinator')
    operations = load_config(path)
    verify_mount(operations)
    release = manifest(release_path(operations))
    if 'reviewer_image' not in release:
        raise ContractError('current release has no isolated reviewer runtime')
    os.environ['JOB_SEARCH_MAINTENANCE_GATE'] = str(Path(operations['data_root']) / 'maintenance/gate.json')
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
        os.environ.pop(key, None)
    return Path(operations['data_root']) / 'operations/review-config' / release['release_id'] / 'runner.json'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operations-config', type=Path, default=Path('/etc/job-search/operations.json'))
    parser.add_argument('action', choices=('start', 'run'))
    parser.add_argument('--source-review-id')
    parser.add_argument('--ordinals', help='Comma-separated source ordinals; at most 320')
    parser.add_argument('--label')
    parser.add_argument('--idempotency-key')
    parser.add_argument('--review-id')
    add_benchmark_arguments(parser)
    args = parser.parse_args(argv)
    try:
        if args.action == 'start' and benchmark_overrides(args):
            raise ContractError('benchmark runtime overrides belong to run, not start')
        path = host_config(args.operations_config)
        config = load_runner_config(path)
        with runner_lock(config.state_dir):
            require_start('review-runner')
            authority = build_authority(config)
            if args.action == 'start':
                if not all((args.source_review_id, args.ordinals, args.label, args.idempotency_key)):
                    raise ContractError('start requires source review, ordinals, label and idempotency key')
                try:
                    ordinals = [int(value) for value in args.ordinals.split(',')]
                except ValueError:
                    raise ContractError('source ordinals must be integers') from None
                result = authority.start_benchmark({
                    'review_id': args.source_review_id, 'ordinals': ordinals,
                    'label': args.label, 'idempotency_key': args.idempotency_key,
                })
                print(json.dumps(result, sort_keys=True))
                return 0
            if not args.review_id:
                raise ContractError('run requires a benchmark review ID')
            status = authority.status(args.review_id)
            if not status['metadata'].get('benchmark'):
                raise ContractError('benchmark run requires a marked experimental review')
        # Reacquires the normal runner lock and maintenance gate, installs the
        # same signal handlers, and uses the same production APIs and runtime.
        forwarded = []
        for name in BENCHMARK_OVERRIDES:
            value = getattr(args, name)
            if value is not None:
                forwarded.append('--' + name.replace('_', '-'))
                if type(value) is not bool:
                    forwarded.append(str(value))
        return runner_main(['run', '--runner-config', str(path), '--review-id',
                            args.review_id, '--max-jobs', '320', *forwarded])
    except (ContractError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
