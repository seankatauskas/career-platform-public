"""Composition of action-specific adapters without lifecycle coupling."""


class ActionProviders:
    test_only = False

    def __init__(self, reply, calendar):
        self.reply, self.calendar = reply, calendar

    def _for(self, action):
        return self.reply if action["kind"] in {"send_reply", "create_reply_draft"} else self.calendar

    def preflight(self, action):
        return self._for(action).preflight(action)

    def perform(self, action):
        return self._for(action).perform(action)

    def reconcile(self, action):
        return self._for(action).reconcile(action)
