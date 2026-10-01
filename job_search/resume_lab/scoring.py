"""Deterministic, explainable ATS proxy scoring.

This score estimates visible requirement evidence and literal recruiter-search
visibility.  It is not a probability and never learns from application outcomes.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    AtsProxyEvaluation,
    CriterionResult,
    EligibilityStatus,
    EvidenceSpan,
    EvidenceStatus,
    Requirement,
    RequirementGraph,
    RequirementKind,
    RequirementPriority,
    ResumeLabError,
    SCORER_REVISION,
    canonical_json,
    content_sha256,
    sha256_text,
)
from .requirements import (
    contains_protected_attribute_label,
    is_non_fit_clause,
    normalize_text,
)


_YEARS = re.compile(r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", re.I)
_NEGATIVE_AUTHORIZATION = re.compile(
    r"\b(?:not authori[sz]ed to work|requires? sponsorship|need sponsorship|"
    r"will require sponsorship)\b",
    re.I,
)
_POSITIVE_AUTHORIZATION = re.compile(
    r"\b(?:authori[sz]ed to work|eligible to work|right to work|"
    r"does not require sponsorship|no sponsorship required)\b",
    re.I,
)
_CLEARANCE_REQUIREMENT = re.compile(r"\bclearance\b", re.I)
_NEGATIVE_CLEARANCE = re.compile(
    r"\b(?:no|without|lack(?:s|ing)?|do(?:es)? not hold)\s+(?:an?\s+)?"
    r"(?:active\s+)?(?:security\s+)?clearance\b|"
    r"\bclearance\s+(?:is\s+)?(?:inactive|expired|revoked)\b",
    re.I,
)
_POSITIVE_CLEARANCE = re.compile(
    r"\b(?:(?:hold(?:s|ing)?|maintain(?:s|ing)?)\s+(?:an?\s+)?"
    r"(?:active\s+)?(?:security\s+)?clearance|"
    r"(?:active|current|valid)\s+(?:security\s+)?clearance|"
    r"(?:security\s+)?clearance\s*[:=-]\s*(?:active|current|valid))\b",
    re.I,
)
_TRAVEL_REQUIREMENT = re.compile(r"\btravel\b", re.I)
_NEGATIVE_TRAVEL = re.compile(
    r"\b(?:cannot|can't|unable to|not (?:able|willing) to|no)\s+travel\b",
    re.I,
)
_POSITIVE_TRAVEL = re.compile(
    r"\b(?:(?:able|available|willing) to travel\b|"
    r"travel\s*[:=-]\s*\d{1,3}\s*%)",
    re.I,
)
_PERCENT = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*%")
_LOCATION_REQUIREMENT = re.compile(
    r"\b(?:located|based|resid(?:e|ing)|liv(?:e|ing))\s+"
    r"(?:(?:in|near)\s+|within\s+(?:\d+(?:\.\d+)?\s+miles?\s+of\s+)?)"
    r"([A-Za-z0-9][A-Za-z0-9 ,.'’/-]{0,80}?)"
    r"(?=(?:[.;]|\bor\b|\band\b|$))",
    re.I,
)
_LICENSE_REQUIREMENT = re.compile(r"\blicen[cs](?:e|ed|ure)\b", re.I)
_NEGATIVE_LICENSE = re.compile(
    r"\b(?:not licensed|no (?:valid )?license|license (?:is )?(?:expired|invalid))\b",
    re.I,
)
_POSITIVE_LICENSE = re.compile(
    r"\b(?:(?:currently|duly) licensed|(?:active|current|valid)\s+license|"
    r"license\s*[:=-]\s*(?:active|current|valid))\b",
    re.I,
)
_AUTHORIZATION_REQUIREMENT = re.compile(
    r"\b(?:work authori[sz]ation|authori[sz]ed to work|visa sponsorship|"
    r"sponsorship|citizen(?:ship)?|eligible to work|right to work)\b",
    re.I,
)


def _qualification_text(value: str) -> str:
    # Remove the entire labelled field, not only the category word. Otherwise a value
    # such as ``Race: X`` can remain behind and accidentally match a malicious JD.
    return "".join(
        (" " * len(line)) if contains_protected_attribute_label(line) else line
        for line in value.splitlines(keepends=True)
    )


def _literal_present(normalized_haystack: str, value: str) -> bool:
    needle = normalize_text(value)
    if not needle:
        return False
    return bool(
        re.search(
            r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
            normalized_haystack,
        )
    )


def _matched_alias(normalized_text: str, alternatives: Sequence[str]) -> Optional[str]:
    for value in alternatives:
        if _literal_present(normalized_text, value):
            return value
    return None


def _evidence_spans(
    original: str, matched: Iterable[str], *, limit: int = 3
) -> Tuple[EvidenceSpan, ...]:
    wanted = tuple(dict.fromkeys(normalize_text(value) for value in matched if value))
    if not wanted:
        return ()
    result: List[EvidenceSpan] = []
    cursor = 0
    for raw in original.splitlines(keepends=True):
        text = raw.rstrip("\r\n")
        normalized = normalize_text(text)
        if normalized and any(_literal_present(normalized, value) for value in wanted):
            start = cursor
            end = start + len(text)
            result.append(EvidenceSpan(text=text, start=start, end=end))
            if len(result) == limit:
                break
        cursor += len(raw)
    return tuple(result)


def _years_near_evidence(
    evidence: Sequence[EvidenceSpan], matched_terms: Sequence[str]
) -> Optional[float]:
    """Associate duration with each matched skill's nearest local clause."""

    if not matched_terms:
        return None
    values: list[float] = []
    for term in matched_terms:
        needle = normalize_text(term)
        candidates: list[tuple[int, float]] = []
        for span in evidence:
            for clause in re.split(r"[,;]|\.(?:\s+|$)", span.text):
                normalized_clause = normalize_text(clause)
                if not _literal_present(normalized_clause, needle):
                    continue
                term_match = re.search(
                    r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                    normalized_clause,
                )
                if term_match is None:
                    continue
                for year_match in _YEARS.finditer(normalized_clause):
                    distance = max(
                        0,
                        term_match.start() - year_match.end(),
                        year_match.start() - term_match.end(),
                    )
                    candidates.append((distance, float(year_match.group(1))))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        values.append(candidates[0][1])
    return min(values) if values else None


