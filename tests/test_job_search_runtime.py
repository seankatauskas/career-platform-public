#!/usr/bin/env python3
"""Offline checks for runtime composition, workflow lineage, and launchd plans."""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import job_search.db as database
from job_search.db import connect
from job_search.launchd import manage_launch_agents, render_launch_agents
from job_search.pipeline import (
    NOTIFICATION_TASK,
    SALARY_BATCH_SIZE,
    FixedCommand,
    OpportunityDAG,
    SalaryDrainHandler,
)
from job_search.runtime import RuntimeConfigV1, build_runtime, load_runtime_config
from job_search.scheduler import workflow_health
from job_search.worker import (
    PermanentTaskError,
    RetryableTaskError,
    TaskContext,
    acquire_worker_lease,
    release_worker_lease,
)


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def write_config(root: Path) -> Path:
    hermes = root / "bin" / "hermes"
    hermes.parent.mkdir(parents=True, exist_ok=True)
    hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hermes, 0o700)
    path = root / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "project_root": str(root),
                "log_dir": "logs",
                "mcp_token_file": "mcp-token",
                "scraper_contact": "person@katauskas.dev",
                "outlook_mail_folders": ["inbox", "archive"],
                "inference_config": "private/inference.json",
                "portable_encryption_key_file": "private/master-key",
                "shortlist_limit": 25,
                "hermes_container": "chief-of-staff",
                "hermes_image": "local/hermes:fixed",
                "hermes_executable": str(hermes),
                "hermes_telegram_target": "local-target",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def test_runtime_config_is_versioned_private_and_contains_no_secret_fields() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = write_config(root)
        config = load_runtime_config(path, required=True)
        assert config.version == 1
        resolved = root.resolve()
        assert config.application_db == resolved / "job-search.db"
        assert config.jobs_db == resolved / "job-boards.db"
        assert config.preference_db == resolved / "job-boards-preference.db"
        assert config.proxy_db == resolved / "job-boards-proxy.db"
        assert config.outlook_mail_folders == ("inbox", "archive")
        assert config.inference_config == resolved / "private/inference.json"
        assert (
            config.portable_encryption_key_file
            == resolved / "private/master-key"
        )
        assert config.environment({})["JOB_SEARCH_INFERENCE_CONFIG"] == str(
            resolved / "private/inference.json"
        )
        assert config.remote_mail_inference_enabled is False
        assert config.public_mapping()["remote_mail_inference_enabled"] is False
        assert "portable_encryption_key_file" not in config.public_mapping()
        assert config.public_mapping()["portable_encryption_configured"] is True
        assert config.shortlist_defaults()["limit"] == 25
        assert config.hermes_executable == resolved / "bin" / "hermes"
        assert "OUTLOOK_MAIL_FOLDER" not in config.environment({})
        secret_like_fields = {
            name
            for name in config.public_mapping()
            if "token" in name or "secret" in name
        }
        assert secret_like_fields == {"mcp_token_file"}
        assert str(config.public_mapping()["mcp_token_file"]).endswith("mcp-token")

        os.chmod(path, 0o644)
        try:
            load_runtime_config(path, required=True)
        except ValueError as exc:
            assert "owner-only" in str(exc)
        else:
            raise AssertionError("world-readable runtime config was accepted")

        bad = {"version": 1, "project_root": str(root), "telegram_bot_token": "secret"}
        try:
            RuntimeConfigV1.from_mapping(bad, default_root=root)
        except ValueError as exc:
            assert "unknown runtime config fields" in str(exc)
        else:
            raise AssertionError("a secret-bearing config field was accepted")

        opted_in = RuntimeConfigV1.from_mapping(
            {
                "version": 1,
                "project_root": str(root),
                "remote_mail_inference_enabled": True,
            },
            default_root=root,
        )
        assert opted_in.remote_mail_inference_enabled is True
        for invalid in ("true", 1, None):
            try:
                RuntimeConfigV1.from_mapping(
                    {
                        "version": 1,
                        "project_root": str(root),
                        "remote_mail_inference_enabled": invalid,
                    },
                    default_root=root,
                )
            except ValueError as exc:
                assert "remote_mail_inference_enabled must be a boolean" in str(exc)
            else:
                raise AssertionError("a non-boolean remote-mail opt-in was accepted")

        try:
            RuntimeConfigV1.from_mapping(
                {
                    "version": 1,
                    "project_root": str(root),
                    "hermes_telegram_target": "ntfy",
                },
                default_root=root,
            )
        except ValueError as exc:
            assert "hermes_executable is required" in str(exc)
        else:
            raise AssertionError("notification delivery accepted no Hermes executable")

        for incomplete in (
            {"resume_lab_db": "resume.db"},
            {"resume_artifact_root": "resume-artifacts"},
            {"resume_tectonic_executable": "/bin/echo"},
            {"resume_tectonic_bundle": "bundle.tar"},
        ):
            try:
                RuntimeConfigV1.from_mapping(
                    {"version": 1, "project_root": str(root), **incomplete},
                    default_root=root,
                )
            except ValueError as exc:
                assert "configured together" in str(exc)
            else:
                raise AssertionError(
                    "incomplete resume runtime configuration was accepted"
                )

        resume_config = RuntimeConfigV1.from_mapping(
            {
                "version": 1,
                "project_root": str(root),
                "resume_lab_db": "private/resume.db",
                "resume_artifact_root": "private/resumes",
                "resume_model_config": "private/model.json",
                "resume_tectonic_executable": "/bin/echo",
                "resume_tectonic_bundle": "private/tectonic.bundle",
                "resume_tectonic_version": "0.15.0",
            },
            default_root=root,
        )
        assert resume_config.resume_lab_db == root.resolve() / "private/resume.db"
        assert resume_config.resume_artifact_root == root.resolve() / "private/resumes"
        assert resume_config.resume_tectonic_version == "0.15.0"

        remote_tools = RuntimeConfigV1.from_mapping(
            {
                "version": 1,
                "project_root": str(root),
                "resume_lab_db": "private/resume.db",
                "resume_artifact_root": "private/resumes",
                "tool_service_socket": "private/tools.sock",
                "resume_tectonic_version": "0.15.0",
            },
            default_root=root,
        )
        assert remote_tools.tool_service_socket == root.resolve() / "private/tools.sock"
        try:
            RuntimeConfigV1.from_mapping(
                {
                    "version": 1,
                    "project_root": str(root),
                    "resume_tectonic_executable": "/bin/echo",
                    "resume_tectonic_bundle": "private/tectonic.bundle",
                    "resume_tectonic_version": "0.15.0",
                    "tool_service_socket": "private/tools.sock",
                },
                default_root=root,
            )
        except ValueError as exc:
            assert "mutually exclusive" in str(exc)
        else:
            raise AssertionError("local and remote document toolchains were both accepted")


