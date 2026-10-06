"""Trusted, resumable coordinator. This module never makes suitability decisions."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
import json
import math
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

    def _pending(self, review_id, kind, excluded=frozenset(), *, free_slots=None):
        if kind == 'adjudicator':
            return [n for n in self.authority.pending_adjudications(review_id) if n not in excluded]
        found, after = [], 0
        target = self._batch_size(kind) * (self.config.concurrency if free_slots is None else free_slots)
        while len(found) < target:
            if kind == 'check':
                page = self.authority.pending_checks(review_id, after=after, limit=200)
                found.extend(ordinal for ordinal in page['ordinals'] if ordinal not in excluded)
                after = page.get('next_after')
                if after is None:
                    break
                continue
            if kind == 'benchmark_check':
                page = self.authority.service.call('batch', {
                    'review_id': review_id, 'after': after, 'limit': 20, 'filter': 'assessed'})
                found.extend(item['ordinal'] for item in page['items']
                             if item['check'] is None and item['ordinal'] not in excluded)
                after = page.get('next_after')
                if after is None:
                    break
                continue
            if self.config.screening_enabled:
                getter = self.authority.pending_routes if kind == 'screening' else self.authority.pending_detailed
                page = getter(review_id, after=after, limit=200)
                found.extend(ordinal for ordinal in page['ordinals'] if ordinal not in excluded)
                after = page.get('next_after')
                if after is None:
                    break
                continue
            page = self.authority.service.call('batch', {
                'review_id': review_id, 'after': after, 'limit': 20, 'filter': 'pending',
            })
            found.extend(item['ordinal'] for item in page['items'] if item['ordinal'] not in excluded)
            after = page.get('next_after')
            if after is None:
                break
        return found[:target] if free_slots is not None else found

    def _batch_size(self, kind):
        return self.config.screening_batch_size if kind == 'screening' else self.config.batch_size

    def _profile(self, kind):
        if kind == 'screening':
            return self.config.screening_model, self.config.screening_reasoning_effort
        if kind in ('check', 'benchmark_check', 'adjudicator'):
            profile = self.config.check_profile()
            return profile['model'], profile['reasoning_effort']
        return self.config.model, self.config.reasoning_effort

    def _assignment(self, review_id, ordinals, kind, *, preload_enabled=True):
        if self._stopping():
            return {'status': 'stopped', 'ordinals': ordinals, 'kind': kind}
        began = self.clock()
        runtime_id = 'worker_' + secrets.token_hex(16)
        model, effort = self._profile(kind)
        grant_kind = 'primary' if kind == 'screening' else 'check' if kind == 'benchmark_check' else kind
        grant = self.authority.issue(review_id, ordinals, grant_kind, {
            'runtime_id': runtime_id, 'model': model, 'reasoning_effort': effort,
        }, **({'purpose': 'screening'} if kind == 'screening' else {}))
        grant['preload_enabled'] = self.config.preload_enabled and preload_enabled
        issued = self.clock()
        worker = None
        try:
            if self._stopping():
                return {'status': 'stopped', 'ordinals': ordinals, 'kind': kind,
                        'grant_id': grant['grant_id'], 'runtime_id': runtime_id}
            # The token stays in trusted runtime composition, never a prompt or journal.
            with self.runtime.worker(grant, runtime_id=runtime_id) as worker:
                self._record(active=True)
                started = self.clock()

                def result(status, **details):
                    ended = self.clock()
                    return {'status': status, 'ordinals': ordinals, 'kind': kind,
                            'model': model, 'reasoning_effort': effort,
                            'grant_id': grant['grant_id'], 'runtime_id': runtime_id,
                            'timings': {'grant_seconds': round(max(0, issued - began), 3),
                                        'launch_seconds': round(max(0, started - issued), 3),
                                        'worker_seconds': round(max(0, ended - started), 3),
                                        'elapsed_seconds': round(max(0, ended - began), 3)},
                            'inference': dict(getattr(worker, 'telemetry', {})),
                            **details}

                next_renew = started + 60
                while worker.poll() is None:
                    now = self.clock()
                    if self._stopping():
                        worker.terminate()
                        return result('interrupted')
                    if now - started >= self.config.assignment_timeout_seconds:
                        worker.terminate()
                        return result('timed_out')
                    if now >= next_renew:
                        self.authority.renew(grant['grant_id'])
                        next_renew = now + 60
                    self.sleep(0.25)
                code = worker.wait()
                return result('exited', exit_code=code)
        finally:
            # Committed receipts survive revocation. A fresh assignment sees only pending items.
            self.authority.revoke(grant['grant_id'])

    def _run_stage(self, pool, review_id, kind, attempts):
        """Refill free slots without redispatching any in-flight ordinal.

        Timeouts retain durable progress and retry only remaining items. Global
        stop/deadline prevents dispatch and lets each worker terminate itself.
        """
        active, failure = {}, None
        try:
            while True:
                routed_without_launch = False
                if not self._stopping() and failure is None and len(active) < self.config.concurrency:
                    assigned = {ordinal for batch in active.values() for ordinal in batch}
                    pending = self._pending(review_id, kind, assigned,
                        **({'free_slots': self.config.concurrency - len(active)} if kind == 'check' else {}))
                    if any(attempts.get((kind, ordinal), 0) >= 1 + self.config.transient_retries
                           for ordinal in pending):
                        # Finish in-flight work, but don't spend more after the
                        # bounded retry budget has been exhausted.
                        failure = ('incomplete', 'assignment_attempts_exhausted')
                    else:
                        from .packing import pack_assignment
                        size = self._batch_size(kind)
                        while pending and len(active) < self.config.concurrency:
                            if self._stopping():
                                break
                            packed = pack_assignment(self.authority, review_id, pending[:size],
                                purpose='screening' if kind == 'screening' else 'detailed',
                                **({'kind': 'adjudicator'} if kind == 'adjudicator' else {}))
                            batch = packed['ordinals']
                            if not batch:
                                # The first oversized screening item has been
                                # durably routed to the full detailed workflow.
                                pending = pending[1:]
                                routed_without_launch = True
                                continue
                            for ordinal in batch:
                                attempts[(kind, ordinal)] = attempts.get((kind, ordinal), 0) + 1
                            active[pool.submit(self._assignment, review_id, batch, kind,
                                               preload_enabled=packed['preload_enabled'])] = batch
                            pending = pending[len(batch):]
                if not active:
                    if routed_without_launch and not self._stopping() and failure is None:
                        continue
                    break
                done, _ = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                results = []
                for future in done:
                    active.pop(future)
                    result = future.result()
                    results.append(result)
                    if result['status'] in ('stopped', 'interrupted'):
                        self.stop.set()
                    elif result['status'] != 'timed_out' and result.get('exit_code') not in (0, 75):
                        self.stop.set()
                        failure = ('failed', 'worker_failed')
                if results:
                    self._record_assignments(results)
        except BaseException:
            self.stop.set()
            raise
        if failure and (failure[0] == 'failed' or not self._stopping()):
            self._record(status=failure[0], active=False, reason=failure[1])
            return False
        if self._stopping():
            self._record(status='interrupted', active=False)
            return False
        return True

    def _record_assignments(self, results):
        # Detailed numeric receipts stay private; public status retains aggregates
        # and the most recent completions rather than growing with the review.
        path = self.config.state_dir / (self.journal['run_id'] + '.assignments.jsonl')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ContractError('assignment metrics must be a private owned regular file')
            with os.fdopen(fd, 'a') as stream:
                fd = -1
                stream.write(''.join(canonical_json(result) + '\n' for result in results))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        totals = dict(self.journal.get('assignment_totals', {}))
        phases = dict(self.journal.get('phase_totals', {}))
        for result in results:
            totals['timeouts'] = totals.get('timeouts', 0) + (result['status'] == 'timed_out')
            for name in ('timings', 'inference'):
                values = dict(totals.get(name, {}))
                for key, value in result.get(name, {}).items():
                    values[key] = values.get(key, 0) + value
                totals[name] = values
            kind = result.get('kind')
            if kind in ('screening', 'primary', 'check', 'benchmark_check', 'adjudicator', 'finalizer'):
                phase = dict(phases.get(kind, {}))
                phase['assignments'] = phase.get('assignments', 0) + 1
                phase['jobs_attempted'] = phase.get('jobs_attempted', 0) + len(result.get('ordinals', []))
                for name in ('timings', 'inference'):
                    values = dict(phase.get(name, {}))
                    for key, value in result.get(name, {}).items():
                        values[key] = values.get(key, 0) + value
                    phase[name] = values
                phases[kind] = phase
        self._record(assignments=self.journal['assignments'] + len(results),
                     last_assignments=results[-2:], assignment_totals=totals, phase_totals=phases)

    def run(self, *, mode='recurring', review_id=None, window_start=None, window_end=None,
            scheduled=False, max_jobs=None, lock_held=False):
        if self.config.concurrency > 16 and (not review_id or scheduled):
            raise ContractError('concurrency above 16 requires an explicit existing nonscheduled benchmark review')
        if self.config.benchmark_check_all and (not review_id or scheduled):
            raise ContractError('benchmark check-all requires an explicit existing nonscheduled benchmark review')
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
                            'configuration': {name: getattr(self.config, name) for name in (
                                'batch_size', 'concurrency', 'preload_enabled',
                                'screening_enabled', 'screening_batch_size',
                                'screening_model', 'screening_reasoning_effort',
                                'benchmark_check_all',
                                'check_model', 'check_reasoning_effort',
                                'assignment_timeout_seconds', 'invocation_timeout_seconds')},
                            'schedule_enabled': self.config.schedule_enabled}
            self._record()
            try:
                current = self._review(mode, review_id, window_start, window_end)
                rid = current['review_id']
                if self.config.concurrency > 16 and not current['metadata'].get('benchmark'):
                    raise ContractError('concurrency above 16 is restricted to benchmark reviews')
                if self.config.benchmark_check_all and not current['metadata'].get('benchmark'):
                    raise ContractError('check-all is restricted to benchmark reviews')
                if current['status'] != 'published':
                    policy = self.authority.freeze_execution_policy(rid, self.config.execution_policy())
                    self._record(execution_policy_sha256=policy['policy_sha256'])
                recovery = self.authority.reconcile_interrupted()
                self.runtime.recover(recovery['workers'])
                self._record(recovered_grants=recovery['revoked_grants'],
                             recovered_claims=recovery['released_claims'])
                self._record(review_id=rid, total=current['total'], status='running')
                if max_jobs is not None and current['total'] > max_jobs:
                    raise ContractError('review window exceeds the trial limit; select a narrower window without dropping jobs')
                if current['status'] == 'published':
                    self._record(status='published', active=False, receipt=current['receipt'])
                    return self.journal
                attempts = {}
                with self.runtime, ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
                    phases = ('screening', 'primary', 'check') if self.config.screening_enabled else ('primary', 'check')
                    if self.config.benchmark_check_all:
                        phases += ('benchmark_check',)
                    phases += ('adjudicator',)
                    for kind in phases:
                        model, effort = self._profile(kind)
                        self._record(phase=kind, phase_model=model, phase_reasoning_effort=effort)
                        if not self._run_stage(pool, rid, kind, attempts):
                            return self.journal
                    status = self.authority.status(rid)
                    if status.get('calibration') is not None:
                        if status['counts'].get('pending') or status['audit_remaining_count'] or status.get('unresolved_disagreement_count', status['disagreement_count']):
                            adjudication = status.get('adjudication', {})
                            reason = ('adjudication_attempt_incomplete' if adjudication.get('incomplete', {}).get('count') else
                                      'adjudication_unresolved' if adjudication.get('unresolved', {}).get('count') else
                                      'independent_review_incomplete')
                            self._record(status='needs_review', active=False, reason=reason)
                            return self.journal
                        if not status['calibration']['complete']:
                            self._record(phase='finalizer', phase_model=self.config.model,
                                         phase_reasoning_effort=self.config.reasoning_effort)
                            for attempt in range(1 + self.config.transient_retries):
                                result = self._assignment(rid, [], 'finalizer')
                                self._record_assignments([result])
                                if self.authority.status(rid)['calibration']['complete'] or self._stopping():
                                    break
                                if result['status'] != 'timed_out' and result.get('exit_code') not in (0, 75):
                                    break
                            if not self.authority.status(rid)['calibration']['complete']:
                                self._record(status='interrupted' if self._stopping() else 'needs_review', active=False,
                                             reason='calibration_incomplete')
                                return self.journal
                        if not self._stopping():
                            state = self.authority.status(rid)['calibration']
                            self.authority.verify_availability(rid, rid + '-availability-' + state['basis_sha256'])
                if self._stopping():
                    self._record(status='interrupted', active=False)
                    return self.journal
                preview = self.authority.preview(rid)
                if self._stopping():
                    self._record(status='interrupted', active=False)
                    return self.journal
                if current['metadata'].get('benchmark') and not [
                        blocker for blocker in preview['blockers'] if blocker != 'benchmark_not_publishable']:
                    self._record(status='benchmark_complete', active=False,
                                 preview_sha256=preview['preview_sha256'])
                    return self.journal
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

    def _runtime_config(self, rubric_version='job-review-v1', *, purpose='detailed', kind='primary', preload_enabled=None):
        from .codex_runtime import RuntimeConfig
        if purpose not in ('detailed', 'screening'):
            raise ContractError('unsupported trusted worker purpose')
        if purpose == 'screening' and not self.config.screening_enabled:
            raise ContractError('screening is disabled in trusted runtime configuration')
        if kind not in ('primary', 'check', 'finalizer', 'adjudicator') or purpose == 'screening' and kind != 'primary':
            raise ContractError('unsupported trusted worker kind and purpose')
        screening = purpose == 'screening'
        profile = self.config.check_profile() if kind in ('check', 'adjudicator') else {
            'model': self.config.model, 'reasoning_effort': self.config.reasoning_effort}
        return RuntimeConfig(image=self.config.model_image,
            model=self.config.screening_model if screening else profile['model'],
            reasoning_effort=self.config.screening_reasoning_effort if screening else profile['reasoning_effort'],
            codex_version=self.config.codex_version, timeout_seconds=self.config.assignment_timeout_seconds,
            worker_uid=self.config.worker_uid, worker_gid=self.config.worker_gid,
            preload_enabled=self.config.preload_enabled if preload_enabled is None else preload_enabled,
            screening_enabled=self.config.screening_enabled, purpose=purpose,
            adjudication_enabled=rubric_version == 'job-review-v2',
            review_contract_version=2 if rubric_version == 'job-review-v2' else 1)

    def recover(self, workers):
        from .codex_runtime import recover_workers
        return recover_workers(self._runtime_config(), workers)

    def __enter__(self):
        from .auth_owner import NativeAuthOwner
        self.stack = ExitStack()
        root = _private_dir(self.config.runtime_dir)
        self.run_root = _private_dir(root / ('run_' + secrets.token_hex(6)))
        self.auth = NativeAuthOwner(self.config.auth_home, self.config.codex_executable)
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    @contextmanager
    def worker(self, grant, *, runtime_id):
        from .codex_runtime import launch_worker
        from .reviewer_api import assignment_proxy
        from .model_gateway import gateway_server, RESPONSE_REJECTION_COUNTERS
        worker_root = _private_dir(self.run_root / ('w_' + secrets.token_hex(6)))
        socket = worker_root / 'review.sock'
        ready = threading.Event()
        model_socket = worker_root / 'model.sock'
        telemetry, telemetry_lock = {}, threading.Lock()
        purpose = grant.get('purpose', 'detailed')
        runtime_config = self._runtime_config(grant.get('rubric_version', 'job-review-v1'),
            purpose=purpose, kind=grant.get('kind', 'primary'),
            preload_enabled=grant.get('preload_enabled', self.config.preload_enabled))

        def record_inference(event):
            fields = ('request_started_count', 'request_count', 'upstream_duration_ms',
                      'gateway_request_rejected_count', 'gateway_response_rejected_count',
                      'upstream_transport_failed_count', 'authentication_failed_count', 'gateway_busy_count',
                      'input_tokens', 'output_tokens',
                      'total_tokens', 'cached_input_tokens', 'reasoning_tokens') + RESPONSE_REJECTION_COUNTERS
            with telemetry_lock:
                for name in fields:
                    value = event.get(name)
                    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                        telemetry[name] = telemetry.get(name, 0) + value
                status = event.get('upstream_status')
                if type(status) is int and 100 <= status <= 599:
                    if status // 100 in (2, 4, 5):
                        name = 'upstream_http_' + str(status // 100) + 'xx'
                        telemetry[name] = telemetry.get(name, 0) + 1
                    if status == 429:
                        name = 'upstream_rate_limited_count'
                        telemetry[name] = telemetry.get(name, 0) + 1

        def record_api(event):
            operation = event.get('operation')
            if operation not in ('assignment', 'context', 'job', 'assessment', 'assessments',
                                 'calibration', 'calibrate', 'calibrations', 'order', 'finalize', 'route', 'routes', 'disagreement', 'resolutions'):
                return
            prefix = 'api_' + operation
            with telemetry_lock:
                telemetry[prefix + '_calls'] = telemetry.get(prefix + '_calls', 0) + 1
                seconds = event.get('elapsed_seconds')
                if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0:
                    telemetry[prefix + '_seconds'] = telemetry.get(prefix + '_seconds', 0) + seconds
                size = event.get('response_bytes')
                if type(size) is int and size >= 0:
                    telemetry[prefix + '_response_bytes'] = telemetry.get(prefix + '_response_bytes', 0) + size

        with gateway_server(model_socket, self.auth, model=runtime_config.model, reasoning_effort=runtime_config.reasoning_effort,
                uid=self.config.worker_uid, gid=self.config.worker_gid, kind=grant.get('kind', 'primary'),
                rubric_version=grant.get('rubric_version', 'job-review-v1'), purpose=purpose,
                telemetry_callback=record_inference), \
                assignment_proxy(socket, self.authority, grant['token'], ready=ready,
                                 telemetry_callback=record_api):
            os.chown(socket, self.config.worker_uid, self.config.worker_gid)
            assignment = {k: grant[k] for k in ('grant_id', 'actor', 'expires_at')}
            assignment['runtime_id'] = runtime_id
            worker = launch_worker(runtime_config, assignment, worker_root,
                                  model_socket=model_socket, review_socket=socket)
            worker.telemetry = telemetry
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
    from ..scanning import scan_status
    from .authority import ReviewAuthority
    from .context import stored_profile_context
    from .service import JobReviews
    application = load_runtime_config(config.application_config, required=True)
    return ReviewAuthority(JobReviews(application.application_db, LocalJobCatalog(application.jobs_db),
        lambda: stored_profile_context(application.resume_lab_db), collection_provider=lambda: scan_status(application)),
        approved_model=config.model, approved_reasoning_effort=config.reasoning_effort,
        approved_check_model=config.check_profile()['model'],
        approved_check_reasoning_effort=config.check_profile()['reasoning_effort'],
        approved_screening_model=config.screening_model,
        approved_screening_reasoning_effort=config.screening_reasoning_effort)


BENCHMARK_OVERRIDES = {
    'benchmark_concurrency': 'concurrency',
    'benchmark_batch_size': 'batch_size',
    'benchmark_model': 'model',
    'benchmark_reasoning_effort': 'reasoning_effort',
    'benchmark_check_model': 'check_model',
    'benchmark_check_reasoning_effort': 'check_reasoning_effort',
    'benchmark_screening_model': 'screening_model',
    'benchmark_screening_reasoning_effort': 'screening_reasoning_effort',
    'benchmark_screening_batch_size': 'screening_batch_size',
    'benchmark_check_all': 'benchmark_check_all',
}


def add_benchmark_arguments(parser):
    parser.add_argument('--benchmark-concurrency', type=int, choices=(2, 4, 8, 16, 32))
    parser.add_argument('--benchmark-batch-size', type=int, choices=range(1, 21))
    parser.add_argument('--benchmark-model')
    parser.add_argument('--benchmark-reasoning-effort', choices=('low', 'medium', 'high'))
    parser.add_argument('--benchmark-check-model')
    parser.add_argument('--benchmark-check-reasoning-effort', choices=('low', 'medium', 'high'))
    parser.add_argument('--benchmark-screening-model')
    parser.add_argument('--benchmark-screening-reasoning-effort', choices=('low', 'medium', 'high'))
    parser.add_argument('--benchmark-screening-batch-size', type=int, choices=range(1, 201))
    parser.add_argument('--benchmark-check-all', action='store_true', default=None)


def benchmark_overrides(args):
    return {field: getattr(args, flag) for flag, field in BENCHMARK_OVERRIDES.items()
            if getattr(args, flag, None) is not None}


def add_arguments(parser):
    parser.add_argument('action', choices=('login', 'readiness', 'run', 'status'))
    parser.add_argument('--runner-config', type=Path, required=True)
    parser.add_argument('--mode', choices=('recurring', 'custom'), default='recurring')
    parser.add_argument('--review-id')
    parser.add_argument('--window-start')
    parser.add_argument('--window-end')
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--max-jobs', type=int)
    add_benchmark_arguments(parser)


def command(args):
    config = load_runner_config(args.runner_config)
    overrides = benchmark_overrides(args)
    if (overrides or config.benchmark_check_all or config.concurrency > 16) and (
            args.action != 'run' or not args.review_id or args.scheduled):
        raise ContractError('benchmark overrides require an explicit existing nonscheduled benchmark review')
    if 'concurrency' in overrides and (type(overrides['concurrency']) is not int or overrides['concurrency'] not in (2, 4, 8, 16, 32)):
        raise ContractError('benchmark concurrency must be 2, 4, 8, 16 or 32')
    if 'reasoning_effort' in overrides and overrides['reasoning_effort'] not in ('low', 'medium', 'high'):
        raise ContractError('benchmark reasoning effort must be low, medium or high')
    if args.action == 'status':
        return runner_status(config)
    require_start('review-runner')
    if args.action == 'login':
        from .auth_owner import NativeAuthOwner
        return NativeAuthOwner(config.auth_home, config.codex_executable).login_device()
    if args.action == 'readiness':
        from .codex_runtime import RuntimeConfig, readiness
        result = readiness(RuntimeConfig(image=config.model_image, model=config.model,
            reasoning_effort=config.reasoning_effort, codex_version=config.codex_version,
            screening_enabled=config.screening_enabled, adjudication_enabled=True))
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
        if overrides or config.benchmark_check_all or config.concurrency > 16:
            if not authority.status(args.review_id)['metadata'].get('benchmark'):
                raise ContractError('runtime override is restricted to benchmark reviews')
            if any(name.startswith('screening_') for name in overrides):
                overrides['screening_enabled'] = True
            config = replace(config, **overrides).validate()
            # The scoped authority and worker must agree on the explicitly
            # selected benchmark model/effort. Production authority is unchanged.
            from .authority import ReviewAuthority
            authority = ReviewAuthority(authority.service, approved_model=config.model,
                approved_reasoning_effort=config.reasoning_effort,
                approved_check_model=config.check_profile()['model'],
                approved_check_reasoning_effort=config.check_profile()['reasoning_effort'],
                approved_screening_model=config.screening_model,
                approved_screening_reasoning_effort=config.screening_reasoning_effort)
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
