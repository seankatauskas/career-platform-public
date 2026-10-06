"""Offline exact send, Sent evidence, privacy and confirmation regressions."""
from tempfile import TemporaryDirectory
import hashlib
import json
from datetime import datetime,timezone,timedelta
from job_search.career_actions import CareerActionService
from job_search.contracts import MutationContext,ConflictError,ContractError
from job_search.db import connect
from job_search.outlook.client import GraphOutlookClient
from job_search.outlook.transport import GraphOutcomeUnknown,GraphLinkError
from job_search.outlook.auth import SEND_SCOPES
from tests.test_job_search_ledger import make_service,start,context
from tests.test_job_search_outlook import session_with,response

NOW='2030-01-01T12:00:00Z'
SYSTEM=MutationContext('career-worker','system','career_worker')
SLOT={'starts_at':'2030-01-02T15:00:00Z','ends_at':'2030-01-02T16:00:00Z'}


class FakeOutlook:
    def __init__(self):
        self.source={'id':'incoming','conversationId':'thread','subject':'Interview','isDraft':False,
            'from':{'emailAddress':{'address':'recruiter@example.com'}},'replyTo':[],
            'body':{'contentType':'text','content':'Please reply'},'lastModifiedDateTime':NOW}
        self.sent=0;self.draft=None;self.fail_send=False;self.events=[];self.writes=[];self.agenda_items=[]
    def preflight_action(self,kind):pass
    def read_message_body(self,id):return dict(self.source) if id=='incoming' else self.draft
    def create_reply_draft(self,id):
        self.draft={'id':'draft','isDraft':True,'subject':'Re: Interview','body':{},'toRecipients':[{'emailAddress':{'address':'recruiter@example.com'}}]}
        return {'id':'draft'}
    def update_reply_draft(self,id,body):self.draft['body']={'contentType':'text','content':body}
    def send_reply_draft(self,id):
        self.sent+=1
        if self.fail_send:raise TimeoutError('uncertain')
        return {'accepted':True}
    def read_agenda(self,a,b):return self.agenda_items
    def read_interview_events(self,a,b):return self.events
    def read_calendar_view(self,a,b):return []
    def write_private_commitment(self,a,b,tx,**kw):
        self.writes.append((a,b,tx,kw));return {'id':'event','changeKey':'version1'}


def setup(directory, *, body='Please reply', subject='Interview'):
    path,ledger=make_service(directory);app=start(ledger)['application']['application_id']
    from job_search.mail.sanitizer import sanitize_mail
    clean=sanitize_mail(subject,body,max_chars=2048)
    evidence=ledger.store.record_mail_evidence({'account_id':'account','immutable_message_id':'incoming','conversation_id':'thread','sender':'recruiter@example.com','subject':subject,'received_at':NOW,'body_sha256':clean.content_sha256,'excerpt':clean.text},context('evidence'))['evidence']['evidence_id']
    observation=ledger.lifecycle.observe_mail({'account_id':'account','immutable_message_id':'incoming','conversation_id':'thread','direction':'inbound','sender':'recruiter@example.com','received_at':NOW,'evidence_id':evidence},SYSTEM)
    with connect(path) as con:
        oid=con.execute('SELECT observation_id FROM lifecycle_mail_observations').fetchone()[0]
        con.execute("INSERT INTO lifecycle_mail_links VALUES (?,?,1,'user',?)",(oid,app,NOW))
        con.execute("UPDATE automation_controls SET enabled=1 WHERE capability IN ('outlook_send','calendar_commitments')")
    provider=FakeOutlook()
    provider.source.update(subject=subject,body={'contentType':'text','content':body})
    service=CareerActionService(ledger,outlook=provider,account_id='account',now_provider=lambda:NOW)
    return path,ledger,service,provider,app,evidence


def proposed(service,app,evidence,slots=()):
    result=service.propose_reply(app,evidence,'Thank you. I can discuss the opportunity.' + ''.join(' '+s['starts_at']+' to '+s['ends_at'] for s in slots),context('propose'),offered_slots=slots)
    service.decide_proposal(result['proposal_id'],'approved',result['payload_hash'],context('approve'),expected_source_hash=result['source_hash'])
    return result


