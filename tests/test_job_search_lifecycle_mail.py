"""Offline regressions for observations, discovery, replay, and archive coverage."""
from dataclasses import replace
from pathlib import Path
import tempfile

from job_search.contracts import ContractError, ConflictError, JobSnapshot, MutationContext, RecommendationProvenance
from job_search.db import connect
from job_search.outlook.state import SQLiteOutlookState
from job_search.service import JobSearchLedger
from job_search.sync import OutlookMailCoordinator
from tests.test_job_search_sync import FakeMail, change
from tests.test_job_search_mail_archive_source import archive_fixture, save_message
from job_search.mail.archive_source import EncryptedArchiveMailSource


def context(key, actor='system'):
    return MutationContext(key, actor, 'mail_lifecycle_test')


def observation(message='m1', **fields):
    return dict(account_id='personal', immutable_message_id=message, conversation_id='thread-1',
                direction='inbound', sender='recruiter@example.test', subject='Recruiter opportunity',
                received_at='2026-09-01T12:00:00Z', modified_at='2026-09-01T12:01:00Z', **fields)


def application(ledger, key='one'):
    return ledger.start_application(JobSnapshot('test', key, '', 'Engineer', 'Example', '', 'https://example.test/job'), RecommendationProvenance(), context('app-'+key, 'user'))['application']['application_id']


def body(**updates):
    value = {'conversationId':'conversation-1', 'sender':{'emailAddress':{'address':'recruiter@example.test'}},
             'subject':'Recruiter opportunity', 'receivedDateTime':'2026-09-01T12:00:00Z',
             'lastModifiedDateTime':'2026-09-01T12:01:00Z',
             'body':{'contentType':'text','content':'Please send your availability for an interview.'}}
    value.update(updates)
    return value


def test_drafts_never_reach_classifiers_and_sent_version_is_observed_once():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'ledger.db'
        ledger, state, mail = JobSearchLedger(path), SQLiteOutlookState(path), FakeMail()
        state.stage_changes('personal','inbox',[change()])
        mail.bodies['message-1'] = body(isDraft=True)
        class NoIngest:
            def ingest(self, **kwargs):
                raise AssertionError('draft or sent mail reached temporal extraction')
        coordinator = OutlookMailCoordinator(mail,state,ledger,secure_ingestor=NoIngest())
        result = coordinator.process_pending()
        assert result.ignored == 1 and result.failed == 0
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0] == 0
            assert con.execute('SELECT last_error FROM outlook_message_stage').fetchone()[0].startswith('draft excluded')
        mail.bodies['message-1'] = body(isDraft=False, parentFolderId='sentitems', sentDateTime='2026-09-01T13:00:00Z', lastModifiedDateTime='2026-09-01T13:00:00Z')
        state.stage_changes('personal','inbox',[replace(change(), modified_at='2026-09-01T13:00:00Z')])
        coordinator.secure_ingestor = None
        result = coordinator.process_pending()
        assert result.processed == 1 and result.proposed == result.auto_applied == 0
        with connect(path) as con:
            assert con.execute('SELECT COUNT(*) FROM lifecycle_mail_observations').fetchone()[0] == 1
            assert con.execute('SELECT direction FROM lifecycle_mail_observations').fetchone()[0] == 'outbound'
            assert con.execute('SELECT COUNT(*) FROM lifecycle_mail_revisions').fetchone()[0] == 2
        assert coordinator.process_pending().processed == 0


