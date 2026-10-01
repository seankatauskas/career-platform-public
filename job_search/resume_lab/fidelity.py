"""Deterministic PDF round-trip fidelity metrics and release gate."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from job_search.contracts import ContractError

from .career_ops import normalize_ats_tokens
from .pdf import PdfExtraction


@dataclass(frozen=True)
class FidelityView:
    token_recall: float
    token_precision: float
    sequence_ratio: float
    duplicate_ratio: float


@dataclass(frozen=True)
class PdfFidelityReport:
    status: str
    logical: FidelityView
    layout: FidelityView
    missing_sections: tuple[str, ...]
    issues: tuple[str, ...]

    @property
    def safe_to_submit(self) -> bool:
        return self.status != "fail"


def _view(expected: tuple[str, ...], actual: tuple[str, ...]) -> FidelityView:
    if not expected or not actual:
        return FidelityView(0.0, 0.0, 0.0, float("inf") if actual else 0.0)
    expected_count = Counter(expected)
    actual_count = Counter(actual)
    overlap = sum(min(count, actual_count[token]) for token, count in expected_count.items())
    recall = overlap / len(expected)
    precision = overlap / len(actual)
    matched = sum(block.size for block in SequenceMatcher(None, expected, actual, autojunk=False).get_matching_blocks())
    sequence = matched / len(expected)
    duplicate = max(0.0, (len(actual) - overlap) / len(expected))
    return FidelityView(recall, precision, sequence, duplicate)


def evaluate_pdf_fidelity(
    intended_text: str,
    extraction: PdfExtraction,
    expected_sections: tuple[str, ...] = (),
) -> PdfFidelityReport:
    if not isinstance(intended_text, str) or not intended_text.strip():
        raise ContractError("intended resume text must not be empty")
    expected = normalize_ats_tokens(intended_text)
    logical_tokens = normalize_ats_tokens(extraction.logical_text)
    layout_tokens = normalize_ats_tokens(extraction.layout_text)
    logical = _view(expected, logical_tokens)
    layout = _view(expected, layout_tokens)
    visible = {token for token in logical_tokens} & {token for token in layout_tokens}
    missing = tuple(
        section for section in expected_sections
        if not set(normalize_ats_tokens(section)) <= visible
    )
    issues: list[str] = []
    worst_recall = min(logical.token_recall, layout.token_recall)
    worst_sequence = min(logical.sequence_ratio, layout.sequence_ratio)
    worst_duplicate = max(logical.duplicate_ratio, layout.duplicate_ratio)
    if missing:
        issues.append("critical section headings were lost")
    if worst_recall < 0.99:
        issues.append("less than 99% of intended tokens survived both parsers")
    if worst_sequence < 0.95:
        issues.append("PDF extraction order differs materially from the intended order")
    if worst_duplicate > 0.05:
        issues.append("PDF extraction contains material duplicate or unexpected text")
    if missing or worst_recall < 0.98 or worst_sequence < 0.90 or worst_duplicate > 0.10:
        status = "fail"
    elif issues:
        status = "warn"
    else:
        status = "pass"
    return PdfFidelityReport(status, logical, layout, missing, tuple(issues))
