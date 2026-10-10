"""One local transaction, receipt and durable handoffs per internal command."""
from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping
import uuid

from .authorization import authorize
from .context import DomainError


SCHEMA = """
CREATE TABLE IF NOT EXISTS command_receipts (
 principal TEXT NOT NULL, operation TEXT NOT NULL, command_key TEXT NOT NULL,
 digest TEXT NOT NULL, result TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(principal, operation, command_key));
CREATE TABLE IF NOT EXISTS command_history (
 id INTEGER PRIMARY KEY, owner TEXT NOT NULL, record_id TEXT NOT NULL,
 operation TEXT NOT NULL, actor_id TEXT NOT NULL, origin TEXT NOT NULL,
 command_key TEXT NOT NULL, causation_id TEXT, before_json TEXT, after_json TEXT,
 recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS command_work (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', recorded_at TEXT NOT NULL,
 UNIQUE(owner,kind,dedupe_key));
CREATE TABLE IF NOT EXISTS command_installation (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), paused INTEGER NOT NULL CHECK(paused=1));
INSERT OR IGNORE INTO command_installation VALUES(1,1);
CREATE TRIGGER IF NOT EXISTS command_history_no_update BEFORE UPDATE ON command_history
BEGIN SELECT RAISE(ABORT,'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS command_history_no_delete BEFORE DELETE ON command_history
BEGIN SELECT RAISE(ABORT,'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS command_receipts_no_update BEFORE UPDATE ON command_receipts
BEGIN SELECT RAISE(ABORT,'receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS command_receipts_no_delete BEFORE DELETE ON command_receipts
BEGIN SELECT RAISE(ABORT,'receipts are immutable'); END;
"""


