"""Configured logical mailbox to authenticated MSAL identity binding.

Composition supplies a read-only selected-home-account accessor, never a token or
an identity from an action payload. The same wrapper supports reply preparation
and dispatch so either path fails when the cached Microsoft account changes.
"""
from job_search.commands import DomainError
from job_search.outlook.transport import GraphHttpError

from .api import PreEffectTransientError
from .calendar import CalendarProvider
from .reply import ReplyProvider


class ConfiguredOutlookProvider:
    test_only = False

    def __init__(self, account_id, expected_home_account_id, selected_home_account_id,
                 client, *, sent_folder_id):
        if not all(isinstance(value, str) and value.strip() for value in
                   (account_id, expected_home_account_id)) or not callable(selected_home_account_id):
            raise ValueError("Outlook requires an exact configured account and Sent folder binding")
        if not callable(sent_folder_id) and not (isinstance(sent_folder_id, str) and sent_folder_id):
            raise ValueError("Outlook requires a configured Sent folder or trusted resolver")
        self.account_id = account_id
        self._expected_home = expected_home_account_id
        self._selected_home = selected_home_account_id
        self._client = client
        self._sent_folder_id = sent_folder_id
        self._reply = ReplyProvider(account_id, client, sent_folder_id=sent_folder_id if isinstance(sent_folder_id, str) else "")
        self._calendar = CalendarProvider(account_id, client)

    def verify_binding(self):
        try:
            selected = self._selected_home()
        except Exception:
            raise DomainError("not_authorized", "The configured Outlook account is unavailable") from None
        if selected != self._expected_home:
            raise DomainError("not_authorized", "The authenticated Outlook account does not match its configured binding")
        return True

    def read_message_body(self, provider_message_id):
        self.verify_binding()
        return self._client.read_message_body(provider_message_id)

    def _provider(self, action):
        self.verify_binding()
        envelope = action["envelope"]
        if envelope["account_id"] != self.account_id:
            raise DomainError("not_authorized", "The action targets another configured mailbox")
        kind = envelope["kind"]
        if kind in {"send_reply", "create_reply_draft"}:
            if not self._reply.sent_folder_id:
                resolved = self._sent_folder_id()
                if not isinstance(resolved, str) or not resolved:
                    raise DomainError("not_authorized", "Authenticated Sent folder is unavailable")
                self._reply.sent_folder_id = resolved
            return self._reply
        if kind in {"create_calendar_entry", "update_calendar_entry", "cancel_calendar_entry"}:
            return self._calendar
        raise DomainError("invalid_input", "Unsupported configured Outlook effect")

    def preflight(self, action):
        try:
            self._provider(action).preflight(action)
        except GraphHttpError as exc:
            decision = exc.decision
            # Only this read-only phase can prove that a transport error occurred
            # before our provider write. perform() errors remain uncertain.
            if decision.retryable and not decision.outcome_unknown:
                raise PreEffectTransientError("Outlook preflight is temporarily unavailable") from None
            raise

    def perform(self, action):
        return self._provider(action).perform(action)

    def reconcile(self, action):
        return self._provider(action).reconcile(action)


class OutlookProviderResolver:
    def __init__(self, providers):
        self._providers = dict(providers)
        if any(key != provider.account_id for key, provider in self._providers.items()):
            raise ValueError("Provider routing must preserve its configured account identity")

    def __call__(self, account_id):
        provider = self._providers.get(account_id)
        if provider is None:
            raise DomainError("not_authorized", "No provider is configured for this mailbox")
        provider.verify_binding()
        return provider