def test_reviewed_discovery_creates_external_without_submission_and_links_routine_reply():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        observed = ledger.lifecycle.observe_mail(observation(), context('observe'))['observation']
        proposal = ledger.lifecycle.propose_discovery({'observation_id':observed['observation_id']}, context('discover'))['discovery']
        result = ledger.lifecycle.decide_discovery({'discovery_id':proposal['discovery_id'], 'decision':'create','employer':'Example','title':'Engineer'}, context('decide','user'))
        app = ledger.get_application_timeline(result['application_id'])['application']
        assert app['ats'] == 'external' and app['job_url_snapshot'] == '' and app['submitted_at'] is None and app['current_phase'] == 'preparing'
        candidates = ledger.list_mail_candidates(account_id='personal', conversation_id='thread-1')
        assert len(candidates) == 1 and candidates[0]['same_conversation']
        reply = ledger.lifecycle.observe_mail(observation('m2'), context('reply'))['observation']
        ledger.lifecycle.link_mail({'observation_id':reply['observation_id'], 'application_id':app['application_id'], 'source':'accepted_conversation'}, context('reply-link'))
        page = ledger.lifecycle.list_application_conversation(app['application_id'], limit=1)
        assert not page['complete'] and page['next_cursor']
        page2 = ledger.lifecycle.list_application_conversation(app['application_id'], limit=1,cursor=page['next_cursor'])
        assert page2['complete'] and page2['items'][0]['observation_id'] != page['items'][0]['observation_id']
        assert all('immutable_message_id' not in item for item in page['items'])


def test_association_requires_review_and_ambiguous_thread_is_not_automatically_linked():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        apps = [application(ledger,key) for key in ('a','b')]
        first = ledger.lifecycle.observe_mail(observation(),context('o1'))['observation']['observation_id']
        for app in apps:
            ledger.lifecycle.link_mail({'observation_id':first,'application_id':app},context('link-'+app,'user'))
        second = ledger.lifecycle.observe_mail(observation('m2'),context('o2'))['observation']['observation_id']
        for actor in ('system','hermes'):
            try:
                ledger.lifecycle.link_mail({'observation_id':second,'application_id':apps[0],'source':'accepted_conversation'},context('bad-'+actor,actor))
            except ContractError:
                pass
            else:
                raise AssertionError('ambiguous or unreviewed association accepted')
        try:
            ledger.lifecycle.observe_mail(observation('fake'), context('fake','hermes'))
        except ContractError:
            pass
        else:
            raise AssertionError('agent fabricated observation')


def test_stale_draft_revision_does_not_erase_sent_evidence():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        sent = observation()
        sent.update(direction='outbound', sent_at='2026-09-01T12:00:00Z')
        result = ledger.lifecycle.observe_mail(sent,context('sent'))
        draft = observation()
        draft.update(direction='draft',modified_at='2026-08-31T12:00:00Z')
        replayed = ledger.lifecycle.observe_mail(draft,context('old-draft'))
        assert replayed['observation']['direction'] == 'outbound'
        assert result['observation']['observation_id'] == replayed['observation']['observation_id']


def test_explicit_replay_is_checkpointed_and_keeps_activation_and_staging_status():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'ledger.db'
        ledger, state, mail = JobSearchLedger(path), SQLiteOutlookState(path), FakeMail()
        for number in (1,2):
            state.stage_changes('personal','inbox',[change('m'+str(number))], query_version=2)
            state.mark_message('personal','inbox','m'+str(number),'ignored',query_version=2)
            mail.bodies['m'+str(number)] = body()
        coordinator = OutlookMailCoordinator(mail,state,ledger,received_since='2026-10-01T00:00:00Z')
        replay = ledger.lifecycle.start_mail_replay({'account_id':'personal','since_at':'2026-09-01T00:00:00Z','until_at':'2026-09-02T00:00:00Z'},context('replay','user'))['replay']
        first = coordinator.process_replay(replay['replay_id'],limit=1)
        assert first['processed'] == 1 and first['status'] == 'running'
        second = coordinator.process_replay(replay['replay_id'],limit=1)
        assert second['processed'] == 2 and second['status'] == 'completed'
        assert coordinator.process_replay(replay['replay_id'])['processed'] == 2
        assert coordinator.received_since == '2026-10-01T00:00:00Z'
        with connect(path) as con:
            assert {r[0] for r in con.execute('SELECT processing_status FROM outlook_message_stage')} == {'ignored'}
            assert con.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0] == 0
        assert ledger.lifecycle.list_discoveries()['items']


