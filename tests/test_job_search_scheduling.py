#!/usr/bin/env python3
"""Offline focused tests for availability planning and approved actions."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from job_search.actions import ActionExecutor
from job_search.availability import (
    AvailabilityPlanner,
    is_blocking,
)
from job_search.contracts import (
    ActionKind,
    ActionProposalInput,
    CalendarBlock,
    ConflictError,
    MutationContext,
    RetryDecision,
    payload_sha256,
)
from job_search.db import connect
from job_search.outlook import GraphHttpError, GraphOutcomeUnknown
from job_search.outlook import OutlookAuthRequired
from job_search.service import JobSearchLedger
from job_search.worker import ApprovedActionTaskHandler, TaskContext


CHICAGO = ZoneInfo("America/Chicago")


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def stamp(offset_seconds: int = 0) -> str:
    return utc_text(datetime.now(timezone.utc) + timedelta(seconds=offset_seconds))


def context(key: str, actor: str = "user", source: str = "dashboard") -> MutationContext:
    return MutationContext(key, actor, source)


class FakeOutlook:
    def __init__(self, blocks=()):
        self.blocks = tuple(blocks)
        self.calendar_calls = []
        self.reply_creates = []
        self.reply_updates = []
        self.holds = []
        self.create_reply_errors = []
        self.update_reply_errors = []
        self.hold_errors = []

    def read_calendar_view(self, starts_at, ends_at):
        self.calendar_calls.append((starts_at, ends_at))
        return self.blocks

    def create_reply_draft(self, message_id):
        self.reply_creates.append(message_id)
        if self.create_reply_errors:
            raise self.create_reply_errors.pop(0)
        return {"id": f"draft-{len(self.reply_creates)}"}

    def update_reply_draft(self, draft_id, body):
        self.reply_updates.append((draft_id, body))
        if self.update_reply_errors:
            raise self.update_reply_errors.pop(0)
        return {"id": draft_id, "isDraft": True}

    def create_private_tentative_hold(self, payload):
        self.holds.append(payload)
        if self.hold_errors:
            raise self.hold_errors.pop(0)
        return {"id": f"event-{len(self.holds)}"}

    def read_delta_page(self, opaque_url):
        raise AssertionError("mail synchronization is outside scheduling")

    def read_message_body(self, immutable_message_id):
        raise AssertionError("message bodies are outside scheduling")


def block(
    starts_at: str,
    ends_at: str,
    *,
    show_as: str = "busy",
    cancelled: bool = False,
    all_day: bool = False,
    remote_id: str = "event-1",
) -> CalendarBlock:
    return CalendarBlock(
        remote_id,
        starts_at,
        ends_at,
        show_as,
        all_day,
        cancelled,
    )


def future_weekday(hour: int = 10) -> tuple[str, str]:
    local = datetime.now(CHICAGO) + timedelta(days=2)
    while local.weekday() >= 5:
        local += timedelta(days=1)
    start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return utc_text(start), utc_text(start + timedelta(minutes=30))


def approved_action(service, kind, payload, *, account_id="outlook-personal", key="one"):
    action = service.create_action_proposal(
        ActionProposalInput(kind, None, account_id, payload, stamp(3600)),
        context(f"propose-{key}", "system", "scheduling"),
    )["action"]
    service.decide_action(
        action["action_id"],
        True,
        payload_sha256(payload),
        context(f"approve-{key}"),
    )
    return action


def test_slots_are_weekday_work_hours_with_notice_and_day_diversity():
    outlook = FakeOutlook()
    planner = AvailabilityPlanner(outlook)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)  # Tuesday 07:00 CDT
    slots = planner.propose_slots(now=now)
    assert [(slot.starts_at, slot.ends_at) for slot in slots] == [
        ("2026-09-02T13:00:00Z", "2026-09-02T13:30:00Z"),
        ("2026-09-02T13:30:00Z", "2026-09-02T14:00:00Z"),
        ("2026-09-03T13:00:00Z", "2026-09-03T13:30:00Z"),
    ]
    assert outlook.calendar_calls == [
        ("2026-09-01T12:00:00Z", "2026-09-15T12:00:00Z")
    ]


def test_precise_show_as_and_cancelled_rules():
    start = "2026-09-02T13:00:00Z"
    end = "2026-09-02T13:30:00Z"
    for value in ("busy", "tentative", "oof", "unknown", "futureValue"):
        assert is_blocking(block(start, end, show_as=value))
    for value in ("free", "workingElsewhere"):
        assert not is_blocking(block(start, end, show_as=value))
    assert not is_blocking(block(start, end, cancelled=True))


def test_preparation_and_recovery_buffers_avoid_adjacent_events():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    outlook = FakeOutlook(
        [block("2026-09-02T12:30:00Z", "2026-09-02T13:00:00Z")]
    )
    slots = AvailabilityPlanner(outlook).propose_slots(now=now)
    # 08:00 CDT needs preparation from 07:30, so it conflicts. 08:30 is first.
    assert slots[0].starts_at == "2026-09-02T13:30:00Z"

    outlook.blocks = (
        block("2026-09-02T13:30:00Z", "2026-09-02T14:00:00Z"),
    )
    slots = AvailabilityPlanner(outlook).propose_slots(now=now)
    # Recovery after 08:00 runs through 08:45, so the 08:30 event blocks it.
    assert slots[0].starts_at == "2026-09-02T14:30:00Z"


def test_dst_boundaries_use_chicago_rules():
    spring = AvailabilityPlanner(FakeOutlook()).propose_slots(
        now=datetime(2026, 3, 6, 13, 0, tzinfo=timezone.utc)
    )
    assert spring[0].starts_at == "2026-03-09T13:00:00Z"  # 08:00 CDT
    fall = AvailabilityPlanner(FakeOutlook()).propose_slots(
        now=datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
    )
    assert fall[0].starts_at == "2026-11-02T14:00:00Z"  # 08:00 CST


def test_fresh_revalidation_enforces_policy_and_reads_only_narrow_window():
    outlook = FakeOutlook()
    planner = AvailabilityPlanner(outlook)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert planner.revalidate(
        "2026-09-02T13:00:00Z", "2026-09-02T13:30:00Z", now=now
    )
    assert outlook.calendar_calls[-1] == (
        "2026-09-02T12:30:00Z",
        "2026-09-02T13:45:00Z",
    )
    try:
        planner.revalidate(
            "2026-09-02T12:30:00Z", "2026-09-02T13:00:00Z", now=now
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a 07:30 local slot was accepted")


def test_store_claims_exact_action_once_and_records_attempts():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Tuesday works."},
        )
        first = service.claim_action(action["action_id"])
        try:
            service.claim_action(action["action_id"])
        except ConflictError:
            pass
        else:
            raise AssertionError("one action was claimed concurrently")
        service.complete_action(first["execution"]["execution_id"], "retryable_failure")
        second = service.claim_action(action["action_id"])
        assert second["execution"]["attempt"] == 2


def test_reply_execution_creates_unsent_draft_then_exact_patch():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        payload = {"message_id": "mail-1", "body": "Tuesday works for me."}
        action = approved_action(service, ActionKind.OUTLOOK_REPLY_DRAFT, payload)
        outlook = FakeOutlook()
        executor = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        )
        result = executor.execute(action["action_id"])
        assert result.outcome == "succeeded"
        assert outlook.reply_creates == ["mail-1"]
        assert outlook.reply_updates == [("draft-1", "Tuesday works for me.")]
        saved = service.get_action(action["action_id"])
        assert saved["status"] == "executed"
        assert saved["executions"][0]["remote_id"] == "draft-1"


def test_reply_patch_retry_reuses_draft_instead_of_creating_duplicate():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
        )
        outlook = FakeOutlook()
        outlook.update_reply_errors.append(
            GraphHttpError(
                429,
                "TooManyRequests",
                RetryDecision(True, stamp(60), False, "retry_http_429"),
            )
        )
        executor = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        )
        first = executor.execute(action["action_id"])
        assert first.outcome == "retryable_failure"
        second = executor.execute(action["action_id"])
        assert second.outcome == "succeeded"
        assert outlook.reply_creates == ["mail-1"]
        assert outlook.reply_updates == [
            ("draft-1", "Approved exact body"),
            ("draft-1", "Approved exact body"),
        ]


def test_ambiguous_reply_creation_requires_reconciliation():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
        )
        outlook = FakeOutlook()
        outlook.create_reply_errors.append(
            GraphOutcomeUnknown(
                None,
                "transport_failure",
                RetryDecision(False, None, True, "outcome_unknown"),
            )
        )
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(action["action_id"])
        assert result.outcome == "uncertain"
        assert service.get_action(action["action_id"])["status"] == "needs_reconciliation"
        assert outlook.reply_updates == []


def test_hold_revalidates_and_uses_stable_transaction_without_attendees():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        starts_at, ends_at = future_weekday()
        payload = {"starts_at": starts_at, "ends_at": ends_at}
        action = approved_action(
            service, ActionKind.CALENDAR_TENTATIVE_HOLD, payload, key="hold"
        )
        outlook = FakeOutlook()
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(action["action_id"])
        assert result.outcome == "succeeded"
        assert len(outlook.calendar_calls) == 1
        sent = outlook.holds[0]
        assert set(sent) == {"start", "end", "transactionId"}
        assert "attendees" not in sent
        assert sent["transactionId"] == action["remote_idempotency_key"]


def test_calendar_change_invalidates_hold_before_write():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        starts_at, ends_at = future_weekday()
        action = approved_action(
            service,
            ActionKind.CALENDAR_TENTATIVE_HOLD,
            {"starts_at": starts_at, "ends_at": ends_at},
            key="conflict",
        )
        outlook = FakeOutlook([block(starts_at, ends_at)])
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(action["action_id"])
        assert result.outcome == "permanent_failure"
        assert result.error == "calendar_conflict"
        assert outlook.holds == []


def test_retryable_hold_reuses_one_transaction_id():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        starts_at, ends_at = future_weekday()
        action = approved_action(
            service,
            ActionKind.CALENDAR_TENTATIVE_HOLD,
            {"starts_at": starts_at, "ends_at": ends_at},
            key="hold-retry",
        )
        outlook = FakeOutlook()
        outlook.hold_errors.append(
            GraphHttpError(
                429,
                "TooManyRequests",
                RetryDecision(True, stamp(60), False, "retry_http_429"),
            )
        )
        executor = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        )
        assert executor.execute(action["action_id"]).outcome == "retryable_failure"
        assert executor.execute(action["action_id"]).outcome == "succeeded"
        assert [hold["transactionId"] for hold in outlook.holds] == [
            action["remote_idempotency_key"],
            action["remote_idempotency_key"],
        ]


def test_account_mismatch_and_payload_expansion_are_rejected_without_outlook_write():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Text", "to": "other@example.test"},
            key="expanded",
        )
        outlook = FakeOutlook()
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(action["action_id"])
        assert result.outcome == "permanent_failure"
        assert outlook.reply_creates == []

        wrong = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-2", "body": "Text"},
            account_id="another-account",
            key="account",
        )
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(wrong["action_id"])
        assert result.error == "account_mismatch"
        assert outlook.reply_creates == []


def test_stale_reply_claim_without_checkpoint_requires_user_reconciliation():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
            key="crash-before-checkpoint",
        )
        claim = service.claim_action(action["action_id"])
        with connect(service.store.db_path) as con:
            con.execute(
                "UPDATE action_executions SET started_at='2020-01-01T00:00:00Z' "
                "WHERE execution_id=?",
                (claim["execution"]["execution_id"],),
            )
        recovered = service.recover_stale_actions(stale_after_seconds=60)
        assert recovered["needs_reconciliation"] == 1
        assert service.get_action(action["action_id"])["status"] == "needs_reconciliation"
        result = service.reconcile_action(
            action["action_id"],
            "not_created",
            "",
            MutationContext("reconcile-not-created", "user", "dashboard"),
        )
        assert result["action_status"] == "pending"


def test_stale_checkpointed_reply_claim_is_safe_to_retry():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
            key="crash-after-checkpoint",
        )
        claim = service.claim_action(action["action_id"])
        service.checkpoint_action_remote_id(
            claim["execution"]["execution_id"], "existing-draft"
        )
        with connect(service.store.db_path) as con:
            con.execute(
                "UPDATE action_executions SET started_at='2020-01-01T00:00:00Z' "
                "WHERE execution_id=?",
                (claim["execution"]["execution_id"],),
            )
        recovered = service.recover_stale_actions(stale_after_seconds=60)
        assert recovered["recovered"] == 1
        outlook = FakeOutlook()
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(action["action_id"])
        assert result.outcome == "succeeded"
        assert outlook.reply_creates == []
        assert outlook.reply_updates == [("existing-draft", "Approved exact body")]


def test_auth_required_during_action_does_not_leave_executing_claim():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
            key="reauth",
        )
        outlook = FakeOutlook()
        outlook.create_reply_errors.append(OutlookAuthRequired("interaction_required"))
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(action["action_id"])
        assert result.outcome == "retryable_failure"
        assert result.error == "outlook_reauth_required"
        assert service.get_action(action["action_id"])["status"] == "approved"


def test_auth_preflight_happens_before_action_claim():
    class PreflightOutlook(FakeOutlook):
        def preflight_action(self, kind):
            assert kind == "outlook_reply_draft"
            raise OutlookAuthRequired("interaction_required")

    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
            key="preflight-reauth",
        )
        outlook = PreflightOutlook()
        result = ActionExecutor(
            service,
            outlook,
            AvailabilityPlanner(outlook),
            account_id="outlook-personal",
        ).execute(action["action_id"])
        assert result.outcome == "retryable_failure" and result.execution_id == ""
        saved = service.get_action(action["action_id"])
        assert saved["status"] == "approved" and saved["executions"] == []


def test_action_retry_limit_transitions_to_visible_failure():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
            key="bounded-action-retries",
        )
        for attempt in range(1, 6):
            claim = service.claim_action(action["action_id"])
            result = service.complete_action(
                claim["execution"]["execution_id"], "retryable_failure"
            )
            assert result["action_status"] == (
                "approved" if attempt < 5 else "failed"
            )
        saved = service.get_action(action["action_id"])
        assert saved["status"] == "failed" and len(saved["executions"]) == 5
        assert any(
            item["id"] == action["action_id"] and item["status"] == "failed"
            for item in service.list_attention_items()
        )


def test_recurring_action_handler_closes_expired_approval_without_remote_write():
    class ExpiredPreflightOutlook(FakeOutlook):
        def preflight_action(self, kind):
            del kind
            raise OutlookAuthRequired("interaction_required")

    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "job-search.db")
        action = approved_action(
            service,
            ActionKind.OUTLOOK_REPLY_DRAFT,
            {"message_id": "mail-1", "body": "Approved exact body"},
            key="expired-action",
        )
        with connect(service.store.db_path) as con:
            con.execute(
                "UPDATE action_approval_decisions SET expires_at='2020-01-01T00:00:00Z' "
                "WHERE action_id=?",
                (action["action_id"],),
            )
        outlook = ExpiredPreflightOutlook()
        handler = ApprovedActionTaskHandler(
            service,
            ActionExecutor(
                service,
                outlook,
                AvailabilityPlanner(outlook),
                account_id="outlook-personal",
            ),
        )
        result = handler(
            {},
            TaskContext("work-expired", "outlook.actions.execute", 1, stamp(), lambda: True),
        )
        assert result["executions"][0]["outcome"] == "permanent_failure"
        assert service.get_action(action["action_id"])["status"] == "failed"
        assert outlook.reply_creates == [] and outlook.reply_updates == []


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} scheduling/action tests)")


if __name__ == "__main__":
    main()
