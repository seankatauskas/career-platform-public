"""Production adapters for evidence ingestion and reviewed Understanding.

Only this composition crosses the operational mail store and application owners.
No legacy event, temporal, lifecycle, or discovery writer is called here.
"""
from dataclasses import dataclass, asdict
import base64
import hashlib
import json

from .commands import CommandContext, DomainError, Principal, digest
from .mail.sanitizer import sanitize_mail
from .worker import FollowUpTask, TaskResult, RetryableTaskError, PermanentTaskError


OWNER_QUERY_VERSION = 3


def worker_context(operation, key):
    return CommandContext(Principal("application-mail-worker", "worker", frozenset({operation})), key, "inferred")


class AccountNotGranted(PermanentTaskError):
    pass


class ApplicationMailIngestor:
    def __init__(self, runtime, archive, account_id):
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("A configured account is required")
        self.runtime, self.archive, self.account_id = runtime, archive, account_id

    def ingest(self, provider_message_id, payload, *, direction, source_version=None):
        if direction not in {"incoming", "outgoing", "draft", "unknown"}:
            raise DomainError("invalid_input", "Unknown mail direction")
        if payload.get("id") != provider_message_id:
            raise DomainError("invalid_input", "Fetched mail identity changed")
        body = payload.get("body") or {}
        if not isinstance(body, dict):
            raise DomainError("invalid_input", "Mail body must be an object")
        direction = "draft" if payload.get("isDraft") is True else direction
        sanitized = sanitize_mail(payload.get("subject") or "", body.get("content") or "",
                                  body_kind=body.get("contentType") or "text", max_chars=250000, drop_quoted_history=False)
        metadata = {key: payload.get(key) for key in ("sender", "from", "replyTo", "toRecipients", "ccRecipients",
            "internetMessageId", "conversationId", "subject", "receivedDateTime", "sentDateTime", "lastModifiedDateTime")}
        metadata["direction"] = direction
        metadata["source_body"] = body  # Exact private original, including quoted history.
        reasons = []
        if sanitized.truncated:
            reasons.append("text_truncated")
        if payload.get("hasAttachments"):
            reasons.append("attachments_not_loaded")
        if direction == "unknown":
            reasons.append("direction_unknown")
        coverage = {"complete": not reasons, "reasons": reasons}
        # Never compare provider change tokens lexically. The archive uses actual
        # modification instants and immutable local sequence numbers for ordering.
        source_version = source_version or digest({"modified_at": payload.get("lastModifiedDateTime"),
            "sha256": sanitized.content_sha256, "metadata": metadata, "coverage": coverage})
        archived = self.archive.archive_message(account_id=self.account_id, provider_message_id=provider_message_id,
            source_version=source_version, text=sanitized.text, metadata=metadata,
            modified_at=payload.get("lastModifiedDateTime"), coverage=coverage)
        dto = {"account_id": self.account_id, "provider_message_id": provider_message_id,
            "source_version": archived["sequence"], "direction": direction,
            "occurred_at": payload.get("sentDateTime") if direction == "outgoing" else payload.get("receivedDateTime"),
            "thread_id": payload.get("conversationId"), "archive_ref": archived["archive_ref"],
            "source_sha256": archived["source_sha256"], "content_chars": archived["content_chars"],
            "make_current": archived["make_current"], "metadata": {"coverage": coverage,
                "attachments": bool(payload.get("hasAttachments")), "sanitizer_version": "mail-sanitizer-v1",
                "subject_hash": hashlib.sha256(sanitized.subject.encode("utf-8")).hexdigest()}}
        # No message body or private routing metadata enters command receipts.
        return self.runtime.command(worker_context("record_message", "ingest:" + archived["archive_ref"] + ":" + str(archived["make_current"])), "record_message", dto)


