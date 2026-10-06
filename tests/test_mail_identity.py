"""Offline regressions for delayed application capture and unsafe mail matches."""
from dataclasses import replace
from pathlib import Path
import tempfile

from job_search.contracts import ApplicationEventType, ContractError, JobSnapshot, MutationContext, ProducerKind, RecommendationProvenance
from job_search.db import connect
from job_search.mail import analyze_mail, build_proposal, sanitize_mail, match_known_template
from job_search.mail.identity import supported_candidates, supported_selection
from job_search.outlook.state import SQLiteOutlookState
from job_search.service import JobSearchLedger
from job_search.sync import OutlookMailCoordinator
from tests.test_job_search_mail import candidate, model_output
from tests.test_job_search_sync import FakeMail, change


def ctx(key, actor='user'):
    return MutationContext(key, actor, 'identity_regression')


def start(ledger, employer, key, title='Forward Deployed Engineer'):
    app = ledger.start_application(JobSnapshot('ashby', key, '', title, employer, employer.lower(),
        'https://example.test/'+key), RecommendationProvenance(), ctx('start-'+key))['application']['application_id']
    ledger.record_submission(app, '2026-09-01T11:59:55Z', ctx('submit-'+key))
    return app


class WrongCompanyClassifier:
    calls = 0

    def classify(self, text, candidates):
        self.calls += 1
        quote = 'our team has your application'
        return dict(event_type='submission_confirmed', application_id=candidates[0]['application_id'],
                    confidence=0.999, evidence_quote=quote, span_start=text.index(quote),
                    span_end=text.index(quote)+len(quote), payload={})


def test_wrong_company_high_confidence_shared_sender_and_stale_thread_are_unassigned():
    mail = sanitize_mail('Thanks for applying to Northstar',
        'Thanks for taking the time to apply for the Forward Deployed Engineer role at Northstar. '
        'This confirms our team has your application.')
    wrong = candidate(employer='Other Company', title='Forward Deployed Engineer', ats='ashby')
    for context in ('', 'sender previously linked to application', 'previously linked email conversation'):
        proposal = analyze_mail(evidence_id='e1', sender_address='no-reply@ashbyhq.com', mail=mail,
            candidates=[replace(wrong, match_context=context)], classifier=WrongCompanyClassifier(), model_version='fixture')
        assert proposal.proposed_application_id is None
        assert proposal.candidate_application_ids == ()
        assert proposal.confidence == 0.999  # Model certainty cannot override identity.


def test_sole_ats_candidate_and_no_employer_are_not_matches():
    for subject, body in (
        ('Application received', 'We have received your application for Software Engineer.'),
        ('Thanks for applying to Northstar', 'Thank you for applying to Northstar.'),
    ):
        result = match_known_template(evidence_id='e1', sender_address='notifications@greenhouse.io',
            mail=sanitize_mail(subject, body), candidates=[candidate(employer='Other Company')], sender_authenticated=True)
        assert result.proposal.proposed_application_id is None
        assert not result.proposal.candidate_application_ids


def test_same_company_roles_and_duplicate_titles_require_unique_evidence():
    first = candidate('one', employer='Northstar', title='Backend Engineer')
    second = candidate('two', employer='Northstar', title='Frontend Engineer')
    assert supported_selection([first, second], 'one', 'Northstar update', 'We received your application.') is None
    assert supported_selection([first, second], 'one', 'Northstar update', 'Backend Engineer application received.') == 'one'
    duplicate = replace(second, title=first.title)
    assert supported_selection([first, duplicate], 'one', 'Northstar update', 'Backend Engineer application received.') is None
    assert supported_selection([first, duplicate], 'one', 'Northstar update', 'Posting job-one application received.') == 'one'


