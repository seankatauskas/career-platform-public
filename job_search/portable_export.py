"""No-clobber export from macOS-bound encrypted state to portable encryption.

The operation deliberately does not rotate either source key in place.  It takes a
consistent SQLite backup, re-encrypts the backup's mail archive in one transaction,
stages a separately encrypted autofill vault, verifies both staged outputs, and only
then publishes new owner-only files.  Outlook tokens are intentionally excluded.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .autofill import EncryptedAutofillVault
from .contracts import ContractError, canonical_json, payload_sha256
from .mail.archive import (
    ARCHIVE_KEY_BYTES,
    ARCHIVE_NONCE_BYTES,
    ARCHIVE_VERSION,
    AESGCMCipher,
    EncryptedMailArchive,
    KeychainArchiveKeyProvider,
)
from .secure_persistence import (
    EncryptedFilePersistence,
    PortableArchiveKeyProvider,
    read_portable_master_key,
)


def require_legacy_export_source(connection) -> None:
    """These single-database exports cannot preserve an owner installation."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    bound = "application_owner_binding" in tables and connection.execute(
        "SELECT 1 FROM application_owner_binding LIMIT 1").fetchone()
    if bound or "installation_identity" in tables:
        raise ContractError("Owner application installations are not supported by portable/state transfer export; "
                            "use the complete installation backup and preserve its original encryption key")


_MAIL_COLUMNS = frozenset(
    {
        "archive_id",
        "account_id",
        "immutable_message_id",
        "key_id",
        "nonce",
        "ciphertext",
        "aad_sha256",
        "sanitized_sha256",
        "sanitized_chars",
        "truncated",
        "created_at",
        "updated_at",
    }
)
_ATTACHMENT_COLUMNS = frozenset(
    {
        "attachment_record_id",
        "archive_id",
        "immutable_attachment_id",
        "mime_type",
        "source_size",
        "source_sha256",
        "extracted_sha256",
        "extracted_chars",
        "key_id",
        "nonce",
        "ciphertext",
        "aad_sha256",
        "created_at",
    }
)


class _StaticArchiveKeyProvider:
    def __init__(self, key_id: str, key: bytes) -> None:
        self._value = (key_id, key)

    def get_or_create_key(self) -> tuple[str, bytes]:
        return self._value


class _ConnectionArchiveReader:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_encrypted_mail_archive(self, archive_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM mail_archive WHERE archive_id=?", (archive_id,)
        ).fetchone()
        if row is None:
            raise ContractError("mail archive was not found during portable export")
        return dict(row)

    def get_encrypted_mail_archive_attachment(
        self, attachment_record_id: str
    ) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM mail_archive_attachments WHERE attachment_record_id=?",
            (attachment_record_id,),
        ).fetchone()
        if row is None:
            raise ContractError(
                "mail archive attachment was not found during portable export"
            )
        return dict(row)


