#!/usr/bin/env python3
"""Focused persistence regressions for the resume-lab sidecar."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import job_search.resume_lab.store as store_module
from job_search.resume_lab import ResumeLabService
from job_search.resume_lab.contracts import ResumeConflictError
from job_search.resume_lab.store import ResumeLabStore, SCHEMA_SQL, connect
from tests.test_resume_lab_core import JOB, create_standard, make_service


def test_active_standard_limit_is_enforced_at_the_writer_boundary() -> None:
    original_limit = store_module.MAX_ACTIVE_STANDARDS
    store_module.MAX_ACTIVE_STANDARDS = 2
    try:
        with tempfile.TemporaryDirectory() as directory:
            _path, service = make_service(directory)
            create_standard(service, "First", 1, "first")
            create_standard(service, "Second", 2, "second")
            try:
                create_standard(service, "Third", 3, "third")
            except ResumeConflictError as exc:
                assert "at most 2" in str(exc)
            else:
                raise AssertionError("more than the active standard limit was accepted")
            assert len(service.list_active_standards()) == 2
    finally:
        store_module.MAX_ACTIVE_STANDARDS = original_limit


def test_retry_result_run_has_a_database_foreign_key() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, _service = make_service(directory)
        with connect(path) as connection:
            keys = connection.execute(
                "PRAGMA foreign_key_list(resume_run_retry_commands)"
            ).fetchall()
        assert any(
            row["from"] == "result_run_id"
            and row["table"] == "resume_runs"
            and row["to"] == "run_id"
            for row in keys
        )


def test_concurrent_open_serializes_legacy_retry_column_migration() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "legacy-resume.db"
        legacy_schema = SCHEMA_SQL.replace(
            "    result_run_id    TEXT,\n", ""
        ).replace(
            "    reconciliation_acknowledged INTEGER NOT NULL DEFAULT 0\n"
            "        CHECK (reconciliation_acknowledged IN (0, 1)),\n",
            "",
        ).replace(
            "    FOREIGN KEY (result_run_id) REFERENCES resume_runs(run_id)\n", ""
        ).replace(
            "    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id),\n"
            ");\nCREATE TRIGGER IF NOT EXISTS resume_run_retry_commands_no_update",
            "    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)\n"
            ");\nCREATE TRIGGER IF NOT EXISTS resume_run_retry_commands_no_update",
        )
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.executescript(legacy_schema)
        path.chmod(0o600)

        original_connect = store_module.connect
        barrier = threading.Barrier(2)

        @contextlib.contextmanager
        def synchronized_connect(db_path):
            with contextlib.closing(original_connect(db_path)) as connection:
                with connection:
                    barrier.wait(timeout=5)
                    yield connection

        failures = []

        def open_store() -> None:
            try:
                ResumeLabStore(path)
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        store_module.connect = synchronized_connect
        try:
            threads = [threading.Thread(target=open_store) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            assert all(not thread.is_alive() for thread in threads)
        finally:
            store_module.connect = original_connect

        assert failures == [], [f"{type(exc).__name__}: {exc}" for exc in failures]
        with connect(path) as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(resume_run_retry_commands)"
                )
            }
        assert {"result_run_id", "reconciliation_acknowledged"} <= columns


def test_wal_initialization_waits_for_a_real_reader_lock() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "legacy-resume.db"
        observed_busy = threading.Event()
        failures = []
        original_connect = sqlite3.connect

        class ObserveBusyConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == "PRAGMA journal_mode = WAL":
                    # Force immediate reporting of the genuine reader lock so
                    # the regression exercises our bounded startup wait without
                    # waiting through SQLite's independent ten-second timeout.
                    super().execute("PRAGMA busy_timeout=0")
                    try:
                        return super().execute(sql, *args)
                    except sqlite3.OperationalError:
                        observed_busy.set()
                        raise
                return super().execute(sql, *args)

        def configured_connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=ObserveBusyConnection)

        def open_store():
            try:
                ResumeLabStore(path)
            except Exception as exc:
                failures.append(exc)

        with contextlib.closing(original_connect(path)) as reader:
            reader.execute("CREATE TABLE fixture(value TEXT)")
            reader.commit()
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM fixture").fetchall()
            with patch.object(store_module.sqlite3, "connect", configured_connect):
                thread = threading.Thread(target=open_store)
                thread.start()
                try:
                    assert observed_busy.wait(timeout=3), "the held reader did not block WAL conversion"
                finally:
                    reader.rollback()
                    thread.join(timeout=15)
                assert not thread.is_alive()
        assert failures == [], [f"{type(exc).__name__}: {exc}" for exc in failures]
        with contextlib.closing(original_connect(path)) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("SELECT MAX(version) FROM resume_lab_schema").fetchone()[0] == 4


def test_wal_initialization_timeout_closes_connection_and_preserves_error() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "legacy-resume.db"
        original_connect = sqlite3.connect
        opened = []

        class ObserveConnection(sqlite3.Connection):
            closed = False

            def execute(self, sql, *args):
                if sql == "PRAGMA journal_mode = WAL":
                    super().execute("PRAGMA busy_timeout=0")
                return super().execute(sql, *args)

            def close(self):
                self.closed = True
                return super().close()

        def configured_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs, factory=ObserveConnection)
            opened.append(connection)
            return connection

        with contextlib.closing(original_connect(path)) as reader:
            reader.execute("CREATE TABLE fixture(value TEXT)")
            reader.commit()
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM fixture").fetchall()
            with patch.object(store_module.sqlite3, "connect", configured_connect), patch.object(store_module.time, "monotonic", side_effect=[0, 11]):
                try:
                    connect(path)
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc)
                else:
                    raise AssertionError("a permanently locked source was opened")
            reader.rollback()
        assert len(opened) == 1 and opened[0].closed


def test_run_order_is_monotonic_immutable_and_backfilled() -> None:
    original_now = store_module._now
    original_new_id = store_module._new_id
    run_ids = iter(
        (
            "run_ffffffffffffffffffffffffffffffff",
            "run_00000000000000000000000000000000",
            "run_11111111111111111111111111111111",
        )
    )

    def deterministic_id(prefix: str) -> str:
        return next(run_ids) if prefix == "run_" else original_new_id(prefix)

    store_module._now = lambda: "2026-09-02T12:00:00Z"
    store_module._new_id = deterministic_id
    try:
        with tempfile.TemporaryDirectory() as directory:
            path, service = make_service(directory)
            create_standard(service)
            first = service.create_run("application-1", JOB, "ordered-run-first")
            second = service.create_run("application-1", JOB, "ordered-run-second")

            # The first random ID sorts after the second, and both timestamps are
            # identical.  Persisted insertion order must still identify the second run.
            assert service.get_latest_application_run("application-1")["run_id"] == (
                second["run_id"]
            )
            assert [run["run_id"] for run in service.list_runs()] == [
                second["run_id"],
                first["run_id"],
            ]
            assert service.create_run(
                "application-1", JOB, "ordered-run-second"
            )["run_id"] == second["run_id"]

            with connect(path) as connection:
                persisted = connection.execute(
                    "SELECT run_id,run_seq FROM resume_run_order ORDER BY run_seq"
                ).fetchall()
                assert [row["run_id"] for row in persisted] == [
                    first["run_id"],
                    second["run_id"],
                ]
                connection.execute("DROP TRIGGER resume_run_order_no_update")
                connection.execute("DROP TRIGGER resume_run_order_no_delete")
                connection.execute("DELETE FROM resume_run_order")

            # Opening an older/partially upgraded sidecar deterministically backfills
            # its missing sequence from resume_runs insertion order.
            upgraded = ResumeLabService(path)
            with connect(path) as connection:
                backfilled = connection.execute(
                    "SELECT run_id,run_seq FROM resume_run_order ORDER BY run_seq"
                ).fetchall()
            assert [row["run_id"] for row in backfilled] == [
                first["run_id"],
                second["run_id"],
            ]
            assert int(backfilled[0]["run_seq"]) < int(backfilled[1]["run_seq"])

            for statement in (
                "UPDATE resume_run_order SET run_seq=run_seq+100 WHERE run_id=?",
                "DELETE FROM resume_run_order WHERE run_id=?",
            ):
                try:
                    with connect(path) as connection:
                        connection.execute(statement, (first["run_id"],))
                except sqlite3.IntegrityError as exc:
                    assert "immutable" in str(exc)
                else:
                    raise AssertionError("persisted resume run order was changed")

            third = upgraded.create_run(
                "application-1", JOB, "ordered-run-third"
            )
            assert upgraded.get_latest_application_run("application-1")[
                "run_id"
            ] == third["run_id"]
            assert [run["run_id"] for run in upgraded.list_runs(("queued",))] == [
                third["run_id"],
                second["run_id"],
                first["run_id"],
            ]
    finally:
        store_module._now = original_now
        store_module._new_id = original_new_id


def main() -> None:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume-lab store tests)")


if __name__ == "__main__":
    main()
