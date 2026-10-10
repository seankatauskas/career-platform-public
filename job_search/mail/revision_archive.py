"""Immutable, account-scoped encrypted evidence for the application owners.

The operational database retains ciphertext; application owners retain opaque
references and hashes only. Legacy mutable archives keep their existing contract.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from job_search.commands import DomainError, encode
from job_search.contracts import utc_now
from job_search.db import connect
from .archive import EncryptedMailArchive, MAX_ARCHIVE_CHARS


SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_mail_archive (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, archive_ref TEXT NOT NULL UNIQUE,
 account_id TEXT NOT NULL, provider_message_id TEXT NOT NULL,
 source_version TEXT NOT NULL, modified_at TEXT, key_id TEXT NOT NULL,
 nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, aad_sha256 TEXT NOT NULL,
 sealed_sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 content_chars INTEGER NOT NULL, coverage TEXT NOT NULL, recorded_at TEXT NOT NULL,
 UNIQUE(account_id,provider_message_id,source_version));
CREATE TABLE IF NOT EXISTS owner_mail_archive_current (
 account_id TEXT NOT NULL, provider_message_id TEXT NOT NULL,
 archive_ref TEXT NOT NULL REFERENCES owner_mail_archive(archive_ref),
 PRIMARY KEY(account_id,provider_message_id));
CREATE TRIGGER IF NOT EXISTS owner_mail_archive_immutable
 BEFORE UPDATE ON owner_mail_archive BEGIN SELECT RAISE(ABORT,'mail revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS owner_mail_archive_no_delete
 BEFORE DELETE ON owner_mail_archive BEGIN SELECT RAISE(ABORT,'mail revision is immutable'); END;
"""


def _instant(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("invalid_input", "Mail modification time requires a timezone") from exc


class ImmutableMailArchive(EncryptedMailArchive):
    def __init__(self, db_path, key_provider, *, predecessor_path=None, **options):
        super().__init__(None, key_provider, **options)
        self.db_path = Path(db_path)
        # The composition verifies this frozen snapshot against its conversion
        # report. Reads below never initialize or migrate the predecessor schema.
        self.predecessor_path = Path(predecessor_path).resolve() if predecessor_path is not None else None
        with connect(self.db_path) as con:
            con.executescript(SCHEMA)

    @staticmethod
    def _aad(account_id, provider_message_id, source_version):
        return {"version": 1, "kind": "application_mail_revision", "account_id": account_id,
                "provider_message_id": provider_message_id, "source_version": source_version}

    def archive_message(self, *, account_id, provider_message_id, source_version, text,
                        coverage, metadata=None, modified_at=None):
        for value in (account_id, provider_message_id, source_version):
            if not isinstance(value, str) or not 1 <= len(value) <= 2048:
                raise DomainError("invalid_input", "Mail archive identity is invalid")
        if not isinstance(text, str) or not text or len(text) > MAX_ARCHIVE_CHARS:
            raise DomainError("invalid_input", "Mail archive text exceeds its bound")
        if not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool:
            raise DomainError("invalid_input", "Mail archive coverage is required")
        modified = _instant(modified_at)
        body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Private routing metadata is encrypted alongside text, never in command receipts.
        plaintext = encode({"text": text, "metadata": metadata or {}})
        aad = self._aad(account_id, provider_message_id, source_version)
        sealed = self._seal(plaintext, aad, MAX_ARCHIVE_CHARS * 2)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM owner_mail_archive WHERE account_id=? AND provider_message_id=? AND source_version=?",
                              (account_id, provider_message_id, source_version)).fetchone()
            if old:
                if old["sealed_sha256"] != sealed.plaintext_sha256 or old["coverage"] != encode(coverage) or old["modified_at"] != modified_at:
                    raise DomainError("idempotency_conflict", "Mail source revision changed")
                return self._public(con, old)
            current = con.execute("SELECT a.* FROM owner_mail_archive_current c JOIN owner_mail_archive a USING(archive_ref) WHERE c.account_id=? AND c.provider_message_id=?",
                                  (account_id, provider_message_id)).fetchone()
            reference = "mailrev_" + uuid.uuid4().hex
            con.execute("INSERT INTO owner_mail_archive(archive_ref,account_id,provider_message_id,source_version,modified_at,key_id,nonce,ciphertext,aad_sha256,sealed_sha256,source_sha256,content_chars,coverage,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (reference, account_id, provider_message_id, source_version, modified_at,
                         sealed.key_id, sealed.nonce, sealed.ciphertext, sealed.aad_sha256,
                         sealed.plaintext_sha256, body_hash, len(text), encode(coverage), utc_now()))
            # Unknown ordering cannot displace a known revision. Equal timestamps
            # also leave the existing pointer until a newer provider time arrives.
            make_current = current is None or modified is not None and (
                current["modified_at"] is None or modified > _instant(current["modified_at"]))
            if make_current:
                con.execute("INSERT INTO owner_mail_archive_current VALUES(?,?,?) ON CONFLICT(account_id,provider_message_id) DO UPDATE SET archive_ref=excluded.archive_ref",
                            (account_id, provider_message_id, reference))
            row = con.execute("SELECT * FROM owner_mail_archive WHERE archive_ref=?", (reference,)).fetchone()
            return self._public(con, row)

    @staticmethod
    def _public(con, row):
        current = con.execute("SELECT archive_ref FROM owner_mail_archive_current WHERE account_id=? AND provider_message_id=?",
                              (row["account_id"], row["provider_message_id"])).fetchone()
        return {**{key: row[key] for key in ("archive_ref", "account_id", "provider_message_id", "source_version", "sequence", "modified_at", "source_sha256", "content_chars")},
                "coverage": json.loads(row["coverage"]), "make_current": current is not None and current[0] == row["archive_ref"]}

    def for_account(self, account_id):
        if not isinstance(account_id, str) or not account_id:
            raise DomainError("not_authorized", "Archive account is required")
        return AccountMailArchive(self, account_id)


