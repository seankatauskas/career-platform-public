"""Pure application-event reduction and projection verification helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .contracts import (
    ApplicationEventType,
    ApplicationPhase,
    TerminalOutcome,
    payload_sha256,
)


PHASE_RANK = {
    ApplicationPhase.PREPARING: 10,
    ApplicationPhase.AWAITING_CONFIRMATION: 20,
    ApplicationPhase.ACTIVE: 30,
    ApplicationPhase.INTERVIEWING: 40,
    ApplicationPhase.OFFER: 50,
    ApplicationPhase.TERMINAL: 60,
}

EVENT_PHASE = {
    ApplicationEventType.APPLICATION_STARTED: ApplicationPhase.PREPARING,
    ApplicationEventType.SUBMISSION_OBSERVED: ApplicationPhase.AWAITING_CONFIRMATION,
    ApplicationEventType.SUBMISSION_CONFIRMED: ApplicationPhase.ACTIVE,
    ApplicationEventType.RECRUITER_CONTACT: ApplicationPhase.ACTIVE,
    ApplicationEventType.ASSESSMENT_REQUESTED: ApplicationPhase.ACTIVE,
    ApplicationEventType.ASSESSMENT_COMPLETED: ApplicationPhase.ACTIVE,
    ApplicationEventType.INTERVIEW_REQUESTED: ApplicationPhase.INTERVIEWING,
    ApplicationEventType.INTERVIEW_SCHEDULED: ApplicationPhase.INTERVIEWING,
    ApplicationEventType.INTERVIEW_COMPLETED: ApplicationPhase.INTERVIEWING,
    ApplicationEventType.OFFER_RECEIVED: ApplicationPhase.OFFER,
    ApplicationEventType.OFFER_ACCEPTED: ApplicationPhase.TERMINAL,
    ApplicationEventType.REJECTION_RECEIVED: ApplicationPhase.TERMINAL,
    ApplicationEventType.WITHDRAWN: ApplicationPhase.TERMINAL,
}

EVENT_OUTCOME = {
    ApplicationEventType.OFFER_ACCEPTED: TerminalOutcome.ACCEPTED,
    ApplicationEventType.REJECTION_RECEIVED: TerminalOutcome.REJECTED,
    ApplicationEventType.WITHDRAWN: TerminalOutcome.WITHDRAWN,
}


@dataclass(frozen=True)
class ApplicationState:
    current_phase: ApplicationPhase
    terminal_outcome: Optional[TerminalOutcome]
    started_at: str
    submitted_at: Optional[str]
    confirmed_at: Optional[str]
    last_activity_at: str
    last_event_seq: int
    updated_at: str

    def projection_values(self) -> Mapping[str, Any]:
        return {
            "current_phase": self.current_phase.value,
            "terminal_outcome": (
                self.terminal_outcome.value if self.terminal_outcome else None
            ),
            "started_at": self.started_at,
            "submitted_at": self.submitted_at,
            "confirmed_at": self.confirmed_at,
            "last_activity_at": self.last_activity_at,
            "last_event_seq": self.last_event_seq,
            "updated_at": self.updated_at,
        }

    @property
    def projection_sha256(self) -> str:
        return payload_sha256(self.projection_values())


def _earliest(current: Optional[str], candidate: str) -> str:
    return candidate if current is None or candidate < current else current


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    return row[key]


def reduce_events(events: Iterable[Mapping[str, Any]]) -> ApplicationState:
    """Fold events in insertion order without allowing ordinary phase regression."""

    phase = ApplicationPhase.PREPARING
    outcome: Optional[TerminalOutcome] = None
    started_at = ""
    submitted_at: Optional[str] = None
    confirmed_at: Optional[str] = None
    last_activity_at = ""
    last_event_seq = 0
    updated_at = ""

    for row in events:
        event_type = ApplicationEventType(str(_row_value(row, "event_type")))
        occurred_at = str(_row_value(row, "occurred_at"))
        recorded_at = str(_row_value(row, "recorded_at"))
        sequence = int(_row_value(row, "event_seq"))
        payload_raw = _row_value(row, "payload_json")
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw

        last_event_seq = sequence
        updated_at = recorded_at
        if not last_activity_at or occurred_at > last_activity_at:
            last_activity_at = occurred_at

        if event_type is ApplicationEventType.APPLICATION_STARTED:
            started_at = _earliest(started_at or None, occurred_at)
        elif event_type is ApplicationEventType.SUBMISSION_OBSERVED:
            submitted_at = _earliest(submitted_at, occurred_at)
        elif event_type is ApplicationEventType.SUBMISSION_CONFIRMED:
            submitted_at = _earliest(submitted_at, occurred_at)
            confirmed_at = _earliest(confirmed_at, occurred_at)

        if event_type is ApplicationEventType.MANUAL_CORRECTION:
            phase = ApplicationPhase(str(payload["target_phase"]))
            raw_outcome = payload.get("target_outcome")
            outcome = TerminalOutcome(str(raw_outcome)) if raw_outcome else None
            continue

        if phase is ApplicationPhase.TERMINAL:
            continue
        target = EVENT_PHASE[event_type]
        if PHASE_RANK[target] > PHASE_RANK[phase]:
            phase = target
        terminal = EVENT_OUTCOME.get(event_type)
        if terminal is not None:
            phase = ApplicationPhase.TERMINAL
            outcome = terminal

    if not started_at or not last_event_seq:
        raise ValueError("an application projection requires application_started")
    return ApplicationState(
        current_phase=phase,
        terminal_outcome=outcome,
        started_at=started_at,
        submitted_at=submitted_at,
        confirmed_at=confirmed_at,
        last_activity_at=last_activity_at,
        last_event_seq=last_event_seq,
        updated_at=updated_at,
    )


def projection_mismatches(
    stored: Mapping[str, Any], expected: ApplicationState
) -> Mapping[str, Mapping[str, Any]]:
    values = dict(expected.projection_values())
    values["projection_sha256"] = expected.projection_sha256
    mismatches = {}
    for field, wanted in values.items():
        found = stored[field]
        if found != wanted:
            mismatches[field] = {"stored": found, "expected": wanted}
    return mismatches

