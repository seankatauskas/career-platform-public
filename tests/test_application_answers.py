"""Application answer history: complete values, auth, replay, and restart coverage."""
import copy
import json
import sqlite3
import tempfile
import threading
import uuid

from job_search.application_answers import save_snapshot, application_snapshots, validate_snapshot
from job_search.contracts import utc_now
from job_search.db import connect
from job_search.dashboard import DashboardController, make_server
from tests.test_browser_tracking import fixture, observation, raises, Catalog, URL
from tests.test_job_search_autofill import EXTENSION_ORIGIN
from tests.test_job_search_dashboard import FakePreferences, request


def capture(attempt):
    return {'capture_id':uuid.uuid4().hex,'attempt_id':attempt,'page_url':URL,'captured_at':utc_now(),
        'snapshot':{'version':1,'omitted_fields':0,'truncated_values':0,'fields':[
            {'field_key':'why','prompt':'Why this company?','section':'Application','control':'textarea','value':'First paragraph.\n\nSecond paragraph with <script>literal text</script>. Cafe\u0301 \U0001f680'},
            {'field_key':'name','prompt':'Full name','section':'','control':'text','value':'Taylor Example'},
            {'field_key':'salary','prompt':'Desired compensation','section':'','control':'text','value':'150000'},
            {'field_key':'terms','prompt':'I agree','section':'','control':'checkbox','value':True},
            {'field_key':'locations','prompt':'Locations','section':'','control':'select','value':['Chicago','Remote']},
            {'field_key':'resume','prompt':'Resume','section':'','control':'file','value':['Taylor_Resume.pdf']},
            {'field_key':'optional','prompt':'Anything else?','section':'','control':'text','value':''},
        ]}}


def test_history_is_durable_exact_and_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        ledger,tracker,device,vault,enrollment=fixture(directory)
        obs=observation(); app=tracker.observe(device,obs)['application_id']
        body=capture(obs['attempt_id']); path=ledger.store.db_path
        result=save_snapshot(path,device,body)
        assert result['saved'] and result['field_count']==7
        assert save_snapshot(path,device,body)==result
        assert application_snapshots(path,app)[0]['snapshot']==body['snapshot']
        assert ledger.get_application_timeline(app)['application']['submitted_at'] is None
        changed=copy.deepcopy(body);changed['snapshot']['fields'][0]['value']='Edited'
        raises(lambda:save_snapshot(path,device,changed))
        second=observation();tracker.observe(device,second)
        changed['capture_id']=uuid.uuid4().hex;changed['attempt_id']=second['attempt_id']
        save_snapshot(path,device,changed)
        controller=DashboardController(ledger,FakePreferences(),autofill=tracker.autofill,jobs=Catalog())
        assert len(controller.application_workspace(app)['answer_snapshots'])==2
        assert controller.application_workspace(app)['answer_snapshots'][0]['snapshot']['fields'][0]['value']=='Edited'
        with connect(path) as con:
            try: con.execute('UPDATE application_answer_snapshots SET captured_at=?',(utc_now(),))
            except sqlite3.IntegrityError: pass
            else: raise AssertionError('snapshots must be immutable')


def test_capture_rejects_cross_application_and_invalid_fields():
    with tempfile.TemporaryDirectory() as directory:
        ledger,tracker,device,vault,enrollment=fixture(directory)
        obs=observation();app=tracker.observe(device,obs)['application_id']
        body=capture(obs['attempt_id']);path=ledger.store.db_path
        raises(lambda:save_snapshot(path,'different-device',body))
        raises(lambda:save_snapshot(path,device,{**body,'attempt_id':uuid.uuid4().hex}))
        raises(lambda:save_snapshot(path,device,{**body,'page_url':URL.replace('12345','9999')}))
        raises(lambda:save_snapshot(path,device,{**body,'page_url':URL.replace('/acme/','/other/')}))
        raises(lambda:save_snapshot(path,device,{**body,'captured_at':'2099-01-01T00:00:00Z'}))
        for update in ({'control':'password'},{'control':[]},{'value':{'nested':'not allowed'}},{'value':'\ud800'},{'field_key':''}):
            bad=copy.deepcopy(body['snapshot']);bad['fields'][0].update(update)
            raises(lambda:validate_snapshot(bad))
        bad=copy.deepcopy(body['snapshot']);bad['fields'].append(bad['fields'][0])
        raises(lambda:validate_snapshot(bad))
        assert application_snapshots(path,app)==[]


def test_authenticated_capture_accepts_long_open_text_and_rejects_revoked_device():
    with tempfile.TemporaryDirectory() as directory:
        ledger,tracker,device,vault,enrollment=fixture(directory)
        controller=DashboardController(ledger,FakePreferences(),autofill=tracker.autofill,jobs=Catalog())
        server=make_server(controller,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            obs=observation(); app=tracker.observe(device,obs)['application_id']
            body=capture(obs['attempt_id']);body['device_token']=enrollment['device_token']
            # Larger than the ordinary 64 KiB request ceiling, preserving Unicode.
            body['snapshot']['fields'][0]['value']='Detailed experience \u2014 Cafe\u0301 \U0001f680 '+('abc\n'*14000)
            body['snapshot']['fields'][1]['value']='x'*20000
            headers={'Origin':EXTENSION_ORIGIN}
            status,_,raw=request(server,'POST','/api/v1/extension/answers',body,headers)
            assert status==200,raw
            assert json.loads(raw)['saved']
            assert application_snapshots(ledger.store.db_path,app)[0]['snapshot']==body['snapshot']
            status,_,raw=request(server,'GET',f'/api/v1/applications/{app}/workspace')
            assert status==200,raw
            assert json.loads(raw)['answer_snapshots'][0]['snapshot']==body['snapshot']
            assert request(server,'POST','/api/v1/extension/answers',body,{'Origin':'https://evil.test'})[0]==403
            tracker.revoke(device)
            assert request(server,'POST','/api/v1/extension/answers',body,headers)[0]==400
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    tests=[value for name,value in list(globals().items()) if name.startswith('test_')]
    for test in tests:test()
    print(f'ok ({len(tests)} application answer history tests)')