def encode(value):
    def default(item):
        if is_dataclass(item):
            return asdict(item)
        raise TypeError("Command values must be JSON serializable")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=default)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Transaction:
    def __init__(self, connection, context, now, owners):
        self.connection, self.context, self.now = connection, context, now
        self._owners, self._scope = owners, None

    def restore_quarantined(self, kind, record_id):
        from .installation import restore_quarantined
        return restore_quarantined(self.connection, kind, record_id)

    @contextmanager
    def scope(self, owner):
        if owner not in set(self._owners.values()):
            raise DomainError("invalid_input", "Unknown transaction owner")
        previous = self._scope
        self._scope = owner
        try:
            yield self
        finally:
            self._scope = previous

    def _authorize_sql(self, action, table, column, database, trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            if table == "sqlite_sequence":
                return sqlite3.SQLITE_OK
            if self._owners.get(table) != self._scope or self._scope is None:
                return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_ALTER_TABLE,
                      sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_CREATE_TABLE,
                      sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def record(self, owner, record_id, operation, before, after):
        if owner != self._scope:
            raise DomainError("not_authorized", "History must be written by its record owner")
        with self.scope("commands"):
            self.connection.execute("""INSERT INTO command_history
                (owner,record_id,operation,actor_id,origin,command_key,causation_id,before_json,after_json,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (owner,record_id,operation,self.context.principal.actor_id,
                self.context.origin,self.context.idempotency_key,self.context.causation_id,
                None if before is None else encode(before),None if after is None else encode(after),self.now))

    def enqueue(self, owner, kind, key, payload):
        if owner != self._scope:
            raise DomainError("not_authorized", "Work must be enqueued by its owner")
        with self.scope("commands"):
            work_id = "work_" + uuid.uuid4().hex
            encoded = encode(payload)
            old = self.connection.execute("SELECT id,payload FROM command_work WHERE owner=? AND kind=? AND dedupe_key=?",
                                          (owner,kind,key)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise DomainError("idempotency_conflict", "Work identity has different content")
                return old["id"]
            self.connection.execute("INSERT INTO command_work(id,owner,kind,dedupe_key,payload,recorded_at) VALUES(?,?,?,?,?,?)",
                                    (work_id,owner,kind,key,encoded,self.now))
            return work_id

    def finish_work(self, work_id):
        with self.scope("commands"):
            self.connection.execute("UPDATE command_work SET status='done' WHERE id=?", (work_id,))


class CommandExecutor:
    def __init__(self, path, schemas: Mapping[str,str], clock=None, schema_migrations=None):
        self.path = Path(path)
        self.clock = clock or utc_now
        from .installation import SCHEMA as CONTROL_SCHEMA, SCHEMA_MIGRATIONS as CONTROL_MIGRATIONS
        schema_migrations={**CONTROL_MIGRATIONS,**(schema_migrations or {})}
        self.schemas = {"commands": SCHEMA, "installation": CONTROL_SCHEMA, **schemas}
        if "installation" in schemas:
            raise DomainError("invalid_input", "Installation control cannot be replaced")
        self.owners = {}
        for owner, sql in self.schemas.items():
            for table in re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?(\w+)",sql,re.I):
                if table in self.owners:
                    raise DomainError("invalid_input", "A table has two owners")
                self.owners[table] = owner
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self._connect()) as con:
            existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and "command_schema_versions" not in existing:
                raise DomainError("invalid_input", "Use a separate candidate database; convert legacy state offline")
            con.execute("CREATE TABLE IF NOT EXISTS command_schema_versions(owner TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
            for owner, sql in self.schemas.items():
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                applied = con.execute("SELECT checksum FROM command_schema_versions WHERE owner=?",(owner,)).fetchone()
                if applied:
                    if applied[0] != checksum:
                        migration = (schema_migrations or {}).get((owner, applied[0], checksum))
                        if migration is None:
                            raise DomainError("version_conflict", "Unknown owner schema migration")
                        # Explicit, checksum-bound upgrades only; rollback covers DDL and registry.
                        con.execute("BEGIN IMMEDIATE")
                        try:
                            for statement in migration:
                                con.execute(statement)
                            con.execute("UPDATE command_schema_versions SET checksum=? WHERE owner=?", (checksum, owner))
                            con.commit()
                        except Exception:
                            con.rollback()
                            raise
                    continue
                con.executescript(sql)
                con.execute("INSERT INTO command_schema_versions VALUES(?,?)",(owner,checksum))
            con.commit()
            self._installation_id=self.installation_identity(con)

    def _connect(self, readonly=False):
        if readonly:
            con = sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro",uri=True,isolation_level=None)
            con.execute("PRAGMA query_only=ON")
        else:
            # Disable statement caching: an authorization check must run in each owner scope.
            con = sqlite3.connect(str(self.path),timeout=30,isolation_level=None,cached_statements=0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        if hasattr(self,"_installation_id"):
            try:
                identity=con.execute("SELECT identity FROM installation_identity WHERE singleton=1").fetchone()
                if identity is None or identity[0]!=self._installation_id:
                    raise DomainError("version_conflict","Application database identity changed")
            except Exception:
                con.close()
                raise
        return con

    @contextmanager
    def read(self):
        con = self._connect(readonly=True)
        try:
            con.execute("BEGIN")
            yield con
        finally:
            con.rollback()
            con.close()

    def run(self, context, operation, input_mapping, callback):
        fingerprint = digest({"input":input_mapping,"origin":context.origin})
        now = self.clock()
        authorize(context,operation,digest(input_mapping),now)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            tx = Transaction(con,context,now,self.owners)
            tx.authorized_operation = operation
            # SQLite lazily constructs this built-in virtual table per connection.
            # Some builds authorize internal schema writes during construction;
            # initialize before the owner guard, without allowing schema writes
            # from command callbacks. No persistent schema or data is created.
            con.execute("SELECT value FROM json_each('[]')").fetchall()
            con.set_authorizer(tx._authorize_sql)
            prior = con.execute("SELECT digest,result FROM command_receipts WHERE principal=? AND operation=? AND command_key=?",
                                (context.principal.actor_id,operation,context.idempotency_key)).fetchone()
            if prior:
                if prior["digest"] != fingerprint:
                    raise DomainError("idempotency_conflict", "Command identity was already used for different input")
                con.rollback()
                return json.loads(prior["result"])
            result = callback(tx)
            encoded = encode(result)
            with tx.scope("commands"):
                con.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?)",(context.principal.actor_id,
                    operation,context.idempotency_key,fingerprint,encoded,now))
            con.commit()
            return json.loads(encoded)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def installation_identity(self, connection=None):
        if connection is not None:
            return connection.execute("SELECT identity FROM installation_identity WHERE singleton=1").fetchone()[0]
        with self.read() as con:
            return self.installation_identity(con)

    def activation_status(self, connection=None):
        if connection is not None:
            row = connection.execute("SELECT paused,revision FROM installation_control WHERE singleton=1").fetchone()
            return {"paused": bool(row["paused"]), "revision": row["revision"]}
        with self.read() as con:
            return self.activation_status(con)

    def activation_guard(self, tx, expected_revision):
        from .installation import restore_required
        state = self.activation_status(tx.connection)
        return not state["paused"] and state["revision"] == expected_revision and restore_required(tx.connection) is None

    @staticmethod
    def restore_quarantined(connection, kind, record_id):
        from .installation import restore_quarantined
        return restore_quarantined(connection, kind, record_id)

    def restore_status(self, connection=None, *, after=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100 or (after is not None and
                (not isinstance(after, (list, tuple)) or len(after) != 2 or not all(isinstance(x, str) for x in after))):
            raise DomainError("invalid_input", "Invalid restore review page")
        if connection is None:
            with self.read() as con:
                return self.restore_status(con, after=after, limit=limit)
        from .installation import restore_required
        pending = restore_required(connection)
        latest = connection.execute("SELECT MAX(revision) FROM installation_restores").fetchone()[0]
        rows = connection.execute("""SELECT kind,record_id,restore_revision FROM installation_restore_quarantine
            WHERE (kind,record_id)>(?,?) ORDER BY kind,record_id LIMIT ?""", (*(after or ("", "")), limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"required": pending is not None, "revision": latest,
                "quarantined": dict(connection.execute("SELECT kind,COUNT(*) FROM installation_restore_quarantine GROUP BY kind")),
                "items": items, "next_cursor": [items[-1]["kind"], items[-1]["record_id"]] if len(rows) > limit else None}

    def acknowledge_restore(self, *, expected_restore_revision, operator, reason):
        """Operator review only; never releases quarantined work or enables dispatch."""
        if type(expected_restore_revision) is not int or not isinstance(operator, str) or not operator.strip() or not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid_input", "An explicit restore review is required")
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                state = self.restore_status(con)
                if not state["required"] or state["revision"] != expected_restore_revision:
                    raise DomainError("version_conflict", "Restore review is stale or already acknowledged")
                # Acknowledging the latest restore covers earlier unresolved
                # restores too; immutable quarantine entries remain in force.
                con.execute("""INSERT INTO installation_restore_reviews
                    SELECT r.revision,?,?,? FROM installation_restores r
                    LEFT JOIN installation_restore_reviews v ON v.revision=r.revision WHERE v.revision IS NULL""",
                    (operator, reason, self.clock()))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.restore_status()

    def set_activation(self, *, paused, expected_revision, operator, reason):
        """Operator composition only. Never registered as an application command/tool."""
        if type(paused) is not bool or type(expected_revision) is not int or not operator or not reason:
            raise DomainError("invalid_input", "Explicit activation decision required")
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                state = self.activation_status(con)
                from .installation import restore_required
                if not paused and restore_required(con) is not None:
                    raise DomainError("needs_reconciliation", "Review restored external work before activating; restored work remains quarantined")
                if state["revision"] != expected_revision:
                    raise DomainError("version_conflict", "Activation decision is stale")
                revision = expected_revision + 1
                con.execute("UPDATE installation_control SET paused=?,revision=? WHERE singleton=1", (int(paused), revision))
                con.execute("INSERT INTO installation_decisions VALUES(?,?,?,?,?)", (revision,int(paused),operator,reason,self.clock()))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return {"paused": paused, "revision": revision}

    def complete_work(self, work_id, *, owner, kind):
        """Trusted queue bridge acknowledgment; business effects commit before this call."""
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT owner,kind FROM command_work WHERE id=?",(work_id,)).fetchone()
            if row is None or row["owner"] != owner or row["kind"] != kind:
                con.rollback()
                raise DomainError("invalid_input", "Work identity mismatch")
            con.execute("UPDATE command_work SET status='done' WHERE id=?",(work_id,))
            con.commit()

    def history(self, connection, record_ids, *, after=0, limit=100):
        if not isinstance(limit,int) or not 1 <= limit <= 100 or not isinstance(after,int) or after < 0:
            raise DomainError("invalid_input", "Invalid history pagination")
        ids = list(record_ids)
        if not ids or len(ids)>200:
            return {"items":[],"next_after":None}
        rows=connection.execute("SELECT * FROM command_history WHERE record_id IN ("+
            ",".join("?" for _ in ids)+") AND id>? ORDER BY id LIMIT ?",(*ids,after,limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            item=dict(row)
            for key in ("before_json","after_json"):
                item[key.removesuffix("_json")]=json.loads(item.pop(key)) if row[key] is not None else None
            items.append(item)
        return {"items":items,"next_after":items[-1]["id"] if len(rows)>limit else None}

    def pending_work(self, connection, *, owner=None, kind=None, limit=100):
        if not isinstance(limit,int) or not 1<=limit<=100:
            raise DomainError("invalid_input","Invalid work limit")
        conditions=["status='pending'"]
        values=[]
        for key,value in (("owner",owner),("kind",kind)):
            if value is not None:
                conditions.append(key+"=?")
                values.append(value)
        rows=connection.execute("SELECT * FROM command_work WHERE "+" AND ".join(conditions)+
            " ORDER BY recorded_at,id LIMIT ?",(*values,limit)).fetchall()
        return [{**dict(row),"payload":json.loads(row["payload"])} for row in rows]

    def work_page(self, connection, *, owner=None, kind=None, limit=100, after=""):
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after,str):
            raise DomainError("invalid_input", "Invalid work pagination")
        conditions, values = ["status='pending'", "id>?"], [after]
        for key,value in (("owner",owner),("kind",kind)):
            if value is not None:
                conditions.append(key+"=?")
                values.append(value)
        rows = connection.execute("SELECT * FROM command_work WHERE "+" AND ".join(conditions)+
            " ORDER BY id LIMIT ?", (*values,limit+1)).fetchall()
        items = [{**dict(row),"payload":json.loads(row["payload"])} for row in rows[:limit]]
        return {"items":items,"next_cursor":items[-1]["id"] if len(rows)>limit else None}

    def find_work(self,connection,owner,kind,key):
        row=connection.execute("SELECT * FROM command_work WHERE owner=? AND kind=? AND dedupe_key=?",
                               (owner,kind,key)).fetchone()
        return dict(row) if row else None
