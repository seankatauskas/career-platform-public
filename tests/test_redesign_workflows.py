"""Cross-owner acceptance against one real database and public transport adapters."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter,AgentApplicationAdapter,BrowserObservationAdapter
from job_search.commands import CommandContext,Principal,DomainError
from job_search.external_actions.api import ProviderOutcome


class WorkflowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime=ApplicationRuntime(Path(self.tmp.name)/"candidate.db",clock=lambda:"2026-10-09T12:00:00Z")
        self.human=HumanApplicationAdapter(self.runtime,"human")
        self.agent=AgentApplicationAdapter(self.runtime)
        self.serial=0
        self.app=self.command("save_job",{"job_source":{"source":"fixture","source_id":"role-1","employer":"Example","title":"Engineer"}})

    def command(self,operation,payload):
        self.serial+=1
        if operation=="close_application" and "expected_records" not in payload:
            with self.runtime.executor.read() as con:
                preview=self.runtime.applications.preview_closure(con,payload["application_id"])
            payload={**payload,"expected_records":preview["expected_records"]}
        return self.human.command(operation,payload,str(self.serial))

    def proposal(self,operation,payload,**extra):
        self.serial+=1
        return self.agent.call("propose_changes",{"operation":operation,"input":payload,
            "application_id":self.app["id"],**extra},idempotency_key=str(self.serial))

    def decide(self,*proposals):
        return self.command("review_changes",{"decisions":[{"proposal_id":p["id"],"expected_version":p["version"],"decision":"accept"} for p in proposals]})

    def message(self):
        principal=Principal("mail-worker","worker",{"record_message"})
        return self.runtime.command(CommandContext(principal,"message","inferred"),"record_message",
            {"account_id":"account","provider_message_id":"provider-message","source_version":1,
             "direction":"incoming","authored_text":"Please reply.","archive_ref":"fixture-body"})

    def test_agent_proposes_human_applies_and_reads_do_not_mutate(self):
        p=self.proposal("create_task",{"application_id":self.app["id"],"kind":"reply","description":"Reply"})
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["records"]["tasks"]["items"],[])
        with self.assertRaises(DomainError):
            self.agent.call("review_changes",{"decisions":[]},idempotency_key="attack")
        self.decide(p)
        first=self.runtime.queries.workspace(self.app["id"])
        second=self.runtime.queries.workspace(self.app["id"])
        self.assertEqual(first,second)
        self.assertEqual(len(first["records"]["tasks"]["items"]),1)

    def test_dependency_bundle_and_stale_review_are_atomic(self):
        message=self.message()
        link=self.proposal("link_message",{"message_id":message["id"],"application_id":self.app["id"]})
        task=self.proposal("create_task",{"application_id":self.app["id"],"kind":"reply","description":"Reply"},dependencies=[link["id"]])
        with self.assertRaises(DomainError):
            self.decide(task)
        result=self.decide(task,link)
        self.assertEqual(len(result["decisions"]),2)
        stale=self.proposal("add_note",{"application_id":self.app["id"],"text":"Would be stale"},expected_versions={"application:"+self.app["id"]:99})
        valid=self.proposal("add_note",{"application_id":self.app["id"],"text":"Must roll back"})
        with self.assertRaises(DomainError):
            self.decide(valid,stale)
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["records"]["notes"]["items"],[])

    def action(self):
        message=self.message()
        link=self.command("link_message",{"message_id":message["id"],"application_id":self.app["id"]})
        task=self.command("create_task",{"application_id":self.app["id"],"kind":"reply","description":"Reply","completion_rule":"verified_send"})
        envelope={"kind":"send_reply","account_id":"account","application_id":self.app["id"],"pursuit_no":1,
            "target":{"message_id":message["id"],"provider_message_id":message["provider_message_id"],
                "source_hash":message["source_sha256"],"provider_source_hash":"b"*64},
            "payload":{"recipients":["recruiter@example.test"],"subject":"Re: Role","body":"Exact e\u0301 reply"},
            "context_versions":{"application:"+self.app["id"]:1,"message:"+message["id"]:1,"association:"+link["id"]:1},
            "consequence":{"operation":"complete_task","task_id":task["id"],"expected_version":1,"completion_rule":"verified_send"}}
        action=self.command("prepare_reply",{"envelope":envelope})
        self.command("authorize_action",{"action_id":action["action_id"],"expected_digest":action["digest"]})
        return action,task

    def test_verified_send_result_delivered_once_without_resending(self):
        action,task=self.action()
        class Provider:
            test_only=True
            writes=0
            def preflight(self,action): pass
            def perform(self,action):
                self.writes+=1
                return ProviderOutcome("succeeded",{"remote_id":"sent"},{"verified":True})
        provider=Provider()
        result=self.runtime.test_action_worker(provider).execute(action["action_id"])
        self.assertEqual(result["execution"],"succeeded")
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["records"]["tasks"]["items"][0]["status"],"open")
        self.runtime.process_results()
        self.runtime.process_results()
        self.assertEqual(provider.writes,1)
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["records"]["tasks"]["items"][0]["status"],"completed")

    def test_closed_application_preserves_external_success_as_conflict(self):
        action,task=self.action()
        class Provider:
            test_only=True
            def preflight(self,action): pass
            def perform(self,action): return ProviderOutcome("succeeded",{}, {"verified":True})
        self.runtime.test_action_worker(Provider()).execute(action["action_id"])
        self.command("close_application",{"application_id":self.app["id"],"expected_version":1,"reason":"Stop pursuit"})
        result=self.runtime.process_results()["items"][0]
        self.assertEqual(result["delivery"],"conflict")
        view=self.runtime.queries.workspace(self.app["id"])
        self.assertEqual(view["actions"]["items"][0]["execution"],"succeeded")
        self.assertEqual(view["records"]["tasks"]["items"][0]["status"],"cancelled")

    def test_analysis_resume_does_not_repeat_model_call_or_duplicate_requests(self):
        message=self.message()
        app_id=self.app["id"]
        class Archive:
            def read_message(self,reference): return "Please reply."
        class Analyzer:
            calls=0
            def analyze(self,context):
                self.calls+=1
                source=context.sources[0]
                evidence=[{"source_id":source.source_id,"revision":source.revision,"start":0,"end":13,"quote":"Please reply."}]
                return {"relevance":"career_related","associations":[{"kind":"application","target_id":app_id,"evidence":evidence}],
                    "facts":[],"requests":[{"kind":"reply","outcome":"Reply","responsible_party":"applicant","requirement":"required","channel":"email","target_id":app_id,"evidence":evidence}],
                    "temporal_facts":[],"uncertainties":[]}
        analyzer=Analyzer()
        arguments={"archive":Archive(),"allowed_accounts":["account"],"analyzer":analyzer,"model_version":"fixture-1",
            "candidate_ids":[app_id],"target_application_id":app_id}
        first=self.runtime.analyze_message(message["id"],**arguments)
        second=self.runtime.analyze_message(message["id"],**arguments)
        self.assertEqual(first,second)
        self.assertEqual(analyzer.calls,1)
        self.assertEqual(len(first["proposals"]),2)
        self.decide(*first["proposals"])
        self.assertEqual(len(self.runtime.queries.workspace(app_id)["records"]["tasks"]["items"]),1)

    def test_missing_source_and_invalid_model_output_remain_visible(self):
        message=self.message()
        class MissingArchive:
            def read_message(self,reference): raise OSError("offline")
        class Archive:
            def read_message(self,reference): return "Please reply."
        class InvalidAnalyzer:
            def analyze(self,context): return {"bad":float("nan")}
        args={"archive":MissingArchive(),"allowed_accounts":["account"],"analyzer":InvalidAnalyzer(),"model_version":"invalid"}
        first=self.runtime.analyze_message(message["id"],candidate_ids=[],**args)
        second=self.runtime.analyze_message(message["id"],candidate_ids=[self.app["id"]],**args)
        self.assertEqual(first["status"],"source_unavailable")
        self.assertEqual(second["status"],"source_unavailable")
        args["archive"]=Archive()
        invalid=self.runtime.analyze_message(message["id"],candidate_ids=[self.app["id"]],**args)
        self.assertEqual(invalid["status"],"failed")
        self.assertEqual(invalid["failure"],"invalid_output")
        view=self.runtime.queries.workspace(self.app["id"])
        self.assertEqual(view["records"]["tasks"]["items"],[])
        self.assertTrue(view["analysis_coverage"]["items"])

    def test_replacement_is_pending_and_cannot_clear_unresolved_blockers(self):
        original=self.proposal("add_note",{"application_id":self.app["id"],"text":"Draft"},blockers=["needs_mapping","needs_time"])
        replacement=self.command("replace_proposal",{"proposal_id":original["id"],"expected_version":original["version"],
            "input":{"application_id":self.app["id"],"text":"Edited"},"resolved_blockers":["needs_mapping"],"reason":"Correct wording"})
        self.assertEqual(replacement["blockers"],["needs_time"])
        with self.assertRaises(DomainError): self.decide(replacement)
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["records"]["notes"]["items"],[])

    def test_scheduled_task_is_authorized_once_without_delivery(self):
        scheduled=self.command("schedule_operation",{"application_id":self.app["id"],"operation":"create_task",
            "input":{"kind":"follow_up","description":"Check the portal"},"due_at":"2026-10-10T12:00:00Z","reason":"My follow-up"})
        self.runtime.executor.clock=lambda:"2026-10-11T12:00:00Z"
        first=self.runtime.process_scheduled()
        second=self.runtime.process_scheduled()
        self.assertEqual(len(first["schedules"]),1)
        self.assertEqual(second["schedules"],[])
        view=self.runtime.queries.workspace(self.app["id"])
        self.assertEqual(len(view["records"]["tasks"]["items"]),1)
        self.assertEqual(view["records"]["schedules"]["items"][0]["status"],"executed")

    def test_browser_confirmation_requires_review_and_keeps_exact_answers(self):
        browser=BrowserObservationAdapter(self.runtime)
        result=browser.observe("paired-device",{"application_id":self.app["id"],"observation_id":"observation-one",
            "attempt_ref":"attempt-one","activity":"website_acknowledgment","answers":{"why":"Exact e\u0301\n  answer"}},idempotency_key="capture")
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["progress"]["stage"],"tracking")
        self.decide(*result["review"]["proposals"])
        view=self.runtime.queries.workspace(self.app["id"])
        self.assertEqual(view["progress"]["stage"],"active")
        self.assertEqual(view["records"]["submissions"]["items"][0]["answers"]["why"],"Exact e\u0301\n  answer")

    def test_combination_keeps_successful_action_history_in_canonical_workspace(self):
        action,_=self.action()
        class Provider:
            test_only=True
            def preflight(self,action): pass
            def perform(self,action): return ProviderOutcome("succeeded",{}, {"verified":True})
        self.runtime.test_action_worker(Provider()).execute(action["action_id"])
        self.runtime.process_results()
        target=self.command("save_job",{"job_source":{"source":"fixture","source_id":"canonical"}})
        with self.runtime.executor.read() as con:
            preview=self.runtime.applications.preview_combination(con,self.app["id"],target["id"])
        self.command("combine_jobs",{"source_application_id":self.app["id"],"target_application_id":target["id"],
            "source_version":1,"target_version":1,"expected_records":preview["expected_records"],
            "disposition":"open","reason":"Reviewed duplicate job"})
        view=self.runtime.queries.workspace(target["id"])
        self.assertEqual(view["actions"]["items"][0]["envelope"]["application_id"],self.app["id"])
        self.assertEqual(view["actions"]["items"][0]["execution"],"succeeded")
        self.assertEqual(view["results"]["items"][0]["delivery"],"applied")

    def test_correction_requires_all_explicit_resolutions_and_new_target_review(self):
        message=self.message()
        link=self.command("link_message",{"message_id":message["id"],"application_id":self.app["id"]})
        proposal=self.proposal("create_task",{"application_id":self.app["id"],"kind":"reply","description":"Source obligation",
            "evidence":[{"source_id":message["id"]}]},evidence=[{"source_id":message["id"]}])
        self.decide(proposal)
        target=self.command("save_job",{"job_source":{"source":"fixture","source_id":"role-two"}})
        with self.runtime.executor.read() as con:
            preview=self.runtime.workflows.preview_association_correction(con,link["id"])
        payload={"association_id":link["id"],"application_id":target["id"],"expected_version":1,
            "preview_digest":preview["preview_digest"],"corrections":[],"reason":"Wrong role"}
        with self.assertRaises(DomainError):
            self.command("correct_association",payload)
        payload["corrections"]=[{"kind":item["kind"],"id":item["id"],"expected_version":item["expected_version"],"resolution":"retract"} for item in preview["affected"]]
        result=self.command("correct_association",payload)
        self.assertEqual(result["association"]["application_id"],target["id"])
        self.assertEqual(self.runtime.queries.workspace(target["id"])["records"]["tasks"]["items"],[])
        self.assertEqual(result["proposals"][0]["status"],"pending")


if __name__=="__main__":
    unittest.main()
