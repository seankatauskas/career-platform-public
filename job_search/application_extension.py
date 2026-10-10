"""Paired extension ingress for the isolated candidate.

The HTTP host supplies validated Host, JSON size limits and CORS handling. Mount
POST ``/api/v1/extension/observations`` and ``/api/v1/extension/answers`` only.
Both use the existing extension's Origin header and body.device_token. Inject
``BrowserTracking.authenticate`` backed by a separate pairing database; never
point its legacy ledger at the candidate. Enrollment remains in that auth host.

This adapter translates the existing durable queues without calling the legacy
observation or answer writers. ``/capture`` is the separate approved-profile fact
flow and is deliberately outside these evidence routes. Provider execution and
human review are never authorized by a device credential.
"""
from __future__ import annotations

import re
import time

from .application_answers import validate_snapshot
from .application_transport import BrowserObservationAdapter
from .browser_tracking import KINDS, identify_job
from .commands import DomainError, digest
from .contracts import ContractError, parse_utc


EXTENSION_PATHS = frozenset({
    "/api/v1/extension/observations", "/api/v1/extension/answers",
})


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", value):
        raise DomainError("invalid_input", "Invalid browser evidence identity")
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise DomainError("invalid_input", "Browser evidence requires an occurrence time")
    try:
        stamp = parse_utc(value)
        if stamp.timestamp() > time.time() + 300:
            raise ContractError("Browser evidence is in the future")
    except (ContractError, ValueError, OverflowError) as exc:
        raise DomainError("invalid_input", "Invalid browser evidence time") from exc
    return value


def _text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise DomainError("invalid_input", "Invalid browser metadata")
    return value


class PairedExtensionAdapter:
    """Only the injected authenticator can establish the browser principal."""

    paths = EXTENSION_PATHS

    def __init__(self, runtime, authenticator, audience):
        if not callable(authenticator) or not isinstance(audience, str) or not audience:
            raise ValueError("A paired-device authenticator and audience are required")
        self._observations = BrowserObservationAdapter(runtime)
        self._authenticate = authenticator
        self._audience = audience

    def handle(self, path, headers, body):
        """Handle an already parsed POST; return the existing queue's receipt shape."""
        if path not in self.paths:
            raise DomainError("invalid_input", "Unsupported extension evidence route")
        if not isinstance(body, dict):
            raise DomainError("invalid_input", "Extension input must be an object")
        token = body.get("device_token")
        origin = next((v for k, v in headers.items() if k.lower() == "origin"), "")
        if not isinstance(token, str) or not 1 <= len(token) <= 1024 or not isinstance(origin, str):
            raise DomainError("not_authorized", "A paired browser connection is required")
        try:
            device = self._authenticate(token, origin, self._audience)
        except ContractError as exc:
            raise DomainError("not_authorized", "Browser connection is not authorized for this origin and audience") from exc
        if not isinstance(device, str) or not device:
            raise DomainError("not_authorized", "Browser connection did not establish a device identity")
        # Credentials and caller authority are never retained in source evidence.
        source = {k: v for k, v in body.items() if k != "device_token"}
        is_answers = path.endswith("/answers")
        allowed = ({"capture_id", "attempt_id", "page_url", "captured_at", "snapshot"}
                   if is_answers else {"observation_id", "attempt_id", "page_url", "kind",
                       "occurred_at", "title", "employer", "metadata", "resume_sha256"})
        if set(source) - allowed:
            raise DomainError("invalid_input", "Unsupported extension evidence fields")
        attempt = _identifier(source.get("attempt_id"))
        external_id = _identifier(source.get("capture_id" if is_answers else "observation_id"))
        try:
            identity = identify_job(source.get("page_url"))
        except (ContractError, ValueError) as exc:
            raise DomainError("invalid_input", "Invalid application page identity") from exc
        source = {**source, "adapter": "paired_extension_v1", "identity": identity}
        job_source = {"source": identity["ats"], "source_id": identity["job_id"]}
        answers, documents = {}, []
        if is_answers:
            try:
                snapshot = validate_snapshot(source["snapshot"])
            except (KeyError, ContractError) as exc:
                raise DomainError("invalid_input", "Invalid exact answer snapshot") from exc
            source["answer_snapshot"] = source.pop("snapshot")
            # Typed fields and labels remain exact in answer_snapshot. The string
            # projection is only a convenience for existing submission text views.
            answers = {f["field_key"]: f["value"] for f in snapshot["fields"] if isinstance(f["value"], str)}
            occurred_at = _timestamp(source.get("captured_at"))
            activity = "answer_capture"
        else:
            kind = source.get("kind")
            if not isinstance(kind, str) or kind not in KINDS:
                raise DomainError("invalid_input", "Unsupported browser observation kind")
            metadata = source.get("metadata", {})
            if not isinstance(metadata, dict) or set(metadata) - {"signal", "request_status", "adapter_version"}:
                raise DomainError("invalid_input", "Invalid browser observation metadata")
            for value in metadata.values():
                _text(value, 120)
            if kind == "site_acknowledged" and metadata.get("signal") not in {"success_dom", "success_route"}:
                raise DomainError("invalid_input", "Acknowledgment requires a supported success signal")
            for name in ("title", "employer"):
                if name in source:
                    label = _text(source[name], 500)
                    if label.strip():
                        job_source[name] = label
            fingerprint = source.get("resume_sha256", "")
            if not isinstance(fingerprint, str) or (fingerprint and not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
                raise DomainError("invalid_input", "Invalid resume fingerprint")
            if fingerprint:
                documents = [{"kind": "resume", "sha256": fingerprint, "provenance": "browser_observed"}]
            occurred_at = _timestamp(source.get("occurred_at"))
            activity = {"attempted": "submission_attempt", "site_acknowledged": "website_acknowledgment"}.get(kind, "submission_signal")
        key = digest({"device": device, "path": path, "source_id": external_id})
        result = self._observations.observe(device, {
            "job_source": job_source, "observation_id": ("answers:" if is_answers else "observation:") + external_id,
            "attempt_ref": attempt, "occurred_at": occurred_at, "activity": activity,
            "answers": answers, "documents": documents, "source": source,
        }, idempotency_key="extension:" + key)
        observation = result["observation"]
        receipt = {"application_id": observation["application_id"], "attempt_id": attempt,
                   "status": "pending_review", "observation_id": observation["id"]}
        if is_answers:
            receipt.update(saved=True, capture_id=external_id, field_count=len(snapshot["fields"]))
        return receipt