def test_environment_notification_target_uses_configured_hermes_executable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        executable = (root / "bin" / "hermes").resolve()
        config = RuntimeConfigV1.from_mapping(
            {
                "version": 1,
                "project_root": str(root),
                "hermes_executable": str(executable),
            },
            default_root=root,
        )
        runtime = build_runtime(
            config,
            lane="core",
            now_provider=lambda: NOW,
            base_environment={"JOB_SEARCH_NOTIFICATION_TARGET": "ntfy"},
            max_work_per_tick=0,
            max_outbox_per_tick=0,
        )
        delivery = runtime.worker.task_handlers["notification.deliver"].__self__
        assert delivery._sender.executable == str(executable)
        assert delivery._sender.target == "ntfy"
        reminder = runtime.worker.task_handlers["outlook.reminders.publish"]
        assert reminder.publisher.policy.enabled_topics
        with connect(config.application_db) as con:
            enabled = con.execute(
                "SELECT COUNT(*) FROM schedule_specs "
                "WHERE schedule_key LIKE 'notification.%' AND enabled=1"
            ).fetchone()[0]
        assert enabled == 2

        missing_executable = RuntimeConfigV1.defaults(root / "missing")
        try:
            build_runtime(
                missing_executable,
                lane="core",
                now_provider=lambda: NOW,
                base_environment={"JOB_SEARCH_NOTIFICATION_TARGET": "ntfy"},
            )
        except ValueError as exc:
            assert "hermes_executable is required" in str(exc)
        else:
            raise AssertionError(
                "environment notification target accepted no executable"
            )


