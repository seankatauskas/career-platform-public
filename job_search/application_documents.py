"""Read application documents from their recorded identity, never today's preference."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from .resume_integration import ResumeArtifactContent


class ApplicationDocumentUnavailable(ValueError):
    """A recorded document cannot currently be retrieved."""

    status = 404


def _record(ledger: Any, resume_lab: Any, application_id: str) -> tuple[Mapping[str, Any], str, str]:
    timeline = ledger.get_application_timeline(application_id)
    submissions = sorted(
        (event for event in timeline["events"] if event["event_type"] in {"submission_observed", "submission_confirmed"}),
        key=lambda event: (event["event_type"] != "submission_observed", event.get("event_seq", 0)),
    )
    if submissions:
        event = submissions[0]
        snapshot = event.get("payload", {}).get("resume")
        return (snapshot if isinstance(snapshot, Mapping) else {}, "Recorded at submission", event.get("occurred_at", ""))
    app = timeline["application"]
    # A saved, unsubmitted draft may retain its own selected document. Submitted
    # records with missing snapshots must never inherit a later selection.
    if app["current_phase"] == "preparing" and not app.get("submitted_at") and resume_lab is not None:
        try:
            selection = resume_lab.get_selection(application_id).get("selection")
        except Exception:
            selection = None
        if selection:
            return ({**selection, "decision": "selected"}, "Saved with draft", selection.get("selected_at", ""))
    return {}, "Recorded resume", ""


def _content(resume_lab: Any, snapshot: Mapping[str, Any]) -> ResumeArtifactContent:
    if resume_lab is None:
        raise ApplicationDocumentUnavailable("Resume storage is unavailable.")
    if snapshot.get("decision") == "selected" and snapshot.get("artifact_id"):
        # The artifact ID is taken exclusively from the immutable submission
        # event (or that draft's saved selection), not any caller input.
        result = resume_lab.get_artifact(str(snapshot["artifact_id"]))
    elif snapshot.get("decision") == "matched_upload" and snapshot.get("standard_version_id") and snapshot.get("sha256"):
        result = resume_lab.get_standard_document(
            str(snapshot["standard_version_id"]), expected_sha256=str(snapshot["sha256"]),
        )
    else:
        raise ApplicationDocumentUnavailable("No resume was recorded for this application.")
    if not isinstance(result, ResumeArtifactContent) or result.content_type != "application/pdf":
        raise ApplicationDocumentUnavailable("The recorded PDF is unavailable.")
    return result


def application_document_content(ledger: Any, resume_lab: Any, application_id: str) -> ResumeArtifactContent:
    """Resolve again at download time, verifying the recorded PDF's managed bytes."""
    snapshot, _, _ = _record(ledger, resume_lab, application_id)
    try:
        return _content(resume_lab, snapshot)
    except ApplicationDocumentUnavailable:
        raise
    except Exception as exc:
        # Optional storage errors must not disclose internal paths to the browser.
        raise ApplicationDocumentUnavailable("The recorded PDF is unavailable.") from exc


def application_documents(ledger: Any, resume_lab: Any, application_id: str) -> list[Mapping[str, Any]]:
    snapshot, source, recorded_at = _record(ledger, resume_lab, application_id)
    if not snapshot or snapshot.get("decision") == "not_tracked":
        return []
    document = {
        "name": str(snapshot.get("name") or "Resume"), "source": source,
        "recorded_at": recorded_at, "available": False, "url": None, "preview_url": None,
    }
    try:
        _content(resume_lab, snapshot)
    except Exception:
        document["reason"] = "The recorded resume is unavailable. Its submission record has been preserved."
    else:
        path = f"/api/v1/applications/{quote(application_id, safe='')}/documents/resume"
        document.update(available=True, preview_url=path, url=path + "?download=1")
    return [document]
