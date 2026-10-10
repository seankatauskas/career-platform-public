"""Legacy and replacement writers cannot share an installation database."""
import hashlib
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from job_search.application_runtime import ApplicationRuntime
from job_search.commands import DomainError
from job_search.db import connect
from job_search.service import JobSearchLedger
from tests.test_job_search_ledger import start


def snapshot(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        schema = con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        counts = {name: con.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for kind, name, _, _ in schema if kind == "table"}
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
    return schema, counts, journal, stat.S_IMODE(path.stat().st_mode), hashlib.sha256(path.read_bytes()).hexdigest()


class IsolationTest(unittest.TestCase):
    def test_legacy_ledger_and_direct_connections_reject_candidate_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.db"
            ApplicationRuntime(path)
            path.chmod(0o640)  # Failed legacy opening must not even change permissions.
            before = snapshot(path)
            with self.assertRaisesRegex(RuntimeError, "candidate database"):
                JobSearchLedger(path)
            self.assertEqual(snapshot(path), before)
            with self.assertRaisesRegex(RuntimeError, "candidate database"):
                connect(path)
            self.assertEqual(snapshot(path), before)
            self.assertFalse(Path(str(path) + "-wal").exists())

    def test_candidate_rejects_legacy_database_without_changing_schema_or_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            ledger = JobSearchLedger(path)
            start(ledger)
            before = snapshot(path)
            with self.assertRaises(DomainError):
                ApplicationRuntime(path)
            self.assertEqual(snapshot(path), before)
            self.assertEqual(len(ledger.list_applications()), 1)

    def test_existing_legacy_connection_can_keep_its_write_lock(self):
        # The format guard uses the SQLite connection itself, not a raw file
        # descriptor whose close would drop another connection's POSIX locks.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            JobSearchLedger(path)
            first, second = connect(path), None
            try:
                first.execute("BEGIN IMMEDIATE")
                second = connect(path)
                second.execute("PRAGMA busy_timeout=0")
                with self.assertRaises(sqlite3.OperationalError):
                    second.execute("BEGIN IMMEDIATE")
            finally:
                first.rollback()
                first.close()
                if second:
                    second.close()


if __name__ == "__main__":
    unittest.main()
