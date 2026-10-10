"""Offline regressions for binding the reviewed snapshot and preserving WAL data."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from job_search.application_installation import freeze_legacy, readiness, require_backend
from job_search.application_migration import CANDIDATE_NAME, convert_snapshot
from job_search.application_runtime import ApplicationRuntime
from job_search.aws_ops import copy_snapshot
from job_search.commands import DomainError
from job_search.db import connect
from job_search.runtime import RuntimeConfigV1
from tests.test_job_search_ledger import make_service, start


class TransitionGuardsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.operational, ledger = make_service(self.temporary.name)
        start(ledger)
        self.report = convert_snapshot(self.operational, self.root / "converted")
        self.candidate = self.root / "converted" / CANDIDATE_NAME
        self.runtime = ApplicationRuntime(self.candidate)
        self.config = replace(RuntimeConfigV1.defaults(self.root),
            application_db=self.operational, application_backend="owners",
            application_owner_db=self.candidate)

    def bind(self):
        return freeze_legacy(self.operational, self.runtime,
            operator="fictional-test-operator", report=self.report)

    def replace_candidate_with_empty_database(self):
        replacement = self.root / "empty.sqlite"
        ApplicationRuntime(replacement)
        os.replace(replacement, self.candidate)
        self.runtime = ApplicationRuntime(self.candidate)

    def assert_legacy_unfenced(self):
        require_backend(replace(self.config, application_backend="legacy"))
        with closing(connect(self.operational)) as con:
            con.execute("UPDATE applications SET updated_at=updated_at")
            con.commit()

    def test_empty_replacement_cannot_bind_using_the_original_conversion_report(self):
        self.replace_candidate_with_empty_database()
        with self.assertRaises(DomainError):
            self.bind()
        self.assert_legacy_unfenced()

    def test_bound_database_replacement_at_same_path_fails_startup_and_readiness(self):
        self.bind()
        require_backend(self.config)
        self.replace_candidate_with_empty_database()
        with self.assertRaises(DomainError):
            require_backend(self.config)
        with self.assertRaises(DomainError):
            readiness(self.config, self.runtime)

    def test_changed_domain_state_cannot_bind_even_with_an_exact_original_manifest(self):
        # A candidate can be edited during rehearsal; binding must compare its
        # actual domain contents, not only the untouched conversion report.
        with sqlite3.connect(self.candidate) as con:
            con.execute("UPDATE app_applications SET version=version+1")
        with self.assertRaises(DomainError):
            self.bind()
        self.assert_legacy_unfenced()

    def test_readonly_ordinary_database_preserves_committed_wal_rows(self):
        source = self.root / "readonly-source"
        source.mkdir()
        database = source / "ordinary.sqlite"
        with closing(sqlite3.connect(database)) as live:
            self.assertEqual(live.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            live.execute("CREATE TABLE observations(value TEXT)")
            live.commit()
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            live.execute("INSERT INTO observations VALUES('committed evidence')")
            live.commit()
            self.assertGreater(Path(str(database) + "-wal").stat().st_size, 0)
            database.chmod(0o400)
            copy_snapshot(source, self.root / "restored")
            with closing(sqlite3.connect(self.root / "restored" / database.name)) as restored:
                self.assertEqual(restored.execute("SELECT value FROM observations").fetchall(),
                    [("committed evidence",)])
                self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
