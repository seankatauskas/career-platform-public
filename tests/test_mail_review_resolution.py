"""Offline user and agent batch-resolution contracts."""
import tempfile
from pathlib import Path
from contextlib import contextmanager

from job_search.contracts import (ApplicationEventType, EventProposalInput, ProducerKind,
    MutationContext, ContractError, ConflictError)
from job_search.db import connect
from job_search.mail.review import MailReviewService
from job_search.mail.sanitizer import sanitize_mail
from job_search.service import JobSearchLedger
from tests.test_job_search_dashboard import start_direct, stamp


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        app = start_direct(ledger, 'resolution')
        yield ledger, MailReviewService(ledger), app


def pending(ledger, suffix='one'):
    mail = sanitize_mail('Your application to Example',
        'Thank you for applying.\n\nWe will not proceed to interview.\nPlease complete the assessment.')
    evidence = ledger.record_mail_evidence(dict(account_id='account', immutable_message_id='message-'+suffix,
        sender='recruiter@example.test', subject=mail.subject, received_at=stamp(),
        body_sha256=mail.content_sha256, excerpt=mail.text), MutationContext('evidence-'+suffix,'system','fixture'))['evidence']
    quote = 'Thank you for applying.'
    start = mail.text.index(quote)
    proposal = ledger.create_event_proposal(EventProposalInput(evidence['evidence_id'], None,
        ApplicationEventType.SUBMISSION_CONFIRMED, ProducerKind.MODEL, 'fixture', .95, [],
        quote, start, start+len(quote), {}, 'proposal-'+suffix),
        MutationContext('proposal-'+suffix,'model','fixture'))['proposal']
    return proposal


def choice(proposal, app, **values):
    return dict(proposal_id=proposal['proposal_id'], decision='record', application_id=app,
                event_type='submission_confirmed', reason='Reviewed the source email and confirmed this application.', **values)


def apply(review, decisions, key='batch'):
    plan = review.preview(decisions)
    context = MutationContext(key,'user','fixture')
    return review.apply(plan['decisions'], plan['preview_hash'], context), plan, context


def rejects(fn, error=ContractError):
    try:
        fn()
    except error:
        return
    raise AssertionError('invalid operation was allowed')


def test_correct_unassigned_event_preserves_original_and_exact_evidence():
    with fixture() as (ledger, review, app):
        proposal = pending(ledger)
        decision = choice(proposal, app)
        decision.update(event_type='rejection_received', evidence_quote='We will not proceed to interview.')
        result, plan, context = apply(review, [decision])
        assert plan['changes'][0]['terminal_outcome'] == 'rejected'
        assert review.apply(plan['decisions'], plan['preview_hash'], context) == result
        with connect(ledger.store.db_path) as con:
            old = con.execute('SELECT * FROM event_proposals WHERE proposal_id=?',(proposal['proposal_id'],)).fetchone()
            assert old['status'] == 'rejected' and old['event_type'] == 'submission_confirmed'
            new = con.execute('SELECT * FROM event_proposals WHERE proposal_id=?',(result['resolved'][0]['replacement_proposal_id'],)).fetchone()
            assert new['status'] == 'accepted' and new['evidence_quote'] == decision['evidence_quote']
            assert con.execute('SELECT terminal_outcome FROM applications WHERE application_id=?',(app,)).fetchone()[0] == 'rejected'
            assert con.execute('SELECT count(*) FROM lifecycle_mail_links WHERE application_id=?',(app,)).fetchone()[0] == 1
        assert ledger.verify_projections() == []


def test_batch_preview_is_read_only_and_stale_batch_is_atomic():
    with fixture() as (ledger, review, app):
        first, second = pending(ledger,'one'), pending(ledger,'two')
        decisions = [choice(first,app),choice(second,app)]
        before = ledger.get_application_timeline(app)
        plan = review.preview(decisions)
        assert ledger.get_application_timeline(app) == before
        ledger.decide_event_proposal(second['proposal_id'],'rejected',None,'Dismissed elsewhere',MutationContext('other','user','fixture'))
        rejects(lambda: review.apply(plan['decisions'],plan['preview_hash'],MutationContext('batch','user','fixture')), ConflictError)
        assert ledger.get_application_timeline(app) == before
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT status FROM event_proposals WHERE proposal_id=?',(first['proposal_id'],)).fetchone()[0] == 'pending'


