"""Offline attention, grounded briefs, activation, and delivery invariants."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from job_search.attention import AttentionService
from job_search.attention.policy import DEFAULTS, slot_time
from job_search.contracts import MutationContext, ContractError, ConflictError
from job_search.db import connect
from job_search.notifications import NotificationIntent
from job_search.service import JobSearchLedger
from tests.test_job_search_ledger import start


class Clock:
    def __init__(self,value='2026-10-05T12:00:00Z'):
        self.value=datetime.fromisoformat(value.replace('Z','+00:00'))
    def __call__(self): return self.value
    def advance(self,minutes): self.value+=timedelta(minutes=minutes)
    def stamp(self): return self.value.isoformat(timespec='seconds').replace('+00:00','Z')


def ctx(key,actor='system'): return MutationContext(key,actor,'attention_test')

def setup(directory,clock=None):
    clock=clock or Clock()
    ledger=JobSearchLedger(Path(directory)/'db')
    attention=AttentionService(ledger,now_provider=clock)
    app=start(ledger)['application']['application_id']
    return ledger,attention,app,clock


def live(attention,**changes):
    return attention.update_preferences({'shadow':False,'minimum_alert_gap_minutes':0,**changes},attention.preferences()['revision'],ctx('prefs:'+str(attention.preferences()['revision']),'user'))


def candidate(attention,key='offer',**values):
    return attention.record_candidate(dict(source_kind='notice',source_id=key,title='Offer received',topic='application.offer_received',source_at=attention._stamp(),**values),ctx('candidate:'+key))['candidate']


def notifications(ledger):
    with connect(ledger.store.db_path) as con:
        return [dict(r) for r in con.execute("SELECT * FROM notification_outbox WHERE policy_id='chief-of-staff-v1'")]


def raises(error,fn):
    try: fn()
    except error: return
    raise AssertionError('expected '+error.__name__)


def test_defaults_and_revision_checked_actor_guard():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        prefs=s.preferences()
        assert prefs['shadow'] and prefs['ai_enabled'] and prefs['overnight_enabled']
        assert not prefs['quiet_hours_enabled'] and prefs['mode']=='important_developments'
        assert prefs['morning_time']=='07:00' and prefs['evening_time']=='19:00'
        raises(ContractError,lambda:s.update_preferences({'shadow':False},0,ctx('bad','hermes')))
        live(s)
        raises(ConflictError,lambda:s.update_preferences({'enabled':False},0,ctx('stale','user')))
        with connect(ledger.store.db_path) as con:
            assert con.execute("SELECT enabled FROM automation_controls WHERE capability='briefing_ai'").fetchone()[0]==0


def test_shadow_records_decisions_without_delivery_and_new_development_alerts_once():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        candidate(s)
        result=s.evaluate(context=ctx('shadow'))
        assert result['items'][0]['route']=='urgent' and not notifications(ledger)
        live(s)
        candidate(s,'new-offer')
        s.evaluate(context=ctx('live'))
        s.evaluate(context=ctx('retry'))
        assert len(notifications(ledger))==1
        assert any(i['reason']=='activation_baseline' for i in s.evaluate(context=ctx('baseline'))['items'])


def test_late_ingestion_is_not_backdated_observation_and_old_mail_is_briefed():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s)
        old=s.record_candidate(dict(source_kind='mail',source_id='old',topic='application.offer_received',source_at='2026-08-01T12:00:00Z',title='Old offer'),ctx('old'))['candidate']
        assert old['observed_at']==clock.stamp() and old['source_at']!=old['observed_at']
        s.evaluate(context=ctx('eval'))
        assert not notifications(ledger)
        fresh=s.record_candidate(dict(source_kind='mail',source_id='late-but-current',topic='application.offer_received',source_at='2026-10-05T11:00:00Z',title='Current offer'),ctx('late-current'))['candidate']
        s.evaluate(context=ctx('eval-current'))
        assert len(notifications(ledger))==1


def test_all_night_important_mode_then_risk_only():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T07:00:00Z'));live(s)
        candidate(s)
        s.evaluate(context=ctx('night'))
        assert len(notifications(ledger))==1
        live(s,mode='risk_only')
        candidate(s,'next-offer')
        result=s.evaluate(context=ctx('risk'))
        assert next(i for i in result['items'] if i['candidate_id']==s.list_candidates()['items'][0]['candidate_id'])['route']=='briefing'


def test_future_obligations_are_collected_and_single_final_nudge():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s)
        due=(clock()+timedelta(minutes=90)).isoformat(timespec='seconds').replace('+00:00','Z')
        ledger.lifecycle.create_task(app,dict(kind='complete_assessment',owner='applicant',note='Complete assessment',due_at=due),ctx('task','user'))
        s.evaluate(context=ctx('collect'))
        first=notifications(ledger)[0]
        assert first['status']=='pending'
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE notification_outbox SET status='delivered' WHERE notification_id=?",(first['notification_id'],))
        clock.advance(65)
        s.evaluate(context=ctx('nudge'))
        s.evaluate(context=ctx('nudge-again'))
        assert len(notifications(ledger))==2


def test_completing_source_or_acknowledging_prevents_pending_delivery():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s)
        task=ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',due_at='2026-10-05T13:00:00Z'),ctx('task','user'))['task']
        s.evaluate(context=ctx('collect'))
        notification=notifications(ledger)[0]
        ledger.lifecycle.transition_task(task['task_id'],'complete',{},ctx('complete','user'))
        with connect(ledger.store.db_path) as con:
            assert not AttentionService.validate_delivery(con,notification,clock.stamp())
        assert notifications(ledger)[0]['status']=='cancelled'
        item=candidate(s,'another');s.evaluate(context=ctx('next'))
        s.acknowledge(item['candidate_id'],ctx('ack','hermes'),expected_revision=1)
        raises(ConflictError,lambda:s.snooze(item['candidate_id'],'2026-10-05T15:00:00Z',ctx('stale','hermes'),expected_revision=1))
        assert all(row['status']=='cancelled' for row in notifications(ledger))


def test_quiet_hours_defer_only_when_explicitly_enabled_and_overnight_disabled():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T07:00:00Z'))
        live(s,quiet_hours_enabled=True,overnight_enabled=False)
        candidate(s);result=s.evaluate(context=ctx('quiet'))
        assert result['items'][0]['reason']=='quiet_hours' and not notifications(ledger)
        clock.advance(300)
        s.evaluate(context=ctx('awake'))
        assert len(notifications(ledger))==1


def test_pause_resume_quarantines_backlog_and_read_gate_blocks_managed_send():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s)
        candidate(s);s.evaluate(context=ctx('queued'))
        row=notifications(ledger)[0]
        with connect(ledger.store.db_path) as con:
            con.execute("INSERT INTO automation_controls VALUES ('notifications',0,1,?)",(clock.stamp(),))
            assert not AttentionService.validate_delivery(con,row,clock.stamp())
            AttentionService.on_activation_changed(con,False,clock.stamp())
            con.execute("UPDATE automation_controls SET enabled=1 WHERE capability='notifications'")
            AttentionService.on_activation_changed(con,True,clock.stamp())
        s.evaluate(context=ctx('resume'))
        assert all(item['status']=='cancelled' for item in notifications(ledger))


def test_portfolio_counts_complete_before_display_limit_and_agenda_gap_visible():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        for index in range(85):
            ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Task '+str(index)),ctx('task'+str(index),'user'))
        preview=s.preview()
        assert preview['snapshot']['counts']['open_tasks']==85
        assert len(preview['snapshot']['facts'])==80 and preview['snapshot']['omitted_facts']==5
        assert preview['snapshot']['coverage']['agenda']['reason']=='not_configured'
        assert not s.history()['items']


def test_briefing_finalizer_fallback_and_late_ai_do_not_duplicate():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T11:55:00Z'));live(s)
        prepared=s.prepare_briefing('morning')['briefing']
        assert s.finalize_briefing(prepared['briefing_id'])['pending']
        clock.advance(5)
        finalized=s.finalize_briefing(prepared['briefing_id'])['briefing']
        assert finalized['renderer']=='deterministic' and finalized['status']=='finalized'
        result=s.complete_generation(prepared['briefing_id'],{'ordered_refs':[]})
        assert result['outcome']=='late_ignored'
        s.finalize_briefing(prepared['briefing_id'],ctx('again'))
        assert len(notifications(ledger))==1
        assert s.prepare_briefing('morning')['briefing']['briefing_id']==prepared['briefing_id']


def test_grounded_model_rejects_invented_ref_owner_action_and_accepts_valid_summary():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        task=ledger.lifecycle.create_task(app,dict(kind='offer_decision',owner='applicant',note='Review offer terms'),ctx('task','user'))['task']
        briefing=s.prepare_briefing('morning')['briefing'];ref='task:'+task['task_id']
        invalids=[{'ordered_refs':['invented']},{'ordered_refs':[ref],'summary_statements':[{'kind':'waiting','fact_refs':[ref]}]}, {'ordered_refs':[ref],'suggested_next_steps':[{'action':'send','fact_ref':ref}]}]
        for index,output in enumerate(invalids):
            assert s.complete_generation(briefing['briefing_id'],output,ctx('invalid'+str(index)))['outcome']=='invalid_fallback'
        output={'ordered_refs':[ref],'summary_statements':[{'kind':'needs_you','fact_refs':[ref]}],'suggested_next_steps':[{'action':'review','fact_ref':ref}]}
        assert s.complete_generation(briefing['briefing_id'],output)['outcome']=='accepted'
        assert 'Your attention is needed' in s.finalize_briefing(briefing['briefing_id'])['briefing']['body']


def test_provider_uses_existing_interface_and_never_emits_unsupported_claims():
    from job_search.inference.contracts import GenerationResult
    class Provider:
        max_input_tokens=12000
        def count_tokens_upper_bound(self,text): return len(text)//3
        def generate(self,messages,**kwargs):
            assert kwargs['max_output_tokens']==1500 and kwargs['schema_name']=='career_attention_briefing'
            return GenerationResult('{"ordered_refs":[]}',{}, {'model':'fixture'})
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);s.generation_provider=Provider()
        briefing=s.prepare_briefing('morning')['briefing']
        assert s.generate_briefing(briefing['briefing_id'])['outcome']=='automation_paused'
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE automation_controls SET enabled=1 WHERE capability='briefing_ai'")
        assert s.generate_briefing(briefing['briefing_id'])['outcome']=='accepted'
        assert s.generate_briefing(briefing['briefing_id'])['outcome']=='already_generated'


def test_dst_slots_daily_uniqueness_and_missed_slots_do_not_flood():
    values=dict(DEFAULTS,morning_time='01:30')
    assert slot_time('morning','2026-11-01',values).isoformat()=='2026-11-01T06:30:00+00:00'
    values['morning_time']='02:30'
    assert slot_time('morning','2026-03-08',values).isoformat()=='2026-03-08T08:00:00+00:00'
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T22:00:00Z'))
        assert not s.due_briefings()
        morning=s.prepare_briefing('morning','2026-10-05')['briefing']
        assert s.finalize_briefing(morning['briefing_id'])['briefing']['status']=='expired'
        assert not notifications(ledger)


def test_monday_week_ahead_and_friday_recap_share_daily_slots():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        assert s.preview('morning')['snapshot']['variant']=='week_ahead'
        clock.value=datetime(2026,10,10,0,0,tzinfo=timezone.utc)
        assert s.preview('evening')['snapshot']['variant']=='week_recap'


def test_notification_adapter_uses_authoritative_task_fields_and_shared_candidate():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s)
        task=ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Verified reply',due_at='2026-10-05T13:00:00Z'),ctx('task','user'))['task']
        intent=NotificationIntent('reminder.due','legacy-source','Untrusted title','Untrusted body',app,{'reminder_id':'task:'+task['task_id']})
        result=s.from_notification(intent,now=clock.stamp())
        assert result['candidate']['title']=='Verified reply' and result['suppressed'] is False
        s.evaluate(context=ctx('collect'))
        assert len(s.list_candidates()['items'])==1 and len(notifications(ledger))==1



def test_deadline_before_next_brief_interrupts_before_two_hour_window_and_risks_ignore_soft_cap():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s,maximum_alerts_per_day=1,minimum_alert_gap_minutes=120)
        candidate(s);s.evaluate(context=ctx('first'))
        # 18:00 Chicago is eleven hours away, but the next briefing is 19:00.
        ledger.lifecycle.create_task(app,dict(kind='complete_assessment',owner='applicant',note='Deadline before evening brief',due_at='2026-10-05T23:00:00Z'),ctx('deadline','user'))
        result=s.evaluate(context=ctx('risk'))
        assert any(item['reason']=='deadline_risk' for item in result['items'])
        assert len(notifications(ledger))==2


def test_briefings_only_preserves_explicit_timed_reminders():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s,mode='briefings_only')
        reminder=ledger.create_reminder(dict(application_id=app,note='Call recruiter now',due_at='2026-10-05T11:59:00Z'),ctx('reminder','user'))['reminder']
        result=s.from_notification(NotificationIntent('reminder.due',reminder['reminder_id'],'Reminder','Call recruiter now',app,{'reminder_id':reminder['reminder_id']}))
        assert result['decision']['reason']=='explicit_reminder' and len(notifications(ledger))==1


def test_prepare_five_minutes_early_and_publish_exactly_at_slot_with_dashboard_link():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T11:55:00Z'));live(s)
        s.dashboard_url='https://career.example.test'
        tick=s.process_tick()
        assert len(tick['generation_pending'])==1 and not notifications(ledger)
        bid=tick['briefings'][0]['briefing_id']
        s.complete_generation(bid,{'ordered_refs':[]})
        clock.advance(1)
        assert s.finalize_briefing(bid).get('pending') and not notifications(ledger)
        clock.advance(4)
        finalized=s.finalize_briefing(bid)['briefing']
        assert finalized['status']=='finalized'
        queued=notifications(ledger)[0]
        assert queued['available_at']=='2026-10-05T12:00:00Z'
        assert '/#settings/chief?briefing='+bid in queued['body']
        assert not ledger.claim_notification('early','2026-10-05T11:59:00Z')
        assert ledger.claim_notification('on-time',clock.stamp())['notification_id']==queued['notification_id']


def test_rendered_refs_include_only_visible_complete_lines_and_reserve_link():
    from job_search.attention.portfolio import render
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        for index in range(12):
            ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Marker[%02d] '%index+'detail '*55),ctx('long'+str(index),'user'))
        snap=s.preview()['snapshot']
        title,body,refs=render(snap,dashboard_url='https://career.example.test/#settings/chief')
        assert len(body)<=1950 and len(refs)<12
        for fact in snap['facts']:
            marker=fact['label'].split(' ',1)[0]
            assert (marker in body)==(fact['ref'] in refs)
        assert 'Coverage is incomplete' in body and body.endswith('/#settings/chief')


def test_daily_itinerary_dedupes_linked_calendar_and_evening_looks_to_tomorrow():
    from tests.test_job_search_interviews import accepted
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        interview=accepted(ledger.lifecycle,app,starts_at='2026-10-05T15:00:00Z',ends_at='2026-10-05T16:00:00Z')['round']
        s.agenda_provider=lambda start,end:dict(items=[dict(id='calendar-1',round_id=interview['round_id'],starts_at='2026-10-05T15:00:00Z',ends_at='2026-10-05T16:00:00Z',title='Duplicate interview'),dict(id='calendar-2',starts_at='2026-10-06T16:00:00Z',ends_at='2026-10-06T17:00:00Z',title='Tomorrow appointment')],coverage={'complete':True})
        morning=s.preview('morning')['snapshot']
        assert not any(f['label']=='Duplicate interview' for f in morning['facts'])
        assert any(f['ref']=='interview:'+interview['round_id'] for f in morning['facts'])
        evening=s.preview('evening')['snapshot']
        assert any(f['label']=='Tomorrow appointment' and f['section']=='itinerary' for f in evening['facts'])
        assert not any(f['ref']=='interview:'+interview['round_id'] for f in evening['facts'])


def test_late_ingested_message_is_delta_once_presented_and_friday_has_completed_recap():
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s,ai_enabled=False)
        first=s.prepare_briefing('morning')['briefing'];done=s.finalize_briefing(first['briefing_id'])['briefing']
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE notification_outbox SET status='delivered' WHERE notification_id=?",(done['notification_id'],))
        clock.advance(60)
        with patch('job_search.store.utc_now',return_value=clock.stamp()):
            mail=ledger.lifecycle.observe_mail(dict(account_id='personal',immutable_message_id='late',direction='inbound',received_at='2026-09-01T12:00:00Z',modified_at='2026-09-01T12:00:00Z',sender='recruiter@example.test',subject='Old message newly discovered'),ctx('late'))['observation']
            ledger.lifecycle.link_mail(dict(observation_id=mail['observation_id'],application_id=app),ctx('link','user'))
            task=ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Response completed'),ctx('task','user'))['task']
            ledger.lifecycle.transition_task(task['task_id'],'complete',{},ctx('complete','user'))
        evening=s.preview('evening')['snapshot']
        assert evening['counts']['new_messages']==1 and evening['counts']['completed_tasks']==1
        message=next(f for f in evening['facts'] if f['kind']=='mail')
        assert message['at']=='2026-09-01T12:00:00Z' and message['observed_at']==clock.stamp()
        clock.value=datetime(2026,10,10,0,0,tzinfo=timezone.utc)
        recap=s.preview('evening')['snapshot']
        assert recap['variant']=='week_recap' and any(f['kind']=='completed' for f in recap['facts'])


def test_activation_preserves_inflight_delivery_for_receipt_reconciliation():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s)
        candidate(s);s.evaluate(context=ctx('queue'))
        row=notifications(ledger)[0]
        ledger.claim_notification('sender',clock.stamp())
        with connect(ledger.store.db_path) as con:
            AttentionService.on_activation_changed(con,False,clock.stamp())
            current=con.execute('SELECT * FROM notification_outbox WHERE notification_id=?',(row['notification_id'],)).fetchone()
            assert current['status']=='delivering' and current['lease_token']


def test_summary_drops_completed_fact_before_finalization():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T11:55:00Z'))
        task=ledger.lifecycle.create_task(app,dict(kind='offer_decision',owner='applicant',note='Review terms today'),ctx('task','user'))['task']
        briefing=s.prepare_briefing('morning')['briefing'];ref='task:'+task['task_id']
        s.complete_generation(briefing['briefing_id'],{'ordered_refs':[ref],'summary_statements':[{'kind':'needs_you','fact_refs':[ref]}]})
        ledger.lifecycle.transition_task(task['task_id'],'complete',{},ctx('complete','user'))
        clock.advance(5)
        result=s.finalize_briefing(briefing['briefing_id'])['briefing']
        assert 'Review terms today' not in result['body'] and ref not in result['selected_refs']


def test_critical_dates_survive_model_omission_and_summary_budget():
    from job_search.attention.portfolio import render
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        task=ledger.lifecycle.create_task(app,dict(kind='complete_assessment',owner='applicant',note='Submit exercise',due_at='2026-10-05T17:00:00Z'),ctx('critical','user'))['task']
        for index in range(3):
            ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Long context '+('detail '*55)),ctx('context'+str(index),'user'))
        snapshot=s.preview()['snapshot'];ref='task:'+task['task_id']
        other=[f['ref'] for f in snapshot['facts'] if f['ref']!=ref]
        title,body,refs=render(snapshot,ordered_refs=other,generation={'summary_statements':[{'kind':'needs_you','fact_refs':other}]})
        assert ref in refs and 'Submit exercise (Mon Oct 05, 12:00 PM CDT)' in body
        assert body.index('Submit exercise')<body.find('Your attention') or 'Your attention' not in body


def test_budget_deferral_propagates_while_fallback_finalization_remains_available():
    from job_search.inference.usage import UsageDeferred
    class Provider:
        max_input_tokens=12000
        def count_tokens_upper_bound(self,text): return 1
        def generate(self,*args,**kwargs): raise UsageDeferred('budget reached','2026-10-05T13:00:00Z')
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T11:55:00Z'));s.generation_provider=Provider()
        briefing=s.prepare_briefing('morning')['briefing']
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE automation_controls SET enabled=1 WHERE capability='briefing_ai'")
        raises(UsageDeferred,lambda:s.generate_briefing(briefing['briefing_id']))
        clock.advance(5)
        assert s.finalize_briefing(briefing['briefing_id'])['briefing']['status']=='finalized'


def test_explicit_reminder_delivery_preserves_requested_time_during_quiet_hours():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T07:00:00Z'))
        live(s,mode='briefings_only',quiet_hours_enabled=True)
        reminder=ledger.create_reminder(dict(application_id=app,note='Requested overnight reminder',due_at=clock.stamp()),ctx('reminder','user'))['reminder']
        s.from_notification(NotificationIntent('reminder.due',reminder['reminder_id'],'Reminder','Requested overnight reminder',app,{'reminder_id':reminder['reminder_id']}))
        row=notifications(ledger)[0]
        with connect(ledger.store.db_path) as con:
            assert AttentionService.validate_delivery(con,row,clock.stamp())


def test_only_delivered_exact_briefing_fact_suppresses_alert_and_allows_one_final_nudge():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s,ai_enabled=False)
        task=ledger.lifecycle.create_task(app,dict(kind='complete_assessment',owner='applicant',note='Submit exercise',due_at='2026-10-05T17:00:00Z'),ctx('task','user'))['task']
        briefing=s.prepare_briefing('morning')['briefing'];final=s.finalize_briefing(briefing['briefing_id'])['briefing']
        s.evaluate(context=ctx('before-receipt'))
        urgent=next(r for r in notifications(ledger) if r['topic']=='attention.urgent')
        with connect(ledger.store.db_path) as con:
            assert AttentionService.validate_delivery(con,urgent,clock.stamp())
            con.execute("UPDATE notification_outbox SET status='delivered',delivered_at=? WHERE notification_id=?",(clock.stamp(),final['notification_id']))
            assert not AttentionService.validate_delivery(con,urgent,clock.stamp())
        result=s.evaluate(context=ctx('after-receipt'))
        assert result['items'][0]['reason']=='covered_by_delivered_briefing'
        clock.advance(275)
        s.evaluate(context=ctx('final-nudge'));s.evaluate(context=ctx('repeat-final'))
        active=[r for r in notifications(ledger) if r['topic']=='attention.urgent' and r['status']=='pending']
        assert len(active)==1
        with connect(ledger.store.db_path) as con:
            assert con.execute('SELECT reason FROM attention_decisions WHERE decision_id=?',(active[0]['attention_decision_id'],)).fetchone()[0]=='final_nudge'


def test_unshown_or_revised_briefing_fact_never_suppresses_critical_alert():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s,ai_enabled=False)
        task=ledger.lifecycle.create_task(app,dict(kind='complete_assessment',owner='applicant',note='Submit exercise',due_at='2026-10-05T17:00:00Z'),ctx('task','user'))['task']
        briefing=s.finalize_briefing(s.prepare_briefing('morning')['briefing']['briefing_id'])['briefing']
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE notification_outbox SET status='delivered',delivered_at=? WHERE notification_id=?",(clock.stamp(),briefing['notification_id']))
            con.execute("UPDATE attention_briefings SET selected_refs_json='[]' WHERE briefing_id=?",(briefing['briefing_id'],))
        s.evaluate(context=ctx('not-shown'))
        assert len([r for r in notifications(ledger) if r['topic']=='attention.urgent'])==1


def test_briefing_waits_for_all_top_three_reply_results_and_uses_company_names():
    from tests.test_lifecycle_core import evidence
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d,Clock('2026-10-05T11:55:00Z'));live(s,ai_enabled=False)
        ids=[]
        for index in range(3):
            eid,_=evidence(ledger,app,key='reply'+str(index));ids.append(eid)
            ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Reply to recruiter',evidence_id=eid),ctx('task'+str(index),'user'))
        with connect(ledger.store.db_path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS career_send_proposals (proposal_id TEXT,application_id TEXT,evidence_id TEXT,source_hash TEXT,status TEXT,expires_at TEXT,account_id TEXT,payload_json TEXT,payload_hash TEXT,created_at TEXT,updated_at TEXT)')
            con.execute('CREATE TABLE IF NOT EXISTS career_reply_sources (application_id TEXT,evidence_id TEXT,source_hash TEXT,checked_at TEXT,source_json TEXT)')
            for pid,eid in (('first',ids[0]),('third',ids[2])):
                con.execute('INSERT INTO career_send_proposals (proposal_id,application_id,evidence_id,source_hash,status,expires_at,account_id,payload_json,payload_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(pid,app,eid,'source','pending','2026-10-06T12:00:00Z','account','{}','hash',clock.stamp(),clock.stamp()))
                con.execute('INSERT INTO career_reply_sources (application_id,evidence_id,source_hash,checked_at,source_json) VALUES (?,?,?,?,?)',(app,eid,'source',clock.stamp(),'{}'))
        briefing=s.prepare_briefing('morning')['briefing'];bid=briefing['briefing_id']
        s.record_ready_reply(bid,{'status':'READY','proposal_id':'first'},app,ids[0])
        assert s.finalize_briefing(bid).get('pending')
        clock.advance(1)
        s.record_ready_reply(bid,{'status':'missing_information','missing_information':['availability']},app,ids[1])
        assert s.finalize_briefing(bid).get('pending')
        clock.advance(1)
        s.record_ready_reply(bid,{'status':'READY','proposal_id':'third'},app,ids[2])
        assert s.finalize_briefing(bid).get('pending')
        clock.advance(3)
        final=s.finalize_briefing(bid)['briefing']
        assert final['status']=='finalized' and app not in final['body'], final
        assert 'Reply ready for ' in final['body'] and 'please provide your available times' in final['body']
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE career_send_proposals SET status='rejected' WHERE proposal_id='first'")
            row=con.execute('SELECT * FROM notification_outbox WHERE notification_id=?',(final['notification_id'],)).fetchone()
            assert not AttentionService.validate_delivery(con,row,clock.stamp())


def test_body_budget_overflow_count_includes_unshown_snapshot_facts():
    from job_search.attention.portfolio import render
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        for index in range(10):
            ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Task '+str(index)+' details '*55),ctx('task'+str(index),'user'))
        snapshot=s.preview()['snapshot'];_,body,refs=render(snapshot)
        omitted=snapshot['fact_count']-len(refs)
        assert snapshot['omitted_facts']==0 and omitted>0
        assert str(omitted)+' additional facts are available' in body


def test_changed_pending_briefing_is_rebuilt_once_and_delivered_receipt_never_replaced():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s,ai_enabled=False)
        task=ledger.lifecycle.create_task(app,dict(kind='complete_assessment',owner='applicant',note='Now completed exercise',due_at='2026-10-05T17:00:00Z'),ctx('task','user'))['task']
        briefing=s.finalize_briefing(s.prepare_briefing('morning')['briefing']['briefing_id'])['briefing']
        original=notifications(ledger)[0]
        ledger.lifecycle.transition_task(task['task_id'],'complete',{},ctx('done','user'))
        with connect(ledger.store.db_path) as con:
            assert not AttentionService.validate_delivery(con,original,clock.stamp())
        refreshed=s.get_briefing(briefing['briefing_id'])
        assert refreshed['notification_id']!=original['notification_id']
        assert 'Next step: Acme — Platform Engineer: Now completed exercise' not in refreshed['body']
        assert refreshed['snapshot']['counts']['applicant_tasks']==0
        with connect(ledger.store.db_path) as con:
            assert not AttentionService.validate_delivery(con,original,clock.stamp())
            current=con.execute('SELECT * FROM notification_outbox WHERE notification_id=?',(refreshed['notification_id'],)).fetchone()
            assert AttentionService.validate_delivery(con,current,clock.stamp())
            con.execute("UPDATE notification_outbox SET status='delivered',delivered_at=? WHERE notification_id=?",(clock.stamp(),refreshed['notification_id']))
            assert not AttentionService.validate_delivery(con,original,clock.stamp())
        assert len(notifications(ledger))==2


def test_unknown_attempt_is_not_replaced_under_new_notification_identity():
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d);live(s,ai_enabled=False)
        task=ledger.lifecycle.create_task(app,dict(kind='reply',owner='applicant',note='Old obligation'),ctx('task','user'))['task']
        briefing=s.finalize_briefing(s.prepare_briefing('morning')['briefing']['briefing_id'])['briefing']
        ledger.lifecycle.transition_task(task['task_id'],'complete',{},ctx('done','user'))
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE notification_outbox SET attempts=2 WHERE notification_id=?",(briefing['notification_id'],))
            row=con.execute('SELECT * FROM notification_outbox WHERE notification_id=?',(briefing['notification_id'],)).fetchone()
            assert not AttentionService.validate_delivery(con,row,clock.stamp())
        assert len(notifications(ledger))==1


def test_ready_proposal_rejected_after_newer_inbound_even_with_fresh_cached_hash():
    from job_search.attention.service import _ready_reply_current
    with tempfile.TemporaryDirectory() as d:
        ledger,s,app,clock=setup(d)
        eid=ledger.record_mail_evidence(dict(account_id='account',immutable_message_id='original',conversation_id='thread',sender='recruiter@example.test',subject='Interview',received_at='2026-10-05T11:00:00Z',body_sha256='a'*64,excerpt='Please reply.'),ctx('evidence'))['evidence']['evidence_id']
        with connect(ledger.store.db_path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS career_send_proposals (proposal_id TEXT,application_id TEXT,evidence_id TEXT,source_hash TEXT,status TEXT,expires_at TEXT,account_id TEXT,payload_json TEXT,payload_hash TEXT,created_at TEXT,updated_at TEXT)')
            con.execute('CREATE TABLE IF NOT EXISTS career_reply_sources (application_id TEXT,evidence_id TEXT,source_hash TEXT,checked_at TEXT,source_json TEXT)')
            con.execute('INSERT INTO career_send_proposals (proposal_id,application_id,evidence_id,source_hash,status,expires_at,account_id,payload_json,payload_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',('proposal',app,eid,'source','pending','2026-10-06T12:00:00Z','account','{}','hash',clock.stamp(),clock.stamp()))
            con.execute('INSERT INTO career_reply_sources (application_id,evidence_id,source_hash,checked_at,source_json) VALUES (?,?,?,?,?)',(app,eid,'source',clock.stamp(),'{}'))
            reply={'status':'READY','proposal_id':'proposal','application_id':app,'evidence_id':eid}
            assert _ready_reply_current(con,reply,clock())
        ledger.lifecycle.observe_mail(dict(account_id='account',immutable_message_id='newer',conversation_id='thread',direction='inbound',received_at='2026-10-05T11:30:00Z',modified_at='2026-10-05T11:30:00Z',sender='recruiter@example.test',subject='Updated invitation'),ctx('newer'))
        with connect(ledger.store.db_path) as con:
            assert not _ready_reply_current(con,reply,clock())


def main():
    tests=[value for key,value in globals().items() if key.startswith('test_') and callable(value)]
    for test in tests: test()
    print(f'ok ({len(tests)} attention tests)')

if __name__=='__main__': main()