def _match_evidence(
    resume_text: str, match: Optional[re.Match[str]]
) -> Tuple[EvidenceSpan, ...]:
    if match is None:
        return ()
    start = resume_text.rfind("\n", 0, match.start()) + 1
    line_end = resume_text.find("\n", match.end())
    end = len(resume_text) if line_end < 0 else line_end
    return (EvidenceSpan(resume_text[start:end], start, end),)


def _eligibility_evidence(
    requirement: Requirement, resume_text: str, normalized_text: str
) -> tuple[EvidenceStatus, Tuple[EvidenceSpan, ...]]:
    source = requirement.source_text
    if _CLEARANCE_REQUIREMENT.search(source):
        negative = _NEGATIVE_CLEARANCE.search(resume_text)
        positive = _POSITIVE_CLEARANCE.search(resume_text)
    elif _TRAVEL_REQUIREMENT.search(source):
        negative = _NEGATIVE_TRAVEL.search(resume_text)
        positive = _POSITIVE_TRAVEL.search(resume_text)
        if negative is None and positive is not None:
            required_percent = _PERCENT.search(source)
            if required_percent is not None:
                evidence = _match_evidence(resume_text, positive)
                offered = [
                    float(match.group(1))
                    for span in evidence
                    for match in _PERCENT.finditer(span.text)
                ]
                if not offered or max(offered) < float(required_percent.group(1)):
                    positive = None
    elif (location := _LOCATION_REQUIREMENT.search(source)) is not None:
        target = location.group(1).strip()
        negative_pattern = re.compile(
            r"\b(?:not (?:located|based|residing|living)|unable to relocate)\b"
            r"[^\n.;]{0,80}" + re.escape(target),
            re.I,
        )
        negative = negative_pattern.search(resume_text)
        evidence = _evidence_spans(resume_text, (target,), limit=1)
        if negative is not None:
            return EvidenceStatus.CONTRADICTED, _match_evidence(
                resume_text, negative
            )
        if evidence:
            return EvidenceStatus.MET, evidence
        return EvidenceStatus.UNKNOWN, ()
    elif _LICENSE_REQUIREMENT.search(source):
        negative = _NEGATIVE_LICENSE.search(resume_text)
        positive = _POSITIVE_LICENSE.search(resume_text)
    elif _AUTHORIZATION_REQUIREMENT.search(source):
        negative = _NEGATIVE_AUTHORIZATION.search(resume_text)
        positive = _POSITIVE_AUTHORIZATION.search(resume_text)
    else:
        return EvidenceStatus.UNKNOWN, ()
    if negative is not None:
        return EvidenceStatus.CONTRADICTED, _match_evidence(resume_text, negative)
    if positive is not None:
        return EvidenceStatus.MET, _match_evidence(resume_text, positive)
    return EvidenceStatus.UNKNOWN, ()