def test_graph_send_empty_202_is_dedicated_and_not_draft_success():
    session,tokens,http=session_with(response(202,None))
    assert GraphOutlookClient(session).send_reply_draft('draft')['accepted']
    assert tokens.calls[0][0]==SEND_SCOPES
    assert http.requests[0][2]['Content-Length']=='0'
    session,_,_=session_with(response(201,None))
    try:GraphOutlookClient(session).create_reply_draft('incoming')
    except GraphOutcomeUnknown:pass
    else:raise AssertionError('empty draft creation must stay ambiguous')


def test_exact_hash_expiry_and_pause_block_sending():
    with TemporaryDirectory() as directory:
        path,_,service,outlook,app,eid=setup(directory)
        proposal=service.propose_reply(app,eid,'Exact body',context('proposal'))
        try:service.decide_proposal(proposal['proposal_id'],'approved','wrong',context('wrong'))
        except ConflictError:pass
        else:raise AssertionError('wrong hash approved')
        service.decide_proposal(proposal['proposal_id'],'approved',proposal['payload_hash'],context('approve'))
        with connect(path) as con:con.execute("UPDATE automation_controls SET enabled=0 WHERE capability='outlook_send'")
        try:service.execute(proposal['proposal_id'],SYSTEM)
        except ConflictError:pass
        else:raise AssertionError('paused sending escaped')
        assert outlook.sent==0
        service.now_provider=lambda:'2030-01-01T12:16:00Z'
        service.run_tick(SYSTEM)
        assert service.get_proposal(proposal['proposal_id'])['status']=='expired'


def test_202_needs_observed_sent_and_timeout_never_resends():
    with TemporaryDirectory() as directory:
        _,_,service,outlook,app,eid=setup(directory)
        proposal=proposed(service,app,eid)
        outlook.fail_send=True
        assert service.execute(proposal['proposal_id'],SYSTEM)['status']=='uncertain'
        service.run_tick(SYSTEM)
        service.execute(proposal['proposal_id'],SYSTEM)
        assert outlook.sent==1


def test_source_change_and_draft_recipient_change_prevent_send():
    with TemporaryDirectory() as directory:
        _,_,service,outlook,app,eid=setup(directory)
        proposal=proposed(service,app,eid)
        outlook.source['body']={'contentType':'text','content':'Different request'}
        assert service.execute(proposal['proposal_id'],SYSTEM)['status']=='expired'
        assert outlook.sent==0
    with TemporaryDirectory() as directory:
        _,_,service,outlook,app,eid=setup(directory)
        proposal=proposed(service,app,eid)
        original=outlook.update_reply_draft
        def edit(id,body):
            original(id,body);outlook.draft['toRecipients']=[{'emailAddress':{'address':'other@example.com'}}]
        outlook.update_reply_draft=edit
        assert service.execute(proposal['proposal_id'],SYSTEM)['status']=='failed'
        assert outlook.sent==0


def test_source_freeze_model_worker_needs_no_outlook_credentials():
    with TemporaryDirectory() as directory:
        _,ledger,service,_,app,eid=setup(directory)
        service.prepare_reply_context(app,eid,SYSTEM)
        model=CareerActionService(ledger,account_id='account',reply_provider=lambda app,evidence:{'body':'Thank you for the update.','missing_information':[]},now_provider=lambda:NOW)
        result=model.prepare_reply(app,eid,context('prepare'))
        assert result['status']=='READY' and result['proposal']['recipients']==['recruiter@example.com']
        model.now_provider=lambda:'2030-01-01T12:16:00Z'
        try:model.prepare_reply(app,eid,context('stale'))
        except ContractError:pass
        else:raise AssertionError('stale source must not generate review')


def test_agenda_private_redaction_atomic_failure_and_freshness():
    with TemporaryDirectory() as directory:
        _,_,service,outlook,_,_=setup(directory)
        outlook.agenda_items=[{'id':'private','source_ref':'private',**SLOT,'title':'Sensitive appointment','private':True,'body':'discard','is_all_day':False}]
        result=service.refresh_agenda(NOW,'2030-01-05T12:00:00Z',SYSTEM)
        assert result['coverage']['complete']
        assert result['items'][0]['title']=='Private commitment' and 'body' not in result['items'][0]
        outlook.read_agenda=lambda a,b:(_ for _ in ()).throw(ValueError('paging failed'))
        try:service.refresh_agenda(NOW,'2030-01-05T12:00:00Z',SYSTEM)
        except ValueError:pass
        result=service.agenda(NOW,'2030-01-05T12:00:00Z')
        assert len(result['items'])==1 and result['coverage']['reason']=='refresh_failed'


