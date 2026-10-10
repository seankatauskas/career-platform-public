"""Command invariants exercised through the real SQLite transaction boundary."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from job_search.commands import CommandContext, CommandExecutor, Delegation, DomainError, Principal, digest


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.executor = CommandExecutor(Path(self.tmp.name)/"candidate.db", {
            "applications":"CREATE TABLE app_notes(id TEXT PRIMARY KEY,text TEXT);",
            "correspondence":"CREATE TABLE corr_messages(id TEXT PRIMARY KEY,text TEXT);",
        },clock=lambda:"2026-10-09T12:00:00Z")
        self.human = Principal("human-1","human",frozenset({"*"}))

    def ctx(self,key="one",principal=None,origin="direct",delegation=None):
        return CommandContext(principal or self.human,key,origin,delegation)

    def write(self,tx,text="exact e\u0301"):
        with tx.scope("applications"):
            tx.connection.execute("INSERT INTO app_notes VALUES('one',?)",(text,))
            tx.record("applications","one","add_note",None,{"text":text})
            tx.enqueue("applications","note_added","one",{"id":"one"})
        return {"text":text}

    def test_replay_concurrent_and_payload_conflict(self):
        def invoke(_):
            return self.executor.run(self.ctx(),"add_note",{"text":"exact"},self.write)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(invoke,range(8)))
        self.assertEqual(results,[results[0]]*8)
        with self.assertRaises(DomainError) as caught:
            self.executor.run(self.ctx(),"add_note",{"text":"different"},self.write)
        self.assertEqual(caught.exception.code,"idempotency_conflict")
        with self.executor.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM command_history").fetchone()[0],1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM command_work").fetchone()[0],1)

    def test_response_encoding_rolls_back_everything(self):
        def broken(tx):
            self.write(tx)
            return {"bad":float("nan")}
        with self.assertRaises(ValueError):
            self.executor.run(self.ctx(),"add_note",{},broken)
        with self.executor.read() as con:
            for table in ("app_notes","command_receipts","command_history","command_work"):
                self.assertEqual(con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0],0)

    def test_non_owner_write_denied_even_after_statement_was_cached(self):
        def wrong(tx):
            with tx.scope("applications"):
                tx.connection.execute("INSERT INTO app_notes VALUES('one','text')")
            with tx.scope("correspondence"):
                tx.connection.execute("INSERT INTO app_notes VALUES('one','text')")
        with self.assertRaises(sqlite3.DatabaseError):
            self.executor.run(self.ctx(),"add_note",{},wrong)

    def test_json_table_reads_work_without_relaxing_owner_or_schema_authorization(self):
        # Linux SQLite initializes json_each lazily with internal schema writes.
        # Each run opens a new connection, so initialization must be per run.
        payload = '[{"source_id":"email-1","revision":"révision-1"}]'
        def query(tx):
            rows = tx.connection.execute("""SELECT json_extract(value,'$.source_id'),
                json_extract(value,'$.revision') FROM json_each(?)""", (payload,)).fetchall()
            with tx.scope("applications"):
                with self.assertRaises(sqlite3.DatabaseError):
                    tx.connection.execute("INSERT INTO corr_messages VALUES('wrong','owner')")
                with self.assertRaises(sqlite3.DatabaseError):
                    tx.connection.execute("CREATE TABLE injected(value TEXT)")
                tx.connection.execute("PRAGMA writable_schema=ON")
                try:
                    with self.assertRaises(sqlite3.DatabaseError):
                        tx.connection.execute("UPDATE sqlite_master SET sql=sql WHERE 0")
                finally:
                    tx.connection.execute("PRAGMA writable_schema=OFF")
            return {"sources": [list(row) for row in rows]}
        for key in ("json-first", "json-second"):
            result = self.executor.run(self.ctx(key),"add_note",{},query)
            self.assertEqual(result,{"sources":[["email-1","révision-1"]]})
        with self.executor.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM corr_messages").fetchone()[0],0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('injected','json_each')").fetchone()[0],0)

    def test_inferred_and_agent_changes_cannot_bypass_review(self):
        agent = Principal("agent","agent",frozenset({"add_note","authorize_action"}))
        for context,operation in ((self.ctx(origin="inferred"),"add_note"),
                (self.ctx(principal=agent),"add_note"),(self.ctx(principal=agent),"authorize_action")):
            with self.assertRaises(DomainError):
                self.executor.run(context,operation,{},self.write)

    def test_exact_delegation_preserves_unicode(self):
        agent = Principal("agent","agent",frozenset({"add_note"}))
        payload = {"text":"exact e\u0301"}
        grant = Delegation("human-1","agent","add_note",digest(payload),"2026-10-09T13:00:00Z")
        result = self.executor.run(self.ctx(principal=agent,delegation=grant),"add_note",payload,self.write)
        self.assertEqual(result,payload)
        self.assertNotEqual(digest(payload),digest({"text":"exact é"}))
        with self.assertRaises(DomainError):
            self.executor.run(self.ctx("two",agent,delegation=grant),"add_note",{"text":"changed"},self.write)

    def test_read_is_side_effect_free_and_legacy_database_rejected(self):
        with self.executor.read() as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO app_notes VALUES('read','no')")
        old=Path(self.tmp.name)/"legacy.db"
        with sqlite3.connect(old) as con:
            con.execute("CREATE TABLE applications(id TEXT)")
        with self.assertRaises(DomainError):
            CommandExecutor(old,{})


if __name__ == "__main__":
    unittest.main()
