"""Grouped evidence, opt-in browser recording, and resumable migration contracts."""
import unittest
from job_search.application_transport import HumanApplicationAdapter, AgentApplicationAdapter
from job_search.applications.review import ReviewService
from job_search.commands import DomainError
from job_search.review_migration import apply
from tests import test_redesign_workflows as workflows
from tests import test_redesign_extension as extension
from tests.test_browser_tracking import observation
from tests import test_owner_processing_recovery as recovery


class GroupedReviewTest(unittest.TestCase):
    setUp = workflows.WorkflowsTest.setUp
    command = workflows.WorkflowsTest.command
    proposal = workflows.WorkflowsTest.proposal
    message = workflows.WorkflowsTest.message
    decide = workflows.WorkflowsTest.decide

    def ref(self):
        m = self.message()
        return {"owner":"correspondence", "source_id":m["id"], "revision":m["revision"], "sha256":m["source_sha256"]}

    def test_group_totals_whole_group_pagination_and_shared_issue(self):
        ref = self.ref()
        for i in range(4):
            self.proposal("add_note", {"application_id":self.app["id"], "text":str(i)}, evidence=[ref])
        self.proposal("add_note", {"application_id":self.app["id"], "text":"separate"})
        service = ReviewService(self.runtime)
        result = service.page(limit=1)
        self.assertEqual(result["totals"], {"decisions":2,"operations":5,"processing":0,"cards":2})
        groups = result["items"] + service.page(limit=1, cursor=result["next_cursor"])["items"]
        self.assertEqual(sorted(len(g["proposals"]) for g in groups), [1,4])
        email = next(g for g in groups if g["source_type"] == "email")
        self.assertEqual(len(email["evidence"]),1)
        self.assertEqual(service.page(group_id=email["proposals"][0]["id"])["items"][0]["id"], email["id"])

    def test_pending_target_correction_preserves_lineage_and_requires_review(self):
        ref = self.ref()
        p = self.proposal("confirm_submission", {"application_id":self.app["id"], "submission_id":"missing", "occurred_at":"2026-10-09T12:00:00Z"}, evidence=[ref], blockers=["requires_target_record_resolution"])
        other = self.command("save_job", {"job_source":{"source":"fixture", "source_id":"other"}})
        group = ReviewService(self.runtime).page()["items"][0]
        replacement = self.command("correct_review_target", {"group_id":group["id"], "expected_digest":group["preview_digest"],
            "application_id":other["id"], "reason":"Receipt identifies another employer", "receipt_scope":"application"})
        proposal = replacement["proposals"][0]
        self.assertEqual(proposal["operation"], "record_submission_receipt")
        self.assertEqual(proposal["application_id"], other["id"])
        self.assertEqual(proposal["blockers"], [])
        self.assertEqual(self.runtime.queries.workspace(other["id"])["records"]["receipts"]["items"], [])
        self.decide(proposal,replacement["association_proposal"])
        workspace = self.runtime.queries.workspace(other["id"])
        self.assertEqual(len(workspace["records"]["receipts"]["items"]),1)
        self.assertEqual(workspace["records"]["submissions"]["items"],[])
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.get(con,p["id"])["status"], "superseded")

    def test_stale_migration_is_reported_and_mail_is_never_auto_accepted(self):
        p = self.proposal("add_note", {"application_id":self.app["id"],"text":"manual"}, evidence=[self.ref()])
        service = ReviewService(self.runtime)
        manifest = service.migration_preview()
        result = apply(self.runtime, manifest, actor="owner")
        self.assertEqual(result["completed"],1)
        self.assertEqual(result,apply(self.runtime,manifest,actor="owner"))
        self.assertEqual(self.runtime.queries.review_queue()["items"][0]["status"], "pending")
        stale = service.migration_preview()
        self.decide(p)
        # Same manifest can replay its prior receipt; a fresh migration request
        # with changed policy must detect the changed source group.
        stale["groups"][0]["expected_digest"] = "changed-preview"
        self.assertEqual(apply(self.runtime,stale,actor="owner")["needs_refresh"],1)


    def test_previous_owner_schemas_upgrade_without_losing_records(self):
        from job_search.commands import CommandExecutor, CommandContext, Principal
        from job_search.applications._store import _before_review_schema
        from job_search.applications.understanding.api import _RECOVERY_PREVIOUS
        from job_search.correspondence.api import SCHEMA as mail_schema
        from job_search.external_actions.api import SCHEMA as actions_schema
        from job_search.application_runtime import ApplicationRuntime
        from pathlib import Path
        path=Path(self.tmp.name)/'previous.db'
        executor=CommandExecutor(path,{'applications':_before_review_schema,'understanding':_RECOVERY_PREVIOUS,'correspondence':mail_schema,'external_actions':actions_schema})
        app=executor.run(CommandContext(Principal('owner','human',{'*'}),'seed'),'save_job',{},
            lambda tx:self.runtime.applications.save_job(tx,{'job_source':{'source':'fixture','source_id':'preserved'}}))
        upgraded=ApplicationRuntime(path)
        with upgraded.executor.read() as con:
            self.assertEqual(upgraded.applications.get_application(con,app['id'])['id'],app['id'])
            self.assertFalse(upgraded.applications.recording_policy(con)['enabled'])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM understand_recovery_budget').fetchone()[0],0)
        self.assertEqual(ApplicationRuntime(path).executor.installation_identity(),executor.installation_identity())

    def test_retarget_cannot_change_a_record_belonging_to_another_application(self):
        task=self.command('create_task',{'application_id':self.app['id'],'kind':'reply','description':'Reply'})
        self.proposal('complete_task',{'task_id':task['id'],'expected_version':1},evidence=[self.ref()])
        other=self.command('save_job',{'job_source':{'source':'fixture','source_id':'other-app'}})
        group=ReviewService(self.runtime).page()['items'][0]
        result=self.command('correct_review_target',{'group_id':group['id'],'expected_digest':group['preview_digest'],'application_id':other['id'],'reason':'Correct pending target'})
        replacement=result['proposals'][0]
        self.assertIn('requires_target_record_resolution',replacement['blockers'])
        with self.assertRaises(DomainError):self.decide(result['association_proposal'],replacement)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.applications.get_record(con,'tasks',task['id'])['status'],'open')
            self.assertIsNone(self.runtime.correspondence.association(con,group['id'][6:]))

    def test_changed_source_invalidates_migration_without_retiring_the_proposal(self):
        from job_search.commands import CommandContext,Principal
        ref=self.ref()
        proposal=self.proposal('add_note',{'application_id':self.app['id'],'text':'keep'},evidence=[ref])
        manifest=ReviewService(self.runtime).migration_preview()
        self.runtime.command(CommandContext(Principal('mail','worker',{'record_message'}),'changed','inferred'),'record_message',
            {'account_id':'account','provider_message_id':'provider-message','source_version':2,'direction':'incoming','authored_text':'New revision','archive_ref':'changed'})
        self.assertEqual(apply(self.runtime,manifest,actor='owner')['needs_refresh'],1)
        current=ReviewService(self.runtime).page()['items'][0]['proposals'][0]
        self.assertEqual(current['id'],proposal['id'])
        self.assertIn('source_revision_changed',current['blockers'])


    def test_migration_retires_legacy_noise_and_replaces_receipts_but_keeps_rejections(self):
        from job_search.commands import CommandContext, Principal
        from job_search.applications.understanding import AnalysisInput, SourceText
        legacy=[]
        for index,(kind,value,text,operation,payload) in enumerate([
            ('contact',{'kind':'marketing'},'Engineering newsletter','record_progress',{'kind':'recruiter_contact'}),
            ('submission',{'status':'confirmed'},'Application received','confirm_submission',{'status':'confirmed'}),
            ('outcome',{'outcome':'rejected'},'We will not proceed','close_application',{'outcome':'rejected','reason':'Employer rejection'}),
        ]):
            message=self.runtime.command(CommandContext(Principal('mail','worker',{'record_message'}),'legacy:'+str(index),'inferred'),'record_message',
                {'account_id':'account','provider_message_id':'legacy:'+str(index),'source_version':1,'direction':'incoming','authored_text':text,'archive_ref':'fixture'})
            ref={'source_id':message['id'],'revision':message['revision'],'start':0,'end':len(text),'quote':text}
            output={'relevance':'career_related','associations':[{'kind':'application','target_id':self.app['id'],'evidence':[ref]}],
                'facts':[{'kind':kind,'value':value,'target_id':self.app['id'],'evidence':[ref]}],'requests':[],'temporal_facts':[],'uncertainties':[]}
            context=AnalysisInput((SourceText(message['id'],message['revision'],message['source_sha256'],text),),candidates=({'id':self.app['id']},),
                versions={'application:'+self.app['id']:1,'message:'+message['id']:message['version']})
            analysis=self.runtime.executor.run(CommandContext(Principal('old-model','worker',{'record_analysis'}),str(index),'inferred'),'record_analysis',{},
                lambda tx:self.runtime.understanding.store_analysis(tx,context=context,model_version='old',output=output))
            def old_projection(tx):
                link=self.runtime.understanding.project_analysis(tx,analysis_id=analysis['id'],projections=[{
                    'finding_id':next(f['id'] for f in analysis['findings'] if f['category']=='associations'),'operation':'link_message',
                    'application_id':self.app['id'],'input':{'message_id':message['id'],'application_id':self.app['id'],'expected_version':0},'expected_versions':context.versions}])['proposals'][0]
                proposal=self.runtime.understanding.project_analysis(tx,analysis_id=analysis['id'],projections=[{
                    'finding_id':next(f['id'] for f in analysis['findings'] if f['category']=='facts'),'operation':operation,'application_id':self.app['id'],
                    'input':{'application_id':self.app['id'],'evidence':[ref],**payload},'expected_versions':context.versions,'dependencies':[link['id']]}])['proposals'][0]
                return {'link':link,'proposal':proposal}
            legacy.append(self.runtime.executor.run(CommandContext(Principal('old-model','worker',{'project_analysis'}),str(index),'inferred'),'project_analysis',{},old_projection))
        manifest=ReviewService(self.runtime).migration_preview()
        result=apply(self.runtime,manifest,actor='owner')
        self.assertEqual(result['needs_refresh'],0)
        with self.runtime.executor.read() as con:
            get=lambda p:self.runtime.understanding.get(con,p['id'])['status']
            self.assertEqual(get(legacy[0]['link']),'superseded')
            self.assertEqual(get(legacy[0]['proposal']),'superseded')
            self.assertEqual(get(legacy[1]['proposal']),'superseded')
            self.assertEqual(get(legacy[2]['proposal']),'pending')
            receipt=next(p for p in self.runtime.understanding.list_pending(con)['items'] if p['operation']=='record_submission_receipt')
            self.assertEqual(receipt['dependencies'],[legacy[1]['link']['id']])
            self.assertEqual(self.runtime.applications.get_application(con,self.app['id'])['disposition'],'open')
            self.assertEqual(self.runtime.applications.list_records(con,self.app['id'],'submissions')['items'],[])


