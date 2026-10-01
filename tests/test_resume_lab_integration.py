#!/usr/bin/env python3
"""Offline checks for the optional resume-lab product boundary."""

from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Optional

from job_search.contracts import ContractError
from job_search.dashboard import DashboardController, make_server
from job_search.integration import LocalJobCatalog
from job_search.resume_integration import ResumeArtifactContent
from job_search.resume_lab.contracts import (
    ResumeBoundaryError,
    ResumeConflictError,
    ResumeNotFoundError,
)
from job_search.resume_lab.gateway import ResumeSetupError
from job_search.service import JobSearchLedger


DESCRIPTION = "Build distributed Python systems and operate Kubernetes services."


def make_jobs_db(path: Path, description: str = DESCRIPTION) -> None:
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE jobs (ats TEXT NOT NULL,id TEXT NOT NULL,company TEXT NOT NULL,"
            "title TEXT NOT NULL,description TEXT NOT NULL,jobUrl TEXT NOT NULL,"
            "publishedAt TEXT,closed_at TEXT,PRIMARY KEY(ats,id))"
        )
        con.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
            (
                "ashby",
                "job-1",
                "Acme",
                "Platform Engineer",
                description,
                "https://example.test/job-1",
                "2026-09-01T12:00:00Z",
            ),
        )


class FakePreferences:
    def create_shortlist(self, options, **_kwargs):
        return {
            "session_id": "shortlist-1",
            "model": {"ready": True},
            "recommendations": [
                {
                    "ats": "ashby",
                    "id": "job-1",
                    "family_id": "family-1",
                    "title": "Platform Engineer",
                    "company": "Acme",
                    "jobUrl": "https://example.test/job-1",
                    "session_id": "shortlist-1",
                    "impression_id": 7,
                    "rank": 1,
                    "ranking_score": 0.9,
                    "semantic_score": 0.8,
                    "model_run_id": "model-1",
                    "policy_id": options["policy"],
                }
            ],
        }


class FakeResumeLab:
    def __init__(self) -> None:
        self.calls = []

    @staticmethod
    def standards():
        return [
            {
                "standard_id": "standard-older",
                "standard_version_id": "version-1",
                "name": "Backend standard",
                "score": 81.0,
                "status": "active",
            },
            {
                "standard_id": "standard-primary",
                "standard_version_id": "version-2",
                "name": "Platform standard",
                "score": 94.0,
                "status": "active",
                "artifact_id": "artifact-standard",
                "evaluation_id": "evaluation-standard",
            },
        ]

    @staticmethod
    def comparisons(approved: bool = False):
        status = "approved" if approved else "ready"
        return [
            {
                "comparison_kind": "grounded_rewrite",
                "status": status,
                "approved": approved,
                "score": 96.0,
                "artifact_id": "artifact-grounded",
                "evaluation_id": "evaluation-grounded",
                "changes": [{"source": "Built APIs", "rewrite": "Built Python APIs"}],
                "added_keywords": ["Python"],
            },
            {
                "comparison_kind": "standard_exaggerated",
                "score": 99.0,
                "artifact_id": "artifact-synthetic",
            },
            {"comparison_kind": "market_ideal", "score": 100.0},
            {"comparison_kind": "keyword_adversarial", "score": 100.0},
        ]

    def prepare(self, job, *, application_id, idempotency_key):
        self.calls.append(("prepare", dict(job), application_id, idempotency_key))
        return {
            "run_id": "run-1",
            "status": "ready",
            "ranked_standards": self.standards(),
            "comparisons": self.comparisons(),
        }

    def list_standards(self, *, limit=25):
        self.calls.append(("list", limit))
        return {"standards": self.standards()[:limit]}

    def start_run(self, job, *, application_id, standard_version_id, idempotency_key):
        self.calls.append(
            ("run", dict(job), application_id, standard_version_id, idempotency_key)
        )
        return {"run_id": "run-1", "status": "ready", "comparisons": self.comparisons()}

    def get_run_result(self, run_id):
        return {"run_id": run_id, "status": "ready", "comparisons": self.comparisons()}

    def retry_run(
        self,
        run_id,
        *,
        idempotency_key,
        reconciliation_acknowledged=False,
    ):
        return {"run_id": run_id, "status": "queued", "idempotent": True}

    def approve_run(self, run_id, *, comparison_kind, idempotency_key):
        self.calls.append(("approve", run_id, comparison_kind, idempotency_key))
        return {
            "run_id": run_id,
            "status": "ready",
            "comparisons": self.comparisons(True),
        }

    def select_resume(
        self, application_id, *, job, artifact_id, evaluation_id, idempotency_key
    ):
        self.calls.append(
            (
                "select",
                application_id,
                dict(job),
                artifact_id,
                evaluation_id,
                idempotency_key,
            )
        )
        return {
            "selection": {"application_id": application_id, "artifact_id": artifact_id}
        }

    def get_selection(self, application_id):
        return {
            "selection": {
                "application_id": application_id,
                "artifact_id": "artifact-grounded",
                "evaluation_id": "evaluation-grounded",
                "comparison_kind": "grounded_rewrite",
                "standard_id": "standard-primary",
                "standard_version_id": "version-2",
                "name": "Platform standard",
            }
        }

    def get_application_workspace(self, application_id):
        self.calls.append(("workspace", application_id))
        return {
            "application_id": application_id,
            "run_id": "run-1",
            "status": "ready",
            "ranked_standards": self.standards(),
            "comparisons": self.comparisons(),
            "selection": self.get_selection(application_id)["selection"],
        }

    def get_artifact(self, artifact_id):
        content = b"%PDF-1.7\nresume\n"
        return ResumeArtifactContent(
            artifact_id,
            "Platform Resume.pdf",
            "application/pdf",
            content,
            hashlib.sha256(content).hexdigest(),
        )

    def compare_for_job(self, ats, job_id):
        return {"ats": ats, "job_id": job_id, "comparisons": self.comparisons()}


