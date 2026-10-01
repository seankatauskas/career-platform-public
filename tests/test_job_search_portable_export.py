#!/usr/bin/env python3
"""Offline checks for no-clobber Mac-to-portable encrypted-state export."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from job_search.autofill import EncryptedAutofillVault
from job_search.contracts import ContractError, MutationContext
from job_search.mail.archive import EncryptedMailArchive, KeychainArchiveKeyProvider
from job_search.portable_export import export_portable_state
from job_search.secure_persistence import (
    EncryptedFilePersistence,
    PortableArchiveKeyProvider,
    initialize_portable_master_key,
)
from job_search.service import JobSearchLedger


class TestCipher:
    """Dependency-free authenticated cipher used only by this offline test."""

    def __init__(self, key: bytes) -> None:
        self.key = key

    def encrypt(self, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        mask = hashlib.sha256(self.key + nonce).digest()
        body = bytes(
            value ^ mask[index % len(mask)]
            for index, value in enumerate(plaintext)
        )
        tag = hmac.new(
            self.key, nonce + aad + body, hashlib.sha256
        ).digest()[:16]
        return body + tag

    def decrypt(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
        body, tag = ciphertext[:-16], ciphertext[-16:]
        expected = hmac.new(
            self.key, nonce + aad + body, hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("authentication failed")
        mask = hashlib.sha256(self.key + nonce).digest()
        return bytes(
            value ^ mask[index % len(mask)] for index, value in enumerate(body)
        )


class ExistingKeyProvider:
    def __init__(self, key: bytes) -> None:
        self.key = key
        self.existing_reads = 0
        self.create_reads = 0

    def _value(self) -> tuple[str, bytes]:
        return hashlib.sha256(self.key).hexdigest()[:32], self.key

    def get_existing_key(self) -> tuple[str, bytes]:
        self.existing_reads += 1
        return self._value()

    def get_or_create_key(self) -> tuple[str, bytes]:
        self.create_reads += 1
        return self._value()


class MemoryPersistence:
    is_encrypted = True

    def __init__(self, value: str = "") -> None:
        self.value = value
        self.saves = 0

    def load(self) -> str:
        return self.value

    def save(self, value: str) -> None:
        self.saves += 1
        self.value = value


class MissingPersistence:
    is_encrypted = True

    def load(self) -> str:
        raise FileNotFoundError("persistence is not initialized")

    def save(self, value: str) -> None:
        del value
        raise AssertionError("missing source persistence must remain read-only")


class ConnectionReader:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get_encrypted_mail_archive(self, archive_id: str):
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM mail_archive WHERE archive_id=?", (archive_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_encrypted_mail_archive_attachment(self, record_id: str):
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM mail_archive_attachments "
                "WHERE attachment_record_id=?",
                (record_id,),
            ).fetchone()
            assert row is not None
            return dict(row)


def _context(value: str) -> MutationContext:
    return MutationContext(value, "system", "portable_export_test")


def _nonce_source():
    counter = 0

    def nonce(size: int) -> bytes:
        nonlocal counter
        counter += 1
        return counter.to_bytes(size, "big")

    return nonce


def _source_archive(root: Path):
    database = root / "source.db"
    service = JobSearchLedger(database)
    provider = ExistingKeyProvider(b"S" * 32)
    archive = EncryptedMailArchive(
        service,
        provider,
        cipher_factory=TestCipher,
        nonce_factory=_nonce_source(),
    )
    message = "BEGIN UNTRUSTED EMAIL\nBODY\nprivate-source-message\nEND UNTRUSTED EMAIL"
    saved = archive.archive_message(
        account_id="personal",
        immutable_message_id="message-1",
        sanitized_text=message,
        truncated=False,
        context=_context("portable-message"),
    )
    archive_id = str(saved["archive"]["archive_id"])
    attachment = "BEGIN UNTRUSTED EMAIL\nBODY\nprivate-attachment\nEND UNTRUSTED EMAIL"
    attached = archive.archive_attachment_text(
        archive_id=archive_id,
        immutable_attachment_id="attachment-1",
        mime_type="text/calendar",
        source_size=128,
        source_sha256=hashlib.sha256(b"calendar").hexdigest(),
        extracted_text=attachment,
        context=_context("portable-attachment"),
    )
    return (
        database,
        provider,
        archive_id,
        str(attached["attachment"]["attachment_record_id"]),
        message,
        attachment,
    )


def _autofill_persistence() -> MemoryPersistence:
    value = {
        "version": 1,
        "private_answers": {
            "disability": {
                "values": ["decline_to_answer"],
                "representation": "options",
                "updated_at": "2026-09-03T12:00:00Z",
            }
        },
        "custom_history": [
            {
                "capture_key": "capture-1",
                "application_id": "application-1",
                "ats": "greenhouse",
                "prompt": "Why this role?",
                "normalized_prompt": "why this role",
                "control": "textarea",
                "value": "private-custom-answer",
                "captured_at": "2026-09-03T12:00:00Z",
            }
        ],
    }
    return MemoryPersistence(json.dumps(value))


def _row_key(path: Path, table: str) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(f"SELECT key_id FROM {table}").fetchone()
        assert row is not None
        return str(row[0])


def expect(error_type, callback, text="") -> None:
    try:
        callback()
    except error_type as exc:
        assert text in str(exc)
    else:
        raise AssertionError(f"expected {error_type.__name__}")


def test_exports_verified_portable_copies_without_mutating_sources() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (
            source_db,
            source_provider,
            archive_id,
            attachment_id,
            message,
            attachment,
        ) = _source_archive(root)
        source_vault = root / "source-autofill.bin"
        source_persistence = _autofill_persistence()
        source_database_bytes = source_db.read_bytes()
        source_vault_value = source_persistence.value
        source_key_id = _row_key(source_db, "mail_archive")
        key_file = initialize_portable_master_key(root / "portable-master-key")
        target_db = root / "export" / "job-search.db"
        target_vault = root / "export" / "autofill-vault.bin"

        result = export_portable_state(
            source_database=source_db.resolve(),
            destination_database=target_db.resolve(),
            portable_key_file=key_file.resolve(),
            source_autofill_vault=source_vault.resolve(),
            destination_autofill_vault=target_vault.resolve(),
            source_archive_key_provider=source_provider,
            source_autofill_persistence=source_persistence,
            cipher_factory=TestCipher,
            nonce_factory=_nonce_source(),
            portable_aesgcm_type=TestCipher,
        )

        assert result["mail_archive_messages"] == 1
        assert result["mail_archive_attachments"] == 1
        assert result["autofill_vault_exported"] is True
        assert result["autofill_private_answers"] == 1
        assert result["autofill_custom_history"] == 1
        assert result["outlook_token_cache_exported"] is False
        assert source_provider.existing_reads == 1
        assert source_provider.create_reads == 1  # setup only; export is read-only
        assert source_db.read_bytes() == source_database_bytes
        assert source_persistence.value == source_vault_value
        assert _row_key(source_db, "mail_archive") == source_key_id
        assert _row_key(target_db, "mail_archive") != source_key_id
        assert source_key_id.encode("ascii") not in target_db.read_bytes()
        with sqlite3.connect(target_db) as connection:
            migrated_results = "\n".join(
                str(row[0])
                for row in connection.execute(
                    "SELECT response_json FROM command_results "
                    "WHERE command_name LIKE 'put_mail_archive%'"
                )
            )
        assert source_key_id not in migrated_results
        assert target_db.stat().st_mode & 0o777 == 0o600
        assert target_vault.stat().st_mode & 0o777 == 0o600
        assert b"private-source-message" not in target_db.read_bytes()
        assert "private-custom-answer" not in target_vault.read_text("utf-8")

        portable_provider = PortableArchiveKeyProvider(key_file)
        target_archive = EncryptedMailArchive(
            ConnectionReader(target_db),
            portable_provider,
            cipher_factory=TestCipher,
        )
        assert target_archive.read_message(archive_id) == message
        assert target_archive.read_attachment_text(attachment_id) == attachment
        replay_archive = EncryptedMailArchive(
            JobSearchLedger(target_db),
            portable_provider,
            cipher_factory=TestCipher,
            nonce_factory=_nonce_source(),
        )
        replayed = replay_archive.archive_message(
            account_id="personal",
            immutable_message_id="message-1",
            sanitized_text=message,
            truncated=False,
            context=_context("portable-message"),
        )
        assert replayed["archive"]["archive_id"] == archive_id
        replayed_attachment = replay_archive.archive_attachment_text(
            archive_id=archive_id,
            immutable_attachment_id="attachment-1",
            mime_type="text/calendar",
            source_size=128,
            source_sha256=hashlib.sha256(b"calendar").hexdigest(),
            extracted_text=attachment,
            context=_context("portable-attachment"),
        )
        assert replayed_attachment["attachment"]["attachment_record_id"] == (
            attachment_id
        )
        portable_persistence = EncryptedFilePersistence(
            target_vault,
            key_file,
            "autofill-vault",
            aesgcm_type=TestCipher,
        )
        portable_vault = EncryptedAutofillVault(
            target_vault, persistence=portable_persistence
        )
        assert portable_vault._load()["custom_history"][0]["value"] == (
            "private-custom-answer"
        )


def test_existing_destination_is_never_clobbered_or_partially_published() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_db, provider, *_unused = _source_archive(root)
        key_file = initialize_portable_master_key(root / "portable-master-key")
        target = root / "already-there.db"
        target.write_bytes(b"operator-owned")
        expect(
            FileExistsError,
            lambda: export_portable_state(
                source_database=source_db.resolve(),
                destination_database=target.resolve(),
                portable_key_file=key_file.resolve(),
                source_archive_key_provider=provider,
                cipher_factory=TestCipher,
                nonce_factory=_nonce_source(),
            ),
            "already exists",
        )
        assert target.read_bytes() == b"operator-owned"
        assert provider.existing_reads == 0


def test_failure_rolls_back_staged_database_and_autofill_outputs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_db, provider, *_unused = _source_archive(root)
        key_file = initialize_portable_master_key(root / "portable-master-key")
        target_db = root / "portable.db"
        target_vault = root / "portable-vault.bin"
        malformed_persistence = MemoryPersistence("not-json")
        malformed_source_value = malformed_persistence.value
        source_bytes = source_db.read_bytes()
        expect(
            ContractError,
            lambda: export_portable_state(
                source_database=source_db.resolve(),
                destination_database=target_db.resolve(),
                portable_key_file=key_file.resolve(),
                source_autofill_vault=(root / "source-vault.bin").resolve(),
                destination_autofill_vault=target_vault.resolve(),
                source_archive_key_provider=provider,
                source_autofill_persistence=malformed_persistence,
                cipher_factory=TestCipher,
                nonce_factory=_nonce_source(),
                portable_aesgcm_type=TestCipher,
            ),
            "vault is invalid",
        )
        assert not target_db.exists()
        assert not target_vault.exists()
        assert source_db.read_bytes() == source_bytes
        assert malformed_persistence.saves == 0
        assert malformed_persistence.value == malformed_source_value


def test_autofill_paths_are_paired_and_existing_vault_is_not_clobbered() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_db, provider, *_unused = _source_archive(root)
        key_file = initialize_portable_master_key(root / "portable-master-key")
        target_db = root / "portable.db"
        source_vault = root / "source-vault.bin"
        persistence = _autofill_persistence()
        expect(
            ContractError,
            lambda: export_portable_state(
                source_database=source_db.resolve(),
                destination_database=target_db.resolve(),
                portable_key_file=key_file.resolve(),
                source_autofill_vault=source_vault.resolve(),
                source_archive_key_provider=provider,
                source_autofill_persistence=persistence,
                cipher_factory=TestCipher,
                nonce_factory=_nonce_source(),
            ),
            "must be provided together",
        )
        assert not target_db.exists()
        target_vault = root / "portable-vault.bin"
        target_vault.write_bytes(b"operator-owned-vault")
        expect(
            FileExistsError,
            lambda: export_portable_state(
                source_database=source_db.resolve(),
                destination_database=target_db.resolve(),
                portable_key_file=key_file.resolve(),
                source_autofill_vault=source_vault.resolve(),
                destination_autofill_vault=target_vault.resolve(),
                source_archive_key_provider=provider,
                source_autofill_persistence=persistence,
                cipher_factory=TestCipher,
                nonce_factory=_nonce_source(),
                portable_aesgcm_type=TestCipher,
            ),
            "already exists",
        )
        assert target_vault.read_bytes() == b"operator-owned-vault"
        assert not target_db.exists()


def test_missing_autofill_source_record_fails_without_publishing_outputs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_db = root / "source.db"
        JobSearchLedger(source_db)
        key_file = initialize_portable_master_key(root / "portable-master-key")
        target_db = root / "portable.db"
        target_vault = root / "portable-vault.bin"

        expect(
            ContractError,
            lambda: export_portable_state(
                source_database=source_db.resolve(),
                destination_database=target_db.resolve(),
                portable_key_file=key_file.resolve(),
                source_autofill_vault=(root / "missing-source-vault.bin").resolve(),
                destination_autofill_vault=target_vault.resolve(),
                source_autofill_persistence=MissingPersistence(),
                cipher_factory=TestCipher,
                nonce_factory=_nonce_source(),
                portable_aesgcm_type=TestCipher,
            ),
            "source encrypted autofill vault does not exist",
        )
        assert not target_db.exists()
        assert not target_vault.exists()


def test_initialized_empty_autofill_source_exports_successfully() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_db = root / "source.db"
        JobSearchLedger(source_db)
        key_file = initialize_portable_master_key(root / "portable-master-key")
        target_db = root / "portable.db"
        target_vault = root / "portable-vault.bin"
        source_persistence = MemoryPersistence(
            json.dumps(
                {"version": 1, "private_answers": {}, "custom_history": []}
            )
        )

        result = export_portable_state(
            source_database=source_db.resolve(),
            destination_database=target_db.resolve(),
            portable_key_file=key_file.resolve(),
            source_autofill_vault=(root / "initialized-source-vault.bin").resolve(),
            destination_autofill_vault=target_vault.resolve(),
            source_autofill_persistence=source_persistence,
            cipher_factory=TestCipher,
            nonce_factory=_nonce_source(),
            portable_aesgcm_type=TestCipher,
        )

        assert result["autofill_vault_exported"] is True
        assert result["autofill_private_answers"] == 0
        assert result["autofill_custom_history"] == 0
        assert source_persistence.saves == 0
        portable_persistence = EncryptedFilePersistence(
            target_vault,
            key_file,
            "autofill-vault",
            aesgcm_type=TestCipher,
        )
        portable_vault = EncryptedAutofillVault(
            target_vault, persistence=portable_persistence
        )
        assert portable_vault._load() == {
            "version": 1,
            "private_answers": {},
            "custom_history": [],
        }


def test_empty_archive_copy_does_not_create_or_read_a_source_archive_key() -> None:
    class ForbiddenProvider:
        def get_existing_key(self):
            raise AssertionError("empty archive must not read a source key")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.db"
        JobSearchLedger(source)
        target = root / "portable.db"
        key_file = initialize_portable_master_key(root / "portable-master-key")
        result = export_portable_state(
            source_database=source.resolve(),
            destination_database=target.resolve(),
            portable_key_file=key_file.resolve(),
            source_archive_key_provider=ForbiddenProvider(),
            cipher_factory=TestCipher,
            nonce_factory=_nonce_source(),
        )
        assert result["mail_archive_messages"] == 0
        assert target.exists()


def test_keychain_existing_key_read_never_creates_missing_source_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        persistence = MemoryPersistence()
        provider = KeychainArchiveKeyProvider(
            Path(directory) / "archive-key.bin", persistence=persistence
        )
        expect(
            ContractError,
            provider.get_existing_key,
            "does not exist",
        )
        assert persistence.saves == 0
        assert persistence.value == ""


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda value: value.__name__):
        test()
    print(f"ok ({len(tests)} portable export tests)")


if __name__ == "__main__":
    main()
