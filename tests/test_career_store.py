#!/usr/bin/env python3
"""Offline career-bank persistence and attestation regressions."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
from pathlib import Path

from job_search.resume_lab.career_store import CareerStore, career_source_facts, renderable_content
from job_search.resume_lab.contracts import ResumeBoundaryError, ResumeConflictError, ResumeLabError
from job_search.resume_lab.store import connect
from job_search.resume_lab.tex import render_resume_tex


CONTENT = {"identity": {"name": "Morgan Example", "email": "morgan@example.test"},
    "experience": [{"company": "Example Company", "role": "Software Engineer", "dates": "2022 -- Present",
        "bullets": ["Built Python services that reduced processing time by 25%."]}],
    "projects": [{"name": "Garden App", "context": "Python", "bullets": ["Built a garden planner using Python."]}],
    "skills": [{"category": "Languages", "items": ["Python", "SQL"]}],
    "education": [{"institution": "Example University", "degree": "BS Computer Science", "dates": "2022"}]}


def expect(error, action):
    try:
        action()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def test_approval_and_later_edits_keep_original_facts_immutable():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        assert store.get_profile()["approved"] is None
        first = store.save_draft(CONTENT)
        assert not store.is_approved(first["revision_id"])
        store.approve_revision(first["revision_id"])
        changed = copy.deepcopy(first["content"])
        bullet = changed["experience"][0]["bullets"][0]
        bullet["text"] = "Built Python services that reduced processing time by 30%."
        second = store.save_draft(changed, expected_revision_id=first["revision_id"])
        assert second["content"]["experience"][0]["bullets"][0]["fact_id"] == bullet["fact_id"]
        assert "25%" in store.get_revision(first["revision_id"])["content"]["experience"][0]["bullets"][0]["text"]
        assert store.get_profile()["approved_revision_id"] == first["revision_id"]
        assert not store.is_approved(second["revision_id"])
        with connect(store.db_path) as con:
            expect(sqlite3.IntegrityError, lambda: con.execute("UPDATE career_profile_revisions SET content_json='{}' WHERE revision_id=?", (first["revision_id"],)))
            expect(sqlite3.IntegrityError, lambda: con.execute("DELETE FROM career_profile_approvals"))
        store.approve_revision(second["revision_id"])
        assert store.is_approved(first["revision_id"]) and store.is_approved(second["revision_id"])


def test_omission_retires_entries_and_bullets_without_erasing_history():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        first = store.save_draft(CONTENT)
        changed = copy.deepcopy(first["content"])
        changed["projects"] = []
        changed["skills"][0]["items"] = changed["skills"][0]["items"][:1]
        second = store.save_draft(changed, expected_revision_id=first["revision_id"])
        assert second["content"]["projects"][0]["retired"] is True
        assert second["content"]["skills"][0]["items"][1]["retired"] is True
        visible = renderable_content(second["content"])
        assert visible["projects"] == [] and visible["skills"][0]["items"] == ["Python"]
        assert "Garden App" not in render_resume_tex(visible).intended_text
        assert any(row["retired"] and row["text"] == "SQL" for row in career_source_facts(second["content"]))


def test_ids_cannot_move_an_accomplishment_between_employers():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        first = store.save_draft(CONTENT)
        changed = copy.deepcopy(first["content"])
        fact = changed["experience"][0]["bullets"].pop()
        changed["projects"][0]["bullets"].append(fact)
        expect(ResumeBoundaryError, lambda: store.save_draft(changed, expected_revision_id=first["revision_id"]))
        assert store.get_profile()["draft_revision_id"] == first["revision_id"]


def test_save_and_approval_replays_are_atomic_and_reject_changed_commands():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        first = store.save_draft(CONTENT, idempotency_key="save1")
        assert store.save_draft(CONTENT, idempotency_key="save1") == first
        expect(ResumeConflictError, lambda: store.save_draft({**CONTENT, "summary": "Other"}, idempotency_key="save1"))
        approval = store.approve_revision(first["revision_id"], idempotency_key="approve1")
        second = store.save_draft(first["content"], expected_revision_id=first["revision_id"], idempotency_key="save2")
        assert store.approve_revision(first["revision_id"], idempotency_key="approve1") == approval
        expect(ResumeConflictError, lambda: store.approve_revision(second["revision_id"], idempotency_key="approve1"))
        assert store.get_profile()["draft_revision_id"] == second["revision_id"]
        assert len(store.export_profile()["revisions"]) == 2


def test_concurrent_edits_cannot_overwrite_the_same_head():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        first = store.save_draft(CONTENT)
        barrier = threading.Barrier(2)
        result = []
        def edit(name):
            content = copy.deepcopy(first["content"])
            content["identity"]["name"] = name
            barrier.wait()
            try:
                result.append(store.save_draft(content, expected_revision_id=first["revision_id"])["revision_id"])
            except ResumeConflictError:
                result.append("conflict")
        threads = [threading.Thread(target=edit, args=(name,)) for name in ("Morgan One", "Morgan Two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert result.count("conflict") == 1
        assert len(store.export_profile()["revisions"]) == 2


def test_incomplete_drafts_are_editable_but_not_attested():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        draft = store.save_draft({"identity": {"name": "Morgan"}})
        expect(ResumeLabError, lambda: store.approve_revision(draft["revision_id"]))
        expect(ResumeBoundaryError, lambda: store.approve_revision(draft["revision_id"], actor="model"))
        serialized = json.dumps(store.export_profile())
        assert json.loads(serialized)["profile"]["approved"] is None
        assert store.db_path.stat().st_mode & 0o777 == 0o600


def test_scalar_limits_reject_facts_the_resume_renderer_cannot_accept():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        for field, size in (("name", 501), ("email", 1001)):
            content = copy.deepcopy(CONTENT)
            content["identity"][field] = "x" * size
            expect(ResumeLabError, lambda: store.save_draft(content))
        content = copy.deepcopy(CONTENT)
        content["experience"][0]["bullets"] = ["x" * 4001]
        expect(ResumeLabError, lambda: store.save_draft(content))
        content = copy.deepcopy(CONTENT)
        content["skills"][0]["items"] = ["x" * 501]
        expect(ResumeLabError, lambda: store.save_draft(content))
        assert store.get_profile()["draft"] is None


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} career store tests)")
