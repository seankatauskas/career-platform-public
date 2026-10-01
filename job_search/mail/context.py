"""Privacy-minimized, bounded candidate context for mail classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from job_search.contracts import ContractError, bounded_candidate_ids, validate_identifier


MAX_CANDIDATE_APPLICATIONS = 20


def _bounded_text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    value = " ".join(value.split())
    if required and not value:
        raise ContractError(f"{field} must not be empty")
    if len(value) > limit:
        raise ContractError(f"{field} exceeds {limit} characters")
    return value


@dataclass(frozen=True)
class CandidateApplication:
    application_id: str
    ats: str
    job_id: str
    employer: str
    title: str
    company_slug: str = ""
    phase: str = ""
    # Local evidence for deterministic matching; not sent to the classifier.
    match_context: str = ""
    submitted_at: str = ""
    submission_attempted_at: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateApplication":
        if not isinstance(value, Mapping):
            raise ContractError("candidate application must be an object")
        application_id = _bounded_text(
            value.get("application_id"), "application_id", 256, required=True,
        )
        validate_identifier(application_id, "application_id")
        return cls(
            application_id=application_id,
            ats=_bounded_text(value.get("ats"), "ats", 32),
            job_id=_bounded_text(value.get("job_id"), "job_id", 256),
            employer=_bounded_text(value.get("employer"), "employer", 300, required=True),
            title=_bounded_text(value.get("title"), "title", 500, required=True),
            company_slug=_bounded_text(value.get("company_slug"), "company_slug", 300),
            phase=_bounded_text(value.get("phase"), "phase", 64),
            match_context=_bounded_text(value.get("match_context"), "match_context", 500),
            submitted_at=_bounded_text(value.get("submitted_at"), "submitted_at", 64),
            submission_attempted_at=_bounded_text(value.get("submission_attempted_at"), "submission_attempted_at", 64),
        )

    def model_context(self) -> dict[str, str]:
        return {
            "application_id": self.application_id,
            "ats": self.ats,
            "job_id": self.job_id,
            "employer": self.employer,
            "title": self.title,
            "company_slug": self.company_slug,
            "phase": self.phase,
            **({"match_context": self.match_context} if self.match_context else {}),
        }


def bounded_candidates(
    values: Sequence[Mapping[str, Any] | CandidateApplication],
    limit: int = MAX_CANDIDATE_APPLICATIONS,
) -> tuple[CandidateApplication, ...]:
    if limit < 1 or limit > MAX_CANDIDATE_APPLICATIONS:
        raise ContractError(f"candidate limit must be between 1 and {MAX_CANDIDATE_APPLICATIONS}")
    candidates = tuple(
        value if isinstance(value, CandidateApplication) else CandidateApplication.from_mapping(value)
        for value in values
    )
    ids = bounded_candidate_ids((item.application_id for item in candidates), limit)
    if len(ids) != len(candidates):
        raise ContractError("candidate application IDs must be unique")
    return candidates
