"""Fail-closed JSON adapter for local resume reasoning tasks."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Optional, Sequence, Union

from job_search.contracts import ContractError, validate_identifier

from .contracts import (
    NORMALIZATION_VALIDATOR_REVISION as NORMALIZATION_VALIDATOR_REVISION,
)
from .grounding import (
    GroundingValidationError,
    validate_grounded_rewrite_text,
)
from .tex import attest_real_resume_links, render_resume_tex

if TYPE_CHECKING:
    from .runpod_model import RunpodResumeModelConfig


MODEL_SCHEMA_VERSION = 1
LOCAL_MODEL_CONFIG_VERSION = 1
MAX_MODEL_INPUT_BYTES = 512 * 1024
MAX_MODEL_OUTPUT_BYTES = 512 * 1024
MAX_MODEL_CONFIG_BYTES = 64 * 1024
MAX_MODEL_TREE_ENTRIES = 16_384
MAX_MODEL_TREE_PATH_BYTES = 4 * 1024 * 1024
MAX_MODEL_TREE_BYTES = 2 * 1024**4
MODEL_TREE_MANIFEST_VERSION = 1
VARIANT_KINDS = frozenset(
    {"grounded_rewrite", "standard_exaggerated", "market_ideal", "keyword_adversarial"}
)
SYNTHETIC_VARIANTS = frozenset(
    {"standard_exaggerated", "market_ideal", "keyword_adversarial"}
)
REQUIREMENT_KINDS = frozenset(
    {"required", "preferred", "responsibility", "eligibility"}
)
REQUIREMENT_LOGIC = frozenset({"atomic", "all", "any", "equivalent"})
EVIDENCE_STATUSES = frozenset(
    {"met", "partial", "not_evidenced", "contradicted", "unknown"}
)
_NUMBER = re.compile(r"(?<![\w])(?:[$€£])?\d[\d,.]*(?:%|[kKmMbB])?(?![\w])")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LEXICAL_TOKEN = re.compile(r"\w+", re.UNICODE)
_SENIORITY = frozenset(
    {"intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager", "director", "vp", "chief"}
)
_SCOPE = frozenset(
    {"enterprise", "global", "production", "large-scale", "high-scale", "millions", "billions", "managed", "led", "owned", "architected"}
)


def _resume_content_output_contract() -> Mapping[str, Any]:
    """Describe the renderer's complete, exact structured-content shape to a model."""

    return {
        "identity": {
            "name": "required non-empty text",
            "contact_line": "optional text or empty string",
            "email": {
                "display": "optional visible text",
                "url": "optional mailto/email target or empty string",
            },
            "linkedin": {
                "display": "optional visible text",
                "url": "optional https target or empty string",
            },
            "github": {
                "display": "optional visible text",
                "url": "optional https target or empty string",
            },
        },
        "summary": "optional text or empty string",
        "experience": [
            {
                "company": "required text",
                "role": "required text",
                "location": "optional text or empty string",
                "dates": "optional text or empty string",
                "bullets": ["zero or more text strings"],
            }
        ],
        "projects": [
            {
                "name": "required text",
                "context": "optional text or empty string",
                "dates": "optional text or empty string",
                "url": "optional https target or empty string",
                "bullets": ["zero or more text strings"],
            }
        ],
        "education": [
            {
                "institution": "required text",
                "degree": "required text",
                "location": "optional text or empty string",
                "dates": "optional text or empty string",
                "details": "optional text or empty string",
            }
        ],
        "skills": [
            {
                "category": "required when items are present",
                "items": ["zero or more individual skill strings"],
            }
        ],
    }

# A normalization response is allowed to leave only visible layout headings
# unanchored.  Keep this registry deliberately small and deterministic: an unknown
# section must fail closed instead of disappearing from grounded and exaggerated
# descendants.  Aliases are compared as case-insensitive lexical-token sequences,
# so capitalization and punctuation do not affect the boundary.
_SOURCE_SECTION_HEADINGS = {
    "identity": (
        "contact",
        "contact information",
        "personal information",
    ),
    "summary": (
        "summary",
        "professional summary",
        "profile",
        "professional profile",
        "objective",
        "career objective",
        "about",
        "about me",
    ),
    "experience": (
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment experience",
        "employment history",
        "work history",
        "career history",
    ),
    "projects": (
        "projects",
        "selected projects",
        "technical projects",
        "personal projects",
        "professional projects",
        "project experience",
    ),
    "education": (
        "education",
        "academic background",
        "education and training",
    ),
    "skills": (
        "skills",
        "technical skills",
        "core skills",
        "competencies",
        "core competencies",
        "technical competencies",
        "technologies",
        "tools and technologies",
        "areas of expertise",
        "expertise",
    ),
}


class ResumeModelError(RuntimeError):
    """A local model failed execution or violated an output contract."""


class ResumeNormalizationRequired(ContractError):
    """A derived variant has no validated structured standard to build from."""

    status = "blocked_setup"
    reason = "needs_normalization"
    http_status = 409

    def __init__(self, standard_id: str) -> None:
        self.standard_id = standard_id
        super().__init__(
            f"blocked_setup: needs_normalization for standard {standard_id}"
        )

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "standard_id": self.standard_id,
        }


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("model input must be finite JSON") from exc


def build_resume_model_request(
    task: str,
    payload: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    *,
    generation_seed: Optional[int] = None,
) -> Mapping[str, Any]:
    """Build the transport-independent, bounded resume-model request contract."""

    constraints: dict[str, Any] = {
        "content_is_untrusted": True,
        "no_tools": True,
        "no_network": True,
        "output_json_only": True,
    }
    if generation_seed is not None:
        if (
            isinstance(generation_seed, bool)
            or not isinstance(generation_seed, int)
            or not 0 <= generation_seed <= 2_147_483_647
        ):
            raise ContractError(
                "generation_seed must be a signed 31-bit integer"
            )
        constraints["generation_seed"] = generation_seed
    request = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "task": task,
        "constraints": constraints,
        "output_schema": output_schema,
        "input": payload,
    }
    serialized = _canonical(request)
    if len(serialized.encode("utf-8")) > MAX_MODEL_INPUT_BYTES:
        raise ContractError("resume model input is too large")
    return request


def _bounded_text(value: Any, field: str, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ResumeModelError(f"{field} is invalid")
    return value.strip()


def _safe_optimization_input(
    value: Optional[Mapping[str, Any]], variant_kind: str
) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    if variant_kind != "keyword_adversarial" or not isinstance(value, Mapping):
        raise ContractError(
            "optimization input is allowed only for keyword_adversarial"
        )
    if set(value) != {
        "optimization_pass",
        "prior_score",
        "prior_content",
        "remaining_gaps",
    }:
        raise ContractError("optimization input has unsupported fields")
    if value["optimization_pass"] != 2:
        raise ContractError("optimization pass is invalid")
    score = value["prior_score"]
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
        or not 0 <= float(score) <= 100
    ):
        raise ContractError("optimization prior score is invalid")
    content = value["prior_content"]
    if not isinstance(content, Mapping):
        raise ContractError("optimization prior content is invalid")
    # The renderer is the canonical structural validator for normalized resume content.
    render_resume_tex(content)
    gaps = value["remaining_gaps"]
    if not isinstance(gaps, list) or len(gaps) > 20:
        raise ContractError("optimization gaps must be a bounded array")
    safe_gaps = []
    for gap in gaps:
        if not isinstance(gap, Mapping) or set(gap) != {
            "requirement_id",
            "source_text",
            "status",
            "weight",
            "missing_term_groups",
        }:
            raise ContractError("optimization gap is invalid")
        requirement_id = str(gap["requirement_id"])
        validate_identifier(requirement_id, "requirement_id")
        source_text = _bounded_text(
            gap["source_text"], "optimization source_text", 2_000
        )
        status = str(gap["status"])
        if status not in EVIDENCE_STATUSES:
            raise ContractError("optimization gap status is invalid")
        weight = gap["weight"]
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or not 0 <= float(weight) <= 100
        ):
            raise ContractError("optimization gap weight is invalid")
        groups = gap["missing_term_groups"]
        if not isinstance(groups, list) or len(groups) > 50:
            raise ContractError("optimization missing term groups are invalid")
        safe_groups = []
        for group in groups:
            if not isinstance(group, list) or not 1 <= len(group) <= 20:
                raise ContractError("optimization missing term group is invalid")
            safe_groups.append(
                [
                    _bounded_text(term, "optimization missing term", 200)
                    for term in group
                ]
            )
        safe_gaps.append(
            {
                "requirement_id": requirement_id,
                "source_text": source_text,
                "status": status,
                "weight": weight,
                "missing_term_groups": safe_groups,
            }
        )
    safe = {
        "optimization_pass": 2,
        "prior_score": score,
        "prior_content": content,
        "remaining_gaps": safe_gaps,
    }
    encoded = _canonical(safe)
    if len(encoded.encode("utf-8")) > 300_000:
        raise ContractError("optimization input is too large")
    return json.loads(encoded)


