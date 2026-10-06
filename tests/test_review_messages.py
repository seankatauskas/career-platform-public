"""Offline review-message resolution across archived and missing mail."""

import tempfile

from job_search.contracts import ContractError, MutationContext
from job_search.mail.archive_source import EncryptedArchiveMailSource
from job_search.outlook.state import SQLiteOutlookState
from job_search.review_messages import review_message
from tests.test_job_search_mail_archive_source import archive_fixture, save_message
from tests.test_job_search_sync import change


def test_failure_message_uses_scoped_archive_and_handles_locked_archive():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        state = SQLiteOutlookState(ledger.store.db_path)
        state.stage_changes('outlook-personal', 'inbox', [change('graph-message-review')])
        state.mark_message('outlook-personal', 'inbox', 'graph-message-review', 'failed', 'analysis failed')
        body = 'First line\n\n' + ('Full message body. ' * 200).rstrip() + '\nFinal line'
        save_message(archive, 'review', 'Original subject', body)
        query = dict(kind='mail_processing_failure', id='graph-message-review',
                     account_id='outlook-personal', folder_ref='inbox', query_version='1')
        source = EncryptedArchiveMailSource(ledger, archive)
        assert review_message(ledger, source, query) == dict(
            subject='Original subject', body=body, available=True, truncated=False)
        for field in ('account_id', 'folder_ref', 'query_version'):
            try:
                review_message(ledger, source, {**query, field: 'wrong'})
            except ContractError:
                pass
            else:
                raise AssertionError('mailbox scope ignored')

        class LockedSource:
            def get_review_message(self, identity):
                raise RuntimeError('Private provider details')

        result = review_message(ledger, LockedSource(), query)
        assert result['available'] is False and result['body'] == ''
        assert 'Private provider' not in str(result)

        # The production dashboard adapter must retain the full-message method.
        from unittest.mock import patch
        from job_search.system import _DashboardMailSource
        with patch('job_search.system._archive_source', return_value=source):
            adapter = _DashboardMailSource(None, ledger)
            assert review_message(ledger, adapter, query)['body'] == body


def test_event_message_resolves_archive_from_evidence():
    from job_search.contracts import ApplicationEventType, EventProposalInput, ProducerKind
    from tests.test_job_search_dashboard import stamp, start_direct

    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        application_id = start_direct(ledger, 'review-message')
        quote = 'Please choose an interview time'
        evidence_id = ledger.record_mail_evidence({
            'account_id': 'outlook-personal', 'immutable_message_id': 'graph-message-event',
            'sender': 'recruiter@example.test', 'subject': 'Interview', 'received_at': stamp(),
            'body_sha256': 'c' * 64, 'excerpt': quote,
        }, MutationContext('review-evidence', 'system', 'test'))['evidence']['evidence_id']
        proposal = ledger.create_event_proposal(EventProposalInput(
            evidence_id, application_id, ApplicationEventType.INTERVIEW_REQUESTED,
            ProducerKind.MODEL, 'model-v1', 0.99, [application_id], quote, 0, len(quote),
            {'occurred_at': stamp()}, 'review-message',
        ), MutationContext('review-proposal', 'model', 'classifier'))['proposal']
        body = quote + '\n\nHere are the details.\n' + 'More detail. ' * 200
        # Sanitization trims trailing whitespace at collection time.
        body = body.rstrip()
        save_message(archive, 'event', 'Interview', body)
        source = EncryptedArchiveMailSource(ledger, archive)
        query = dict(kind='event_proposal', id=proposal['proposal_id'])
        assert review_message(ledger, source, query)['body'] == body
        fallback = review_message(ledger, None, query)
        assert fallback['body'] == quote and not fallback['available']


if __name__ == '__main__':
    test_failure_message_uses_scoped_archive_and_handles_locked_archive()
    test_event_message_resolves_archive_from_evidence()
    print('ok (2 review message tests)')
