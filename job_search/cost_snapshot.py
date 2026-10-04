"""Read-only, credential-free cost snapshot contract shared with the dashboard.

Billing totals, prepaid balances, and per-key usage have different scopes. They
are deliberately never added into a misleading cross-provider grand total.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import stat

VERSION = 1
REFRESH_SECONDS = 86400
STALE_SECONDS = 36 * 3600
MAX_BYTES = 65536
PROVIDERS = ("aws", "runpod", "openrouter")
METRICS = {
    "aws": {"charges": "Charges before credits / refunds", "credits": "Credit adjustments",
            "refunds": "Refund adjustments", "net": "Net reported cost"},
    "runpod": {"balance": "Account credit balance", "lifetime_usage": "Lifetime account usage",
               "hourly_rate": "Current account rate / hour"},
    "openrouter": {"balance": "Account credit balance", "purchased": "Lifetime credits purchased",
                   "lifetime_usage": "Lifetime account usage", "key_usage": "This key · lifetime usage",
                   "key_monthly_usage": "This key · provider-reported monthly usage"},
}
REASONS = {
    "not_configured": "Cost collection is not configured for this provider.",
    "credentials_unavailable": "The host could not read this provider's billing credential.",
    "access_denied": "Billing access was denied. Check the host's read-only billing permissions.",
    "partial_billing_access": "This key can read only some billing fields. Unavailable figures are omitted, not reported as zero.",
    "provider_unavailable": "The billing service could not be reached. Previous figures are retained when available.",
    "invalid_response": "The billing service returned an incomplete or unsupported response.",
    "account_balance_not_configured": "Account credit balance needs an OpenRouter management key on the host. Shown usage covers only the configured inference key.",
    "key_usage_unavailable": "Account totals are available, but usage for the configured inference key is unavailable.",
    "no_completed_days": "This UTC month has no completed days yet. No month-to-date total is reported.",
    "no_data": "The billing provider has not reported data for this period yet.",
    "snapshot_missing": "The host has not published a cost snapshot yet.",
    "snapshot_invalid": "The saved cost snapshot could not be validated.",
}
THRESHOLDS = {"aws_monthly_charges_usd": ("aws", "charges", "above"),
              "runpod_balance_usd": ("runpod", "balance", "below"),
              "openrouter_balance_usd": ("openrouter", "balance", "below")}


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) != 20 or not value.endswith("Z"):
        raise ValueError("invalid timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def money(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid money")
    if len(str(value)) > 64:
        raise ValueError("invalid money")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid money") from None
    if not amount.is_finite() or abs(amount) > Decimal("1000000000000"):
        raise ValueError("invalid money")
    return format(amount.quantize(Decimal("0.000001")), "f")


def sanitize_snapshot(raw: dict) -> dict:
    """Explicit allowlist; raw provider bodies, errors, account IDs and keys never pass."""
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != VERSION:
        raise ValueError("unsupported snapshot")
    generated = timestamp(parse_timestamp(raw["generated_at"]))
    providers = []
    rows = raw["providers"]
    if not isinstance(rows, list) or len(rows) != len(PROVIDERS):
        raise ValueError("invalid providers")
    for provider_id in PROVIDERS:
        matches = [item for item in rows if isinstance(item, dict) and item.get("id") == provider_id]
        if len(matches) != 1:
            raise ValueError("invalid providers")
        item = matches[0]
        status = item["status"]
        if status not in {"ok", "partial", "error", "not_configured", "no_data"}:
            raise ValueError("invalid status")
        reason = item.get("reason_code")
        if reason is not None and reason not in REASONS:
            raise ValueError("invalid reason")
        metrics = item.get("metrics", {})
        if not isinstance(metrics, dict) or set(metrics) - METRICS[provider_id].keys():
            raise ValueError("invalid metrics")
        observed = item.get("observed_at")
        attempted = timestamp(parse_timestamp(item["attempted_at"]))
        if observed is not None:
            observed = timestamp(parse_timestamp(observed))
            if observed > attempted:
                raise ValueError("invalid observation time")
        if metrics and observed is None:
            raise ValueError("metrics lack observation time")
        period = item.get("period")
        if period is not None:
            start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
            if start >= end or (end - start).days > 31:
                raise ValueError("invalid period")
            period = {"start": start.isoformat(), "end": end.isoformat()}
        providers.append({"id": provider_id, "status": status, "reason_code": reason,
                          "attempted_at": attempted, "observed_at": observed,
                          "period": period, "estimated": item.get("estimated") is True,
                          "metrics": {key: money(value) for key, value in metrics.items()}})
    thresholds = raw.get("thresholds", {})
    if not isinstance(thresholds, dict) or set(thresholds) - THRESHOLDS.keys():
        raise ValueError("invalid thresholds")
    thresholds = {key: money(value) for key, value in thresholds.items()}
    if any(Decimal(value) < 0 for value in thresholds.values()):
        raise ValueError("invalid thresholds")
    return {"schema_version": VERSION, "generated_at": generated,
            "providers": providers, "thresholds": thresholds}


def load_snapshot(path: Path) -> dict:
    """Bounded regular-file read, with no writes, credentials or network access."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("invalid snapshot file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("invalid snapshot file")
    return sanitize_snapshot(json.loads(data))


def read_cost_snapshot(path: Path | None, *, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    unavailable = {"schema_version": VERSION, "available": False, "providers": [],
                   "alerts": [], "refresh_seconds": REFRESH_SECONDS}
    if path is None:
        return {**unavailable, "reason_code": "not_configured", "message": "Cost collection is not configured for this installation."}
    try:
        snapshot = load_snapshot(path)
        if parse_timestamp(snapshot["generated_at"]) > now + timedelta(minutes=5):
            raise ValueError("future snapshot")
        if any(parse_timestamp(item["attempted_at"]) > now + timedelta(minutes=5) for item in snapshot["providers"]):
            raise ValueError("future observation")
    except FileNotFoundError:
        return {**unavailable, "reason_code": "snapshot_missing", "message": REASONS["snapshot_missing"]}
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {**unavailable, "reason_code": "snapshot_invalid", "message": REASONS["snapshot_invalid"]}
    alerts = []
    for item in snapshot["providers"]:
        observed = item["observed_at"]
        item["stale"] = observed is not None and (now - parse_timestamp(observed)).total_seconds() > STALE_SECONDS
        item["message"] = REASONS.get(item["reason_code"], "")
        item["metric_labels"] = METRICS[item["id"]]
        if item["stale"] or item["status"] == "error":
            alerts.append({"provider": item["id"], "kind": "stale" if item["stale"] else "unavailable"})
        for key, (provider, metric, direction) in THRESHOLDS.items():
            if provider != item["id"] or key not in snapshot["thresholds"] or metric not in item["metrics"]:
                continue
            # Old-month figures must not trigger a new month's budget warning.
            if provider == "aws" and (not item["period"] or item["period"]["start"] != now.date().replace(day=1).isoformat()):
                continue
            value, threshold = Decimal(item["metrics"][metric]), Decimal(snapshot["thresholds"][key])
            if (value >= threshold if direction == "above" else value <= threshold):
                alerts.append({"provider": provider, "kind": "threshold", "metric": metric,
                               "threshold": str(threshold), "direction": direction,
                               "stale": item["stale"] or item["status"] == "error"})
    return {**snapshot, "available": True, "alerts": alerts, "refresh_seconds": REFRESH_SECONDS,
            "next_refresh_due_at": timestamp(parse_timestamp(snapshot["generated_at"]) + timedelta(seconds=REFRESH_SECONDS))}
