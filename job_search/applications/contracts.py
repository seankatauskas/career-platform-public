"""Immutable operation inputs; exact text is preserved without normalization."""
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ApplicationTarget:
    application_id: Optional[str] = None
    job_source: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class SaveJob(ApplicationTarget):
    job_snapshot: Optional[Mapping[str, Any]] = None
    recommendation: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class AddNote(ApplicationTarget):
    text: str = ""


@dataclass(frozen=True)
class CreateTask(ApplicationTarget):
    kind: str = "other"
    description: str = ""
    responsible_party: str = "applicant"
    due_at: Optional[str] = None
    completion_rule: str = "human_decision"
    origin_ref: Optional[str] = None
    related_id: Optional[str] = None
    evidence: Tuple[Mapping[str, Any], ...] = ()
    reminders_enabled: bool = False


@dataclass(frozen=True)
class TaskDecision:
    task_id: str
    expected_version: int
    reason: str
    result_id: Optional[str] = None


@dataclass(frozen=True)
class SnoozeTask:
    task_id: str
    expected_version: int
    until: str


@dataclass(frozen=True)
class BrowserObservation(ApplicationTarget):
    device_id: str = ""
    observation_id: str = ""
    attempt_ref: str = ""
    occurred_at: Optional[str] = None
    activity: str = "submission_attempt"
    answers: Mapping[str, str] = field(default_factory=dict)
    documents: Tuple[Mapping[str, Any], ...] = ()
    source: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Submission(ApplicationTarget):
    submission_id: Optional[str] = None
    expected_version: Optional[int] = None
    status: str = "attempted"
    occurred_at: Optional[str] = None
    answers: Mapping[str, str] = field(default_factory=dict)
    documents: Tuple[Mapping[str, Any], ...] = ()
    evidence: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class AttachSubmissionAnswers:
    submission_id: str
    expected_version: int
    observation_id: str


@dataclass(frozen=True)
class Interview(ApplicationTarget):
    interview_id: Optional[str] = None
    expected_version: Optional[int] = None
    status: str = "requested"
    title: str = "Interview"
    round_kind: str = "interview"
    participants: Tuple[str, ...] = ()
    location: Optional[str] = None
    join_url: Optional[str] = None
    organizer: Optional[str] = None
    note: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    timezone: Optional[str] = None
    employer_confirmed: Optional[bool] = None
    create_task: bool = False
    reminders_enabled: bool = False
    evidence: Tuple[Mapping[str, Any], ...] = ()
    expected_related_versions: Optional[Mapping[str, int]] = None


@dataclass(frozen=True)
class CancelInterview:
    interview_id: str
    expected_version: int
    reason: str
    cancellation_kind: str = "user_not_attending"
    expected_related_versions: Optional[Mapping[str, int]] = None


@dataclass(frozen=True)
class Assessment(ApplicationTarget):
    assessment_id: Optional[str] = None
    expected_version: Optional[int] = None
    status: str = "requested"
    description: str = ""
    channel: str = "unknown"
    due_at: Optional[str] = None
    deadline_text: Optional[str] = None
    create_task: bool = False
    evidence: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Offer(ApplicationTarget):
    offer_id: Optional[str] = None
    expected_version: Optional[int] = None
    status: str = "offered"
    terms: Mapping[str, Any] = field(default_factory=dict)
    create_task: bool = False
    evidence: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class OfferDecision:
    offer_id: str
    expected_version: int
    status: str
    reason: str


@dataclass(frozen=True)
class ApplicationDecision:
    application_id: str
    expected_version: int
    outcome: str = "stopped_pursuing"
    reason: str = ""
    expected_records: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressFact(ApplicationTarget):
    kind: str = "recruiter_contact"
    evidence: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class CorrectProgress:
    application_id: str
    corrections: Tuple[Mapping[str, Any], ...]
    reason: str


@dataclass(frozen=True)
class CombineJobs:
    source_application_id: str
    target_application_id: str
    source_version: int
    target_version: int
    disposition: str
    outcome: Optional[str] = None
    reason: str = ""
    expected_records: Optional[Mapping[str, int]] = None


@dataclass(frozen=True)
class CreateReminder(ApplicationTarget):
    at: str = ""
    related_id: Optional[str] = None
    description: str = ""


@dataclass(frozen=True)
class ReminderDecision:
    reminder_id: str
    expected_version: int
    reason: str


@dataclass(frozen=True)
class ScheduleOperation:
    application_id: str
    operation: str
    input: Mapping[str, Any]
    due_at: str
    reason: str


@dataclass(frozen=True)
class ScheduleDecision:
    schedule_id: str
    expected_version: int
    reason: str


@dataclass(frozen=True)
class ReenableReminder:
    reminder_id: str
    expected_version: int
    due_at: str
    reason: str


@dataclass(frozen=True)
class NotificationDelivery:
    delivery_id: str
    reminder_id: str
    expected_version: int
    receipt_id: str
    outcome: str
    observed_at: str