def test_confirmed_slot_requires_sent_observation_then_private_commitment():
    with TemporaryDirectory() as directory:
        path,ledger,service,outlook,app,eid=setup(directory)
        proposal=proposed(service,app,eid,[SLOT]);service.execute(proposal['proposal_id'],SYSTEM)
        def observe(id,direction,excerpt,sender,when):
            from job_search.mail.sanitizer import sanitize_mail
            sanitized=sanitize_mail('Re: Interview' if direction=='outbound' else 'Interview',excerpt,max_chars=2048)
            ev=ledger.store.record_mail_evidence({'account_id':'account','immutable_message_id':id,'conversation_id':'thread','sender':sender,'subject':sanitized.subject,'received_at':when,'body_sha256':sanitized.content_sha256,'excerpt':sanitized.text},context('ev-'+id))['evidence']['evidence_id']
            result=ledger.lifecycle.observe_mail({'account_id':'account','immutable_message_id':id,'conversation_id':'thread','direction':direction,'sender':sender,'recipients':['recruiter@example.com'] if direction=='outbound' else ['me@example.com'],'received_at':when,'sent_at':when if direction=='outbound' else None,'evidence_id':ev},MutationContext('obs-'+id,'system','outlook'))
            with connect(path) as con:
                oid=con.execute('SELECT observation_id FROM lifecycle_mail_observations WHERE immutable_message_id=?',(id,)).fetchone()[0]
                con.execute("INSERT OR IGNORE INTO lifecycle_mail_links VALUES (?,?,1,'user',?)",(oid,app,when))
            return oid
        observe('confirmation','inbound','Confirmed '+SLOT['starts_at']+' to '+SLOT['ends_at'],'recruiter@example.com','2030-01-01T12:02:00Z')
        assert service.reconcile_confirmations(SYSTEM)==[]
        oid=observe('draft','outbound',proposal['body'],'me@example.com','2030-01-01T12:01:00Z')
        assert service.observe_sent({'observation_id':oid},SYSTEM)['matched']
        assert service.reconcile_confirmations(SYSTEM)[0]['status']=='pending'
        result=service.reconcile_commitments(SYSTEM)
        assert result['results'][0]['status']=='created' and len(outlook.writes)==1
        rounds=ledger.lifecycle.list_interview_rounds(application_id=app)['rounds']
        assert len(rounds)==1 and rounds[0]['status']=='confirmed'
        assert rounds[0]['calendar_event_id']==''  # own creation never becomes employer calendar evidence
        with connect(path) as con:
            assert con.execute("SELECT count(*) FROM interview_reminders WHERE status='pending'").fetchone()[0]==2
        observe('cancel-message','inbound','The interview is cancelled.','recruiter@example.com','2030-01-01T12:03:00Z')
        result=service.reconcile_confirmations(SYSTEM)
        assert result[0]['status']=='pending_cancel'
        assert ledger.lifecycle.list_interview_rounds(application_id=app)['rounds'][0]['status']=='cancelled'
        with connect(path) as con:
            assert con.execute("SELECT count(*) FROM interview_reminders WHERE status='pending'").fetchone()[0]==0




def test_natural_slot_confirmation_and_timezone_ambiguity():
    from job_search.career_actions.slots import matched_slots,authored_text,label
    one={**SLOT,'time_zone':'America/Chicago'}
    # Jan 2 2030 is Wednesday; 15Z is 9am CST.
    assert matched_slots('Wednesday at 9 works',[one])==[one]
    assert matched_slots('That works, confirmed',[one])==[one]
    assert matched_slots('Wednesday at 9 ET works',[one])==[]
    assert matched_slots('Wednesday at 9 and at 10 works',[one])==[]
    assert matched_slots('10 works',[one])==[]
    assert matched_slots('That works, confirmed\nJoin: https://meet.example.com/123456',[one])==[one]
    assert matched_slots('Wednesday at 9 CST works',[one])==[one]
    other={'starts_at':'2030-01-02T21:00:00Z','ends_at':'2030-01-02T22:00:00Z','time_zone':'America/Chicago'}
    assert matched_slots('Wednesday at 9 works',[one,other])==[one]
    assert matched_slots('That works, confirmed',[one,other])==[]
    assert authored_text('Thanks\nOn Tuesday recruiter wrote:\nWednesday at 9 confirmed')=='Thanks'
    assert authored_text('Forwarded message\nWednesday at 9 confirmed')==''
    assert '09:00 AM' in label(one) and 'CST' in label(one)


