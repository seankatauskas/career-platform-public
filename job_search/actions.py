"""Approval-bound executor for unsent drafts and private tentative holds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .availability import AvailabilityConflict, AvailabilityPlanner
from .contracts import ActionKind, ConflictError, ContractError, OutlookTransport, parse_utc
from .outlook import GraphHttpError, GraphOutcomeUnknown, OutlookAuthRequired


class ActionStore(Protocol):
    def get_action(self, action_id: str) -> Mapping[str, Any]: ...

    def normalize_action_eligibility(self, action_id: str) -> Mapping[str, Any]: ...

    def claim_action(self, action_id: str) -> Mapping[str, Any]: ...

    def checkpoint_action_remote_id(
        self, execution_id: str, remote_id: str
    ) -> Mapping[str, Any]: ...

    def complete_action(
        self,
        execution_id: str,
        outcome: str,
        *,
        remote_id: str = "",
        error: str = "",
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ActionExecutionResult:
    action_id: str
    execution_id: str
    outcome: str
    remote_id: str = ""
    error: str = ""
    retry_at: str = ""


def _reply_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    if set(payload) != {"message_id", "body"}:
        raise ContractError("reply draft payload must contain only message_id and body")
    message_id = payload.get("message_id")
    body = payload.get("body")
    if not isinstance(message_id, str) or not message_id:
        raise ContractError("reply draft message_id is required")
    if not isinstance(body, str) or not body.strip():
        raise ContractError("reply draft body is required")
    return message_id, body


def _hold_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    if set(payload) != {"starts_at", "ends_at"}:
        raise ContractError("calendar hold payload must contain only starts_at and ends_at")
    starts_at = payload.get("starts_at")
    ends_at = payload.get("ends_at")
    if not isinstance(starts_at, str) or not isinstance(ends_at, str):
        raise ContractError("calendar hold requires UTC timestamps")
    if parse_utc(ends_at) <= parse_utc(starts_at):
        raise ContractError("calendar hold end must be after start")
    return starts_at, ends_at


class ActionExecutor:
    """Executes only the exact payload already approved in the ledger."""

    def __init__(
        self,
        store: ActionStore,
        outlook: OutlookTransport,
        availability: AvailabilityPlanner,
        *,
        account_id: str,
    ) -> None:
        self._store = store
        self._outlook = outlook
        self._availability = availability
        self._account_id = account_id

    def execute(self, action_id: str) -> ActionExecutionResult:
        preview = self._store.get_action(action_id)
        eligibility = self._store.normalize_action_eligibility(action_id)
        if not eligibility["eligible"]:
            return ActionExecutionResult(
                action_id,
                "",
                "permanent_failure",
                error="action_not_executable",
            )
        preflight = getattr(self._outlook, "preflight_action", None)
        if callable(preflight) and preview["account_id"] == self._account_id:
            try:
                preflight(str(preview["kind"]))
            except OutlookAuthRequired:
                return ActionExecutionResult(
                    action_id,
                    "",
                    "retryable_failure",
                    error="outlook_reauth_required",
                )
        try:
            claim = self._store.claim_action(action_id)
        except ConflictError:
            return ActionExecutionResult(
                action_id,
                "",
                "permanent_failure",
                error="action_not_executable",
            )
        action = claim["action"]
        execution = claim["execution"]
        execution_id = str(execution["execution_id"])
        if action["account_id"] != self._account_id:
            return self._complete(
                action_id, execution_id, "permanent_failure", error="account_mismatch"
            )
        try:
            kind = ActionKind(str(action["kind"]))
            if kind is ActionKind.OUTLOOK_REPLY_DRAFT:
                return self._execute_reply(
                    action_id,
                    execution_id,
                    action["payload"],
                    str(claim.get("prior_remote_id") or ""),
                )
            if kind is ActionKind.CALENDAR_TENTATIVE_HOLD:
                return self._execute_hold(
                    action_id,
                    execution_id,
                    action["payload"],
                    str(action["remote_idempotency_key"]),
                )
            raise ContractError("unsupported action kind")
        except AvailabilityConflict:
            return self._complete(
                action_id, execution_id, "permanent_failure", error="calendar_conflict"
            )
        except GraphOutcomeUnknown as exc:
            return self._complete(
                action_id,
                execution_id,
                "uncertain",
                error=exc.error_code or "outcome_unknown",
            )
        except GraphHttpError as exc:
            if exc.decision.outcome_unknown:
                outcome = "uncertain"
            elif exc.decision.retryable:
                outcome = "retryable_failure"
            else:
                outcome = "permanent_failure"
            return self._complete(
                action_id,
                execution_id,
                outcome,
                error=exc.error_code or exc.decision.reason,
                retry_at=exc.decision.next_attempt_at or "",
            )
        except OutlookAuthRequired:
            return self._complete(
                action_id,
                execution_id,
                "retryable_failure",
                error="outlook_reauth_required",
            )
        except (ContractError, ValueError, KeyError, TypeError):
            return self._complete(
                action_id, execution_id, "permanent_failure", error="invalid_action_payload"
            )

    def _execute_reply(
        self,
        action_id: str,
        execution_id: str,
        payload: Mapping[str, Any],
        prior_remote_id: str,
    ) -> ActionExecutionResult:
        message_id, body = _reply_payload(payload)
        draft_id = prior_remote_id
        if not draft_id:
            draft = self._outlook.create_reply_draft(message_id)
            draft_id = str(draft.get("id") or "")
            if not draft_id:
                raise ContractError("Outlook did not return a draft id")
            self._store.checkpoint_action_remote_id(execution_id, draft_id)
        updated = self._outlook.update_reply_draft(draft_id, body)
        if updated.get("isDraft") is False:
            raise ContractError("Outlook reply is no longer an unsent draft")
        return self._complete(
            action_id, execution_id, "succeeded", remote_id=draft_id
        )

    def _execute_hold(
        self,
        action_id: str,
        execution_id: str,
        payload: Mapping[str, Any],
        transaction_id: str,
    ) -> ActionExecutionResult:
        starts_at, ends_at = _hold_payload(payload)
        self._availability.require_fresh(starts_at, ends_at)
        created = self._outlook.create_private_tentative_hold(
            {
                "start": {"dateTime": starts_at},
                "end": {"dateTime": ends_at},
                "transactionId": transaction_id,
            }
        )
        remote_id = str(created.get("id") or "")
        if not remote_id:
            raise ContractError("Outlook did not return an event id")
        self._store.checkpoint_action_remote_id(execution_id, remote_id)
        return self._complete(
            action_id, execution_id, "succeeded", remote_id=remote_id
        )

    def _complete(
        self,
        action_id: str,
        execution_id: str,
        outcome: str,
        *,
        remote_id: str = "",
        error: str = "",
        retry_at: str = "",
    ) -> ActionExecutionResult:
        completed = self._store.complete_action(
            execution_id,
            outcome,
            remote_id=remote_id,
            error=error,
        )
        return ActionExecutionResult(
            action_id,
            execution_id,
            str(completed["execution_status"]),
            str(completed.get("remote_id") or ""),
            str(completed.get("error") or ""),
            retry_at,
        )
