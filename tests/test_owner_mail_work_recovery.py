"""Exact owner/operational reconciliation retains dead attempt history and ambiguity."""
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from job_search.application_mail import build_application_mail_handlers
from job_search.application_mail_recovery import reconcile_mail_work, reconcile_reference
from job_search.commands import DomainError
from job_search.db import connect, prepare_database
from job_search.recovery import RecoveryService, unresolved_work_count
from tests.test_job_search_automation import enqueue_work
from tests import test_owner_processing_recovery as processing_tests
from tests.test_owner_dashboard_review import Archive


class OwnerMailWorkRecoveryTest(unittest.TestCase):
    key = processing_tests.ProcessingRecoveryTest.key
    worker = processing_tests.ProcessingRecoveryTest.worker
    message = processing_tests.ProcessingRecoveryTest.message
    failure = processing_tests.ProcessingRecoveryTest.failure
    success = processing_tests.ProcessingRecoveryTest.success
    issue = processing_tests.ProcessingRecoveryTest.issue
    decide = processing_tests.ProcessingRecoveryTest.decide

    def setUp(self):
        processing_tests.ProcessingRecoveryTest.setUp(self)
        self.operational = self.root / "operational.db"
        prepare_database(self.operational, "2026-10-10T12:00:00Z")

    def work(self, kind):
        with self.runtime.executor.read() as con:
            items = self.runtime.executor.work_page(con)["items"]
        return next(item for item in items if item["kind"] == kind)

    @staticmethod
    def reference(work):
        return {"owner":work["owner"],"kind":work["kind"],"key":work["dedupe_key"],"work_id":work["id"]}

    def dead(self, identity, work, **updates):
        task = "applications.mail.project" if work["kind"] == "project_analysis" else "applications.mail.understand"
        enqueue_work(self.operational, identity, task, status="dead", attempts=1, workflow_id=updates.pop("workflow_id", ""))
        with connect(self.operational) as con:
            con.execute("UPDATE work_items SET payload_json=?,completed_at=?,last_error='kept error',failure_kind='permanent' WHERE work_id=?",
                (json.dumps(self.reference(work)), "2026-10-10T12:00:00Z", identity))
            con.execute("INSERT INTO job_runs(run_id,work_id,scheduled_for,started_at,completed_at,outcome,result_json,error) VALUES(?,?,?,?,?,'dead','{}','kept failure')",
                ("run-" + identity,identity,"2026-10-10T11:00:00Z","2026-10-10T11:00:00Z","2026-10-10T12:00:00Z"))
            for name, value in updates.items():
                assert name in {"external_outcome","lease_token","payload_json","task_kind","workflow_id"}
                con.execute("UPDATE work_items SET " + name + "=? WHERE work_id=?", (value, identity))

    def reconcile(self, *, account="allowed", after="", limit=100):
        with self.runtime.executor.read() as con:
            page = self.runtime.executor.work_page(con)
        return reconcile_mail_work(self.runtime, account, owner_work=page["items"], operational_db=self.operational, after=after, limit=limit)

    def rows(self):
        with connect(self.operational) as con:
            return {row["work_id"]:dict(row) for row in con.execute("SELECT * FROM work_items")}

    def scheduled_dispatch_failure(self, work, *, paged=False):
        from job_search.scheduler import materialize_due_schedules, utc_stamp
        from job_search.worker import FollowUpTask, PermanentTaskError, TaskResult, Worker
        now = datetime(2026,10,10,12,tzinfo=timezone.utc)
        with connect(self.operational) as con:
            con.execute("""INSERT INTO schedule_specs
                (schedule_key,task_kind,schedule_json,enabled,coalesce,next_due_at,updated_at)
                VALUES('applications.mail.dispatch','applications.mail.dispatch',?,1,1,?,?)""",
                (json.dumps({"kind":"interval","minutes":5}), utc_stamp(now), utc_stamp(now)))
        self.assertEqual(materialize_due_schedules(self.operational,now)["created"], 1)
        def dispatch(payload, context):
            if paged and not payload.get("after"):
                return TaskResult({}, (FollowUpTask("applications.mail.dispatch", {"after":"page-one"}),))
            return TaskResult({}, (
                FollowUpTask("applications.mail.understand",self.reference(work),lane="model",priority=10),
                FollowUpTask("applications.mail.understand",{"unrelated":"sibling"},lane="model")))
        core = Worker(self.operational,task_handlers={"applications.mail.dispatch":dispatch},
                      now_provider=lambda:now,max_work_per_tick=3,max_outbox_per_tick=0)
        self.assertEqual(core.tick(now=now)["work"]["succeeded"], 2 if paged else 1)
        def failed(payload, context):
            raise PermanentTaskError("fictional retained model failure")
        model = Worker(self.operational,task_handlers={"applications.mail.understand":failed},lane="model",
                       now_provider=lambda:now,max_work_per_tick=1,max_outbox_per_tick=0)
        self.assertEqual(model.tick(now=now)["work"]["dead"], 1)
        return next(row for row in self.rows().values() if row["status"] == "dead")

    def assert_dispatch_recovery(self, *, paged):
        self.failure()
        failed = self.scheduled_dispatch_failure(self.work("understand_message"),paged=paged)
        if not paged:
            with connect(self.operational) as con:
                con.execute("UPDATE work_items SET external_outcome='terminal' WHERE work_id=?",(failed["work_id"],))
            failed = self.rows()[failed["work_id"]]
        before = self.rows()
        with connect(self.operational) as con:
            runs = [tuple(row) for row in con.execute("SELECT * FROM job_runs ORDER BY run_id")]
        self.assertEqual(self.reconcile()["operational_work_resolved"], 0)
        self.decide("retry_processing")
        self.assertEqual(self.reconcile()["operational_work_resolved"], 1)
        after = self.rows()
        self.assertEqual(after[failed["work_id"]], {**failed,"status":"cancelled",
                                                     "recovery_revision":failed["recovery_revision"]+1})
        for key, row in before.items():
            if key != failed["work_id"]:
                self.assertEqual(after[key],row)
        with connect(self.operational) as con:
            self.assertEqual([tuple(row) for row in con.execute("SELECT * FROM job_runs ORDER BY run_id")],runs)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0],0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM workflow_watermarks").fetchone()[0],0)
            receipt = con.execute("SELECT * FROM work_owner_resolutions").fetchone()
            self.assertEqual(json.loads(receipt["before_json"]),failed)
            lineage = json.loads(receipt["proof_json"])["dispatch_lineage"]
            self.assertEqual(len(lineage),2 if paged else 1)
            self.assertEqual(lineage[0]["work_id"],failed["parent_work_id"])
            self.assertEqual(lineage[-1]["schedule_key"],"applications.mail.dispatch")
        self.assertEqual(self.reconcile()["operational_work_resolved"],0)

    def test_scheduled_mail_dispatch_label_is_not_a_managed_workflow(self):
        self.assert_dispatch_recovery(paged=False)

    def test_paged_dispatch_ancestry_preserves_parents_sibling_and_failure_history(self):
        self.assert_dispatch_recovery(paged=True)

    def test_managed_workflow_active_parent_and_unknown_effect_stay_blocked(self):
        self.failure()
        failed = self.scheduled_dispatch_failure(self.work("understand_message"))
        self.decide("resolve_processing")
        parent = failed["parent_work_id"]
        with connect(self.operational) as con:
            con.execute("""INSERT INTO workflow_runs
                (workflow_id,workflow_kind,root_work_id,trigger_task_kind,scheduled_for,status,started_at)
                VALUES(?,'opportunity_refresh',?,'ats.new_only','2026-10-10','failed','2026-10-10')""",
                (failed["workflow_id"],parent))
        self.assertEqual(self.reconcile()["operational_work_resolved"],0)
        with connect(self.operational) as con:
            self.assertEqual(con.execute("SELECT status FROM workflow_runs").fetchone()[0],"failed")
            con.execute("DELETE FROM workflow_runs")
        # An orphan historical watermark also prevents treating the ID as an
        # unmanaged dispatch label, even if the workflow row was lost.
        with sqlite3.connect(self.operational) as con:
            con.execute("INSERT INTO workflow_watermarks VALUES(?,'ats_ingested',?,'2026-10-10',?)",
                        (failed["workflow_id"],parent,"a"*64))
        self.assertEqual(self.reconcile()["operational_work_resolved"],0)
        with connect(self.operational) as con:
            con.execute("DELETE FROM workflow_watermarks")
            con.execute("UPDATE work_items SET status='running' WHERE work_id=?",(parent,))
        self.assertEqual(self.reconcile()["operational_work_resolved"],0)
        with connect(self.operational) as con:
            con.execute("UPDATE work_items SET status='succeeded' WHERE work_id=?",(parent,))
            con.execute("UPDATE work_items SET external_outcome='unknown' WHERE work_id=?",(failed["work_id"],))
        self.assertEqual(self.reconcile()["operational_work_resolved"],0)
        self.assertEqual(self.rows()[failed["work_id"]]["status"],"dead")

    def test_missing_cross_workflow_or_non_dispatch_ancestry_stays_blocked(self):
        self.failure()
        failed = self.scheduled_dispatch_failure(self.work("understand_message"))
        self.decide("resolve_processing")
        with connect(self.operational) as con:
            for name, updates in (("missing",{"parent_work_id":"missing"}),
                                  ("cross-workflow",{"workflow_id":"different-workflow"}),
                                  ("cycle",{"parent_work_id":"cycle"})):
                row = {**failed,"work_id":name,"dedupe_key":name,**updates}
                con.execute("INSERT INTO work_items ("+",".join(row)+") VALUES ("+",".join("?" for _ in row)+")",tuple(row.values()))
            con.execute("UPDATE work_items SET external_outcome='unknown' WHERE work_id=?",(failed["work_id"],))
        self.assertEqual(self.reconcile()["operational_work_resolved"],0)
        with connect(self.operational) as con:
            con.execute("UPDATE work_items SET external_outcome='none' WHERE work_id=?",(failed["work_id"],))
            con.execute("UPDATE work_items SET task_kind='outlook.mail.sync' WHERE work_id=?",(failed["parent_work_id"],))
        self.assertEqual(self.reconcile()["operational_work_resolved"],0)
        self.assertTrue(all(row["status"] == "dead" for row in self.rows().values()
                            if row["task_kind"] == "applications.mail.understand" and row["priority"] == 10))

    def test_unresolved_current_work_stays_then_manual_resolution_retires_exact_history(self):
        self.failure()
        source = self.work("understand_message")
        self.dead("old", source)
        self.assertEqual(self.reconcile()["operational_work_resolved"], 0)
        self.assertEqual(self.rows()["old"]["status"], "dead")
        self.decide("resolve_processing")
        result = self.reconcile()
        self.assertEqual(result["owner_work_completed"], 1)
        self.assertEqual(result["operational_work_resolved"], 1)
        row = self.rows()["old"]
        self.assertEqual((row["status"],row["attempts"],row["last_error"]), ("cancelled",1,"kept error"))
        with connect(self.operational) as con:
            self.assertEqual(unresolved_work_count(con), 0)
            self.assertEqual(tuple(con.execute("SELECT outcome,error FROM job_runs").fetchone()), ("dead","kept failure"))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM work_owner_resolutions").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError): con.execute("DELETE FROM work_owner_resolutions")
        self.assertEqual(self.reconcile()["operational_work_resolved"], 0)

    def test_newer_success_retires_old_projection_outbox_and_old_dead_worker(self):
        first = self.success(complete=False, relevance="uncertain")
        old_projection = self.work("project_analysis")
        self.dead("old-project", old_projection)
        newer = self.success(relevance="unrelated")
        self.runtime.project_message_analysis(newer["id"])
        result = self.reconcile()
        self.assertEqual(result["operational_work_resolved"], 1)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.executor.find_work(con,"understanding","project_analysis",first["id"])["status"], "done")
            self.assertEqual(len(self.runtime.understanding.coverage(con)["items"]), 2)
        self.assertEqual(self.rows()["old-project"]["status"], "cancelled")

    def test_explicit_retry_source_failure_retires_original_only(self):
        self.failure()
        source = self.work("understand_message")
        self.dead("source-failed", source)
        self.decide("retry_processing")
        retry = self.work("retry_processing")
        self.failure(retry=retry["payload"], code="source_unavailable")
        with self.runtime.executor.read() as con:
            latest = self.runtime.understanding.get_analysis(con,self.issue()["analysis_id"])
        self.assertEqual(latest["descriptor"]["context"]["processing_attempt"]["issue_id"], self.issue()["issue_id"])
        self.assertEqual(self.reconcile()["operational_work_resolved"], 1)
        self.assertEqual(self.issue()["status"], "open")
        self.assertEqual(self.issue()["failure_code"], "source_unavailable")
        self.assertEqual(self.rows()["source-failed"]["status"], "cancelled")

    def test_wrong_account_unknown_provider_active_lease_and_bad_lineage_stay_visible(self):
        self.failure()
        work = self.work("understand_message")
        self.decide("resolve_processing")
        self.dead("unknown", work, external_outcome="unknown")
        self.dead("leased", work, lease_token="still-owned")
        self.dead("mismatch", work, payload_json=json.dumps({**self.reference(work),"work_id":"other-owner-work"}))
        self.dead("different-kind", work, task_kind="resume.optimize")
        self.dead("workflow", work, workflow_id="preserve-workflow-owner")
        self.dead("valid", work)
        self.assertEqual(self.reconcile(account="other")["operational_work_resolved"], 0)
        self.assertEqual(self.reconcile()["operational_work_resolved"], 1)
        self.assertEqual({key for key,value in self.rows().items() if value["status"] == "dead"}, {"unknown","leased","mismatch","different-kind","workflow"})

    def test_nonterminal_inference_blocks_even_when_work_external_flag_says_none(self):
        # An invocation can remain unknown even if a historical work flag is stale.
        self.failure()
        work = self.work("understand_message")
        self.decide("resolve_processing")
        self.dead("inference", work)
        with connect(self.operational) as con:
            con.execute("""INSERT INTO inference_invocations
                (invocation_id,work_id,work_revision,request_sha256,provider_fingerprint,capability,state,reserved_tokens,budget_day,created_at,updated_at)
                VALUES('inv','inference',0,?,?,'structured_generation','unknown',0,'2026-10-10',?,?)""",
                ("a"*64,"b"*64,"2026-10-10T12:00:00Z","2026-10-10T12:00:00Z"))
        self.assertEqual(self.reconcile()["operational_work_resolved"], 0)
        with connect(self.operational) as con:
            con.execute("UPDATE inference_invocations SET state='cancelled' WHERE invocation_id='inv'")
        self.assertEqual(self.reconcile()["operational_work_resolved"], 1)

    def test_operational_commit_then_owner_crash_is_idempotently_repairable(self):
        self.failure()
        work = self.work("understand_message")
        self.decide("resolve_processing")
        self.dead("crash", work)
        service = RecoveryService(self.operational)
        item = service.owner_mail_candidates()["items"][0]
        from job_search.commands.transactions import Transaction
        with patch.object(Transaction,"record",side_effect=RuntimeError("simulated process failure")):
            with self.assertRaises(RuntimeError):
                reconcile_reference(self.runtime,self.reference(work),"allowed",operational=service,item=item)
        self.assertEqual(self.rows()["crash"]["status"], "cancelled")
        result = reconcile_reference(self.runtime,self.reference(work),"allowed",operational=service,item=item)
        self.assertTrue(result["reconciled"])
        with connect(self.operational) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM work_owner_resolutions").fetchone()[0], 1)

    def test_recovery_pages_do_not_stop_at_current_unresolved_dead_work(self):
        self.failure()
        work = self.work("understand_message")
        self.decide("resolve_processing")
        self.dead("a-blocked", work, external_outcome="unknown")
        self.dead("b-resolved", work)
        first = self.reconcile(limit=1)
        self.assertEqual(first["next_cursor"], "a-blocked")
        self.assertEqual(first["operational_work_resolved"], 0)
        self.assertEqual(self.reconcile(after=first["next_cursor"],limit=1)["operational_work_resolved"], 1)

    def test_dispatch_reconciles_only_obsolete_work_and_does_not_reenqueue_it(self):
        from types import SimpleNamespace
        self.failure()
        work = self.work("understand_message")
        self.dead("dispatcher", work)
        self.decide("resolve_processing")
        handlers = build_application_mail_handlers(self.runtime, Archive(), "allowed", None, "unconfigured",
                                                  operational_db=self.operational)
        result = handlers["applications.mail.dispatch"]({}, SimpleNamespace(heartbeat=lambda:True))
        self.assertEqual(result.result["recovery"]["operational_work_resolved"], 1)
        self.assertEqual(result.follow_ups, ())
        self.assertEqual(self.rows()["dispatcher"]["status"], "cancelled")

    def test_operational_schema_install_is_additive_atomic_and_idempotent(self):
        from job_search.recovery import install_owner_resolution_schema
        with connect(self.operational) as con:
            before = [tuple(row) for row in con.execute("SELECT * FROM schema_migrations ORDER BY version")]
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 24)
            con.execute("DROP TRIGGER work_owner_resolutions_no_update")
            con.execute("DROP TRIGGER work_owner_resolutions_no_delete")
            con.execute("DROP TABLE work_owner_resolutions")
        def interrupted(con):
            install_owner_resolution_schema(con)
            raise RuntimeError("interrupted additive install")
        with patch("job_search.recovery.install_owner_resolution_schema", side_effect=interrupted):
            with self.assertRaises(RuntimeError): prepare_database(self.operational, "2026-10-10T12:00:00Z")
        with connect(self.operational) as con:
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='work_owner_resolutions'").fetchone())
            self.assertEqual([tuple(row) for row in con.execute("SELECT * FROM schema_migrations ORDER BY version")], before)
        prepare_database(self.operational, "2026-10-10T12:00:00Z")
        self.failure()
        work = self.work("understand_message")
        self.dead("audit-retained", work)
        self.decide("resolve_processing")
        self.reconcile()
        with connect(self.operational) as con:
            receipt = tuple(con.execute("SELECT * FROM work_owner_resolutions").fetchone())
        prepare_database(self.operational, "2026-10-10T12:00:00Z")
        prepare_database(self.operational, "2026-10-10T12:00:00Z")
        with connect(self.operational) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 24)
            self.assertEqual([tuple(row) for row in con.execute("SELECT * FROM schema_migrations ORDER BY version")], before)
            self.assertEqual(tuple(con.execute("SELECT * FROM work_owner_resolutions").fetchone()), receipt)
            self.assertEqual(con.execute("SELECT outcome,error FROM job_runs").fetchone()["error"], "kept failure")


if __name__ == "__main__": unittest.main()
