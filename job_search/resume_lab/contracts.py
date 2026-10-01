"""Frozen contracts for the isolated resume laboratory.

The resume laboratory deliberately has no application-ledger, browser-extension, or
external-delivery capability.  Real application artifacts and synthetic research
artifacts share pure rendering/scoring code, but their identities and allowed claims
remain disjoint at every persisted boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional, Tuple


RESUME_LAB_SCHEMA_VERSION = 1
SCORER_REVISION = "ats-proxy-v2-evidence-association"
NORMALIZATION_VALIDATOR_REVISION = "normalization-source-coverage-v1"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ResumeLabError(ValueError):
    """Input is outside the resume-lab contract."""

    http_status = 400


class ResumeBoundaryError(ResumeLabError):
    """A real/synthetic or managed-artifact boundary was crossed."""

    http_status = 409


class ResumeConflictError(ResumeLabError):
    """An immutable identity was reused with different content."""

    http_status = 409


class ResumeNotFoundError(ResumeLabError):
    """A requested resume-lab object does not exist."""

    http_status = 404


class ResumePurpose(str, Enum):
    REAL_APPLICATION = "real_application"
    SYNTHETIC_RESEARCH = "synthetic_research"


class VariantKind(str, Enum):
    STANDARD = "standard"
    GROUNDED_REWRITE = "grounded_rewrite"
    STANDARD_EXAGGERATED = "standard_exaggerated"
    MARKET_IDEAL = "market_ideal"
    KEYWORD_ADVERSARIAL = "keyword_adversarial"


VARIANT_PURPOSE = {
    VariantKind.STANDARD: ResumePurpose.REAL_APPLICATION,
    VariantKind.GROUNDED_REWRITE: ResumePurpose.REAL_APPLICATION,
    VariantKind.STANDARD_EXAGGERATED: ResumePurpose.SYNTHETIC_RESEARCH,
    VariantKind.MARKET_IDEAL: ResumePurpose.SYNTHETIC_RESEARCH,
    VariantKind.KEYWORD_ADVERSARIAL: ResumePurpose.SYNTHETIC_RESEARCH,
}


class ClaimOrigin(str, Enum):
    FIXED_FACT = "fixed_fact"
    USER_ATTESTED = "user_attested"
    GENERATED_WORDING = "generated_wording"
    SYNTHETIC_GENERATED = "synthetic_generated"


class RequirementPriority(str, Enum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    RESPONSIBILITY = "responsibility"


class RequirementKind(str, Enum):
    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATION = "certification"
    ELIGIBILITY = "eligibility"
    OTHER = "other"


class EvidenceStatus(str, Enum):
    MET = "met"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    NOT_EVIDENCED = "not_evidenced"
    CONTRADICTED = "contradicted"


class EligibilityStatus(str, Enum):
    CLEAR = "clear"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


class ResumeRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunItemStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


RUN_VARIANTS = (
    VariantKind.GROUNDED_REWRITE,
    VariantKind.STANDARD_EXAGGERATED,
    VariantKind.MARKET_IDEAL,
    VariantKind.KEYWORD_ADVERSARIAL,
)


def validate_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ResumeLabError(f"{field} is invalid")
    return value


def validate_sha256(value: str, field: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return value
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ResumeLabError(f"{field} must be a lowercase SHA-256")
    return value


def clean_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ResumeLabError(f"{field} must be text")
    result = unicodedata.normalize("NFC", value).strip()
    if not result or len(result) > maximum or "\x00" in result:
        raise ResumeLabError(f"{field} is invalid")
    return result


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _normalize_json(asdict(value))
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ResumeLabError("JSON numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ResumeLabError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_sha256(value: Any) -> str:
    return sha256_text(canonical_json(value))


@dataclass(frozen=True)
class ResumeClaim:
    claim_id: str
    text: str
    origin: ClaimOrigin
    source_fact_ids: Tuple[str, ...] = ()

    def validate(self, purpose: ResumePurpose, *, grounded: bool = False) -> None:
        validate_identifier(self.claim_id, "claim_id")
        clean_text(self.text, "claim text", 8_000)
        for fact_id in self.source_fact_ids:
            validate_identifier(fact_id, "source_fact_id")
        if len(set(self.source_fact_ids)) != len(self.source_fact_ids):
            raise ResumeLabError("source_fact_ids must be unique")
        if purpose is ResumePurpose.REAL_APPLICATION:
            if self.origin is ClaimOrigin.SYNTHETIC_GENERATED:
                raise ResumeBoundaryError(
                    "synthetic claims cannot enter a real artifact"
                )
            if (
                grounded
                and self.origin is ClaimOrigin.GENERATED_WORDING
                and not self.source_fact_ids
            ):
                raise ResumeBoundaryError(
                    "generated wording in a grounded rewrite requires a source fact"
                )
        elif self.origin is not ClaimOrigin.SYNTHETIC_GENERATED:
            raise ResumeBoundaryError(
                "synthetic research artifacts may contain only synthetic claims"
            )


@dataclass(frozen=True)
class StandardVersionInput:
    tex_source: str
    plain_text: str
    claims: Tuple[ResumeClaim, ...]
    authored_by: str = "user"
    normalized_content: Optional[Mapping[str, Any]] = None
    import_metadata: Optional[Mapping[str, Any]] = None

    def validate(self) -> None:
        clean_text(self.tex_source, "tex_source", 500_000)
        clean_text(self.plain_text, "plain_text", 200_000)
        validate_identifier(self.authored_by, "authored_by")
        if not self.claims:
            raise ResumeLabError("a standard version requires at least one claim")
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise ResumeLabError("claim IDs must be unique")
        for claim in self.claims:
            claim.validate(ResumePurpose.REAL_APPLICATION)
        if self.normalized_content is not None:
            if not isinstance(self.normalized_content, Mapping):
                raise ResumeLabError(
                    "normalized_content must be an object when present"
                )
            canonical_json(self.normalized_content)
        if self.import_metadata is not None:
            if not isinstance(self.import_metadata, Mapping):
                raise ResumeLabError("import_metadata must be an object when present")
            required = {
                "managed_relative_path",
                "pdf_sha256",
                "parsed_text",
                "parse_fidelity",
            }
            if not required.issubset(self.import_metadata):
                raise ResumeLabError(
                    "import_metadata requires path, PDF hash, parsed text, and fidelity"
                )
            managed_path = _managed_relative_path(
                self.import_metadata["managed_relative_path"]
            )
            if not managed_path.startswith("real/"):
                raise ResumeBoundaryError(
                    "standard import artifact must stay under the real artifact root"
                )
            validate_sha256(self.import_metadata["pdf_sha256"], "import pdf_sha256")
            clean_text(
                self.import_metadata["parsed_text"], "import parsed_text", 200_000
            )
            fidelity = self.import_metadata["parse_fidelity"]
            if (
                isinstance(fidelity, bool)
                or not isinstance(fidelity, (int, float))
                or not math.isfinite(float(fidelity))
                or not 0 <= float(fidelity) <= 1
            ):
                raise ResumeLabError(
                    "import parse_fidelity must be between zero and one"
                )
            canonical_json(self.import_metadata)

    @property
    def fingerprint(self) -> str:
        self.validate()
        return content_sha256(
            {
                "tex_source": self.tex_source,
                "plain_text": self.plain_text,
                "claims": self.claims,
                "normalized_content": self.normalized_content,
                "import_metadata": self.import_metadata,
            }
        )


@dataclass(frozen=True)
class JobSnapshot:
    ats: str
    job_id: str
    title: str
    description: str
    employer: str = ""

    def validate(self) -> None:
        validate_identifier(self.ats, "ats")
        validate_identifier(self.job_id, "job_id")
        clean_text(self.title, "job title", 1_000)
        clean_text(self.description, "job description", 500_000)
        if not isinstance(self.employer, str) or len(self.employer) > 1_000:
            raise ResumeLabError("employer is invalid")

    @property
    def fingerprint(self) -> str:
        self.validate()
        return content_sha256(
            {
                "ats": self.ats.lower(),
                "job_id": self.job_id,
                "title": self.title,
                "description": self.description,
                "employer": self.employer,
            }
        )


def _managed_relative_path(value: str) -> str:
    cleaned = clean_text(value, "managed_relative_path", 2_048)
    if "\\" in cleaned or any(ord(character) < 32 for character in cleaned):
        raise ResumeBoundaryError("managed artifact path is invalid")
    path = PurePosixPath(cleaned)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ResumeBoundaryError("managed artifact path must stay beneath its root")
    return cleaned


@dataclass(frozen=True)
class ArtifactInput:
    variant_kind: VariantKind
    purpose: ResumePurpose
    job: JobSnapshot
    tex_source: str
    intended_text: str
    claims: Tuple[ResumeClaim, ...]
    managed_relative_path: str
    pdf_sha256: str
    parsed_text: str
    parse_fidelity: float
    parse_safe: bool
    generator_revision: str
    base_version_id: Optional[str] = None
    study_id: Optional[str] = None
    pair_id: Optional[str] = None
    treatment: str = ""
    generation_seed: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source_mode: str = "standard"
    composition_id: Optional[str] = None

    def validate(self) -> None:
        expected = VARIANT_PURPOSE[self.variant_kind]
        if self.purpose is not expected:
            raise ResumeBoundaryError(
                f"{self.variant_kind.value} must use purpose={expected.value}"
            )
        self.job.validate()
        clean_text(self.tex_source, "tex_source", 500_000)
        clean_text(self.intended_text, "intended_text", 200_000)
        clean_text(self.parsed_text, "parsed_text", 200_000)
        _managed_relative_path(self.managed_relative_path)
        validate_sha256(self.pdf_sha256, "pdf_sha256")
        clean_text(self.generator_revision, "generator_revision", 256)
        if not isinstance(self.parse_safe, bool):
            raise ResumeLabError("parse_safe must be a boolean")
        if isinstance(self.parse_fidelity, bool) or not isinstance(
            self.parse_fidelity, (int, float)
        ):
            raise ResumeLabError("parse_fidelity must be a number")
        if (
            not math.isfinite(float(self.parse_fidelity))
            or not 0 <= float(self.parse_fidelity) <= 1
        ):
            raise ResumeLabError("parse_fidelity must be between zero and one")
        if not self.claims or len({item.claim_id for item in self.claims}) != len(
            self.claims
        ):
            raise ResumeLabError(
                "artifact claims must be nonempty and uniquely identified"
            )
        grounded = self.variant_kind is VariantKind.GROUNDED_REWRITE
        for claim in self.claims:
            claim.validate(self.purpose, grounded=grounded)
        needs_base = self.variant_kind in {
            VariantKind.STANDARD,
            VariantKind.GROUNDED_REWRITE,
            VariantKind.STANDARD_EXAGGERATED,
        }
        if self.source_mode not in {"standard", "career_profile"}:
            raise ResumeBoundaryError("artifact source mode is invalid")
        if self.source_mode == "career_profile":
            if self.base_version_id or not self.composition_id or self.variant_kind is VariantKind.STANDARD:
                raise ResumeBoundaryError("career artifact requires only its composition source")
            validate_identifier(self.composition_id, "composition_id")
        elif self.composition_id:
            raise ResumeBoundaryError("standard artifact cannot reference a career composition")
        if needs_base and not self.base_version_id and self.source_mode == "standard":
            raise ResumeLabError(f"{self.variant_kind.value} requires a base version")
        if self.base_version_id:
            validate_identifier(self.base_version_id, "base_version_id")
        if self.purpose is ResumePurpose.SYNTHETIC_RESEARCH:
            if not self.study_id or not self.pair_id:
                raise ResumeBoundaryError(
                    "synthetic artifacts require study_id and pair_id provenance"
                )
            validate_identifier(self.study_id, "study_id")
            validate_identifier(self.pair_id, "pair_id")
            if isinstance(self.generation_seed, bool) or not isinstance(
                self.generation_seed, int
            ):
                raise ResumeLabError(
                    "synthetic artifacts require an integer generation_seed"
                )
        elif any(
            value not in {None, ""}
            for value in (
                self.study_id,
                self.pair_id,
                self.treatment,
                self.generation_seed,
            )
        ):
            raise ResumeBoundaryError(
                "research provenance cannot enter a real artifact"
            )
        canonical_json(self.metadata or {})

    @property
    def content_fingerprint(self) -> str:
        self.validate()
        return content_sha256(
            {
                "variant_kind": self.variant_kind,
                "purpose": self.purpose,
                "job_fingerprint": self.job.fingerprint,
                "tex_source": self.tex_source,
                "intended_text": self.intended_text,
                "claims": self.claims,
                "pdf_sha256": self.pdf_sha256,
                "parsed_text": self.parsed_text,
                "parse_fidelity": self.parse_fidelity,
                "parse_safe": self.parse_safe,
                "generator_revision": self.generator_revision,
                **({"source_mode": self.source_mode, "composition_id": self.composition_id} if self.source_mode == "career_profile" else {}),
                "base_version_id": self.base_version_id,
                "study_id": self.study_id,
                "pair_id": self.pair_id,
                "treatment": self.treatment,
                "generation_seed": self.generation_seed,
                "metadata": self.metadata or {},
            }
        )


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    priority: RequirementPriority
    kind: RequirementKind
    source_text: str
    term_groups: Tuple[Tuple[str, ...], ...]
    minimum_years: Optional[float] = None
    source_start: Optional[int] = None
    source_end: Optional[int] = None
    extraction_origin: str = "deterministic"


@dataclass(frozen=True)
class RequirementGraph:
    graph_revision: str
    job_fingerprint: str
    requirements: Tuple[Requirement, ...]
    fingerprint: str


@dataclass(frozen=True)
class EvidenceSpan:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class CriterionResult:
    requirement_id: str
    priority: RequirementPriority
    kind: RequirementKind
    source_text: str
    status: EvidenceStatus
    matched_terms: Tuple[str, ...]
    missing_term_groups: Tuple[Tuple[str, ...], ...]
    evidence: Tuple[EvidenceSpan, ...]
    weight: float
    value: float
    confidence: float = 1.0
    match_method: str = "literal"


@dataclass(frozen=True)
class AtsProxyEvaluation:
    scorer_revision: str
    artifact_text_sha256: str
    requirement_graph_fingerprint: str
    requirement_evidence: float
    search_visibility: float
    screening_readiness: float
    parsed_fit: float
    eligibility_status: EligibilityStatus
    criteria: Tuple[CriterionResult, ...]
    cache_key: str


@dataclass(frozen=True)
class ComparisonSlot:
    slot_id: str
    variant_kind: VariantKind
    purpose: ResumePurpose
    standard_id: Optional[str]
    base_version_id: Optional[str]
    label: str


@dataclass(frozen=True)
class ComparisonManifest:
    job_fingerprint: str
    selected_standard_id: str
    selected_version_id: str
    selection_reason: str
    slots: Tuple[ComparisonSlot, ...]


def claims_from_json(value: str) -> Tuple[ResumeClaim, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise ResumeLabError("stored claims are invalid")
    return tuple(
        ResumeClaim(
            claim_id=str(item["claim_id"]),
            text=str(item["text"]),
            origin=ClaimOrigin(str(item["origin"])),
            source_fact_ids=tuple(
                str(value) for value in item.get("source_fact_ids", ())
            ),
        )
        for item in raw
    )


__all__ = [
    "ArtifactInput",
    "AtsProxyEvaluation",
    "ClaimOrigin",
    "ComparisonManifest",
    "ComparisonSlot",
    "CriterionResult",
    "EligibilityStatus",
    "EvidenceSpan",
    "EvidenceStatus",
    "JobSnapshot",
    "Requirement",
    "RequirementGraph",
    "RequirementKind",
    "RequirementPriority",
    "RESUME_LAB_SCHEMA_VERSION",
    "ResumeBoundaryError",
    "ResumeClaim",
    "ResumeConflictError",
    "ResumeLabError",
    "ResumeNotFoundError",
    "ResumePurpose",
    "ResumeRunStatus",
    "RunItemStatus",
    "RUN_VARIANTS",
    "SCORER_REVISION",
    "NORMALIZATION_VALIDATOR_REVISION",
    "StandardVersionInput",
    "VARIANT_PURPOSE",
    "VariantKind",
    "canonical_json",
    "claims_from_json",
    "content_sha256",
    "sha256_text",
    "validate_identifier",
    "validate_sha256",
]
