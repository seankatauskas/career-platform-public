"""Persisted installation control, separate from business commands and test marker."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS installation_control (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 paused INTEGER NOT NULL CHECK(paused IN (0,1)), revision INTEGER NOT NULL);
INSERT OR IGNORE INTO installation_control VALUES(1,1,0);
CREATE TABLE IF NOT EXISTS installation_decisions (
 revision INTEGER PRIMARY KEY, paused INTEGER NOT NULL,
 operator TEXT NOT NULL, reason TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS installation_decisions_no_update BEFORE UPDATE ON installation_decisions
BEGIN SELECT RAISE(ABORT,'activation history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS installation_decisions_no_delete BEFORE DELETE ON installation_decisions
BEGIN SELECT RAISE(ABORT,'activation history is append-only'); END;
"""

from hashlib import sha256
_PRE_IDENTITY_SCHEMA = SCHEMA
_IDENTITY_SQL = """CREATE TABLE IF NOT EXISTS installation_identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL UNIQUE)"""
_SEED_IDENTITY_SQL = "INSERT OR IGNORE INTO installation_identity VALUES(1,lower(hex(randomblob(16))))"
SCHEMA += '\n' + _IDENTITY_SQL + ';\n' + _SEED_IDENTITY_SQL + ';\n'
SCHEMA_MIGRATIONS = {('installation',sha256(_PRE_IDENTITY_SCHEMA.encode()).hexdigest(),sha256(SCHEMA.encode()).hexdigest()):(_IDENTITY_SQL,_SEED_IDENTITY_SQL)}

_PRE_RESTORE_SCHEMA = SCHEMA
_RESTORE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS installation_restores(
      revision INTEGER PRIMARY KEY, restored_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS installation_restore_reviews(
      revision INTEGER PRIMARY KEY REFERENCES installation_restores(revision),
      operator TEXT NOT NULL, reason TEXT NOT NULL, reviewed_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS installation_restore_quarantine(
      kind TEXT NOT NULL, record_id TEXT NOT NULL,
      restore_revision INTEGER NOT NULL REFERENCES installation_restores(revision),
      PRIMARY KEY(kind,record_id))""",
) + tuple(
    "CREATE TRIGGER IF NOT EXISTS %s_no_%s BEFORE %s ON %s "
    "BEGIN SELECT RAISE(ABORT,'restore history is immutable'); END" % (table, operation.lower(), operation, table)
    for table in ("installation_restores", "installation_restore_reviews", "installation_restore_quarantine")
    for operation in ("UPDATE", "DELETE")
)
SCHEMA += "\n" + ";\n".join(_RESTORE_STATEMENTS) + ";\n"
SCHEMA_MIGRATIONS = {
    ("installation", sha256(_PRE_IDENTITY_SCHEMA.encode()).hexdigest(), sha256(SCHEMA.encode()).hexdigest()):
        (_IDENTITY_SQL, _SEED_IDENTITY_SQL) + _RESTORE_STATEMENTS,
    ("installation", sha256(_PRE_RESTORE_SCHEMA.encode()).hexdigest(), sha256(SCHEMA.encode()).hexdigest()):
        _RESTORE_STATEMENTS,
}


def record_restore(connection, *, restored_at, quarantined_records):
    """Offline operator hook; caller holds the restored database write transaction.

    Composition supplies owner record identities that must never automatically
    execute from this older snapshot. This function preserves their business
    state and approval history; review acknowledges uncertainty, not nonexecution.
    """
    if not connection.in_transaction:
        raise ValueError("Restore fencing requires an active database transaction")
    current = sha256(SCHEMA.encode()).hexdigest()
    prior = connection.execute("SELECT checksum FROM command_schema_versions WHERE owner='installation'").fetchone()
    if prior is None:
        raise ValueError("Restored installation schema is missing")
    if prior[0] != current:
        migration = SCHEMA_MIGRATIONS.get(("installation", prior[0], current))
        if migration is None:
            raise ValueError("Unknown restored installation schema")
        for statement in migration:
            connection.execute(statement)
        connection.execute("UPDATE command_schema_versions SET checksum=? WHERE owner='installation'", (current,))
    records = list(quarantined_records)
    if any(kind not in {"external_action", "reminder"} or not isinstance(identifier, str) or not identifier
           for kind, identifier in records):
        raise ValueError("Invalid restored work identity")
    revision = connection.execute("SELECT revision FROM installation_control WHERE singleton=1").fetchone()[0] + 1
    connection.execute("UPDATE installation_control SET paused=1,revision=? WHERE singleton=1", (revision,))
    connection.execute("INSERT INTO installation_decisions VALUES(?,1,'restore','restored installations require review',?)", (revision, restored_at))
    connection.execute("INSERT INTO installation_restores VALUES(?,?)", (revision, restored_at))
    connection.executemany("INSERT OR IGNORE INTO installation_restore_quarantine VALUES(?,?,?)",
        ((kind, identifier, revision) for kind, identifier in records))
    return revision


def restore_required(connection):
    return connection.execute("""SELECT r.revision FROM installation_restores r
        LEFT JOIN installation_restore_reviews v ON v.revision=r.revision
        WHERE v.revision IS NULL ORDER BY r.revision DESC LIMIT 1""").fetchone()


def restore_quarantined(connection, kind, record_id):
    return connection.execute("SELECT 1 FROM installation_restore_quarantine WHERE kind=? AND record_id=?",
                              (kind, record_id)).fetchone() is not None
