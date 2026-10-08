"""Offline suggested actions, full-message matching, and decision validation."""
import tempfile
from unittest.mock import patch

from job_search.contracts import (
    ApplicationEventType, ContractError, EventProposalInput, JobSnapshot,
    MutationContext, ProducerKind, RecommendationProvenance,
)
from job_search.db import connect
from job_search.mail.archive_source import EncryptedArchiveMailSource
from job_search.review_messages import review_message
from job_search.review_recommendations import enrich_review_items, review_job_snapshot
from tests.test_job_search_mail_archive_source import archive_fixture, save_message


STAMP = '2026-10-01T12:00:00Z'


def context(key, actor='system'):
    return MutationContext(key, actor, 'review_recommendation_test')


def application(ledger, key, employer='Example Co', title='Platform Engineer'):
    return ledger.start_application(
        JobSnapshot('external', key, '', title, employer, '', ''),
        RecommendationProvenance(), context('application-' + key, 'user'),
    )['application']['application_id']


def evidence(ledger, archive, key, subject, body, excerpt='Please review this update.'):
    archived = save_message(archive, key, subject, body)
    result = ledger.record_mail_evidence({
        'account_id': 'outlook-personal', 'immutable_message_id': 'graph-message-' + key,
        'conversation_id': 'conversation-' + key,
        'sender': 'no-reply@shared-ats.test', 'subject': subject, 'received_at': STAMP,
        'body_sha256': 'c' * 64, 'excerpt': excerpt,
    }, context('evidence-' + key))['evidence']
    observed = ledger.lifecycle.observe_mail({
        'account_id': 'outlook-personal', 'immutable_message_id': 'graph-message-' + key,
        'conversation_id': 'conversation-' + key, 'direction': 'inbound',
        'sender': 'no-reply@shared-ats.test', 'subject': subject, 'received_at': STAMP,
        'archive_id': archived['archive_id'], 'evidence_id': result['evidence_id'],
    }, context('observe-' + key))['observation']
    return result, observed


def discovery(ledger, archive, key, subject, body, **kwargs):
    _, observed = evidence(ledger, archive, key, subject, body, **kwargs)
    saved = ledger.lifecycle.propose_discovery({'observation_id': observed['observation_id']},
                                              context('discover-' + key))['discovery']
    return next(item for item in ledger.lifecycle.list_lifecycle_reviews()
                if item['id'] == saved['discovery_id'])


def event(ledger, archive, key, subject, body, application_id=None, candidate_ids=None, payload=None,
          event_type=ApplicationEventType.REJECTION_RECEIVED, **kwargs):
    saved, _ = evidence(ledger, archive, key, subject, body, **kwargs)
    quote = saved['excerpt']
    proposal = ledger.create_event_proposal(EventProposalInput(
        saved['evidence_id'], application_id, event_type,
        ProducerKind.MODEL, 'test-v1', 0.8,
        candidate_ids if candidate_ids is not None else [application_id] if application_id else [],
        quote, 0, len(quote), payload or {}, 'review-' + key,
    ), context('proposal-' + key, 'model'))['proposal']
    return next(item for item in ledger.list_attention_items() if item['id'] == proposal['proposal_id'])


def test_discovery_matches_role_late_in_full_body_without_writing_or_returning_plaintext():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, archive = archive_fixture(directory)
        target = application(ledger, 'platform-role')
        application(ledger, 'designer-role', title='Product Designer')
        marker = 'PRIVATE FULL MESSAGE MARKER'
        item = discovery(ledger, archive, 'late-role', 'Application update',
                         ('Generic introduction. ' * 180) + '\nExample Co\nPlatform Engineer\n' + marker)
        source = EncryptedArchiveMailSource(ledger, archive)
        with connect(path) as con:
            before = con.execute('SELECT COUNT(*) FROM application_events').fetchone()[0]
        result = enrich_review_items(ledger, source, [item])[0]
        suggestion = result['suggested_resolution']
        assert suggestion['application_id'] == target
        assert suggestion['action'] == 'link' and suggestion['confidence'] == 'high'
        assert not suggestion['requires_selection']
        assert 'role title' in suggestion['explanation']
        assert 'suggested_resolution' not in item  # Input is not mutated.
        assert marker not in str(result)
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM application_events').fetchone()[0] == before
            assert con.execute('SELECT COUNT(*) FROM lifecycle_mail_links').fetchone()[0] == 0
        assert marker.encode() not in path.read_bytes()


