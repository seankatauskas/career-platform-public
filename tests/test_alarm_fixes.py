"""Regression cases from missing metrics, backup alerts, and blocked mail."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from job_search import aws_ops as ops
from job_search.db import connect
from job_search.inference.contracts import InferenceResponseRejected
from job_search.inference.usage import (
    begin_invocation, current_scope, InvocationReconciliationRequired,
    UsagePolicy, usage_report,
)
from job_search.mail.model import ModelExecutionError
from job_search.contracts import ContractError, ConflictError
from tests.test_mail_understanding_runtime import fixture
from tests import test_job_search_aws_ops as operations


class MailOutputTests(unittest.TestCase):
    def test_archived_review_validation_cannot_block_mailbox_inference(self):
        from tests.test_review_recovery import fixture as archived_fixture, Classifier, stage
        from job_search.review_recovery import recover_review
        from job_search.inference.providers import _managed_sync_transport
        for mode in ('invalid', 'malformed', 'empty', 'save_failed', 'unknown'):
            with self.subTest(mode=mode), TemporaryDirectory() as d:
                path, ledger, _, source, query, _ = archived_fixture(d)
                before = stage(path)
                transport = _managed_sync_transport(lambda *args: {'usage': {'total_tokens': 10}}, 'fixture', 'generation')
                classifier = Classifier('invalid' if mode == 'invalid' else 'valid')
                original = classifier.classify
                def classify(text, candidates):
                    transport('https://fixture.invalid', {}, b'{"max_tokens":20}', 30, 1000)
                    if mode == 'malformed': raise ModelExecutionError('invalid JSON')
                    if mode == 'empty': raise InferenceResponseRejected('no text', retryable=False)
                    if mode == 'unknown': raise RuntimeError('unexpected interruption after response')
                    return original(text, candidates)
                with patch.object(classifier, 'classify', side_effect=classify):
                    if mode == 'save_failed':
                        with patch.object(ledger.store, '_idempotent', side_effect=RuntimeError('lost database save')), self.assertRaises(InvocationReconciliationRequired):
                            recover_review(ledger, source, query, classifier, 'fixture', usage_limits={'max_inflight': 1})
                    else:
                        expected = InvocationReconciliationRequired if mode == 'unknown' else ContractError
                        with self.assertRaises(expected):
                            recover_review(ledger, source, query, classifier, 'fixture', usage_limits={'max_inflight': 1})
                report = usage_report(path)
                self.assertEqual(report['uncertain'], int(mode in ('save_failed', 'unknown')))
                self.assertEqual(report['inflight'], int(mode in ('save_failed', 'unknown')))
                self.assertEqual(report['reserved_requests'], 1)
                self.assertEqual(stage(path), before)
                with connect(path) as con:
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0], 0)

    def test_rejected_answer_releases_slot_and_allows_bounded_retry(self):
        for error in (ModelExecutionError('invalid JSON'), ContractError('invalid evidence'),
                      InferenceResponseRejected('no text', retryable=False)):
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as d:
                path, ledger, runtime, analyzer, obs, candidate, _ = fixture(d)
                runtime.policy = UsagePolicy(max_inflight=1)
                original = analyzer.analyze
                def answer(request):
                    call = begin_invocation('fixture', 'generation', b'private-input', reserved_tokens=10)
                    call.submitting(); call.terminal('completed')
                    raise error
                with patch.object(analyzer, 'analyze', side_effect=answer), self.assertRaises(type(error)):
                    runtime.process(obs, subject='Interview', body='Please reply', candidates=[candidate])
                report = usage_report(path)
                self.assertEqual((report['inflight'], report['uncertain'], report['reserved_requests']), (0, 0, 1))
                with connect(path) as con:
                    invocation = dict(con.execute('SELECT * FROM inference_invocations').fetchone())
                    self.assertEqual(invocation['state'], 'failed')
                    self.assertEqual(invocation['reconciliation_reason'], 'inference_output_rejected')
                    self.assertEqual(con.execute('SELECT state FROM mail_understanding_analyses').fetchone()[0], 'failed')
                    self.assertEqual(con.execute('SELECT external_outcome FROM work_items WHERE task_kind=?', ('mail.understanding',)).fetchone()[0], 'terminal')
                def retry(request):
                    call = begin_invocation('fixture', 'generation', b'private-input', reserved_tokens=10)
                    call.submitting(); call.terminal('completed')
                    return original(request)
                with patch.object(analyzer, 'analyze', side_effect=retry):
                    result = runtime.process(obs, subject='Interview', body='Please reply', candidates=[candidate])
                self.assertTrue(result['findings'])
                self.assertEqual(usage_report(path)['reserved_requests'], 2)

    def test_valid_answer_lost_before_save_still_requires_reconciliation(self):
        with TemporaryDirectory() as d:
            path, ledger, runtime, analyzer, obs, candidate, _ = fixture(d)
            original = analyzer.analyze
            def answer(request):
                call = begin_invocation('fixture', 'generation', b'input', reserved_tokens=10)
                call.submitting(); call.terminal('completed')
                return original(request)
            with patch.object(analyzer, 'analyze', side_effect=answer), \
                 patch.object(runtime.service, 'save', side_effect=ContractError('save failed')), \
                 self.assertRaises(InvocationReconciliationRequired):
                runtime.process(obs, subject='Interview', body='Please reply', candidates=[candidate])
            self.assertEqual(usage_report(path)['uncertain'], 1)

    def test_validation_error_cannot_release_an_unknown_submission(self):
        with TemporaryDirectory() as d:
            path, ledger, runtime, analyzer, obs, candidate, _ = fixture(d)
            def answer(request):
                call = begin_invocation('fixture', 'generation', b'input', reserved_tokens=10)
                call.submitting(); call.unknown()
                raise ModelExecutionError('invalid response')
            with patch.object(analyzer, 'analyze', side_effect=answer), self.assertRaises(InvocationReconciliationRequired):
                runtime.process(obs, subject='Interview', body='Please reply', candidates=[candidate])
            self.assertEqual(usage_report(path)['uncertain'], 1)

    def test_stale_worker_cannot_classify_completed_answer_as_rejected(self):
        with TemporaryDirectory() as d:
            path, ledger, runtime, analyzer, obs, candidate, _ = fixture(d)
            def answer(request):
                call = begin_invocation('fixture', 'generation', b'input', reserved_tokens=10)
                call.submitting(); call.terminal('completed')
                with connect(path) as con:
                    con.execute('UPDATE work_items SET recovery_revision=recovery_revision+1 WHERE work_id=?', (current_scope().work_id,))
                raise ModelExecutionError('invalid answer from stale worker')
            with patch.object(analyzer, 'analyze', side_effect=answer), self.assertRaises(ConflictError):
                runtime.process(obs, subject='Interview', body='Please reply', candidates=[candidate])
            with connect(path) as con:
                self.assertEqual(con.execute('SELECT state FROM inference_invocations').fetchone()[0], 'completed')


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = operations.OperationsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data = self.fixture.c, self.fixture.data
        self.c['instance_id'] = 'i-fixture'
        (self.data / 'activation.json').write_text('{"enabled": true}')

    def report(self, operation=None, held=False):
        with patch.object(ops, 'preflight', return_value={'issues': []}), \
             patch.object(ops, 'healthy', return_value=False), \
             patch.object(ops, 'domain_readiness', side_effect=ops.OpsError('probe failed')), \
             patch.object(ops, 'read_operation', return_value=operation), \
             patch.object(ops, 'operation_lock_held', return_value=held), \
             patch.object(ops, 'aws') as aws:
            report = ops.status(self.c, publish=True)
        return report, {m['MetricName']: m['Value'] for m in json.loads(aws.call_args.args[-1])}

    def test_backup_grace_requires_live_lock_valid_start_and_bounded_duration(self):
        now = datetime.now(timezone.utc)
        base = dict(operation_id='a'*32, kind='backup', complete=False, started_at=now.isoformat())
        for held, changes, expected in (
            (True, {}, 1), (False, {}, 0), (None, {}, 0),
            (True, {'complete': True}, 0), (True, {'kind': 'deploy'}, 0),
            (True, {'started_at': (now-timedelta(hours=2)).isoformat()}, 0),
            (True, {'started_at': (now+timedelta(minutes=5)).isoformat()}, 0),
            (True, {'started_at': 'invalid'}, 0),
        ):
            with self.subTest(held=held, changes=changes):
                report, metrics = self.report({**base, **changes}, held)
                self.assertEqual(metrics['MaintenanceActive'], expected)
                self.assertEqual(metrics['Healthy'], 0)
                self.assertEqual(metrics['DomainReady'], 0)
                self.assertEqual(metrics['MonitorHeartbeat'], 1)
                self.assertEqual(metrics['BackupAgeSeconds'], 999999)

    def test_running_backup_is_not_failed_but_abandoned_attempt_is(self):
        path = self.data / 'backup-attempt.json'
        now = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
        def backup(c):
            attempt = json.loads(path.read_text())
            self.assertTrue(attempt['in_progress'])
            self.assertFalse(attempt['failed'])
            self.assertFalse(self.report(held=True)[0]['backup_attempt_failed'])
            self.assertTrue(self.report(held=False)[0]['backup_attempt_failed'])
            self.assertTrue(self.report(held=None)[0]['backup_attempt_failed'])
            return {'status': 'backed_up'}
        with patch.object(ops, 'backup_unlocked', side_effect=backup):
            ops.scheduled_backup(self.c, now=now)
        self.assertEqual(json.loads(path.read_text())['in_progress'], False)
        self.assertFalse(self.report()[0]['backup_attempt_failed'])

    def test_failed_backup_remains_failed_during_retry_until_success(self):
        now = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
        with patch.object(ops, 'backup_unlocked', side_effect=ops.OpsError('upload failed')):
            with self.assertRaises(ops.OpsError): ops.scheduled_backup(self.c, now=now)
        self.assertTrue(self.report()[0]['backup_attempt_failed'])
        def retry(c):
            self.assertTrue(self.report(held=True)[0]['backup_attempt_failed'])
            return {'status': 'backed_up'}
        with patch.object(ops, 'backup_unlocked', side_effect=retry):
            ops.scheduled_backup(self.c, now=now+timedelta(minutes=30))
        self.assertFalse(self.report()[0]['backup_attempt_failed'])

    def test_two_daily_windows_survive_midnight_and_do_not_repeat_success(self):
        now = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
        with patch.object(ops, 'backup_unlocked', return_value={'status': 'backed_up'}) as backup:
            for hours in (0, 1, 11.5, 12, 13, 16, 23.5, 24):
                ops.scheduled_backup(self.c, now=now+timedelta(hours=hours))
            self.assertEqual(backup.call_count, 3)

    def test_monitor_readiness_uses_workflow_and_snapshot_without_ranking_scan(self):
        from job_search.runtime import RuntimeConfigV1
        from job_search.runtime_readiness import runtime_readiness
        from job_search.service import JobSearchLedger
        config = replace(RuntimeConfigV1.defaults(self.data), shortlist_policy='compare')
        JobSearchLedger(config.application_db)
        with patch('job_search.runtime_readiness.dependency_health', side_effect=AssertionError('live dependencies')), \
             patch('job_search.ranking.refresh.inspect_policies', side_effect=AssertionError('catalog scan')):
            report = runtime_readiness(config, use_snapshot=True, include_ranking_details=False)
        self.assertIn('ranking', {c['id'] for c in report['capabilities']})
        self.assertNotIn('ranking_policies', report)
        self.assertIn('pending_reconciliation', report['metrics'])

    def test_diagnostic_subprocess_timeout_preserves_publication(self):
        def hung_probe(c):
            ops.run(['fixture'], timeout=4500)
        with patch.object(ops, 'preflight', return_value={'issues': []}), \
             patch.object(ops, 'healthy', return_value=True), \
             patch.object(ops, 'domain_readiness', side_effect=hung_probe), \
             patch.object(ops.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 50)) as run, \
             patch.object(ops, 'aws') as aws:
            report = ops.status(self.c, publish=True)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 50)
        metrics = {m['MetricName']: m['Value'] for m in json.loads(aws.call_args.args[-1])}
        self.assertEqual(metrics['Healthy'], 1)
        self.assertEqual(metrics['DomainReady'], 0)
        self.assertEqual(metrics['MonitorHeartbeat'], 1)
        self.assertEqual(report['domain']['reason_code'], 'domain_report_unavailable')
        self.assertIsNone(ops._STATUS_DEADLINE.get())


if __name__ == '__main__':
    unittest.main()
