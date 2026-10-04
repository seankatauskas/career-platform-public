#!/usr/bin/env python3
"""Workspace association, partial availability, and HTTP read regressions."""
import json
from tests.test_job_search_dashboard import dashboard, start_direct, stamp, request
from job_search.contracts import EventProposalInput, ApplicationEventType, ProducerKind, MutationContext


def test_scoped_evidence_and_partial_context():
    with dashboard() as (server, controller, ledger, _):
        first = start_direct(ledger, 'first')
        other = start_direct(ledger, 'other')  # same company and title, distinct application
        quote = 'Please choose an interview time'
        evidence = ledger.record_mail_evidence({
            'account_id': 'account', 'immutable_message_id': 'message',
            'sender': 'recruiter@example.test', 'subject': 'Interview',
            'received_at': stamp(), 'body_sha256': 'c' * 64, 'excerpt': quote,
        }, MutationContext('mail', 'system', 'outlook_sync'))['evidence']['evidence_id']
        proposal = ledger.create_event_proposal(EventProposalInput(
            evidence, first, ApplicationEventType.INTERVIEW_REQUESTED, ProducerKind.MODEL,
            'fixture', .99, [first, other], quote, 0, len(quote), {}, 'proposal',
        ), MutationContext('proposal', 'model', 'classifier'))['proposal']
        archive = ledger.put_mail_archive({
            'account_id': 'account', 'immutable_message_id': 'message', 'key_id': 'fixture',
            'nonce': b'n' * 12, 'ciphertext': b'c' * 32, 'aad_sha256': 'a' * 64,
            'sanitized_sha256': 'b' * 64, 'sanitized_chars': 30, 'truncated': False,
        }, MutationContext('archive', 'system', 'outlook_sync'))
        assert controller.application_workspace(other)['messages'] == []
        missing = controller.application_workspace(first)
        assert missing['messages'][0]['excerpt'] == quote
        assert missing['messages'][0]['available'] is False
        assert missing['resume']['available'] is False
        calls = []
        class Mail:
            def get_mail_message(self, message_id):
                calls.append(message_id)
                return {'subject': 'Interview', 'excerpt': '<script>inert text</script>'}
        controller.mail_source = Mail()
        status, _, body = request(server, 'GET', f'/api/v1/applications/{first}/workspace')
        response = json.loads(body)
        assert status == 200 and response['messages'][0]['available']
        assert len(calls) == 1
        assert response['messages'][0]['excerpt'] == '<script>inert text</script>'
        assert 'ciphertext' not in response['messages'][0]
        assert 'account_id' not in response['messages'][0]
        class Locked:
            def get_mail_message(self, _):
                raise RuntimeError('private key path must not leak')
        controller.mail_source = Locked()
        partial = controller.application_workspace(first)
        assert partial['events'] and partial['messages'][0]['available'] is False
        assert 'private key' not in json.dumps(partial)
        ledger.decide_event_proposal(proposal['proposal_id'], 'accepted', selected_application_id=other,
            reason='Corrected association', context=MutationContext('accept', 'user', 'dashboard'))
        assert controller.application_workspace(first)['messages'] == []
        assert controller.application_workspace(other)['messages']


def test_confirmation_email_provenance():
    with dashboard() as (_, controller, ledger, _):
        first = start_direct(ledger, 'confirmation-first')
        other = start_direct(ledger, 'confirmation-other')
        received_at = stamp(-3600)
        quote = 'Your application has been received'
        evidence = ledger.record_mail_evidence({
            'account_id': 'account', 'immutable_message_id': 'confirmation',
            'sender': 'no-reply@example.test', 'subject': 'Thank you for applying',
            'received_at': received_at, 'body_sha256': 'd' * 64, 'excerpt': quote,
        }, MutationContext('confirmation-mail', 'system', 'outlook_sync'))['evidence']['evidence_id']
        proposal = ledger.create_event_proposal(EventProposalInput(
            evidence, first, ApplicationEventType.SUBMISSION_CONFIRMED, ProducerKind.RULE,
            'fixture', .99, [first, other], quote, 0, len(quote), {}, 'confirmation-proposal',
        ), MutationContext('confirmation-proposal', 'system', 'classifier'))['proposal']
        assert all('email_evidence' not in event for event in controller.application_workspace(first)['events'])
        ledger.decide_event_proposal(proposal['proposal_id'], 'accepted', selected_application_id=other,
            reason='Corrected association', context=MutationContext('confirmation-accept', 'user', 'codex'))
        assert all('email_evidence' not in event for event in controller.application_workspace(first)['events'])
        events = controller.application_workspace(other)['events']
        assert 'email_evidence' not in events[0]
        confirmation = events[-1]
        assert confirmation['source_kind'] == 'codex'
        assert confirmation['occurred_at'] == received_at
        assert confirmation['recorded_at'] != received_at
        assert confirmation['email_evidence'] == {
            'evidence_id': evidence, 'sender': 'no-reply@example.test',
            'subject': 'Thank you for applying', 'received_at': received_at,
            'evidence_quote': quote,
        }


if __name__ == '__main__':
    test_scoped_evidence_and_partial_context()
    test_confirmation_email_provenance()
    print('ok (workspace association, archive failure, HTTP, corrected match and confirmation provenance)')
