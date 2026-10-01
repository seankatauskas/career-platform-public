"""Narrow integration between the private application ledger and ranking database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from job_search.ranking.labeler import (
    ApiError,
    create_shortlist_session,
    policy_recommendations,
    prepare_preferences,
    record_recommendation_impressions,
    save_recommendation_feedback,
)

from .contracts import ContractError


@dataclass(frozen=True)
class PreferencePaths:
    jobs_db: Path
    preference_db: Path
    proxy_db: Path


class PreferenceGateway:
    """Calls the preference product without granting it application-DB access."""

    def __init__(self, paths: PreferencePaths) -> None:
        self.paths = paths

    def create_shortlist(
        self,
        options: dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
        excluded_job_keys: Iterable[tuple[str, str]] = (),
    ) -> dict[str, Any]:
        exclusions = self._normalize_job_keys(excluded_job_keys)
        return create_shortlist_session(
            self.paths.jobs_db,
            self.paths.preference_db,
            self.paths.proxy_db,
            options,
            idempotency_key=idempotency_key,
            actor=actor,
            excluded_job_keys=exclusions,
        )

    def preview_shortlist(
        self,
        options: dict[str, Any],
        *,
        excluded_job_keys: Iterable[tuple[str, str]] = (),
    ) -> dict[str, Any]:
        """Read current candidates without creating sessions or impressions."""

        return policy_recommendations(
            self.paths.jobs_db,
            self.paths.preference_db,
            self.paths.proxy_db,
            options,
            excluded_job_keys=self._normalize_job_keys(excluded_job_keys),
        )

    def record_notification_shortlist(
        self,
        result: dict[str, Any],
        *,
        workflow_id: str,
    ) -> dict[str, Any]:
        """Record only the candidates in an already-materialized shortlist alert."""

        workflow = str(workflow_id).strip()
        if not workflow:
            raise ContractError("notification shortlist workflow_id is required")
        prepare_preferences(self.paths.jobs_db)
        return record_recommendation_impressions(
            self.paths.jobs_db,
            result,
            str((result.get("options") or {}).get("policy") or "champion"),
            idempotency_key="notification-shortlist:" + workflow,
            actor="notification",
        )

    def notification_exposed_job_keys(
        self,
        workflow_ids: Sequence[str],
        job_keys: Iterable[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        """Return candidate keys exposed by the specified durable shortlist alerts."""

        idempotency_keys = {
            "notification-shortlist:" + str(value)
            for value in workflow_ids
            if str(value)
        }
        candidates = self._normalize_job_keys(job_keys)
        if not idempotency_keys or not candidates or not self.paths.jobs_db.is_file():
            return set()
        if len(candidates) > 100:
            raise ContractError("notification shortlist candidates exceed 100 jobs")
        uri = self.paths.jobs_db.resolve().as_uri() + "?mode=ro"
        exposed: set[tuple[str, str]] = set()
        with sqlite3.connect(uri, uri=True, timeout=10) as con:
            for ats, job_id in candidates:
                rows = con.execute(
                    "SELECT s.idempotency_key FROM recommendation_impressions i "
                    "JOIN recommendation_sessions s ON s.session_id=i.session_id "
                    "WHERE i.ats=? AND i.job_id=?",
                    (ats, job_id),
                )
                if any(str(row[0]) in idempotency_keys for row in rows):
                    exposed.add((ats, job_id))
        return exposed

    @staticmethod
    def _normalize_job_keys(
        job_keys: Iterable[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        return {
            (str(ats).strip().lower(), str(job_id).strip())
            for ats, job_id in job_keys
            if str(ats).strip() and str(job_id).strip()
        }

    def deliver_applied_feedback(
        self,
        payload: Mapping[str, Any],
        *,
        source_event_id: str,
    ) -> dict[str, Any]:
        """Idempotent outbox sink for one submission-observed event."""

        if payload.get("policy_id") == "curated":
            return {"ok": True, "created": False, "preference_label_changed": False,
                    "reason": "curated_selection"}
        required = ("ats", "job_id")
        missing = [name for name in required if not str(payload.get(name) or "").strip()]
        if missing:
            raise ContractError(f"applied feedback is missing {', '.join(missing)}")
        event_id = str(source_event_id or "").strip()
        if not event_id:
            raise ContractError("source_event_id is required")
        feedback = {
            "ats": str(payload["ats"]),
            "id": str(payload["job_id"]),
            "action": "applied",
            "rank": payload.get("recommendation_rank"),
            "model_run_id": payload.get("recommendation_model_run_id", ""),
            "policy_id": payload.get("recommendation_policy_id", "champion"),
            "session_id": payload.get("recommendation_session_id", ""),
            "impression_id": payload.get("recommendation_impression_id"),
            "semantic_score": payload.get("semantic_score"),
            "ranking_score": payload.get("ranking_score"),
            "note": "application submission observed",
        }
        try:
            return save_recommendation_feedback(
                self.paths.jobs_db,
                feedback,
                source_event_id=f"application_event:{event_id}",
            )
        except ApiError as exc:
            raise ContractError(str(exc)) from exc
