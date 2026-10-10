"""Production activation, configured identity, dispatch and notification recovery."""
from datetime import datetime, timedelta
from contextlib import closing
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
import uuid

from job_search.application_runtime import ApplicationRuntime
from job_search.commands import CommandContext, Principal
from job_search.commands import DomainError, CommandExecutor
from job_search.application_execution import ProductionExecution, ConfiguredOutlookProvider
from job_search.external_actions.api import ProviderOutcome, SCHEMA, SCHEMA_MIGRATIONS, PreEffectTransientError
from job_search.external_actions.worker import ExternalActionWorker
from job_search.hermes_delivery import HermesDeliveryBridgeError, delivery_fingerprint
from job_search.outlook.transport import GraphHttpError, RetryDecision
from tests import test_redesign_external_actions as action_fixtures


class ExecutionQueriesTest(unittest.TestCase):
    def test_exact_job_lookup_and_stale_notification_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            now = ["2026-10-09T12:00:00Z"]
            runtime = ApplicationRuntime(Path(directory) / "owners.db", clock=lambda: now[0])
            def human(operation, payload):
                return runtime.command(CommandContext(Principal("human", "human", {"*"}), uuid.uuid4().hex), operation, payload)
            app = human("save_job", {"job_source": {"source": "test", "source_id": "one"}})
            with runtime.executor.read() as con:
                self.assertEqual(runtime.applications.find_application_by_job(con, "test", "one")["id"], app["id"])
                self.assertIsNone(runtime.applications.find_application_by_job(con, "test", "missing"))
            reminder = human("create_reminder", {"application_id": app["id"], "at": "2026-10-09T12:01:00Z", "description": "Follow up"})
            now[0] = "2026-10-09T12:02:00Z"
            result = runtime.process_scheduled()
            item = result["notifications"]["items"][0]
            with runtime.executor.read() as con:
                self.assertTrue(runtime.applications.notification_delivery_applicable(con, item["delivery_id"]))
                self.assertEqual(runtime.applications.get_notification_handoff(con, item["delivery_id"])["reminder_version"], reminder["version"])
            human("cancel_reminder", {"reminder_id": reminder["id"], "expected_version": reminder["version"], "reason": "No longer needed"})
            with runtime.executor.read() as con:
                self.assertFalse(runtime.applications.notification_delivery_applicable(con, item["delivery_id"]))


class ActivatedActionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = action_fixtures.ExternalActionsTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.worker = ExternalActionWorker(f.executor, f.ops, f.provider, f.worker_context,
            lambda tx, envelope: True, activation_status=f.executor.activation_status,
            activation_guard=f.executor.activation_guard, lease_seconds=180)

    def activate(self, paused=False):
        e = self.fixture.executor
        return e.set_activation(paused=paused, expected_revision=e.activation_status()["revision"],
                                operator="operator", reason="Fictional readiness verification")

    def test_startup_paused_then_revision_is_persisted(self):
        f = self.fixture
        action = f.approve(f.prepare())
        self.assertEqual(self.worker.execute(action["action_id"])["status"], "paused")
        self.assertEqual((f.provider.writes, f.provider.reads), (0, 0))
        self.activate()
        self.assertEqual(self.worker.execute(action["action_id"])["execution"], "awaiting_confirmation")
        with f.executor.read() as con:
            row = con.execute("SELECT activation_revision FROM action_activation_fences").fetchone()
        self.assertEqual(row[0], 1)

    def test_pause_or_pause_resume_during_preflight_fences_every_write(self):
        f = self.fixture
        action = f.approve(f.prepare())
        self.activate()
        def preflight(_):
            self.activate(True)
            self.activate(False)
        f.provider.preflight = preflight
        result = self.worker.execute(action["action_id"])
        self.assertTrue(result["dispatch_paused"])
        self.assertEqual(f.provider.writes, 0)
        self.assertEqual(result["execution"], "queued")
        self.assertEqual(result["authorization"], "approved")

    def test_multistep_effect_renews_between_reads_and_writes(self):
        f = self.fixture
        action = f.approve(f.prepare(consequence=False))
        self.activate()
        stages = iter([ProviderOutcome("progress", {"stage": "created"}),
                       ProviderOutcome("progress", {"stage": "verified"}),
                       ProviderOutcome("succeeded", {"stage": "sent"}, {"verified": "sent"})])
        def preflight(_):
            f.now += timedelta(seconds=90)
        def perform(_):
            f.provider.writes += 1
            f.now += timedelta(seconds=90)
            return next(stages)
        f.provider.preflight, f.provider.perform = preflight, perform
        self.assertEqual(self.worker.execute(action["action_id"])["execution"], "succeeded")
        self.assertEqual(f.provider.writes, 3)

    def test_late_effect_is_uncertain_and_cannot_be_replayed(self):
        f = self.fixture
        action = f.approve(f.prepare())
        self.activate()
        def perform(_):
            f.provider.writes += 1
            f.now += timedelta(seconds=181)
            return ProviderOutcome("accepted", {"remote_id": "known"})
        f.provider.perform = perform
        self.assertEqual(self.worker.execute(action["action_id"])["execution"], "uncertain")
        with self.assertRaises(DomainError):
            self.worker.execute(action["action_id"])
        self.assertEqual(f.provider.writes, 1)

    def test_lost_core_worker_lease_prevents_provider_write(self):
        f = self.fixture
        action = f.approve(f.prepare())
        self.activate()
        self.worker.heartbeat = lambda: False
        result = self.worker.execute(action["action_id"])
        self.assertEqual(result["execution"], "queued")
        self.assertEqual(f.provider.writes, 0)

    def composition(self):
        f = self.fixture
        runtime = SimpleNamespace(executor=f.executor, actions=f.ops, result_context=f.worker_context,
                                  workflows=SimpleNamespace(applicability=lambda tx, envelope: True))
        return ProductionExecution(runtime, lambda account: f.provider,
                                   f.executor.activation_status, f.executor.activation_guard)

    def test_durable_bridge_ack_distinguishes_new_retry_from_future_work(self):
        f = self.fixture
        action = f.approve(f.prepare())
        self.activate()
        execution = self.composition()
        f.provider.preflight_error = PreEffectTransientError()
        self.assertTrue(execution.execute_action({"action_id": action["action_id"]})["work_complete"])
        self.assertFalse(execution.execute_action({"action_id": action["action_id"]})["work_complete"])
        f.now += timedelta(seconds=31)
        f.provider.preflight_error = None
        self.assertTrue(execution.execute_action({"action_id": action["action_id"]})["work_complete"])
        self.assertTrue(execution.execute_action({"action_id": action["action_id"]})["work_complete"])
        self.assertEqual(f.provider.writes, 1)
        result = execution.dispatch()
        self.assertTrue(any(task.task_kind == "application.reconcile_action" for task in result.follow_ups))

    def test_dispatch_pagination_advances_every_stream_without_cycling(self):
        f = self.fixture
        self.activate()
        def enqueue(tx):
            with tx.scope("external_actions"):
                for number in range(205):
                    tx.enqueue("external_actions", "execute_action", "fixture:" + str(number), {"action_id": "fixture:" + str(number)})
            return {"created": True}
        f.work_run("execute_action", enqueue)
        execution = self.composition()
        payload, identifiers, rounds = {}, [], 0
        while payload is not None:
            result = execution.dispatch(payload)
            continuations = [item for item in result.follow_ups if item.task_kind == "application.dispatch"]
            identifiers.extend(item.payload["owner_work_id"] for item in result.follow_ups if item.task_kind == "application.execute_action")
            payload = continuations[0].payload if continuations else None
            rounds += 1
            self.assertLessEqual(rounds, 3)
        self.assertEqual((len(identifiers), len(set(identifiers))), (205, 205))


