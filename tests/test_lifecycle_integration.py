"""Cross-surface lifecycle acceptance using real services and fictional evidence."""
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from job_search.contracts import MutationContext, ContractError
from job_search.hermes import HermesAdapter, HermesValidationError
from job_search.hermes_mcp import _mcp_tools
from job_search.lifecycle.dashboard import mutate
from job_search.service import JobSearchLedger
from tests.test_job_search_ledger import start, stamp
from tests.test_job_search_hermes import FakeCapabilities
from tests.test_job_search_dashboard import dashboard, session, post, request


def ctx(key, actor='user'):
    return MutationContext(key,actor,'lifecycle_acceptance')


def adapter(ledger):
    capabilities=replace(FakeCapabilities().capabilities(), lifecycle_call=ledger.lifecycle.call_tool)
    return HermesAdapter(capabilities)


def test_hermes_proposal_dashboard_decision_and_briefing_share_state():
    with dashboard() as (server,controller,ledger,_):
        app=start(ledger)['application']['application_id']
        agent=adapter(ledger)
        proposed=agent.invoke('propose_application_update',{'application_id':app,'kind':'task','payload':{'values':{'kind':'complete_assessment','owner':'applicant','note':'Submit the take-home','due_at':stamp(86400)}},'idempotency_key':'agent-task'})
        assert not ledger.lifecycle.list_tasks(app)
        proposal_id=proposed['proposal']['proposal_id']
        cookie,csrf=session(server)
        command={'proposal_id':proposal_id,'decision':'accepted','idempotency_key':'approve-task'}
        denied=request(server,'POST','/api/v1/lifecycle/corrections/decide',command,{'Cookie':cookie})
        assert denied[0] == 403
        status,_,body=post(server,'/api/v1/lifecycle/corrections/decide',command,cookie,csrf)
        assert status == 200, body
        assert post(server,'/api/v1/lifecycle/corrections/decide',command,cookie,csrf)[0] == 200
        briefing=agent.invoke('get_application_briefing',{'application_id':app})
        task=briefing['next_obligations'][0]
        assert task['owner']=='applicant' and 'take-home' in briefing['explanation']
        assert len(ledger.lifecycle.list_tasks(app))==1
        status,_,body=request(server,'GET',f'/api/v1/applications/{app}/briefing',headers={'Cookie':cookie})
        assert status==200,body
        assert json.loads(body)['next_obligations'][0]['task_id']==task['task_id']
        assert controller.application_workspace(app)['briefing']['next_obligations'][0]['task_id']==task['task_id']
        assert not briefing['coverage']['complete']
        assert not any('decide' in tool['name'] for tool in _mcp_tools(agent))
        for forbidden in ('decide_correction','send_mail','create_task'):
            try: agent.invoke(forbidden,{})
            except HermesValidationError: pass
            else: raise AssertionError('agent acquired direct mutation authority')


def test_browser_commands_accept_real_task_detail_and_interview_shapes():
    with tempfile.TemporaryDirectory() as directory:
        ledger=JobSearchLedger(Path(directory)/'ledger.db');app=start(ledger)['application']['application_id']
        task=mutate(ledger.lifecycle,'tasks/create',{'application_id':app,'values':{'kind':'send_availability','owner':'applicant','note':'Send times'}},ctx('task'))['task']
        mutate(ledger.lifecycle,'tasks/transition',{'task_id':task['task_id'],'operation':'snooze','values':{'snoozed_until':stamp(86400)}},ctx('snooze'))
        record=mutate(ledger.lifecycle,'details/record',{'application_id':app,'kind':'assessment','values':{'status':'requested','title':'Take home','note':'Use the portal','due_at':stamp(86400)}},ctx('detail'))
        assert record['detail']['details']['title']=='Take home'
        proposed=mutate(ledger.lifecycle,'interviews/propose',{'application_id':app,'details':{'round_kind':'Technical','status':'confirmed','starts_at':stamp(172800),'ends_at':stamp(176400),'time_zone':'UTC'}},ctx('interview'))
        mutate(ledger.lifecycle,'interviews/decide',{'proposal_id':proposed['revision']['revision_id'],'decision':'accepted'},ctx('interview-approve'))
        briefing=ledger.lifecycle.get_application_briefing(app)
        assert briefing['interviews']['rounds'][0]['status']=='confirmed'
        assert len([r for r in briefing['reminders'] if r['source']=='interview'])==2
        assert ledger.lifecycle.list_upcoming_interviews()[0]['round_kind']=='Technical'
        mutate(ledger.lifecycle,'follow-up/configure',{'application_id':app,'after_days':7},ctx('followup'))
        mutate(ledger.lifecycle,'tasks/transition',{'task_id':task['task_id'],'operation':'complete'},ctx('complete'))
        assert next(t for t in ledger.lifecycle.list_tasks(app) if t['task_id']==task['task_id'])['status']=='completed'


