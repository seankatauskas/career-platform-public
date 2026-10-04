"""Trusted, resumable coordinator. This module never makes suitability decisions."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager, nullcontext
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import threading
import time

from ..contracts import ContractError, canonical_json
from ..maintenance import draining, require_start
from .contracts import stamp, timestamp
from .runner_config import RunnerConfig, load_runner_config


def _private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ContractError('review runner directory cannot be a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ContractError('review runner directory must be owned by its user with mode 0700')
    return path


def _write_json(path, value):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def runner_lock(state_dir):
    import fcntl
    state_dir = _private_dir(state_dir)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(state_dir / 'runner.lock', flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ContractError('review runner lock must be a private owned regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError('another review invocation is already running') from None
        yield
    finally:
        os.close(fd)


def runner_status(config):
    path = config.state_dir / 'status.json'
    result = {'schedule_enabled': config.schedule_enabled,
              'schedule_calendar': config.schedule_calendar,
              'model': config.model, 'reasoning_effort': config.reasoning_effort,
              'last_run': None}
    if path.exists():
        result['last_run'] = json.loads(path.read_text())
        if result['last_run'].get('active'):
            try:
                with runner_lock(config.state_dir):
                    result['last_run'].update(active=False, status='interrupted', recovery_required=True)
            except ContractError as exc:
                if str(exc) != 'another review invocation is already running':
                    raise
    return result


class ReviewCoordinator:
    def __init__(self, config: RunnerConfig, authority, runtime, *,
                 is_draining=draining, monotonic=time.monotonic, sleep=time.sleep):
        self.config = config.validate()
        self.authority, self.runtime = authority, runtime
        self.is_draining, self.clock, self.sleep = is_draining, monotonic, sleep
        self.stop = threading.Event()
        self._journal_lock = threading.Lock()

    def _record(self, **changes):
        with self._journal_lock:
            self.journal.update(changes, updated_at=stamp())
            _write_json(self.config.state_dir / 'status.json', self.journal)
            _write_json(self.config.state_dir / (self.journal['run_id'] + '.json'), self.journal)

    def _stopping(self):
        return self.stop.is_set() or self.is_draining() or self.clock() >= self.deadline

    def _review(self, mode, review_id, window_start, window_end):
        service = self.authority.service
        if review_id:
            result = self.authority.status(review_id)
        else:
            result = None
            if mode == 'recurring':
                before = None
                while True:
                    page = service.call('list', {} if before is None else {'before': before})
                    active = [r for r in page['reviews'] if r['mode'] == 'recurring' and r['status'] == 'active']
                    if active:
                        result = self.authority.status(active[0]['review_id'])
                        break
                    before = page.get('next_before')
                    if before is None:
                        break
                if result is None:
                    collection = service.collection_provider() if service.collection_provider else {}
                    last_scan = collection.get('last_scan_at')
                    if not last_scan or (timestamp(service.now()) - timestamp(last_scan)).total_seconds() > self.config.max_collection_age_seconds:
                        raise ContractError('a fresh completed collection is required before a recurring review')
            if result is None:
                args = {'mode': mode, 'idempotency_key': self.journal['run_id'] + '-start'}
                if window_start:
                    args['window_start'] = window_start
                if window_end:
                    args['window_end'] = window_end
                result = self.authority.start(args)
        if result['metadata'].get('execution_mode') != 'isolated':
            raise ContractError('existing review is not isolated; finish it separately or choose a custom window')
        return result

    def _pending(self, review_id, kind):
        if kind == 'check':
            return self.authority.status(review_id)['audit_remaining']
        found, after = [], 0
        while len(found) < self.config.batch_size * self.config.concurrency:
            page = self.authority.service.call('batch', {
                'review_id': review_id, 'after': after, 'limit': 20, 'filter': 'pending',
            })
            found.extend(item['ordinal'] for item in page['items'])
            after = page.get('next_after')
            if after is None:
                break
        return found

    def _assignment(self, review_id, ordinals, kind):
        if self._stopping():
            return {'status': 'stopped', 'ordinals': ordinals, 'kind': kind}
        runtime_id = 'worker_' + secrets.token_hex(16)
        grant = self.authority.issue(review_id, ordinals, kind, {
            'runtime_id': runtime_id, 'model': self.config.model,
            'reasoning_effort': self.config.reasoning_effort,
        })
        worker = None
        try:
            # The token stays in trusted runtime composition, never a prompt or journal.
            with self.runtime.worker(grant, runtime_id=runtime_id) as worker:
                self._record(active=True)
                started = self.clock()
                next_renew = started + 60
                while worker.poll() is None:
                    now = self.clock()
                    if self._stopping() or now - started >= self.config.assignment_timeout_seconds:
                        worker.terminate()
                        return {'status': 'interrupted', 'ordinals': ordinals, 'kind': kind}
                    if now >= next_renew:
                        self.authority.renew(grant['grant_id'])
                        next_renew = now + 60
                    self.sleep(0.25)
                code = worker.wait()
                return {'status': 'exited', 'exit_code': code, 'ordinals': ordinals,
                        'kind': kind, 'grant_id': grant['grant_id'], 'runtime_id': runtime_id}
        finally:
            # Committed receipts survive revocation. A fresh assignment sees only pending items.
            self.authority.revoke(grant['grant_id'])

    def run(self, *, mode='recurring', review_id=None, window_start=None, window_end=None,
            scheduled=False, max_jobs=None, lock_held=False):
        if scheduled and not self.config.schedule_enabled:
            return {'status': 'disabled', 'schedule_enabled': False}
        if self.is_draining():
            return {'status': 'maintenance', 'schedule_enabled': self.config.schedule_enabled}
        with nullcontext() if lock_held else runner_lock(self.config.state_dir):
            if self.is_draining():
                return {'status': 'maintenance', 'schedule_enabled': self.config.schedule_enabled}
            self.deadline = self.clock() + self.config.invocation_timeout_seconds
            self.journal = {'run_id': 'review_run_' + secrets.token_hex(16), 'started_at': stamp(),
                            'status': 'starting', 'active': True, 'model': self.config.model,
                            'reasoning_effort': self.config.reasoning_effort, 'assignments': 0,
                            'schedule_enabled': self.config.schedule_enabled}
            self._record()
            try:
                recovery = self.authority.reconcile_interrupted()
                self.runtime.recover(recovery['workers'])
                self._record(recovered_grants=recovery['revoked_grants'],
                             recovered_claims=recovery['released_claims'])
                current = self._review(mode, review_id, window_start, window_end)
                rid = current['review_id']
                self._record(review_id=rid, total=current['total'], status='running')
                if max_jobs is not None and current['total'] > max_jobs:
                    raise ContractError('review window exceeds the trial limit; select a narrower window without dropping jobs')
                if current['status'] == 'published':
                    self._record(status='published', active=False, receipt=current['receipt'])
                    return self.journal
                attempts = {}
                with self.runtime, ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
                    for kind in ('primary', 'check'):
                        while not self._stopping():
                            ordinals = self._pending(rid, kind)
                            if not ordinals:
                                break
                            if any(attempts.get((kind, ordinal), 0) >= 1 + self.config.transient_retries for ordinal in ordinals):
                                self._record(status='incomplete', active=False, reason='assignment_attempts_exhausted')
                                return self.journal
                            size = self.config.batch_size
                            batches = [ordinals[i:i + size] for i in range(0, min(len(ordinals), size * self.config.concurrency), size)]
                            for batch in batches:
                                for ordinal in batch:
                                    attempts[(kind, ordinal)] = attempts.get((kind, ordinal), 0) + 1
                            futures = [pool.submit(self._assignment, rid, batch, kind) for batch in batches]
                            try:
                                results = []
                                for future in as_completed(futures):
                                    result = future.result()
                                    results.append(result)
                                    if result['status'] in ('stopped', 'interrupted'):
                                        self.stop.set()
                                    elif result.get('exit_code') not in (0, 75):
                                        self.stop.set()
                                        self._record(status='failed', active=False,
                                            reason='worker_failed', last_assignments=results)
                                        return self.journal
                            except BaseException:
                                self.stop.set()
                                raise
                            self._record(assignments=self.journal['assignments'] + len(results), last_assignments=results)
                        if self._stopping():
                            self._record(status='interrupted', active=False)
                            return self.journal
                if self._stopping():
                    self._record(status='interrupted', active=False)
                    return self.journal
                preview = self.authority.preview(rid)
                if not preview['ready']:
                    self._record(status='needs_review', active=False, blockers=preview['blockers'])
                    return self.journal
                if self._stopping():
                    self._record(status='interrupted', active=False)
                    return self.journal
                receipt = self.authority.publish(rid, preview['preview_sha256'], rid + '-isolated-publish')
                self._record(status='published', active=False, receipt=receipt)
                return self.journal
            except BaseException as exc:
                self.stop.set()
                detail = str(exc) if isinstance(exc, ContractError) else type(exc).__name__
                self._record(status='failed', active=False, error=detail)
                raise


class ProductionRuntime:
    """Compose two narrow sockets without giving workers host credentials."""
    def __init__(self, config, authority):
        self.config, self.authority = config, authority

    def _runtime_config(self):
        from .codex_runtime import RuntimeConfig
        return RuntimeConfig(image=self.config.model_image,
            model=self.config.model, reasoning_effort=self.config.reasoning_effort,
            codex_version=self.config.codex_version, timeout_seconds=self.config.assignment_timeout_seconds,
            worker_uid=self.config.worker_uid, worker_gid=self.config.worker_gid)

    def recover(self, workers):
        from .codex_runtime import recover_workers
        return recover_workers(self._runtime_config(), workers)

    def __enter__(self):
        from .auth_owner import NativeAuthOwner
        from .model_gateway import gateway_server
        self.stack = ExitStack()
        root = _private_dir(self.config.runtime_dir)
        self.run_root = _private_dir(root / ('run_' + secrets.token_hex(6)))
        self.model_socket = self.run_root / 'model.sock'
        self.auth = NativeAuthOwner(self.config.auth_home, self.config.codex_executable)
        try:
            self.stack.enter_context(gateway_server(self.model_socket, self.auth,
                model=self.config.model, reasoning_effort=self.config.reasoning_effort,
                uid=self.config.worker_uid, gid=self.config.worker_gid))
        except BaseException:
            self.stack.close()
            raise
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    @contextmanager
    def worker(self, grant, *, runtime_id):
        from .codex_runtime import launch_worker
        from .reviewer_api import assignment_proxy
        worker_root = _private_dir(self.run_root / ('w_' + secrets.token_hex(6)))
        socket = worker_root / 'review.sock'
        ready = threading.Event()
        with assignment_proxy(socket, self.authority, grant['token'], ready=ready):
            os.chown(socket, self.config.worker_uid, self.config.worker_gid)
            runtime_config = self._runtime_config()
            assignment = {k: grant[k] for k in ('grant_id', 'actor', 'expires_at')}
            assignment['runtime_id'] = runtime_id
            worker = launch_worker(runtime_config, assignment, worker_root,
                                  model_socket=self.model_socket, review_socket=socket)
            try:
                self.authority.mark_launch(grant['grant_id'], runtime_id, worker.receipt)
                ready.set()
                yield worker
            finally:
                if worker.poll() is None:
                    worker.terminate()


def build_authority(config):
    from ..runtime import load_runtime_config
    from ..integration import LocalJobCatalog
    from ..resume_lab.gateway import build_resume_lab_read_gateway
    from ..scanning import scan_status
    from .authority import ReviewAuthority
    from .context import profile_context
    from .service import JobReviews
    application = load_runtime_config(config.application_config, required=True)
    resume = build_resume_lab_read_gateway(application)
    return ReviewAuthority(JobReviews(application.application_db, LocalJobCatalog(application.jobs_db),
        lambda: profile_context(resume), collection_provider=lambda: scan_status(application)),
        approved_model=config.model, approved_reasoning_effort=config.reasoning_effort)


def add_arguments(parser):
    parser.add_argument('action', choices=('login', 'readiness', 'run', 'status'))
    parser.add_argument('--runner-config', type=Path, required=True)
    parser.add_argument('--mode', choices=('recurring', 'custom'), default='recurring')
    parser.add_argument('--review-id')
    parser.add_argument('--window-start')
    parser.add_argument('--window-end')
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--max-jobs', type=int)


def command(args):
    config = load_runner_config(args.runner_config)
    if args.action == 'status':
        return runner_status(config)
    require_start('review-runner')
    if args.action == 'login':
        from .auth_owner import NativeAuthOwner
        return NativeAuthOwner(config.auth_home, config.codex_executable).login_device()
    if args.action == 'readiness':
        from .codex_runtime import RuntimeConfig, readiness
        result = readiness(RuntimeConfig(image=config.model_image, model=config.model,
            reasoning_effort=config.reasoning_effort, codex_version=config.codex_version))
        return {'runtime': result, 'schedule_enabled': config.schedule_enabled,
                'schedule_calendar': config.schedule_calendar,
                'auth_configured': (config.auth_home / 'auth.json').is_file()}
    if args.max_jobs is not None and args.max_jobs < 1:
        raise ContractError('max-jobs must be positive')
    if args.scheduled and not config.schedule_enabled:
        return {'status': 'disabled', 'schedule_enabled': False}
    # The maintenance gate is checked under the same lock drained by deployment,
    # before opening or migrating either database.
    with runner_lock(config.state_dir):
        require_start('review-runner')
        authority = build_authority(config)
        coordinator = ReviewCoordinator(config, authority, ProductionRuntime(config, authority))
        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: coordinator.stop.set())
        try:
            return coordinator.run(mode=args.mode, review_id=args.review_id, window_start=args.window_start,
                window_end=args.window_end, scheduled=args.scheduled, max_jobs=args.max_jobs, lock_held=True)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    try:
        result = command(parser.parse_args(argv))
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get('status') not in ('failed', 'incomplete', 'needs_review', 'interrupted') else 2
    except (ContractError, OSError, RuntimeError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
