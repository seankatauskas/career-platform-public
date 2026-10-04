#!/usr/bin/env python3
"""Offline tests for SQLite-owned schedules and bounded worker execution."""

from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_search.contracts import JobSnapshot, MutationContext, RecommendationProvenance
from job_search.db import connect, prepare_database
from job_search.scheduler import (
    automation_health,
    has_real_scraper_contact,
    has_outlook_config,
    materialize_due_schedules,
    seed_default_schedules,
    utc_stamp,
)
from job_search.service import JobSearchLedger
from job_search.worker import (
    ApprovedActionTaskHandler,
    ATSCommandHandler,
    FollowUpTask,
    OutlookMailTaskHandler,
    PermanentTaskError,
    RetryableTaskError,
    TaskContext,
    TaskResult,
    Worker,
    acquire_worker_lease,
    preference_outbox_handler,
    release_worker_lease,
    run_command_with_heartbeat,
)
from job_search.sync import ProcessingResult, SyncResult


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def make_db(directory: str) -> Path:
    path = Path(directory) / "job-search.db"
    prepare_database(path, utc_stamp(NOW))
    return path


def enqueue_work(
    db_path: Path,
    work_id: str,
    task_kind: str,
    *,
    due: datetime = NOW,
    max_attempts: int = 3,
    status: str = "queued",
    attempts: int = 0,
    lease_expires_at: str | None = None,
    workflow_id: str = "",
) -> None:
    payload = {"scheduled_for": utc_stamp(due), "value": work_id}
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO work_items "
            "(work_id,schedule_key,task_kind,dedupe_key,payload_json,status,priority,due_at,"
            "attempts,max_attempts,lease_owner,lease_token,lease_expires_at,created_at,started_at,"
            "lane,workflow_id) VALUES (?,NULL,?,?,?,?,0,?,?,?,?,?,?,?,?,'core',?)",
            (
                work_id, task_kind, f"dedupe:{work_id}", json.dumps(payload), status,
                utc_stamp(due), attempts, max_attempts,
                "dead-worker" if status == "running" else None,
                "dead-token" if status == "running" else None,
                lease_expires_at,
                utc_stamp(due), utc_stamp(due) if status == "running" else None,
                workflow_id,
            ),
        )
        if status == "running":
            con.execute(
                "INSERT INTO job_runs "
                "(run_id,work_id,scheduled_for,started_at,result_json,error) "
                "VALUES (?,?,?,?,?,'')",
                (f"run-{work_id}", work_id, utc_stamp(due), utc_stamp(due), "{}"),
            )


def test_discovery_migration_preserves_pause_and_retains_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        environment = {"JOB_SCRAPER_CONTACT": "person@katauskas.dev"}
        seed_default_schedules(db_path, NOW, environment)
        with connect(db_path) as con:
            con.execute(
                "INSERT INTO schedule_specs SELECT 'ats.discovery.recent.sunday_0300',"
                "task_kind,schedule_json,enabled,coalesce,next_due_at,updated_at,enabled_since "
                "FROM schedule_specs WHERE schedule_key='ats.discovery.recent.0500'"
            )
            con.execute("INSERT INTO automation_controls VALUES ('collection',0,1,?)", (utc_stamp(NOW),))
        enqueue_work(db_path, "legacy-discovery", "ats.refresh_recent", due=NOW)
        with connect(db_path) as con:
            con.execute("UPDATE work_items SET schedule_key='ats.discovery.recent.sunday_0300' WHERE work_id='legacy-discovery'")
        seed_default_schedules(db_path, NOW, environment)
        with connect(db_path) as con:
            rows = con.execute("SELECT enabled FROM schedule_specs WHERE task_kind='ats.refresh_recent'").fetchall()
            assert con.execute("SELECT status FROM work_items WHERE work_id='legacy-discovery'").fetchone()[0] == "cancelled"
        assert len(rows) == 3 and all(row[0] == 0 for row in rows)