def test_core_and_model_lanes_execute_the_critical_dag_with_stable_watermarks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = load_runtime_config(write_config(root), required=True)
        commands = []

        def runner(command, **kwargs):
            assert kwargs["shell"] is False
            commands.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")

        core = build_runtime(
            config,
            lane="core",
            now_provider=lambda: NOW,
            command_runner=runner,
            salary_status_provider=lambda _path: {
                "activated_at": None,
                "actionable": 0,
            },
            base_environment={},
            max_work_per_tick=2,
            max_outbox_per_tick=0,
        )
        with connect(config.application_db) as con:
            con.execute(
                "UPDATE schedule_specs SET next_due_at=? "
                "WHERE schedule_key='ats.new_only.1000'",
                ("2026-09-02T12:00:00Z",),
            )
        first = core.tick(NOW)
        assert first["lane"] == "core"
        assert first["work"]["succeeded"] == 2
        assert len(commands) == 2
        scraper = next(
            command for command in commands if command[1:3] == ("-m", "job_search.collection.boards")
        )
        assert "--new-only" in scraper
        concurrency = scraper.index("--concurrency")
        assert scraper[concurrency + 1] == "8"

        with connect(config.application_db) as con:
            rows = [
                dict(row)
                for row in con.execute(
                    "SELECT work_id,task_kind,lane,workflow_id,parent_work_id,status "
                    "FROM work_items WHERE workflow_id<>'' ORDER BY created_at,priority DESC"
                )
            ]
        root_work = next(row for row in rows if row["task_kind"] == "ats.new_only")
        location = next(
            row for row in rows if row["task_kind"] == "opportunity.location_refresh"
        )
        preference = next(
            row for row in rows if row["task_kind"] == "opportunity.preference_refresh"
        )
        salary = next(
            row for row in rows if row["task_kind"] == "opportunity.salary_drain"
        )
        assert location["parent_work_id"] == root_work["work_id"]
        assert salary["parent_work_id"] == root_work["work_id"]
        assert preference["parent_work_id"] == location["work_id"]
        assert {row["workflow_id"] for row in rows} == {root_work["workflow_id"]}
        assert preference["lane"] == salary["lane"] == "model"

        model = build_runtime(
            config,
            lane="model",
            now_provider=lambda: NOW,
            command_runner=runner,
            salary_status_provider=lambda _path: {
                "activated_at": None,
                "actionable": 0,
            },
            base_environment={},
            max_work_per_tick=2,
            max_outbox_per_tick=0,
        )
        second = model.tick(NOW)
        assert second["lane"] == "model" and second["work"]["succeeded"] == 2
        health = workflow_health(config.application_db, NOW)
        workflow = health["workflows"][0]
        assert workflow["status"] == "stable"
        assert workflow["stable_at"] == "2026-09-02T12:00:00Z"
        assert health["latest_watermarks"]["recommendations_stable"]

        waiting = core.tick(NOW)
        assert waiting["work"]["succeeded"] == 2
        with connect(config.application_db) as con:
            evaluated = con.execute(
                "SELECT status,attempts FROM work_items WHERE task_kind=?",
                (NOTIFICATION_TASK,),
            ).fetchall()
            assert len(evaluated) == 2
            assert all(tuple(row) == ("succeeded", 1) for row in evaluated)

        completed = workflow_health(config.application_db, NOW)["workflows"][0]
        assert completed["status"] == "completed"
        assert completed["watermarks"]["shortlist_evaluated"]

        # Callers can still replace the evaluator at the explicit composition seam.
        overridden = build_runtime(
            config,
            lane="core",
            now_provider=lambda: NOW,
            command_runner=runner,
            salary_status_provider=lambda _path: {
                "activated_at": None,
                "actionable": 0,
            },
            task_overrides={
                NOTIFICATION_TASK: lambda _payload, _context: {"sent": False}
            },
            base_environment={},
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        )
        assert overridden.worker.task_handlers[NOTIFICATION_TASK]({}, object()) == {
            "sent": False
        }


