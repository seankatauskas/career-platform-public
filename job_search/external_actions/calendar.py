"""Owned private calendar effects through existing conditional Outlook methods."""
from .api import ProviderOutcome
from .service import instant, stamp, digest


def provider_time(value):
    if not isinstance(value, dict) or str(value.get("timeZone", "")).upper() not in {"UTC", "ETC/UTC"}:
        raise ValueError("Provider did not return UTC")
    text = value.get("dateTime", "")
    return instant(text if text.endswith("Z") or "+" in text else text + "Z")


class CalendarProvider:
    test_only = False

    def __init__(self, account_id, client):
        self.account_id, self.client = account_id, client

    def preflight(self, action):
        envelope = action["envelope"]
        if envelope["account_id"] != self.account_id:
            raise ValueError("Provider account mismatch")
        self.client.preflight_action("calendar_tentative_hold")
        if envelope["kind"] != "create_calendar_entry":
            event = self.client.read_owned_event(envelope["target"]["remote_id"])
            self._verify_owned(event, action)
            if event.get("@odata.etag") != envelope["target"]["etag"]:
                raise ValueError("Owned calendar event changed")

    def perform(self, action):
        envelope = action["envelope"]
        kind, target, payload = envelope["kind"], envelope["target"], envelope["payload"]
        if kind == "cancel_calendar_entry":
            self.client.delete_private_commitment(target["remote_id"], target["etag"])
            return ProviderOutcome("succeeded", {"remote_id": target["remote_id"]},
                                   {"remote_id": target["remote_id"], "verified": "conditional_delete_acknowledged"})
        transaction_id = envelope["operation_id"] if kind == "create_calendar_entry" else target["transaction_id"]
        result = self.client.write_private_commitment(
            stamp(instant(payload["starts_at"])), stamp(instant(payload["ends_at"])), transaction_id,
            remote_id=target.get("remote_id", ""), etag=target.get("etag", ""),
            location=payload.get("location", ""), join_url=payload.get("join_url", ""))
        remote = result.get("id") or target.get("remote_id")
        if not remote:
            raise ValueError("Calendar write returned no identity")
        # Persist identity before reading it back. If readback fails, recovery can
        # reconcile this exact item, never a coincident calendar event.
        return ProviderOutcome("accepted", {"remote_id": remote, "transaction_id": transaction_id},
                               {"remote_id": remote, "verified": False})

    def reconcile(self, action):
        envelope = action["envelope"]
        if envelope["account_id"] != self.account_id:
            return ProviderOutcome("uncertain", action["checkpoint"], error_code="account_mismatch")
        if envelope["kind"] == "cancel_calendar_entry":
            # A later 404 can mean access loss. No deletion receipt means unknown.
            return ProviderOutcome("uncertain", action["checkpoint"], error_code="deletion_receipt_missing")
        remote = action["checkpoint"].get("remote_id") or envelope["target"].get("remote_id")
        if not remote:
            return ProviderOutcome("uncertain", error_code="calendar_identity_unknown")
        event = self.client.read_owned_event(remote)
        try:
            self._verify_owned(event, action)
            payload = envelope["payload"]
            expected_body = ("Location: " + payload["location"] + "\n" if payload.get("location") else "") + ("Join: " + payload["join_url"] if payload.get("join_url") else "")
            if (provider_time(event.get("start")) != instant(payload["starts_at"])
                    or provider_time(event.get("end")) != instant(payload["ends_at"])
                    or event.get("subject") != "Private career commitment"
                    or (event.get("body") or {}).get("contentType", "").lower() != "text"
                    or (event.get("body") or {}).get("content") != expected_body
                    or (event.get("location") or {}).get("displayName", "") != payload.get("location", "")
                    or event.get("showAs") != "busy"):
                raise ValueError("Calendar effect differs from exact approval")
        except (ValueError, TypeError):
            return ProviderOutcome("mismatch", action["checkpoint"], {"remote_id": remote, "source_hash": digest(event)}, "calendar_content_differs")
        return ProviderOutcome("succeeded", action["checkpoint"],
                               {"remote_id": remote, "verified": "owned_private_event", "etag": event.get("@odata.etag"), "source_hash": digest(event)})

    @staticmethod
    def _verify_owned(event, action):
        envelope = action["envelope"]
        expected_transaction = envelope["operation_id"] if envelope["kind"] == "create_calendar_entry" else envelope["target"]["transaction_id"]
        expected_remote = action["checkpoint"].get("remote_id") or envelope["target"].get("remote_id")
        if (event.get("id") != expected_remote or event.get("transactionId") != expected_transaction
                or event.get("attendees") or event.get("isOrganizer") is not True
                or event.get("sensitivity") != "private" or event.get("isCancelled")):
            raise ValueError("Calendar ownership cannot be verified")
