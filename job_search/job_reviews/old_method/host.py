"""Trusted asynchronous orchestration; source-only worker, host-only publication."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time

from ...contracts import ContractError
from ...maintenance import require_start, draining
from ...runtime import load_runtime_config
from ..availability import check_boards
from ..contracts import identifier
from ..runner import build_authority, runner_lock, _private_dir, _write_json
from ..runner_config import load_runner_config
from . import WORKFLOW
from .store import Store
from .runtime import worker, read_output


def execution(config):
    return {'image': config.model_image, 'model': config.model,
            'reasoning_effort': config.reasoning_effort, 'codex_version': config.codex_version}


def dispatch(rid, runner_config, *, run=subprocess.run):
    identifier(rid, 'review_id')
    release = Path(__file__).resolve().parents[3]
    gate = os.environ.get('JOB_SEARCH_MAINTENANCE_GATE')
    if not gate:
        raise ContractError('AWS asynchronous dispatch requires the maintenance gate')
    # A transient service owns the process after SSM/the calling session exits.
    run(['systemd-run', '--quiet', '--collect', '--unit=job-search-old-method-' + rid,
         '--property=Type=exec', '--property=UMask=0077', '--property=KillMode=mixed',
         '--property=TimeoutStopSec=660', '--property=NoNewPrivileges=true',
         '--setenv=JOB_SEARCH_MAINTENANCE_GATE=' + gate,
         '--working-directory=' + str(release), '/usr/bin/python3', '-m',
         'job_search.job_reviews.old_method.host', 'run', '--runner-config', str(runner_config),
         '--review-id', rid], check=True, capture_output=True, text=True)


@contextmanager
def serialized(config, stopped):
    while True:
        require_start('review-runner')
        if stopped.is_set():
            raise ContractError('review stopped while queued')
        lock = runner_lock(config.state_dir)
        try:
            lock.__enter__()
            break
        except ContractError as exc:
            if str(exc) != 'another review invocation is already running':
                raise
            stopped.wait(1)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def run_review(config, store, rid, *, launch=worker, availability=check_boards):
    stopped = threading.Event()
    previous_signals = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_signals[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        with serialized(config, stopped):
            require_start('review-runner')
            status = store.status(rid)
            if status['status'] == 'published' or status['phase'] == 'qualified':
                return status
            meta = store.metadata(rid)
            if meta['execution'] != execution(config):
                raise ContractError('resume requires the frozen execution profile and image')
            packet = store.packet(rid)
            root = _private_dir(config.state_dir / 'old-method' / rid / ('attempt_' + secrets.token_hex(6)))
            store.update(rid, phase='running', error=None, attempt=str(root))
            deadline = time.monotonic() + config.invocation_timeout_seconds
            journal = {'workflow': WORKFLOW, 'review_id': rid, 'active': True, 'status': 'running'}
            _write_json(config.state_dir/'status.json', journal)
            totals = {}
            def telemetry(event):
                for key, value in event.items():
                    if type(value) in (int, float):
                        totals[key] = totals.get(key, 0) + value
            try:
                result = meta.get('result')
                if result and result.get('sealed') is True:
                    store.checkpoint(rid, result, sealed=True)
                else:
                    with launch(config, packet, root, previous=meta.get('result'), telemetry=telemetry) as handle:
                        last_saved = None
                        while handle.poll() is None:
                            path = handle.output_directory/'progress.json'
                            if path.exists():
                                candidate = read_output(path)
                                if candidate != last_saved:
                                    store.checkpoint(rid, candidate)
                                    last_saved = candidate
                            if stopped.is_set() or draining() or time.monotonic() >= deadline:
                                handle.terminate()
                                store.update(rid, phase='interrupted', inference=totals)
                                return store.status(rid)
                            time.sleep(1)
                        path = handle.output_directory/'progress.json'
                        if path.exists():
                            store.checkpoint(rid, read_output(path))
                        if handle.wait() != 0:
                            raise ContractError('Codex worker failed; saved progress can be resumed')
                        result = read_output(handle.output_directory/'result.json')
                        if result != read_output(path):
                            raise ContractError('saved work changed after the final result was sealed')
                        store.checkpoint(rid, result, sealed=True)
                if stopped.is_set() or draining():
                    store.update(rid, phase='interrupted', inference=totals)
                    return store.status(rid)
                store.update(rid, phase='validating', inference=totals)
                if not meta['publish']:
                    store.update(rid, phase='qualified')
                    return store.status(rid)
                app = load_runtime_config(config.application_config, required=True)
                selected = [packet['jobs'][n-1] for n in result['order']]
                observations = availability(selected, contact=app.scraper_contact or '')
                if stopped.is_set() or draining():
                    store.update(rid, phase='interrupted')
                    return store.status(rid)
                store.publish(rid, observations)
                return store.status(rid)
            except Exception as exc:
                store.update(rid, phase='failed', error=str(exc) if isinstance(exc, ContractError) else type(exc).__name__, inference=totals)
                raise
            finally:
                _write_json(config.state_dir/'status.json', {**journal, 'active': False, 'status': store.status(rid)['phase']})
    finally:
        for sig, old in previous_signals.items():
            signal.signal(sig, old)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('submit', 'run', 'status', 'resume', 'export'))
    parser.add_argument('--runner-config', type=Path, required=True)
    parser.add_argument('--review-id')
    parser.add_argument('--window-start')
    parser.add_argument('--window-end')
    parser.add_argument('--no-publish', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_runner_config(args.runner_config)
        store = Store(build_authority(config).service)
        if args.action == 'submit':
            if not args.window_start or not args.window_end:
                raise ContractError('explicit window_start and window_end are required')
            with runner_lock(config.state_dir):
                require_start('review-runner')
                result = store.create(args.window_start, args.window_end, execution=execution(config), publish=not args.no_publish)
                try:
                    dispatch(result['review_id'], args.runner_config)
                except Exception:
                    store.update(result['review_id'], phase='dispatch_failed')
                    print(json.dumps(store.status(result['review_id'])))
                    raise
        else:
            if not args.review_id:
                raise ContractError('review_id is required')
            result = store.status(args.review_id)
            if args.action == 'resume':
                require_start('review-runner')
                if result['status'] != 'active' or result['phase'] == 'qualified':
                    raise ContractError('review is already complete')
                dispatch(args.review_id, args.runner_config)
            elif args.action == 'run':
                result = run_review(config, store, args.review_id)
            elif args.action == 'export':
                if args.output is None:
                    raise ContractError('export requires a private output path')
                if args.output.exists():
                    raise ContractError('output already exists')
                _write_json(args.output, store.packet(args.review_id))
                result = {'review_id': args.review_id, 'output': str(args.output)}
        print(json.dumps(result))
        return 0
    except (ContractError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc) if isinstance(exc, ContractError) else type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
