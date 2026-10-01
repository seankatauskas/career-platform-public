"""Compose credential-free diagnostics without starting workers or creating state."""
from __future__ import annotations

from contextlib import closing
import os
import re
import sqlite3
from typing import Any, Mapping, Optional

from .dependency_health import dependency_health
from .runtime import RuntimeConfigV1


def runtime_usage(config: RuntimeConfigV1) -> dict[str, Any]:
    from .inference.usage import UsagePolicy, usage_report
    usage = usage_report(config.application_db)
    limits = UsagePolicy.from_mapping(config.inference_usage_limits).mapping()
    usage.update(limits=limits, configured=any(value is not None for value in limits.values()))
    return usage


def runtime_readiness(
    config: RuntimeConfigV1, *, dependencies: Optional[Mapping[str, Any]] = None,
    automation_enabled: bool = True, use_snapshot: bool = False,
) -> dict[str, Any]:
    from .readiness import readiness_report, _report

    report = dict(readiness_report(
        config.application_db, dependencies=dependencies if dependencies is not None else (None if use_snapshot else dependency_health(config)),
        automation_enabled=automation_enabled,
    ))
    if use_snapshot and dependencies is None:
        from .dependency_snapshot import read_snapshot
        report = _report(report["checked_at"], report["capabilities"] + read_snapshot(config.application_db), report["metrics"])
    try:
        usage = runtime_usage(config)
    except sqlite3.Error:
        usage = None  # No initialized usage ledger; database readiness explains it.
    if usage is not None:
        from .readiness import _capability
        # Every process can read the non-secret runtime limits. Use the current
        # configuration even before its first request or after a limit changes.
        limits = usage["limits"]
        state, reason, action = "ready", "inference_usage_within_limits", "none"
        if usage["uncertain"]:
            state, reason, action = "blocked", "inference_reconciliation_required", "inspect_inference_recovery"
        elif not usage["configured"]:
            state, reason, action = "disabled", "inference_limits_not_configured", "review_usage_limits"
        else:
            for field, count, code in (
                ("daily_requests", usage["reserved_requests"], "inference_daily_request_limit"),
                ("daily_tokens", usage["reserved_tokens"], "inference_daily_token_limit"),
                ("max_inflight", usage["inflight"], "inference_inflight_limit"),
            ):
                if limits[field] is not None and count >= limits[field]:
                    state, reason = "paused", code
                    break
        report = _report(report["checked_at"], report["capabilities"] + [_capability(
            "inference_usage", state, configured=usage["configured"], enabled=usage["configured"],
            reason=reason, action=action,
        )], report["metrics"])
        report["inference_usage"] = usage
    if config.shortlist_policy != "champion":
        from .ranking.refresh import inspect_policies
        from .readiness import _capability
        policies=inspect_policies(config.preference_db,config.proxy_db,config.jobs_db)
        extra=[_capability("ranking_"+name, row['status'], reason=row['reason'], action="inspect_ranking_setup" if row['status'] != 'ready' else 'none') for name,row in policies.items()]
        usage=report.get("inference_usage")
        report=_report(report['checked_at'],report['capabilities']+extra,report['metrics'])
        report['ranking_policies']=policies
        if usage is not None: report['inference_usage']=usage
    # Use configuration to describe configuration, never schedule enablement.
    from .scheduler import has_real_scraper_contact
    configured = {
        "ats.ingestion": has_real_scraper_contact(config.environment({})) and
            (config.board_registry_path is None or config.board_registry_path.is_file()),
        "ats.discovery": has_real_scraper_contact(config.environment({})),
        "outlook": bool(config.outlook_client_id),
        "notifications": bool(config.hermes_telegram_target and config.hermes_executable),
        "ranking": all(row.get("artifact_present") for row in report.get("ranking_policies", {}).values())
            if report.get("ranking_policies") else config.preference_db.is_file(),
        "shortlist": config.preference_db.is_file(),
    }
    for item in report["capabilities"]:
        if item["id"] in configured:
            item["configured"] = configured[item["id"]]
        item["activation_state"] = "enabled" if item["enabled"] else "paused" if item["status"] == "paused" else "disabled"
    from .verification import verification_capabilities
    extras = verification_capabilities(config)
    report.update(_report(report["checked_at"], report["capabilities"] + extras, report["metrics"]))
    sha = os.environ.get("JOB_SEARCH_SOURCE_REVISION", "")
    version = None
    try:
        with closing(sqlite3.connect(config.application_db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            version = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error:
        pass
    report["release"] = {
        "source_sha": sha if re.fullmatch(r"[a-f0-9]{40}", sha) else None,
        "schema_version": version,
        "identity_verified": bool(re.fullmatch(r"[a-f0-9]{40}", sha)),
    }
    report["external_services_verified"] = False
    return report
