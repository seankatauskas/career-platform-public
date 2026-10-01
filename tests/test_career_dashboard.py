#!/usr/bin/env python3
"""Offline HTTP boundaries for career drafts, uploads, and tailored resumes."""
from __future__ import annotations

import copy
import hashlib
import json

from job_search.dashboard import MAX_CAREER_DOCUMENT_BYTES, MAX_REQUEST_BYTES
from job_search.resume_integration import ResumeArtifactContent
from job_search.resume_lab.contracts import ResumeConflictError
from tests.test_resume_lab_integration import FakeResumeLab, browser_session, post, request, resume_dashboard


CONTENT = {
    "identity": {"name": "Example Person", "email": "example@example.test"},
    "summary": "", "education": [], "projects": [], "skills": [],
    "experience": [{"entry_id": "entry-one", "company": "Example", "role": "Engineer",
        "location": "Remote", "dates": "2020–2024", "retired": False,
        "bullets": [{"fact_id": "fact-one", "text": "Built Python services", "retired": False}]}],
}


class CareerLab(FakeResumeLab):
    def __init__(self):
        super().__init__()
        self.profile = {"draft_revision_id": None, "approved_revision_id": None, "draft": None, "approved": None}

    def get_career_profile(self):
        return copy.deepcopy(self.profile)

    def save_career_profile(self, content, *, expected_revision_id, idempotency_key):
        if expected_revision_id != self.profile["draft_revision_id"]:
            raise ResumeConflictError("career draft changed")
        revision = {"revision_id": "revision-one", "content": copy.deepcopy(content), "provenance": {}}
        self.profile.update(draft_revision_id="revision-one", draft=revision)
        self.calls.append(("career-save", expected_revision_id, idempotency_key))
        return self.get_career_profile()

    def approve_career_profile(self, revision_id, *, idempotency_key):
        assert revision_id == self.profile["draft_revision_id"]
        self.profile.update(approved_revision_id=revision_id, approved=copy.deepcopy(self.profile["draft"]))
        self.calls.append(("career-approve", revision_id, idempotency_key))
        return self.get_career_profile()

    def export_career_profile(self):
        return {"schema_version": 1, "content": CONTENT}

    def import_career_standard(self, standard_version_id, *, idempotency_key):
        self.calls.append(("career-standard", standard_version_id, idempotency_key))
        return {"revision_id": "revision-one"}

    def import_career_document(self, content, *, filename, content_type, idempotency_key):
        self.calls.append(("career-import", content, filename, content_type, idempotency_key))
        return {"import_id": "import-one", "status": "queued"}

    def get_career_import(self, import_id):
        return {"import_id": import_id, "status": "succeeded", "result": {"revision_id": "revision-one"}}

    def regenerate_career_run(self, run_id, *, pinned_fact_ids, excluded_fact_ids, idempotency_key, use_latest_profile=False):
        self.calls.append(("career-regenerate", run_id, pinned_fact_ids, excluded_fact_ids, idempotency_key, use_latest_profile))
        return {"run_id": "run-next", "status": "queued", "source_mode": "career_profile"}

    def start_research_comparisons(self, run_id, *, idempotency_key):
        self.calls.append(("career-research", run_id, idempotency_key))
        return {"run_id": "research-one", "parent_run_id": run_id, "status": "queued"}

    def get_artifact_source(self, artifact_id):
        source = b"\\documentclass{article}\n\\begin{document}Example Person\\end{document}\n"
        return ResumeArtifactContent(artifact_id, "resume.tex", "text/plain; charset=utf-8", source, hashlib.sha256(source).hexdigest())


def test_career_draft_review_export_and_stale_edit():
    with resume_dashboard(CareerLab()) as (server, _controller, lab):
        cookie, csrf = browser_session(server)
        headers = {"Cookie": cookie}
        status, _, data = request(server, "GET", "/api/v1/career-profile", headers=headers)
        assert status == 200 and json.loads(data)["configured"] is True
        status, _, data = post(server, "/api/v1/career-profile", {
            "content": CONTENT, "expected_revision_id": None, "idempotency_key": "save-one",
        }, cookie, csrf)
        assert status == 200 and json.loads(data)["approved"] is None
        status, _, _ = post(server, "/api/v1/career-profile", {
            "content": CONTENT, "expected_revision_id": "stale", "idempotency_key": "save-stale",
        }, cookie, csrf)
        assert status == 409
        status, _, data = post(server, "/api/v1/career-profile/approve", {
            "revision_id": "revision-one", "idempotency_key": "approve-one",
        }, cookie, csrf)
        assert status == 200 and json.loads(data)["approved_revision_id"] == "revision-one"
        status, response_headers, data = request(server, "GET", "/api/v1/career-profile/export", headers=headers)
        assert status == 200 and json.loads(data)["content"] == CONTENT
        assert "attachment" in response_headers["content-disposition"]
        assert response_headers["cache-control"] == "no-store"
        status, _, _ = post(server, "/api/v1/career-profile/import-standard", {
            "standard_version_id": "version-one", "idempotency_key": "seed-one",
        }, cookie, csrf)
        assert status == 200 and lab.calls[-1] == ("career-standard", "version-one", "seed-one")


