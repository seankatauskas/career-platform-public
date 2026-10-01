#!/usr/bin/env python3
"""Regression cases for printable career facts and bounded rewrite fallback."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from job_search.resume_lab.contracts import ResumeLabError
from tests.test_career_resume import CareerModel, Context, JOB, PROFILE, Toolchain, gateway_at


def save_approved(gateway, content):
    profile = gateway.get_career_profile()
    saved = gateway.save_career_profile(content,
        expected_revision_id=profile["draft_revision_id"], idempotency_key="edge-profile")
    gateway.approve_career_profile(saved["draft_revision_id"], idempotency_key="edge-approve")
    return gateway.get_career_profile()["approved"]


def generate(gateway, application_id):
    prepared = gateway.prepare(JOB, application_id=application_id, idempotency_key="edge-compose")
    result = gateway.handle_work({"run_id": prepared["run_id"]}, Context())
    assert result["status"] == "succeeded", gateway.get_run_result(prepared["run_id"])
    view = gateway.get_run_result(prepared["run_id"])
    return gateway.service.get_artifact(view["comparisons"][0]["artifact_id"], include_content=True), view


def test_oversized_supported_rewrite_falls_back_to_approved_wording():
    class OverlongModel(CareerModel):
        def _invoke(self, task, payload, schema):
            if task == "rewrite_career_facts":
                return {"rewrites": [{"fact_id": row["fact_id"],
                    "text": ((row["text"] + " ") * 90).strip() if i == 0 else row["text"]}
                    for i, row in enumerate(payload["facts"])]}
            if task == "assess_career_rewrites":
                raise AssertionError("overlong output must fail before model assessment")
            return super()._invoke(task, payload, schema)

    with TemporaryDirectory() as directory:
        gateway, application_id = gateway_at(Path(directory), model=OverlongModel())
        artifact, _ = generate(gateway, application_id)
        assert artifact["metadata"]["wording_status"] == "approved_wording_rewrite_unavailable"
        assert artifact["metadata"]["rewrites"] == {}
        assert artifact["intended_text"].count(PROFILE["experience"][0]["bullets"][0]) == 1


def test_heading_only_experience_and_projects_are_printable_approved_facts():
    for section in ("experience", "projects"):
        with TemporaryDirectory() as directory:
            gateway, application_id = gateway_at(Path(directory))
            content = {"identity": copy.deepcopy(PROFILE["identity"]), "summary": "",
                "education": [], "experience": [], "projects": [], "skills": []}
            entry = copy.deepcopy(PROFILE[section][0])
            entry["bullets"] = []
            content[section] = [entry]
            save_approved(gateway, content)
            artifact, view = generate(gateway, application_id)
            assert entry["company" if section == "experience" else "name"] in artifact["intended_text"]
            assert len(view["composition"]["selected"]) == 1
            assert view["composition"]["selected"][0]["kind"] == "entry"


def test_empty_skill_category_requires_printable_content_and_cannot_be_pinned():
    with TemporaryDirectory() as directory:
        gateway, application_id = gateway_at(Path(directory))
        content = {"identity": copy.deepcopy(PROFILE["identity"]), "summary": "",
            "education": [], "experience": [], "projects": [],
            "skills": [{"category": "Languages", "items": []}]}
        revision = save_approved(gateway, content)
        for pins in ([], [revision["content"]["skills"][0]["entry_id"]]):
            try:
                gateway.prepare_from_career_profile(JOB, application_id=application_id,
                    idempotency_key="empty-skills-" + str(len(pins)), pinned_fact_ids=pins)
            except ResumeLabError as exc:
                assert "individual skill" in str(exc)
                assert "approve" not in str(exc)
            else:
                raise AssertionError("an empty category must not produce an identity-only resume")


def test_heading_only_role_survives_unrelated_overflow_pruning():
    class OverflowOnce(Toolchain):
        def build(self, *args, **kwargs):
            built = super().build(*args, **kwargs)
            if len(self.calls) == 1:
                return replace(built, extracted=replace(built.extracted, pages=2))
            return built

    with TemporaryDirectory() as directory:
        toolchain = OverflowOnce()
        gateway, application_id = gateway_at(Path(directory), toolchain=toolchain)
        content = copy.deepcopy(PROFILE)
        content["experience"][0]["bullets"] = []
        # Give the factual heading high relevance so a lower-ranked optional
        # fact is pruned first. Cleanup must not then remove the valid heading.
        content["experience"][0]["company"] = "Python PostgreSQL Kubernetes"
        content["experience"][0]["role"] = "Python Software Engineer"
        save_approved(gateway, content)
        artifact, _ = generate(gateway, application_id)
        assert len(toolchain.calls) == 2
        assert "Python PostgreSQL Kubernetes" in artifact["intended_text"]


def test_overflow_cannot_prune_every_fact_and_publish_contact_only_resume():
    class OverflowOnce(Toolchain):
        def build(self, *args, **kwargs):
            built = super().build(*args, **kwargs)
            return replace(built, extracted=replace(built.extracted, pages=2)) if len(self.calls) == 1 else built

    with TemporaryDirectory() as directory:
        toolchain = OverflowOnce()
        gateway, application_id = gateway_at(Path(directory), toolchain=toolchain)
        entry = copy.deepcopy(PROFILE["experience"][0])
        entry["bullets"] = []
        save_approved(gateway, {"identity": copy.deepcopy(PROFILE["identity"]), "summary": "",
            "education": [], "experience": [entry], "projects": [], "skills": []})
        prepared = gateway.prepare(JOB, application_id=application_id, idempotency_key="empty-overflow")
        result = gateway.handle_work({"run_id": prepared["run_id"]}, Context())
        view = gateway.get_run_result(prepared["run_id"])
        assert result["status"] == "failed"
        assert view["comparisons"][0]["error_code"] == "career_one_page_overflow"
        assert not view["comparisons"][0].get("artifact_id")
        assert len(toolchain.calls) == 1


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} career composition edge tests)")


if __name__ == "__main__":
    main()