def test_conflicting_employer_overrides_posting_id_and_incidental_company_mention():
    wrong = candidate(employer='Other Company')
    assert not supported_candidates([wrong], 'Thanks for applying to Northstar',
        'Previously employed at Other Company. Posting job-app-1.')
    assert not supported_candidates([candidate(employer='North')], 'Thanks for applying to Northstar', '')
    assert supported_candidates([candidate(employer='northstarinc')], 'Thanks for applying to Northstar, Inc.', '')


def test_another_role_at_the_same_company_does_not_use_the_only_known_application():
    item = candidate(employer='Northstar', title='Backend Engineer')
    assert not supported_candidates([item], 'Thanks for applying to Northstar',
        'Thanks for taking the time to apply for the Frontend Engineer role at Northstar.')
    assert supported_selection([item], item.application_id, 'Application job-app-1', 'Received.') == item.application_id


def test_generic_recruiting_language_does_not_invent_conflicting_identity():
    item = candidate(employer='Northstar', title='Software Engineer')
    assert supported_candidates([item], 'Your application at Northstar',
        'We cannot move forward with your application at this time.') == (item,)
    assert supported_candidates([item], 'Your application to Northstar',
        'Apply for future openings that you feel align with your experience and motivation. '
        'Thanks for applying for a role with us.') == (item,)
    assert supported_candidates([item], 'Thanks for applying to the Software Engineer role at Northstar',
        'We received your application.') == (item,)
    assert not supported_candidates([item], 'Thanks for applying to Other Employer',
        'We cannot move forward with your application at this time.')


def test_keyword_free_reply_can_keep_a_reviewed_thread_but_not_a_shared_sender():
    item = candidate()
    assert not supported_candidates([replace(item, match_context='sender previously linked to application')], 'Re: Hello', 'Tuesday works.')
    assert supported_selection([replace(item, match_context='previously linked email conversation')],
        item.application_id, 'Re: Hello', 'Tuesday works.') == item.application_id


def test_truncated_candidate_context_never_preselects_a_model_match():
    mail = sanitize_mail('Example Labs update', 'We have received your application for Software Engineer.')
    class Classifier:
        def classify(self, *_): return model_output(mail)
    proposal = analyze_mail(evidence_id='e', sender_address='recruiter@example.test', mail=mail,
        candidates=[candidate()], classifier=Classifier(), model_version='fixture', candidate_context_complete=False)
    assert proposal.proposed_application_id is None


def test_email_before_browser_capture_stays_reviewable_then_accepts_late_application_once():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'ledger.db'
        ledger, state, mail = JobSearchLedger(path), SQLiteOutlookState(path), FakeMail()
        wrong = start(ledger, 'Other Company', 'wrong')
        prior = ledger.lifecycle.observe_mail(dict(account_id='personal', immutable_message_id='prior',
            conversation_id='conversation-1', direction='inbound', subject='Previous conversation',
            received_at='2026-09-01T11:00:00Z', modified_at='2026-09-01T11:00:00Z'), MutationContext('prior', 'system', 'outlook_sync'))['observation']
        ledger.lifecycle.link_mail(dict(observation_id=prior['observation_id'], application_id=wrong), ctx('prior-link'))
        subject = 'Thanks for applying to Northstar'
        state.stage_changes('personal', 'inbox', [replace(change(), subject=subject, sender_address='no-reply@ashbyhq.com')])
        mail.bodies['message-1'] = dict(subject=subject, receivedDateTime='2026-09-01T12:00:00Z',
            conversationId='conversation-1', body={'contentType':'text', 'content':
            'Thank you for the time spent applying for the Forward Deployed Engineer role at Northstar. '
            'This confirms our team has your application.'})
        classifier = WrongCompanyClassifier()
        class ArchiveProbe:
            def ingest(self, **kwargs):
                assert not kwargs['candidates'] and not kwargs['analyze_temporal']
                return None
        coordinator = OutlookMailCoordinator(mail, state, ledger, classifier=classifier, secure_ingestor=ArchiveProbe())
        result = coordinator.process_pending()
        assert result.processed == result.proposed == 1 and result.auto_applied == 0
        item, = ledger.list_attention_items()
        assert item['application_id'] is None and item['candidate_application_ids'] == []
        try:
            ledger.decide_event_proposal(item['id'], 'accepted', wrong, 'wrong company', ctx('wrong'))
            raise AssertionError('accepted unrelated application')
        except ContractError:
            pass
        right = start(ledger, 'Northstar', 'right')
        refreshed, = ledger.list_attention_items()
        assert refreshed['id'] == item['id'] and refreshed['application_id'] is None
        assert refreshed['candidate_application_ids'] == [right]
        accepted = ledger.decide_event_proposal(item['id'], 'accepted', right, 'reviewed late arrival', ctx('accept'))
        assert ledger.decide_event_proposal(item['id'], 'accepted', right, 'reviewed late arrival', ctx('accept')) == accepted
        assert accepted['event']['occurred_at'] == '2026-09-01T12:00:00Z'
        assert ledger.get_application_timeline(right)['application']['current_phase'] == 'active'
        assert ledger.get_application_timeline(wrong)['application']['current_phase'] == 'awaiting_confirmation'
        assert not ledger.list_attention_items()
        assert coordinator.process_pending().processed == 0 and classifier.calls == 1
        with connect(path) as con:
            assert con.execute('SELECT count(*) FROM event_proposals').fetchone()[0] == 1
            assert con.execute('SELECT count(*) FROM lifecycle_mail_links WHERE application_id=?', (right,)).fetchone()[0] == 1