class BrowserRecordingTest(unittest.TestCase):
    setUp = extension.PairedExtensionTest.setUp
    send = extension.PairedExtensionTest.send
    records = extension.PairedExtensionTest.records

    def enable(self):
        return HumanApplicationAdapter(self.runtime,"owner").command("configure_browser_recording", {"enabled":True,"expected_version":0},"enable")

    def test_opt_in_records_one_attempt_then_confirmation_and_replays(self):
        self.enable()
        first = observation(title="Engineer",employer="Acme")
        self.send(first)
        self.assertEqual(len(self.records("submissions")),1)
        self.assertEqual(self.records("submissions")[0]["status"],"attempted")
        ack = observation("site_acknowledged",first["attempt_id"])
        self.send(ack)
        self.send(ack)
        self.assertEqual(len(self.records("submissions")),1)
        self.assertEqual(self.records("submissions")[0]["status"],"confirmed")
        self.assertEqual(ReviewService(self.runtime).page()["totals"]["cards"],0)
        with self.assertRaises(DomainError):
            AgentApplicationAdapter(self.runtime).call("configure_browser_recording",{"enabled":True,"expected_version":1},idempotency_key="bad")

    def test_preview_migrates_existing_browser_without_mail_authority(self):
        first = observation(title="Engineer",employer="Acme")
        self.send(first)
        self.send(observation("site_acknowledged",first["attempt_id"]))
        service = ReviewService(self.runtime)
        manifest = service.migration_preview(enable_browser=True)
        self.assertFalse(manifest["policy"]["enabled"])
        self.assertEqual(manifest["groups"][0]["decisions"][0]["disposition"],"record_browser")
        result = apply(self.runtime,manifest,actor="owner")
        self.assertEqual(result["needs_refresh"],0)
        self.assertEqual(result,apply(self.runtime,manifest,actor="owner"))
        self.assertEqual(len(self.records("submissions")),1)
        self.assertEqual(service.page()["items"],[])

    def test_late_answers_attach_once_to_the_exact_automatic_attempt(self):
        from tests.test_browser_tracking import URL
        from job_search.contracts import utc_now
        self.enable()
        first=observation(title="Engineer",employer="Acme")
        self.send(first)
        capture={"capture_id":"c"*32, "attempt_id":first["attempt_id"], "page_url":URL,"captured_at":utc_now(),
            "snapshot":{"version":1,"fields":[{"field_key":"why","prompt":"Why this role?","section":"","control":"textarea","value":"Exact answer"}],"omitted_fields":0,"truncated_values":0}}
        self.send(capture,"answers")
        self.send(capture,"answers")
        submissions=self.records("submissions")
        self.assertEqual(len(submissions),1)
        self.assertEqual(len(submissions[0]["answer_snapshots"]),1)
        self.assertEqual(ReviewService(self.runtime).page()["items"],[])

    def test_conflicting_documents_are_retained_for_review(self):
        first=observation(title="Engineer",employer="Acme",resume_sha256="a"*64)
        self.send(first)
        self.send(observation("site_acknowledged",first["attempt_id"],resume_sha256="b"*64))
        manifest=ReviewService(self.runtime).migration_preview(enable_browser=True)
        self.assertEqual(manifest["groups"][0]["decisions"][0]["disposition"],"retained_browser_exception")
        apply(self.runtime,manifest,actor="owner")
        self.assertEqual(self.records("submissions"),[])
        self.assertTrue(ReviewService(self.runtime).page()["items"][0]["proposals"][0]["blockers"])