def test_archive_pagination_reports_coverage_and_eventually_finds_old_match():
    with tempfile.TemporaryDirectory() as directory:
        _, ledger, archive = archive_fixture(directory)
        for index in range(7):
            save_message(archive,str(index),'Interview '+str(index),'unique matching body')
        source = EncryptedArchiveMailSource(ledger,archive,scan_limit=2)
        ids, cursor = set(), None
        while True:
            page = source.search_mail_page('matching',1,cursor=cursor)
            assert page['scanned'] <= 2
            for item in page['items']:
                assert item['message_id'] not in ids
                ids.add(item['message_id'])
            cursor = page['next_cursor']
            if page['complete']:
                break
        assert len(ids) == 7
        first = source.search_mail_page('absent',1)
        assert not first['items'] and not first['complete'] and first['next_cursor']
        try:
            source.search_mail_page('different',1,cursor=first['next_cursor'])
        except ContractError:
            pass
        else:
            raise AssertionError('search cursor changed query')


def test_routine_reply_target_requires_linked_incoming_evidence():
    from job_search.mail.sanitizer import sanitize_mail
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        app = application(ledger)
        for direction in ('inbound','outbound','draft'):
            clean = sanitize_mail('Reply', 'Please send availability')
            evidence = ledger.record_mail_evidence({'account_id':'personal','immutable_message_id':direction,'sender':'recruiter@example.test','subject':clean.subject,'received_at':'2026-09-01T12:00:00Z','body_sha256':clean.content_sha256,'excerpt':clean.text},context('evidence-'+direction))['evidence']
            values = observation(direction)
            values.update(direction=direction,evidence_id=evidence['evidence_id'])
            if direction == 'outbound':
                values['sent_at'] = '2026-09-01T12:00:00Z'
            observed = ledger.lifecycle.observe_mail(values,context('observation-'+direction))['observation']
            ledger.lifecycle.link_mail({'observation_id':observed['observation_id'],'application_id':app},context('link-'+direction,'user'))
            try:
                resolved = ledger.resolve_reply_evidence(evidence['evidence_id'],app,'personal')
            except ContractError:
                assert direction != 'inbound'
            else:
                assert direction == 'inbound' and resolved['immutable_message_id'] == direction


def test_legacy_accepted_evidence_gets_an_unknown_direction_observation_atomically():
    from job_search.lifecycle.mail import link_accepted_evidence
    from job_search.mail.sanitizer import sanitize_mail
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        app = application(ledger)
        clean = sanitize_mail('Application confirmation', 'We received it')
        evidence = ledger.record_mail_evidence({'account_id':'personal','immutable_message_id':'legacy','sender':'recruiter@example.test','subject':clean.subject,'received_at':'2026-09-01T12:00:00Z','body_sha256':clean.content_sha256,'excerpt':clean.text},context('evidence'))['evidence']
        with connect(ledger.store.db_path) as con:
            first = link_accepted_evidence(con,evidence['evidence_id'],app,context('accept','user'),'2026-10-01T12:00:00Z')
            second = link_accepted_evidence(con,evidence['evidence_id'],app,context('accept-again','user'),'2026-10-01T12:00:00Z')
            assert first == second
        page = ledger.lifecycle.list_application_conversation(app)
        assert len(page['items']) == 1 and page['items'][0]['direction'] == 'unknown'


