"""Locked local evaluation and per-event auto-apply safety gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from job_search.contracts import ApplicationEventType, EventProposalInput, payload_sha256

from .context import CandidateApplication, bounded_candidates
from .proposals import ProposalValidationError, validate_model_output
from .sanitizer import SanitizedMail


MIN_HIGH_CONFIDENCE_EXAMPLES = 50
MIN_OBSERVED_PRECISION = 0.99
DEFAULT_HIGH_CONFIDENCE_THRESHOLD = 0.90


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    mail: SanitizedMail
    candidates: Sequence[CandidateApplication | Mapping[str, Any]]
    expected_event_type: ApplicationEventType | None
    expected_application_id: str | None


@dataclass(frozen=True)
class EvaluationObservation:
    expected_event_type: ApplicationEventType | None
    expected_application_id: str | None
    predicted_event_type: ApplicationEventType | None
    predicted_application_id: str | None
    confidence: float
    valid: bool = True


@dataclass(frozen=True)
class EventClassMetrics:
    event_type: ApplicationEventType
    high_confidence_predictions: int
    correct_predictions: int
    wrong_application_matches: int
    observed_precision: float

    @property
    def gate_passed(self) -> bool:
        return (
            self.high_confidence_predictions >= MIN_HIGH_CONFIDENCE_EXAMPLES
            and self.observed_precision >= MIN_OBSERVED_PRECISION
            and self.wrong_application_matches == 0
        )


@dataclass(frozen=True)
class EvaluationReport:
    producer_version: str
    dataset_fingerprint: str
    high_confidence_threshold: float
    total_cases: int
    invalid_outputs: int
    by_event: Mapping[ApplicationEventType, EventClassMetrics]


def evaluate_observations(
    observations: Sequence[EvaluationObservation],
    *,
    producer_version: str,
    dataset_fingerprint: str,
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
) -> EvaluationReport:
    if not 0.0 <= high_confidence_threshold <= 1.0:
        raise ValueError("high_confidence_threshold must be between 0 and 1")
    metrics: dict[ApplicationEventType, EventClassMetrics] = {}
    for event_type in ApplicationEventType:
        predictions = [
            item for item in observations
            if item.valid
            and item.predicted_event_type is event_type
            and item.confidence >= high_confidence_threshold
        ]
        if not predictions:
            continue
        correct = sum(
            item.expected_event_type is event_type
            and item.expected_application_id == item.predicted_application_id
            for item in predictions
        )
        wrong_applications = sum(
            item.predicted_application_id is not None
            and item.predicted_application_id != item.expected_application_id
            for item in predictions
        )
        metrics[event_type] = EventClassMetrics(
            event_type=event_type,
            high_confidence_predictions=len(predictions),
            correct_predictions=correct,
            wrong_application_matches=wrong_applications,
            observed_precision=correct / len(predictions),
        )
    return EvaluationReport(
        producer_version=producer_version,
        dataset_fingerprint=dataset_fingerprint,
        high_confidence_threshold=high_confidence_threshold,
        total_cases=len(observations),
        invalid_outputs=sum(not item.valid for item in observations),
        by_event=metrics,
    )


def run_benchmark(
    classifier: Any,
    cases: Sequence[BenchmarkCase],
    *,
    producer_version: str,
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
) -> EvaluationReport:
    case_ids = [case.case_id for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("benchmark case IDs must be unique")
    fingerprint_rows = []
    observations: list[EvaluationObservation] = []
    for case in cases:
        candidates = bounded_candidates(case.candidates)
        fingerprint_rows.append({
            "case_id": case.case_id,
            "mail_sha256": case.mail.content_sha256,
            "candidate_ids": [item.application_id for item in candidates],
            "expected_event_type": (
                case.expected_event_type.value if case.expected_event_type else None
            ),
            "expected_application_id": case.expected_application_id,
        })
        try:
            raw = classifier.classify(
                case.mail.text, [item.model_context() for item in candidates],
            )
            proposal = validate_model_output(
                raw,
                evidence_id=case.case_id,
                mail=case.mail,
                candidates=candidates,
                producer_version=producer_version,
            )
            observations.append(_observation(case, proposal))
        except (ProposalValidationError, RuntimeError, ValueError):
            observations.append(EvaluationObservation(
                case.expected_event_type,
                case.expected_application_id,
                None,
                None,
                0.0,
                False,
            ))
    return evaluate_observations(
        observations,
        producer_version=producer_version,
        dataset_fingerprint=payload_sha256(fingerprint_rows),
        high_confidence_threshold=high_confidence_threshold,
    )


def _observation(case: BenchmarkCase, proposal: EventProposalInput) -> EvaluationObservation:
    return EvaluationObservation(
        expected_event_type=case.expected_event_type,
        expected_application_id=case.expected_application_id,
        predicted_event_type=proposal.event_type,
        predicted_application_id=proposal.proposed_application_id,
        confidence=proposal.confidence,
        valid=True,
    )
