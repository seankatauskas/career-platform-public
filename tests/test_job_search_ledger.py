#!/usr/bin/env python3
"""Offline focused tests for the deterministic job-search event ledger."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_search.contracts import (
    ActionKind,
    ActionProposalInput,
    ApplicationEventType,
    ConflictError,
    ContractError,
    EventInput,
    EventProposalInput,
    JobSnapshot,
    MutationContext,
    ProducerKind,
    RecommendationProvenance,
    payload_sha256,
)
from job_search.db import MIGRATIONS, connect
from job_search.service import JobSearchLedger
from job_search.store import LedgerStore


def stamp(offset_seconds: int = 0) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def context(key: str, actor: str = "user", source: str = "dashboard") -> MutationContext:
    return MutationContext(key, actor, source)


def snapshot(job_id: str = "job-1") -> JobSnapshot:
    return JobSnapshot(
        ats="ashby",
        job_id=job_id,
        family_id="family-1",
        title="Platform Engineer",
        employer="Acme",
        company_slug="acme",
        job_url=f"https://example.test/jobs/{job_id}",
    )


def provenance() -> RecommendationProvenance:
    return RecommendationProvenance(
        session_id="session-1",
        impression_id=17,
        model_run_id="model-1",
        policy_id="selective",
        rank=3,
        semantic_score=0.8,
        ranking_score=0.7,
    )


def make_service(directory: str) -> tuple[Path, JobSearchLedger]:
    path = Path(directory) / "job-search.db"
    return path, JobSearchLedger(path)


def start(service: JobSearchLedger, job_id: str = "job-1", key: str = "start-1"):
    return service.start_application(snapshot(job_id), provenance(), context(key))


def test_second_connection_preserves_existing_wal_transaction_lock() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ledger.db"
        first = connect(path)
        second = None
        try:
            first.execute("CREATE TABLE lock_probe (value INTEGER)")
            first.commit()
            first.execute("BEGIN IMMEDIATE")
            # An exclusive external writer must remain blocked while the first
            # connection owns its transaction, including after another connect.
            probe = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0)
try:
    connection.execute('PRAGMA locking_mode=EXCLUSIVE')
    connection.execute('BEGIN EXCLUSIVE')
    connection.execute('SELECT * FROM lock_probe').fetchall()
except sqlite3.OperationalError as error:
    if str(error) != 'database is locked':
        raise
    print('blocked')
else:
    print('acquired')
finally:
    connection.close()
"""
            for open_second in (False, True):
                if open_second:
                    second = connect(path)
                result = subprocess.run(
                    [sys.executable, "-c", probe, str(path)],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                assert result.stdout.strip() == "blocked", result.stdout
            first.rollback()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert not list(path.parent.glob(".sqlite-create-*"))
        finally:
            if second is not None:
                second.close()
            first.close()


def test_database_is_private_configured_and_migrated() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, _service = make_service(directory)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        with connect(path) as con:
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert con.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            migration = con.execute(
                "SELECT version,name,length(checksum) FROM schema_migrations"
            ).fetchone()
            assert tuple(migration) == (1, "initial_job_search_ledger", 64)
            assert con.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(
                MIGRATIONS
            )
            assert con.execute("PRAGMA user_version").fetchone()[0] == MIGRATIONS[-1][0]
            evidence_fks = {
                (row[2], row[3], row[4])
                for row in con.execute("PRAGMA foreign_key_list(event_proposals)")
            }
            assert ("mail_evidence", "evidence_id", "evidence_id") in evidence_fks


def test_migration_reasserts_latest_version_after_a_reserved_gap_is_filled() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, _service = make_service(directory)
        with sqlite3.connect(path) as con:
            con.execute("PRAGMA user_version = 5")

        JobSearchLedger(path)

        with sqlite3.connect(path) as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == MIGRATIONS[-1][0]


def test_start_is_idempotent_by_command_and_concrete_job() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        first = start(service)
        replay = start(service)
        duplicate = start(service, key="start-same-job-again")
        assert first == replay
        assert first["created"] is True
        assert duplicate["created"] is False
        assert duplicate["application"]["application_id"] == first["application"]["application_id"]
        with sqlite3.connect(path) as con:
            assert con.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
            assert con.execute("SELECT count(*) FROM application_events").fetchone()[0] == 1

        raised = False
        try:
            service.start_application(snapshot("other-job"), provenance(), context("start-1"))
        except ConflictError:
            raised = True
        assert raised, "an idempotency key cannot be reused for another request"


def test_submission_and_feedback_outbox_commit_once() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        submitted_at = stamp(-60)
        first = service.record_submission(
            application_id, submitted_at, context("submit-1"), {"observed_by": "user"}
        )
        replay = service.record_submission(
            application_id, submitted_at, context("submit-1"), {"observed_by": "user"}
        )
        assert first == replay
        assert first["application"]["current_phase"] == "awaiting_confirmation"
        outbox = service.list_outbox()
        assert len(outbox) == 1
        assert outbox[0]["topic"] == "recommendation.applied"
        assert outbox[0]["source_event_id"] == first["event"]["event_id"]
        assert outbox[0]["payload"]["impression_id"] == 17

        service.record_event(
            EventInput(
                application_id,
                ApplicationEventType.SUBMISSION_CONFIRMED,
                stamp(),
                {},
                "mail-confirmation-1",
                context("confirm-1", "rule", "outlook_rule"),
            )
        )
        with sqlite3.connect(path) as con:
            assert con.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == 1


def test_submission_snapshot_is_resolved_under_the_application_writer_lock() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        lock_observed = []

        def snapshot() -> dict:
            contender = sqlite3.connect(path, timeout=0)
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).lower()
                lock_observed.append(True)
            else:
                contender.rollback()
                raise AssertionError("submission snapshot ran outside the writer lock")
            finally:
                contender.close()
            return {
                "resume": {
                    "decision": "selected",
                    "artifact_id": "artifact-1",
                    "evaluation_id": "evaluation-1",
                    "comparison_kind": "standard",
                }
            }

        first = service.record_submission(
            application_id,
            stamp(),
            context("submit-with-snapshot"),
            payload_factory=snapshot,
            request_payload={"resume_decision": "selected"},
        )
        replay = service.record_submission(
            application_id,
            first["event"]["occurred_at"],
            context("submit-with-snapshot"),
            payload_factory=lambda: (_ for _ in ()).throw(
                AssertionError("idempotent replay reevaluated the resume selection")
            ),
            request_payload={"resume_decision": "selected"},
        )

        assert lock_observed == [True]
        assert replay == first
        assert first["event"]["payload"]["resume"]["artifact_id"] == "artifact-1"