def test_calendar_patch_and_delete_require_version_and_never_invite():
    from job_search.outlook.transport import RetryClass
    from job_search.outlook.auth import HOLD_SCOPES
    session,_,http=session_with(response(201,{'id':'event','changeKey':'one'}),response(200,{'id':'event','changeKey':'two'}),response(204,None))
    client=GraphOutlookClient(session)
    client.write_private_commitment(SLOT['starts_at'],SLOT['ends_at'],'stable')
    client.write_private_commitment(SLOT['starts_at'],SLOT['ends_at'],'stable',remote_id='event',etag='W/"one"')
    client.delete_private_commitment('event','W/"two"')
    create=json.loads(http.requests[0][3]);patch=json.loads(http.requests[1][3])
    assert create['attendees']==[] and create['showAs']=='busy' and create['sensitivity']=='private'
    assert patch['attendees']==[] and 'transactionId' not in patch
    assert http.requests[1][2]['If-Match']=='W/"one"'
    try:client.delete_private_commitment('event','')
    except ValueError:pass
    else:raise AssertionError('unversioned deletion accepted')
    try:session.request_json('POST','/v1.0/me/events/event/accept',scopes=HOLD_SCOPES,retry_class=RetryClass.IDEMPOTENT_WRITE)
    except GraphLinkError:pass
    else:raise AssertionError('invitation acceptance escaped allowlist')


def test_ready_reply_reuses_unchanged_pending_review_without_generation():
    with TemporaryDirectory() as directory:
        _,ledger,service,outlook,app,eid=setup(directory)
        service.prepare_reply_context(app,eid,SYSTEM)
        count=[]
        def generate(a,e):count.append(1);return {'body':'Thank you.','missing_information':[]}
        model=CareerActionService(ledger,account_id='account',reply_provider=generate,now_provider=lambda:NOW)
        first=model.prepare_reply(app,eid,context('first'))
        second=model.prepare_reply(app,eid,context('second'))
        assert first['proposal_id']==second['proposal_id'] and len(count)==1



def seed_commitment(path,service,app,eid):
    proposal=proposed(service,app,eid,[SLOT])
    with connect(path) as con:
        con.execute("UPDATE career_send_proposals SET status='observed_sent' WHERE proposal_id=?",(proposal['proposal_id'],))
        con.execute("INSERT INTO career_commitments(commitment_id,proposal_id,confirmation_evidence_id,starts_at,ends_at,status,transaction_id,organizer,created_at,updated_at) VALUES ('commitment',?,?,?,?, 'pending','transaction','recruiter@example.com',?,?)",(proposal['proposal_id'],eid,SLOT['starts_at'],SLOT['ends_at'],NOW,NOW))
    return proposal


def test_owned_calendar_cancel_is_versioned_and_rejects_attendees():
    with TemporaryDirectory() as directory:
        path,_,service,outlook,app,eid=setup(directory)
        seed_commitment(path,service,app,eid)
        assert service.reconcile_commitments(SYSTEM)['results'][0]['status']=='created'
        owned={'id':'event','transactionId':'transaction','changeKey':'version1','@odata.etag':'etag1','attendees':[], 'isOrganizer':True,'sensitivity':'private','start':{'dateTime':SLOT['starts_at'],'timeZone':'UTC'},'end':{'dateTime':SLOT['ends_at'],'timeZone':'UTC'}}
        outlook.read_owned_event=lambda id:owned
        deleted=[];outlook.delete_private_commitment=lambda id,etag:deleted.append((id,etag))
        current=service.list_commitments()['commitments'][0]
        service.review_commitment('commitment','cancelled',context('cancel'),expected_version=current['version'])
        try:service.review_commitment('commitment','confirmed',context('old'),expected_version=current['version'])
        except ConflictError:pass
        else:raise AssertionError('stale decision resurrected cancellation')
        owned['attendees']=[{'emailAddress':{'address':'external@example.com'}}]
        assert service.reconcile_commitments(SYSTEM)['results'][0]['status']=='needs_review'
        assert deleted==[]


def test_confirmed_calendar_overlap_is_preserved_and_flagged():
    from job_search.contracts import CalendarBlock
    with TemporaryDirectory() as directory:
        path,_,service,outlook,app,eid=setup(directory)
        seed_commitment(path,service,app,eid)
        outlook.read_calendar_view=lambda a,b:[CalendarBlock('other',SLOT['starts_at'],SLOT['ends_at'],'busy',False,False,'key')]
        result=service.reconcile_commitments(SYSTEM)
        assert result['results'][0]['status']=='created' and result['results'][0]['conflicts']==['other']
        assert len(outlook.writes)==1
        assert json.loads(service.list_commitments()['commitments'][0]['conflict_json'])==['other']