def test_lane_leases_do_not_block_each_other() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = RuntimeConfigV1.defaults(Path(directory)).application_db
        from job_search.db import prepare_database

        prepare_database(db, "2026-09-02T12:00:00Z")
        core = acquire_worker_lease(db, owner="core", now=NOW, lane="core")
        model = acquire_worker_lease(db, owner="model", now=NOW, lane="model")
        assert core and model and core != model
        release_worker_lease(db, core, lane="core")
        release_worker_lease(db, model, lane="model")


def test_refresh_recent_is_discovery_only_and_has_no_derived_followups() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = load_runtime_config(write_config(root), required=True)
        commands = []

        def runner(command, **_kwargs):
            commands.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")

        runtime = build_runtime(
            config,
            lane="core",
            now_provider=lambda: NOW,
            command_runner=runner,
            base_environment={},
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        )
        with connect(config.application_db) as con:
            con.execute(
                "UPDATE schedule_specs SET next_due_at=? "
                "WHERE schedule_key='ats.discovery.recent.0500'",
                ("2026-09-02T12:00:00Z",),
            )
        report = runtime.tick(NOW)
        assert report["work"]["succeeded"] == 1
        assert "--refresh-recent" in commands[0]
        with connect(config.application_db) as con:
            derived = con.execute(
                "SELECT COUNT(*) FROM work_items WHERE parent_work_id IS NOT NULL"
            ).fetchone()[0]
        assert derived == 0


def test_fixed_command_preserves_permanent_and_retryable_exit_semantics() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        context = TaskContext(
            "command-exit",
            "opportunity.preference_refresh",
            1,
            "2026-09-02T12:00:00Z",
            lambda: True,
            "model",
            "workflow-exit",
            "parent-exit",
        )

        def result(code):
            return lambda command, **_kwargs: subprocess.CompletedProcess(
                command, code, "configuration failed", ""
            )

        permanent = FixedCommand(
            ("python", "-m", "job_search.ranking.model", "refresh"),
            project_root=root,
            environment_provider=lambda: {},
            runner=result(78),
        )
        try:
            permanent.run(context)
        except PermanentTaskError as exc:
            assert "configuration failed" in str(exc)
        else:
            raise AssertionError("permanent model failure was marked retryable")

        retryable = FixedCommand(
            ("python", "-m", "job_search.ranking.model", "refresh"),
            project_root=root,
            environment_provider=lambda: {},
            runner=result(75),
        )
        try:
            retryable.run(context)
        except RetryableTaskError:
            pass
        else:
            raise AssertionError("temporary model failure was marked permanent")


def test_salary_side_branch_is_bounded_and_only_notifies_when_drained() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        calls = []

        def runner(command, **_kwargs):
            calls.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, '{"processed": 25}', "")

        command = FixedCommand(
            ("python", "-m", "job_search.salary.llm", "run", "--limit", str(SALARY_BATCH_SIZE)),
            project_root=root,
            environment_provider=lambda: {},
            runner=runner,
        )
        context = TaskContext(
            "salary-1",
            "opportunity.salary_drain",
            1,
            "2026-09-02T12:00:00Z",
            lambda: True,
            "model",
            "workflow-1",
            "ats-1",
        )
        states = iter(
            [
                {"activated_at": "now", "actionable": 30},
                {"activated_at": "now", "actionable": 5},
            ]
        )
        first = SalaryDrainHandler(
            command, OpportunityDAG(), root / "jobs.db", lambda _path: next(states)
        )({}, context)
        assert len(calls) == 1
        assert first.result["batch_size"] == 25 and not first.result["queue_empty"]
        assert first.follow_ups[0].task_kind == "opportunity.salary_drain"
        assert first.follow_ups[0].delay_seconds == 300

        states = iter(
            [
                {"activated_at": "now", "actionable": 5},
                {"activated_at": "now", "actionable": 0},
            ]
        )
        last = SalaryDrainHandler(
            command, OpportunityDAG(), root / "jobs.db", lambda _path: next(states)
        )({}, context)
        assert last.result["queue_empty"]
        assert last.follow_ups[0].task_kind == NOTIFICATION_TASK
        assert last.follow_ups[0].payload["reason"] == "salary_queue_drained"

        for state in (
            {"activated_at": None, "actionable": 0},
            {"activated_at": "now", "actionable": 0},
        ):
            already_empty = SalaryDrainHandler(
                command,
                OpportunityDAG(),
                root / "jobs.db",
                lambda _path, value=state: value,
            )({}, context)
            assert already_empty.result["queue_empty"]
            assert already_empty.follow_ups[0].task_kind == NOTIFICATION_TASK
            assert (
                already_empty.follow_ups[0].payload["reason"] == "salary_queue_drained"
            )


