"""Offline browser registration, identity, lifecycle, replay and privacy checks."""
import json
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_search.autofill import AutofillBroker, EncryptedAutofillVault
from job_search.browser_tracking import BrowserTracking, identify_job
from job_search.contracts import ContractError, ConflictError, EventInput, ApplicationEventType, MutationContext, utc_now
from job_search.db import connect
from tests.test_job_search_autofill import make_ledger, profile, EXTENSION_ORIGIN, private_descriptors

URL='https://job-boards.greenhouse.io/acme/jobs/12345'

class Catalog:
    def get_job(self, ats, job_id):
        if job_id!='12345': raise ContractError('job was not found')
        return {'ats':ats,'id':job_id,'title':'Engineer','company':'Acme','jobUrl':URL}

class MemoryEncrypted:
    is_encrypted=True
    value=''
    def load(self): return self.value
    def save(self, value): self.value=value


def fixture(directory):
    ledger=make_ledger(directory)
    vault=EncryptedAutofillVault(Path(directory)/'vault',persistence=MemoryEncrypted())
    tracker=BrowserTracking(ledger,Catalog(),AutofillBroker(ledger,profile(),vault))
    issued=tracker.issue_pairing('local')
    enrollment=tracker.enroll(issued['pairing_code'],EXTENSION_ORIGIN,'local')
    device=tracker.authenticate(enrollment['device_token'],EXTENSION_ORIGIN,'local')
    return ledger,tracker,device,vault,enrollment


def observation(kind='attempted', attempt=None, url=URL, **kw):
    return {'observation_id':uuid.uuid4().hex,'attempt_id':attempt or uuid.uuid4().hex,
            'page_url':url,'kind':kind,'occurred_at':utc_now(),
            'metadata':{'signal':'success_dom'} if kind=='site_acknowledged' else {},**kw}


def raises(fn):
    try: fn()
    except (ContractError,ConflictError): return
    raise AssertionError('expected contract failure')


def test_identity_and_registration():
    assert identify_job(URL+'?utm_source=x#application')['job_id']=='12345'
    assert identify_job(URL+'/confirmation?source=email') == identify_job(URL)
    raises(lambda:identify_job(URL+'/verification'))
    raises(lambda:identify_job(URL+'/confirmation/other'))
    assert identify_job('https://boards.greenhouse.io/embed/job_app?for=acme&token=12345')['job_id']=='12345'
    raises(lambda:identify_job('https://evil.test/acme/jobs/12345'))
    raises(lambda:identify_job('https://job-boards.greenhouse.io/evil/12345/other'))
    raises(lambda:identify_job('https://user:secret@job-boards.greenhouse.io/acme/jobs/12345'))
    with tempfile.TemporaryDirectory() as d:
        ledger,t,device,vault,enrollment=fixture(d)
        assert t.resolve(URL)['known'] and not ledger.list_applications()
        raises(lambda:t.resolve(URL.replace('/acme/','/different/')))
        raises(lambda:t.authenticate(enrollment['device_token'],'chrome-extension://'+'b'*32,'local'))
        raises(lambda:t.authenticate(enrollment['device_token'],EXTENSION_ORIGIN,'https://other'))
        code=t.issue_pairing('local')['pairing_code'];t.enroll(code,EXTENSION_ORIGIN,'local')
        raises(lambda:t.enroll(code,EXTENSION_ORIGIN,'local'))
        assert enrollment['device_token'] not in json.dumps(t.devices())
        t.revoke(device)
        raises(lambda:t.authenticate(enrollment['device_token'],EXTENSION_ORIGIN,'local'))


def test_attempts_do_not_count_and_success_deduplicates():
    with tempfile.TemporaryDirectory() as d:
        ledger,t,device,vault,_=fixture(d)
        first=observation(); r=t.observe(device,first); aid=r['application_id']
        assert t.observe(device,first)==r
        assert ledger.get_application_timeline(aid)['application']['submitted_at'] is None
        assert not ledger.list_outbox()
        raises(lambda:t.observe(device,{**first,'kind':'failed'}))
        sent=observation('request_sent',first['attempt_id']);t.observe(device,sent)
        t.observe(device,observation('request_completed',first['attempt_id']))
        assert ledger.get_application_timeline(aid)['application']['submitted_at'] is None
        t.observe(device,observation('failed',first['attempt_id']))
        assert not ledger.list_outbox()
        # A new valid attempt, in another tab, still belongs to the same application.
        second=observation(); t.observe(device,second)
        assert t.attempt_status(device,first['attempt_id'])['attempt_id']==first['attempt_id']
        success=observation('site_acknowledged',second['attempt_id']);t.observe(device,success)
        t.observe(device,success)
        t.observe(device,observation('site_acknowledged',first['attempt_id']))
        events=ledger.get_application_timeline(aid)['events']
        assert sum(e['event_type']=='submission_observed' for e in events)==1
        assert len([x for x in ledger.list_outbox() if x['topic']=='recommendation.applied'])==1
        assert len(ledger.list_applications())==1
        assert t.application_status(aid)['status']=='site_acknowledged'
        # Restarted broker can authenticate and replay a committed observation.
        t2=BrowserTracking(ledger,Catalog(),t.autofill)
        assert t2.observe(device,success)['status']=='site_acknowledged'
        raises(lambda:t.observe(device,observation('site_acknowledged',uuid.uuid4().hex)))
        raises(lambda:t.observe(device,observation('failed',first['attempt_id'],URL.replace('12345','789'))))


