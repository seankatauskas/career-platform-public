"""Governor waits preserve owner retries; worker redelivery resumes exact work."""
from datetime import datetime, timedelta, timezone
import json
import unittest

from job_search.application_mail import build_application_mail_handlers, worker_context
from job_search.commands import DomainError
from job_search.db import connect
from job_search.inference.usage import AllowanceService, begin_invocation, InvocationPending
from job_search.scheduler import utc_stamp
from job_search.worker import Worker, PermanentTaskError
from tests import test_redesign_mail as mail_tests
from tests.test_job_search_automation import enqueue_work


class OwnerInferenceDeferralTest(unittest.TestCase):
    payload = mail_tests.ProductionMailTest.payload
    ingest = mail_tests.ProductionMailTest.ingest
    scheduled = mail_tests.ProductionMailTest.scheduled

    def setUp(self):
        mail_tests.ProductionMailTest.setUp(self)
        self.now = datetime(2026,10,10,12,tzinfo=timezone.utc)
        self.runtime.executor.clock = lambda:utc_stamp(self.now)
        self.handlers = build_application_mail_handlers(self.runtime,self.archive,"account-a",self.analyzer,
                                                        "fake-v1",operational_db=self.operational)
        self.serial = 0
        self.remote_calls = 0

    def key(self):
        self.serial += 1
        return str(self.serial)

    def issue(self):
        with self.runtime.executor.read() as con:
            return self.runtime.understanding.processing_for_source(con,self.source["id"],self.source["revision"])

    def decision(self, operation):
        issue = self.issue()
        return self.human.command(operation,{"issue_id":issue["issue_id"],"expected_version":issue["version"],
            "expected_analysis_id":issue["analysis_id"],"reason":"Retry after allowance deferral"},self.key())

    def make_retry(self):
        self.source = self.ingest()
        original = self.scheduled()[0]
        self.analyzer.failure = DomainError("invalid_output","fictional validation failure")
        with self.assertRaises(PermanentTaskError):
            self.handlers[original.task_kind](original.payload,self.context)
        self.analyzer.failure = None
        self.decision("retry_processing")

    def model_worker(self, *, limit=1):
        return Worker(self.operational,task_handlers=self.handlers,lane="model",now_provider=lambda:self.now,
                      max_work_per_tick=limit,max_outbox_per_tick=0,inference_usage_limits={"daily_tokens":100})

    def dispatch(self):
        identity = "dispatch-"+self.key()
        enqueue_work(self.operational,identity,"applications.mail.dispatch",due=self.now)
        with connect(self.operational) as con:
            con.execute("UPDATE work_items SET payload_json='{}' WHERE work_id=?",(identity,))
        Worker(self.operational,task_handlers=self.handlers,now_provider=lambda:self.now,
               max_work_per_tick=1,max_outbox_per_tick=0).tick(now=self.now)

    def consume_allowance(self):
        enqueue_work(self.operational,"budget","fixture.budget",due=self.now)
        def consume(payload, context):
            invocation = begin_invocation("fixture","fixture",b"earlier work",reserved_tokens=100)
            invocation.submitting()
            invocation.terminal("completed",observed_tokens=100)
            return {}
        Worker(self.operational,task_handlers={"fixture.budget":consume},now_provider=lambda:self.now,
               max_work_per_tick=1,max_outbox_per_tick=0,inference_usage_limits={"daily_tokens":100}).tick(now=self.now)

    def govern_analyzer(self, *, pending=False):
        analyze = self.analyzer.analyze
        def governed(context):
            invocation = begin_invocation("fixture","mail",b"same exact fictional request",
                                          reserved_tokens=1,retrieval_kind="runpod_job")
            if invocation.state == "reserved":
                invocation.submitting()
                self.remote_calls += 1
                invocation.accepted("same-provider-job")
                if pending:
                    raise InvocationPending()
            else:
                self.assertEqual(invocation.job_id,"same-provider-job")
            invocation.terminal("completed",observed_tokens=1)
            return analyze(context)
        self.analyzer.analyze = governed

    def queued_mail(self):
        with connect(self.operational) as con:
            return [dict(row) for row in con.execute("SELECT * FROM work_items WHERE task_kind='applications.mail.understand' ORDER BY created_at,work_id")]

    def retry_work(self):
        with self.runtime.executor.read() as con:
            return [item for item in self.runtime.executor.work_page(con)["items"] if item["kind"] == "retry_processing"]

    def test_daily_allowance_wait_preserves_retry_and_runs_after_midnight(self):
        self.make_retry()
        self.consume_allowance()
        self.govern_analyzer()
        self.dispatch()
        before = self.issue()
        self.assertEqual(self.model_worker().tick(now=self.now)["work"]["retried"],1)
        self.assertEqual(self.remote_calls,0)
        self.assertEqual(self.issue(),before)
        item = self.queued_mail()[0]
        self.assertEqual((item["status"],item["attempts"],item["failure_kind"],item["due_at"]),
                         ("queued",0,"usage_deferred","2026-10-11T00:00:00Z"))
        self.dispatch()  # Reconciliation must not retire this still-applicable retry.
        self.assertEqual(len(self.retry_work()),1)
        self.assertEqual(self.model_worker().tick(now=self.now)["work"]["succeeded"],0)
        self.now = datetime(2026,10,11,tzinfo=timezone.utc)
        self.assertEqual(self.model_worker().tick(now=self.now)["work"]["succeeded"],1)
        self.assertEqual(self.remote_calls,1)
        self.assertEqual(self.issue()["status"],"succeeded")
        self.assertEqual(self.issue()["attempt_count"],before["attempt_count"]+1)
        self.assertEqual(self.retry_work(),[])

    def test_pending_invocation_polls_same_provider_job_without_failed_attempt(self):
        self.make_retry()
        self.govern_analyzer(pending=True)
        self.dispatch()
        before = self.issue()
        self.assertEqual(self.model_worker().tick(now=self.now)["work"]["retried"],1)
        self.assertEqual(self.issue(),before)
        with connect(self.operational) as con:
            original = dict(con.execute("SELECT * FROM inference_invocations").fetchone())
        self.assertEqual(original["state"],"accepted")
        self.dispatch()
        self.now += timedelta(minutes=5)
        self.assertEqual(self.model_worker().tick(now=self.now)["work"]["succeeded"],1)
        self.assertEqual(self.remote_calls,1)
        with connect(self.operational) as con:
            rows = list(con.execute("SELECT invocation_id,provider_job_id,state FROM inference_invocations"))
        self.assertEqual([tuple(row) for row in rows],[(original["invocation_id"],"same-provider-job","completed")])
        self.assertEqual(self.issue()["attempt_count"],before["attempt_count"]+1)

    def test_allowance_grant_resumes_the_waiting_owner_retry_immediately(self):
        self.make_retry()
        self.consume_allowance()
        self.govern_analyzer()
        self.dispatch()
        before = self.issue()
        self.model_worker().tick(now=self.now)
        self.assertEqual(self.remote_calls,0)
        self.assertEqual(self.issue(),before)
        receipt = AllowanceService(self.operational).grant(budget_day="2026-10-10",tokens=100,
            command_id="user-top-up",reason="User requested an early allowance reset",now=self.now)
        self.assertEqual(receipt["woken_work_ids"],[self.queued_mail()[0]["work_id"]])
        self.assertEqual(self.model_worker().tick(now=self.now)["work"]["succeeded"],1)
        self.assertEqual(self.remote_calls,1)
        self.assertEqual(self.issue()["status"],"succeeded")
        self.assertEqual(self.retry_work(),[])

    def test_manual_resolution_while_waiting_prevents_later_model_submission(self):
        self.make_retry()
        self.consume_allowance()
        self.govern_analyzer()
        self.dispatch()
        self.model_worker().tick(now=self.now)
        self.decision("resolve_processing")
        self.now += timedelta(days=1)
        self.model_worker().tick(now=self.now)
        self.assertEqual(self.remote_calls,0)
        self.assertEqual(self.issue()["status"],"resolved_manually")

    def test_existing_erroneous_deferred_failure_recovers_by_new_public_retry(self):
        self.make_retry()
        self.consume_allowance()
        self.govern_analyzer()
        self.dispatch()
        self.model_worker().tick(now=self.now)
        old_retry = self.retry_work()[0]
        # Recreate the released version's erroneous receipt through the owner
        # write surface; preserve it as history instead of editing old rows.
        self.runtime.executor.run(worker_context("record_analysis","legacy-deferral"),"record_analysis",{},
            lambda tx:self.runtime.understanding.record_source_failure(tx,
                source_refs=[{"source_id":self.source["id"],"revision":self.source["revision"],
                              "sha256":self.source["source_sha256"]}],failure_code="provider_failed",retry=old_retry["payload"]))
        failed = self.issue()
        self.dispatch()
        self.assertEqual(self.retry_work(),[])
        self.decision("retry_processing")
        self.dispatch()
        self.model_worker().tick(now=self.now)
        self.assertEqual(self.issue()["status"],"retry_queued")
        self.assertEqual(self.issue()["analysis_id"],failed["analysis_id"])
        self.assertEqual(self.remote_calls,0)
        self.now += timedelta(days=1)
        self.assertEqual(self.model_worker(limit=5).tick(now=self.now)["work"]["succeeded"],2)
        self.assertEqual(self.remote_calls,1)  # Old delivery is obsolete, only new retry submits.
        self.assertEqual(self.issue()["status"],"succeeded")
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.get_analysis(con,failed["analysis_id"])["failure_code"],"provider_failed")

    def test_stale_deferral_cannot_release_replacement_claim(self):
        self.make_retry()
        context = self.analyzer.calls[0]
        def claim():
            return self.runtime.executor.run(worker_context("analyze_observation",self.key()),"analyze_observation",{},
                lambda tx:self.runtime.understanding.claim_analysis(tx,context=context,model_version="lease-fixture"))
        old = claim()
        self.now += timedelta(minutes=6)
        current = claim()
        with self.assertRaises(DomainError) as caught:
            self.runtime.executor.run(worker_context("analyze_observation",self.key()),"analyze_observation",{},
                lambda tx:self.runtime.understanding.defer_analysis(tx,claim=old,
                    reason_code="inference_daily_token_limit",retry_at="2026-10-11T00:00:00Z"))
        self.assertEqual(caught.exception.code,"version_conflict")
        with self.runtime.executor.read() as con:
            row = con.execute("SELECT * FROM understand_claims WHERE input_key=?",(current["input_key"],)).fetchone()
            self.assertEqual((row["status"],row["token"],row["generation"]),("processing",current["token"],current["generation"]))


if __name__ == "__main__": unittest.main()
