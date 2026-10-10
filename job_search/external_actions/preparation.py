"""Read-only reply preparation before the command transaction begins."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .reply import addresses, source_digest


@dataclass(frozen=True)
class ReplyPreparation:
    status: str
    target: Mapping[str, str] | None
    payload: Mapping[str, Any] | None
    context_versions: Mapping[str, int]
    blockers: tuple[str, ...] = ()


def prepare_reply_context(client, *, account_id, provider_account_id, message_id,
                          provider_message_id, source_hash, context_versions, body):
    """Capture provider context without opening a transaction or writing a draft.

    The caller first reads an immutable Correspondence DTO and its association /
    application versions. After this call it must revalidate those versions in
    the shared command transaction before saving the resulting proposal.
    Authentication-bound provider_account_id comes from trusted composition,
    never model-supplied fields. Blocked results contain no fabricated digest.
    """
    if account_id != provider_account_id:
        return ReplyPreparation("blocked", None, None, dict(context_versions), ("account_mismatch",))
    try:
        source = client.read_message_body(provider_message_id)
        if source.get("id") != provider_message_id or source.get("isDraft") is not False:
            return ReplyPreparation("blocked", None, None, dict(context_versions), ("source_identity_unverified",))
        recipients = addresses(source.get("replyTo") or [source.get("from") or source.get("sender")])
        subject = source.get("subject")
        if not isinstance(subject, str) or not subject or not 1 <= len(recipients) <= 10:
            return ReplyPreparation("blocked", None, None, dict(context_versions), ("reply_context_incomplete",))
        # Graph's createReply creates this reply subject. Readback still checks
        # exact agreement before send; unusual provider behavior blocks dispatch.
        reply_subject = subject if subject[:3].casefold() == "re:" else "Re: " + subject
        target = {"message_id": message_id, "provider_message_id": provider_message_id,
                  "source_hash": source_hash, "provider_source_hash": source_digest(source)}
        return ReplyPreparation("ready", target, {"recipients": recipients, "subject": reply_subject, "body": body}, dict(context_versions))
    except Exception:
        # A retry can be requested, but an unavailable provider is never evidence
        # of a valid reply context. Error text can contain private provider data.
        return ReplyPreparation("blocked", None, None, dict(context_versions), ("provider_context_unavailable",))