def _exact_keys(value: Any, expected: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ResumeModelError(f"{field} has an invalid schema")
    return value


def _identifier(value: Any, field: str) -> str:
    try:
        validate_identifier(value, field)
    except (TypeError, ValueError) as exc:
        raise ResumeModelError(str(exc)) from exc
    return str(value)


def _finite_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResumeModelError("confidence must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ResumeModelError("confidence must be between zero and one")
    return result


def validate_requirement_output(output: Any, job_description: str) -> Mapping[str, Any]:
    value = _exact_keys(output, {"requirements"}, "requirement extraction")
    rows = value["requirements"]
    if not isinstance(rows, list) or len(rows) > 200:
        raise ResumeModelError("requirements must be a bounded array")
    identifiers: set[str] = set()
    source_spans: list[tuple[int, int]] = []
    normalized = []
    expected = {"requirement_id", "text", "kind", "logic", "priority", "source_start", "source_end"}
    for row in rows:
        item = _exact_keys(row, expected, "requirement")
        requirement_id = _identifier(item["requirement_id"], "requirement_id")
        if requirement_id in identifiers:
            raise ResumeModelError("requirement ids must be unique")
        identifiers.add(requirement_id)
        text = _bounded_text(item["text"], "requirement text")
        kind = item["kind"]
        logic = item["logic"]
        priority = item["priority"]
        start, end = item["source_start"], item["source_end"]
        if kind not in REQUIREMENT_KINDS or logic not in REQUIREMENT_LOGIC:
            raise ResumeModelError("requirement kind or logic is invalid")
        if isinstance(priority, bool) or priority not in {0, 1, 2}:
            raise ResumeModelError("requirement priority is invalid")
        if kind == "required" and priority != 2:
            raise ResumeModelError("required qualifications must have priority 2")
        if kind == "eligibility" and priority != 0:
            raise ResumeModelError("eligibility must remain outside fit scoring")
        if any(isinstance(part, bool) or not isinstance(part, int) for part in (start, end)):
            raise ResumeModelError("requirement source span is invalid")
        if not 0 <= start < end <= len(job_description) or job_description[start:end] != text:
            raise ResumeModelError("requirement text is not an exact job-description span")
        if any(start < prior_end and prior_start < end for prior_start, prior_end in source_spans):
            raise ResumeModelError("requirement source spans must not overlap")
        source_spans.append((start, end))
        normalized.append(dict(item))
    return {"requirements": normalized}


def validate_evidence_output(output: Any, candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    value = _exact_keys(output, {"status", "confidence", "reason", "evidence"}, "evidence adjudication")
    if value["status"] not in EVIDENCE_STATUSES:
        raise ResumeModelError("evidence status is invalid")
    confidence = _finite_confidence(value["confidence"])
    reason = _bounded_text(value["reason"], "evidence reason", 4000)
    source = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ContractError("evidence candidates must be objects")
        claim_id = _identifier(candidate.get("claim_id"), "claim_id")
        text = candidate.get("text")
        if not isinstance(text, str):
            raise ContractError("evidence candidate text must be text")
        source[claim_id] = text
    evidence = value["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 20:
        raise ResumeModelError("evidence must be a bounded array")
    normalized = []
    for row in evidence:
        item = _exact_keys(row, {"claim_id", "quote"}, "evidence item")
        claim_id = _identifier(item["claim_id"], "claim_id")
        quote = _bounded_text(item["quote"], "evidence quote", 4000)
        if claim_id not in source or quote not in source[claim_id]:
            raise ResumeModelError("evidence quote is not an exact candidate claim substring")
        normalized.append({"claim_id": claim_id, "quote": quote})
    if value["status"] in {"met", "partial", "contradicted"} and not normalized:
        raise ResumeModelError("the evidence status requires a cited claim")
    if value["status"] in {"not_evidenced", "unknown"} and normalized:
        raise ResumeModelError("absent or unknown evidence cannot cite a claim")
    return {"status": value["status"], "confidence": confidence, "reason": reason, "evidence": normalized}


def validate_evidence_batch_output(
    output: Any,
    requirements: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Validate one model response covering every requested requirement exactly once."""

    if (
        not isinstance(requirements, Sequence)
        or isinstance(requirements, (str, bytes, bytearray))
        or len(requirements) > 64
    ):
        raise ContractError("evidence requirements must be a bounded array")
    ordered_ids: list[str] = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            raise ContractError("evidence requirements must be objects")
        requirement_id = _identifier(
            requirement.get("requirement_id"), "requirement_id"
        )
        if requirement_id in ordered_ids:
            raise ContractError("evidence requirement ids must be unique")
        ordered_ids.append(requirement_id)

    value = _exact_keys(output, {"adjudications"}, "batch evidence adjudication")
    rows = value["adjudications"]
    if not isinstance(rows, list) or len(rows) != len(ordered_ids):
        raise ResumeModelError(
            "batch evidence must adjudicate every requested requirement exactly once"
        )
    normalized: dict[str, Mapping[str, Any]] = {}
    expected = {"requirement_id", "status", "confidence", "reason", "evidence"}
    for row in rows:
        item = _exact_keys(row, expected, "batch evidence item")
        requirement_id = _identifier(item["requirement_id"], "requirement_id")
        if requirement_id not in ordered_ids or requirement_id in normalized:
            raise ResumeModelError(
                "batch evidence requirement ids must match the request exactly once"
            )
        normalized[requirement_id] = validate_evidence_output(
            {key: item[key] for key in expected if key != "requirement_id"},
            candidates,
        )
    if set(normalized) != set(ordered_ids):
        raise ResumeModelError(
            "batch evidence must adjudicate every requested requirement exactly once"
        )
    return {
        "adjudications": [
            {"requirement_id": requirement_id, **normalized[requirement_id]}
            for requirement_id in ordered_ids
        ]
    }


def select_base_standard(standards: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not isinstance(standards, Sequence) or isinstance(standards, (str, bytes)):
        raise ContractError("standard_candidates must be an array")
    ranks: set[int] = set()
    ranked = []
    for item in standards:
        if not isinstance(item, Mapping):
            raise ContractError("standard candidates must be objects")
        standard_id = item.get("standard_id")
        rank = item.get("rank")
        if not isinstance(standard_id, str) or not standard_id:
            raise ContractError("standard candidate id is invalid")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0 or rank in ranks:
            raise ContractError("standard candidate ranks must be unique positive integers")
        ranks.add(rank)
        ranked.append(item)
    winners = [item for item in ranked if item["rank"] == 1]
    if len(winners) != 1:
        raise ContractError("exactly one highest-ranked standard with rank 1 is required")
    if not isinstance(winners[0].get("content"), Mapping):
        raise ResumeNormalizationRequired(str(winners[0]["standard_id"]))
    return winners[0]


def variant_generation_status(
    variant_kind: str, standards: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Report whether a variant can be generated without invoking a model."""

    if variant_kind not in VARIANT_KINDS:
        raise ContractError("unknown resume variant kind")
    if variant_kind in {"market_ideal", "keyword_adversarial"}:
        return {"status": "ready", "reason": None, "base_standard_id": None}
    try:
        base = select_base_standard(standards)
    except ResumeNormalizationRequired as exc:
        return exc.as_mapping()
    return {
        "status": "ready",
        "reason": None,
        "base_standard_id": base["standard_id"],
    }


def _json_pointer(document: Any, path: str) -> Any:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1000:
        raise ResumeModelError("claim path is invalid")
    value = document
    for raw in path[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ResumeModelError("claim path does not exist in generated content")
    if isinstance(value, Mapping) and set(value) >= {"text"}:
        value = value["text"]
    return value


def _claimable_paths(content: Mapping[str, Any]) -> set[str]:
    paths = set()
    if content.get("summary"):
        paths.add("/summary")
    for section in ("experience", "projects"):
        for index, entry in enumerate(content.get(section) or []):
            for bullet_index, _item in enumerate(entry.get("bullets") or []):
                paths.add(f"/{section}/{index}/bullets/{bullet_index}")
    for index, entry in enumerate(content.get("skills") or []):
        for item_index, _item in enumerate(entry.get("items") or []):
            paths.add(f"/skills/{index}/items/{item_index}")
    for index, entry in enumerate(content.get("education") or []):
        if entry.get("details"):
            paths.add(f"/education/{index}/details")
    return paths


def _fixed_grounded_shape(content: Mapping[str, Any]) -> Any:
    return {
        "identity": content.get("identity"),
        "experience": [
            {key: entry.get(key) for key in ("company", "role", "location", "dates")}
            for entry in content.get("experience") or []
        ],
        "projects": [
            {key: entry.get(key) for key in ("name", "context", "dates", "url")}
            for entry in content.get("projects") or []
        ],
        "education": [
            {key: entry.get(key) for key in ("institution", "degree", "location", "dates")}
            for entry in content.get("education") or []
        ],
        "skill_categories": [
            entry.get("category") for entry in content.get("skills") or []
        ],
        "slot_counts": {
            "summary": int(bool(content.get("summary"))),
            "experience": [len(entry.get("bullets") or []) for entry in content.get("experience") or []],
            "projects": [len(entry.get("bullets") or []) for entry in content.get("projects") or []],
            "education_details": [
                int(bool(entry.get("details")))
                for entry in content.get("education") or []
            ],
            "skills": [len(entry.get("items") or []) for entry in content.get("skills") or []],
        },
    }


def _visible_fixed_paths(content: Mapping[str, Any]) -> set[str]:
    """Return structural fields that must be anchored in imported PDF text.

    Hyperlink targets are intentionally excluded: PDF text extraction exposes their
    display labels, not their hidden URI annotations.
    """

    paths = {"/identity/name"}
    identity = content.get("identity") or {}
    if identity.get("contact_line"):
        paths.add("/identity/contact_line")
    for key in ("email", "linkedin", "github"):
        item = identity.get(key)
        if isinstance(item, str) and item:
            paths.add(f"/identity/{key}")
        elif isinstance(item, Mapping) and item.get("display"):
            paths.add(f"/identity/{key}/display")
    for section, keys in (
        ("experience", ("company", "role", "location", "dates")),
        ("projects", ("name", "context", "dates")),
        ("education", ("institution", "degree", "location", "dates")),
    ):
        for index, entry in enumerate(content.get(section) or []):
            for key in keys:
                if entry.get(key):
                    paths.add(f"/{section}/{index}/{key}")
    for index, entry in enumerate(content.get("skills") or []):
        if entry.get("category"):
            paths.add(f"/skills/{index}/category")
    return paths


def _resume_path_order(content: Mapping[str, Any], *, template_version: str = "career-ops-v1") -> tuple[str, ...]:
    """Return visible value paths in the renderer's logical reading order."""

    result = ["/identity/name"]
    identity = content.get("identity") or {}
    if identity.get("contact_line"):
        result.append("/identity/contact_line")
    for key in ("email", "linkedin", "github"):
        item = identity.get(key)
        if isinstance(item, str) and item:
            result.append(f"/identity/{key}")
        elif isinstance(item, Mapping) and item.get("display"):
            result.append(f"/identity/{key}/display")
    if content.get("summary"):
        result.append("/summary")
    education_paths = []
    education_keys = (("institution", "location", "degree", "dates", "details")
        if template_version == "jake-v1" else ("institution", "dates", "degree", "location", "details"))
    for index, entry in enumerate(content.get("education") or []):
        for key in education_keys:
            if entry.get(key):
                education_paths.append(f"/education/{index}/{key}")
    if template_version == "jake-v1":
        result.extend(education_paths)
    for index, entry in enumerate(content.get("experience") or []):
        for key in (("role", "dates", "company", "location") if template_version == "jake-v1"
                    else ("company", "dates", "role", "location")):
            if entry.get(key):
                result.append(f"/experience/{index}/{key}")
        result.extend(
            f"/experience/{index}/bullets/{bullet_index}"
            for bullet_index, _item in enumerate(entry.get("bullets") or [])
        )
    for index, entry in enumerate(content.get("projects") or []):
        for key in ("name", "context", "dates"):
            if entry.get(key):
                result.append(f"/projects/{index}/{key}")
        result.extend(
            f"/projects/{index}/bullets/{bullet_index}"
            for bullet_index, _item in enumerate(entry.get("bullets") or [])
        )
    if template_version != "jake-v1":
        result.extend(education_paths)
    for index, entry in enumerate(content.get("skills") or []):
        if entry.get("category"):
            result.append(f"/skills/{index}/category")
        result.extend(
            f"/skills/{index}/items/{item_index}"
            for item_index, _item in enumerate(entry.get("items") or [])
        )
    return tuple(result)


def _validate_normalization_spans(
    content: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    """Reject span reuse, overlap, and structural path misattribution."""

    ordered = sorted(
        (
            int(item["source_start"]),
            int(item["source_end"]),
            str(item["path"]),
        )
        for item in rows
    )
    previous_end = -1
    for start, end, _path in ordered:
        if start < previous_end:
            raise ResumeModelError(
                "normalization source spans must be unique and non-overlapping"
            )
        previous_end = end
    # Imported sources can use either supported historical layout. Each complete
    # source must match one layout; never accept a model-selected arbitrary order.
    for version in ("jake-v1", "career-ops-v1"):
        rank = {path: index for index, path in enumerate(_resume_path_order(content, template_version=version))}
        path_ranks = [rank[path] for _start, _end, path in ordered]
        if path_ranks == sorted(path_ranks) and len(set(path_ranks)) == len(path_ranks):
            return
    raise ResumeModelError("normalization source spans do not follow rendered resume path order")


def _present_source_headings(content: Mapping[str, Any]) -> set[tuple[str, ...]]:
    """Return exact heading aliases permitted by the normalized document shape."""

    present = {"identity"}
    if content.get("summary"):
        present.add("summary")
    for section in ("experience", "projects", "education", "skills"):
        if content.get(section):
            present.add(section)
    return {
        tuple(match.group(0).casefold() for match in _LEXICAL_TOKEN.finditer(alias))
        for section in present
        for alias in _SOURCE_SECTION_HEADINGS[section]
    }


def _validate_normalization_source_coverage(
    content: Mapping[str, Any],
    parsed_pdf_text: str,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Require exact positional coverage of every meaningful source token.

    Validating all paths in the model-selected output is insufficient: a model can
    produce a smaller, internally consistent resume and silently omit an entire
    source section.  This gate reverses that trust direction.  Every lexical token
    in the imported PDF must be inside one already-validated exact source span.
    The sole exception is a standalone, deterministic heading for a section that
    is actually present in the normalized shape.  Unknown or absent-section
    headings therefore fail closed too.

    Coverage is positional rather than bag-of-words based, so repeated lines cannot
    borrow coverage from a retained occurrence elsewhere in the resume.
    """

    covered = bytearray(len(parsed_pdf_text))
    for item in rows:
        start = int(item["source_start"])
        end = int(item["source_end"])
        covered[start:end] = b"\x01" * (end - start)

    allowed_headings = _present_source_headings(content)
    offset = 0
    for line in parsed_pdf_text.splitlines(keepends=True):
        anchored_token = False
        uncovered_tokens: list[str] = []
        for match in _LEXICAL_TOKEN.finditer(line):
            start = offset + match.start()
            end = offset + match.end()
            token_coverage = covered[start:end]
            if token_coverage and all(token_coverage):
                anchored_token = True
            elif token_coverage and any(token_coverage):
                raise ResumeModelError(
                    "normalization source coverage partially anchors a source token"
                )
            else:
                uncovered_tokens.append(match.group(0).casefold())
        if uncovered_tokens and (
            anchored_token or tuple(uncovered_tokens) not in allowed_headings
        ):
            raise ResumeModelError(
                "normalization leaves meaningful parsed-PDF text unrepresented"
            )
        offset += len(line)


def _validate_source_span(
    item: Mapping[str, Any], parsed_pdf_text: str, *, field: str
) -> tuple[str, str, int, int]:
    path = _bounded_text(item["path"], f"{field} path", 1000)
    text = _bounded_text(item["text"], f"{field} text", 4000)
    start, end = item["source_start"], item["source_end"]
    if any(isinstance(part, bool) or not isinstance(part, int) for part in (start, end)):
        raise ResumeModelError(f"{field} source span is invalid")
    if not 0 <= start < end <= len(parsed_pdf_text):
        raise ResumeModelError(f"{field} source span is invalid")
    if parsed_pdf_text[start:end] != text:
        raise ResumeModelError(f"{field} is not an exact parsed-PDF span")
    return path, text, start, end


def validate_standard_normalization_output(
    output: Any, parsed_pdf_text: str
) -> Mapping[str, Any]:
    """Validate a model-produced structured view of an imported TeX/PDF resume."""

    value = _exact_keys(
        output, {"content", "fixed_fields", "claims"}, "standard normalization"
    )
    content = value["content"]
    if not isinstance(content, Mapping):
        raise ResumeModelError("normalized content must be an object")
    render_resume_tex(content)
    # The model may infer structure from visible PDF text, but PDF extraction does
    # not attest hidden URI annotations.  Preserve contact labels and derive their
    # targets deterministically; discard every unattested project/explicit target.
    content = attest_real_resume_links(content)
    render_resume_tex(content)

    fixed_fields = value["fixed_fields"]
    if not isinstance(fixed_fields, list) or len(fixed_fields) > 500:
        raise ResumeModelError("fixed_fields must be a bounded array")
    normalized_fixed = []
    fixed_paths: set[str] = set()
    for raw in fixed_fields:
        item = _exact_keys(
            raw, {"path", "text", "source_start", "source_end"}, "fixed field"
        )
        path, text, start, end = _validate_source_span(
            item, parsed_pdf_text, field="fixed field"
        )
        if path in fixed_paths or path not in _visible_fixed_paths(content):
            raise ResumeModelError("fixed field path is duplicate or not structural")
        if _json_pointer(content, path) != text:
            raise ResumeModelError("fixed field text differs from normalized content")
        fixed_paths.add(path)
        normalized_fixed.append(
            {"path": path, "text": text, "source_start": start, "source_end": end}
        )
    if fixed_paths != _visible_fixed_paths(content):
        raise ResumeModelError("every visible fixed field needs an exact source span")

    claims = value["claims"]
    if not isinstance(claims, list) or len(claims) > 500:
        raise ResumeModelError("claims must be a bounded array")
    normalized_claims = []
    claim_paths: set[str] = set()
    claim_ids: set[str] = set()
    for raw in claims:
        raw_claim = _exact_keys(
            raw,
            (
                {"claim_id", "path", "text", "source_start", "source_end"}
                if not isinstance(raw, Mapping)
                or "allowed_equivalent_terms" not in raw
                else {
                    "claim_id",
                    "path",
                    "text",
                    "source_start",
                    "source_end",
                    "allowed_equivalent_terms",
                }
            ),
            "source claim",
        )
        if (
            "allowed_equivalent_terms" in raw_claim
            and raw_claim["allowed_equivalent_terms"] != []
        ):
            raise ResumeModelError(
                "normalization cannot supply equivalent source terms"
            )
        item = {
            key: raw_claim[key]
            for key in ("claim_id", "path", "text", "source_start", "source_end")
        }
        claim_id = _identifier(item["claim_id"], "claim_id")
        path, text, start, end = _validate_source_span(
            item, parsed_pdf_text, field="source claim"
        )
        if claim_id in claim_ids or path in claim_paths or path not in _claimable_paths(content):
            raise ResumeModelError("source claim id/path is duplicate or not claimable")
        if _json_pointer(content, path) != text:
            raise ResumeModelError("source claim text differs from normalized content")
        claim_ids.add(claim_id)
        claim_paths.add(path)
        normalized_claims.append(
            {
                "claim_id": claim_id,
                "path": path,
                "text": text,
                "source_start": start,
                "source_end": end,
                "allowed_equivalent_terms": [],
            }
        )
    if claim_paths != _claimable_paths(content):
        raise ResumeModelError("every editable slot needs one atomic source claim")
    source_rows = [*normalized_fixed, *normalized_claims]
    _validate_normalization_spans(content, source_rows)
    _validate_normalization_source_coverage(content, parsed_pdf_text, source_rows)
    return {
        "content": dict(content),
        "fixed_fields": normalized_fixed,
        "claims": normalized_claims,
    }


def _terms(text: str, vocabulary: set[str]) -> set[str]:
    normalized = text.casefold()
    return {term for term in vocabulary if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", normalized)}


def _lexical_tokens(text: str) -> tuple[str, ...]:
    """Return case-insensitive lexical tokens, treating punctuation as movable."""

    return tuple(match.group(0).casefold() for match in _LEXICAL_TOKEN.finditer(text))


def _contains_token_sequence(tokens: Sequence[str], candidate: Sequence[str]) -> bool:
    if not candidate or len(candidate) > len(tokens):
        return False
    width = len(candidate)
    return any(
        tuple(tokens[index : index + width]) == tuple(candidate)
        for index in range(len(tokens) - width + 1)
    )


def validate_variant_output(
    output: Any,
    variant_kind: str,
    base: Mapping[str, Any] | None,
    source_claims: Sequence[Mapping[str, Any]],
    guarded_terms: Sequence[str],
) -> Mapping[str, Any]:
    value = _exact_keys(
        output,
        {"variant_kind", "base_standard_id", "synthetic", "research_only", "content", "claims"},
        "generated variant",
    )
    if variant_kind not in VARIANT_KINDS or value["variant_kind"] != variant_kind:
        raise ResumeModelError("variant kind is invalid or changed")
    synthetic = variant_kind in SYNTHETIC_VARIANTS
    if value["synthetic"] is not synthetic or value["research_only"] is not synthetic:
        raise ResumeModelError("variant synthetic/research boundary is invalid")
    expected_base = base.get("standard_id") if base else None
    if value["base_standard_id"] != expected_base:
        raise ResumeModelError("variant base standard is invalid")
    content = value["content"]
    if not isinstance(content, Mapping):
        raise ResumeModelError("variant content must be an object")
    render_resume_tex(content)  # Strictly validate the generated document shape.
    comparison_base = base
    if variant_kind == "grounded_rewrite":
        # Re-apply the link boundary even for a standard normalized before the
        # current validator revision.  The returned selectable content can contain
        # no model-controlled hidden target.
        content = attest_real_resume_links(content)
        assert base is not None
        comparison_base = {
            **base,
            "content": attest_real_resume_links(base["content"]),
        }
        render_resume_tex(content)

    source: dict[str, Mapping[str, Any]] = {}
    for row in source_claims:
        if not isinstance(row, Mapping):
            raise ContractError("source claims must be objects")
        claim_id = _identifier(row.get("claim_id"), "claim_id")
        text = row.get("text")
        aliases = row.get("allowed_equivalent_terms", [])
        source_path = row.get("path")
        if not isinstance(text, str) or not isinstance(aliases, list) or any(not isinstance(term, str) for term in aliases):
            raise ContractError("source claim text or equivalent terms are invalid")
        if source_path is not None and (
            not isinstance(source_path, str)
            or not source_path.startswith("/")
            or len(source_path) > 1_000
        ):
            raise ContractError("source claim path is invalid")
        if claim_id in source:
            raise ContractError("source claim ids must be unique")
        source[claim_id] = {
            "text": text,
            "path": source_path,
            "allowed_equivalent_terms": aliases,
        }

    claims = value["claims"]
    if not isinstance(claims, list) or len(claims) > 500:
        raise ResumeModelError("variant claims must be a bounded array")
    normalized_claims = []
    paths: set[str] = set()
    ids: set[str] = set()
    used_grounding_sources: set[str] = set()
    allowed_origins = {
        "grounded_rewrite": {"source", "source_rewrite"},
        "standard_exaggerated": {"source", "source_rewrite", "synthetic"},
        "market_ideal": {"synthetic"},
        "keyword_adversarial": {"synthetic", "keyword_adversarial"},
    }[variant_kind]
    guarded = {term.strip().casefold() for term in guarded_terms if isinstance(term, str) and term.strip()}
    for raw in claims:
        item = _exact_keys(
            raw,
            {"claim_id", "path", "text", "origin", "source_claim_ids", "added_equivalent_terms"},
            "variant claim",
        )
        claim_id = _identifier(item["claim_id"], "claim_id")
        path = _bounded_text(item["path"], "claim path", 1000)
        text = _bounded_text(item["text"], "claim text", 4000)
        if claim_id in ids or path in paths:
            raise ResumeModelError("variant claim ids and paths must be unique")
        ids.add(claim_id)
        paths.add(path)
        if _json_pointer(content, path) != text:
            raise ResumeModelError("variant claim text differs from generated content")
        if item["origin"] not in allowed_origins:
            raise ResumeModelError("claim origin is invalid for this variant")
        source_ids = item["source_claim_ids"]
        added = item["added_equivalent_terms"]
        if not isinstance(source_ids, list) or not isinstance(added, list) or any(not isinstance(part, str) for part in (*source_ids, *added)):
            raise ResumeModelError("claim provenance arrays are invalid")
        if any(source_id not in source for source_id in source_ids):
            raise ResumeModelError("variant cites an unknown source claim")
        if variant_kind in {"market_ideal", "keyword_adversarial"} and (source_ids or added):
            raise ResumeModelError("independent synthetic variants cannot cite a standard")
        if variant_kind == "grounded_rewrite":
            if not source_ids or len(set(source_ids)) != len(source_ids):
                raise ResumeModelError(
                    "every grounded claim requires unique source claims"
                )
            source_id = source_ids[0]
            if source_id in used_grounding_sources:
                raise ResumeModelError(
                    "a source claim may be used by only one grounded slot"
                )
            used_grounding_sources.add(source_id)
            if source[source_id]["path"] is None:
                raise ResumeModelError(
                    "every grounded source claim requires an immutable path"
                )
            if source[source_id]["path"] != path:
                raise ResumeModelError(
                    "grounded claim path differs from its immutable source path"
                )
            support_paths = [source[value]["path"] for value in source_ids[1:]]
            if support_paths and not (
                path == "/summary" or path.startswith("/skills/")
            ):
                raise ResumeModelError(
                    "skill support cannot be projected into historical experience or projects"
                )
            if any(
                not isinstance(support_path, str)
                or not support_path.startswith("/skills/")
                for support_path in support_paths
            ):
                raise ResumeModelError(
                    "grounded supporting sources must be user-attested skill claims"
                )
            source_text = str(source[source_id]["text"])
            allowed = {
                term.casefold()
                for source_id in source_ids
                for term in source[source_id]["allowed_equivalent_terms"]
            }
            added_set = {term.casefold() for term in added}
            if len(added_set) != len(added):
                raise ResumeModelError("grounded equivalent terms must be unique")
            if not added_set <= allowed or any(term not in text.casefold() for term in added_set):
                raise ResumeModelError("grounded rewrite added vocabulary is not an allowed equivalent")
            output_numbers = [item.casefold() for item in _NUMBER.findall(text)]
            source_numbers = [
                item.casefold() for item in _NUMBER.findall(source_text)
            ]
            if output_numbers != source_numbers:
                raise ResumeModelError(
                    "grounded rewrite changed or removed a metric, or introduced "
                    "a new metric"
                )
            for vocabulary, label in ((_SENIORITY, "seniority"), (_SCOPE, "scope")):
                if not _terms(text, set(vocabulary)) <= _terms(source_text, set(vocabulary)):
                    raise ResumeModelError(f"grounded rewrite introduced new {label}")
            supported_guarded = set().union(
                *(
                    _terms(str(source[value]["text"]), guarded)
                    for value in source_ids
                )
            )
            newly_guarded = _terms(text, guarded) - _terms(
                source_text, guarded
            )
            if not newly_guarded <= (added_set | supported_guarded):
                raise ResumeModelError("grounded rewrite introduced an unapproved tool or JD term")

            output_tokens = _lexical_tokens(text)
            for term in added:
                equivalent_tokens = _lexical_tokens(term)
                if not _contains_token_sequence(output_tokens, equivalent_tokens):
                    raise ResumeModelError(
                        "grounded rewrite declared an equivalent term absent from its text"
                    )
            try:
                validate_grounded_rewrite_text(
                    text,
                    [str(source[value]["text"]) for value in source_ids],
                )
            except GroundingValidationError as exc:
                raise ResumeModelError(str(exc)) from exc
        normalized_claims.append(dict(item))
    if variant_kind == "grounded_rewrite":
        assert comparison_base is not None
        if _fixed_grounded_shape(content) != _fixed_grounded_shape(
            comparison_base["content"]
        ):
            raise ResumeModelError("grounded rewrite changed fixed identity, history, or slot structure")
        if paths != _claimable_paths(content):
            raise ResumeModelError("every editable grounded slot needs exactly one provenance record")
    if variant_kind == "standard_exaggerated":
        assert base is not None
        if _fixed_grounded_shape(content) != _fixed_grounded_shape(base["content"]):
            raise ResumeModelError(
                "standard exaggeration changed fixed identity, history, or slot structure"
            )
        if paths != _claimable_paths(content):
            raise ResumeModelError(
                "every exaggerated editable slot needs exactly one provenance record"
            )
        for item in normalized_claims:
            path = str(item["path"])
            unchanged = _json_pointer(content, path) == _json_pointer(
                base["content"], path
            )
            source_ids = item["source_claim_ids"]
            added = item["added_equivalent_terms"]
            if unchanged:
                if (
                    item["origin"] not in {"source", "source_rewrite"}
                    or len(source_ids) != 1
                    or added
                ):
                    raise ResumeModelError(
                        "every unchanged exaggerated slot must cite its same-path source"
                    )
                source_id = source_ids[0]
                if (
                    source[source_id]["path"] != path
                    or source[source_id]["text"] != item["text"]
                ):
                    raise ResumeModelError(
                        "unchanged exaggerated provenance must match its source path and text"
                    )
            elif (
                item["origin"] != "synthetic"
                or source_ids
                or added
            ):
                raise ResumeModelError(
                    "every exaggerated editable change must be isolated synthetic content"
                )
    if (
        variant_kind in {"market_ideal", "keyword_adversarial"}
        and paths != _claimable_paths(content)
    ):
        raise ResumeModelError(
            "every editable independent synthetic slot needs exactly one provenance record"
        )
    return {
        "variant_kind": variant_kind,
        "base_standard_id": expected_base,
        "synthetic": synthetic,
        "research_only": synthetic,
        "content": dict(content),
        "claims": normalized_claims,
    }


def _sandbox_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _model_read_path_is_too_broad(path: Path) -> bool:
    resolved = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    repository = Path(__file__).resolve().parents[2]
    data_root = Path("/System/Volumes/Data")
    mirrored_home = data_root / home.relative_to("/")
    mirrored_repository = data_root / repository.relative_to("/")
    forbidden = {
        value.resolve()
        for value in {
            Path("/"),
            home,
            repository,
            Path("/System"),
            Path("/usr"),
            Path("/Library"),
            Path("/private"),
            Path("/var"),
            Path("/etc"),
            data_root,
            data_root / "Users",
            mirrored_home,
            mirrored_repository,
            Path("/Volumes"),
        }
    }
    return (
        resolved in forbidden
        or resolved in home.parents
        or resolved.parent == Path("/Volumes")
    )


def macos_model_sandbox(
    command: Sequence[str], directory: Path, allowed_read_paths: Sequence[Path]
) -> tuple[str, ...]:
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.exists():
        raise ResumeModelError("local resume model isolation is unavailable")
    located = (
        shutil.which(command[0])
        if not Path(command[0]).is_absolute()
        else command[0]
    )
    if not located:
        raise ResumeModelError("local resume model executable was not found")
    invoked_executable = Path(located).expanduser().absolute()
    try:
        resolved_executable = invoked_executable.resolve(strict=True)
    except OSError as exc:
        raise ResumeModelError("local resume model executable was not found") from exc
    logical_directory = Path(directory).expanduser().absolute()
    directory = logical_directory.resolve()
    write_directories = {logical_directory, directory}
    executable_targets = {invoked_executable, resolved_executable}
    for parent in resolved_executable.parents:
        helper = parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
        if helper.is_file():
            executable_targets.add(helper.resolve())
            break
    paths = {
        Path("/System"),
        Path("/usr/bin"),
        Path("/usr/lib"),
        Path("/usr/share"),
        *executable_targets,
        *allowed_read_paths,
    }
    executable_literals = " ".join(
        f'(literal "{_sandbox_literal(str(path))}")'
        for path in sorted(executable_targets, key=str)
    )
    clauses = [
        "(version 1)", "(deny default)", "(deny network*)", "(allow process-info*)",
        # Python and dynamic loaders must stat the filesystem root while resolving
        # explicitly allowed descendants. This grants the node, never its subtree.
        '(allow file-read* (literal "/"))',
        f"(allow process-exec {executable_literals})",
        '(allow file-read* (literal "/dev/null") (literal "/dev/urandom"))',
    ]
    for writable in sorted(write_directories, key=str):
        clauses.append(
            f'(allow file-read* file-write* (subpath "{_sandbox_literal(str(writable))}"))'
        )
    resolved_paths = {
        Path(item).expanduser().resolve() for item in paths
    } | {invoked_executable}
    ancestors = {
        parent
        for path in (*resolved_paths, *write_directories)
        for parent in path.parents
        if parent != Path("/")
    }
    for ancestor in sorted(ancestors, key=str):
        clauses.append(
            f'(allow file-read* (literal "{_sandbox_literal(str(ancestor))}"))'
        )
    for path in sorted(resolved_paths, key=str):
        kind = "subpath" if path.is_dir() else "literal"
        clauses.append(f'(allow file-read* ({kind} "{_sandbox_literal(str(path))}"))')
    return (
        str(sandbox),
        "-p",
        "\n".join(clauses),
        "--",
        str(invoked_executable),
        *command[1:],
    )


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(stat.S_IFMT(value.st_mode)),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
    )


def _hash_regular_model_file(
    path: Path,
) -> tuple[str, tuple[int, ...]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResumeModelError("configured model artifact is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ResumeModelError("configured model artifact file is not regular")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _stat_identity(before) != _stat_identity(after):
        raise ResumeModelError("configured model artifact changed during verification")
    return digest.hexdigest(), _stat_identity(after)


def _bounded_model_tree_snapshot(
    root: Path,
) -> tuple[list[list[Any]], list[tuple[str, Path, tuple[int, ...]]]]:
    """Return a deterministic metadata manifest without following any links."""

    try:
        root_info = os.lstat(root)
    except OSError as exc:
        raise ResumeModelError("configured model artifact is unavailable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ResumeModelError("configured model directory is invalid")
    metadata: list[list[Any]] = [["directory", "", *_stat_identity(root_info)]]
    files: list[tuple[str, Path, tuple[int, ...]]] = []
    pending = [root]
    entry_count = 0
    path_bytes = 0
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise ResumeModelError("configured model directory cannot be read") from exc
        child_directories = []
        for entry in entries:
            entry_count += 1
            if entry_count > MAX_MODEL_TREE_ENTRIES:
                raise ResumeModelError("configured model directory has too many entries")
            candidate = Path(entry.path)
            try:
                relative = candidate.relative_to(root).as_posix()
                encoded_relative = relative.encode("utf-8")
                info = entry.stat(follow_symlinks=False)
            except (OSError, UnicodeEncodeError, ValueError) as exc:
                raise ResumeModelError("configured model directory entry is invalid") from exc
            path_bytes += len(encoded_relative)
            if path_bytes > MAX_MODEL_TREE_PATH_BYTES:
                raise ResumeModelError("configured model directory paths are too large")
            identity = _stat_identity(info)
            if stat.S_ISLNK(info.st_mode):
                raise ResumeModelError("configured model directory cannot contain links")
            if stat.S_ISDIR(info.st_mode):
                metadata.append(["directory", relative, *identity])
                child_directories.append(candidate)
            elif stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > MAX_MODEL_TREE_BYTES:
                    raise ResumeModelError("configured model directory is too large")
                metadata.append(["file", relative, *identity])
                files.append((relative, candidate, identity))
            else:
                raise ResumeModelError(
                    "configured model directory contains a non-regular entry"
                )
        pending.extend(reversed(child_directories))
    metadata.sort(key=lambda item: (str(item[1]), str(item[0])))
    files.sort(key=lambda item: item[0])
    return metadata, files


def _metadata_fingerprint(metadata: Sequence[Sequence[Any]]) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "manifest_version": MODEL_TREE_MANIFEST_VERSION,
                "metadata": metadata,
            }
        ).encode("utf-8")
    ).hexdigest()


def _model_artifact_identity(path: Path) -> tuple[str, str]:
    """Hash model bytes once and return a cheap mutation fingerprint."""

    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ResumeModelError("configured model artifact is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ResumeModelError("configured model artifact cannot be a link")
    if stat.S_ISREG(info.st_mode):
        digest, identity = _hash_regular_model_file(path)
        return digest, _metadata_fingerprint([["file", "", *identity]])
    if not stat.S_ISDIR(info.st_mode):
        raise ResumeModelError("configured model artifact type is unsupported")

    before, files = _bounded_model_tree_snapshot(path)
    manifest = []
    for relative, candidate, expected_identity in files:
        digest, observed_identity = _hash_regular_model_file(candidate)
        if observed_identity != expected_identity:
            raise ResumeModelError(
                "configured model artifact changed during verification"
            )
        manifest.append(
            {"path": relative, "size": observed_identity[3], "sha256": digest}
        )
    after, after_files = _bounded_model_tree_snapshot(path)
    if before != after or [item[0] for item in files] != [item[0] for item in after_files]:
        raise ResumeModelError("configured model artifact changed during verification")
    digest = hashlib.sha256(
        _canonical(
            {
                "manifest_version": MODEL_TREE_MANIFEST_VERSION,
                "files": manifest,
            }
        ).encode("utf-8")
    ).hexdigest()
    return digest, _metadata_fingerprint(after)


def model_artifact_sha256(path: Path) -> str:
    """Compute the resume model's file or canonical bounded-tree SHA-256."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ContractError("model artifact path must be absolute")
    return _model_artifact_identity(candidate)[0]


def _model_artifact_fingerprint(path: Path) -> str:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ResumeModelError("configured model artifact is unavailable") from exc
    if stat.S_ISREG(info.st_mode):
        return _metadata_fingerprint([["file", "", *_stat_identity(info)]])
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        metadata, _files = _bounded_model_tree_snapshot(path)
        return _metadata_fingerprint(metadata)
    raise ResumeModelError("configured model artifact type is unsupported")


def _command_option(command: Sequence[str], name: str) -> Optional[str]:
    values = []
    for index, item in enumerate(command):
        if item == name:
            if index + 1 >= len(command) or command[index + 1].startswith("--"):
                raise ContractError(f"resume model command {name} is invalid")
            values.append(command[index + 1])
        elif item.startswith(f"{name}="):
            values.append(item[len(name) + 1 :])
    if len(values) > 1 or any(not value for value in values):
        raise ContractError(f"resume model command {name} must appear exactly once")
    return values[0] if values else None


def _model_command_spec(
    command: Sequence[str], *, required: bool
) -> tuple[Optional[str], Optional[Path]]:
    backend = _command_option(command, "--backend")
    raw_path = _command_option(command, "--model-path")
    if backend is None and raw_path is None and not required:
        return None, None
    if backend is None or raw_path is None:
        raise ContractError(
            "resume model command requires one --backend and one --model-path"
        )
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise ContractError("resume model --model-path must be absolute")
    return backend, candidate


def _replace_command_option(
    command: Sequence[str], name: str, value: str
) -> tuple[str, ...]:
    result = list(command)
    for index, item in enumerate(result):
        if item == name:
            result[index + 1] = value
            return tuple(result)
        if item.startswith(f"{name}="):
            result[index] = f"{name}={value}"
            return tuple(result)
    raise ContractError(f"resume model command {name} is missing")


def _path_is_allowlisted(path: Path, allowed: Sequence[Path]) -> bool:
    for root in allowed:
        if path == root:
            return True
        if root.is_dir():
            try:
                path.relative_to(root)
                return True
            except ValueError:
                pass
    return False


_MODEL_IN_PROCESS_LOCK = threading.Lock()


def _open_model_lock(path: Path) -> int:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ResumeModelError("local resume model admission lock is unavailable") from exc
    except OSError as exc:
        raise ResumeModelError("local resume model admission lock is unavailable") from exc
    info = os.fstat(descriptor)
    current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != current_uid
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise ResumeModelError("local resume model admission lock is unsafe")
    return descriptor


def _model_lock_is_available(path: Optional[Path]) -> bool:
    if path is None or not path.is_absolute() or not path.parent.is_dir():
        return False
    if not path.exists():
        return os.access(path.parent, os.W_OK)
    try:
        info = os.lstat(path)
    except OSError:
        return False
    current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == current_uid
        and not info.st_mode & 0o077
        and info.st_nlink == 1
        and os.access(path, os.R_OK | os.W_OK)
    )


@contextmanager
def _model_admission(
    lock_path: Optional[Path], timeout_seconds: float
) -> Iterator[None]:
    deadline = time.monotonic() + timeout_seconds
    if not _MODEL_IN_PROCESS_LOCK.acquire(timeout=timeout_seconds):
        raise ResumeModelError("local resume model admission timed out")
    descriptor: Optional[int] = None
    try:
        if lock_path is not None:
            descriptor = _open_model_lock(lock_path)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ResumeModelError(
                            "local resume model admission timed out"
                        )
                    time.sleep(min(0.05, remaining))
        yield
    finally:
        try:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    os.close(descriptor)
        finally:
            _MODEL_IN_PROCESS_LOCK.release()


@dataclass(frozen=True)
class LocalResumeModelConfig:
    producer_version: str
    command: tuple[str, ...]
    allowed_read_paths: tuple[Path, ...] = ()
    timeout_seconds: float = 120.0
    version: int = LOCAL_MODEL_CONFIG_VERSION
    model_sha256: Optional[str] = None
    admission_lock_path: Optional[Path] = None

    def build(self) -> "LocalJsonResumeModel":
        return LocalJsonResumeModel(self)


def load_resume_model_config(
    path: Path,
) -> Union[LocalResumeModelConfig, "RunpodResumeModelConfig"]:
    """Load an owner-only local-v1 or Runpod-v2 model configuration."""

    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise ContractError("resume model config must be an owner-only regular file") from exc
    try:
        info = os.fstat(descriptor)
        current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != current_uid
        ):
            raise ContractError("resume model config must be owner-only (mode 0600)")
        chunks = []
        remaining = MAX_MODEL_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not encoded or len(encoded) > MAX_MODEL_CONFIG_BYTES:
        raise ContractError("resume model config is empty or too large")
    try:
        raw = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("resume model config must be valid UTF-8 JSON") from exc
    if isinstance(raw, Mapping) and "provider" in raw:
        provider = raw.get("provider")
        if provider != "runpod_serverless_vllm":
            raise ContractError("resume model provider is unsupported")
        from .runpod_model import RunpodResumeModelConfig

        return RunpodResumeModelConfig.from_mapping(raw)
    expected = {
        "version", "producer_version", "command", "allowed_read_paths",
        "timeout_seconds", "model_sha256",
    }
    if isinstance(raw, Mapping) and "model_sha256" not in raw:
        raise ContractError("resume model config requires model_sha256")
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ContractError("resume model config fields do not match version 1")
    if raw["version"] != LOCAL_MODEL_CONFIG_VERSION:
        raise ContractError("resume model config version must be 1")
    producer_version = raw["producer_version"]
    try:
        validate_identifier(producer_version, "producer_version")
    except (TypeError, ValueError) as exc:
        raise ContractError(str(exc)) from exc
    command = raw["command"]
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 64
        or any(
            not isinstance(item, str) or not item or len(item) > 4096
            for item in command
        )
    ):
        raise ContractError("resume model command must be a bounded argv array")
    # A file-loaded config is the production trust boundary. It must name the
    # exact local backend/model artifact whose bytes the operator attested.
    _model_command_spec(command, required=True)
    model_sha256 = raw["model_sha256"]
    if not isinstance(model_sha256, str) or not _SHA256.fullmatch(model_sha256):
        raise ContractError("model_sha256 must be a lowercase SHA-256 digest")
    raw_paths = raw["allowed_read_paths"]
    if not isinstance(raw_paths, list) or len(raw_paths) > 32:
        raise ContractError("allowed_read_paths must be a bounded array")
    paths = []
    for item in raw_paths:
        if not isinstance(item, str) or not item or len(item) > 4096:
            raise ContractError("allowed_read_paths entries must be paths")
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            raise ContractError("allowed_read_paths entries must be absolute")
        resolved = candidate.resolve()
        if _model_read_path_is_too_broad(resolved):
            raise ContractError("allowed_read_paths entry is too broad")
        paths.append(resolved)
    timeout = raw["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ContractError("timeout_seconds must be a number")
    timeout_value = float(timeout)
    if not math.isfinite(timeout_value) or not 1 <= timeout_value <= 300:
        raise ContractError("timeout_seconds must be between 1 and 300")
    resolved_config = target.resolve()
    lock_path = resolved_config.with_name(f".{resolved_config.name}.admission.lock")
    return LocalResumeModelConfig(
        producer_version=str(producer_version),
        command=tuple(command),
        allowed_read_paths=tuple(paths),
        timeout_seconds=timeout_value,
        version=LOCAL_MODEL_CONFIG_VERSION,
        model_sha256=model_sha256,
        admission_lock_path=lock_path,
    )


def resume_model_status(
    config: Union[LocalResumeModelConfig, "RunpodResumeModelConfig", None],
) -> Mapping[str, Any]:
    """Return a redacted, side-effect-free model readiness report."""

    if config is None:
        return {
            "status": "blocked_setup",
            "reason": "model_config_missing",
            "config_version": None,
            "producer_version": None,
            "model_sha256": None,
            "backend": None,
            "command_available": False,
            "isolation_available": False,
            "missing_read_paths": 0,
        }
    if not isinstance(config, LocalResumeModelConfig):
        from .runpod_model import runpod_resume_model_status

        return runpod_resume_model_status(config)
    command_name = config.command[0] if config.command else ""
    executable = (
        command_name
        if command_name and Path(command_name).is_absolute()
        else shutil.which(command_name)
    )
    command_available = bool(
        executable and Path(executable).is_file() and os.access(executable, os.X_OK)
    )
    isolation_available = (
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()
    )
    missing_paths = sum(not Path(item).exists() for item in config.allowed_read_paths)
    broad_paths = sum(
        _model_read_path_is_too_broad(Path(item))
        for item in config.allowed_read_paths
    )
    try:
        backend, model_path = _model_command_spec(
            config.command, required=config.model_sha256 is not None
        )
        command_contract_valid = config.model_sha256 is not None
    except ContractError:
        backend, model_path = None, None
        command_contract_valid = False
    digest_valid = bool(
        isinstance(config.model_sha256, str)
        and _SHA256.fullmatch(config.model_sha256)
    )
    backend_supported = backend == "llama-cpp-python"
    model_available = bool(model_path and model_path.is_file())
    model_allowlisted = bool(
        model_path
        and _path_is_allowlisted(
            model_path.resolve(strict=False), config.allowed_read_paths
        )
    )
    lock_available = _model_lock_is_available(config.admission_lock_path)
    ready = (
        config.version == LOCAL_MODEL_CONFIG_VERSION
        and digest_valid
        and command_contract_valid
        and backend_supported
        and model_available
        and model_allowlisted
        and lock_available
        and command_available
        and isolation_available
        and missing_paths == 0
        and broad_paths == 0
    )
    reasons = []
    if config.version != LOCAL_MODEL_CONFIG_VERSION:
        reasons.append("config_version_unsupported")
    if not digest_valid:
        reasons.append("model_identity_missing")
    if not command_contract_valid:
        reasons.append("model_command_contract_invalid")
    if backend == "mlx-lm":
        reasons.append("model_backend_unvalidated")
    elif not backend_supported:
        reasons.append("model_backend_unsupported")
    if not model_available:
        reasons.append("model_artifact_unavailable")
    if not model_allowlisted:
        reasons.append("model_artifact_not_allowlisted")
    if not lock_available:
        reasons.append("model_admission_lock_unavailable")
    if not command_available:
        reasons.append("model_command_unavailable")
    if not isolation_available:
        reasons.append("model_isolation_unavailable")
    if missing_paths:
        reasons.append("allowed_read_path_missing")
    if broad_paths:
        reasons.append("allowed_read_path_too_broad")
    return {
        "status": "ready" if ready else "blocked_setup",
        "reason": None if ready else reasons[0],
        "config_version": config.version,
        "producer_version": config.producer_version,
        "model_sha256": config.model_sha256 if digest_valid else None,
        "backend": backend,
        "command_available": command_available,
        "isolation_available": isolation_available,
        "missing_read_paths": missing_paths,
    }


def _terminate_model_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate the isolated process group without leaking an implementation error."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _run_bounded_model_process(
    command: Sequence[str],
    *,
    input: str,
    timeout: float,
    cwd: str,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run a model while actively capping combined stdout/stderr in memory.

    ``subprocess.run(..., PIPE)`` buffers without a limit before returning.  Reader
    threads drain both streams concurrently, retain at most the configured budget,
    and kill the isolated process group as soon as one additional byte is observed.
    """

    try:
        process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
        )
    except OSError:
        raise
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    capture_lock = threading.Lock()
    overflow = threading.Event()
    reader_errors: list[OSError] = []

    def read_stream(stream: Any, name: str) -> None:
        try:
            while True:
                chunk = stream.read1(64 * 1024)
                if not chunk:
                    return
                with capture_lock:
                    used = len(buffers["stdout"]) + len(buffers["stderr"])
                    remaining = MAX_MODEL_OUTPUT_BYTES - used
                    if len(chunk) > remaining:
                        if remaining > 0:
                            buffers[name].extend(chunk[:remaining])
                        overflow.set()
                    else:
                        buffers[name].extend(chunk)
                if overflow.is_set():
                    _terminate_model_process(process)
                    return
        except (OSError, ValueError) as exc:
            if not overflow.is_set():
                reader_errors.append(OSError(str(exc)))
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def write_input() -> None:
        try:
            process.stdin.write(input.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    readers = (
        threading.Thread(
            target=read_stream,
            args=(process.stdout, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=(process.stderr, "stderr"),
            daemon=True,
        ),
    )
    writer = threading.Thread(target=write_input, daemon=True)
    for thread in (*readers, writer):
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate_model_process(process)
        process.wait()
        for thread in (*readers, writer):
            thread.join(timeout=1)
        raise
    for thread in (*readers, writer):
        thread.join(timeout=max(0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in (*readers, writer)):
        # A forked descendant may retain a pipe after the command leader exits.
        # Keep the original deadline authoritative for the whole process group.
        _terminate_model_process(process)
        for thread in (*readers, writer):
            thread.join(timeout=1)
        raise subprocess.TimeoutExpired(tuple(command), timeout)
    if overflow.is_set():
        raise ResumeModelError("local resume model output is too large")
    if reader_errors:
        raise OSError("local resume model output capture failed")
    try:
        stdout = bytes(buffers["stdout"]).decode("utf-8")
        stderr = bytes(buffers["stderr"]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResumeModelError("local resume model output is not UTF-8") from exc
    return subprocess.CompletedProcess(tuple(command), returncode, stdout, stderr)


class LocalJsonResumeModel:
    """Invoke a fixed JSON-in/JSON-out local model with task-specific validation."""

    def __init__(
        self,
        config: LocalResumeModelConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        isolation_builder: Callable[[Sequence[str], Path, Sequence[Path]], Sequence[str]] = macos_model_sandbox,
    ) -> None:
        if config.version != LOCAL_MODEL_CONFIG_VERSION:
            raise ContractError("resume model config version must be 1")
        _identifier(config.producer_version, "producer_version")
        if not 1 <= len(config.command) <= 64 or any(not isinstance(item, str) or not item or len(item) > 4096 for item in config.command):
            raise ContractError("resume model command must be a bounded argv array")
        if not 1 <= float(config.timeout_seconds) <= 300:
            raise ContractError("resume model timeout must be between 1 and 300 seconds")
        paths = tuple(Path(item).expanduser().resolve() for item in config.allowed_read_paths)
        if len(paths) > 32 or any(not Path(item).is_absolute() for item in config.allowed_read_paths):
            raise ContractError("resume model read paths must be bounded absolute paths")
        if any(_model_read_path_is_too_broad(path) for path in paths):
            raise ContractError("resume model read path is too broad")
        model_sha256 = config.model_sha256
        if model_sha256 is not None and (
            not isinstance(model_sha256, str)
            or not _SHA256.fullmatch(model_sha256)
        ):
            raise ContractError("model_sha256 must be a lowercase SHA-256 digest")
        backend, configured_model_path = _model_command_spec(
            config.command, required=model_sha256 is not None
        )
        if backend == "mlx-lm":
            raise ContractError(
                "mlx-lm production isolation is not validated; "
                "use llama-cpp-python"
            )
        if backend is not None and backend != "llama-cpp-python":
            raise ContractError("resume model production backend is unsupported")
        lock_path = config.admission_lock_path
        if lock_path is not None:
            lock_path = Path(lock_path).expanduser()
            if not lock_path.is_absolute() or not lock_path.parent.is_dir():
                raise ContractError("resume model admission lock path is invalid")
            lock_path = lock_path.resolve(strict=False)
        command = tuple(config.command)
        model_path: Optional[Path] = None
        fingerprint: Optional[str] = None
        if model_sha256 is not None:
            assert configured_model_path is not None
            try:
                if stat.S_ISLNK(os.lstat(configured_model_path).st_mode):
                    raise ContractError(
                        "resume model --model-path cannot be a link"
                    )
                model_path = configured_model_path.resolve(strict=True)
            except OSError as exc:
                raise ResumeModelError("configured model artifact is unavailable") from exc
            if not _path_is_allowlisted(model_path, paths):
                raise ContractError("configured model artifact is not allowlisted")
            command = _replace_command_option(
                command, "--model-path", str(model_path)
            )
            actual_digest, fingerprint = _model_artifact_identity(model_path)
            if not hmac.compare_digest(actual_digest, model_sha256):
                raise ResumeModelError(
                    "configured model artifact does not match model_sha256"
                )
        self.config = LocalResumeModelConfig(
            producer_version=config.producer_version,
            command=command,
            allowed_read_paths=paths,
            timeout_seconds=float(config.timeout_seconds),
            version=config.version,
            model_sha256=model_sha256,
            admission_lock_path=lock_path,
        )
        self.runner = runner
        self.isolation_builder = isolation_builder
        self._model_path = model_path
        self._model_fingerprint = fingerprint

    def _assert_model_identity_unchanged(self) -> None:
        if self._model_path is None or self._model_fingerprint is None:
            return
        if not hmac.compare_digest(
            _model_artifact_fingerprint(self._model_path),
            self._model_fingerprint,
        ):
            raise ResumeModelError(
                "configured model artifact changed after startup verification"
            )

    def _invoke(
        self,
        task: str,
        payload: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        *,
        generation_seed: Optional[int] = None,
    ) -> Mapping[str, Any]:
        request = build_resume_model_request(
            task,
            payload,
            output_schema,
            generation_seed=generation_seed,
        )
        serialized = _canonical(request)
        with _model_admission(
            self.config.admission_lock_path, self.config.timeout_seconds
        ):
            self._assert_model_identity_unchanged()
            with tempfile.TemporaryDirectory(prefix="job-resume-model-") as name:
                directory = Path(name).resolve()
                isolated = tuple(
                    self.isolation_builder(
                        self.config.command,
                        directory,
                        self.config.allowed_read_paths,
                    )
                )
                if not isolated:
                    raise ResumeModelError(
                        "resume model isolation returned an empty command"
                    )
                try:
                    environment = {
                        "HOME": str(directory),
                        "TMPDIR": str(directory),
                        "PATH": "/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                    }
                    if self.runner is None:
                        completed = _run_bounded_model_process(
                            isolated,
                            input=serialized,
                            timeout=self.config.timeout_seconds,
                            cwd=str(directory),
                            env=environment,
                        )
                    else:
                        # Injectable runners are a test seam; production always uses
                        # the actively bounded process path above.
                        completed = self.runner(
                            isolated,
                            input=serialized,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=self.config.timeout_seconds,
                            check=False,
                            shell=False,
                            cwd=str(directory),
                            env=environment,
                        )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ResumeModelError(
                        "local resume model failed safely"
                    ) from exc
            self._assert_model_identity_unchanged()
        if completed.returncode != 0:
            raise ResumeModelError(f"local resume model exited with status {completed.returncode}")
        output = completed.stdout
        error_output = completed.stderr
        if (
            not isinstance(output, str)
            or not isinstance(error_output, str)
            or not output.strip()
            or len(output.encode("utf-8"))
            + len(error_output.encode("utf-8"))
            > MAX_MODEL_OUTPUT_BYTES
        ):
            raise ResumeModelError("local resume model output is empty or too large")
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ResumeModelError("local resume model output is not JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ResumeModelError("local resume model output must be an object")
        return parsed

    def extract_requirements(self, job_description: str) -> Mapping[str, Any]:
        if not isinstance(job_description, str) or not job_description.strip() or len(job_description) > 300_000:
            raise ContractError("job description must be bounded text")
        output = self._invoke(
            "extract_job_requirements",
            {"job_description": job_description},
            {"requirements": [{"requirement_id": "id", "text": "exact source span", "kind": sorted(REQUIREMENT_KINDS), "logic": sorted(REQUIREMENT_LOGIC), "priority": [0, 1, 2], "source_start": "integer", "source_end": "integer"}]},
        )
        return validate_requirement_output(output, job_description)

    def normalize_standard_resume(
        self, tex_source: str, parsed_pdf_text: str
    ) -> Mapping[str, Any]:
        """Turn an arbitrary imported TeX/PDF resume into guarded structured data."""

        if (
            not isinstance(tex_source, str)
            or not tex_source.strip()
            or len(tex_source.encode("utf-8")) > MAX_MODEL_INPUT_BYTES
            or "\x00" in tex_source
        ):
            raise ContractError("TeX source must be bounded text")
        if (
            not isinstance(parsed_pdf_text, str)
            or not parsed_pdf_text.strip()
            or len(parsed_pdf_text) > 300_000
            or "\x00" in parsed_pdf_text
        ):
            raise ContractError("parsed PDF text must be bounded text")
        output = self._invoke(
            "normalize_standard_resume",
            {"tex_source": tex_source, "parsed_pdf_text": parsed_pdf_text},
            {
                "content": _resume_content_output_contract(),
                "fixed_fields": [{
                    "path": "JSON pointer", "text": "exact parsed-PDF span",
                    "source_start": "integer", "source_end": "integer",
                }],
                "claims": [{
                    "claim_id": "id", "path": "JSON pointer",
                    "text": "exact parsed-PDF span", "source_start": "integer",
                    "source_end": "integer",
                }],
            },
        )
        return validate_standard_normalization_output(output, parsed_pdf_text)

    def adjudicate_evidence(
        self, requirement: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        safe_candidates = self._safe_evidence_candidates(candidates)
        output = self._invoke(
            "adjudicate_ambiguous_resume_evidence",
            {"requirement": dict(requirement), "candidate_claims": safe_candidates},
            {"status": sorted(EVIDENCE_STATUSES), "confidence": "0..1", "reason": "text", "evidence": [{"claim_id": "id", "quote": "exact claim substring"}]},
        )
        return validate_evidence_output(output, safe_candidates)

    @staticmethod
    def _safe_evidence_candidates(
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        safe_candidates = []
        for value in candidates:
            if not isinstance(value, Mapping) or set(value) - {"claim_id", "text", "section"}:
                raise ContractError("evidence candidates expose only claim_id, text, and section")
            safe_candidates.append({key: value.get(key, "") for key in ("claim_id", "text", "section")})
        return safe_candidates

    def adjudicate_evidence_batch(
        self,
        requirements: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Adjudicate a bounded resume in one isolated model invocation."""

        if (
            not isinstance(requirements, Sequence)
            or isinstance(requirements, (str, bytes, bytearray))
            or len(requirements) > 64
        ):
            raise ContractError("evidence requirements must be a bounded array")
        safe_requirements: list[Mapping[str, Any]] = []
        allowed = {
            "requirement_id",
            "text",
            "kind",
            "priority",
            "minimum_years",
            "term_groups",
        }
        for value in requirements:
            if not isinstance(value, Mapping) or set(value) - allowed:
                raise ContractError(
                    "evidence requirements contain unsupported fields"
                )
            safe_requirements.append({key: value.get(key) for key in allowed})
        safe_candidates = self._safe_evidence_candidates(candidates)
        output = self._invoke(
            "adjudicate_resume_evidence_batch",
            {
                "requirements": safe_requirements,
                "candidate_claims": safe_candidates,
            },
            {
                "adjudications": [
                    {
                        "requirement_id": "exact requested id",
                        "status": sorted(EVIDENCE_STATUSES),
                        "confidence": "0..1",
                        "reason": "text",
                        "evidence": [
                            {
                                "claim_id": "id",
                                "quote": "exact claim substring",
                            }
                        ],
                    }
                ]
            },
        )
        return validate_evidence_batch_output(
            output, safe_requirements, safe_candidates
        )

    def generate_variant(
        self,
        variant_kind: str,
        job: Mapping[str, Any],
        standard_candidates: Sequence[Mapping[str, Any]],
        source_claims: Sequence[Mapping[str, Any]],
        guarded_terms: Sequence[str] = (),
        *,
        generation_seed: Optional[int] = None,
        optimization_input: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        if variant_kind not in VARIANT_KINDS:
            raise ContractError("unknown resume variant kind")
        base = select_base_standard(standard_candidates) if variant_kind in {"grounded_rewrite", "standard_exaggerated"} else None
        constraints = {
            "grounded_rewrite": (
                "Preserve the rank-1 standard's fixed identity/history and every "
                "editable slot. Each output slot must cite its same-path source claim "
                "first. Only summary and skill slots may additionally cite user-attested "
                "/skills/ claims; never project them into experience or projects. Preserve "
                "every non-article source concept and its relationship order. Reword only "
                "through articles, punctuation, case, or explicitly declared reviewed "
                "equivalents. Preserve each metric with its neighboring concepts, plus "
                "negation, qualification, ownership, "
                "comparison, math, currency, range, scope, and seniority exactly. Never add "
                "an unsupported tool, fact, metric, scope, or seniority."
            ),
            "standard_exaggerated": (
                "Create a research-only synthetic exaggeration derived from the rank-1 "
                "standard. Preserve its identity, employers, roles, education, projects, "
                "dates, locations, skill categories, and editable slot counts. Return one "
                "provenance record for every editable slot. An unchanged slot must be "
                "source/source_rewrite and cite exactly its same-path source claim. A "
                "changed slot must be synthetic with empty source_claim_ids and "
                "added_equivalent_terms."
            ),
            "market_ideal": "Create an independent research-only synthetic ideal candidate for the job; do not cite or derive from a standard.",
            "keyword_adversarial": "Create an independent research-only keyword-adversarial candidate; label adversarial claims and do not cite a standard.",
        }[variant_kind]
        optimization = _safe_optimization_input(optimization_input, variant_kind)
        payload = {
            "variant_kind": variant_kind,
            "constraint": constraints,
            "job": dict(job),
            "base_standard": dict(base) if base else None,
            "source_claims": [dict(value) for value in source_claims] if base else [],
            "guarded_terms": list(guarded_terms),
            "optimization": optimization,
        }
        output = self._invoke(
            "generate_structured_resume_variant",
            payload,
            {
                "variant_kind": variant_kind,
                "base_standard_id": (
                    "exact base_standard.standard_id"
                    if base
                    else "JSON null"
                ),
                "synthetic": variant_kind in SYNTHETIC_VARIANTS,
                "research_only": variant_kind in SYNTHETIC_VARIANTS,
                "content": _resume_content_output_contract(),
                "claims": [
                    {
                        "claim_id": "unique id",
                        "path": "exact JSON pointer to one editable content string",
                        "text": "exact string stored at path",
                        "origin": {
                            "grounded_rewrite": "source or source_rewrite",
                            "standard_exaggerated": (
                                "source, source_rewrite, or synthetic"
                            ),
                            "market_ideal": "synthetic",
                            "keyword_adversarial": "keyword_adversarial",
                        }[variant_kind],
                        "source_claim_ids": {
                            "grounded_rewrite": [
                                "same-path source claim id first",
                                "optional user-attested /skills/ support ids",
                            ],
                            "standard_exaggerated": (
                                "exactly one same-path source id for an unchanged "
                                "slot; otherwise an empty array"
                            ),
                            "market_ideal": [],
                            "keyword_adversarial": [],
                        }[variant_kind],
                        "added_equivalent_terms": (
                            ["only an allowed equivalent actually added to text"]
                            if variant_kind == "grounded_rewrite"
                            else []
                        ),
                    }
                ],
            },
            generation_seed=generation_seed,
        )
        return validate_variant_output(output, variant_kind, base, source_claims if base else (), guarded_terms)
