#!/usr/bin/env python3
"""Offline integration checks for secure mail workers and local reminders."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from job_search.contracts import (
    JobSnapshot,
    MutationContext,
    RecommendationProvenance,
)
from job_search.db import connect
from job_search.mail.attachments import ExtractedAttachment, ICS_MIME
from job_search.mail.runtime import build_secure_mail_ingestor
from job_search.notifications import DurableNotificationPublisher
from job_search.runtime import RuntimeConfigV1, build_runtime
from job_search.service import JobSearchLedger
from job_search.sync import MailboxSyncResult, ProcessingResult
from job_search.worker import (
    DueLocalReminderTaskHandler,
    OutlookMailTaskHandler,
    TaskContext,
)


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-09-02T12:00:00Z"


def context(key: str, actor: str = "system") -> MutationContext:
    return MutationContext(key, actor, "mail_runtime_test")


def task_context() -> TaskContext:
    return TaskContext(
        "mail-work-1", "outlook.mail.sync", 1, STAMP, lambda: True
    )


def due_reminder(ledger: JobSearchLedger, path: Path, *, suffix: str = "1") -> str:
    application_id = str(
        ledger.start_application(
            JobSnapshot(
                "greenhouse",
                "job-" + suffix,
                "family-" + suffix,
                "Platform Engineer",
                "Example Labs",
                "example",
                "https://example.test/jobs/" + suffix,
            ),
            RecommendationProvenance(),
            context("start-" + suffix, "user"),
        )["application"]["application_id"]
    )
    archive_id = "archive-" + suffix
    proposal_id = "temporal-" + suffix
    reminder_id = "local-reminder-" + suffix
    with connect(path) as con:
        con.execute(
            "INSERT INTO mail_archive "
            "(archive_id,account_id,immutable_message_id,key_id,nonce,ciphertext,"
            "aad_sha256,sanitized_sha256,sanitized_chars,truncated,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
            (
                archive_id,
                "outlook-personal",
                "message-" + suffix,
                "key-1",
                b"N" * 12,
                b"C" * 16,
                "a" * 64,
                "b" * 64,
                20,
                STAMP,
                STAMP,
            ),
        )
        con.execute(
            "INSERT INTO temporal_proposals "
            "(temporal_proposal_id,dedupe_key,archive_id,application_id,kind,"
            "starts_at,ends_at,due_at,time_zone,confidence,evidence_quote,span_start,"
            "span_end,source_sha256,producer_version,status,created_at,decided_at) "
            "VALUES (?,?,?,?,? ,NULL,NULL,?,?,?,?,?,?,?,?, 'accepted',?,?)",
            (
                proposal_id,
                "temporal-dedupe-" + suffix,
                archive_id,
                application_id,
                "deadline",
                STAMP,
                "America/Chicago",
                0.9,
                "submit by noon",
                0,
                14,
                "b" * 64,
                "temporal-v1",
                STAMP,
                STAMP,
            ),
        )
        con.execute(
            "INSERT INTO local_reminders "
            "(reminder_id,temporal_proposal_id,application_id,kind,due_at,status,created_at) "
            "VALUES (?,?,?,'deadline',?,'pending',?)",
            (reminder_id, proposal_id, application_id, STAMP, STAMP),
        )
    return reminder_id


def test_all_history_handler_uses_query_v2_and_returns_counts_not_task_secrets() -> None:
    calls = []

    class Coordinator:
        def sync_all_history(
            self, account_id, max_folders, max_pages_per_folder, heartbeat
        ):
            assert heartbeat()
            calls.append(("sync", account_id, max_folders, max_pages_per_folder))
            return MailboxSyncResult(8, 3, 5, 7, 9)

        def process_pending(self, **options):
            assert options["heartbeat"]()
            calls.append(
                (
                    "process",
                    options["query_version"],
                    options["limit"],
                    options["transient_attempt"],
                )
            )
            return ProcessingResult(4, 2, 0, 1, 0)

    handler = OutlookMailTaskHandler(
        Coordinator(), account_id="outlook-personal", all_history=True
    )
    result = handler(
        {"access_token": "graph-secret", "raw_body": "private body"},
        task_context(),
    )
    assert calls == [
        ("sync", "outlook-personal", 4096, 100),
        ("process", 2, 100, 1),
    ]
    encoded = json.dumps(result, sort_keys=True)
    assert "graph-secret" not in encoded and "private body" not in encoded
    assert result["sync"]["excluded"] == 3
    assert result["processing"]["processed"] == 4


def test_secure_builder_archives_eligible_nonrecruiting_attachments() -> None:
    class Archive:
        def __init__(self):
            self.messages = []
            self.attachments = []

        def archive_message(self, **values):
            self.messages.append(values)
            return {"archive": {"archive_id": "archive-1"}}

        def archive_attachment_text(self, **values):
            self.attachments.append(values)
            return {
                "created": True,
                "attachment": {"attachment_record_id": "attachment-record-1"},
            }

    class Attachments:
        def acquire(self, message_id):
            assert message_id == "message-1"
            return (
                ExtractedAttachment(
                    "attachment-1",
                    ICS_MIME,
                    64,
                    "c" * 64,
                    "BEGIN UNTRUSTED EMAIL\ncalendar\nEND UNTRUSTED EMAIL",
                    False,
                ),
            )

    archive = Archive()
    ingestor = build_secure_mail_ingestor(
        object(), object(), archive=archive, attachments=Attachments()
    )
    result = ingestor.ingest(
        account_id="outlook-personal",
        immutable_message_id="message-1",
        subject="Account statement",
        body="private historical body",
        body_kind="text",
        received_at=STAMP,
        candidates=(),
        has_attachments=True,
        analyze_temporal=False,
    )
    assert result.attachments_archived == 1 and result.temporal_proposals == 0
    assert len(archive.messages) == len(archive.attachments) == 1


def test_due_reminder_is_durably_published_then_completed_without_duplicates() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        ledger = JobSearchLedger(path)
        reminder_id = due_reminder(ledger, path)
        publisher = DurableNotificationPublisher(ledger, now=lambda: NOW)
        handler = DueLocalReminderTaskHandler(
            ledger, publisher, now_provider=lambda: NOW
        )
        first = handler({}, task_context())
        second = handler({}, task_context())
        assert first == {
            "due": 1,
            "published": 1,
            "completed": 1,
            "suppressed": 0,
        }
        assert second["due"] == 0
        with connect(path) as con:
            reminder = con.execute(
                "SELECT status FROM local_reminders WHERE reminder_id=?",
                (reminder_id,),
            ).fetchone()
            notifications = con.execute(
                "SELECT topic,title,body,status FROM notification_outbox"
            ).fetchall()
        assert reminder["status"] == "completed"
        assert len(notifications) == 1
        assert notifications[0]["topic"] == "reminder.due"
        assert notifications[0]["status"] == "pending"
        assert "Example Labs" in notifications[0]["body"]


def test_outbox_insert_survives_completion_failure_and_replay_finishes_reminder() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        ledger = JobSearchLedger(path)
        reminder_id = due_reminder(ledger, path, suffix="crash")

        class FailCompletionOnce:
            def __init__(self):
                self.failed = False

            def __getattr__(self, name):
                return getattr(ledger, name)

            def complete_local_reminder(self, *args, **kwargs):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("simulated crash boundary")
                return ledger.complete_local_reminder(*args, **kwargs)

        service = FailCompletionOnce()
        publisher = DurableNotificationPublisher(ledger, now=lambda: NOW)
        handler = DueLocalReminderTaskHandler(
            service, publisher, now_provider=lambda: NOW
        )
        try:
            handler({}, task_context())
        except RuntimeError as exc:
            assert "crash boundary" in str(exc)
        else:
            raise AssertionError("completion failure did not escape for worker retry")
        with connect(path) as con:
            assert con.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
            assert con.execute(
                "SELECT status FROM local_reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()[0] == "pending"
        replay = handler({}, task_context())
        assert replay["completed"] == 1
        with connect(path) as con:
            assert con.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
            assert con.execute(
                "SELECT status FROM local_reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()[0] == "completed"


def test_core_runtime_registers_reminders_on_the_existing_five_minute_tick() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = RuntimeConfigV1.defaults(Path(directory))
        runtime = build_runtime(
            config,
            lane="core",
            now_provider=lambda: NOW,
            base_environment={},
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        )
        assert isinstance(
            runtime.worker.task_handlers["system.worker_tick"],
            DueLocalReminderTaskHandler,
        )
        assert runtime.worker.task_handlers["outlook.reminders.publish"] is runtime.worker.task_handlers[
            "system.worker_tick"
        ]
        assert not runtime.worker.task_handlers[
            "system.worker_tick"
        ].publisher.policy.enabled_topics

        configured = RuntimeConfigV1.from_mapping(
            {
                "version": 1,
                "project_root": str(Path(directory)),
                "hermes_executable": "/opt/hermes/bin/hermes",
                "hermes_telegram_target": "ntfy",
            }
        )
        enabled_runtime = build_runtime(
            configured,
            lane="core",
            now_provider=lambda: NOW,
            base_environment={},
            max_work_per_tick=1,
            max_outbox_per_tick=0,
        )
        assert "reminder.due" in enabled_runtime.worker.task_handlers[
            "system.worker_tick"
        ].publisher.policy.enabled_topics


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} mail runtime tests)")


if __name__ == "__main__":
    main()
