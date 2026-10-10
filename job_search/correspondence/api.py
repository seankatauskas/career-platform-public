"""Message revisions and reviewed associations; never lifecycle inference.

Full text remains in the existing encrypted archive. Immutable content references
and hashes live here; a caller supplies its authorized archive reader when loading
evidence. No provider connection is opened by this module.
"""
import hashlib
import json
import uuid

from job_search.commands import DomainError, digest, encode

SCHEMA = """
CREATE TABLE IF NOT EXISTS corr_messages (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, provider_message_id TEXT NOT NULL,
 thread_id TEXT, latest_revision TEXT NOT NULL, version INTEGER NOT NULL,
 UNIQUE(account_id,provider_message_id));
CREATE TABLE IF NOT EXISTS corr_revisions (
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES corr_messages(id),
 source_version TEXT NOT NULL, direction TEXT NOT NULL, occurred_at TEXT,
 received_at TEXT NOT NULL, source_sha256 TEXT NOT NULL, archive_ref TEXT,
 content_chars INTEGER NOT NULL, metadata TEXT NOT NULL, fingerprint TEXT NOT NULL,
 UNIQUE(message_id,source_version));
CREATE TABLE IF NOT EXISTS corr_associations (
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE REFERENCES corr_messages(id),
 application_id TEXT NOT NULL, version INTEGER NOT NULL, reason TEXT);
"""


def _text(value, name, limit=512):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise DomainError("invalid_input", "Invalid " + name)
    return value


def _human(tx):
    if tx.context.principal.kind != "human" or tx.context.origin == "inferred":
        raise DomainError("not_authorized", "Associations require an explicit human decision")