def test_failure_between_event_and_outbox_rolls_back_for_safe_retry() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        original = LedgerStore._insert_feedback_outbox

        def fail(*_args):
            raise RuntimeError("simulated crash")

        LedgerStore._insert_feedback_outbox = staticmethod(fail)
        try:
            raised = False
            try:
                service.record_submission(
                    application_id, stamp(), context("crash-submit")
                )
            except RuntimeError:
                raised = True
            assert raised
        finally:
            LedgerStore._insert_feedback_outbox = staticmethod(original)

        with sqlite3.connect(path) as con:
            assert con.execute(
                "SELECT count(*) FROM application_events WHERE event_type='submission_observed'"
            ).fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == 0
            assert con.execute(
                "SELECT count(*) FROM command_results WHERE idempotency_key='crash-submit'"
            ).fetchone()[0] == 0

        result = service.record_submission(
            application_id, stamp(), context("crash-submit")
        )
        assert result["created"] is True
        assert len(service.list_outbox()) == 1


def test_signal_lifecycle_event_enqueues_exactly_one_notification_outbox_item() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        non_signal = EventInput(
            application_id,
            ApplicationEventType.SUBMISSION_CONFIRMED,
            stamp(),
            {},
            "confirmation-no-alert",
            context("confirmation-no-alert", "rule", "outlook_rule"),
        )
        service.record_event(non_signal)
        assert service.list_outbox() == []

        signal = EventInput(
            application_id,
            ApplicationEventType.RECRUITER_CONTACT,
            stamp(1),
            {},
            "recruiter-alert",
            context("recruiter-alert", "rule", "outlook_rule"),
        )
        first = service.record_event(signal)
        replay = service.record_event(signal)
        assert first == replay
        outbox = service.list_outbox()
        assert len(outbox) == 1
        assert outbox[0]["topic"] == "notification.application_event"
        assert outbox[0]["source_event_id"] == first["event"]["event_id"]
        assert outbox[0]["payload"] == {
            "application_id": application_id,
            "event_type": "recruiter_contact",
            "source_event_id": first["event"]["event_id"],
        }