class ExternalSchemaMigrationTest(unittest.TestCase):
    def test_only_exact_prior_schema_gets_activation_fence_upgrade(self):
        old = SCHEMA[:SCHEMA.index("CREATE TABLE IF NOT EXISTS action_activation_fences")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            CommandExecutor(path, {"external_actions": old})
            with self.assertRaises(DomainError):
                CommandExecutor(path, {"external_actions": SCHEMA})
            executor = CommandExecutor(path, {"external_actions": SCHEMA}, schema_migrations=SCHEMA_MIGRATIONS)
            with executor.read() as con:
                self.assertEqual(con.execute("SELECT count(*) FROM action_activation_fences").fetchone()[0], 0)


class ConfiguredProviderTest(unittest.TestCase):
    def test_logical_label_cannot_relabel_another_microsoft_account(self):
        selected = ["wrong-home"]
        reads = []
        client = SimpleNamespace(read_message_body=lambda mid: reads.append(mid) or {"id": mid})
        bound = ConfiguredOutlookProvider("logical", "approved-home", lambda: selected[0], client,
                                         sent_folder_id=lambda: reads.append("sentitems") or "sent")
        self.assertEqual(reads, [])
        with self.assertRaises(DomainError):
            bound.read_message_body("message")
        self.assertEqual(reads, [])
        selected[0] = "approved-home"
        self.assertEqual(bound.read_message_body("message"), {"id": "message"})
        with self.assertRaises(DomainError):
            bound.preflight({"envelope": {"kind": "send_reply", "account_id": "other"}})
        self.assertEqual(reads, ["message"])

    def test_transient_mapping_is_only_in_readonly_preflight(self):
        failure = GraphHttpError(503, "unavailable", RetryDecision(True, None, False, "retry"))
        def broken(_):
            raise failure
        bound = ConfiguredOutlookProvider("logical", "home", lambda: "home", object(), sent_folder_id="sent")
        bound._reply.preflight = broken
        bound._reply.perform = broken
        action = {"envelope": {"kind": "send_reply", "account_id": "logical"}}
        with self.assertRaises(PreEffectTransientError):
            bound.preflight(action)
        with self.assertRaises(GraphHttpError):
            bound.perform(action)


class FakeNotificationClient:
    def __init__(self):
        self.receipts = {}
        self.sends = 0
        self.uncertain = False

    def status(self, identifier):
        return self.receipts.get(identifier, {"delivery_id": identifier, "state": "not_found"})

    def send(self, identifier, title, body):
        self.sends += 1
        self.receipts[identifier] = {"delivery_id": identifier,
            "state": "reconciliation_required" if self.uncertain else "delivered",
            "payload_sha256": delivery_fingerprint("telegram", title, body), "updated_at": "2026-10-09 12:02:00"}
        if self.uncertain:
            raise HermesDeliveryBridgeError("Unknown send", retryable=False, code="delivery_reconciliation_required")


class NotificationExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = "2026-10-09T12:00:00Z"
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / "owner.db", clock=lambda: self.now)
        self.client = FakeNotificationClient()
        self.execution = ProductionExecution(self.runtime, lambda _: None,
            self.runtime.executor.activation_status, self.runtime.executor.activation_guard,
            notification_client=self.client, notification_target="telegram")
        app = self.human("save_job", {"job_source": {"source": "test", "source_id": "one"}})
        self.reminder = self.human("create_reminder", {"application_id": app["id"],
            "at": "2026-10-09T12:01:00Z", "description": "Follow up"})
        self.now = "2026-10-09T12:02:00Z"
        self.handoff = self.runtime.process_scheduled()["notifications"]["items"][0]

    def human(self, operation, payload):
        return self.runtime.command(CommandContext(Principal("human", "human", {"*"}), uuid.uuid4().hex), operation, payload)

    def activate(self):
        self.runtime.executor.set_activation(paused=False, expected_revision=0, operator="operator", reason="fixture")

    def test_delivery_receipt_recovers_after_local_crash_without_resend(self):
        self.assertEqual(self.execution.deliver_owner_notification(self.handoff)["status"], "paused")
        self.activate()
        original = self.runtime.command
        self.runtime.command = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash before local receipt"))
        with self.assertRaises(RuntimeError):
            self.execution.deliver_owner_notification(self.handoff)
        self.runtime.command = original
        result = self.execution.deliver_owner_notification(self.handoff)
        self.assertEqual(result["status"], "delivered")
        self.now = "2026-10-09T12:05:00Z"
        self.assertEqual(self.execution.deliver_owner_notification(self.handoff), result)
        self.assertEqual(self.client.sends, 1)
        self.assertEqual(self.execution.dispatch().result["advisory_timers_completed"], 1)

    def test_uncertain_notification_requires_reconciliation_and_no_repeat(self):
        self.activate()
        self.client.uncertain = True
        self.assertFalse(self.execution.deliver_owner_notification(self.handoff)["work_complete"])
        self.assertEqual(self.execution.deliver_owner_notification(self.handoff)["status"], "needs_reconciliation")
        self.assertEqual(self.client.sends, 1)

    def test_stale_handoff_is_suppressed_and_fake_receipt_is_rejected(self):
        self.activate()
        self.human("cancel_reminder", {"reminder_id": self.reminder["id"],
            "expected_version": self.reminder["version"], "reason": "cancel"})
        result = self.execution.deliver_owner_notification(self.handoff)
        self.assertEqual(result, {"status": "suppressed", "work_complete": True})
        self.assertEqual(self.client.sends, 0)
        self.client.receipts[self.handoff["delivery_id"]] = {"state": "delivered", "payload_sha256": "wrong"}
        self.assertEqual(self.execution.deliver_owner_notification(self.handoff)["status"], "receipt_conflict")

    def test_dispatch_only_materializes_bounded_core_work(self):
        self.assertEqual(self.execution.dispatch().follow_ups, ())
        self.activate()
        result = self.execution.dispatch()
        self.assertEqual(len(result.follow_ups), 1)
        work = result.follow_ups[0]
        self.assertEqual(work.task_kind, "application.deliver_owner_notification")
        self.assertIn("owner_work_id", work.payload)
        self.assertEqual(self.client.sends, 0)

    def core_worker(self, *, max_work_per_tick=20):
        from job_search.db import connect, prepare_database
        from job_search.worker import Worker, RetryableTaskError
        operational = Path(self.tmp.name) / "operational.db"
        prepare_database(operational, self.now)
        with closing(connect(operational)) as con:
            con.execute("""INSERT INTO schedule_specs
                (schedule_key,task_kind,schedule_json,enabled,coalesce,next_due_at,updated_at)
                VALUES('owner-dispatch','application.dispatch',?,1,1,?,?)""",
                ('{"kind":"interval","minutes":1}', self.now, self.now))
            con.commit()

        def deliver(payload, context):
            result = self.execution.deliver_owner_notification(payload, context)
            if not result["work_complete"]:
                raise RetryableTaskError("Owner notification awaits recovery", retry_after_seconds=60)
            self.runtime.executor.complete_work(payload["owner_work_id"],
                owner="applications", kind="deliver_owner_notification")
            return result

        worker = Worker(operational,
            task_handlers={"application.dispatch": self.execution.dispatch,
                           "application.deliver_owner_notification": deliver},
            now_provider=lambda: datetime.fromisoformat(self.now.replace("Z", "+00:00")),
            max_work_per_tick=max_work_per_tick)
        return worker, operational

    def notification_batches(self, operational):
        from job_search.db import connect
        with closing(connect(operational)) as con:
            return [dict(row) for row in con.execute("""SELECT status,attempts,dedupe_key,last_error
                FROM work_items WHERE task_kind='application.deliver_owner_notification'
                ORDER BY created_at,work_id""")]

    def test_hourly_recovery_observes_late_sidecar_receipt_after_core_retries_exhaust(self):
        self.activate()
        self.client.uncertain = True
        worker, operational = self.core_worker()
        for minute in (2, 3, 5, 9, 17):
            self.now = "2026-10-09T12:%02d:00Z" % minute
            worker.tick()
        batches = self.notification_batches(operational)
        self.assertEqual([(item["status"], item["attempts"]) for item in batches], [("dead", 5)])
        self.assertTrue(batches[0]["last_error"])
        self.assertEqual(self.client.sends, 1)
        with self.runtime.executor.read() as con:
            pending = self.runtime.executor.pending_work(con, owner="applications", kind="deliver_owner_notification")
        self.assertEqual(len(pending), 1)
        # Later reconciliation resolves the sidecar receipt. A new operational
        # batch reads that receipt and acknowledges owner work without sending.
        self.client.receipts[self.handoff["delivery_id"]]["state"] = "delivered"
        self.now = "2026-10-09T13:02:00Z"
        worker.tick()
        batches = self.notification_batches(operational)
        self.assertEqual([(item["status"], item["attempts"]) for item in batches], [("dead", 5), ("succeeded", 1)])
        self.assertEqual(self.client.sends, 1)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.executor.pending_work(con, owner="applications", kind="deliver_owner_notification"), [])

    def test_reactivation_recovers_paused_exhausted_batch_in_same_hour(self):
        self.activate()
        worker, operational = self.core_worker(max_work_per_tick=1)
        worker.tick()  # Dispatch is durable; pause before its notification runs.
        self.runtime.executor.set_activation(paused=True, expected_revision=1,
            operator="operator", reason="Pause queued work")
        worker.max_work_per_tick = 20
        for minute in (3, 4, 6, 10, 18):
            self.now = "2026-10-09T12:%02d:00Z" % minute
            worker.tick()
        self.assertEqual([(item["status"], item["attempts"]) for item in self.notification_batches(operational)], [("dead", 5)])
        self.assertEqual(self.client.sends, 0)
        self.runtime.executor.set_activation(paused=False, expected_revision=2,
            operator="operator", reason="Resume queued work")
        self.now = "2026-10-09T12:19:00Z"
        worker.tick()
        batches = self.notification_batches(operational)
        self.assertEqual([(item["status"], item["attempts"]) for item in batches], [("dead", 5), ("succeeded", 1)])
        self.assertNotEqual(batches[0]["dedupe_key"], batches[1]["dedupe_key"])
        self.assertEqual(self.client.sends, 1)


if __name__ == "__main__":
    unittest.main()
