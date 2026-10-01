"""Replay-safe Outlook synchronization and mail-to-application proposals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from email.utils import parseaddr
from typing import Any, Callable, Mapping, Optional, Sequence

from .contracts import MAIL_EXCERPT_LIMIT, MutationContext, payload_sha256
from .inference import InferenceTransportError
from .mail.context import CandidateApplication
from .mail.evaluation import EvaluationReport
from .mail.pipeline import analyze_mail
from .mail.policy import ProposalDisposition, decide_proposal
from .mail.sanitizer import sanitize_mail
from .outlook.mail import GraphMailClient
from .outlook.auth import OutlookAuthRequired
from .outlook.state import SQLiteOutlookState
from .outlook.transport import GraphHttpError
from .service import DeterministicJobSearchService


QUERY_VERSION = 1
ALL_HISTORY_QUERY_VERSION = 2
RECRUITING_TERMS = (
    "application", "apply", "recruit", "interview", "assessment", "candidate",
    "offer", "position", "role", "hiring", "greenhouse", "ashby", "lever",
    "workday",
)
TRUSTED_MICROSOFT_AUTHSERV_SUFFIXES = (
    ".prod.outlook.com",
    ".protection.outlook.com",
)


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
    proposed: int
    auto_applied: int


@dataclass(frozen=True)
class MailboxSyncResult:
    discovered: int
    excluded: int
    folders_synced: int
    messages_staged: int
    pages: int


def likely_recruiting_message(sender: str, subject: str) -> bool:
    searchable = f"{sender}\n{subject}".casefold()
    return any(term in searchable for term in RECRUITING_TERMS)


def _sender(payload: Mapping[str, Any], fallback: str) -> str:
    value = payload.get("sender") or payload.get("from")
    if isinstance(value, Mapping):
        address = value.get("emailAddress")
        if isinstance(address, Mapping) and isinstance(address.get("address"), str):
            return str(address["address"])
    return fallback


def _body(payload: Mapping[str, Any]) -> tuple[str, str]:
    value = payload.get("body")
    if not isinstance(value, Mapping) or not isinstance(value.get("content"), str):
        raise ValueError("Outlook message body is missing")
    return str(value["content"]), str(value.get("contentType") or "text")


def authenticated_sender(payload: Mapping[str, Any], sender: str) -> bool:
    """Require a Microsoft-stamped DMARC pass aligned to the visible From domain.

    The sender-controlled display address alone is never an authentication signal.
    Missing, malformed, sender-injected, or conflicting Graph header data therefore
    fails closed and leaves a rule-derived proposal for human review.
    """

    address = parseaddr(sender)[1].strip().casefold()
    if address.count("@") != 1:
        return False
    domain = address.rsplit("@", 1)[1].rstrip(".")
    headers = payload.get("internetMessageHeaders")
    if not isinstance(headers, list):
        return False
    # Only the first Authentication-Results field is authoritative. A sender may
    # inject additional copies farther down the message, so never search for any
    # later header that happens to say "pass".
    authentication_result = ""
    for header in headers:
        if not isinstance(header, Mapping):
            continue
        if str(header.get("name") or "").casefold() != "authentication-results":
            continue
        authentication_result = str(header.get("value") or "").casefold().strip()
        break
    if not authentication_result or ";" not in authentication_result:
        return False
    first_segment, remainder = authentication_result.split(";", 1)
    # Exchange Online commonly omits RFC 7601's optional authserv-id and starts
    # directly with `spf=...`. If an authserv-id is present, pin it to Outlook;
    # either way require Microsoft's composite-authentication result as well.
    if re.match(r"\s*(?:spf|dkim|dmarc|compauth)\s*=", first_segment):
        results = authentication_result
    else:
        stamp = re.fullmatch(r"([a-z0-9.-]+)(?:\s+[0-9]+)?", first_segment.strip())
        authserv_id = stamp.group(1).rstrip(".") if stamp else ""
        if not (authserv_id == "mx.microsoft.com" or any(
            authserv_id.endswith(suffix)
            for suffix in TRUSTED_MICROSOFT_AUTHSERV_SUFFIXES
        )):
            return False
        results = remainder
    if not re.search(r"(?:^|[;\s])dmarc\s*=\s*pass(?:[;\s]|$)", results):
        return False
    if not re.search(r"(?:^|[;\s])compauth\s*=\s*pass(?:[;\s]|$)", results):
        return False
    matches = re.findall(r"header\.from\s*=\s*([^;\s()]+)", results)
    return any(item.rstrip(".") == domain for item in matches)


class OutlookMailCoordinator:
    def __init__(
        self,
        mail: GraphMailClient,
        state: SQLiteOutlookState,
        service: DeterministicJobSearchService,
        classifier: Any | None = None,
        model_version: str = "local-mail-model-v1",
        evaluation_reports: Optional[Mapping[str, EvaluationReport]] = None,
        secure_ingestor: Any | None = None,
        received_since: str | None = None,
        recruiting_only: bool = False,
    ) -> None:
        self.mail = mail
        self.state = state
        self.service = service
        self.classifier = classifier
        self.model_version = model_version
        self.evaluation_reports = dict(evaluation_reports or {})
        self.secure_ingestor = secure_ingestor
        self.received_since = received_since
        self.recruiting_only = recruiting_only
        if received_since:
            from .contracts import parse_utc
            parse_utc(received_since)

    def sync_folder(
        self,
        account_id: str,
        folder_ref: str = "inbox",
        *,
        query_version: int = QUERY_VERSION,
        now: Optional[datetime] = None,
        max_pages: int = 10_000,
        heartbeat: Optional[Callable[[], bool]] = None,
        all_history: bool = False,
    ) -> SyncResult:
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        cursor = self.state.load(account_id, folder_ref, query_version)
        staged = 0
        pages = 0
        connector_key = f"outlook:{account_id}:{folder_ref}"
        try:
            if all_history and cursor.needs_backfill and not cursor.in_flight_next_link:
                url = self.mail.initial_all_history_delta_url(folder=folder_ref)
            elif cursor.needs_backfill and not cursor.in_flight_next_link:
                url: Optional[str] = self.mail.backfill_url(folder=folder_ref, now=now)
                while url:
                    if pages >= max_pages:
                        raise RuntimeError("Outlook backfill exceeded its page bound")
                    page = self.mail.read_backfill_page(url)
                    staged += self.state.stage_changes(
                        account_id, folder_ref, page.changes, query_version
                    )
                    pages += 1
                    if heartbeat is not None and not heartbeat():
                        raise RuntimeError("Outlook sync worker lease was lost")
                    url = page.next_link
                url = self.mail.initial_delta_url(folder=folder_ref, now=now)
            else:
                url = cursor.in_flight_next_link or cursor.committed_delta_link
                if not url:
                    url = self.mail.initial_delta_url(folder=folder_ref, now=now)

            while url:
                if pages >= max_pages:
                    self.state.set_health(connector_key, "degraded", "initial synchronization continuing")
                    return SyncResult(staged, pages, False, False)
                page = self.mail.read_delta_page(url)
                staged += self.state.stage_changes(
                    account_id, folder_ref, page.changes, query_version
                )
                pages += 1
                if heartbeat is not None and not heartbeat():
                    raise RuntimeError("Outlook sync worker lease was lost")
                if page.next_link:
                    cursor = self.state.checkpoint(cursor, page.next_link)
                    url = page.next_link
                elif page.delta_link:
                    cursor = self.state.commit(cursor, page.delta_link)
                    url = None
                else:  # guarded by GraphMailClient, retained for injected fakes
                    raise ValueError("Outlook delta page has no cursor")
        except OutlookAuthRequired as exc:
            self.state.set_health(connector_key, "reauth_required", str(exc))
            raise
        except GraphHttpError as exc:
            if exc.status == 410 or exc.error_code.casefold() == "syncstatenotfound":
                self.state.reset(cursor)
                self.state.set_health(
                    connector_key, "degraded", "delta cursor expired; bounded reset required"
                )
                return SyncResult(staged, pages, True, False)
            status = "reauth_required" if exc.status == 401 else "degraded"
            self.state.set_health(
                connector_key,
                status,
                str(exc),
                next_attempt_at=exc.decision.next_attempt_at,
            )
            raise
        except Exception as exc:
            self.state.set_health(connector_key, "failed", str(exc))
            raise
        self.state.set_health(connector_key, "healthy", success=True)
        return SyncResult(staged, pages, False, True)

    def discover_mailbox_folders(
        self, account_id: str, *, max_folders: int = 4096
    ) -> Mapping[str, int]:
        """Persist an ID-only folder inventory, excluding Junk/Deleted subtrees."""

        junk = self.mail.read_mail_folder("junkemail")
        deleted = self.mail.read_mail_folder("deleteditems")
        discovered = {item.folder_id: item for item in self.mail.list_folder_tree(
            max_folders=max_folders
        )}
        discovered[junk.folder_id] = junk
        discovered[deleted.folder_id] = deleted
        return self.state.replace_folder_inventory(
            account_id,
            tuple(discovered.values()),
            excluded_roots={junk.folder_id: "junk", deleted.folder_id: "deleted"},
        )

    def sync_all_history(
        self,
        account_id: str,
        *,
        max_folders: int = 4096,
        max_pages_per_folder: int = 10_000,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> MailboxSyncResult:
        """Discover and delta-sync every non-Junk/non-Deleted folder from history start."""

        inventory = self.discover_mailbox_folders(
            account_id, max_folders=max_folders
        )
        staged = pages = synced = 0
        for folder_id in self.state.eligible_folders(account_id):
            result = self.sync_folder(
                account_id,
                folder_id,
                query_version=ALL_HISTORY_QUERY_VERSION,
                max_pages=max_pages_per_folder,
                heartbeat=heartbeat,
                all_history=True,
            )
            staged += result.staged
            pages += result.pages
            synced += int(result.cursor_committed)
        return MailboxSyncResult(
            int(inventory["folders"]),
            int(inventory["excluded"]),
            synced,
            staged,
            pages,
        )

    def _candidates(self, subject: str = "", content: str = "", body_kind: str = "text",
                    received_at: str = "", *, sender: str = "", account_id: str = "",
                    conversation_id: str = "") -> tuple[Sequence[CandidateApplication], bool]:
        from .mail.matching import rank_mail_candidates
        message = sanitize_mail(subject, content, body_kind=body_kind)
        rows = rank_mail_candidates(self.service.list_mail_candidates(
            received_at=received_at, sender=sender, account_id=account_id,
            conversation_id=conversation_id,
        ), message.subject + " " + message.body)
        # Compare the entire local history first, then bound external model input.
        # Keep every positively matching alternative when it fits. Truncation
        # always leaves the result review-only, even if a top match looks strong.
        relevant = [row for row in rows if row["mail_match_score"] > 0]
        pool = relevant or rows
        complete = len(pool) <= 20
        selected = pool[:20]
        values = [CandidateApplication(
            application_id=str(row["application_id"]), ats=str(row["ats"]),
            job_id=str(row["job_id"]), employer=str(row["employer_snapshot"]),
            title=str(row["title_snapshot"]), company_slug=str(row["company_slug_snapshot"]),
            phase=str(row["current_phase"]), submitted_at=str(row["submitted_at"] or ""),
            submission_attempted_at=str(row["submission_attempted_at"] or ""),
            match_context=str(row["mail_match_context"]),
        ) for row in selected]
        return values, complete

    def process_pending(
        self,
        limit: int = 100,
        *,
        query_version: int = QUERY_VERSION,
        transient_attempt: int = 1,
        transient_limit: int = 5,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> ProcessingResult:
        if not 1 <= transient_attempt <= transient_limit <= 20:
            raise ValueError("invalid transient mail attempt bounds")
        processed = ignored = failed = proposed = auto_applied = 0
        pending_options = {"query_version":query_version}
        if self.received_since: pending_options["received_since"] = self.received_since
        for staged in self.state.pending_messages(limit, **pending_options):
            identity = (
                str(staged["account_id"]),
                str(staged["folder_ref"]),
                str(staged["immutable_message_id"]),
            )
            if self.received_since:
                from .contracts import parse_utc
                try:
                    eligible = bool(staged["received_at"]) and parse_utc(str(staged["received_at"])) >= parse_utc(self.received_since)
                except ValueError:
                    eligible = False
                if not eligible:
                    self.state.mark_message(*identity, "ignored", query_version=query_version)
                    ignored += 1
                    if heartbeat is not None and not heartbeat():
                        raise RuntimeError("Outlook sync worker lease was lost")
                    continue
            from .mail.rules import is_application_verification_email
            if is_application_verification_email(str(staged["sender"]), str(staged["subject"])):
                self.state.mark_message(*identity, "ignored", query_version=query_version)
                ignored += 1
                if heartbeat is not None and not heartbeat():
                    raise RuntimeError("Outlook mail worker lease was lost")
                continue
            recruiting = likely_recruiting_message(
                str(staged["sender"]), str(staged["subject"])
            )
            if not recruiting:
                # Replies often omit recruiting keywords. Previously linked threads,
                # senders, and named applications still deserve classification.
                context, _ = self._candidates(
                    str(staged["subject"]), sender=str(staged["sender"]),
                    account_id=identity[0], conversation_id=str(staged["conversation_id"]),
                )
                recruiting = any(item.match_context for item in context)
            if not recruiting and (self.recruiting_only or self.secure_ingestor is None):
                self.state.mark_message(
                    *identity, "ignored", query_version=query_version
                )
                ignored += 1
                if heartbeat is not None and not heartbeat():
                    raise RuntimeError("Outlook mail worker lease was lost")
                continue
            try:
                payload = self.mail.read_message_body(identity[2])
                content, content_type = _body(payload)
                subject = str(payload.get("subject") or staged["subject"])
                sender = _sender(payload, str(staged["sender"]))
                received_at = str(payload.get("receivedDateTime") or staged["received_at"] or "")
                candidates = None
                candidate_context_complete = False
                if self.secure_ingestor is not None:
                    candidates, candidate_context_complete = self._candidates(
                        subject, content, content_type, received_at, sender=sender,
                        account_id=identity[0], conversation_id=str(payload.get("conversationId") or staged["conversation_id"] or ""),
                    )
                    self.secure_ingestor.ingest(
                        account_id=identity[0],
                        immutable_message_id=identity[2],
                        subject=subject,
                        body=content,
                        body_kind=content_type,
                        received_at=received_at,
                        candidates=candidates,
                        has_attachments=bool(payload.get("hasAttachments", False)),
                        analyze_temporal=recruiting,
                    )
                if not recruiting:
                    self.state.mark_message(
                        *identity, "ignored", query_version=query_version
                    )
                    ignored += 1
                    if heartbeat is not None and not heartbeat():
                        raise RuntimeError("Outlook mail worker lease was lost")
                    continue
                # Rules and classifiers see exactly the text retained as evidence,
                # so every cited span remains reviewable after raw mail is discarded.
                sanitized = sanitize_mail(
                    subject,
                    content,
                    body_kind=content_type,
                    max_chars=MAIL_EXCERPT_LIMIT,
                )
                evidence_result = self.service.record_mail_evidence(
                    {
                        "account_id": identity[0],
                        "immutable_message_id": identity[2],
                        "conversation_id": str(
                            payload.get("conversationId") or staged["conversation_id"] or ""
                        ),
                        "sender": sender,
                        "subject": sanitized.subject,
                        "received_at": received_at,
                        "body_sha256": sanitized.content_sha256,
                        "excerpt": sanitized.text,
                    },
                    MutationContext(
                        "evidence:" + payload_sha256({
                            "account": identity[0], "message": identity[2],
                            "body": sanitized.content_sha256,
                        }),
                        "system",
                        "outlook_sync",
                        identity[2],
                    ),
                )
                evidence_id = str(evidence_result["evidence"]["evidence_id"])
                if candidates is None:
                    candidates, candidate_context_complete = self._candidates(
                        subject, content, content_type, received_at, sender=sender,
                        account_id=identity[0], conversation_id=str(payload.get("conversationId") or staged["conversation_id"] or ""),
                    )
                proposal = analyze_mail(
                    evidence_id=evidence_id,
                    sender_address=sender,
                    mail=sanitized,
                    candidates=candidates,
                    classifier=self.classifier,
                    model_version=self.model_version,
                    received_at=received_at,
                    sender_authenticated=authenticated_sender(payload, sender),
                    candidate_context_complete=candidate_context_complete,
                )
                if proposal is not None:
                    created = self.service.create_event_proposal(
                        proposal,
                        MutationContext(
                            "proposal:" + proposal.dedupe_key,
                            proposal.producer_kind.value,
                            "outlook_mail",
                            identity[2],
                        ),
                    )
                    proposed += int(bool(created["created"]))
                    report = self.evaluation_reports.get(proposal.producer_version)
                    decision = decide_proposal(proposal, evaluation_report=report)
                    # A bounded classifier sees at most twenty candidates. If there
                    # are more, application identity is incomplete and the proposal
                    # must remain visible for human review.
                    if (
                        candidate_context_complete
                        and decision.disposition is ProposalDisposition.AUTO_APPLY
                    ):
                        self.service.auto_apply_event_proposal(
                            str(created["proposal"]["proposal_id"]),
                            MutationContext(
                                "auto:" + proposal.dedupe_key,
                                "system",
                                "mail_policy",
                                identity[2],
                            ),
                        )
                        auto_applied += 1
                self.state.mark_message(
                    *identity, "processed", query_version=query_version
                )
                processed += 1
            except OutlookAuthRequired as exc:
                self.state.set_health(
                    f"outlook:{identity[0]}:{identity[1]}",
                    "reauth_required",
                    str(exc),
                )
                raise
            except GraphHttpError as exc:
                if exc.decision.retryable and transient_attempt < transient_limit:
                    self.state.set_health(
                        f"outlook:{identity[0]}:{identity[1]}",
                        "degraded",
                        str(exc),
                        next_attempt_at=exc.decision.next_attempt_at,
                    )
                    raise
                self.state.mark_message(
                    *identity, "failed", str(exc), query_version=query_version
                )
                failed += 1
            except InferenceTransportError as exc:
                if getattr(exc, "defer_without_attempt", False):
                    # A local usage policy made no remote call. Preserve staged
                    # evidence for the next bounded run rather than failing it.
                    raise
                if exc.retryable and transient_attempt < transient_limit:
                    self.state.set_health(
                        f"outlook:{identity[0]}:{identity[1]}",
                        "degraded",
                        str(exc),
                    )
                    raise
                self.state.mark_message(
                    *identity, "failed", str(exc), query_version=query_version
                )
                failed += 1
            except Exception as exc:
                self.state.mark_message(
                    *identity, "failed", str(exc), query_version=query_version
                )
                failed += 1
            if heartbeat is not None and not heartbeat():
                raise RuntimeError("Outlook mail worker lease was lost")
        return ProcessingResult(processed, ignored, failed, proposed, auto_applied)
