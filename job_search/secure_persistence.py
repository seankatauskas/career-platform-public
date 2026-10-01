"""Portable encrypted persistence backed by one externally managed master key.

macOS deployments keep using Keychain through ``msal-extensions`` by default. A
headless Linux/container deployment can instead mount one owner-only 256-bit secret
from its secret manager. Purpose-derived AES-GCM keys keep Outlook, autofill, and mail
archive material cryptographically separate while all persisted files remain
authenticated ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Tuple

from .contracts import ContractError


MASTER_KEY_BYTES = 32
NONCE_BYTES = 12
ENVELOPE_VERSION = 2
LEGACY_ENVELOPE_VERSION = 1
MAX_KEY_FILE_BYTES = 256
MAX_PLAINTEXT_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE_BYTES = 12 * 1024 * 1024
_PURPOSE = re.compile(r"[a-z][a-z0-9.-]{0,127}")


_FileIdentity = Tuple[int, int, int, int, int, int, int, int]
_KeyAttestation = Tuple[_FileIdentity, bytes]


def _file_identity(info: os.stat_result) -> _FileIdentity:
    """Return metadata that changes for replacement or in-place modification."""

    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_uid),
        int(stat.S_IMODE(info.st_mode)),
        int(info.st_nlink),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
    )


def _read_owner_only_with_identity(
    path: Path, maximum: int, label: str
) -> tuple[bytes, _FileIdentity]:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ContractError(f"{label} path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise ContractError(f"{label} must be an owner-only regular file") from exc
    try:
        before = os.fstat(descriptor)
        current_uid = getattr(os, "geteuid", lambda: before.st_uid)()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != current_uid
            or before.st_mode & 0o077
            or before.st_nlink != 1
        ):
            raise ContractError(f"{label} must be owner-only (mode 0600 or stricter)")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = _file_identity(after)
    if identity != _file_identity(before):
        raise ContractError(f"{label} changed while being read")
    try:
        path_identity = _file_identity(os.lstat(target))
    except OSError as exc:
        raise ContractError(f"{label} changed while being read") from exc
    if path_identity != identity:
        raise ContractError(f"{label} changed while being read")
    value = b"".join(chunks)
    if not value or len(value) > maximum:
        raise ContractError(f"{label} is empty or too large")
    return value, identity


def _read_owner_only(path: Path, maximum: int, label: str) -> bytes:
    value, _ = _read_owner_only_with_identity(path, maximum, label)
    return value


def _decode_portable_master_key(encoded: bytes) -> bytes:
    if len(encoded) == MASTER_KEY_BYTES:
        return encoded
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise ContractError("portable encryption key is not valid base64") from exc
    if len(key) != MASTER_KEY_BYTES:
        raise ContractError("portable encryption key must contain exactly 256 bits")
    return key


def _read_portable_master_key_attested(
    path: Path,
) -> tuple[bytes, _KeyAttestation]:
    encoded, identity = _read_owner_only_with_identity(
        path, MAX_KEY_FILE_BYTES, "portable encryption key"
    )
    key = _decode_portable_master_key(encoded)
    fingerprint = hashlib.sha256(
        b"job-search/portable-key-attestation/v1/" + key
    ).digest()
    return key, (identity, fingerprint)


def _assert_portable_master_key_unchanged(
    path: Path, expected: _KeyAttestation
) -> None:
    _, observed = _read_portable_master_key_attested(path)
    identity_matches = observed[0] == expected[0]
    fingerprint_matches = hmac.compare_digest(observed[1], expected[1])
    if not identity_matches or not fingerprint_matches:
        raise ContractError(
            "portable encryption key changed while process was running"
        )


def read_portable_master_key(path: Path) -> bytes:
    """Read exactly 256 bits from an owner-only raw or base64 key file."""

    key, _ = _read_portable_master_key_attested(path)
    return key


def initialize_portable_master_key(path: Path) -> Path:
    """Create a new mode-0600 base64 master key without overwriting any file."""

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ContractError("portable encryption key path must be absolute")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(target, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        value = base64.b64encode(secrets.token_bytes(MASTER_KEY_BYTES)) + b"\n"
        os.write(descriptor, value)
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        target.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    return target


def _derived_key(master: bytes, purpose: str) -> bytes:
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise ContractError("encrypted persistence purpose is invalid")
    return hmac.new(
        master,
        b"job-search/portable-encryption/v1/" + purpose.encode("ascii"),
        hashlib.sha256,
    ).digest()


def _existing_target_is_safe(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == current_uid
        and not info.st_mode & 0o077
        and info.st_nlink == 1
    )


class EncryptedFilePersistence:
    """The small persistence interface consumed by ``msal-extensions`` and vaults."""

    is_encrypted = True

    def __init__(
        self,
        location: Path,
        key_file: Path,
        purpose: str,
        *,
        maximum_plaintext_bytes: int = MAX_PLAINTEXT_BYTES,
        aesgcm_type: Any = None,
    ) -> None:
        target = Path(location).expanduser()
        key_path = Path(key_file).expanduser()
        if not target.is_absolute() or not key_path.is_absolute():
            raise ContractError("encrypted persistence paths must be absolute")
        if (
            isinstance(maximum_plaintext_bytes, bool)
            or not isinstance(maximum_plaintext_bytes, int)
            or not 1 <= maximum_plaintext_bytes <= MAX_PLAINTEXT_BYTES
        ):
            raise ContractError("encrypted persistence size limit is invalid")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.location = target.parent.resolve() / target.name
        self.key_file = key_path
        self.purpose = purpose
        self.maximum_plaintext_bytes = maximum_plaintext_bytes
        master_key, self._key_attestation = _read_portable_master_key_attested(key_path)
        key = _derived_key(master_key, purpose)
        if aesgcm_type is None:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except ImportError as exc:  # pragma: no cover - optional live dependency
                raise ContractError(
                    "portable encrypted persistence requires cryptography"
                ) from exc
            aesgcm_type = AESGCM
        self._cipher = aesgcm_type(key)
        self._legacy_aad = (
            b"job-search-encrypted-persistence-v1:" + purpose.encode("ascii")
        )
        self._aad = b"job-search-encrypted-persistence-v2:" + purpose.encode("ascii")
        self._lock = threading.RLock()

    def get_location(self) -> str:
        return str(self.location)

    def time_last_modified(self) -> float:
        try:
            return self.location.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def load(self) -> str:
        with self._lock:
            _assert_portable_master_key_unchanged(
                self.key_file, self._key_attestation
            )
            try:
                raw = _read_owner_only(
                    self.location, MAX_ENVELOPE_BYTES, "encrypted persistence file"
                )
            except ContractError as exc:
                if not self.location.exists():
                    raise FileNotFoundError(str(self.location)) from exc
                raise
            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError
                version = value.get("version")
                expected_fields = {
                    "version",
                    "purpose",
                    "nonce",
                    "ciphertext",
                }
                if version == LEGACY_ENVELOPE_VERSION:
                    expected_fields.add("plaintext_sha256")
                if (
                    not isinstance(version, int)
                    or isinstance(version, bool)
                    or version not in {LEGACY_ENVELOPE_VERSION, ENVELOPE_VERSION}
                    or set(value) != expected_fields
                    or value["purpose"] != self.purpose
                ):
                    raise ValueError
                if version == LEGACY_ENVELOPE_VERSION and (
                    not isinstance(value["plaintext_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["plaintext_sha256"])
                ):
                    raise ValueError
                nonce = base64.b64decode(value["nonce"], validate=True)
                ciphertext = base64.b64decode(value["ciphertext"], validate=True)
                if len(nonce) != NONCE_BYTES or len(ciphertext) < 16:
                    raise ValueError
                aad = (
                    self._legacy_aad
                    if version == LEGACY_ENVELOPE_VERSION
                    else self._aad
                )
                plaintext = self._cipher.decrypt(nonce, ciphertext, aad)
                if len(plaintext) > self.maximum_plaintext_bytes:
                    raise ValueError
                if version == LEGACY_ENVELOPE_VERSION and not hmac.compare_digest(
                    hashlib.sha256(plaintext).hexdigest(), value["plaintext_sha256"]
                ):
                    raise ValueError
                return plaintext.decode("utf-8")
            except Exception as exc:
                raise ContractError(
                    "encrypted persistence authentication failed"
                ) from exc

    def save(self, content: str) -> None:
        if not isinstance(content, str):
            raise ContractError("encrypted persistence content must be text")
        plaintext = content.encode("utf-8")
        if len(plaintext) > self.maximum_plaintext_bytes:
            raise ContractError("encrypted persistence content is too large")
        with self._lock:
            _assert_portable_master_key_unchanged(
                self.key_file, self._key_attestation
            )
            nonce = secrets.token_bytes(NONCE_BYTES)
            ciphertext = self._cipher.encrypt(nonce, plaintext, self._aad)
            envelope = json.dumps(
                {
                    "version": ENVELOPE_VERSION,
                    "purpose": self.purpose,
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(envelope) > MAX_ENVELOPE_BYTES:
                raise ContractError("encrypted persistence envelope is too large")
            if not _existing_target_is_safe(self.location):
                raise ContractError(
                    "encrypted persistence file must be owner-only (mode 0600)"
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.location.name}.", dir=str(self.location.parent)
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(envelope)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.location)
                os.chmod(self.location, 0o600, follow_symlinks=False)
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                temporary.unlink(missing_ok=True)
                raise


class PortableArchiveKeyProvider:
    """Derive the archive key directly from the mounted portable master key."""

    def __init__(self, key_file: Path) -> None:
        self._key_file = Path(key_file).expanduser()
        master_key, self._key_attestation = _read_portable_master_key_attested(
            self._key_file
        )
        self._key = _derived_key(master_key, "mail-archive-content")
        self._lock = threading.RLock()

    def assert_unchanged(self) -> None:
        """Fail closed if the mounted key file no longer matches startup state."""

        with self._lock:
            _assert_portable_master_key_unchanged(
                self._key_file, self._key_attestation
            )

    def get_or_create_key(self) -> tuple[str, bytes]:
        with self._lock:
            self.assert_unchanged()
            return hashlib.sha256(self._key).hexdigest()[:32], self._key

    def get_existing_key(self) -> tuple[str, bytes]:
        """Return the derived key using the read-only archive-export contract."""

        return self.get_or_create_key()


__all__ = [
    "EncryptedFilePersistence",
    "PortableArchiveKeyProvider",
    "initialize_portable_master_key",
    "read_portable_master_key",
]