def test_reducer_prevents_regression_and_terminal_conflicts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        service.record_event(
            EventInput(
                application_id,
                ApplicationEventType.INTERVIEW_SCHEDULED,
                stamp(),
                {"round": "first"},
                "interview-1",
                context("interview-1", "rule", "outlook_rule"),
            )
        )
        late_submission = service.record_event(
            EventInput(
                application_id,
                ApplicationEventType.SUBMISSION_OBSERVED,
                stamp(-3600),
                {},
                "late-submission",
                context("late-submission", "user", "dashboard"),
            )
        )
        assert late_submission["application"]["current_phase"] == "interviewing"

        corrected = service.record_event(
            EventInput(
                application_id,
                ApplicationEventType.MANUAL_CORRECTION,
                stamp(1),
                {"target_phase": "active", "target_outcome": None, "reason": "wrong stage"},
                "correction-1",
                context("correction-1"),
            )
        )
        assert corrected["application"]["current_phase"] == "active"
        rejected = service.record_event(
            EventInput(
                application_id,
                ApplicationEventType.REJECTION_RECEIVED,
                stamp(2),
                {},
                "rejection-1",
                context("rejection-1", "user", "review"),
            )
        )
        assert rejected["application"]["terminal_outcome"] == "rejected"
        after_terminal = service.record_event(
            EventInput(
                application_id,
                ApplicationEventType.RECRUITER_CONTACT,
                stamp(3),
                {},
                "late-contact",
                context("late-contact", "rule", "outlook_rule"),
            )
        )
        assert after_terminal["application"]["terminal_outcome"] == "rejected"

        raised = False
        try:
            service.record_event(
                EventInput(
                    application_id,
                    ApplicationEventType.OFFER_ACCEPTED,
                    stamp(4),
                    {},
                    "conflicting-acceptance",
                    context("conflicting-acceptance"),
                )
            )
        except ConflictError:
            raised = True
        assert raised


def test_manual_correction_is_user_only() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        raised = False
        try:
            service.record_event(
                EventInput(
                    application_id,
                    ApplicationEventType.MANUAL_CORRECTION,
                    stamp(),
                    {"target_phase": "active", "reason": "model request"},
                    "bad-correction",
                    context("bad-correction", "model", "classifier"),
                )
            )
        except ContractError:
            raised = True
        assert raised


def test_events_and_application_roots_are_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        with sqlite3.connect(path) as con:
            for statement in (
                "UPDATE application_events SET payload_json='{}'",
                "DELETE FROM application_events",
                "UPDATE applications SET title_snapshot='tampered'",
            ):
                raised = False
                try:
                    con.execute(statement)
                except sqlite3.IntegrityError:
                    raised = True
                assert raised, statement
                con.rollback()
            assert con.execute(
                "SELECT title_snapshot FROM applications WHERE application_id=?",
                (application_id,),
            ).fetchone()[0] == "Platform Engineer"


def test_projection_verification_and_rebuild() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        assert service.verify_projections() == []
        with sqlite3.connect(path) as con:
            con.execute(
                "UPDATE applications SET current_phase='offer',projection_sha256='bad' "
                "WHERE application_id=?",
                (application_id,),
            )
        failures = service.verify_projections()
        assert failures and failures[0]["application_id"] == application_id
        rebuilt = service.rebuild_projections(
            dry_run=False, context=context("rebuild-1", "system", "maintenance")
        )
        assert rebuilt == [application_id]
        assert service.verify_projections() == []