def test_create_missing_application_and_prevent_duplicate_creation():
    with fixture() as (ledger, review, app):
        p = pending(ledger)
        decision = choice(p, app); decision.pop('application_id')
        decision['new_application'] = {'employer':'Example External','title':'Engineer'}
        result, plan, _ = apply(review,[decision])
        assert plan['changes'][0]['creates_application']
        new_id = result['resolved'][0]['application_id']
        application = ledger.get_application_timeline(new_id)['application']
        assert application['ats'] == 'external' and application['current_phase'] == 'active'
        decision['proposal_id'] = pending(ledger,'two')['proposal_id']
        rejects(lambda: review.preview([decision]), ConflictError)


def test_failure_during_second_write_rolls_back_the_entire_batch():
    from unittest.mock import patch
    with fixture() as (ledger, review, app):
        decisions = [choice(pending(ledger, suffix), app) for suffix in ('one', 'two')]
        plan = review.preview(decisions)
        before = ledger.get_application_timeline(app)
        original = ledger.store._decide_event_proposal
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated write failure')
            return original(*args, **kwargs)
        context = MutationContext('retryable-batch', 'user', 'fixture')
        with patch.object(ledger.store, '_decide_event_proposal', side_effect=fail_second):
            rejects(lambda: review.apply(plan['decisions'], plan['preview_hash'], context), RuntimeError)
        assert ledger.get_application_timeline(app) == before
        assert len(review.list_pending()['items']) == 2
        assert len(review.apply(plan['decisions'], plan['preview_hash'], context)['resolved']) == 2


def test_keep_message_with_explicit_task_and_dismiss_without_application():
    with fixture() as (ledger, review, app):
        p, other = pending(ledger), pending(ledger,'two')
        decisions = [dict(proposal_id=p['proposal_id'],decision='keep',application_id=app,reason='Document request, no status change',
                          task={'kind':'send_document','note':'Send the requested portfolio'}),
                     dict(proposal_id=other['proposal_id'],decision='dismiss',reason='Duplicate notice')]
        apply(review, decisions)
        assert ledger.get_application_timeline(app)['application']['current_phase'] == 'preparing'
        tasks = ledger.lifecycle.list_tasks(app)
        assert len(tasks) == 1 and tasks[0]['kind'] == 'send_document'
        assert review.list_pending()['items'] == []


def test_invalid_batch_never_partially_applies_and_model_cannot_decide():
    with fixture() as (ledger, review, app):
        p = pending(ledger)
        decision = choice(p,app)
        rejects(lambda: review.preview([decision,decision]))
        rejects(lambda: review.preview([{**decision,'event_type':'rejection_received'}]))
        rejects(lambda: review.preview([{**decision,'evidence_quote':'Invented quote'}]))
        plan=review.preview([decision])
        rejects(lambda: review.apply(plan['decisions'],plan['preview_hash'],MutationContext('model','hermes','fixture')))
        rejects(lambda: review.apply(plan['decisions'],'wrong',MutationContext('user','user','fixture')),ConflictError)
        assert len(review.list_pending()['items']) == 1


def test_interview_event_does_not_invent_an_availability_task():
    with fixture() as (ledger, review, app):
        p=pending(ledger)
        decision=choice(p,app);decision.update(event_type='interview_requested',evidence_quote=p['evidence_quote'])
        apply(review,[decision])
        assert ledger.lifecycle.list_tasks(app) == []


def test_changed_application_and_existing_tasks_invalidate_preview():
    with fixture() as (ledger,review,app):
        p=pending(ledger);plan=review.preview([choice(p,app)])
        ledger.lifecycle.create_task(app,{'kind':'follow_up','owner':'applicant','note':'Existing task'},MutationContext('task','user','fixture'))
        rejects(lambda:review.apply(plan['decisions'],plan['preview_hash'],MutationContext('resolve','user','fixture')),ConflictError)
        assert len(review.list_pending()['items']) == 1


