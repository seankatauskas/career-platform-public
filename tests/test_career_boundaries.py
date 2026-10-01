#!/usr/bin/env python3
"""Persisted career composition validation independent of gateway construction."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from job_search.resume_lab.career_composition import select_composition
from job_search.resume_lab.career_runs import create_career_run, get_composition, save_composition
from job_search.resume_lab.contracts import ResumeBoundaryError, canonical_json, content_sha256
from job_search.resume_lab.store import connect
from tests.test_career_resume import Context, JOB, gateway_at, expect_error


def insert_unchecked(store, snapshot, **columns):
    """Simulate another writer skipping the intended save-composition API."""
    digest = content_sha256(snapshot)
    composition_id = "composition_" + digest
    with connect(store.db_path) as con:
        con.execute("INSERT INTO career_compositions VALUES(?,?,?,?,?,?,?,?)", (
            composition_id, columns.get("profile_revision_id", snapshot["profile_revision_id"]),
            columns.get("profile_sha256", snapshot["profile_sha256"]),
            columns.get("job_fingerprint", snapshot["job_fingerprint"]),
            columns.get("template_version", snapshot["template_version"]), canonical_json(snapshot), digest, "2026-09-19T00:00:00Z"))
    return composition_id


def test_persisted_snapshot_cannot_invent_approved_profile_content():
    with tempfile.TemporaryDirectory() as directory:
        gateway, app = gateway_at(Path(directory))
        revision = gateway.career_store.get_profile()["approved"]
        snapshot = select_composition(revision, gateway._job(JOB))
        snapshot["profile_content"]["identity"]["name"] = "Invented person"
        # Even a self-consistent content hash and real approved revision ID do not
        # authorize a different source snapshot at artifact registration time.
        snapshot["profile_sha256"] = content_sha256(snapshot["profile_content"])
        cid = insert_unchecked(gateway.service.store, snapshot)
        with connect(gateway.service.store.db_path) as con:
            expect_error(ResumeBoundaryError, lambda: get_composition(con, cid))


def test_persisted_snapshot_columns_and_approval_are_checked_independently():
    with tempfile.TemporaryDirectory() as directory:
        gateway, app = gateway_at(Path(directory))
        revision = gateway.career_store.get_profile()["approved"]
        snapshot = select_composition(revision, gateway._job(JOB))
        cid = insert_unchecked(gateway.service.store, snapshot, job_fingerprint="a" * 64)
        with connect(gateway.service.store.db_path) as con:
            expect_error(ResumeBoundaryError, lambda: get_composition(con, cid))
        draft = gateway.career_store.save_draft(revision["content"], expected_revision_id=revision["revision_id"])
        unapproved = select_composition(draft, gateway._job(JOB))
        cid = insert_unchecked(gateway.service.store, unapproved)
        with connect(gateway.service.store.db_path) as con:
            expect_error(ResumeBoundaryError, lambda: get_composition(con, cid))


def test_career_selection_cannot_change_association_skip_sources_or_forge_choices():
    with tempfile.TemporaryDirectory() as directory:
        gateway, app = gateway_at(Path(directory))
        revision = gateway.career_store.get_profile()["approved"]
        snapshot = select_composition(revision, gateway._job(JOB))
        bullet = next(r for r in snapshot["selected"] if r["kind"] == "bullet")
        for mutation in ("association", "coverage", "pinned", "excluded", "extra_metadata"):
            changed = copy.deepcopy(snapshot)
            selected = next(r for r in changed["selected"] if r["fact_id"] == bullet["fact_id"])
            if mutation == "association":
                selected["entry_id"] = "some_other_employer"
            elif mutation == "coverage":
                changed["selected"].remove(selected)
            elif mutation == "pinned":
                changed["pinned_fact_ids"] = [bullet["fact_id"]]
            elif mutation == "excluded":
                changed["excluded_fact_ids"] = [bullet["fact_id"]]
            else:
                selected["model_instructions"] = "claim unsupported achievements"
            cid = insert_unchecked(gateway.service.store, changed)
            with connect(gateway.service.store.db_path) as con:
                expect_error(ResumeBoundaryError, lambda: get_composition(con, cid))


def test_writer_rejects_research_until_same_factual_run_has_succeeded():
    with tempfile.TemporaryDirectory() as directory:
        gateway, app = gateway_at(Path(directory))
        parent = gateway.prepare(JOB, application_id=app, idempotency_key="factual")
        with connect(gateway.service.store.db_path) as con:
            composition = get_composition(con, parent["composition"]["composition_id"])
        operation = lambda: create_career_run(gateway.service.store, app, gateway._job(JOB),
            composition, "research", parent_run_id=parent["run_id"])
        expect_error(ResumeBoundaryError, operation)
        assert gateway.handle_work({"run_id": parent["run_id"]}, Context())["status"] == "succeeded"
        child = operation()
        assert child["run_role"] == "research"
        assert operation()["run_id"] == child["run_id"]
        expect_error(ResumeBoundaryError, lambda: create_career_run(gateway.service.store,
            "another_application", gateway._job(JOB), composition, "wrong_app", parent_run_id=parent["run_id"]))


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} career boundary tests)")
