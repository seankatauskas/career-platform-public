"""Composition of career attention services; external writes remain worker-owned."""
from __future__ import annotations

from .contracts import MutationContext
from .worker import FollowUpTask, TaskResult


def _context(context, source):
    return MutationContext(source + ":" + context.work_id, "system", source)


class AttentionTick:
    def __init__(self, ledger, config):
        self.ledger, self.config = ledger, config

    def __call__(self, payload, context):
        if not context.heartbeat():
            raise RuntimeError("attention worker lease was lost")
        result = self.ledger.attention.process_tick(context=_context(context, "attention_worker"))
        from .db import connect
        with connect(self.ledger.store.db_path) as con:
            control = con.execute("SELECT enabled FROM automation_controls WHERE capability='briefing_ai'").fetchone()
        if not self.config.briefing_ai_enabled or self.config.briefing_inference_config is None or (control is not None and not control['enabled']):
            return TaskResult(result)
        followups = [FollowUpTask("briefing.compose", {"briefing_id": item}, lane="model", priority=95, max_attempts=1, dedupe_key="briefing:" + item)
                     for item in result.get("generation_pending", ())]
        from .contracts import payload_sha256
        followups.extend(FollowUpTask("career.reply.prepare", request, lane="model", priority=90, max_attempts=1, dedupe_key="reply:" + payload_sha256(request))
                         for request in result.get("reply_requests", ()))
        return TaskResult(result, tuple(followups))


class CareerWorker:
    def __init__(self, actions, operation):
        self.actions, self.operation = actions, operation

    def __call__(self, payload, context):
        if not context.heartbeat():
            raise RuntimeError("career worker lease was lost")
        return getattr(self.actions, self.operation)(_context(context, context.task_kind))


class ReplyContextWorker:
    def __init__(self, actions):
        self.actions = actions

    def __call__(self, payload, context):
        if not context.heartbeat():
            raise RuntimeError("reply context worker lease was lost")
        result = self.actions.prepare_reply_context(str(payload['application_id']), str(payload['evidence_id']), _context(context, 'career_reply_context'))
        return TaskResult(result, (FollowUpTask('career.reply.prepare', dict(payload), lane='model', priority=90, max_attempts=1),))


class AgendaCalendarWorker:
    def __init__(self, original, actions, now_provider):
        self.original, self.actions, self.now_provider = original, actions, now_provider

    def __call__(self, payload, context):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from .scheduler import utc_stamp
        if not payload.get("after"):
            local = self.now_provider().astimezone(ZoneInfo("America/Chicago"))
            start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            self.actions.refresh_agenda(utc_stamp(start), utc_stamp(start + timedelta(days=13)), _context(context, "career_agenda"))
        return self.original(payload, context)


def configure_services(ledger, config, *, outlook=None, generation_provider=None, now_provider=None):
    from .attention import AttentionService
    from .career_actions import CareerActionService
    from .interactions import InteractionsService

    from .availability import AvailabilityPolicy
    actions = CareerActionService(
        ledger, outlook=outlook, account_id=config.outlook_account_id,
        reply_provider=generation_provider, now_provider=now_provider,
        availability_policy=AvailabilityPolicy(timezone_name=config.timezone),
    )
    attention = AttentionService(
        ledger, now_provider=now_provider,
        agenda_provider=actions.agenda,
        generation_provider=generation_provider, dashboard_url=config.dashboard_https_origin,
    )
    identity = None
    if config.interaction_token_file is not None:
        identity = {"bot_id": config.telegram_bot_id, "user_id": config.telegram_user_id,
                    "chat_id": config.telegram_chat_id}
    ledger._career_actions = actions
    ledger._attention = attention
    ledger._interactions = InteractionsService(
        ledger, actions=actions, attention=attention, identity=identity,
        now_provider=now_provider,
    )
    return ledger


def build_generation_provider(config):
    """Only the explicitly configured briefing profile permits model egress."""
    if not config.briefing_ai_enabled or config.briefing_inference_config is None:
        return None
    from .inference import build_structured_provider, load_inference_config
    profile = load_inference_config(config.briefing_inference_config)
    return build_structured_provider(profile)