class CorrespondenceOperations:
    def import_records(self, tx, *, records, source_snapshot):
        """Import historical owner DTOs without inference or delivery side effects."""
        if tx.context.origin != "migration" or tx.context.principal.kind != "worker":
            raise DomainError("not_authorized", "Historical import requires the migration worker")
        _text(source_snapshot, "source snapshot", 2000)
        if set(records) - {"messages", "revisions", "associations"}:
            raise DomainError("invalid_input", "Unsupported correspondence import records")
        counts = {}
        with tx.scope("correspondence"):
            for group in ("messages", "revisions", "associations"):
                counts[group] = 0
                for record in records.get(group, ()):
                    record = dict(record)
                    _text(record.get("id"), "historical identity")
                    if group == "messages":
                        if set(record) != {"id", "account_id", "provider_message_id", "thread_id", "latest_revision", "version"}:
                            raise DomainError("invalid_input", "Invalid historical message fields")
                        sql = "INSERT INTO corr_messages VALUES(:id,:account_id,:provider_message_id,:thread_id,:latest_revision,:version)"
                    elif group == "revisions":
                        allowed = {"id", "message_id", "source_version", "direction", "occurred_at", "received_at", "source_sha256", "archive_ref", "content_chars", "metadata", "fingerprint"}
                        if set(record) - allowed or allowed - {"fingerprint"} - set(record):
                            raise DomainError("invalid_input", "Invalid historical revision fields")
                        if record["direction"] not in {"incoming", "outgoing", "draft", "unknown"}:
                            raise DomainError("invalid_input", "Invalid historical message direction")
                        if not isinstance(record["source_sha256"], str) or len(record["source_sha256"]) != 64:
                            raise DomainError("invalid_input", "Historical evidence hash is required")
                        record["source_version"] = str(record["source_version"])
                        metadata = record["metadata"] if isinstance(record["metadata"], dict) else json.loads(record["metadata"])
                        if set(metadata) - {"coverage", "attachments", "sanitizer_version", "subject_hash"}:
                            raise DomainError("invalid_input", "Historical metadata contains unsupported fields")
                        record.setdefault("fingerprint", digest({"direction": record["direction"], "occurred_at": record["occurred_at"],
                            "sha256": record["source_sha256"], "chars": record["content_chars"], "metadata": metadata}))
                        record["metadata"] = encode(metadata)
                        sql = "INSERT INTO corr_revisions VALUES(:id,:message_id,:source_version,:direction,:occurred_at,:received_at,:source_sha256,:archive_ref,:content_chars,:metadata,:fingerprint)"
                    else:
                        if set(record) != {"id", "message_id", "application_id", "version", "reason"}:
                            raise DomainError("invalid_input", "Invalid historical association fields")
                        sql = "INSERT INTO corr_associations VALUES(:id,:message_id,:application_id,:version,:reason)"
                    tx.connection.execute(sql, record)
                    tx.record("correspondence", record["id"], "import_snapshot", None,
                              {"source_snapshot": source_snapshot, "record_kind": group, "record_id": record["id"]})
                    counts[group] += 1
            missing = tx.connection.execute("SELECT m.id FROM corr_messages m LEFT JOIN corr_revisions r ON r.id=m.latest_revision AND r.message_id=m.id WHERE r.id IS NULL LIMIT 1").fetchone()
            if missing:
                raise DomainError("dependency_unresolved", "Historical message references a missing revision")
        return {"counts": counts, "source_snapshot": source_snapshot, "dispatch_created": False}

    def record_message(self, tx, *, account_id, provider_message_id, source_version,
                       direction, authored_text=None, occurred_at=None, thread_id=None,
                       metadata=None, archive_ref=None, source_sha256=None,
                       content_chars=None, make_current=True):
        """Preserve an opaque source revision. Out-of-order imports set make_current=False.

        Source versions are opaque provider tokens and must not be sorted. Existing
        revision retries never move the current pointer backwards. Known historical
        revisions can be added without moving it using make_current=False.
        """
        numeric_version = isinstance(source_version, int) and not isinstance(source_version, bool)
        if numeric_version:
            if source_version < 0:
                raise DomainError("invalid_input", "Invalid numeric source version")
            source_version = str(source_version)
        for name, value in (("account_id", account_id), ("provider_message_id", provider_message_id),
                            ("source_version", source_version)):
            _text(value, name)
        if direction not in {"incoming", "outgoing", "draft", "unknown"}:
            raise DomainError("invalid_input", "Invalid message direction")
        if authored_text is not None:
            if not isinstance(authored_text, str) or len(authored_text) > 250000:
                raise DomainError("invalid_input", "Invalid bounded source text")
            actual = hashlib.sha256(authored_text.encode("utf-8")).hexdigest()
            if source_sha256 is not None and source_sha256 != actual:
                raise DomainError("invalid_input", "Source hash mismatch")
            source_sha256, content_chars = actual, len(authored_text)
        if (not isinstance(source_sha256, str) or len(source_sha256) != 64 or
                any(c not in "0123456789abcdef" for c in source_sha256) or
                not isinstance(content_chars, int) or isinstance(content_chars, bool) or
                not 0 <= content_chars <= 250000):
            raise DomainError("invalid_input", "Source digest and bounded size are required")
        metadata = dict(metadata or {})
        if set(metadata) - {"coverage", "attachments", "sanitizer_version", "subject_hash"}:
            raise DomainError("invalid_input", "Message metadata contains unsupported fields")
        fingerprint = digest({"direction": direction, "occurred_at": occurred_at,
                              "sha256": source_sha256, "chars": content_chars, "metadata": metadata})
        with tx.scope("correspondence"):
            row = tx.connection.execute("SELECT * FROM corr_messages WHERE account_id=? AND provider_message_id=?",
                                        (account_id, provider_message_id)).fetchone()
            if row and numeric_version:
                latest = tx.connection.execute("SELECT source_version FROM corr_revisions WHERE id=?", (row["latest_revision"],)).fetchone()
                if latest and latest[0].isdigit() and int(source_version) < int(latest[0]):
                    make_current = False
            message_id = row["id"] if row else "msg_" + uuid.uuid4().hex
            old = tx.connection.execute("SELECT * FROM corr_revisions WHERE message_id=? AND source_version=?",
                                        (message_id, source_version)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise DomainError("idempotency_conflict", "A source revision cannot change content")
                return self.get(tx.connection, message_id, revision=old["id"])
            revision = "rev_" + uuid.uuid4().hex
            if row is None:
                tx.connection.execute("INSERT INTO corr_messages VALUES(?,?,?,?,?,1)",
                                      (message_id, account_id, provider_message_id, thread_id, revision))
            elif make_current:
                tx.connection.execute("UPDATE corr_messages SET latest_revision=?,version=version+1 WHERE id=?",
                                      (revision, message_id))
            tx.connection.execute("INSERT INTO corr_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                  (revision, message_id, source_version, direction, occurred_at, tx.now,
                                   source_sha256, archive_ref, content_chars, encode(metadata), fingerprint))
            result = self.get(tx.connection, message_id, revision=revision)
            tx.record("correspondence", revision, "record_message", None, result)
            tx.enqueue("correspondence", "understand_message" if direction == "incoming" else "observe_outgoing",
                       revision, {"message_id": message_id, "revision": revision})
            return result

    def get(self, connection, message_id, revision=None):
        message = connection.execute("SELECT * FROM corr_messages WHERE id=?", (message_id,)).fetchone()
        if message is None:
            raise DomainError("not_found", "Message not found")
        source = connection.execute("SELECT * FROM corr_revisions WHERE message_id=? AND id=?",
                                    (message_id, revision or message["latest_revision"])).fetchone()
        if source is None:
            raise DomainError("not_found", "Message revision not found")
        result = dict(message)
        result.update({k: source[k] for k in source.keys() if k not in {"id", "message_id", "metadata", "fingerprint"}})
        result.update(revision=source["id"], metadata=json.loads(source["metadata"]))
        return result

    def evidence(self, connection, message_id, *, archive, revision=None, allowed_accounts=(), limit=250000):
        source = self.get(connection, message_id, revision)
        if source["account_id"] not in allowed_accounts:
            raise DomainError("not_authorized", "Evidence account is not granted")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 250000:
            raise DomainError("invalid_input", "Invalid evidence bound")
        if source["archive_ref"] is None:
            return {**source, "text": None, "coverage": {"complete": False, "reason": "archive_missing"}}
        try:
            text = archive.read_message(source["archive_ref"])
        except Exception:
            return {**source, "text": None, "coverage": {"complete": False, "reason": "archive_unavailable"}}
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != source["source_sha256"]:
            raise DomainError("invalid_input", "Archive revision does not match preserved evidence")
        declared = source["metadata"].get("coverage", {"complete": True})
        if not isinstance(declared, dict) or not isinstance(declared.get("complete"), bool):
            declared = {"complete": False, "reason": "invalid_source_coverage"}
        coverage = dict(declared)
        reasons = list(coverage.get("reasons", []))
        if coverage.get("reason"):
            reasons.append(coverage["reason"])
        if len(text) > limit:
            reasons.append("text_truncated")
        # This API opens message text only. Attachments need separate authorized
        # source loading before a caller can claim complete semantic coverage.
        if source["metadata"].get("attachments"):
            reasons.append("attachments_not_loaded")
        coverage["complete"] = declared["complete"] and not reasons
        coverage["reasons"] = list(dict.fromkeys(reasons))
        return {**source, "text": text[:limit], "coverage": coverage}

    def link_message(self, tx, *, message_id, application_id, expected_version=0):
        _human(tx)
        _text(application_id, "application_id")
        self.get(tx.connection, message_id)
        with tx.scope("correspondence"):
            old = tx.connection.execute("SELECT * FROM corr_associations WHERE message_id=?", (message_id,)).fetchone()
            if old:
                if old["version"] != expected_version or old["application_id"] != application_id:
                    raise DomainError("version_conflict", "Association already decided; use correction")
                return dict(old)
            if expected_version != 0:
                raise DomainError("version_conflict", "Association version changed")
            result = {"id": "assoc_" + uuid.uuid4().hex, "message_id": message_id,
                      "application_id": application_id, "version": 1, "reason": None}
            tx.connection.execute("INSERT INTO corr_associations VALUES(:id,:message_id,:application_id,:version,:reason)", result)
            tx.record("correspondence", result["id"], "link_message", None, result)
            return result

    def association(self, connection, message_id):
        row = connection.execute("SELECT * FROM corr_associations WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row else None

    def get_association(self, connection, association_id):
        row = connection.execute("SELECT * FROM corr_associations WHERE id=?", (association_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "Association not found")
        return dict(row)

    def list_associations(self, connection, application_id, limit=100, after=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise DomainError("invalid_input", "Invalid association page size")
        rows = connection.execute("SELECT * FROM corr_associations WHERE application_id=? AND id>? ORDER BY id LIMIT ?", (application_id, after or "", limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None, "truncated": len(rows) > limit}

    def linked_messages(self, connection, *, allowed_accounts, limit=100, after=None):
        """Bounded archive-search candidates, restricted to reviewed links and accounts."""
        accounts = tuple(allowed_accounts)
        if not accounts or len(accounts) > 100 or any(not isinstance(a, str) or not a for a in accounts):
            raise DomainError("not_authorized", "Explicit evidence accounts are required")
        if type(limit) is not int or not 1 <= limit <= 100 or (after is not None and (not isinstance(after, str) or len(after) > 512)):
            raise DomainError("invalid_input", "Invalid linked-message page")
        placeholders = ",".join("?" for _ in accounts)
        rows = connection.execute("SELECT a.message_id,a.application_id FROM corr_associations a JOIN corr_messages m ON m.id=a.message_id "
            "WHERE m.account_id IN (" + placeholders + ") AND a.message_id>? ORDER BY a.message_id LIMIT ?", (*accounts, after or "", limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["message_id"] if len(rows) > limit else None, "truncated": len(rows) > limit}

    def correct_association(self, tx, *, association_id, application_id, expected_version, reason):
        _human(tx)
        _text(application_id, "application_id")
        _text(reason, "reason", 2000)
        with tx.scope("correspondence"):
            row = tx.connection.execute("SELECT * FROM corr_associations WHERE id=?", (association_id,)).fetchone()
            if row is None:
                raise DomainError("not_found", "Association not found")
            if row["version"] != expected_version:
                raise DomainError("version_conflict", "Association changed")
            before = dict(row)
            after = {**before, "application_id": application_id, "version": expected_version + 1, "reason": reason}
            tx.connection.execute("UPDATE corr_associations SET application_id=?,version=?,reason=? WHERE id=?",
                                  (application_id, after["version"], reason, association_id))
            tx.record("correspondence", association_id, "correct_association", before, after)
            return after

    def conversation(self, connection, application_id, limit=50, after=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise DomainError("invalid_input", "Invalid page size")
        rows = connection.execute("SELECT message_id FROM corr_associations WHERE application_id=? AND message_id>? ORDER BY message_id LIMIT ?",
                                  (application_id, after or "", limit + 1)).fetchall()
        items = [self.get(connection, row["message_id"]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None,
                "coverage": {"complete": len(rows) <= limit}}
