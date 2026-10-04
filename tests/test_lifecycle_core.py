"""Offline lifecycle task/detail/review regression coverage."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from job_search.contracts import (ApplicationEventType, ContractError, ConflictError,
    EventInput, EventProposalInput, ProducerKind)
from job_search.db import connect
from job_search.lifecycle.core import CoreMixin
from job_search.notifications import NotificationPolicy
from tests.test_job_search_ledger import make_service, start, context, stamp


def setup(directory):
    path, ledger = make_service(directory)
    core = CoreMixin()
    core.store = ledger.store
    core.ledger = ledger
    application_id = start(ledger)['application']['application_id']
    return path,ledger,core,application_id


def raises(error,fn):
    try:
        fn()
    except error:
        return
    raise AssertionError('expected '+error.__name__)


def evidence(ledger,app,key='evidence',received='2026-01-01T00:00:00Z',event_type=ApplicationEventType.ASSESSMENT_REQUESTED):
    saved = ledger.record_mail_evidence(dict(account_id='account',immutable_message_id=key,
        sender='recruiter@example.test',subject='Next step',received_at=received,
        body_sha256='a'*64,excerpt='Please complete this assessment.'),context(key,'system','outlook_sync'))
    eid = saved['evidence']['evidence_id']
    result = ledger.create_event_proposal(EventProposalInput(evidence_id=eid,
        proposed_application_id=app,event_type=event_type,producer_kind=ProducerKind.RULE,
        producer_version='test-v1',confidence=1,candidate_application_ids=[app],
        evidence_quote='complete this assessment',span_start=7,span_end=31,
        payload={},dedupe_key=key+'-proposal'),context(key+'-proposal','system','outlook_sync'))
    return eid,result['proposal']['proposal_id']


def test_tasks_audited_idempotent_actor_enforced_and_snoozable():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        values = dict(kind='send_availability',owner='applicant',note='Send three times',due_at=stamp(86400))
        raises(ContractError,lambda:core.create_task(app,values,context('denied','hermes')))
        first = core.create_task(app,values,context('task'))['task']
        assert core.create_task(app,values,context('task'))['task'] == first
        raises(ConflictError,lambda:core.create_task(app,{**values,'owner':'employer'},context('task')))
        core.transition_task(first['task_id'],'snooze',{'snoozed_until':stamp(172800)},context('snooze'))
        core.transition_task(first['task_id'],'complete',{},context('complete'))
        assert core.list_tasks(app)[0]['status'] == 'completed'
        assert len(core.task_history(first['task_id'])) == 3
        raises(ConflictError,lambda:core.transition_task(first['task_id'],'cancel',{},context('cancel')))
        with connect(path) as con:
            raises(sqlite3.IntegrityError,lambda:con.execute('DELETE FROM lifecycle_task_revisions'))


def test_task_supersession_and_cross_application_evidence_rejected():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        other = start(ledger,'job-2','start-2')['application']['application_id']
        eid,_ = evidence(ledger,app)
        raises(ContractError,lambda:core.create_task(other,dict(kind='reply',evidence_id=eid),context('wrong')))
        first = core.create_task(app,dict(kind='reply',evidence_id=eid),context('first'))['task']
        replacement = core.create_task(app,dict(kind='reply',supersedes_task_id=first['task_id']),context('replace'))['task']
        assert replacement['supersedes_task_id'] == first['task_id']
        assert {row['status'] for row in core.list_tasks(app)} == {'open','superseded'}


def test_late_review_uses_mail_receipt_and_creates_single_task():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        eid,proposal = evidence(ledger,app)
        result = ledger.decide_event_proposal(proposal,'accepted',app,'reviewed',context('approve'))
        assert result['event']['occurred_at'] == '2026-01-01T00:00:00Z'
        assert result['event']['recorded_at'] != result['event']['occurred_at']
        ledger.decide_event_proposal(proposal,'accepted',app,'reviewed',context('approve'))
        tasks = core.list_tasks(app)
        assert len(tasks) == 1 and tasks[0]['evidence_id'] == eid


def test_auto_apply_and_review_have_same_event_date():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        _,proposal = evidence(ledger,app)
        applied = ledger.auto_apply_event_proposal(proposal,context('auto','system'))
        assert applied['event']['occurred_at'] == '2026-01-01T00:00:00Z'
        assert len(core.list_tasks(app)) == 1


def test_detail_offer_revisions_and_terminal_cleanup_are_atomic():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        first = core.record_detail(app,'offer',dict(status='offered',title='Offer one',due_at=stamp(86400),terms={'base':100000}),context('offer'))['detail']
        next_ = core.record_detail(app,'offer',dict(status='negotiating',terms={'base':120000},due_at=stamp(172800),expected_revision_no=first['revision_no']),context('revision'),first['detail_id'])['detail']
        assert next_['revision_no'] == 2
        assert sum(task['status']=='open' for task in core.list_tasks(app)) == 1
        ledger.create_reminder(dict(application_id=app,due_at=stamp(86400),note='Review offer'),context('reminder'))
        final = core.record_detail(app,'offer',dict(status='employer_withdrawn',expected_revision_no=next_['revision_no']),context('withdrawn'),first['detail_id'])['detail']
        assert final['details']['terms'] == {'base':120000}
        assert len(core.detail_history(first['detail_id'])) == 3
        assert ledger.get_application_timeline(app)['application']['terminal_outcome'] == 'rejected'
        assert all(task['status'] != 'open' for task in core.list_tasks(app))
        assert core.list_unified_reminders(app)[0]['status'] == 'cancelled'
        raises(ConflictError,lambda:core.create_task(app,dict(kind='reply'),context('terminal-task')))


def test_assessment_completion_is_separate_from_reminder_delivery():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        saved = core.record_detail(app,'assessment',dict(status='requested',title='Take home',due_at=stamp(5)),context('assessment'))['detail']
        now = stamp(10)
        published = core.publish_due_tasks(now,context('publish','system'))
        assert published['published'] == 1
        assert core.list_tasks(app)[0]['status'] == 'open'
        assert core.publish_due_tasks(now,context('publish-again','system'))['published'] == 0
        core.record_detail(app,'assessment',dict(status='submitted',expected_revision_no=saved['revision_no']),context('submitted'),saved['detail_id'])
        assert core.list_tasks(app)[0]['status'] == 'completed'
        with connect(path) as con:
            assert con.execute("SELECT status FROM notification_outbox WHERE dedupe_key LIKE 'lifecycle-task:%'").fetchone()['status'] == 'cancelled'


def test_due_task_batch_progress_and_disabled_policy():
    with tempfile.TemporaryDirectory() as directory:
        _,_,core,app = setup(directory)
        for i in range(3):
            core.create_task(app,dict(kind='reply',due_at=stamp(5)),context('task-'+str(i)))
        assert core.publish_due_tasks(stamp(10),context('disabled','system'),policy=NotificationPolicy(enabled_topics=frozenset()))['published'] == 0
        for i in range(3):
            assert core.publish_due_tasks(stamp(10),context('batch-'+str(i),'system'),limit=1)['published'] == 1
        assert core.publish_due_tasks(stamp(10),context('empty','system'),limit=1)['published'] == 0


def test_review_proposals_cannot_self_approve_and_reopen_is_explicit():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        proposal = core.propose_correction(app,'task',{'values':{'kind':'reply','owner':'applicant'}},context('proposal','hermes'))['proposal']
        raises(ContractError,lambda:core.decide_correction(proposal['proposal_id'],'accepted',context('self','hermes')))
        core.decide_correction(proposal['proposal_id'],'accepted',context('accepted'))
        assert len(core.list_tasks(app)) == 1
        terminal = core.propose_correction(app,'phase',{'target_phase':'terminal','target_outcome':'withdrawn','reason':'User withdrew'},context('terminal','hermes'))['proposal']
        core.decide_correction(terminal['proposal_id'],'accepted',context('terminal-yes'))
        reopened = core.propose_correction(app,'reopen',{'target_phase':'active','reason':'Recruiter restarted process'},context('reopen','hermes'))['proposal']
        core.decide_correction(reopened['proposal_id'],'accepted',context('reopen-yes'))
        assert ledger.get_application_timeline(app)['application']['current_phase'] == 'active'
        assert core.list_tasks(app)[0]['status'] == 'cancelled'
        assert len(core.list_corrections(app,status='accepted')) == 3


def test_unified_reminder_ids_and_user_cancel():
    with tempfile.TemporaryDirectory() as directory:
        _,ledger,core,app = setup(directory)
        ledger.create_reminder(dict(application_id=app,due_at=stamp(86400),note='Follow up'),context('reminder'))
        reminder = core.list_unified_reminders(app,statuses=['scheduled'])[0]
        assert reminder['reminder_id'].startswith('general:')
        raises(ContractError,lambda:core.cancel_unified_reminder(reminder['reminder_id'],context('bad','hermes')))
        core.cancel_unified_reminder(reminder['reminder_id'],context('cancel'))
        assert not core.list_unified_reminders(app,statuses=['scheduled'])


def test_follow_up_policy_audit_and_validation():
    with tempfile.TemporaryDirectory() as directory:
        path,_,core,app = setup(directory)
        raises(ContractError,lambda:core.configure_follow_up(app,0,context('bad')))
        assert core.configure_follow_up(app,7,context('configure'))['policy_version'] == 1
        assert core.configure_follow_up(app,None,context('disable'))['policy_version'] == 2
        with connect(path) as con:
            assert con.execute('SELECT count(*) FROM lifecycle_follow_up_policy_revisions').fetchone()[0] == 2



def test_stale_task_and_offer_proposals_do_not_overwrite_newer_decisions():
    with tempfile.TemporaryDirectory() as directory:
        _,_,core,app = setup(directory)
        task = core.create_task(app,dict(kind='reply'),context('task'))['task']
        proposal = core.propose_correction(app,'task_transition',dict(task_id=task['task_id'],operation='complete',values={}),context('proposal','hermes'))['proposal']
        core.transition_task(task['task_id'],'snooze',dict(snoozed_until=stamp(86400)),context('snooze'))
        raises(ConflictError,lambda:core.decide_correction(proposal['proposal_id'],'accepted',context('stale')))
        detail = core.record_detail(app,'offer',dict(status='offered'),context('offer'))['detail']
        proposal = core.propose_correction(app,'detail',dict(kind='offer',detail_id=detail['detail_id'],values={'status':'accepted'}),context('offer-proposal','hermes'))['proposal']
        core.record_detail(app,'offer',dict(status='negotiating',expected_revision_no=detail['revision_no']),context('negotiating'),detail['detail_id'])
        raises(ConflictError,lambda:core.decide_correction(proposal['proposal_id'],'accepted',context('stale-offer')))
        assert core.list_details(app)[0]['status'] == 'negotiating'


def test_all_remaining_correction_kinds_preserve_original_facts():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        other = start(ledger,'job-2','start-2')['application']['application_id']
        first_event = ledger.get_application_timeline(app)['events'][0]['event_id']
        corrections = [
            ('supersede_fact',dict(event_id=first_event,target_phase='active',reason='Corrected stage')),
            ('detail',dict(kind='assessment',values=dict(status='requested',title='Skills test'))),
            ('duplicate',dict(target_application_id=other,target_phase='terminal',target_outcome='withdrawn',reason='Duplicate external record')),
        ]
        for index,(kind,payload) in enumerate(corrections):
            proposal = core.propose_correction(app,kind,payload,context('proposal-'+str(index),'hermes'))['proposal']
            core.decide_correction(proposal['proposal_id'],'accepted',context('accept-'+str(index)))
        assert core.list_details(app)[0]['kind'] == 'assessment'
        with connect(path) as con:
            assert con.execute('SELECT 1 FROM application_events WHERE event_id=?',(first_event,)).fetchone()
        assert ledger.get_application_timeline(app)['application']['terminal_outcome'] == 'withdrawn'


def test_association_correction_invalidates_old_evidence_and_tasks():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,_,app = setup(directory)
        core = ledger.lifecycle
        other = start(ledger,'job-2','start-2')['application']['application_id']
        eid,proposal = evidence(ledger,app)
        ledger.decide_event_proposal(proposal,'accepted',app,'reviewed',context('event-accepted'))
        correction = core.propose_correction(app,'association',dict(evidence_id=eid,target_application_id=other,target_phase='preparing',reason='Wrong role'),context('association','hermes'))['proposal']
        core.decide_correction(correction['proposal_id'],'accepted',context('association-accepted'))
        assert core.list_tasks(app)[0]['status'] == 'cancelled'
        assert ledger.get_application_timeline(app)['application']['current_phase'] == 'preparing'
        raises(ContractError,lambda:core.create_task(app,dict(kind='reply',evidence_id=eid),context('stale-evidence')))
        core.create_task(other,dict(kind='reply',evidence_id=eid),context('right-evidence'))
        with connect(path) as con:
            assert con.execute('SELECT count(*) FROM lifecycle_mail_link_history').fetchone()[0] >= 1
            mail = con.execute('SELECT account_id,conversation_id,sender FROM mail_evidence WHERE evidence_id=?',(eid,)).fetchone()
        candidates = ledger.list_mail_candidates(account_id=mail['account_id'], conversation_id=mail['conversation_id'], sender=mail['sender'])
        # Immutable old approvals must not restore an association explicitly repaired.
        assert not any(row['same_conversation'] for row in candidates if row['application_id'] == app)
        if mail['conversation_id']:
            assert next(row for row in candidates if row['application_id'] == other)['same_conversation']


def test_follow_up_uses_latest_communication_and_inbound_cancels_it():
    with tempfile.TemporaryDirectory() as directory:
        _,ledger,_,app = setup(directory)
        core = ledger.lifecycle
        core.configure_follow_up(app,7,context('policy'))
        sent = core.observe_mail(dict(account_id='a',immutable_message_id='sent',direction='outbound',sender='me@example.test',subject='Following up',sent_at=stamp(),modified_at=stamp()),context('sent','system'))['observation']
        core.link_mail(dict(observation_id=sent['observation_id'],application_id=app),context('sent-linked'))
        assert core.evaluate_follow_ups(stamp(),context('evaluate','system'))['created'] == 1
        assert core.evaluate_follow_ups(stamp(),context('again','system'))['created'] == 0
        assert core.list_tasks(app)[0]['owner'] == 'applicant'
        incoming = core.observe_mail(dict(account_id='a',immutable_message_id='inbound',direction='inbound',sender='recruiter@example.test',subject='Next steps',received_at=stamp(60),modified_at=stamp(60)),context('inbound','system'))['observation']
        core.link_mail(dict(observation_id=incoming['observation_id'],application_id=app),context('inbound-linked'))
        assert core.evaluate_follow_ups(stamp(60),context('reply-evaluate','system'))['cancelled'] == 1
        assert not core.list_tasks(app,status='open')


def test_terminal_closure_cancels_already_queued_reminder_notifications():
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        core.create_task(app,dict(kind='reply',due_at=stamp(5)),context('task'))
        core.publish_due_tasks(stamp(10),context('publish','system'))
        proposal = core.propose_correction(app,'phase',dict(target_phase='terminal',target_outcome='withdrawn',reason='Done'),context('close-proposal'))['proposal']
        core.decide_correction(proposal['proposal_id'],'accepted',context('close'))
        with connect(path) as con:
            assert not con.execute("SELECT 1 FROM notification_outbox WHERE topic='reminder.due' AND status IN ('pending','delivering')").fetchone()



def test_association_of_applied_fact_requires_reviewed_old_phase():
    with tempfile.TemporaryDirectory() as directory:
        _,ledger,_,app = setup(directory)
        core = ledger.lifecycle
        other = start(ledger,'job-2','start-2')['application']['application_id']
        eid,event_proposal = evidence(ledger,app)
        ledger.decide_event_proposal(event_proposal,'accepted',app,'reviewed',context('event'))
        proposal = core.propose_correction(app,'association',dict(evidence_id=eid,target_application_id=other,reason='Wrong role'),context('correction'))['proposal']
        raises(ContractError,lambda:core.decide_correction(proposal['proposal_id'],'accepted',context('approve')))
        assert core.list_tasks(app)[0]['status'] == 'open'
        assert core.list_application_conversation(app)['items']
        assert not core.list_application_conversation(other)['items']


def test_reviewed_deadline_creates_unknown_obligation_without_historical_alerts():
    from tests.test_job_search_secure_mail import make_archive, FixedDeadlineExtractor, NOW
    from job_search.mail.context import CandidateApplication
    from job_search.mail import TemporalSource, TemporalProposalEngine
    with tempfile.TemporaryDirectory() as directory:
        ledger,archive,_ = make_archive(directory)
        app = start(ledger)['application']['application_id']
        text = 'BEGIN UNTRUSTED EMAIL\nSUBJECT\nAssessment\nBODY\nPlease complete the assessment by Friday.\nEND UNTRUSTED EMAIL'
        saved = archive.archive_message(account_id='personal',immutable_message_id='deadline-message',sanitized_text=text,truncated=False,context=context('archive','system'))
        source = TemporalSource(saved['archive']['archive_id'],text,NOW)
        proposal = TemporalProposalEngine(ledger,FixedDeadlineExtractor(),'deadline-v1').propose(source,[CandidateApplication(app,'ashby','job-1','Acme','Platform Engineer')])[0]['proposal']
        ledger.decide_temporal_proposal(proposal['temporal_proposal_id'],'accepted','reviewed',context('approve'))
        task = ledger.lifecycle.list_tasks(app)[0]
        assert task['owner'] == 'unknown' and task['kind'] == 'follow_up'
        assert task['note'] == 'Review recorded deadline'
        reminders = ledger.lifecycle.list_unified_reminders(app)
        assert len(reminders) == 1 and reminders[0]['status'] == 'cancelled'
        assert not ledger.list_due_local_reminders(stamp())
        # The original due date and recorded decision remain available for audit.
        with connect(ledger.store.db_path) as con:
            saved_reminder = con.execute('SELECT * FROM local_reminders').fetchone()
            assert saved_reminder['due_at'] == '2026-09-04T22:00:00Z'
            assert saved_reminder['completed_at'] == saved_reminder['created_at']
        # User-authored reminders keep their independent, prospective semantics.
        reminder = ledger.create_reminder(dict(application_id=app,due_at=stamp(5),note='Review the old deadline now'),context('intentional-reminder'))['reminder']
        assert reminder['status'] == 'scheduled'
        assert ledger.lifecycle.publish_due_tasks(stamp(),context('publish','system'))['published'] == 0


def test_multiple_producer_events_for_same_fact_share_one_task():
    from job_search.lifecycle.core import ensure_event_task
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        eid,proposal = evidence(ledger,app)
        first = ledger.decide_event_proposal(proposal,'accepted',app,'reviewed',context('accept'))['event']
        with connect(path) as con:
            second,_ = ledger.store._append_event(con,app,ApplicationEventType.ASSESSMENT_REQUESTED,first['occurred_at'],{},'replayed-producer',context('producer','system'),stamp())
            ensure_event_task(con,ledger.store,second,eid,stamp())
        assert len(core.list_tasks(app)) == 1



def test_cancelling_queued_general_reminder_cancels_notification_and_delivery_lease():
    from job_search.notifications import NotificationIntent
    with tempfile.TemporaryDirectory() as directory:
        path,ledger,core,app = setup(directory)
        reminder = ledger.create_reminder(dict(application_id=app,due_at=stamp(5),note='Follow up'),context('reminder'))['reminder']
        notification = ledger.publish_notification(NotificationIntent(topic='reminder.due',source_id=reminder['reminder_id'],title='Reminder',body='Follow up',application_id=app,context={'reminder_id':reminder['reminder_id']}),NotificationPolicy(),available_at=stamp())['notification']
        ledger.complete_reminder(reminder['reminder_id'],context('completed','system'))
        published = core.list_unified_reminders(app)[0]
        assert published['status'] == 'completed' and published['delivery_status'] == 'pending'
        ledger.claim_notification('test-worker',stamp(2))
        assert core.list_unified_reminders(app)[0]['delivery_status'] == 'delivering'
        ledger.cancel_reminder(reminder['reminder_id'],context('cancel','hermes'))
        with connect(path) as con:
            saved = con.execute('SELECT * FROM notification_outbox WHERE notification_id=?',(notification['notification_id'],)).fetchone()
            assert saved['status'] == 'cancelled' and saved['lease_token'] is None
        assert core.list_unified_reminders(app)[0]['status'] == 'cancelled'
        assert core.list_unified_reminders(app)[0]['delivery_status'] == 'cancelled'


def test_cancelling_queued_local_reminder_preserves_task_completion_semantics():
    from unittest.mock import patch
    from tests.test_job_search_secure_mail import make_archive, FixedDeadlineExtractor, NOW
    from job_search.mail.context import CandidateApplication
    from job_search.mail import TemporalSource, TemporalProposalEngine
    from job_search.notifications import NotificationIntent
    with tempfile.TemporaryDirectory() as directory:
        ledger,archive,_ = make_archive(directory)
        app = start(ledger)['application']['application_id']
        text = 'Please complete the assessment by Friday.'
        saved = archive.archive_message(account_id='personal',immutable_message_id='deadline-message',sanitized_text=text,truncated=False,context=context('archive','system'))
        proposal = TemporalProposalEngine(ledger,FixedDeadlineExtractor(),'deadline-v1').propose(TemporalSource(saved['archive']['archive_id'],text,NOW),[CandidateApplication(app,'ashby','job-1','Acme','Engineer')])[0]['proposal']
        with patch('job_search.store.utc_now',return_value=NOW):
            accepted = ledger.decide_temporal_proposal(proposal['temporal_proposal_id'],'accepted','reviewed',context('approve'))
        reminder = accepted['reminders'][0]
        assert reminder['status'] == 'pending'
        notification = ledger.publish_notification(NotificationIntent(topic='reminder.due',source_id=reminder['reminder_id'],title='Deadline',body='Due now',application_id=app,context={'reminder_id':reminder['reminder_id']}),NotificationPolicy(),available_at=stamp())['notification']
        ledger.complete_local_reminder(reminder['reminder_id'],'completed',context('completed','system'))
        published = ledger.lifecycle.list_unified_reminders(app)[0]
        assert published['status'] == 'completed' and published['delivery_status'] == 'pending'
        ledger.lifecycle.cancel_unified_reminder('local:'+reminder['reminder_id'],context('cancel'))
        assert ledger.lifecycle.list_tasks(app)[0]['status'] == 'open'
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT status FROM notification_outbox WHERE notification_id=?',(notification['notification_id'],)).fetchone()['status'] == 'cancelled'
            assert con.execute('SELECT status FROM local_reminders WHERE reminder_id=?',(reminder['reminder_id'],)).fetchone()['status'] == 'dismissed'


def test_details_pagination_returns_disjoint_stable_pages():
    with tempfile.TemporaryDirectory() as directory:
        _,_,core,app = setup(directory)
        for index in range(3):
            core.record_detail(app,'assessment',dict(status='requested',title='Assessment '+str(index)),context('detail-'+str(index)))
        first = core.list_details(app,kind='assessment',limit=2)
        last = core.list_details(app,kind='assessment',limit=2,offset=2)
        assert len(first) == 2 and len(last) == 1
        assert not ({item['detail_id'] for item in first} & {item['detail_id'] for item in last})
        raises(ContractError,lambda:core.list_details(app,offset=-1))



def test_old_deadline_refining_existing_task_stays_quiet_until_user_snoozes():
    from unittest.mock import patch
    from tests.test_job_search_secure_mail import make_archive, FixedDeadlineExtractor, NOW
    from job_search.mail.context import CandidateApplication
    from job_search.mail import TemporalSource, TemporalProposalEngine
    with tempfile.TemporaryDirectory() as directory:
        ledger,archive,_ = make_archive(directory)
        app = start(ledger)['application']['application_id']
        eid,event_proposal = evidence(ledger,app,key='deadline-message',received=NOW)
        with patch('job_search.store.utc_now',return_value=NOW):
            ledger.decide_event_proposal(event_proposal,'accepted',app,'reviewed',context('event-accepted'))
        original = ledger.lifecycle.list_tasks(app)[0]
        saved = archive.archive_message(account_id='account',immutable_message_id='deadline-message',sanitized_text='Please complete the assessment by Friday.',truncated=False,context=context('archive','system'))
        proposal = TemporalProposalEngine(ledger,FixedDeadlineExtractor(),'deadline-v1').propose(TemporalSource(saved['archive']['archive_id'],'Please complete the assessment by Friday.',NOW),[CandidateApplication(app,'ashby','job-1','Acme','Engineer')])[0]['proposal']
        with patch('job_search.store.utc_now',return_value='2026-09-10T12:00:00Z'):
            ledger.decide_temporal_proposal(proposal['temporal_proposal_id'],'accepted','late review',context('deadline-accepted'))
            task = ledger.lifecycle.list_tasks(app)[0]
            assert task['task_id'] == original['task_id'] and task['revision_no'] == 2
            assert ledger.lifecycle.publish_due_tasks('2026-09-10T12:00:00Z',context('quiet','system'))['published'] == 0
            ledger.lifecycle.transition_task(task['task_id'],'snooze',dict(snoozed_until='2026-09-11T12:00:00Z'),context('intentional-snooze'))
            assert ledger.lifecycle.publish_due_tasks('2026-09-11T12:00:00Z',context('explicit','system'))['published'] == 1



def test_direct_detail_update_requires_current_revision_without_partial_writes():
    with tempfile.TemporaryDirectory() as directory:
        _,_,core,app = setup(directory)
        initial = core.record_detail(app,'offer',dict(status='offered',terms={'base':100000}),context('offer'))['detail']
        changed = core.record_detail(app,'offer',dict(status='negotiating',terms={'base':120000},expected_revision_no=1),context('new-offer'),initial['detail_id'])['detail']
        assert changed['revision_no'] == 2
        assert 'expected_revision_no' not in changed['details']
        for expected in (None, True, '2', 0):
            raises(ContractError,lambda:core.record_detail(app,'offer',dict(status='accepted',expected_revision_no=expected),context('bad-'+str(expected)),initial['detail_id']))
        raises(ConflictError,lambda:core.record_detail(app,'offer',dict(status='accepted',expected_revision_no=1),context('stale-tab'),initial['detail_id']))
        current = core.list_details(app)[0]
        assert current['status'] == 'negotiating' and current['revision_no'] == 2
        assert len(core.detail_history(initial['detail_id'])) == 2
        assert len(core.list_tasks(app,status='open')) == 1
        accepted = core.record_detail(app,'offer',dict(status='accepted',expected_revision_no=2),context('fresh-tab'),initial['detail_id'])['detail']
        replayed = core.record_detail(app,'offer',dict(status='accepted',expected_revision_no=2),context('fresh-tab'),initial['detail_id'])['detail']
        assert accepted == replayed and accepted['revision_no'] == 3


def test_reviewed_detail_proposal_uses_captured_revision_without_client_revision():
    with tempfile.TemporaryDirectory() as directory:
        _,_,core,app = setup(directory)
        detail = core.record_detail(app,'assessment',dict(status='requested'),context('assessment'))['detail']
        proposal = core.propose_correction(app,'detail',dict(kind='assessment',detail_id=detail['detail_id'],values={'status':'completed'}),context('propose','hermes'))['proposal']
        result = core.decide_correction(proposal['proposal_id'],'accepted',context('approve'))
        assert result['detail']['status'] == 'completed' and result['detail']['revision_no'] == 2



def test_reminder_delivery_status_distinguishes_unpublished_unknown_sent_and_failed():
    from job_search.notifications import NotificationIntent
    with tempfile.TemporaryDirectory() as directory:
        _,ledger,core,app = setup(directory)
        for index,outcome in enumerate(('succeeded','permanent_failure')):
            reminder = ledger.create_reminder(dict(application_id=app,due_at=stamp(5),note='Reminder '+str(index)),context('reminder-'+str(index)))['reminder']
            get = lambda: next(item for item in core.list_unified_reminders(app) if item['native_id'] == reminder['reminder_id'])
            assert get()['delivery_status'] == 'never_published'
            ledger.complete_reminder(reminder['reminder_id'],context('source-complete-'+str(index),'system'))
            assert get()['delivery_status'] == 'unknown'
            ledger.publish_notification(NotificationIntent(topic='reminder.due',source_id=reminder['reminder_id'],title='Reminder',body='Due',application_id=app,context={'reminder_id':'general:'+reminder['reminder_id']}),NotificationPolicy(),available_at=stamp())
            assert get()['delivery_status'] == 'pending'
            claimed = ledger.claim_notification('test-worker',stamp(2))
            ledger.complete_notification(claimed['notification_id'],claimed['lease_token'],outcome,stamp(3))
            assert get()['delivery_status'] == ('sent' if outcome == 'succeeded' else 'failed')
            assert get()['status'] == 'completed'


def main():
    tests = [value for key,value in globals().items() if key.startswith('test_') and callable(value)]
    for test in tests:
        test()
    print(f'ok ({len(tests)} lifecycle core tests)')


if __name__ == '__main__':
    main()