def test_existing_recruiter_invite_prevents_private_duplicate():
    with TemporaryDirectory() as directory:
        path,_,service,outlook,app,eid=setup(directory)
        seed_commitment(path,service,app,eid)
        outlook.events=[{'remote_id':'invite',**SLOT,'organizer':'recruiter@example.com','is_organizer':False,'is_cancelled':False,'change_key':'v1','modified_at':NOW}]
        result=service.reconcile_commitments(SYSTEM)
        assert result['results'][0]['status']=='linked_invite' and outlook.writes==[]
        rounds=service.ledger.lifecycle.list_interview_rounds(application_id=app)['rounds']
        assert len(rounds)==1 and rounds[0]['calendar_event_id']=='invite'



def test_grounded_availability_preparation_selects_verified_slots_only():
    from types import SimpleNamespace
    with TemporaryDirectory() as directory:
        _,ledger,service,outlook,app,eid=setup(directory)
        ledger.lifecycle.create_task(app,{'kind':'send_availability','owner':'applicant','evidence_id':eid},context('need-times'))
        service.prepare_reply_context(app,eid,SYSTEM)
        class Model:
            def generate(self,messages,**kwargs):
                inputs=json.loads(messages[1]['content'])
                assert inputs['availability_requested'] and len(inputs['verified_candidate_slots'])==3
                return SimpleNamespace(text=json.dumps({'body':'Here are some times that work.','offered_slots':inputs['verified_candidate_slots'][:2],'missing_information':[]}))
        model=CareerActionService(ledger,account_id='account',reply_provider=Model(),now_provider=lambda:NOW)
        prepared=model.prepare_reply(app,eid,context('prepare-times'))
        assert prepared['status']=='READY' and len(prepared['proposal']['offered_slots'])==2
        assert 'Offered times:' in prepared['proposal']['body'] and 'CST' in prepared['proposal']['body']
        assert prepared['proposal']['offered_slots'][0]['time_zone']=='America/Chicago'


def test_changed_calendar_blocks_send_after_exact_approval():
    from job_search.contracts import CalendarBlock
    with TemporaryDirectory() as directory:
        _,_,service,outlook,app,eid=setup(directory)
        proposal=proposed(service,app,eid,[SLOT])
        outlook.read_calendar_view=lambda a,b:[CalendarBlock('new-conflict',SLOT['starts_at'],SLOT['ends_at'],'busy',False,False,'v')]
        result=service.execute(proposal['proposal_id'],SYSTEM)
        assert result['status']=='failed' and outlook.sent==0


def test_ordinary_recruiter_question_creates_one_reply_obligation():
    with TemporaryDirectory() as directory:
        path,ledger,service,outlook,app,eid=setup(directory)
        assert len(service.record_reply_obligations(SYSTEM))==1
        assert service.record_reply_obligations(SYSTEM)==[]
        with connect(path) as con:
            rows=con.execute("SELECT kind,evidence_id FROM lifecycle_tasks WHERE application_id=?",(app,)).fetchall()
        assert len(rows)==1 and rows[0]['kind']=='reply' and rows[0]['evidence_id']==eid


def test_application_receipts_do_not_create_reply_tasks_or_change_phase():
    # Fictional equivalents of rhetorical receipt text and an ATS footer URL.
    receipts = [
        ('Application confirmation', 'Please accept this email as confirmation of your application.\n\n'
         'So, what happens next?!\n\nOur team will review your application and contact you if there are next steps.'),
        ('Thanks for applying', 'Thank you for applying. We will review your application and get back to you.\n\n'
         'Report this email<https://ats.example.com/report-abuse?organizationId=example&emailId=receipt>'),
        ('Your application', 'Our hiring team is reviewing your profile.\n\n'
         'If you have any questions, just let us know.'),
    ]
    for subject, body in receipts:
        with TemporaryDirectory() as directory:
            path,ledger,service,outlook,app,eid=setup(directory,subject=subject,body=body)
            with connect(path) as con:
                before=con.execute('SELECT current_phase FROM applications WHERE application_id=?',(app,)).fetchone()[0]
            assert service.record_reply_obligations(SYSTEM)==[], subject
            assert service.record_reply_obligations(SYSTEM)==[], subject
            with connect(path) as con:
                assert con.execute('SELECT count(*) FROM lifecycle_tasks').fetchone()[0]==0
                assert con.execute('SELECT current_phase FROM applications WHERE application_id=?',(app,)).fetchone()[0]==before
            assert outlook.sent==0


