"""Named pending-operation recipes; no owner reads and no accepted writes.

Values are structured analyzer findings. Unknown prose stays visible for mapping;
the mapper never guesses calendar instants, offer terms, or existing record IDs.
"""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FACT_FIELDS = {
    "submission": {"status", "submission_id", "occurred_at"},
    "contact": {"kind"},
    "assessment": {"assessment_id", "status", "description", "channel", "due_at", "deadline_text"},
    "offer": {"offer_id", "status", "terms"},
    "interview": {"interview_id", "status", "title", "round_kind", "start_at", "end_at", "timezone",
                  "participants", "location", "join_url", "organizer", "note", "employer_confirmed", "reason", "cancellation_kind"},
    "outcome": {"outcome", "reason"},
}


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _member(value, choices):
    return isinstance(value, str) and value in choices


def fact_recipe(finding, target, descriptor):
    content, blockers = finding["content"], []
    kind, original = content["kind"], content["value"]
    value = dict(original) if isinstance(original, dict) else {}
    if set(value) - FACT_FIELDS[kind]:
        blockers.append("needs_mapping")
    value = {k: v for k, v in value.items() if k in FACT_FIELDS[kind]}
    payload = {"application_id": target, "evidence": content["evidence"]}
    versions = descriptor["versions"]
    context = descriptor.get("context", {})
    if not target:
        blockers.append("needs_target")

    if kind == "contact":
        payload["kind"] = "recruiter_contact"
        if value.get("kind", "recruiter_contact") != "recruiter_contact":
            blockers.append("needs_mapping")
        operation = "record_progress"
    elif kind == "submission":
        status = value.get("status", original if isinstance(original, str) else None)
        status = {"received": "confirmed", "submitted": "attempted"}.get(status, status) if isinstance(status, str) else None
        if not _member(status, {"attempted", "confirmed", "failed", "retracted"}):
            status = None
            blockers.append("needs_mapping")
        operation = "confirm_submission" if status in {None, "confirmed"} else "record_submission"
        payload.update(value)
        payload["status"] = status
        if value.get("occurred_at") and not _instant(value["occurred_at"]):
            blockers.append("needs_time")
    elif kind == "assessment":
        operation = "update_assessment" if value.get("assessment_id") else "record_assessment"
        payload.update(value)
        payload["create_task"] = False
        if not _member(value.get("status"), {"requested", "submitted", "completed", "cancelled"}) or not isinstance(value.get("description"), str) or not value["description"]:
            blockers.append("needs_mapping")
        if value.get("due_at") and not _instant(value["due_at"]):
            blockers.append("needs_time")
    elif kind == "offer":
        operation = "record_offer"
        payload.update(value)
        payload["create_task"] = False
        if not _member(value.get("status"), {"offered", "negotiating", "accepted", "declined", "expired", "employer_withdrawn"}) or not isinstance(value.get("terms"), dict):
            blockers.append("needs_mapping")
    elif kind == "interview":
        status = value.get("status")
        operation = "reschedule_interview" if value.get("interview_id") and status == "scheduled" else "schedule_interview"
        payload.update({k: v for k, v in value.items() if k not in {"reason", "cancellation_kind"}})
        payload["create_task"] = False
        payload["reminders_enabled"] = False
        if status == "cancelled":
            operation = "cancel_interview"
            payload = {k: value[k] for k in ("interview_id", "reason", "cancellation_kind") if k in value}
            if not value.get("interview_id"):
                blockers.append("needs_target")
            if not value.get("reason") or not _member(value.get("cancellation_kind"), {"employer_cancelled", "user_not_attending", "correction"}):
                blockers.append("needs_mapping")
        elif not _member(status, {"requested", "scheduled", "completed"}):
            blockers.append("needs_mapping")
        if status == "scheduled" or value.get("start_at") or value.get("end_at"):
            start, end = _instant(value.get("start_at")), _instant(value.get("end_at"))
            try:
                ZoneInfo(value.get("timezone"))
                valid_zone = True
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                valid_zone = False
            if start is None or end is None or end <= start or not valid_zone:
                blockers.append("needs_time")
        if value.get("interview_id"):
            preview = context.get("interview_previews", {}).get(value["interview_id"]) if isinstance(value["interview_id"], str) else None
            if not isinstance(preview, dict) or not isinstance(preview.get("expected_related_versions"), dict):
                blockers.append("requires_related_preview")
            else:
                payload["expected_related_versions"] = preview["expected_related_versions"]
    else:
        operation = "close_application"
        outcome = value.get("outcome", original if isinstance(original, str) else None)
        payload = {"application_id": target, "outcome": outcome,
                   "reason": value.get("reason", "Reviewed external outcome: " + outcome if isinstance(outcome, str) else "")}
        if not _member(outcome, {"accepted", "rejected", "withdrawn", "stopped_pursuing"}):
            blockers.append("needs_mapping")
        preview = context.get("closure_previews", {}).get(target)
        if not isinstance(preview, dict) or not isinstance(preview.get("expected_records"), dict) or type(preview.get("expected_version")) is not int:
            blockers.append("requires_closure_preview")
        else:
            payload.update(expected_version=preview["expected_version"], expected_records=preview["expected_records"])

    identity_field = {"submission": "submission_id", "assessment": "assessment_id", "offer": "offer_id", "interview": "interview_id"}.get(kind)
    if identity_field and value.get(identity_field):
        identity = value[identity_field]
        expected = versions.get(kind + ":" + identity) if isinstance(identity, str) else None
        if type(expected) is not int:
            blockers.append("needs_target")
        else:
            payload["expected_version"] = expected
    return operation, payload, list(dict.fromkeys(blockers))