def test_subject_and_exact_posting_id_disambiguate_same_employer_roles():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        target = application(ledger, 'platform-123')
        application(ledger, 'platform-1234')
        item = discovery(ledger, archive, 'posting-id', 'Example Co / platform-123', 'Application update.')
        result = enrich_review_items(ledger, EncryptedArchiveMailSource(ledger, archive), [item])[0]
        assert result['suggested_resolution']['application_id'] == target


def test_tied_supported_roles_preselect_a_low_confidence_default_but_unrelated_roles_do_not():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        first = application(ledger, 'platform-one')
        second = application(ledger, 'platform-two')
        source = EncryptedArchiveMailSource(ledger, archive)
        ambiguous = discovery(ledger, archive, 'ambiguous', 'Example Co', 'Platform Engineer update.')
        generic = discovery(ledger, archive, 'generic', 'Platform Engineer', 'Please choose an interview time.')
        results = enrich_review_items(ledger, source, [ambiguous, generic])
        assert results[0]['suggested_resolution']['application_id'] == min(first, second)
        assert not results[0]['suggested_resolution']['requires_selection']
        assert results[0]['suggested_resolution']['confidence'] == 'low'
        assert results[0]['suggested_resolution']['explanation'].startswith('Suggested match; check')
        assert len(results[0]['application_matches']) == 2
        assert results[1]['suggested_resolution']['application_id'] is None
        assert results[1]['suggested_resolution']['requires_selection']
        assert results[1]['application_matches'] == []


def test_partial_role_wording_preselects_closest_supported_application():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        target = application(ledger, 'role-101', title='Senior Python Platform Engineer')
        other = application(ledger, 'role-202', title='Software Engineer')
        item = event(ledger, archive, 'partial-role', 'Example Co update',
                     'We reviewed your experience with Python platform development.')
        source = EncryptedArchiveMailSource(ledger, archive)
        result = enrich_review_items(ledger, source, [item])[0]
        assert set(result['candidate_application_ids']) == {target, other}
        assert result['suggested_resolution']['application_id'] == target
        assert result['suggested_resolution']['confidence'] == 'medium'
        assert not result['suggested_resolution']['requires_selection']
        # The default does not remove another supported choice from manual review.
        assert ledger.decide_event_proposal(
            item['id'], 'accepted', other, '', context('partial-role-override', 'user'),
            review_mail_content=review_message(ledger, source, item),
        )['decision'] == 'accepted'


def test_many_ambiguous_applications_use_one_exact_identity_pass():
    from job_search.mail.identity import supported_candidates
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        applications = [application(ledger, f'role-{number:03}') for number in range(60)]
        item = discovery(ledger, archive, 'many-roles', 'Example Co',
                         'Background information. ' * 200 + '\nPlatform Engineer')
        with patch('job_search.mail.identity.supported_candidates', wraps=supported_candidates) as disambiguate:
            result = enrich_review_items(ledger, EncryptedArchiveMailSource(ledger, archive), [item])[0]
        assert result['suggested_resolution']['application_id'] == min(applications)
        assert result['suggested_resolution']['confidence'] == 'low'
        assert len(result['application_matches']) == len(applications)
        assert disambiguate.call_count == 1


def test_conflicting_explicit_employer_excludes_incidental_company_and_job_mentions():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        application(ledger, 'wrong-job-123', employer='Other Company')
        target = application(ledger, 'right-job-456')
        item = discovery(ledger, archive, 'conflict', 'Your application to Example Co',
                         'We reviewed your Platform Engineer application.\nOther Company wrong-job-123 is a partner.')
        result = enrich_review_items(ledger, EncryptedArchiveMailSource(ledger, archive), [item])[0]
        assert result['suggested_resolution']['application_id'] == target
        assert [match['application_id'] for match in result['application_matches']] == [target]


