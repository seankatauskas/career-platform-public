#!/usr/bin/env python3
"""Offline integration checks for Outlook delta state and mail proposals."""

from __future__ import annotations

import tempfile
from pathlib import Path

from job_search.contracts import (
    ApplicationEventType,
    JobSnapshot,
    MailChange,
    MailDeltaPage,
    MutationContext,
    RecommendationProvenance,
    RetryDecision,
)
from job_search.mail import EvaluationObservation, evaluate_observations
from job_search.inference import InferenceTransportError
from job_search.outlook.cursor import CursorConflict
from job_search.outlook import GraphHttpError
from job_search.outlook.mail import MailBackfillPage
from job_search.outlook.state import SQLiteOutlookState
from job_search.service import JobSearchLedger
from job_search.sync import OutlookMailCoordinator, authenticated_sender


class FakeMail:
    def __init__(self):
        self.backfills = {}
        self.deltas = {}
        self.bodies = {}

    @staticmethod
    def backfill_url(*, folder="inbox", now=None, days=90):
        return f"backfill:{folder}"

    @staticmethod
    def initial_delta_url(*, folder="inbox", now=None, days=90):
        return f"delta:{folder}"

    def read_backfill_page(self, url):
        return self.backfills[url]

    def read_delta_page(self, url):
        return self.deltas[url]

    def read_message_body(self, immutable_message_id):
        value = self.bodies[immutable_message_id]
        if isinstance(value, Exception):
            raise value
        return value


def change(message_id="message-1"):
    return MailChange(
        immutable_id=message_id,
        removed=False,
        conversation_id="conversation-1",
        internet_message_id="internet-1",
        sender_address="notifications@greenhouse.io",
        subject="Thank you for applying",
        received_at="2026-09-01T12:00:00Z",
        modified_at="2026-09-01T12:00:01Z",
    )


def test_sender_auth_uses_only_first_microsoft_stamped_result():
    sender = "notifications@greenhouse.io"
    trusted = {
        "internetMessageHeaders": [
            {
                "name": "Authentication-Results",
                "value": (
                    "spf=pass smtp.mailfrom=greenhouse.io; dkim=pass; "
                    "dmarc=pass header.from=greenhouse.io; compauth=pass reason=100"
                ),
            }
        ]
    }
    assert authenticated_sender(trusted, sender)
    for prefix, expected in [("mx.microsoft.com 1", True), ("mx.microsoft.com", True),
                             ("mx.microsoft.com.evil.test 1", False), ("evil-mx.microsoft.com 1", False)]:
        versioned = {"internetMessageHeaders":[{"name":"Authentication-Results", "value":
            prefix + "; dmarc=pass header.from=greenhouse.io; compauth=pass reason=100"}]}
        assert authenticated_sender(versioned, sender) is expected


    injected = {
        "internetMessageHeaders": [
            {
                "name": "Authentication-Results",
                "value": (
                    "attacker.example; dmarc=pass header.from=greenhouse.io; "
                    "compauth=pass reason=100"
                ),
            },
            trusted["internetMessageHeaders"][0],
        ]
    }
    assert not authenticated_sender(injected, sender)

    receiver_failed_then_injected_passed = {
        "internetMessageHeaders": [
            {
                "name": "Authentication-Results",
                "value": (
                    "NAM11-DM6.prod.outlook.com; "
                    "dmarc=fail header.from=greenhouse.io; compauth=pass reason=100"
                ),
            },
            {
                "name": "Authentication-Results",
                "value": (
                    "NAM11-DM6.prod.outlook.com; "
                    "dmarc=pass header.from=greenhouse.io; compauth=pass reason=100"
                ),
            },
        ]
    }
    assert not authenticated_sender(receiver_failed_then_injected_passed, sender)


def test_sqlite_cursor_is_optimistic_and_message_replays_are_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        state = SQLiteOutlookState(Path(directory) / "job-search.db")
        original = state.load("personal", "inbox", 1)
        advanced = state.checkpoint(original, "https://graph.microsoft.com/v1.0/next")
        try:
            state.checkpoint(original, "https://graph.microsoft.com/v1.0/stale")
        except CursorConflict:
            pass
        else:
            raise AssertionError("stale cursor update was accepted")
        state.stage_changes("personal", "inbox", [change()])
        state.stage_changes("personal", "inbox", [change()])
        assert len(state.pending_messages()) == 1
        committed = state.commit(advanced, "https://graph.microsoft.com/v1.0/delta")
        assert not committed.needs_backfill and committed.in_flight_next_link is None


