"""Bounded, side-effect-free views assembled exclusively from public owner queries."""
import base64
import json

from job_search.commands import DomainError, digest, encode


RECORD_KINDS = {"task": "tasks", "tasks": "tasks", "assessment": "assessments", "assessments": "assessments",
    "offer": "offers", "offers": "offers", "interview": "interviews", "interviews": "interviews",
    "reminder": "reminders", "reminders": "reminders", "submission": "submissions", "submissions": "submissions",
    "note": "notes", "notes": "notes", "progress": "progress"}


def _limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise DomainError("invalid_input", "Query limit must be between 1 and 100")
    return value


def _fields(payload, allowed, required=()):
    if not isinstance(payload, dict) or set(payload) - set(allowed) or set(required) - set(payload):
        raise DomainError("invalid_input", "Unsupported or missing query fields")
    if "application_id" in payload and (not isinstance(payload["application_id"], str) or not payload["application_id"]):
        raise DomainError("invalid_input", "Invalid application identity")
    _limit(payload.get("limit", 50))


def _cursor(value, identity):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError()
        decoded = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if set(decoded) != {"version", "query", "after"} or decoded["version"] != 1 or decoded["query"] != digest(identity):
            raise ValueError()
        if type(decoded["after"]) not in {str, int}:
            raise ValueError()
        return decoded["after"]
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DomainError("invalid_input", "Cursor does not belong to this query") from exc


def _page(result, identity):
    result = dict(result)
    after = result.get("next_cursor", result.get("next_after"))
    result["next_cursor"] = None if after is None else base64.urlsafe_b64encode(encode({
        "version": 1, "query": digest(identity), "after": after}).encode("utf-8")).decode("ascii")
    result.setdefault("truncated", after is not None)
    return result


