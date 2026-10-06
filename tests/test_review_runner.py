"""Coordinator recovery tests: no model, credentials, Docker, or network."""
import copy
import argparse
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.runner import ReviewCoordinator, runner_lock, runner_status
from job_search.job_reviews.runner_config import RunnerConfig, load_runner_config
from tests import test_agent_job_reviews as review_fixtures


class FinishedWorker:
    def __init__(self, code=0):
        self.code = code
    def poll(self):
        return self.code
    def wait(self):
        return self.code
    def terminate(self):
        self.code = -15


class FakeRuntime:
    def __init__(self, authority, assessment, *, partial=False, empty=False, disagreement=False, resolution='unresolved'):
        self.authority, self.assessment = authority, assessment
        self.partial, self.empty, self.disagreement = partial, empty, disagreement
        self.resolution = resolution
        self.calls = []
        self.recovered = []
    def recover(self, workers):
        self.recovered = workers

    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    @contextmanager
    def worker(self, grant, *, runtime_id):
        self.authority.mark_launch(grant['grant_id'], runtime_id, {
            'container_id': hashlib.sha256(runtime_id.encode()).hexdigest(),
            'image_digest': 'sha256:' + '1' * 64, 'config_sha256': '2' * 64,
        })
        api = lambda op, args=None: self.authority.scoped_call(grant['token'], op, args)
        assignment = api('assignment')
        phase = 'screening' if assignment.get('purpose') == 'screening' else assignment['kind']
        self.calls.append((phase, [j['ordinal'] for j in assignment['jobs']]))
        if assignment['kind'] == 'adjudicator':
            from job_search.job_reviews.codex_runtime import preload_evidence
            class Client:
                def call(_self, operation, args): return api(operation, args)
            packet = preload_evidence(Client())
            if not self.empty and self.resolution is not None:
                for job in packet['jobs']:
                    dispute = job['disagreement']
                    api('resolutions', {'resolutions': [{'ordinal':job['ordinal'],
                        'basis_sha256':dispute['basis_sha256'], 'choice':self.resolution,
                        'checked_dimensions':dispute['differing_dimensions'],
                        'explanation':'Scope and seniority differ despite Python API relevance.',
                        'evidence':[{'field':'description','quote':'Python APIs','fact_id':'fact-1'}]}]})
            yield FinishedWorker()
            return
        if assignment['kind'] == 'finalizer':
            if not self.empty:
                after, items = 0, []
                while True:
                    page = api('calibration', {'after': after, 'limit': 1})
                    items.extend(page['items'])
                    after = page['next_after']
                    if after is None:
                        break
                for position, item in enumerate(items, 1):
                    api('calibrate', {'ordinal': item['ordinal'], 'position': position})
                api('finalize')
            yield FinishedWorker()
            return
        jobs = assignment['jobs'][:1] if self.partial else assignment['jobs']
        if not self.empty:
            for job in jobs:
                offset = 0
                while True:
                    page = api('job', {'ordinal': job['ordinal'], 'offset': offset})
                    offset = page['next_offset']
                    if offset is None:
                        break
                if phase == 'screening':
                    api('routes', {'routes': [{'ordinal': job['ordinal'], 'route': 'detailed'}]})
                    continue
                value = copy.deepcopy(self.assessment)
                if assignment['rubric_version'] == 'job-review-v2':
                    value.update(eligibility='no_known_barrier', eligibility_condition='', next_step='apply', category='core')
                if assignment['kind'] == 'check' and self.disagreement:
                    value.update(decision='broad_only')
                api('assessment', {'ordinal': job['ordinal'], 'assessment': value})
        yield FinishedWorker(75 if self.partial else 0)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        from job_search.job_reviews.authority import ReviewAuthority
        self.fixture = review_fixtures.ReviewTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.service.availability_checker = lambda jobs: [dict(ats=j['ats'], job_id=j['id'], status='unknown', checked_at=self.fixture.now, source='', reason='offline fixture') for j in jobs]
        self.authority = ReviewAuthority(self.fixture.service)
        self.config = RunnerConfig(application_config=self.root / 'app.json',
            state_dir=self.root / 'runner', runtime_dir=self.root / 'runtime',
            auth_home=self.root / 'auth', model_image='example/reviewer@sha256:' + '1' * 64)

    def coordinator(self, **kwargs):
        runtime = FakeRuntime(self.authority, self.fixture.assessment(), **kwargs)
        runner = ReviewCoordinator(self.config, self.authority, runtime, is_draining=lambda: False)
        return runner, runtime

    def run_custom(self, runner, **kwargs):
        return runner.run(mode='custom', window_start='2026-10-01T00:00:00Z', **kwargs)

    def test_primary_and_independent_check_publish_without_scores(self):
        runner, runtime = self.coordinator()
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        self.assertEqual([c[0] for c in runtime.calls], ['primary', 'check', 'finalizer'])
        self.assertEqual(len(result['receipt']['lists']), 2)
        self.assertFalse(runner_status(self.config)['schedule_enabled'])
        self.assertEqual(result['configuration'], {
            'batch_size': 20, 'concurrency': 2, 'preload_enabled': True,
            'screening_enabled': False, 'screening_batch_size': 200,
            'screening_model': None, 'screening_reasoning_effort': None,
            'benchmark_check_all': False,
            'check_model': None, 'check_reasoning_effort': None,
            'assignment_timeout_seconds': 1200, 'invocation_timeout_seconds': 3600})
        serialized = (self.config.state_dir / 'status.json').read_text()
        self.assertNotIn('"token":', serialized)
        self.assertNotIn('Python APIs', serialized)

    def test_schedule_remains_disabled_without_creating_state(self):
        runner, runtime = self.coordinator()
        self.assertEqual(runner.run(scheduled=True)['status'], 'disabled')
        self.assertFalse(self.config.state_dir.exists())
        self.assertEqual(runtime.calls, [])

    def test_maintenance_prevents_dispatch(self):
        runner, runtime = self.coordinator()
        runner.is_draining = lambda: True
        self.assertEqual(runner.run()['status'], 'maintenance')
        self.assertEqual(runtime.calls, [])

    def test_disagreements_preserve_both_judgments_without_publication(self):
        runner, _ = self.coordinator(disagreement=True)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'needs_review')
        status = self.authority.status(result['review_id'])
        self.assertEqual(status['disagreement_count'], 1)
        self.assertEqual(result['reason'], 'adjudication_unresolved')
        self.assertIsNone(status['receipt'])

    def test_consumed_adjudication_without_result_stays_blocked_on_resume(self):
        runner, runtime = self.coordinator(disagreement=True, resolution=None)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['reason'], 'adjudication_attempt_incomplete')
        self.assertEqual([kind for kind, _ in runtime.calls], ['primary', 'check', 'adjudicator'])
        replacement, new_runtime = self.coordinator(resolution='check')
        resumed = replacement.run(review_id=result['review_id'])
        self.assertEqual(resumed['status'], 'needs_review')
        self.assertEqual(resumed['reason'], 'adjudication_attempt_incomplete')
        self.assertEqual(new_runtime.calls, [])
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_adjudication_resolves_once_and_publishes_effective_assessment(self):
        runner, runtime = self.coordinator(disagreement=True, resolution='check')
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        status = self.authority.status(result['review_id'])
        self.assertEqual(status['disagreement_count'], 1)
        self.assertEqual(status['unresolved_disagreement_count'], 0)
        self.assertEqual([kind for kind, _ in runtime.calls], ['primary','check','adjudicator','finalizer'])
        preview = self.authority.preview(result['review_id'])
        self.assertEqual(preview['targeted_count'], 0)
        self.assertEqual(preview['broad_count'], 1)

    def test_partial_worker_progress_is_not_repeated(self):
        self.fixture.insert('b')
        runner, runtime = self.coordinator(partial=True)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        primary = [jobs for kind, jobs in runtime.calls if kind == 'primary']
        self.assertEqual(primary, [[1, 2], [2]])

    def test_free_slot_refills_while_other_assignment_is_still_running(self):
        self.fixture.insert('b')
        self.fixture.insert('c')
        self.config = replace(self.config, batch_size=1)
        runner, runtime = self.coordinator()
        original = runner._assignment
        third_started = {kind: threading.Event() for kind in ('primary', 'check')}

        def delayed_assignment(rid, ordinals, kind, **kwargs):
            if kind in third_started:
                if ordinals == [1]:
                    self.assertTrue(third_started[kind].wait(5), 'free slot waited for the straggler')
                elif ordinals == [3]:
                    third_started[kind].set()
            return original(rid, ordinals, kind, **kwargs)

        with patch.object(runner, '_assignment', side_effect=delayed_assignment):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        for kind in third_started:
            self.assertCountEqual([jobs for stage, jobs in runtime.calls if stage == kind], [[1], [2], [3]])
        self.assertEqual(result['assignments'], 7)

    def test_required_check_queue_fills_eight_workers_beyond_status_limit(self):
        for ordinal in range(2, 181):
            self.fixture.insert('check-capacity-' + str(ordinal))
        self.config = replace(self.config, concurrency=8)
        runner, runtime = self.coordinator()
        original = runner._assignment
        lock, all_slots_started = threading.Lock(), threading.Event()
        first_wave = []

        def assignment(rid, ordinals, kind, **kwargs):
            if kind == 'check':
                with lock:
                    if len(first_wave) < 8:
                        first_wave.append(list(ordinals))
                    if len(first_wave) == 8:
                        all_slots_started.set()
                self.assertTrue(all_slots_started.wait(5), 'required check queue left available workers idle')
            return original(rid, ordinals, kind, **kwargs)

        with patch.object(runner, '_assignment', side_effect=assignment):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(len(first_wave), 8)
        self.assertEqual(len({n for batch in first_wave for n in batch}), 160)
        checked = [n for kind, batch in runtime.calls if kind == 'check' for n in batch]
        self.assertCountEqual(checked, range(1, 181))
        self.assertEqual(self.authority.status(result['review_id'])['audit_required_count'], 180)

    def test_check_pagination_and_active_exclusions_preserve_complete_audit_sample(self):
        from tests.test_review_authority import AuthorityTests
        scope = AuthorityTests('runTest')
        scope.setUp()
        self.addCleanup(scope.doCleanups)
        for ordinal in range(2, 261):
            scope.fixture.insert('audit-page-' + str(ordinal))
        rid = scope.start(rubric_version='job-review-v2')
        selected = scope.fixture.assessment(eligibility='no_known_barrier', eligibility_condition='',
                                            next_step='apply', category='core')
        excluded = scope.fixture.assessment('exclude', reason_code='qualification',
            eligibility='no_known_barrier', eligibility_condition='', next_step='explore', category='core')
        for first in range(1, 261, 20):
            ordinals = list(range(first, first + 20))
            grant = scope.issue(rid, ordinals)
            for ordinal in ordinals:
                scope.read(grant, ordinal)
            scope.authority.scoped_call(grant['token'], 'assessments', {'assessments': [
                {'ordinal': ordinal, 'assessment': selected if ordinal <= 200 else excluded}
                for ordinal in ordinals]})
            scope.authority.revoke(grant['grant_id'])
        status = scope.authority.status(rid)
        self.assertEqual(status['audit_required_count'], 250)
        self.assertEqual(len(status['audit_remaining']), 100)
        page = scope.authority.pending_checks(rid, limit=200)
        self.assertEqual(page, {'ordinals': list(range(1, 201)), 'next_after': 200})
        tail = scope.authority.pending_checks(rid, after=page['next_after'], limit=200)
        self.assertEqual(len(tail['ordinals']), 50)
        self.assertIsNone(tail['next_after'])
        required = page['ordinals'] + tail['ordinals']
        completed = tail['ordinals'][:5]
        grant = scope.issue(rid, completed, 'check')
        for ordinal in completed:
            scope.assess(grant, ordinal, excluded)
        scope.authority.revoke(grant['grant_id'])
        config = replace(self.config, concurrency=8)
        runner = ReviewCoordinator(config, scope.authority, None, is_draining=lambda: False)
        active = set(range(1, 101))
        pending = runner._pending(rid, 'check', active)
        self.assertEqual(pending, [n for n in required if n not in active and n not in completed])
        self.assertEqual(scope.authority.status(rid)['audit_required_count'], 250)
        self.assertEqual(scope.authority.status(rid)['audit_remaining_count'], 245)
        for args in ({'after': -1}, {'limit': 201}, {'limit': True}):
            with self.subTest(args=args), self.assertRaises(ContractError):
                scope.authority.pending_checks(rid, **args)
        with self.assertRaises(ContractError):
            scope.authority.scoped_call(grant['token'], 'pending_checks', {})

    def test_assignment_timeout_retries_only_uncommitted_jobs_and_revokes_old_grant(self):
        self.fixture.insert('b')
        self.config = replace(self.config, concurrency=1, assignment_timeout_seconds=30)
        runner, runtime = self.coordinator(partial=True)
        original = runtime.worker
        clock, issued, hung = [0], [], []
        runner.clock = lambda: clock[0]
        runner.sleep = lambda _seconds: clock.__setitem__(0, clock[0] + 10)

        @contextmanager
        def timeout_once(grant, **kwargs):
            issued.append(grant)
            with original(grant, **kwargs) as worker:
                if len(issued) == 1:
                    worker.code = None
                    hung.append(worker)
                yield worker

        with patch.object(runtime, 'worker', side_effect=timeout_once):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published')
        self.assertEqual([jobs for kind, jobs in runtime.calls if kind == 'primary'], [[1, 2], [2]])
        self.assertEqual(hung[0].code, -15)
        self.assertFalse(runner.stop.is_set())
        metrics_path = self.config.state_dir / (result['run_id'] + '.assignments.jsonl')
        receipts = [json.loads(line) for line in metrics_path.read_text().splitlines()]
        self.assertEqual(receipts[0]['status'], 'timed_out')
        self.assertEqual(receipts[0]['timings']['worker_seconds'], 30)
        self.assertEqual(result['assignment_totals']['timeouts'], 1)
        self.assertEqual(metrics_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('assignment_history', result)
        for grant in issued:
            with self.assertRaises(ContractError):
                self.authority.scoped_call(grant['token'], 'assignment')
        timings = result['last_assignments'][0]['timings']
        self.assertEqual(set(timings), {'grant_seconds', 'launch_seconds', 'worker_seconds', 'elapsed_seconds'})
        self.assertTrue(all(value >= 0 for value in timings.values()))

    def test_stop_terminates_inflight_worker_without_dispatching_pending_remainder(self):
        self.fixture.insert('b')
        self.config = replace(self.config, concurrency=1, batch_size=1)
        runner, runtime = self.coordinator()
        original = runtime.worker
        workers = []

        @contextmanager
        def stopped_worker(grant, **kwargs):
            with original(grant, **kwargs) as worker:
                worker.code = None
                workers.append(worker)
                runner.stop.set()
                yield worker

        with patch.object(runtime, 'worker', side_effect=stopped_worker):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(runtime.calls, [('primary', [1])])
        self.assertEqual(workers[0].code, -15)
        self.assertEqual(self.authority.status(result['review_id'])['counts']['pending'], 1)
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_timeouts_exhaust_bounded_attempts_without_publication(self):
        self.config = replace(self.config, concurrency=1, assignment_timeout_seconds=30)
        runner, runtime = self.coordinator(empty=True)
        original = runtime.worker
        clock = [0]
        runner.clock = lambda: clock[0]
        runner.sleep = lambda _seconds: clock.__setitem__(0, clock[0] + 10)

        @contextmanager
        def always_timeout(grant, **kwargs):
            with original(grant, **kwargs) as worker:
                worker.code = None
                yield worker

        with patch.object(runtime, 'worker', side_effect=always_timeout):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'assignment_attempts_exhausted')
        self.assertEqual(len(runtime.calls), 3)
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_stop_during_grant_issue_prevents_worker_launch_and_revokes_grant(self):
        runner, runtime = self.coordinator()
        issue, grants = self.authority.issue, []

        def stop_on_issue(*args, **kwargs):
            grant = issue(*args, **kwargs)
            grants.append(grant)
            runner.stop.set()
            return grant

        with patch.object(self.authority, 'issue', side_effect=stop_on_issue):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(runtime.calls, [])
        for grant in grants:
            with self.assertRaises(ContractError):
                self.authority.scoped_call(grant['token'], 'assignment')

    def test_global_deadline_stops_instead_of_retrying_assignment(self):
        self.fixture.insert('b')
        self.config = replace(self.config, concurrency=1, assignment_timeout_seconds=30,
                              invocation_timeout_seconds=10)
        runner, runtime = self.coordinator(partial=True)
        original = runtime.worker
        clock = [0]
        runner.clock = lambda: clock[0]
        runner.sleep = lambda _seconds: clock.__setitem__(0, clock[0] + 10)

        @contextmanager
        def hanging_worker(grant, **kwargs):
            with original(grant, **kwargs) as worker:
                worker.code = None
                yield worker

        with patch.object(runtime, 'worker', side_effect=hanging_worker):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(runtime.calls, [('primary', [1, 2])])
        self.assertEqual(self.authority.status(result['review_id'])['counts']['pending'], 1)

    def test_incomplete_run_resumes_ledger_with_fresh_workers(self):
        runner, runtime = self.coordinator(empty=True)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(len(runtime.calls), 3)
        replacement, new_runtime = self.coordinator()
        final = replacement.run(review_id=result['review_id'])
        self.assertEqual(final['status'], 'published')
        self.assertEqual(final['review_id'], result['review_id'])
        self.assertEqual(len(new_runtime.calls), 3)

    def test_fatal_worker_exit_stops_without_retry(self):
        runner, runtime = self.coordinator(empty=True)
        with patch.object(FinishedWorker, 'wait', return_value=1):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'worker_failed')
        self.assertEqual(len(runtime.calls), 1)
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_stop_after_preview_prevents_publication(self):
        runner, runtime = self.coordinator()
        preview = self.authority.preview
        def stopped_preview(rid):
            result = preview(rid)
            runner.stop.set()
            return result
        with patch.object(self.authority, 'preview', side_effect=stopped_preview):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'interrupted')
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_benchmark_completion_requires_all_other_gates_and_never_publishes(self):
        start, preview = self.authority.start, self.authority.preview

        def benchmark_start(args):
            result = start(args)
            result['metadata']['benchmark'] = {'source': 'synthetic-test'}
            return result

        for extra_blockers in ([], ['evidence_changed']):
            runner, _ = self.coordinator()

            def benchmark_preview(rid):
                result = preview(rid)
                return dict(result, ready=False,
                            blockers=result['blockers'] + ['benchmark_not_publishable'] + extra_blockers)

            with self.subTest(extra_blockers=extra_blockers), \
                 patch.object(self.authority, 'start', side_effect=benchmark_start), \
                 patch.object(self.authority, 'preview', side_effect=benchmark_preview), \
                 patch.object(self.authority, 'publish', wraps=self.authority.publish) as publish:
                result = self.run_custom(runner)
            self.assertEqual(result['status'], 'needs_review' if extra_blockers else 'benchmark_complete')
            publish.assert_not_called()
            self.assertIsNone(self.authority.status(result['review_id'])['receipt'])

    def test_control_runtime_can_disable_preload_without_changing_model_or_scope(self):
        from job_search.job_reviews.runner import ProductionRuntime
        config = replace(self.config, preload_enabled=False, batch_size=5, assignment_timeout_seconds=600)
        runtime = ProductionRuntime(config, self.authority)._runtime_config('job-review-v2')
        self.assertFalse(runtime.preload_enabled)
        self.assertEqual((runtime.model, runtime.reasoning_effort), ('gpt-6-astra', 'high'))
        self.assertEqual(runtime.review_contract_version, 2)

    def test_crash_reconciliation_happens_before_replacement_dispatch(self):
        current = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                        'idempotency_key': 'crashed-run'})
        grant = self.authority.issue(current['review_id'], [1], 'primary', {
            'runtime_id': 'worker_' + '3' * 32, 'model': self.config.model,
            'reasoning_effort': self.config.reasoning_effort})
        runner, runtime = self.coordinator()
        result = runner.run(review_id=current['review_id'])
        self.assertEqual(result['status'], 'published')
        self.assertEqual(runtime.recovered[0]['grant_id'], grant['grant_id'])
        self.assertEqual(result['recovered_grants'], 1)
        with self.assertRaises(ContractError):
            self.authority.scoped_call(grant['token'], 'assignment')

    def test_trial_limit_never_silently_truncates_review(self):
        self.fixture.insert('b')
        runner, runtime = self.coordinator()
        with self.assertRaisesRegex(ContractError, 'trial limit'):
            self.run_custom(runner, max_jobs=1)
        result = runner_status(self.config)['last_run']
        self.assertEqual(self.authority.status(result['review_id'])['total'], 2)
        self.assertEqual(runtime.calls, [])

    def test_old_unisolated_review_cannot_be_adopted(self):
        rid = self.fixture.start()
        runner, runtime = self.coordinator()
        with self.assertRaisesRegex(ContractError, 'not managed|not isolated'):
            runner.run(review_id=rid)
        self.assertEqual(runtime.calls, [])

    def test_lock_excludes_overlapping_manual_or_timer_runs(self):
        with runner_lock(self.config.state_dir):
            with self.assertRaisesRegex(ContractError, 'already running'):
                with runner_lock(self.config.state_dir):
                    pass

    def test_config_requires_pinned_image_and_explicit_schedule(self):
        with self.assertRaisesRegex(ContractError, 'sha256'):
            replace(self.config, model_image='reviewer:latest').validate()
        with self.assertRaisesRegex(ContractError, 'calendar'):
            replace(self.config, schedule_enabled=True).validate()
        with self.assertRaisesRegex(ContractError, 'preload_enabled'):
            replace(self.config, preload_enabled='false').validate()
        with self.assertRaisesRegex(ContractError, 'concurrency'):
            replace(self.config, concurrency=33).validate()
        value = dict(self.config.__dict__)
        for key, item in value.items():
            if isinstance(item, Path):
                value[key] = str(item)
        path = self.root / 'runner.json'
        path.write_text(json.dumps(value))
        self.assertEqual(load_runner_config(path), self.config)
        value['token'] = 'not-allowed'
        path.write_text(json.dumps(value))
        with self.assertRaises(ContractError):
            load_runner_config(path)

    def test_concurrency_cli_override_requires_an_existing_benchmark(self):
        from job_search.job_reviews.runner import add_arguments, command
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        base = ['run', '--runner-config', str(self.root / 'runner.json'), '--benchmark-concurrency', '4']
        with patch('job_search.job_reviews.runner.load_runner_config', return_value=self.config), \
             patch('job_search.job_reviews.runner.require_start'), \
             patch('job_search.job_reviews.runner.build_authority', return_value=self.authority), \
             patch('job_search.job_reviews.runner.ProductionRuntime') as runtime, \
             patch('job_search.job_reviews.runner.ReviewCoordinator') as coordinator:
            with self.assertRaisesRegex(ContractError, 'explicit existing'):
                command(parser.parse_args(base))
            runtime.assert_not_called()
            ordinary = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                             'idempotency_key': 'ordinary-review'})
            with self.assertRaisesRegex(ContractError, 'restricted to benchmark'):
                command(parser.parse_args(base + ['--review-id', ordinary['review_id']]))
            runtime.assert_not_called()
            with self.assertRaisesRegex(ContractError, 'nonscheduled'):
                command(parser.parse_args(base + ['--review-id', ordinary['review_id'], '--scheduled']))
            runtime.assert_not_called()
            with patch.object(self.authority, 'status', return_value={'metadata': {'benchmark': {'arm': 'candidate'}}}):
                coordinator.return_value.run.return_value = {'status': 'benchmark_complete'}
                result = command(parser.parse_args(base + ['--review-id', ordinary['review_id']]))
            self.assertEqual(result['status'], 'benchmark_complete')
            used_config = runtime.call_args.args[0]
            self.assertEqual(used_config.concurrency, 4)
            self.assertEqual(used_config.state_dir, self.config.state_dir)
            self.assertEqual(self.config.concurrency, 2)
            self.assertTrue(coordinator.return_value.run.call_args.kwargs['lock_held'])

    def test_each_benchmark_override_rejects_ordinary_new_scheduled_and_nonrun(self):
        from job_search.job_reviews.runner import add_arguments, command
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ordinary = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                         'idempotency_key': 'ordinary-knobs'})['review_id']
        with patch('job_search.job_reviews.runner.load_runner_config', return_value=self.config), \
             patch('job_search.job_reviews.runner.require_start'), \
             patch('job_search.job_reviews.runner.build_authority', return_value=self.authority), \
             patch('job_search.job_reviews.runner.ProductionRuntime') as runtime:
            for flag, value in (('model', 'benchmark-model'), ('reasoning-effort', 'low'),
                                ('check-model', 'check-model'), ('check-reasoning-effort', 'high'),
                                ('batch-size', '5'), ('concurrency', '4'),
                                ('screening-model', 'screen-model'), ('screening-reasoning-effort', 'low'),
                                ('screening-batch-size', '200'), ('check-all', None)):
                base = ['--runner-config', str(self.root / 'runner.json'), '--benchmark-' + flag]
                if value is not None:
                    base.append(value)
                for action, extra in [('run', []), ('run', ['--review-id', ordinary]),
                                      ('run', ['--review-id', ordinary, '--scheduled']),
                                      ('status', ['--review-id', ordinary]),
                                      ('login', ['--review-id', ordinary]),
                                      ('readiness', ['--review-id', ordinary])]:
                    with self.subTest(flag=flag, action=action, extra=extra), self.assertRaises(ContractError):
                        command(parser.parse_args([action, *base, *extra]))
            runtime.assert_not_called()

    def screening_config(self, **changes):
        from job_search.job_reviews.authority import ReviewAuthority
        self.config = replace(self.config, screening_enabled=True, screening_model='screen-model',
                              screening_reasoning_effort='low', **changes).validate()
        self.authority = ReviewAuthority(self.fixture.service, approved_model=self.config.model,
            approved_reasoning_effort=self.config.reasoning_effort,
            approved_screening_model=self.config.screening_model,
            approved_screening_reasoning_effort=self.config.screening_reasoning_effort)

    def test_screening_routes_every_item_before_detail_and_blind_checks(self):
        self.fixture.insert('b')
        self.screening_config(concurrency=4)
        runner, runtime = self.coordinator()
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published', result)
        self.assertEqual(runtime.calls, [('screening', [1, 2]), ('primary', [1, 2]),
                                         ('check', [1, 2]), ('finalizer', [])])
        status = self.authority.status(result['review_id'])
        self.assertEqual(status['counts'].get('pending', 0), 0)
        self.assertEqual(status['audit_remaining_count'], 0)
        receipts = [json.loads(line) for line in (self.config.state_dir /
                    (result['run_id'] + '.assignments.jsonl')).read_text().splitlines()]
        self.assertEqual([(row['kind'], row['model'], row['reasoning_effort']) for row in receipts],
            [('screening', 'screen-model', 'low'), ('primary', 'gpt-6-astra', 'high'),
             ('check', 'gpt-6-astra', 'high'), ('finalizer', 'gpt-6-astra', 'high')])

    def test_screening_partial_retry_keeps_routes_and_detailed_assessments(self):
        self.fixture.insert('b')
        self.screening_config(concurrency=1)
        runner, runtime = self.coordinator(partial=True)
        result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published', result)
        self.assertEqual([jobs for kind, jobs in runtime.calls if kind == 'screening'], [[1, 2], [2]])
        self.assertEqual([jobs for kind, jobs in runtime.calls if kind == 'primary'], [[1, 2], [2]])

    def test_screening_profile_config_is_explicit_and_runtime_bound(self):
        from job_search.job_reviews.runner import ProductionRuntime
        for values in ({'screening_enabled': True}, {'screening_model': 'screen-model'},
                       {'screening_enabled': 'true'}, {'screening_batch_size': 201},
                       {'screening_batch_size': True}):
            with self.subTest(values=values), self.assertRaises(ContractError):
                replace(self.config, **values).validate()
        self.screening_config(concurrency=16)
        with self.assertRaisesRegex(ContractError, 'preloaded'):
            replace(self.config, preload_enabled=False).validate()
        runtime = ProductionRuntime(self.config, self.authority)
        screen = runtime._runtime_config('job-review-v2', purpose='screening')
        detail = runtime._runtime_config('job-review-v2', preload_enabled=False)
        self.assertEqual((screen.model, screen.reasoning_effort, screen.purpose), ('screen-model', 'low', 'screening'))
        self.assertTrue(screen.screening_enabled)
        self.assertTrue(screen.preload_enabled)
        self.assertEqual((detail.model, detail.reasoning_effort, detail.purpose), ('gpt-6-astra', 'high', 'detailed'))
        self.assertFalse(detail.preload_enabled)

    def test_oversized_routes_continue_across_queue_pages_and_always_receive_full_detail(self):
        import sqlite3
        description = 'Build Python APIs and SQL systems. ' + 'X' * 180_000
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET description=?', (description,))
        self.fixture.insert('b', description=description)
        self.fixture.insert('c', description=description)
        self.screening_config(concurrency=1, screening_batch_size=1)
        runner, runtime = self.coordinator()
        original = runtime.worker
        launches = []

        @contextmanager
        def record(grant, **kwargs):
            launches.append((grant.get('purpose', 'detailed'), grant.get('preload_enabled')))
            with original(grant, **kwargs) as worker:
                yield worker

        with patch.object(runtime, 'worker', side_effect=record):
            result = self.run_custom(runner)
        self.assertEqual(result['status'], 'published', result)
        self.assertFalse(any(kind == 'screening' for kind, _ in runtime.calls))
        self.assertEqual([jobs for kind, jobs in runtime.calls if kind == 'primary'], [[1], [2], [3]])
        self.assertTrue(all(purpose == 'detailed' and not preload for purpose, preload in launches[:-1]))

    def test_changed_policy_is_rejected_before_recovery_or_dispatch(self):
        self.screening_config()
        rid = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                    'idempotency_key': 'frozen-policy'})['review_id']
        self.authority.freeze_execution_policy(rid, self.config.execution_policy())
        self.config = replace(self.config, concurrency=4)
        runner, runtime = self.coordinator()
        with patch.object(self.authority, 'reconcile_interrupted') as recovery, self.assertRaises(ContractError):
            runner.run(review_id=rid)
        recovery.assert_not_called()
        self.assertEqual(runtime.calls, [])

    def test_large_completion_group_keeps_public_status_bounded_and_all_private_receipts(self):
        runner, _ = self.coordinator()
        result = self.run_custom(runner)
        previous = result['assignments']
        completions = [{'status': 'exited', 'kind': 'screening', 'exit_code': 0,
                        'ordinals': list(range(index * 200 + 1, (index + 1) * 200 + 1)),
                        'timings': {'elapsed_seconds': 1}, 'inference': {'request_count': 1}}
                       for index in range(16)]
        runner._record_assignments(completions)
        self.assertEqual(runner.journal['assignments'], previous + 16)
        self.assertEqual(runner.journal['last_assignments'], completions[-2:])
        lines = (self.config.state_dir / (result['run_id'] + '.assignments.jsonl')).read_text().splitlines()
        self.assertEqual([json.loads(line) for line in lines[-16:]], completions)

    def test_check_all_benchmark_adds_blind_checks_beyond_unchanged_exclusion_sample(self):
        import sqlite3
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET title=?,description=?',
                        ('Preschool Teacher', 'Teach children in preschool classrooms.'))
        for index in range(1, 60):
            self.fixture.insert('teacher-' + str(index), title='Preschool Teacher',
                                description='Teach children in preschool classrooms.')
        source = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                       'idempotency_key': 'all-check-source'})['review_id']
        for all_checks, expected in ((False, 50), (True, 60)):
            panel = self.authority.start_benchmark({'review_id': source, 'ordinals': list(range(1, 61)),
                'label': 'all-check comparison', 'idempotency_key': 'all-check-' + str(all_checks)})['review_id']
            self.config = replace(self.config, benchmark_check_all=all_checks)
            runner, runtime = self.coordinator()
            runtime.assessment = self.fixture.assessment(decision='exclude', stage='detailed',
                reason_code='non_technical', family='non_technical', alignment='unrelated',
                explanation='Preschool teaching is outside technical work.',
                evidence=[{'field': 'description', 'quote': 'Teach children'}], strengths=[], unknowns=[])
            result = runner.run(review_id=panel)
            self.assertEqual(result['status'], 'benchmark_complete', result)
            checked = [ordinal for kind, jobs in runtime.calls if kind == 'check' for ordinal in jobs]
            self.assertEqual(len(checked), expected)
            self.assertEqual(len(set(checked)), expected)
            self.assertEqual(result['phase_totals']['check']['jobs_attempted'], 50)
            if all_checks:
                self.assertEqual(set(checked), set(range(1, 61)))
                self.assertEqual(result['phase_totals']['benchmark_check']['jobs_attempted'], 10)
            else:
                self.assertNotIn('benchmark_check', result['phase_totals'])

    def test_check_all_config_cannot_enable_ordinary_or_scheduled_coordinator_run(self):
        with self.assertRaisesRegex(ContractError, 'benchmark_check_all'):
            replace(self.config, benchmark_check_all=1).validate()
        ordinary = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                         'idempotency_key': 'check-all-denied'})['review_id']
        self.config = replace(self.config, benchmark_check_all=True)
        runner, runtime = self.coordinator()
        for kwargs in ({}, {'review_id': ordinary}, {'review_id': ordinary, 'scheduled': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                runner.run(**kwargs)
        self.assertEqual(runtime.calls, [])

    def test_benchmark_selected_model_and_effort_execute_and_are_audited(self):
        from job_search.job_reviews.runner import add_arguments, command
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        source = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                       'idempotency_key': 'benchmark-source'})['review_id']
        for effort in ('low', 'medium', 'high'):
            panel = self.authority.start_benchmark({'review_id': source, 'ordinals': [1],
                'label': effort, 'idempotency_key': 'panel-' + effort})['review_id']
            args = parser.parse_args(['run', '--runner-config', str(self.root / 'runner.json'),
                '--review-id', panel, '--benchmark-model', 'benchmark-model',
                '--benchmark-reasoning-effort', effort, '--benchmark-batch-size', '5',
                '--benchmark-concurrency', '4'])
            with self.subTest(effort=effort), \
                 patch('job_search.job_reviews.runner.load_runner_config', return_value=self.config), \
                 patch('job_search.job_reviews.runner.require_start'), \
                 patch('job_search.job_reviews.runner.build_authority', return_value=self.authority), \
                 patch('job_search.job_reviews.runner.ProductionRuntime',
                       side_effect=lambda config, authority: FakeRuntime(authority, self.fixture.assessment())), \
                 patch('job_search.job_reviews.runner.ReviewCoordinator',
                       side_effect=lambda config, authority, runtime: ReviewCoordinator(
                           config, authority, runtime, is_draining=lambda: False)):
                result = command(args)
            self.assertEqual(result['status'], 'benchmark_complete', result)
            self.assertEqual((result['model'], result['reasoning_effort']), ('benchmark-model', effort))
            self.assertEqual(result['configuration']['batch_size'], 5)
            self.assertEqual(result['configuration']['concurrency'], 4)
            self.assertIsNone(self.authority.status(panel)['receipt'])
            saved = json.loads((self.config.state_dir / 'status.json').read_text())
            self.assertEqual((saved['model'], saved['reasoning_effort']), ('benchmark-model', effort))
        self.assertEqual((self.config.model, self.config.reasoning_effort,
                          self.config.batch_size, self.config.concurrency), ('gpt-6-astra', 'high', 20, 2))

    def test_benchmark_overrides_keep_config_validation(self):
        from job_search.job_reviews.runner import add_arguments, command
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(['run', '--runner-config', str(self.root / 'runner.json'),
                                  '--review-id', 'marked-panel', '--benchmark-model', 'valid-model'])
        with patch('job_search.job_reviews.runner.load_runner_config', return_value=self.config), \
             patch('job_search.job_reviews.runner.require_start'), \
             patch('job_search.job_reviews.runner.build_authority', return_value=self.authority), \
             patch.object(self.authority, 'status', return_value={'metadata': {'benchmark': {'label': 'panel'}}}), \
             patch('job_search.job_reviews.runner.ProductionRuntime') as runtime:
            for name, value in [('benchmark_model', ''), ('benchmark_model', '  '),
                                ('benchmark_model', 'bad\nmodel'), ('benchmark_batch_size', 21),
                                ('benchmark_batch_size', True), ('benchmark_reasoning_effort', 'xhigh'),
                                ('benchmark_concurrency', 3)]:
                bad = copy.copy(args)
                setattr(bad, name, value)
                with self.subTest(name=name, value=value), self.assertRaises(ContractError):
                    command(bad)
            runtime.assert_not_called()


if __name__ == '__main__':
    unittest.main()
