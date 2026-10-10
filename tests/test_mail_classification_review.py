"""Rejected classifications stay reviewable without inventing application events."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError, MutationContext
from job_search.db import connect
from job_search.mail.model import ModelOutputError, ModelExecutionError
from job_search.mail.pipeline import analyze_mail
from job_search.mail.sanitizer import sanitize_mail
from job_search.mail.review import MailReviewService
from job_search.review_messages import review_message
from job_search.review_recovery import recover_review
from job_search.inference.contracts import InferenceResponseRejected, InferenceTransportError
from job_search.inference.usage import begin_invocation, usage_report, InvocationReconciliationRequired
from tests.test_review_recovery import fixture, Classifier, GovernedClassifier, stage, QUOTE, SUBJECT, BODY
from tests.test_job_search_sync import FakeMail, change, SQLiteOutlookState, JobSearchLedger, OutlookMailCoordinator
from tests.test_mail_understanding_runtime import fixture as shared_fixture


class ManualReviewTests(unittest.TestCase):
    def test_shared_ownership_removes_manual_review_from_both_queues(self):
        from job_search.mail.understanding_store import MailUnderstandingService
        with TemporaryDirectory() as directory:
            path, ledger, _, source, query, _ = fixture(directory)
            result = recover_review(ledger, source, query, Classifier('invalid'), 'fixture')
            reviews = MailReviewService(ledger)
            evidence_id = reviews.list_pending()['items'][0]['evidence_id']
            self.assertTrue(any(i['id'] == result['review_id'] for i in ledger.list_attention_items()))
            MailUnderstandingService(ledger).own_evidence(
                evidence_id, MutationContext('shared-owner', 'system', 'mail_understanding'))
            self.assertEqual(reviews.list_pending()['items'], [])
            self.assertFalse(any(i['id'] == result['review_id'] for i in ledger.list_attention_items()))
            with self.assertRaises(ContractError):
                reviews.preview([dict(review_id=result['review_id'], decision='dismiss', reason='Read email')])
            with connect(path) as con:
                self.assertEqual(con.execute('SELECT status FROM mail_classification_reviews').fetchone()[0], 'pending')

    def test_archived_bad_answer_is_pending_review_and_successful_work(self):
        for mode, excerpt in [('invalid', None), ('valid', 'A different retained excerpt')]:
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                path, ledger, _, source, query, _ = fixture(directory, existing_excerpt=excerpt)
                classifier = GovernedClassifier(mode)
                result = recover_review(ledger, source, query, classifier, 'fixture')
                self.assertEqual(result['status'], 'pending')
                self.assertTrue(result['review_id'].startswith('unclassified:'))
                self.assertEqual(stage(path)['processing_status'], 'processed')
                repeated = recover_review(ledger, None, query, None, 'fixture')
                self.assertEqual(repeated['review_id'], result['review_id'])
                self.assertFalse(repeated['created'])
                self.assertEqual(classifier.posts, 1)
                with connect(path) as con:
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0], 0)
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM application_events').fetchone()[0], 0)
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM lifecycle_mail_links').fetchone()[0], 0)
                    self.assertEqual(con.execute('SELECT status FROM work_items').fetchone()[0], 'succeeded')
                report = usage_report(path)
                self.assertEqual((report['inflight'], report['uncertain'], report['reserved_requests']), (0, 0, 1))
                from job_search.readiness import readiness_report
                readiness = readiness_report(path)
                self.assertFalse(any(c['reason_code'] in ('staged_mail_failed', 'unresolved_work_failed') for c in readiness['capabilities']))
                items = ledger.list_attention_items()
                item = next(i for i in items if i['kind'] == 'mail_classification_review')
                self.assertNotIn('evidence_quote', item)
                self.assertFalse(any(i['kind'] == 'mail_processing_failure' for i in items))
                self.assertEqual(review_message(ledger, source, item)['subject'], SUBJECT)

    def test_obsolete_owner_can_resolve_against_manual_review_but_not_unknown_usage(self):
        from job_search.recovery import RecoveryService
        with TemporaryDirectory() as directory:
            path, ledger, _, source, query, _ = fixture(directory)
            review = recover_review(ledger, source, query, GovernedClassifier('invalid'), 'fixture')
            with connect(path) as con:
                con.execute("UPDATE work_items SET status='dead',failure_kind='retryable',failure_retryable=1")
                work = dict(con.execute('SELECT * FROM work_items').fetchone())
                con.execute("UPDATE inference_invocations SET state='unknown'")
            service = RecoveryService(path)
            with self.assertRaises(ContractError):
                service.resolve_mail_review(work['work_id'], expected_revision=work['recovery_revision'], command_id='blocked')
            with connect(path) as con:
                con.execute("UPDATE inference_invocations SET state='completed'")
            result = service.resolve_mail_review(work['work_id'], expected_revision=work['recovery_revision'], command_id='resolve')
            self.assertEqual(result['evidence']['review_id'], review['review_id'])
            self.assertEqual(result['status'], 'cancelled')

    def test_receipt_contractions_preserve_exact_evidence_and_outcome_guards(self):
        for opening in ('We received', 'We have received', "We've received", 'We’ve received'):
            mail = sanitize_mail('Thank you for applying to Northstar', opening + ' your application for Engineer.')
            proposal = analyze_mail(evidence_id='fixture', sender_address='no-reply@ashbyhq.com',
                mail=mail, candidates=[], classifier=None, model_version='fixture')
            self.assertEqual(proposal.event_type.value, 'submission_confirmed')
            self.assertTrue(mail.verifies_evidence(proposal.evidence_quote, proposal.span_start, proposal.span_end))
        for body, event in [('We have not received your application.', None),
                            ('We’ve received your application. An interview may follow.', None),
                            ('We’ve received your application. We have decided to move forward with other candidates.', 'rejection_received')]:
            proposal = analyze_mail(evidence_id='fixture', sender_address='no-reply@ashbyhq.com',
                mail=sanitize_mail('Application received', body), candidates=[], classifier=None, model_version='fixture')
            self.assertEqual(proposal.event_type.value if proposal else None, event)

    def test_archived_receipt_uses_rules_even_without_a_model(self):
        with TemporaryDirectory() as directory:
            path, ledger, _, source, query, _ = fixture(directory)
            with connect(path) as con:
                con.execute("UPDATE outlook_message_stage SET sender='no-reply@ashbyhq.com'")
            result = recover_review(ledger, source, query, None, 'fixture')
            self.assertEqual(result['status'], 'pending')
            with connect(path) as con:
                proposal = con.execute('SELECT * FROM event_proposals').fetchone()
                self.assertEqual(proposal['producer_kind'], 'rule')
                self.assertIsNone(proposal['proposed_application_id'])
                self.assertEqual(con.execute('SELECT COUNT(*) FROM inference_invocations').fetchone()[0], 0)

    def test_reviewer_must_choose_event_and_exact_source_before_previewed_resolution(self):
        for decision in ('record', 'keep', 'dismiss'):
            with self.subTest(decision=decision), TemporaryDirectory() as directory:
                path, ledger, _, source, query, _ = fixture(directory)
                result = recover_review(ledger, source, query, Classifier('invalid'), 'fixture')
                reviews = MailReviewService(ledger)
                listed = reviews.list_pending()['items']
                self.assertEqual(listed[0]['review_id'], result['review_id'])
                choice = dict(review_id=result['review_id'], decision=decision, reason='Read the email')
                if decision != 'dismiss':
                    choice['new_application'] = dict(employer='Northstar Labs', title='Software Engineer')
                if decision == 'record':
                    with self.assertRaises(ContractError): reviews.preview([choice])
                    choice.update(event_type='submission_confirmed', evidence_quote='invented quote')
                    with self.assertRaises(ContractError): reviews.preview([choice])
                    choice['evidence_quote'] = 'BEGIN UNTRUSTED EMAIL'
                    with self.assertRaises(ContractError): reviews.preview([choice])
                    choice['evidence_quote'] = QUOTE
                plan = reviews.preview([choice])
                self.assertEqual(ledger.list_applications(), [])
                with self.assertRaises(ContractError):
                    reviews.apply(plan['decisions'], plan['preview_hash'], MutationContext('no-user', 'model', 'fixture'))
                context = MutationContext('resolve', 'user', 'fixture')
                applied = reviews.apply(plan['decisions'], plan['preview_hash'], context)
                self.assertEqual(reviews.apply(plan['decisions'], plan['preview_hash'], context), applied)
                self.assertEqual(reviews.list_pending()['items'], [])
                with connect(path) as con:
                    row = con.execute('SELECT * FROM mail_classification_reviews').fetchone()
                    self.assertEqual(row['status'], 'resolved')
                    self.assertEqual(json.loads(row['resolution_json'])['decision'], decision)
                    count = con.execute("SELECT COUNT(*) FROM application_events WHERE event_type='submission_confirmed'").fetchone()[0]
                    self.assertEqual(count, int(decision == 'record'))

    def test_sync_rejection_is_reviewable_but_transport_and_execution_failures_are_not(self):
        errors = [ModelOutputError('bad JSON'), InferenceResponseRejected('empty answer', retryable=False),
                  InferenceTransportError('endpoint unavailable', retryable=False), ModelExecutionError('binary missing')]
        for error in errors:
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as directory:
                path = Path(directory) / 'ledger.db'
                ledger, state, mail = JobSearchLedger(path), SQLiteOutlookState(path), FakeMail()
                state.stage_changes('personal', 'inbox', [change()])
                mail.bodies['message-1'] = dict(subject='Recruiting update', receivedDateTime='2026-09-01T12:00:00Z',
                    sender={'emailAddress': {'address': 'recruiter@example.test'}}, body={'contentType':'text', 'content':'Please read this recruiting update.'})
                classifier = Classifier()
                with patch.object(classifier, 'classify', side_effect=error) as invoke:
                    coordinator = OutlookMailCoordinator(mail, state, ledger, classifier=classifier)
                    result = coordinator.process_pending()
                    reviewable = isinstance(error, (ModelOutputError, InferenceResponseRejected))
                    self.assertEqual(result.failed, int(not reviewable))
                    self.assertEqual(result.processed, int(reviewable))
                    if reviewable:
                        with connect(path) as con:
                            con.execute("UPDATE outlook_message_stage SET processing_status='pending'")
                        self.assertEqual(coordinator.process_pending().processed, 1)
                        self.assertEqual(invoke.call_count, 1)
                    with connect(path) as con:
                        self.assertEqual(con.execute('SELECT COUNT(*) FROM mail_classification_reviews').fetchone()[0], int(reviewable))
                        self.assertEqual(con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0], 0)

    def test_rejected_answer_save_failure_keeps_failure_and_unknown_usage(self):
        with TemporaryDirectory() as directory:
            path, ledger, _, source, query, _ = fixture(directory)
            with connect(path) as con:
                con.execute("CREATE TRIGGER fail_manual BEFORE INSERT ON mail_classification_reviews BEGIN SELECT RAISE(ABORT,'disk failure'); END")
            with self.assertRaises(InvocationReconciliationRequired):
                recover_review(ledger, source, query, GovernedClassifier('invalid'), 'fixture')
            self.assertEqual(stage(path)['processing_status'], 'failed')
            self.assertEqual(usage_report(path)['uncertain'], 1)
            with connect(path) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM mail_classification_reviews').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0], 0)

    def test_shared_rejected_answer_is_only_uncertainty_and_unknown_calls_stay_blocked(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), TemporaryDirectory() as directory:
                path, ledger, runtime, analyzer, observation, candidate, _ = shared_fixture(directory)
                def answer(request):
                    call = begin_invocation('fixture', 'generation', b'input', reserved_tokens=10)
                    call.submitting()
                    if unknown: call.unknown()
                    else: call.terminal('completed')
                    raise ModelOutputError('bad JSON')
                with patch.object(analyzer, 'analyze', side_effect=answer):
                    if unknown:
                        with self.assertRaises(InvocationReconciliationRequired):
                            runtime.process(observation, subject='Recruiting', body='Please reply', candidates=[candidate])
                    else:
                        result = runtime.process(observation, subject='Recruiting', body='Please reply', candidates=[candidate])
                        self.assertTrue(result['findings'])
                        self.assertTrue(all(f['type'] == 'uncertainty' for f in result['findings']))
                        with connect(path) as con:
                            self.assertEqual(con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0], 0)
                            self.assertEqual(con.execute('SELECT status FROM work_items').fetchone()[0], 'succeeded')
                self.assertEqual(usage_report(path)['uncertain'], int(unknown))


if __name__ == '__main__':
    unittest.main()
