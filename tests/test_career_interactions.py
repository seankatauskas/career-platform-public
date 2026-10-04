"""Offline trusted interaction, transport, retry, and browser authorization checks."""
import asyncio
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import sys
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

from job_search.contracts import ContractError,ConflictError,MutationContext
from job_search.service import JobSearchLedger
from job_search.interactions.service import InteractionsService
from job_search.interactions.server import make_interaction_server
from job_search.interactions.telegram import Spool,envelope_from_update,tool_policy,allowed_tools_from_env,DEFAULT_ALLOWED_TOOLS,TelegramInteractions
from job_search.interactions.tools import call,TOOL_NAMES

NOW=datetime(2026,10,3,12,tzinfo=timezone.utc)
IDENTITY={'bot_id':'123','user_id':'456','chat_id':'456'}
USER=MutationContext('review','user','test')


class Actions:
    def __init__(self):
        self.calls=[];self.results={};self.fail_after_commit=False
        self.row={'proposal_id':'proposal1','application_id':'application1','evidence_id':'evidence1','account_id':'mail1','status':'pending','payload_hash':'a'*64,'source_hash':'b'*64,'recipients':['recruiter@example.test'],'subject':'Interview','body':'Thursday works.','offered_slots':[],'expires_at':'2026-10-03T12:15:00Z'}
    def get_proposal(self,key):return dict(self.row)
    def list_proposals(self,**kwargs):return {'proposals':[dict(self.row)]}
    def decide_proposal(self,key,decision,digest,context,*,expected_source_hash=None):
        if context.idempotency_key in self.results:return self.results[context.idempotency_key]
        if digest!=self.row['payload_hash'] or expected_source_hash!=self.row['source_hash'] or self.row['status']!='pending':raise ConflictError('proposal changed')
        self.calls.append((key,decision,digest,context))
        self.row['status']=decision;result={'proposal_id':key,'status':decision}
        self.results[context.idempotency_key]=result
        if self.fail_after_commit:raise RuntimeError('simulated lost reply')
        return result


class Attention:
    def __init__(self):self.calls=[];self.revision=1
    def acknowledge(self,key,context,*,expected_revision):
        if expected_revision!=self.revision:raise ConflictError('candidate changed')
        self.calls.append(('ack',key));self.revision+=1;return {'candidate':{'candidate_id':key,'status':'acknowledged'}}
    def snooze(self,key,until,context,*,expected_revision):
        if expected_revision!=self.revision:raise ConflictError('candidate changed')
        self.calls.append(('snooze',key,until));self.revision+=1;return {'candidate':{'candidate_id':key,'status':'snoozed'}}
    def preferences(self):return {'revision':self.revision,'mode':'important_developments'}
    def update_preferences(self,changes,revision,context):
        if revision!=self.revision:raise ConflictError('preferences changed')
        self.revision+=1;return {'revision':self.revision,**changes}


def fixture(directory):
    ledger=JobSearchLedger(Path(directory)/'ledger.db');actions=Actions();attention=Attention();clock=[NOW]
    service=InteractionsService(ledger,actions=actions,attention=attention,identity=IDENTITY,now_provider=lambda:clock[0])
    ticket=service.review_action('proposal1',USER);service.claim_delivery(ticket['ticket_id']);service.mark_delivered(ticket['ticket_id'],'100')
    return service,actions,attention,clock,ticket


def envelope(**values):
    return {**IDENTITY,'chat_type':'private','update_id':'1','message_id':'101','reply_to_message_id':'100','text':'yes send it',**values}


def rejects(fn):
    try:fn()
    except ContractError:return
    raise AssertionError('expected a rejected contract')


def test_exact_private_reply_and_replay():
    with tempfile.TemporaryDirectory() as d:
        service,actions,_,_,ticket=fixture(d)
        first=service.ingest(envelope());assert first['status']=='completed'
        assert service.ingest(envelope())==first
        assert len(actions.calls)==1 and actions.calls[0][3].actor_kind=='user'
        assert service.ingest(envelope(update_id='2'))['status']=='clarification'
        rejects(lambda:service.ingest(envelope(text='no')))


