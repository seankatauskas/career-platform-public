"""Read-only runtime dependency diagnostics, shared by CLI and dashboard."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from .runtime import RuntimeConfigV1


def dependency_health(config: RuntimeConfigV1) -> dict[str, Any]:
    """Inspect configured runtime boundaries without issuing a model request."""

    from job_search.inference import configured_inference_path

    inference_path_error = False
    try:
        inference_path = configured_inference_path(config.inference_config)
    except ValueError:
        inference_path = None
        inference_path_error = True
    issues: list[str] = []
    local_mail_selected = config.mail_classifier_config is not None
    remote_mail_requested = (
        config.remote_mail_inference_enabled and not local_mail_selected
    )
    remote_mail: dict[str, Any] = {
        "egress_enabled": config.remote_mail_inference_enabled,
        "active": False,
        "status": (
            "local_classifier_selected"
            if local_mail_selected
            else "blocked_setup"
            if remote_mail_requested
            else "disabled"
        ),
        "external_endpoint_probed": False,
    }
    inference: dict[str, Any] = {
        "configured": inference_path is not None or inference_path_error,
        "configuration_ready": False,
        "structured_generation": False,
        "embeddings": False,
        "external_endpoint_probed": False,
        "remote_mail": remote_mail,
        "preference_embeddings": {
            "status": "not_configured",
            "remote_configured": False,
            "champion_present": False,
            "identity_match": None,
            "automatic_migration": False,
        },
    }
    if inference_path_error:
        inference["status"] = "blocked_setup"
        issues.append("inference_configuration")
        if remote_mail_requested:
            issues.append("remote_mail_inference")
    elif inference_path is None:
        inference["status"] = "disabled"
        if remote_mail_requested:
            issues.append("remote_mail_inference")
    else:
        try:
            from job_search.inference import load_inference_config
            from job_search.inference.config import load_credential

            loaded = load_inference_config(inference_path)
            for provider in (
                loaded.structured_generation,
                loaded.embeddings,
            ):
                if provider is not None:
                    load_credential(provider.credential_file)
            inference.update(
                {
                    "status": "configuration_ready",
                    "configuration_ready": True,
                    "structured_generation": loaded.structured_generation is not None,
                    "embeddings": loaded.embeddings is not None,
                }
            )
            if remote_mail_requested:
                from job_search.mail.remote import TEMPORAL_MAX_OUTPUT_TOKENS

                if (
                    loaded.structured_generation is None
                    or loaded.structured_generation.default_max_output_tokens
                    < TEMPORAL_MAX_OUTPUT_TOKENS
                ):
                    inference["status"] = "attention"
                    issues.append("remote_mail_inference")
                else:
                    remote_mail.update(
                        {"active": True, "status": "configuration_ready"}
                    )
            if loaded.embeddings is not None:
                preflight = _preference_embedding_preflight(
                    config.preference_db,
                    loaded.embeddings.embedding_identity,
                    proxy_db=config.proxy_db if config.shortlist_policy != "champion" else None,
                )
                inference["preference_embeddings"] = preflight
                if preflight["status"] == "migration_required":
                    inference["status"] = "attention"
                    issues.append("preference_embedding_migration")
                elif preflight["status"] == "blocked_preflight":
                    inference["status"] = "attention"
                    issues.append("preference_embedding_preflight")
        except (OSError, RuntimeError, ValueError):
            inference["status"] = "blocked_setup"
            issues.append("inference_configuration")
            if remote_mail_requested:
                issues.append("remote_mail_inference")

    resume_enabled = bool(config.resume_lab_db and config.resume_artifact_root)
    if resume_enabled:
        from job_search.resume_lab.gateway import resume_lab_status

        resume = dict(resume_lab_status(config))
        if not (resume.get("standard_ready") if config.resume_mode == "standard" else resume.get("ready_for_generation")):
            issues.append("resume_lab")
    else:
        resume = {
            "status": "disabled",
            "configured": False,
            "ready_for_import": False,
            "ready_for_generation": False,
        }

    notification: dict[str, Any] = {
        "configured": bool(config.hermes_telegram_target),
        "delivery_ready": False,
        "gateway_running": False,
        "transport": "disabled",
    }
    if not config.hermes_telegram_target:
        notification["status"] = "disabled"
    elif config.hermes_notification_socket is not None:
        notification["transport"] = "private_socket"
        try:
            from job_search.hermes_delivery import (
                SERVICE_REVISION,
                HermesDeliveryClient,
            )

            report = HermesDeliveryClient(
                config.hermes_notification_socket,
                expected_target=config.hermes_telegram_target,
                timeout_seconds=3,
            ).ping()
            notification["delivery_ready"] = (
                report.get("service_revision") == SERVICE_REVISION
            )
            notification["gateway_running"] = bool(report.get("gateway_running"))
            notification["status"] = (
                "ready"
                if notification["delivery_ready"] and notification["gateway_running"]
                else "blocked_setup"
            )
        except (OSError, RuntimeError, ValueError):
            notification["status"] = "blocked_setup"
        if notification["status"] != "ready":
            issues.append("hermes_notification")
    else:
        notification["transport"] = "local_executable"
        executable = config.hermes_executable
        notification["delivery_ready"] = bool(
            executable and executable.is_file() and os.access(executable, os.X_OK)
        )
        notification["status"] = (
            "ready" if notification["delivery_ready"] else "blocked_setup"
        )
        if notification["status"] != "ready":
            issues.append("hermes_notification")

    outlook_configured = bool(config.outlook_client_id)
    configuration_ready_unprobed = bool(
        inference.get("status") == "configuration_ready"
        or resume.get("status") == "configuration_ready"
        or outlook_configured
    )
    return {
        "schema_version": 1,
        "status": (
            "attention"
            if issues
            else "configuration_ready"
            if configuration_ready_unprobed
            else "ready"
        ),
        "issues": sorted(set(issues)),
        "inference": inference,
        "resume_lab": resume,
        "notifications": notification,
        "outlook": {
            "configured": outlook_configured,
            "status": "configured" if outlook_configured else "disabled",
            "authentication_probed": False,
        },
    }


def _revision_fingerprint(revision: str) -> str:
    return hashlib.sha256(revision.encode("utf-8")).hexdigest()[:16]


def _preference_embedding_preflight(
    state_db: Path,
    configured_revision: str,
    proxy_db: Path | None = None,
) -> dict[str, Any]:
    """Compare remote and champion identities without exposing either revision."""

    report: dict[str, Any] = {
        "status": "no_champion",
        "remote_configured": True,
        "champion_present": False,
        "identity_match": None,
        "automatic_migration": False,
        "configured_identity_fingerprint": _revision_fingerprint(configured_revision),
    }
    target = Path(state_db)
    if not target.is_file():
        return report
    try:
        uri = target.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as connection:
            connection.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('preference_state','preference_model_runs')"
                )
            }
            if tables != {"preference_state", "preference_model_runs"}:
                return report
            row = connection.execute(
                "SELECT r.model_revision FROM preference_state AS s "
                "JOIN preference_model_runs AS r ON r.run_id=s.value "
                "WHERE s.key='champion_run_id' LIMIT 1"
            ).fetchone()
    except (OSError, sqlite3.Error):
        report["status"] = "blocked_preflight"
        return report
    if proxy_db is None and (row is None or not isinstance(row[0], str) or not row[0]):
        return report
    if proxy_db is not None:
        from .ranking.refresh import policy_runs, POLICIES
        try:
            runs=policy_runs(proxy_db)
            if set(runs) != set(POLICIES):
                report['status']='blocked_preflight'; return report
            with sqlite3.connect(target.resolve().as_uri()+"?mode=ro",uri=True) as con:
                revisions=[con.execute('SELECT model_revision FROM preference_model_runs WHERE run_id=?',(runs[p],)).fetchone() for p in POLICIES]
            if any(not r for r in revisions):
                report['status']='blocked_preflight'; return report
            matches=all(r[0] == configured_revision for r in revisions)
            report.update(status='ready' if matches else 'migration_required',champion_present=False,identity_match=matches)
            return report
        except (OSError,ValueError,sqlite3.Error):
            report['status']='blocked_preflight'; return report
    champion_revision = str(row[0])
    matches = champion_revision == configured_revision
    report.update(
        {
            "status": "ready" if matches else "migration_required",
            "champion_present": True,
            "identity_match": matches,
            "champion_identity_fingerprint": _revision_fingerprint(champion_revision),
        }
    )
    return report