def test_observed_edited_send_completes_exact_reply_then_inbound_resolves_waiting():
    from datetime import datetime, timedelta, timezone
    from job_search.contracts import ActionKind, ActionProposalInput, payload_sha256
    from job_search.mail.sanitizer import sanitize_mail
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        app = application(ledger)
        def add_evidence(message, direction, instant):
            clean = sanitize_mail('Availability', 'Edited reply with Thursday instead of Tuesday')
            evidence = ledger.record_mail_evidence({'account_id':'personal','immutable_message_id':message,'sender':'recruiter@example.test','subject':clean.subject,'received_at':instant,'body_sha256':clean.content_sha256,'excerpt':clean.text},context('evidence-'+message))['evidence']
            values = observation(message)
            values.update(direction=direction,evidence_id=evidence['evidence_id'],received_at=instant,modified_at=instant)
            if direction == 'outbound':
                values['sent_at'] = instant
            observed = ledger.lifecycle.observe_mail(values,context('observation-'+message))['observation']
            return evidence,observed
        incoming, observed = add_evidence('incoming','inbound','2026-09-01T12:00:00Z')
        ledger.lifecycle.link_mail({'observation_id':observed['observation_id'],'application_id':app},context('inbound-link','user'))
        task = ledger.lifecycle.create_task(app,{'kind':'send_availability','owner':'applicant','evidence_id':incoming['evidence_id'],'source_time':'2026-09-01T12:00:00Z'},context('task','user'))['task']
        unrelated = ledger.lifecycle.create_task(app,{'kind':'reply','owner':'applicant'},context('other-task','user'))['task']
        payload = {'message_id':'incoming','body':'Tuesday works.'}
        expires = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace('+00:00','Z')
        action = ledger.create_action_proposal(ActionProposalInput(ActionKind.OUTLOOK_REPLY_DRAFT,app,'personal',payload,expires),context('draft-action'))['action']
        ledger.decide_action(action['action_id'],True,payload_sha256(payload),context('approve','user'))
        execution = ledger.claim_action(action['action_id'])
        execution_id = execution['execution']['execution_id']
        ledger.complete_action(execution_id,'succeeded',remote_id='edited-draft')
        assert {item['task_id']:item['status'] for item in ledger.lifecycle.list_tasks(app)}[task['task_id']] == 'open'
        sent, observed_sent = add_evidence('edited-draft','outbound','2026-09-02T12:00:00Z')
        tasks = ledger.lifecycle.list_tasks(app)
        indexed = {item['task_id']:item for item in tasks}
        assert indexed[task['task_id']]['status'] == 'completed'
        assert indexed[task['task_id']]['completed_evidence_id'] == sent['evidence_id']
        assert indexed[unrelated['task_id']]['status'] == 'open'
        waits = [item for item in tasks if item['owner']=='employer']
        assert len(waits) == 1 and waits[0]['status'] == 'open'
        _, next_mail = add_evidence('next-inbound','inbound','2026-09-03T12:00:00Z')
        ledger.lifecycle.link_mail({'observation_id':next_mail['observation_id'],'application_id':app,'source':'accepted_conversation'},context('next-link'))
        assert [item for item in ledger.lifecycle.list_tasks(app) if item['owner']=='employer'][0]['status'] == 'superseded'


