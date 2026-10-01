"""Offline crash/restart and atomic usage tests with real SQLite reservations."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from job_search.contracts import ConflictError, ContractError
from job_search.db import connect, prepare_database
from job_search.inference import load_inference_config, RunpodQueuedStructuredGenerator, RunpodQueuedEmbeddingProvider, OpenAICompatibleStructuredGenerator
from job_search.inference.contracts import InferenceTransportError
from job_search.inference.usage import (UsagePolicy, UsageDeferred, InvocationPending, InvocationReconciliationRequired,
    InvocationRecoveryService, begin_invocation, invocation_scope, usage_report, work_can_resume, heartbeat_scope)
from job_search.scheduler import utc_stamp
from job_search.worker import Worker
from tests.test_job_search_inference import _queued_profile, _profile, _queue_clock, _completion, QueueTransport
from tests.test_resume_lab_runpod_model import _model, _response, _completion as resume_completion, FakeTransport


NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


class Crash(BaseException):
    pass


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "applications.db"
        prepare_database(self.path, utc_stamp(NOW))
        self.work()
        self.config = load_inference_config(_queued_profile(self.root))

    def work(self, identity="work-a", status="running", revision=1):
        with connect(self.path) as con:
            con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,created_at,recovery_revision,lease_expires_at) VALUES (?,'opportunity.preference_refresh',?,'{}',?,?,3,?,?,?)",
                        (identity, identity, status, utc_stamp(NOW), utc_stamp(NOW), revision, utc_stamp(NOW + timedelta(minutes=4))))

    def revision(self, revision=2, identity="work-a"):
        with connect(self.path) as con:
            con.execute("UPDATE work_items SET recovery_revision=?,status='running',lease_expires_at=? WHERE work_id=?", (revision, utc_stamp(NOW+timedelta(minutes=4)), identity))

    def scope(self, identity="work-a", revision=1, *, policy=None, now=NOW):
        return invocation_scope(self.path, identity, revision, policy=policy, clock=lambda: now)

    def provider(self, responses, config=None):
        transport = QueueTransport(responses)
        _, clock, sleep = _queue_clock()
        return RunpodQueuedStructuredGenerator(config or self.config.structured_generation, transport=transport, clock=clock, sleeper=sleep), transport

    def generate(self, provider, text="Sensitive evidence: fixture person"):
        return provider.generate([{"role":"user", "content":text}], json_schema={"type":"object"}, schema_name="fixture", max_output_tokens=100)

    def completed(self, identity="job-one"):
        return {"id": identity, "status":"COMPLETED", "output": [_completion()]}

    def accepted_then_crash(self):
        provider, transport = self.provider([{"id":"job-one", "status":"IN_QUEUE"}, Crash()])
        with self.scope(), self.assertRaises(Crash):
            self.generate(provider)
        return transport

    def test_pre_post_reservation_is_durable_without_prompt_or_response_storage(self):
        with self.scope():
            invocation = begin_invocation("private-provider-url", "generation", b"Sensitive evidence", reserved_tokens=900)
        report = usage_report(self.path, now=NOW)
        self.assertEqual(report["reserved_requests"], 1)
        self.assertEqual(report["reserved_tokens"], 900)
        self.assertEqual(report["inflight"], 1)
        self.assertEqual(invocation.state, "reserved")
        with sqlite3.connect(self.path) as con:
            dump = "\n".join(con.iterdump())
        self.assertNotIn("Sensitive evidence", dump)
        self.assertNotIn("private-provider-url", dump)

    def test_crash_before_post_reuses_reservation(self):
        with self.scope():
            original = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
        self.revision()
        with self.scope(revision=2):
            recovered = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            recovered.submitting()
            recovered.accepted("job-one")
            recovered.terminal("completed")
        self.assertEqual(original.invocation_id, recovered.invocation_id)
        self.assertEqual(usage_report(self.path, now=NOW)["reserved_requests"], 1)

    def test_accepted_job_is_polled_by_new_provider_after_restart(self):
        first = self.accepted_then_crash()
        self.revision()
        second, transport = self.provider([self.completed()])
        with self.scope(revision=2):
            self.assertEqual(self.generate(second).text, '{"answer":true}')
        self.assertEqual([call["method"] for call in first.calls], ["POST", "GET"])
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])
        self.assertTrue(transport.calls[0]["url"].endswith("/status/job-one"))
        self.assertEqual(usage_report(self.path, now=NOW)["reserved_requests"], 1)
        self.assertEqual(usage_report(self.path, now=NOW)["inflight"], 0)

    def test_crash_after_completion_retrieves_same_result_without_new_post(self):
        first, _ = self.provider([self.completed()])
        with self.scope(): self.generate(first)
        self.revision()
        second, transport = self.provider([self.completed()])
        with self.scope(revision=2): self.generate(second)
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_crash_during_submission_cannot_repeat_post(self):
        first, transport = self.provider([Crash()])
        with self.scope(), self.assertRaises(Crash): self.generate(first)
        self.revision()
        second, later = self.provider([])
        with self.scope(revision=2), self.assertRaises(InvocationReconciliationRequired): self.generate(second)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(later.calls, [])
        self.assertEqual(usage_report(self.path, now=NOW)["uncertain"], 1)

    def test_changed_prompt_or_provider_cannot_bypass_pending_old_attempt(self):
        self.accepted_then_crash()
        self.revision()
        for text, config in (("different prompt", None), ("Sensitive evidence: fixture person", replace(self.config.structured_generation, endpoint_id="newendpoint"))):
            provider, transport = self.provider([], config)
            with self.subTest(text=text, config=config), self.scope(revision=2), self.assertRaises(InvocationReconciliationRequired):
                self.generate(provider, text)
            self.assertEqual(transport.calls, [])

    def test_stale_worker_cannot_update_accepted_outcome(self):
        with self.scope():
            invocation = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            invocation.submitting(); invocation.accepted("job-one")
        self.revision()
        with self.assertRaises(ConflictError): invocation.terminal("completed")
        self.assertEqual(InvocationRecoveryService(self.path).list_invocations()[0]["state"], "accepted")

    def test_state_compare_and_swap_rejects_duplicate_submission(self):
        with self.scope():
            first = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            second = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            first.submitting()
            with self.assertRaises(InvocationReconciliationRequired): second.submitting()

    def test_atomic_last_request_reservation_across_workers(self):
        self.work("work-b")
        def reserve(identity):
            with self.scope(identity, policy=UsagePolicy(daily_requests=1)):
                try:
                    begin_invocation("provider", "generation", identity.encode(), reserved_tokens=100)
                    return "reserved"
                except UsageDeferred:
                    return "deferred"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(reserve, ["work-a", "work-b"])), ["deferred", "reserved"])

    def test_daily_token_budget_is_atomic_and_conservative(self):
        self.work("work-b")
        with self.scope(policy=UsagePolicy(daily_tokens=100)):
            inv = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            inv.submitting(); inv.accepted("job-one"); inv.terminal("completed", observed_tokens=3)
        with self.scope("work-b", policy=UsagePolicy(daily_tokens=100)), self.assertRaises(UsageDeferred):
            begin_invocation("provider", "generation", b"other", reserved_tokens=1)
        self.assertEqual(usage_report(self.path, now=NOW)["reserved_tokens"], 100)

    def test_utc_rollover_does_not_release_uncertain_inflight_capacity(self):
        with self.scope(policy=UsagePolicy(daily_requests=1, max_inflight=1)):
            inv = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            inv.submitting(); inv.unknown()
        self.work("work-b")
        with self.scope("work-b", policy=UsagePolicy(daily_requests=1, max_inflight=1), now=NOW+timedelta(days=1)), self.assertRaises(UsageDeferred) as caught:
            begin_invocation("provider", "generation", b"other", reserved_tokens=100)
        self.assertEqual(caught.exception.reason_code, "inference_inflight_limit")
        report = usage_report(self.path, now=NOW+timedelta(days=1))
        self.assertEqual(report["reserved_requests"], 0)
        self.assertEqual(report["inflight"], 1)

    def test_utc_rollover_reopens_daily_request_allowance(self):
        with self.scope(policy=UsagePolicy(daily_requests=1)):
            inv = begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            inv.submitting(); inv.accepted("job-one"); inv.terminal("completed")
        self.work("work-b")
        with self.scope("work-b", policy=UsagePolicy(daily_requests=1), now=NOW+timedelta(days=1)):
            begin_invocation("provider", "generation", b"other", reserved_tokens=100)
        self.assertEqual(usage_report(self.path, now=NOW+timedelta(days=1))["reserved_requests"], 1)

    def test_terminal_failed_job_can_get_one_new_bounded_attempt(self):
        first, _ = self.provider([{"id":"job-one", "status":"FAILED"}])
        with self.scope(), self.assertRaises(InferenceTransportError): self.generate(first)
        self.revision()
        second, calls = self.provider([self.completed("job-two")])
        with self.scope(revision=2): self.generate(second)
        self.assertEqual(calls.calls[0]["method"], "POST")
        self.assertEqual(usage_report(self.path, now=NOW)["reserved_requests"], 2)

    def test_synchronous_ambiguous_call_is_reserved_and_never_reposted(self):
        config = load_inference_config(_profile(self.root)).structured_generation
        calls = []
        def transport(*args):
            calls.append(args)
            raise InferenceTransportError("lost response", retryable=True)
        provider = OpenAICompatibleStructuredGenerator(config, transport)
        with self.scope(), self.assertRaises(InferenceTransportError): self.generate(provider)
        self.revision()
        with self.scope(revision=2), self.assertRaises(InvocationReconciliationRequired): self.generate(provider)
        self.assertEqual(len(calls), 1)

    def test_synchronous_success_does_not_persist_sensitive_result(self):
        config = load_inference_config(_profile(self.root)).structured_generation
        provider = OpenAICompatibleStructuredGenerator(config, lambda *_args: _completion('{"private":"private-output-value"}'))
        with self.scope(): self.generate(provider)
        with sqlite3.connect(self.path) as con: dump = "\n".join(con.iterdump())
        self.assertNotIn("private-output-value", dump)
        self.assertEqual(usage_report(self.path, now=NOW)["inflight"], 0)

    def test_synchronous_success_without_checkpoint_is_inspectable_after_restart(self):
        config=load_inference_config(_profile(self.root)).structured_generation
        calls=[]
        def transport(*_args): calls.append(True); return _completion()
        provider=OpenAICompatibleStructuredGenerator(config,transport)
        with self.scope(): self.generate(provider)
        self.revision()
        with self.scope(revision=2),self.assertRaises(InvocationReconciliationRequired): self.generate(provider)
        item=InvocationRecoveryService(self.path).list_invocations()[0]
        self.assertEqual(item["reconciliation_reason"],"inference_result_not_checkpointed")
        self.assertEqual(item["retrieval_kind"],"none")
        self.assertEqual(len(calls),1)

    def test_embedding_batches_share_the_same_request_governor(self):
        config = self.config.embeddings
        output = {"model":config.model, "data":[{"index":0,"embedding":[1.,0.,0.]},{"index":1,"embedding":[0.,1.,0.]}],
                  "job_search_model_revision":"c"*40,"job_search_worker_protocol":config.worker_protocol}
        transport = QueueTransport([{"id":"embed-one","status":"COMPLETED","output":output}])
        _, clock, sleep = _queue_clock()
        provider = RunpodQueuedEmbeddingProvider(config, transport=transport, clock=clock, sleeper=sleep)
        with self.scope(policy=UsagePolicy(daily_requests=1)), self.assertRaises(UsageDeferred): provider.encode(["one","two","three"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(usage_report(self.path, now=NOW)["reserved_requests"], 1)

    def test_resume_provider_shares_ledger_and_polls_accepted_id_after_restart(self):
        first = FakeTransport([_response({"id":"resume-one","status":"IN_QUEUE"}), Crash()])
        model, _ = _model(self.root, first)
        with self.scope(), self.assertRaises(Crash): model._invoke("extract_job_requirements", {"job_description":"fixture"}, {"requirements":[]})
        self.revision()
        second = FakeTransport([_response({"id":"resume-one","status":"COMPLETED","output":resume_completion('{"requirements":[]}')})])
        model2, _ = _model(self.root, second)
        with self.scope(revision=2):
            self.assertEqual(model2._invoke("extract_job_requirements", {"job_description":"fixture"}, {"requirements":[]}), {"requirements":[]})
        self.assertEqual([call["method"] for call in second.calls], ["GET"])
        self.assertEqual(usage_report(self.path, now=NOW)["reserved_requests"], 1)

    def test_usage_deferral_preserves_queue_and_does_not_consume_attempt(self):
        with connect(self.path) as con: con.execute("UPDATE work_items SET status='queued',attempts=0,max_attempts=1,recovery_revision=0")
        provider, transport = self.provider([])
        worker = Worker(self.path, task_handlers={"opportunity.preference_refresh":lambda _p,_c:self.generate(provider)},
                        now_provider=lambda:NOW, max_work_per_tick=1, max_outbox_per_tick=0, inference_usage_limits={"daily_tokens":1})
        worker.tick()
        with connect(self.path) as con:
            row = con.execute("SELECT status,attempts,failure_kind,due_at FROM work_items").fetchone()
        self.assertEqual(tuple(row[:3]), ("queued",0,"usage_deferred"))
        self.assertEqual(row[3], "2026-09-02T00:00:00Z")
        self.assertEqual(transport.calls, [])
        self.assertEqual(usage_report(self.path, now=NOW)["deferred_work"], 1)

    def test_worker_cannot_report_success_after_swallowed_ambiguous_submission(self):
        with connect(self.path) as con: con.execute("UPDATE work_items SET status='queued',recovery_revision=0")
        def handler(_p,_c):
            inv=begin_invocation("provider","generation",b"request",reserved_tokens=100)
            inv.submitting(); inv.unknown()
            return {"status":"failed"}
        worker=Worker(self.path,task_handlers={"opportunity.preference_refresh":handler},now_provider=lambda:NOW,max_work_per_tick=1,max_outbox_per_tick=0)
        worker.tick()
        with connect(self.path) as con: row=con.execute("SELECT status,external_outcome FROM work_items").fetchone()
        self.assertEqual(tuple(row), ("dead","unknown"))

    def test_policy_validation_and_unpriced_coverage_are_explicit(self):
        for raw in ({"daily_requests":True},{"daily_tokens":0},{"dollars":25},[],"bad"):
            with self.subTest(raw=raw), self.assertRaises(ContractError): UsagePolicy.from_mapping(raw)
        report=usage_report(self.path,now=NOW)
        self.assertEqual(report["billing"],"unpriced")
        self.assertEqual(report["coverage"],"platform_managed_inference")

    def test_operator_reconciliation_is_audited_idempotent_and_preserves_daily_charge(self):
        with self.scope():
            inv=begin_invocation("provider","generation",b"request",reserved_tokens=100)
            inv.submitting(); inv.unknown()
        service=InvocationRecoveryService(self.path)
        item=service.list_invocations()[0]
        with self.assertRaises(ConflictError): service.reconcile(inv.invocation_id,expected_updated_at=item["updated_at"],command_id="review",resolution="absent",now=NOW)
        with connect(self.path) as con: con.execute("UPDATE work_items SET status='dead'")
        result=service.reconcile(inv.invocation_id,expected_updated_at=item["updated_at"],command_id="review",resolution="absent",now=NOW)
        self.assertEqual(result,service.reconcile(inv.invocation_id,expected_updated_at=item["updated_at"],command_id="review",resolution="absent",now=NOW+timedelta(days=1)))
        report=usage_report(self.path,now=NOW)
        self.assertEqual(report["inflight"],0)
        self.assertEqual(report["reserved_requests"],1)
        with connect(self.path) as con:
            with self.assertRaises(sqlite3.IntegrityError): con.execute("DELETE FROM inference_recovery_commands")

    def test_unknown_completed_job_requires_id_and_invalidates_old_worker(self):
        with self.scope():
            inv=begin_invocation("provider","generation",b"request",reserved_tokens=100,retrieval_kind="runpod_job")
            inv.submitting(); inv.unknown()
        with connect(self.path) as con: con.execute("UPDATE work_items SET status='dead'")
        service=InvocationRecoveryService(self.path)
        stamp=service.list_invocations()[0]["updated_at"]
        with self.assertRaises(ContractError): service.reconcile(inv.invocation_id,expected_updated_at=stamp,command_id="review",resolution="completed",now=NOW)
        service.reconcile(inv.invocation_id,expected_updated_at=stamp,command_id="review",resolution="completed",provider_job_id="job-confirmed",now=NOW)
        with self.assertRaises(ConflictError): inv.terminal("completed")
        self.assertTrue(work_can_resume(self.path,"work-a"))

    def test_hermes_and_stale_review_cannot_reconcile(self):
        with self.scope():
            inv=begin_invocation("provider","generation",b"request",reserved_tokens=100)
            inv.submitting(); inv.unknown()
        with connect(self.path) as con: con.execute("UPDATE work_items SET status='dead'")
        service=InvocationRecoveryService(self.path)
        with self.assertRaises(ContractError): service.reconcile(inv.invocation_id,expected_updated_at=utc_stamp(NOW),command_id="review",resolution="absent",actor_kind="hermes",now=NOW)
        with self.assertRaises(ConflictError): service.reconcile(inv.invocation_id,expected_updated_at=utc_stamp(NOW-timedelta(seconds=1)),command_id="review",resolution="absent",now=NOW)

    def test_old_reservation_rebooks_today_before_post_and_blocks_extra_request(self):
        with self.scope(policy=UsagePolicy(daily_requests=1)):
            begin_invocation("provider", "generation", b"old", reserved_tokens=100)
        tomorrow = NOW + timedelta(days=1)
        self.revision()
        with self.scope(revision=2, policy=UsagePolicy(daily_requests=1), now=tomorrow):
            old = begin_invocation("provider", "generation", b"old", reserved_tokens=100)
            old.submitting(); old.accepted("old-job"); old.terminal("completed")
        self.work("work-b")
        with self.scope("work-b", policy=UsagePolicy(daily_requests=1), now=tomorrow), self.assertRaises(UsageDeferred):
            begin_invocation("provider", "generation", b"fresh", reserved_tokens=100)
        self.assertEqual(usage_report(self.path, now=tomorrow)["reserved_requests"], 1)

    def test_old_reservation_cannot_post_if_today_is_already_full(self):
        with self.scope(policy=UsagePolicy(daily_tokens=100)):
            begin_invocation("provider", "generation", b"old", reserved_tokens=100)
        self.work("work-b")
        tomorrow = NOW + timedelta(days=1)
        with self.scope("work-b", policy=UsagePolicy(daily_tokens=100), now=tomorrow):
            fresh=begin_invocation("provider", "generation", b"fresh", reserved_tokens=100)
            fresh.submitting(); fresh.accepted("fresh-job"); fresh.terminal("completed")
        self.revision()
        with self.scope(revision=2, policy=UsagePolicy(daily_tokens=100), now=tomorrow):
            old=begin_invocation("provider", "generation", b"old", reserved_tokens=100)
            with self.assertRaises(UsageDeferred): old.submitting()
        self.assertEqual(InvocationRecoveryService(self.path).list_invocations()[0]["state"], "reserved")

    def test_expired_worker_is_fenced_by_operator_reconciliation(self):
        with self.scope():
            invocation=begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            invocation.submitting(); invocation.unknown()
        service=InvocationRecoveryService(self.path)
        item=service.list_invocations()[0]
        service.reconcile(invocation.invocation_id, expected_updated_at=item["updated_at"], command_id="review-expired", resolution="absent", now=NOW+timedelta(minutes=5))
        with connect(self.path) as con:
            work=con.execute("SELECT status,recovery_revision,lease_expires_at FROM work_items").fetchone()
        self.assertEqual(tuple(work), ("dead",2,None))
        with self.scope(), self.assertRaises(ConflictError): heartbeat_scope()

    def test_synchronous_reconciliation_cannot_invent_retrievable_job(self):
        with self.scope():
            invocation=begin_invocation("provider", "generation", b"request", reserved_tokens=100)
            invocation.submitting(); invocation.unknown()
        with connect(self.path) as con: con.execute("UPDATE work_items SET status='dead'")
        with self.assertRaises(ContractError):
            InvocationRecoveryService(self.path).reconcile(invocation.invocation_id, expected_updated_at=utc_stamp(NOW), command_id="sync-review", resolution="completed", provider_job_id="invented-job", now=NOW)
        self.assertEqual(usage_report(self.path,now=NOW)["uncertain"],1)

    def test_permanent_saved_id_failure_becomes_visible_reconciliation(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                if completed:
                    self.work("completed-work")
                    identity="completed-work"
                    provider,_=self.provider([self.completed()])
                    with self.scope(identity): self.generate(provider)
                    self.revision(identity=identity)
                else:
                    identity="work-a"
                    self.accepted_then_crash(); self.revision()
                provider,transport=self.provider([InferenceTransportError("missing",retryable=False,status_code=404)])
                with self.scope(identity,revision=2), self.assertRaises(InvocationReconciliationRequired): self.generate(provider)
                self.assertEqual([call["method"] for call in transport.calls],["GET"])
                rows=InvocationRecoveryService(self.path).list_invocations()
                row=next(row for row in rows if row["work_id"]==identity)
                self.assertEqual(row["state"],"unknown")
                self.assertEqual(row["reconciliation_reason"],"inference_result_unavailable")

    def test_transient_saved_id_failure_stays_pollable(self):
        self.accepted_then_crash(); self.revision()
        provider,transport=self.provider([InferenceTransportError("temporary",retryable=True,status_code=503)])
        with self.scope(revision=2), self.assertRaises(InvocationPending): self.generate(provider)
        self.assertEqual(InvocationRecoveryService(self.path).list_invocations()[0]["state"],"accepted")
        self.assertEqual([call["method"] for call in transport.calls],["GET"])

    def test_resume_optional_fallback_rethrows_budget_and_reconciliation(self):
        from job_search.resume_lab.gateway import _raise_if_runpod_reconciliation
        for exc in (UsageDeferred("inference_daily_request_limit",utc_stamp(NOW)),InvocationPending(),InvocationReconciliationRequired()):
            with self.subTest(exc=type(exc).__name__), self.assertRaises(type(exc)):
                _raise_if_runpod_reconciliation(exc)

    def test_worker_lease_recovery_polls_saved_id_without_second_post(self):
        self.accepted_then_crash()
        with connect(self.path) as con:
            con.execute("UPDATE work_items SET lease_expires_at=?,external_outcome='in_flight'",(utc_stamp(NOW-timedelta(seconds=1)),))
        provider, transport=self.provider([self.completed()])
        worker=Worker(self.path,task_handlers={"opportunity.preference_refresh":lambda _p,_c:{"text":self.generate(provider).text}},now_provider=lambda:NOW,max_work_per_tick=1,max_outbox_per_tick=0)
        worker.tick()
        with connect(self.path) as con: status=con.execute("SELECT status FROM work_items").fetchone()[0]
        self.assertEqual(status,"succeeded")
        self.assertEqual([call["method"] for call in transport.calls],["GET"])

    def test_worker_lease_recovery_never_reposts_unknown(self):
        with self.scope():
            inv=begin_invocation("provider","generation",b"request",reserved_tokens=100)
            inv.submitting()
        with connect(self.path) as con: con.execute("UPDATE work_items SET lease_expires_at=?",(utc_stamp(NOW-timedelta(seconds=1)),))
        called=[]
        worker=Worker(self.path,task_handlers={"opportunity.preference_refresh":lambda _p,_c:called.append(True)},now_provider=lambda:NOW,max_work_per_tick=1,max_outbox_per_tick=0)
        worker.tick()
        with connect(self.path) as con: status=con.execute("SELECT status FROM work_items").fetchone()[0]
        self.assertEqual(status,"dead")
        self.assertEqual(called,[])

    def test_child_command_receives_scope_and_preserves_deferral(self):
        from job_search.pipeline import FixedCommand
        from job_search.worker import TaskContext
        calls=[]
        def runner(command, **kwargs):
            calls.append(kwargs["env"])
            return subprocess.CompletedProcess(command,76,"",json.dumps({"type":"inference_usage_deferred","reason":"inference_daily_request_limit","retry_at":"2026-09-02T00:00:00Z"}))
        command=FixedCommand(["python3","fixture.py"],project_root=self.root,environment_provider=lambda:{},runner=runner)
        context=TaskContext("work-a","opportunity.preference_refresh",1,utc_stamp(NOW),lambda:True)
        with self.scope(policy=UsagePolicy(daily_requests=1)), self.assertRaises(UsageDeferred) as caught:
            command.run(context)
        self.assertEqual(caught.exception.retry_at,"2026-09-02T00:00:00Z")
        self.assertEqual(calls[0]["JOB_SEARCH_INVOCATION_WORK"],"work-a")
        self.assertEqual(json.loads(calls[0]["JOB_SEARCH_INFERENCE_USAGE_LIMITS"])["daily_requests"],1)

    def test_proxy_teacher_deferral_keeps_pending_without_spending_attempt(self):
        from job_search.ranking.proxy import prepare_run, run_teacher
        from tests.test_preference_proxy import _make_source, _profile as proxy_profile, FakeTeacher
        class DeferredTeacher(FakeTeacher):
            def generate(self, _prompt):
                raise UsageDeferred("inference_daily_request_limit","2026-09-02T00:00:00Z")
        source=_make_source(str(self.root))
        proxy=self.root/"proxy.db"
        profile=proxy_profile(str(self.root))
        prepare_run(source,proxy,profile,10,2,7)
        with self.assertRaises(UsageDeferred): run_teacher(proxy,profile,"training",1,teacher=DeferredTeacher())
        with sqlite3.connect(proxy) as con:
            rows=con.execute("SELECT status,attempts FROM proxy_queue").fetchall()
        self.assertTrue(all(status=="pending" and attempts==0 for status,attempts in rows))

    def test_invalid_saved_id_status_requires_review_instead_of_endless_polling(self):
        self.accepted_then_crash(); self.revision()
        provider,transport=self.provider([{"id":"unexpected-job","status":"COMPLETED","output":[_completion()]}])
        with self.scope(revision=2),self.assertRaises(InvocationReconciliationRequired): self.generate(provider)
        self.assertEqual(InvocationRecoveryService(self.path).list_invocations()[0]["state"],"unknown")
        self.assertEqual(len(transport.calls),1)

    def test_ledger_and_limits_survive_sqlite_backup_restore(self):
        self.accepted_then_crash()
        restored=self.root/"restored.db"
        with sqlite3.connect(self.path) as source,sqlite3.connect(restored) as target: source.backup(target)
        self.path=restored
        self.revision()
        provider,transport=self.provider([self.completed()])
        with self.scope(revision=2): self.generate(provider)
        self.assertEqual([call["method"] for call in transport.calls],["GET"])
        self.assertEqual(usage_report(self.path,now=NOW)["reserved_requests"],1)

    def test_audited_resume_work_continues_by_same_id_without_extra_post(self):
        from job_search.recovery import RecoveryService
        self.accepted_then_crash()
        with connect(self.path) as con:
            con.execute("UPDATE work_items SET status='dead',task_kind='resume.optimize',external_outcome='unknown',failure_retryable=0,lease_token=NULL")
        self.assertFalse(RecoveryService(self.path).list_work()[0]["retry_allowed"])
        service=InvocationRecoveryService(self.path)
        item=service.list_invocations()[0]
        result=service.reconcile(item["invocation_id"],expected_updated_at=item["updated_at"],command_id="resume-review",resolution="completed",now=NOW)
        self.assertEqual(result["next_action"],"resume_same_work")
        work=RecoveryService(self.path).list_work()[0]
        self.assertTrue(work["retry_allowed"])
        RecoveryService(self.path).retry("work-a",expected_revision=work["revision"],command_id="resume-continue",now=NOW)
        provider,transport=self.provider([self.completed()])
        worker=Worker(self.path,task_handlers={"resume.optimize":lambda _p,_c:{"text":self.generate(provider).text}},now_provider=lambda:NOW,max_work_per_tick=1,max_outbox_per_tick=0)
        worker.tick()
        self.assertEqual([call["method"] for call in transport.calls],["GET"])
        with connect(self.path) as con: self.assertEqual(con.execute("SELECT status FROM work_items").fetchone()[0],"succeeded")

    def test_resume_absent_reconciliation_requires_existing_domain_retry(self):
        from job_search.recovery import RecoveryService
        from job_search.inference.usage import resume_restart_was_reconciled
        with self.scope():
            inv=begin_invocation("provider","resume_generation",b"request",reserved_tokens=100,retrieval_kind="runpod_job")
            inv.submitting(); inv.unknown()
        with connect(self.path) as con:
            con.execute("UPDATE work_items SET status='dead',task_kind='resume.optimize',payload_json=?,lease_token=NULL",(json.dumps({"run_id":"run-fixture"}),))
            self.assertFalse(resume_restart_was_reconciled(con,"run-fixture"))
        result=InvocationRecoveryService(self.path).reconcile(inv.invocation_id,expected_updated_at=utc_stamp(NOW),command_id="absent-review",resolution="absent",now=NOW)
        self.assertEqual(result["next_action"],"retry_resume_after_reconciliation")
        self.assertFalse(RecoveryService(self.path).list_work()[0]["retry_allowed"])
        with connect(self.path) as con:
            self.assertTrue(resume_restart_was_reconciled(con,"run-fixture"))
            self.assertFalse(resume_restart_was_reconciled(con,"wrong-run"))

    def test_running_resume_requires_audited_absence_before_existing_retry_can_create_successor(self):
        from tests.test_resume_lab_gateway import make_gateway, start_application, JOB
        from job_search.resume_lab.contracts import ResumeConflictError
        gateway=make_gateway(self.root)
        _ledger,application_id=start_application(self.root)
        gateway.import_standard("Platform",1,"Python Kubernetes production systems")
        prepared=gateway.prepare(JOB,application_id=application_id,idempotency_key="prepare-reviewed-resume")
        run_id=str(prepared["run_id"])
        gateway.service.start_run(run_id,owner_token="orphaned-owner")
        with connect(self.path) as con:
            work=con.execute("SELECT work_id FROM work_items WHERE task_kind='resume.optimize'").fetchone()[0]
            con.execute("UPDATE work_items SET status='running',recovery_revision=1 WHERE work_id=?",(work,))
        with self.scope(work):
            inv=begin_invocation("provider","resume_generation",b"request",reserved_tokens=100,retrieval_kind="runpod_job")
            inv.submitting(); inv.unknown()
        with connect(self.path) as con: con.execute("UPDATE work_items SET status='dead' WHERE work_id=?",(work,))
        with self.assertRaises(ResumeConflictError):
            gateway.retry_run(run_id,idempotency_key="too-early",reconciliation_acknowledged=True)
        InvocationRecoveryService(self.path).reconcile(inv.invocation_id,expected_updated_at=utc_stamp(NOW),command_id="review-resume-absent",resolution="absent",now=NOW)
        successor=gateway.retry_run(run_id,idempotency_key="reviewed-retry",reconciliation_acknowledged=True)
        self.assertNotEqual(successor["run_id"],run_id)
        self.assertEqual(successor["status"],"queued")
        self.assertEqual(gateway.service.get_run(run_id)["status"],"failed")


if __name__ == "__main__": unittest.main()