def test_stage_requeues_null_safe_changes_and_isolates_query_versions():
    with tempfile.TemporaryDirectory() as directory:
        state = SQLiteOutlookState(Path(directory) / "job-search.db")
        original = change("versioned-message")
        original = MailChange(
            immutable_id=original.immutable_id,
            removed=False,
            sender_address=original.sender_address,
            subject=original.subject,
            received_at=original.received_at,
            modified_at=None,
        )
        state.stage_changes("personal", "inbox", [original], query_version=1)
        state.mark_message(
            "personal", "inbox", original.immutable_id, "processed", query_version=1
        )
        state.stage_changes("personal", "inbox", [original], query_version=1)
        assert state.pending_messages(query_version=1) == []

        modified = MailChange(
            immutable_id=original.immutable_id,
            removed=False,
            sender_address=original.sender_address,
            subject=original.subject,
            received_at=original.received_at,
            modified_at="2026-09-01T12:05:00Z",
        )
        state.stage_changes("personal", "inbox", [modified], query_version=1)
        assert len(state.pending_messages(query_version=1)) == 1
        state.mark_message(
            "personal", "inbox", original.immutable_id, "ignored", query_version=1
        )

        state.stage_changes("personal", "inbox", [modified], query_version=2)
        assert state.pending_messages(query_version=1) == []
        assert len(state.pending_messages(query_version=2)) == 1


def test_initial_backfill_and_delta_commit_only_after_all_pages():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        state = SQLiteOutlookState(path)
        service = JobSearchLedger(path)
        mail = FakeMail()
        mail.backfills = {
            "backfill:inbox": MailBackfillPage((change("old-1"),), "backfill:page-2"),
            "backfill:page-2": MailBackfillPage((change("old-2"),), None),
        }
        mail.deltas = {
            "delta:inbox": MailDeltaPage((change("new-1"),), None, "https://graph.microsoft.com/v1.0/final-delta"),
        }
        result = OutlookMailCoordinator(mail, state, service).sync_folder("personal")
        assert result.pages == 3 and result.staged == 3 and result.cursor_committed
        cursor = state.load("personal", "inbox", 1)
        assert cursor.committed_delta_link.endswith("final-delta")
        assert len(state.pending_messages()) == 3

        replay = OutlookMailCoordinator(mail, state, service).sync_folder(
            "personal", query_version=2
        )
        assert replay.cursor_committed
        assert len(state.pending_messages(query_version=1)) == 3
        assert len(state.pending_messages(query_version=2)) == 3


def test_sync_stops_at_page_boundary_when_worker_lease_is_lost():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        state = SQLiteOutlookState(path)
        service = JobSearchLedger(path)
        mail = FakeMail()
        mail.backfills = {
            "backfill:inbox": MailBackfillPage(
                (change("lease-page"),), "backfill:should-not-run"
            )
        }
        coordinator = OutlookMailCoordinator(mail, state, service)
        try:
            coordinator.sync_folder("personal", heartbeat=lambda: False)
        except RuntimeError as exc:
            assert "lease was lost" in str(exc)
        else:
            raise AssertionError("Outlook sync continued after losing its worker lease")
        assert len(state.pending_messages()) == 1

def test_known_confirmation_becomes_evidence_proposal_and_event_once():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        service = JobSearchLedger(path)
        application = service.start_application(
            JobSnapshot(
                "greenhouse", "42", "family-42", "Platform Engineer",
                "Example Corp", "example", "https://example.test/jobs/42",
            ),
            RecommendationProvenance(),
            MutationContext("start", "user", "dashboard"),
        )["application"]
        service.record_submission(
            application["application_id"],
            "2026-09-01T11:00:00Z",
            MutationContext("submitted", "user", "dashboard"),
        )
        state = SQLiteOutlookState(path)
        state.stage_changes("personal", "inbox", [change()])
        mail = FakeMail()
        mail.bodies["message-1"] = {
            "id": "message-1",
            "conversationId": "conversation-1",
            "sender": {"emailAddress": {"address": "notifications@greenhouse.io"}},
            "subject": "Thank you for applying",
            "receivedDateTime": "2026-09-01T12:00:00Z",
            "internetMessageHeaders": [
                {
                    "name": "Authentication-Results",
                    "value": (
                        "spf=pass smtp.mailfrom=greenhouse.io; dkim=pass; "
                        "dmarc=pass header.from=greenhouse.io; "
                        "compauth=pass reason=100"
                    ),
                }
            ],
            "body": {
                "contentType": "text",
                "content": (
                    "We have received your application for Platform Engineer "
                    "at Example Corp."
                ),
            },
        }
        coordinator = OutlookMailCoordinator(mail, state, service)
        first = coordinator.process_pending()
        second = coordinator.process_pending()
        assert first.processed == 1 and first.proposed == 1 and first.auto_applied == 1
        assert second.processed == 0
        timeline = service.get_application_timeline(application["application_id"])
        assert timeline["application"]["current_phase"] == "active"
        assert [event["event_type"] for event in timeline["events"]].count(
            "submission_confirmed"
        ) == 1
        assert service.list_attention_items() == []