def test_user_direction_review_unlocks_custom_folder_mail_with_audit_and_source_time():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'ledger.db'
        ledger, state, mail = JobSearchLedger(path), SQLiteOutlookState(path), FakeMail()
        app = application(ledger)
        state.stage_changes('personal','archive-custom',[change()],query_version=2)
        mail.bodies['message-1'] = body(parentFolderId='archive-custom')
        coordinator = OutlookMailCoordinator(mail,state,ledger)
        result = coordinator.process_pending(query_version=2)
        assert result.processed == 1 and result.proposed == 0
        discovery = ledger.lifecycle.list_discoveries()['items'][0]
        observed = ledger.lifecycle.get_mail_observation(discovery['observation_id'])
        assert observed['direction'] == 'unknown'
        request = {'observation_id':observed['observation_id'],'direction':'inbound','reason':'This is the recruiter reply in my archive.','expected_updated_at':observed['updated_at']}
        for actor in ('system','hermes'):
            try:
                ledger.lifecycle.review_mail_direction(request,context('unauthorized-'+actor,actor))
            except ContractError:
                pass
            else:
                raise AssertionError('direction review bypassed user boundary')
        result = ledger.lifecycle.review_mail_direction(request,context('direction','user'))
        assert result['observation']['source_at'] == observed['received_at']
        assert result['observation']['modified_at'] == observed['modified_at']
        ledger.lifecycle.decide_discovery({'discovery_id':discovery['discovery_id'],'decision':'link','application_id':app},context('link-discovery','user'))
        assert ledger.resolve_reply_evidence(observed['evidence_id'],app,'personal')['application_id'] == app
        reviewed = ledger.lifecycle.get_mail_observation(observed['observation_id'])
        assert len(reviewed['direction_decisions']) == 1
        stale = observation('message-1')
        stale.update(direction='unknown',received_at='2026-09-01T12:00:00Z',modified_at='2026-09-02T12:00:00Z')
        assert ledger.lifecycle.observe_mail(stale,context('stale-unknown'))['observation']['direction'] == 'inbound'
        replay = ledger.lifecycle.start_mail_replay({'account_id':'personal','since_at':'2026-09-01T00:00:00Z','until_at':'2026-09-02T00:00:00Z'},context('reviewed-replay','user'))['replay']
        assert coordinator.process_replay(replay['replay_id'])['status'] == 'completed'
        assert ledger.lifecycle.get_mail_observation(observed['observation_id'])['direction'] == 'inbound'
        with connect(path) as con:
            try:
                con.execute('DELETE FROM lifecycle_mail_direction_decisions')
            except Exception as exc:
                assert 'append-only' in str(exc)
            else:
                raise AssertionError('direction audit could be erased')
        try:
            ledger.lifecycle.review_mail_direction(request,context('stale-direction','user'))
        except ConflictError:
            pass
        else:
            raise AssertionError('stale direction decision accepted')


def test_reviewed_outbound_requires_observed_sent_timestamp():
    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory)/'ledger.db')
        values = observation()
        values['direction'] = 'unknown'
        observed = ledger.lifecycle.observe_mail(values,context('unknown'))['observation']
        request = {'observation_id':observed['observation_id'],'direction':'outbound','reason':'My message','expected_updated_at':observed['updated_at']}
        try:
            ledger.lifecycle.review_mail_direction(request,context('outbound','user'))
        except ContractError:
            pass
        else:
            raise AssertionError('outbound without sent evidence accepted')
        values = observation('has-sent')
        values.update(direction='unknown',sent_at='2026-09-01T11:59:00Z')
        observed = ledger.lifecycle.observe_mail(values,context('unknown-sent'))['observation']
        request.update(observation_id=observed['observation_id'],expected_updated_at=observed['updated_at'])
        result = ledger.lifecycle.review_mail_direction(request,context('verified-outbound','user'))
        assert result['observation']['source_at'] == values['sent_at']


def test_replay_status_listing_is_bounded_private_and_account_filtered():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'ledger.db'
        ledger, state = JobSearchLedger(path), SQLiteOutlookState(path)
        state.stage_changes('alpha','inbox',[change()],query_version=2)
        state.stage_changes('beta','inbox',[change()],query_version=2)
        for number,account in enumerate(('alpha','alpha','beta')):
            ledger.lifecycle.start_mail_replay({'account_id':account,'since_at':'2026-09-01T00:00:00Z','until_at':'2026-09-02T00:00:00Z'},context('replay-'+str(number),'user'))
        assert ledger.lifecycle.list_mail_replay_accounts() == ['alpha','beta']
        pending = ledger.lifecycle.list_pending_replays(account_id='beta')
        assert len(pending) == 1 and pending[0]['account_id'] == 'beta'
        first = ledger.lifecycle.list_mail_replays(account_id='alpha',limit=1)
        assert not first['complete'] and first['next_cursor']
        second = ledger.lifecycle.list_mail_replays(account_id='alpha',limit=1,cursor=first['next_cursor'])
        assert second['complete'] and second['items'][0]['replay_id'] != first['items'][0]['replay_id']
        assert not {'after_stage_rowid','max_stage_rowid','immutable_message_id'} & set(first['items'][0])
        try:
            ledger.lifecycle.list_mail_replays(account_id='beta',cursor=first['next_cursor'])
        except ContractError:
            pass
        else:
            raise AssertionError('replay cursor crossed account filter')
        try:
            ledger.lifecycle.list_mail_replays(limit=101)
        except ContractError:
            pass
        else:
            raise AssertionError('unbounded replay listing accepted')


