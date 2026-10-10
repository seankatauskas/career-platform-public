"""Pure compatibility translation for the isolated replacement runtime.

This module cannot invoke a legacy writer, query a database or construct authority.
Authenticated transports pass the translated call to the same candidate runtime.
Unsupported old semantics fail explicitly rather than silently recreating them.
"""
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .commands import DomainError


@dataclass(frozen=True)
class TranslatedCall:
    kind: str
    operation: str
    input: Mapping[str, Any]
    idempotency_key: Optional[str] = None


def _error(message):
    raise DomainError("invalid_input", message)


def _fields(value, allowed, required=()):
    if not isinstance(value, Mapping) or set(value) - set(allowed) or set(required) - set(value):
        _error("Legacy fields do not match the replacement contract")
    return dict(value)


def _body(value):
    if not isinstance(value, Mapping):
        _error("Operation input must be an object")
    body = dict(value)
    if set(body) & {"actor_kind", "principal", "capabilities", "origin", "delegation", "auto_apply", "auto_accept"}:
        raise DomainError("not_authorized", "Authority and automatic inference acceptance cannot be supplied by a caller")
    key = body.pop("idempotency_key", None)
    return body, key


def _rename(value, old, new):
    if old in value:
        if new in value:
            _error("Do not supply both old and replacement field names")
        value[new] = value.pop(old)


def _version(value):
    _rename(value, "revision_no", "expected_version")
    _rename(value, "expected_revision_no", "expected_version")
    if type(value.get("expected_version")) is not int or value["expected_version"] < 1:
        _error("An explicit expected_version from the reviewed record is required")


def _task(application_id, values):
    values = _fields(values, {"kind", "owner", "responsible_party", "note", "description", "due_at", "completion_rule", "origin_ref", "related_id", "evidence", "reminders_enabled"}, {"kind"})
    _rename(values, "owner", "responsible_party")
    _rename(values, "note", "description")
    values.setdefault("description", str(values["kind"]).replace("_", " "))
    values.setdefault("responsible_party", "unknown")
    return "create_task", {"application_id": application_id, **values}


def _task_transition(body):
    body = _fields(body, {"task_id", "operation", "values"}, {"task_id", "operation"})
    values = _fields(body.get("values", {}), {"revision_no", "expected_revision_no", "expected_version", "reason", "snoozed_until", "until"})
    _version(values)
    operation = {"complete": "complete_task", "cancel": "cancel_task", "snooze": "snooze_task"}.get(body["operation"])
    if operation is None:
        _error("Task supersession requires an explicit replacement/correction review")
    if operation == "snooze_task":
        _rename(values, "snoozed_until", "until")
        values.pop("reason", None)
        if "until" not in values:
            _error("Snooze requires an explicit time")
    else:
        if not values.get("reason"):
            _error("Task decision requires a reason")
        if set(values) & {"snoozed_until", "until"}:
            _error("Only snooze changes notification time")
    return operation, {"task_id": body["task_id"], **values}


def _detail(body):
    body = _fields(body, {"application_id", "kind", "values", "detail_id"}, {"application_id", "kind", "values"})
    kind = body["kind"]
    if kind not in {"assessment", "offer"}:
        _error("Unsupported application detail kind")
    if not isinstance(body["values"], Mapping):
        _error("Detail values must be an object")
    values = dict(body["values"])
    _rename(values, "expected_revision_no", "expected_version")
    _rename(values, "revision_no", "expected_version")
    if body.get("detail_id"):
        _version(values)
        values[kind + "_id"] = body["detail_id"]
    if kind == "assessment":
        _rename(values, "title", "description")
        values = _fields(values, {"assessment_id", "expected_version", "status", "description", "channel", "due_at", "deadline_text", "create_task", "evidence"}, {"description"})
        operation = "update_assessment" if body.get("detail_id") else "record_assessment"
    else:
        values = _fields(values, {"offer_id", "expected_version", "status", "terms", "create_task", "evidence"})
        operation = "record_offer"
    return operation, {"application_id": body["application_id"], **values}


def _interview(application_id, details):
    values = _fields(details, {"round_id", "interview_id", "revision_no", "expected_revision_no", "expected_version", "round_kind", "title", "status", "starts_at", "start_at", "ends_at", "end_at", "time_zone", "timezone", "participants", "location", "join_url", "organizer", "note", "employer_confirmed", "evidence", "create_task", "reminders_enabled", "expected_related_versions", "reason", "cancellation_kind"})
    for old, new in (("round_id", "interview_id"), ("starts_at", "start_at"), ("ends_at", "end_at"), ("time_zone", "timezone")):
        _rename(values, old, new)
    if values.get("interview_id"):
        _version(values)
    status = values.get("status", "proposed")
    if status == "cancelled":
        if not values.get("interview_id") or not values.get("reason"):
            _error("Cancellation requires existing round, reviewed version and reason")
        values.pop("status")
        return "cancel_interview", _fields(values, {"interview_id", "expected_version", "reason", "cancellation_kind", "expected_related_versions"}, {"interview_id", "expected_version", "reason"})
    if status in {"confirmed", "rescheduled"}:
        values["status"] = "scheduled"
    elif status == "proposed":
        values["status"] = "scheduled" if values.get("start_at") or values.get("end_at") else "requested"
    elif status not in {"requested", "scheduled", "completed"}:
        _error("Unsupported interview status")
    return ("reschedule_interview" if values.get("interview_id") and values["status"] == "scheduled" else "schedule_interview"), {"application_id": application_id, **values}


