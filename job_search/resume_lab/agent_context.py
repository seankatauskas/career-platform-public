"""Application-bound factual resume text for the read-only agent boundary."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping

from job_search.db import connect as ledger_connect
from .contracts import ResumeNotFoundError, sha256_text, validate_identifier
from .store import connect as resume_connect


MAX_RESUME_CONTENT_CHARS = 12_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def application_resume_content(gateway: Any, application_id: str) -> Mapping[str, Any]:
    """Resolve through the application, never through an agent-supplied artifact ID.

    Submitted applications use their immutable submission snapshot. In-progress
    applications may expose their currently selected factual artifact. This reads
    persisted text only: no model, PDF compiler, filesystem path, or career draft.
    """
    validate_identifier(application_id, "application_id")
    result = {"application_id": application_id, "available": False, "truncated": False}

    def unavailable(reason: str) -> Mapping[str, Any]:
        return {**result, "reason": reason}

    if gateway.application_db is None or not gateway.application_db.is_file():
        return unavailable("application_ledger_unavailable")
    try:
        with ledger_connect(gateway.application_db) as connection:
            connection.execute("BEGIN")
            owner = getattr(gateway, "application_gateway", None)
            if owner is not None:
                timeline = owner.get_application_timeline(application_id)
                app = timeline["application"]
                event = next(iter(timeline["events"]), None)
                submission = {"event_id": event["event_id"], "payload_json": json.dumps(event["payload"])} if event else None
            else:
                app = connection.execute(
                    "SELECT ats,job_id,current_phase,submitted_at FROM applications WHERE application_id=?",
                    (application_id,),
                ).fetchone()
                submission = connection.execute(
                    "SELECT event_id,payload_json FROM application_events WHERE application_id=? "
                    "AND event_type IN ('submission_observed','submission_confirmed') "
                    "ORDER BY CASE WHEN event_type='submission_observed' THEN 0 ELSE 1 END,event_seq LIMIT 1", (application_id,),
                ).fetchone()
            if app is None:
                return unavailable("application_not_found")
            if submission is not None:
                snapshot = json.loads(submission["payload_json"]).get("resume")
                if not isinstance(snapshot, Mapping):
                    return unavailable("submission_resume_not_recorded")
                if snapshot.get("decision") == "not_tracked":
                    return unavailable("submission_resume_not_tracked")
                if snapshot.get("decision") == "matched_upload":
                    # This binding is based on uploaded bytes, independent of resume scoring.
                    if owner is not None:
                        bound = any(document.get("sha256") == snapshot.get("sha256") for observation in owner.browser_observations(application_id) for document in observation.get("documents", []))
                    else:
                        with ledger_connect(gateway.application_db) as tracking:
                            bound = tracking.execute("SELECT 1 FROM browser_attempts WHERE application_id=? AND resume_sha256=?", (application_id, snapshot.get("sha256"))).fetchone()
                    if not bound:
                        return unavailable("resume_binding_unavailable")
                    version = gateway.service.store.get_standard_version(str(snapshot.get("standard_version_id") or ""))
                    metadata = version.get("import_metadata") or {}
                    if metadata.get("pdf_sha256") != snapshot.get("sha256") or not metadata.get("parse_safe"):
                        return unavailable("resume_binding_unavailable")
                    text = _CONTROL.sub("", str(version.get("plain_text") or ""))
                    return {**result, "available": bool(text.strip()), "reason": None if text.strip() else "resume_text_unavailable",
                        "text": text[:MAX_RESUME_CONTENT_CHARS], "text_characters": len(text), "truncated": len(text)>MAX_RESUME_CONTENT_CHARS,
                        "provenance": {"binding": "submitted", "comparison_kind": "standard", "standard_version_id": snapshot["standard_version_id"],
                                       "pdf_sha256": snapshot["sha256"], "submission_event_id": submission["event_id"]}}
                if snapshot.get("decision") != "selected":
                    return unavailable("submission_resume_not_recorded")
                artifact_id = str(snapshot.get("artifact_id") or "")
                evaluation_id = str(snapshot.get("evaluation_id") or "")
                binding = "submitted"
            elif owner is None and (app["submitted_at"] or app["current_phase"] != "preparing"):
                return unavailable("submission_resume_not_recorded")
            else:
                selection = gateway.service.current_application_selection(application_id)
                if selection is None:
                    return unavailable("resume_not_selected")
                artifact_id = str(selection["artifact_id"])
                evaluation_id = ""
                binding = "selected"

            # A snapshot alone does not grant access to an unrelated artifact.
            with resume_connect(gateway.service.store.db_path) as resumes:
                selected = resumes.execute(
                    "SELECT 1 FROM application_resume_selections WHERE application_id=? AND artifact_id=?",
                    (application_id, artifact_id),
                ).fetchone()
            if selected is None:
                return unavailable("resume_binding_unavailable")
            try:
                artifact = gateway.service.get_artifact(artifact_id, include_content=True)
            except ResumeNotFoundError:
                return unavailable("resume_artifact_unavailable")
            if (artifact["purpose"] != "real_application"
                or artifact["variant_kind"] not in {"standard", "grounded_rewrite"}
                or not artifact["parse_safe"]
                or artifact["ats"] != app["ats"] or artifact["job_id"] != app["job_id"]):
                return unavailable("resume_not_factual")
            metadata = artifact.get("metadata") or {}
            actual_evaluation_id = str(metadata.get("evaluation_id") or "")
            if not actual_evaluation_id:
                return unavailable("resume_evaluation_unavailable")
            if evaluation_id and evaluation_id != actual_evaluation_id:
                return unavailable("resume_evaluation_unavailable")
            evaluation_id = actual_evaluation_id
            evaluation = gateway.service.store.get_cached_evaluation(evaluation_id)
            if (not evaluation or not gateway.service.store.has_artifact_evaluation(artifact_id, evaluation_id)
                or evaluation.get("artifact_text_sha256") != sha256_text(artifact["parsed_text"])):
                return unavailable("resume_evaluation_unavailable")
            text = _CONTROL.sub("", str(artifact["parsed_text"]))
            if not text.strip():
                return unavailable("resume_text_unavailable")
            provenance = {
                "artifact_id": artifact_id, "evaluation_id": evaluation_id,
                "binding": binding, "comparison_kind": artifact["variant_kind"],
                "source_mode": artifact.get("source_mode", "standard"),
                "content_sha256": artifact["content_sha256"],
                "text_sha256": sha256_text(artifact["parsed_text"]),
            }
            if submission is not None:
                provenance["submission_event_id"] = submission["event_id"]
            for key in ("profile_revision_id", "composition_id", "template_version"):
                value = metadata.get(key) or artifact.get(key)
                if isinstance(value, str) and value:
                    provenance[key] = value[:256]
            return {**result, "available": True, "reason": None,
                "text": text[:MAX_RESUME_CONTENT_CHARS], "text_characters": len(text),
                "truncated": len(text) > MAX_RESUME_CONTENT_CHARS,
                "provenance": provenance}
    except (sqlite3.Error, OSError, json.JSONDecodeError):
        return unavailable("resume_storage_unavailable")