def test_discovery_precedes_paired_scans_and_follows_chicago_dst() -> None:
    from job_search.scheduler import DEFAULT_SCHEDULES, next_occurrence
    specs = {item.schedule_key: item for item in DEFAULT_SCHEDULES}
    for date, offset in (("2026-09-30", 5), ("2026-12-01", 6)):
        midnight = datetime.fromisoformat(date + "T00:00:00+00:00")
        for hour in (5, 17):
            discovery = specs[f"ats.discovery.recent.{hour:02d}00"]
            scan = specs[f"ats.new_only.{hour+1:02d}00"]
            due = next_occurrence(discovery.schedule, midnight)
            assert due.hour == hour + offset
            assert next_occurrence(scan.schedule, midnight) - due == timedelta(hours=1)
            assert discovery.schedule["priority"] > scan.schedule["priority"]


def test_default_schedules_are_exact_central_time_and_contact_gated() -> None:
    assert not has_real_scraper_contact({})
    assert not has_real_scraper_contact({"JOB_SCRAPER_CONTACT": "you@example.com"})
    assert has_real_scraper_contact({"JOB_SCRAPER_CONTACT": "person@katauskas.dev"})
    assert not has_outlook_config({})
    assert not has_outlook_config({"OUTLOOK_CLIENT_ID": "not-a-guid"})
    outlook_environment = {
        "OUTLOOK_CLIENT_ID": "12345678-1234-1234-1234-123456789abc"
    }
    assert has_outlook_config(outlook_environment)
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        result = seed_default_schedules(db_path, NOW, {})
        assert not result["ats_enabled"] and not result["outlook_enabled"]
        assert result["timezone"] == "America/Chicago"
        with connect(db_path) as con:
            rows = {row["schedule_key"]: dict(row) for row in con.execute(
                "SELECT * FROM schedule_specs"
            )}
        assert rows["system.worker.five_minute"]["next_due_at"] == "2026-09-01T12:05:00Z"
        assert rows["ats.authoritative.0200"]["next_due_at"] == "2026-09-02T07:00:00Z"
        assert rows["ats.new_only.1000"]["next_due_at"] == "2026-09-01T15:00:00Z"
        assert rows["ats.new_only.2200"]["next_due_at"] == "2026-09-02T03:00:00Z"
        assert rows["ats.discovery.recent.0500"]["next_due_at"] == "2026-09-02T10:00:00Z"
        assert rows["ats.discovery.recent.1700"]["next_due_at"] == "2026-09-01T22:00:00Z"
        assert all(
            not row["enabled"] for key, row in rows.items() if key.startswith("ats.")
        )
        assert not rows["outlook.mail.five_minute"]["enabled"]
        assert not rows["outlook.actions.five_minute"]["enabled"]
        assert not rows["notification.delivery.five_minute"]["enabled"]
        assert not rows["notification.reminders.five_minute"]["enabled"]
        seed_default_schedules(
            db_path, NOW, {"JOB_SCRAPER_CONTACT": "person@katauskas.dev"},
        )
        with connect(db_path) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM schedule_specs WHERE schedule_key LIKE 'ats.%' AND enabled=1"
            ).fetchone()[0] == 8
        seed_default_schedules(db_path, NOW, outlook_environment)
        with connect(db_path) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM schedule_specs "
                "WHERE schedule_key LIKE 'outlook.%' AND enabled=1"
            ).fetchone()[0] == 4
        seed_default_schedules(
            db_path, NOW, {"JOB_SEARCH_NOTIFICATION_TARGET": "ntfy"}
        )
        with connect(db_path) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM schedule_specs "
                "WHERE schedule_key LIKE 'notification.%' AND enabled=1"
            ).fetchone()[0] == 2