def test_failed_replay_retains_checkpoint_requires_review_and_does_not_block_other_jobs():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'ledger.db'
        ledger, state, mail = JobSearchLedger(path), SQLiteOutlookState(path), FakeMail()
        for number in (1,2):
            state.stage_changes('personal','inbox',[change('m'+str(number))],query_version=2)
            mail.bodies['m'+str(number)] = body()
        mail.bodies['m2'] = RuntimeError('sensitive raw message must not escape')
        values = {'account_id':'personal','since_at':'2026-09-01T00:00:00Z','until_at':'2026-09-02T00:00:00Z'}
        first = ledger.lifecycle.start_mail_replay(values,context('first','user'))['replay']
        second = ledger.lifecycle.start_mail_replay(values,context('second','user'))['replay']
        coordinator = OutlookMailCoordinator(mail,state,ledger)
        try:
            coordinator.process_replay(first['replay_id'])
        except RuntimeError:
            pass
        else:
            raise AssertionError('failed message did not stop replay')
        failed = ledger.lifecycle.get_mail_replay(first['replay_id'])
        assert failed['status'] == 'failed' and failed['processed'] == 1 and failed['failure_count'] == 1
        assert 'sensitive' not in failed['last_error']
        assert [r['replay_id'] for r in ledger.lifecycle.list_pending_replays()] == [second['replay_id']]
        try:
            ledger.lifecycle.transition_mail_replay(first['replay_id'],'retry',context('denied','hermes'))
        except ContractError:
            pass
        else:
            raise AssertionError('agent retried history without user review')
        ledger.lifecycle.transition_mail_replay(first['replay_id'],'retry',context('retry','user'))
        mail.bodies['m2'] = body()
        completed = coordinator.process_replay(first['replay_id'])
        assert completed['status'] == 'completed' and completed['processed'] == 2
        ledger.lifecycle.transition_mail_replay(second['replay_id'],'cancel',context('cancel','user'))
        mail.bodies.clear()
        assert coordinator.process_replay(second['replay_id'])['status'] == 'cancelled'
        assert not ledger.lifecycle.list_pending_replays()


def test_replay_lost_lease_keeps_checkpoint_recoverable_without_user_retry():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'ledger.db'
        ledger, state, mail = JobSearchLedger(path), SQLiteOutlookState(path), FakeMail()
        state.stage_changes('personal','inbox',[change()],query_version=2)
        mail.bodies['message-1'] = body()
        replay = ledger.lifecycle.start_mail_replay({'account_id':'personal','since_at':'2026-09-01T00:00:00Z','until_at':'2026-09-02T00:00:00Z'},context('start','user'))['replay']
        calls = []
        def heartbeat():
            calls.append(True)
            return len(calls) == 1
        coordinator = OutlookMailCoordinator(mail,state,ledger)
        try:
            coordinator.process_replay(replay['replay_id'],heartbeat=heartbeat)
        except RuntimeError:
            pass
        else:
            raise AssertionError('replay ignored its lost lease')
        current = ledger.lifecycle.get_mail_replay(replay['replay_id'])
        assert current['status'] == 'pending' and current['processed'] == 0
        assert coordinator.process_replay(replay['replay_id'])['processed'] == 1


def main():
    tests = [value for name,value in globals().items() if name.startswith('test_')]
    for test in tests:
        test()
    print(f'ok ({len(tests)} lifecycle mail tests)')


if __name__ == '__main__':
    main()
