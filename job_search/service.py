"""Public deterministic service facade for the job-search ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .contracts import (
    ActionProposalInput,
    ContractError,
    EventInput,
    EventProposalInput,
    JobSnapshot,
    MutationContext,
    RecommendationProvenance,
    TemporalProposalInput,
    payload_sha256,
)
from .notifications import NotificationIntent, NotificationPolicy
from .store import LedgerStore


class DeterministicJobSearchService:
    """Stable mutation boundary used by local adapters and future agents."""

    def __init__(self, db_path: Path):
        self.store = LedgerStore(Path(db_path))

    @property
    def lifecycle(self):
        from .lifecycle.service import LifecycleService
        if not hasattr(self, "_lifecycle"):
            self._lifecycle = LifecycleService(self)
        return self._lifecycle

    @property
    def attention(self):
        from .attention import AttentionService
        if not hasattr(self, "_attention"):
            self._attention = AttentionService(self)
        return self._attention

    @property
    def career_actions(self):
        from .career_actions import CareerActionService
        if not hasattr(self, "_career_actions"):
            self._career_actions = CareerActionService(self)
        return self._career_actions

    @property
    def interactions(self):
        from .interactions import InteractionsService
        if not hasattr(self, "_interactions"):
            self._interactions = InteractionsService(self)
        return self._interactions

    @property
    def mail_understanding(self):
        from .mail.understanding_store import MailUnderstandingService
        if not hasattr(self, "_mail_understanding"):
            self._mail_understanding = MailUnderstandingService(self)
        return self._mail_understanding

    def request_career_reply(self, application_id: str, evidence_id: str, context: MutationContext):
        """Queue preparation without granting the caller permission to send mail."""
        request = {"application_id": application_id, "evidence_id": evidence_id}
        return self.store._idempotent("request_career_reply", context, request,
            lambda con, stamp: self._queue_career_reply(con, request, context, stamp))

    def _queue_career_reply(self, con, request, context, stamp):
        """Enqueue within the caller's transaction, including task revision checks."""
        from .contracts import canonical_json
        from .worker import _stable_id
        application_id, evidence_id = request['application_id'], request['evidence_id']
        application = self.store._application(con, application_id)
        if application['current_phase'] == 'terminal':
            raise ContractError('application is closed')
        evidence = con.execute("SELECT account_id FROM mail_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if evidence is None:
            raise ContractError("reply evidence not found")
        # The worker repeats source validation before capturing external context.
        linked = con.execute("SELECT 1 FROM lifecycle_mail_links l JOIN lifecycle_mail_observations o USING(observation_id) WHERE l.application_id=? AND o.evidence_id=? AND o.direction='inbound'", (application_id,evidence_id)).fetchone()
        if linked is None:
            raise ContractError("reply evidence must be linked to this application")
        key = "career-reply:" + payload_sha256({**request, "command":context.idempotency_key})
        work_id = _stable_id("work", key)
        con.execute("INSERT OR IGNORE INTO work_items (work_id,task_kind,dedupe_key,payload_json,status,priority,due_at,max_attempts,created_at,lane) VALUES (?,?,?,?,'queued',90,?,1,?,'core')",
                    (work_id,"career.reply.context",key,canonical_json(request),stamp,stamp))
        return {"queued":True,"work_id":work_id,"task_kind":"career.reply.context"}

    def start_application(
        self,
        snapshot: JobSnapshot,
        provenance: RecommendationProvenance,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        return self.store.start_application(snapshot, provenance, context)

    def record_event(self, event: EventInput) -> Mapping[str, Any]:
        return self.store.record_event(event)

    def record_submission(
        self,
        application_id: str,
        occurred_at: str,
        context: MutationContext,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        payload_factory: Optional[Callable[[], Mapping[str, Any]]] = None,
        request_payload: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        if payload is not None and payload_factory is not None:
            raise ContractError("submission accepts payload or payload_factory, not both")
        fixed_payload = dict(payload or {})
        factory = payload_factory or (lambda: fixed_payload)
        request = dict(
            request_payload if request_payload is not None else fixed_payload
        )
        dedupe_key = "submission-observed:" + payload_sha256(
            {
                "application_id": application_id,
                "idempotency_key": context.idempotency_key,
            }
        )
        return self.store.record_submission(
            application_id,
            occurred_at,
            context,
            dedupe_key,
            factory,
            request,
        )

    def record_mail_evidence(
        self,
        evidence: Mapping[str, Any],
        context: MutationContext,
    ) -> Mapping[str, Any]:
        return self.store.record_mail_evidence(evidence, context)

    def get_sanitized_evidence(self, evidence_id: str) -> Mapping[str, Any]:
        evidence = self.store.get_mail_evidence(evidence_id)
        allowed = (
            "evidence_id",
            "sender",
            "subject",
            "received_at",
            "body_sha256",
            "excerpt",
            "created_at",
        )
        return {name: evidence[name] for name in allowed}

    def resolve_reply_evidence(
        self, evidence_id: str, application_id: str, account_id: str
    ) -> Mapping[str, Any]:
        return self.store.resolve_reply_evidence(
            evidence_id, application_id, account_id
        )

    def put_mail_archive(
        self, record: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.put_mail_archive(record, context)

    def get_encrypted_mail_archive(self, archive_id: str) -> Mapping[str, Any]:
        return self.store.get_encrypted_mail_archive(archive_id)

    def list_mail_archive_index(
        self, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_mail_archive_index(limit=limit)

    def put_mail_archive_attachment(
        self, record: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.put_mail_archive_attachment(record, context)

    def get_encrypted_mail_archive_attachment(
        self, attachment_record_id: str
    ) -> Mapping[str, Any]:
        return self.store.get_encrypted_mail_archive_attachment(attachment_record_id)

    def create_temporal_proposal(
        self, proposal: TemporalProposalInput, context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.create_temporal_proposal(proposal, context)

    def decide_temporal_proposal(
        self,
        temporal_proposal_id: str,
        decision: str,
        reason: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        result = self.store.decide_temporal_proposal(
            temporal_proposal_id, decision, reason, context
        )
        from .db import connect
        from .mail.understanding_store import available
        with connect(self.store.db_path) as con:
            finding = con.execute("SELECT f.analysis_id FROM mail_understanding_projections p JOIN mail_understanding_findings f USING(finding_id) WHERE p.kind='temporal_proposal' AND p.target_id=?",(temporal_proposal_id,)).fetchone() if available(con) else None
        if finding:
            self.mail_understanding.project(finding['analysis_id'],MutationContext('temporal-projection:'+context.idempotency_key,'system','mail_understanding'))
        return result

    def list_temporal_proposals(
        self, statuses: Optional[Sequence[str]] = None, *, limit: int = 200
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_temporal_proposals(statuses, limit=limit)

    def list_interview_schedules(
        self, *, limit: int = 200, application_id: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None, starts_after: Optional[str] = None,
        starts_before: Optional[str] = None, offset: int = 0,
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_interview_schedules(limit=limit, application_id=application_id,
            statuses=statuses, starts_after=starts_after, starts_before=starts_before, offset=offset)

    def list_due_local_reminders(
        self, now: str, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_due_local_reminders(now, limit=limit)

    def complete_local_reminder(
        self,
        reminder_id: str,
        resolution: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        return self.store.complete_local_reminder(reminder_id, resolution, context)

    def create_event_proposal(
        self, proposal: EventProposalInput, context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.create_event_proposal(proposal, context)

    def decide_event_proposal(
        self,
        proposal_id: str,
        decision: str,
        selected_application_id: Optional[str],
        reason: str,
        context: MutationContext,
        *,
        review_mail_content: Optional[Mapping[str, Any]] = None,
        review_job_snapshot: Optional[JobSnapshot] = None,
    ) -> Mapping[str, Any]:
        return self.store.decide_event_proposal(
            proposal_id, decision, selected_application_id, reason, context,
            review_mail_content=review_mail_content,
            review_job_snapshot=review_job_snapshot,
        )

    def auto_apply_event_proposal(
        self,
        proposal_id: str,
        context: MutationContext,
        automation_policy_id: str = "",
    ) -> Mapping[str, Any]:
        return self.store.auto_apply_event_proposal(
            proposal_id, context, automation_policy_id
        )

    def create_action_proposal(
        self, proposal: ActionProposalInput, context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.create_action_proposal(proposal, context)

    def decide_action(
        self,
        action_id: str,
        approve: bool,
        exact_payload_sha256: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        return self.store.decide_action(
            action_id, approve, exact_payload_sha256, context
        )

    def get_action(self, action_id: str) -> Mapping[str, Any]:
        return self.store.get_action(action_id)

    def normalize_action_eligibility(self, action_id: str) -> Mapping[str, Any]:
        return self.store.normalize_action_eligibility(action_id)

    def claim_action(self, action_id: str) -> Mapping[str, Any]:
        return self.store.claim_action(action_id)

    def checkpoint_action_remote_id(
        self, execution_id: str, remote_id: str
    ) -> Mapping[str, Any]:
        return self.store.checkpoint_action_remote_id(execution_id, remote_id)

    def complete_action(
        self,
        execution_id: str,
        outcome: str,
        *,
        remote_id: str = "",
        error: str = "",
    ) -> Mapping[str, Any]:
        return self.store.complete_action(
            execution_id, outcome, remote_id=remote_id, error=error
        )

    def recover_stale_actions(
        self, now: Optional[str] = None, *, stale_after_seconds: int = 300
    ) -> Mapping[str, int]:
        return self.store.recover_stale_actions(
            now, stale_after_seconds=stale_after_seconds
        )

    def reconcile_action(
        self,
        action_id: str,
        resolution: str,
        remote_id: str,
        context: MutationContext,
    ) -> Mapping[str, Any]:
        return self.store.reconcile_action(
            action_id, resolution, remote_id, context
        )

    def create_reminder(
        self, reminder: Mapping[str, Any], context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.create_reminder(reminder, context)

    def list_reminders(
        self,
        statuses: Optional[Sequence[str]] = None,
        limit: int = 100,
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_reminders(statuses, limit)

    def cancel_reminder(
        self, reminder_id: str, context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.cancel_reminder(reminder_id, context)

    def complete_reminder(
        self, reminder_id: str, context: MutationContext
    ) -> Mapping[str, Any]:
        return self.store.complete_reminder(reminder_id, context)

    def publish_notification(
        self,
        intent: NotificationIntent,
        policy: NotificationPolicy = NotificationPolicy(),
        *,
        available_at: str = "",
    ) -> Mapping[str, Any]:
        if type(intent) is not NotificationIntent:
            raise ContractError("notification intent must be a NotificationIntent")
        if type(policy) is not NotificationPolicy:
            raise ContractError("notification policy must be a NotificationPolicy")
        notification = policy.evaluate(intent, available_at=available_at)
        if notification is None:
            return {"created": False, "suppressed": True, "topic": intent.topic}
        if policy.policy_id == "chief-of-staff-v1":
            return self.attention.from_notification(intent, now=available_at or None)
        key = "notify:" + payload_sha256(
            {"topic": intent.topic, "source_id": intent.source_id}
        )
        return self.store._enqueue_notification(
            notification,
            MutationContext(key, "system", "notification_policy", intent.source_id),
        )

    def claim_notification(
        self, worker_id: str, now: str, lease_seconds: int = 60
    ) -> Optional[Mapping[str, Any]]:
        return self.store.claim_notification(worker_id, now, lease_seconds)

    def defer_notification_for_receipt(self, notification_id, lease_token, now, retry_at):
        return self.store.defer_notification_for_receipt(notification_id, lease_token, now, retry_at)

    def validate_notification_claim(self, notification_id: str, lease_token: str, now: str) -> bool:
        return self.store.validate_notification_claim(notification_id, lease_token, now)

    def complete_notification(
        self,
        notification_id: str,
        lease_token: str,
        outcome: str,
        now: str,
        *,
        retry_at: Optional[str] = None,
        error: str = "",
    ) -> Mapping[str, Any]:
        return self.store.complete_notification(
            notification_id,
            lease_token,
            outcome,
            now,
            retry_at=retry_at,
            error=error,
        )

    def list_notification_outbox(
        self, statuses: Optional[Sequence[str]] = None, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_notification_outbox(statuses, limit)

    def get_shortlist_notification_state(self) -> Mapping[str, Any]:
        return self.store.get_shortlist_notification_state()

    def get_application_timeline(self, application_id: str) -> Mapping[str, Any]:
        return self.store.get_application_timeline(application_id)

    def list_applications(
        self, phases: Optional[Sequence[str]] = None, limit: int = 200
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_applications(phases, limit)

    def resolve_mail_failure(self, account_id: str, folder_ref: str, message_id: str,
                             query_version: int, action: str, context: MutationContext) -> Mapping[str, Any]:
        return self.store.resolve_mail_failure(account_id, folder_ref, message_id, query_version, action, context)

    def list_mail_candidates(self, **context: Any) -> Sequence[Mapping[str, Any]]:
        return self.store.list_mail_candidates(**context)

    def application_job_keys(self) -> Sequence[tuple[str, str]]:
        return self.store.application_job_keys()

    def application_keys(self) -> Sequence[tuple[str, str]]:
        return self.store.application_job_keys()

    def recent_company_applications(self) -> Sequence[Mapping[str, Any]]:
        return self.store.recent_company_applications()

    def list_actions(
        self, statuses: Optional[Sequence[str]] = None
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_actions(statuses)

    def system_health(self) -> Mapping[str, Any]:
        return self.store.system_health()

    def list_application_mail(self, application_id: str) -> Sequence[Mapping[str, Any]]:
        return self.store.list_application_mail(application_id)

    def list_attention_items(self) -> Sequence[Mapping[str, Any]]:
        return self.store.list_attention_items()

    def verify_projections(self) -> Sequence[Mapping[str, Any]]:
        return self.store.verify_projections()

    def rebuild_projections(
        self,
        dry_run: bool = True,
        context: Optional[MutationContext] = None,
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.rebuild_projections(dry_run=dry_run, context=context)

    def list_outbox(self, status: str = "pending") -> Sequence[Mapping[str, Any]]:
        return self.store.list_outbox(status)


# Concise alias for local callers; the frozen Protocol remains job_search.contracts.JobSearchService.
JobSearchLedger = DeterministicJobSearchService
