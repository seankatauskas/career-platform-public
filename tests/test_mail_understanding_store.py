"""Offline authenticated persistence, independent review, and projection regressions."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from job_search.contracts import ContractError, ConflictError, MutationContext
from job_search.db import connect
from job_search.mail.archive import EncryptedMailArchive
from job_search.mail.context import CandidateApplication
from job_search.mail.sanitizer import sanitize_mail
from job_search.mail.understanding_schema import SCHEMA
from job_search.mail.understanding_store import MailUnderstandingService, available
from job_search.inference.usage import InvocationRecoveryService, begin_invocation, invocation_scope
from tests.test_job_search_ledger import make_service, start, context
from tests.test_job_search_secure_mail import TestCipher

NOW='2030-01-01T12:00:00Z'
SYSTEM=MutationContext('understanding-worker','system','understanding_test')
BODY='Acme Platform Engineer. Thank you for applying. Please complete the assessment by 2030-01-05 at 12:00 UTC. Please confirm receipt by email.'


class Key:
    def get_or_create_key(self):
        return 'test-key', b'k'*32


def setup(directory, body=BODY, *, producer='understanding-test-v1', mode='shared'):
    path,ledger=make_service(directory)
    with connect(path) as con:
        if not available(con):
            con.executescript(SCHEMA)
    app=start(ledger)['application']['application_id']
    clean=sanitize_mail('Application',body,max_chars=2048)
    evidence=ledger.record_mail_evidence({'account_id':'account','immutable_message_id':'mail','conversation_id':'thread','sender':'recruiter@acme.test','subject':'Application','received_at':NOW,'body_sha256':clean.content_sha256,'excerpt':clean.text},context('evidence'))['evidence']['evidence_id']
    archive=EncryptedMailArchive(ledger,Key(),cipher_factory=TestCipher)
    archived=archive.archive_message(account_id='account',immutable_message_id='mail',sanitized_text=body,truncated=False,context=SYSTEM)
    archive_id=archived['archive']['archive_id']
    observed=ledger.lifecycle.observe_mail({'account_id':'account','immutable_message_id':'mail','conversation_id':'thread','direction':'inbound','sender':'recruiter@acme.test','received_at':NOW,'evidence_id':evidence,'archive_id':archive_id},SYSTEM)['observation']
    request={'schema_version':'mail_understanding_v1','account_id':'account','immutable_message_id':'mail','observation_id':observed['observation_id'],'evidence_id':evidence,'received_at':NOW,'sources':[{'source_id':'current','kind':'current','text':body,'source_at':NOW,'sha256':hashlib.sha256(body.encode()).hexdigest(),'truncated':False,'archive_id':archive_id}],'archive_id':archive_id,'candidates':[CandidateApplication(app,'ashby','job-1','Acme','Platform Engineer').model_context()],'candidate_context_complete':True,'coverage':[],'producer_version':producer}
    if mode=='replay':request['replay_id']='review-batch'
    return path,ledger,MailUnderstandingService(ledger,archive),request,app


def citation(request,quote):
    text=request['sources'][0]['text']; start=text.index(quote)
    return {'source_id':'current','quote':quote,'start':start,'end':start+len(quote)}


def raw(request,app,*,event=True,assessment=True,reply=False,deadline=False):
    result={'relevance':'career','events':[],'actions':[],'temporal_facts':[],'uncertainties':[]}
    if event:result['events'].append({'event_type':'submission_confirmed','application_id':app,'confidence':.99,'evidence':[citation(request,'Thank you for applying.')]})
    if assessment:result['actions'].append({'kind':'complete_assessment','description':'Complete the assessment','actor':'applicant','obligation':'required','channel':'portal','temporal_index':0 if deadline else None,'application_id':app,'confidence':.99,'evidence':[citation(request,'Please complete the assessment')]})
    if reply:result['actions'].append({'kind':'reply','description':'Confirm receipt by email','actor':'applicant','obligation':'required','channel':'email','temporal_index':None,'application_id':app,'confidence':.99,'evidence':[citation(request,'Please confirm receipt by email.')]})
    if deadline:result['temporal_facts'].append({'kind':'deadline','wording':'2030-01-05 at 12:00 UTC','starts_at':None,'ends_at':None,'due_at':'2030-01-05T12:00:00Z','time_zone':'UTC','application_id':app,'confidence':.99,'evidence':[citation(request,'2030-01-05 at 12:00 UTC')]})
    return result


def analyze(service,request,result,mode='shared'):
    claim=service.claim(request,SYSTEM,mode=mode)
    if claim['state']!='saved':
        service.save(claim['analysis_id'],claim['claim_token'],result,SYSTEM)
    return service.project(claim['analysis_id'],SYSTEM)


def choices(detail,app,types=None,decision='accepted'):
    return [{'finding_id':f['finding_id'],'decision':decision,'application_id':app,'reason':'Reviewed'} for f in detail['findings'] if types is None or f['type'] in types]


def test_encrypted_sources_claim_fencing_and_resume():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims=list(executor.map(lambda _:service.claim(request,SYSTEM),range(2)))
        assert sorted(c['state'] for c in claims)==['busy','claimed']
        first=next(c for c in claims if c['state']=='claimed')
        assert service.owns_message('account','mail')
        assert service.get_request(first['analysis_id'])==request
        with connect(path) as con:
            record=con.execute('SELECT request_json FROM mail_understanding_analyses').fetchone()[0]
            assert request['sources'][0]['text'] not in record and '"text"' not in record
            con.execute("UPDATE mail_understanding_analyses SET lease_expires_at='2000-01-01T00:00:00Z'")
        second=service.claim(request,SYSTEM)
        assert second['claim_token']!=first['claim_token']
        try:service.save(first['analysis_id'],first['claim_token'],raw(request,app),SYSTEM)
        except ConflictError:pass
        else:raise AssertionError('expired claim saved')
        service.save(second['analysis_id'],second['claim_token'],raw(request,app),SYSTEM)
        assert service.claim(request,SYSTEM)['state']=='saved'
        assert service.find_for_message('account','mail')['analysis_id']==first['analysis_id']
        with connect(path) as con:
            try:con.execute("UPDATE mail_understanding_sources SET ciphertext=x'00'")
            except sqlite3.IntegrityError:pass
            else:raise AssertionError('authenticated source mutated')


def test_long_quote_and_receipt_acceptance_does_not_accept_action():
    with TemporaryDirectory() as directory:
        body='Acme Platform Engineer. '+('irrelevant background '*200)+BODY
        path,ledger,service,request,app=setup(directory,body)
        result=raw(request,app)
        assert result['events'][0]['evidence'][0]['start']>2048
        detail=analyze(service,request,result)
        rows=ledger.list_attention_items()
        assert len([r for r in rows if r['kind']=='mail_analysis'])==1
        assert not any(r['kind']=='event_proposal' for r in rows)
        user=MailUnderstandingService(ledger) # review requires no archive decryption key
        command=choices(detail,app,{'event'})+choices(detail,app,{'action'},'rejected')
        detail=user.decide(detail['analysis_id'],detail['revision'],command,context('mixed'))
        assert {f['type']:f['status'] for f in detail['findings']}=={'event':'accepted','action':'rejected'}
        assert ledger.lifecycle.list_tasks(app)==[]
        assert service.list_reviews()==[]


def test_atomic_batch_rolls_back_and_rejects_stale_review():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        detail=analyze(service,request,raw(request,app))
        command=choices(detail,app)
        command[-1]['application_id']='not-a-candidate'
        try:service.decide(detail['analysis_id'],detail['revision'],command,context('invalid'))
        except ContractError:pass
        else:raise AssertionError('bad review applied')
        assert all(f['status']=='pending' for f in service.get(detail['analysis_id'])['findings'])
        current=service.decide(detail['analysis_id'],detail['revision'],choices(detail,app,{'event'}),context('one'))
        try:service.decide(detail['analysis_id'],detail['revision'],choices(detail,app,{'action'}),context('stale'))
        except ConflictError:pass
        else:raise AssertionError('stale batch applied')
        assert current['revision']!=detail['revision']


def test_two_actions_and_deadline_are_independent_in_both_orders():
    for temporal_first in (True,False):
        with TemporaryDirectory() as directory:
            path,ledger,service,request,app=setup(directory)
            detail=analyze(service,request,raw(request,app,event=False,reply=True,deadline=True))
            first={'temporal'} if temporal_first else {'action'}
            detail=service.decide(detail['analysis_id'],detail['revision'],choices(detail,app,first),context('first'))
            tasks=ledger.lifecycle.list_tasks(app)
            assert len(tasks)==(0 if temporal_first else 2)
            assert not any(t['due_at'] for t in tasks)
            detail=service.decide(detail['analysis_id'],detail['revision'],choices(detail,app,{'action'} if temporal_first else {'temporal'}),context('second'))
            tasks=ledger.lifecycle.list_tasks(app)
            assert len(tasks)==2
            assert {t['kind']:t['due_at'] for t in tasks}=={'reply':None,'complete_assessment':'2030-01-05T12:00:00Z'}
            service.project(detail['analysis_id'],SYSTEM)
            assert len(ledger.lifecycle.list_tasks(app))==2
            with connect(path) as con:
                assert con.execute('SELECT COUNT(*) FROM local_reminders').fetchone()[0]==0


def test_legacy_producers_are_suppressed_from_claim_and_direct_api():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        ledger.lifecycle.link_mail({'observation_id':request['observation_id'],'application_id':app},context('link'))
        claim=service.claim(request,SYSTEM)
        assert ledger.career_actions.record_reply_obligations(SYSTEM)==[]
        result=raw(request,app,assessment=False)
        result['events'][0]['event_type']='interview_requested'
        service.save(claim['analysis_id'],claim['claim_token'],result,SYSTEM)
        detail=service.project(claim['analysis_id'],SYSTEM)
        pid=detail['findings'][0]['projection']['id']
        ledger.decide_event_proposal(pid,'accepted',app,'Reviewed',context('legacy-api'))
        assert ledger.lifecycle.list_tasks(app)==[]


def test_reviewed_other_mapping_and_immutable_correction():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        result=raw(request,app,event=False)
        result['actions'][0].update(kind='other',description='Book through the interview portal')
        detail=analyze(service,request,result)
        command=choices(detail,app)
        command[0]['replacement']={**detail['findings'][0]['value'],'task_kind':'follow_up'}
        user=MailUnderstandingService(ledger)
        reviewed=user.decide(detail['analysis_id'],detail['revision'],command,context('correct'))
        assert sorted(f['status'] for f in reviewed['findings'])==['accepted','rejected']
        tasks=ledger.lifecycle.list_tasks(app)
        assert len(tasks)==1 and tasks[0]['kind']=='follow_up'
        assert tasks[0]['note']=='Book through the interview portal'


def test_correction_cannot_invent_evidence():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        detail=analyze(service,request,raw(request,app,event=False))
        command=choices(detail,app)
        command[0]['replacement']=deepcopy(detail['findings'][0]['value'])
        command[0]['replacement']['evidence'][0]['quote']='Invented words'
        try:MailUnderstandingService(ledger).decide(detail['analysis_id'],detail['revision'],command,context('bad-correction'))
        except ContractError:pass
        else:raise AssertionError('unverified correction accepted')
        assert len(service.get(detail['analysis_id'])['findings'])==1


def test_cancelled_task_is_not_resurrected_by_new_version():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        detail=analyze(service,request,raw(request,app,event=False))
        service.decide(detail['analysis_id'],detail['revision'],choices(detail,app),context('accept'))
        task=ledger.lifecycle.list_tasks(app)[0]
        ledger.lifecycle.transition_task(task['task_id'],'cancel',{'reason':'No longer needed'},context('cancel'))
        next_request={**request,'producer_version':'understanding-test-v2','replay_id':'new-version'}
        replay=analyze(service,next_request,raw(request,app,event=False),'replay')
        try:service.decide(replay['analysis_id'],replay['revision'],choices(replay,app),context('retry-cancelled'))
        except ConflictError:pass
        else:raise AssertionError('cancelled task was reopened')
        assert len(ledger.lifecycle.list_tasks(app))==1
        assert ledger.lifecycle.list_tasks(app)[0]['status']=='cancelled'


def test_changed_context_cannot_bypass_message_claim_and_saved_checkpoint():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        first=service.claim(request,SYSTEM)
        changed={**request,'coverage':[{'source_id':'history','reason':'changed'}]}
        assert service.claim(changed,SYSTEM)['state']=='busy'
        service.fail(first['analysis_id'],first['claim_token'],'invalid_output',SYSTEM)
        retry=service.claim(changed,SYSTEM)
        assert retry['analysis_id']==first['analysis_id']
        assert service.get_request(retry['analysis_id'])==request
        service.save(retry['analysis_id'],retry['claim_token'],raw(request,app),SYSTEM)
        assert service.claim(changed,SYSTEM)=={'analysis_id':first['analysis_id'],'claim_token':None,'state':'saved'}


def test_shadow_and_replay_stay_outside_regular_review():
    for mode in ('shadow','replay'):
        with TemporaryDirectory() as directory:
            path,ledger,service,request,app=setup(directory,mode=mode)
            detail=analyze(service,request,raw(request,app),mode)
            assert service.list_reviews()==[]
            assert not any(r['kind'] in ('mail_analysis','event_proposal','temporal_proposal') for r in ledger.list_attention_items())
            assert ledger.lifecycle.list_tasks(app)==[]
            assert len(service.list_reviews(history=True))==(1 if mode=='replay' else 0)
            assert not service.owns_message('account','mail')


def test_invalid_model_output_cannot_be_saved_or_projected():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        claim=service.claim(request,SYSTEM)
        value=raw(request,app); value['events'][0]['evidence'][0]['end']+=1
        try:service.save(claim['analysis_id'],claim['claim_token'],value,SYSTEM)
        except ContractError:pass
        else:raise AssertionError('unsupported evidence saved')
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM mail_understanding_findings').fetchone()[0]==0
        service.fail(claim['analysis_id'],claim['claim_token'],'invalid_output',SYSTEM)
        assert service.claim(request,SYSTEM)['state']=='claimed'


def test_reviewed_deadline_correction_tracks_original_action_reference():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        detail=analyze(service,request,raw(request,app,event=False,deadline=True))
        temporal=next(f for f in detail['findings'] if f['type']=='temporal')
        command=choices(detail,app)
        selected=next(c for c in command if c['finding_id']==temporal['finding_id'])
        selected['replacement']={**temporal['value'],'due_at':'2030-01-06T12:00:00Z'}
        reviewed=MailUnderstandingService(ledger).decide(detail['analysis_id'],detail['revision'],command,context('correct-time'))
        assert len(reviewed['findings'])==3
        assert ledger.lifecycle.list_tasks(app)[0]['due_at']=='2030-01-06T12:00:00Z'


def test_existing_temporal_api_projects_only_accepted_linked_action():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        detail=analyze(service,request,raw(request,app,event=False,deadline=True))
        detail=service.decide(detail['analysis_id'],detail['revision'],choices(detail,app,{'action'}),context('action'))
        pid=next(f['projection']['id'] for f in detail['findings'] if f['type']=='temporal')
        ledger.decide_temporal_proposal(pid,'accepted','Reviewed',context('temporal'))
        assert len(ledger.lifecycle.list_tasks(app))==1
        assert ledger.lifecycle.list_tasks(app)[0]['due_at']=='2030-01-05T12:00:00Z'


class Evaluated:
    policy_id='action-policy'
    def allows(self,typ,value,request):return True
    def event_policy(self,event_type):
        return {'policy_id':'event-policy:'+event_type,'threshold':.99,'example_count':50,'observed_precision':1.0,'wrong_application_matches':0,'evaluation_sha256':'a'*64}


def test_evaluated_event_and_action_gates_are_independent_and_idempotent():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        detail=analyze(service,request,raw(request,app))
        assert all(f['status']=='pending' for f in detail['findings'])
        detail=service.project(detail['analysis_id'],SYSTEM,evaluation_report=Evaluated())
        assert all(f['status']=='accepted' for f in detail['findings'])
        service.project(detail['analysis_id'],SYSTEM,evaluation_report=Evaluated())
        assert len(ledger.lifecycle.list_tasks(app))==1
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        request['coverage']=[{'source_id':'missing','reason':'attachment unavailable'}]
        detail=analyze(service,request,raw(request,app))
        detail=service.project(detail['analysis_id'],SYSTEM,evaluation_report=Evaluated())
        assert all(f['status']=='pending' for f in detail['findings'])
        assert ledger.lifecycle.list_tasks(app)==[]


def test_usage_deferral_is_not_a_provider_attempt_and_uncertain_is_distinct():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        for _ in range(5):
            claim=service.claim(request,SYSTEM)
            assert claim['state']=='claimed'
            service.fail(claim['analysis_id'],claim['claim_token'],'usage_deferred',SYSTEM)
        claim=service.claim(request,SYSTEM)
        service.fail(claim['analysis_id'],claim['claim_token'],'usage_reconciliation_required',SYSTEM)
        assert service.claim(request,SYSTEM)['state']=='uncertain'


def test_reviewed_replay_does_not_publish_event_or_task_alerts():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory,mode='replay')
        result=raw(request,app,deadline=True)
        result['events'][0]['event_type']='interview_requested'
        detail=analyze(service,request,result,'replay')
        service.decide(detail['analysis_id'],detail['revision'],choices(detail,app),context('history'))
        ledger.lifecycle.publish_due_tasks('2030-01-07T12:00:00Z',SYSTEM)
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0]==0
        assert len(ledger.lifecycle.list_tasks(app))==1


def test_explicit_renewal_reuses_cited_prior_task():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        detail=analyze(service,request,raw(request,app,event=False))
        service.decide(detail['analysis_id'],detail['revision'],choices(detail,app),context('first-request'))
        first_task=ledger.lifecycle.list_tasks(app)[0]['task_id']
        body='Acme Platform Engineer. Please complete the assessment as requested yesterday.'
        now='2030-01-02T12:00:00Z'
        clean=sanitize_mail('Reminder',body,max_chars=2048)
        eid=ledger.record_mail_evidence({'account_id':'account','immutable_message_id':'renewal','conversation_id':'thread','sender':'recruiter@acme.test','subject':'Reminder','received_at':now,'body_sha256':clean.content_sha256,'excerpt':clean.text},context('renewal-evidence'))['evidence']['evidence_id']
        ctx=MutationContext('renewal','system','understanding_test')
        archived=service.archive.archive_message(account_id='account',immutable_message_id='renewal',sanitized_text=body,truncated=False,context=ctx)['archive']['archive_id']
        oid=ledger.lifecycle.observe_mail({'account_id':'account','immutable_message_id':'renewal','conversation_id':'thread','direction':'inbound','sender':'recruiter@acme.test','received_at':now,'evidence_id':eid,'archive_id':archived},ctx)['observation']['observation_id']
        current={'source_id':'current','kind':'current','text':body,'source_at':now,'sha256':hashlib.sha256(body.encode()).hexdigest(),'truncated':False,'archive_id':archived}
        prior={**request['sources'][0],'source_id':'prior','kind':'prior_inbound'}
        renewed={**request,'immutable_message_id':'renewal','observation_id':oid,'evidence_id':eid,'received_at':now,'sources':[current,prior],'archive_id':archived}
        result=raw(renewed,app,event=False)
        result['actions'][0]['evidence'].append({**citation(request,'Please complete the assessment'),'source_id':'prior'})
        detail=analyze(service,renewed,result)
        service.decide(detail['analysis_id'],detail['revision'],choices(detail,app),context('renew-request'))
        tasks=ledger.lifecycle.list_tasks(app)
        assert len(tasks)==1 and tasks[0]['task_id']==first_task


def test_conflicting_outcomes_cannot_use_list_order_to_choose_state():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        result=raw(request,app,assessment=False)
        result['events'][0]['event_type']='rejection_received'
        result['events'].append({**result['events'][0],'event_type':'offer_accepted'})
        detail=analyze(service,request,result)
        assert any(f['type']=='uncertainty' for f in detail['findings'])
        try:service.decide(detail['analysis_id'],detail['revision'],choices(detail,app,{'event'}),context('contradictory'))
        except ContractError:pass
        else:raise AssertionError('list order selected conflicting terminal outcome')
        assert all(f['status']=='pending' for f in service.get(detail['analysis_id'])['findings'])


def test_distinct_same_kind_requests_have_independent_tasks():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        result=raw(request,app,event=False)
        result['actions'].append({**result['actions'][0],'description':'Complete the separate written assessment','evidence':[citation(request,'Please complete the assessment by 2030-01-05 at 12:00 UTC.')]})
        detail=analyze(service,request,result)
        service.decide(detail['analysis_id'],detail['revision'],choices(detail,app),context('distinct'))
        tasks=ledger.lifecycle.list_tasks(app)
        assert len(tasks)==2
        assert {t['note'] for t in tasks}=={'Complete the assessment','Complete the separate written assessment'}
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        result=raw(request,app,event=False)
        result['actions'].append(deepcopy(result['actions'][0]))
        detail=analyze(service,request,result)
        service.decide(detail['analysis_id'],detail['revision'],choices(detail,app),context('duplicate'))
        assert len(ledger.lifecycle.list_tasks(app))==1


def test_uncertain_relevance_always_has_a_review_item():
    with TemporaryDirectory() as directory:
        _,ledger,service,request,app=setup(directory)
        result={'relevance':'uncertain','events':[],'actions':[],'temporal_facts':[],'uncertainties':[]}
        detail=analyze(service,request,result)
        assert len(detail['findings'])==1
        assert detail['findings'][0]['type']=='uncertainty'
        assert len(service.list_reviews())==1


def bind_test_work(path,service,claim,work_id='parent-mail-sync'):
    with connect(path) as con:
        con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,created_at,recovery_revision) VALUES(?,'mail.sync',?,'{}','running',?,3,?,0)",(work_id,work_id,NOW,NOW))
    service.bind_inference_work(claim['analysis_id'],claim['claim_token'],work_id,0,SYSTEM)
    return work_id


def test_uncertain_outcome_blocks_every_mode_until_audited_failure():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        first=service.claim(request,SYSTEM)
        work=bind_test_work(path,service,first)
        with invocation_scope(path,work,0):
            call=begin_invocation('test-provider','mail_understanding',b'private-request',reserved_tokens=1)
            call.submitting();call.unknown()
        service.fail(first['analysis_id'],first['claim_token'],'usage_reconciliation_required',SYSTEM)
        replay_request={**request,'replay_id':'new-replay','producer_version':'different-model'}
        assert service.claim(replay_request,SYSTEM,mode='replay')['state']=='uncertain'
        assert service.claim(request,SYSTEM,mode='shadow')['state']=='uncertain'
        assert not service.can_retry_message('account','mail')
        with connect(path) as con:
            con.execute("UPDATE work_items SET status='dead',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE work_id=?",(work,))
            before=con.execute('SELECT updated_at FROM inference_invocations WHERE invocation_id=?',(call.invocation_id,)).fetchone()[0]
        InvocationRecoveryService(path).reconcile(call.invocation_id,expected_updated_at=before,command_id='audited-safe-failure',resolution='failed')
        assert service.can_retry_message('account','mail')
        changed={**request,'coverage':[{'source_id':'history','reason':'changed'}]}
        retry=service.claim(changed,SYSTEM)
        assert retry['state']=='claimed' and retry['analysis_id']==first['analysis_id']
        assert service.get_request(retry['analysis_id'])==request
        assert service.claim(replay_request,SYSTEM,mode='replay')['state']=='busy'


def test_active_claim_and_lost_checkpoint_cannot_be_bypassed_by_replay():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        first=service.claim(request,SYSTEM)
        replay={**request,'replay_id':'another-run'}
        assert service.claim(replay,SYSTEM,mode='replay')['state']=='busy'
        work=bind_test_work(path,service,first,'mail-understanding:'+first['analysis_id'])
        with invocation_scope(path,work,0):
            call=begin_invocation('test-provider','mail_understanding',b'private-request',reserved_tokens=1)
            call.submitting();call.terminal('completed')
        with connect(path) as con:
            con.execute("UPDATE mail_understanding_analyses SET lease_expires_at='2000-01-01T00:00:00Z'")
        assert service.claim(replay,SYSTEM,mode='replay')['state']=='uncertain'
        with connect(path) as con:
            assert con.execute('SELECT state FROM inference_invocations').fetchone()[0]=='unknown'
            assert con.execute('SELECT COUNT(*) FROM mail_understanding_analyses').fetchone()[0]==1


def test_sealed_failed_attempt_does_not_capture_later_parent_success():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        first=service.claim(request,SYSTEM)
        work=bind_test_work(path,service,first)
        with invocation_scope(path,work,0):
            failed=begin_invocation('test-provider','mail_understanding',b'first',reserved_tokens=1)
            failed.submitting();failed.terminal('failed')
        service.finish_inference_work(first['analysis_id'],first['claim_token'],work,[failed.invocation_id],SYSTEM)
        service.fail(first['analysis_id'],first['claim_token'],'provider_failed',SYSTEM)
        with invocation_scope(path,work,0):
            sibling=begin_invocation('test-provider','legacy_mail',b'unrelated sibling',reserved_tokens=1)
            sibling.submitting();sibling.terminal('completed')
        retry=service.claim(request,SYSTEM)
        assert retry['state']=='claimed' and retry['analysis_id']==first['analysis_id']
        with connect(path) as con:
            assert con.execute('SELECT state FROM inference_invocations WHERE invocation_id=?',(sibling.invocation_id,)).fetchone()[0]=='completed'
        try:service.finish_inference_work(first['analysis_id'],first['claim_token'],work,[failed.invocation_id,sibling.invocation_id],SYSTEM)
        except ConflictError:pass
        else:raise AssertionError('sealed attempt expanded to an unrelated sibling')


def test_unsealed_attempt_stops_at_next_binding_high_water():
    with TemporaryDirectory() as directory:
        path,ledger,service,request,app=setup(directory)
        first=service.claim(request,SYSTEM)
        work=bind_test_work(path,service,first)
        with invocation_scope(path,work,0):
            failed=begin_invocation('test-provider','mail_understanding',b'first',reserved_tokens=1)
            failed.submitting();failed.terminal('failed')
        service.fail(first['analysis_id'],first['claim_token'],'provider_failed',SYSTEM)
        second=service.claim(request,SYSTEM,mode='shadow')
        service.bind_inference_work(second['analysis_id'],second['claim_token'],work,0,SYSTEM)
        with invocation_scope(path,work,0):
            sibling=begin_invocation('test-provider','mail_understanding',b'next bound message',reserved_tokens=1)
            sibling.submitting();sibling.terminal('completed')
        service.save(second['analysis_id'],second['claim_token'],raw(request,app),SYSTEM)
        retry=service.claim(request,SYSTEM)
        assert retry['state']=='claimed'
        with connect(path) as con:
            assert con.execute('SELECT state FROM inference_invocations WHERE invocation_id=?',(sibling.invocation_id,)).fetchone()[0]=='completed'


def main():
    tests=[v for k,v in globals().items() if k.startswith('test_') and callable(v)]
    for test in tests:test()
    print(f'ok ({len(tests)} shared mail store tests)')


if __name__=='__main__':main()
