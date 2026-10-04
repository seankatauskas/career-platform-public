"""Offline integration checks for chief-of-staff composition and delivery boundaries."""
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile

from job_search.contracts import canonical_json
from job_search.db import connect
from job_search.notifications import HermesSendClient, NotificationIntent, NotificationSendError
from job_search.runtime import RuntimeConfigV1, build_runtime, build_local_reminder_handler
from job_search.service import JobSearchLedger
from job_search.worker import FollowUpTask, TaskResult, Worker

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
STAMP = '2026-10-05T12:00:00Z'


def test_runtime_uses_attention_and_keeps_new_mutations_paused():
    with tempfile.TemporaryDirectory() as directory:
        config = RuntimeConfigV1.defaults(Path(directory))
        runtime = build_runtime(config, now_provider=lambda: NOW, base_environment={})
        ledger = JobSearchLedger(config.application_db)
        preferences = ledger.attention.preferences()
        assert preferences['mode'] == 'important_developments'
        assert preferences['shadow'] is True
        assert preferences['quiet_hours_enabled'] is False
        with connect(config.application_db) as con:
            controls = dict(con.execute('SELECT capability,enabled FROM automation_controls'))
        assert all(controls[name] == 0 for name in ('briefing_ai','outlook_send','calendar_commitments'))
        assert 'attention.tick' in runtime.worker.task_handlers
        handler = build_local_reminder_handler(config, now_provider=lambda: NOW)
        assert handler.publisher.policy.policy_id == 'chief-of-staff-v1'


def test_pause_blocks_claim_and_is_rechecked_before_sending():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / 'app.db')
        ledger.publish_notification(NotificationIntent('reminder.due','one','Reminder','Review your appointment.'), available_at=STAMP)
        with connect(ledger.store.db_path) as con:
            con.execute("INSERT INTO automation_controls VALUES ('notifications',0,0,?)", (STAMP,))
        assert ledger.claim_notification('worker', STAMP) is None
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE automation_controls SET enabled=1 WHERE capability='notifications'")
        row = ledger.claim_notification('worker', STAMP)
        assert row is not None
        with connect(ledger.store.db_path) as con:
            con.execute("UPDATE automation_controls SET enabled=0 WHERE capability='notifications'")
        assert not ledger.validate_notification_claim(row['notification_id'],row['lease_token'],STAMP)
        with connect(ledger.store.db_path) as con:
            saved = con.execute('SELECT status,lease_token FROM notification_outbox').fetchone()
        assert saved['status'] == 'pending' and saved['lease_token'] is None


def test_repeated_ticks_create_one_semantic_model_followup():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / 'app.db')
        with connect(ledger.store.db_path) as con:
            for identity in ('first','second'):
                con.execute("INSERT INTO work_items (work_id,task_kind,dedupe_key,payload_json,status,due_at,created_at,max_attempts,lane) VALUES (?,?,?,?,'queued',?,?,3,'core')",
                            (identity,'test.prepare',identity,canonical_json({}),STAMP,STAMP))
        def handler(payload,context):
            return TaskResult({'prepared':True}, (FollowUpTask('test.compose',{'briefing_id':'one'},lane='model',dedupe_key='briefing:one'),))
        worker = Worker(ledger.store.db_path,task_handlers={'test.prepare':handler},now_provider=lambda: NOW,lane='core')
        worker.tick()
        with connect(ledger.store.db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM work_items WHERE task_kind='test.compose'").fetchone()[0] == 1


def test_ambiguous_local_delivery_is_not_retried():
    class Runner:
        def run(self,*args,**kwargs):
            raise subprocess.TimeoutExpired('hermes',30)
    sender = HermesSendClient(executable=Path('/bin/echo'),target='telegram',runner=Runner())
    try:
        sender.send({'title':'Review','body':'A reply is ready.'})
    except NotificationSendError as exc:
        assert not exc.retryable and str(exc) == 'delivery_reconciliation_required'
    else:
        raise AssertionError('ambiguous delivery must require reconciliation')


def test_telegram_identity_requires_separate_bearer_and_private_owner():
    with tempfile.TemporaryDirectory() as directory:
        for values in ({'telegram_user_id':'1'}, {'interaction_token_file':'token','telegram_bot_id':'2','telegram_user_id':'1','telegram_chat_id':'-5'}):
            try:
                RuntimeConfigV1.from_mapping({'version':1,**values},default_root=Path(directory))
            except ValueError:
                pass
            else:
                raise AssertionError('incomplete or group-chat identity must fail')


def test_managed_alert_waits_for_receipt_without_spending_failure_attempts():
    from dataclasses import replace
    from job_search.chief_runtime import configure_services
    from job_search.contracts import MutationContext
    from job_search.interactions.notifications import InteractionNotificationSender
    from job_search.notifications import NotificationOutboxHandler
    with tempfile.TemporaryDirectory() as directory:
        config = replace(RuntimeConfigV1.defaults(Path(directory)),
                         interaction_token_file=Path(directory)/'interaction-token',
                         telegram_bot_id='123', telegram_user_id='456', telegram_chat_id='456')
        ledger = JobSearchLedger(config.application_db)
        clock = [NOW]
        configure_services(ledger, config, now_provider=lambda: clock[0])
        prefs = ledger.attention.preferences()
        ledger.attention.update_preferences({'shadow':False}, prefs['revision'], MutationContext('activate','user','test'))
        ledger.attention.from_notification(NotificationIntent('application.offer_received','offer-one','Offer received','Review the offer.'))
        class Fallback:
            def send(self,row):
                raise AssertionError('an actionable card must not also send a plain alert')
        handler = NotificationOutboxHandler(ledger, InteractionNotificationSender(ledger,Fallback(),now_provider=lambda:clock[0]), now=lambda:clock[0])
        from datetime import timedelta
        for _ in range(4):
            assert handler.handle_task({}) == {'delivered':0,'retried':0,'dead':0}
            clock[0] += timedelta(seconds=16)
        pending = ledger.list_notification_outbox(('pending',))
        assert len(pending)==1 and pending[0]['attempts']==0
        tickets = ledger.interactions.pending_reviews()['items']
        assert len(tickets)==1
        assert ledger.interactions.claim_delivery(tickets[0]['ticket_id'])['send_allowed']
        ledger.interactions.mark_delivered(tickets[0]['ticket_id'],'789')
        assert len(ledger.list_notification_outbox(('delivered',)))==1
        assert ledger.claim_notification('next-worker', clock[0].isoformat(timespec='seconds').replace('+00:00','Z')) is None


if __name__ == '__main__':
    checks = [value for name,value in globals().copy().items() if name.startswith('test_')]
    for check in checks:
        check()
    print(f'ok ({len(checks)} chief runtime tests)')
