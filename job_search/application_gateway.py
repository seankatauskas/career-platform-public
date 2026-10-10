"""Production application boundary for hosts that also serve operational features.

The operational ledger is never an application-state fallback. Owner transactions
serialize resume selection and submission; the operational queue keeps its own
idempotent receipts. A crash can leave a prepared resume without a queue receipt,
so retry uses the same resume run/key, never a second application submission.
"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import sqlite3
import re
from collections import Counter

from .commands import CommandContext, DomainError, Principal
from .contracts import ContractError, ConflictError, utc_now


def build_application_gateway(config, operational_ledger):
    backend = getattr(config, "application_backend", "legacy")
    if backend == "legacy":
        return None
    if backend != "owners" or not getattr(config, "application_owner_db", None):
        raise ValueError("Owners backend requires a separate application_owner_db")
    from .application_installation import require_backend
    require_backend(config)
    from .application_runtime import ApplicationRuntime
    from .integration import LocalJobCatalog
    return ApplicationGateway(ApplicationRuntime(config.application_owner_db), operational_ledger,
                              catalog=LocalJobCatalog(config.jobs_db))


class ApplicationGateway:
    """Explicit host facade. No attribute forwarding to a legacy lifecycle writer."""

    def __init__(self, runtime, operational_ledger, *, catalog=None):
        if runtime.executor.path.resolve() == operational_ledger.store.db_path.resolve():
            raise ValueError("Application owners and operational state require separate databases")
        self.runtime, self.operational_ledger, self.catalog = runtime, operational_ledger, catalog

    @staticmethod
    def _context(context):
        if context.actor_kind != "user":
            raise DomainError("not_authorized", "Legacy automatic mutations cannot establish application authority")
        return CommandContext(Principal("human:" + (context.source_ref or context.source_kind), "human", frozenset({"*"})), context.idempotency_key)

    def command(self, operation, payload, key, actor_id):
        from .application_transport import HumanApplicationAdapter
        if operation in {"retry_processing", "resolve_processing"}:
            with self.runtime.executor.read() as con:
                issue = self.runtime.understanding.get_processing_issue(con, payload.get("issue_id"))
            reader = getattr(self.runtime, "mail_reader", None)
            if reader is None or not reader.can_review_source(issue["source_id"], issue["revision"], issue["sha256"]):
                raise DomainError("not_authorized", "Processing recovery requires an authorized evidence account")
        return HumanApplicationAdapter(self.runtime, actor_id).command(operation, payload, key)

    def _rows(self, connection, application_id, kind, *, current_only=False):
        result, after = [], None
        for _ in range(100):
            page = self.runtime.applications.list_records(connection, application_id, kind,
                current_only=current_only, limit=200, after_id=after)
            result.extend(page["items"])
            after = page.get("next_cursor")
            if after is None:
                return result
        raise DomainError("dependency_unresolved", "Application history exceeds the host query bound")

    def _application(self, connection, app):
        owners = self.runtime.applications
        job = owners.get_job(connection, app["job_id"])
        sources = job["sources"]
        source = next((s for s in sources if s["source"] in {"ashby", "greenhouse", "lever"}), sources[0] if sources else {})
        stage = owners.stage(connection, app["id"])
        snapshot = job.get("recorded_snapshot") or {}
        if snapshot:
            source = {"source": snapshot["ats"], "source_id": snapshot["job_id"]}
        submission_summary = owners.submission_summary(connection, app["id"])
        posting = {}
        if not snapshot and self.catalog is not None and source.get("source") in {"ashby", "greenhouse", "lever"}:
            try:
                posting = self.catalog.get_job(source["source"], source["source_id"])
            except ContractError:
                pass
        return {**app, "application_id": app["id"], "owner_job_id": app["job_id"],
                "ats": source.get("source", ""), "job_id": source.get("source_id", ""),
                "title_snapshot": snapshot.get("title") or job.get("title") or posting.get("title") or "Application",
                "employer_snapshot": snapshot.get("employer") or job.get("employer") or posting.get("company") or "",
                "company_slug_snapshot": snapshot.get("company_slug") or posting.get("company") or "", "family_id": snapshot.get("family_id") or "",
                "job_url_snapshot": snapshot.get("job_url") or posting.get("jobUrl") or "", "current_phase": stage["legacy_phase"],
                "job_metadata_source": "recorded_snapshot" if snapshot else "catalog_and_identity",
                "terminal_outcome": app["outcome"], "started_at": app["created_at"],
                "submitted_at": submission_summary["submitted_at"], "confirmed_at": submission_summary["confirmed_at"],
                "submission_summary": submission_summary,
                "last_event_seq": app["version"], "projection_sha256": None, "progress": stage,
                "shortlist_excluded": self._shortlist_excluded(connection, app)}

    def get_application_timeline(self, application_id):
        with self.runtime.executor.read() as con:
            return self._timeline(con, application_id)

    def _timeline(self, con, application_id):
        app = self.runtime.applications.get_application(con, application_id)
        rows = self._rows(con, app["id"], "submissions")
        events = []
        for row in sorted(rows, key=lambda s: (s["created_at"], s["id"])):
            if row["status"] not in {"attempted", "confirmed"}:
                continue
            resume = next((d for d in row.get("documents", []) if isinstance(d, dict) and ("decision" in d or d.get("kind") == "resume")), {})
            events.append({"event_id": row["id"], "event_seq": len(events) + 1,
                "event_type": "submission_confirmed" if row["status"] == "confirmed" else "submission_observed",
                "occurred_at": row.get("occurred_at") or row["created_at"], "payload": {"resume": resume},
                "record_kind": "submission", "record_version": row["version"]})
        return {"application": self._application(con, app), "events": events, "history_format": "owner_records"}

    def application_page(self, *, limit=100, cursor=None):
        from .applications.queries import _cursor, _page, _limit
        _limit(limit)
        identity = {"operation": "dashboard_applications"}
        after = _cursor(cursor, identity)
        if after is not None and not isinstance(after, str):
            raise DomainError("invalid_input", "Invalid application cursor")
        with self.runtime.executor.read() as con:
            page = self.runtime.applications.list_applications(con, limit=limit, after_id=after)
            page = _page(page, identity)
            return {"applications": [self._application(con, app) for app in page.pop("items")], **page}

    def application_workspace(self, application_id):
        """Original dashboard DTO enriched with owner records and preserved captures."""
        workspace = self.runtime.queries.workspace(application_id)
        with self.runtime.executor.read() as con:
            timeline = self._timeline(con, application_id)
            canonical = timeline["application"]["application_id"]
            submissions = self._rows(con, canonical, "submissions")
            observations = self._rows(con, canonical, "observations")
        captures = {}
        for submission in submissions:
            for capture in submission.get("captured_answer_snapshots", []):
                captures[("imported", capture["capture_id"])] = {**capture,
                    "submission_id": submission["id"], "review_status": submission["status"]}
        reviewed = {capture["observation_id"]: submission["id"] for submission in submissions
                    if submission["status"] in {"attempted", "confirmed"}
                    for capture in submission.get("answer_snapshots", [])}
        for observation in observations:
            source = observation.get("source", {})
            if observation.get("activity") == "answer_capture" and "answer_snapshot" in source:
                captures[("observation", observation["id"])] = {
                    "capture_id": observation["id"], "observation_id": observation["id"],
                    "captured_at": observation.get("occurred_at") or observation["created_at"],
                    "snapshot": source["answer_snapshot"], "attempt_id": observation.get("attempt_ref"),
                    "submission_id": reviewed.get(observation["id"]),
                    "review_status": "accepted" if observation["id"] in reviewed else "unreviewed"}
        return {**workspace, **timeline, "answer_snapshots": sorted(captures.values(),
                    key=lambda c: (c.get("captured_at") or "", c["capture_id"]), reverse=True),
                "browser_observations": observations, "messages": workspace["conversation"]["items"]}

    def list_applications(self, phases=None, limit=200):
        rows, after = [], None
        with self.runtime.executor.read() as con:
            while len(rows) < limit:
                page = self.runtime.applications.list_applications(con, limit=min(200, limit), after_id=after)
                rows.extend(value for value in (self._application(con, app) for app in page["items"])
                            if not phases or value["current_phase"] in phases)
                after = page.get("next_cursor")
                if after is None:
                    break
        return rows[:limit]

    def lookup_job(self, ats, job_id):
        with self.runtime.executor.read() as con:
            app = self.runtime.applications.find_application_by_job(con, ats, job_id)
            return self._application(con, app) if app else None

    def _shortlist_excluded(self, connection, app):
        # Tracking or proposing work does not mean this job was applied to.
        return app["disposition"] == "closed" or bool(self.runtime.applications.query_records(
            connection, "submissions", application_id=app["id"], current_only=True,
            statuses=("attempted", "confirmed"), limit=1)["items"])

    def application_keys(self):
        result, after = [], None
        with self.runtime.executor.read() as con:
            while True:
                page = self.runtime.applications.list_applications(con, limit=200, after_id=after)
                for app in page["items"]:
                    if not self._shortlist_excluded(con, app):
                        continue
                    result.extend((s["source"], s["source_id"]) for s in self.runtime.applications.get_job(con, app["job_id"])["sources"])
                after = page.get("next_cursor")
                if after is None:
                    return result

    application_job_keys = application_keys

    def recent_company_applications(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=180)
        rows = [dict(app, applied_at=app["submitted_at"], window_days=180)
                for app in self.list_applications(limit=20000) if app["submitted_at"]
                and datetime.fromisoformat(app["submitted_at"].replace("Z", "+00:00")) >= cutoff]
        return sorted(rows, key=lambda app: app["applied_at"], reverse=True)

    def start_application(self, snapshot, provenance, context):
        snapshot.validate()
        provenance.validate()
        value = {"job_source": {"source": snapshot.ats, "source_id": snapshot.job_id,
                              "title": snapshot.title, "employer": snapshot.employer},
                 "job_snapshot": asdict(snapshot), "recommendation": asdict(provenance)}
        app = self.runtime.command(self._context(context), "save_job", value)
        return {"application": self.get_application_timeline(app["id"])["application"], "application_id": app["id"]}

    def record_submission(self, application_id, occurred_at, context, payload=None, *, payload_factory=None, request_payload=None):
        if payload is not None and payload_factory is not None:
            raise ContractError("submission accepts payload or payload_factory, not both")
        request = {"application_id": application_id, "occurred_at": occurred_at,
                   "request": dict(request_payload if request_payload is not None else payload or {})}
        def apply(tx):
            # Evaluate selection while the authoritative owner writer lock is held.
            captured = dict(payload_factory() if payload_factory else payload or {})
            documents = [captured["resume"]] if isinstance(captured.get("resume"), dict) else []
            result = self.runtime.workflows.apply_internal(tx, "record_submission", {
                "application_id": application_id, "occurred_at": occurred_at,
                "status": "attempted", "documents": documents})
            return {"application": self._timeline(tx.connection, application_id)["application"],
                    "submission": result, "application_id": result["application_id"]}
        return self.runtime.executor.run(self._context(context), "record_submission", request, apply)

    def require_document_editable(self, application_id, job, *, connection=None):
        if connection is None:
            with self.runtime.executor.read() as con:
                return self.require_document_editable(application_id, job, connection=con)
        app = self.runtime.applications.get_application(connection, application_id)
        sources = self.runtime.applications.get_job(connection, app["job_id"])["sources"]
        if {"source": job.ats, "source_id": job.job_id} not in sources:
            raise ConflictError("resume job does not match the application")
        result = self._application(connection, app)
        if app["disposition"] != "open":
            raise ConflictError("resume changes require an open application")
        return result

    @contextmanager
    def document_edit_lock(self, application_id, job):
        # No domain writes: this reservation excludes submissions/closure until
        # the sidecar change and idempotent operational enqueue have committed.
        con = sqlite3.connect(str(self.runtime.executor.path), timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("PRAGMA query_only=ON")
            yield self.require_document_editable(application_id, job, connection=con)
        finally:
            con.rollback()
            con.close()

    def browser_attempt(self, device, attempt):
        with self.runtime.executor.read() as con:
            result = self.runtime.applications.get_browser_attempt(con, device, attempt, limit=200)
        if result is None:
            raise ContractError("browser attempt not found")
        return result

    def browser_observations(self, application_id):
        with self.runtime.executor.read() as con:
            return self._rows(con, application_id, "observations")

    def list_actions(self, statuses=None):
        with self.runtime.executor.read() as con:
            result = self.runtime.actions.list_actions(con, limit=100)
        return result["items"]

    def list_attention_items(self):
        return self.runtime.queries.review_queue(limit=100)["items"]

    def list_reminders(self, statuses=None, limit=100):
        with self.runtime.executor.read() as con:
            rows = self.runtime.applications.query_records(con, "reminders", statuses=statuses or (), limit=min(limit, 200))["items"]
        return [{**row, "reminder_id": row["id"], "note": row.get("description") or row.get("kind") or "Reminder",
                 "due_at": row.get("next_notification_at") or row.get("at")} for row in rows]

    def system_health(self):
        """Application health comes exclusively from the active owners.

        Operational collection/ranking diagnostics have their own readiness view;
        frozen lifecycle projections and queues are never current owner health.
        """
        apps = self.list_applications(limit=20000)
        review = self.runtime.queries.review_queue(limit=100)
        with self.runtime.executor.read() as con:
            reminders = self.runtime.applications.query_records(con, "reminders", current_only=True, limit=200)
            schedules = self.runtime.applications.query_records(con, "schedules", current_only=True, statuses=("pending",), limit=200)
            actions = self.runtime.actions.list_actions(con, limit=200)
            action_readiness = self.runtime.actions.readiness_summary(con)
            reminder_recovery = self.runtime.applications.notification_recovery_summary(con)
            restored = self.runtime.executor.restore_status(con)
            work = self.runtime.executor.work_page(con, limit=100)
            activation = self.runtime.executor.activation_status(con)
        action_counts = dict(Counter(item["execution"] for item in actions["items"]))
        coverage = {"applications_truncated": len(apps) == 20000,
                    "reviews_truncated": review.get("next_cursor") is not None,
                    "reminders_truncated": reminders.get("next_cursor") is not None,
                    "schedules_truncated": schedules.get("next_cursor") is not None,
                    "actions_truncated": actions.get("next_cursor") is not None,
                    "work_truncated": work.get("next_cursor") is not None}
        needs_attention = (restored["required"] or action_readiness["uncertain"] or reminder_recovery["restore_quarantined_reminders"] or
                           any(action_readiness["execution_counts"].get(state, 0) for state in ("failed", "uncertain", "needs_reconciliation")))
        return {"status": "attention" if needs_attention else "incomplete" if any(coverage.values()) else "healthy",
                "checked_at": utc_now(), "database": str(self.runtime.executor.path),
                "applications": dict(Counter(app["current_phase"] for app in apps)),
                "pending_reviews": len(review["items"]),
                "reminders": {"counts": dict(Counter(item["status"] for item in reminders["items"])), "recovery": reminder_recovery},
                "schedules": {"pending": len(schedules["items"])}, "actions": {"counts": action_counts, "readiness": action_readiness},
                "work": {"counts": {"pending": len(work["items"])}},
                "application_backend": "owners", "application_delivery": activation,
                "application_restore": {"required": restored["required"], "revision": restored["revision"]},
                "application_coverage": coverage}

    def http_query(self, path, query):
        """Resolve application routes before the operational dashboard dispatch."""
        if any(len(values) != 1 for values in query.values()):
            raise DomainError("invalid_input", "Query parameters must be singular")
        args = {key: values[0] for key, values in query.items()}
        prefix = "/api/v1/application-owner/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            if name == "applications":
                return True, self.runtime.queries.list_applications(limit=int(args.get("limit", 50)), after=args.get("after"))
            if name == "workspace":
                return True, self.runtime.queries.workspace(args["application_id"])
            if name == "workspace-page":
                return True, self.runtime.queries.workspace_page(args["application_id"], args["group"], limit=int(args.get("limit", 25)), cursor=args.get("cursor"))
            if name == "review":
                if set(args) - {"application_id", "group", "limit", "cursor"}:
                    raise DomainError("invalid_input", "Unsupported review query fields")
                return True, self.runtime.queries.dashboard_review(application_id=args.get("application_id"),
                    group=args.get("group"), limit=int(args.get("limit", 25)), cursor=args.get("cursor"))
            if name == "review-source":
                if set(args) != {"source_id", "revision", "sha256"} or any(not value for value in args.values()):
                    raise DomainError("invalid_input", "Exact source identity, revision, and hash are required")
                reader = getattr(self.runtime, "mail_reader", None)
                if reader is None:
                    return True, {"available": False, "text": None, "reason": "archive_content_requires_authorized_reader"}
                content = reader.read_review_source(args["source_id"], args["revision"], args["sha256"])
                return True, {**content, "available": content["text"] is not None,
                              "reason": "" if content["text"] is not None else "archive_unavailable"}
            if name == "briefing":
                return True, self.runtime.queries.briefing()
            if name in {"closure-preview", "correction-preview"}:
                with self.runtime.executor.read() as con:
                    result = (self.runtime.applications.preview_closure(con, args["application_id"]) if name == "closure-preview" else
                              self.runtime.workflows.preview_association_correction(con, args["association_id"]))
                return True, result
            raise DomainError("not_found", "Unknown application route")
        if path == "/api/v1/applications":
            if set(args) - {"limit", "cursor"}:
                raise DomainError("invalid_input", "Unsupported application query fields")
            return True, self.application_page(limit=int(args.get("limit", 100)), cursor=args.get("cursor"))
        if path == "/api/v1/interviews":
            return True, {"applications": self.list_applications(("interviewing", "offer"))}
        if path in {"/api/v1/attention", "/api/v1/lifecycle/reviews"}:
            return True, {"items": self.list_attention_items()}
        if path == "/api/v1/actions":
            return True, {"actions": self.list_actions()}
        if path == "/api/v1/reminders":
            return True, {"reminders": self.list_reminders()}
        if path == "/api/v1/health":
            return True, self.system_health()
        match = re.fullmatch(r"/api/v1/applications/([A-Za-z0-9._:-]+)(?:/(briefing|conversation))?", path)
        if match:
            if match[2] == "conversation":
                return True, self.runtime.queries.query("conversation", {"application_id": match[1], **args})
            return True, self.runtime.queries.workspace(match[1]) if match[2] else self.get_application_timeline(match[1])
        message = re.fullmatch(r"/api/v1/applications/([A-Za-z0-9._:-]+)/conversation/([A-Za-z0-9._:-]+)", path)
        if message:
            with self.runtime.executor.read() as con:
                app = self.runtime.applications.get_application(con, message[1])
                association = self.runtime.correspondence.association(con, message[2])
                if association is None or association["application_id"] != app["id"]:
                    raise DomainError("not_found", "Message is not linked to this application")
                source = self.runtime.correspondence.get(con, message[2])
            result = {"available": False, "reason": "archive_content_requires_authorized_reader",
                      "subject": "Correspondence", "excerpt": "", "sender": "", "direction": source["direction"]}
            reader = getattr(self.runtime, "mail_reader", None)
            if reader is not None:
                # The reader independently checks the current association and account
                # grant before opening the hash-bound current or predecessor archive.
                content = reader.read_message(app["id"], message[2])
                available = content["text"] is not None
                result.update(available=available, excerpt=content["text"] if available else "",
                              revision=content["revision"], coverage=content["coverage"],
                              reason="" if available else "archive_unavailable")
            return True, result
        if self.legacy_domain_route(path):
            raise DomainError("invalid_input", "Use the application workspace's reviewed owner operation")
        return False, None

    @staticmethod
    def legacy_domain_route(path):
        return path.startswith(("/api/v1/lifecycle/", "/api/v1/chief/", "/api/v1/mail-review/",
            "/api/v1/mail-analyses", "/api/v1/mail/", "/api/v1/proposals/", "/api/v1/temporal-proposals/",
            "/api/v1/actions/", "/api/v1/reminders/", "/api/v1/attention/"))

    def http_command(self, path, body, key, actor_id):
        if path.startswith("/api/v1/application-commands/"):
            return True, self.command(path.rsplit("/", 1)[-1], body, key, actor_id)
        if path.startswith("/api/v1/lifecycle/"):
            from .application_compatibility import translate_dashboard
            call = translate_dashboard(path, body)
            operation, payload = call.operation, dict(call.input)
            if call.kind == "proposal":
                payload = {"operation": operation, "input": payload, "application_id": payload.get("application_id")}
                operation = "propose_changes"
            return True, self.command(operation, payload, key, actor_id)
        if self.legacy_domain_route(path):
            raise DomainError("invalid_input", "Use the application workspace's reviewed owner operation")
        return False, None
