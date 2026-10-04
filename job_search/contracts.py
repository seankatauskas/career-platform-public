"""Frozen v1 contracts shared by the ledger, workers, UI, and future agents."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence


API_VERSION = "v1"
EVENT_SCHEMA_VERSION = 1
ACTION_APPROVAL_TTL_SECONDS = 15 * 60
ACTION_EXECUTION_MAX_ATTEMPTS = 5
MAIL_EXCERPT_LIMIT = 2048

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ContractError(ValueError):
    """A caller supplied data outside the frozen service contract."""


class ConflictError(ContractError):
    """An idempotency key or lifecycle decision conflicts with existing state."""


class ApplicationPhase(str, Enum):
    PREPARING = "preparing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ACTIVE = "active"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    TERMINAL = "terminal"


class TerminalOutcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class ApplicationEventType(str, Enum):
    APPLICATION_STARTED = "application_started"
    SUBMISSION_OBSERVED = "submission_observed"
    SUBMISSION_CONFIRMED = "submission_confirmed"
    RECRUITER_CONTACT = "recruiter_contact"
    ASSESSMENT_REQUESTED = "assessment_requested"
    ASSESSMENT_COMPLETED = "assessment_completed"
    INTERVIEW_REQUESTED = "interview_requested"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    INTERVIEW_COMPLETED = "interview_completed"
    OFFER_RECEIVED = "offer_received"
    OFFER_ACCEPTED = "offer_accepted"
    REJECTION_RECEIVED = "rejection_received"
    WITHDRAWN = "withdrawn"
    MANUAL_CORRECTION = "manual_correction"


TERMINAL_EVENT_TYPES = frozenset(
    {
        ApplicationEventType.OFFER_ACCEPTED,
        ApplicationEventType.REJECTION_RECEIVED,
        ApplicationEventType.WITHDRAWN,
    }
)

MODEL_AUTO_APPLY_EVENT_TYPES = frozenset(
    {
        ApplicationEventType.SUBMISSION_CONFIRMED,
        ApplicationEventType.RECRUITER_CONTACT,
        ApplicationEventType.ASSESSMENT_REQUESTED,
        ApplicationEventType.ASSESSMENT_COMPLETED,
        ApplicationEventType.INTERVIEW_REQUESTED,
        ApplicationEventType.INTERVIEW_SCHEDULED,
        ApplicationEventType.INTERVIEW_COMPLETED,
        ApplicationEventType.OFFER_RECEIVED,
    }
)


class ProducerKind(str, Enum):
    RULE = "rule"
    MODEL = "model"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    AUTO_APPLIED = "auto_applied"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"


class ActionKind(str, Enum):
    OUTLOOK_REPLY_DRAFT = "outlook_reply_draft"
    CALENDAR_TENTATIVE_HOLD = "calendar_tentative_hold"


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    SUPERSEDED = "superseded"


class TemporalProposalKind(str, Enum):
    INTERVIEW = "interview"
    DEADLINE = "deadline"


class TemporalProposalStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONFLICT = "conflict"


class WorkStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MutationContext:
    idempotency_key: str
    actor_kind: str
    source_kind: str
    source_ref: str = ""

    def validate(self) -> None:
        validate_identifier(self.idempotency_key, "idempotency_key")
        validate_identifier(self.actor_kind, "actor_kind")
        validate_identifier(self.source_kind, "source_kind")


@dataclass(frozen=True)
class JobSnapshot:
    ats: str
    job_id: str
    family_id: str
    title: str
    employer: str
    company_slug: str
    job_url: str

    def validate(self) -> None:
        validate_identifier(self.ats, "ats")
        validate_identifier(self.job_id, "job_id")
        if not self.title.strip():
            raise ContractError("title must not be empty")
        if not self.employer.strip():
            raise ContractError("employer must not be empty")
        if self.ats == "external" and not self.job_url:
            return  # Reviewed external roles may have no known posting URL.
        if not self.job_url.startswith(("https://", "http://")):
            raise ContractError("job_url must be an HTTP(S) URL")


@dataclass(frozen=True)
class RecommendationProvenance:
    session_id: str = ""
    impression_id: Optional[int] = None
    model_run_id: str = ""
    policy_id: str = ""
    rank: Optional[int] = None
    semantic_score: Optional[float] = None
    ranking_score: Optional[float] = None

    def validate(self) -> None:
        if self.impression_id is not None and self.impression_id <= 0:
            raise ContractError("impression_id must be positive")
        if self.rank is not None and self.rank <= 0:
            raise ContractError("rank must be positive")
        for name, value in (
            ("semantic_score", self.semantic_score),
            ("ranking_score", self.ranking_score),
        ):
            if value is not None and not math.isfinite(value):
                raise ContractError(f"{name} must be finite")


@dataclass(frozen=True)
class EventInput:
    application_id: str
    event_type: ApplicationEventType
    occurred_at: str
    payload: Mapping[str, Any]
    dedupe_key: str
    context: MutationContext


@dataclass(frozen=True)
class EventProposalInput:
    evidence_id: str
    proposed_application_id: Optional[str]
    event_type: ApplicationEventType
    producer_kind: ProducerKind
    producer_version: str
    confidence: float
    candidate_application_ids: Sequence[str]
    evidence_quote: str
    span_start: Optional[int]
    span_end: Optional[int]
    payload: Mapping[str, Any]
    dedupe_key: str


@dataclass(frozen=True)
class ActionProposalInput:
    kind: ActionKind
    application_id: Optional[str]
    account_id: str
    payload: Mapping[str, Any]
    expires_at: str


@dataclass(frozen=True)
class TemporalProposalInput:
    archive_id: str
    attachment_record_id: Optional[str]
    application_id: str
    kind: TemporalProposalKind
    starts_at: Optional[str]
    ends_at: Optional[str]
    due_at: Optional[str]
    time_zone: str
    confidence: float
    evidence_quote: str
    span_start: int
    span_end: int
    source_sha256: str
    producer_version: str
    dedupe_key: str


@dataclass(frozen=True)
class MailChange:
    immutable_id: str
    removed: bool
    conversation_id: Optional[str] = None
    internet_message_id: Optional[str] = None
    sender_address: Optional[str] = None
    subject: Optional[str] = None
    received_at: Optional[str] = None
    modified_at: Optional[str] = None
    body_preview: Optional[str] = None
    web_link: Optional[str] = None


@dataclass(frozen=True)
class MailDeltaPage:
    changes: Sequence[MailChange]
    next_link: Optional[str]
    delta_link: Optional[str]


@dataclass(frozen=True)
class CalendarBlock:
    remote_id: str
    starts_at: str
    ends_at: str
    show_as: str
    is_all_day: bool
    is_cancelled: bool
    change_key: str = ""


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    next_attempt_at: Optional[str]
    outcome_unknown: bool
    reason: str


class JobSearchService(Protocol):
    def start_application(
        self,
        snapshot: JobSnapshot,
        provenance: RecommendationProvenance,
        context: MutationContext,
    ) -> Mapping[str, Any]: ...

    def record_event(self, event: EventInput) -> Mapping[str, Any]: ...

    def create_event_proposal(
        self, proposal: EventProposalInput, context: MutationContext
    ) -> Mapping[str, Any]: ...

    def create_action_proposal(
        self, proposal: ActionProposalInput, context: MutationContext
    ) -> Mapping[str, Any]: ...

    def put_mail_archive(
        self, record: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]: ...

    def get_encrypted_mail_archive(self, archive_id: str) -> Mapping[str, Any]: ...

    def put_mail_archive_attachment(
        self, record: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]: ...

    def get_encrypted_mail_archive_attachment(
        self, attachment_record_id: str
    ) -> Mapping[str, Any]: ...

    def create_temporal_proposal(
        self, proposal: TemporalProposalInput, context: MutationContext
    ) -> Mapping[str, Any]: ...

    def decide_temporal_proposal(
        self,
        temporal_proposal_id: str,
        decision: str,
        reason: str,
        context: MutationContext,
    ) -> Mapping[str, Any]: ...

    def list_temporal_proposals(
        self, statuses: Optional[Sequence[str]] = None, *, limit: int = 200
    ) -> Sequence[Mapping[str, Any]]: ...

    def list_interview_schedules(self, *, limit: int = 200) -> Sequence[Mapping[str, Any]]: ...

    def list_due_local_reminders(
        self, now: str, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]: ...

    def complete_local_reminder(
        self,
        reminder_id: str,
        resolution: str,
        context: MutationContext,
    ) -> Mapping[str, Any]: ...

    def get_application_timeline(self, application_id: str) -> Mapping[str, Any]: ...

    def list_attention_items(self) -> Sequence[Mapping[str, Any]]: ...

    def verify_projections(self) -> Sequence[Mapping[str, Any]]: ...


class MailClassifier(Protocol):
    def classify(
        self,
        sanitized_text: str,
        candidate_applications: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


class OutlookTransport(Protocol):
    """Narrow Outlook capability surface; intentionally has no send method."""

    def read_delta_page(self, opaque_url: str) -> MailDeltaPage: ...

    def read_message_body(self, immutable_message_id: str) -> Mapping[str, Any]: ...

    def read_calendar_view(self, starts_at: str, ends_at: str) -> Sequence[CalendarBlock]: ...

    def create_reply_draft(self, immutable_message_id: str) -> Mapping[str, Any]: ...

    def update_reply_draft(self, draft_id: str, body: str) -> Mapping[str, Any]: ...

    def create_private_tentative_hold(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


def validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{field} is invalid")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractError("timestamp must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError("invalid UTC timestamp") from exc
    return parsed.astimezone(timezone.utc)


def _normalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _normalize(asdict(value))
    if isinstance(value, Mapping):
        return {unicodedata.normalize("NFC", str(key)): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("JSON numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ContractError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_event_payload(event_type: ApplicationEventType, payload: Mapping[str, Any]) -> None:
    canonical_json(payload)
    if event_type is not ApplicationEventType.MANUAL_CORRECTION:
        return
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise ContractError("manual correction requires a reason")
    try:
        phase = ApplicationPhase(str(payload.get("target_phase")))
    except ValueError as exc:
        raise ContractError("manual correction requires a valid target_phase") from exc
    outcome = payload.get("target_outcome")
    if phase is ApplicationPhase.TERMINAL:
        try:
            TerminalOutcome(str(outcome))
        except ValueError as exc:
            raise ContractError("terminal correction requires a valid target_outcome") from exc
    elif outcome not in {None, ""}:
        raise ContractError("nonterminal correction cannot set target_outcome")


def bounded_candidate_ids(values: Iterable[str], limit: int = 20) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(values))
    if len(result) > limit:
        raise ContractError(f"candidate application list exceeds {limit}")
    for value in result:
        validate_identifier(value, "candidate_application_id")
    return result
