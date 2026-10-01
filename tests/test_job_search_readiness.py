"""Offline domain readiness and operator recovery, using real SQLite state."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search import db
from job_search.contracts import ConflictError, ContractError
from job_search.readiness import readiness_report
from job_search.recovery import RecoveryService
from job_search.scheduler import seed_default_schedules, utc_stamp
from job_search.worker import Worker, RetryableTaskError, PermanentTaskError


NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
ENV = {"JOB_SCRAPER_CONTACT": "test@katauskas.dev",
       "OUTLOOK_CLIENT_ID": "12345678-1234-1234-1234-123456789abc",
       "JOB_SEARCH_NOTIFICATION_TARGET": "fixture-target"}


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "application.db"
        db.prepare_database(self.path, utc_stamp(NOW))
        seed_default_schedules(self.path, NOW, ENV)

    def work(self, identity="work-a", kind="system.worker_tick", *, at=NOW,
             status="queued", schedule=None, retryable=None, outcome="none", max_attempts=1):
        stamp = utc_stamp(at)
        with db.connect(self.path) as con:
            con.execute(
                "INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,priority,due_at,"
                "attempts,max_attempts,created_at,started_at,completed_at,schedule_key,"
                "failure_kind,failure_retryable,external_outcome) VALUES (?,?,?,'{}',?,0,?,0,?,?,?,?,?,?,?,?)",
                (identity, kind, identity, status, stamp, max_attempts, stamp,
                 stamp if status != "queued" else None,
                 stamp if status in {"dead", "succeeded"} else None, schedule,
                 "retryable" if retryable else "legacy_unknown", retryable, outcome),
            )
            if status != "queued":
                con.execute(
                    "INSERT INTO job_runs(run_id,work_id,scheduled_for,started_at,completed_at,outcome) VALUES (?,?,?,?,?,?)",
                    ("run-"+identity, identity, stamp, stamp, stamp if status != "running" else None,
                     "succeeded" if status == "succeeded" else "dead" if status == "dead" else None),
                )

    def report(self, *, at=NOW, paused=False, dependencies=None):
        result = readiness_report(self.path, now=at, automation_enabled=not paused, dependencies=dependencies)
        self.assertNotEqual(result["capabilities"][0]["reason_code"], "state_unavailable", result)
        return result, {item["id"]: item for item in result["capabilities"]}

    def test_missing_database_read_does_not_create_or_initialize(self):
        missing = self.path.parent / "missing" / "state.db"
        result = readiness_report(missing, now=NOW)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(sqlite3.OperationalError):
            RecoveryService(missing).list_work()
        self.assertFalse(missing.parent.exists())

    def test_status_has_stable_shape_and_never_uses_network(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result, caps = self.report()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(caps["automation"]["status"], "configured_unverified")
        self.assertEqual(caps["outlook"]["reason_code"], "awaiting_first_scheduled_run")
        for cap in caps.values():
            self.assertEqual(set(cap), {"id", "status", "configured", "enabled", "last_attempt_at", "last_success_at", "reason_code", "next_action"})
        self.assertNotIn(str(self.path), json.dumps(result))

    def test_initial_grace_then_missed_schedule_is_stale(self):
        _, first = self.report(at=NOW + timedelta(minutes=19))
        self.assertEqual(first["automation"]["status"], "configured_unverified")
        _, later = self.report(at=NOW + timedelta(minutes=21))
        self.assertEqual(later["automation"]["status"], "stale")

    def test_restarting_does_not_renew_startup_grace(self):
        seed_default_schedules(self.path, NOW + timedelta(hours=1), ENV)
        _, caps = self.report(at=NOW + timedelta(hours=1))
        self.assertEqual(caps["automation"]["status"], "stale")

    def test_reenabling_sets_new_grace_and_does_not_replay_disabled_period(self):
        seed_default_schedules(self.path, NOW + timedelta(days=1), {})
        _, disabled = self.report(at=NOW + timedelta(days=1))
        self.assertEqual(disabled["outlook"]["status"], "disabled")
        resumed = NOW + timedelta(days=2)
        seed_default_schedules(self.path, resumed, ENV)
        _, caps = self.report(at=resumed)
        self.assertEqual(caps["outlook"]["status"], "configured_unverified")
        with db.connect(self.path) as con:
            row = con.execute("SELECT enabled_since,next_due_at FROM schedule_specs WHERE task_kind='outlook.mail.sync'").fetchone()
        self.assertEqual(row[0], utc_stamp(resumed))
        self.assertGreater(row[1], utc_stamp(resumed))

    def test_success_with_empty_results_counts_as_healthy(self):
        self.work(status="succeeded", at=NOW + timedelta(minutes=5), schedule="system.worker.five_minute")
        _, caps = self.report(at=NOW + timedelta(minutes=10))
        self.assertEqual(caps["automation"]["status"], "ready")

    def test_historical_failure_is_resolved_by_later_success_without_deleting_history(self):
        key = "system.worker.five_minute"
        self.work("old", status="dead", schedule=key, retryable=1)
        self.work("new", status="succeeded", schedule=key, at=NOW + timedelta(minutes=5))
        result, caps = self.report(at=NOW + timedelta(minutes=10))
        self.assertEqual(caps["automation"]["status"], "ready")
        self.assertEqual(result["metrics"]["unresolved_work"], 0)
        rows = RecoveryService(self.path).list_work()
        self.assertEqual(rows[0]["reason_code"], "superseded_by_success")
        self.assertFalse(rows[0]["retry_allowed"])

    def test_disabled_failure_does_not_make_current_automation_unhealthy(self):
        self.work(status="dead", kind="outlook.mail.sync", schedule="outlook.mail.five_minute", retryable=1)
        seed_default_schedules(self.path, NOW, {})
        result, caps = self.report()
        self.assertEqual(result["metrics"]["unresolved_work"], 0)
        self.assertEqual(caps["outlook"]["status"], "disabled")

    def test_latest_dead_work_is_blocked_even_inside_freshness_grace(self):
        self.work(status="dead", schedule="system.worker.five_minute", retryable=1)
        _, caps = self.report()
        self.assertEqual(caps["automation"]["status"], "blocked")

    def test_pausing_suppresses_overdue_schedule_failures(self):
        _, caps = self.report(at=NOW + timedelta(days=30), paused=True)
        self.assertEqual(caps["automation"]["status"], "paused")
        self.assertEqual(caps["outlook"]["status"], "paused")

    def test_unknown_external_outcome_remains_visible_while_paused(self):
        self.work(status="dead", outcome="unknown", retryable=1)
        result, caps = self.report(paused=True)
        self.assertEqual(caps["work_queue"]["status"], "blocked")
        self.assertEqual(result["metrics"]["pending_reconciliation"], 1)

    def test_connector_reauth_is_not_hidden_by_successful_worker(self):
        with db.connect(self.path) as con:
            con.execute("INSERT INTO connector_health(connector_key,status,detail,updated_at) VALUES ('outlook:fixture:inbox','reauth_required','SECRET',?)", (utc_stamp(NOW),))
        result, caps = self.report()
        self.assertEqual(caps["outlook"]["next_action"], "reconnect_outlook")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_dependency_config_does_not_claim_live_inference_or_telegram(self):
        _, caps = self.report(dependencies={"inference": {"configured": True, "status": "configuration_ready"},
                                           "notifications": {"configured": True, "status": "ready"}})
        self.assertEqual(caps["inference"]["status"], "configured_unverified")
        self.assertEqual(caps["notification_transport"]["status"], "configured_unverified")

    def test_migration_requirement_is_explicit(self):
        _, caps = self.report(dependencies={"inference": {"configured": True, "status": "attention",
                                "preference_embeddings": {"status": "migration_required"}}})
        self.assertEqual(caps["ranking_model"]["reason_code"], "migration_required")

    def test_latest_pipeline_watermark_resolves_earlier_failed_workflow(self):
        self.work("old-root", "ats.new_only", status="dead")
        self.work("new-root", "ats.new_only", status="succeeded", at=NOW + timedelta(minutes=1))
        with db.connect(self.path) as con:
            for identity, state, at in (("old", "failed", NOW), ("new", "completed", NOW + timedelta(minutes=1))):
                con.execute("INSERT INTO workflow_runs(workflow_id,workflow_kind,root_work_id,trigger_task_kind,scheduled_for,status,started_at) VALUES (?,'opportunity_refresh',?,'ats.new_only',?,?,?)", (identity, identity+"-root", utc_stamp(at), state, utc_stamp(at)))
            for mark in ("recommendations_stable", "shortlist_evaluated"):
                con.execute("INSERT INTO workflow_watermarks(workflow_id,watermark_key,work_id,reached_at,result_sha256) VALUES ('new',?,'new-root',?,?)", (mark, utc_stamp(NOW + timedelta(minutes=1)), "a"*64))
        _, caps = self.report(at=NOW + timedelta(minutes=2))
        self.assertEqual(caps["ranking"]["status"], "ready")
        self.assertEqual(caps["shortlist"]["status"], "ready")

    def test_pipeline_missing_watermark_becomes_stale(self):
        self.work("root", "ats.new_only", status="succeeded")
        with db.connect(self.path) as con:
            con.execute("INSERT INTO workflow_runs(workflow_id,workflow_kind,root_work_id,trigger_task_kind,scheduled_for,status,started_at) VALUES ('wf','opportunity_refresh','root','ats.new_only',?,'running',?)", (utc_stamp(NOW), utc_stamp(NOW)))
        _, caps = self.report(at=NOW + timedelta(hours=3))
        self.assertEqual(caps["ranking"]["reason_code"], "workflow_progress_overdue")

    def test_notification_ambiguity_uses_exact_structured_error_code(self):
        with db.connect(self.path) as con:
            con.execute("INSERT INTO notification_outbox(notification_id,dedupe_key,topic,policy_id,title,body,status,max_attempts,available_at,last_error,created_at) VALUES ('notice','notice','fixture','fixture','Fixture','Private text','dead',3,?,'delivery_reconciliation_required',?)", (utc_stamp(NOW), utc_stamp(NOW)))
        result, caps = self.report()
        self.assertEqual(result["metrics"]["pending_reconciliation"], 1)
        self.assertEqual(caps["notification_outbox"]["next_action"], "inspect_reconciliation")
        self.assertNotIn("Private text", json.dumps(result))

    def test_backup_restore_preserves_recovery_audit_and_revision(self):
        self.work(status="dead", retryable=1)
        result = RecoveryService(self.path).retry("work-a", expected_revision=0, command_id="retry", now=NOW)
        restored = self.path.parent / "restored.db"
        with sqlite3.connect(self.path) as source, sqlite3.connect(restored) as destination:
            source.backup(destination)
        replay = RecoveryService(restored).retry("work-a", expected_revision=0, command_id="retry", now=NOW + timedelta(days=2))
        self.assertEqual(replay, result)

    def test_schedule_cadence_uses_success_not_posting_count(self):
        at = NOW + timedelta(days=1)
        with db.connect(self.path) as con:
            schedules = con.execute("SELECT schedule_key,task_kind FROM schedule_specs WHERE task_kind IN ('ats.authoritative','ats.new_only')").fetchall()
        for i, row in enumerate(schedules):
            self.work(str(i), row[1], status="succeeded", schedule=row[0], at=at)
        _, caps = self.report(at=at + timedelta(minutes=1))
        self.assertEqual(caps["ats.ingestion"]["status"], "ready")

    def test_safe_retry_is_audited_and_idempotent_with_current_revision(self):
        self.work(status="dead", retryable=1)
        service = RecoveryService(self.path)
        first = service.retry("work-a", expected_revision=0, command_id="retry-1", now=NOW)
        repeated = service.retry("work-a", expected_revision=0, command_id="retry-1", now=NOW + timedelta(days=1))
        self.assertEqual(first, repeated)
        with db.connect(self.path) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM work_recovery_commands").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT status FROM work_items").fetchone()[0], "queued")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM work_recovery_commands")

    def test_only_one_concurrent_recovery_wins(self):
        self.work(status="dead", retryable=1)
        def retry(index):
            try:
                return RecoveryService(self.path).retry("work-a", expected_revision=0, command_id=f"retry-{index}", now=NOW)["status"]
            except ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(retry, range(2))), ["conflict", "queued"])

    def test_stale_revision_and_command_reuse_are_rejected(self):
        self.work(status="dead", retryable=1)
        service = RecoveryService(self.path)
        with self.assertRaises(ConflictError):
            service.retry("work-a", expected_revision=1, command_id="bad", now=NOW)
        service.retry("work-a", expected_revision=0, command_id="done", now=NOW)
        with self.assertRaises(ConflictError):
            service.retry("work-a", expected_revision=1, command_id="done", now=NOW)

    def test_legacy_unknown_permanent_actions_and_remote_ambiguity_cannot_retry(self):
        cases = [("legacy", "system.worker_tick", None, "none"),
                 ("permanent", "system.worker_tick", 0, "none"),
                 ("action", "outlook.actions.execute", 1, "none"),
                 ("send", "notification.deliver", 1, "none"),
                 ("unknown", "system.worker_tick", 1, "unknown"),
                 ("accepted", "system.worker_tick", 1, "in_flight")]
        for identity, kind, retryable, outcome in cases:
            self.work(identity, kind, status="dead", retryable=retryable, outcome=outcome)
            with self.subTest(identity=identity), self.assertRaises(ConflictError):
                RecoveryService(self.path).retry(identity, expected_revision=0, command_id="retry-"+identity, now=NOW)
        self.assertFalse(any(item["retry_allowed"] for item in RecoveryService(self.path).list_work()))

    def test_hermes_cannot_authorize_operator_recovery(self):
        self.work(status="dead", retryable=1)
        with self.assertRaises(ContractError):
            RecoveryService(self.path).retry("work-a", expected_revision=0, command_id="attempt", actor_kind="hermes", now=NOW)

    def test_worker_persists_failure_classification_without_guessing(self):
        for i, error in enumerate((RetryableTaskError("fixture"), PermanentTaskError("fixture"), RuntimeError("fixture"))):
            identity = "work-"+str(i)
            self.work(identity)
            def fail(_payload, _context, exc=error):
                raise exc
            Worker(self.path, task_handlers={"system.worker_tick": fail}, now_provider=lambda: NOW,
                   max_work_per_tick=1, max_outbox_per_tick=0).tick()
        rows = {item["work_id"]: item for item in RecoveryService(self.path).list_work()}
        self.assertTrue(rows["work-0"]["retry_allowed"])
        self.assertFalse(rows["work-1"]["retry_allowed"])
        self.assertEqual(rows["work-2"]["failure_kind"], "unknown")

    def test_expired_remote_invocation_is_not_requeued(self):
        self.work(status="running", outcome="in_flight")
        with db.connect(self.path) as con:
            con.execute("UPDATE work_items SET lease_expires_at=?,lease_token='lost',lease_owner='gone'", (utc_stamp(NOW - timedelta(minutes=1)),))
        worker = Worker(self.path, max_work_per_tick=0, max_outbox_per_tick=0)
        result = worker._recover_expired(NOW)
        self.assertEqual(result["reconciliation"], 1)
        item = RecoveryService(self.path).list_work()[0]
        self.assertEqual(item["reason_code"], "external_reconciliation_required")

    def test_failure_after_remote_submit_cannot_use_generic_retryable_exception(self):
        self.work()
        def fail(_payload, context):
            with db.connect(self.path) as con:
                con.execute("UPDATE work_items SET external_outcome='in_flight' WHERE work_id=?", (context.work_id,))
            raise RetryableTaskError("lost polling response")
        Worker(self.path, task_handlers={"system.worker_tick": fail}, now_provider=lambda: NOW,
               max_work_per_tick=1, max_outbox_per_tick=0).tick()
        self.assertEqual(RecoveryService(self.path).list_work()[0]["reason_code"], "external_reconciliation_required")

    def test_known_terminal_failed_remote_job_can_retry(self):
        self.work(status="dead", retryable=1, outcome="terminal")
        result = RecoveryService(self.path).retry("work-a", expected_revision=0, command_id="retry", now=NOW)
        self.assertEqual(result["status"], "queued")

    def test_repeated_crashes_cannot_escape_the_attempt_limit(self):
        self.work(status="running")
        with db.connect(self.path) as con:
            con.execute("UPDATE work_items SET attempts=1,lease_expires_at=?", (utc_stamp(NOW - timedelta(minutes=1)),))
        result = Worker(self.path, max_work_per_tick=0, max_outbox_per_tick=0)._recover_expired(NOW)
        self.assertEqual(result["exhausted"], 1)
        self.assertEqual(result["work"], 0)
        self.assertEqual(RecoveryService(self.path).list_work()[0]["failure_kind"], "lease_expired")

    def test_queued_work_and_expired_lease_are_visible_without_mutation(self):
        self.work(at=NOW - timedelta(hours=1))
        self.work("expired", status="running")
        with db.connect(self.path) as con:
            con.execute("UPDATE work_items SET lease_expires_at=? WHERE work_id='expired'", (utc_stamp(NOW - timedelta(minutes=1)),))
        result, caps = self.report()
        self.assertEqual(result["metrics"]["overdue_work"], 1)
        self.assertEqual(result["metrics"]["expired_work_leases"], 1)
        self.assertEqual(caps["work_queue"]["status"], "stale")
        with db.connect(self.path) as con:
            self.assertEqual(con.execute("SELECT status FROM work_items WHERE work_id='expired'").fetchone()[0], "running")

    def test_migration_from_v6_keeps_checksums_and_legacy_failure_inspect_only(self):
        path = self.path.parent / "v6.db"
        prior = tuple(item for item in db.MIGRATIONS if item[0] <= 6)
        with patch.object(db, "MIGRATIONS", prior):
            db.prepare_database(path, utc_stamp(NOW))
        before = sqlite3.connect(path).execute("SELECT version,checksum FROM schema_migrations").fetchall()
        db.prepare_database(path, utc_stamp(NOW))
        db.prepare_database(path, utc_stamp(NOW))
        with db.connect(path) as con:
            self.assertEqual([tuple(row) for row in con.execute("SELECT version,checksum FROM schema_migrations WHERE version<=6")], before)
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])


if __name__ == "__main__":
    unittest.main()
