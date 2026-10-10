"""Reply effects using the existing narrow Outlook client.

Creation, modification, and sending are separate durable checkpoints. A sent
observation, including exact content and Sent folder identity, proves sending.
"""
from .api import ProviderOutcome
from .service import digest


def addresses(values):
    if not isinstance(values, list):
        raise ValueError("Invalid provider recipients")
    result = []
    for item in values:
        address = (item.get("emailAddress") or {}).get("address")
        if not isinstance(address, str) or "@" not in address:
            raise ValueError("Invalid provider recipient")
        result.append(address.casefold())
    return sorted(result)


def source_digest(message):
    return digest({key: message.get(key) for key in (
        "id", "conversationId", "lastModifiedDateTime", "body", "replyTo", "from",
        "sender", "subject", "isDraft")})


class ReplyProvider:
    test_only = False

    def __init__(self, account_id, client, *, sent_folder_id):
        self.account_id, self.client, self.sent_folder_id = account_id, client, sent_folder_id

    def preflight(self, action):
        envelope = action["envelope"]
        if envelope["account_id"] != self.account_id:
            raise ValueError("Provider account mismatch")
        self.client.preflight_action("outlook_reply_send" if envelope["kind"] == "send_reply" else "outlook_reply_draft")
        source = self.client.read_message_body(envelope["target"]["provider_message_id"])
        if source.get("id") != envelope["target"]["provider_message_id"] or source_digest(source) != envelope["target"]["provider_source_hash"]:
            raise ValueError("Reply source changed")
        actual = addresses(source.get("replyTo") or [source.get("from") or source.get("sender")])
        expected = sorted(a.casefold() for a in envelope["payload"]["recipients"])
        if actual != expected:
            raise ValueError("Reply recipients changed")
        checkpoint = action["checkpoint"]
        if checkpoint.get("stage") == "verified_draft":
            self._verify_message(self.client.read_message_body(checkpoint["remote_id"]), action, draft=True)

    def perform(self, action):
        envelope, checkpoint = action["envelope"], action["checkpoint"]
        if not checkpoint:
            response = self.client.create_reply_draft(envelope["target"]["provider_message_id"])
            remote = response.get("id")
            if not isinstance(remote, str) or not remote:
                raise ValueError("Draft creation did not return an identity")
            return ProviderOutcome("progress", {"stage": "created_draft", "remote_id": remote})
        remote = checkpoint["remote_id"]
        if checkpoint["stage"] == "created_draft":
            self.client.update_reply_draft(remote, envelope["payload"]["body"])
            observed = self.client.read_message_body(remote)
            self._verify_message(observed, action, draft=True)
            updated = {"stage": "verified_draft", "remote_id": remote}
            if envelope["kind"] == "create_reply_draft":
                return ProviderOutcome("succeeded", updated, {"remote_id": remote, "verified": "draft"})
            return ProviderOutcome("progress", updated)
        if checkpoint["stage"] != "verified_draft" or envelope["kind"] != "send_reply":
            raise ValueError("Unexpected reply execution checkpoint")
        self.client.send_reply_draft(remote)
        return ProviderOutcome("accepted", {"stage": "send_requested", "remote_id": remote},
                               {"remote_id": remote, "verified": False})

    def reconcile(self, action):
        checkpoint = action["checkpoint"]
        if action["envelope"]["account_id"] != self.account_id:
            return ProviderOutcome("uncertain", checkpoint, error_code="account_mismatch")
        remote = checkpoint.get("remote_id")
        if not remote:
            return ProviderOutcome("uncertain", error_code="draft_identity_unknown")
        observed = self.client.read_message_body(remote)
        if observed.get("isDraft") is False:
            if not self.sent_folder_id or observed.get("parentFolderId") != self.sent_folder_id or not observed.get("sentDateTime"):
                return ProviderOutcome("uncertain", checkpoint, error_code="sent_observation_missing")
            try:
                self._verify_message(observed, action, draft=False)
            except ValueError:
                return ProviderOutcome("mismatch", checkpoint,
                                       {"remote_id": remote, "source_hash": digest(observed), "sent_at": observed.get("sentDateTime")},
                                       "sent_content_differs")
            if action["envelope"]["kind"] != "send_reply":
                return ProviderOutcome("mismatch", checkpoint, {"remote_id": remote}, "draft_was_sent_externally")
            return ProviderOutcome("succeeded", checkpoint,
                                   {"remote_id": remote, "verified": "sent", "source_hash": digest(observed), "sent_at": observed["sentDateTime"]})
        # A draft still visible after a send request does not prove that sending
        # failed; provider replication can lag. It must never authorize resending.
        if checkpoint.get("stage") != "created_draft":
            return ProviderOutcome("uncertain", checkpoint, error_code="sending_not_yet_observed")
        self._verify_message(observed, action, draft=True)
        verified = {"stage": "verified_draft", "remote_id": remote}
        if action["envelope"]["kind"] == "create_reply_draft":
            return ProviderOutcome("succeeded", verified, {"remote_id": remote, "verified": "draft"})
        return ProviderOutcome("progress", verified, {"remote_id": remote, "verified": "draft"})

    @staticmethod
    def _verify_message(message, action, *, draft):
        payload = action["envelope"]["payload"]
        body = message.get("body") or {}
        remote = action["checkpoint"].get("remote_id")
        if (message.get("id") != remote or message.get("isDraft") is not draft
                or addresses(message.get("toRecipients") or []) != sorted(a.casefold() for a in payload["recipients"])
                or message.get("ccRecipients") or message.get("bccRecipients") or message.get("hasAttachments")
                or message.get("subject") != payload["subject"]
                or body.get("contentType", "").lower() != "text" or body.get("content") != payload["body"]):
            raise ValueError("Provider message differs from exact approval")
