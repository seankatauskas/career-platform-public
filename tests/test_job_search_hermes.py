#!/usr/bin/env python3
"""Offline focused checks for the capability-limited Hermes adapter."""

from __future__ import annotations

import inspect
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from job_search.contracts import MutationContext
from job_search.hermes import (
    HermesAdapter,
    HermesCapabilities,
    HermesToolError,
    HermesValidationError,
    MAX_OUTPUT_BYTES,
    TOOL_NAMES,
)
from job_search.service import JobSearchLedger


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class FakeCapabilities:
    def __init__(self):
        self.requests = []

    def capabilities(self):
        return HermesCapabilities(
            search_jobs=lambda query, limit: [
                {"id": "job-1", "title": "Engineer", "query": query}
            ][:limit],
            list_shortlist=self.list_shortlist,
            list_applications=lambda phases, limit: [
                {"application_id": "app-1", "current_phase": "active"}
            ][:limit],
            list_attention_items=lambda: [{"id": "review-1", "body_html": "secret"}],
            get_application_timeline=self.timeline,
            list_interviews=lambda limit: [
                {
                    "interview_schedule_id": "schedule-1",
                    "application_id": "app-1",
                    "starts_at": "2026-09-03T12:00:00Z",
                    "ends_at": "2026-09-03T12:30:00Z",
                    "time_zone": "UTC",
                    "status": "active",
                    "temporal_proposal_id": "private-proposal",
                }
            ]
            * (limit + 2),
            search_mail=lambda query, limit: [
                {
                    "message_id": "message-1",
                    "subject": "Interview",
                    "excerpt": query,
                    "immutable_message_id": "private",
                }
            ][:limit],
            get_mail_message=lambda message_id: {
                "message_id": message_id,
                "subject": "Interview",
                "excerpt": "Sanitized mail",
                "raw_body": "private",
            },
            get_sanitized_evidence=self.evidence,
            propose_reply=self.propose,
            propose_interview_slots=self.propose,
            create_reminder=self.propose,
            list_reminders=lambda statuses, limit: [
                {
                    "reminder_id": "reminder-1",
                    "status": "scheduled",
                    "note": "Follow up",
                }
            ][:limit],
            cancel_reminder=lambda request: {
                "cancelled": True,
                "reminder": {
                    "reminder_id": request["reminder_id"],
                    "status": "cancelled",
                },
            },
            get_action_status=self.action,
            system_health=lambda: {
                "status": "healthy",
                "database": "/private/ledger.db",
                "token": "bad",
                "connectors": [
                    {
                        "connector_key": "outlook",
                        "status": "healthy",
                        "detail": "token at /private/path",
                    }
                ],
            },
            list_resume_standards=lambda limit: {
                "standards": [
                    {
                        "standard_id": "standard-1",
                        "name": "Platform standard",
                        "score": 92.0,
                        "source_tex": "private source",
                    }
                ][:limit]
            },
            compare_resumes_for_job=lambda ats, job_id: {
                "ats": ats,
                "job_id": job_id,
                "comparisons": [{"comparison_kind": "grounded_rewrite", "score": 94.0}],
            },
            get_application_resume=lambda application_id: {
                "selection": {
                    "application_id": application_id,
                    "standard_id": "standard-1",
                    "standard_version_id": "standard-version-1",
                    "name": "Platform standard",
                    "artifact_id": "artifact-1",
                    "evaluation_id": "evaluation-1",
                    "file_path": "/private/resume.pdf",
                    "managed_relative_path": "real/private/resume.pdf",
                    "changes": [
                        {
                            "source": "private source wording",
                            "rewrite": "private rewritten wording",
                        }
                    ],
                    "criteria": [
                        {
                            "source_text": "private job criterion",
                            "evidence": [{"text": "private resume evidence"}],
                        }
                    ],
                    "added_keywords": ["private keyword"],
                    "plain_text": "private parsed resume",
                    "parsed_text": "private parsed resume",
                    "intended_text": "private intended resume",
                    "tex_source": "private TeX resume",
                    "summary": "private summary",
                    "contact_line": "private contact details",
                    "bullets": ["private bullet"],
                    "identity": {"email": "private@example.test"},
                    "claims_json": "private claims",
                    "metadata_json": "private metadata",
                    "metadata": {"private": "private metadata object"},
                    "pdf_sha256": "private digest",
                }
            },
        )

    def list_shortlist(self, options):
        self.requests.append(options)
        return {"recommendations": [{"title": "Engineer", "description": "x" * 5000}]}

    def timeline(self, application_id):
        return {
            "application": {
                "application_id": application_id,
                "current_phase": "interviewing",
                "last_activity_at": "2026-09-01T10:00:00Z",
                "database": "/bad",
            },
            "events": [
                {
                    "event_type": "submission_observed",
                    "source_ref": "private-browser-session",
                    "payload": {
                        "observed_by": "manual_extension_action",
                        "resume": {
                            "decision": "selected",
                            "comparison_kind": "grounded_rewrite",
                            "standard_id": "standard-1",
                            "standard_version_id": "standard-version-1",
                            "name": "Platform standard",
                            "artifact_id": "artifact-1",
                            "evaluation_id": "evaluation-1",
                            "plain_text": "private resume contents",
                            "metadata": {"private": True},
                        },
                    },
                },
                {
                    "event_type": "interview_requested",
                    "source_ref": "mail-secret",
                    "payload": {"note": "schedule"},
                }
            ],
        }

    def evidence(self, evidence_id):
        return {
            "evidence_id": evidence_id,
            "account_id": "private",
            "immutable_message_id": "mail-secret",
            "sender": "Recruiter",
            "subject": "Interview",
            "excerpt": "Choose a time",
            "raw_body": "secret",
        }

    def propose(self, request):
        self.requests.append(request)
        return {
            "created": True,
            "action": {
                "action_id": "action-1",
                "application_id": request["application_id"],
                "status": "pending",
                "payload_json": "secret",
                "remote_idempotency_key": "secret",
            },
        }

    def action(self, action_id):
        return {
            "action_id": action_id,
            "status": "pending",
            "payload": {"body": "private reply"},
            "approvals": [],
            "executions": [{"status": "retryable_failure", "error": "/private/path"}],
        }


