#!/usr/bin/env python3
"""Offline checks for cloud-portable encrypted state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

from job_search.contracts import ContractError
from job_search.secure_persistence import (
    EncryptedFilePersistence,
    PortableArchiveKeyProvider,
    initialize_portable_master_key,
    read_portable_master_key,
)


def expect(error_type, callback, text=""):
    try:
        callback()
    except error_type as exc:
        assert text in str(exc)
    else:
        raise AssertionError(f"expected {error_type.__name__}")


def test_key_creation_permissions_and_no_overwrite() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "private" / "master-key"
        assert initialize_portable_master_key(path) == path
        assert len(read_portable_master_key(path)) == 32
        assert path.stat().st_mode & 0o777 == 0o600
        expect(FileExistsError, lambda: initialize_portable_master_key(path))

        os.chmod(path, 0o644)
        expect(ContractError, lambda: read_portable_master_key(path), "owner-only")
        os.chmod(path, 0o600)
        link = path.with_name("link")
        link.symlink_to(path)
        expect(ContractError, lambda: read_portable_master_key(link), "owner-only")


def test_encrypted_file_round_trip_and_tamper_rejection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = initialize_portable_master_key(root / "master-key")
        target = root / "state" / "tokens.bin"
        persistence = EncryptedFilePersistence(target, key, "outlook-token-cache")
        expect(FileNotFoundError, persistence.load)
        plaintext = '{"access_token":"private-canary"}'
        persistence.save(plaintext)
        raw = target.read_text(encoding="utf-8")
        assert "private-canary" not in raw
        assert persistence.load() == plaintext
        assert persistence.is_encrypted is True
        assert persistence.get_location() == str(target.resolve())
        assert persistence.time_last_modified() > 0

        value = json.loads(raw)
        assert value["version"] == 2
        assert set(value) == {"version", "purpose", "nonce", "ciphertext"}
        assert "plaintext_sha256" not in value
        assert hashlib.sha256(plaintext.encode("utf-8")).hexdigest() not in raw
        ciphertext = bytearray(base64.b64decode(value["ciphertext"], validate=True))
        ciphertext[0] ^= 1
        value["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
        target.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(target, 0o600)
        expect(ContractError, persistence.load, "authentication failed")


def test_legacy_digest_envelope_is_readable_but_all_new_writes_are_version_two() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = initialize_portable_master_key(root / "master-key")
        target = root / "legacy.bin"
        persistence = EncryptedFilePersistence(target, key, "outlook-token-cache")
        plaintext = b'{"legacy":"private-state"}'
        nonce = os.urandom(12)
        ciphertext = persistence._cipher.encrypt(
            nonce, plaintext, persistence._legacy_aad
        )
        target.write_text(
            json.dumps(
                {
                    "version": 1,
                    "purpose": "outlook-token-cache",
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                    "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(target, 0o600)

        assert persistence.load() == plaintext.decode("utf-8")
        persistence.save("rewritten-private-state")
        rewritten = json.loads(target.read_text(encoding="utf-8"))
        assert rewritten["version"] == 2
        assert "plaintext_sha256" not in rewritten


def test_purposes_and_master_keys_are_isolated() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first_key = initialize_portable_master_key(root / "first-key")
        second_key = initialize_portable_master_key(root / "second-key")
        target = root / "vault.bin"
        EncryptedFilePersistence(target, first_key, "autofill-vault").save("value")
        expect(
            ContractError,
            lambda: EncryptedFilePersistence(
                target, first_key, "outlook-token-cache"
            ).load(),
            "authentication failed",
        )
        expect(
            ContractError,
            lambda: EncryptedFilePersistence(
                target, second_key, "autofill-vault"
            ).load(),
            "authentication failed",
        )

        archive = PortableArchiveKeyProvider(first_key)
        key_id, content_key = archive.get_or_create_key()
        assert len(key_id) == 32 and len(content_key) == 32
        assert archive.get_or_create_key() == (key_id, content_key)


def test_in_place_master_key_change_fails_closed_for_every_key_user() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = initialize_portable_master_key(root / "master-key")
        persistence = EncryptedFilePersistence(
            root / "state.bin", key, "autofill-vault"
        )
        provider = PortableArchiveKeyProvider(key)
        persistence.save("private-state")
        provider.get_or_create_key()
        state_before_key_change = persistence.location.read_bytes()

        key.write_bytes(base64.b64encode(os.urandom(32)) + b"\n")
        os.chmod(key, 0o600)

        expect(ContractError, persistence.load, "changed while process was running")
        expect(
            ContractError,
            lambda: persistence.save("must-not-be-written"),
            "changed while process was running",
        )
        expect(
            ContractError,
            provider.get_or_create_key,
            "changed while process was running",
        )
        expect(
            ContractError,
            provider.get_existing_key,
            "changed while process was running",
        )
        assert persistence.location.read_bytes() == state_before_key_change


def test_atomic_master_key_replacement_fails_closed_even_with_same_key() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = initialize_portable_master_key(root / "master-key")
        persistence = EncryptedFilePersistence(
            root / "state.bin", key, "outlook-token-cache"
        )
        provider = PortableArchiveKeyProvider(key)
        persistence.save("private-state")
        expected_archive_key = provider.get_or_create_key()
        state_before_key_change = persistence.location.read_bytes()

        replacement = root / "replacement-key"
        replacement.write_bytes(key.read_bytes())
        os.chmod(replacement, 0o600)
        os.replace(replacement, key)

        expect(ContractError, persistence.load, "changed while process was running")
        expect(
            ContractError,
            lambda: persistence.save("must-not-be-written"),
            "changed while process was running",
        )
        expect(
            ContractError,
            provider.get_or_create_key,
            "changed while process was running",
        )
        assert expected_archive_key[1] != b""
        assert persistence.location.read_bytes() == state_before_key_change


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in sorted(tests, key=lambda value: value.__name__):
        test()
    print("ok")


if __name__ == "__main__":
    main()
