"""Reply tasks stay visible before drafting and resolve across Review and briefings."""
import json
import tempfile

from job_search.contracts import ContractError, ConflictError
from job_search.db import connect
from job_search.mail.sanitizer import sanitize_mail
from job_search.review_messages import review_message
from tests.test_job_search_ledger import make_service, start, context, stamp
from tests.test_job_search_dashboard import dashboard, request, session, post
from tests.test_lifecycle_core import raises


def reply_fixture(ledger, body='Could you please share your availability?'):
    app = start(ledger)['application']['application_id']
    mail = sanitize_mail('Next steps', body)
    evidence = ledger.record_mail_evidence(dict(
        account_id='account', immutable_message_id='incoming', conversation_id='thread',
        sender='recruiter@example.test', subject=mail.subject, received_at=stamp(),
        body_sha256=mail.content_sha256, excerpt=mail.text,
    ), context('evidence', 'system'))['evidence']['evidence_id']
    observation = ledger.lifecycle.observe_mail(dict(
        account_id='account', immutable_message_id='incoming', conversation_id='thread',
        direction='inbound', received_at=stamp(), evidence_id=evidence,
    ), context('observation', 'system'))['observation']
    ledger.lifecycle.link_mail(dict(observation_id=observation['observation_id'],
                                   application_id=app), context('link'))
    task = ledger.lifecycle.create_task(app, dict(kind='reply', owner='applicant',
        evidence_id=evidence, note='Please share your availability.'), context('reply'))['task']
    return app, evidence, task


def test_review_includes_open_applicant_replies_without_drafts_and_resolves_source():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger = make_service(directory)
        app, eid, task = reply_fixture(ledger)
        availability = ledger.lifecycle.create_task(app, dict(kind='send_availability', owner='applicant'), context('availability'))['task']
        for name, values in [('waiting', dict(kind='reply', owner='employer')),
                             ('unknown', dict(kind='reply', owner='unknown')),
                             ('assessment', dict(kind='complete_assessment', owner='applicant'))]:
            ledger.lifecycle.create_task(app, values, context(name))
        for operation in ('cancel', 'complete', 'supersede'):
            closed = ledger.lifecycle.create_task(app, dict(kind='reply', owner='applicant'), context(operation))['task']
            ledger.lifecycle.transition_task(closed['task_id'], operation, {}, context(operation+'-decision'))
        rows = [r for r in ledger.list_attention_items() if r['kind']=='reply_request']
        assert {r['id'] for r in rows} == {task['task_id'], availability['task_id']}
        row = next(r for r in rows if r['id']==task['task_id'])
        assert row['application_id']==app and row['evidence_id']==eid and row['revision_no']==1
        assert row['subject']=='Next steps' and row['status']=='review'
        assert review_message(ledger, None, row)['body']=='Could you please share your availability?'
        assert not ledger.list_actions() and not ledger.career_actions.list_proposals()['proposals']
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE applications SET current_phase='terminal' WHERE application_id=?", (app,))
        assert not any(r['kind']=='reply_request' for r in ledger.list_attention_items())


def test_http_review_prepare_and_dismiss_share_task_state_and_audit():
    with dashboard() as (server, _, ledger, _):
        app, eid, task = reply_fixture(ledger)
        cookie, csrf = session(server)
        def get(path):
            status, _, body = request(server, 'GET', path, headers={'Cookie':cookie})
            assert status==200, body
            return json.loads(body)
        rows = get('/api/v1/attention')['items']
        assert any(r['kind']=='reply_request' and r['id']==task['task_id'] for r in rows)
        message = get('/api/v1/attention/message?kind=reply_request&id='+task['task_id'])
        assert message['body']=='Could you please share your availability?'
        assert any(f['ref']=='task:'+task['task_id'] for f in ledger.attention.preview()['snapshot']['facts'])
        prepare = dict(idempotency_key='prepare', task_id=task['task_id'], expected_revision=1)
        path = '/api/v1/lifecycle/tasks/prepare-reply'
        assert request(server,'POST',path,prepare)[0]==403
        status, _, body = post(server,path,prepare,cookie,csrf)
        assert status==200, body
        assert json.loads(body)['queued']
        assert post(server,path,prepare,cookie,csrf)[2]==body
        with connect(ledger.store.db_path) as con:
            work = con.execute("SELECT * FROM work_items WHERE task_kind='career.reply.context'").fetchall()
            assert len(work)==1
            assert json.loads(work[0]['payload_json'])==dict(application_id=app, evidence_id=eid)
            assert con.execute('SELECT COUNT(*) FROM career_send_proposals').fetchone()[0]==0
        # Queuing does not remove the obligation or imply mail was sent.
        assert ledger.lifecycle.list_tasks(app, status='open')[0]['task_id']==task['task_id']
        dismiss = dict(idempotency_key='dismiss', task_id=task['task_id'], operation='cancel',
                       values=dict(expected_revision=1, reason='Reviewed email: no reply needed.'))
        status, _, body = post(server,'/api/v1/lifecycle/tasks/transition',dismiss,cookie,csrf)
        assert status==200, body
        assert json.loads(body)['task']['status']=='cancelled'
        assert not any(r['kind']=='reply_request' for r in get('/api/v1/attention')['items'])
        assert not ledger.lifecycle.get_application_briefing(app)['next_obligations']
        assert not any(f['ref']=='task:'+task['task_id'] for f in ledger.attention.preview()['snapshot']['facts'])
        revision = ledger.lifecycle.task_history(task['task_id'])[-1]
        assert revision['actor_kind']=='user' and revision['operation']=='cancel'
        assert revision['state']['transition_reason']=='Reviewed email: no reply needed.'
        prepare['idempotency_key']='stale-prepare'
        assert post(server,path,prepare,cookie,csrf)[0]==409
        # Existing legacy scanning cannot recreate a dismissed task for the same mail.
        ledger.career_actions.record_reply_obligations(context('scan', 'system'))
        assert not ledger.lifecycle.list_tasks(app, status='open')


def test_stale_reviews_and_unlinked_sources_cannot_queue_or_dismiss():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger = make_service(directory)
        app, _, task = reply_fixture(ledger)
        lifecycle = ledger.lifecycle
        raises(ContractError, lambda: lifecycle.prepare_task_reply(task['task_id'], 1, context('agent', 'hermes')))
        raises(ContractError, lambda: lifecycle.prepare_task_reply(task['task_id'], True, context('bool')))
        lifecycle.transition_task(task['task_id'], 'snooze', dict(snoozed_until=stamp(3600)), context('snooze'))
        raises(ConflictError, lambda: lifecycle.prepare_task_reply(task['task_id'], 1, context('old-prepare')))
        raises(ConflictError, lambda: lifecycle.transition_task(task['task_id'], 'cancel', dict(expected_revision=1), context('old-dismiss')))
        assert lifecycle.list_tasks(app)[0]['status']=='open'
        orphan = lifecycle.create_task(app, dict(kind='reply', owner='applicant'), context('orphan'))['task']
        raises(ContractError, lambda: lifecycle.prepare_task_reply(orphan['task_id'], 1, context('orphan-prepare')))
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE lifecycle_mail_observations SET direction='outbound'")
        raises(ContractError, lambda: lifecycle.prepare_task_reply(task['task_id'], 2, context('outbound-prepare')))
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT COUNT(*) FROM work_items').fetchone()[0]==0


if __name__ == '__main__':
    tests = [value for name, value in list(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
    print(f'ok ({len(tests)} reply review tests)')