def adapter():
    fake = FakeCapabilities()
    return HermesAdapter(fake.capabilities(), now=lambda: NOW), fake


def expect_validation(name, args):
    client, _ = adapter()
    try:
        client.invoke(name, args)
    except HermesValidationError:
        return
    raise AssertionError("unsafe tool call was accepted")


def test_registry_is_exact_and_forbidden_capabilities_are_absent():
    client, _ = adapter()
    assert client.tool_names == TOOL_NAMES
    assert tuple(item["name"] for item in client.tool_definitions()) == TOOL_NAMES
    assert all(
        not item["input_schema"]["additionalProperties"]
        for item in client.tool_definitions()
    )
    capability_fields = set(HermesCapabilities.__dataclass_fields__)
    forbidden = {
        "approve_action",
        "execute_action",
        "sql",
        "outlook_search",
        "shell",
        "filesystem",
        "token",
        "invoke_model",
        "send_mail",
    }
    assert capability_fields.isdisjoint(forbidden)
    source = inspect.getsource(__import__("job_search.hermes", fromlist=["*"]))
    for forbidden_import in (
        "import sqlite3",
        "import subprocess",
        "from pathlib",
        "job_search.outlook",
        "job_search.mail.model",
    ):
        assert forbidden_import not in source
    for tool in forbidden:
        expect_validation(tool, {})


def test_arguments_are_strict_and_proposals_cannot_approve_or_execute():
    client, fake = adapter()
    expect_validation("list_shortlist", {"limit": True})
    expect_validation("get_application_timeline", {"application_id": "bad id"})
    expect_validation(
        "propose_reply",
        {
            "application_id": "app-1",
            "evidence_id": "ev-1",
            "body": "ok",
            "idempotency_key": "key",
            "approve": True,
        },
    )
    expect_validation(
        "propose_interview_slots",
        {"application_id": "app-1", "duration_minutes": 17, "idempotency_key": "key"},
    )
    expect_validation(
        "create_reminder",
        {
            "application_id": "app-1",
            "due_at": "2026-08-01T00:00:00Z",
            "note": "old",
            "idempotency_key": "key",
        },
    )
    result = client.invoke(
        "propose_reply",
        {
            "application_id": "app-1",
            "evidence_id": "ev-1",
            "body": "Tuesday works.",
            "idempotency_key": "reply-1",
        },
    )
    assert result == {
        "action_id": "action-1",
        "application_id": "app-1",
        "status": "pending",
        "created": True,
    }
    assert fake.requests[-1] == {
        "application_id": "app-1",
        "evidence_id": "ev-1",
        "body": "Tuesday works.",
        "idempotency_key": "reply-1",
    }


def test_outputs_are_bounded_and_private_fields_are_removed():
    client, _ = adapter()
    shortlist = client.invoke("list_shortlist", {})
    assert len(shortlist["recommendations"][0]["description"]) <= 2048
    health = client.invoke("system_health", {})
    assert health == {
        "status": "healthy",
        "connectors": [{"connector_key": "outlook", "status": "healthy"}],
    }
    timeline = client.invoke("get_application_timeline", {"application_id": "app-1"})
    assert "database" not in timeline["application"]
    assert "source_ref" not in timeline["events"][0]
    assert timeline["events"][0]["payload"]["resume"] == {
        "decision": "selected",
        "comparison_kind": "grounded_rewrite",
        "standard_id": "standard-1",
        "standard_version_id": "standard-version-1",
        "name": "Platform standard",
    }
    encoded_timeline = json.dumps(timeline)
    for private_value in (
        "artifact-1",
        "evaluation-1",
        "private resume contents",
        '"metadata"',
    ):
        assert private_value not in encoded_timeline
    evidence = client.invoke("get_sanitized_evidence", {"evidence_id": "ev-1"})
    assert set(evidence) == {"evidence_id", "sender", "subject", "excerpt"}
    action = client.invoke("get_action_status", {"action_id": "action-1"})
    assert "payload" not in action and action["execution_count"] == 1
    assert len(__import__("json").dumps(shortlist).encode()) <= MAX_OUTPUT_BYTES


