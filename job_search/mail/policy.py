"""Deterministic review versus auto-apply decisions for validated proposals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from job_search.contracts import (
    MODEL_AUTO_APPLY_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    EventProposalInput,
    ProducerKind,
)

from .evaluation import EvaluationReport


class ProposalDisposition(str, Enum):
    AUTO_APPLY = "auto_apply"
    REVIEW = "review"


@dataclass(frozen=True)
class PolicyDecision:
    disposition: ProposalDisposition
    reason: str


def decide_proposal(
    proposal: EventProposalInput,
    *,
    evaluation_report: EvaluationReport | None = None,
) -> PolicyDecision:
    if proposal.proposed_application_id is None:
        return PolicyDecision(ProposalDisposition.REVIEW, "application identity is ambiguous")
    if proposal.event_type in TERMINAL_EVENT_TYPES:
        return PolicyDecision(ProposalDisposition.REVIEW, "terminal outcomes always require review")
    if proposal.event_type not in MODEL_AUTO_APPLY_EVENT_TYPES:
        return PolicyDecision(ProposalDisposition.REVIEW, "event type is not auto-eligible")
    if proposal.producer_kind is ProducerKind.RULE:
        if proposal.confidence == 1.0:
            return PolicyDecision(ProposalDisposition.AUTO_APPLY, "verified deterministic rule")
        return PolicyDecision(ProposalDisposition.REVIEW, "rule confidence is not exact")
    if proposal.producer_kind is not ProducerKind.MODEL:
        return PolicyDecision(ProposalDisposition.REVIEW, "unknown proposal producer")
    if evaluation_report is None:
        return PolicyDecision(ProposalDisposition.REVIEW, "model has no locked evaluation")
    if evaluation_report.producer_version != proposal.producer_version:
        return PolicyDecision(ProposalDisposition.REVIEW, "model version differs from evaluation")
    metrics = evaluation_report.by_event.get(proposal.event_type)
    if metrics is None or not metrics.gate_passed:
        return PolicyDecision(ProposalDisposition.REVIEW, "event class has not passed its safety gate")
    if proposal.confidence < evaluation_report.high_confidence_threshold:
        return PolicyDecision(ProposalDisposition.REVIEW, "proposal is below calibrated threshold")
    return PolicyDecision(ProposalDisposition.AUTO_APPLY, "model class passed locked safety gate")
