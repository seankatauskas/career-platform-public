"""Rule-first mail analysis with a strictly validated local-model fallback."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from job_search.contracts import EventProposalInput

from .context import CandidateApplication, bounded_candidates
from .proposals import validate_model_output
from .rules import match_known_template
from .sanitizer import SanitizedMail


def analyze_mail(
    *,
    evidence_id: str,
    sender_address: str,
    mail: SanitizedMail,
    candidates: Sequence[CandidateApplication | Mapping[str, Any]],
    classifier: Any | None,
    model_version: str,
    received_at: str = "",
    sender_authenticated: bool = False,
    candidate_context_complete: bool = True,
) -> EventProposalInput | None:
    bounded = bounded_candidates(candidates)
    rule = match_known_template(
        evidence_id=evidence_id,
        sender_address=sender_address,
        mail=mail,
        candidates=bounded,
        received_at=received_at,
        sender_authenticated=sender_authenticated,
        candidate_context_complete=candidate_context_complete,
    )
    if rule is not None:
        return rule.proposal
    if classifier is None:
        return None
    raw = classifier.classify(mail.text, [item.model_context() for item in bounded])
    return validate_model_output(
        raw,
        evidence_id=evidence_id,
        mail=mail,
        candidates=bounded,
        producer_version=model_version,
    )