def test_unassigned_event_expands_full_body_candidates_and_revalidates_decision():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, archive = archive_fixture(directory)
        target = application(ledger, 'platform-target')
        unrelated = application(ledger, 'other-job', employer='Unrelated Company')
        body = 'Please review this update.\n' + ('Background information. ' * 160) + '\nExample Co\nPlatform Engineer'
        item = event(ledger, archive, 'full-event', 'Application update', body)
        assert item['candidate_application_ids'] == []
        source = EncryptedArchiveMailSource(ledger, archive)
        result = enrich_review_items(ledger, source, [item])[0]
        assert result['candidate_application_ids'] == [target]
        assert result['suggested_resolution']['application_id'] == target
        assert result['suggested_resolution']['label'] == 'Record rejection'
        content = review_message(ledger, source, item)
        for selected, supplied_content in ((unrelated, content), (target, None)):
            try:
                ledger.decide_event_proposal(item['id'], 'accepted', selected, '',
                                            context('wrong-' + selected, 'user'), review_mail_content=supplied_content)
            except ContractError:
                pass
            else:
                raise AssertionError('Unsupported application accepted')
        result = ledger.decide_event_proposal(item['id'], 'accepted', target, '',
                                             context('right-target', 'user'), review_mail_content=content)
        assert result['decision'] == 'accepted'
        assert ledger.get_application_timeline(target)['application']['terminal_outcome'] == 'rejected'
        assert body.encode() not in path.read_bytes()
        # Same command still replays if the archive subsequently becomes unavailable.
        assert ledger.decide_event_proposal(item['id'], 'accepted', target, '',
                                           context('right-target', 'user')) == result


def test_locked_archive_falls_back_to_excerpt_and_does_not_disclose_provider_errors():
    class Locked:
        def get_review_message(self, archive_id):
            raise RuntimeError('PRIVATE PROVIDER FAILURE')

    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        target = application(ledger, 'fallback-target')
        item = event(ledger, archive, 'fallback', 'Application update', 'Archived details',
                     excerpt='Example Co Platform Engineer update.')
        result = enrich_review_items(ledger, Locked(), [item])[0]
        assert result['suggested_resolution']['application_id'] == target
        assert 'PRIVATE PROVIDER FAILURE' not in str(result)
        content = review_message(ledger, Locked(), item)
        assert ledger.decide_event_proposal(item['id'], 'accepted', target, '',
                                           context('fallback-accept', 'user'), review_mail_content=content)['decision'] == 'accepted'


def test_existing_selection_and_failure_actions_remain_usable():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        target = application(ledger, 'already-selected')
        item = event(ledger, archive, 'assigned', 'Application update', 'Please review this update.', target)
        results = enrich_review_items(ledger, None, [item,
            {'id': 'retry', 'kind': 'mail_processing_failure', 'can_retry': True},
            {'id': 'removed', 'kind': 'mail_processing_failure', 'can_retry': False},
        ])
        assert results[0]['suggested_resolution']['application_id'] == target
        assert not results[0]['suggested_resolution']['requires_selection']
        assert results[1]['suggested_resolution']['action'] == 'retry'
        assert results[2]['suggested_resolution']['action'] == 'dismiss'


def test_explicit_identity_conflict_does_not_recommend_saved_assigned_application():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        wrong = application(ledger, 'wrong-assigned', employer='Other Company')
        item = event(ledger, archive, 'assigned-conflict', 'Application update',
                     'Your application to Example Co\nPlatform Engineer', wrong)
        result = enrich_review_items(ledger, EncryptedArchiveMailSource(ledger, archive), [item])[0]
        assert result['application_id'] == wrong  # Saved proposal remains unchanged.
        assert result['suggested_resolution']['application_id'] is None
        assert result['suggested_resolution']['requires_selection']
        assert 'different employer or role' in result['suggested_resolution']['explanation']