class ApplicationMailReader:
    """Human-facing body read bound to an accepted association and account grant."""
    def __init__(self, runtime, archive, allowed_accounts):
        self.runtime, self.archive = runtime, archive
        self.allowed_accounts = frozenset(allowed_accounts)

    def read_message(self, application_id, message_id, *, limit=250000):
        with self.runtime.executor.read() as con:
            return self._read_message(con, application_id, message_id, limit=limit)

    def _read_message(self, con, application_id, message_id, *, limit=250000):
        app = self.runtime.applications.get_application(con, application_id)
        association = self.runtime.correspondence.association(con, message_id)
        if association is None or association["application_id"] != app["id"]:
            raise DomainError("not_authorized", "Message is not associated with this application")
        source = self.runtime.correspondence.get(con, message_id)
        if source["account_id"] not in self.allowed_accounts:
            raise DomainError("not_authorized", "Evidence account is not granted")
        evidence = self.runtime.correspondence.evidence(con, message_id,
            archive=self.archive.for_account(source["account_id"]), allowed_accounts=self.allowed_accounts, limit=limit)
        return {"message_id": message_id, "revision": evidence["revision"],
                "text": evidence["text"], "coverage": evidence["coverage"]}

    def can_review_source(self, source_id, revision, sha256):
        with self.runtime.executor.read() as con:
            source = self.runtime.correspondence.get(con, source_id, revision)
            if source["account_id"] not in self.allowed_accounts:
                raise DomainError("not_authorized", "Evidence account is not granted")
            if source["source_sha256"] != sha256:
                raise DomainError("version_conflict", "Reviewed source hash changed")
        return True

    def read_review_source(self, source_id, revision, sha256, *, limit=250000):
        """Human review may open unassociated evidence, bound to its exact revision.

        This method is exposed only by the authenticated dashboard. Application and
        agent reads continue to require a current reviewed association.
        """
        if (not isinstance(source_id, str) or not source_id or
                not isinstance(revision, str) or not revision or
                not isinstance(sha256, str) or len(sha256) != 64):
            raise DomainError("invalid_input", "Exact source identity, revision, and hash are required")
        with self.runtime.executor.read() as con:
            source = self.runtime.correspondence.get(con, source_id, revision)
            if source["account_id"] not in self.allowed_accounts:
                raise DomainError("not_authorized", "Evidence account is not granted")
            if source["source_sha256"] != sha256:
                raise DomainError("version_conflict", "Reviewed source hash does not match its preserved revision")
            evidence = self.runtime.correspondence.evidence(con, source_id, revision=revision,
                archive=self.archive.for_account(source["account_id"]),
                allowed_accounts=self.allowed_accounts, limit=limit)
            return {"message_id": source_id, "revision": evidence["revision"], "sha256": sha256,
                    "text": evidence["text"], "coverage": evidence["coverage"]}

    def search_mail_page(self, query, limit=25, *, cursor=None):
        """Search at most 100 reviewed message bodies per page, without provider I/O."""
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or type(limit) is not int or not 1 <= limit <= 25:
            raise DomainError("invalid_input", "Invalid bounded mail search")
        scope = digest({"query": query, "accounts": sorted(self.allowed_accounts), "scope": "reviewed_correspondence_v1"})
        after = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                if set(value) != {"scope", "after"} or value["scope"] != scope or not isinstance(value["after"], str):
                    raise ValueError()
                after = value["after"]
            except (ValueError, TypeError, UnicodeError) as exc:
                raise DomainError("invalid_input", "Cursor does not belong to this mail search") from exc
        items, reasons, scanned, next_after = [], set(), 0, None
        with self.runtime.executor.read() as con:
            page = self.runtime.correspondence.linked_messages(con, allowed_accounts=self.allowed_accounts, limit=100, after=after)
            for index, candidate in enumerate(page["items"]):
                evidence = self._read_message(con, candidate["application_id"], candidate["message_id"])
                scanned += 1
                if not evidence["coverage"]["complete"]:
                    reasons.add("incomplete_message_evidence")
                body = evidence["text"]
                position = body.casefold().find(query.casefold()) if body is not None else -1
                if position >= 0:
                    items.append({"application_id": candidate["application_id"], "message_id": candidate["message_id"],
                        "revision": evidence["revision"], "excerpt": body[max(0, position - 160):position + 340], "coverage": evidence["coverage"]})
                more = index + 1 < len(page["items"]) or page["next_cursor"] is not None
                next_after = candidate["message_id"] if more else None
                if len(items) == limit:
                    break
        if next_after:
            reasons.add("more_messages_to_search")
        next_cursor = base64.urlsafe_b64encode(json.dumps({"scope": scope, "after": next_after}).encode()).decode() if next_after else None
        return {"items": items, "next_cursor": next_cursor, "truncated": next_cursor is not None,
            "coverage": {"complete": not reasons, "scope": "reviewed_correspondence", "scanned": scanned,
                         "scan_limit": 100, "reasons": sorted(reasons)}}