def test_persisted_reopen_proposals_are_audited_and_stale_reviews_rejected():
    with tempfile.TemporaryDirectory() as directory:
        ledger=JobSearchLedger(Path(directory)/'ledger.db');app=start(ledger)['application']['application_id']
        agent=adapter(ledger)
        proposal=agent.invoke('propose_application_update',{'application_id':app,'kind':'phase','payload':{'target_phase':'active','reason':'User reported a phone conversation'},'idempotency_key':'phase'})['proposal']
        ledger.lifecycle.decide_correction(proposal['proposal_id'],'accepted',ctx('phase-decision'))
        assert ledger.get_application_timeline(app)['application']['current_phase']=='active'
        assert len(ledger.get_application_timeline(app)['events'])==2
        try: mutate(ledger.lifecycle,'tasks/create',{'application_id':app,'values':{'kind':'reply'}},ctx('bad','hermes'))
        except ContractError: pass
        else: raise AssertionError('Hermes bypassed dashboard user boundary')


def test_history_pages_and_replay_controls_share_authenticated_service():
    from tests.test_job_search_lifecycle_mail import observation
    from job_search.outlook.state import SQLiteOutlookState
    from tests.test_job_search_sync import change
    from job_search.lifecycle.runtime import MailReplayTaskHandler
    with dashboard() as (server,controller,ledger,_):
        app=start(ledger)['application']['application_id']
        service=ledger.lifecycle
        task=service.create_task(app,{'kind':'reply','owner':'applicant'},ctx('task'))['task']
        service.transition_task(task['task_id'],'complete',{},ctx('done'))
        agent=adapter(ledger)
        first=agent.invoke('get_application_record_history',{'kind':'task','record_id':task['task_id'],'limit':1})
        assert first['next_revision']==1 and not first['complete']
        second=agent.invoke('get_application_record_history',{'kind':'task','record_id':task['task_id'],'after_revision':1})
        assert second['complete'] and second['items'][0]['state']['status']=='completed'
        for i in range(3):
            item=service.observe_mail(observation('message-'+str(i)),ctx('observe-'+str(i),'system'))['observation']
            service.link_mail({'observation_id':item['observation_id'],'application_id':app},ctx('link-'+str(i)))
        cookie,csrf=session(server)
        status,_,body=request(server,'GET',f'/api/v1/applications/{app}/conversation?limit=2',headers={'Cookie':cookie})
        page=json.loads(body);assert status==200 and len(page['items'])==2 and page['next_cursor']
        message_id=page['items'][0]['observation_id']
        status,_,body=request(server,'GET',f'/api/v1/applications/{app}/conversation/{message_id}',headers={'Cookie':cookie})
        assert status==200 and not json.loads(body)['available']
        status,_,_=request(server,'GET',f'/api/v1/applications/unlinked/conversation/{message_id}',headers={'Cookie':cookie})
        assert status==400
        second=agent.invoke('list_application_conversation',{'application_id':app,'cursor':page['next_cursor']})
        assert len(second['items'])==1 and second['complete']
        SQLiteOutlookState(ledger.store.db_path).stage_changes('personal','inbox',[change()])
        status,_,body=post(server,'/api/v1/lifecycle/replay/start',{'account_id':'personal','since_at':'2026-08-01T00:00:00Z','until_at':'2026-10-01T00:00:00Z','idempotency_key':'replay'},cookie,csrf)
        assert status==200,body
        replay=json.loads(body)['replay']
        status,_,body=request(server,'GET','/api/v1/lifecycle/replays',headers={'Cookie':cookie})
        assert status==200 and 'personal' in json.loads(body)['accounts']
        class Coordinator:
            def process_replay(self,*args,**kwargs):
                raise AssertionError('worker crossed Outlook account boundary')
        class Context:
            def heartbeat(self):return True
        assert MailReplayTaskHandler(Coordinator(),service,account_id='another-account')({},Context())=={'replays':0}
        status,_,body=post(server,'/api/v1/lifecycle/replay/transition',{'replay_id':replay['replay_id'],'operation':'cancel','idempotency_key':'cancel-replay'},cookie,csrf)
        assert status==200,body
        assert not service.list_pending_replays(account_id='personal')


def test_briefing_prioritizes_open_work_before_bounded_closed_history():
    with tempfile.TemporaryDirectory() as directory:
        ledger=JobSearchLedger(Path(directory)/'ledger.db');app=start(ledger)['application']['application_id']
        service=ledger.lifecycle
        for i in range(100):
            task=service.create_task(app,{'kind':'reply','due_at':stamp(100),'owner':'applicant'},ctx('task-'+str(i)))['task']
            service.transition_task(task['task_id'],'complete',{},ctx('done-'+str(i)))
        pending=service.create_task(app,{'kind':'send_document','note':'Send portfolio','owner':'applicant'},ctx('pending'))['task']
        briefing=service.get_application_briefing(app)
        assert briefing['next_obligations'][0]['task_id']==pending['task_id']
        assert briefing['tasks'][0]['task_id']==pending['task_id']
        assert 'Send portfolio' in briefing['explanation'] and briefing['truncated']


if __name__=='__main__':
    tests=[value for name,value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:test()
    print(f'ok ({len(tests)} lifecycle integration tests)')