def test_event_proposal_review_applies_one_event() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        excerpt = "0123456789schedule a first interview"
        evidence_id = service.record_mail_evidence(
            {
                "account_id": "outlook-personal",
                "immutable_message_id": "message-1",
                "sender": "recruiter@example.test",
                "subject": "Interview",
                "received_at": stamp(),
                "body_sha256": "a" * 64,
                "excerpt": excerpt,
            },
            context("evidence-1", "system", "outlook_sync"),
        )["evidence"]["evidence_id"]
        proposal = EventProposalInput(
            evidence_id=evidence_id,
            proposed_application_id=application_id,
            event_type=ApplicationEventType.INTERVIEW_REQUESTED,
            producer_kind=ProducerKind.MODEL,
            producer_version="qwen-v1",
            confidence=0.995,
            candidate_application_ids=[application_id],
            evidence_quote="schedule a first interview",
            span_start=10,
            span_end=36,
            payload={"occurred_at": stamp()},
            dedupe_key="message-1-interview-requested",
        )
        created = service.create_event_proposal(
            proposal, context("proposal-1", "model", "classifier")
        )
        assert created["proposal"]["status"] == "pending"
        assert service.list_attention_items()[0]["kind"] == "event_proposal"
        decided = service.decide_event_proposal(
            created["proposal"]["proposal_id"],
            "accepted",
            application_id,
            "correct match",
            context("proposal-decision-1", "user", "review"),
        )
        assert decided["event"]["event_type"] == "interview_requested"
        assert service.get_application_timeline(application_id)["application"]["current_phase"] == "interviewing"
        with sqlite3.connect(path) as con:
            assert con.execute(
                "SELECT count(*) FROM application_events WHERE event_type='interview_requested'"
            ).fetchone()[0] == 1


def test_event_proposal_requires_stored_exact_evidence_span() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        evidence_id = service.record_mail_evidence(
            {
                "account_id": "outlook-personal",
                "immutable_message_id": "message-binding",
                "sender": "recruiter@example.test",
                "subject": "Interview",
                "received_at": stamp(),
                "body_sha256": "b" * 64,
                "excerpt": "Interview requested for Platform Engineer",
            },
            context("evidence-binding", "system", "outlook_sync"),
        )["evidence"]["evidence_id"]

        def proposal(bound_evidence_id: str, quote: str) -> EventProposalInput:
            return EventProposalInput(
                evidence_id=bound_evidence_id,
                proposed_application_id=application_id,
                event_type=ApplicationEventType.INTERVIEW_REQUESTED,
                producer_kind=ProducerKind.MODEL,
                producer_version="qwen-v1",
                confidence=0.995,
                candidate_application_ids=[application_id],
                evidence_quote=quote,
                span_start=0,
                span_end=len(quote),
                payload={},
                dedupe_key="binding-" + bound_evidence_id,
            )

        for key, value in (
            ("missing-evidence", proposal("not-recorded", "Interview requested")),
            ("mismatched-evidence", proposal(evidence_id, "Forged evidence text")),
        ):
            try:
                service.create_event_proposal(
                    value, context(key, "model", "classifier")
                )
            except ContractError:
                pass
            else:
                raise AssertionError("proposal accepted evidence not bound to its source")


def test_action_approval_is_exact_and_expires_in_fifteen_minutes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        application_id = start(service)["application"]["application_id"]
        payload = {"message_id": "mail-1", "body": "Tuesday works for me."}
        action = service.create_action_proposal(
            ActionProposalInput(
                ActionKind.OUTLOOK_REPLY_DRAFT,
                application_id,
                "outlook-personal",
                payload,
                stamp(3600),
            ),
            context("action-1", "system", "scheduling"),
        )["action"]
        raised = False
        try:
            service.decide_action(
                action["action_id"], False, "0" * 64, context("wrong-hash")
            )
        except ConflictError:
            raised = True
        assert raised
        approval = service.decide_action(
            action["action_id"],
            True,
            payload_sha256(payload),
            context("approve-1"),
        )
        assert approval["decision"] == "approve"
        with sqlite3.connect(path) as con:
            created_at, expires_at = con.execute(
                "SELECT created_at,expires_at FROM action_approval_decisions "
                "WHERE decision_id=?",
                (approval["decision_id"],),
            ).fetchone()
        approved_for = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) - (
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        )
        assert approved_for == timedelta(seconds=900)


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search ledger tests)")


if __name__ == "__main__":
    main()
