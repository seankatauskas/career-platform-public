"""Offline provider billing contracts, safe cache and dashboard authorization."""
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from job_search import cost_collector as collector
from job_search.cost_snapshot import load_snapshot, read_cost_snapshot, money, timestamp
from tests.test_job_search_dashboard import dashboard, request, session

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def snapshot(now=NOW):
    return collector.collect_snapshot({
        "aws": lambda: {"status": "ok", "metrics": {"charges": "8.50", "credits": "-8", "refunds": "0", "net": "0.50"},
                       "period": {"start": "2026-10-01", "end": "2026-10-02"}, "estimated": True},
        "runpod": lambda: {"status": "ok", "metrics": {"balance": "5.33", "lifetime_usage": "4.67", "hourly_rate": "0"}},
        "openrouter": lambda: {"status": "partial", "reason_code": "account_balance_not_configured", "metrics": {"key_usage": ".014418", "key_monthly_usage": ".01"}},
    }, now=now)


@contextmanager
def keyfile():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "key"
        path.write_text("private-fixture-key\n")
        path.chmod(0o600)
        yield path


def test_aws_pagination_signed_adjustments_and_utc_period():
    calls = []
    def page(payload):
        calls.append(payload)
        rows = [("Usage", "12.123456"), ("Credit", "-10")] if len(calls) == 1 else [("Refund", "-1"), ("Tax", ".25")]
        return {"ResultsByTime": [{"TimePeriod": payload["TimePeriod"], "Estimated": True,
                    "Groups": [{"Keys": [kind], "Metrics": {"UnblendedCost": {"Amount": value, "Unit": "USD"}}} for kind, value in rows]}],
                **({"NextPageToken": "second"} if len(calls) == 1 else {})}
    result = collector.aws_costs(NOW, page=page)
    assert len(calls) == 2 and calls[1]["NextPageToken"] == "second"
    assert calls[0]["TimePeriod"] == {"Start": "2026-10-01", "End": "2026-10-02"}
    assert calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "RECORD_TYPE"}]
    assert result["metrics"] == {"charges": "12.373456", "credits": "-10.000000", "refunds": "-1.000000", "net": "1.373456"}
    assert result["estimated"] is True


def test_aws_no_completed_days_or_empty_response_never_means_zero():
    fetch = Mock(side_effect=AssertionError("paid API must not be called"))
    first = collector.aws_costs(NOW.replace(day=1), page=fetch)
    assert first["reason_code"] == "no_completed_days" and first["metrics"] == {}
    empty = collector.aws_costs(NOW, page=lambda payload: {"ResultsByTime": []})
    assert empty["status"] == "no_data" and empty["metrics"] == {}


