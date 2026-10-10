"""Active owner diagnostics must not inherit retired application truth."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.application_installation import freeze_legacy
from job_search.application_migration import convert_snapshot
from job_search.application_production import _seed
from job_search.application_readiness import application_readiness
from job_search.application_runtime import ApplicationRuntime
from job_search.db import connect
from job_search.readiness import readiness_report
from job_search.runtime import RuntimeConfigV1
from job_search.runtime_readiness import runtime_readiness
from job_search.verification import fingerprint
from tests.test_job_search_ledger import make_service, start
from tests import test_job_search_readiness as operational_fixtures


class OwnerReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path, ledger = make_service(self.tmp.name)
        start(ledger)
        report = convert_snapshot(self.path, self.root/'owners')
        self.runtime = ApplicationRuntime(self.root/'owners/candidate.sqlite')
        freeze_legacy(self.path, self.runtime, operator='fixture', report=report)
        self.config = replace(RuntimeConfigV1.defaults(self.root), application_db=self.path,
                              application_backend='owners', application_owner_db=self.runtime.executor.path)
        _seed(self.config, lambda: datetime.now(timezone.utc), {'OUTLOOK_CLIENT_ID':'fixture'})

    def test_owner_schedules_replace_retired_schedule_health(self):
        report = readiness_report(self.path, application_backend='owners')
        caps = {c['id']:c for c in report['capabilities']}
        self.assertEqual(caps['automation']['reason_code'], 'awaiting_first_scheduled_run')
        self.assertEqual(caps['outlook']['reason_code'], 'awaiting_first_scheduled_run')
        self.assertEqual(caps['application_outbox']['status'], 'ready')

    def test_retired_work_failure_does_not_mask_active_work_failure(self):
        # Reuse the production queue fixture, including its failure metadata.
        helper = operational_fixtures.DomainTests(); helper.path = self.path
        helper.work('old-failure','outlook.mail.sync',status='dead')
        report = readiness_report(self.path, application_backend='owners')
        self.assertEqual(report['metrics']['unresolved_work'],0)
        helper.work('new-failure','applications.mail.sync',status='dead')
        report = readiness_report(self.path, application_backend='owners')
        self.assertEqual(report['metrics']['unresolved_work'],1)

    def test_recovery_cannot_resurrect_retired_application_work(self):
        from job_search.recovery import RecoveryService
        from job_search.contracts import ConflictError
        helper = operational_fixtures.DomainTests(); helper.path = self.path
        helper.work('retired-mail','outlook.mail.sync',status='dead',retryable=True)
        recovery = RecoveryService(self.path)
        self.assertEqual(recovery.list_work(),[])
        with self.assertRaisesRegex(ConflictError,'retired_application_work'):
            recovery.retry('retired-mail',expected_revision=0,command_id='retry-old')
        with self.assertRaisesRegex(ConflictError,'retired_application_work'):
            recovery.resolve_mail_review('retired-mail',expected_revision=0,command_id='resolve-old')

    def test_independent_success_does_not_resolve_failed_owner_work(self):
        from job_search.recovery import RecoveryService
        with connect(self.path) as con:
            for identity,state,workflow,stamp in (
                ('notice-a','dead','one','2026-01-01T00:00:00Z'),
                ('notice-b','succeeded','two','2026-01-01T00:01:00Z'),
            ):
                con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,priority,due_at,attempts,max_attempts,created_at,workflow_id) VALUES (?,'application.deliver_owner_notification',?,'{}',?,0,?,0,1,?,?)",(identity,identity,state,stamp,stamp,workflow))
        report = readiness_report(self.path, application_backend='owners')
        self.assertEqual(report['metrics']['unresolved_work'],1)
        self.assertEqual(RecoveryService(self.path).list_work()[0]['reason_code'],'use_domain_recovery')

    def test_owner_report_uses_installation_without_reading_chief_state(self):
        with patch('job_search.chief_status.chief_status', side_effect=AssertionError('retired truth')):
            report = runtime_readiness(self.config, dependencies={}, include_ranking_details=False)
        self.assertNotIn('chief_of_staff', report)
        self.assertTrue(report['application_owners']['paused'])
        self.assertEqual(report['application_owners']['conversion_blockers'],0)
        self.assertIn('application_execution',{c['id'] for c in report['capabilities']})

    def test_historical_uncertainty_is_reported_through_its_new_owner(self):
        from job_search.commands import CommandContext, Principal
        context = CommandContext(Principal('converter','worker',{'import_snapshot'}),'historical-action','migration')
        self.runtime.executor.run(context,'import_snapshot',{},lambda tx:self.runtime.actions.import_history(tx,'old-action',{'uncertain':True}))
        state, caps = application_readiness(self.config)
        self.assertEqual(state['uncertain'],1)
        self.assertEqual(caps[1]['reason_code'],'external_reconciliation_required')

    def test_missing_owner_database_is_reported_without_creating_it(self):
        path = self.root/'missing.sqlite'
        result, caps = application_readiness(replace(self.config,application_owner_db=path))
        self.assertEqual(caps[0]['status'],'blocked')
        self.assertFalse(path.exists())

    def test_new_profile_and_account_invalidate_old_verification(self):
        profile = self.root/'mail.json'; profile.write_text('{}')
        selected=replace(self.config,mail_inference_config=profile,outlook_home_account_id='one')
        self.assertNotEqual(fingerprint(selected,'outlook_read'),fingerprint(replace(selected,outlook_home_account_id='two'),'outlook_read'))
        before=fingerprint(selected,'mail_inference'); profile.write_text('{"changed":true}')
        self.assertNotEqual(before,fingerprint(selected,'mail_inference'))
        self.assertNotEqual(before,fingerprint(replace(selected,application_backend='legacy'),'mail_inference'))


if __name__=='__main__':unittest.main()
