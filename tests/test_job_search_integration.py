#!/usr/bin/env python3
"""Offline checks for concrete job, Hermes-proposal, and alert composition."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_search.availability import InterviewSlot
from job_search.contracts import (
    ApplicationEventType,
    ContractError,
    EventProposalInput,
    JobSnapshot,
    MutationContext,
    ProducerKind,
    RecommendationProvenance,
)
from job_search.integration import (
    ApplicationEventNotificationHandler,
    ConfiguredShortlistSource,
    LedgerProposalSource,
    LocalJobCatalog,
    ReminderNotificationHandler,
    ShortlistNotificationEvaluator,
)
from job_search.notifications import DurableNotificationPublisher
from job_search.service import JobSearchLedger
from job_search.worker import OutboxContext, TaskContext


NOW = datetime(2026, 9, 2, 16, 0, tzinfo=timezone.utc)


def start(ledger: JobSearchLedger, suffix: str = "one") -> str:
    return str(
        ledger.start_application(
            JobSnapshot(
                "ashby",
                "job-" + suffix,
                "family-" + suffix,
                "Platform Engineer",
                "Signal Co",
                "signal",
                "https://example.test/" + suffix,
            ),
            RecommendationProvenance(),
            MutationContext("start-" + suffix, "user", "test"),
        )["application"]["application_id"]
    )


def test_local_job_catalog_is_bounded_read_only_and_ignores_closed_jobs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "jobs.db"
        with sqlite3.connect(path) as con:
            con.executescript(
                """
                CREATE TABLE jobs (
                    ats TEXT NOT NULL,id TEXT NOT NULL,company TEXT,title TEXT,
                    description TEXT,location TEXT,publishedAt TEXT,jobUrl TEXT,
                    closed_at TEXT,PRIMARY KEY (ats,id)
                );
                """
            )
            con.executemany(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "ashby", "one", "Signal Co", "Platform Engineer",
                        "Build distributed systems", "Remote", "2026-09-02T12:00:00Z",
                        "https://example.test/one", None,
                    ),
                    (
                        "lever", "two", "Old Co", "Platform Engineer",
                        "Build systems", "Remote", "2026-09-01T12:00:00Z",
                        "https://example.test/two", "2026-09-02T13:00:00Z",
                    ),
                ],
            )
        rows = LocalJobCatalog(path).search_jobs("platform systems", 10)
        assert len(rows) == 1 and rows[0]["id"] == "one"
        assert "description" not in rows[0]
        with sqlite3.connect(path) as con:
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_configured_shortlist_uses_defaults_and_application_exclusions() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = []

        def create_shortlist(self, options, **kwargs):
            self.calls.append((dict(options), kwargs))
            return {"recommendations": [], "session_id": "session-one"}

        def exposed_job_keys(self, _session_ids, _job_keys):
            return set()

    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "ledger.db")
        start(ledger)
        gateway = Gateway()
        source = ConfiguredShortlistSource(
            gateway,
            ledger,
            {"limit": 20, "days": 30, "policy": "champion"},
            now=lambda: NOW,
        )
        first = source.list_shortlist({"limit": 5, "policy": "selective"})
        second = source.list_shortlist({"limit": 5, "policy": "selective"})
        assert first == second
        options, values = gateway.calls[0]
        assert options == {"limit": 5, "days": 30, "policy": "selective"}
        assert values["actor"] == "hermes"
        assert values["excluded_job_keys"] == [("ashby", "job-one")]
        assert gateway.calls[0][1]["idempotency_key"] == gateway.calls[1][1]["idempotency_key"]


def test_hermes_proposals_create_exact_dashboard_approvals_only() -> None:
    proposal_now = datetime.now(timezone.utc) + timedelta(days=1)
    slot_start = proposal_now + timedelta(days=2)
    slot_end = slot_start + timedelta(minutes=30)

    class Availability:
        def propose_slots(self, *, now, duration_minutes):
            assert now == proposal_now and duration_minutes == 30
            return (
                InterviewSlot(
                    slot_start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    slot_end.isoformat(timespec="seconds").replace("+00:00", "Z"),
                ),
            )

    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "ledger.db")
        application_id = start(ledger, "proposal")
        excerpt = "Tuesday works for me."
        evidence = ledger.record_mail_evidence(
            {
                "account_id": "outlook-personal",
                "immutable_message_id": "immutable-mail-one",
                "sender": "recruiter@example.test",
                "subject": "Interview",
                "received_at": "2026-09-02T15:00:00Z",
                "body_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                "excerpt": excerpt,
            },
            MutationContext("evidence-one", "system", "test"),
        )["evidence"]
        event_proposal = ledger.create_event_proposal(
            EventProposalInput(
                evidence_id=evidence["evidence_id"],
                proposed_application_id=application_id,
                event_type=ApplicationEventType.RECRUITER_CONTACT,
                producer_kind=ProducerKind.RULE,
                producer_version="test-rule-v1",
                confidence=1.0,
                candidate_application_ids=[application_id],
                evidence_quote=excerpt,
                span_start=0,
                span_end=len(excerpt),
                payload={},
                dedupe_key="reply-evidence-link",
            ),
            MutationContext("reply-evidence-proposal", "system", "test"),
        )["proposal"]
        ledger.decide_event_proposal(
            event_proposal["proposal_id"],
            "accepted",
            application_id,
            "verified reply thread",
            MutationContext("reply-evidence-accepted", "user", "test"),
        )
        proposals = LedgerProposalSource(
            ledger,
            account_id="outlook-personal",
            availability=Availability(),  # type: ignore[arg-type]
            now=lambda: proposal_now,
        )
        reply = proposals.propose_reply(
            {
                "application_id": application_id,
                "evidence_id": evidence["evidence_id"],
                "body": "Tuesday works.",
                "idempotency_key": "reply-one",
            }
        )["action"]
        assert reply["status"] == "pending"
        assert ledger.get_action(reply["action_id"])["payload"] == {
            "message_id": "immutable-mail-one",
            "body": "Tuesday works.",
        }
        hold = proposals.propose_interview_slots(
            {
                "application_id": application_id,
                "duration_minutes": 30,
                "idempotency_key": "hold-one",
            }
        )["action"]
        assert hold["kind"] == "calendar_tentative_hold"
        assert all(item["status"] == "pending" for item in ledger.list_actions())

        other_application_id = start(ledger, "other-reply")
        for source, rejected_application in (
            (proposals, other_application_id),
            (
                LedgerProposalSource(
                    ledger, account_id="other-outlook-account", now=lambda: proposal_now
                ),
                application_id,
            ),
        ):
            try:
                source.propose_reply(
                    {
                        "application_id": rejected_application,
                        "evidence_id": evidence["evidence_id"],
                        "body": "This must not be proposed.",
                        "idempotency_key": "rejected-" + rejected_application,
                    }
                )
            except ContractError:
                pass
            else:
                raise AssertionError("reply evidence crossed its application/account binding")


class Publisher:
    def __init__(self) -> None:
        self.intents = []

    def publish(self, intent):
        self.intents.append(intent)
        return {"created": True, "suppressed": False}


def test_shortlist_and_lifecycle_alerts_are_signal_only() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.recorded = []

        def preview_shortlist(self, options, **kwargs):
            assert options["limit"] == 20
            assert kwargs["excluded_job_keys"] == [("ashby", "job-alerts")]
            return {
                "model": {"ready": True},
                "options": options,
                "recommendations": [
                    {
                        "ats": "ashby",
                        "id": "platform-one",
                        "title": "Platform Engineer",
                        "company": "Signal Co",
                    },
                    {
                        "ats": "lever",
                        "id": "infra-one",
                        "title": "Infra Engineer",
                        "company": "Useful Co",
                    },
                ],
            }

        def notification_exposed_job_keys(self, _workflow_ids, _job_keys):
            return set()

        def record_notification_shortlist(self, result, *, workflow_id):
            self.recorded.append((result, workflow_id))
            return {**result, "session_id": "session-one"}

    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "ledger.db")
        application_id = start(ledger, "alerts")
        publisher = Publisher()
        gateway = Gateway()
        evaluator = ShortlistNotificationEvaluator(
            gateway,  # type: ignore[arg-type]
            ledger,
            publisher,  # type: ignore[arg-type]
            options={"limit": 20, "days": 30, "policy": "champion"},
            enabled=True,
            minimum_jobs=2,
            now=lambda: NOW,
        )
        context = TaskContext(
            "work-one", "notification.shortlist_evaluate", 1,
            "2026-09-02T16:00:00Z", lambda: True,
            workflow_id="workflow-one",
        )
        result = evaluator({}, context)
        assert result["notification_created"] and len(publisher.intents) == 1
        assert result["session_id"] == "session-one"
        assert gateway.recorded[0][1] == "workflow-one"
        assert publisher.intents[0].topic == "shortlist.ready"
        assert publisher.intents[0].context == {"workflow": "workflow-one"}
        assert "Open the dashboard" in publisher.intents[0].body
        assert "session" not in publisher.intents[0].body.casefold()

        outbox_context = OutboxContext(
            "outbox-one", "notification.application_event", "event-one", 1,
            lambda: True,
        )
        handler = ApplicationEventNotificationHandler(
            ledger, publisher  # type: ignore[arg-type]
        )
        update = handler(
            {"application_id": application_id, "event_type": "interview_requested"},
            outbox_context,
        )
        assert update["created"]
        assert publisher.intents[-1].topic == "application.interview_requested"
        assert handler(
            {"application_id": application_id, "event_type": "interview_completed"},
            outbox_context,
        )["suppressed"]


def test_shortlist_alerts_require_unseen_jobs_and_cooldown_includes_boundary() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.next_key = ("ashby", "job-one")
            self.workflows = {}
            self.calls = 0
            self.record_calls = 0

        def preview_shortlist(self, options, **_kwargs):
            self.calls += 1
            return {
                "options": options,
                "recommendations": [
                    {
                        "ats": self.next_key[0],
                        "id": self.next_key[1],
                        "title": "Platform Engineer",
                        "company": "Signal Co",
                    }
                ],
            }

        def notification_exposed_job_keys(self, workflow_ids, job_keys):
            prior = set()
            for workflow_id in workflow_ids:
                prior.update(self.workflows.get(workflow_id, set()))
            return prior.intersection(job_keys)

        def record_notification_shortlist(self, result, *, workflow_id):
            self.record_calls += 1
            self.workflows[workflow_id] = {
                (item["ats"], item["id"])
                for item in result["recommendations"]
            }
            return {**result, "session_id": "session-" + str(self.record_calls)}

    with tempfile.TemporaryDirectory() as directory:
        ledger = JobSearchLedger(Path(directory) / "ledger.db")
        gateway = Gateway()
        current = [datetime.now(timezone.utc)]
        publisher = DurableNotificationPublisher(ledger, now=lambda: current[0])
        evaluator = ShortlistNotificationEvaluator(
            gateway,  # type: ignore[arg-type]
            ledger,
            publisher,
            options={"limit": 20, "days": 30, "policy": "champion"},
            enabled=True,
            minimum_jobs=1,
            cooldown_minutes=240,
            now=lambda: current[0],
        )

        def evaluate(number: int):
            return evaluator(
                {},
                TaskContext(
                    f"work-{number}",
                    "notification.shortlist_evaluate",
                    1,
                    current[0].isoformat(),
                    lambda: True,
                    workflow_id=f"workflow-{number}",
                ),
            )

        assert evaluate(1)["notification_created"]
        latest = ledger.get_shortlist_notification_state()["latest_created_at"]
        current[0] = datetime.fromisoformat(str(latest).replace("Z", "+00:00")) + timedelta(
            minutes=240
        )
        gateway.next_key = ("ashby", "job-two")
        assert evaluate(2)["reason"] == "cooldown"
        assert gateway.calls == 1

        current[0] += timedelta(seconds=1)
        assert evaluate(3)["notification_created"]
        assert len(ledger.list_notification_outbox()) == 2

        latest = ledger.get_shortlist_notification_state()["latest_created_at"]
        current[0] = datetime.fromisoformat(str(latest).replace("Z", "+00:00")) + timedelta(
            minutes=240, seconds=1
        )
        repeated = evaluate(4)
        assert repeated["reason"] == "below_minimum"
        assert repeated["jobs"] == 0
        assert gateway.record_calls == 2
        assert len(ledger.list_notification_outbox()) == 2


def test_due_reminder_is_durably_queued_once_then_completed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        reminder_now = datetime.now(timezone.utc) + timedelta(days=1)
        ledger = JobSearchLedger(Path(directory) / "ledger.db")
        application_id = start(ledger, "reminder")
        reminder = ledger.create_reminder(
            {
                "application_id": application_id,
                "due_at": (reminder_now + timedelta(minutes=5)).isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "note": "Prepare examples for the interview",
            },
            MutationContext("create-reminder", "hermes", "test"),
        )["reminder"]
        publisher = Publisher()
        handler = ReminderNotificationHandler(
            ledger,
            publisher,  # type: ignore[arg-type]
            now=lambda: reminder_now + timedelta(minutes=6),
        )
        context = TaskContext(
            "reminder-work", "notification.reminders_due", 1,
            "2026-09-02T16:06:00Z", lambda: True,
        )
        assert handler({}, context)["queued"] == 1
        assert publisher.intents[0].source_id == reminder["reminder_id"]
        assert ledger.list_reminders(("completed",))[0]["reminder_id"] == reminder["reminder_id"]
        assert handler({}, context)["due"] == 0


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search integration tests)")


if __name__ == "__main__":
    main()
