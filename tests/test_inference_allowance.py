"""A bounded daily grant releases waiting work without rewriting provider history."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3
import unittest

from job_search.contracts import ConflictError, ContractError
from job_search.db import connect
from job_search.inference.usage import AllowanceService, UsagePolicy, UsageDeferred, begin_invocation, usage_report
from job_search.scheduler import utc_stamp
from tests import test_job_search_inference_usage as usage_tests

NOW = usage_tests.NOW


class AllowanceTests(unittest.TestCase):
    setUp = usage_tests.UsageTests.setUp
    work = usage_tests.UsageTests.work
    scope = usage_tests.UsageTests.scope

    def grant(self, **overrides):
        return AllowanceService(self.path).grant(**dict(
            dict(budget_day=NOW.date().isoformat(), tokens=100, command_id="grant-1",
                 reason="User requested one more daily allowance", now=NOW), **overrides))

    def consume(self, *, policy=None):
        with self.scope(policy=policy or UsagePolicy(daily_tokens=100)):
            inv = begin_invocation("provider", "generation", b"earlier work", reserved_tokens=100)
            inv.submitting()
            inv.terminal("completed", observed_tokens=5)

    def test_grant_preserves_history_and_expires_at_utc_midnight(self):
        self.consume()
        with connect(self.path) as con:
            before = [tuple(row) for row in con.execute("SELECT * FROM inference_invocations")]
        receipt = self.grant()
        self.assertEqual(receipt["expires_at"], "2026-09-02T00:00:00Z")
        report = usage_report(self.path, now=NOW)
        self.assertEqual((report["base_limits"]["daily_tokens"], report["limits"]["daily_tokens"], report["reserved_tokens"]), (100, 200, 100))
        with connect(self.path) as con:
            self.assertEqual([tuple(row) for row in con.execute("SELECT * FROM inference_invocations")], before)
        self.work("work-b")
        with self.scope("work-b", policy=UsagePolicy(daily_tokens=100)):
            inv = begin_invocation("provider", "generation", b"new work", reserved_tokens=100)
            inv.submitting()
            inv.terminal("completed")
        self.work("work-c")
        with self.scope("work-c", policy=UsagePolicy(daily_tokens=100)), self.assertRaises(UsageDeferred):
            begin_invocation("provider", "generation", b"over grant", reserved_tokens=1)
        tomorrow = NOW + timedelta(days=1)
        report = usage_report(self.path, now=tomorrow)
        self.assertEqual((report["limits"]["daily_tokens"], report["additional_tokens"], report["reserved_tokens"]), (100, 0, 0))
        with self.scope("work-c", policy=UsagePolicy(daily_tokens=100), now=tomorrow), self.assertRaises(UsageDeferred):
            begin_invocation("provider", "generation", b"expired grant", reserved_tokens=101)

    def test_idempotent_concurrent_grants_and_immutable_audit(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: self.grant(), range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.grant(now=NOW + timedelta(days=1)), receipts[0])
        self.assertEqual(usage_report(self.path, now=NOW)["additional_tokens"], 100)
        with self.assertRaises(ConflictError):
            self.grant(tokens=101)
        for sql in ("DELETE FROM inference_allowance_grants", "UPDATE inference_allowance_grants SET tokens=200"):
            with connect(self.path) as con, self.assertRaises(sqlite3.IntegrityError):
                con.execute(sql)

    def test_only_unleased_safe_allowance_waits_are_woken(self):
        for name in ("safe", "dead", "leased", "accepted", "unknown", "polling", "ordinary"):
            self.work(name, status="queued")
        tomorrow = utc_stamp(NOW + timedelta(days=1))
        with connect(self.path) as con:
            con.execute("UPDATE work_items SET due_at=?,failure_kind='usage_deferred',lease_expires_at=NULL WHERE status='queued'", (tomorrow,))
            con.execute("UPDATE work_items SET status='dead' WHERE work_id='dead'")
            con.execute("UPDATE work_items SET lease_token='lease',lease_owner='owner' WHERE work_id='leased'")
            con.execute("UPDATE work_items SET external_outcome='unknown' WHERE work_id='unknown'")
            con.execute("UPDATE work_items SET failure_kind='inference_waiting' WHERE work_id='polling'")
            con.execute("UPDATE work_items SET failure_kind='' WHERE work_id='ordinary'")
            con.execute("UPDATE work_items SET status='running' WHERE work_id='accepted'")
        with self.scope("accepted"):
            inv = begin_invocation("provider", "generation", b"accepted", reserved_tokens=1)
            inv.submitting()
            inv.accepted("job-existing")
        with connect(self.path) as con:
            # Even inconsistent work metadata must not bypass invocation evidence.
            con.execute("UPDATE work_items SET status='queued',external_outcome='none' WHERE work_id='accepted'")
        receipt = self.grant()
        self.assertEqual(receipt["woken_work_ids"], ["safe"])
        with connect(self.path) as con:
            rows = dict(con.execute("SELECT work_id,due_at FROM work_items WHERE work_id!='work-a'"))
        self.assertEqual(rows.pop("safe"), utc_stamp(NOW))
        self.assertTrue(all(value == tomorrow for value in rows.values()))

    def test_grant_does_not_increase_requests_or_inflight_limits(self):
        policy = UsagePolicy(daily_tokens=100, daily_requests=1, max_inflight=1)
        self.consume(policy=policy)
        self.grant()
        self.work("work-b")
        with self.scope("work-b", policy=policy), self.assertRaises(UsageDeferred) as denied:
            begin_invocation("provider", "generation", b"second request", reserved_tokens=1)
        self.assertEqual(denied.exception.reason_code, "inference_daily_request_limit")
        with self.scope("work-b", policy=UsagePolicy(daily_tokens=100, max_inflight=1)):
            inv = begin_invocation("provider", "generation", b"second request", reserved_tokens=1)
            inv.submitting()
            inv.unknown()
        self.work("work-c")
        with self.scope("work-c", policy=UsagePolicy(daily_tokens=100, max_inflight=1)), self.assertRaises(UsageDeferred) as denied:
            begin_invocation("provider", "generation", b"third request", reserved_tokens=1)
        self.assertEqual(denied.exception.reason_code, "inference_inflight_limit")

    def test_invalid_grants_are_rejected_without_writes(self):
        for overrides in ({"tokens": 0}, {"tokens": True}, {"tokens": 10**12 + 1},
                          {"budget_day": "2026-09-02"}, {"budget_day": None},
                          {"reason": " "}, {"actor_kind": "agent"}, {"command_id": "bad id"}):
            with self.subTest(overrides=overrides), self.assertRaises(ContractError):
                self.grant(**overrides)
        self.assertEqual(usage_report(self.path, now=NOW)["additional_tokens"], 0)

    def test_report_uses_current_config_and_unlimited_stays_unlimited(self):
        self.grant()
        report = usage_report(self.path, now=NOW, policy=UsagePolicy(daily_tokens=200, max_inflight=1))
        self.assertEqual(report["limits"], {"daily_requests": None, "daily_tokens": 300, "max_inflight": 1})
        self.assertEqual(usage_report(self.path, now=NOW, policy=UsagePolicy())["limits"]["daily_tokens"], None)

    def test_concurrent_workers_cannot_overdraw_the_grant(self):
        self.consume()
        self.grant()
        for identity in ("work-b", "work-c"):
            self.work(identity)
        def reserve(identity):
            with self.scope(identity, policy=UsagePolicy(daily_tokens=100)):
                try:
                    begin_invocation("provider", "generation", identity.encode(), reserved_tokens=100)
                    return "reserved"
                except UsageDeferred:
                    return "deferred"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(reserve, ("work-b", "work-c"))), ["deferred", "reserved"])

    def test_cli_grant_and_report_with_current_configuration(self):
        from job_search.cli import main
        config = self.root / "config.json"
        config.write_text(json.dumps({"version": 1, "project_root": str(self.root),
            "application_db": str(self.path), "inference_usage_limits": {"daily_tokens": 100}}))
        config.chmod(0o600)
        day = datetime.now(timezone.utc).date().isoformat()
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--config", str(config), "inference-allowance-grant", "--budget-day", day,
                "--tokens", "100", "--reason", "User-requested top-up", "--idempotency-key", "cli-grant"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["additional_tokens"], 100)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config", str(config), "inference-usage"]), 0)
        self.assertEqual(json.loads(output.getvalue())["limits"]["daily_tokens"], 200)


if __name__ == "__main__":
    unittest.main()
