#!/usr/bin/env python3
"""Offline checks for the frozen job-search v1 contracts."""

import math
import unicodedata

from job_search.contracts import (
    MODEL_AUTO_APPLY_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    ApplicationEventType,
    ApplicationPhase,
    ContractError,
    JobSnapshot,
    TerminalOutcome,
    canonical_json,
    parse_utc,
    payload_sha256,
    validate_event_payload,
)


def test_event_vocabulary_is_frozen():
    assert {item.value for item in ApplicationEventType} == {
        "application_started", "submission_observed", "submission_confirmed",
        "recruiter_contact", "assessment_requested", "assessment_completed",
        "interview_requested", "interview_scheduled", "interview_completed",
        "offer_received", "offer_accepted", "rejection_received", "withdrawn",
        "manual_correction",
    }
    assert not (TERMINAL_EVENT_TYPES & MODEL_AUTO_APPLY_EVENT_TYPES)


def test_canonical_json_normalizes_unicode_and_order():
    decomposed = "e\u0301"
    composed = unicodedata.normalize("NFC", decomposed)
    left = {"z": [decomposed], "a": 1}
    right = {"a": 1, "z": [composed]}
    assert canonical_json(left) == canonical_json(right)
    assert payload_sha256(left) == payload_sha256(right)


def test_canonical_json_rejects_non_finite_numbers():
    for value in (math.nan, math.inf, -math.inf):
        try:
            canonical_json({"value": value})
        except ContractError:
            pass
        else:
            raise AssertionError("non-finite number was accepted")


def test_timestamps_must_be_utc_z():
    assert parse_utc("2026-09-01T12:00:00Z").utcoffset().total_seconds() == 0
    try:
        parse_utc("2026-09-01T07:00:00-05:00")
    except ContractError:
        pass
    else:
        raise AssertionError("non-Z timestamp was accepted")


def test_manual_correction_contract():
    validate_event_payload(ApplicationEventType.MANUAL_CORRECTION, {
        "reason": "Corrected from recruiter evidence",
        "target_phase": ApplicationPhase.TERMINAL.value,
        "target_outcome": TerminalOutcome.REJECTED.value,
    })
    try:
        validate_event_payload(ApplicationEventType.MANUAL_CORRECTION, {
            "reason": "", "target_phase": "active",
        })
    except ContractError:
        pass
    else:
        raise AssertionError("correction without reason was accepted")


def test_job_snapshot_requires_stable_key_and_url():
    JobSnapshot("greenhouse", "123", "family", "Engineer", "Example", "example", "https://example.test/job").validate()
    try:
        JobSnapshot("green house", "123", "", "Engineer", "Example", "example", "javascript:bad").validate()
    except ContractError:
        pass
    else:
        raise AssertionError("invalid snapshot was accepted")


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search contract tests)")


if __name__ == "__main__":
    main()
