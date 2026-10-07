#!/usr/bin/env python3
"""Offline checks for the bounded encrypted archive source used by Hermes."""

from __future__ import annotations

import hashlib
import hmac
import tempfile
from pathlib import Path

from job_search.contracts import ContractError, MutationContext
from job_search.mail.archive import EncryptedMailArchive, KeychainArchiveKeyProvider
from job_search.mail.archive_source import (
    MAX_ARCHIVES_SCANNED,
    MAX_MESSAGE_EXCERPT,
    MAX_RESULT_EXCERPT,
    EncryptedArchiveMailSource,
    build_archive_mail_source,
)
from job_search.mail.sanitizer import sanitize_mail
from job_search.service import JobSearchLedger


class EncryptedPersistence:
    is_encrypted = True

    def __init__(self) -> None:
        self.value = ""

    def load(self):
        return self.value

    def save(self, value):
        self.value = value


class TestCipher:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def encrypt(self, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        mask = hashlib.sha256(self.key + nonce).digest()
        body = bytes(
            value ^ mask[index % len(mask)] for index, value in enumerate(plaintext)
        )
        return body + hmac.new(
            self.key, nonce + aad + body, hashlib.sha256
        ).digest()[:16]

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


def archive_fixture(directory: str):
    path = Path(directory) / "job-search.db"
    ledger = JobSearchLedger(path)
    persistence = EncryptedPersistence()
    provider = KeychainArchiveKeyProvider(
        Path(directory) / "archive-key.bin", persistence=persistence
    )
    archive = EncryptedMailArchive(
        ledger,
        provider,
        cipher_factory=TestCipher,
        nonce_factory=lambda size: b"N" * size,
    )
    return path, ledger, archive


def save_message(
    archive: EncryptedMailArchive,
    suffix: str,
    subject: str,
    body: str,
):
    sanitized = sanitize_mail(subject, body, max_chars=1_000_000)
    return archive.archive_message(
        account_id="outlook-personal",
        immutable_message_id="graph-message-" + suffix,
        sanitized_text=sanitized.text,
        truncated=sanitized.truncated,
        context=MutationContext(
            "archive-source-" + suffix, "system", "archive_source_test"
        ),
    )["archive"]


def test_source_searches_decrypted_all_mail_without_exposing_graph_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, ledger, archive = archive_fixture(directory)
        irrelevant = save_message(
            archive, "account", "Account notice", "This is not a recruiting message."
        )
        target = save_message(
            archive,
            "interview",
            "Interview next steps",
            "A" * 3000 + " Please discuss platform reliability on Thursday. " + "Z" * 100,
        )
        source = EncryptedArchiveMailSource(ledger, archive)
        result = source.search_mail("platform reliability", 10)
        assert len(result) == 1
        assert result[0]["message_id"] == target["archive_id"]
        assert result[0]["subject"] == "Interview next steps"
        assert "platform reliability" in result[0]["excerpt"]
        assert len(result[0]["excerpt"]) <= MAX_RESULT_EXCERPT
        assert irrelevant["archive_id"] != result[0]["message_id"]
        assert "graph-message-interview" not in str(result)
        assert b"platform reliability" not in path.read_bytes()

        message = source.get_mail_message(str(target["archive_id"]))
        assert message["message_id"] == target["archive_id"]
        assert set(message) == {"message_id", "subject", "excerpt"}
        assert len(message["excerpt"]) <= MAX_MESSAGE_EXCERPT

        review = source.get_review_message(str(target["archive_id"]))
        assert review == {
            'subject': 'Interview next steps',
            'body': 'A' * 3000 + ' Please discuss platform reliability on Thursday. ' + 'Z' * 100,
            'truncated': False,
        }


def test_archive_index_is_metadata_only_and_scan_and_result_counts_are_bounded() -> None:
    class Ledger:
        def __init__(self):
            self.limits = []

        def list_mail_archive_index(self, *, limit):
            self.limits.append(limit)
            return tuple({"archive_id": f"archive-{index}"} for index in range(10))

    class Archive:
        def __init__(self):
            self.reads = []

        def read_message(self, archive_id):
            self.reads.append(archive_id)
            return sanitize_mail(
                "Interview", f"matching message {archive_id}", max_chars=2048
            ).text

    ledger = Ledger()
    archive = Archive()
    source = EncryptedArchiveMailSource(ledger, archive, scan_limit=3)
    results = source.search_mail("matching", 2)
    assert ledger.limits == [3]
    assert archive.reads == ["archive-0", "archive-1"]
    assert [item["message_id"] for item in results] == ["archive-0", "archive-1"]
    assert all(set(item) == {"message_id", "subject", "excerpt"} for item in results)
    archive.reads.clear()
    assert source.search_mail("absent", 2) == ()
    assert archive.reads == ["archive-0", "archive-1", "archive-2"]

    with tempfile.TemporaryDirectory() as directory:
        _path, real_ledger, _real_archive = archive_fixture(directory)
        saved = save_message(_real_archive, "index", "Status", "Private")
        index = real_ledger.list_mail_archive_index(limit=1)
        assert set(index[0]) == {
            "archive_id",
            "sanitized_chars",
            "truncated",
            "created_at",
            "updated_at",
        }
        assert index[0]["archive_id"] == saved["archive_id"]
        assert not ({"account_id", "immutable_message_id", "key_id", "ciphertext"} & set(index[0]))


def test_source_rejects_unbounded_inputs_and_factory_accepts_only_archive_capability() -> None:
    class Ledger:
        def list_mail_archive_index(self, *, limit):
            assert limit == 1
            return ()

    class Archive:
        def read_message(self, _archive_id):
            raise AssertionError("invalid request reached archive")

    source = build_archive_mail_source(Ledger(), archive=Archive(), scan_limit=1)
    assert source.search_mail("nothing", 1) == ()
    for query, limit in (("", 1), ("x" * 201, 1), ("ok", 0), ("ok", 26)):
        try:
            source.search_mail(query, limit)
        except ContractError:
            pass
        else:
            raise AssertionError("unbounded archive search was accepted")
    try:
        EncryptedArchiveMailSource(Ledger(), Archive(), scan_limit=MAX_ARCHIVES_SCANNED + 1)
    except ValueError:
        pass
    else:
        raise AssertionError("unbounded archive scan was accepted")
    try:
        source.get_mail_message("bad id")
    except ContractError:
        pass
    else:
        raise AssertionError("invalid archive id was accepted")


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} mail archive source tests)")


if __name__ == "__main__":
    main()
