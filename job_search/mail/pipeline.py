"""Rule-first mail analysis with a strictly validated local-model fallback."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from job_search.contracts import EventProposalInput, ContractError, validate_identifier

from .context import CandidateApplication, bounded_candidates
from .proposals import build_proposal, validate_model_output, ProposalValidationError
from .model import ModelOutputError
from .classification_review import ClassificationRejected, assert_reviewable_outcome
from .identity import supported_candidates, supported_selection
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
    from ..inference.contracts import InferenceResponseRejected
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
    validate_identifier(evidence_id, 'evidence_id')
    validate_identifier(model_version, 'model_version')
    try:
        raw = classifier.classify(mail.text, [item.model_context() for item in bounded])
        try:
            proposal = validate_model_output(
                raw,
                evidence_id=evidence_id,
                mail=mail,
                candidates=bounded,
                producer_version=model_version,
            )
        except ContractError:
            raise ProposalValidationError('model classification failed validation') from None
    except (ProposalValidationError, ModelOutputError, InferenceResponseRejected):
        assert_reviewable_outcome()
        raise ClassificationRejected() from None
    # Validate the model's original IDs and quote first, then independently guard
    # identity. Unsupported matches become durable unassigned review items.
    return build_proposal(
        evidence_id=evidence_id, mail=mail,
        candidates=supported_candidates(bounded, mail.subject, mail.body),
        application_id=supported_selection(bounded, proposal.proposed_application_id, mail.subject, mail.body)
        if candidate_context_complete else None,
        event_type=proposal.event_type, producer_kind=proposal.producer_kind,
        producer_version=proposal.producer_version, confidence=proposal.confidence,
        evidence_quote=proposal.evidence_quote, span_start=proposal.span_start,
        span_end=proposal.span_end,
    )