def test_stronger_full_body_role_and_posting_id_override_saved_choice_within_candidates():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        assigned = application(ledger, 'role-id-engineer')
        target = application(ledger, 'role-id-designer', title='Product Designer')
        item = event(ledger, archive, 'stronger-match', 'Application update',
                     'Example Co\nProduct Designer\nrole-id-designer', assigned,
                     candidate_ids=[assigned, target])
        source = EncryptedArchiveMailSource(ledger, archive)
        result = enrich_review_items(ledger, source, [item])[0]
        assert result['application_id'] == assigned
        assert result['suggested_resolution']['application_id'] == target
        assert not result['suggested_resolution']['requires_selection']
        assert ledger.decide_event_proposal(
            item['id'], 'accepted', target, '', context('stronger-match-accept', 'user'),
            review_mail_content=review_message(ledger, source, item),
        )['decision'] == 'accepted'


def test_review_suggests_unsubmitted_tracked_application_without_inventing_submission():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        target = ledger.start_application(
            JobSnapshot('ashby', 'unsubmitted-target', '', 'Platform Engineer', 'Example Co', '',
                        'https://jobs.example.test/unsubmitted-target'),
            RecommendationProvenance(), context('unsubmitted-target', 'user'),
        )['application']['application_id']
        unrelated = ledger.start_application(
            JobSnapshot('ashby', 'unsubmitted-unrelated', '', 'Product Designer', 'Unrelated Co', '',
                        'https://jobs.example.test/unsubmitted-unrelated'),
            RecommendationProvenance(), context('unsubmitted-unrelated', 'user'),
        )['application']['application_id']
        # These records remain outside the automatic classifier's candidate set.
        assert ledger.list_mail_candidates() == []
        item = event(ledger, archive, 'unsubmitted-review', 'Application update',
                     'Example Co\nPlatform Engineer\nWe are unable to move forward.')
        source = EncryptedArchiveMailSource(ledger, archive)
        recommended = enrich_review_items(ledger, source, [item])[0]
        assert recommended['candidate_application_ids'] == [target]
        assert recommended['suggested_resolution']['application_id'] == target
        assert recommended['suggested_resolution']['label'] == 'Record rejection'
        assert ledger.list_mail_candidates() == []
        before = ledger.get_application_timeline(target)
        assert before['application']['submitted_at'] is None
        assert before['application']['current_phase'] == 'preparing'
        assert [row['event_type'] for row in before['events']] == ['application_started']
        content = review_message(ledger, source, item)
        try:
            ledger.decide_event_proposal(item['id'], 'accepted', unrelated, '',
                                        context('unsubmitted-unrelated-decision', 'user'), review_mail_content=content)
        except ContractError:
            pass
        else:
            raise AssertionError('Unrelated unsubmitted application was accepted')
        ledger.decide_event_proposal(item['id'], 'accepted', target, '',
                                    context('unsubmitted-target-decision', 'user'), review_mail_content=content)
        after = ledger.get_application_timeline(target)
        assert after['application']['submitted_at'] is None
        assert after['application']['terminal_outcome'] == 'rejected'
        assert [row['event_type'] for row in after['events']] == ['application_started', 'rejection_received']


class Catalog:
    job = dict(ats='ashby', id='catalog-role-101', company='Example Co', title='Platform Engineer',
               jobUrl='https://jobs.example.test/catalog-role-101', closed_at=None,
               match_confidence='high', match_reason='Company and role named in email.')

    def review_candidates(self, subject, body, limit=20):
        assert 'Example Co' in subject + body
        return [self.job]

    def get_job(self, ats, identity):
        assert (ats, identity) == (self.job['ats'], self.job['id'])
        return self.job


def catalog_selection():
    return {key: Catalog.job[key] for key in ('ats', 'id')}