def test_materialized_outlook_work_runs_through_bounded_worker_handlers() -> None:
    class FakeCoordinator:
        def __init__(self):
            self.calls = []

        def sync_folder(self, account_id, folder_ref, max_pages, heartbeat):
            assert heartbeat()
            self.calls.append(("sync", account_id, folder_ref, max_pages))
            return SyncResult(2, 1, False, True)

        def process_pending(self, limit, transient_attempt, transient_limit, heartbeat):
            assert heartbeat()
            self.calls.append(("process", limit, transient_attempt, transient_limit))
            return ProcessingResult(1, 0, 0, 0, 0)

    class FakeService:
        def __init__(self):
            self.recovered = 0

        def recover_stale_actions(self, now, stale_after_seconds):
            del now, stale_after_seconds
            self.recovered += 1
            return {"recovered": 0, "needs_reconciliation": 0, "failed": 0}

        def list_actions(self, statuses):
            assert statuses == ("approved",)
            return []

    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        seed_default_schedules(
            db_path,
            NOW,
            {"OUTLOOK_CLIENT_ID": "12345678-1234-1234-1234-123456789abc"},
        )
        coordinator = FakeCoordinator()
        service = FakeService()
        handlers = {
            "outlook.mail.sync": OutlookMailTaskHandler(
                coordinator,
                account_id="outlook-personal",
                folder_refs=("inbox", "archive"),
            ),
            "outlook.actions.execute": ApprovedActionTaskHandler(
                service, object(), now_provider=lambda: NOW + timedelta(minutes=5)
            ),
        }
        # This fixture exercises mail/actions only; lifecycle workers have their
        # own acceptance suite and must not consume this fixture's three slots.
        with connect(db_path) as con:
            con.execute("UPDATE schedule_specs SET enabled=0 WHERE task_kind IN ('outlook.calendar.sync','outlook.mail.replay','attention.tick','career.mail.reconcile')")
        report = Worker(
            db_path,
            task_handlers=handlers,
            owner="outlook-runtime",
            now_provider=lambda: NOW + timedelta(minutes=5),
            max_work_per_tick=3,
            max_outbox_per_tick=0,
        ).tick(now=NOW + timedelta(minutes=5))
        assert report["materialized"]["created"] == 3
        assert report["work"]["succeeded"] == 3
        assert coordinator.calls == [
            ("sync", "outlook-personal", "inbox", 100),
            ("sync", "outlook-personal", "archive", 100),
            ("process", 100, 1, 5),
        ]
        assert service.recovered == 1


