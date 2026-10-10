"""Trusted, read-only context assembly before proposing an exact reply.

Agent inputs contain only a local message ID and authored content. Composition
resolves provider/account context; the agent never supplies credentials, provider
IDs, recipients or source hashes. Preparing returns an envelope, not authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from .commands import DomainError
from .external_actions.preparation import prepare_reply_context


class ReadOnlyReplyClient(Protocol):
    def read_message_body(self, provider_message_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ReplyProviderContext:
    """Authenticated account binding supplied by trusted system composition."""
    account_id: str
    client: ReadOnlyReplyClient


class ReplyPreparationService:
    """Read owner context, then provider context, without any mutation or approval.

    provider_resolver(account_id) returns ReplyProviderContext or None. It resolves
    an existing authenticated read-only client, never interactive sign-in or a
    provider effect. If unavailable, preparation returns explicit blockers.

    A ready envelope must pass runtime.workflows.applicability(tx, envelope) inside
    the command transaction immediately before actions.prepare_reply(tx, envelope).
    This final check is required even if preparation finished a moment earlier.
    """
    def __init__(self, runtime, provider_resolver: Optional[Callable[[str], Optional[ReplyProviderContext]]] = None):
        self.runtime = runtime
        self.provider_resolver = provider_resolver

    @staticmethod
    def _blocked(*reasons):
        return {"status": "blocked", "envelope": None, "blockers": list(reasons)}

    def prepare(self, message_id, body, kind="send_reply", task_id=None, allow_closed=False):
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 500:
            raise DomainError("invalid_input", "A bounded local message identity is required")
        if not isinstance(body, str) or not body.strip() or len(body) > 100000:
            raise DomainError("invalid_input", "A bounded exact reply body is required")
        if kind not in {"send_reply", "create_reply_draft"} or type(allow_closed) is not bool:
            raise DomainError("invalid_input", "Unsupported reply preparation options")
        if task_id is not None and (not isinstance(task_id, str) or not 1 <= len(task_id) <= 500):
            raise DomainError("invalid_input", "A bounded local task identity is required")
        if task_id is not None and kind != "send_reply":
            return self._blocked("draft_cannot_complete_task")
        try:
            with self.runtime.executor.read() as connection:
                message = self.runtime.correspondence.get(connection, message_id)
                if message["direction"] != "incoming":
                    return self._blocked("incoming_message_required")
                association = self.runtime.correspondence.association(connection, message_id)
                if association is None:
                    return self._blocked("reviewed_association_required")
                app = self.runtime.applications.get_application(connection, association["application_id"])
                if association["application_id"] != app["id"]:
                    return self._blocked("association_requires_canonical_target")
                if app["disposition"] == "closed" and not allow_closed:
                    return self._blocked("application_closed")
                versions = {"application:" + app["id"]: app["version"],
                            "message:" + message["id"]: message["version"],
                            "association:" + association["id"]: association["version"]}
                consequence = None
                if task_id is not None:
                    task = self.runtime.applications.get_record(connection, "tasks", task_id)
                    if (task["application_id"] != app["id"] or task["pursuit_no"] != app["pursuit_no"]
                            or task["status"] != "open" or app["disposition"] != "open"
                            or task.get("completion_rule") != "verified_send"
                            or task.get("kind") not in {"reply", "send_availability", "send_document"}):
                        return self._blocked("task_consequence_not_applicable")
                    versions["task:" + task["id"]] = task["version"]
                    consequence = {"operation": "complete_task", "task_id": task["id"],
                                   "expected_version": task["version"], "completion_rule": "verified_send"}
        except DomainError as exc:
            if exc.code == "not_found":
                return self._blocked("local_context_unavailable")
            raise
        # End the read transaction before any provider resolver or provider I/O.
        if self.provider_resolver is None:
            return self._blocked("provider_context_unavailable")
        try:
            provider = self.provider_resolver(message["account_id"])
        except Exception:
            return self._blocked("provider_context_unavailable")
        if not isinstance(provider, ReplyProviderContext) or provider.client is None:
            return self._blocked("provider_context_unavailable")
        context = prepare_reply_context(provider.client, account_id=message["account_id"],
            provider_account_id=provider.account_id, message_id=message["id"],
            provider_message_id=message["provider_message_id"], source_hash=message["source_sha256"],
            context_versions=versions, body=body)
        if context.status != "ready":
            return self._blocked(*context.blockers)
        envelope = {"kind": kind, "account_id": message["account_id"], "application_id": app["id"],
                    "pursuit_no": app["pursuit_no"], "target": dict(context.target),
                    "payload": dict(context.payload), "context_versions": dict(context.context_versions),
                    "consequence": consequence, "allow_closed": allow_closed}
        return {"status": "ready", "envelope": envelope, "blockers": []}