def test_company_only_confirmation_uses_browser_attempt_and_replays_once():
    from datetime import datetime, timedelta, timezone
    from tests.test_browser_tracking import fixture, observation
    with tempfile.TemporaryDirectory() as directory:
        service, tracker, device, _, _ = fixture(directory)
        attempt = observation()
        result = tracker.observe(device, attempt)
        received = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        state = SQLiteOutlookState(service.store.db_path)
        state.stage_changes("personal", "inbox", [change()])
        mail = FakeMail()
        mail.bodies["message-1"] = {
            "id":"message-1", "subject":"Thank you for applying to Acme",
            "sender":{"emailAddress":{"address":"no-reply@us.greenhouse-mail.io"}},
            "receivedDateTime":received,
            "internetMessageHeaders":[{"name":"Authentication-Results", "value":
                "spf=pass; dkim=pass; dmarc=pass header.from=us.greenhouse-mail.io; compauth=pass reason=100"}],
            "body":{"contentType":"text", "content":"Your application has been received."},
        }
        coordinator = OutlookMailCoordinator(mail, state, service)
        candidates, complete = coordinator._candidates("Acme", received_at=received)
        assert complete and candidates[0].submission_attempted_at == attempt['occurred_at']
        assert not candidates[0].submitted_at  # The site redirect may not yet have been seen.
        # Simulate the prior rule's pending proposal for this same email.
        from dataclasses import replace
        create = service.create_event_proposal
        def with_previous_rule(proposal, context):
            create(replace(proposal, confidence=0.99, producer_version="previous-rule",
                           dedupe_key="previous-rule:"+proposal.dedupe_key),
                   MutationContext("previous-rule-proposal", "rule", "outlook_mail"))
            return create(proposal, context)
        service.create_event_proposal = with_previous_rule
        first = coordinator.process_pending()
        assert first.auto_applied == 1 and first.failed == 0
        from job_search.db import connect
        with connect(service.store.db_path) as con:
            assert [r[0] for r in con.execute("SELECT status FROM event_proposals ORDER BY confidence")] == ["superseded", "auto_applied"]
        assert coordinator.process_pending().processed == 0
        timeline = service.get_application_timeline(result['application_id'])
        assert timeline['application']['current_phase'] == 'active'
        assert sum(e['event_type']=='submission_confirmed' for e in timeline['events']) == 1
        service.create_event_proposal = create
        # A later reply has no company/title or recruiting keyword. Retrieve its
        # older application from the accepted conversation, ahead of newer entries.
        for i in range(25):
            other = service.start_application(JobSnapshot("greenhouse", str(9000+i), "", "Other Role",
                "Other Employer "+str(i), "other"+str(i), "https://example.test/jobs/"+str(i)),
                RecommendationProvenance(), MutationContext("other"+str(i), "user", "test"))["application"]
            service.record_submission(other["application_id"], received,
                MutationContext("other-submitted"+str(i), "user", "test"))
        state.stage_changes("personal", "inbox", [replace(change("message-2"),
            subject="Re: Checking in", sender_address="jane@example.test")])
        mail.bodies["message-2"] = {
            "id":"message-2", "conversationId":"conversation-1", "subject":"Re: Checking in",
            "sender":{"emailAddress":{"address":"jane@example.test"}},
            "receivedDateTime":received,
            "body":{"contentType":"text", "content":"Following up on our conversation."},
        }
        class UncertainCorrespondence:
            def classify(self, text, candidates):
                assert candidates[0]['application_id'] == result['application_id']
                assert 'previously linked email conversation' in candidates[0]['match_context']
                quote = 'Following up on our conversation.'
                start = text.index(quote)
                return {'event_type':'recruiter_contact', 'application_id':candidates[0]['application_id'],
                        'confidence':0.65, 'evidence_quote':quote, 'span_start':start,
                        'span_end':start+len(quote), 'payload':{}}
        coordinator.classifier = UncertainCorrespondence()
        followup = coordinator.process_pending()
        assert followup.processed == 1 and followup.proposed == 1 and followup.auto_applied == 0
        attention = service.list_attention_items()
        assert len(attention) == 1 and attention[0]['application_id'] == result['application_id']
        assert attention[0]['detail'] == 'recruiter_contact'
        assert not any(e['event_type']=='recruiter_contact' for e in service.get_application_timeline(result['application_id'])['events'])
        # More than twenty plausible identities must not silently become certain.
        contexts, complete = coordinator._candidates('Other Employer', 'Other Role')
        assert len(contexts) == 20 and not complete