def test_wrong_identity_edited_forwarded_and_bare_yes():
    with tempfile.TemporaryDirectory() as d:
        service,actions,_,_,_=fixture(d)
        for changes in ({'user_id':'999'},{'chat_id':'999'},{'bot_id':'999'},{'chat_type':'group'},{'is_bot':True},{'edited':True},{'forwarded':True},{'actor_kind':'user'}):
            rejects(lambda:service.ingest(envelope(**changes)))
        result=service.ingest(envelope(reply_to_message_id=None));assert result['status']=='clarification'
        assert not actions.calls


def test_expired_changed_and_wrong_button_target():
    with tempfile.TemporaryDirectory() as d:
        service,actions,_,clock,ticket=fixture(d)
        bad=envelope(callback_id='cb1',command='approve',message_id='100',ticket_id='f'*32)
        assert service.ingest(bad)['status']=='clarification'
        clock[0]+=timedelta(minutes=16)
        assert service.ingest(envelope(update_id='2'))['status']=='clarification'
        assert not actions.calls
    with tempfile.TemporaryDirectory() as d:
        service,actions,_,_,ticket=fixture(d);actions.row['source_hash']='c'*64
        assert service.ingest(envelope())['status']=='rejected';assert not actions.calls


def test_action_commit_interruption_recovers_exactly_once():
    with tempfile.TemporaryDirectory() as d:
        service,actions,_,_,_=fixture(d);actions.fail_after_commit=True
        try:service.ingest(envelope())
        except RuntimeError:pass
        else:raise AssertionError('expected failure after action commit')
        result=service.resume_pending();assert result[0]['status']=='completed';assert len(actions.calls)==1


def test_ack_snooze_revision_and_no_approval():
    for command in ('ack','snooze'):
        with tempfile.TemporaryDirectory() as d:
            service,actions,attention,_,_=fixture(d)
            candidate={'candidate_id':'candidate1','revision':1,'title':'Reply','summary':'Reply requested'}
            ticket=service.review_attention(candidate,USER);service.claim_delivery(ticket['ticket_id']);service.mark_delivered(ticket['ticket_id'],'200')
            result=service.ingest(envelope(callback_id='cb',ticket_id=ticket['ticket_id'],message_id='200',command=command))
            assert result['status']=='completed';assert not actions.calls;assert attention.calls[0][0]==command
    with tempfile.TemporaryDirectory() as d:
        service,actions,attention,_,_=fixture(d)
        ticket=service.review_attention({'candidate_id':'candidate1','revision':1},USER);service.claim_delivery(ticket['ticket_id']);service.mark_delivered(ticket['ticket_id'],'200');attention.revision=2
        assert service.ingest(envelope(message_id='201',reply_to_message_id='200',text='got it'))['status']=='rejected'


def test_spool_survives_restart_and_detects_changed_update():
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'spool.db';spool=Spool(path);key=spool.put(envelope())
        assert len(Spool(path).pending())==1
        try:spool.put(envelope(text='no'))
        except ValueError:pass
        else:raise AssertionError('changed update accepted')
        assert spool.claim_delivery('ticket1');assert not Spool(path).claim_delivery('ticket1')
        spool.sent('ticket1','10');assert Spool(path).unrecorded_deliveries()[0]['message_id']=='10'
        spool.complete(key,{'status':'completed'});assert not Spool(path).pending()
        assert path.stat().st_mode & 0o077 == 0


def test_raw_telegram_metadata_and_restricted_policy():
    human=NS(id=456,is_bot=False);bot=NS(id=123,is_bot=True)
    msg=NS(message_id=101,chat=NS(id=456,type='private'),from_user=human,text='yes send it',reply_to_message=NS(message_id=100,from_user=bot))
    update=NS(message=msg,update_id=5)
    value=envelope_from_update(update,123,IDENTITY);assert value['reply_to_message_id']=='100' and value['user_id']=='456'
    msg.forward_origin=object();assert envelope_from_update(update,123,IDENTITY) is None
    with patch.dict(os.environ,{'JOB_SEARCH_CAREER_ALLOWED_TOOLS_JSON':'[]'}):
        allowed=allowed_tools_from_env();assert allowed==DEFAULT_ALLOWED_TOOLS
    assert tool_policy(allowed,'terminal')['action']=='block'
    assert tool_policy(allowed,'mcp_job_search_get_briefing') is None
    with patch.dict(os.environ,{'JOB_SEARCH_CAREER_ALLOWED_TOOLS_JSON':'["mcp_other_execute"]'}):
        try:allowed_tools_from_env()
        except ValueError:pass
        else:raise AssertionError('allowlist expanded')


