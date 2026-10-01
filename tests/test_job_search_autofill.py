#!/usr/bin/env python3
"""Offline privacy and handoff tests for Chromium application autofill."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from job_search.autofill import (
    HANDOFF_TTL_SECONDS,
    AutofillBroker,
    AutofillProfile,
    EncryptedAutofillVault,
    canonical_private_option,
    is_sensitive_prompt,
    load_profile,
    validate_ats_page,
    validate_descriptors,
    validate_extension_origin,
)
from job_search.contracts import ConflictError, ContractError, JobSnapshot, MutationContext, RecommendationProvenance
from job_search.dashboard import DashboardController, make_server
from job_search.service import JobSearchLedger


EXTENSION_ORIGIN = "chrome-extension://" + "a" * 32


def stamp(seconds: int = 0) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def profile_mapping() -> Mapping[str, Any]:
    return {
        "version": 1,
        "contact": {
            "first_name": "Sean",
            "last_name": "Example",
            "email": "sean@example.test",
            "phone": "+1 555 0100",
            "city": "Chicago",
            "state": "IL",
            "linkedin_url": "https://linkedin.example/sean",
        },
        "work_history": [
            {
                "employer": "Example Labs",
                "title": "Software Engineer",
                "start_month": "January",
                "start_year": "2022",
                "summary": "Built reliable data systems.",
            }
        ],
        "approved_answers": [
            {
                "answer_id": "why-role",
                "prompt": "Why are you interested in this role?",
                "value": "The product and engineering scope are compelling.",
                "ats": ["greenhouse", "ashby", "lever"],
            }
        ],
    }


def profile() -> AutofillProfile:
    return AutofillProfile.from_mapping(profile_mapping())


def make_ledger(directory: str) -> JobSearchLedger:
    return JobSearchLedger(Path(directory) / "job-search.db")


def start_application(ledger: JobSearchLedger, ats: str = "greenhouse") -> str:
    hosts = {
        "greenhouse": "job-boards.greenhouse.io",
        "ashby": "jobs.ashbyhq.com",
        "lever": "jobs.lever.co",
    }
    result = ledger.start_application(
        JobSnapshot(
            ats,
            "job-1",
            "family-1",
            "Platform Engineer",
            "Acme",
            "acme",
            f"https://{hosts[ats]}/acme/job-1",
        ),
        RecommendationProvenance(),
        MutationContext("start-autofill", "user", "dashboard"),
    )
    return result["application"]["application_id"]


def descriptors():
    return [
        {"field_id": "first", "kind": "first_name", "prompt": "First name"},
        {"field_id": "mail", "kind": "email", "prompt": "Email"},
        {
            "field_id": "employer",
            "kind": "work_employer",
            "prompt": "Employer",
            "history_index": 0,
        },
        {
            "field_id": "why",
            "kind": "approved_answer",
            "prompt": "Why are you interested in this role?",
        },
    ]


def private_descriptors():
    return [
        {
            "field_id": "disability",
            "kind": "private_answer",
            "prompt": "Voluntary disability status",
            "control": "radio_group",
            "options": [
                {"option_id": "yes", "label": "Yes, I have a disability"},
                {"option_id": "no", "label": "No, I do not have a disability"},
                {"option_id": "decline", "label": "I prefer not to answer"},
            ],
        },
        {
            "field_id": "race",
            "kind": "private_answer",
            "prompt": "Race and ethnicity",
            "control": "checkbox_group",
            "options": [
                {"option_id": "asian", "label": "Asian"},
                {"option_id": "white", "label": "White"},
                {"option_id": "decline", "label": "Decline to self-identify"},
            ],
        },
        {
            "field_id": "custom",
            "kind": "approved_answer",
            "prompt": "Describe an interesting technical project",
            "control": "textarea",
            "options": [],
        },
    ]


def test_profile_returns_only_active_safe_assignments() -> None:
    result = profile().assignments("greenhouse", descriptors())
    assert result == [
        {"field_id": "first", "value": "Sean"},
        {"field_id": "mail", "value": "sean@example.test"},
        {"field_id": "employer", "value": "Example Labs"},
        {
            "field_id": "why",
            "value": "The product and engineering scope are compelling.",
        },
    ]
    flattened = json.dumps(result)
    assert "phone" not in flattened and "linkedin" not in flattened
    assert "salary" not in flattened


def test_every_prohibited_prompt_class_is_blocked() -> None:
    prompts = (
        "Gender identity",
        "Race and ethnicity",
        "Legal attestation and electronic signature",
        "Desired salary expectation",
        "Are you legally authorized to work?",
        "Will you need visa sponsorship?",
        "Disability status",
        "Protected veteran status",
        "Voluntary EEO questionnaire",
        "Are you Hispanic or Latino?",
        "Are you 18 or older?",
        "Desired annual compensation",
        "Employment eligibility",
        "I agree to a background check",
        "Upload resume",
        "Submit application",
    )
    assert all(is_sensitive_prompt(prompt) for prompt in prompts)
    for prompt in prompts:
        bad = dict(profile_mapping())
        bad["approved_answers"] = [
            {"answer_id": "unsafe", "prompt": prompt, "value": "yes", "ats": ["ashby"]}
        ]
        try:
            AutofillProfile.from_mapping(bad)
        except ContractError:
            pass
        else:
            raise AssertionError(f"sensitive prompt was approved: {prompt}")


def test_descriptor_and_endpoint_contracts_reject_broad_or_mismatched_access() -> None:
    for descriptor in (
        {"field_id": "upload", "kind": "file_upload", "prompt": "Resume"},
        {"field_id": "submit", "kind": "final_submit", "prompt": "Submit"},
        {"field_id": "salary", "kind": "salary", "prompt": "Salary"},
        {
            "field_id": "salary-answer",
            "kind": "approved_answer",
            "prompt": "Desired salary",
        },
    ):
        try:
            validate_descriptors([descriptor])
        except ContractError:
            pass
        else:
            raise AssertionError("unsafe descriptor kind was accepted")
    assert validate_extension_origin(EXTENSION_ORIGIN) == EXTENSION_ORIGIN
    for bad_origin in ("https://example.test", "chrome-extension://short", "moz-extension://" + "a" * 32):
        try:
            validate_extension_origin(bad_origin)
        except ContractError:
            pass
        else:
            raise AssertionError("non-Chromium origin was accepted")
    assert validate_ats_page("lever", "https://jobs.lever.co/acme/job") == "jobs.lever.co"
    try:
        validate_ats_page("lever", "https://jobs.ashbyhq.com/acme/job")
    except ContractError:
        pass
    else:
        raise AssertionError("mismatched ATS hostname was accepted")


class FakeEncryptedPersistence:
    is_encrypted = True

    def __init__(self):
        self.value = ""

    def load(self):
        return self.value

    def save(self, value):
        self.value = value


def test_encrypted_vault_captures_private_and_history_then_maps_across_ats() -> None:
    with tempfile.TemporaryDirectory() as directory:
        persistence = FakeEncryptedPersistence()
        vault = EncryptedAutofillVault(
            Path(directory) / "vault.bin", persistence=persistence
        )
        ledger = make_ledger(directory)
        application_id = start_application(ledger)
        broker = AutofillBroker(ledger, profile(), vault)
        issued = broker.issue(application_id, "capture-session", "capture-handoff")
        exchanged = broker.exchange(
            issued["pairing_code"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            private_descriptors(),
        )
        assert exchanged["assignments"] == []
        staged = broker.stage_capture(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            [
                {"field_id": "disability", "option_ids": ["no"]},
                {"field_id": "race", "option_ids": ["asian"]},
                {"field_id": "custom", "value": "Built a reliable event pipeline."},
            ],
        )
        assert staged == {"staged": True, "answer_count": 3}
        assert persistence.value == "", "staging must remain process-local"
        assert len(ledger.get_application_timeline(application_id)["events"]) == 1
        submitted = broker.mark_submitted(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            "capture-submit",
            "not_tracked",
        )
        assert submitted["autofill_capture"] == {
            "private_answers": 2,
            "custom_answers": 1,
        }
        replay = broker.mark_submitted(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            "capture-submit",
            "not_tracked",
        )
        assert replay == submitted
        saved = json.loads(persistence.value)
        assert saved["private_answers"]["disability"]["values"] == ["no"]
        assert saved["private_answers"]["disability"]["representation"] == "options"
        assert saved["private_answers"]["race_ethnicity"]["values"] == ["asian"]
        assert saved["custom_history"][0]["value"] == "Built a reliable event pipeline."
        assert len(saved["custom_history"]) == 1
        public_result = json.dumps(submitted)
        public_timeline = json.dumps(ledger.get_application_timeline(application_id))
        assert "Built a reliable event pipeline." not in public_result
        assert "Built a reliable event pipeline." not in public_timeline

        ashby_fields = [
            {
                "field_id": "different-disability",
                "kind": "private_answer",
                "prompt": "Do you have a disability?",
                "control": "select",
                "options": [
                    {"option_id": "without", "label": "Individual without a disability"},
                    {"option_id": "with", "label": "Individual with a disability"},
                ],
            },
            {
                "field_id": "different-race",
                "kind": "private_answer",
                "prompt": "Please identify your race",
                "control": "checkbox_group",
                "options": [
                    {"option_id": "a", "label": "Asian"},
                    {"option_id": "b", "label": "Caucasian / White"},
                ],
            },
            {
                "field_id": "same-custom",
                "kind": "approved_answer",
                "prompt": "Describe an interesting technical project",
                "control": "textarea",
                "options": [],
            },
        ]
        assert vault.assignments("ashby", ashby_fields) == [
            {"field_id": "different-disability", "option_ids": ["without"]},
            {"field_id": "different-race", "option_ids": ["a"]},
        ]


def test_vault_rejects_plaintext_and_ambiguous_private_choices() -> None:
    class Plaintext(FakeEncryptedPersistence):
        is_encrypted = False

    with tempfile.TemporaryDirectory() as directory:
        try:
            EncryptedAutofillVault(Path(directory) / "plain", persistence=Plaintext())
        except ContractError:
            pass
        else:
            raise AssertionError("plaintext private persistence was accepted")
        persistence = FakeEncryptedPersistence()
        persistence.value = json.dumps(
            {
                "version": 1,
                "private_answers": {
                    "disability": {
                        "values": ["no"],
                        "representation": "options",
                        "updated_at": stamp(),
                    }
                },
                "custom_history": [],
            }
        )
        vault = EncryptedAutofillVault(
            Path(directory) / "ambiguous", persistence=persistence
        )
        fields = [
            {
                "field_id": "ambiguous",
                "kind": "private_answer",
                "prompt": "Disability status",
                "control": "select",
                "options": [
                    {"option_id": "first", "label": "No disability"},
                    {
                        "option_id": "second",
                        "label": "Individual without a disability",
                    },
                ],
            }
        ]
        assert vault.assignments("lever", fields) == []
        text_field = [
            {
                "field_id": "free-text",
                "kind": "private_answer",
                "prompt": "Disability status",
                "control": "text",
                "options": [],
            }
        ]
        assert vault.assignments("lever", text_field) == []
    assert canonical_private_option("disability", "No disability") == "no"


def test_capture_is_receipt_scoped_and_not_persisted_without_manual_mark() -> None:
    with tempfile.TemporaryDirectory() as directory:
        persistence = FakeEncryptedPersistence()
        vault = EncryptedAutofillVault(
            Path(directory) / "vault.bin", persistence=persistence
        )
        ledger = make_ledger(directory)
        application_id = start_application(ledger)
        broker = AutofillBroker(ledger, profile(), vault)
        issued = broker.issue(application_id, "capture-scope", "capture-scope-issue")
        exchanged = broker.exchange(
            issued["pairing_code"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            private_descriptors(),
        )
        for wrong_origin, wrong_ats, wrong_page in (
            (
                "chrome-extension://" + "b" * 32,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/job-1",
            ),
            (
                EXTENSION_ORIGIN,
                "lever",
                "https://jobs.lever.co/acme/job-1",
            ),
            (
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/another-job",
            ),
        ):
            try:
                broker.stage_capture(
                    exchanged["submission_token"],
                    wrong_origin,
                    wrong_ats,
                    wrong_page,
                    [{"field_id": "disability", "option_ids": ["no"]}],
                )
            except ContractError:
                pass
            else:
                raise AssertionError("capture escaped its receipt scope")
        broker.stage_capture(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            [{"field_id": "disability", "option_ids": ["no"]}],
        )
        assert persistence.value == ""
        assert len(ledger.get_application_timeline(application_id)["events"]) == 1


def test_capture_commit_is_retry_safe_and_clears_the_staged_snapshot() -> None:
    class FailFirstSubmission:
        def __init__(self, ledger):
            self.ledger = ledger
            self.failed = False

        def __getattr__(self, name):
            return getattr(self.ledger, name)

        def record_submission(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated ledger interruption")
            return self.ledger.record_submission(*args, **kwargs)

    with tempfile.TemporaryDirectory() as directory:
        persistence = FakeEncryptedPersistence()
        vault = EncryptedAutofillVault(
            Path(directory) / "vault.bin", persistence=persistence
        )
        ledger = make_ledger(directory)
        application_id = start_application(ledger)
        broker = AutofillBroker(FailFirstSubmission(ledger), profile(), vault)
        issued = broker.issue(application_id, "capture-retry", "capture-retry-issue")
        exchanged = broker.exchange(
            issued["pairing_code"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            private_descriptors(),
        )
        broker.stage_capture(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            [{"field_id": "custom", "value": "A retry-safe answer."}],
        )
        try:
            broker.mark_submitted(
                exchanged["submission_token"],
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/job-1",
                "retry-submit",
                "not_tracked",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("simulated ledger interruption did not occur")
        saved = json.loads(persistence.value)
        assert len(saved["custom_history"]) == 1
        try:
            broker.stage_capture(
                exchanged["submission_token"],
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/job-1",
                [{"field_id": "custom", "value": "A replacement answer."}],
            )
        except ConflictError:
            pass
        else:
            raise AssertionError("staged capture changed after commit began")
        submitted = broker.mark_submitted(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            "retry-submit",
            "not_tracked",
        )
        assert submitted["autofill_capture"] == {
            "private_answers": 0,
            "custom_answers": 1,
        }
        assert len(json.loads(persistence.value)["custom_history"]) == 1


def test_mode_0600_profile_loader() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "autofill-profile.json"
        path.write_text(json.dumps(profile_mapping()), encoding="utf-8")
        os.chmod(path, 0o644)
        try:
            load_profile(path)
        except ContractError:
            pass
        else:
            raise AssertionError("world-readable profile was loaded")
        os.chmod(path, 0o600)
        assert load_profile(path).version == 1


def test_handoff_is_scoped_short_lived_one_time_and_mark_is_manual_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger = make_ledger(directory)
        application_id = start_application(ledger)
        now = [1000.0]
        broker = AutofillBroker(ledger, profile(), clock=lambda: now[0])
        issued = broker.issue(application_id, "browser-session", "handoff-1")
        replay = broker.issue(application_id, "browser-session", "handoff-1")
        assert replay["pairing_code"] == issued["pairing_code"]
        try:
            broker.exchange(
                issued["pairing_code"],
                EXTENSION_ORIGIN,
                "ashby",
                "https://jobs.ashbyhq.com/acme/job-1",
                descriptors(),
            )
        except ContractError:
            pass
        else:
            raise AssertionError("handoff crossed ATS scope")
        try:
            broker.exchange(
                issued["pairing_code"],
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/another-job",
                descriptors(),
            )
        except ContractError:
            pass
        else:
            raise AssertionError("handoff crossed job scope")
        exchanged = broker.exchange(
            issued["pairing_code"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            descriptors(),
        )
        assert exchanged["profile_version"] == 1
        assert exchanged["resume"] == {"status": "unavailable"}
        assert "contact" not in exchanged and "work_history" not in exchanged
        try:
            broker.exchange(
                issued["pairing_code"],
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/job-1",
                descriptors(),
            )
        except ContractError:
            pass
        else:
            raise AssertionError("pairing code was used twice")

        before = ledger.get_application_timeline(application_id)
        assert len(before["events"]) == 1, "exchange must not claim submission"
        try:
            broker.mark_submitted(
                exchanged["submission_token"],
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/another-job",
                "wrong-page-submit",
                "not_tracked",
            )
        except ContractError:
            pass
        else:
            raise AssertionError("submission receipt crossed job-page scope")
        submitted = broker.mark_submitted(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            "manual-submit-1",
            "not_tracked",
        )
        replay_submit = broker.mark_submitted(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            "manual-submit-1",
            "not_tracked",
        )
        assert submitted == replay_submit
        assert submitted["application"]["current_phase"] == "awaiting_confirmation"
        assert ledger.get_application_timeline(application_id)["events"][-1][
            "payload"
        ]["resume"] == {"decision": "not_tracked"}
        assert len(ledger.list_outbox()) == 1
        try:
            broker.mark_submitted(
                exchanged["submission_token"],
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/job-1",
                "different-key",
                "not_tracked",
            )
        except ConflictError:
            pass
        else:
            raise AssertionError("submission receipt was reused for another action")

        second = broker.issue(application_id, "browser-session", "handoff-expiring")
        now[0] += HANDOFF_TTL_SECONDS + 1
        try:
            broker.exchange(
                second["pairing_code"],
                EXTENSION_ORIGIN,
                "greenhouse",
                "https://job-boards.greenhouse.io/acme/jobs/job-1",
                [],
            )
        except ContractError:
            pass
        else:
            raise AssertionError("expired handoff was accepted")


def test_selected_resume_status_and_metadata_use_the_deferred_submission_context() -> None:
    class ObservingLedger:
        def __init__(self, ledger):
            self.ledger = ledger
            self.received_factory = False

        def get_application_timeline(self, application_id):
            return self.ledger.get_application_timeline(application_id)

        def record_submission(self, *args, **kwargs):
            self.received_factory = callable(kwargs.get("payload_factory"))
            assert "selected" not in context_calls
            return self.ledger.record_submission(*args, **kwargs)

    with tempfile.TemporaryDirectory() as directory:
        ledger = make_ledger(directory)
        application_id = start_application(ledger)
        context_calls = []
        resume_snapshot = {
            "decision": "selected",
            "artifact_id": "real_artifact_1",
            "evaluation_id": "evaluation_1",
            "comparison_kind": "standard",
            "standard_id": "std_1",
            "standard_version_id": "stdv_1",
            "name": "Platform standard",
        }

        def submission_context(supplied_application_id, decision):
            assert supplied_application_id == application_id
            context_calls.append(decision)
            return {"resume": dict(resume_snapshot)}

        observed = ObservingLedger(ledger)
        broker = AutofillBroker(
            observed,
            profile(),
            submission_context=submission_context,
        )
        issued = broker.issue(
            application_id, "selected-resume-session", "selected-resume-handoff"
        )
        exchanged = broker.exchange(
            issued["pairing_code"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            descriptors(),
        )
        assert exchanged["resume"] == {
            "status": "selected",
            "name": "Platform standard",
            "comparison_kind": "standard",
        }
        assert "artifact_id" not in exchanged["resume"]
        assert context_calls == ["automatic"]

        broker.mark_submitted(
            exchanged["submission_token"],
            EXTENSION_ORIGIN,
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/job-1",
            "selected-resume-submit",
            "selected",
        )
        assert observed.received_factory is True
        assert context_calls == ["automatic", "selected"]
        assert ledger.get_application_timeline(application_id)["events"][-1][
            "payload"
        ] == {
            "observed_by": "manual_extension_action",
            "resume": resume_snapshot,
        }


class FakePreferences:
    def create_shortlist(self, *_args, **_kwargs):
        return {"recommendations": []}


@contextmanager
def dashboard_server():
    with tempfile.TemporaryDirectory() as directory:
        ledger = make_ledger(directory)
        application_id = start_application(ledger)
        broker = AutofillBroker(ledger, profile())
        controller = DashboardController(ledger, FakePreferences(), autofill=broker)  # type: ignore[arg-type]
        server = make_server(controller, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, ledger, application_id
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def http_request(
    server,
    method: str,
    path: str,
    body: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
):
    port = server.server_address[1]
    encoded = json.dumps(body).encode() if body is not None else None
    sent = {"Host": f"127.0.0.1:{port}", **dict(headers or {})}
    if body is not None:
        sent["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, encoded, sent)
    response = connection.getresponse()
    data = response.read()
    result_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, result_headers, data


def test_dashboard_and_extension_endpoints_keep_tokens_out_of_urls() -> None:
    with dashboard_server() as (server, ledger, application_id):
        port = server.server_address[1]
        status, headers, body = http_request(server, "GET", "/api/v1/session")
        session = json.loads(body)
        cookie = headers["set-cookie"].split(";", 1)[0]
        same_origin = {
            "Cookie": cookie,
            "Origin": f"http://127.0.0.1:{port}",
            "X-CSRF-Token": session["csrf_token"],
        }
        status, _headers, body = http_request(
            server,
            "POST",
            "/api/v1/autofill/handoffs",
            {"application_id": application_id, "idempotency_key": "issue-http"},
            same_origin,
        )
        issued = json.loads(body)
        assert status == 200 and issued["pairing_code"] not in "/api/v1/autofill/handoffs"

        status, headers, _body = http_request(
            server,
            "OPTIONS",
            "/api/v1/autofill/exchange",
            headers={
                "Origin": EXTENSION_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert status == 204
        assert headers["access-control-allow-origin"] == EXTENSION_ORIGIN
        assert headers["access-control-allow-private-network"] == "true"

        status, headers, body = http_request(
            server,
            "POST",
            "/api/v1/autofill/exchange",
            {
                "pairing_code": issued["pairing_code"],
                "ats": "greenhouse",
                "page_url": "https://job-boards.greenhouse.io/acme/jobs/job-1",
                "fields": descriptors(),
            },
            {"Origin": EXTENSION_ORIGIN},
        )
        exchanged = json.loads(body)
        assert status == 200 and headers["access-control-allow-origin"] == EXTENSION_ORIGIN
        assert "set-cookie" not in headers
        assert issued["pairing_code"].encode() not in body
        assert exchanged["submission_token"]

        status, headers, body = http_request(
            server,
            "POST",
            "/api/v1/autofill/capture",
            {
                "submission_token": exchanged["submission_token"],
                "ats": "greenhouse",
                "page_url": "https://job-boards.greenhouse.io/acme/jobs/job-1",
                "answers": [
                    {
                        "field_id": "why",
                        "value": "The product and engineering scope are compelling.",
                    }
                ],
            },
            {"Origin": EXTENSION_ORIGIN},
        )
        assert status == 200 and json.loads(body)["answer_count"] == 1
        assert headers["access-control-allow-origin"] == EXTENSION_ORIGIN

        for invalid_decision in (None, "automatic"):
            request_body = {
                "submission_token": exchanged["submission_token"],
                "idempotency_key": "extension-submit-invalid-choice",
                "ats": "greenhouse",
                "page_url": "https://job-boards.greenhouse.io/acme/jobs/job-1",
            }
            if invalid_decision is not None:
                request_body["resume_decision"] = invalid_decision
            status, _headers, _body = http_request(
                server,
                "POST",
                "/api/v1/autofill/submitted",
                request_body,
                {"Origin": EXTENSION_ORIGIN},
            )
            assert status == 400
            assert ledger.get_application_timeline(application_id)["application"][
                "current_phase"
            ] == "preparing"

        status, _headers, body = http_request(
            server,
            "POST",
            "/api/v1/autofill/submitted",
            {
                "submission_token": exchanged["submission_token"],
                "idempotency_key": "extension-submit-http",
                "ats": "greenhouse",
                "page_url": "https://job-boards.greenhouse.io/acme/jobs/job-1",
                "resume_decision": "not_tracked",
            },
            {"Origin": EXTENSION_ORIGIN},
        )
        submitted = json.loads(body)
        assert status == 200 and submitted["application"]["current_phase"] == "awaiting_confirmation"
        assert submitted["autofill_capture"] == {
            "private_answers": 0,
            "custom_answers": 0,
        }
        timeline = ledger.get_application_timeline(application_id)
        assert timeline["events"][-1]["payload"] == {
            "observed_by": "manual_extension_action",
            "resume": {"decision": "not_tracked"},
        }


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search autofill tests)")


if __name__ == "__main__":
    main()
