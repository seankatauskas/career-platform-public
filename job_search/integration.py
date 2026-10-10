"""Concrete, capability-limited composition for shortlist, Hermes, and alerts."""

from __future__ import annotations

import sqlite3
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .availability import AvailabilityPlanner
from .contracts import (
    ACTION_APPROVAL_TTL_SECONDS,
    ActionKind,
    ActionProposalInput,
    ContractError,
    MutationContext,
    parse_utc,
    payload_sha256,
)
from .hermes import HermesSources
from .notifications import (
    DurableNotificationPublisher,
    NotificationIntent,
)
from .preference import PreferenceGateway
from .resume_integration import ResumeLabGateway
from .service import JobSearchLedger
from .worker import OutboxContext, TaskContext


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ContractError("integration clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class LocalJobCatalog:
    """Bounded, read-only text search over normalized jobs."""

    _RESULT_FIELDS = (
        "ats",
        "id",
        "company",
        "title",
        "department",
        "team",
        "employmentType",
        "location",
        "isRemote",
        "workplaceType",
        "publishedAt",
        "posted_at",
        "source_updated_at",
        "jobUrl",
    )
    _SEARCH_FIELDS = ("title", "company", "description", "location", "department", "team")
    _DETAIL_FIELDS = (
        "ats",
        "company",
        "id",
        "title",
        "department",
        "team",
        "employmentType",
        "location",
        "isRemote",
        "workplaceType",
        "publishedAt",
        "posted_at",
        "source_updated_at",
        "jobUrl",
        "description",
        "description_html",
        "matched",
        "first_seen",
        "last_seen",
        "closed_at",
    )
    MAX_DESCRIPTION_CHARS = 256_000

    def posting_dates(self, identities: Sequence[tuple[str, str]]) -> dict:
        from .collection.history import DATE_FIELDS
        return self._posting_metadata(identities, DATE_FIELDS)

    def posting_summaries(self, identities: Sequence[tuple[str, str]]) -> dict:
        """Batch lightweight job details for cards, without reading descriptions."""
        from .collection.history import DATE_FIELDS
        return self._posting_metadata(identities, (*DATE_FIELDS, "title", "company",
            "location", "employmentType", "workplaceType", "isRemote", "jobUrl"))

    def _posting_metadata(self, identities: Sequence[tuple[str, str]], requested_fields: Sequence[str]) -> dict:
        if not self.jobs_db.is_file():
            return {}
        result = {}
        with closing(sqlite3.connect(self.jobs_db.as_uri() + '?mode=ro', uri=True, timeout=10)) as con:
            con.row_factory = sqlite3.Row
            columns = {row[1] for row in con.execute('PRAGMA table_info(jobs)')}
            if not {'ats', 'id'}.issubset(columns):
                return result
            fields = ['ats', 'id', *(key for key in requested_fields if key in columns)]
            for start in range(0, len(identities), 200):
                batch = identities[start:start + 200]
                # Tuple IN scans the entire catalog on production SQLite versions.
                # Explicit composite-key predicates use the existing primary-key index.
                predicates = ' OR '.join('(ats=? AND id=?)' for _ in batch)
                for row in con.execute(f"SELECT {','.join(fields)} FROM jobs WHERE {predicates}",
                                       tuple(value for pair in batch for value in pair)):
                    result[(row['ats'], row['id'])] = dict(row)
        return result

    def posting_history(self, ats: str, job_id: str, before: int | None = None) -> dict:
        from .collection.history import read_history
        job = self.posting_dates([(ats, job_id)]).get((ats, job_id))
        if job is None:
            return {'available': False, 'events': [], 'next_before': None,
                    'history_note': 'This posting is not available in the local catalog.'}
        with sqlite3.connect(self.jobs_db.as_uri() + '?mode=ro', uri=True, timeout=10) as con:
            con.row_factory = sqlite3.Row
            return {'available': True, **read_history(con, job, before)}

    def __init__(self, jobs_db: Path) -> None:
        self.jobs_db = Path(jobs_db).expanduser().resolve()

    @staticmethod
    def _like(value: str) -> str:
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace(
            "_", "\\_"
        ) + "%"

    def search_jobs(self, query: str, limit: int) -> Sequence[Mapping[str, Any]]:
        query = " ".join(str(query).split())
        if not query or len(query) > 200:
            raise ContractError("job query must be 1 to 200 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ContractError("job search limit must be between 1 and 50")
        if not self.jobs_db.is_file():
            raise ContractError("normalized jobs database is unavailable")
        terms = query.casefold().split()[:8]
        uri = self.jobs_db.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=10) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            if not table:
                raise ContractError("normalized jobs database is not initialized")
            columns = {str(row[1]) for row in con.execute("PRAGMA table_info(jobs)")}
            required = {"ats", "id", "title", "company"}
            if not required.issubset(columns):
                raise ContractError("normalized jobs schema is incompatible")
            searchable = [name for name in self._SEARCH_FIELDS if name in columns]
            expressions = [
                "COALESCE(" + name + ",'')" for name in searchable
            ]
            haystack = "lower(" + " || ' ' || ".join(expressions) + ")"
            clauses = [f"{haystack} LIKE ? ESCAPE '\\'" for _ in terms]
            parameters: list[Any] = [self._like(term) for term in terms]
            if "closed_at" in columns:
                clauses.append("closed_at IS NULL")
            selected = [name for name in self._RESULT_FIELDS if name in columns]
            order = (
                "datetime(publishedAt) DESC, company COLLATE NOCASE, title COLLATE NOCASE"
                if "publishedAt" in columns
                else "company COLLATE NOCASE, title COLLATE NOCASE"
            )
            rows = con.execute(
                "SELECT " + ",".join(selected) + " FROM jobs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY " + order + " LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def review_candidates(self, subject: str, body: str, limit: int = 20) -> Sequence[Mapping[str, Any]]:
        """Find email-related postings, including jobs with no application record.

        Only company identities and metadata for matching boards are read. Closed
        postings remain eligible because their recruiting emails can arrive later.
        Posting recency breaks equal relevance ties, never establishes identity.
        This lookup never creates an application or infers a submission.
        """
        from .mail.context import CandidateApplication
        from .mail.identity import review_supported_candidates, unique_supported_application
        from .mail.matching import rank_mail_candidates
        from .mail.rules import _searchable

        if not isinstance(subject, str) or not isinstance(body, str):
            raise ContractError("review subject and body must be strings")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ContractError("review candidate limit must be between 1 and 50")
        if not self.jobs_db.is_file():
            return ()
        message = subject + "\n" + body
        haystack = _searchable(message)
        if not haystack:
            return ()
        words = haystack.split()
        word_set = set(words)

        def contains(value):
            value = _searchable(str(value or ""))
            return len(value) >= 4 and bool(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", haystack))

        def score(title, job_id):
            role = _searchable(str(title or ""))
            role_words = set(role.split()) - {"the", "a", "of", "and", "i", "ii", "iii"}
            return (100 if contains(job_id) else 0) + (30 if contains(title) else
                10 * len(word_set & role_words) / len(role_words) if role_words else 0)

        with closing(sqlite3.connect(self.jobs_db.as_uri() + "?mode=ro", uri=True, timeout=10)) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
            if not {"ats", "id", "company", "title"}.issubset(columns):
                return ()
            selected = [field for field in (*self._RESULT_FIELDS, "closed_at") if field in columns]
            projection = ",".join(selected)
            # Parse timestamps in SQLite so offsets sort correctly and malformed
            # dates can fall back to publication/collection time. Older catalogs
            # without dates retain deterministic identity ordering.
            dates = [f"julianday({field})" for field in ("posted_at", "publishedAt", "first_seen")
                     if field in columns]
            recency = "COALESCE(" + ",".join([*dates, "0"]) + ")" if dates else "0"
            projection += f",{recency} AS review_recency"
            boards = [(row['ats'], row['company']) for row in con.execute(
                "SELECT DISTINCT ats,company FROM jobs WHERE company IS NOT NULL")]
            # Match compact board slugs against whole words, without normalizing
            # the full email once for every company in the catalog.
            names = {_searchable(str(company)).replace(" ", "") for _, company in boards}
            names = {name for name in names if len(name) >= 3}
            maximum = max(map(len, names), default=0)
            mentioned = set()
            for start in range(len(words)):
                value = ""
                for end in range(start, len(words)):
                    value += words[end]
                    if len(value) > maximum:
                        break
                    if value in names:
                        mentioned.add(value)
            matches = [(ats, company) for ats, company in boards
                       if _searchable(str(company)).replace(" ", "") in mentioned]
            rows = {}
            truncated = False
            candidate_cap = max(100, limit * 5)
            con.create_function("review_role_score", 2, score)
            for ats, company in matches:
                found = con.execute(
                    f"SELECT {projection} FROM jobs WHERE ats=? AND company=? "
                    "ORDER BY review_role_score(title,id) DESC,review_recency DESC,id LIMIT ?",
                    (ats, company, candidate_cap + 1),
                ).fetchall()
                truncated = truncated or len(found) > candidate_cap
                for row in found[:candidate_cap]:
                    rows[(row['ats'], row['id'])] = dict(row)
            # A posting ID can identify the employer even when the email omits
            # its name. Composite-key lookups use the existing (ats,id) index.
            identifiers = sorted({variant for token in re.findall(r"[\w][\w-]{3,255}", message)
                                  for variant in (token, token.lower())})
            for ats in sorted({ats for ats, _ in boards}):
                for start in range(0, len(identifiers), 200):
                    batch = identifiers[start:start + 200]
                    placeholders = ",".join("?" for _ in batch)
                    for row in con.execute(f"SELECT {projection} FROM jobs WHERE ats=? AND id IN ({placeholders})", (ats, *batch)):
                        rows[(row['ats'], row['id'])] = dict(row)

        ranked = rank_mail_candidates([
            {**row, "application_id": f"catalog:{row['ats']}:{row['id']}",
             "job_id": row['id'], "employer_snapshot": row['company'],
             "company_slug_snapshot": row['company'], "title_snapshot": row['title']}
            for row in sorted(rows.values(), key=lambda row: (-row['review_recency'], row['ats'], row['id']))
        ], message)
        candidates = [CandidateApplication(
            row['application_id'], row['ats'], row['id'], row['company'], row['title'], row['company'],
        ) for row in ranked]
        supported = review_supported_candidates(candidates, subject, body)
        eligible = {candidate.application_id for candidate in supported}
        unique_id = unique_supported_application(supported, subject, body)
        ranked = [row for row in ranked if row['application_id'] in eligible]
        result = []
        for row in ranked:
            identity = row['application_id']
            unique = identity == unique_id
            reason = row['mail_match_context']
            if (row is ranked[0] and len(ranked) > 1
                    and row['mail_match_score'] == ranked[1]['mail_match_score']
                    and row['review_recency'] > ranked[1]['review_recency']):
                reason += '; most recent posting among equally matching roles'
            result.append({**{field: row[field] for field in selected},
                "match_reason": reason, "match_score": row['mail_match_score'],
                "match_confidence": "high" if unique and not truncated else "medium",
                "match_unique": unique and not truncated,
                "candidates_truncated": truncated or len(supported) > limit})
            if len(result) == limit:
                break
        return tuple(result)

    def get_job(self, ats: str, job_id: str) -> Mapping[str, Any]:
        """Read one exact normalized posting, including its bounded description.

        Only known columns are projected, so a derived-table or provider migration
        cannot silently expose arbitrary data.  Oversized descriptions fail closed
        instead of being truncated and then scored as though they were complete.
        """

        normalized_ats = str(ats or "").strip().lower()
        normalized_id = str(job_id or "").strip()
        if (
            not normalized_ats
            or len(normalized_ats) > 32
            or not normalized_id
            or len(normalized_id) > 256
        ):
            raise ContractError("job identity is invalid")
        if not self.jobs_db.is_file():
            raise ContractError("normalized jobs database is unavailable")
        uri = self.jobs_db.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            if not table:
                raise ContractError("normalized jobs database is not initialized")
            columns = {str(row[1]) for row in con.execute("PRAGMA table_info(jobs)")}
            required = {"ats", "id", "title", "company", "description"}
            if not required.issubset(columns):
                raise ContractError("normalized jobs schema is incompatible")
            selected = [name for name in self._DETAIL_FIELDS if name in columns]
            row = con.execute(
                "SELECT " + ",".join(selected) + " FROM jobs WHERE ats=? AND id=?",
                (normalized_ats, normalized_id),
            ).fetchone()
        if row is None:
            raise ContractError("job was not found")
        result = dict(row)
        description = str(result.get("description") or "")
        if len(description) > self.MAX_DESCRIPTION_CHARS:
            raise ContractError("job description exceeds the resume-analysis limit")
        # Preview markup is optional and must not change resume-analysis limits.
        if len(str(result.get("description_html") or "")) > self.MAX_DESCRIPTION_CHARS * 4:
            result.pop("description_html", None)
        result["description"] = description
        return result


class ConfiguredShortlistSource:
    """Apply runtime defaults and exclude jobs already in the application ledger."""

    def __init__(
        self,
        gateway: PreferenceGateway,
        ledger: JobSearchLedger,
        defaults: Mapping[str, Any],
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.defaults = dict(defaults)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def list_shortlist(self, options: Mapping[str, Any]) -> Mapping[str, Any]:
        effective = dict(self.defaults)
        effective.update(
            {name: options[name] for name in ("limit", "policy") if name in options}
        )
        current = self.now()
        if current.tzinfo is None:
            raise ContractError("shortlist clock must be timezone-aware")
        bucket = int(current.astimezone(timezone.utc).timestamp()) // (5 * 60)
        key = "hermes-shortlist:" + payload_sha256(
            {"bucket": bucket, "options": effective}
        )
        return self.gateway.create_shortlist(
            effective,
            idempotency_key=key,
            actor="hermes",
            excluded_job_keys=self.ledger.application_keys(),
        )


class LedgerProposalSource:
    """Turn Hermes suggestions into exact, expiring dashboard approvals."""

    def __init__(
        self,
        ledger: JobSearchLedger,
        *,
        account_id: str,
        availability: Optional[AvailabilityPlanner] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.ledger = ledger
        self.account_id = account_id
        self.availability = availability
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _expiry(self) -> str:
        return _utc_text(self.now() + timedelta(seconds=ACTION_APPROVAL_TTL_SECONDS))

    def propose_reply(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence_id = str(request["evidence_id"])
        application_id = str(request["application_id"])
        evidence = self.ledger.resolve_reply_evidence(
            evidence_id, application_id, self.account_id
        )
        return self.ledger.create_action_proposal(
            ActionProposalInput(
                ActionKind.OUTLOOK_REPLY_DRAFT,
                application_id,
                self.account_id,
                {
                    "message_id": str(evidence["immutable_message_id"]),
                    "body": str(request["body"]),
                },
                self._expiry(),
            ),
            MutationContext(
                str(request["idempotency_key"]),
                "hermes",
                "hermes_proposal",
                evidence_id,
            ),
        )

    def propose_interview_slots(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if self.availability is None:
            raise ContractError("Outlook calendar availability is not configured")
        application_id = str(request["application_id"])
        self.ledger.get_application_timeline(application_id)
        slots = self.availability.propose_slots(
            now=self.now(), duration_minutes=int(request["duration_minutes"])
        )
        if not slots:
            raise ContractError("no interview slot is available in the configured horizon")
        selected = slots[0]
        return self.ledger.create_action_proposal(
            ActionProposalInput(
                ActionKind.CALENDAR_TENTATIVE_HOLD,
                application_id,
                self.account_id,
                {"starts_at": selected.starts_at, "ends_at": selected.ends_at},
                self._expiry(),
            ),
            MutationContext(
                str(request["idempotency_key"]),
                "hermes",
                "hermes_proposal",
                application_id,
            ),
        )


class ShortlistNotificationEvaluator:
    """Create at most one actionable shortlist alert for an opportunity workflow."""

    def __init__(
        self,
        gateway: PreferenceGateway,
        ledger: JobSearchLedger,
        publisher: DurableNotificationPublisher,
        *,
        options: Mapping[str, Any],
        enabled: bool,
        minimum_jobs: int = 1,
        cooldown_minutes: int = 240,
        first_seen_since: Optional[str] = None,
        now: Optional[Callable[[], datetime]] = None,
        application_gateway=None,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.applications = application_gateway or ledger
        self.publisher = publisher
        self.options = dict(options)
        self.enabled = bool(enabled)
        self.minimum_jobs = minimum_jobs
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.first_seen_since = parse_utc(first_seen_since) if first_seen_since else None
        self.now = now or (lambda: datetime.now(timezone.utc))

    def __call__(
        self, payload: Mapping[str, Any], context: TaskContext
    ) -> Mapping[str, Any]:
        del payload
        if not self.enabled:
            return {"evaluated": True, "suppressed": True, "reason": "disabled"}
        current = self.now()
        if current.tzinfo is None:
            raise ContractError("notification clock must be timezone-aware")
        workflow = context.workflow_id or context.work_id
        state = self.ledger.get_shortlist_notification_state()
        latest = state.get("latest_created_at")
        if (
            workflow not in tuple(state.get("workflow_ids") or ())
            and self.cooldown > timedelta(0)
            and latest
            and parse_utc(str(latest))
            >= current.astimezone(timezone.utc) - self.cooldown
        ):
            return {
                "evaluated": True,
                "suppressed": True,
                "reason": "cooldown",
                "jobs": 0,
            }
        result = self.gateway.preview_shortlist(
            dict(self.options),
            excluded_job_keys=self.applications.application_keys(),
        )
        recommendations = list(result.get("recommendations") or ())
        keyed = []
        current_keys: set[tuple[str, str]] = set()
        for item in recommendations:
            if self.first_seen_since is not None:
                # The initial catalog is a baseline, not a backlog of phone alerts.
                # Unverifiable observation dates cannot count as newly found jobs.
                try:
                    observed = parse_utc(str(item.get("first_seen") or "").replace("+00:00", "Z"))
                except (ValueError, TypeError):
                    continue
                if observed < self.first_seen_since:
                    continue
            key = (
                str(item.get("ats") or "").strip().lower(),
                str(item.get("id") or "").strip(),
            )
            if not all(key) or key in current_keys:
                continue
            current_keys.add(key)
            keyed.append((item, key))
        exposed = self.gateway.notification_exposed_job_keys(
            tuple(state.get("workflow_ids") or ()), current_keys
        )
        new_recommendations = [item for item, key in keyed if key not in exposed]
        if len(new_recommendations) < self.minimum_jobs:
            return {
                "evaluated": True,
                "suppressed": True,
                "reason": "below_minimum",
                "jobs": len(new_recommendations),
            }
        lines = []
        for item in new_recommendations[:5]:
            lines.append(
                f"{item.get('title') or 'Role'} — {item.get('company') or 'Unknown company'}"
            )
        body = "\n".join(lines)
        if len(new_recommendations) > len(lines):
            body += f"\n…and {len(new_recommendations) - len(lines)} more matched."
        body += "\nOpen the dashboard to review current recommendations."
        published = self.publisher.publish(
            NotificationIntent(
                "shortlist.ready",
                "shortlist:" + workflow,
                f"{len(new_recommendations)} new high-signal jobs are ready",
                body,
                context={"workflow": workflow},
            )
        )
        recorded = None
        if not published.get("suppressed"):
            notification_result = dict(result)
            notification_result["recommendations"] = new_recommendations
            recorded = self.gateway.record_notification_shortlist(
                notification_result,
                workflow_id=workflow,
            )
        return {
            "evaluated": True,
            "suppressed": bool(published.get("suppressed")),
            "jobs": len(new_recommendations),
            "notification_created": bool(published.get("created")),
            "session_id": recorded.get("session_id") if recorded else None,
        }


class ApplicationEventNotificationHandler:
    """Convert lifecycle-event outbox records into allowlisted phone alerts."""

    _TOPICS = {
        "recruiter_contact": "mail.recruiter_update",
        "assessment_requested": "attention.required",
        "interview_requested": "application.interview_requested",
        "interview_scheduled": "application.interview_scheduled",
        "offer_received": "application.offer_received",
        "rejection_received": "application.rejection_received",
    }

    def __init__(self, ledger: JobSearchLedger, publisher: DurableNotificationPublisher) -> None:
        self.ledger = ledger
        self.publisher = publisher

    def __call__(
        self, payload: Mapping[str, Any], context: OutboxContext
    ) -> Mapping[str, Any]:
        event_type = str(payload.get("event_type") or "")
        topic = self._TOPICS.get(event_type)
        if topic is None:
            return {"suppressed": True, "reason": "non_signal_event"}
        application_id = str(payload.get("application_id") or "")
        timeline = self.ledger.get_application_timeline(application_id)
        application = timeline["application"]
        employer = str(application.get("employer_snapshot") or "Employer")
        title = str(application.get("title_snapshot") or "role")
        labels = {
            "recruiter_contact": "Recruiter update",
            "assessment_requested": "Assessment needs attention",
            "interview_requested": "Interview requested",
            "interview_scheduled": "Interview scheduled",
            "offer_received": "Offer received",
            "rejection_received": "Application update",
        }
        return self.publisher.publish(
            NotificationIntent(
                topic,
                context.source_event_id,
                labels[event_type],
                f"{employer} — {title}. Open the dashboard for the verified timeline.",
                application_id,
                {"application_id": application_id, "event_id": context.source_event_id},
            )
        )


class ReminderNotificationHandler:
    """Move due chief-of-staff reminders into the durable notification outbox."""

    def __init__(
        self,
        ledger: JobSearchLedger,
        publisher: DurableNotificationPublisher,
        *,
        now: Optional[Callable[[], datetime]] = None,
        limit: int = 25,
    ) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("reminder notification limit must be between 1 and 100")
        self.ledger = ledger
        self.publisher = publisher
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.limit = limit

    def __call__(
        self, payload: Mapping[str, Any], context: TaskContext
    ) -> Mapping[str, Any]:
        del payload
        current = self.now()
        stamp = _utc_text(current)
        queued = suppressed = 0
        due = [
            item
            for item in self.ledger.list_reminders(("scheduled",), self.limit)
            if parse_utc(str(item["due_at"])) <= current.astimezone(timezone.utc)
        ]
        for reminder in due:
            reminder_id = str(reminder["reminder_id"])
            result = self.publisher.publish(
                NotificationIntent(
                    "reminder.due",
                    reminder_id,
                    "Job-search reminder",
                    str(reminder["note"]),
                    str(reminder["application_id"]),
                    {
                        "application_id": str(reminder["application_id"]),
                        "reminder_id": reminder_id,
                    },
                )
            )
            if result.get("suppressed"):
                suppressed += 1
                continue
            self.ledger.complete_reminder(
                reminder_id,
                MutationContext(
                    "reminder-queued:" + reminder_id,
                    "system",
                    "notification_dispatch",
                    context.work_id,
                ),
            )
            queued += 1
            if not context.heartbeat():
                raise RuntimeError("worker lease was lost while dispatching reminders")
        return {"due": len(due), "queued": queued, "suppressed": suppressed, "at": stamp}


def make_hermes_sources(
    *,
    jobs: LocalJobCatalog,
    shortlist: ConfiguredShortlistSource,
    ledger: JobSearchLedger,
    mail: Any,
    proposals: LedgerProposalSource,
    resume: Optional[ResumeLabGateway] = None,
    readiness: Optional[Any] = None,
    curated: Optional[Any] = None,
    reviews: Optional[Any] = None,
) -> HermesSources:
    """The single explicit assembly point for the chief-of-staff capabilities."""

    return HermesSources(jobs, shortlist, ledger, mail, proposals, resume, readiness=readiness, curated=curated, reviews=reviews)


__all__ = [
    "ApplicationEventNotificationHandler",
    "ConfiguredShortlistSource",
    "LedgerProposalSource",
    "LocalJobCatalog",
    "ReminderNotificationHandler",
    "ShortlistNotificationEvaluator",
    "make_hermes_sources",
]
