"""Shared lifecycle authority; transport clients never receive this store."""
from .core import CoreMixin
from .mail import MailMixin
from .interviews import InterviewMixin
from .briefing import BriefingMixin

class LifecycleService(CoreMixin, MailMixin, InterviewMixin, BriefingMixin):
    def __init__(self, ledger):
        self.ledger = ledger
        self.store = ledger.store

    def call_tool(self, name, args):
        from .tools import call
        return call(self, name, args)