def test_reply_requests_ignore_support_boilerplate_links_and_quoted_history():
    from job_search.career_actions.reply_requests import reply_request_evidence
    from job_search.mail.sanitizer import sanitize_mail
    bodies = [
        'What happens next? We will review your application.',
        'Why join our team? Read more on our website.',
        'Check https://example.com/faq?question=please%20reply for details.',
        'Check www.example.com/faq?question=available for details.',
        'If you have questions, please reply to this email.',
        'Please reply if you have any questions.',
        'If you need assistance,\nplease let us know.',
        'Any questions? Please reply to this email.',
        'Feel free to let us know if you need anything.',
        'Please do not reply to this automated message.',
        'No reply is required. Please confirm your details in the portal.',
        'No need to send anything yet.',
        'We will let you know our decision.',
        'Thanks for the update.\n> Could you share your availability?',
        'Thanks for the update.\nOn Tuesday recruiter wrote:\nPlease send your availability.',
        'Forwarded message\nAre you available tomorrow?',
    ]
    for body in bodies:
        clean=sanitize_mail('Could you reply? Interview availability',body,max_chars=2048)
        assert reply_request_evidence(clean.text) is None, body


def test_explicit_requests_preserve_evidence_without_claiming_an_interview():
    bodies = [
        'Please reply to confirm receipt.',
        'Could you share your availability for a call?',
        'Are you available for a call next week?',
        'What is your availability?',
        'When could you start?',
        'Would you be interested in discussing this role?',
        'Let us know which time works for you.',
        'Could you please send your updated resume?',
        'Please confirm your notice period.',
        'Thank you for applying. Please share your availability. If you have questions, please reply.',
        'Thanks for applying.\n\nCould you share your salary expectations?\n\nIf you need help, let us know.',
    ]
    for body in bodies:
        with TemporaryDirectory() as directory:
            path,ledger,service,outlook,app,eid=setup(directory,body=body)
            result=service.record_reply_obligations(SYSTEM)
            assert len(result)==1, body
            task=result[0]
            assert task['kind']=='reply' and task['owner']=='applicant'
            assert task['evidence_id']==eid
            assert task['policy_version']=='explicit-reply-request-v2'
            quote=task['note'].removeprefix('Reply requested: ')
            assert quote and quote in body, body
            assert service.record_reply_obligations(SYSTEM)==[]
            with connect(path) as con:
                assert con.execute("SELECT count(*) FROM application_events WHERE event_type LIKE 'interview%'").fetchone()[0]==0
            assert outlook.sent==0


def test_reply_task_guards_preserve_direction_existing_tasks_and_corrections():
    for direction in ('outbound','draft','unknown'):
        with TemporaryDirectory() as directory:
            path,ledger,service,outlook,app,eid=setup(directory)
            with connect(path) as con:
                con.execute('UPDATE lifecycle_mail_observations SET direction=?',(direction,))
            assert service.record_reply_obligations(SYSTEM)==[]
    with TemporaryDirectory() as directory:
        path,ledger,service,outlook,app,eid=setup(directory)
        ledger.lifecycle.create_task(app,{'kind':'send_availability','owner':'applicant','evidence_id':eid},context('interview-request'))
        assert service.record_reply_obligations(SYSTEM)==[]
    with TemporaryDirectory() as directory:
        path,ledger,service,outlook,app,eid=setup(directory)
        task=service.record_reply_obligations(SYSTEM)[0]
        ledger.lifecycle.transition_task(task['task_id'],'cancel',{'reason':'Reviewed; no reply needed'},context('correction'))
        assert service.record_reply_obligations(SYSTEM)==[]
        with connect(path) as con:
            assert con.execute('SELECT status FROM lifecycle_tasks').fetchone()[0]=='cancelled'
            assert con.execute('SELECT count(*) FROM lifecycle_task_revisions').fetchone()[0]==2


def main():
    tests=[v for k,v in globals().items() if k.startswith('test_') and callable(v)]
    for test in tests:test()
    print(f'ok ({len(tests)} career action tests)')
if __name__=='__main__':main()