def test_catalog_only_event_suggestion_creates_application_only_on_click_and_replays_once():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        item = event(ledger, archive, 'catalog-event', 'Application update', 'Example Co\nPlatform Engineer')
        source = EncryptedArchiveMailSource(ledger, archive)
        result = enrich_review_items(ledger, source, [item], jobs=Catalog())[0]
        assert ledger.list_applications() == []
        assert result['suggested_resolution']['application_id'] is None
        assert result['suggested_resolution']['job']['id'] == Catalog.job['id']
        assert result['suggested_resolution']['label'] == 'Record rejection'
        assert not result['suggested_resolution']['requires_selection']
        assert result['job_matches'][0]['explanation'].startswith(Catalog.job['match_reason'])
        content = review_message(ledger, source, item)
        snapshot = review_job_snapshot(Catalog(), catalog_selection(), content)
        result = ledger.decide_event_proposal(
            item['id'], 'accepted', None, '', context('catalog-event-accept', 'user'),
            review_mail_content=content, review_job_snapshot=snapshot,
        )
        applications = ledger.list_applications()
        assert len(applications) == 1
        assert applications[0]['job_id'] == Catalog.job['id']
        assert applications[0]['submitted_at'] is None
        assert applications[0]['terminal_outcome'] == 'rejected'
        timeline = ledger.get_application_timeline(applications[0]['application_id'])
        assert [row['event_type'] for row in timeline['events']] == ['application_started', 'rejection_received']
        assert ledger.decide_event_proposal(
            item['id'], 'accepted', None, '', context('catalog-event-accept', 'user'),
            review_mail_content=content, review_job_snapshot=snapshot,
        ) == result
        assert len(ledger.list_applications()) == 1
        assert len(ledger.get_application_timeline(applications[0]['application_id'])['events']) == 2


def test_catalog_event_reuses_application_that_appears_after_suggestion():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        item = event(ledger, archive, 'catalog-race', 'Application update', 'Example Co\nPlatform Engineer')
        source = EncryptedArchiveMailSource(ledger, archive)
        assert enrich_review_items(ledger, source, [item], jobs=Catalog())[0]['job_matches']
        content = review_message(ledger, source, item)
        snapshot = review_job_snapshot(Catalog(), catalog_selection(), content)
        existing = ledger.start_application(snapshot, RecommendationProvenance(),
                                            context('catalog-race-start', 'user'))['application']['application_id']
        result = ledger.decide_event_proposal(
            item['id'], 'accepted', None, '', context('catalog-race-accept', 'user'),
            review_mail_content=content, review_job_snapshot=snapshot,
        )
        assert result['event']['application_id'] == existing
        assert len(ledger.list_applications()) == 1


def test_catalog_acceptance_rejects_unrelated_job_and_rolls_back_failed_event():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, archive = archive_fixture(directory)
        item = event(ledger, archive, 'catalog-invalid', 'Your application to Example Co',
                     'Platform Engineer', payload={'occurred_at': 'invalid timestamp'})
        source = EncryptedArchiveMailSource(ledger, archive)
        content = review_message(ledger, source, item)
        valid = review_job_snapshot(Catalog(), catalog_selection(), content)
        invalid = JobSnapshot('ashby', 'unrelated-role-202', '', 'Designer', 'Other Company', '',
                              'https://jobs.example.test/unrelated')
        for name, snapshot in [('unrelated', invalid), ('bad-event', valid)]:
            try:
                ledger.decide_event_proposal(
                    item['id'], 'accepted', None, '', context('catalog-invalid-' + name, 'user'),
                    review_mail_content=content, review_job_snapshot=snapshot,
                )
            except ContractError:
                pass
            else:
                raise AssertionError('Invalid catalog decision succeeded')
            assert ledger.list_applications() == []
            with connect(path) as con:
                assert con.execute('SELECT COUNT(*) FROM application_events').fetchone()[0] == 0
                assert con.execute('SELECT COUNT(*) FROM lifecycle_mail_links').fetchone()[0] == 0
                assert con.execute('SELECT status FROM event_proposals WHERE proposal_id=?', (item['id'],)).fetchone()[0] == 'pending'