def test_application_verification_mail_is_ignored_without_fetching_body_or_model():
    from dataclasses import replace
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory)/"state.db")
        state = SQLiteOutlookState(service.store.db_path)
        state.stage_changes("personal", "inbox", [replace(change("security-code"),
            subject="Security code for your application to TeleTracking Technologies, Inc.",
            sender_address="no-reply@us.greenhouse-mail.io")], query_version=2)
        result = OutlookMailCoordinator(FakeMail(), state, service).process_pending(query_version=2)
        assert result.ignored == 1 and result.failed == 0 and result.proposed == 0
        assert not service.list_attention_items()
        from job_search.mail.rules import is_application_verification_email
        assert not is_application_verification_email("recruiter@example.test", "Security code for your application")
        assert not is_application_verification_email("no-reply@greenhouse.io", "Thank you for applying")


def test_transient_body_failure_retries_then_processes_without_losing_message():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        service = JobSearchLedger(path)
        state = SQLiteOutlookState(path)
        state.stage_changes("personal", "inbox", [change("retry-message")])
        mail = FakeMail()
        mail.bodies["retry-message"] = GraphHttpError(
            429,
            "TooManyRequests",
            RetryDecision(True, "2026-09-01T12:05:00Z", False, "retry_http_429"),
        )
        coordinator = OutlookMailCoordinator(mail, state, service)
        try:
            coordinator.process_pending(transient_attempt=1, transient_limit=3)
        except GraphHttpError:
            pass
        else:
            raise AssertionError("transient Graph failure did not escape to worker retry")
        assert len(state.pending_messages()) == 1
        mail.bodies["retry-message"] = {
            "id": "retry-message",
            "sender": {"emailAddress": {"address": "recruiter@example.test"}},
            "subject": "Recruiting update",
            "receivedDateTime": "2026-09-01T12:00:00Z",
            "body": {"contentType": "text", "content": "A general recruiting update."},
        }
        result = coordinator.process_pending(transient_attempt=2, transient_limit=3)
        assert result.processed == 1 and state.pending_messages() == []


def test_transient_remote_inference_failure_retries_without_losing_message():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        service = JobSearchLedger(path)
        state = SQLiteOutlookState(path)
        state.stage_changes("personal", "inbox", [change("remote-retry")])
        mail = FakeMail()
        mail.bodies["remote-retry"] = {
            "id": "remote-retry",
            "sender": {"emailAddress": {"address": "recruiter@example.test"}},
            "subject": "Recruiting update",
            "receivedDateTime": "2026-09-01T12:00:00Z",
            "body": {"contentType": "text", "content": "A recruiting update."},
        }

        class UnavailableClassifier:
            def classify(self, _text, _candidates):
                raise InferenceTransportError(
                    "inference endpoint was unavailable", retryable=True
                )

        coordinator = OutlookMailCoordinator(
            mail, state, service, classifier=UnavailableClassifier()
        )
        try:
            coordinator.process_pending(transient_attempt=1, transient_limit=3)
        except InferenceTransportError:
            pass
        else:
            raise AssertionError("transient inference failure did not request retry")
        assert len(state.pending_messages()) == 1
        coordinator.classifier = None
        result = coordinator.process_pending(transient_attempt=2, transient_limit=3)
        assert result.processed == 1 and state.pending_messages() == []


def test_exhausted_transient_body_failure_is_visible_for_attention():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        service = JobSearchLedger(path)
        state = SQLiteOutlookState(path)
        state.stage_changes("personal", "inbox", [change("dead-message")])
        mail = FakeMail()
        mail.bodies["dead-message"] = GraphHttpError(
            503,
            "ServiceUnavailable",
            RetryDecision(True, "2026-09-01T12:05:00Z", False, "retry_http_503"),
        )
        result = OutlookMailCoordinator(mail, state, service).process_pending(
            transient_attempt=3, transient_limit=3
        )
        assert result.failed == 1 and state.pending_messages() == []
        attention = service.list_attention_items()
        assert any(item["kind"] == "mail_processing_failure" for item in attention)
        assert service.system_health()["status"] == "attention"


