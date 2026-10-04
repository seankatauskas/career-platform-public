"""Bounded, read-only refresh of calendar identities reviewed by the user."""
from datetime import datetime, timedelta, timezone

from ..contracts import MutationContext
from ..worker import FollowUpTask, TaskResult


class InterviewCalendarTaskHandler:
    def __init__(
        self, lifecycle, calendar, *, account_id, limit=50, activation_start=None
    ):
        if not 1 <= limit <= 100:
            raise ValueError("calendar reconciliation limit must be between 1 and 100")
        self.lifecycle = lifecycle
        self.calendar = calendar
        self.account_id = account_id
        self.limit = limit
        self.activation_start = activation_start

    def __call__(self, payload, context):
        if self.activation_start is not None and self.activation_start() is None:
            raise ValueError("activate Outlook before reconciling interviews")
        legacy = self.lifecycle.import_pending_legacy_interviews(
            MutationContext(
                "calendar-import:" + context.work_id, "system", "outlook_calendar"
            ),
            limit=self.limit,
        )
        page = self.lifecycle.list_calendar_interview_links(
            self.account_id, after=str(payload.get("after") or ""), limit=self.limit
        )
        discovery = {"status": "not_requested"}
        if not payload.get("after") and hasattr(self.calendar, "read_interview_events"):
            current = datetime.now(timezone.utc)
            starts_at = current.isoformat(timespec="seconds").replace("+00:00", "Z")
            ends_at = (
                (current + timedelta(days=14))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
            calendar_events = self.calendar.read_interview_events(
                starts_at, ends_at, max_events=500
            )
            discovery = self.lifecycle.discover_calendar_interviews(
                self.account_id,
                calendar_events,
                MutationContext(
                    "calendar-discovery:" + context.work_id,
                    "system",
                    "outlook_calendar",
                ),
            )
            discovery["window"] = {"starts_at": starts_at, "ends_at": ends_at}
            discovery["status"] = "checked"
        events = []
        # Fetch each linked event directly: an interview moved outside a calendar
        # view window must still invalidate its former reminders. Missing events
        # propagate as failures and never become inferred cancellations.
        for link in page["links"]:
            if not context.heartbeat():
                raise RuntimeError("calendar reconciliation worker lease was lost")
            events.append(self.calendar.read_interview_event(link["calendar_event_id"]))
        result = self.lifecycle.reconcile_calendar_events(
            self.account_id,
            events,
            MutationContext(
                "calendar-work:" + context.work_id, "system", "outlook_calendar"
            ),
        )
        result["discovery"] = discovery
        result["legacy_import"] = legacy
        result["coverage"] = {
            "linked_only": True,
            "page_size": len(events),
            "next_after": page["next_after"],
        }
        follow_ups = ()
        if page["next_after"]:
            follow_ups = (
                FollowUpTask(
                    context.task_kind,
                    {"after": page["next_after"]},
                    workflow_id=context.workflow_id,
                    parent_work_id=context.work_id,
                ),
            )
        return TaskResult(result, follow_ups)