@contextmanager
def resume_dashboard(resume_lab=None):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        jobs_db = root / "jobs.db"
        make_jobs_db(jobs_db)
        ledger = JobSearchLedger(root / "ledger.db")
        lab = resume_lab or FakeResumeLab()
        controller = DashboardController(
            ledger,
            FakePreferences(),  # type: ignore[arg-type]
            jobs=LocalJobCatalog(jobs_db),
            resume_lab=lab,  # type: ignore[arg-type]
        )
        server = make_server(controller, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, controller, lab
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def request(server, method: str, path: str, body: Optional[Any] = None, headers=None):
    port = server.server_address[1]
    sent_headers = {"Host": f"127.0.0.1:{port}", **dict(headers or {})}
    encoded = body
    if isinstance(body, Mapping):
        encoded = json.dumps(body).encode("utf-8")
        sent_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, body=encoded, headers=sent_headers)
    response = connection.getresponse()
    data = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, response_headers, data


def browser_session(server):
    status, headers, body = request(server, "GET", "/api/v1/session")
    assert status == 200
    return headers["set-cookie"].split(";", 1)[0], json.loads(body)["csrf_token"]


def post(server, path: str, body: Mapping[str, Any], cookie: str, csrf: str):
    port = server.server_address[1]
    return request(
        server,
        "POST",
        path,
        body,
        {
            "Cookie": cookie,
            "Origin": f"http://127.0.0.1:{port}",
            "X-CSRF-Token": csrf,
        },
    )


def test_exact_job_catalog_reads_one_full_bounded_posting() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "jobs.db"
        make_jobs_db(path)
        catalog = LocalJobCatalog(path)
        row = catalog.get_job("ASHBY", "job-1")
        assert row["id"] == "job-1" and row["description"] == DESCRIPTION
        try:
            catalog.get_job("ashby", "job-1' OR 1=1 --")
        except ContractError as exc:
            assert "not found" in str(exc)
        else:
            raise AssertionError("exact job lookup accepted an injected identity")
        with sqlite3.connect(path) as con:
            con.execute(
                "UPDATE jobs SET description=? WHERE ats='ashby' AND id='job-1'",
                ("x" * (LocalJobCatalog.MAX_DESCRIPTION_CHARS + 1),),
            )
        try:
            catalog.get_job("ashby", "job-1")
        except ContractError as exc:
            assert "exceeds" in str(exc)
        else:
            raise AssertionError("oversized job text was silently truncated")