def test_reserved_migration_restores_the_highest_applied_user_version() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        original = database.MIGRATIONS
        database.MIGRATIONS = tuple(item for item in original if item[0] != 4)
        try:
            database.prepare_database(path, "2026-09-02T12:00:00Z")
        finally:
            database.MIGRATIONS = original
        database.prepare_database(path, "2026-09-02T12:01:00Z")
        with connect(path) as con:
            assert [
                int(row[0])
                for row in con.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ] == [item[0] for item in original]
            assert con.execute("PRAGMA user_version").fetchone()[0] == original[-1][0]


def test_failed_migration_rolls_back_its_schema_and_ledger_entry() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        database.prepare_database(path, "2026-09-02T12:00:00Z")
        original = database.MIGRATIONS
        broken_version = original[-1][0] + 1
        database.MIGRATIONS = (
            *original,
            (
                broken_version,
                "broken_future_migration",
                "CREATE TABLE must_rollback (value TEXT);\nTHIS IS NOT SQL;\n",
            ),
        )
        try:
            try:
                database.prepare_database(path, "2026-09-02T12:01:00Z")
            except sqlite3.Error:
                pass
            else:
                raise AssertionError("invalid migration unexpectedly succeeded")
        finally:
            database.MIGRATIONS = original
        with connect(path) as con:
            assert (
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='must_rollback'"
                ).fetchone()
                is None
            )
            assert (
                con.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (broken_version,)
                ).fetchone()
                is None
            )
            assert con.execute("PRAGMA user_version").fetchone()[0] == original[-1][0]


def test_concurrent_migrators_lock_before_reading_the_ledger() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        migrations = database.MIGRATIONS
        database.MIGRATIONS = migrations[:3]
        try:
            database.prepare_database(path, "2026-09-02T11:00:00Z")
        finally:
            database.MIGRATIONS = migrations

        original_reader = database._applied_migrations
        first_read = threading.Event()
        release_first = threading.Event()
        second_read = threading.Event()
        reader_lock = threading.Lock()
        reads = 0

        def controlled_reader(connection):
            nonlocal reads
            rows = original_reader(connection)
            with reader_lock:
                reads += 1
                current = reads
            if current == 1:
                first_read.set()
                assert release_first.wait(5)
            else:
                second_read.set()
            return rows

        database._applied_migrations = controlled_reader
        failures = []

        def run_migration():
            try:
                database.prepare_database(path, "2026-09-02T12:00:00Z")
            except Exception as exc:
                failures.append(exc)

        first = threading.Thread(target=run_migration)
        second = threading.Thread(target=run_migration)
        try:
            first.start()
            assert first_read.wait(5)
            second.start()
            assert not second_read.wait(0.1)
            release_first.set()
            first.join(5)
            second.join(5)
        finally:
            release_first.set()
            first.join(5)
            second.join(5)
            database._applied_migrations = original_reader
        assert not failures
        assert second_read.is_set()


def test_concurrent_cold_start_configures_wal_without_lock_errors() -> None:
    for _ in range(10):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job-search.db"
            start = threading.Barrier(4)
            failures = []

            def initialize():
                try:
                    start.wait()
                    database.prepare_database(path, "2026-09-02T12:00:00Z")
                except Exception as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=initialize) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            assert not failures
            assert all(not thread.is_alive() for thread in threads)


