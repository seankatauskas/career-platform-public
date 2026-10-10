"""Strict, bounded multi-finding analysis with source-verified attribution."""
from dataclasses import dataclass, field
import hashlib
import math
from typing import Mapping, Tuple

from job_search.commands import DomainError, digest, encode


@dataclass(frozen=True)
class SourceText:
    source_id: str
    revision: str
    sha256: str
    text: str
    direction: str = "incoming"
    role: str = "authored"


@dataclass(frozen=True)
class AnalysisInput:
    sources: Tuple[SourceText, ...]
    candidates: tuple = ()
    versions: Mapping = field(default_factory=dict)
    coverage: Mapping = field(default_factory=lambda: {"complete": True})
    context: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if not 1 <= len(self.sources) <= 50 or len(self.candidates) > 20:
            raise DomainError("invalid_input", "Analysis context exceeds bounded limits")
        if any(not isinstance(s, SourceText) or not isinstance(s.source_id, str) or not s.source_id or
               not isinstance(s.revision, str) or not s.revision for s in self.sources):
            raise DomainError("invalid_input", "Invalid analysis source identities")
        if any(not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"] for c in self.candidates):
            raise DomainError("invalid_input", "Invalid candidate identity")
        if len({(s.source_id, s.revision) for s in self.sources}) != len(self.sources):
            raise DomainError("invalid_input", "Duplicate source references")
        for source in self.sources:
            if (not isinstance(source.text, str) or len(source.text) > 250000 or
                    source.direction not in {"incoming", "outgoing", "draft", "browser"} or
                    source.role not in {"authored", "quoted", "attachment", "thread"} or
                    hashlib.sha256(source.text.encode("utf-8")).hexdigest() != source.sha256):
                raise DomainError("invalid_input", "Invalid source evidence")
        if not isinstance(self.coverage.get("complete"), bool):
            raise DomainError("invalid_input", "Coverage must state completeness")

    def descriptor(self):
        """Private source text never enters persistence or command receipts."""
        return {"sources": [{"source_id": s.source_id, "revision": s.revision,
                             "sha256": s.sha256, "direction": s.direction, "role": s.role}
                            for s in self.sources], "candidates": self.candidates,
                "versions": dict(self.versions), "coverage": dict(self.coverage), "context": dict(self.context)}

    def fingerprint(self):
        return digest(self.descriptor())


FIELDS = {"relevance", "associations", "facts", "requests", "temporal_facts", "uncertainties"}
KINDS = {
    "associations": {"application", "job"},
    "facts": {"submission", "contact", "assessment", "interview", "offer", "outcome"},
    "requests": {"reply", "book_interview", "send_availability", "assessment", "documents", "follow_up", "other"},
    "temporal_facts": {"deadline", "interval", "date"},
}
ALLOWED = {
    "associations": {"kind", "target_id", "evidence", "confidence"},
    "facts": {"kind", "value", "target_id", "evidence", "confidence"},
    "requests": {"kind", "outcome", "responsible_party", "requirement", "channel", "target_id", "evidence", "confidence"},
    "temporal_facts": {"kind", "wording", "normalized", "timezone", "missing", "target_id", "evidence", "confidence"},
}


def validate_analysis(value, context):
    try:
        bounded = len(encode(value).encode("utf-8")) <= 65536
    except (TypeError, ValueError):
        bounded = False
    if not bounded:
        raise DomainError("invalid_input", "Analysis must be bounded finite JSON")
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise DomainError("invalid_input", "Analysis fields must match schema version 1")
    if not isinstance(value["relevance"], str) or value["relevance"] not in {"career_related", "unrelated", "uncertain"}:
        raise DomainError("invalid_input", "Invalid relevance")
    sources = {(s.source_id, s.revision): s for s in context.sources}
    targets = {candidate["id"] for candidate in context.candidates}
    output = {"relevance": value["relevance"]}
    for group, kinds in KINDS.items():
        findings = value[group]
        if not isinstance(findings, list) or len(findings) > 20:
            raise DomainError("invalid_input", "Finding array exceeds limit")
        output[group] = []
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) - ALLOWED[group] or not isinstance(finding.get("kind"), str) or finding["kind"] not in kinds:
                raise DomainError("invalid_input", "Invalid finding fields or kind")
            if finding.get("target_id") is not None and (not isinstance(finding["target_id"], str) or finding["target_id"] not in targets):
                raise DomainError("invalid_input", "Finding target is outside supplied candidates")
            confidence = finding.get("confidence")
            if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
                                            not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise DomainError("invalid_input", "Invalid confidence")
            evidence = finding.get("evidence")
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
                raise DomainError("invalid_input", "Each finding needs bounded source evidence")
            authored = False
            for span in evidence:
                if not isinstance(span, dict) or set(span) != {"source_id", "revision", "start", "end", "quote"}:
                    raise DomainError("invalid_input", "Invalid evidence fields")
                if not isinstance(span["source_id"], str) or not isinstance(span["revision"], str):
                    raise DomainError("invalid_input", "Invalid evidence identity")
                source = sources.get((span["source_id"], span["revision"]))
                start, end, quote = span["start"], span["end"], span["quote"]
                if (source is None or not isinstance(start, int) or isinstance(start, bool) or
                        not isinstance(end, int) or isinstance(end, bool) or
                        not isinstance(quote, str) or not 1 <= len(quote) <= 512 or
                        not 0 <= start < end <= len(source.text) or source.text[start:end] != quote):
                    raise DomainError("invalid_input", "Evidence span does not match source")
                authored |= source.role == "authored" and source.direction in {"incoming", "browser"}
            if group == "requests":
                if (not isinstance(finding.get("requirement"), str) or finding["requirement"] not in {"required", "optional", "unclear"} or
                        not isinstance(finding.get("responsible_party"), str) or finding["responsible_party"] not in {"applicant", "employer", "other", "unclear"} or
                        not isinstance(finding.get("channel"), str) or finding["channel"] not in {"email", "booking_link", "portal", "phone", "other", "unclear"} or
                        not isinstance(finding.get("outcome"), str) or not 1 <= len(finding["outcome"]) <= 2000):
                    raise DomainError("invalid_input", "Invalid requested outcome")
            if group == "facts" and ("value" not in finding or not isinstance(finding["value"], (str, dict))):
                raise DomainError("invalid_input", "Facts require a structured or text value")
            if group == "temporal_facts":
                if not isinstance(finding.get("wording"), str) or not isinstance(finding.get("missing", []), list):
                    raise DomainError("invalid_input", "Invalid temporal evidence")
                if finding.get("normalized") and (not finding.get("timezone") or finding.get("missing")):
                    raise DomainError("invalid_input", "Incomplete temporal evidence cannot supply a normalized instant")
            output[group].append({**finding, "actionable_source": authored})
    if value["relevance"] == "unrelated" and any(value[group] for group in KINDS):
        raise DomainError("invalid_input", "Unrelated analysis cannot contain career findings")
    uncertainty = value["uncertainties"]
    if not isinstance(uncertainty, list) or len(uncertainty) > 10 or any(
            not isinstance(item, str) or not 1 <= len(item) <= 2000 for item in uncertainty):
        raise DomainError("invalid_input", "Invalid uncertainty list")
    output["uncertainties"] = uncertainty
    return output