def _absolute_input(path: Path, label: str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ContractError(f"{label} path must be absolute")
    try:
        info = os.lstat(target)
    except OSError as exc:
        raise ContractError(f"{label} must be an existing regular file") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ContractError(f"{label} must be an existing regular file")
    return target.parent.resolve() / target.name


def _absolute_output(path: Path, label: str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ContractError(f"{label} path must be absolute")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent.resolve()
    resolved = parent / target.name
    try:
        os.lstat(resolved)
    except FileNotFoundError:
        return resolved
    except OSError as exc:
        raise ContractError(f"{label} destination could not be inspected") from exc
    raise FileExistsError(f"{label} destination already exists: {resolved}")


def _absolute_keychain_location(path: Path, label: str) -> Path:
    """Validate a logical Keychain location, which need not have an on-disk file."""

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ContractError(f"{label} path must be absolute")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"{label} parent directory must exist") from exc
    if not parent.is_dir():
        raise ContractError(f"{label} parent directory must exist")
    resolved = parent / target.name
    try:
        info = os.lstat(resolved)
    except FileNotFoundError:
        return resolved
    except OSError as exc:
        raise ContractError(f"{label} could not be inspected") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ContractError(f"{label} must not be a symlink or special file")
    return resolved


def _private_regular(path: Path, label: str) -> None:
    info = os.lstat(path)
    current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != current_uid
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise ContractError(f"{label} must be an owner-only regular file")


def _new_staging_file(target: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{target.name}.portable-export-", dir=str(target.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return Path(raw_path)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _schema_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _message_aad(row: Mapping[str, Any]) -> bytes:
    return canonical_json(
        {
            "version": ARCHIVE_VERSION,
            "kind": "message",
            "account_id": row["account_id"],
            "immutable_message_id": row["immutable_message_id"],
        }
    ).encode("utf-8")


def _attachment_aad(row: Mapping[str, Any]) -> bytes:
    return canonical_json(
        {
            "version": ARCHIVE_VERSION,
            "kind": "attachment_text",
            "archive_id": row["archive_id"],
            "immutable_attachment_id": row["immutable_attachment_id"],
            "mime_type": row["mime_type"],
            "source_sha256": row["source_sha256"],
        }
    ).encode("utf-8")


def _read_existing_archive_key(provider: Any) -> tuple[str, bytes]:
    reader = getattr(provider, "get_existing_key", None)
    if not callable(reader):
        raise ContractError(
            "source archive provider must support read-only get_existing_key"
        )
    key_id, key = reader()
    if (
        not isinstance(key_id, str)
        or not key_id
        or not isinstance(key, bytes)
        or len(key) != ARCHIVE_KEY_BYTES
        or key_id != hashlib.sha256(key).hexdigest()[:32]
    ):
        raise ContractError("source archive provider returned an invalid key identity")
    return key_id, key


def _nonce(nonce_factory: Any, seen: set[bytes]) -> bytes:
    value = nonce_factory(ARCHIVE_NONCE_BYTES)
    if (
        not isinstance(value, bytes)
        or len(value) != ARCHIVE_NONCE_BYTES
        or value in seen
    ):
        raise ContractError("portable archive nonce source is invalid or repeated")
    seen.add(value)
    return value


def _rekey_command_results(
    connection: sqlite3.Connection, target_key_id: str
) -> None:
    """Keep durable idempotency requests consistent with the copied ciphertext key."""

    shapes = {
        "put_mail_archive": (
            "archive",
            {
                "account_id",
                "immutable_message_id",
                "key_id",
                "aad_sha256",
                "sanitized_sha256",
                "sanitized_chars",
                "truncated",
            },
        ),
        "put_mail_archive_attachment": (
            "attachment",
            {
                "archive_id",
                "immutable_attachment_id",
                "mime_type",
                "source_size",
                "source_sha256",
                "extracted_sha256",
                "extracted_chars",
                "key_id",
                "aad_sha256",
            },
        ),
    }
    rows = connection.execute(
        "SELECT command_name,idempotency_key,response_json FROM command_results "
        "WHERE command_name IN ('put_mail_archive','put_mail_archive_attachment')"
    ).fetchall()
    for row in rows:
        command_name = str(row["command_name"])
        container, request_fields = shapes[command_name]
        try:
            response = json.loads(str(row["response_json"]))
            record = response[container]
            if (
                not isinstance(response, dict)
                or set(response) != {"created", container}
                or not isinstance(response["created"], bool)
                or not isinstance(record, dict)
                or not request_fields.issubset(record)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "archive idempotency record is invalid during portable export"
            ) from exc
        migrated_record = dict(record)
        migrated_record["key_id"] = target_key_id
        migrated_response = dict(response)
        migrated_response[container] = migrated_record
        normalized_request = {
            name: migrated_record[name] for name in request_fields
        }
        if command_name == "put_mail_archive":
            normalized_request["truncated"] = bool(
                normalized_request["truncated"]
            )
        connection.execute(
            "UPDATE command_results SET request_sha256=?,response_json=? "
            "WHERE command_name=? AND idempotency_key=?",
            (
                payload_sha256(normalized_request),
                canonical_json(migrated_response),
                command_name,
                row["idempotency_key"],
            ),
        )


def _rekey_database(
    connection: sqlite3.Connection,
    *,
    source_archive_key_provider: Any,
    target_archive_key_provider: PortableArchiveKeyProvider,
    cipher_factory: Any,
    nonce_factory: Any,
) -> tuple[int, int]:
    if not _MAIL_COLUMNS.issubset(_schema_columns(connection, "mail_archive")):
        raise ContractError("source database is missing the mail archive schema")
    if not _ATTACHMENT_COLUMNS.issubset(
        _schema_columns(connection, "mail_archive_attachments")
    ):
        raise ContractError("source database is missing the attachment archive schema")
    message_rows = connection.execute(
        "SELECT * FROM mail_archive ORDER BY archive_id"
    ).fetchall()
    attachment_rows = connection.execute(
        "SELECT * FROM mail_archive_attachments ORDER BY attachment_record_id"
    ).fetchall()
    shared_rows = connection.execute(
        "SELECT * FROM mail_understanding_sources ORDER BY analysis_id,source_id"
    ).fetchall() if _schema_columns(connection, "mail_understanding_sources") else []
    owner_refs = connection.execute(
        "SELECT archive_ref FROM owner_mail_archive ORDER BY sequence"
    ).fetchall() if _schema_columns(connection, "owner_mail_archive") else []
    if not message_rows and attachment_rows:
        raise ContractError("attachment archive exists without a mail archive")
    if not message_rows and not shared_rows and not owner_refs:
        return 0, 0

    if source_archive_key_provider is None:
        source_archive_key_provider = KeychainArchiveKeyProvider(read_only=True)
    source_key_id, source_key = _read_existing_archive_key(
        source_archive_key_provider
    )
    target_key_id, target_key = target_archive_key_provider.get_existing_key()
    if source_key_id == target_key_id or source_key == target_key:
        raise ContractError("portable archive key must differ from the source key")
    reader = _ConnectionArchiveReader(connection)
    source_archive = EncryptedMailArchive(
        reader,
        _StaticArchiveKeyProvider(source_key_id, source_key),
        cipher_factory=cipher_factory,
    )
    target_cipher = cipher_factory(target_key)
    seen_nonces: set[bytes] = set()

    for saved_row in message_rows:
        row = dict(saved_row)
        plaintext = source_archive.read_message(str(row["archive_id"]))
        if len(plaintext) != row["sanitized_chars"]:
            raise ContractError("mail archive character count does not match plaintext")
        aad = _message_aad(row)
        if hashlib.sha256(aad).hexdigest() != row["aad_sha256"]:
            raise ContractError("mail archive authenticated metadata does not match")
        nonce = _nonce(nonce_factory, seen_nonces)
        ciphertext = target_cipher.encrypt(nonce, plaintext.encode("utf-8"), aad)
        if not isinstance(ciphertext, bytes) or len(ciphertext) < 16:
            raise ContractError("portable archive cipher returned invalid ciphertext")
        connection.execute(
            "UPDATE mail_archive SET key_id=?,nonce=?,ciphertext=? WHERE archive_id=?",
            (target_key_id, nonce, ciphertext, row["archive_id"]),
        )

    for saved_row in attachment_rows:
        row = dict(saved_row)
        plaintext = source_archive.read_attachment_text(
            str(row["attachment_record_id"])
        )
        if len(plaintext) != row["extracted_chars"]:
            raise ContractError(
                "mail archive attachment character count does not match plaintext"
            )
        aad = _attachment_aad(row)
        if hashlib.sha256(aad).hexdigest() != row["aad_sha256"]:
            raise ContractError(
                "mail archive attachment authenticated metadata does not match"
            )
        nonce = _nonce(nonce_factory, seen_nonces)
        ciphertext = target_cipher.encrypt(nonce, plaintext.encode("utf-8"), aad)
        if not isinstance(ciphertext, bytes) or len(ciphertext) < 16:
            raise ContractError("portable archive cipher returned invalid ciphertext")
        connection.execute(
            "UPDATE mail_archive_attachments SET key_id=?,nonce=?,ciphertext=? "
            "WHERE attachment_record_id=?",
            (target_key_id, nonce, ciphertext, row["attachment_record_id"]),
        )

    # Only the staging backup is rekeyed. Restore its append-only guard within
    # the same transaction; the source database and plaintext hashes are untouched.
    shared_trigger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='mail_understanding_sources_no_update'"
    ).fetchone() if shared_rows else None
    if shared_rows and not shared_trigger:
        raise ContractError("shared mail source immutability guard is missing")
    if shared_rows:
        connection.execute("DROP TRIGGER mail_understanding_sources_no_update")
        try:
            for saved_row in shared_rows:
                row = dict(saved_row)
                fields = dict(version=1, kind='mail_understanding_source',
                              analysis_id=row['analysis_id'], source_id=row['source_id'])
                text = source_archive._open(row, fields, 'plaintext_sha256')
                if len(text) != row['plaintext_chars']:
                    raise ContractError('shared mail source length does not match')
                aad = canonical_json(fields).encode('utf-8')
                nonce = _nonce(nonce_factory, seen_nonces)
                ciphertext = target_cipher.encrypt(nonce, text.encode('utf-8'), aad)
                if not isinstance(ciphertext, bytes) or len(ciphertext) < 16:
                    raise ContractError('portable shared mail cipher returned invalid ciphertext')
                connection.execute(
                    'UPDATE mail_understanding_sources SET key_id=?,nonce=?,ciphertext=? WHERE analysis_id=? AND source_id=?',
                    (target_key_id, nonce, ciphertext, row['analysis_id'], row['source_id']))
        finally:
            connection.execute(shared_trigger['sql'])

    # Immutable owner revisions are also part of the private operational archive.
    # Process one ciphertext at a time; alter only the staging copy's encryption.
    if owner_refs:
        from .mail.revision_archive import ImmutableMailArchive
        trigger = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='owner_mail_archive_immutable'").fetchone()
        if trigger is None:
            raise ContractError("owner mail revision immutability guard is missing")
        connection.execute("SAVEPOINT rekey_owner_mail")
        try:
            connection.execute("DROP TRIGGER owner_mail_archive_immutable")
            for reference in owner_refs:
                row = connection.execute("SELECT * FROM owner_mail_archive WHERE archive_ref=?", (reference["archive_ref"],)).fetchone()
                fields = ImmutableMailArchive._aad(row["account_id"], row["provider_message_id"], row["source_version"])
                text = source_archive._open(row, fields, "sealed_sha256")
                value = json.loads(text)
                if len(value["text"]) != row["content_chars"] or hashlib.sha256(value["text"].encode("utf-8")).hexdigest() != row["source_sha256"]:
                    raise ContractError("owner mail revision source does not match")
                aad = canonical_json(fields).encode("utf-8")
                nonce = _nonce(nonce_factory, seen_nonces)
                ciphertext = target_cipher.encrypt(nonce, text.encode("utf-8"), aad)
                if not isinstance(ciphertext, bytes) or len(ciphertext) < 16:
                    raise ContractError("portable owner mail cipher returned invalid ciphertext")
                connection.execute("UPDATE owner_mail_archive SET key_id=?,nonce=?,ciphertext=? WHERE archive_ref=?",
                                   (target_key_id, nonce, ciphertext, reference["archive_ref"]))
            connection.execute(trigger["sql"])
        except Exception:
            connection.execute("ROLLBACK TO rekey_owner_mail")
            raise
        finally:
            connection.execute("RELEASE rekey_owner_mail")

    _rekey_command_results(connection, target_key_id)

    target_archive = EncryptedMailArchive(
        reader,
        _StaticArchiveKeyProvider(target_key_id, target_key),
        cipher_factory=cipher_factory,
    )
    for row in message_rows:
        text = target_archive.read_message(str(row["archive_id"]))
        if len(text) != row["sanitized_chars"]:
            raise ContractError("portable mail archive verification failed")
    for row in attachment_rows:
        text = target_archive.read_attachment_text(str(row["attachment_record_id"]))
        if len(text) != row["extracted_chars"]:
            raise ContractError("portable attachment archive verification failed")
    for row in shared_rows:
        saved = connection.execute('SELECT * FROM mail_understanding_sources WHERE analysis_id=? AND source_id=?',
                                   (row['analysis_id'], row['source_id'])).fetchone()
        fields = dict(version=1, kind='mail_understanding_source', analysis_id=row['analysis_id'], source_id=row['source_id'])
        text = target_archive._open(saved, fields, 'plaintext_sha256')
        if len(text) != row['plaintext_chars']:
            raise ContractError('portable shared mail source verification failed')
    for reference in owner_refs:
        row = connection.execute("SELECT * FROM owner_mail_archive WHERE archive_ref=?", (reference["archive_ref"],)).fetchone()
        fields = ImmutableMailArchive._aad(row["account_id"], row["provider_message_id"], row["source_version"])
        text = target_archive._open(row, fields, "sealed_sha256")
        value = json.loads(text)
        if len(value["text"]) != row["content_chars"] or hashlib.sha256(value["text"].encode("utf-8")).hexdigest() != row["source_sha256"]:
            raise ContractError("portable owner mail revision verification failed")
    return len(message_rows) + len(owner_refs), len(attachment_rows)


def _stage_database(
    source: Path,
    target: Path,
    key_file: Path,
    *,
    source_archive_key_provider: Any,
    cipher_factory: Any,
    nonce_factory: Any,
) -> tuple[Path, int, int]:
    temporary = _new_staging_file(target)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            source.as_uri() + "?mode=ro", uri=True, timeout=10
        )
        destination_connection = sqlite3.connect(str(temporary), timeout=10)
        destination_connection.row_factory = sqlite3.Row
        source_connection.backup(destination_connection)
        require_legacy_export_source(destination_connection)
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        destination_connection.execute("PRAGMA foreign_keys=ON")
        secure_delete = destination_connection.execute(
            "PRAGMA secure_delete=ON"
        ).fetchone()
        if secure_delete is None or int(secure_delete[0]) != 1:
            raise ContractError("portable database could not enable secure deletion")
        destination_connection.execute("BEGIN IMMEDIATE")
        try:
            messages, attachments = _rekey_database(
                destination_connection,
                source_archive_key_provider=source_archive_key_provider,
                target_archive_key_provider=PortableArchiveKeyProvider(key_file),
                cipher_factory=cipher_factory,
                nonce_factory=nonce_factory,
            )
            violations = destination_connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise ContractError("portable database failed foreign-key validation")
            check = destination_connection.execute("PRAGMA integrity_check").fetchall()
            if len(check) != 1 or str(check[0][0]).casefold() != "ok":
                raise ContractError("portable database failed SQLite integrity validation")
            destination_connection.commit()
        except Exception:
            destination_connection.rollback()
            raise
        # Compact the copy so source-key identifiers/ciphertext in replaced or
        # previously deleted SQLite pages do not hitchhike into the portable file.
        destination_connection.execute("VACUUM")
        check = destination_connection.execute("PRAGMA integrity_check").fetchall()
        if len(check) != 1 or str(check[0][0]).casefold() != "ok":
            raise ContractError("compacted portable database failed integrity validation")
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        os.chmod(temporary, 0o600, follow_symlinks=False)
        _private_regular(temporary, "staged portable database")
        _fsync_file(temporary)
        return temporary, messages, attachments
    except Exception:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-journal").unlink(missing_ok=True)
        Path(str(temporary) + "-wal").unlink(missing_ok=True)
        Path(str(temporary) + "-shm").unlink(missing_ok=True)
        raise