def test_schedule_materialization_coalesces_missed_and_outstanding_ticks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        seed_default_schedules(db_path, NOW, {})
        with connect(db_path) as con:
            con.execute("UPDATE schedule_specs SET enabled=0 WHERE task_kind='attention.tick'")
        later = NOW + timedelta(minutes=25)
        first = materialize_due_schedules(db_path, later)
        assert first == {"schedules": 1, "created": 1, "coalesced": 0}
        with connect(db_path) as con:
            work = con.execute("SELECT * FROM work_items").fetchone()
            assert json.loads(work["payload_json"])["scheduled_for"] == "2026-09-01T12:25:00Z"
            assert con.execute(
                "SELECT next_due_at FROM schedule_specs WHERE schedule_key='system.worker.five_minute'"
            ).fetchone()[0] == "2026-09-01T12:30:00Z"
            con.execute(
                "UPDATE schedule_specs SET next_due_at='2026-09-01T12:20:00Z' "
                "WHERE schedule_key='system.worker.five_minute'"
            )
        second = materialize_due_schedules(db_path, later)
        assert second["created"] == 0 and second["coalesced"] == 1
        with connect(db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1


def test_worker_lease_blocks_overlap_and_can_be_reclaimed_after_expiry() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        first = acquire_worker_lease(db_path, owner="one", now=NOW, lease_seconds=60)
        assert first
        assert acquire_worker_lease(db_path, owner="two", now=NOW, lease_seconds=60) is None
        second = acquire_worker_lease(
            db_path, owner="two", now=NOW + timedelta(seconds=61), lease_seconds=60,
        )
        assert second and second != first
        release_worker_lease(db_path, second)


def test_worker_bounds_each_tick_and_records_health() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        seen = []
        for index in range(5):
            enqueue_work(db_path, f"work-{index}", "test.task")

        def handler(payload, context):
            seen.append((payload["value"], context.attempt))
            return {"handled": payload["value"]}

        worker = Worker(
            db_path,
            task_handlers={"test.task": handler},
            owner="bounded",
            now_provider=lambda: NOW,
            max_work_per_tick=2,
            max_outbox_per_tick=0,
        )
        report = worker.tick(now=NOW)
        assert report["work"] == {"succeeded": 2, "retried": 0, "dead": 0}
        assert len(seen) == 2 and report["health"]["work_counts"]["queued"] == 3
        with connect(db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 2
            assert con.execute(
                "SELECT COUNT(*) FROM job_runs WHERE outcome='succeeded'"
            ).fetchone()[0] == 2


def test_retry_after_beats_exponential_delay_then_success_reuses_run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        enqueue_work(db_path, "retry-work", "retry.task")
        calls = 0

        def handler(payload, context):
            nonlocal calls
            del payload, context
            calls += 1
            if calls == 1:
                raise RetryableTaskError("throttled", retry_after_seconds=600)
            return {"ok": True}

        first = Worker(
            db_path,
            task_handlers={"retry.task": handler},
            owner="retry-one",
            now_provider=lambda: NOW,
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        ).tick(now=NOW)
        assert first["work"]["retried"] == 1
        with connect(db_path) as con:
            row = con.execute("SELECT status,due_at,attempts FROM work_items").fetchone()
            assert tuple(row) == ("queued", "2026-09-01T12:10:00Z", 1)
            assert con.execute("SELECT outcome FROM job_runs").fetchone()[0] == "failed"

        later = NOW + timedelta(minutes=10)
        second = Worker(
            db_path,
            task_handlers={"retry.task": handler},
            owner="retry-two",
            now_provider=lambda: later,
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        ).tick(now=later)
        assert second["work"]["succeeded"] == 1 and calls == 2
        with connect(db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 1
            assert con.execute("SELECT outcome FROM job_runs").fetchone()[0] == "succeeded"


def test_long_work_records_finish_time_and_retries_after_completion():
    for fails in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            enqueue_work(db, "a-long", "long.task")
            enqueue_work(db, "b-next", "next.task")
            elapsed = [100.0]
            def long_task(payload, context):
                elapsed[0] += 600
                if fails:
                    raise RetryableTaskError("retry after finishing")
                return {}
            worker = Worker(db, task_handlers={"long.task": long_task, "next.task": lambda p, c: {}},
                            owner="clock-test", now_provider=lambda: NOW,
                            max_work_per_tick=2, max_outbox_per_tick=0)
            with patch("job_search.worker.time.monotonic", side_effect=lambda: elapsed[0]):
                worker.tick(now=NOW)
            with connect(db) as con:
                assert con.execute("SELECT completed_at FROM job_runs WHERE work_id='a-long'").fetchone()[0] == utc_stamp(NOW + timedelta(minutes=10))
                assert con.execute("SELECT started_at FROM job_runs WHERE work_id='b-next'").fetchone()[0] == utc_stamp(NOW + timedelta(minutes=10))
                if fails:
                    assert con.execute("SELECT due_at FROM work_items WHERE work_id='a-long'").fetchone()[0] == utc_stamp(NOW + timedelta(minutes=11))


def test_ats_failure_keeps_error_tail_and_redacts_before_truncation():
    diagnostic = "progress " * 1000 + "token=" + "private" * 300 + "\nFatal discovery error: HTTP 502"
    handler = ATSCommandHandler("refresh_recent", project_root=ROOT, jobs_db=ROOT / "unused.db",
        environment_provider=lambda: {"JOB_SCRAPER_CONTACT": "person@katauskas.dev"},
        runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", diagnostic))
    try:
        handler({}, TaskContext("work-1", "ats.refresh_recent", 1, utc_stamp(NOW), lambda: True))
        raise AssertionError("failed command accepted")
    except RetryableTaskError as exc:
        assert "Fatal discovery error: HTTP 502" in str(exc)
        assert "private" not in str(exc)
        assert len(str(exc)) < 900


def test_permanent_failure_is_dead_without_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        enqueue_work(db_path, "dead-work", "dead.task", max_attempts=5)

        def fail(payload, context):
            del payload, context
            raise PermanentTaskError("invalid task configuration")

        report = Worker(
            db_path,
            task_handlers={"dead.task": fail},
            owner="dead",
            now_provider=lambda: NOW,
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        ).tick(now=NOW)
        assert report["work"]["dead"] == 1
        with connect(db_path) as con:
            assert con.execute("SELECT status FROM work_items").fetchone()[0] == "dead"
            assert con.execute("SELECT outcome FROM job_runs").fetchone()[0] == "dead"


def test_expired_running_work_is_recovered_once() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        enqueue_work(
            db_path,
            "crashed-work",
            "recover.task",
            status="running",
            attempts=1,
            lease_expires_at=utc_stamp(NOW - timedelta(seconds=1)),
        )
        seen = []

        def handler(payload, context):
            seen.append(context.attempt)
            return {"recovered": payload["value"]}

        report = Worker(
            db_path,
            task_handlers={"recover.task": handler},
            owner="recovery",
            now_provider=lambda: NOW,
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        ).tick(now=NOW)
        assert report["recovered"]["work"] == 1
        assert report["work"]["succeeded"] == 1 and seen == [2]


def test_stale_worker_cannot_fail_a_reclaimed_workflow() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        enqueue_work(
            db_path,
            "root-work",
            "ats.new_only",
            status="running",
            attempts=1,
            lease_expires_at=utc_stamp(NOW - timedelta(seconds=1)),
            workflow_id="workflow-1",
        )
        with connect(db_path) as con:
            con.execute(
                "INSERT INTO workflow_runs "
                "(workflow_id,workflow_kind,root_work_id,trigger_task_kind,scheduled_for,"
                "status,started_at,last_error) VALUES "
                "('workflow-1','opportunity_refresh','root-work','ats.new_only',?,"
                "'running',?,'')",
                (utc_stamp(NOW), utc_stamp(NOW)),
            )
            stale_item = dict(
                con.execute(
                    "SELECT * FROM work_items WHERE work_id='root-work'"
                ).fetchone()
            )
            stale_item["payload"] = json.loads(stale_item["payload_json"])

        replacement = Worker(db_path, now_provider=lambda: NOW)
        assert replacement._recover_expired(NOW)["work"] == 1
        claimed, replacement_token = replacement._claim_work(NOW)
        stale = Worker(db_path, now_provider=lambda: NOW)
        try:
            stale._fail_work(
                stale_item,
                "dead-token",
                PermanentTaskError("stale worker lost its lease"),
                NOW,
            )
        except RuntimeError as exc:
            assert "lease was lost" in str(exc)
        else:
            raise AssertionError("stale worker changed a reclaimed workflow")

        replacement._complete_work(
            claimed, replacement_token, TaskResult({"ok": True}), NOW
        )
        with connect(db_path) as con:
            assert con.execute(
                "SELECT status FROM work_items WHERE work_id='root-work'"
            ).fetchone()[0] == "succeeded"
            assert con.execute(
                "SELECT outcome FROM job_runs WHERE work_id='root-work'"
            ).fetchone()[0] == "succeeded"
            assert con.execute(
                "SELECT status FROM workflow_runs WHERE workflow_id='workflow-1'"
            ).fetchone()[0] == "running"


def test_preference_outbox_delivery_is_bounded_and_marks_delivered() -> None:
    class Gateway:
        def __init__(self):
            self.calls = []

        def deliver_applied_feedback(self, payload, *, source_event_id):
            self.calls.append((dict(payload), source_event_id))
            return {"ok": True, "created": True}

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "job-search.db"
        service = JobSearchLedger(db_path)
        started = service.start_application(
            JobSnapshot(
                "ashby", "job-1", "family-1", "Engineer", "Acme", "acme",
                "https://example.test/job-1",
            ),
            RecommendationProvenance(impression_id=7),
            MutationContext("start", "user", "dashboard"),
        )
        submission = service.record_submission(
            started["application"]["application_id"],
            "2026-09-01T11:59:00Z",
            MutationContext("submit", "user", "dashboard"),
        )
        gateway = Gateway()
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        worker = Worker(
            db_path,
            outbox_handlers={
                "recommendation.applied": preference_outbox_handler(gateway),
            },
            owner="outbox",
            now_provider=lambda: future,
            max_work_per_tick=0,
            max_outbox_per_tick=1,
        )
        report = worker.tick(now=future)
        assert report["outbox"]["delivered"] == 1
        assert gateway.calls[0][1] == submission["event"]["event_id"]
        assert worker.tick(now=future)["outbox"]["delivered"] == 0
        assert len(gateway.calls) == 1


def test_post_scrape_follow_up_is_durable_and_processed_within_bound() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        enqueue_work(db_path, "scrape-work", "ats.fake")
        followed = []

        def scrape(payload, context):
            del payload
            return TaskResult(
                {"scraped": True},
                [FollowUpTask("post.preference", {"source_work_id": context.work_id})],
            )

        def post(payload, context):
            followed.append((payload["source_work_id"], context.task_kind))
            return {"refreshed": True}

        report = Worker(
            db_path,
            task_handlers={"ats.fake": scrape, "post.preference": post},
            owner="hooks",
            now_provider=lambda: NOW,
            max_work_per_tick=2,
            max_outbox_per_tick=0,
        ).tick(now=NOW)
        assert report["work"]["succeeded"] == 2
        assert followed == [("scrape-work", "post.preference")]


def test_ats_handler_uses_fixed_safe_commands_and_requires_contact() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    context = TaskContext("work-1", "ats.new_only", 1, utc_stamp(NOW), lambda: True)
    handler = ATSCommandHandler(
        "new_only",
        project_root=ROOT,
        jobs_db=ROOT / "job-boards.db",
        environment_provider=lambda: {"JOB_SCRAPER_CONTACT": "person@katauskas.dev"},
        runner=runner,
        follow_up_task_kinds=("post.preference",),
    )
    result = handler({}, context)
    command, kwargs = calls[0]
    assert "--all" in command and "--new-only" in command
    concurrency = command.index("--concurrency")
    assert command[concurrency + 1] == "8" and command.count("--concurrency") == 1
    output = command.index("--out")
    assert command[output + 1] == str((ROOT / "job-boards").resolve())
    assert kwargs["shell"] is False and result.result["concurrency"] == 8
    assert result.follow_ups[0].task_kind == "post.preference"

    blocked = ATSCommandHandler(
        "authoritative",
        project_root=ROOT,
        jobs_db=ROOT / "job-boards.db",
        environment_provider=lambda: {},
        runner=runner,
    )
    try:
        blocked({}, context)
        raise AssertionError("ATS task ran without JOB_SCRAPER_CONTACT")
    except RetryableTaskError as exc:
        assert exc.retry_after_seconds == 4 * 60 * 60
    assert len(calls) == 1


def test_long_subprocess_heartbeats_and_terminates_if_lease_is_lost() -> None:
    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.returncode = 0
            self.calls = 0
            self.terminated = False

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(("fake",), timeout)
            return "ok", ""

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    heartbeats = []
    context = TaskContext(
        "long-work", "ats.new_only", 1, utc_stamp(NOW),
        lambda: heartbeats.append(True) or True,
    )
    process = FakeProcess()
    result = run_command_with_heartbeat(
        ("fake",),
        context,
        cwd=str(ROOT),
        env={},
        timeout_seconds=60,
        heartbeat_interval_seconds=1,
        popen_factory=lambda *_args, **_kwargs: process,
    )
    assert result.returncode == 0 and heartbeats == [True]

    lost = FakeProcess()
    try:
        run_command_with_heartbeat(
            ("fake",),
            TaskContext("lost", "ats.new_only", 1, utc_stamp(NOW), lambda: False),
            cwd=str(ROOT),
            env={},
            timeout_seconds=60,
            heartbeat_interval_seconds=1,
            popen_factory=lambda *_args, **_kwargs: lost,
        )
    except PermanentTaskError:
        pass
    else:
        raise AssertionError("lease-lost subprocess was allowed to continue")
    assert lost.terminated


def test_health_and_launchd_template_expose_operations_without_installing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = make_db(directory)
        seed_default_schedules(db_path, NOW, {})
        enqueue_work(db_path, "health-work", "health.task", due=NOW - timedelta(minutes=1))
        health = automation_health(db_path, NOW)
        assert health["overdue_work"] == 1
        assert health["schedules"] and health["checked_at"] == utc_stamp(NOW)

    template_path = ROOT / "launchd" / "com.local.job-search-worker.plist.template"
    template = template_path.read_bytes()
    plist = plistlib.loads(template)
    assert plist["StartInterval"] == 300 and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"][2] == "job_search.worker"
    assert plist["EnvironmentVariables"]["JOB_SCRAPER_CONTACT"] == "__JOB_SCRAPER_CONTACT__"
    assert plist["EnvironmentVariables"]["OUTLOOK_CLIENT_ID"] == "__OUTLOOK_CLIENT_ID__"
    assert (
        plist["EnvironmentVariables"]["JOB_SEARCH_MAIL_CLASSIFIER_CONFIG"]
        == "__MAIL_CLASSIFIER_CONFIG__"
    )
    assert plist["EnvironmentVariables"]["OUTLOOK_ACCOUNT_ID"] == "__OUTLOOK_ACCOUNT_ID__"


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search automation tests)")


if __name__ == "__main__":
    main()
    OutlookMailTaskHandler,
