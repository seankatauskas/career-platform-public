"""Private Unix-socket bridge between the deterministic core and Hermes.

The control-plane worker must not receive Hermes provider or messaging credentials.
In the cloud topology Hermes therefore owns those credentials and exposes only this
bounded ``ping``/``send`` protocol over an owner-only Unix socket.  The same module is
copied into the pinned Hermes image and can supervise its foreground gateway process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import signal
import socket
import socketserver
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
SERVICE_REVISION = "hermes-delivery-bridge-v2"
MAX_FRAME_BYTES = 20 * 1024
MAX_MESSAGE_BYTES = 16 * 1024
DEFAULT_TIMEOUT_SECONDS = 40.0
MAX_MCP_TOKEN_BYTES = 16 * 1024
_TARGET = re.compile(r"[A-Za-z0-9_-]+(?::[^\s\x00-\x1f\x7f]+)*\Z")
_DELIVERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class HermesDeliveryBridgeError(RuntimeError):
    """A bounded delivery request failed before a trusted result was returned."""

    def __init__(self, message: str, *, retryable: bool = True, code: str = "request_rejected") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def _canonical(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise HermesDeliveryBridgeError(
            "Hermes delivery message is not finite JSON", retryable=False
        ) from exc
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        raise HermesDeliveryBridgeError(
            "Hermes delivery message exceeds its size limit", retryable=False
        )
    return encoded + b"\n"


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _parse_frame(raw: bytes) -> Mapping[str, Any]:
    if not raw or len(raw) > MAX_FRAME_BYTES + 1 or not raw.endswith(b"\n"):
        raise HermesDeliveryBridgeError(
            "Hermes delivery message is incomplete or too large", retryable=False
        )
    try:
        value = json.loads(raw[:-1].decode("utf-8"), parse_constant=_reject_constant)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise HermesDeliveryBridgeError(
            "Hermes delivery message is invalid JSON", retryable=False
        ) from exc
    if not isinstance(value, Mapping):
        raise HermesDeliveryBridgeError(
            "Hermes delivery message must be an object", retryable=False
        )
    return value


def _safe_socket(path: Path, *, require_exists: bool) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise HermesDeliveryBridgeError(
            "Hermes delivery socket path must be absolute", retryable=False
        )
    try:
        parent = target.parent.resolve(strict=True)
        parent_info = parent.stat()
    except OSError as exc:
        raise HermesDeliveryBridgeError(
            "Hermes delivery socket directory is unavailable"
        ) from exc
    current_uid = getattr(os, "geteuid", lambda: parent_info.st_uid)()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != current_uid
        or parent_info.st_mode & 0o077
    ):
        raise HermesDeliveryBridgeError(
            "Hermes delivery socket directory must be owner-only", retryable=False
        )
    normalized = parent / target.name
    try:
        info = os.lstat(normalized)
    except FileNotFoundError:
        if require_exists:
            raise HermesDeliveryBridgeError(
                "Hermes delivery socket is unavailable"
            ) from None
        return normalized
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != current_uid
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise HermesDeliveryBridgeError(
            "Hermes delivery socket is unsafe", retryable=False
        )
    return normalized


def _validate_text(title: Any, body: Any) -> tuple[str, str, str]:
    if not isinstance(title, str) or not isinstance(body, str):
        raise HermesDeliveryBridgeError(
            "Hermes delivery text is invalid", retryable=False
        )
    clean_title = title.strip()
    clean_body = body.strip()
    if not 1 <= len(clean_title) <= 200 or len(clean_body) > 2000:
        raise HermesDeliveryBridgeError(
            "Hermes delivery text is outside its limits", retryable=False
        )
    if any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"}
        for character in clean_title + clean_body
    ):
        raise HermesDeliveryBridgeError(
            "Hermes delivery text contains control characters", retryable=False
        )
    message = clean_title + ("\n\n" + clean_body if clean_body else "")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise HermesDeliveryBridgeError(
            "Hermes delivery message exceeds its byte limit", retryable=False
        )
    return clean_title, clean_body, message


def _validate_target(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise HermesDeliveryBridgeError(
            "Hermes delivery target is invalid", retryable=False
        )
    target = value.strip()
    if target != value or not _TARGET.fullmatch(target):
        raise HermesDeliveryBridgeError(
            "Hermes delivery target is invalid", retryable=False
        )
    return target


def _target_fingerprint(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()


def delivery_fingerprint(target: str, title: str, body: str) -> str:
    clean_title, clean_body, _ = _validate_text(title, body)
    return hashlib.sha256(_canonical({"target": _validate_target(target),
        "title": clean_title, "body": clean_body})).hexdigest()


def _read_owner_only_token(path: Path, *, expected_uid: int | None = None) -> str:
    target = Path(path)
    if not target.is_absolute():
        raise HermesDeliveryBridgeError(
            "Hermes MCP token path must be absolute", retryable=False
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise HermesDeliveryBridgeError(
            "Hermes MCP token is unavailable", retryable=False
        ) from exc
    try:
        info = os.fstat(descriptor)
        current_uid = (
            expected_uid
            if expected_uid is not None
            else getattr(os, "geteuid", lambda: info.st_uid)()
        )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != current_uid
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 32 <= info.st_size <= MAX_MCP_TOKEN_BYTES
        ):
            raise HermesDeliveryBridgeError(
                "Hermes MCP token must be an owner-only regular file",
                retryable=False,
            )
        chunks: list[bytes] = []
        remaining = MAX_MCP_TOKEN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        value = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HermesDeliveryBridgeError(
            "Hermes MCP token is invalid", retryable=False
        ) from exc
    if not 32 <= len(value) <= MAX_MCP_TOKEN_BYTES or any(
        character.isspace() for character in value
    ):
        raise HermesDeliveryBridgeError("Hermes MCP token is invalid", retryable=False)
    return value


def install_mcp_environment(
    token_file: Path,
    environment_directory: Path,
    *,
    owner_name: str = "hermes",
) -> None:
    """Install one validated token into s6's in-memory environment directory."""

    try:
        owner_uid = pwd.getpwnam(owner_name).pw_uid
    except (KeyError, TypeError) as exc:
        raise HermesDeliveryBridgeError(
            "Hermes runtime owner is unavailable", retryable=False
        ) from exc
    directory = Path(environment_directory)
    if not directory.is_absolute():
        raise HermesDeliveryBridgeError(
            "Hermes environment directory must be absolute", retryable=False
        )
    try:
        resolved = directory.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise HermesDeliveryBridgeError(
            "Hermes environment directory is unavailable", retryable=False
        ) from exc
    current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != current_uid
        or info.st_mode & 0o022
    ):
        raise HermesDeliveryBridgeError(
            "Hermes environment directory is unsafe", retryable=False
        )
    token = _read_owner_only_token(token_file, expected_uid=owner_uid)
    destination = resolved / "JOB_SEARCH_MCP_TOKEN"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise HermesDeliveryBridgeError(
            "Hermes MCP environment already exists", retryable=False
        ) from exc
    except OSError as exc:
        raise HermesDeliveryBridgeError(
            "Hermes MCP environment cannot be installed", retryable=False
        ) from exc
    try:
        encoded = token.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class HermesDeliveryClient:
    """One-request-per-connection client used by the notification outbox."""

    def __init__(
        self,
        socket_path: Path,
        *,
        expected_target: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not 1 <= float(timeout_seconds) <= 120:
            raise ValueError(
                "Hermes delivery timeout must be between 1 and 120 seconds"
            )
        self.socket_path = Path(socket_path).expanduser()
        self.expected_target = (
            _validate_target(expected_target) if expected_target is not None else None
        )
        self.timeout_seconds = float(timeout_seconds)

    def _call(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in {"ping", "send", "status", "reconcile"}:
            raise HermesDeliveryBridgeError(
                "Hermes delivery operation is invalid", retryable=False
            )
        target = _safe_socket(self.socket_path, require_exists=True)
        request = _canonical(
            {
                "version": PROTOCOL_VERSION,
                "operation": operation,
                "payload": dict(payload),
            }
        )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(str(target))
            connection.sendall(request)
            # A newline completes this protocol frame.  Avoid half-closing after
            # sendall: a fast peer can reply and close first, making macOS report
            # ENOTCONN even though the complete response remains readable.
            chunks: list[bytes] = []
            remaining = MAX_FRAME_BYTES + 2
            while remaining:
                chunk = connection.recv(min(16 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
                if b"\n" in chunk:
                    break
        except (OSError, TimeoutError) as exc:
            raise HermesDeliveryBridgeError(
                "Hermes delivery service is unavailable"
            ) from exc
        finally:
            connection.close()
        response = _parse_frame(b"".join(chunks))
        if set(response) != {"version", "ok", "result", "error", "retryable"}:
            raise HermesDeliveryBridgeError(
                "Hermes delivery response has an invalid shape", retryable=False
            )
        if (
            response.get("version") != PROTOCOL_VERSION
            or not isinstance(response.get("ok"), bool)
            or not isinstance(response.get("retryable"), bool)
        ):
            raise HermesDeliveryBridgeError(
                "Hermes delivery response has an invalid version", retryable=False
            )
        if not response["ok"]:
            code = response.get("error")
            if code not in {"delivery_reconciliation_required", "delivery_payload_conflict"}:
                code = "request_rejected"
            raise HermesDeliveryBridgeError(
                "Hermes delivery service rejected the request",
                retryable=bool(response["retryable"]),
                code=code,
            )
        if response["error"] is not None or not isinstance(
            response.get("result"), Mapping
        ):
            raise HermesDeliveryBridgeError(
                "Hermes delivery response has an invalid result", retryable=False
            )
        return response["result"]

    def ping(self) -> Mapping[str, Any]:
        result = self._call("ping", {})
        if self.expected_target is not None and result.get(
            "target_fingerprint"
        ) != _target_fingerprint(self.expected_target):
            raise HermesDeliveryBridgeError(
                "Hermes delivery target does not match the sidecar",
                retryable=False,
            )
        return result

    def send(self, delivery_id: str, title: str, body: str) -> None:
        if not isinstance(delivery_id, str) or not _DELIVERY_ID.fullmatch(delivery_id):
            raise HermesDeliveryBridgeError(
                "Hermes delivery id is invalid", retryable=False
            )
        clean_title, clean_body, _message = _validate_text(title, body)
        if self.expected_target is not None:
            self.ping()
        result = self._call(
            "send",
            {
                "delivery_id": delivery_id,
                "title": clean_title,
                "body": clean_body,
            },
        )
        if result != {"delivered": True}:
            raise HermesDeliveryBridgeError(
                "Hermes delivery response was not an acknowledgement", retryable=False
            )

    def status(self, delivery_id: str) -> Mapping[str, Any]:
        if self.expected_target is not None:
            self.ping()
        return self._call("status", {"delivery_id": delivery_id})

    def reconcile(self, delivery_id: str, *, expected_attempts: int,
                  expected_payload_sha256: str, outcome: str) -> Mapping[str, Any]:
        """Caller must obtain the user's exact decision outside this private bridge."""
        if self.expected_target is not None:
            self.ping()
        return self._call("reconcile", {"delivery_id": delivery_id,
            "expected_attempts": expected_attempts,
            "expected_payload_sha256": expected_payload_sha256, "outcome": outcome})


class DeliveryReceiptStore:
    """Durable intent and receipt guard; uncertain sends require a user decision."""

    def __init__(self, path: Path) -> None:
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise HermesDeliveryBridgeError(
                "Hermes receipt database path must be absolute", retryable=False
            )
        try:
            parent = target.parent.resolve(strict=True)
            info = parent.stat()
        except OSError as exc:
            raise HermesDeliveryBridgeError(
                "Hermes receipt database directory is unavailable", retryable=False
            ) from exc
        current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != current_uid
            or info.st_mode & 0o077
        ):
            raise HermesDeliveryBridgeError(
                "Hermes receipt database directory must be owner-only",
                retryable=False,
            )
        self.path = parent / target.name
        try:
            existing = os.lstat(self.path)
        except FileNotFoundError:
            descriptor = os.open(
                self.path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
        else:
            if (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid != current_uid
                or existing.st_mode & 0o077
                or existing.st_nlink != 1
            ):
                raise HermesDeliveryBridgeError(
                    "Hermes receipt database is unsafe", retryable=False
                )
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS delivered ("
                "delivery_id TEXT PRIMARY KEY, delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS delivery_attempts ("
                "delivery_id TEXT PRIMARY KEY,payload_sha256 TEXT,state TEXT NOT NULL "
                "CHECK(state IN ('in_flight','retryable','delivered','reconciliation_required','abandoned','legacy_delivered')),"
                "attempts INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            # Legacy receipts prove a send completed but cannot verify a payload.
            # Preserve them indefinitely rather than silently allowing a resend.
            connection.execute(
                "INSERT OR IGNORE INTO delivery_attempts(delivery_id,state,attempts,updated_at) "
                "SELECT delivery_id,'legacy_delivered',1,delivered_at FROM delivered"
            )
            connection.execute("PRAGMA user_version=2")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def contains(self, delivery_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM delivery_attempts WHERE delivery_id=? AND state IN ('delivered','legacy_delivered')", (delivery_id,)
            ).fetchone()
        return row is not None

    def status(self, delivery_id: str) -> Mapping[str, Any]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM delivery_attempts WHERE delivery_id=?", (delivery_id,)).fetchone()
        return dict(row) if row is not None else {"delivery_id": delivery_id, "state": "not_found", "attempts": 0}

    def begin(self, delivery_id: str, payload_sha256: str) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_sha256,state FROM delivery_attempts WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if row is not None:
                if row[0] is None:
                    connection.commit()
                    return "legacy_delivered"
                if row[0] != payload_sha256:
                    raise HermesDeliveryBridgeError("delivery id belongs to a different payload",
                        retryable=False, code="delivery_payload_conflict")
                if row[1] == "in_flight":
                    connection.execute("UPDATE delivery_attempts SET state='reconciliation_required',updated_at=CURRENT_TIMESTAMP WHERE delivery_id=?", (delivery_id,))
                    connection.commit()
                    return "reconciliation_required"
                if row[1] != "retryable":
                    connection.commit()
                    return str(row[1])
                connection.execute("UPDATE delivery_attempts SET state='in_flight',attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE delivery_id=?", (delivery_id,))
            else:
                connection.execute("INSERT INTO delivery_attempts(delivery_id,payload_sha256,state,attempts) VALUES (?,?,'in_flight',1)", (delivery_id, payload_sha256))
            connection.commit()
            return "in_flight"

    def finish(self, delivery_id: str, state: str) -> None:
        if state not in {"delivered", "retryable", "reconciliation_required"}:
            raise ValueError("invalid delivery completion state")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute("UPDATE delivery_attempts SET state=?,updated_at=CURRENT_TIMESTAMP WHERE delivery_id=? AND state='in_flight'", (state, delivery_id))
            if updated.rowcount != 1:
                raise HermesDeliveryBridgeError("delivery attempt state changed", retryable=False,
                    code="delivery_reconciliation_required")
            connection.commit()

    def reconcile(self, delivery_id: str, *, expected_attempts: int,
                  expected_payload_sha256: str, outcome: str) -> Mapping[str, Any]:
        states = {"delivered": "delivered", "not_delivered": "retryable", "abandoned": "abandoned"}
        if not isinstance(outcome, str) or outcome not in states or isinstance(expected_attempts, bool) or not isinstance(expected_attempts, int) or expected_attempts < 1:
            raise HermesDeliveryBridgeError("invalid reconciliation decision", retryable=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload_sha256,state,attempts FROM delivery_attempts WHERE delivery_id=?", (delivery_id,)).fetchone()
            if (row is None or row[0] != expected_payload_sha256 or row[2] != expected_attempts
                or row[1] not in {"in_flight", "reconciliation_required", states[outcome]}):
                raise HermesDeliveryBridgeError("reconciliation state changed", retryable=False)
            connection.execute("UPDATE delivery_attempts SET state=?,updated_at=CURRENT_TIMESTAMP WHERE delivery_id=?", (states[outcome], delivery_id))
            connection.commit()
        return self.status(delivery_id)


class HermesDispatcher:
    """Fixed-command adapter that never exposes a general Hermes execution surface."""

    def __init__(
        self,
        executable: Path,
        *,
        target: str,
        receipts: DeliveryReceiptStore,
        timeout_seconds: int = 30,
        gateway: GatewaySupervisor | None = None,
        gateway_service: Path | None = None,
    ) -> None:
        executable_path = Path(executable)
        if (
            not executable_path.is_absolute()
            or not executable_path.is_file()
            or not os.access(executable_path, os.X_OK)
        ):
            raise HermesDeliveryBridgeError(
                "Hermes executable is unavailable", retryable=False
            )
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("Hermes send timeout must be between 1 and 120 seconds")
        self.executable = executable_path
        self.target = _validate_target(target)
        self.receipts = receipts
        self.timeout_seconds = timeout_seconds
        self.gateway = gateway
        self.gateway_service = Path(gateway_service) if gateway_service else None
        if self.gateway_service is not None and (
            gateway is not None or not self.gateway_service.is_absolute()
        ):
            raise HermesDeliveryBridgeError(
                "Hermes gateway status configuration is invalid", retryable=False
            )

    def _gateway_running(self) -> bool:
        if self.gateway is not None:
            return self.gateway.running
        if self.gateway_service is None:
            return False
        status_executable = Path("/command/s6-svstat")
        if not status_executable.is_file() or not os.access(status_executable, os.X_OK):
            return False
        try:
            result = subprocess.run(
                (str(status_executable), "-u", str(self.gateway_service)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        # s6-svstat exits successfully for both up and down services. Its -u
        # output, rather than its exit code alone, identifies the running state.
        return result.returncode == 0 and result.stdout.strip() == "true"

    def dispatch(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation == "ping":
            if payload:
                raise HermesDeliveryBridgeError(
                    "Hermes ping payload must be empty", retryable=False
                )
            return {
                "service_revision": SERVICE_REVISION,
                "gateway_running": self._gateway_running(),
                "target_fingerprint": _target_fingerprint(self.target),
            }
        if operation in {"status", "reconcile"}:
            keys = {"delivery_id"} if operation == "status" else {"delivery_id", "expected_attempts", "expected_payload_sha256", "outcome"}
            if set(payload) != keys or not isinstance(payload.get("delivery_id"), str) or not _DELIVERY_ID.fullmatch(payload["delivery_id"]):
                raise HermesDeliveryBridgeError("invalid receipt request", retryable=False)
            if operation == "status":
                return self.receipts.status(payload["delivery_id"])
            return self.receipts.reconcile(**payload)
        if operation != "send" or set(payload) != {
            "delivery_id",
            "title",
            "body",
        }:
            raise HermesDeliveryBridgeError(
                "Hermes delivery payload is invalid", retryable=False
            )
        delivery_id = payload["delivery_id"]
        if not isinstance(delivery_id, str) or not _DELIVERY_ID.fullmatch(delivery_id):
            raise HermesDeliveryBridgeError(
                "Hermes delivery id is invalid", retryable=False
            )
        _title, _body, message = _validate_text(payload["title"], payload["body"])
        fingerprint = delivery_fingerprint(self.target, _title, _body)
        state = self.receipts.begin(delivery_id, fingerprint)
        if state in {"delivered", "legacy_delivered"}:
            return {"delivered": True}
        if state != "in_flight":
            raise HermesDeliveryBridgeError("delivery needs an explicit reconciliation decision",
                retryable=False, code="delivery_reconciliation_required")
        try:
            process = subprocess.Popen(
                (str(self.executable), "send", "--to", self.target),
                stdin=subprocess.PIPE,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            self.receipts.finish(delivery_id, "retryable")
            raise HermesDeliveryBridgeError(
                "Hermes delivery is temporarily unavailable"
            ) from exc
        try:
            process.communicate(message, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            self.receipts.finish(delivery_id, "reconciliation_required")
            raise HermesDeliveryBridgeError(
                "Hermes delivery outcome is unknown", retryable=False,
                code="delivery_reconciliation_required",
            ) from exc
        if process.returncode:
            self.receipts.finish(delivery_id, "reconciliation_required")
            raise HermesDeliveryBridgeError(
                "Hermes delivery command failed",
                retryable=False, code="delivery_reconciliation_required",
            )
        try:
            self.receipts.finish(delivery_id, "delivered")
        except sqlite3.Error as exc:
            raise HermesDeliveryBridgeError(
                "Hermes delivery acknowledgement could not be recorded",
                retryable=False, code="delivery_reconciliation_required",
            ) from exc
        return {"delivered": True}


class GatewaySupervisor:
    """Small external supervisor for ``hermes gateway run`` in the bridge image."""

    def __init__(self, executable: Path, *, mcp_token_file: Path | None = None) -> None:
        self.executable = Path(executable)
        self.mcp_token_file = mcp_token_file
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        environment = dict(os.environ)
        if self.mcp_token_file is not None:
            environment["JOB_SEARCH_MCP_TOKEN"] = _read_owner_only_token(
                self.mcp_token_file
            )
        self.process = subprocess.Popen(
            (
                str(self.executable),
                "gateway",
                "run",
                "--no-supervise",
            ),
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            close_fds=True,
            env=environment,
        )

    def maintain(self) -> None:
        if self.process is None:
            self.start()
            return
        status = self.process.poll()
        if status is None:
            return
        if status == 75:
            self.process = None
            self.start()
            return
        raise HermesDeliveryBridgeError("Hermes gateway exited unexpectedly")

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class _HermesHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(10)
        retryable = False
        try:
            request = _parse_frame(self.rfile.readline(MAX_FRAME_BYTES + 2))
            if set(request) != {"version", "operation", "payload"}:
                raise HermesDeliveryBridgeError(
                    "Hermes delivery request has an invalid shape", retryable=False
                )
            if request.get("version") != PROTOCOL_VERSION:
                raise HermesDeliveryBridgeError(
                    "Hermes delivery protocol is unsupported", retryable=False
                )
            operation = request.get("operation")
            payload = request.get("payload")
            if not isinstance(operation, str) or not isinstance(payload, Mapping):
                raise HermesDeliveryBridgeError(
                    "Hermes delivery request fields are invalid", retryable=False
                )
            result = self.server.dispatcher.dispatch(operation, payload)  # type: ignore[attr-defined]
            response = {
                "version": PROTOCOL_VERSION,
                "ok": True,
                "result": result,
                "error": None,
                "retryable": False,
            }
        except HermesDeliveryBridgeError as exc:
            retryable = exc.retryable
            response = {
                "version": PROTOCOL_VERSION,
                "ok": False,
                "result": None,
                "error": exc.code,
                "retryable": retryable,
            }
        except Exception:  # noqa: BLE001 - outer trust boundary
            response = {
                "version": PROTOCOL_VERSION,
                "ok": False,
                "result": None,
                "error": "request_rejected",
                "retryable": True,
            }
        try:
            self.wfile.write(_canonical(response))
        except (OSError, HermesDeliveryBridgeError):
            return


class _HermesServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, path: Path, dispatcher: HermesDispatcher) -> None:
        self.dispatcher = dispatcher
        super().__init__(str(path), _HermesHandler)


def serve(
    socket_path: Path,
    executable: Path,
    *,
    target_name: str,
    receipt_database: Path,
    mcp_token_file: Path | None = None,
    supervise_gateway: bool = False,
    gateway_service: Path | None = None,
) -> int:
    target = _safe_socket(socket_path, require_exists=False)
    if target.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(target))
        except OSError:
            target.unlink()
        else:
            raise HermesDeliveryBridgeError(
                "Hermes delivery socket is already active", retryable=False
            )
        finally:
            probe.close()
    gateway = (
        GatewaySupervisor(executable, mcp_token_file=mcp_token_file)
        if supervise_gateway
        else None
    )
    dispatcher = HermesDispatcher(
        executable,
        target=target_name,
        receipts=DeliveryReceiptStore(receipt_database),
        gateway=gateway,
        gateway_service=gateway_service,
    )
    server = _HermesServer(target, dispatcher)
    os.chmod(target, 0o600)
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous: dict[signal.Signals, Any] = {}
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    server.timeout = 0.5
    try:
        if gateway is not None:
            gateway.start()
        while not stopping:
            if gateway is not None:
                gateway.maintain()
            server.handle_request()
    finally:
        if gateway is not None:
            gateway.stop()
        server.server_close()
        try:
            if _safe_socket(target, require_exists=True) == target:
                target.unlink()
        except HermesDeliveryBridgeError:
            pass
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private cloud Hermes delivery bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("serve")
    start.add_argument("--socket", type=Path, required=True)
    start.add_argument("--hermes-executable", type=Path, required=True)
    start.add_argument("--target", required=True)
    start.add_argument("--receipt-database", type=Path, required=True)
    start.add_argument("--mcp-token-file", type=Path)
    start.add_argument("--supervise-gateway", action="store_true")
    start.add_argument("--gateway-service", type=Path)
    install = commands.add_parser("install-mcp-environment")
    install.add_argument("--token-file", type=Path, required=True)
    install.add_argument("--environment-directory", type=Path, required=True)
    install.add_argument("--owner-name", default="hermes")
    health = commands.add_parser("healthcheck")
    health.add_argument("--socket", type=Path, required=True)
    health.add_argument("--require-gateway", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install-mcp-environment":
            install_mcp_environment(
                args.token_file,
                args.environment_directory,
                owner_name=args.owner_name,
            )
            return 0
        if args.command == "healthcheck":
            report = HermesDeliveryClient(args.socket, timeout_seconds=3).ping()
            healthy = report.get("service_revision") == SERVICE_REVISION
            if args.require_gateway:
                healthy = healthy and report.get("gateway_running") is True
            return 0 if healthy else 1
        return serve(
            args.socket,
            args.hermes_executable,
            target_name=args.target,
            receipt_database=args.receipt_database,
            mcp_token_file=args.mcp_token_file,
            supervise_gateway=args.supervise_gateway,
            gateway_service=args.gateway_service,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"job-search Hermes delivery: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PROTOCOL_VERSION",
    "SERVICE_REVISION",
    "HermesDeliveryBridgeError",
    "HermesDeliveryClient",
    "HermesDispatcher",
    "install_mcp_environment",
    "serve",
]
