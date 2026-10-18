"""Production startup verifies existing state; maintenance alone migrates it."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import gc
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search import db
from job_search.application_runtime import ApplicationRuntime
from job_search.cli import main
from job_search.collection import boards
from job_search.runtime import RuntimeConfigV1, build_runtime
from job_search.schema_lifecycle import check_config, explicit_migration, migrate_config


class SchemaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = RuntimeConfigV1.defaults(self.root)
        migrate_config(self.config)
        gc.collect()
        self.mode = patch.dict(os.environ, {"JOB_SEARCH_SCHEMA_MODE": "require_current"})
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def state(self):
        gc.collect()
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(("-shm", "-wal"))}

    def test_check_and_constructors_leave_database_bytes_unchanged(self):
        before = self.state()
        self.assertEqual(check_config(self.config)["schema"], "ready")
        db.prepare_database(self.config.application_db, "unused")
        with closing(sqlite3.connect(self.config.jobs_db)) as con:
            for prepare in (boards._prepare, boards._prepare_board_registry, boards._prepare_etags):
                prepare(con)
        self.assertEqual(before, self.state())

    def test_missing_database_is_not_created(self):
        missing = self.root / "missing" / "db.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            db.prepare_database(missing, "unused")
        self.assertFalse(missing.parent.exists())

    def test_local_binding_probe_preserves_bootstrap_without_creating_state(self):
        from job_search.application_installation import binding
        missing = self.root / "uninitialized" / "ledger.db"
        with patch.dict(os.environ, {"JOB_SEARCH_SCHEMA_MODE": "migrate"}):
            self.assertIsNone(binding(missing))
        self.assertFalse(missing.parent.exists())
        with self.assertRaisesRegex(RuntimeError, "database is missing"):
            binding(missing)
        self.assertFalse(missing.parent.exists())

    def test_pending_migration_is_rejected_and_explicit_migration_repairs_it(self):
        # A genuinely older schema, rather than falsifying its ledger.
        old = self.root / "older.sqlite"
        with explicit_migration(), patch.object(db, "MIGRATIONS", db.MIGRATIONS[:-1]):
            db.prepare_database(old, "old")
        before = old.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "version or checksum"):
            db.prepare_database(old, "unused")
        self.assertEqual(before, old.read_bytes())
        with explicit_migration():
            db.prepare_database(old, "new")
        db.prepare_database(old, "unused")

    def test_newer_or_drifted_ledger_fails_closed(self):
        for sql in ("PRAGMA user_version=999", "UPDATE schema_migrations SET checksum='drift' WHERE version=1"):
            with self.subTest(sql=sql), closing(sqlite3.connect(self.config.application_db)) as con:
                con.execute(sql)
                con.commit()
                with self.assertRaisesRegex(RuntimeError, "version or checksum"):
                    check_config(self.config)

    def test_missing_audit_schema_and_changed_trigger_rejected(self):
        with closing(sqlite3.connect(self.config.application_db)) as con:
            con.execute("DROP TRIGGER inference_allowance_no_delete")
            con.commit()
        with self.assertRaisesRegex(RuntimeError, "missing trigger"):
            check_config(self.config)

    def test_missing_collector_index_is_not_created_at_runtime(self):
        with closing(sqlite3.connect(self.config.jobs_db)) as con:
            con.execute("DROP INDEX jobs_discovery_candidates")
            con.commit()
            with self.assertRaisesRegex(RuntimeError, "missing index"):
                boards._prepare(con)
        with self.assertRaisesRegex(RuntimeError, "missing index"):
            check_config(self.config)
        migrate_config(self.config)
        check_config(self.config)

    def test_current_owner_constructor_only_reads_and_commands_remain_writable(self):
        owner = self.root / "owner.sqlite"
        with explicit_migration():
            ApplicationRuntime(owner)
        before = owner.read_bytes()
        runtime = ApplicationRuntime(owner)
        self.assertEqual(before, owner.read_bytes())
        state = runtime.executor.set_activation(paused=False, expected_revision=0,
            operator="test", reason="explicit user activity")
        self.assertFalse(state["paused"])
        ApplicationRuntime(owner)

    def test_owner_schema_drift_and_unknown_owner_rejected_without_repair(self):
        owner = self.root / "owner.sqlite"
        with explicit_migration():
            ApplicationRuntime(owner)
        with closing(sqlite3.connect(owner)) as con:
            con.execute("UPDATE command_schema_versions SET checksum='unknown' WHERE owner='applications'")
            con.commit()
        before = owner.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "owner schema"):
            ApplicationRuntime(owner)
        self.assertEqual(before, owner.read_bytes())

    def test_real_converted_bound_owner_installation_can_start_strictly(self):
        from job_search.application_installation import freeze_legacy
        from job_search.application_migration import convert_snapshot, CANDIDATE_NAME
        with explicit_migration():
            report = convert_snapshot(self.config.application_db, self.root / "converted")
            owner = self.root / "converted" / CANDIDATE_NAME
            runtime = ApplicationRuntime(owner)
            self.assertTrue(report["ready_for_review"])
            freeze_legacy(self.config.application_db, runtime, operator="test", report=report)
        config = replace(self.config, application_backend="owners", application_owner_db=owner)
        migrate_config(config)  # Prepare the configured owner mail archive explicitly.
        check_config(config)
        ApplicationRuntime(owner)
        build_runtime(config)
        with closing(sqlite3.connect(owner)) as con:
            con.execute("UPDATE command_schema_versions SET checksum='unknown' WHERE owner='migration'")
            con.commit()
        with self.assertRaisesRegex(RuntimeError, "owner schema"):
            check_config(config)

    def owner_config(self):
        from job_search.application_installation import freeze_legacy
        from job_search.application_migration import convert_snapshot, CANDIDATE_NAME
        with explicit_migration():
            report = convert_snapshot(self.config.application_db, self.root / "converted")
            owner = self.root / "converted" / CANDIDATE_NAME
            runtime = ApplicationRuntime(owner)
            freeze_legacy(self.config.application_db, runtime, operator="test", report=report)
        config = replace(self.config, application_backend="owners", application_owner_db=owner)
        migrate_config(config)
        return config

    def test_owner_startup_with_different_clocks_does_not_reseed_or_update_schedules(self):
        config = self.owner_config()
        before = self.state()
        with closing(sqlite3.connect(config.application_db)) as con:
            schedules = list(con.execute('SELECT * FROM schedule_specs ORDER BY schedule_key'))
        real_connect = sqlite3.connect
        writes = []
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
                  sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_INDEX,
                  sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_CREATE_TRIGGER,
                  sqlite3.SQLITE_DROP_TRIGGER}
        def guarded_connect(database, *args, **kwargs):
            connection = real_connect(database, *args, **kwargs)
            if str(database) != ':memory:':
                def authorize(action, first, second, db_name, trigger):
                    if action in denied:
                        writes.append((action, first, second))
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
            return connection
        with patch('job_search.application_production._seed', side_effect=AssertionError('startup seeded owners')), \
                patch('sqlite3.connect', side_effect=guarded_connect):
            for lane, year in (('core', 2030), ('model', 2040), ('core', 2050)):
                build_runtime(config, lane=lane, now_provider=lambda year=year: datetime(year, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(writes, [])
        with closing(sqlite3.connect(config.application_db)) as con:
            self.assertEqual(schedules, list(con.execute('SELECT * FROM schedule_specs ORDER BY schedule_key')))
        self.assertEqual(before, self.state())

    def test_missing_owner_schedule_fails_without_repair_until_explicit_migration(self):
        config = self.owner_config()
        with closing(sqlite3.connect(config.application_db)) as con, con:
            con.execute("DELETE FROM schedule_specs WHERE schedule_key='applications.tick'")
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'owner schedules'):
            check_config(config)
        with self.assertRaisesRegex(RuntimeError, 'owner schedules'):
            build_runtime(config)
        self.assertEqual(before, self.state())
        migrate_config(config)
        check_config(config)
        build_runtime(config)

    def test_missing_schedules_reject_startup_and_maintenance_seeds_them(self):
        with closing(sqlite3.connect(self.config.application_db)) as con:
            con.execute("DELETE FROM schedule_specs")
            con.commit()
        with self.assertRaisesRegex(RuntimeError, "schedules"):
            build_runtime(self.config)
        migrate_config(self.config)
        with patch("job_search.runtime.seed_default_schedules", side_effect=AssertionError("startup seeded schedules")):
            runtime = build_runtime(self.config)
        self.assertEqual(runtime.schedules["seeded"], 0)

    def test_explicit_cli_migration_overrides_strict_mode_and_check_is_read_only(self):
        root = self.root / "cli"
        root.mkdir()
        config = root / 'config.json'
        config.write_text(json.dumps({'version': 1, 'project_root': str(root)}))
        config.chmod(0o600)
        arguments = ['--config', str(config), '--project-root', str(root)]
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main([*arguments, "migrate"]), 0)
        before = self.state()
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main([*arguments, "check-schema"]), 0)
        self.assertIn("require-current-v2", output.getvalue())
        self.assertEqual(before, self.state())

    def test_invalid_mode_fails_closed(self):
        with patch.dict(os.environ, {"JOB_SEARCH_SCHEMA_MODE": "require_curent"}):
            with self.assertRaisesRegex(RuntimeError, "Unknown"):
                db.prepare_database(self.config.application_db, "unused")

    def test_offline_check_does_not_create_sidecars(self):
        # Model the host's stopped/checkpointed writer boundary explicitly.
        # A last read-only connection cannot checkpoint a preceding writer's WAL.
        for path in (self.config.application_db, self.config.jobs_db):
            with closing(sqlite3.connect(path)) as con:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        check_config(self.config, offline=True)
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_live_wal_is_read_live_but_rejected_offline(self):
        with closing(sqlite3.connect(self.config.application_db)) as writer:
            writer.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=1")
            writer.commit()
            with self.assertRaisesRegex(RuntimeError, "version or checksum"):
                check_config(self.config)
            with self.assertRaisesRegex(RuntimeError, "checkpointed"):
                check_config(self.config, offline=True)

    def test_offline_rejects_journal_and_nonregular_sidecars(self):
        # Sidecar safety must be checked before opening SQLite at all. A plain
        # file makes that ordering observable and avoids platform-dependent WAL
        # cleanup in the fault fixture; no real journals are deleted.
        standalone = self.root / "standalone.db"
        standalone.write_bytes(b"must reject the unsafe sidecar before SQLite open")
        config = replace(self.config, application_db=standalone)
        for suffix, kind in (("-journal", "file"), ("-wal", "directory"), ("-shm", "symlink")):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(standalone) + suffix)
                if kind == "file":
                    sidecar.write_bytes(b"journal")
                elif kind == "directory":
                    sidecar.mkdir()
                else:
                    sidecar.symlink_to(self.root / "absent")
                try:
                    with self.assertRaisesRegex(RuntimeError, "checkpointed"):
                        check_config(config, offline=True)
                finally:
                    sidecar.rmdir() if kind == "directory" else sidecar.unlink()


if __name__ == "__main__":
    unittest.main()
