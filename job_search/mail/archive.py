"""Authenticated, Keychain-backed storage for sanitized Outlook content."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from job_search.contracts import ContractError, MutationContext, canonical_json, payload_sha256


ARCHIVE_VERSION = 1
ARCHIVE_KEY_BYTES = 32
ARCHIVE_NONCE_BYTES = 12
MAX_ARCHIVE_CHARS = 1_000_000
MAX_ATTACHMENT_TEXT_CHARS = 256_000


class ArchiveKeyProvider(Protocol):
    def get_or_create_key(self) -> tuple[str, bytes]: ...


class AuthenticatedCipher(Protocol):
    def encrypt(self, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes: ...

    def decrypt(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes: ...


class AESGCMCipher:
    """Lazy optional AES-256-GCM adapter."""

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != ARCHIVE_KEY_BYTES:
            raise ContractError("mail archive key must be 256 bits")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise ContractError("encrypted mail archive requires cryptography") from exc
        self._cipher = AESGCM(key)

    def encrypt(self, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        return self._cipher.encrypt(nonce, plaintext, aad)

    def decrypt(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
        return self._cipher.decrypt(nonce, ciphertext, aad)


def default_archive_key_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "job-search" / "mail-archive-key.bin"


class KeychainArchiveKeyProvider:
    """Persist only the random archive key through macOS encrypted persistence.

    The large archive remains AES-GCM ciphertext in SQLite. There is deliberately no
    file/plaintext fallback if Keychain persistence is unavailable.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        persistence: Any = None,
        extensions_module: Any = None,
        read_only: bool = False,
    ) -> None:
        self.path = Path(path or default_archive_key_path()).expanduser()
        self._read_only = bool(read_only)
        if self._read_only:
            if not self.path.parent.is_dir():
                raise ContractError("mail archive Keychain location does not exist")
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if persistence is None:
            if sys.platform != "darwin":
                raise ContractError("mail archive Keychain persistence requires macOS")
            if extensions_module is None:
                try:
                    import msal_extensions as extensions_module  # type: ignore[import-not-found]
                except ImportError as exc:  # pragma: no cover - optional live dependency
                    raise ContractError("encrypted mail archive requires msal-extensions") from exc
            try:
                persistence = extensions_module.build_encrypted_persistence(str(self.path))
            except Exception as exc:
                raise ContractError("mail archive Keychain persistence is unavailable") from exc
        if not bool(getattr(persistence, "is_encrypted", False)):
            raise ContractError("refusing plaintext mail archive key persistence")
        self._persistence = persistence

    def _load_key(self) -> bytes | None:
        try:
            raw = self._persistence.load()
        except Exception as exc:
            if self.path.exists() and type(exc).__name__ not in {
                "PersistenceNotFound", "FileNotFoundError",
            }:
                raise ContractError("mail archive key could not be read") from exc
            return None
        if raw:
            try:
                value = json.loads(raw)
                encoded = value["key"]
                if set(value) != {"version", "key"} or value["version"] != ARCHIVE_VERSION:
                    raise ValueError
                key = base64.b64decode(encoded, validate=True)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContractError("mail archive Keychain record is invalid") from exc
            if len(key) != ARCHIVE_KEY_BYTES:
                raise ContractError("mail archive Keychain record has an invalid key")
            return key
        return None

    @staticmethod
    def _identity(key: bytes) -> tuple[str, bytes]:
        key_id = hashlib.sha256(key).hexdigest()[:32]
        return key_id, key

    def get_existing_key(self) -> tuple[str, bytes]:
        """Read an existing archive key without ever creating or replacing it."""

        key = self._load_key()
        if key is None:
            raise ContractError("mail archive Keychain record does not exist")
        return self._identity(key)

    def get_or_create_key(self) -> tuple[str, bytes]:
        key = self._load_key()
        if key is None:
            if self._read_only:
                raise ContractError("mail archive Keychain record does not exist")
            key = secrets.token_bytes(ARCHIVE_KEY_BYTES)
            value = {
                "version": ARCHIVE_VERSION,
                "key": base64.b64encode(key).decode("ascii"),
            }
            try:
                self._persistence.save(canonical_json(value))
            except Exception as exc:
                raise ContractError("mail archive key could not be saved") from exc
        return self._identity(key)


@dataclass(frozen=True)
class SealedText:
    key_id: str
    nonce: bytes
    ciphertext: bytes
    aad_sha256: str
    plaintext_sha256: str
    plaintext_chars: int