@dataclass(frozen=True)
class SyncResult:
    staged: int
    pages: int
    reset: bool
    cursor_committed: bool


@dataclass(frozen=True)
class ProcessingResult:
    processed: int
    ignored: int
    failed: int
    proposed: int = 0
    auto_applied: int = 0


class OwnerMailTaskHandler:
    """Capture every message in explicit folders; Understanding owns relevance."""
    def __init__(self,coordinator,*,account_id,folder_refs,max_messages=100,activation_start=None):
        if not isinstance(folder_refs,(tuple,list)):
            raise ValueError("Explicit bounded mail folders are required")
        self.folder_refs = tuple(dict.fromkeys(folder_refs))
        if not self.folder_refs or len(self.folder_refs)>100 or any(not isinstance(f,str) or not f.strip() for f in self.folder_refs):
            raise ValueError("Explicit bounded mail folders are required")
        self.coordinator,self.account_id = coordinator,account_id
        self.max_messages,self.activation_start = max_messages,activation_start

    def __call__(self,payload,context):
        if self.activation_start is not None:
            start = self.activation_start()
            if start is None: raise ValueError("Activate mail before synchronizing new messages")
            from .contracts import parse_utc
            parse_utc(start)
            self.coordinator.received_since = start
        sync = {folder:asdict(self.coordinator.sync_folder(self.account_id,folder,all_history=True,
            heartbeat=context.heartbeat)) for folder in self.folder_refs}
        processed = self.coordinator.process_pending(self.max_messages,heartbeat=context.heartbeat,folder_refs=self.folder_refs)
        return {"folder_syncs":sync,"processing":asdict(processed)}


class OwnerMailCoordinator:
    """Capture scoped immutable messages; no relevance or lifecycle decisions."""
    def __init__(self, mail, state, ingestor, *, received_since=None):
        self.mail, self.state, self.ingestor = mail, state, ingestor
        self.received_since = received_since
        self._roles = {"inbox": "incoming", "sentitems": "outgoing", "drafts": "draft"}

    def _account(self, account_id):
        if account_id != self.ingestor.account_id:
            raise DomainError("not_authorized", "Mail account is outside this connector")

    def sync_folder(self, account_id, folder_ref="inbox", *, query_version=OWNER_QUERY_VERSION,
                    now=None, max_pages=100, heartbeat=None, all_history=False):
        self._account(account_id)
        if not 1 <= max_pages <= 10000:
            raise ValueError("Invalid mail page bound")
        # A distinct query version prevents candidate ingestion from consuming or
        # acknowledging the historical semantic coordinator's staging rows.
        query_version = OWNER_QUERY_VERSION
        cursor = self.state.load(account_id, folder_ref, query_version)
        url = cursor.in_flight_next_link or cursor.committed_delta_link
        if url is None:
            url = self.mail.initial_all_history_delta_url(folder=folder_ref) if all_history else self.mail.initial_delta_url(folder=folder_ref, now=now)
        staged = pages = 0
        from .outlook.transport import GraphHttpError
        while url and pages < max_pages:
            if heartbeat is not None and not heartbeat():
                raise RetryableTaskError("Mail worker lease was lost")
            try:
                page = self.mail.read_delta_page(url)
            except GraphHttpError as exc:
                if exc.status == 410 or exc.error_code.casefold() == "syncstatenotfound":
                    self.state.reset(cursor)
                    return SyncResult(staged, pages, True, False)
                raise
            staged += self.state.stage_changes(account_id, folder_ref, page.changes, query_version)
            pages += 1
            if page.next_link:
                cursor = self.state.checkpoint(cursor, page.next_link)
                url = page.next_link
            elif page.delta_link:
                self.state.commit(cursor, page.delta_link)
                url = None
            else:
                raise ValueError("Mail page has no cursor")
        return SyncResult(staged, pages, False, url is None)

    def process_pending(self, limit=100, *, query_version=OWNER_QUERY_VERSION, transient_attempt=1,
                        transient_limit=5, heartbeat=None,folder_refs=None):
        del query_version, transient_attempt, transient_limit
        rows = self.state.pending_messages(limit, query_version=OWNER_QUERY_VERSION,
                    account_id=self.ingestor.account_id, received_since=self.received_since,folder_refs=folder_refs)
        processed = 0
        for row in rows:
            if heartbeat is not None and not heartbeat():
                raise RetryableTaskError("Mail worker lease was lost")
            payload = self.mail.read_message_body(row["immutable_message_id"])
            folder = payload.get("parentFolderId") or row["folder_ref"]
            self.ingestor.ingest(row["immutable_message_id"], payload, direction=self._roles.get(folder, self._roles.get(row["folder_ref"], "unknown")))
            processed += self.state.mark_revision(row["account_id"], row["folder_ref"], row["immutable_message_id"],
                query_version=OWNER_QUERY_VERSION, modified_at=row["modified_at"])
        return ProcessingResult(processed, 0, 0)