def test_repaired_association_is_authoritative_for_future_thread_matching():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        wrong = start(ledger, 'Other Company', 'wrong')
        right = start(ledger, 'Northstar', 'right')
        mail = sanitize_mail('Thanks for applying to Northstar', 'We have received your application.')
        evidence = ledger.record_mail_evidence(dict(account_id='personal', immutable_message_id='old',
            conversation_id='thread', sender='no-reply@ashbyhq.com', subject=mail.subject,
            received_at='2026-09-01T12:00:00Z', body_sha256=mail.content_sha256, excerpt=mail.text), ctx('evidence'))['evidence']
        # Reproduce an already-reviewed legacy misassociation, then use the public repair API.
        quote = 'We have received your application'
        proposal = build_proposal(evidence_id=evidence['evidence_id'], mail=mail,
            candidates=[candidate(wrong, employer='Other Company')], application_id=wrong,
            event_type=ApplicationEventType.SUBMISSION_CONFIRMED, producer_kind=ProducerKind.MODEL,
            producer_version='legacy', confidence=.99, evidence_quote=quote,
            span_start=mail.text.index(quote), span_end=mail.text.index(quote)+len(quote))
        saved = ledger.create_event_proposal(proposal, ctx('legacy'))['proposal']
        ledger.decide_event_proposal(saved['proposal_id'], 'accepted', wrong, 'legacy decision', ctx('old-decision'))
        correction = ledger.lifecycle.propose_correction(wrong, 'association', dict(
            evidence_id=evidence['evidence_id'], target_application_id=right,
            target_phase='awaiting_confirmation', reason='Wrong company'), ctx('correction'))['proposal']
        ledger.lifecycle.decide_correction(correction['proposal_id'], 'accepted', ctx('repair'))
        candidates = {row['application_id']: row for row in ledger.list_mail_candidates(
            account_id='personal', conversation_id='thread', sender='no-reply@ashbyhq.com')}
        assert candidates[right]['same_conversation']
        assert not candidates[wrong]['same_conversation']
        assert not candidates[wrong]['same_sender']
        assert not ledger.lifecycle.list_application_conversation(wrong)['items']
        assert len(ledger.lifecycle.list_application_conversation(right)['items']) == 1


if __name__ == '__main__':
    tests = [value for name, value in list(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
    print(f'ok ({len(tests)} mail identity tests)')
