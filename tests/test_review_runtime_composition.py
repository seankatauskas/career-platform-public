"""Real coordinator/authority/socket composition; Docker and account auth are fake."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from job_search.contracts import ContractError, payload_sha256
from job_search.job_reviews.authority import ReviewAuthority
from job_search.job_reviews.reviewer_api import ReviewerClient
from job_search.job_reviews.codex_runtime import preload_evidence
from job_search.job_reviews.runner import ProductionRuntime, ReviewCoordinator
from job_search.job_reviews.runner_config import RunnerConfig
from tests import test_agent_job_reviews as fixtures


class ThreadWorker:
    """A stand-in container that uses only the credentialless public client."""
    def __init__(self, assignment, image, consume=None):
        self.receipt = {'container_id': hashlib.sha256(assignment['runtime_id'].encode()).hexdigest(),
                        'image_digest': image.split('@')[-1], 'config_sha256': '2' * 64}
        self.started = threading.Event()
        self.finished = threading.Event()
        self.error = None
        self.code = None
        self.terminated = False
        self.thread = None
        if consume is not None:
            def run():
                self.started.set()
                try:
                    consume()
                    self.code = 0
                except BaseException as exc:
                    self.error = exc
                    self.code = 1
                finally:
                    self.finished.set()
            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()

    def poll(self):
        return self.code

    def wait(self, timeout=5):
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise AssertionError('synthetic reviewer did not terminate')
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15


@unittest.skipUnless(os.getuid() > 0 and os.getgid() > 0, 'composition uses the current non-root reviewer UID')
class RuntimeCompositionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temp = tempfile.TemporaryDirectory(prefix='rc-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture.service.availability_checker = lambda jobs: [dict(ats=j['ats'], job_id=j['id'], status='unknown', checked_at=self.fixture.now, source='', reason='offline fixture') for j in jobs]
        self.authority = ReviewAuthority(self.fixture.service)
        self.config = RunnerConfig(application_config=self.root / 'app.json',
            state_dir=self.root / 's', runtime_dir=self.root / 'r', auth_home=self.root / 'auth',
            model_image='example/reviewer@sha256:' + '1' * 64,
            worker_uid=os.getuid(), worker_gid=os.getgid(), batch_size=1, concurrency=2)
        self.workers, self.grants, self.assignments, self.transcripts = [], [], [], []
        self.events = []
        self.lock = threading.Lock()

    def track_issue(self, original):
        def issue(*args, **kwargs):
            grant = original(*args, **kwargs)
            with self.lock:
                self.grants.append(grant)
            return grant
        return issue

    def assert_revoked(self):
        self.assertTrue(self.grants)
        for grant in self.grants:
            with self.assertRaises(ContractError):
                self.authority.scoped_call(grant['token'], 'assignment')
        journal = ''.join(path.read_text() for path in self.config.state_dir.glob('*.json'))
        for grant in self.grants:
            self.assertNotIn(grant['token'], journal)
        self.assertNotIn('Python APIs', journal)

    def test_worker_api_telemetry_aggregates_only_fixed_numeric_fields(self):
        from job_search.job_reviews.reviewer_api import assignment_proxy
        from job_search.job_reviews.model_gateway import gateway_server, RESPONSE_REJECTION_COUNTERS
        current = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                        'idempotency_key': 'telemetry-test'})
        runtime_id = 'worker_' + '7' * 32
        grant = self.authority.issue(current['review_id'], [1], 'primary', {
            'runtime_id': runtime_id, 'model': self.config.model,
            'reasoning_effort': self.config.reasoning_effort})

        @contextmanager
        def observed_api(*args, **kwargs):
            callback = kwargs['telemetry_callback']
            with assignment_proxy(*args, **kwargs) as server:
                callback({'operation': 'context', 'elapsed_seconds': .25, 'response_bytes': 10,
                          'status': 200, 'body': 'PRIVATE_BODY', 'headers': 'PRIVATE_HEADERS'})
                callback({'operation': 'context', 'elapsed_seconds': .5, 'response_bytes': 20, 'status': 400})
                callback({'operation': 'order', 'elapsed_seconds': .125, 'response_bytes': 40, 'status': 200})
                callback({'operation': 'PRIVATE_ROUTE', 'elapsed_seconds': 1, 'response_bytes': 30,
                          'error': 'PRIVATE_ERROR'})
                yield server

        @contextmanager
        def observed_model(*args, **kwargs):
            with gateway_server(*args, **kwargs) as server:
                kwargs['telemetry_callback']({'request_started_count': 2,
                    'gateway_request_rejected_count': 1, 'gateway_response_rejected_count': 1,
                    'upstream_transport_failed_count': 1, 'authentication_failed_count': 1,
                    'gateway_busy_count': 1, 'PRIVATE_untrusted_count': 1,
                    'gateway_response_rejected_PRIVATE_count': 1,
                    **{key: 1 for key in RESPONSE_REJECTION_COUNTERS}})
                for invalid in (-1, True, '1', float('nan'), float('inf')):
                    kwargs['telemetry_callback']({'request_started_count': invalid,
                                                  'gateway_busy_count': invalid,
                                                  **{key: invalid for key in RESPONSE_REJECTION_COUNTERS}})
                kwargs['telemetry_callback']({'request_count': 1, 'upstream_duration_ms': 40,
                                              'input_tokens': 20, 'upstream_status': 200, 'text': 'PRIVATE_MODEL_TEXT'})
                for status in (429, 500, 404, 99, 600, True, '429'):
                    kwargs['telemetry_callback']({'upstream_status': status, 'error': 'PRIVATE_ERROR'})
                yield server

        def launch(config, assignment, *_args, **_kwargs):
            worker = ThreadWorker(assignment, config.image)
            worker.code = 0
            return worker

        with patch('job_search.job_reviews.auth_owner.NativeAuthOwner'), \
             patch('job_search.job_reviews.codex_runtime.launch_worker', side_effect=launch), \
             patch('job_search.job_reviews.reviewer_api.assignment_proxy', side_effect=observed_api), \
             patch('job_search.job_reviews.model_gateway.gateway_server', side_effect=observed_model), \
             ProductionRuntime(self.config, self.authority) as runtime:
            with runtime.worker(grant, runtime_id=runtime_id) as worker:
                self.assertEqual(worker.telemetry, {'api_context_calls': 2, 'api_context_seconds': .75,
                    'api_context_response_bytes': 30, 'request_started_count': 2, 'request_count': 1,
                    'api_order_calls': 1, 'api_order_seconds': .125, 'api_order_response_bytes': 40,
                    'gateway_request_rejected_count': 1, 'gateway_response_rejected_count': 1,
                    'upstream_transport_failed_count': 1, 'authentication_failed_count': 1,
                    'gateway_busy_count': 1, **{key: 1 for key in RESPONSE_REJECTION_COUNTERS},
                    'upstream_duration_ms': 40, 'input_tokens': 20, 'upstream_http_2xx': 1,
                    'upstream_http_4xx': 2, 'upstream_http_5xx': 1, 'upstream_rate_limited_count': 1})
                self.assertNotIn('PRIVATE_', json.dumps(worker.telemetry))
        self.authority.revoke(grant['grant_id'])

    def test_parallel_primary_check_complete_over_real_private_sockets(self):
        description = 'Python APIs and SQL systems. ' + 'Posting evidence. ' * 900
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET description=?', (description,))
            con.execute('ALTER TABLE jobs ADD COLUMN ranking_score TEXT')
            con.execute("UPDATE jobs SET ranking_score='RANKING_SENTINEL_MUST_STAY_PRIVATE'")
        # Add a second posting without relying on the fixture INSERT column count.
        with sqlite3.connect(self.fixture.db) as con:
            con.execute("INSERT INTO jobs SELECT ats,'b',title,company,location,description,jobUrl,posted_at,source_updated_at,first_seen,last_seen,closed_at,ranking_score FROM jobs WHERE id='a'")
        self.fixture.facts.append({'fact_id': 'fact-2', 'source': 'experience', 'text': 'Built SQL reports'})
        self.fixture.profile['fingerprint'] = payload_sha256(self.fixture.facts)
        interrupted = self.authority.start({'mode': 'custom', 'window_start': '2026-10-01T00:00:00Z',
                                            'idempotency_key': 'composition-interrupted'})
        self.authority.freeze_execution_policy(interrupted['review_id'], self.config.execution_policy())
        abandoned = self.authority.issue(interrupted['review_id'], [1], 'primary', {
            'runtime_id': 'worker_' + 'a' * 32, 'model': self.config.model,
            'reasoning_effort': self.config.reasoning_effort})
        original_issue, original_mark = self.authority.issue, self.authority.mark_launch
        original_scoped = self.authority.scoped_call
        launched = set()
        completed = []

        def mark(grant_id, runtime_id, receipt):
            result = original_mark(grant_id, runtime_id, receipt)
            with self.lock:
                launched.add(grant_id)
                self.events.append(('recorded', grant_id))
            return result

        def scoped(token, operation, args=None):
            grant = next(item for item in self.grants if item['token'] == token)
            self.assertIn(grant['grant_id'], launched)
            with self.lock:
                self.events.append(('tool', grant['grant_id']))
            return original_scoped(token, operation, args)

        def recover(config, workers):
            self.assertEqual(config.worker_uid, os.getuid())
            self.assertEqual(len(workers), 1)
            self.assertEqual(workers[0]['grant_id'], abandoned['grant_id'])
            self.assertEqual(workers[0]['runtime_id'], 'worker_' + 'a' * 32)
            self.assertIsNone(workers[0]['container_id'])
            with self.assertRaises(ContractError):
                original_scoped(abandoned['token'], 'assignment')
            self.events.append(('recovered', None))
            return {'removed_container_ids': []}

        def launch(config, assignment, private_run_dir, *, model_socket, review_socket):
            self.assertEqual(self.events[0], ('recovered', None))
            self.assertEqual(set(assignment), {'grant_id', 'actor', 'expires_at', 'runtime_id'})
            self.assertNotIn('token', assignment)
            self.assertEqual(Path(review_socket).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(review_socket).stat().st_uid, os.getuid())
            self.assertTrue(Path(model_socket).is_socket())
            self.assertLess(len(str(review_socket).encode()), 104)
            self.assignments.append(copy.deepcopy(assignment))
            client = ReviewerClient(review_socket)

            def consume():
                transcript = []
                class RecordingClient:
                    def call(self, operation, args):
                        value = client.call(operation, args)
                        transcript.append(value)
                        return value
                packet = preload_evidence(RecordingClient())
                info = packet['assignment']
                self.assertEqual(len(packet['context']['facts']), 2)
                if info['kind'] == 'finalizer':
                    result = client.call('calibrations', {'calibrations': [
                        {'ordinal': item['ordinal'], 'position': position}
                        for position, item in enumerate(packet['calibration']['items'], 1)]})
                    self.assertTrue(all(row['status'] == 'saved' for row in result['results']))
                    client.call('finalize')
                    with self.lock:
                        self.transcripts.extend(transcript)
                    return
                for job in packet['jobs']:
                    self.assertNotIn('assessment', job)
                    self.assertEqual(job['job']['description'], description)
                    self.assertGreater(sum('description' in page for page in transcript), 1)
                    result = client.call('assessments', {'assessments': [{'ordinal': job['ordinal'], 'assessment': self.fixture.assessment(eligibility='no_known_barrier', eligibility_condition='', next_step='apply', category='core')}]})
                    self.assertEqual(result['results'][0]['status'], 'saved')
                    transcript.append(result)
                    with self.lock:
                        completed.append((info['kind'], job['ordinal']))
                with self.lock:
                    self.transcripts.extend(transcript)

            worker = ThreadWorker(assignment, config.image, consume)
            self.workers.append(worker)
            self.assertTrue(worker.started.wait(1))
            # launch_worker has not returned: ProductionRuntime cannot have
            # recorded this receipt or opened its real assignment_proxy barrier.
            self.assertFalse(worker.finished.wait(.05))
            self.assertNotIn(assignment['grant_id'], launched)
            return worker

        runtime = ProductionRuntime(self.config, self.authority)
        runner = ReviewCoordinator(self.config, self.authority, runtime, is_draining=lambda: False)
        auth = Mock()
        auth._request_headers.side_effect = AssertionError('composition test must never request model credentials')
        with patch('job_search.job_reviews.auth_owner.NativeAuthOwner', return_value=auth), \
             patch('job_search.job_reviews.codex_runtime.launch_worker', side_effect=launch), \
             patch('job_search.job_reviews.codex_runtime.recover_workers', side_effect=recover) as recovery, \
             patch.object(self.authority, 'issue', side_effect=self.track_issue(original_issue)), \
             patch.object(self.authority, 'mark_launch', side_effect=mark), \
             patch.object(self.authority, 'scoped_call', side_effect=scoped):
            result = runner.run(review_id=interrupted['review_id'])
        self.assertEqual([repr(w.error) for w in self.workers if w.error], [])
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['recovered_grants'], 1)
        self.assertEqual(set(completed), {('primary', 1), ('primary', 2), ('check', 1), ('check', 2)})
        self.assertEqual(len(result['receipt']['lists']), 2)
        self.assertEqual(len(self.assignments), 5)
        self.assertEqual(len({a['actor'] for a in self.assignments}), 5)
        self.assertEqual(len({a['runtime_id'] for a in self.assignments}), 5)
        self.assertNotIn('RANKING_SENTINEL_MUST_STAY_PRIVATE', json.dumps(self.transcripts))
        self.assertEqual(list(self.config.runtime_dir.rglob('*.sock')), [])
        self.assertFalse(self.config.schedule_enabled)
        recovery.assert_called_once()
        auth._request_headers.assert_not_called()
        self.assert_revoked()

    def test_distinct_checker_profile_binds_gateway_and_real_scoped_grants(self):
        from job_search.job_reviews.model_gateway import gateway_server
        self.config = replace(self.config, check_model='checker-model', check_reasoning_effort='medium').validate()
        self.authority = ReviewAuthority(self.fixture.service, approved_check_model='checker-model',
                                         approved_check_reasoning_effort='medium')
        observed = []
        @contextmanager
        def gateway(*args, **kwargs):
            observed.append((kwargs['kind'], kwargs['model'], kwargs['reasoning_effort']))
            with gateway_server(*args, **kwargs) as server:
                yield server
        with patch('job_search.job_reviews.model_gateway.gateway_server', side_effect=gateway):
            self.test_parallel_primary_check_complete_over_real_private_sockets()
        self.assertEqual(set(observed), {('primary', self.config.model, self.config.reasoning_effort),
            ('check', 'checker-model', 'medium'), ('finalizer', self.config.model, self.config.reasoning_effort)})
    def test_launch_receipt_failure_terminates_worker_and_revokes_grant(self):
        original_issue = self.authority.issue
        def launch(config, assignment, private_run_dir, **sockets):
            worker = ThreadWorker(assignment, config.image)
            self.workers.append(worker)
            return worker
        runtime = ProductionRuntime(self.config, self.authority)
        runner = ReviewCoordinator(self.config, self.authority, runtime, is_draining=lambda: False)
        with patch('job_search.job_reviews.auth_owner.NativeAuthOwner'), \
             patch('job_search.job_reviews.codex_runtime.launch_worker', side_effect=launch), \
             patch('job_search.job_reviews.codex_runtime.recover_workers', return_value={'removed_container_ids': []}), \
             patch.object(self.authority, 'issue', side_effect=self.track_issue(original_issue)), \
             patch.object(self.authority, 'mark_launch', side_effect=ContractError('synthetic receipt rejection')), \
             patch.object(self.authority, 'scoped_call', wraps=self.authority.scoped_call) as scoped:
            with self.assertRaisesRegex(ContractError, 'synthetic receipt rejection'):
                runner.run(mode='custom', window_start='2026-10-01T00:00:00Z')
        scoped.assert_not_called()
        self.assertEqual(len(self.workers), 1)
        self.assertTrue(self.workers[0].terminated)
        self.assertEqual(list(self.config.runtime_dir.rglob('*.sock')), [])
        result = json.loads((self.config.state_dir / 'status.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(self.authority.status(result['review_id'])['receipt'])
        self.assert_revoked()


if __name__ == '__main__':
    unittest.main()
