"""Migrated production scheduling, Understanding grounding and delivery regression."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from tests import test_redesign_production
from tests.test_attention import Clock, live
from job_search.application_production import build_owner_handlers
from job_search.application_installation import LEGACY_TASKS
from job_search.applications.understanding.briefing import valid_selection
from job_search.commands import CommandContext, Principal
from job_search.contracts import MutationContext
from job_search.db import connect
from job_search.notifications import NotificationOutboxHandler
from job_search.runtime import build_runtime


class OwnerBriefingsTest(unittest.TestCase):
    def setUp(self):
        test_redesign_production.ProductionTests.setUp(self)
        self.clock = Clock('2026-10-10T11:56:00Z')  # Saturday, 06:56 Chicago
        self.config = replace(self.config, briefing_inference_config=self.root/'briefing.json')
        self.handlers = build_owner_handlers(self.config, {}, lane='core', clock=self.clock, ledger=self.legacy)
        self.attention = self.legacy.attention
        live(self.attention)
        with connect(self.config.application_db) as con:
            con.execute("UPDATE automation_controls SET enabled=1 WHERE capability='briefing_ai'")
        self.runtime.executor.set_activation(paused=False, expected_revision=0, operator='fixture', reason='Briefing fixture')
        self.context = SimpleNamespace(heartbeat=lambda: True, work_id='brief-test', attempt=1)
        self.counter = 0
        self.app = self.command('save_job', {'job_source': {'source':'fixture','source_id':'brief'},
            'job_snapshot': {'ats':'fixture','job_id':'brief','company':'Fictional Company','title':'Engineer'}})['id']
        self.task = self.command('create_task', {'application_id': self.app, 'kind':'complete_assessment',
            'description':'Complete the accepted assessment', 'due_at':'2026-10-10T19:00:00Z'})

    def command(self, operation, payload):
        self.counter += 1
        return self.runtime.command(CommandContext(Principal('human','human',frozenset({operation})), 'brief-'+str(self.counter), 'direct'), operation, payload)

    def tick(self):
        return self.handlers['applications.briefing.tick']({}, self.context)

    def due(self):
        self.tick(); self.clock.advance(4); return self.tick()

    def rows(self):
        with connect(self.config.application_db) as con:
            return [dict(r) for r in con.execute("SELECT * FROM notification_outbox WHERE topic='briefing.ready'")]

    def send(self):
        sent=[]
        sender=SimpleNamespace(send=lambda row:sent.append(row))
        result=NotificationOutboxHandler(self.legacy,sender,now=self.clock).handle_task({})
        return sent,result

    def test_migrated_weekend_schedule_generates_once_and_delivers(self):
        before=self.runtime.queries.briefing_records()
        result=self.tick()
        self.assertEqual(len(result.follow_ups),1)
        self.assertEqual(result.follow_ups[0].task_kind,'applications.briefing.compose')
        self.assertEqual(self.rows(),[])
        self.clock.advance(4); self.tick(); self.tick()
        self.assertEqual(len(self.rows()),1)
        self.assertIn('Complete the accepted assessment',self.rows()[0]['body'])
        sent,result=self.send()
        self.assertEqual(result['delivered'],1)
        self.assertEqual(len(sent),1)
        self.assertEqual(self.send()[0],[])
        self.assertEqual(self.runtime.queries.briefing_records(),before)

    def test_real_runtime_retains_new_schedules_and_never_unfreezes_legacy(self):
        core=build_runtime(self.config,lane='core',base_environment={},now_provider=self.clock)
        model=build_runtime(self.config,lane='model',base_environment={},now_provider=self.clock)
        self.assertIn('applications.briefing.tick',core.worker.task_handlers)
        self.assertIn('applications.briefing.compose',model.worker.task_handlers)
        with connect(self.config.application_db) as con:
            enabled={r[0] for r in con.execute('SELECT task_kind FROM schedule_specs WHERE enabled=1')}
        with connect(self.config.application_db) as con:
            schedule = json.loads(con.execute("SELECT schedule_json FROM schedule_specs WHERE task_kind='applications.briefing.tick'").fetchone()[0])
        self.assertEqual(schedule['priority'], 95)
        self.assertIn('applications.briefing.tick',enabled)
        self.assertFalse(enabled & LEGACY_TASKS)
        self.assertFalse(set(core.worker.task_handlers) & LEGACY_TASKS)

    def test_finalized_slot_does_not_rebuild_portfolio_on_every_tick(self):
        self.due()
        with patch.object(self.attention.queries, 'briefing_records', side_effect=AssertionError('unnecessary portfolio read')):
            self.tick()

    def test_model_failure_does_not_delay_factual_brief(self):
        self.tick(); bid=self.attention.history()['items'][0]['briefing_id']
        self.attention.generation_provider=SimpleNamespace(count_tokens_upper_bound=lambda _:1,max_input_tokens=12000,
            generate=lambda *a,**k: (_ for _ in ()).throw(ValueError('fixture failure')))
        self.assertEqual(self.attention.generate_briefing(bid)['outcome'],'generation_failed_fallback')
        self.clock.advance(4); self.tick()
        self.assertEqual(len(self.send()[0]),1)

    def test_understanding_selection_is_grounded_and_late_output_is_ignored(self):
        self.tick(); b=self.attention.history()['items'][0]; ref=b['snapshot']['facts'][0]['ref']
        self.assertFalse(valid_selection({'ordered_refs':['invented']},b['snapshot']))
        self.assertFalse(valid_selection({'ordered_refs':[ref], 'suggested_next_steps':[{'action':'reply','fact_ref':ref}]},b['snapshot']))
        self.assertEqual(self.attention.complete_generation(b['briefing_id'],{'ordered_refs':[ref]})['outcome'],'accepted')
        self.clock.advance(4);self.tick()
        self.assertEqual(self.attention.complete_generation(b['briefing_id'],{'ordered_refs':[]})['outcome'],'late_ignored')
        self.assertEqual(self.attention.history()['items'][0]['renderer'],'grounded_model_selection')

    def test_task_completion_between_prepare_and_finalize_drops_stale_fact(self):
        self.tick()
        self.command('complete_task',{'task_id':self.task['id'],'expected_version':1,'reason':'Done'})
        self.clock.advance(4);self.tick()
        self.assertNotIn('Complete the accepted assessment', self.rows()[0]['body'])

    def test_task_completion_after_queue_refreshes_before_delivery(self):
        self.due()
        self.command('complete_task',{'task_id':self.task['id'],'expected_version':1,'reason':'Done'})
        self.assertEqual(self.send()[0],[])  # first claim replaces the queued payload
        sent,_=self.send()
        self.assertEqual(len(sent),1)
        self.assertNotIn('Complete the accepted assessment',sent[0]['body'])
        self.assertEqual([r['status'] for r in self.rows()].count('delivered'),1)

    def test_no_new_delivery_id_after_uncertain_attempt(self):
        self.due()
        with connect(self.config.application_db) as con:
            con.execute("UPDATE notification_outbox SET attempts=1 WHERE topic='briefing.ready'")
        self.command('complete_task',{'task_id':self.task['id'],'expected_version':1,'reason':'Done'})
        self.assertEqual(self.send()[0],[])
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.rows()[0]['last_error'],'delivery_reconciliation_required')

    def test_expired_brief_is_not_sent_or_caught_up_next_day(self):
        self.tick();self.clock.advance(95);self.tick()
        self.assertEqual(self.rows(),[])
        self.assertEqual(self.attention.history()['items'][0]['status'],'expired')

    def test_paused_installation_blocks_delivery(self):
        self.due()
        self.runtime.executor.set_activation(paused=True,expected_revision=1,operator='fixture',reason='Pause')
        self.assertEqual(self.send()[0],[])

    def test_notifications_pause_and_preferences_revoke_queue(self):
        self.due()
        self.attention.update_preferences({'enabled':False},self.attention.preferences()['revision'],MutationContext('disable','user','fixture'))
        self.assertEqual(self.send()[0],[])
        self.assertEqual(self.rows()[0]['status'],'cancelled')

    def test_preview_uses_owner_query_and_explicit_coverage_without_writes(self):
        with patch('job_search.attention.service.portfolio_snapshot',side_effect=AssertionError('legacy snapshot read')):
            preview=self.attention.preview()
        self.assertEqual(preview['snapshot']['backend'],'owners')
        self.assertFalse(preview['snapshot']['coverage']['agenda']['complete'])
        self.assertEqual(self.rows(),[])

    def test_pending_inference_is_review_not_an_accepted_task(self):
        self.command('propose_changes',{'operation':'create_task','input':{'application_id':self.app,'kind':'reply','description':'Unaccepted reply'},
            'application_id':self.app,'evidence':[],'expected_versions':{},'dependencies':[]})
        preview=self.attention.preview()
        self.assertIn('Pending review: create task',preview['body'])
        self.assertNotIn('Unaccepted reply',preview['body'])
        self.assertEqual(preview['snapshot']['counts']['applicant_tasks'],1)

    def test_owner_dashboard_restores_only_briefing_routes(self):
        from job_search.application_gateway import build_application_gateway
        gateway=build_application_gateway(self.config,self.legacy)
        handled,result=gateway.http_query('/api/v1/chief/preview',{})
        self.assertTrue(handled);self.assertEqual(result['snapshot']['backend'],'owners')
        handled,prefs=gateway.http_query('/api/v1/chief/preferences',{})
        handled,result=gateway.http_command('/api/v1/chief/preferences',{'changes':{'morning_time':'08:00'},'expected_revision':prefs['revision']},'save-prefs','fixture')
        self.assertEqual(result['morning_time'],'08:00')
        with self.assertRaises(Exception):gateway.http_command('/api/v1/chief/actions/decide',{},'forbidden','fixture')

    def test_readiness_detects_missing_briefing_scheduler(self):
        from job_search.chief_status import owner_briefing_status
        config = replace(self.config, hermes_notification_socket=self.root/'hermes.sock', hermes_telegram_target='telegram')
        result, caps = owner_briefing_status(config, {'paused':False})
        self.assertTrue(result['schedule_enabled'])
        self.assertEqual(caps[0]['status'], 'ready')
        with connect(self.config.application_db) as con:
            con.execute("UPDATE schedule_specs SET enabled=0 WHERE task_kind='applications.briefing.tick'")
        result, caps = owner_briefing_status(config, {'paused':False})
        self.assertEqual(caps[0]['status'], 'blocked')

    def test_ai_control_pause_does_not_pause_deterministic_delivery(self):
        with connect(self.config.application_db) as con:
            con.execute("UPDATE automation_controls SET enabled=0 WHERE capability='briefing_ai'")
        self.assertEqual(self.tick().follow_ups, ())
        self.clock.advance(4); self.tick()
        self.assertEqual(len(self.send()[0]), 1)

    def test_monday_and_friday_variants_and_evening_slot(self):
        self.clock.value = datetime(2026,10,12,11,56,tzinfo=timezone.utc)
        self.assertEqual(self.attention.preview()['snapshot']['variant'], 'week_ahead')
        self.clock.value = datetime(2026,10,17,0,0,tzinfo=timezone.utc)
        self.assertEqual(self.attention.preview('evening')['snapshot']['variant'], 'week_recap')
        self.tick()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.attention.history()['items'][0]['slot'], 'evening')

    def test_first_tick_after_slot_uses_fallback_within_grace_window(self):
        self.clock.advance(14)
        self.tick()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.send()[0]), 1)

    def test_unconfigured_delivery_validator_cannot_send_owner_snapshot(self):
        self.due()
        del self.legacy.store.notification_validator
        self.assertEqual(self.send()[0], [])


if __name__=='__main__':unittest.main()