class ApplicationQueries:
    def __init__(self, executor, applications, correspondence, understanding, actions, *, mail_source=None):
        self.executor, self.applications = executor, applications
        self.correspondence, self.understanding, self.actions = correspondence, understanding, actions
        self.mail_source = mail_source

    def _application(self, connection, app):
        return {**app, "job": self.applications.get_job(connection, app["job_id"]),
                "submission_summary": self.applications.submission_summary(connection, app["id"]),
                "progress": self.applications.stage(connection, app["id"])}

    @staticmethod
    def _workspace_identity(application_id, group):
        return {"operation": "workspace_page", "application_id": application_id, "group": group}

    def workspace_page(self, application_id, group, *, limit=25, cursor=None):
        """Page one workspace collection with a cursor bound to its application."""
        _limit(limit)
        if group not in {"notes", "tasks", "submissions", "interviews", "assessments", "offers", "reminders", "schedules", "progress", "review", "conversation", "actions", "results"}:
            raise DomainError("invalid_input", "Unsupported workspace collection")
        with self.executor.read() as con:
            app = self.applications.get_application(con, application_id)
            identity = self._workspace_identity(app["id"], group)
            after = _cursor(cursor, identity)
            if after is not None and not isinstance(after, str):
                raise DomainError("invalid_input", "Invalid workspace cursor")
            if group == "review":
                page = self.understanding.list_pending(con, application_id=app["id"], limit=limit, after=after)
            elif group == "conversation":
                page = self.correspondence.conversation(con, app["id"], limit=limit, after=after)
            elif group in {"actions", "results"}:
                identities = self.applications.application_identities(con, app["id"])
                if group == "actions":
                    page = self.actions.list_actions(con, app["id"], application_ids=identities["items"], limit=limit, after=after or "")
                else:
                    page = self.actions.list_results(con, application_ids=identities["items"], limit=limit, after=after or "")
                page["history_identity_coverage"] = {"complete": not identities["truncated"]}
            else:
                page = self.applications.list_records(con, app["id"], group, limit=limit, after_id=after)
            return _page(page, identity)

    def workspace(self, application_id, *, limit=25):
        _limit(limit)
        with self.executor.read() as con:
            app = self.applications.get_application(con, application_id)
            app = {**app, "submission_summary": self.applications.submission_summary(con, app["id"])}
            identities=self.applications.application_identities(con,app["id"])
            groups = {kind: _page(self.applications.list_records(con, app["id"], kind, limit=limit), self._workspace_identity(app["id"], kind))
                      for kind in ("notes", "tasks", "submissions", "interviews", "assessments", "offers", "reminders", "schedules", "progress")}
            analysis = self.understanding.coverage(con, application_id=app["id"], limit=limit)
            return {"application": app, "job": self.applications.get_job(con, app["job_id"]),
                "resolved_from": application_id if app["id"] != application_id else None,
                "progress": self.applications.stage(con, app["id"]), "records": groups,
                "conversation": _page(self.correspondence.conversation(con, app["id"], limit=limit), self._workspace_identity(app["id"], "conversation")),
                "review": _page(self.understanding.list_pending(con, application_id=app["id"], limit=limit), self._workspace_identity(app["id"], "review")),
                "analysis_coverage": analysis, "processing": analysis,
                "actions": _page(self.actions.list_actions(con, app["id"], application_ids=identities["items"], limit=limit), self._workspace_identity(app["id"], "actions")),
                "results": _page(self.actions.list_results(con, application_ids=identities["items"], limit=limit), self._workspace_identity(app["id"], "results")),
                "history_identity_coverage": {"complete":not identities["truncated"]}, "paused": self.executor.activation_status(con)["paused"]}

    def list_applications(self, *, limit=50, after=None):
        _limit(limit)
        with self.executor.read() as con:
            result = self.applications.list_applications(con, limit=limit, after_id=after)
            return {**result, "items": [self._application(con, app) for app in result["items"]]}

    def review_queue(self, *, application_id=None, limit=50, after=None):
        _limit(limit)
        with self.executor.read() as con:
            if application_id:
                application_id = self.applications.get_application(con, application_id)["id"]
            return self.understanding.list_pending(con, application_id=application_id, limit=limit, after=after)

    def _current_processing(self, con, *, application_id, limit, after):
        # Correspondence owns which revision is current. Skip old revisions before
        # applying the display bound, retaining a continuation when scanning stops.
        items, scanned, cursor = [], 0, after
        while len(items) <= limit and scanned < 1000:
            page = self.understanding.processing_issues(con, limit=100, after=cursor)
            if not page["items"]:
                return {"items": items, "next_cursor": None, "truncated": False}
            for issue in page["items"]:
                cursor = issue["issue_id"]
                scanned += 1
                try:
                    source = self.correspondence.get(con, issue["source_id"])
                except DomainError as exc:
                    if exc.code != "not_found": raise
                    source = None
                if source is not None and source["revision"] != issue["revision"]:
                    continue
                # Matching candidates are analysis context, not ownership. Only
                # Correspondence's current association connects an issue to an app.
                association = self.correspondence.association(con, issue["source_id"])
                linked_id = self.applications.get_application(con, association["application_id"])["id"] if association else None
                if application_id is not None and linked_id != application_id:
                    continue
                if len(items) == limit:
                    return {"items": items, "next_cursor": items[-1]["issue_id"], "truncated": True}
                items.append({**issue, "application_id": linked_id})
            if not page["next_cursor"]:
                return {"items": items, "next_cursor": None, "truncated": False}
        return {"items": items, "next_cursor": cursor, "truncated": True}

    def dashboard_review(self, *, application_id=None, group=None, limit=25, cursor=None):
        """Independently paged queues keep approvals and unassociated evidence visible.

        The initial response contains at most ``limit`` rows per group; continuation
        requests select exactly one group and its bound cursor.
        """
        _limit(limit)
        groups = ("proposals", "external_actions", "processing", "processing_history")
        if group is not None and group not in groups:
            raise DomainError("invalid_input", "Unsupported review group")
        if cursor is not None and group is None:
            raise DomainError("invalid_input", "A continuation requires its review group")
        items, pages, applications = [], {}, {}
        with self.executor.read() as con:
            if application_id:
                application_id = self.applications.get_application(con, application_id)["id"]
            def application(identifier):
                if not identifier:
                    return None
                if identifier not in applications:
                    applications[identifier] = self._application(con, self.applications.get_application(con, identifier))
                return applications[identifier]
            def source_reference(source):
                source = dict(source)
                if source.get("owner") not in {None, "correspondence"}:
                    return source
                if not source.get("source_id") or not source.get("revision"):
                    return {**source, "owner": "unavailable"}
                try:
                    preserved = self.correspondence.get(con, source["source_id"], source["revision"])
                    source["owner"] = "correspondence"
                    source.setdefault("sha256", preserved["source_sha256"])
                except DomainError as exc:
                    if exc.code != "not_found":
                        raise
                    source["owner"] = "applications" if source.get("direction") == "browser" else "unavailable"
                return source
            for name in (groups if group is None else (group,)):
                identity = {"operation": "dashboard_review", "application_id": application_id, "group": name}
                after = _cursor(cursor, identity)
                if after is not None and (type(after) is not int if name == "processing_history" else not isinstance(after, str)):
                    raise DomainError("invalid_input", "Invalid review cursor")
                if name == "proposals":
                    page = self.understanding.list_pending(con, application_id=application_id, limit=limit, after=after)
                    rows = [{**p, "kind": "proposal", "application": application(p["application_id"]),
                             "evidence": [source_reference(e) for e in p["evidence"]]} for p in page["items"]]
                elif name == "external_actions":
                    aliases = self.applications.application_identities(con, application_id) if application_id else None
                    page = self.actions.list_review_actions(con, application_ids=aliases["items"] if aliases else None, limit=limit, after=after or "")
                    rows = [{"kind": "external_action", "id": a["action_id"], "application_id": a["application_id"],
                             "application": application(a["application_id"]), "action": a} for a in page["items"]]
                    if aliases:
                        page["history_identity_coverage"] = {"complete": not aliases["truncated"]}
                elif name == "processing":
                    page = self._current_processing(con, application_id=application_id, limit=limit, after=after)
                    rows = [{"kind": "processing", "id": a["issue_id"], "application_id": a["application_id"],
                             "processing": {**a, "sources": [source_reference(e) for e in a["sources"]]}} for a in page["items"]]
                else:
                    page = self.understanding.coverage(con, application_id=application_id, limit=limit, after=after)
                    rows = []
                    for attempt in page["items"]:
                        issues = []
                        for source in attempt["sources"]:
                            issue = self.understanding.processing_for_source(con, source["source_id"], source["revision"])
                            if issue is not None:
                                issues.append(issue)
                        current = next((i for i in issues if i["analysis_id"] == attempt["analysis_id"]), None)
                        rows.append({"kind": "processing_history", "id": attempt["analysis_id"], "application_id": application_id,
                            "processing": {**attempt, "is_history": True, "current_issue": current,
                                           "sources": [source_reference(e) for e in attempt["sources"]]}})
                paged = _page(page, identity)
                pages[name] = {key: value for key, value in paged.items() if key != "items"}
                items.extend(rows)
        return {"items": items, "pages": pages, "truncated": any(p["truncated"] for p in pages.values())}

    def briefing(self, *, limit=20):
        _limit(limit)
        # The entire briefing uses one read snapshot; no classifier or mutation runs.
        with self.executor.read() as con:
            applications = self.applications.list_applications(con, limit=limit)
            applications = {**applications, "items": [self._application(con, app) for app in applications["items"]]}
            review = self.understanding.list_pending(con, limit=limit)
            analysis = self.understanding.coverage(con, limit=limit)
            return {"applications": applications, "review": review, "analysis_coverage": analysis,
                    "coverage": {"applications_truncated": applications["truncated"],
                                 "review_truncated": review["next_cursor"] is not None,
                                 "analysis_truncated": analysis["truncated"]}, "paused": self.executor.activation_status(con)["paused"]}

    def query(self, operation, payload):
        """Execute translated read vocabulary; reject unsupported legacy semantics."""
        if operation == "workspace":
            _fields(payload, {"application_id", "limit"}, {"application_id"})
            return self.workspace(**payload)
        if operation == "search_mail_history":
            _fields(payload, {"query", "limit", "cursor"}, {"query"})
            if self.mail_source is None:
                raise DomainError("source_unavailable", "A bounded authorized archive source is required for mail search")
            if not isinstance(payload["query"], str) or not 1 <= len(payload["query"].strip()) <= 200:
                raise DomainError("invalid_input", "Mail search requires a bounded text query")
            if payload.get("limit", 25) > 25:
                raise DomainError("invalid_input", "Private archive search limit is at most 25")
            return self.mail_source.search_mail_page(payload["query"], payload.get("limit", 25), cursor=payload.get("cursor"))
        schemas = {
            "conversation": ({"application_id", "limit", "cursor"}, {"application_id"}),
            "tasks": ({"application_id", "status", "limit", "cursor"}, {"application_id"}),
            "details": ({"application_id", "kind", "limit", "cursor"}, {"application_id"}),
            "history": ({"kind", "record_id", "limit", "cursor", "after_revision"}, {"kind", "record_id"}),
            "review_queue": ({"application_id", "limit", "cursor"}, set()),
            "interviews": ({"application_id", "statuses", "starts_after", "starts_before", "limit", "cursor"}, set()),
            "reminders": ({"application_id", "status", "limit", "cursor"}, set()),
        }
        if operation not in schemas:
            raise DomainError("invalid_input", "Unsupported public query")
        _fields(payload, *schemas[operation])
        limit = payload.get("limit", 50)
        identity = {"operation": operation, "filters": {k: v for k, v in payload.items() if k not in {"cursor", "limit"}}}
        after = _cursor(payload.get("cursor"), identity)
        if operation != "history" and after is not None and not isinstance(after, str):
            raise DomainError("invalid_input", "Invalid record cursor")
        with self.executor.read() as con:
            app_id = payload.get("application_id")
            if app_id:
                app_id = self.applications.get_application(con, app_id)["id"]
            if operation == "conversation":
                return _page(self.correspondence.conversation(con, app_id, limit=limit, after=after), identity)
            if operation == "review_queue":
                return _page(self.understanding.list_pending(con, application_id=app_id, limit=limit, after=after), identity)
            if operation == "history":
                if payload.get("after_revision", 0) != 0:
                    raise DomainError("invalid_input", "History uses its returned event cursor, not a record revision offset")
                kind, record_id = payload["kind"], payload["record_id"]
                if kind == "application":
                    record_id = self.applications.get_application(con, record_id)["id"]
                elif isinstance(kind, str) and kind in RECORD_KINDS:
                    self.applications.get_record(con, RECORD_KINDS[kind], record_id)
                else:
                    raise DomainError("invalid_input", "Unsupported historical record kind")
                if after is not None and (type(after) is not int or after < 0):
                    raise DomainError("invalid_input", "Invalid history cursor")
                return _page(self.executor.history(con, [record_id], after=after or 0, limit=limit), identity)
            if operation == "details":
                kind = payload.get("kind")
                if kind is not None and (not isinstance(kind, str) or kind not in {"assessment", "assessments", "offer", "offers"}):
                    raise DomainError("invalid_input", "Details support assessment and offer records")
                kinds = [RECORD_KINDS[kind]] if kind else ["assessments", "offers"]
                last_kind, last_id = (after.split(":", 1) if after and ":" in after else (None, None))
                if after and (not last_kind or last_kind not in kinds or not last_id):
                    raise DomainError("invalid_input", "Invalid detail cursor")
                items, more = [], False
                for group in kinds:
                    if last_kind and group < last_kind:
                        continue
                    result = self.applications.list_records(con, app_id, group, limit=limit + 1,
                        after_id=last_id if group == last_kind else None)
                    items.extend({**item, "kind": group} for item in result["items"])
                    more |= result["truncated"]
                items.sort(key=lambda item: (item["kind"], item["id"]))
                more |= len(items) > limit
                items = items[:limit]
                return _page({"items": items, "next_cursor": items[-1]["kind"] + ":" + items[-1]["id"] if items and more else None,
                              "truncated": more}, identity)
            statuses = payload.get("statuses", [])
            if operation in {"tasks", "reminders"}:
                status = payload.get("status", "all")
                statuses = [] if status == "all" else [status]
            if not isinstance(statuses, list) or any(not isinstance(s, str) for s in statuses):
                raise DomainError("invalid_input", "Status filters must be strings")
            result = self.applications.query_records(con, operation, application_id=app_id,
                statuses=statuses, starts_after=payload.get("starts_after"), starts_before=payload.get("starts_before"),
                limit=limit, after_id=after)
            return _page(result, identity)
