"""Durable lifecycle ticks; all external delivery remains in the existing outbox."""
from ..contracts import MutationContext
from ..scheduler import utc_stamp
from ..worker import DueLocalReminderTaskHandler


class LifecycleReminderTick(DueLocalReminderTaskHandler):
    def __init__(self, local_handler, lifecycle, *, now, policy):
        super().__init__(local_handler.service, local_handler.publisher, now_provider=now, limit=local_handler.limit)
        self.lifecycle = lifecycle
        self.now = now
        self.policy = policy

    def __call__(self, payload, context):
        result = dict(super().__call__(payload, context))
        stamp = utc_stamp(self.now())
        if not context.heartbeat():
            raise RuntimeError('lifecycle worker lease was lost')
        ctx = MutationContext('lifecycle-followup:' + context.work_id, 'system', 'lifecycle_worker')
        result['follow_ups'] = self.lifecycle.evaluate_follow_ups(stamp, ctx)
        result['tasks'] = self.lifecycle.publish_due_tasks(stamp, MutationContext('lifecycle-tasks:' + context.work_id, 'system', 'lifecycle_worker'), policy=self.policy)
        result['interviews'] = self.lifecycle.publish_due_interview_reminders(stamp, MutationContext('lifecycle-interviews:' + context.work_id, 'system', 'lifecycle_worker'), policy=self.policy)
        return result


class MailReplayTaskHandler:
    def __init__(self, coordinator, lifecycle, *, account_id, limit=50):
        self.account_id = account_id
        self.coordinator = coordinator
        self.lifecycle = lifecycle
        self.limit = limit

    def __call__(self, payload, context):
        # The replay row is the checkpoint. A periodic tick recovers a crash even
        # between user approval and the first worker invocation.
        pending = self.lifecycle.list_pending_replays(limit=1, account_id=self.account_id)
        if not pending:
            return {'replays':0}
        if not context.heartbeat():
            raise RuntimeError('mail replay worker lease was lost')
        result = self.coordinator.process_replay(pending[0]['replay_id'], limit=self.limit, heartbeat=context.heartbeat)
        return {'replays':1, 'progress':result}