class AccountMailArchive:
    def __init__(self, archive, account_id):
        self._archive, self.account_id = archive, account_id

    def read_source(self, archive_ref):
        if isinstance(archive_ref, str) and archive_ref.startswith("predecessor:mail_archive:"):
            return self._predecessor(archive_ref[len("predecessor:mail_archive:"):])
        with connect(self._archive.db_path) as con:
            row = con.execute("SELECT * FROM owner_mail_archive WHERE archive_ref=? AND account_id=?", (archive_ref, self.account_id)).fetchone()
        if row is None:
            raise DomainError("not_authorized", "Archive reference is outside the granted account")
        aad = self._archive._aad(row["account_id"], row["provider_message_id"], row["source_version"])
        value = json.loads(self._archive._open(row, aad, "sealed_sha256"))
        if hashlib.sha256(value["text"].encode("utf-8")).hexdigest() != row["source_sha256"]:
            raise DomainError("invalid_input", "Archive source digest changed")
        return {**value, "account_id": row["account_id"], "provider_message_id": row["provider_message_id"],
                "source_version": row["source_version"], "coverage": json.loads(row["coverage"])}

    def read_message(self, archive_ref):
        return self.read_source(archive_ref)["text"]

    def _predecessor(self, archive_id):
        path = self._archive.predecessor_path
        if path is None:
            raise DomainError("not_found", "Historical mail snapshot is not configured")
        con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA query_only=ON")
            row = con.execute("SELECT * FROM mail_archive WHERE archive_id=? AND account_id=?", (archive_id, self.account_id)).fetchone()
        finally:
            con.close()
        if row is None:
            raise DomainError("not_authorized", "Historical archive reference is outside the granted account")
        fields = {"version": 1, "kind": "message", "account_id": row["account_id"],
                  "immutable_message_id": row["immutable_message_id"]}
        text = self._archive._open(row, fields, "sanitized_sha256")
        if len(text) != row["sanitized_chars"]:
            raise DomainError("invalid_input", "Historical mail source length changed")
        return {"text": text, "metadata": {}, "account_id": row["account_id"],
                "provider_message_id": row["immutable_message_id"], "source_version": "predecessor:" + archive_id,
                "coverage": {"complete": not bool(row["truncated"]), "reasons": ["text_truncated"] if row["truncated"] else []}}
