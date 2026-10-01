#!/usr/bin/env python3
"""Offline application-bound resume context, including real MCP HTTP calls."""
from __future__ import annotations

import copy
import json
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from unittest import mock

from job_search.contracts import MutationContext
from job_search.dashboard import resume_submission_snapshot
from job_search.hermes import HermesAdapter, MAX_RESUME_CONTENT_CHARS
from job_search.hermes_mcp import make_mcp_server
from job_search.resume_lab.gateway import build_resume_lab_read_gateway
from job_search.service import JobSearchLedger
from tests.test_career_resume import Context, gateway_at, JOB
from tests.test_job_search_hermes import FakeCapabilities
from tests.test_job_search_hermes_runtime import TOKEN, mcp_request
from types import SimpleNamespace


def selected_resume(root):
    gateway, app = gateway_at(root)
    run = gateway.prepare(JOB, application_id=app, idempotency_key="prepare")
    assert gateway.handle_work({"run_id": run["run_id"]}, Context())["status"] == "succeeded"
    candidate = gateway.get_run_result(run["run_id"])["comparisons"][0]
    gateway.approve_run(run["run_id"], comparison_kind="grounded_rewrite", idempotency_key="approve")
    gateway.select_resume(app, job=JOB, artifact_id=candidate["artifact_id"],
        evaluation_id=candidate["evaluation_id"], idempotency_key="select")
    return gateway, app


def test_resume_context_uses_frozen_submitted_artifact_without_model_or_tools():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway, app = selected_resume(root)
        before = gateway.get_application_resume_content(app)
        assert before["available"] and before["provenance"]["binding"] == "selected"
        assert "Python" in before["text"]
        ledger = JobSearchLedger(root / "applications.db")
        ledger.record_submission(app, "2026-09-20T12:00:00Z", MutationContext("submit", "user", "test"),
            payload=resume_submission_snapshot(gateway, app, "selected"))
        profile = gateway.get_career_profile()
        changed = copy.deepcopy(profile["draft"]["content"])
        changed["experience"][0]["bullets"][0]["text"] = "Built unrelated Rust services."
        saved = gateway.save_career_profile(changed, expected_revision_id=profile["draft_revision_id"], idempotency_key="edit")
        gateway.approve_career_profile(saved["draft_revision_id"], idempotency_key="approve-new")
        reader = build_resume_lab_read_gateway(SimpleNamespace(resume_lab_db=root / "resume.db",
            resume_artifact_root=root / "artifacts", application_db=root / "applications.db"))
        assert reader.model is None and reader.toolchain is None
        after = reader.get_application_resume_content(app)
        assert after["text"] == before["text"] and after["provenance"]["binding"] == "submitted"
        assert "submission_event_id" in after["provenance"]
        encoded = json.dumps(after)
        for forbidden in ("tex_source", "managed_relative_path", "claims", "career_draft", str(root)):
            assert forbidden not in encoded


def test_untracked_submission_never_falls_back_to_selected_resume():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway, app = selected_resume(root)
        JobSearchLedger(root / "applications.db").record_submission(app, "2026-09-20T12:00:00Z",
            MutationContext("submit-untracked", "user", "test"), payload={"resume": {"decision": "not_tracked"}})
        assert gateway.get_application_resume_content(app)["reason"] == "submission_resume_not_tracked"


def test_missing_legacy_or_unselected_application_is_explicit():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway, app = gateway_at(root)
        assert gateway.get_application_resume_content("unknown-app")["reason"] == "application_not_found"
        assert gateway.get_application_resume_content(app)["reason"] == "resume_not_selected"
        JobSearchLedger(root / "applications.db").record_submission(app, "2026-09-20T12:00:00Z",
            MutationContext("legacy-submit", "user", "test"), payload={})
        assert gateway.get_application_resume_content(app)["reason"] == "submission_resume_not_recorded"


def test_research_or_different_job_artifact_is_never_returned():
    with tempfile.TemporaryDirectory() as directory:
        gateway, app = selected_resume(Path(directory))
        actual = gateway.service.get_artifact
        for changes in ({"purpose": "synthetic_research"}, {"variant_kind": "market_ideal"}, {"job_id": "another-job"}):
            def altered(*args, **kwargs):
                return {**actual(*args, **kwargs), **changes}
            with mock.patch.object(gateway.service, "get_artifact", side_effect=altered):
                result = gateway.get_application_resume_content(app)
                assert result["reason"] == "resume_not_factual" and "text" not in result
        def without_evaluation(*args, **kwargs):
            return {**actual(*args, **kwargs), "metadata": {}}
        with mock.patch.object(gateway.service, "get_artifact", side_effect=without_evaluation):
            assert gateway.get_application_resume_content(app)["reason"] == "resume_evaluation_unavailable"


def test_new_tool_uses_real_mcp_http_and_bounds_long_resume():
    original = FakeCapabilities().capabilities()
    calls = []
    def content(app):
        calls.append(app)
        return {"application_id": app, "available": True, "text": "x" * (MAX_RESUME_CONTENT_CHARS + 10),
            "provenance": {"binding": "submitted", "artifact_id": "real_1", "path": "/private/secret"},
            "raw_bank": "private"}
    adapter = HermesAdapter(replace(original, get_application_resume_content=content))
    server = make_mcp_server(adapter, TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, listed = mcp_request(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(t for t in listed["result"]["tools"] if t["name"] == "get_application_resume_content")
        assert status == 200 and tool["annotations"]["readOnlyHint"]
        status, response = mcp_request(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": tool["name"], "arguments": {"application_id": "app-1"}}})
        body = json.loads(response["result"]["content"][0]["text"])
        assert status == 200 and body["truncated"] and len(body["text"]) == MAX_RESUME_CONTENT_CHARS
        assert calls == ["app-1"] and "path" not in body["provenance"] and "raw_bank" not in body
        _, rejected = mcp_request(server, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": tool["name"], "arguments": {"application_id": "app-1", "artifact_id": "real_other"}}})
        assert rejected["result"]["isError"] and calls == ["app-1"]
        assert HermesAdapter(original).invoke(tool["name"], {"application_id": "app-1"})["reason"] == "resume_not_configured"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_readiness_is_bounded_and_strips_details_and_paths():
    capabilities = replace(FakeCapabilities().capabilities(), system_health=lambda: {
        "status": "attention", "readiness": {"status": "blocked", "secret": "private",
            "capabilities": [{"id": "outlook", "status": "unconfigured", "configured": False,
                "enabled": False, "reason_code": "credentials_missing", "next_action": "/private/key",
                "last_attempt_at": "2026-09-20T12:00:00Z", "last_success_at": "private token",
                "detail": "private", "path": "/private/config"}]}})
    result = HermesAdapter(capabilities).invoke("system_health")
    readiness = result["readiness"]
    assert readiness["status"] == "blocked"
    row = readiness["capabilities"][0]
    assert row["reason_code"] == "credentials_missing" and row["next_action"] is None
    assert row["last_success_at"] is None and row["last_attempt_at"] == "2026-09-20T12:00:00Z"
    assert "private" not in json.dumps(result)


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume context tests)")