def _stage_autofill(
    source: Path,
    target: Path,
    key_file: Path,
    *,
    source_persistence: Any = None,
    portable_aesgcm_type: Any = None,
) -> tuple[Path, Mapping[str, int]]:
    temporary = _new_staging_file(target)
    # EncryptedFilePersistence treats an empty existing file as invalid.  The staging
    # inode is only a reservation; remove it before its first no-clobber encrypted save.
    temporary.unlink()
    try:
        source_vault = EncryptedAutofillVault(source, persistence=source_persistence)
        target_persistence = EncryptedFilePersistence(
            temporary,
            key_file,
            "autofill-vault",
            aesgcm_type=portable_aesgcm_type,
        )
        portable_vault = EncryptedAutofillVault(
            temporary, persistence=target_persistence
        )
        counts = source_vault.copy_encrypted_state_to(portable_vault)
        os.chmod(temporary, 0o600, follow_symlinks=False)
        _private_regular(temporary, "staged portable autofill vault")
        _fsync_file(temporary)
        return temporary, counts
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _publish_no_clobber(staged: Path, target: Path) -> None:
    linked = False
    try:
        os.link(staged, target, follow_symlinks=False)
        linked = True
    except FileExistsError as exc:
        raise FileExistsError(f"portable export destination already exists: {target}") from exc
    try:
        os.chmod(target, 0o600, follow_symlinks=False)
        staged.unlink()
        _private_regular(target, "portable export output")
        _fsync_directory(target.parent)
    except Exception:
        if linked:
            target.unlink(missing_ok=True)
            _fsync_directory(target.parent)
        raise