def test_document_upload_keeps_csrf_mime_path_and_size_boundaries():
    with resume_dashboard(CareerLab()) as (server, _controller, lab):
        cookie, csrf = browser_session(server)
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf,
            "Origin": f"http://127.0.0.1:{server.server_address[1]}",
            "X-File-Name": "My%20Resume.txt", "Content-Type": "text/plain", "Idempotency-Key": "import-one"}
        document = b"Experience\n" * 8000  # larger than ordinary JSON, still bounded
        assert len(document) > MAX_REQUEST_BYTES
        status, _, data = request(server, "POST", "/api/v1/career-profile/imports", document, headers)
        assert status == 202 and json.loads(data)["import_id"] == "import-one"
        assert lab.calls[-1] == ("career-import", document, "My Resume.txt", "text/plain", "import-one")
        status, _, data = request(server, "GET", "/api/v1/career-profile/imports/import-one", headers={"Cookie": cookie})
        assert status == 200 and json.loads(data)["status"] == "succeeded"
        for changes, expected in (({"X-CSRF-Token": "invalid"}, 403),
                                  ({"Origin": "https://evil.test"}, 403),
                                  ({"X-File-Name": "..%2Fsecret"}, 400),
                                  ({"Content-Type": "text/html"}, 400),
                                  ({"Content-Length": str(MAX_CAREER_DOCUMENT_BYTES + 1)}, 413)):
            before = len(lab.calls)
            status, _, _ = request(server, "POST", "/api/v1/career-profile/imports", b"small", {**headers, **changes})
            assert status == expected, (changes, status)
            assert len(lab.calls) == before
        status, _, _ = post(server, "/api/v1/career-profile", {
            "content": {"summary": "x" * MAX_REQUEST_BYTES}, "idempotency_key": "too-big",
        }, cookie, csrf)
        assert status == 413  # upload allowance must not relax ordinary JSON


def test_composition_commands_and_exact_tex_download():
    with resume_dashboard(CareerLab()) as (server, _controller, lab):
        cookie, csrf = browser_session(server)
        status, _, data = post(server, "/api/v1/resume-lab/runs/primary-one/regenerate", {
            "pinned_fact_ids": ["fact-one"], "excluded_fact_ids": ["fact-two"], "idempotency_key": "regenerate-one",
        }, cookie, csrf)
        assert status == 200 and json.loads(data)["run_id"] == "run-next"
        assert lab.calls[-1] == ("career-regenerate", "primary-one", ["fact-one"], ["fact-two"], "regenerate-one", False)
        status, _, _ = post(server, "/api/v1/resume-lab/runs/primary-one/regenerate", {
            "pinned_fact_ids": [], "excluded_fact_ids": [], "use_latest_profile": True, "idempotency_key": "latest-one",
        }, cookie, csrf)
        assert status == 200 and lab.calls[-1][-1] is True
        for choices in ({"pinned_fact_ids": ["same"], "excluded_fact_ids": ["same"]},
                        {"pinned_fact_ids": "not-array"}, {"pinned_fact_ids": ["same", "same"]}, {"use_latest_profile": "true"}):
            status, _, _ = post(server, "/api/v1/resume-lab/runs/primary-one/regenerate", {
                **choices, "idempotency_key": "bad-choices",
            }, cookie, csrf)
            assert status == 400
        status, _, data = post(server, "/api/v1/resume-lab/runs/primary-one/research", {
            "idempotency_key": "research-one",
        }, cookie, csrf)
        assert status == 200 and json.loads(data)["parent_run_id"] == "primary-one"
        status, response_headers, data = request(server, "GET", "/api/v1/resume-lab/artifacts/artifact-one/source", headers={"Cookie": cookie})
        assert status == 200 and data == lab.get_artifact_source("artifact-one").content
        assert response_headers["content-type"] == "text/plain; charset=utf-8"
        assert response_headers["x-content-sha256"] == hashlib.sha256(data).hexdigest()
        assert response_headers["content-disposition"].endswith('filename="resume.tex"')


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} career dashboard tests)")
