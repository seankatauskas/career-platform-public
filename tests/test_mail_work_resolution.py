"""Obsolete mail recovery closes only with terminal, same-message evidence."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_search.cli import main
from job_search.contracts import ConflictError, ContractError
from job_search.db import connect
from job_search.readiness import readiness_report
from job_search.recovery import RecoveryService, unresolved_work_count
from job_search.review_recovery import recover_review
from tests.test_review_recovery import Classifier, fixture


class MailWorkResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path, self.ledger, _, source, query, _ = fixture(self.temp.name)
        self.proposal = recover_review(self.ledger, source, query, Classifier(), 'fixture-model-v1')
        # Mirror production: mailbox processing has replaced an earlier failed
        # archived-review attempt, whose synchronous invocation was reconciled.
        with connect(self.path) as con:
            con.execute("UPDATE event_proposals SET status='rejected'")
            con.execute("UPDATE work_items SET status='dead',attempts=3,recovery_revision=4,"
                        "failure_kind='retryable',failure_retryable=1,external_outcome='terminal',"
                        "last_error='inference_reconciliation_required'")
            self.work = dict(con.execute('SELECT * FROM work_items').fetchone())
            con.execute("INSERT INTO inference_invocations(invocation_id,work_id,request_sha256,"
                        "provider_fingerprint,capability,work_revision,state,reserved_tokens,budget_day,created_at,updated_at) "
                        "VALUES('inv-old',?,'request','provider','generation',0,'failed',100,'2026-10-07',?,?)",
                        (self.work['work_id'], self.work['created_at'], self.work['created_at']))
        self.service = RecoveryService(self.path)

    def resolve(self, **changes):
        args = dict(expected_revision=4, command_id='resolve-old-mail')
        args.update(changes)
        return self.service.resolve_mail_review(self.work['work_id'], **args)

    def test_resolution_clears_health_without_changing_mail_or_inference_and_audits(self):
        with connect(self.path) as con:
            proposal_before = dict(con.execute('SELECT * FROM event_proposals').fetchone())
            invocation_before = dict(con.execute('SELECT * FROM inference_invocations').fetchone())
            stage_before = dict(con.execute('SELECT * FROM outlook_message_stage').fetchone())
            self.assertEqual(unresolved_work_count(con), 1)
        summary = self.service.list_work()[0]
        self.assertTrue(summary['resolution_allowed'])
        self.assertFalse(summary['retry_allowed'])
        result = self.resolve()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['evidence']['proposal_status'], 'rejected')
        self.assertEqual(self.resolve(), result)
        with connect(self.path) as con:
            self.assertEqual(unresolved_work_count(con), 0)
            self.assertEqual(dict(con.execute('SELECT * FROM event_proposals').fetchone()), proposal_before)
            self.assertEqual(dict(con.execute('SELECT * FROM inference_invocations').fetchone()), invocation_before)
            self.assertEqual(dict(con.execute('SELECT * FROM outlook_message_stage').fetchone()), stage_before)
            work = dict(con.execute('SELECT * FROM work_items').fetchone())
            self.assertEqual(work, {**self.work, 'status': 'cancelled', 'recovery_revision': 5})
            audit = dict(con.execute('SELECT * FROM work_recovery_commands').fetchone())
            self.assertEqual(json.loads(audit['before_json'])['last_error'], self.work['last_error'])
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM work_recovery_commands")
        queue = next(row for row in readiness_report(self.path)['capabilities'] if row['id'] == 'work_queue')
        self.assertEqual(queue['status'], 'ready')

    def test_uncertain_invocations_and_work_outcome_block_even_if_email_processed(self):
        for state in ('reserved', 'submitting', 'accepted', 'unknown'):
            with self.subTest(state=state), connect(self.path) as con:
                con.execute('UPDATE inference_invocations SET state=?', (state,))
            with self.assertRaisesRegex(ConflictError, 'external_reconciliation_required'):
                self.resolve()
            self.assertFalse(self.service.list_work()[0]['resolution_allowed'])
        with connect(self.path) as con:
            con.execute("UPDATE inference_invocations SET state='failed'")
            con.execute("UPDATE work_items SET external_outcome='unknown'")
        with self.assertRaisesRegex(ConflictError, 'external_reconciliation_required'):
            self.resolve()

    def test_pending_failed_and_other_message_evidence_cannot_clear_work(self):
        for status in ('pending', 'failed', 'ignored'):
            with connect(self.path) as con:
                con.execute('UPDATE outlook_message_stage SET processing_status=?', (status,))
            with self.assertRaisesRegex(ConflictError, 'mail_review_not_processed'):
                self.resolve()
        with connect(self.path) as con:
            con.execute("UPDATE outlook_message_stage SET processing_status='processed'")
            con.execute("UPDATE work_items SET created_at='2099-01-01T00:00:00Z'")
        with self.assertRaisesRegex(ConflictError, 'mail_review_proposal_missing'):
            self.resolve()

    def test_identity_owner_revision_and_actor_guards(self):
        with self.assertRaisesRegex(ContractError, 'user decision'):
            self.resolve(actor_kind='agent')
        with self.assertRaisesRegex(ConflictError, 'work changed'):
            self.resolve(expected_revision=3)
        cases = [('lease_token', 'owned', 'work_still_owned'),
                 ('task_kind', 'outlook.mail.sync', 'not_archived_review_work'),
                 ('payload_json', '{}', 'invalid_review_work_identity')]
        for column, value, reason in cases:
            with connect(self.path) as con:
                con.execute(f'UPDATE work_items SET {column}=?', (value,))
            with self.assertRaisesRegex(ConflictError, reason):
                self.resolve()
            with connect(self.path) as con:
                con.execute(f'UPDATE work_items SET {column}=?', (self.work[column],))
        with connect(self.path) as con:
            con.execute("UPDATE mail_evidence SET account_id='different-account'")
        with self.assertRaisesRegex(ConflictError, 'mail_review_evidence_missing'):
            self.resolve()

    def test_concurrent_commands_and_cross_action_idempotency(self):
        def attempt(command):
            try:
                return self.resolve(command_id=command)['status']
            except ConflictError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, ['one', 'two'])), ['cancelled', 'conflict'])
        with connect(self.path) as con:
            command = con.execute('SELECT command_id FROM work_recovery_commands').fetchone()[0]
        with self.assertRaisesRegex(ConflictError, 'different decision'):
            self.service.retry(self.work['work_id'], expected_revision=4, command_id=command)

    def test_cli_and_inspection_are_scoped_and_do_not_initialize_missing_state(self):
        absent = Path(self.temp.name) / 'absent.db'
        with self.assertRaises(ContractError):
            RecoveryService(absent).resolve_mail_review('work', expected_revision=0, command_id='new')
        self.assertFalse(absent.exists())
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps({'version': 1, 'project_root': self.temp.name, 'application_db': str(self.path)}))
        config.chmod(0o600)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = main(['--config', str(config), 'work-resolve-mail', self.work['work_id'],
                           '--expected-revision', '4', '--idempotency-key', 'cli-resolve'])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