def test_controller_keeps_resume_lab_optional_and_delegates_idempotent_replays() -> None:
    with resume_dashboard() as (_server, controller, lab):
        shortlist = controller.create_shortlist("browser-1", {}, "shortlist-create")
        prepared = controller.prepare_application(
            "browser-1", shortlist["session_id"], 7, "prepare-1"
        )
        application_id = prepared["application"]["application_id"]
        assert prepared["resume_lab"]["configured"] is True
        assert lab.calls[0][1]["description"] == DESCRIPTION
        assert controller.list_resume_standards()["standards"]
        assert (
            controller.start_resume_run(application_id, "version-2", "run-start")[
                "run_id"
            ]
            == "run-1"
        )
        assert controller.get_resume_run_result("run-1")["status"] == "ready"
        assert (
            controller.get_resume_workspace(application_id)["application_id"]
            == application_id
        )
        assert controller.retry_resume_run("run-1", "retry-1")["status"] == "queued"
        approved = controller.approve_resume_run(
            "run-1", "grounded_rewrite", "approve-1"
        )
        assert approved["comparisons"][0]["approved"] is True
        selected = controller.select_resume(
            application_id, "artifact-grounded", "evaluation-grounded", "select-1"
        )
        assert selected["selection"]["artifact_id"] == "artifact-grounded"
        assert controller.get_resume_artifact("artifact-grounded").content.startswith(
            b"%PDF"
        )
        controller.record_submission(
            "browser-1",
            application_id,
            "2026-09-02T12:00:00Z",
            "submitted-1",
            "selected",
        )
        timeline = controller.ledger.get_application_timeline(application_id)
        assert timeline["events"][-1]["payload"]["resume"] == {
            "decision": "selected",
            "artifact_id": "artifact-grounded",
            "evaluation_id": "evaluation-grounded",
            "comparison_kind": "grounded_rewrite",
            "standard_id": "standard-primary",
            "standard_version_id": "version-2",
            "name": "Platform standard",
        }
        replayed = controller.select_resume(
            application_id,
            "artifact-grounded",
            "evaluation-grounded",
            "select-1",
        )
        assert replayed["selection"]["artifact_id"] == "artifact-grounded"

        plain = DashboardController(controller.ledger, FakePreferences())  # type: ignore[arg-type]
        assert plain.list_resume_standards() == {"configured": False, "standards": []}


def test_http_exposes_full_resume_workbench_without_storage_paths() -> None:
    with resume_dashboard() as (server, _controller, _lab):
        cookie, csrf = browser_session(server)
        status, _headers, body = post(
            server,
            "/api/v1/shortlist",
            {"idempotency_key": "shortlist-http", "options": {}},
            cookie,
            csrf,
        )
        shortlist = json.loads(body)
        assert status == 200
        status, _headers, body = post(
            server,
            "/api/v1/resume-lab/prepare",
            {
                "idempotency_key": "prepare-http",
                "session_id": shortlist["session_id"],
                "impression_id": 7,
            },
            cookie,
            csrf,
        )
        prepared = json.loads(body)
        application_id = prepared["application"]["application_id"]
        assert status == 200 and prepared["job_url"].startswith("https://")
        status, _headers, body = request(
            server,
            "GET",
            "/api/v1/resume-lab/standards?limit=2",
            headers={"Cookie": cookie},
        )
        assert status == 200 and len(json.loads(body)["standards"]) == 2

        status, _headers, body = post(
            server,
            "/api/v1/resume-lab/runs",
            {
                "idempotency_key": "run-http",
                "application_id": application_id,
                "standard_version_id": "version-2",
            },
            cookie,
            csrf,
        )
        assert status == 200 and json.loads(body)["run_id"] == "run-1"
        assert (
            request(
                server,
                "GET",
                "/api/v1/resume-lab/runs/run-1/result",
                headers={"Cookie": cookie},
            )[0]
            == 200
        )
        assert (
            post(
                server,
                "/api/v1/resume-lab/runs/run-1/retry",
                {"idempotency_key": "retry-http"},
                cookie,
                csrf,
            )[0]
            == 200
        )
        assert (
            post(
                server,
                "/api/v1/resume-lab/runs/run-1/approve",
                {
                    "idempotency_key": "approve-http",
                    "comparison_kind": "grounded_rewrite",
                },
                cookie,
                csrf,
            )[0]
            == 200
        )
        assert (
            post(
                server,
                f"/api/v1/applications/{application_id}/resume-selection",
                {
                    "idempotency_key": "selection-http",
                    "artifact_id": "artifact-grounded",
                    "evaluation_id": "evaluation-grounded",
                },
                cookie,
                csrf,
            )[0]
            == 200
        )
        assert (
            request(
                server,
                "GET",
                f"/api/v1/applications/{application_id}/resume-selection",
                headers={"Cookie": cookie},
            )[0]
            == 200
        )
        status, _headers, body = request(
            server,
            "GET",
            f"/api/v1/applications/{application_id}/resume-workspace",
            headers={"Cookie": cookie},
        )
        reopened = json.loads(body)
        assert status == 200
        assert reopened["run_id"] == "run-1"
        assert reopened["ranked_standards"][0]["score"] == 81.0
        status, headers, artifact = request(
            server,
            "GET",
            "/api/v1/resume-lab/artifacts/artifact-synthetic?disposition=inline",
            headers={"Cookie": cookie},
        )
        assert status == 200 and artifact.startswith(b"%PDF")
        assert headers["content-disposition"].startswith("inline;")
        assert "/" not in headers["content-disposition"].split('"')[1]


