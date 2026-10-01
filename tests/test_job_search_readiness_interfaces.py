"""Readiness inspection and recovery preserve the dashboard authorization boundary."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sqlite3
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from job_search.dependency_snapshot import publish_snapshot, read_snapshot
from job_search.service import JobSearchLedger
from unittest.mock import patch, Mock

from job_search import cli as job_search_app
from job_search.runtime import RuntimeConfigV1, build_runtime
from job_search.runtime_readiness import runtime_readiness
from tests.test_job_search_dashboard import dashboard, request, session


def test_unconfigured_inspection_never_initializes_state_or_calls_provider():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        root = Path(directory)
        config = RuntimeConfigV1.defaults(root)
        with patch("urllib.request.urlopen", side_effect=AssertionError("external request")):
            result = runtime_readiness(config)
        assert result["schema_version"] == 1
        assert result["external_services_verified"] is False
        assert result["release"]["source_sha"] is None
        assert not config.application_db.exists()
        assert not list(root.iterdir())


def test_cli_readiness_is_read_only_and_returns_blocked_details_as_json():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        root = Path(directory)
        config = root / "config.json"
        config.write_text(json.dumps({"version": 1, "project_root": str(root)}))
        config.chmod(0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = job_search_app.main(["--config", str(config), "readiness"])
        result = json.loads(output.getvalue())
        assert code == 0 and "capabilities" in result
        assert not (root / "job-search.db").exists()


def test_readiness_views_and_recovery_enforce_csrf_and_current_revision():
    with dashboard() as (server, controller, ledger, preferences):
        cookie, csrf = session(server)
        for path in ("/api/v1/ops", "/api/v1/ops/readiness", "/api/v1/ops/work"):
            status, _, body = request(server, "GET", path, headers={"Cookie": cookie})
            assert status == 200, body
        route = "/api/v1/ops/work/work-1/retry"
        body = {"idempotency_key": "recover-1", "expected_revision": 2}
        port = server.server_address[1]
        headers = {"Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"}
        with patch("job_search.recovery.RecoveryService.retry", return_value={"status": "queued"}) as retry:
            assert request(server, "POST", route, body, headers)[0] == 403
            retry.assert_not_called()
            headers["X-CSRF-Token"] = csrf
            assert request(server, "POST", route, {**body, "expected_revision": True}, headers)[0] == 400
            retry.assert_not_called()
            assert request(server, "POST", route, body, headers)[0] == 200
            assert retry.call_args.kwargs["expected_revision"] == 2
            assert retry.call_args.kwargs["command_id"] == "recover-1"
            assert retry.call_args.kwargs["actor_kind"] == "user"


def test_snapshot_only_exposes_codes_and_expires_without_reading_secrets():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        root = Path(directory)
        config = RuntimeConfigV1.defaults(root)
        # The production migration owns this exact table; this fixture also works
        # against the previous schema to exercise the upgrade read path.
        JobSearchLedger(config.application_db)
        with sqlite3.connect(config.application_db) as con:
            con.execute("CREATE TABLE IF NOT EXISTS runtime_dependency_snapshot(singleton INTEGER PRIMARY KEY CHECK(singleton=1),observed_at TEXT NOT NULL,source_revision TEXT NOT NULL DEFAULT '',capabilities_json TEXT NOT NULL)")
        stamp = datetime.now(timezone.utc)
        dependencies = {"inference": {"configured": True, "status": "configuration_ready", "secret": "NEVER-EXPOSE", "path": "/private/model"}}
        publish_snapshot(config.application_db, dependencies, now=stamp)
        with patch("job_search.runtime_readiness.dependency_health", side_effect=AssertionError("read private configuration")):
            report = runtime_readiness(config, use_snapshot=True)
        assert "NEVER-EXPOSE" not in json.dumps(report) and "/private/" not in json.dumps(report)
        assert any(item["id"] == "inference" and item["status"] == "configured_unverified" for item in report["capabilities"])
        assert read_snapshot(config.application_db, now=stamp+timedelta(minutes=16))[0]["status"] == "stale"
        with patch.dict(os.environ, {"JOB_SEARCH_SOURCE_REVISION": "a"*40}):
            assert read_snapshot(config.application_db, now=stamp)[0]["reason_code"] == "dependency_snapshot_release_changed"
        missing = root / "missing.db"
        assert read_snapshot(missing)[0]["status"] == "configured_unverified"
        assert not missing.exists()


def test_notification_reconciliation_requires_dashboard_csrf_and_human_context():
    with dashboard() as (server, controller, ledger, preferences):
        cookie, csrf = session(server)
        controller.notification_recovery = Mock()
        controller.notification_recovery.reconcile.return_value = {"status": "delivered"}
        route = "/api/v1/ops/notifications/notice-1/reconcile"
        body = {"idempotency_key": "delivery-decision-1", "expected_attempts": 1,
                "expected_payload_sha256": "a"*64, "outcome": "delivered"}
        headers = {"Cookie": cookie, "Origin": f"http://127.0.0.1:{server.server_address[1]}"}
        assert request(server, "POST", route, body, headers)[0] == 403
        controller.notification_recovery.reconcile.assert_not_called()
        headers["X-CSRF-Token"] = csrf
        assert request(server, "POST", route, body, headers)[0] == 200
        call = controller.notification_recovery.reconcile.call_args
        assert call.kwargs["context"].actor_kind == "user"
        assert call.kwargs["context"].source_kind == "dashboard_notification_recovery"
        assert call.kwargs["expected_payload_sha256"] == "a"*64


def test_idle_inference_limits_use_current_configuration_without_a_paid_request():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        config = replace(RuntimeConfigV1.defaults(Path(directory)), inference_usage_limits={"daily_requests": 2})
        JobSearchLedger(config.application_db)
        with patch("job_search.runtime_readiness.dependency_health", side_effect=AssertionError("private provider configuration")):
            report = runtime_readiness(config, use_snapshot=True)
            assert report["inference_usage"]["configured"]
            assert report["inference_usage"]["limits"]["daily_requests"] == 2
            assert report["inference_usage"]["reserved_requests"] == 0
            assert next(item for item in report["capabilities"] if item["id"] == "inference_usage")["status"] == "ready"
            changed = runtime_readiness(replace(config, inference_usage_limits={"daily_requests": 1}), use_snapshot=True)
            assert changed["inference_usage"]["limits"]["daily_requests"] == 1


def test_inference_cli_usage_does_not_initialize_missing_state():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        root = Path(directory)
        config = root / "config.json"
        config.write_text(json.dumps({"version": 1, "project_root": str(root)}))
        config.chmod(0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = job_search_app.main(["--config", str(config), "inference-usage"])
        assert code == 2 and json.loads(output.getvalue())["status"] == "rejected"
        assert list(root.iterdir()) == [config]


def test_shared_runtime_publishes_snapshot_without_cloud_wrapper():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        config = RuntimeConfigV1.defaults(Path(directory))
        core = build_runtime(config, lane="core", base_environment={}, max_work_per_tick=0, max_outbox_per_tick=0)
        core.tick()
        observed = read_snapshot(config.application_db)
        assert {item["id"] for item in observed} == {"inference", "resume", "notification_transport"}
        assert all(item["status"] == "disabled" for item in observed)


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} readiness interface tests)")


if __name__ == "__main__":
    main()