def test_catalog_discovery_creates_then_reuses_linked_record_without_submission():
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, archive = archive_fixture(directory)
        first = discovery(ledger, archive, 'catalog-discovery-one', 'Application update', 'Example Co\nPlatform Engineer')
        second = discovery(ledger, archive, 'catalog-discovery-two', 'Application update', 'Example Co\nPlatform Engineer')
        source = EncryptedArchiveMailSource(ledger, archive)
        suggestion = enrich_review_items(ledger, source, [first], jobs=Catalog())[0]['suggested_resolution']
        assert suggestion['action'] == 'link_job'
        assert not suggestion['requires_selection']
        assert ledger.list_applications() == []
        snapshot = review_job_snapshot(Catalog(), catalog_selection(), review_message(ledger, source, first))
        payload = dict(discovery_id=first['id'], decision='link_job', selected_job=catalog_selection())
        try:
            ledger.lifecycle.decide_discovery({**payload, 'selected_job': {'ats': 'lever', 'id': 'wrong'}},
                                             context('catalog-discovery-wrong', 'user'), review_job_snapshot=snapshot)
        except ContractError:
            pass
        else:
            raise AssertionError('Mismatched selected job identity accepted')
        first_result = ledger.lifecycle.decide_discovery(payload, context('catalog-discovery-first', 'user'),
                                                        review_job_snapshot=snapshot)
        assert ledger.lifecycle.decide_discovery(payload, context('catalog-discovery-first', 'user'),
                                                review_job_snapshot=snapshot) == first_result
        second_result = ledger.lifecycle.decide_discovery({**payload, 'discovery_id': second['id']},
                                                         context('catalog-discovery-second', 'user'),
                                                         review_job_snapshot=snapshot)
        assert second_result['application_id'] == first_result['application_id']
        assert len(ledger.list_applications()) == 1
        timeline = ledger.get_application_timeline(first_result['application_id'])
        assert timeline['application']['submitted_at'] is None
        assert [row['event_type'] for row in timeline['events']] == ['application_started']
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM lifecycle_mail_links').fetchone()[0] == 2


def test_confirmation_without_browser_capture_suggests_exact_catalog_role_and_records_on_approval():
    from tests.test_review_catalog import dated_catalog
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, archive = archive_fixture(directory)
        title = 'Software Engineer, Early Careers AI/UI'
        jobs = dated_catalog(directory, [
            ('greenhouse', 'exact-job', 'streambox', title, None),
            ('greenhouse', 'similar-job', 'streambox', 'Software Engineer, Early Careers focused on AI and UI', None),
        ], [('2026-09-30T00:00:00Z', None, None, 'exact-job'),
            ('2026-09-15T00:00:00Z', None, None, 'similar-job')])
        body = 'Thank you for applying to the ' + title + ' role!'
        item = event(ledger, archive, 'missing-capture', 'Thank you for applying to Streambox', body,
                     event_type=ApplicationEventType.SUBMISSION_CONFIRMED, excerpt=body)
        source = EncryptedArchiveMailSource(ledger, archive)
        result = enrich_review_items(ledger, source, [item], jobs=jobs)[0]
        suggestion = result['suggested_resolution']
        assert suggestion['job']['id'] == 'exact-job'
        assert suggestion['confidence'] == 'high' and not suggestion['requires_selection']
        assert [row['id'] for row in result['job_matches']] == ['exact-job', 'similar-job']
        assert ledger.list_applications() == [] and ledger.list_mail_candidates() == []
        content = review_message(ledger, source, item)
        snapshot = review_job_snapshot(jobs, {'ats': 'greenhouse', 'id': 'exact-job'}, content)
        decision_context = context('confirm-missing-capture', 'user')
        accepted = ledger.decide_event_proposal(item['id'], 'accepted', None, 'Exact company and role match',
            decision_context, review_mail_content=content, review_job_snapshot=snapshot)
        application_id = accepted['event']['application_id']
        timeline = ledger.get_application_timeline(application_id)
        assert timeline['application']['job_id'] == 'exact-job'
        assert timeline['application']['job_url_snapshot'] == 'https://example.test/exact-job'
        assert timeline['application']['confirmed_at'] == STAMP
        assert [row['event_type'] for row in timeline['events']] == ['application_started', 'submission_confirmed']
        assert ledger.decide_event_proposal(item['id'], 'accepted', None, 'Exact company and role match',
            decision_context, review_mail_content=content, review_job_snapshot=snapshot) == accepted
        with connect(path) as con:
            assert con.execute('SELECT count(*) FROM browser_attempts').fetchone()[0] == 0
            assert con.execute('SELECT count(*) FROM lifecycle_mail_links WHERE application_id=?', (application_id,)).fetchone()[0] == 1
        assert ledger.list_attention_items() == []


if __name__ == '__main__':
    tests = [value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]
    for test in tests:
        test()
    print(f'ok ({len(tests)} review recommendation tests)')
