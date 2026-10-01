"""Sanitized enrollment receipts; reads never probe a provider or refresh OAuth."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CAPABILITIES = {
    "outlook_read": ("outlook_client_id", "outlook_account_id"),
    "outlook_drafts": ("outlook_client_id", "outlook_account_id"),
    "outlook_holds": ("outlook_client_id", "outlook_account_id"),
    "mail_inference": ("inference_config", "remote_mail_inference_enabled"),
    "hermes_tools": ("hermes_executable", "mcp_port"),
    "notification_delivery": ("hermes_executable", "hermes_telegram_target"),
}


def fingerprint(config, capability: str) -> str:
    values = {key: str(getattr(config, key)) for key in CAPABILITIES[capability]}
    if capability == "mail_inference" and config.inference_config:
        values["profile_digest"] = hashlib.sha256(config.inference_config.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _read(config) -> dict:
    path = config.log_dir / "connection-verifications.json"
    try:
        if path.stat().st_mode & 0o077:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record_verification(config, capability: str, *, succeeded: bool) -> dict:
    if capability not in CAPABILITIES or not isinstance(succeeded, bool):
        raise ValueError("unknown verification capability")
    values = _read(config)
    receipt = {"fingerprint": fingerprint(config, capability), "succeeded": succeeded,
               "checked_at": datetime.now(timezone.utc).isoformat()}
    values[capability] = receipt
    config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=config.log_dir, prefix=".verification-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(values, stream, sort_keys=True)
        os.replace(name, config.log_dir / "connection-verifications.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return receipt


def verification_capabilities(config) -> list[dict]:
    from .readiness import _capability
    rows = []
    for name, receipt in _read(config).items():
        if name not in CAPABILITIES or not isinstance(receipt, dict):
            continue
        try:
            matches = receipt.get("fingerprint") == fingerprint(config, name)
            stamp = datetime.fromisoformat(receipt["checked_at"])
            if stamp.tzinfo is None:
                continue
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        success = receipt.get("succeeded") is True
        current = matches and 0 <= age <= 86400
        rows.append(_capability(
            name + "_connection", "ready" if current and success else "blocked" if current else "configured_unverified",
            attempt=receipt["checked_at"], success=receipt["checked_at"] if success else None,
            reason="verified_connection" if current and success else "connection_check_failed" if current else "verification_expired_or_configuration_changed",
            action="none" if current and success else "verify_connection",
        ))
    return rows