def test_email_first_and_encrypted_capture():
    with tempfile.TemporaryDirectory() as d:
        ledger,t,device,vault,_=fixture(d)
        o=observation();r=t.observe(device,o);aid=r['application_id']
        t.stage_capture(device,{'attempt_id':o['attempt_id'],'fields':private_descriptors(),
            'answers':[{'field_id':'custom','value':'An example answer.'}]})
        assert not vault._load()['custom_history']
        # No raw answer leaks into any SQLite observation metadata.
        with connect(ledger.store.db_path) as con:
            dump='\n'.join(con.iterdump());assert 'An example answer' not in dump
        ledger.record_event(EventInput(aid,ApplicationEventType.SUBMISSION_CONFIRMED,utc_now(),{},'mail-confirm',MutationContext('mail-confirm','system','outlook')))
        assert t.application_status(aid)['status']=='email_confirmed'
        t.maintain_captures();assert len(vault._load()['custom_history'])==1
        t.maintain_captures();assert len(vault._load()['custom_history'])==1
        t.observe(device,observation('site_acknowledged',o['attempt_id']))
        assert len([x for x in ledger.list_outbox() if x['topic']=='recommendation.applied'])==1
        assert sum(e['event_type']=='submission_observed' for e in ledger.get_application_timeline(aid)['events'])==1


def test_unknown_import_unresolved_and_resume():
    with tempfile.TemporaryDirectory() as d:
        ledger,t,device,vault,_=fixture(d)
        old=(datetime.now(timezone.utc)-timedelta(minutes=12)).isoformat().replace('+00:00','Z')
        o=observation(url=URL.replace('12345','56789'),occurred_at=old,title='Backend Engineer',employer='Acme')
        r=t.observe(device,o)
        assert t.resolve(o['page_url'])['known']
        assert t.application_status(r['application_id'])['status']=='unresolved'
        assert any(x['kind']=='browser_submission' for x in ledger.list_attention_items())
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT count(*) FROM browser_jobs').fetchone()[0]==1
        raises(lambda:t.observe(device,observation(metadata={'request_body':'private'})))
        raises(lambda:t.observe(device,observation(resume_sha256='not-a-hash')))
        t.stage_capture(device,{'attempt_id':o['attempt_id'],'fields':private_descriptors(),'answers':[]})
        state=vault._load();state['pending_captures'][o['attempt_id']]['expires_at']=0;vault._save(state)
        t.maintain_captures();assert not vault._load()['pending_captures']