def test_retry_failed_mail_repairs_layout_and_persists_exact_evidence_once():
    import json
    from job_search.mail import RemoteMailClassifier
    from tests.test_job_search_remote_mail import FakeProvider, evidence_response

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        service = JobSearchLedger(path)
        state = SQLiteOutlookState(path)
        message_id = "layout-message"
        state.stage_changes("personal", "inbox", [change(message_id)], query_version=2)
        mail = FakeMail()
        mail.bodies[message_id] = {
            "id": message_id,
            "sender": {"emailAddress": {"address": "recruiter@example.test"}},
            "subject": "You’re on our radar — thanks for applying to Example Labs 🚀",
            "receivedDateTime": "2026-09-01T12:00:00Z",
            "body": {"contentType": "html", "content": "<p>Thanks for applying to</p><p>Example Labs.</p>"},
        }
        response = evidence_response("Thanks for applying to Example Labs.", application_id=None)

        class UnalignedClassifier:
            def classify(self, _text, _candidates):
                return response

        coordinator = OutlookMailCoordinator(mail, state, service, classifier=UnalignedClassifier())
        failed = coordinator.process_pending(query_version=2)
        assert failed.failed == 1 and failed.proposed == 0 and failed.auto_applied == 0
        assert any(item["kind"] == "mail_processing_failure" for item in service.list_attention_items())
        service.resolve_mail_failure(
            "personal", "inbox", message_id, 2, "retry",
            MutationContext("retry-layout", "user", "dashboard"),
        )
        classifier = RemoteMailClassifier(FakeProvider(json.dumps(response)))
        coordinator.classifier = classifier
        coordinator.model_version = classifier.producer_version
        recovered = coordinator.process_pending(query_version=2)
        assert recovered.processed == 1 and recovered.failed == 0 and recovered.proposed == 1
        assert recovered.auto_applied == 0
        attention = service.list_attention_items()
        assert not any(item["kind"] == "mail_processing_failure" for item in attention)
        proposals = [item for item in attention if item["kind"] == "event_proposal"]
        assert len(proposals) == 1
        assert proposals[0]["evidence_quote"] == "Thanks for applying to\n\nExample Labs."
        assert coordinator.process_pending(query_version=2).proposed == 0


def test_model_cannot_auto_apply_with_more_than_twenty_candidate_applications():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "job-search.db"
        service = JobSearchLedger(path)
        for index in range(21):
            application = service.start_application(
                JobSnapshot(
                    "greenhouse",
                    f"overflow-{index}",
                    f"family-overflow-{index}",
                    f"Role {index}",
                    "Overflow Corp",
                    "overflow",
                    f"https://example.test/jobs/overflow-{index}",
                ),
                RecommendationProvenance(),
                MutationContext(f"overflow-start-{index}", "user", "dashboard"),
            )["application"]
            service.record_submission(application["application_id"], "2026-09-01T11:00:00Z",
                MutationContext(f"overflow-submitted-{index}", "user", "dashboard"))

        state = SQLiteOutlookState(path)
        state.stage_changes(
            "personal", "inbox", [change("overflow-message")]
        )
        mail = FakeMail()
        mail.bodies["overflow-message"] = {
            "id": "overflow-message",
            "sender": {"emailAddress": {"address": "recruiter@example.test"}},
            "subject": "Recruiting update",
            "receivedDateTime": "2026-09-01T12:00:00Z",
            "body": {
                "contentType": "text",
                "content": "Quarterly planning details. " + ("x" * 5_000),
            },
        }

        class Classifier:
            observed_text = ""

            def classify(self, text, candidates):
                self.observed_text = text
                quote = "Quarterly planning details."
                start = text.index(quote)
                return {
                    "event_type": "interview_requested",
                    "application_id": candidates[0]["application_id"],
                    "confidence": 0.99,
                    "evidence_quote": quote,
                    "span_start": start,
                    "span_end": start + len(quote),
                    "payload": {},
                }

        event = ApplicationEventType.INTERVIEW_REQUESTED
        observation = EvaluationObservation(event, "sample", event, "sample", 0.99)
        report = evaluate_observations(
            [observation] * 50,
            producer_version="overflow-model-v1",
            dataset_fingerprint="overflow-dataset-v1",
        )
        classifier = Classifier()
        coordinator = OutlookMailCoordinator(
            mail,
            state,
            service,
            classifier=classifier,
            model_version="overflow-model-v1",
            evaluation_reports={"overflow-model-v1": report},
        )
        result = coordinator.process_pending()
        assert len(classifier.observed_text) == 2_048
        assert result.proposed == 1 and result.auto_applied == 0
        assert any(
            item["kind"] == "event_proposal"
            for item in service.list_attention_items()
        )


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search sync tests)")


if __name__ == "__main__":
    main()