def test_shared_ownership_requires_grouped_review_and_pages_cover_all_items():
    with fixture() as (ledger,review,app):
        one,two=pending(ledger),pending(ledger,'two')
        page=review.list_pending(limit=1)
        assert len(page['items']) == 1 and page['next_cursor']
        next_page=review.list_pending(limit=1,after=page['next_cursor'])
        assert len(next_page['items']) == 1 and not next_page['next_cursor']
        assert page['items'][0]['proposal_id'] != next_page['items'][0]['proposal_id']
        ledger.mail_understanding.own_evidence(one['evidence_id'],MutationContext('owned','system','fixture'))
        rejects(lambda:review.preview([choice(one,app)]))
        assert len(review.list_pending()['items']) == 1


def test_batch_previews_sequential_status_and_rejects_conflicting_outcomes():
    with fixture() as (ledger,review,app):
        first,second=pending(ledger),pending(ledger,'two')
        receipt=choice(first,app)
        rejection=choice(second,app);rejection.update(event_type='rejection_received',evidence_quote='We will not proceed to interview.')
        plan=review.preview([receipt,rejection])
        assert plan['changes'][1]['from_phase'] == 'active'
        assert plan['changes'][1]['terminal_outcome'] == 'rejected'
        opposite={**receipt,'event_type':'offer_accepted','evidence_quote':first['evidence_quote']}
        rejects(lambda:review.preview([opposite,rejection]),ConflictError)


def test_http_preview_requires_csrf_and_resolution_uses_saved_preview():
    from tests.test_job_search_dashboard import dashboard, session, post, request
    import json
    with dashboard() as (server,controller,ledger,_):
        app=start_direct(ledger,'http-resolution');p=pending(ledger)
        cookie,csrf=session(server)
        status,_,data=request(server,'GET','/api/v1/mail-review/applications?search=example',headers={'Cookie':cookie})
        assert status == 200 and any(row['application_id'] == app for row in json.loads(data)['applications'])
        body={'decisions':[choice(p,app)]}
        assert post(server,'/api/v1/mail-review/preview',body,cookie,'wrong')[0] == 403
        status,_,data=post(server,'/api/v1/mail-review/preview',body,cookie,csrf)
        assert status == 200
        plan=json.loads(data)
        status,_,data=request(server,'GET','/api/v1/attention/message?kind=event_proposal&id='+p['proposal_id'],headers={'Cookie':cookie})
        assert status == 200 and 'BEGIN UNTRUSTED' not in json.loads(data)['body']
        payload={'decisions':plan['decisions'],'preview_hash':plan['preview_hash'],'idempotency_key':'http-apply'}
        first=post(server,'/api/v1/mail-review/resolve',payload,cookie,csrf)
        assert first[0] == 200, first
        assert post(server,'/api/v1/mail-review/resolve',payload,cookie,csrf)[2] == first[2]
        assert ledger.get_application_timeline(app)['application']['current_phase'] == 'active'


def test_cli_can_export_preview_and_apply_the_same_batch():
    import json
    import subprocess
    import sys
    with fixture() as (ledger,review,app):
        p=pending(ledger)
        command=[sys.executable,'-m','job_search','--db',str(ledger.store.db_path),'mail-review']
        exported=subprocess.run(command+['list'],check=True,capture_output=True,text=True)
        assert json.loads(exported.stdout)['items'][0]['proposal_id'] == p['proposal_id']
        preview=subprocess.run(command+['preview'],input=json.dumps({'decisions':[choice(p,app)]}),check=True,capture_output=True,text=True)
        result=subprocess.run(command+['apply','--idempotency-key','cli-batch'],input=preview.stdout,check=True,capture_output=True,text=True)
        assert len(json.loads(result.stdout)['resolved']) == 1


def main():
    tests=[test for name,test in globals().items() if name.startswith('test_')]
    for test in tests: test()
    print(f'ok ({len(tests)} mail review resolution tests)')


if __name__ == '__main__': main()
