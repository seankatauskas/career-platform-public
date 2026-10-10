"""Common authority checks; owners still validate each operation's domain rules."""
from datetime import datetime

from .context import CommandContext, DomainError


HUMAN_DECISIONS = frozenset({"review_changes", "authorize_action", "reject_action",
    "retry_processing", "resolve_processing", "revoke_action", "correct_association", "combine_jobs", "delegate_operation", "replace_proposal"})
BOOKKEEPING = frozenset({"record_message", "record_browser_observation", "analyze_observation",
    "record_analysis", "project_analysis", "propose_changes", "prepare_reply", "prepare_calendar_change",
    "claim_action", "begin_write", "finish_attempt", "reconcile_action", "reconcile_result",
    "acknowledge_result", "apply_action_result", "execute_action", "expire_actions",
    "record_delivery", "claim_reminder", "import_snapshot", "run_scheduled", "queue_due_reminders"})


def authorize(context: CommandContext, operation: str, payload_digest: str, now: str) -> None:
    principal = context.principal
    if operation not in principal.capabilities and not (
        principal.kind == "human" and "*" in principal.capabilities
    ):
        raise DomainError("not_authorized", "Operation is not granted to this principal")
    if operation in HUMAN_DECISIONS and principal.kind != "human":
        raise DomainError("not_authorized", "This decision requires human authority")
    if operation in BOOKKEEPING:
        if operation in {"apply_action_result", "import_snapshot", "claim_action", "begin_write",
                         "finish_attempt", "reconcile_result", "acknowledge_result", "execute_action",
                         "claim_reminder", "record_delivery", "expire_actions", "run_scheduled", "queue_due_reminders"} and principal.kind != "worker":
            raise DomainError("not_authorized", "This operation requires a bounded worker capability")
        return
    if context.origin == "inferred":
        raise DomainError("not_authorized", "Inferred changes must be reviewed")
    if principal.kind == "human":
        return
    grant = context.delegation
    if grant is None or grant.recipient_id != principal.actor_id or not grant.issuer_id or (
        grant.operation != operation or grant.payload_digest != payload_digest
    ):
        raise DomainError("not_authorized", "An exact human delegation is required")
    try:
        valid = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00")) > datetime.fromisoformat(now.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise DomainError("not_authorized", "The delegation expired")