def _criterion(
    requirement: Requirement, resume_text: str, normalized_text: str
) -> CriterionResult:
    if is_non_fit_clause(requirement.source_text):
        return CriterionResult(
            requirement_id=requirement.requirement_id,
            priority=requirement.priority,
            kind=requirement.kind,
            source_text=requirement.source_text,
            status=EvidenceStatus.UNKNOWN,
            matched_terms=(),
            missing_term_groups=requirement.term_groups,
            evidence=(),
            weight=0.0,
            value=0.0,
            confidence=0.0,
            match_method="excluded_non_fit",
        )
    if requirement.kind is RequirementKind.ELIGIBILITY:
        status, evidence = _eligibility_evidence(
            requirement, resume_text, normalized_text
        )
        return CriterionResult(
            requirement_id=requirement.requirement_id,
            priority=requirement.priority,
            kind=requirement.kind,
            source_text=requirement.source_text,
            status=status,
            matched_terms=(),
            missing_term_groups=requirement.term_groups,
            evidence=evidence,
            weight=0.0,
            value=0.0,
            confidence=0.0 if status is EvidenceStatus.UNKNOWN else 1.0,
            match_method=(
                "unresolved" if status is EvidenceStatus.UNKNOWN else "literal"
            ),
        )

    matched = []
    missing = []
    for group in requirement.term_groups:
        alias = _matched_alias(normalized_text, group)
        if alias is None:
            missing.append(group)
        else:
            matched.append(alias)
    evidence = _evidence_spans(resume_text, matched)
    groups = len(requirement.term_groups)
    coverage = len(matched) / groups if groups else 0.0
    if groups and coverage == 1:
        status = EvidenceStatus.MET
    elif coverage > 0:
        status = EvidenceStatus.PARTIAL
    else:
        status = EvidenceStatus.NOT_EVIDENCED

    if requirement.minimum_years is not None and status is EvidenceStatus.MET:
        years = _years_near_evidence(evidence, matched)
        if years is None or years < requirement.minimum_years:
            status = EvidenceStatus.PARTIAL

    value = {
        EvidenceStatus.MET: 1.0,
        EvidenceStatus.PARTIAL: 0.5,
        EvidenceStatus.UNKNOWN: 0.0,
        EvidenceStatus.NOT_EVIDENCED: 0.0,
        EvidenceStatus.CONTRADICTED: 0.0,
    }[status]
    weight = 2.0 if requirement.priority is RequirementPriority.REQUIRED else 1.0
    return CriterionResult(
        requirement_id=requirement.requirement_id,
        priority=requirement.priority,
        kind=requirement.kind,
        source_text=requirement.source_text,
        status=status,
        matched_terms=tuple(matched),
        missing_term_groups=tuple(missing),
        evidence=evidence,
        weight=weight,
        value=value,
    )