def test_uploaded_resume_matches_private_standard_bytes():
    import hashlib
    from tests.test_resume_lab_gateway import make_gateway, _pdf, FakeExtractor
    with tempfile.TemporaryDirectory() as d:
        gateway=make_gateway(Path(d))
        content='Taylor Example\nSoftware Engineer\nPython and PostgreSQL production experience.'
        pdf=_pdf(content)
        imported=gateway.import_existing_standard('Standard',1,pdf=pdf,tex_source='source',intended_text=content,extractor=FakeExtractor())
        digest=hashlib.sha256(pdf).hexdigest()
        assert gateway.match_uploaded_resume(digest)['standard_version_id']==imported['standard_version_id']
        assert gateway.match_uploaded_resume('f'*64) is None
        from job_search.service import JobSearchLedger
        ledger=JobSearchLedger(Path(d)/'applications.db')
        t=BrowserTracking(ledger,Catalog(),AutofillBroker(ledger,profile()),gateway)
        import base64
        attachment=t.resume_attachment(URL)['resume']
        assert base64.b64decode(attachment['content_base64'])==pdf
        assert attachment['sha256']==digest and attachment['filename']=='Sean_Example_Resume.pdf'
        assert not ledger.list_applications(), 'Reading a resume must not start an application'
        # Preferred standard is chosen by manual rank, not import recency.
        gateway.import_existing_standard('Secondary',2,pdf=_pdf(content+' Secondary.'),
            tex_source='secondary',intended_text=content+' Secondary.',extractor=FakeExtractor())
        assert gateway.get_autofill_resume().sha256==digest
        original_selection=gateway.get_selection
        original_artifact=gateway.get_artifact
        from job_search.resume_integration import ResumeArtifactContent
        chosen=ResumeArtifactContent('selected','selected.pdf','application/pdf',pdf,digest)
        gateway.get_selection=lambda app: {'selection':{'artifact_id':'selected'}}
        gateway.get_artifact=lambda aid: chosen if aid=='selected' else None
        assert gateway.get_autofill_resume('existing') is chosen
        gateway.get_selection=original_selection;gateway.get_artifact=original_artifact
        code=t.issue_pairing('local')['pairing_code']; e=t.enroll(code,EXTENSION_ORIGIN,'local')
        o=observation(resume_sha256=digest);r=t.observe(e['device_id'],o)
        t.observe(e['device_id'],observation('site_acknowledged',o['attempt_id']))
        snapshot=ledger.get_application_timeline(r['application_id'])['events'][-1]['payload']['resume']
        assert snapshot['decision']=='matched_upload' and snapshot['sha256']==digest
        exposed=gateway.get_application_resume_content(r['application_id'])
        assert exposed['available'] and 'PostgreSQL' in exposed['text']


def test_mail_context_includes_older_matching_attempt():
    from job_search.sync import OutlookMailCoordinator
    from job_search.contracts import JobSnapshot, RecommendationProvenance
    with tempfile.TemporaryDirectory() as d:
        ledger,t,device,vault,_=fixture(d)
        original=t.observe(device,observation())['application_id']
        for i in range(25):
            ledger.start_application(JobSnapshot('greenhouse',str(8000+i),'','Other role','Other Employer '+str(i),'other'+str(i),'https://job-boards.greenhouse.io/other/jobs/'+str(8000+i)),RecommendationProvenance(),MutationContext('other-'+str(i),'user','test'))
        coordinator=OutlookMailCoordinator.__new__(OutlookMailCoordinator);coordinator.service=ledger
        candidates,complete=coordinator._candidates('Thank you for applying to Acme','We received your application for Engineer at Acme.')
        assert complete and [c.application_id for c in candidates]==[original]


def test_registered_http_boundary_requires_token_and_origin():
    import threading
    from job_search.dashboard import DashboardController, make_server
    from tests.test_job_search_dashboard import FakePreferences, request
    with tempfile.TemporaryDirectory() as d:
        ledger,t,device,vault,enrollment=fixture(d)
        controller=DashboardController(ledger,FakePreferences(),autofill=t.autofill,jobs=Catalog())
        server=make_server(controller,0); thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            payload={'page_url':URL,'device_token':enrollment['device_token']}
            headers={'Origin':EXTENSION_ORIGIN}
            status,_,body=request(server,'POST','/api/v1/extension/resolve',payload,headers)
            assert status==200 and json.loads(body)['known']
            assert request(server,'POST','/api/v1/extension/resolve',{'page_url':URL},headers)[0]==400
            assert request(server,'POST','/api/v1/extension/resolve',payload,{'Origin':'https://evil.test'})[0]==403
            from job_search.resume_integration import ResumeArtifactContent
            from types import SimpleNamespace
            import hashlib, base64
            pdf=b'%PDF-1.7\nfixture\n%%EOF'
            controller.browser_tracking.resume_lab=SimpleNamespace(get_autofill_resume=lambda app:
                ResumeArtifactContent('fixture','resume.pdf','application/pdf',pdf,hashlib.sha256(pdf).hexdigest()))
            status,headers_out,body=request(server,'POST','/api/v1/extension/resume',payload,headers)
            assert status==200 and base64.b64decode(json.loads(body)['resume']['content_base64'])==pdf
            assert request(server,'POST','/api/v1/extension/resume',{'page_url':URL},headers)[0]==400
            assert request(server,'POST','/api/v1/extension/resume',payload,{'Origin':'https://evil.test'})[0]==403
            assert request(server,'POST','/api/v1/extension/resume',{**payload,'page_url':'https://evil.test'},headers)[0]==400
            t.revoke(device)
            assert request(server,'POST','/api/v1/extension/resume',payload,headers)[0]==400
            assert request(server,'POST','/api/v1/extension/resolve',payload,headers)[0]==400
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    tests=[v for k,v in list(globals().items()) if k.startswith('test_')]
    for test in tests:test()
    print(f'ok ({len(tests)} browser tracking tests)')
