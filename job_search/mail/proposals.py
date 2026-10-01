"""Strict construction and validation of mail-derived event proposals."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

from job_search.contracts import (
    ApplicationEventType,
    ContractError,
    EventProposalInput,
    ProducerKind,
    canonical_json,
    payload_sha256,
    validate_identifier,
)

from .context import CandidateApplication, bounded_candidates
from .sanitizer import SanitizedMail


MAX_EVIDENCE_QUOTE_CHARS = 512
MODEL_OUTPUT_FIELDS = frozenset(
    {
        "event_type", "application_id", "confidence", "evidence_quote",
        "span_start", "span_end", "payload",
    }
)
MAIL_EVENT_TYPES = frozenset(
    {
        ApplicationEventType.SUBMISSION_CONFIRMED,
        ApplicationEventType.RECRUITER_CONTACT,
        ApplicationEventType.ASSESSMENT_REQUESTED,
        ApplicationEventType.ASSESSMENT_COMPLETED,
        ApplicationEventType.INTERVIEW_REQUESTED,
        ApplicationEventType.INTERVIEW_SCHEDULED,
        ApplicationEventType.INTERVIEW_COMPLETED,
        ApplicationEventType.OFFER_RECEIVED,
        ApplicationEventType.OFFER_ACCEPTED,
        ApplicationEventType.REJECTION_RECEIVED,
        ApplicationEventType.WITHDRAWN,
    }
)


class ProposalValidationError(ContractError):
    pass


def _event_type(value: Any) -> ApplicationEventType:
    if not isinstance(value, str):
        raise ProposalValidationError("event_type must be a string")
    try:
        result = ApplicationEventType(value)
    except ValueError as exc:
        raise ProposalValidationError("unknown event_type") from exc
    if result not in MAIL_EVENT_TYPES:
        raise ProposalValidationError("event_type cannot be inferred from mail")
    return result


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProposalValidationError("confidence must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ProposalValidationError("confidence must be between 0 and 1")
    return result


def _span(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProposalValidationError(f"{field} must be an integer")
    return value


def build_proposal(
    *,
    evidence_id: str,
    mail: SanitizedMail,
    candidates: Sequence[CandidateApplication | Mapping[str, Any]],
    event_type: ApplicationEventType,
    application_id: str | None,
    producer_kind: ProducerKind,
    producer_version: str,
    confidence: float,
    evidence_quote: str,
    span_start: int,
    span_end: int,
    payload: Mapping[str, Any] | None = None,
) -> EventProposalInput:
    validate_identifier(evidence_id, "evidence_id")
    validate_identifier(producer_version, "producer_version")
    event_type = _event_type(event_type.value if isinstance(event_type, ApplicationEventType) else event_type)
    confidence = _confidence(confidence)
    bounded = bounded_candidates(candidates)
    candidate_ids = tuple(item.application_id for item in bounded)
    if application_id is not None:
        validate_identifier(application_id, "application_id")
        if application_id not in candidate_ids:
            raise ProposalValidationError("application_id is outside candidate context")
    if not isinstance(evidence_quote, str) or not evidence_quote.strip():
        raise ProposalValidationError("evidence_quote must not be empty")
    if len(evidence_quote) > MAX_EVIDENCE_QUOTE_CHARS:
        raise ProposalValidationError("evidence_quote is too long")
    span_start = _span(span_start, "span_start")
    span_end = _span(span_end, "span_end")
    if not mail.verifies_evidence(evidence_quote, span_start, span_end):
        raise ProposalValidationError("evidence quote and span do not match sanitized mail")
    safe_payload = {} if payload is None else payload
    if not isinstance(safe_payload, Mapping) or safe_payload:
        raise ProposalValidationError("mail proposals require an empty payload")
    canonical_json(safe_payload)
    dedupe_key = "mail:" + payload_sha256(
        {
            "evidence_id": evidence_id,
            "event_type": event_type.value,
            "application_id": application_id,
            "producer_kind": producer_kind.value,
            "producer_version": producer_version,
            "span_start": span_start,
            "span_end": span_end,
        }
    )
    return EventProposalInput(
        evidence_id=evidence_id,
        proposed_application_id=application_id,
        event_type=event_type,
        producer_kind=producer_kind,
        producer_version=producer_version,
        confidence=confidence,
        candidate_application_ids=candidate_ids,
        evidence_quote=evidence_quote,
        span_start=span_start,
        span_end=span_end,
        payload={},
        dedupe_key=dedupe_key,
    )


def validate_model_output(
    raw: str | Mapping[str, Any],
    *,
    evidence_id: str,
    mail: SanitizedMail,
    candidates: Sequence[CandidateApplication | Mapping[str, Any]],
    producer_version: str,
) -> EventProposalInput:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProposalValidationError("model output is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise ProposalValidationError("model output must be an object")
    keys = frozenset(raw)
    if keys != MODEL_OUTPUT_FIELDS:
        missing = sorted(MODEL_OUTPUT_FIELDS - keys)
        extra = sorted(keys - MODEL_OUTPUT_FIELDS)
        raise ProposalValidationError(f"model output fields mismatch: missing={missing}, extra={extra}")
    application_id = raw["application_id"]
    if application_id is not None and not isinstance(application_id, str):
        raise ProposalValidationError("application_id must be a string or null")
    return build_proposal(
        evidence_id=evidence_id,
        mail=mail,
        candidates=candidates,
        event_type=_event_type(raw["event_type"]),
        application_id=application_id,
        producer_kind=ProducerKind.MODEL,
        producer_version=producer_version,
        confidence=_confidence(raw["confidence"]),
        evidence_quote=raw["evidence_quote"],
        span_start=_span(raw["span_start"], "span_start"),
        span_end=_span(raw["span_end"], "span_end"),
        payload=raw["payload"],
    )