def _validated_semantic_overrides(
    resume_text: str,
    graph: RequirementGraph,
    semantic_overrides: Optional[Mapping[str, Mapping[str, Any]]],
) -> Dict[str, Mapping[str, Any]]:
    if semantic_overrides is None:
        return {}
    if not isinstance(semantic_overrides, Mapping):
        raise ResumeLabError(
            "semantic_overrides must be an object keyed by requirement_id"
        )
    requirements = {item.requirement_id: item for item in graph.requirements}
    if len(requirements) != len(graph.requirements):
        raise ResumeLabError("requirement graph contains duplicate IDs")
    result: Dict[str, Mapping[str, Any]] = {}
    for requirement_id, raw in semantic_overrides.items():
        if not isinstance(requirement_id, str) or requirement_id not in requirements:
            raise ResumeLabError(
                "semantic override references an unknown requirement_id"
            )
        if is_non_fit_clause(requirements[requirement_id].source_text):
            raise ResumeLabError(
                "semantic overrides cannot score a non-fit requirement"
            )
        if requirements[requirement_id].kind is RequirementKind.ELIGIBILITY:
            raise ResumeLabError(
                "semantic overrides cannot resolve an eligibility requirement"
            )
        if not isinstance(raw, Mapping) or set(raw) != {
            "status",
            "evidence",
            "confidence",
        }:
            raise ResumeLabError(
                "semantic override requires exactly status, evidence, and confidence"
            )
        status_value = raw["status"]
        try:
            status = EvidenceStatus(
                str(
                    status_value.value
                    if hasattr(status_value, "value")
                    else status_value
                )
            )
        except ValueError:
            raise ResumeLabError("semantic override status is invalid") from None
        confidence = raw["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise ResumeLabError(
                "semantic override confidence must be between zero and one"
            )
        raw_evidence = raw["evidence"]
        if isinstance(raw_evidence, (str, bytes)) or not isinstance(
            raw_evidence, Sequence
        ):
            raise ResumeLabError(
                "semantic override evidence must be a sequence of spans"
            )
        if len(raw_evidence) > 3:
            raise ResumeLabError(
                "semantic override accepts at most three evidence spans"
            )
        evidence = []
        seen = set()
        for raw_span in raw_evidence:
            if isinstance(raw_span, EvidenceSpan):
                span = raw_span
            elif isinstance(raw_span, Mapping) and set(raw_span) == {
                "text",
                "start",
                "end",
            }:
                span = EvidenceSpan(
                    text=raw_span["text"],
                    start=raw_span["start"],
                    end=raw_span["end"],
                )
            else:
                raise ResumeLabError(
                    "semantic evidence spans require exactly text, start, and end"
                )
            if (
                not isinstance(span.text, str)
                or not span.text
                or "\x00" in span.text
                or isinstance(span.start, bool)
                or not isinstance(span.start, int)
                or isinstance(span.end, bool)
                or not isinstance(span.end, int)
                or span.start < 0
                or span.end <= span.start
                or span.end > len(resume_text)
                or resume_text[span.start : span.end] != span.text
            ):
                raise ResumeLabError(
                    "semantic evidence must exactly match a valid resume-text span"
                )
            key = (span.start, span.end, span.text)
            if key not in seen:
                seen.add(key)
                evidence.append(span)
        evidenced_status = status in {
            EvidenceStatus.MET,
            EvidenceStatus.PARTIAL,
            EvidenceStatus.CONTRADICTED,
        }
        if evidenced_status and not evidence:
            raise ResumeLabError("semantic override status requires source evidence")
        if not evidenced_status and evidence:
            raise ResumeLabError("unevidenced semantic status cannot contain evidence")
        result[requirement_id] = {
            "status": status,
            "evidence": tuple(evidence),
            "confidence": float(confidence),
        }
    # Force full JSON validation before this payload participates in a cache identity.
    canonical_json(result)
    return result