def capture_candidates(runtime, message_id, text, *, limit=20, scan_limit=200):
    """Bounded retrieval is context, never an accepted association."""
    if not 1 <= limit <= 20 or not limit <= scan_limit <= 200:
        raise ValueError("Invalid candidate retrieval bounds")
    with runtime.executor.read() as con:
        page = runtime.applications.list_applications(con, limit=scan_limit)
        associated = runtime.correspondence.association(con, message_id)
        ranked = []
        normalized = text.casefold()
        for app in page["items"]:
            job = runtime.applications.get_job(con, app["job_id"])
            score = sum(bool(job.get(key)) and str(job[key]).casefold() in normalized for key in ("employer", "title"))
            ranked.append((score, app["id"]))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        # No match is useful context. Arbitrary applications add no evidence and
        # turn unrelated mail into an oversized, falsely incomplete analysis.
        matches = [identity for score, identity in ranked if score]
        if associated:
            matches = [associated["application_id"]] + [identity for identity in matches if identity != associated["application_id"]]
    return {"candidate_ids": matches[:limit], "coverage": {"complete": not page["truncated"] and len(matches) <= limit,
            "scanned": len(page["items"]), "scan_limit": scan_limit, "selected": min(limit, len(matches))}}


def build_application_mail_handlers(runtime, archive, account_id, analyzer, model_version, *, operational_db=None):
    """Handlers registered by the root composition on existing worker lanes."""
    scoped = archive.for_account(account_id)
    names = {("correspondence", "understand_message"): "applications.mail.understand",
             ("correspondence", "observe_outgoing"): "applications.mail.outgoing",
             ("understanding", "retry_processing"): "applications.mail.understand",
             ("understanding", "project_analysis"): "applications.mail.project"}

    def check_work(value):
        if not isinstance(value, dict) or set(value) != {"owner", "kind", "key", "work_id"} or (value["owner"], value["kind"]) not in names:
            raise PermanentTaskError("Invalid application mail work reference")
        with runtime.executor.read() as con:
            work = runtime.executor.find_work(con, value["owner"], value["kind"], value["key"])
            if work is None or work["id"] != value["work_id"]:
                raise PermanentTaskError("Application mail work reference changed")
            payload = json.loads(work["payload"])
            if value["kind"] == "project_analysis":
                analysis = runtime.understanding.get_analysis(con, payload["analysis_id"])
                captured = analysis["descriptor"]["context"].get("mail_projection")
                if not captured:
                    raise PermanentTaskError("Analysis has no captured mail context")
                source = runtime.correspondence.get(con, captured["message_id"], captured["revision"])
            else:
                source = runtime.correspondence.get(con, payload["message_id"], payload["revision"])
            if source["account_id"] != account_id:
                raise AccountNotGranted("Application mail account is not granted")
        return work, payload, source

    def dispatch(payload, context):
        if set(payload) <= {"schedule_key", "scheduled_for", "configuration"}:
            payload = payload.get("configuration", {})
        if not isinstance(payload, dict) or set(payload) - {"after", "recovery_after", "recovery_only"}:
            raise PermanentTaskError("Invalid mail dispatcher input")
        with runtime.executor.read() as con:
            page = ({"items":[],"next_cursor":None} if payload.get("recovery_only") else
                runtime.executor.work_page(con, limit=100, after=payload.get("after", "")))
        from .application_mail_recovery import reconcile_mail_work
        recovery = reconcile_mail_work(runtime, account_id, owner_work=page["items"],
            operational_db=operational_db, after=payload.get("recovery_after", ""))
        followups = []
        if recovery["next_cursor"]:
            followups.append(FollowUpTask("applications.mail.dispatch", {
                "recovery_after":recovery["next_cursor"],"recovery_only":True}, lane="core"))
        for item in page["items"]:
            pair = (item["owner"], item["kind"])
            if pair not in names:
                continue
            reference = {"owner": item["owner"], "kind": item["kind"], "key": item["dedupe_key"], "work_id": item["id"]}
            try:
                current, _, _ = check_work(reference)
                if current["status"] == "done":
                    continue
            except AccountNotGranted:
                continue  # Another configured account owns this work.
            except (DomainError, PermanentTaskError, KeyError):
                pass  # Dispatch malformed records too: their handler records a visible failure.
            followups.append(FollowUpTask(names[pair], reference, lane="model" if pair[1] in {"understand_message", "retry_processing"} else "core",
                                         dedupe_key=item["id"]))
        if page["next_cursor"]:
            followups.append(FollowUpTask("applications.mail.dispatch", {"after": page["next_cursor"]}, lane="core"))
        return TaskResult({"scheduled": len(followups), "more": bool(page["next_cursor"]), "recovery":recovery}, tuple(followups))

    def understand(value, context):
        work, payload, source = check_work(value)
        if value["kind"] not in {"understand_message", "retry_processing"}:
            raise PermanentTaskError("Mail work was routed to the wrong handler")
        if work["status"] == "done":
            return {"status": "already_processed"}
        retry = payload if value["kind"] == "retry_processing" else None
        with runtime.executor.read() as con:
            latest = runtime.correspondence.get(con, payload["message_id"])
            applicable = (runtime.understanding.processing_retry_applicable(con, retry) if retry else
                runtime.understanding.processing_source_allowed(con, payload["message_id"], payload["revision"]))
            applicable = applicable and latest["revision"] == payload["revision"]
        if not applicable:
            runtime.executor.complete_work(work["id"], owner=value["owner"], kind=value["kind"])
            return {"status": "superseded"}
        if analyzer is None:
            raise PermanentTaskError("Application Understanding provider is not configured")
        # Account-scoped archive access precedes any private candidate retrieval.
        try:
            text = scoped.read_message(source["archive_ref"])
            candidates = capture_candidates(runtime, payload["message_id"], text)
        except Exception:
            # Runtime records a bounded source-failure receipt using immutable refs.
            candidates = {"candidate_ids": [], "coverage": {"complete": False}}
        try:
            result = runtime.analyze_message(payload["message_id"], revision=payload["revision"],
                archive=scoped, allowed_accounts=(account_id,), analyzer=analyzer, model_version=model_version,
                candidate_ids=candidates["candidate_ids"], candidate_coverage=candidates["coverage"], heartbeat=context.heartbeat,
                processing_retry=retry)
        except DomainError as exc:
            if exc.code != "version_conflict" or not retry:
                raise
            with runtime.executor.read() as con:
                current = runtime.understanding.processing_retry_applicable(con, retry)
            if current:
                raise
            runtime.executor.complete_work(work["id"], owner=value["owner"], kind=value["kind"])
            return {"status": "superseded"}
        if result.get("status") in {"busy", "source_unavailable"}:
            raise RetryableTaskError("Application mail evidence is temporarily unavailable", retry_after_seconds=60)
        if result.get("status") == "failed":
            raise PermanentTaskError("Application mail analysis failed validation")
        if retry:
            runtime.executor.complete_work(work["id"], owner=value["owner"], kind=value["kind"])
        return {"status": "processed", "analysis_id": result.get("analysis_id"), "proposal_count": len(result.get("proposals", ())) }

    def project(value, context):
        work, payload, source = check_work(value)
        if value["kind"] != "project_analysis":
            raise PermanentTaskError("Mail work was routed to the wrong handler")
        if work["status"] == "done":
            return {"status": "already_processed"}
        result = runtime.project_message_analysis(payload["analysis_id"])
        return {"status": "processed", "analysis_id": result["analysis_id"], "proposal_count": len(result["proposals"])}

    def outgoing(value, context):
        work, payload, source = check_work(value)
        if value["kind"] != "observe_outgoing":
            raise PermanentTaskError("Mail work was routed to the wrong handler")
        if source["direction"] == "incoming":
            raise PermanentTaskError("Incoming mail cannot use outgoing bookkeeping")
        runtime.executor.complete_work(work["id"], owner="correspondence", kind="observe_outgoing")
        return {"status": "preserved", "direction": source["direction"], "inferred_changes": 0}

    return {"applications.mail.dispatch": dispatch, "applications.mail.understand": understand,
            "applications.mail.project": project, "applications.mail.outgoing": outgoing}