def test_service_transport_auth_origin_body_and_health():
    with tempfile.TemporaryDirectory() as d:
        service,actions,_,_,_=fixture(d);token='x'*48
        server=make_interaction_server(service,token,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def req(path,body=None,auth=True,origin=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=2)
            headers={'Content-Type':'application/json'}
            if auth:headers['Authorization']='Bearer '+token
            if origin:headers['Origin']=origin
            conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
            r=conn.getresponse();data=json.loads(r.read());conn.close();return r.status,data
        try:
            assert req('/health',auth=False)[0]==401
            assert req('/health')[1]['status']=='ok'
            assert req('/v1/interactions',envelope(),origin='http://evil.test')[0]==403
            assert req('/v1/interactions',envelope())[1]['status']=='completed'
            assert len(actions.calls)==1
        finally:server.shutdown();server.server_close();thread.join()


def test_dashboard_csrf_and_revision():
    from tests.test_job_search_dashboard import dashboard,session,post
    with dashboard() as (server,controller,ledger,prefs):
        controller.ledger=NS(attention=Attention())
        cookie,csrf=session(server);body={'changes':{'mode':'risk_only'},'expected_revision':1,'idempotency_key':'settings1'}
        assert post(server,'/api/v1/chief/preferences',body,cookie,'wrong')[0]==403
        assert post(server,'/api/v1/chief/preferences',body,cookie,csrf)[0]==200
        body['idempotency_key']='settings2';assert post(server,'/api/v1/chief/preferences',body,cookie,csrf)[0]==409


def test_model_tools_cannot_decide_and_review_is_canonical():
    assert not any('approve' in n or 'execute' in n or n in ('acknowledge','snooze') for n in TOOL_NAMES)
    with tempfile.TemporaryDirectory() as d:
        service,actions,attention,_,_=fixture(d)
        ledger=NS(interactions=service,career_actions=actions,attention=attention)
        result=call(ledger,'review_career_reply',{'proposal_id':'proposal1','idempotency_key':'review2'})
        assert result['status']=='review_sent' and not actions.calls
        rejects(lambda:call(ledger,'review_career_reply',{'proposal_id':'proposal1','idempotency_key':'review3','actor_kind':'user'}))


def test_urgent_notification_one_card_receipt_then_ack_leaves_task_open():
    from tests.test_attention import setup,live,ctx,notifications
    from job_search.interactions.notifications import InteractionNotificationSender,InteractionDeliveryPending
    from job_search.db import connect
    with tempfile.TemporaryDirectory() as d:
        ledger,attention,app,clock=setup(d);live(attention)
        task=ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',due_at='2026-10-05T13:00:00Z'),ctx('task','user'))['task']
        attention.evaluate(context=ctx('collect'));notification=notifications(ledger)[0]
        service=InteractionsService(ledger,actions=Actions(),attention=attention,identity=IDENTITY,now_provider=clock)
        fallback=NS(send=lambda value:(_ for _ in ()).throw(AssertionError('duplicate plain alert')))
        sender=InteractionNotificationSender(NS(store=ledger.store,interactions=service),fallback,clock)
        try:sender.send(notification)
        except InteractionDeliveryPending as pending:assert pending.delivery_state=='queued'
        else:raise AssertionError('queueing falsely marked delivered')
        assert notifications(ledger)[0]['status']=='pending'
        class Client:
            def call(self,path,payload=None):
                if path=='/v1/reviews':return service.pending_reviews()
                if path=='/v1/reviews/claim':return service.claim_delivery(payload['ticket_id'])
                if path=='/v1/reviews/delivered':return service.mark_delivered(payload['ticket_id'],payload['message_id'])
                if path=='/v1/interactions':return service.ingest(payload)
                if path=='/v1/reviews/unknown':return service.delivery_unknown(payload['ticket_id'])
                raise AssertionError(path)
        messages=[]
        async def send_message(**values):messages.append(values);return NS(message_id=700+len(messages))
        fake_telegram=NS(InlineKeyboardButton=lambda label,**kw:dict(label=label,**kw),InlineKeyboardMarkup=lambda rows:rows)
        bridge=TelegramInteractions(NS(bot=NS(send_message=send_message)),IDENTITY,Client(),Spool(Path(d)/'spool.db'))
        with patch.dict(sys.modules,{'telegram':fake_telegram}):
            asyncio.run(bridge.tick());asyncio.run(bridge.tick())
        assert len(messages)==1 and messages[0]['reply_markup'][0][0]['callback_data'].endswith(':ack')
        assert notifications(ledger)[0]['status']=='delivered'
        # The same outbox job now sees an authoritative external delivery receipt.
        try:sender.send(notification)
        except InteractionDeliveryPending as pending:assert pending.delivery_state=='sent'
        else:raise AssertionError('managed receipt must return through receipt-aware lease completion')
        result=service.ingest(envelope(update_id='9',message_id='702',reply_to_message_id='701',text='got it'))
        assert result['status']=='completed'
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT status FROM lifecycle_tasks WHERE task_id=?',(task['task_id'],)).fetchone()[0]=='open'


def test_delivery_claim_revalidates_source_and_crash_intent_never_repeats():
    from tests.test_attention import setup,live,ctx,notifications
    from job_search.interactions.notifications import InteractionNotificationSender,InteractionDeliveryPending
    with tempfile.TemporaryDirectory() as d:
        ledger,attention,app,clock=setup(d);live(attention)
        task=ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',due_at='2026-10-05T13:00:00Z'),ctx('task','user'))['task']
        attention.evaluate(context=ctx('collect'));notification=notifications(ledger)[0]
        service=InteractionsService(ledger,attention=attention,identity=IDENTITY,now_provider=clock)
        sender=InteractionNotificationSender(NS(store=ledger.store,interactions=service),NS(send=lambda row:None),clock)
        try:sender.send(notification)
        except InteractionDeliveryPending:pass
        ticket=service.pending_reviews()['items'][0]
        ledger.lifecycle.transition_task(task['task_id'],'complete',{},ctx('completed','user'))
        assert service.claim_delivery(ticket['ticket_id'])['send_allowed'] is False
        assert notifications(ledger)[0]['status']=='cancelled'
    with tempfile.TemporaryDirectory() as d:
        service,_,_,_,ticket=fixture(d)
        # No second external send is authorized even if a claimed gateway died.
        assert not service.claim_delivery(ticket['ticket_id'])['send_allowed']


def test_ambiguous_telegram_send_is_never_automatically_repeated():
    with tempfile.TemporaryDirectory() as d:
        ledger=JobSearchLedger(Path(d)/'db');service=InteractionsService(ledger,actions=Actions(),identity=IDENTITY,now_provider=lambda:NOW)
        ticket=service.review_action('proposal1',USER)
        class Client:
            def call(self,path,payload=None):
                if path=='/v1/reviews':return service.pending_reviews()
                if path=='/v1/reviews/claim':return service.claim_delivery(payload['ticket_id'])
                if path=='/v1/reviews/unknown':return service.delivery_unknown(payload['ticket_id'])
                raise AssertionError(path)
        attempted=[]
        async def timeout(**kwargs):attempted.append(kwargs);raise TimeoutError('external result unknown')
        bridge=TelegramInteractions(NS(bot=NS(send_message=timeout)),IDENTITY,Client(),Spool(Path(d)/'spool'))
        sdk=NS(InlineKeyboardButton=lambda label,**kw:dict(label=label,**kw),InlineKeyboardMarkup=lambda rows:rows)
        with patch.dict(sys.modules,{'telegram':sdk}):
            try:asyncio.run(bridge.tick())
            except TimeoutError:pass
            asyncio.run(bridge.tick())
        assert len(attempted)==1 and not service.pending_reviews()['items']
        assert bridge.spool.delivery(ticket['ticket_id'])['status']=='unknown'


def uncertain_alert(directory):
    from tests.test_attention import setup,live,ctx,notifications,candidate
    from job_search.interactions.notifications import InteractionNotificationSender,InteractionDeliveryPending
    from job_search.db import connect
    ledger,attention,app,clock=setup(directory);live(attention);candidate(attention);attention.evaluate(context=ctx('alert'))
    notification=notifications(ledger)[0];actions=Actions()
    service=InteractionsService(ledger,actions=actions,attention=attention,identity=IDENTITY,now_provider=clock)
    sender=InteractionNotificationSender(NS(store=ledger.store,interactions=service),NS(send=lambda row:None),clock)
    try:sender.send(notification)
    except InteractionDeliveryPending:pass
    ticket=service.pending_reviews()['items'][0];service.claim_delivery(ticket['ticket_id']);service.delivery_unknown(ticket['ticket_id'])
    with connect(ledger.store.db_path) as con:
        con.execute("UPDATE notification_outbox SET status='dead',last_error='delivery_reconciliation_required' WHERE notification_id=?",(notification['notification_id'],))
    return service,ledger,actions,notification,ticket,clock


def recovery_values(row,decision='received'):
    return dict(ticket_id=row['ticket_id'],decision=decision,expected_revision=row['delivery_revision'],payload_sha256=row['payload_sha256'],source_version=row['source_version'],identity=row['identity'])


def test_delivery_recovery_exact_identity_revision_audit_and_idempotency():
    from job_search.db import connect
    with tempfile.TemporaryDirectory() as d:
        service,ledger,actions,notification,ticket,clock=uncertain_alert(d)
        row=service.list_delivery_recovery()['items'][0];values=recovery_values(row)
        rejects(lambda:service.reconcile_delivery(values,MutationContext('bad-actor','hermes','test')))
        for changes in ({'expected_revision':99},{'payload_sha256':'f'*64},{'source_version':'changed'},{'identity':{**IDENTITY,'user_id':'789'}}):
            rejects(lambda:service.reconcile_delivery({**values,**changes},USER))
        result=service.reconcile_delivery(values,USER)
        assert service.reconcile_delivery(values,USER)==result
        rejects(lambda:service.reconcile_delivery({**values,'decision':'abandon'},USER))
        assert not service.list_delivery_recovery()['items'] and not actions.calls
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT status FROM notification_outbox WHERE notification_id=?',(notification['notification_id'],)).fetchone()[0]=='delivered'
            assert con.execute('SELECT count(*) FROM interaction_delivery_decisions').fetchone()[0]==1
            try:con.execute("DELETE FROM interaction_delivery_decisions")
            except Exception:pass
            else:raise AssertionError('recovery audit was mutable')


def test_abandon_and_late_receipt_never_resurrect_cancelled_delivery():
    from job_search.db import connect
    with tempfile.TemporaryDirectory() as d:
        service,ledger,actions,notification,ticket,clock=uncertain_alert(d)
        row=service.list_delivery_recovery()['items'][0]
        service.reconcile_delivery(recovery_values(row,'abandon'),USER)
        service.mark_delivered(ticket['ticket_id'],'900')
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT status FROM notification_outbox WHERE notification_id=?',(notification['notification_id'],)).fetchone()[0]=='cancelled'
        assert not actions.calls
    with tempfile.TemporaryDirectory() as d:
        service,ledger,actions,notification,ticket,clock=uncertain_alert(d)
        stale=recovery_values(service.list_delivery_recovery()['items'][0])
        service.mark_delivered(ticket['ticket_id'],'901')
        rejects(lambda:service.reconcile_delivery(stale,USER))
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT status FROM notification_outbox WHERE notification_id=?',(notification['notification_id'],)).fetchone()[0]=='delivered'


def test_claimed_expired_intent_can_be_recovered_without_plugin_returning():
    with tempfile.TemporaryDirectory() as d:
        ledger=JobSearchLedger(Path(d)/'db');clock=[NOW]
        service=InteractionsService(ledger,actions=Actions(),identity=IDENTITY,now_provider=lambda:clock[0])
        ticket=service.review_action('proposal1',USER);service.claim_delivery(ticket['ticket_id'])
        assert not service.list_delivery_recovery()['items']
        clock[0]+=timedelta(minutes=3)
        row=service.list_delivery_recovery()['items'][0]
        assert row['delivery_state']=='claimed'
        assert service.reconcile_delivery(recovery_values(row,'abandon'),USER)['decision']=='abandon'


def main():
    tests=[v for k,v in globals().items() if k.startswith('test_') and callable(v)]
    for test in tests:test()
    print('ok (%d career interaction tests)'%len(tests))


if __name__=='__main__':main()