def test_launchd_generation_is_secret_free_and_dry_run_has_no_side_effects() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = write_config(root)
        config = load_runtime_config(path, required=True)
        rendered = render_launch_agents(
            config,
            config_path=path,
            python_executable=Path("/usr/bin/python3"),
        )
        assert set(rendered) == {"core", "model", "dashboard", "mcp"}
        for lane in ("core", "model"):
            data = rendered[lane]
            plist = plistlib.loads(data)
            assert plist["StartInterval"] == 300 and plist["RunAtLoad"] is True
            assert "KeepAlive" not in plist
            assert plist["ProgramArguments"][-2:] == ["--lane", lane]
            assert "EnvironmentVariables" not in plist
            assert b"local-target" not in data and b"katauskas.dev" not in data
        for service in ("dashboard", "mcp"):
            data = rendered[service]
            plist = plistlib.loads(data)
            assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
            assert plist["ThrottleInterval"] == 30
            assert "StartInterval" not in plist
            assert plist["ProgramArguments"] == [
                "/usr/bin/python3",
                "-m",
                "job_search.system",
                "--config",
                str(path.resolve()),
                service,
            ]
            assert "EnvironmentVariables" not in plist
            assert b"mcp-token" not in data

        output = root / "LaunchAgents"

        def forbidden_runner(*_args, **_kwargs):
            raise AssertionError("dry-run attempted to call launchctl")

        plan = manage_launch_agents(
            config,
            action="install",
            apply=False,
            output_dir=output,
            config_path=path,
            runner=forbidden_runner,
        )
        assert not plan["applied"]
        assert not output.exists()
        assert all(item["command"][1] == "bootstrap" for item in plan["services"])


@patch("job_search.launchd.sys.executable", "/usr/bin/python3")
def test_launchd_apply_is_idempotent_and_uninstall_tolerates_an_absent_service() -> (
    None
):
    class FakeLaunchctl:
        def __init__(self) -> None:
            self.loaded = set()
            self.fail_next_bootstrap = ""

        def __call__(self, command, **_kwargs):
            command = tuple(command)
            operation = command[1]
            if operation in {"print", "bootout"}:
                label = command[-1].rsplit("/", 1)[-1]
            else:
                with Path(command[-1]).open("rb") as handle:
                    label = str(plistlib.load(handle)["Label"])
            if operation == "print":
                return subprocess.CompletedProcess(
                    command, 0 if label in self.loaded else 113, "", ""
                )
            if operation == "bootout":
                if label not in self.loaded:
                    return subprocess.CompletedProcess(command, 113, "", "not loaded")
                self.loaded.remove(label)
                return subprocess.CompletedProcess(command, 0, "", "")
            if label == self.fail_next_bootstrap:
                self.fail_next_bootstrap = ""
                return subprocess.CompletedProcess(command, 5, "", "simulated failure")
            if label in self.loaded:
                return subprocess.CompletedProcess(command, 5, "", "already loaded")
            self.loaded.add(label)
            return subprocess.CompletedProcess(command, 0, "", "")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config_path = write_config(root)
        config = load_runtime_config(config_path, required=True)
        from job_search.system import initialize_mcp_token

        initialize_mcp_token(config.mcp_token_file)
        output = root / "LaunchAgents"
        launchctl = FakeLaunchctl()
        labels = {
            "com.local.job-search-core",
            "com.local.job-search-model",
            "com.local.job-search-dashboard",
            "com.local.job-search-mcp",
        }

        assert config.hermes_executable is not None
        os.chmod(config.hermes_executable, 0o600)
        try:
            manage_launch_agents(
                config,
                action="install",
                apply=True,
                output_dir=output,
                config_path=config_path,
                runner=launchctl,
            )
        except ValueError as exc:
            assert "executable hermes_executable" in str(exc)
        else:
            raise AssertionError("launchd accepted a non-executable Hermes path")
        assert not output.exists() and not launchctl.loaded
        os.chmod(config.hermes_executable, 0o700)

        first = manage_launch_agents(
            config,
            action="install",
            apply=True,
            output_dir=output,
            config_path=config_path,
            python_executable=Path("/usr/bin/python3"),
            runner=launchctl,
        )
        assert first["ok"] and launchctl.loaded == labels
        original = {
            path.name: path.read_bytes() for path in sorted(output.glob("*.plist"))
        }

        second = manage_launch_agents(
            config,
            action="install",
            apply=True,
            output_dir=output,
            config_path=config_path,
            python_executable=Path("/usr/bin/python3"),
            runner=launchctl,
        )
        assert second["ok"] and launchctl.loaded == labels

        launchctl.fail_next_bootstrap = "com.local.job-search-mcp"
        failed = manage_launch_agents(
            config,
            action="install",
            apply=True,
            output_dir=output,
            config_path=config_path,
            python_executable=Path("/different/python3"),
            runner=launchctl,
        )
        assert not failed["ok"] and launchctl.loaded == labels
        assert {
            path.name: path.read_bytes() for path in sorted(output.glob("*.plist"))
        } == original

        launchctl.loaded.remove("com.local.job-search-core")
        removed = manage_launch_agents(
            config,
            action="uninstall",
            apply=True,
            output_dir=output,
            config_path=config_path,
            runner=launchctl,
        )
        assert removed["ok"] and not launchctl.loaded
        assert not list(output.glob("*.plist"))

        repeated = manage_launch_agents(
            config,
            action="uninstall",
            apply=True,
            output_dir=output,
            config_path=config_path,
            runner=launchctl,
        )
        assert repeated["ok"] and not launchctl.loaded
        assert not list(output.glob("*.plist"))