class BoundedRecoveryTest(unittest.TestCase):
    setUp=recovery.ProcessingRecoveryTest.setUp
    key=recovery.ProcessingRecoveryTest.key
    message=recovery.ProcessingRecoveryTest.message
    worker=recovery.ProcessingRecoveryTest.worker
    failure=recovery.ProcessingRecoveryTest.failure
    issue=recovery.ProcessingRecoveryTest.issue

    def test_two_automatic_attempts_retained_and_source_grouped(self):
        self.failure(code="evidence_mismatch")
        for _ in range(2):
            manifest=ReviewService(self.runtime).migration_preview()
            result=apply(self.runtime,manifest,actor="owner")
            self.assertEqual(result["needs_refresh"],0)
            self.assertEqual(self.issue()["status"],"retry_queued")
            with self.runtime.executor.read() as con:
                work=self.runtime.executor.work_page(con,limit=100)["items"]
                retry=next(w["payload"] for w in reversed(work) if w["kind"]=="retry_processing" and w["payload"]["analysis_id"]==self.issue()["analysis_id"])
            self.failure(retry=retry,code="evidence_mismatch")
        result=apply(self.runtime,ReviewService(self.runtime).migration_preview(),actor="owner")
        self.assertEqual(self.issue()["status"],"open")
        self.assertEqual(self.issue()["attempt_count"],3)
        self.assertEqual(ReviewService(self.runtime).page()["totals"]["cards"],1)


if __name__ == '__main__':
    unittest.main()