def translate_dashboard(path, body):
    """Translate the existing lifecycle mutation route suffix or full v1 path."""
    body, key = _body(body)
    path = path.removeprefix("/api/v1/lifecycle/")
    kind = "command"
    if path == "tasks/create":
        body = _fields(body, {"application_id", "values"}, {"application_id", "values"})
        operation, payload = _task(body["application_id"], body["values"])
    elif path == "tasks/transition":
        operation, payload = _task_transition(body)
    elif path == "details/record":
        operation, payload = _detail(body)
    elif path == "interviews/propose":
        body = _fields(body, {"application_id", "details"}, {"application_id", "details"})
        operation, payload = _interview(body["application_id"], body["details"])
        kind = "proposal"
    elif path == "reminders/cancel":
        body = _fields(body, {"reminder_id", "revision_no", "expected_version", "reason"}, {"reminder_id", "reason"})
        _version(body)
        operation, payload = "cancel_reminder", body
    else:
        _error("This legacy operation requires the new explicit review or snapshot-conversion contract")
    return TranslatedCall(kind, operation, payload, key)


def translate_tool(name, args):
    """Keep existing compatible MCP names; mutations remain proposals by default."""
    args, key = _body(args)
    if name == "propose_application_update":
        args = _fields(args, {"application_id", "kind", "payload"}, {"application_id", "kind", "payload"})
        app, kind, value = args["application_id"], args["kind"], args["payload"]
        if kind == "task":
            value = _fields(value, {"values"}, {"values"})
            operation, payload = _task(app, value["values"])
        elif kind == "task_transition":
            operation, payload = _task_transition(value)
        elif kind == "detail":
            operation, payload = _detail({"application_id": app, **value})
        else:
            _error("Phase overrides and identity corrections require the new fact-level preview")
        return TranslatedCall("proposal", operation, payload, key)
    if name == "propose_interview_revision":
        args = _fields(args, {"application_id", "details"}, {"application_id", "details"})
        operation, payload = _interview(args["application_id"], args["details"])
        return TranslatedCall("proposal", operation, payload, key)
    if name == "create_reminder":
        args = _fields(args, {"application_id", "due_at", "note"}, {"application_id", "due_at", "note"})
        return TranslatedCall("proposal", "create_reminder", {"application_id": args["application_id"], "at": args["due_at"], "description": args["note"]}, key)
    if name == "cancel_reminder":
        call = translate_dashboard("reminders/cancel", args)
        return TranslatedCall("proposal", call.operation, call.input, key)
    fields = {
        "get_application_briefing": {"application_id"},
        "list_application_conversation": {"application_id", "limit", "cursor"},
        "list_application_tasks": {"application_id", "status", "limit", "offset", "cursor"},
        "list_application_details": {"application_id", "kind", "limit", "offset", "cursor"},
        "get_application_record_history": {"kind", "record_id", "limit", "after_revision", "cursor"},
        "list_lifecycle_reviews": {"application_id", "limit", "offset", "cursor"},
        "list_interview_rounds": {"application_id", "statuses", "starts_after", "starts_before", "limit", "offset", "cursor"},
        "list_application_reminders": {"application_id", "status", "limit", "offset", "cursor"},
        "list_reminders": {"status", "limit", "cursor"},
        "search_mail_history": {"query", "limit", "cursor"},
    }
    if name not in fields:
        _error("Unknown compatibility tool")
    args = _fields(args, fields[name])
    if args.get("offset", 0) != 0:
        _error("Use the returned stable cursor; offset pagination cannot preserve a changing review list")
    args.pop("offset", None)
    if "limit" in args and (type(args["limit"]) is not int or not 1 <= args["limit"] <= 100):
        _error("Query limit must be between 1 and 100")
    queries = {
        "get_application_briefing": "workspace", "list_application_conversation": "conversation",
        "list_application_tasks": "tasks", "list_application_details": "details",
        "get_application_record_history": "history", "list_lifecycle_reviews": "review_queue",
        "list_interview_rounds": "interviews", "list_application_reminders": "reminders",
        "list_reminders": "reminders", "search_mail_history": "search_mail_history",
    }
    if name not in queries:
        _error("Unknown compatibility tool")
    if name in {"get_application_briefing", "list_application_conversation", "list_application_tasks", "list_application_details"} and not args.get("application_id"):
        _error("Application identity required")
    if name == "list_interview_rounds" and "statuses" in args:
        mapping = {"proposed": "requested", "confirmed": "scheduled", "rescheduled": "scheduled", "cancelled": "cancelled", "completed": "completed"}
        if not isinstance(args["statuses"], list) or any(status not in mapping for status in args["statuses"]):
            _error("Invalid interview statuses")
        args["statuses"] = list(dict.fromkeys(mapping[status] for status in args["statuses"]))
    if name == "list_reminders" and "status" in args:
        statuses = {"all": "all", "scheduled": "pending", "cancelled": "cancelled", "completed": "delivered"}
        if args["status"] not in statuses:
            _error("Invalid reminder status")
        args["status"] = statuses[args["status"]]
    return TranslatedCall("query", queries[name], args, key)


def legacy_record(kind, record):
    """Return compatibility aliases without changing authoritative owner data."""
    result = dict(record)
    if "version" in result:
        result["revision_no"] = result["version"]
    if kind == "tasks":
        result["note"] = result.get("description")
        result["owner"] = result.get("responsible_party")
    if kind == "interviews":
        result["round_id"] = result.get("id")
        result["starts_at"] = result.get("start_at")
        result["ends_at"] = result.get("end_at")
        result["time_zone"] = result.get("timezone")
    return result