def export_portable_state(
    *,
    source_database: Path,
    destination_database: Path,
    portable_key_file: Path,
    destination_autofill_vault: Path | None = None,
    source_autofill_vault: Path | None = None,
    source_archive_key_provider: Any = None,
    source_autofill_persistence: Any = None,
    cipher_factory: Any = AESGCMCipher,
    nonce_factory: Any = secrets.token_bytes,
    portable_aesgcm_type: Any = None,
) -> Mapping[str, Any]:
    """Create verified portable copies without modifying or replacing source state."""

    source_db = _absolute_input(source_database, "source database")
    from contextlib import closing
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as con:
        require_legacy_export_source(con)
    target_db = _absolute_output(destination_database, "portable database")
    key_file = _absolute_input(portable_key_file, "portable encryption key")
    # Validate key contents and ownership before staging a potentially large backup.
    read_portable_master_key(key_file)
    if source_db == target_db:
        raise ContractError("portable database destination must differ from its source")

    source_vault: Path | None = None
    target_vault: Path | None = None
    export_vault = source_autofill_vault is not None
    if export_vault != (destination_autofill_vault is not None):
        raise ContractError(
            "source and destination autofill vault paths must be provided together"
        )
    if source_autofill_vault is not None and destination_autofill_vault is not None:
        raw_source_vault = Path(source_autofill_vault).expanduser()
        if not raw_source_vault.is_absolute():
            raise ContractError("source autofill vault path must be absolute")
        source_vault = _absolute_keychain_location(
            raw_source_vault, "source autofill vault"
        )
        target_vault = _absolute_output(
            destination_autofill_vault, "portable autofill vault"
        )
        if source_vault == target_vault or target_vault == target_db:
            raise ContractError("portable export input and output paths must be distinct")

    staged_database: Path | None = None
    staged_vault: Path | None = None
    autofill_counts = {"private_answers": 0, "custom_history": 0}
    published: list[Path] = []
    try:
        staged_database, messages, attachments = _stage_database(
            source_db,
            target_db,
            key_file,
            source_archive_key_provider=source_archive_key_provider,
            cipher_factory=cipher_factory,
            nonce_factory=nonce_factory,
        )
        if source_vault is not None and target_vault is not None:
            staged_vault, autofill_counts = _stage_autofill(
                source_vault,
                target_vault,
                key_file,
                source_persistence=source_autofill_persistence,
                portable_aesgcm_type=portable_aesgcm_type,
            )
        if staged_vault is not None and target_vault is not None:
            _publish_no_clobber(staged_vault, target_vault)
            staged_vault = None
            published.append(target_vault)
        _publish_no_clobber(staged_database, target_db)
        staged_database = None
        published.append(target_db)
    except Exception:
        for path in reversed(published):
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        if staged_database is not None:
            staged_database.unlink(missing_ok=True)
        if staged_vault is not None:
            staged_vault.unlink(missing_ok=True)
        raise

    return {
        "exported": True,
        "source_database": str(source_db),
        "portable_database": str(target_db),
        "mail_archive_messages": messages,
        "mail_archive_attachments": attachments,
        "autofill_vault_exported": target_vault is not None,
        "autofill_private_answers": autofill_counts["private_answers"],
        "autofill_custom_history": autofill_counts["custom_history"],
        "portable_autofill_vault": str(target_vault) if target_vault else None,
        "outlook_token_cache_exported": False,
    }


__all__ = ["export_portable_state"]
