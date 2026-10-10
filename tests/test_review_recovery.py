"""Offline archived analysis recovery with a configured-model fake."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from job_search.contracts import ContractError, JobSnapshot, MutationContext, RecommendationProvenance
from job_search.db import connect
from job_search.mail.archive_source import EncryptedArchiveMailSource
from job_search.outlook.state import SQLiteOutlookState
from job_search.review_recovery import recover_review
from tests.test_job_search_mail_archive_source import archive_fixture, save_message
from tests.test_job_search_sync import change


SUBJECT = 'Application received: Software Engineer II, Platform (Remote, United States)'
BODY = ('Thank you for applying to Northstar Labs.\n'
        'We received your application for Software Engineer II, Platform (Remote, United States).\n'
        'Our team will review your experience and contact you about next steps.')
QUOTE = 'We received your application'


class Classifier:
    def __init__(self, mode='valid'):
        self.mode, self.calls = mode, []

    def classify(self, text, candidates):
        self.calls.append((text, candidates))
        if self.mode == 'error':
            raise RuntimeError('PRIVATE PROVIDER INFORMATION')
        start = text.index(QUOTE)
        return dict(event_type='submission_confirmed',
                    application_id=candidates[0]['application_id'] if candidates else None,
                    confidence=0.99, evidence_quote='Not an actual quote' if self.mode == 'invalid' else QUOTE,
                    span_start=start, span_end=start + len(QUOTE), payload={})


def fixture(directory, existing_excerpt=None):
    path, ledger, archive = archive_fixture(directory)
    staged = replace(change('graph-message-recovery'), subject=SUBJECT, sender_address='recruiter@example.test')
    state = SQLiteOutlookState(path)
    state.stage_changes('outlook-personal', 'inbox', [staged])
    state.mark_message('outlook-personal', 'inbox', staged.immutable_id, 'failed', 'Graph HTTP 400: original failure')
    archived = save_message(archive, 'recovery', SUBJECT, BODY)
    if existing_excerpt is not None:
        ledger.record_mail_evidence(dict(account_id='outlook-personal', immutable_message_id=staged.immutable_id,
            conversation_id=staged.conversation_id, sender=staged.sender_address, subject=SUBJECT,
            received_at=staged.received_at, body_sha256='c' * 64, excerpt=existing_excerpt),
            MutationContext('existing-evidence', 'system', 'recovery-test'))
    query = dict(account_id='outlook-personal', folder_ref='inbox', query_version=1, message_id=staged.immutable_id)
    return path, ledger, archive, EncryptedArchiveMailSource(ledger, archive), query, archived


def stage(path):
    with connect(path) as con:
        return dict(con.execute('SELECT * FROM outlook_message_stage').fetchone())


def test_archived_confirmation_uses_configured_model_creates_pending_and_reuses_on_retry():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, _, source, query, _ = fixture(directory)
        classifier = Classifier()
        result = recover_review(ledger, source, query, classifier, 'fixture-model-v1')
        assert result['created'] and result['status'] == 'pending'
        assert len(classifier.calls) == 1
        assert len(classifier.calls[0][0]) <= 2048
        assert classifier.calls[0][1] == []  # Catalog matching happens after recovery.
        assert ledger.list_applications() == []
        with connect(path) as con:
            proposal = dict(con.execute('SELECT * FROM event_proposals').fetchone())
            assert proposal['producer_kind'] == 'model'
            assert proposal['proposed_application_id'] is None
            assert proposal['event_type'] == 'submission_confirmed'
            assert con.execute('SELECT COUNT(*) FROM application_events').fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM lifecycle_mail_links').fetchone()[0] == 0
        assert stage(path)['processing_status'] == 'processed'
        repeated = recover_review(ledger, None, query, None, 'fixture-model-v2')
        assert repeated['proposal_id'] == result['proposal_id'] and not repeated['created']
        assert len(classifier.calls) == 1


def test_recovery_includes_unsubmitted_application_context_but_leaves_selection_unassigned():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, _, source, query, _ = fixture(directory)
        application_id = ledger.start_application(JobSnapshot('ashby', 'northstar-123', '',
            'Software Engineer II, Platform', 'Northstar Labs', 'northstar', 'https://example.test/northstar-123'),
            RecommendationProvenance(), MutationContext('app', 'user', 'recovery-test'))['application']['application_id']
        assert ledger.list_mail_candidates() == []
        classifier = Classifier()
        result = recover_review(ledger, source, query, classifier, 'fixture-model-v1')
        assert classifier.calls[0][1][0]['application_id'] == application_id
        with connect(path) as con:
            assert con.execute('SELECT proposed_application_id FROM event_proposals WHERE proposal_id=?',
                               (result['proposal_id'],)).fetchone()[0] is None
        timeline = ledger.get_application_timeline(application_id)
        assert timeline['application']['submitted_at'] is None
        assert [event['event_type'] for event in timeline['events']] == ['application_started']


def test_existing_evidence_is_preserved_and_quote_offsets_are_relocated():
    with tempfile.TemporaryDirectory() as directory:
        excerpt = 'Stored evidence prefix. ' + QUOTE + ' and will contact you.'
        path, ledger, _, source, query, _ = fixture(directory, existing_excerpt=excerpt)
        with connect(path) as con:
            before = dict(con.execute('SELECT * FROM mail_evidence').fetchone())
        result = recover_review(ledger, source, query, Classifier(), 'fixture-model-v1')
        with connect(path) as con:
            assert dict(con.execute('SELECT * FROM mail_evidence').fetchone()) == before
            proposal = con.execute('SELECT * FROM event_proposals WHERE proposal_id=?', (result['proposal_id'],)).fetchone()
            assert proposal['span_start'] == excerpt.index(QUOTE)
            assert excerpt[proposal['span_start']:proposal['span_end']] == QUOTE


def test_model_execution_failure_preserves_original_review():
    for mode, existing in [('error', None)]:
        with tempfile.TemporaryDirectory() as directory:
            path, ledger, _, source, query, _ = fixture(directory, existing_excerpt=existing)
            before = stage(path)
            with connect(path) as con:
                archived = dict(con.execute('SELECT * FROM mail_archive').fetchone())
                evidence_count = con.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0]
            try:
                recover_review(ledger, source, query, Classifier(mode), 'fixture-model-v1')
            except ContractError as error:
                assert 'PRIVATE PROVIDER INFORMATION' not in str(error)
            else:
                raise AssertionError('Invalid analysis was accepted')
            assert stage(path) == before
            with connect(path) as con:
                assert con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0] == 0
                assert con.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0] == evidence_count
                assert dict(con.execute('SELECT * FROM mail_archive').fetchone()) == archived


def test_mailbox_scope_missing_archive_and_known_outbound_or_draft_fail_closed():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, _, source, query, archived = fixture(directory)
        classifier = Classifier()
        for wrong in [{'account_id': 'wrong'}, {'folder_ref': 'wrong'}, {'query_version': 2}]:
            try:
                recover_review(ledger, source, {**query, **wrong}, classifier, 'fixture-model-v1')
            except ContractError:
                pass
            else:
                raise AssertionError('Incorrect mailbox scope accepted')
        try:
            recover_review(ledger, None, query, classifier, 'fixture-model-v1')
        except ContractError:
            pass
        else:
            raise AssertionError('Unavailable archive was analyzed')
        assert classifier.calls == []
        ledger.lifecycle.observe_mail(dict(account_id=query['account_id'], immutable_message_id=query['message_id'],
            direction='outbound', sender='applicant@example.test', subject=SUBJECT,
            received_at='2026-09-01T12:00:00Z', sent_at='2026-09-01T12:01:00Z', archive_id=archived['archive_id']),
            MutationContext('sent-observation', 'system', 'recovery-test'))
        try:
            recover_review(ledger, source, query, classifier, 'fixture-model-v1')
        except ContractError:
            pass
        else:
            raise AssertionError('Outbound archived message was classified as incoming')
        assert classifier.calls == []
        assert stage(path)['processing_status'] == 'failed'


def test_concurrent_recovery_classifies_once_and_publishes_one_pending_proposal():
    started, proceed = threading.Event(), threading.Event()

    class SlowClassifier(Classifier):
        def classify(self, text, candidates):
            started.set()
            assert proceed.wait(5)
            return super().classify(text, candidates)

    with tempfile.TemporaryDirectory() as directory:
        path, ledger, _, source, query, _ = fixture(directory)
        classifier = SlowClassifier()
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(recover_review, ledger, source, query, classifier, 'fixture-model-v1')
            assert started.wait(5)
            second = executor.submit(recover_review, ledger, source, query, classifier, 'fixture-model-v1')
            proceed.set()
            first_result, second_result = first.result(), second.result()
        assert first_result['proposal_id'] == second_result['proposal_id']
        assert len(classifier.calls) == 1
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0] == 1


def test_dismissal_during_analysis_prevents_recovery_mutations():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, _, source, query, _ = fixture(directory)

        class DismissClassifier(Classifier):
            def classify(self, text, candidates):
                ledger.resolve_mail_failure(query['account_id'], query['folder_ref'], query['message_id'],
                    query['query_version'], 'dismiss', MutationContext('dismiss', 'user', 'recovery-test'))
                return super().classify(text, candidates)

        try:
            recover_review(ledger, source, query, DismissClassifier(), 'fixture-model-v1')
        except ContractError:
            pass
        else:
            raise AssertionError('Analysis replaced the user dismissal')
        assert stage(path)['processing_status'] == 'ignored'
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0] == 0


def test_opaque_sent_and_draft_folder_ids_are_rejected_without_an_observation():
    for role in ('sentitems', 'drafts'):
        with tempfile.TemporaryDirectory() as directory:
            path, ledger, _, source, query, _ = fixture(directory)
            opaque_folder = 'opaque-folder-' + role
            with connect(path) as con:
                con.execute('UPDATE outlook_message_stage SET folder_ref=?', (opaque_folder,))
                con.execute('INSERT INTO lifecycle_mail_folders VALUES (?,?,?)',
                            (query['account_id'], opaque_folder, role))
            classifier = Classifier()
            try:
                recover_review(ledger, source, {**query, 'folder_ref': opaque_folder}, classifier, 'fixture-model-v1')
            except ContractError:
                pass
            else:
                raise AssertionError('Known sent or draft folder was treated as incoming')
            assert classifier.calls == []
            assert stage(path)['processing_status'] == 'failed'


def test_stage_completion_failure_rolls_back_evidence_and_proposal_in_same_transaction():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, _, source, query, _ = fixture(directory)
        before = stage(path)
        with connect(path) as con:
            con.execute("CREATE TRIGGER fail_recovery_stage BEFORE UPDATE ON outlook_message_stage "
                        "WHEN NEW.processing_status='processed' BEGIN SELECT RAISE(ABORT, 'stage write failed'); END")
        classifier = Classifier()
        try:
            recover_review(ledger, source, query, classifier, 'fixture-model-v1')
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('Injected stage transition failure was ignored')
        assert stage(path) == before
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM command_results WHERE command_name='recover_mail_review'").fetchone()[0] == 0


class GovernedClassifier(Classifier):
    def __init__(self, mode='valid'):
        super().__init__(mode)
        self.posts = 0

    def classify(self, text, candidates):
        from job_search.inference.usage import begin_invocation
        invocation = begin_invocation('fictional-review-provider', 'generation', text.encode(), reserved_tokens=100)
        assert invocation is not None  # Dashboard recovery must never be unmanaged.
        invocation.submitting()
        self.posts += 1
        invocation.terminal('completed', observed_tokens=50)
        return super().classify(text, candidates)


def test_review_recovery_obeys_daily_inference_limit_and_retries_do_not_spend_again():
    from job_search.inference.usage import UsageDeferred
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, archive, source, query, _ = fixture(directory)
        classifier = GovernedClassifier()
        first = recover_review(ledger, source, query, classifier, 'fixture-model-v1', usage_limits={'daily_requests': 1})
        repeated = recover_review(ledger, source, query, classifier, 'fixture-model-v1', usage_limits={'daily_requests': 1})
        assert first['proposal_id'] == repeated['proposal_id'] and classifier.posts == 1
        second_message = replace(change('graph-message-second-recovery'), subject=SUBJECT, sender_address='recruiter@example.test')
        state = SQLiteOutlookState(path)
        state.stage_changes('outlook-personal', 'inbox', [second_message])
        state.mark_message('outlook-personal', 'inbox', second_message.immutable_id, 'failed', 'Original second error')
        save_message(archive, 'second-recovery', SUBJECT, BODY + '\nSecond message.')
        try:
            recover_review(ledger, source, {**query, 'message_id': second_message.immutable_id}, classifier,
                           'fixture-model-v1', usage_limits={'daily_requests': 1})
        except UsageDeferred:
            pass
        else:
            raise AssertionError('Archived recovery bypassed daily request limit')
        assert classifier.posts == 1
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM inference_invocations').fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM work_items WHERE status='succeeded'").fetchone()[0] == 1
            failed = con.execute('SELECT processing_status,last_error FROM outlook_message_stage WHERE immutable_message_id=?',
                                 (second_message.immutable_id,)).fetchone()
            assert tuple(failed) == ('failed', 'Original second error')


def test_uncheckpointed_model_result_requires_reconciliation_without_duplicate_provider_request():
    from job_search.inference.usage import InvocationReconciliationRequired
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, _, source, query, _ = fixture(directory)
        classifier = GovernedClassifier()
        for _ in range(2):
            try:
                with patch.object(ledger.store, '_idempotent', side_effect=RuntimeError('lost valid result before persistence')):
                    recover_review(ledger, source, query, classifier, 'fixture-model-v1')
            except InvocationReconciliationRequired:
                pass
            else:
                raise AssertionError('Lost model result was retried without reconciliation')
        assert classifier.posts == 1
        with connect(path) as con:
            assert con.execute('SELECT state FROM inference_invocations').fetchone()[0] == 'unknown'
            assert con.execute('SELECT COUNT(*) FROM event_proposals').fetchone()[0] == 0
        assert stage(path)['processing_status'] == 'failed'


def test_dashboard_does_not_offer_legacy_recovery_in_shared_or_paused_mail_mode():
    from job_search.runtime import RuntimeConfigV1
    from job_search.system import build_dashboard_controller
    for mode in ('legacy', 'shared', 'paused'):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(RuntimeConfigV1.defaults(Path(directory)), mail_understanding_mode=mode,
                             mail_understanding_source_scope='current', remote_mail_inference_enabled=True)
            with patch('job_search.chief_runtime.configure_services'), \
                    patch('job_search.system._configured_resume_lab', return_value=None), \
                    patch('job_search.system.DashboardController') as controller:
                build_dashboard_controller(config)
                factory = controller.call_args.kwargs['review_classifier_factory']
                assert bool(factory) == (mode == 'legacy')


if __name__ == '__main__':
    tests = [value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]
    for test in tests:
        test()
    print(f'ok ({len(tests)} archived review recovery tests)')
