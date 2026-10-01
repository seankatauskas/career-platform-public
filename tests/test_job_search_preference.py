#!/usr/bin/env python3
"""Offline contract tests for the ranking/application gateway."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.preference import PreferenceGateway, PreferencePaths


def gateway() -> PreferenceGateway:
    root = Path(tempfile.gettempdir())
    return PreferenceGateway(PreferencePaths(root / "jobs.db", root / "pref.db", root / "proxy.db"))


def test_shortlist_passes_only_concrete_exclusions():
    with patch("job_search.preference.create_shortlist_session") as call:
        call.return_value = {"recommendations": []}
        result = gateway().create_shortlist(
            {"policy": "champion"}, idempotency_key="one", actor="dashboard",
            excluded_job_keys=[("ASHBY", "1"), ("", "ignored")],
        )
    assert result == {"recommendations": []}
    assert call.call_args.kwargs["excluded_job_keys"] == {("ashby", "1")}


def test_notification_preview_is_read_only_until_materialized():
    candidate = {
        "recommendations": [],
        "options": {"policy": "champion"},
    }
    with patch(
        "job_search.preference.policy_recommendations", return_value=candidate
    ) as preview:
        with patch("job_search.preference.prepare_preferences") as prepare:
            with patch(
                "job_search.preference.record_recommendation_impressions",
                return_value={**candidate, "session_id": "session-one"},
            ) as record:
                subject = gateway()
                assert subject.preview_shortlist(
                    {"policy": "champion"},
                    excluded_job_keys=[("ASHBY", "1"), ("", "ignored")],
                ) == candidate
                prepare.assert_not_called()
                record.assert_not_called()
                saved = subject.record_notification_shortlist(
                    dict(candidate), workflow_id="workflow-one"
                )

    assert saved["session_id"] == "session-one"
    prepare.assert_called_once()
    assert preview.call_args.kwargs["excluded_job_keys"] == {("ashby", "1")}
    assert record.call_args.kwargs == {
        "idempotency_key": "notification-shortlist:workflow-one",
        "actor": "notification",
    }


def test_notification_exposure_is_scoped_to_materialized_workflows():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        jobs_db = root / "jobs.db"
        with sqlite3.connect(jobs_db) as con:
            con.executescript(
                """
                CREATE TABLE recommendation_sessions (
                    session_id TEXT PRIMARY KEY,
                    idempotency_key TEXT
                );
                CREATE TABLE recommendation_impressions (
                    session_id TEXT NOT NULL,
                    ats TEXT NOT NULL,
                    job_id TEXT NOT NULL
                );
                INSERT INTO recommendation_sessions VALUES
                    ('alert-session', 'notification-shortlist:workflow-one'),
                    ('dashboard-session', 'dashboard:unrelated');
                INSERT INTO recommendation_impressions VALUES
                    ('alert-session', 'ashby', 'one'),
                    ('dashboard-session', 'lever', 'two');
                """
            )
        subject = PreferenceGateway(
            PreferencePaths(jobs_db, root / "pref.db", root / "proxy.db")
        )
        exposed = subject.notification_exposed_job_keys(
            ["workflow-one"], [("ASHBY", "one"), ("lever", "two")]
        )
        assert exposed == {("ashby", "one")}


def test_outbox_delivery_uses_event_as_unique_source():
    with patch("job_search.preference.save_recommendation_feedback") as call:
        call.return_value = {"ok": True, "created": True}
        result = gateway().deliver_applied_feedback(
            {"ats": "greenhouse", "job_id": "42"}, source_event_id="event-42",
        )
    assert result["created"]
    assert call.call_args.kwargs["source_event_id"] == "application_event:event-42"
    assert call.call_args.args[1]["action"] == "applied"


def test_outbox_delivery_requires_stable_job_key():
    try:
        gateway().deliver_applied_feedback({"ats": "ashby"}, source_event_id="event")
    except ContractError:
        pass
    else:
        raise AssertionError("missing job id was accepted")


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search preference tests)")


if __name__ == "__main__":
    main()