def test_existing_application_recovery_ranks_all_standards_before_starting() -> None:
    class RecoveryResumeLab(FakeResumeLab):
        @staticmethod
        def standards():
            return [
                {
                    "standard_id": "manual-first",
                    "standard_version_id": "manual-first-version",
                    "name": "Manual favorite",
                    "manual_rank": 1,
                    "score": 42.0,
                },
                {
                    "standard_id": "fit-winner",
                    "standard_version_id": "fit-winner-version",
                    "name": "Role winner",
                    "manual_rank": 2,
                    "score": 96.0,
                },
            ]

        def prepare(self, job, *, application_id, idempotency_key):
            self.calls.append(("prepare", dict(job), application_id, idempotency_key))
            ranked = list(reversed(self.standards()))
            return {
                "run_id": "recovered-run",
                "status": "queued",
                "selected_standard_id": "fit-winner",
                "selected_standard_version_id": "fit-winner-version",
                "ranked_standards": ranked,
                "comparisons": [],
            }

        def start_run(self, *_args, **_kwargs):
            raise AssertionError("recovery must not start list index zero directly")

    lab = RecoveryResumeLab()
    assert lab.standards()[0]["manual_rank"] == 1
    assert lab.standards()[0]["standard_id"] != "fit-winner"
    with resume_dashboard(lab) as (server, _controller, _lab):
        cookie, csrf = browser_session(server)
        status, _headers, body = post(
            server,
            "/api/v1/shortlist",
            {"idempotency_key": "recovery-shortlist", "options": {}},
            cookie,
            csrf,
        )
        shortlist = json.loads(body)
        assert status == 200
        status, _headers, body = post(
            server,
            "/api/v1/applications/start",
            {
                "idempotency_key": "recovery-application",
                "session_id": shortlist["session_id"],
                "impression_id": 7,
            },
            cookie,
            csrf,
        )
        application_id = json.loads(body)["application"]["application_id"]
        assert status == 200
        status, _headers, body = post(
            server,
            f"/api/v1/resume-lab/applications/{application_id}/prepare",
            {"idempotency_key": "recovery-prepare"},
            cookie,
            csrf,
        )
        recovered = json.loads(body)
        assert status == 200
        assert recovered["selected_standard_id"] == "fit-winner"
        assert recovered["ranked_standards"][0]["standard_id"] == "fit-winner"
        assert [call[0] for call in lab.calls].count("prepare") == 1


def test_missing_application_resume_reads_return_not_found() -> None:
    with resume_dashboard() as (server, _controller, _lab):
        cookie, _csrf = browser_session(server)
        for suffix in ("resume-workspace", "resume-selection"):
            status, _headers, body = request(
                server,
                "GET",
                f"/api/v1/applications/missing-application/{suffix}",
                headers={"Cookie": cookie},
            )
            assert status == 404
            assert json.loads(body) == {"error": "application was not found"}


def test_http_maps_resume_domain_failures_to_actionable_statuses() -> None:
    class FailingResumeLab(FakeResumeLab):
        def __init__(self, error) -> None:
            super().__init__()
            self.error = error

        def list_standards(self, *, limit=25):
            raise self.error

    cases = (
        (ResumeBoundaryError("research artifact cannot be selected"), 409),
        (ResumeConflictError("application is no longer preparing"), 409),
        (ResumeNotFoundError("resume run was not found"), 404),
        (ResumeSetupError("local model is not configured"), 503),
    )
    for error, expected_status in cases:
        with resume_dashboard(FailingResumeLab(error)) as (server, _controller, _lab):
            cookie, _csrf = browser_session(server)
            status, _headers, body = request(
                server,
                "GET",
                "/api/v1/resume-lab/standards?limit=2",
                headers={"Cookie": cookie},
            )
            assert status == expected_status
            assert json.loads(body) == {"error": str(error)}


def test_retired_preparation_controls_are_absent() -> None:
    web = Path(__file__).parents[1] / "job_search/web"
    script = "\n".join(path.read_text("utf-8") for path in web.glob("*.js"))
    for retired in ("Prepare application", "Pair autofill", "generateResearchComparisons", "renderResumeWorkspace", "resumeCandidateCard"):
        assert retired not in script
    assert 'data-tab="documents"' in (web / "applications-view.js").read_text("utf-8")
    assert "function resumeCommandEnvelope" in script
    assert "sessionStorage.setItem" in script


def main() -> None:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume-lab integration tests)")


if __name__ == "__main__":
    main()