def test_status_explanation_is_deterministic_and_uses_no_model():
    client, _ = adapter()
    result = client.invoke("explain_status", {"application_id": "app-1"})
    assert result["phase"] == "interviewing"
    assert result["last_event_type"] == "interview_requested"
    assert result["explanation"] == "The application is in the interview stage."


def test_extended_search_and_reminder_tools_stay_bounded():
    client, _ = adapter()
    assert client.invoke("search_jobs", {"query": "platform"})["jobs"][0][
        "id"
    ] == "job-1"
    assert client.invoke("list_applications", {"phase": "active"})[
        "applications"
    ][0]["current_phase"] == "active"
    interviews = client.invoke("list_interviews", {"limit": 1})["interviews"]
    assert len(interviews) == 1
    assert interviews[0]["interview_schedule_id"] == "schedule-1"
    assert "temporal_proposal_id" not in interviews[0]
    mail = client.invoke("search_mail", {"query": "interview"})["messages"][0]
    assert "immutable_message_id" not in mail
    assert client.invoke("get_mail_message", {"message_id": "message-1"})[
        "excerpt"
    ] == "Sanitized mail"
    assert client.invoke("list_reminders", {})["reminders"][0][
        "status"
    ] == "scheduled"
    cancelled = client.invoke(
        "cancel_reminder",
        {"reminder_id": "reminder-1", "idempotency_key": "cancel-1"},
    )
    assert cancelled["cancelled"] and cancelled["status"] == "cancelled"
    expect_validation("search_mail", {"query": "x", "folder": "all"})


def test_resume_tools_are_read_only_bounded_views():
    client, _ = adapter()
    standards = client.invoke("list_resume_standards", {"limit": 1})
    assert standards["standards"][0]["score"] == 92.0
    assert "source_tex" not in standards["standards"][0]
    comparison = client.invoke(
        "compare_resumes_for_job", {"ats": "ashby", "job_id": "job-1"}
    )
    assert comparison["comparisons"][0]["comparison_kind"] == "grounded_rewrite"
    selection = client.invoke(
        "get_application_resume", {"application_id": "app-1"}
    )
    assert selection["selection"]["application_id"] == "app-1"
    assert selection["selection"]["standard_id"] == "standard-1"
    assert selection["selection"]["standard_version_id"] == "standard-version-1"
    assert selection["selection"]["name"] == "Platform standard"
    assert "artifact_id" not in selection["selection"]
    assert "evaluation_id" not in selection["selection"]
    assert "file_path" not in selection["selection"]
    assert "managed_relative_path" not in selection["selection"]
    assert "metadata" not in selection["selection"]
    encoded = json.dumps(selection)
    assert "private source wording" not in encoded
    assert "private rewritten wording" not in encoded
    assert "private job criterion" not in encoded
    assert "private resume evidence" not in encoded
    assert "private keyword" not in encoded
    assert "private parsed resume" not in encoded
    assert "private intended resume" not in encoded
    assert "private TeX resume" not in encoded
    assert "private summary" not in encoded
    assert "private contact details" not in encoded
    assert "private bullet" not in encoded
    assert "private@example.test" not in encoded
    assert "private claims" not in encoded
    assert "private metadata" not in encoded
    assert "private metadata object" not in encoded
    assert "private digest" not in encoded
    expect_validation("compare_resumes_for_job", {"ats": "unknown", "job_id": "job-1"})


def test_capability_errors_do_not_leak_private_details():
    caps = FakeCapabilities().capabilities()
    caps = HermesCapabilities(
        **{
            **caps.__dict__,
            "system_health": lambda: (_ for _ in ()).throw(
                RuntimeError("token at /private/path")
            ),
        }
    )
    try:
        HermesAdapter(caps).invoke("system_health", {})
    except HermesToolError as exc:
        assert "token" not in str(exc) and "/private" not in str(exc)
    else:
        raise AssertionError("capability error escaped")


def test_ledger_exposes_only_persisted_sanitized_evidence_read():
    with tempfile.TemporaryDirectory() as directory:
        service = JobSearchLedger(Path(directory) / "ledger.db")
        saved = service.record_mail_evidence(
            {
                "account_id": "outlook",
                "immutable_message_id": "mail-1",
                "conversation_id": "conversation-1",
                "sender": "recruiter@example.test",
                "subject": "Interview",
                "received_at": "2026-09-01T10:00:00Z",
                "body_sha256": "a" * 64,
                "excerpt": "Can you meet Tuesday?",
            },
            MutationContext("evidence-1", "system", "outlook"),
        )["evidence"]
        evidence = service.get_sanitized_evidence(saved["evidence_id"])
        assert evidence["excerpt"] == "Can you meet Tuesday?"
        assert "account_id" not in evidence
        assert "immutable_message_id" not in evidence


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} Hermes tests)")


if __name__ == "__main__":
    main()
