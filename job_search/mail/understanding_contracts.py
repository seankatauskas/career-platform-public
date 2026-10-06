"""Versioned, evidence-bound contract for shared mail understanding.

This module performs no inference and grants no mutation authority.  Validation
returns JSON data; persistence assigns finding identities and review decisions.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from job_search.contracts import ContractError, parse_utc, validate_identifier

from .context import bounded_candidates
from .identity import supported_selection
from .proposals import MAIL_EVENT_TYPES

SCHEMA_VERSION = "mail_understanding_v1"
MAX_FINDINGS = 8
MAX_EVIDENCE = 3
MAX_QUOTE_CHARS = 512
MAX_SOURCES = 12
MAX_SOURCE_CHARS = 64_000
OUTPUT_FIELDS = frozenset({"relevance", "events", "actions", "temporal_facts", "uncertainties"})
SOURCE_KINDS = frozenset({"current", "quoted", "prior_inbound", "prior_outbound", "attachment"})
ACTION_KINDS = frozenset({"reply", "send_availability", "complete_assessment", "offer_decision", "other"})
_BASE = {"application_id", "confidence", "evidence"}
_FIELDS = {
    "events": _BASE | {"event_type"},
    "actions": _BASE | {"kind", "description", "actor", "obligation", "channel", "temporal_index"},
    "temporal_facts": _BASE | {"kind", "wording", "starts_at", "ends_at", "due_at", "time_zone"},
    "uncertainties": {"reason", "description", "finding_type", "finding_index"},
}


class AnalysisValidationError(ContractError):
    """The analyzer did not satisfy the shared understanding contract."""


def _object(value: Any, fields: set[str] | frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise AnalysisValidationError(f"{label} fields do not match the schema")
    return value


def _text(value: Any, label: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AnalysisValidationError(f"{label} must be bounded nonempty text")
    return value


def _enum(value: Any, choices: Sequence[str] | set[str] | frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise AnalysisValidationError(f"{label} is invalid")
    return value


def validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact manifest whose fingerprint is claimed and persisted."""
    required = {"schema_version", "account_id", "immutable_message_id", "observation_id", "evidence_id", "received_at", "sources", "candidates", "candidate_context_complete", "coverage", "producer_version"}
    if not isinstance(request, Mapping) or not required <= set(request) or set(request) - required - {"archive_id", "replay_id"}:
        raise AnalysisValidationError("analysis request fields do not match the schema")
    if request["schema_version"] != SCHEMA_VERSION:
        raise AnalysisValidationError("unsupported analysis schema")
    for name in ("account_id", "observation_id", "evidence_id", "producer_version"):
        validate_identifier(request[name], name)
    _text(request["immutable_message_id"], "immutable_message_id", 2048)
    for name in ("archive_id", "replay_id"):
        if request.get(name) is not None:
            validate_identifier(request[name], name)
    parse_utc(request["received_at"])
    if type(request["candidate_context_complete"]) is not bool:
        raise AnalysisValidationError("candidate_context_complete must be boolean")
    if not isinstance(request["candidates"], (list, tuple)):
        raise AnalysisValidationError("candidates must be an array")
    bounded_candidates(request["candidates"])
    sources = request["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise AnalysisValidationError("sources must be a bounded nonempty array")
    ids: set[str] = set()
    for source in sources:
        required_source = {"source_id", "kind", "text", "source_at", "sha256", "truncated"}
        if not isinstance(source, Mapping) or not required_source <= set(source) or set(source) - required_source - {"archive_id", "attachment_record_id"}:
            raise AnalysisValidationError("source fields do not match the schema")
        validate_identifier(source["source_id"], "source_id")
        if source["source_id"] in ids:
            raise AnalysisValidationError("source IDs must be unique")
        ids.add(source["source_id"])
        _enum(source["kind"], SOURCE_KINDS, "source kind")
        _text(source["text"], "source text", MAX_SOURCE_CHARS)
        parse_utc(source["source_at"])
        if type(source["truncated"]) is not bool:
            raise AnalysisValidationError("source truncated must be boolean")
        if source["sha256"] != hashlib.sha256(source["text"].encode("utf-8")).hexdigest():
            raise AnalysisValidationError("source digest does not match supplied text")
        for name in ("archive_id", "attachment_record_id"):
            if source.get(name) is not None:
                validate_identifier(source[name], name)
    if sum(source["kind"] == "current" for source in sources) != 1:
        raise AnalysisValidationError("exactly one current source is required")
    if sum(len(source["text"]) for source in sources) > MAX_SOURCE_CHARS:
        raise AnalysisValidationError("total source text exceeds its bound")
    coverage = request["coverage"]
    if not isinstance(coverage, list) or len(coverage) > 128:
        raise AnalysisValidationError("coverage must be a bounded array")
    for item in coverage:
        _object(item, {"source_id", "reason"}, "coverage")
        validate_identifier(item["source_id"], "coverage source_id")
        _text(item["reason"], "coverage reason", 128)
    return json.loads(json.dumps(request, allow_nan=False))


def validate_analysis(raw: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    request = validate_request(request)
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise AnalysisValidationError("analysis output exceeds its byte bound")
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise AnalysisValidationError("analysis output is not JSON") from exc
    _object(raw, OUTPUT_FIELDS, "analysis")
    try:
        if len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 64 * 1024:
            raise AnalysisValidationError("analysis output exceeds its byte bound")
    except (ValueError, TypeError) as exc:
        raise AnalysisValidationError("analysis output must contain finite JSON values") from exc
    result = {"relevance": _enum(raw["relevance"], {"career", "not_career", "uncertain"}, "relevance")}
    for key, fields in _FIELDS.items():
        values = raw[key]
        if not isinstance(values, list) or len(values) > MAX_FINDINGS:
            raise AnalysisValidationError(f"{key} must be a bounded array")
        result[key] = [dict(_object(value, fields, key)) for value in values]
    sources = {item["source_id"]: item for item in request["sources"]}
    candidates = bounded_candidates(request["candidates"])
    candidate_ids = {item.application_id for item in candidates}
    current = next(item["text"] for item in request["sources"] if item["kind"] == "current")
    identity_held = False
    historical_actions = False
    for key in ("events", "actions", "temporal_facts"):
        for finding in result[key]:
            application_id = finding["application_id"]
            if application_id is not None:
                validate_identifier(application_id, "application_id")
                if application_id not in candidate_ids:
                    raise AnalysisValidationError("application_id is outside candidate context")
            confidence = finding["confidence"]
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise AnalysisValidationError("finding confidence must be a finite number from zero through one")
            finding["confidence"] = float(confidence)
            evidence = finding["evidence"]
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE:
                raise AnalysisValidationError("finding evidence must be a bounded nonempty array")
            checked = []
            for citation in evidence:
                _object(citation, {"source_id", "quote", "start", "end"}, "evidence")
                source_id = citation["source_id"]
                if not isinstance(source_id, str) or source_id not in sources:
                    raise AnalysisValidationError("evidence source is outside supplied context")
                quote = _text(citation["quote"], "evidence quote", MAX_QUOTE_CHARS)
                start, end = citation["start"], citation["end"]
                if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(sources[source_id]["text"]) or sources[source_id]["text"][start:end] != quote:
                    raise AnalysisValidationError("evidence quote does not match supplied source span")
                checked.append(dict(citation))
            finding["evidence"] = checked
            if application_id is not None and (not request["candidate_context_complete"] or not supported_selection(candidates, application_id, "", current)):
                finding["application_id"] = None
                identity_held = True
            if key == "events":
                _enum(finding["event_type"], {item.value for item in MAIL_EVENT_TYPES}, "event_type")
                if not any(sources[item["source_id"]]["kind"] in {"current", "attachment"} for item in checked):
                    finding["application_id"] = None
                    historical_actions = True
            elif key == "actions":
                _enum(finding["kind"], ACTION_KINDS, "action kind")
                _text(finding["description"], "action description")
                _enum(finding["actor"], {"applicant", "employer", "unknown"}, "action actor")
                _enum(finding["obligation"], {"required", "optional", "unclear"}, "action obligation")
                _enum(finding["channel"], {"email", "portal", "other", "unknown"}, "action channel")
                index = finding["temporal_index"]
                if index is not None and (type(index) is not int or not 0 <= index < len(result["temporal_facts"])):
                    raise AnalysisValidationError("action temporal_index is outside this analysis")
                if not any(sources[item["source_id"]]["kind"] in {"current", "attachment"} for item in checked):
                    finding["obligation"] = "unclear"
                    historical_actions = True
            else:
                _validate_temporal(finding, request["received_at"])
                if not any(sources[item["source_id"]]["kind"] in {"current", "attachment"} for item in checked):
                    finding["application_id"] = None
                    historical_actions = True
    for action in result["actions"]:
        index = action["temporal_index"]
        if index is not None:
            temporal_application = result["temporal_facts"][index]["application_id"]
            if action["application_id"] is not None and temporal_application is not None and action["application_id"] != temporal_application:
                raise AnalysisValidationError("action temporal reference belongs to another application")
    for uncertainty in result["uncertainties"]:
        _text(uncertainty["reason"], "uncertainty reason", 128)
        _text(uncertainty["description"], "uncertainty description", 256)
        kind = _enum(uncertainty["finding_type"], {"event", "action", "temporal", "message"}, "uncertainty finding_type")
        index = uncertainty["finding_index"]
        arrays = {"event": "events", "action": "actions", "temporal": "temporal_facts"}
        if (kind == "message" and index is not None) or (kind != "message" and (type(index) is not int or not 0 <= index < len(result[arrays[kind]]))):
            raise AnalysisValidationError("uncertainty finding_index is invalid")
    for needed, reason, description in (
        (result['relevance'] == 'uncertain', 'relevance_requires_review', 'The message could not be confidently identified as career correspondence.'),
        (identity_held, "identity_requires_review", "Application assignment is unsupported or candidate context is incomplete."),
        (historical_actions, "historical_request_requires_review", "Findings supported only by prior or quoted messages cannot establish a new development."),
    ):
        if needed and len(result["uncertainties"]) < MAX_FINDINGS and not any(item["reason"] == reason for item in result["uncertainties"]):
            result["uncertainties"].append({"reason": reason, "description": description, "finding_type": "message", "finding_index": None})
    if result["relevance"] == "not_career" and any(result[key] for key in ("events", "actions", "temporal_facts")):
        raise AnalysisValidationError("not_career analysis cannot contain career findings")
    return result


def _validate_temporal(finding: Mapping[str, Any], received_at: str) -> None:
    kind = _enum(finding["kind"], {"interview", "deadline"}, "temporal kind")
    _text(finding["wording"], "temporal wording")
    zone = finding["time_zone"]
    if zone is not None:
        _text(zone, "temporal time_zone", 128)
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AnalysisValidationError("temporal time_zone is unknown") from exc
    received = parse_utc(received_at)
    parsed = {}
    for field in ("starts_at", "ends_at", "due_at"):
        value = finding[field]
        if value is not None:
            if not isinstance(value, str):
                raise AnalysisValidationError("temporal timestamps must be strings or null")
            stamp = parse_utc(value)
            if value != stamp.isoformat(timespec="seconds").replace("+00:00", "Z"):
                raise AnalysisValidationError("temporal timestamps must be second-precision UTC")
            if stamp < received - timedelta(days=1) or stamp > received + timedelta(days=730):
                raise AnalysisValidationError("temporal timestamp is outside the supported horizon")
            parsed[field] = stamp
    if kind == "interview" and finding["due_at"] is not None:
        raise AnalysisValidationError("interview facts cannot have due_at")
    if kind == "deadline" and (finding["starts_at"] is not None or finding["ends_at"] is not None):
        raise AnalysisValidationError("deadline facts cannot have interview intervals")
    if "starts_at" in parsed and "ends_at" in parsed and not timedelta(0) < parsed["ends_at"] - parsed["starts_at"] <= timedelta(hours=12):
        raise AnalysisValidationError("temporal interview interval is invalid")


def analysis_schema(candidate_ids: Sequence[str]) -> dict[str, Any]:
    """JSON Schema shared by local and remote adapters and strict validation."""
    ids = list(candidate_ids)
    if len(ids) > 20 or len(ids) != len(set(ids)):
        raise AnalysisValidationError("candidate IDs must be unique and bounded")
    for identity in ids:
        validate_identifier(identity, "application_id")
    def obj(properties):
        return {"type": "object", "properties": properties, "required": sorted(properties), "additionalProperties": False}
    def text(limit=512):
        return {"type": "string", "minLength": 1, "maxLength": limit}
    def enum(values):
        return {"type": "string", "enum": sorted(values)}
    nullable_text = {"type": ["string", "null"], "maxLength": 128}
    evidence = {"type": "array", "minItems": 1, "maxItems": MAX_EVIDENCE, "items": obj({"source_id": text(256), "quote": text(), "start": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 1}})}
    base = {"application_id": {"enum": [*ids, None]}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "evidence": evidence}
    event = obj({**base, "event_type": enum(item.value for item in MAIL_EVENT_TYPES)})
    action = obj({**base, "kind": enum(ACTION_KINDS), "description": text(), "actor": enum({"applicant", "employer", "unknown"}), "obligation": enum({"required", "optional", "unclear"}), "channel": enum({"email", "portal", "other", "unknown"}), "temporal_index": {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_FINDINGS - 1}})
    temporal = obj({**base, "kind": enum({"interview", "deadline"}), "wording": text(), **{field: dict(nullable_text) for field in ("starts_at", "ends_at", "due_at", "time_zone")}})
    uncertainty = obj({"reason": text(128), "description": text(256), "finding_type": enum({"event", "action", "temporal", "message"}), "finding_index": {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_FINDINGS - 1}})
    return obj({"relevance": enum({"career", "not_career", "uncertain"}), **{key: {"type": "array", "maxItems": MAX_FINDINGS, "items": schema} for key, schema in (("events", event), ("actions", action), ("temporal_facts", temporal), ("uncertainties", uncertainty))}})
