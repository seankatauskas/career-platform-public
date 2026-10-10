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
from .mail.classification_review import ClassificationRejected, save_review
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
        understanding: Any | None = None,
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
        self.understanding = understanding
        self._sent_folders = {}
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
        sent = self.mail.read_mail_folder("sentitems")
        inbox = self.mail.read_mail_folder("inbox")
        self._sent_folders[account_id] = sent.folder_id
        from .db import connect
        with connect(self.state.db_path) as con:
            for folder, role in ((sent, 'sentitems'), (inbox, 'inbox')):
                con.execute('INSERT INTO lifecycle_mail_folders VALUES (?,?,?) ON CONFLICT(account_id,role) DO UPDATE SET folder_id=excluded.folder_id', (account_id, folder.folder_id, role))
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
        _replay_rows=None,
    ) -> ProcessingResult:
        if not 1 <= transient_attempt <= transient_limit <= 20:
            raise ValueError("invalid transient mail attempt bounds")
        processed = ignored = failed = proposed = auto_applied = 0
        pending_options = {"query_version":query_version}
        historical = _replay_rows is not None
        if self.received_since and not historical: pending_options["received_since"] = self.received_since
        rows = _replay_rows if historical else self.state.pending_messages(limit, **pending_options)
        def mark(*args, **kwargs):
            if not historical:
                self.state.mark_message(*args, **kwargs)
        for staged in rows:
            identity = (
                str(staged["account_id"]),
                str(staged["folder_ref"]),
                str(staged["immutable_message_id"]),
            )
            if self.received_since and not historical:
                from .contracts import parse_utc
                try:
                    eligible = bool(staged["received_at"]) and parse_utc(str(staged["received_at"])) >= parse_utc(self.received_since)
                except ValueError:
                    eligible = False
                if not eligible:
                    mark(*identity, "ignored", query_version=query_version)
                    ignored += 1
                    if heartbeat is not None and not heartbeat():
                        raise RuntimeError("Outlook sync worker lease was lost")
                    continue
            from .mail.rules import is_application_verification_email
            if is_application_verification_email(str(staged["sender"]), str(staged["subject"])):
                mark(*identity, "ignored", query_version=query_version)
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
                mark(
                    *identity, "ignored", query_version=query_version
                )
                ignored += 1
                if heartbeat is not None and not heartbeat():
                    raise RuntimeError("Outlook mail worker lease was lost")
                continue
            try:
                payload = self.mail.read_message_body(identity[2])
                # Gate drafts before archiving or invoking either classifier. Draft
                # edits return to pending through modified_at and are checked again.
                if payload.get('isDraft') is True:
                    self.service.lifecycle.observe_mail({
                        'account_id': identity[0], 'immutable_message_id': identity[2],
                        'conversation_id': str(payload.get('conversationId') or staged['conversation_id'] or ''),
                        'folder_ref': identity[1], 'direction': 'draft',
                        'subject': sanitize_mail(str(payload.get('subject') or staged['subject']), '').subject[:512],
                        'source_at': str(payload.get('lastModifiedDateTime') or staged['modified_at'] or staged['received_at']),
                        'modified_at': str(payload.get('lastModifiedDateTime') or staged['modified_at'] or staged['received_at']),
                    }, MutationContext('draft-observation:' + payload_sha256({'id':identity,'modified':payload.get('lastModifiedDateTime') or staged['modified_at']}), 'system', 'outlook_sync'))
                    mark(*identity, 'ignored', 'draft excluded from lifecycle evidence', query_version=query_version)
                    ignored += 1
                    continue
                content, content_type = _body(payload)
                subject = str(payload.get("subject") or staged["subject"])
                sender = _sender(payload, str(staged["sender"]))
                received_at = str(payload.get("receivedDateTime") or staged["received_at"] or "")
                folder = str(payload.get('parentFolderId') or identity[1])
                from .db import connect
                with connect(self.state.db_path) as con:
                    known = con.execute('SELECT role FROM lifecycle_mail_folders WHERE account_id=? AND folder_id=?', (identity[0], folder)).fetchone()
                    prior = con.execute('SELECT direction FROM lifecycle_mail_observations WHERE account_id=? AND immutable_message_id=?', (identity[0], identity[2])).fetchone()
                role = known['role'] if known else folder
                direction = 'outbound' if role == 'sentitems' else ('inbound' if role == 'inbox' else 'unknown')
                if prior and prior['direction'] in {'inbound', 'outbound'}:
                    direction = prior['direction']
                recipients = []
                for field in ('toRecipients', 'ccRecipients'):
                    for recipient in (payload.get(field) or [])[:100]:
                        address = recipient.get('emailAddress', {}).get('address', '') if isinstance(recipient, Mapping) else ''
                        if address:
                            recipients.append(str(address)[:512])
                archive_id = None
                understanding_service = getattr(self.service, "mail_understanding", None)
                shared_owned = bool(understanding_service and understanding_service.owns_message(identity[0], identity[2]))
                understanding_mode = self.understanding.mode if self.understanding else "legacy"
                ingestion_coverage = ()
                candidates, candidate_context_complete = self._candidates(
                    subject, content, content_type, received_at, sender=sender,
                    account_id=identity[0], conversation_id=str(payload.get("conversationId") or staged["conversation_id"] or ""),
                )
                if self.secure_ingestor is not None:
                    candidates, candidate_context_complete = self._candidates(
                        subject, content, content_type, received_at, sender=sender,
                        account_id=identity[0], conversation_id=str(payload.get("conversationId") or staged["conversation_id"] or ""),
                    )
                    from .mail.identity import supported_selection
                    identity_mail = sanitize_mail(subject, content, body_kind=content_type)
                    temporal_candidates = tuple(c for c in candidates if supported_selection(
                        candidates, c.application_id, identity_mail.subject, identity_mail.body
                    )) if candidate_context_complete else ()
                    ingested = self.secure_ingestor.ingest(
                        account_id=identity[0],
                        immutable_message_id=identity[2],
                        subject=subject,
                        body=content,
                        body_kind=content_type,
                        received_at=received_at,
                        candidates=temporal_candidates,
                        has_attachments=bool(payload.get("hasAttachments", False)),
                        analyze_temporal=recruiting and direction == 'inbound' and not historical and bool(temporal_candidates) and understanding_mode in {'legacy','shadow'} and not shared_owned,
                        **({'report_coverage': True} if understanding_mode in {'shared','shadow','paused'} else {}),
                    )
                    archive_id = getattr(ingested, 'archive_id', None)
                    ingestion_coverage = getattr(ingested, 'coverage', ())
                if not recruiting:
                    mark(
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
                if understanding_service and understanding_mode in {"shared", "paused"} and direction == "inbound" and not historical:
                    understanding_service.own_evidence(evidence_id, MutationContext("understanding-owner:" + evidence_id, "system", "outlook_sync"))
                if candidates is None:
                    candidates, candidate_context_complete = self._candidates(
                        subject, content, content_type, received_at, sender=sender,
                        account_id=identity[0], conversation_id=str(payload.get("conversationId") or staged["conversation_id"] or ""),
                    )
                observation_values = {
                    'account_id': identity[0], 'immutable_message_id': identity[2],
                    'conversation_id': str(payload.get('conversationId') or staged['conversation_id'] or ''),
                    'folder_ref': folder, 'direction': direction, 'sender': sender[:512],
                    'recipients': recipients[:100], 'subject': sanitized.subject[:512],
                    'received_at': received_at, 'sent_at': payload.get('sentDateTime'),
                    'source_at': received_at, 'modified_at': payload.get('lastModifiedDateTime') or staged['modified_at'] or received_at,
                    'evidence_id': evidence_id, 'archive_id': archive_id,
                }
                observed = self.service.lifecycle.observe_mail(observation_values, MutationContext('observe:' + payload_sha256(observation_values), 'system', 'outlook_sync'))['observation']
                from .mail.identity import supported_selection
                linked = [c for c in candidates if 'previously linked email conversation' in c.match_context
                          and candidate_context_complete and supported_selection(
                              candidates, c.application_id, sanitized.subject, sanitized.body)]
                if len(linked) == 1:
                    self.service.lifecycle.link_mail({'observation_id': observed['observation_id'], 'application_id': linked[0].application_id, 'confidence':1.0, 'source':'accepted_conversation'}, MutationContext('mail-link:' + observed['observation_id'] + ':' + linked[0].application_id, 'system', 'outlook_sync'))
                if direction in {'outbound', 'unknown'}:
                    if direction == 'unknown' and not linked:
                        self.service.lifecycle.propose_discovery({'observation_id': observed['observation_id']}, MutationContext('discovery:' + observed['observation_id'], 'system', 'outlook_sync'))
                    mark(*identity, 'processed', query_version=query_version)
                    processed += 1
                    continue
                # Shared ownership survives mode changes: legacy interpretation cannot
                # recreate tasks or temporal proposals for an already migrated message.
                if understanding_mode == 'paused':
                    continue
                if shared_owned and understanding_mode != 'shared':
                    mark(*identity, 'processed', query_version=query_version)
                    processed += 1
                    continue
                if self.understanding is not None and understanding_mode in {'shadow','shared'}:
                    try:
                        understood = self.understanding.process(
                            {**observed, 'account_id': identity[0], 'immutable_message_id': identity[2]},
                            subject=subject, body=content, body_kind=content_type,
                            candidates=candidates, candidate_context_complete=candidate_context_complete,
                            coverage=ingestion_coverage, heartbeat=heartbeat,
                            replay_id='legacy-replay' if historical else None,
                        )
                    except Exception:
                        if understanding_mode == 'shared':
                            raise
                        # Shadow failures remain diagnostic and cannot change legacy effects.
                        understood = {'state': 'shadow_failed'}
                    if understanding_mode == 'shared':
                        if understood.get('state') in {'busy','paused'}:
                            continue
                        mark(*identity, 'processed', query_version=query_version)
                        processed += 1
                        continue
                with connect(self.state.db_path) as con:
                    classification_review = con.execute(
                        'SELECT 1 FROM mail_classification_reviews WHERE evidence_id=?', (evidence_id,)
                    ).fetchone() is not None
                proposal = None
                try:
                    if not classification_review:
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
                except ClassificationRejected:
                    save_review(self.service.store, evidence_id, self.model_version,
                        MutationContext('unclassified:' + evidence_id, 'system', 'outlook_mail', identity[2]))
                    proposal = None
                    classification_review = True
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
                        not historical
                        and direction == 'inbound'
                        and candidate_context_complete
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
                if proposal is not None and proposal.proposed_application_id and candidate_context_complete and (created['proposal']['status'] in {'accepted', 'auto_applied'} or (not historical and direction == 'inbound' and decision.disposition is ProposalDisposition.AUTO_APPLY)):
                    self.service.lifecycle.link_mail({'observation_id': observed['observation_id'], 'application_id': proposal.proposed_application_id, 'confidence': proposal.confidence, 'source':'lifecycle_proposal'}, MutationContext('proposal-link:' + observed['observation_id'] + ':' + proposal.proposed_application_id, 'system', 'outlook_sync'))
                elif proposal is None and not classification_review and not linked and recruiting:
                    self.service.lifecycle.propose_discovery({'observation_id': observed['observation_id']}, MutationContext('discovery:' + observed['observation_id'], 'system', 'outlook_sync'))
                mark(
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
                mark(
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
                mark(
                    *identity, "failed", str(exc), query_version=query_version
                )
                failed += 1
            except Exception as exc:
                mark(
                    *identity, "failed", str(exc), query_version=query_version
                )
                failed += 1
            if heartbeat is not None and not heartbeat():
                raise RuntimeError("Outlook mail worker lease was lost")
        return ProcessingResult(processed, ignored, failed, proposed, auto_applied)

    def process_replay(self, replay_id: str, *, limit: int = 50, heartbeat: Optional[Callable[[], bool]] = None):
        """Analyze a bounded explicit snapshot without changing normal activation."""
        from .contracts import ConflictError, utc_now
        from .db import connect
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('replay limit must be between 1 and 100')
        replay = self.service.lifecycle.get_mail_replay(replay_id)
        if replay['status'] in {'completed','cancelled'}:
            return replay
        if replay['status'] == 'failed':
            raise ConflictError('failed replay requires an explicit user retry')
        with connect(self.state.db_path) as con:
            rows = [dict(row) for row in con.execute(
                'SELECT rowid AS stage_rowid,* FROM outlook_message_stage WHERE account_id=? AND query_version=? AND rowid>? AND rowid<=? AND removed=0 AND julianday(received_at)>=julianday(?) AND julianday(received_at)<julianday(?) ORDER BY rowid LIMIT ?',
                (replay['account_id'], replay['query_version'], replay['after_stage_rowid'], replay['max_stage_rowid'], replay['since_at'], replay['until_at'], limit+1))]
        for row in rows[:limit]:
            if heartbeat is not None and not heartbeat():
                raise RuntimeError('historical replay worker lease was lost')
            current = self.service.lifecycle.get_mail_replay(replay_id)
            if current['status'] == 'cancelled':
                return current
            try:
                result = self.process_pending(1, query_version=replay['query_version'], _replay_rows=[row], heartbeat=heartbeat)
                if result.failed:
                    raise RuntimeError('historical replay failed; checkpoint retained for retry')
            except Exception as exc:
                # Respect existing worker retries and inference deferrals. Losing
                # a lease cannot grant this worker authority to fail the replay.
                if heartbeat is not None and not heartbeat():
                    raise
                if isinstance(exc, GraphHttpError) and exc.decision.retryable:
                    raise
                if isinstance(exc, InferenceTransportError) and (exc.retryable or getattr(exc, 'defer_without_attempt', False)):
                    raise
                with connect(self.state.db_path) as con:
                    con.execute("UPDATE lifecycle_mail_replays SET status='failed',last_error=?,failure_count=failure_count+1,updated_at=? WHERE replay_id=? AND after_stage_rowid=? AND status IN ('pending','running')", ('mail processing failed; resolve the connector issue before retrying',utc_now(),replay_id,replay['after_stage_rowid']))
                raise
            with connect(self.state.db_path) as con:
                changed = con.execute("UPDATE lifecycle_mail_replays SET after_stage_rowid=?,processed=processed+1,status='running',updated_at=? WHERE replay_id=? AND after_stage_rowid=? AND status IN ('pending','running')", (row['stage_rowid'], utc_now(), replay_id, replay['after_stage_rowid']))
                if changed.rowcount != 1:
                    raise ConflictError('historical replay checkpoint changed concurrently')
            replay['after_stage_rowid'] = row['stage_rowid']
        if len(rows) <= limit:
            with connect(self.state.db_path) as con:
                con.execute("UPDATE lifecycle_mail_replays SET status='completed',updated_at=? WHERE replay_id=? AND status IN ('pending','running')", (utc_now(), replay_id))
        return self.service.lifecycle.get_mail_replay(replay_id)
