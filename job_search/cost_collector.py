"""Host-only billing collector. Never packaged into the dashboard or Hermes image.

No inference or resource mutations. The only writes are an atomic sanitized local
snapshot; provider credentials stay in host-private files and HTTP headers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from http.client import HTTPException
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .cost_snapshot import (METRICS, PROVIDERS, REFRESH_SECONDS, THRESHOLDS, load_snapshot,
                           money, parse_timestamp, sanitize_snapshot, timestamp)


class CostError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CostError("provider_unavailable")


def read_key(path: Path, *, allowed_uids: set[int] | None = None, private_only: bool = False) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & (0o077 if private_only else 0o037)
                    or info.st_uid not in (allowed_uids if allowed_uids is not None else {0, os.geteuid()})
                    or info.st_size > 4096):
                raise CostError("credentials_unavailable")
            key = stream.read(4097).strip()
        if not key or len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise CostError("credentials_unavailable")
        return key
    except (OSError, UnicodeError):
        raise CostError("credentials_unavailable") from None


def fetch_json(url: str, key: str, payload: dict | None = None) -> dict:
    request = Request(url, headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json", "Accept": "application/json",
                                   "User-Agent": "job-search-cost-monitor/1.0"},
                      data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with build_opener(NoRedirect()).open(request, timeout=25) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise CostError("invalid_response")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise CostError("invalid_response")
        return result
    except HTTPError as exc:
        raise CostError("access_denied" if exc.code in {401, 403} else "provider_unavailable") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise CostError("provider_unavailable") from None
    except (ValueError, UnicodeError):
        raise CostError("invalid_response") from None


def aws_page(request: dict) -> dict:
    try:
        response = subprocess.run(["aws", "--region", "us-east-1", "--no-cli-pager", "ce", "get-cost-and-usage",
                                   "--no-paginate", "--output", "json", "--cli-input-json", json.dumps(request)],
                                  check=True, capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "AWS_MAX_ATTEMPTS": "1", "AWS_PAGER": ""})
        return json.loads(response.stdout)
    except subprocess.CalledProcessError as exc:
        # Inspect only known markers; never persist or print CLI stderr.
        code = "access_denied" if any(value in (exc.stderr or "") for value in
                                      ("AccessDenied", "Unauthorized", "ExpiredToken", "InvalidClientTokenId")) else "provider_unavailable"
        raise CostError(code) from None
    except (subprocess.SubprocessError, OSError):
        raise CostError("provider_unavailable") from None
    except ValueError:
        raise CostError("invalid_response") from None


def aws_costs(now: datetime, *, page=aws_page) -> dict:
    now = now.astimezone(timezone.utc)
    start, end = now.date().replace(day=1).isoformat(), now.date().isoformat()
    if start == end:
        return {"status": "no_data", "reason_code": "no_completed_days", "metrics": {}}
    request = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "MONTHLY",
               "Metrics": ["UnblendedCost"], "GroupBy": [{"Type": "DIMENSION", "Key": "RECORD_TYPE"}]}
    totals = {key: Decimal(0) for key in METRICS["aws"]}
    seen, estimated, data_seen = set(), False, False
    # Small account query; bounded pagination prevents unexpected polling bills.
    for _ in range(10):
        result = page(request)
        periods = result["ResultsByTime"]
        if not isinstance(periods, list):
            raise CostError("invalid_response")
        for period in periods:
            if period["TimePeriod"] != request["TimePeriod"]:
                raise CostError("invalid_response")
            estimated = estimated or period.get("Estimated") is True
            for group in period["Groups"]:
                if len(group["Keys"]) != 1:
                    raise CostError("invalid_response")
                record = group["Keys"][0]
                value = group["Metrics"]["UnblendedCost"]
                if value["Unit"] != "USD":
                    raise CostError("invalid_response")
                amount = Decimal(money(value["Amount"]))
                totals["net"] += amount
                totals["credits" if record == "Credit" else "refunds" if record == "Refund" else "charges"] += amount
                data_seen = True
        token = result.get("NextPageToken")
        if not token:
            break
        if not isinstance(token, str) or len(token) > 8192 or token in seen:
            raise CostError("invalid_response")
        seen.add(token)
        request = {**request, "NextPageToken": token}
    else:
        raise CostError("invalid_response")
    return {"status": "ok" if data_seen else "no_data", "reason_code": None if data_seen else "no_data",
            "period": {"start": start, "end": end}, "estimated": estimated,
            "metrics": {key: money(value) for key, value in totals.items()} if data_seen else {}}


def runpod_costs(key_file: Path, *, fetch=fetch_json, key_reader=read_key) -> dict:
    result = fetch("https://api.runpod.io/graphql", key_reader(key_file),
                   {"query": "query CareerPlatformCosts { myself { clientBalance clientLifetimeSpend currentSpendPerHr } }"})
    data = result["data"]["myself"]
    fields = {"clientBalance": "balance", "clientLifetimeSpend": "lifetime_usage",
              "currentSpendPerHr": "hourly_rate"}
    denied = set()
    errors = result.get("errors", [])
    if not isinstance(errors, list) or not isinstance(data, dict):
        raise CostError("invalid_response")
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        if (not isinstance(path, list) or len(path) != 2 or path[0] != "myself"
                or path[1] not in fields or error.get("extensions", {}).get("code") != "UNAUTHORIZED"
                or data.get(path[1]) is not None):
            raise CostError("invalid_response")
        denied.add(path[1])
    metrics = {metric: money(data[field]) for field, metric in fields.items() if field not in denied}
    if not metrics:
        raise CostError("access_denied")
    return {"status": "partial" if denied else "ok", "metrics": metrics,
            "reason_code": "partial_billing_access" if denied else None}


def openrouter_costs(key_file: Path | None, management_key_file: Path | None, *, fetch=fetch_json, key_reader=read_key) -> dict:
    metrics = {}
    if management_key_file is not None:
        data = fetch("https://openrouter.ai/api/v1/credits", read_key(management_key_file, private_only=True))["data"]
        purchased, usage = Decimal(money(data["total_credits"])), Decimal(money(data["total_usage"]))
        metrics.update(purchased=money(purchased), lifetime_usage=money(usage), balance=money(purchased - usage))
    if key_file is not None:
        try:
            data = fetch("https://openrouter.ai/api/v1/key", key_reader(key_file))["data"]
            metrics.update(key_usage=money(data["usage"]), key_monthly_usage=money(data["usage_monthly"]))
        except (CostError, KeyError, TypeError, ValueError):
            if not metrics:
                raise
            return {"status": "partial", "reason_code": "key_usage_unavailable", "metrics": metrics}
    if not metrics:
        raise CostError("credentials_unavailable")
    return {"status": "ok" if management_key_file else "partial", "metrics": metrics,
            "reason_code": None if management_key_file else "account_balance_not_configured"}


def collect_snapshot(collectors: dict, *, previous: dict | None = None, now: datetime | None = None,
                     thresholds: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stamp = timestamp(now)
    prior = {item["id"]: item for item in (previous or {}).get("providers", [])}
    providers = []
    for provider in PROVIDERS:
        item = {"id": provider, "status": "not_configured", "reason_code": "not_configured",
                "attempted_at": stamp, "observed_at": None, "metrics": {}}
        if provider in collectors:
            try:
                result = collectors[provider]()
                item.update(result, observed_at=stamp if result.get("metrics") else None)
                item["reason_code"] = result.get("reason_code")
            except Exception as exc:
                # A single provider failure must not hide other balances or leak
                # request headers, a GraphQL error body or AWS CLI credentials.
                item.update(prior.get(provider, {}))
                item.update(id=provider, status="error", attempted_at=stamp,
                            reason_code=exc.code if isinstance(exc, CostError) else "invalid_response")
        providers.append(item)
    return sanitize_snapshot({"schema_version": 1, "generated_at": stamp,
                              "providers": providers, "thresholds": thresholds or {}})


def publish_snapshot(path: Path, snapshot: dict, *, gid: int | None = None) -> None:
    data = (json.dumps(sanitize_snapshot(snapshot), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.parent.is_symlink() or (path.exists() and path.is_symlink()):
        raise CostError("invalid_response")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if gid is not None:
        os.chown(path.parent, -1, gid)
    fd, temporary = tempfile.mkstemp(prefix=".costs-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o640)
            if gid is not None:
                os.fchown(stream.fileno(), -1, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_attempt(path: Path) -> datetime | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.geteuid() or info.st_size > 1024):
                raise CostError("invalid_response")
            data = json.loads(stream.read(1025))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise CostError("invalid_response")
        return parse_timestamp(data["attempted_at"])
    except FileNotFoundError:
        return None


def reserve_attempt(path: Path, now: datetime, *, gid: int) -> None:
    """Durable pre-call reservation: a killed collector cannot repoll paid APIs."""
    if path.parent.is_symlink() or path.is_symlink():
        raise CostError("invalid_response")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    info = path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise CostError("invalid_response")
    os.chown(path.parent, -1, gid)
    fd, temporary = tempfile.mkstemp(prefix=".attempt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"version": 1, "attempted_at": timestamp(now)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    # Host imports are intentionally outside the credential-free view module.
    from .aws_ops import OpsError, load_config, lock
    from .operation_journal import require_idle
    try:
        config = load_config(args.config)
        options = config.get("costs", {})
        if not isinstance(options, dict) or type(options.get("enabled", False)) is not bool:
            raise ValueError("invalid cost configuration")
        if not options.get("enabled", False):
            print(json.dumps({"status": "disabled"}))
            return 0
        with lock(config):
            require_idle(config)
            root = Path(config["data_root"])
            destination = root / "costs" / "snapshot.json"
            now = datetime.now(timezone.utc)
            try:
                previous = load_snapshot(destination)
            except (OSError, ValueError, KeyError, TypeError):
                previous = None
            if previous is not None:
                age = (now - parse_timestamp(previous["generated_at"])).total_seconds()
                if age < 0:
                    raise ValueError("future snapshot")
                if 0 <= age < REFRESH_SECONDS:
                    print(json.dumps({"status": "cached"}))
                    return 0
            attempt_path = destination.parent / ".last-attempt.json"
            last_attempt = read_attempt(attempt_path)
            if last_attempt is not None:
                age = (now - last_attempt).total_seconds()
                if age < 0:
                    raise ValueError("future attempt marker")
                if age < REFRESH_SECONDS:
                    print(json.dumps({"status": "cached"}))
                    return 0
            def key_path(name: str, default: str | None) -> Path | None:
                value = options.get(name, str(root / "private" / default) if default else None)
                if value is None:
                    return None
                path = Path(value)
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError("invalid credential path")
                return path
            runpod = key_path("runpod_key_file", "runpod-api-key")
            openrouter = key_path("openrouter_key_file", "openrouter-api-key")
            management = key_path("openrouter_management_key_file", None)
            if type(options.get("aws_enabled", True)) is not bool:
                raise ValueError("invalid AWS setting")
            collectors = {}
            key_reader = lambda path: read_key(path, allowed_uids={0, os.geteuid(), int(config.get("app_uid", 10001))})
            if options.get("aws_enabled", True):
                collectors["aws"] = lambda: aws_costs(now)
            if runpod is not None:
                collectors["runpod"] = lambda: runpod_costs(runpod, key_reader=key_reader)
            if openrouter is not None or management is not None:
                collectors["openrouter"] = lambda: openrouter_costs(openrouter, management, key_reader=key_reader)
            # Validate thresholds before any paid Cost Explorer requests.
            thresholds = options.get("thresholds", {})
            if not isinstance(thresholds, dict) or set(thresholds) - THRESHOLDS.keys() or any(Decimal(money(value)) < 0 for value in thresholds.values()):
                raise ValueError("invalid thresholds")
            reserve_attempt(attempt_path, now, gid=int(config.get("app_gid", 10001)))
            snapshot = collect_snapshot(collectors, previous=previous, now=now, thresholds=thresholds)
            publish_snapshot(destination, snapshot, gid=int(config.get("app_gid", 10001)))
            print(json.dumps({"status": "published", "providers": {item["id"]: item["status"] for item in snapshot["providers"]}}))
        return 0
    except (CostError, OpsError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "unavailable", "reason_code": "host_configuration_or_operation_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