def test_work_item_lineage_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = RuntimeConfigV1.defaults(Path(directory))
        from job_search.db import prepare_database

        prepare_database(config.application_db, "2026-09-02T12:00:00Z")
        with connect(config.application_db) as con:
            con.execute(
                "INSERT INTO work_items "
                "(work_id,task_kind,dedupe_key,payload_json,status,priority,due_at,"
                "attempts,max_attempts,created_at,lane,workflow_id) "
                "VALUES ('one','test','one','{}','queued',0,?,0,1,?,'core','flow')",
                ("2026-09-02T12:00:00Z", "2026-09-02T12:00:00Z"),
            )
            try:
                con.execute("UPDATE work_items SET lane='model' WHERE work_id='one'")
            except sqlite3.IntegrityError as exc:
                assert "lineage is immutable" in str(exc)
            else:
                raise AssertionError("work lineage was mutated")


def test_outlook_poll_interval_changes_only_mail_and_survives_reseeding() -> None:
    from job_search.scheduler import seed_default_schedules
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = {"version": 1, "project_root": str(root),
                    "outlook_client_id": "12345678-1234-1234-1234-123456789abc"}
        config = RuntimeConfigV1.from_mapping(settings)
        database.prepare_database(config.application_db, "2026-09-02T12:00:00Z")
        seed_default_schedules(config.application_db, NOW, config.environment({}))
        slower = RuntimeConfigV1.from_mapping({**settings, "outlook_poll_interval_minutes": 15})
        env = slower.environment({"JOB_SEARCH_OUTLOOK_POLL_INTERVAL_MINUTES": "1"})
        assert env["JOB_SEARCH_OUTLOOK_POLL_INTERVAL_MINUTES"] == "15"
        seed_default_schedules(config.application_db, NOW, env)
        seed_default_schedules(config.application_db, NOW, env)
        with connect(config.application_db) as con:
            rows = {r["task_kind"]: dict(r) for r in con.execute("SELECT * FROM schedule_specs")}
        mail = rows["outlook.mail.sync"]
        assert mail["schedule_key"] == "outlook.mail.five_minute"
        assert json.loads(mail["schedule_json"])["minutes"] == 15
        assert mail["next_due_at"] == "2026-09-02T12:15:00Z"
        assert json.loads(rows["outlook.actions.execute"]["schedule_json"])["minutes"] == 5
        assert json.loads(rows["system.worker_tick"]["schedule_json"])["minutes"] == 5
        for invalid in (True, 0, -1, 1441, 1.5, "15"):
            try:
                RuntimeConfigV1.from_mapping({**settings, "outlook_poll_interval_minutes": invalid})
            except ValueError:
                pass
            else:
                raise AssertionError("invalid polling interval accepted")


def main() -> None:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search runtime tests)")


if __name__ == "__main__":
    main()