def _apply_semantic_overrides(
    criteria: Sequence[CriterionResult],
    graph: RequirementGraph,
    overrides: Mapping[str, Mapping[str, Any]],
) -> Tuple[CriterionResult, ...]:
    requirements = {item.requirement_id: item for item in graph.requirements}
    values = {
        EvidenceStatus.MET: 1.0,
        EvidenceStatus.PARTIAL: 0.5,
        EvidenceStatus.UNKNOWN: 0.0,
        EvidenceStatus.NOT_EVIDENCED: 0.0,
        EvidenceStatus.CONTRADICTED: 0.0,
    }
    resolved = []
    for criterion in criteria:
        override = overrides.get(criterion.requirement_id)
        # Literal matches, contradictions, and numeric partial matches are authoritative.
        if (
            criterion.kind is RequirementKind.ELIGIBILITY
            or override is None
            or criterion.status not in {
            EvidenceStatus.UNKNOWN,
            EvidenceStatus.NOT_EVIDENCED,
            }
        ):
            resolved.append(criterion)
            continue
        status = override["status"]
        evidence = override["evidence"]
        requirement = requirements[criterion.requirement_id]
        # A semantic alias cannot erase an explicit numeric duration requirement.
        if requirement.minimum_years is not None and status is EvidenceStatus.MET:
            semantic_terms = tuple(
                term
                for group in requirement.term_groups
                for term in group
                if any(
                    _literal_present(normalize_text(span.text), term)
                    for span in evidence
                )
            )
            years = _years_near_evidence(evidence, semantic_terms)
            if years is None or years < requirement.minimum_years:
                status = EvidenceStatus.PARTIAL
        resolved.append(
            replace(
                criterion,
                status=status,
                evidence=evidence,
                value=values[status],
                confidence=override["confidence"],
                match_method="semantic",
            )
        )
    return tuple(resolved)


def _visibility(graph: RequirementGraph, normalized_text: str) -> float:
    groups = []
    seen = set()
    for requirement in graph.requirements:
        if requirement.kind is RequirementKind.ELIGIBILITY:
            continue
        if is_non_fit_clause(requirement.source_text):
            continue
        for group in requirement.term_groups:
            key = tuple(sorted(normalize_text(value) for value in group))
            if key and key not in seen:
                seen.add(key)
                groups.append(group)
    if not groups:
        return 0.0
    present = sum(
        1 for group in groups if _matched_alias(normalized_text, group) is not None
    )
    return round(100.0 * present / len(groups), 2)


def score_ats_proxy(
    resume_text: str,
    graph: RequirementGraph,
    *,
    scorer_revision: str = SCORER_REVISION,
    semantic_overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> AtsProxyEvaluation:
    """Score only unique, visible evidence; repeated keywords receive no extra credit."""

    if not isinstance(resume_text, str) or not resume_text.strip():
        raise ValueError("resume_text must not be empty")
    if not isinstance(scorer_revision, str) or not scorer_revision.strip():
        raise ValueError("scorer_revision must not be empty")
    qualification_text = _qualification_text(resume_text)
    normalized = normalize_text(qualification_text)
    lexical_criteria = tuple(
        _criterion(requirement, resume_text, normalized)
        for requirement in graph.requirements
    )
    overrides = _validated_semantic_overrides(resume_text, graph, semantic_overrides)
    criteria = _apply_semantic_overrides(lexical_criteria, graph, overrides)
    scoreable = [item for item in criteria if item.weight > 0]
    denominator = sum(item.weight for item in scoreable)
    requirement_evidence = (
        round(
            100.0 * sum(item.weight * item.value for item in scoreable) / denominator,
            2,
        )
        if denominator
        else 0.0
    )
    visibility = _visibility(graph, normalized)
    readiness = round(0.8 * requirement_evidence + 0.2 * visibility, 2)
    eligibility_criteria = [
        item for item in criteria if item.kind is RequirementKind.ELIGIBILITY
    ]
    if any(item.status is EvidenceStatus.CONTRADICTED for item in eligibility_criteria):
        eligibility = EligibilityStatus.BLOCKED
    elif eligibility_criteria and any(
        item.status is EvidenceStatus.UNKNOWN for item in eligibility_criteria
    ):
        eligibility = EligibilityStatus.UNKNOWN
    else:
        eligibility = EligibilityStatus.CLEAR
    artifact_hash = sha256_text(resume_text)
    cache_key = content_sha256(
        {
            "scorer_revision": scorer_revision,
            "artifact_text_sha256": artifact_hash,
            "requirement_graph_fingerprint": graph.fingerprint,
            "semantic_overrides": overrides,
        }
    )
    return AtsProxyEvaluation(
        scorer_revision=scorer_revision,
        artifact_text_sha256=artifact_hash,
        requirement_graph_fingerprint=graph.fingerprint,
        requirement_evidence=requirement_evidence,
        search_visibility=visibility,
        screening_readiness=readiness,
        parsed_fit=readiness,
        eligibility_status=eligibility,
        criteria=criteria,
        cache_key=cache_key,
    )


__all__ = ["score_ats_proxy"]