def test_aws_rejects_mixed_currency_wrong_period_and_pagination_cycle():
    def payload(**change):
        return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
                "Groups": [{"Keys": ["Usage"], "Metrics": {"UnblendedCost": {"Amount": "1", "Unit": "USD"}}}]}], **change}
    invalid = payload()
    invalid["ResultsByTime"][0]["Groups"][0]["Metrics"]["UnblendedCost"]["Unit"] = "EUR"
    for raw in (invalid, {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-01", "End": "2026-09-30"}, "Groups": []}]}, payload(NextPageToken="repeat")):
        try:
            collector.aws_costs(NOW, page=lambda request: raw)
        except collector.CostError as exc:
            assert exc.code == "invalid_response"
        else:
            raise AssertionError("invalid AWS response accepted")


def test_runpod_only_queries_billing_fields_and_drops_private_response_fields():
    with keyfile() as path:
        fetch = Mock(return_value={"data": {"myself": {"clientBalance": 5.33, "clientLifetimeSpend": 4.67, "currentSpendPerHr": 0,
                                                        "email": "DO-NOT-PUBLISH", "apiKeys": ["SECRET"]}}})
        result = collector.runpod_costs(path, fetch=fetch)
        assert result["metrics"]["balance"] == "5.330000"
        assert result["metrics"]["hourly_rate"] == "0.000000"
        assert fetch.call_args.args[0] == "https://api.runpod.io/graphql"
        assert "clientLifetimeSpend" in fetch.call_args.args[2]["query"]
        assert "apiKeys" not in fetch.call_args.args[2]["query"]
        assert "SECRET" not in json.dumps(result) and "DO-NOT-PUBLISH" not in json.dumps(result)


def test_openrouter_account_balance_is_not_key_limit_and_usage_is_scoped():
    with keyfile() as path:
        fetch = Mock(side_effect=[{"data": {"total_credits": 10, "total_usage": .014418}},
                                  {"data": {"usage": .01, "usage_monthly": .007, "limit_remaining": 500, "label": "PRIVATE"}}])
        result = collector.openrouter_costs(path, path, fetch=fetch)
        assert result["metrics"]["balance"] == "9.985582"
        assert result["metrics"]["key_monthly_usage"] == "0.007000"
        assert "500" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
        normal = Mock(return_value={"data": {"usage": .01, "usage_monthly": .007, "limit_remaining": 500}})
        partial = collector.openrouter_costs(path, None, fetch=normal)
        assert "balance" not in partial["metrics"] and partial["status"] == "partial"
        assert normal.call_count == 1 and normal.call_args.args[0].endswith("/key")


def test_provider_requests_identify_the_client_without_changing_auth_or_redirect_policy():
    opener = Mock()
    opener.open.return_value = BytesIO(b"{}")
    with patch.object(collector, "build_opener", return_value=opener) as build:
        assert collector.fetch_json("https://api.runpod.io/graphql", "fixture-key", {"query": "fixture"}) == {}
    request = opener.open.call_args.args[0]
    assert request.get_header("User-agent") == "job-search-cost-monitor/1.0"
    assert request.get_header("Authorization") == "Bearer fixture-key"
    assert isinstance(build.call_args.args[0], collector.NoRedirect)


def test_runpod_field_level_denial_preserves_available_balance_without_inventing_usage():
    with keyfile() as path:
        result = {"data": {"myself": {"clientBalance": 5.33, "clientLifetimeSpend": None, "currentSpendPerHr": 0}},
                  "errors": [{"message": "Unauthorized", "path": ["myself", "clientLifetimeSpend"],
                              "extensions": {"code": "UNAUTHORIZED"}}]}
        partial = collector.runpod_costs(path, fetch=Mock(return_value=result))
        assert partial["status"] == "partial" and partial["reason_code"] == "partial_billing_access"
        assert partial["metrics"] == {"balance": "5.330000", "hourly_rate": "0.000000"}
        view = collector.collect_snapshot({"runpod": lambda: partial}, now=NOW)
        assert view["providers"][1]["metrics"] == partial["metrics"]
        for change in ({"extensions": {"code": "INTERNAL_ERROR"}}, {"path": ["unexpected"]}):
            invalid = {**result, "errors": [{**result["errors"][0], **change}]}
            try:
                collector.runpod_costs(path, fetch=Mock(return_value=invalid))
            except collector.CostError as exc:
                assert exc.code == "invalid_response"
            else:
                raise AssertionError("unexpected GraphQL failure accepted as valid billing")


def test_failure_keeps_last_good_figures_with_original_timestamp_and_no_secret():
    old = snapshot()
    def failing():
        raise RuntimeError("Authorization: Bearer DO-NOT-PUBLISH /private/key")
    new = collector.collect_snapshot({"aws": failing, "runpod": failing}, previous=old, now=NOW + timedelta(days=1))
    assert new["providers"][0]["metrics"] == old["providers"][0]["metrics"]
    assert new["providers"][0]["observed_at"] == timestamp(NOW)
    assert new["providers"][0]["attempted_at"] == timestamp(NOW + timedelta(days=1))
    assert new["providers"][0]["status"] == "error"
    assert new["providers"][2]["status"] == "not_configured" and new["providers"][2]["metrics"] == {}
    assert "DO-NOT-PUBLISH" not in json.dumps(new)


def test_missing_corrupt_oversize_symlink_future_and_nonfinite_snapshots_fail_closed():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "snapshot.json"
        assert read_cost_snapshot(None)["reason_code"] == "not_configured"
        assert read_cost_snapshot(path)["reason_code"] == "snapshot_missing"
        assert list(Path(directory).iterdir()) == []
        for text in ("not json", "x" * 65537, json.dumps({**snapshot(), "schema_version": True}), json.dumps(snapshot(NOW + timedelta(days=1)))):
            path.write_text(text)
            assert read_cost_snapshot(path, now=NOW)["reason_code"] == "snapshot_invalid"
        invalid = snapshot()
        invalid["providers"][0]["metrics"]["net"] = "NaN"
        path.write_text(json.dumps(invalid))
        assert read_cost_snapshot(path, now=NOW)["reason_code"] == "snapshot_invalid"
        link = Path(directory) / "link"
        link.symlink_to(path)
        assert read_cost_snapshot(link, now=NOW)["reason_code"] == "snapshot_invalid"
    for value in (True, None, "NaN", "Infinity", "1e100", "secret"):
        try:
            money(value)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid amount accepted")


def test_atomic_snapshot_is_sanitized_read_only_and_stale_with_opt_in_warnings():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "costs" / "snapshot.json"
        data = snapshot()
        data["raw_error"] = "PRIVATE"
        data["providers"][0]["api_key"] = "SECRET"
        data["thresholds"] = {"aws_monthly_charges_usd": "5", "runpod_balance_usd": "6"}
        collector.publish_snapshot(path, data)
        assert path.stat().st_mode & 0o777 == 0o640
        assert "PRIVATE" not in path.read_text() and "SECRET" not in path.read_text()
        before = path.stat().st_mtime_ns
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), patch("subprocess.run", side_effect=AssertionError("provider CLI")):
            view = read_cost_snapshot(path, now=NOW + timedelta(hours=37))
        assert path.stat().st_mtime_ns == before
        assert view["available"] is True and all(item["stale"] for item in view["providers"])
        assert len([alert for alert in view["alerts"] if alert["kind"] == "threshold"]) == 2
        next_month = read_cost_snapshot(path, now=NOW.replace(month=11))
        assert not any(alert["provider"] == "aws" and alert["kind"] == "threshold" for alert in next_month["alerts"])


def test_credentials_permissions_redirect_and_transport_errors_are_safe():
    with keyfile() as path:
        assert collector.read_key(path) == "private-fixture-key"
        for mode in (0o666, 0o620, 0o607, 0o650):
            path.chmod(mode)
            try:
                collector.read_key(path)
            except collector.CostError as exc:
                assert exc.code == "credentials_unavailable"
            else:
                raise AssertionError("unsafe key permissions accepted")
        path.chmod(0o640)
        assert collector.read_key(path) == "private-fixture-key"
        try:
            collector.read_key(path, private_only=True)
        except collector.CostError:
            pass
        else:
            raise AssertionError("management key group-readable")
    try:
        collector.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")
    except collector.CostError:
        pass
    else:
        raise AssertionError("credential redirect permitted")


def test_dashboard_cost_endpoint_uses_authorized_local_snapshot_without_network():
    with tempfile.TemporaryDirectory() as directory, dashboard() as (server, controller, ledger, preferences):
        path = Path(directory) / "costs.json"
        collector.publish_snapshot(path, snapshot(datetime.now(timezone.utc)))
        controller.cost_snapshot_path = path
        cookie, _ = session(server)
        with patch("job_search.cost_collector.aws_page", side_effect=AssertionError("billing call")):
            status, _, body = request(server, "GET", "/api/v1/ops/costs", headers={"Cookie": cookie})
            assert status == 200 and json.loads(body)["available"] is True
            status, _, body = request(server, "GET", "/api/v1/ops", headers={"Cookie": cookie})
            assert status == 200 and "costs" in json.loads(body)
            assert request(server, "GET", "/api/v1/ops/costs", headers={"Cookie": cookie, "Host": "evil.invalid"})[0] != 200
            assert request(server, "POST", "/api/v1/ops/costs", {}, {"Cookie": cookie})[0] == 403


def test_cli_is_opt_in_cached_and_does_not_call_providers_for_invalid_thresholds():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = {"data_root": str(root), "app_gid": os.getgid(), "costs": {"enabled": False}}
        fresh = snapshot(datetime.now(timezone.utc))
        old = snapshot(datetime.now(timezone.utc) - timedelta(days=2))
        @contextmanager
        def fake_lock(config):
            yield
        with patch("job_search.aws_ops.load_config", return_value=config), patch("job_search.aws_ops.lock", fake_lock), patch("job_search.operation_journal.require_idle"), redirect_stdout(StringIO()):
            with patch.object(collector, "collect_snapshot", side_effect=AssertionError("external access")):
                assert collector.main(["--config", str(root / "config")]) == 0
                assert list(root.iterdir()) == []
                config["costs"]["enabled"] = True
                collector.publish_snapshot(root / "costs" / "snapshot.json", fresh)
                assert collector.main(["--config", str(root / "config")]) == 0
                collector.publish_snapshot(root / "costs" / "snapshot.json", old)
                config["costs"]["thresholds"] = {"unknown": 1}
                assert collector.main(["--config", str(root / "config")]) == 1


def test_attempt_reservation_survives_publish_failure_and_malformed_marker_fails_closed():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = {"data_root": str(root), "app_gid": os.getgid(), "costs": {"enabled": True}}
        @contextmanager
        def fake_lock(config):
            yield
        with patch("job_search.aws_ops.load_config", return_value=config), patch("job_search.aws_ops.lock", fake_lock), patch("job_search.operation_journal.require_idle"), redirect_stdout(StringIO()):
            fake_collect = Mock(return_value=snapshot(datetime.now(timezone.utc)))
            with patch.object(collector, "collect_snapshot", fake_collect), patch.object(collector, "publish_snapshot", side_effect=OSError("disk failure")):
                assert collector.main(["--config", str(root / "config")]) == 1
                marker = root / "costs" / ".last-attempt.json"
                assert marker.stat().st_mode & 0o777 == 0o600
                assert collector.main(["--config", str(root / "config")]) == 0
                assert fake_collect.call_count == 1
            with patch.object(collector, "collect_snapshot", side_effect=AssertionError("paid API call")):
                for malformed in ("invalid", "null", "[]", json.dumps({"version": True, "attempted_at": timestamp(NOW)})):
                    marker.write_text(malformed)
                    assert collector.main(["--config", str(root / "config")]) == 1
                marker.write_text(json.dumps({"version": 1, "attempted_at": timestamp(datetime.now(timezone.utc) + timedelta(days=1))}))
                assert collector.main(["--config", str(root / "config")]) == 1


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} cost dashboard tests)")


if __name__ == "__main__":
    main()