class EncryptedMailArchive:
    """Seal/decrypt sanitized text while delegating durable writes to the service."""

    def __init__(
        self,
        service: Any,
        key_provider: ArchiveKeyProvider,
        *,
        cipher_factory: Any = AESGCMCipher,
        nonce_factory: Any = secrets.token_bytes,
    ) -> None:
        self._service = service
        self._key_id, self._key = key_provider.get_or_create_key()
        validator = getattr(key_provider, "assert_unchanged", None)
        self._key_validator = validator if callable(validator) else None
        if len(self._key) != ARCHIVE_KEY_BYTES:
            raise ContractError("mail archive key must be 256 bits")
        self._cipher = cipher_factory(self._key)
        self._nonce_factory = nonce_factory

    def _seal(self, text: str, aad_fields: Mapping[str, Any], limit: int) -> SealedText:
        if self._key_validator is not None:
            self._key_validator()
        if not isinstance(text, str) or not text or len(text) > limit:
            raise ContractError("sanitized archive text is empty or exceeds its limit")
        plaintext = text.encode("utf-8")
        aad = canonical_json(aad_fields).encode("utf-8")
        nonce = self._nonce_factory(ARCHIVE_NONCE_BYTES)
        if not isinstance(nonce, bytes) or len(nonce) != ARCHIVE_NONCE_BYTES:
            raise ContractError("mail archive nonce source is invalid")
        ciphertext = self._cipher.encrypt(nonce, plaintext, aad)
        if not isinstance(ciphertext, bytes) or len(ciphertext) < 16:
            raise ContractError("mail archive cipher returned invalid ciphertext")
        return SealedText(
            self._key_id,
            nonce,
            ciphertext,
            hashlib.sha256(aad).hexdigest(),
            hashlib.sha256(plaintext).hexdigest(),
            len(text),
        )

    def archive_message(
        self,
        *,
        account_id: str,
        immutable_message_id: str,
        sanitized_text: str,
        truncated: bool,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        aad_fields = {
            "version": ARCHIVE_VERSION,
            "kind": "message",
            "account_id": account_id,
            "immutable_message_id": immutable_message_id,
        }
        sealed = self._seal(sanitized_text, aad_fields, MAX_ARCHIVE_CHARS)
        return self._service.put_mail_archive(
            {
                "account_id": account_id,
                "immutable_message_id": immutable_message_id,
                "key_id": sealed.key_id,
                "nonce": sealed.nonce,
                "ciphertext": sealed.ciphertext,
                "aad_sha256": sealed.aad_sha256,
                "sanitized_sha256": sealed.plaintext_sha256,
                "sanitized_chars": sealed.plaintext_chars,
                "truncated": bool(truncated),
            },
            context,
        )

    def archive_attachment_text(
        self,
        *,
        archive_id: str,
        immutable_attachment_id: str,
        mime_type: str,
        source_size: int,
        source_sha256: str,
        extracted_text: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        aad_fields = {
            "version": ARCHIVE_VERSION,
            "kind": "attachment_text",
            "archive_id": archive_id,
            "immutable_attachment_id": immutable_attachment_id,
            "mime_type": mime_type,
            "source_sha256": source_sha256,
        }
        sealed = self._seal(extracted_text, aad_fields, MAX_ATTACHMENT_TEXT_CHARS)
        return self._service.put_mail_archive_attachment(
            {
                "archive_id": archive_id,
                "immutable_attachment_id": immutable_attachment_id,
                "mime_type": mime_type,
                "source_size": source_size,
                "source_sha256": source_sha256,
                "extracted_sha256": sealed.plaintext_sha256,
                "extracted_chars": sealed.plaintext_chars,
                "key_id": sealed.key_id,
                "nonce": sealed.nonce,
                "ciphertext": sealed.ciphertext,
                "aad_sha256": sealed.aad_sha256,
            },
            context,
        )

    def read_message(self, archive_id: str) -> str:
        record = self._service.get_encrypted_mail_archive(archive_id)
        aad_fields = {
            "version": ARCHIVE_VERSION,
            "kind": "message",
            "account_id": record["account_id"],
            "immutable_message_id": record["immutable_message_id"],
        }
        return self._open(record, aad_fields, "sanitized_sha256")

    def read_attachment_text(self, attachment_record_id: str) -> str:
        record = self._service.get_encrypted_mail_archive_attachment(
            attachment_record_id
        )
        aad_fields = {
            "version": ARCHIVE_VERSION,
            "kind": "attachment_text",
            "archive_id": record["archive_id"],
            "immutable_attachment_id": record["immutable_attachment_id"],
            "mime_type": record["mime_type"],
            "source_sha256": record["source_sha256"],
        }
        return self._open(record, aad_fields, "extracted_sha256")

    def _open(
        self, record: Mapping[str, Any], aad_fields: Mapping[str, Any], digest_field: str
    ) -> str:
        if self._key_validator is not None:
            self._key_validator()
        if record["key_id"] != self._key_id:
            raise ContractError("mail archive key is unavailable")
        aad = canonical_json(aad_fields).encode("utf-8")
        if hashlib.sha256(aad).hexdigest() != record["aad_sha256"]:
            raise ContractError("mail archive authenticated metadata does not match")
        try:
            plaintext = self._cipher.decrypt(record["nonce"], record["ciphertext"], aad)
            text = plaintext.decode("utf-8")
        except Exception as exc:
            raise ContractError("mail archive authentication failed") from exc
        if hashlib.sha256(plaintext).hexdigest() != record[digest_field]:
            raise ContractError("mail archive plaintext digest does not match")
        return text


def archive_context(prefix: str, values: Mapping[str, Any]) -> MutationContext:
    return MutationContext(
        f"{prefix}:" + payload_sha256(values), "system", "outlook_secure_archive"
    )
