"""Offline shared orchestration, checkpoint recovery, evaluation and replay checks."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import hashlib
import sqlite3
import os
import unittest
from unittest.mock import patch

from job_search.contracts import MutationContext, ContractError
from job_search.db import connect
from job_search.mail.archive import EncryptedMailArchive
from job_search.mail.context import CandidateApplication
from job_search.mail.understanding_evaluation import evaluate_cases, load_report, validate_report
from job_search.mail.understanding_runtime import MailUnderstandingRuntime
from job_search.mail.understanding_replay import UnderstandingReplay
from job_search.runtime import RuntimeConfigV1
from tests.test_job_search_career_actions import setup, SYSTEM, NOW


class TestKey:
    def __init__(self, key=bytes(range(32))):self.key=key
    def get_or_create_key(self):
        return hashlib.sha256(self.key).hexdigest()[:32],self.key
    get_existing_key=get_or_create_key


class Analyzer:
    producer_version = 'fixture-understanding-v1'
    def __init__(self):self.calls=0
    def prepare(self,request):return request
    def analyze(self,request):
        self.calls+=1
        source=next(s for s in request['sources'] if s['kind']=='current')
        quote='Please reply'
        offset=source['text'].index(quote)
        return dict(relevance='career',events=[],temporal_facts=[],uncertainties=[],actions=[dict(
            application_id=request['candidates'][0]['application_id'],confidence=.99,
            evidence=[dict(source_id=source['source_id'],quote=quote,start=offset,end=offset+len(quote))],
            kind='reply',description='Reply to the recruiter',actor='applicant',obligation='required',channel='email',temporal_index=None)])


def fixture(directory):
    path,ledger,career,outlook,app,eid=setup(directory,body='Please reply')
    archive=EncryptedMailArchive(ledger,TestKey())
    from job_search.mail.sanitizer import sanitize_mail
    clean=sanitize_mail('Interview','Please reply')
    archived=archive.archive_message(account_id='account',immutable_message_id='incoming',sanitized_text=clean.text,truncated=False,context=MutationContext('archive-test','system','test'))
    with connect(path) as con:
        con.execute('UPDATE lifecycle_mail_observations SET archive_id=? WHERE evidence_id=?',(archived['archive']['archive_id'],eid))
        obs=dict(con.execute('SELECT * FROM lifecycle_mail_observations WHERE evidence_id=?',(eid,)).fetchone())
        row=con.execute('SELECT * FROM applications WHERE application_id=?',(app,)).fetchone()
    candidate=CandidateApplication(app,row['ats'],row['job_id'],row['employer_snapshot'],row['title_snapshot'],phase=row['current_phase'],match_context='previously linked email conversation')
    analyzer=Analyzer()
    runtime=MailUnderstandingRuntime(ledger,archive,analyzer,source_scope='thread_attachments')
    return path,ledger,runtime,analyzer,obs,candidate,career


class SharedRuntimeTests(unittest.TestCase):
    def test_checkpoint_survives_projection_failure_without_reanalysis(self):
        with TemporaryDirectory() as d:
            path,ledger,runtime,analyzer,obs,candidate,career=fixture(d)
            with patch.object(runtime.service,'project',side_effect=RuntimeError('fixture crash')):
                with self.assertRaises(RuntimeError):runtime.process(obs,subject='Interview',body='Please reply',candidates=[candidate])
            self.assertEqual(analyzer.calls,1)
            result=runtime.process(obs,subject='Interview',body='Please reply',candidates=[])
            self.assertEqual(analyzer.calls,1)
            self.assertTrue(result['findings'])
            # Shared-owned mail cannot gain a parallel regex-created reply task.
            self.assertEqual(career.record_reply_obligations(SYSTEM),[])
            with connect(path) as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM lifecycle_tasks').fetchone()[0],0)

    def test_no_provider_call_when_paused(self):
        with TemporaryDirectory() as d:
            _,_,runtime,analyzer,obs,candidate,_=fixture(d)
            runtime.mode='paused'
            self.assertEqual(runtime.process(obs,subject='Interview',body='Please reply',candidates=[candidate])['state'],'paused')
            self.assertEqual(analyzer.calls,0)

    def test_lost_worker_lease_does_not_invoke_model(self):
        with TemporaryDirectory() as d:
            _,_,runtime,analyzer,obs,candidate,_=fixture(d)
            with self.assertRaises(RuntimeError):runtime.process(obs,subject='Interview',body='Please reply',candidates=[candidate],heartbeat=lambda:False)
            self.assertEqual(analyzer.calls,0)

    def test_history_is_fixed_membership_idempotent_review_only(self):
        with TemporaryDirectory() as d:
            path,ledger,runtime,analyzer,obs,_,_=fixture(d)
            replay=UnderstandingReplay(ledger,runtime)
            self.assertEqual(replay.preview('account')['messages'],1)
            ctx=MutationContext('start-history','user','test')
            result=replay.start('account',ctx)
            self.assertEqual(replay.start('account',ctx),result)
            finished=replay.run_batch(result['replay_id'])
            self.assertEqual(finished['counts'],{'done':1})
            self.assertEqual(analyzer.calls,1)
            replay.run_batch(result['replay_id'])
            self.assertEqual(analyzer.calls,1)
            self.assertEqual(runtime.service.list_reviews(),[])
            self.assertEqual(len(runtime.service.list_reviews(history=True)),1)
            with connect(path) as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM lifecycle_tasks').fetchone()[0],0)

    def test_cancel_prevents_replay_provider_calls(self):
        with TemporaryDirectory() as d:
            _,ledger,runtime,analyzer,_,_,_=fixture(d)
            replay=UnderstandingReplay(ledger,runtime)
            job=replay.start('account',MutationContext('start','user','test'))
            replay.cancel(job['replay_id'],MutationContext('cancel','user','test'))
            with self.assertRaises(ContractError):replay.run_batch(job['replay_id'])
            self.assertEqual(analyzer.calls,0)

    def test_shared_sync_calls_analyzer_for_receipt_and_accepts_long_action_evidence(self):
        from job_search.mail.secure_ingest import SecureMailIngestor
        from job_search.outlook.state import SQLiteOutlookState
        from job_search.sync import OutlookMailCoordinator
        from tests.test_job_search_ledger import make_service,start
        from tests.test_job_search_sync import FakeMail,change
        with TemporaryDirectory() as d:
            path,ledger=make_service(d)
            with patch('job_search.store.utc_now',return_value='2026-09-01T11:00:00Z'):
                app=start(ledger)['application']['application_id']
                ledger.record_submission(app,'2026-09-01T11:05:00Z',MutationContext('submitted','user','test'))
            mail=FakeMail();state=SQLiteOutlookState(path)
            state.stage_changes('personal','inbox',[change()])
            body='Thank you for applying to Acme for the Platform Engineer role. '+('Informational text. '*200)+'Please reply with your portfolio.'
            mail.bodies['message-1']=dict(id='message-1',conversationId='conversation-1',subject='Your application to Acme',receivedDateTime='2026-09-01T12:00:00Z',body=dict(contentType='text',content=body),sender={'emailAddress':{'address':'notifications@greenhouse.io'}})
            archive=EncryptedMailArchive(ledger,TestKey())
            analyzer=Analyzer();runtime=MailUnderstandingRuntime(ledger,archive,analyzer)
            class ForbiddenTemporal:
                def propose(self,*args,**kwargs):raise AssertionError('independent temporal inference')
            ingestor=SecureMailIngestor(archive,temporal=ForbiddenTemporal())
            sync=OutlookMailCoordinator(mail,state,ledger,secure_ingestor=ingestor,understanding=runtime)
            result=sync.process_pending()
            with connect(path) as con:failure=con.execute('SELECT last_error FROM outlook_message_stage').fetchone()[0]
            self.assertEqual((result.processed,result.failed),(1,0),failure)
            self.assertEqual(analyzer.calls,1)
            review=ledger.mail_understanding.list_reviews()[0]
            finding=next(f for f in review['findings'] if f['type']=='action')
            self.assertGreater(finding['value']['evidence'][0]['start'],2048)
            with connect(path) as con:
                self.assertNotIn('Please reply',con.execute('SELECT excerpt FROM mail_evidence').fetchone()[0])
                self.assertEqual(con.execute('SELECT COUNT(*) FROM lifecycle_tasks').fetchone()[0],0)
            reviewed=ledger.mail_understanding.decide(review['analysis_id'],review['revision'],[
                dict(finding_id=finding['finding_id'],decision='accepted',application_id=app,reason='Verified request')],MutationContext('accept-long-request','user','test'))
            with connect(path) as con:
                task=con.execute('SELECT kind,evidence_id FROM lifecycle_tasks').fetchone()
                self.assertEqual(task['kind'],'reply')
                self.assertEqual(task['evidence_id'],review['evidence_id'])
            # Same persisted observation reprocessed in legacy mode cannot run its old detector.
            state.stage_changes('personal','inbox',[change()])
            self.assertEqual(sync.process_pending().processed,0)
            self.assertEqual(analyzer.calls,1)

    def test_portable_export_rekeys_shared_sources_and_preserves_guard(self):
        from job_search.portable_export import _rekey_database
        from job_search.mail.archive import AESGCMCipher
        from job_search.mail.understanding_store import MailUnderstandingService
        from job_search.service import JobSearchLedger
        with TemporaryDirectory() as d:
            path,ledger,runtime,_,obs,candidate,_=fixture(d)
            result=runtime.process(obs,subject='Interview',body='Please reply',candidates=[candidate])
            target=Path(d)/'portable.db'
            source=sqlite3.connect(path);dest=sqlite3.connect(target);dest.row_factory=sqlite3.Row
            try:
                source.backup(dest)
                with dest:
                    _rekey_database(dest,source_archive_key_provider=TestKey(),target_archive_key_provider=TestKey(bytes(reversed(range(32)))),cipher_factory=AESGCMCipher,nonce_factory=os.urandom)
                with self.assertRaises(sqlite3.IntegrityError):
                    dest.execute("UPDATE mail_understanding_sources SET plaintext_chars=0")
            finally:source.close();dest.close()
            exported=JobSearchLedger(target)
            archive=EncryptedMailArchive(exported,TestKey(bytes(reversed(range(32)))))
            request=MailUnderstandingService(exported,archive).get_request(result['analysis_id'])
            self.assertIn('Please reply',request['sources'][0]['text'])
            self.assertIn('Please reply',runtime.service.get_request(result['analysis_id'])['sources'][0]['text'])

    def test_shared_runtime_config_requires_explicit_source_scope(self):
        from dataclasses import replace
        baseline=RuntimeConfigV1.defaults()
        with self.assertRaises(ValueError):replace(baseline,mail_understanding_mode='shared').validate()
        replace(baseline,mail_understanding_mode='shared',mail_understanding_source_scope='current').validate()


class EvaluationTests(unittest.TestCase):
    def report(self,kind='reviewed_private_holdout'):
        cases=[dict(case_id=str(i),expected=[dict(kind='action',application_id='app',label='reply')],predicted=[dict(kind='action',application_id='app',label='reply',confidence=.99)]) for i in range(50)]
        return evaluate_cases(cases,producer_version='fixture-understanding-v1',dataset_kind=kind)['report']

    def test_version_bound_action_gate_and_private_artifact(self):
        data=self.report();report=validate_report(data)
        finding=dict(application_id='app',kind='reply',confidence=.99,actor='applicant',obligation='required')
        self.assertTrue(report.allows('action',finding,dict(producer_version='fixture-understanding-v1')))
        self.assertFalse(report.allows('action',finding,dict(producer_version='changed-model')))
        self.assertFalse(report.allows('action',finding,dict(producer_version='fixture-understanding-v1',coverage=[{'reason':'missing'}])))
        with TemporaryDirectory() as d:
            path=Path(d)/'report.json';path.write_text(json.dumps(data));path.chmod(0o600)
            self.assertIsNotNone(load_report(path))
            path.chmod(0o644);self.assertIsNone(load_report(path))
        with self.assertRaises(ContractError):validate_report(self.report('synthetic'))

    def test_invalid_outputs_and_duplicate_predictions_do_not_inflate_precision(self):
        pred=dict(kind='action',application_id='app',label='reply',confidence=.99)
        expected=dict(kind='action',application_id='app',label='reply')
        result=evaluate_cases([dict(case_id='one',expected=[expected],predicted=[pred,pred]),dict(case_id='two',expected=[expected],predicted=[pred],invalid_outputs=1)],producer_version='fixture')
        metric=result['report']['classes']['action:reply']
        self.assertEqual(metric['correct_predictions'],1)
        self.assertEqual(metric['high_confidence_predictions'],2)
        self.assertEqual(metric['missed_findings'],1)
        self.assertEqual(result['metrics']['duplicate_predictions'],1)


if __name__=='__main__':unittest.main()
