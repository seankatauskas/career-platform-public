"""Dependency-free, loopback-only dashboard for the deterministic job-search core."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .application_documents import application_documents, application_document_content
from .browser_tracking import BrowserTracking
from .application_answers import application_snapshots, save_snapshot, exact_json, MAX_SNAPSHOT_BYTES
from .job_preview import render_description
from .autofill import (
    AutofillBroker,
    AutofillProfile,
    EncryptedAutofillVault,
    load_profile,
    validate_extension_origin,
)
from .contracts import (
    ConflictError,
    ContractError,
    JobSnapshot,
    MutationContext,
    RecommendationProvenance,
    canonical_json,
    payload_sha256,
    utc_now,
    validate_identifier,
)
from .dashboard_access import DashboardAccess
from .preference import PreferenceGateway, PreferencePaths
from .resume_integration import (
    COMPARISON_KINDS,
    MAX_STANDARD_RESUMES,
    ExactJobCatalog,
    ResumeArtifactContent,
    ResumeLabGateway,
    ResumeLabUnavailable,
)
from .service import JobSearchLedger
from .scheduler import has_outlook_config, has_real_scraper_contact


MAX_REQUEST_BYTES = 64 * 1024
MAX_CAREER_DOCUMENT_BYTES = 5 * 1024 * 1024
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_COOKIE = "job_search_session"
WEB_ROOT = Path(__file__).with_name("web")


@dataclass(frozen=True)
class DashboardSettings:
    resume_mode: str = "tailored"
    timezone: str = "America/Chicago"
    ats_refresh_hours: int = 4
    outlook_configured: bool = False
    job_scraper_contact_configured: bool = False
    mail_folder: str = "inbox"


def resume_submission_snapshot(
    resume_lab: Optional[ResumeLabGateway],
    application_id: str,
    decision: str,
) -> Mapping[str, Any]:
    """Bind a submission to a selected real artifact or an explicit opt-out."""

    if decision not in {"selected", "not_tracked", "automatic"}:
        raise ContractError("resume_decision must be selected or not_tracked")
    selection = None
    if resume_lab is not None:
        response = resume_lab.get_selection(application_id)
        if not isinstance(response, Mapping):
            raise ContractError("resume selection response is invalid")
        selection = response.get("selection")
        if selection is not None and not isinstance(selection, Mapping):
            raise ContractError("resume selection response is invalid")
    effective = (
        "selected"
        if decision == "automatic" and selection is not None
        else "not_tracked"
        if decision == "automatic"
        else decision
    )
    if effective == "not_tracked":
        return {"resume": {"decision": "not_tracked"}}
    if selection is None:
        raise ConflictError(
            "select a resume or explicitly submit with no tracked resume"
        )
    artifact_id = str(selection.get("artifact_id") or "")
    evaluation_id = str(selection.get("evaluation_id") or "")
    comparison_kind = str(selection.get("comparison_kind") or "")
    validate_identifier(artifact_id, "artifact_id")
    validate_identifier(evaluation_id, "evaluation_id")
    if comparison_kind not in {"standard", "grounded_rewrite"}:
        raise ContractError("selected resume kind is invalid")
    snapshot: Dict[str, Any] = {
        "decision": "selected",
        "artifact_id": artifact_id,
        "evaluation_id": evaluation_id,
        "comparison_kind": comparison_kind,
    }
    for key in ("standard_id", "standard_version_id", "name", "source_mode", "profile_revision_id", "composition_id", "template_version"):
        value = selection.get(key)
        if isinstance(value, str) and value:
            snapshot[key] = value[:500]
    return {"resume": snapshot}


@dataclass
class _Session:
    session_id: str
    csrf_token: str
    touched_at: float
    audience: str = "local"


class SessionManager:
    """Small process-local session registry; no token is persisted in the ledger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, _Session] = {}

    def resolve(self, cookie_header: str, audience: str = "local") -> Tuple[_Session, bool]:
        supplied = ""
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or "")
            morsel = cookie.get(SESSION_COOKIE)
            supplied = morsel.value if morsel else ""
        except Exception:
            supplied = ""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            current = self._sessions.get(supplied)
            if current and current.audience == audience:
                current.touched_at = now
                return current, False
            current = _Session(secrets.token_hex(24), secrets.token_hex(32), now, audience)
            self._sessions[current.session_id] = current
            return current, True

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, value in self._sessions.items()
            if now - value.touched_at > SESSION_TTL_SECONDS
        ]
        for key in expired:
            self._sessions.pop(key, None)
        while len(self._sessions) >= 128:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].touched_at)
            self._sessions.pop(oldest, None)


class DashboardController:
    """Transforms dashboard requests into the two narrow service boundaries."""

    def __init__(
        self,
        ledger: JobSearchLedger,
        preferences: PreferenceGateway,
        settings: DashboardSettings = DashboardSettings(),
        autofill: Optional[AutofillBroker] = None,
        jobs: Optional[ExactJobCatalog] = None,
        resume_lab: Optional[ResumeLabGateway] = None,
        readiness: Optional[Callable[[], Mapping[str, Any]]] = None,
        notification_recovery: Optional[Any] = None,
        mail_source: Optional[Any] = None,
        demo_mode: bool = False,
        automation_config: Any = None,
        cost_snapshot_path: Optional[Path] = None,
        review_classifier_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        from .curated import CuratedShortlists
        self.curated = CuratedShortlists(ledger.store.db_path, jobs)
        self.ledger = ledger
        self.preferences = preferences
        self.settings = settings
        self.jobs = jobs
        self.resume_lab = resume_lab
        self._readiness = readiness
        self.notification_recovery = notification_recovery
        self.mail_source = mail_source
        self.review_classifier_factory = review_classifier_factory
        self._review_analysis_lock = threading.Lock()
        self.demo_mode = demo_mode
        self.automation_config = automation_config
        self.cost_snapshot_path = cost_snapshot_path
        from .job_reviews.service import JobReviews
        from .job_reviews.context import profile_context
        from .scanning import scan_status
        self.job_reviews = JobReviews(ledger.store.db_path, jobs, lambda: profile_context(self.resume_lab),
            collection_provider=(lambda: scan_status(automation_config)) if automation_config else None)

        self.autofill = autofill or AutofillBroker(
            ledger,
            AutofillProfile.empty(),
            submission_context=lambda application_id, decision: resume_submission_snapshot(
                self.resume_lab, application_id, decision
            ),
        )
        self.browser_tracking = BrowserTracking(ledger, jobs, self.autofill, resume_lab)
        self._shortlist_lock = threading.Lock()
        self._shortlists: Dict[str, Mapping[str, Any]] = {}

    def with_posting_dates(self, rows: Sequence[Mapping[str, Any]]) -> list[dict]:
        identities = [(str(row.get('ats', '')), str(row.get('job_id') or row.get('id', ''))) for row in rows]
        provider = getattr(self.jobs, 'posting_summaries', None) or getattr(self.jobs, 'posting_dates', None)
        dates = provider(identities) if provider else {}
        return [{**row, 'job_posting': dates.get(identity, {key: row.get(key) for key in
                 ('ats', 'publishedAt', 'posted_at', 'source_updated_at', 'first_seen')})}
                for row, identity in zip(rows, identities)]

    def application_job_history(self, application_id: str, before: int | None = None) -> Mapping[str, Any]:
        app = self.ledger.get_application_timeline(application_id)['application']
        if not hasattr(self.jobs, 'posting_history'):
            return {'available': False, 'events': [], 'next_before': None,
                    'history_note': 'Posting history is unavailable for this catalog.'}
        return self.jobs.posting_history(app['ats'], app['job_id'], before)

    def with_recent_company_applications(self, rows: Sequence[Mapping[str, Any]]) -> list[dict]:
        """Refresh company history without changing saved recommendation snapshots."""
        if not rows:
            return []

        def company_key(value: Any) -> str:
            # Exact normalized aliases work across ATS without merging similar names.
            return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())

        latest = {}
        for order, application in enumerate(self.ledger.recent_company_applications()):
            metadata = {key: application[key] for key in ('application_id', 'applied_at', 'window_days')}
            employer = company_key(application['employer_snapshot'])
            slug = company_key(application['company_slug_snapshot'])
            # Collector-backed applications copy the board slug into both fields.
            # That is not evidence of a shared employer name across ATS providers.
            if employer and employer != slug:
                latest.setdefault(('employer', employer), (order, metadata))
            if slug:
                latest.setdefault(('board', company_key(application['ats']), slug), (order, metadata))
        enriched = []
        for row in rows:
            displayed = {**row, **(row.get('job_posting') or {})}
            company = company_key(displayed.get('company'))
            matches = [latest[key] for key in (
                ('employer', company), ('board', company_key(displayed.get('ats')), company),
            ) if key in latest]
            metadata = min(matches, key=lambda match: match[0])[1] if matches else None
            enriched.append({**row, 'recent_company_application': metadata})
        return enriched

    def conversation_message(self, application_id: str, observation_id: str) -> Mapping[str, Any]:
        observation = self.ledger.lifecycle.get_mail_observation(observation_id)
        if application_id not in observation['application_ids']:
            raise ContractError('message is not linked to this application')
        result = {'subject':observation['subject'], 'sender':observation['sender'], 'excerpt':'', 'available':False}
        if observation.get('evidence_id'):
            result['excerpt'] = self.ledger.get_sanitized_evidence(observation['evidence_id'])['excerpt']
        if self.mail_source is not None and observation.get('archive_id'):
            try:
                content = self.mail_source.get_mail_message(observation['archive_id'])
                result.update(subject=content.get('subject',''), excerpt=content.get('excerpt',''), available=True)
            except Exception:
                pass  # An inaccessible archive cannot disclose key/provider errors.
        return result

    def application_workspace(self, application_id: str) -> Mapping[str, Any]:
        timeline = self.ledger.get_application_timeline(application_id)
        messages = []
        for evidence in self.ledger.list_application_mail(application_id):
            message = {name: evidence[name] for name in ("evidence_id", "sender", "received_at", "excerpt")}
            message.update(available=False, subject="Recruiter message", reason="archive_unavailable")
            if self.mail_source is not None and evidence.get("archive_id"):
                try:
                    content = self.mail_source.get_mail_message(evidence["archive_id"])
                    message.update(subject=content.get("subject", ""), excerpt=content.get("excerpt", ""), available=True, reason="")
                except Exception:
                    # A locked archive must not hide the application or leak key errors.
                    pass
            messages.append(message)
        resume: Mapping[str, Any] = {"available": False, "reason": "resume_not_configured"}
        if self.resume_lab is not None:
            try:
                resume = self.resume_lab.get_selection(application_id)
            except Exception:
                resume = {"available": False, "reason": "resume_unavailable"}
        return {
            **timeline, "briefing": self.ledger.lifecycle.get_application_briefing(application_id), "resume": resume, "messages": messages,
            "documents": application_documents(self.ledger, self.resume_lab, application_id),
            "application": self.with_posting_dates([timeline['application']])[0],
            "job_history": self.application_job_history(application_id),
            "browser_tracking": self.browser_tracking.application_status(application_id),
            "browser_observations": self.browser_tracking.evidence(application_id),
            "answer_snapshots": application_snapshots(self.ledger.store.db_path, application_id),
            "interviews": list(self.ledger.list_interview_schedules(application_id=application_id)),
            "actions": [item for item in self.ledger.list_actions() if item.get("application_id") == application_id],
            "reviews": [item for item in self.ledger.list_attention_items() if item.get("kind") != "action_proposal" and (item.get("application_id") == application_id or application_id in item.get("candidate_application_ids", []))],
        }

    @staticmethod
    def _integer(
        options: Mapping[str, Any], name: str, default: int, minimum: int, maximum: int
    ) -> int:
        raw = options.get(name, default)
        if isinstance(raw, bool):
            raise ContractError(f"{name} must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ContractError(f"{name} must be between {minimum} and {maximum}")
        return value

    @classmethod
    def shortlist_options(cls, supplied: Mapping[str, Any]) -> Dict[str, Any]:
        limit = cls._integer(supplied, "limit", 20, 1, 100)
        days = cls._integer(supplied, "days", 30, 1, 3650)
        max_company = cls._integer(
            supplied, "max_per_company", min(2, limit), 1, limit
        )
        max_title = cls._integer(supplied, "max_per_title", min(2, limit), 1, limit)
        remote = supplied.get("remote_only", False)
        if not isinstance(remote, bool):
            raise ContractError("remote_only must be a boolean")
        policy = str(supplied.get("policy", "champion")).strip().lower()
        if policy not in {"champion", "selective", "broad", "compare"}:
            raise ContractError("unknown recommendation policy")
        raw_floor = supplied.get("salary_floor")
        try:
            salary_floor = None if raw_floor in {None, ""} else float(raw_floor)
        except (TypeError, ValueError) as exc:
            raise ContractError("salary_floor must be a non-negative number") from exc
        if salary_floor is not None and (salary_floor < 0 or salary_floor > 10_000_000):
            raise ContractError("salary_floor must be a non-negative annual amount")
        return {
            "limit": limit,
            "days": days,
            "salary_floor": salary_floor,
            "remote_only": remote,
            "max_per_company": max_company,
            "max_per_title": max_title,
            "policy": policy,
        }

    def create_shortlist(
        self,
        browser_session_id: str,
        supplied_options: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        options = self.shortlist_options(supplied_options)
        result = self.preferences.create_shortlist(
            options,
            idempotency_key=idempotency_key,
            actor="dashboard",
            excluded_job_keys=self.ledger.application_keys(),
        )
        result = {**result, 'recommendations': self.with_recent_company_applications(
            self.with_posting_dates(result.get('recommendations', [])))}
        if self.automation_config is not None:
            from .ranking.refresh import inspect_policies
            config = self.automation_config
            result = {**result, 'data_status': inspect_policies(config.preference_db, config.proxy_db, config.jobs_db)}
        with self._shortlist_lock:
            self._shortlists[browser_session_id] = result
        return result

    def cached_shortlist(self, browser_session_id: str) -> Mapping[str, Any]:
        with self._shortlist_lock:
            result = self._shortlists.get(
                browser_session_id,
                {"recommendations": [], "model": {"ready": False}, "session_id": None},
            )
        return {**result, 'recommendations': self.with_recent_company_applications(result.get('recommendations', []))}

    def curated_list(self, list_id: str) -> Mapping[str, Any]:
        result = self.curated.get(list_id)
        rows = self.with_recent_company_applications(self.with_posting_dates(result['recommendations']))
        ordinals = self.job_reviews.publication_ordinals(list_id)
        summaries = (self.job_reviews.publication_summaries(list_id)
                     if hasattr(self.job_reviews, 'publication_summaries') else {})
        for row in rows:
            row['closed_at'] = row.get('job_posting', {}).get('closed_at', row.get('closed_at'))
            row['review_ordinal'] = ordinals.get((row['ats'], row['id']))
            summary = summaries.get((row['ats'], row['id']))
            if summary:
                row['review_summary'] = summary
        return {**result, 'recommendations': rows, 'review': self.job_reviews.publication_summary(list_id)}

    def curated_job(self, list_id: str, ats: str, job_id: str) -> Mapping[str, Any]:
        self.curated.item(list_id, ats, job_id)
        if self.jobs is None:
            return {'available': False}
        try:
            return {'available': True, 'job': self.jobs.get_job(ats, job_id)}
        except ContractError:
            return {'available': False}

    def prepare_curated(self, list_id: str, ats: str, job_id: str, key: str) -> Mapping[str, Any]:
        job = self.curated.item(list_id, ats, job_id)
        if not job.get('application_id'):
            if self.jobs is None:
                raise ContractError('job catalog is unavailable')
            current = self.jobs.get_job(ats, job_id)
            if current.get('closed_at'):
                raise ContractError('this posting is closed')
        snapshot = JobSnapshot(ats=ats, job_id=job_id, family_id=job.get('family_id') or '',
            title=job['title'], employer=job['company'], company_slug=job['company'], job_url=job['jobUrl'])
        started = self.ledger.start_application(snapshot,
            RecommendationProvenance(session_id=list_id, policy_id='curated', rank=job['rank']),
            MutationContext(key, 'user', 'dashboard', source_ref=list_id))
        return self._prepare_started_application(started, key)

    def job_preview(self, ats: str, job_id: str, application_id: str = "") -> Mapping[str, Any]:
        """Read current catalog text without starting an application or model work."""
        job = {}
        if application_id:
            app = self._application(application_id)
            ats, job_id = str(app.get("ats") or ""), str(app.get("job_id") or "")
            job = {"ats": ats, "id": job_id, "title": app["title_snapshot"],
                   "company": app["employer_snapshot"], "jobUrl": app.get("job_url_snapshot")}
        if not ats or len(ats) > 32 or not job_id or len(job_id) > 256:
            raise ContractError("job identity is invalid")
        available = False
        reason = "The saved job description is unavailable."
        if self.jobs is not None:
            try:
                job = dict(self.jobs.get_job(ats, job_id))
                available = True
            except ContractError as exc:
                reason = str(exc)
        rich = job.pop("description_html", "")
        plain = job.pop("description", "")
        html = render_description(rich) or render_description(plain)
        return {
            "job": job, "available": available, "description_html": html,
            "formatting_note": (
                "Saved plain text. Original formatting has not been collected for this role yet."
                if plain and not rich else ""
            ),
            "description_note": "" if html else "No saved job description is available for this role.",
            "catalog_note": "" if available else reason,
        }

    def shortlist_job(self, browser_session_id: str, ats: str, job_id: str) -> Mapping[str, Any]:
        rows = self.cached_shortlist(browser_session_id).get("recommendations", [])
        if not any(row.get("ats") == ats and str(row.get("id")) == job_id for row in rows):
            raise ContractError("refresh the shortlist before opening this role")
        if self.jobs is None:
            return {"available": False, "reason": "job_catalog_unavailable"}
        return {"available": True, "job": self.jobs.get_job(ats, job_id)}

    def start_application(
        self,
        browser_session_id: str,
        shortlist_session_id: str,
        impression_id: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        result = self.cached_shortlist(browser_session_id)
        if str(result.get("session_id") or "") != shortlist_session_id:
            raise ContractError("shortlist session is no longer active; refresh it")
        recommendation = next(
            (
                row
                for row in result.get("recommendations", [])
                if int(row.get("impression_id") or 0) == impression_id
            ),
            None,
        )
        if recommendation is None:
            raise ContractError("recommendation impression was not found")
        snapshot = JobSnapshot(
            ats=str(recommendation.get("ats") or "").strip().lower(),
            job_id=str(recommendation.get("id") or "").strip(),
            family_id=str(recommendation.get("family_id") or "").strip(),
            title=str(recommendation.get("title") or "").strip(),
            employer=str(recommendation.get("company") or "").strip(),
            company_slug=str(recommendation.get("company") or "").strip(),
            job_url=str(recommendation.get("jobUrl") or "").strip(),
        )
        provenance = RecommendationProvenance(
            session_id=shortlist_session_id,
            impression_id=impression_id,
            model_run_id=str(recommendation.get("model_run_id") or ""),
            policy_id=str(recommendation.get("policy_id") or ""),
            rank=(
                int(recommendation["rank"])
                if recommendation.get("rank") is not None
                else None
            ),
            semantic_score=(
                float(recommendation["semantic_score"])
                if recommendation.get("semantic_score") is not None
                else None
            ),
            ranking_score=(
                float(recommendation["ranking_score"])
                if recommendation.get("ranking_score") is not None
                else None
            ),
        )
        return self.ledger.start_application(
            snapshot,
            provenance,
            MutationContext(
                idempotency_key=idempotency_key,
                actor_kind="user",
                source_kind="dashboard",
                source_ref=browser_session_id,
            ),
        )

    def _resume_gateway(self) -> ResumeLabGateway:
        if self.resume_lab is None:
            raise ResumeLabUnavailable("resume laboratory is not configured")
        return self.resume_lab

    def _application(self, application_id: str) -> Mapping[str, Any]:
        validate_identifier(application_id, "application_id")
        try:
            value = self.ledger.get_application_timeline(application_id)
        except ContractError as exc:
            if str(exc) == "application not found":
                raise _DashboardNotFound("application was not found") from exc
            raise
        application = value.get("application") if isinstance(value, Mapping) else None
        if not isinstance(application, Mapping):
            raise _DashboardNotFound("application was not found")
        return application

    def _application_job(self, application_id: str) -> Mapping[str, Any]:
        application = self._application(application_id)
        if self.jobs is None:
            raise ResumeLabUnavailable("normalized job catalog is not configured")
        return self.jobs.get_job(
            str(application.get("ats") or ""),
            str(application.get("job_id") or ""),
        )

    def prepare_application(
        self,
        browser_session_id: str,
        shortlist_session_id: str,
        impression_id: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Start the immutable ledger lifecycle, then prepare its resume workspace."""

        started = self.start_application(
            browser_session_id,
            shortlist_session_id,
            impression_id,
            idempotency_key,
        )
        return self._prepare_started_application(started, idempotency_key)

    def _prepare_started_application(self, started: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        application = started.get("application", {})
        application_id = str(application.get("application_id") or "")
        job_url = str(application.get("job_url_snapshot") or "")
        if self.resume_lab is None:
            return {
                **started,
                "job_url": job_url,
                "resume_lab": {
                    "configured": False,
                    "detail": "Resume laboratory is not configured.",
                },
            }
        job = self._application_job(application_id)
        prepare = self.resume_lab.use_standard if self.settings.resume_mode == "standard" else self.resume_lab.prepare
        prepared = prepare(
            job,
            application_id=application_id,
            idempotency_key=idempotency_key,
        )
        return {
            **started,
            "job_url": job_url or str(job.get("jobUrl") or ""),
            "resume_lab": {"configured": True, **dict(prepared)},
        }

    def list_resume_standards(self, limit: int = MAX_STANDARD_RESUMES) -> Mapping[str, Any]:
        if self.resume_lab is None:
            return {"configured": False, "standards": []}
        return {
            "configured": True,
            **dict(self.resume_lab.list_standards(limit=limit)),
        }

    def prepare_existing_application(
        self,
        application_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Rank every active standard before preparing an existing application."""

        self._application(application_id)
        gateway = self._resume_gateway()
        prepare = gateway.use_standard if self.settings.resume_mode == "standard" else gateway.prepare
        return prepare(
            self._application_job(application_id),
            application_id=application_id,
            idempotency_key=idempotency_key,
        )

    def start_resume_run(
        self,
        application_id: str,
        standard_version_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        validate_identifier(standard_version_id, "standard_version_id")
        self._application(application_id)
        return self._resume_gateway().start_run(
            self._application_job(application_id),
            application_id=application_id,
            standard_version_id=standard_version_id,
            idempotency_key=idempotency_key,
        )

    def get_resume_run_result(self, run_id: str) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        return self._resume_gateway().get_run_result(run_id)

    def retry_resume_run(
        self,
        run_id: str,
        idempotency_key: str,
        reconciliation_acknowledged: bool = False,
    ) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        return self._resume_gateway().retry_run(
            run_id,
            idempotency_key=idempotency_key,
            reconciliation_acknowledged=reconciliation_acknowledged,
        )

    def approve_resume_run(
        self,
        run_id: str,
        comparison_kind: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        if comparison_kind not in COMPARISON_KINDS:
            raise ContractError("comparison_kind is invalid")
        return self._resume_gateway().approve_run(
            run_id,
            comparison_kind=comparison_kind,
            idempotency_key=idempotency_key,
        )

    def get_resume_selection(self, application_id: str) -> Mapping[str, Any]:
        self._application(application_id)
        return self._resume_gateway().get_selection(application_id)

    def get_resume_workspace(self, application_id: str) -> Mapping[str, Any]:
        """Reopen the latest immutable resume comparison for one application."""

        self._application(application_id)
        return self._resume_gateway().get_application_workspace(application_id)

    def select_resume(
        self,
        application_id: str,
        artifact_id: str,
        evaluation_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        self._application(application_id)
        validate_identifier(artifact_id, "artifact_id")
        validate_identifier(evaluation_id, "evaluation_id")
        return self._resume_gateway().select_resume(
            application_id,
            job=self._application_job(application_id),
            artifact_id=artifact_id,
            evaluation_id=evaluation_id,
            idempotency_key=idempotency_key,
        )

    def get_resume_artifact(self, artifact_id: str) -> ResumeArtifactContent:
        validate_identifier(artifact_id, "artifact_id")
        artifact = self._resume_gateway().get_artifact(artifact_id)
        if not isinstance(artifact, ResumeArtifactContent):
            raise ContractError("resume artifact response is invalid")
        return artifact

    def get_resume_source(self, artifact_id: str) -> ResumeArtifactContent:
        validate_identifier(artifact_id, "artifact_id")
        artifact = self._resume_gateway().get_artifact_source(artifact_id)
        if not isinstance(artifact, ResumeArtifactContent):
            raise ContractError("resume source response is invalid")
        return artifact

    def get_career_profile(self) -> Mapping[str, Any]:
        if self.resume_lab is None:
            return {"configured": False, "draft": None, "approved": None}
        return {"configured": True, **dict(self.resume_lab.get_career_profile())}

    def save_career_profile(self, body: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        content = body.get("content")
        expected = body.get("expected_revision_id")
        if not isinstance(content, Mapping):
            raise ContractError("career content must be an object")
        if expected is not None:
            validate_identifier(expected, "expected_revision_id")
        return self._resume_gateway().save_career_profile(
            content, expected_revision_id=expected, idempotency_key=idempotency_key
        )

    def approve_career_profile(self, revision_id: str, idempotency_key: str) -> Mapping[str, Any]:
        validate_identifier(revision_id, "revision_id")
        return self._resume_gateway().approve_career_profile(revision_id, idempotency_key=idempotency_key)

    def import_career_standard(self, version_id: str, idempotency_key: str) -> Mapping[str, Any]:
        validate_identifier(version_id, "standard_version_id")
        return self._resume_gateway().import_career_standard(version_id, idempotency_key=idempotency_key)

    def get_career_import(self, import_id: str) -> Mapping[str, Any]:
        validate_identifier(import_id, "import_id")
        return self._resume_gateway().get_career_import(import_id)

    def regenerate_career_run(self, run_id: str, body: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        choices = {}
        for name in ("pinned_fact_ids", "excluded_fact_ids"):
            values = body.get(name, [])
            if not isinstance(values, list) or len(values) > 2000:
                raise ContractError(f"{name} must be a bounded array")
            for value in values:
                validate_identifier(value, name)
            if len(set(values)) != len(values):
                raise ContractError(f"{name} contains duplicates")
            choices[name] = values
        if set(choices["pinned_fact_ids"]) & set(choices["excluded_fact_ids"]):
            raise ContractError("a fact cannot be both pinned and excluded")
        use_latest = body.get("use_latest_profile", False)
        if not isinstance(use_latest, bool):
            raise ContractError("use_latest_profile must be a boolean")
        return self._resume_gateway().regenerate_career_run(
            run_id, **choices, idempotency_key=idempotency_key, use_latest_profile=use_latest
        )

    def start_research_comparisons(self, run_id: str, idempotency_key: str) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        return self._resume_gateway().start_research_comparisons(run_id, idempotency_key=idempotency_key)

    def record_submission(
        self,
        browser_session_id: str,
        application_id: str,
        occurred_at: str,
        idempotency_key: str,
        resume_decision: str,
    ) -> Mapping[str, Any]:
        if resume_decision not in {"selected", "not_tracked"}:
            raise ContractError(
                "resume_decision must be selected or not_tracked"
            )
        return self.ledger.record_submission(
            application_id,
            occurred_at,
            MutationContext(
                idempotency_key=idempotency_key,
                actor_kind="user",
                source_kind="dashboard",
                source_ref=browser_session_id,
            ),
            payload_factory=lambda: resume_submission_snapshot(
                self.resume_lab, application_id, resume_decision
            ),
            request_payload={"resume_decision": resume_decision},
        )

    def settings_view(self) -> Mapping[str, Any]:
        return {**asdict(self.settings), **({"demo_mode": True} if self.demo_mode else {})}

    def notification_summaries(self, limit: int = 25) -> Sequence[Mapping[str, Any]]:
        notifications = []
        for item in self.ledger.list_notification_outbox(limit=limit):
            notifications.append(
                {
                    key: item.get(key)
                    for key in (
                        "notification_id",
                        "topic",
                        "policy_id",
                        "status",
                        "attempts",
                        "max_attempts",
                        "available_at",
                        "created_at",
                        "delivered_at",
                    )
                }
            )
        return notifications

    def reminder_summaries(self, limit: int = 25) -> Sequence[Mapping[str, Any]]:
        return [
            {
                key: item.get(key)
                for key in (
                    "reminder_id",
                    "application_id",
                    "note",
                    "due_at",
                    "status",
                    "created_at",
                    "cancelled_at",
                    "completed_at",
                )
            }
            for item in self.ledger.list_reminders(limit=limit)
        ]

    def readiness_view(self) -> Mapping[str, Any]:
        if self._readiness is not None:
            return self._readiness()
        from .readiness import readiness_report
        return readiness_report(self.ledger.store.db_path)

    def recovery_work(self) -> Sequence[Mapping[str, Any]]:
        from .recovery import RecoveryService
        return RecoveryService(self.ledger.store.db_path).list_work(limit=100)

    def retry_work(self, work_id: str, body: Mapping[str, Any], command_id: str) -> Mapping[str, Any]:
        from .recovery import RecoveryService
        revision = body.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ContractError("expected_revision must be a non-negative integer")
        return RecoveryService(self.ledger.store.db_path).retry(
            work_id, expected_revision=revision, command_id=command_id, actor_kind="user"
        )

    def reconcile_notification(self, notification_id: str, body: Mapping[str, Any], context: MutationContext) -> Mapping[str, Any]:
        if self.notification_recovery is None:
            raise ContractError("notification reconciliation is not configured")
        return self.notification_recovery.reconcile(
            notification_id, expected_attempts=body.get("expected_attempts"),
            expected_payload_sha256=body.get("expected_payload_sha256"),
            outcome=body.get("outcome"), context=context,
        )

    def automation_decision(self, body, command_id):
        if self.automation_config is None:
            raise ContractError("automation control is unavailable in this environment")
        from .activation import set_control
        try:
            return set_control(self.automation_config,body.get('capability'),body.get('enabled'),
                               expected_revision=body.get('expected_revision'),command_id=command_id)
        except ValueError as exc:
            raise ContractError(str(exc)) from exc

    def pipeline_view(self, *, policy_status=None) -> Mapping[str, Any]:
        from .scanning import scan_status
        from .ranking.progress import ranking_progress
        collection = scan_status(self.automation_config) if self.automation_config else None
        if collection is not None and self.demo_mode:
            collection = {**collection, "available": False, "reason": "Real scans are disabled in this local preview."}
        return {"collection": collection,
                "ranking": ranking_progress(self.automation_config, policy_status=policy_status) if self.automation_config else None}

    def ops_view(self) -> Mapping[str, Any]:
        from .activation import controls
        automation = controls(self.ledger.store.db_path) if self.automation_config else []
        health = self.ledger.system_health()
        delivery = self.notification_recovery.list_pending() if self.notification_recovery else {
            "items": [], "bridge_available": False, "reason_code": "not_configured", "truncated": False
        }
        readiness = self.readiness_view()
        return {
            "automation": automation,
            **self.pipeline_view(policy_status=readiness.get('ranking_policies')),
            "status": health["status"],
            "checked_at": health["checked_at"],
            "health": health,
            "readiness": readiness,
            "costs": self.cost_view(),
            "recovery": {"items": self.recovery_work()},
            "reminders": {
                "counts": health.get("reminders", {}).get("counts", {}),
                "items": self.reminder_summaries(),
            },
            "notifications": {
                "counts": health.get("notifications", {}).get("counts", {}),
                "items": self.notification_summaries(),
                "reconciliation": delivery["items"],
                "reconciliation_status": {key: value for key, value in delivery.items() if key != "items"},
            },
        }

    def cost_view(self) -> Mapping[str, Any]:
        from .cost_snapshot import read_cost_snapshot
        return read_cost_snapshot(self.cost_snapshot_path)

    def issue_autofill_handoff(
        self,
        browser_session_id: str,
        application_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        return self.autofill.issue(
            application_id, browser_session_id, idempotency_key
        )


STATIC_ROUTES = {
    "/assets/lifecycle-view.js": ("lifecycle-view.js", "text/javascript; charset=utf-8"),
    "/assets/applications-view.js": ("applications-view.js", "text/javascript; charset=utf-8"),
    "/assets/applications-view.css": ("applications-view.css", "text/css; charset=utf-8"),
    "/assets/shortlist-view.js": ("shortlist-view.js", "text/javascript; charset=utf-8"),
    "/assets/shortlist-view.css": ("shortlist-view.css", "text/css; charset=utf-8"),
    "/assets/review-view.js": ("review-view.js", "text/javascript; charset=utf-8"),
    "/assets/review-view.css": ("review-view.css", "text/css; charset=utf-8"),
    "/assets/settings-view.js": ("settings-view.js", "text/javascript; charset=utf-8"),
    "/assets/chief-view.js": ("chief-view.js", "text/javascript; charset=utf-8"),
    "/assets/settings-view.css": ("settings-view.css", "text/css; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/job-preview.js": ("job-preview.js", "text/javascript; charset=utf-8"),
    "/assets/console.js": ("console.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

APPLICATION_PATH = re.compile(r"^/api/v1/applications/([A-Za-z0-9._:-]+)$")
APPLICATION_JOB_HISTORY_PATH = re.compile(r"^/api/v1/applications/([A-Za-z0-9._:-]+)/job-history$")
APPLICATION_DOCUMENT_PATH = re.compile(r"^/api/v1/applications/([A-Za-z0-9._:-]+)/documents/resume$")
APPLICATION_WORKSPACE_PATH = re.compile(r"^/api/v1/applications/([A-Za-z0-9._:-]+)/workspace$")
SUBMISSION_PATH = re.compile(
    r"^/api/v1/applications/([A-Za-z0-9._:-]+)/submitted$"
)
PROPOSAL_DECISION_PATH = re.compile(
    r"^/api/v1/proposals/([A-Za-z0-9._:-]+)/decision$"
)
MAIL_ANALYSIS_PATH = re.compile(r"^/api/v1/mail-analyses/([A-Za-z0-9._:-]+)$")
MAIL_ANALYSIS_DECISIONS_PATH = re.compile(r"^/api/v1/mail-analyses/([A-Za-z0-9._:-]+)/decisions$")
TEMPORAL_PROPOSAL_DECISION_PATH = re.compile(
    r"^/api/v1/temporal-proposals/([A-Za-z0-9._:-]+)/decision$"
)
ACTION_PATH = re.compile(r"^/api/v1/actions/([A-Za-z0-9._:-]+)$")
ACTION_DECISION_PATH = re.compile(
    r"^/api/v1/actions/([A-Za-z0-9._:-]+)/decision$"
)
ACTION_RECONCILE_PATH = re.compile(
    r"^/api/v1/actions/([A-Za-z0-9._:-]+)/reconcile$"
)
REMINDER_CANCEL_PATH = re.compile(
    r"^/api/v1/reminders/([A-Za-z0-9._:-]+)/cancel$"
)
RESUME_RUN_RESULT_PATH = re.compile(
    r"^/api/v1/resume-lab/runs/([A-Za-z0-9._:-]+)/result$"
)
RESUME_APPLICATION_PREPARE_PATH = re.compile(
    r"^/api/v1/resume-lab/applications/([A-Za-z0-9._:-]+)/prepare$"
)
RESUME_RUN_RETRY_PATH = re.compile(
    r"^/api/v1/resume-lab/runs/([A-Za-z0-9._:-]+)/retry$"
)
RESUME_RUN_APPROVE_PATH = re.compile(
    r"^/api/v1/resume-lab/runs/([A-Za-z0-9._:-]+)/approve$"
)
RESUME_SELECTION_PATH = re.compile(
    r"^/api/v1/applications/([A-Za-z0-9._:-]+)/resume-selection$"
)
RESUME_WORKSPACE_PATH = re.compile(
    r"^/api/v1/applications/([A-Za-z0-9._:-]+)/resume-workspace$"
)
RESUME_STANDARD_DOCUMENT_PATH = re.compile(r"^/api/v1/resume-lab/standards/([A-Za-z0-9._:-]+)/document$")
RESUME_ARTIFACT_PATH = re.compile(
    r"^/api/v1/resume-lab/artifacts/([A-Za-z0-9._:-]+)$"
)
RESUME_SOURCE_PATH = re.compile(r"^/api/v1/resume-lab/artifacts/([A-Za-z0-9._:-]+)/source$")
CAREER_IMPORT_PATH = re.compile(r"^/api/v1/career-profile/imports/([A-Za-z0-9._:-]+)$")
CAREER_REGENERATE_PATH = re.compile(r"^/api/v1/resume-lab/runs/([A-Za-z0-9._:-]+)/regenerate$")
CAREER_RESEARCH_PATH = re.compile(r"^/api/v1/resume-lab/runs/([A-Za-z0-9._:-]+)/research$")
EXTENSION_POST_PATHS = frozenset(
    {
        "/api/v1/extension/enroll",
        "/api/v1/extension/resolve",
        "/api/v1/extension/observations",
        "/api/v1/extension/assignments",
        "/api/v1/extension/resume",
        "/api/v1/extension/capture",
        "/api/v1/extension/answers",
        "/api/v1/extension/status",
        "/api/v1/autofill/exchange",
        "/api/v1/autofill/capture",
        "/api/v1/autofill/submitted",
    }
)


def make_handler(
    controller: DashboardController, sessions: Optional[SessionManager] = None,
    *, https_origin: str = "", allowed_tailscale_login: str = "",
) -> type[BaseHTTPRequestHandler]:
    session_manager = sessions or SessionManager()
    access = DashboardAccess(https_origin, allowed_tailscale_login)

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "JobSearchDashboard/1"
        sys_version = ""

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        @property
        def _port(self) -> int:
            return int(self.server.server_address[1])

        def _valid_host(self) -> bool:
            # Do not infer authentication from arbitrary Forwarded/X-Forwarded
            # headers. Serve strips incoming identity headers before adding its
            # authenticated identity; host loopback is the trust boundary.
            self._secure_session = False
            self._session_audience = "local"
            if self.client_address[0] != "127.0.0.1":
                return False
            if len(self.headers.get_all("Host", [])) != 1:
                return False
            if len(self.headers.get_all("Origin", [])) > 1:
                return False
            host = str(self.headers.get("Host") or "").strip().lower()
            if access.https_origin and host == access.host:
                logins = self.headers.get_all("Tailscale-User-Login", [])
                if len(logins) != 1 or logins[0] != access.allowed_tailscale_login:
                    return False
                if self.headers.get_all("Forwarded", []):
                    return False
                for name, expected in (
                    ("X-Forwarded-Host", access.host),
                    ("X-Forwarded-Proto", "https"),
                ):
                    values = self.headers.get_all(name, [])
                    if values and values != [expected]:
                        return False
                self._secure_session = True
                self._session_audience = access.https_origin + "|" + logins[0]
                return True
            # A proxy request with a forged localhost Host must never fall back
            # to local-user trust (including requests from tagged tailnet nodes).
            if any(
                name.lower().startswith(("tailscale-", "x-forwarded-"))
                or name.lower() == "forwarded"
                for name in self.headers
            ):
                return False
            return host in {
                f"127.0.0.1:{self._port}",
                f"localhost:{self._port}",
            }

        def _valid_origin(self) -> bool:
            if len(self.headers.get_all("Origin", [])) != 1:
                return False
            origin = str(self.headers.get("Origin") or "").strip().lower()
            if self._secure_session:
                return origin == access.https_origin
            return origin in {
                f"http://127.0.0.1:{self._port}",
                f"http://localhost:{self._port}",
            }

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            self.send_header("Cache-Control", "no-store")
            cors_origin = getattr(self, "_cors_origin", "")
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            session: Optional[_Session] = None,
            new_session: bool = False,
            extra_headers: Optional[Mapping[str, str]] = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            if session is not None and new_session:
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={session.session_id}; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}"
                    + ("; Secure" if getattr(self, "_secure_session", False) else ""),
                )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(
            self,
            value: Any,
            status: int = HTTPStatus.OK,
            session: Optional[_Session] = None,
            new_session: bool = False,
            exact_text: bool = False,
        ) -> None:
            self._send(
                status,
                (exact_json(value) if exact_text else canonical_json(value)).encode("utf-8"),
                "application/json; charset=utf-8",
                session,
                new_session,
            )

        def _error(
            self,
            status: int,
            message: str,
            session: Optional[_Session] = None,
            new_session: bool = False,
        ) -> None:
            self._json(
                {"error": message}, status=status, session=session, new_session=new_session
            )

        def _read_json(self) -> Mapping[str, Any]:
            if self.headers.get("Transfer-Encoding"):
                raise ContractError("streaming request bodies are not supported")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ContractError("Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ContractError("invalid Content-Length") from exc
            maximum = MAX_SNAPSHOT_BYTES + 16384 if urlsplit(self.path).path == '/api/v1/extension/answers' else MAX_REQUEST_BYTES
            if length < 0 or length > maximum:
                raise _RequestTooLarge
            content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                raise ContractError("Content-Type must be application/json")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContractError("request body must be valid UTF-8 JSON") from exc
            if not isinstance(value, dict):
                raise ContractError("request body must be a JSON object")
            return value

        def _read_career_document(self) -> tuple[bytes, str, str]:
            if self.headers.get("Transfer-Encoding"):
                raise ContractError("streaming request bodies are not supported")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise ContractError("valid Content-Length is required") from exc
            if length > MAX_CAREER_DOCUMENT_BYTES:
                raise _RequestTooLarge
            if length <= 0:
                raise ContractError("career document is empty")
            filename = unquote(str(self.headers.get("X-File-Name") or ""))
            if not filename or len(filename) > 200 or any(c in filename for c in ("/", "\\", "\x00", "\r", "\n")):
                raise ContractError("career document filename is invalid")
            content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {
                "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text/plain", "application/json",
            }:
                raise ContractError("import a PDF, DOCX, text, or exported career JSON document")
            content = self.rfile.read(length)
            if len(content) != length:
                raise ContractError("career document upload is incomplete")
            return content, filename, content_type

        def _idempotency(self, body: Mapping[str, Any]) -> str:
            header = str(self.headers.get("Idempotency-Key") or "").strip()
            embedded = str(body.get("idempotency_key") or "").strip()
            if header and embedded and header != embedded:
                raise ConflictError("idempotency header and body do not match")
            value = header or embedded
            validate_identifier(value, "idempotency_key")
            return value

        def _mutation_context(
            self, key: str, session: _Session, source: str = "dashboard"
        ) -> MutationContext:
            return MutationContext(key, "user", source, session.session_id)

        def _handle_exception(
            self, exc: Exception, session: _Session, new_session: bool
        ) -> None:
            if isinstance(exc, _RequestTooLarge):
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large", session, new_session)
            elif isinstance(exc, ConflictError):
                self._error(HTTPStatus.CONFLICT, str(exc), session, new_session)
            elif hasattr(exc, "http_status") and isinstance(
                getattr(exc, "http_status"), int
            ):
                status = int(getattr(exc, "http_status"))
                self._error(
                    status if 400 <= status < 600 else 400,
                    str(exc),
                    session,
                    new_session,
                )
            elif isinstance(exc, ContractError):
                self._error(HTTPStatus.BAD_REQUEST, str(exc), session, new_session)
            elif hasattr(exc, "status") and isinstance(getattr(exc, "status"), int):
                status = int(getattr(exc, "status"))
                self._error(status if 400 <= status < 600 else 400, str(exc), session, new_session)
            else:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error", session, new_session)

        def _handle_extension_exception(self, exc: Exception) -> None:
            if isinstance(exc, _RequestTooLarge):
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            elif isinstance(exc, ConflictError):
                self._error(HTTPStatus.CONFLICT, str(exc))
            elif isinstance(exc, ContractError):
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            else:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

        def do_OPTIONS(self) -> None:
            if not self._valid_host():
                self._error(HTTPStatus.BAD_REQUEST, "invalid Host header")
                return
            parsed_url = urlsplit(self.path)
            path = parsed_url.path
            if path not in EXTENSION_POST_PATHS:
                self._error(HTTPStatus.NOT_FOUND, "route not found")
                return
            try:
                self._cors_origin = validate_extension_origin(
                    str(self.headers.get("Origin") or "")
                )
            except ContractError:
                self._error(HTTPStatus.FORBIDDEN, "invalid extension Origin header")
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
            self.send_header("Access-Control-Max-Age", "600")
            if str(self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self._security_headers()
            self.end_headers()

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if not self._valid_host():
                self._error(HTTPStatus.BAD_REQUEST, "invalid Host header")
                return
            if self._secure_session and "Origin" in self.headers and not self._valid_origin():
                self._error(HTTPStatus.FORBIDDEN, "invalid Origin header")
                return
            session, new_session = session_manager.resolve(
                str(self.headers.get("Cookie") or ""), self._session_audience
            )
            parsed_url = urlsplit(self.path)
            path = parsed_url.path
            try:
                if path in STATIC_ROUTES:
                    filename, content_type = STATIC_ROUTES[path]
                    self._send(
                        HTTPStatus.OK,
                        (WEB_ROOT / filename).read_bytes(),
                        content_type,
                        session,
                        new_session,
                    )
                    return
                if path == "/api/v1/session":
                    self._json(
                        {"csrf_token": session.csrf_token, "api_version": "v1", "demo_mode": controller.demo_mode},
                        session=session,
                        new_session=new_session,
                    )
                    return
                if path == "/api/v1/job-reviews":
                    self._json(controller.job_reviews.call('list'), session=session, new_session=new_session)
                    return
                if path == "/api/v1/job-reviews/brief":
                    self._json(controller.job_reviews.call('brief'), session=session, new_session=new_session)
                    return
                if path.startswith("/api/v1/job-reviews/"):
                    rid = path[len("/api/v1/job-reviews/"):]
                    self._json(controller.job_reviews.call('status', {'review_id': rid}), session=session, new_session=new_session)
                    return
                if path == "/api/v1/curated-shortlists":
                    query = parse_qs(parsed_url.query)
                    before = query.get('before', [None])[0]
                    if before is not None and (not before.isdigit() or int(before) < 1):
                        raise ContractError('before must be a positive integer')
                    self._json(controller.curated.lists(int(before) if before else None), session=session, new_session=new_session)
                    return
                if path == "/api/v1/curated-shortlist":
                    query = parse_qs(parsed_url.query)
                    self._json(controller.curated_list(query.get('list_id', [''])[0]), session=session, new_session=new_session)
                    return
                if path == "/api/v1/curated-shortlist/job":
                    query = parse_qs(parsed_url.query)
                    self._json(controller.curated_job(query.get('list_id', [''])[0], query.get('ats', [''])[0], query.get('id', [''])[0]), session=session, new_session=new_session)
                    return
                if path == "/api/v1/jobs/preview":
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {"ats", "id", "application_id"} or any(len(values) != 1 for values in query.values()):
                        raise ContractError("invalid preview query")
                    self._json(controller.job_preview(query.get("ats", [""])[0], query.get("id", [""])[0],
                        query.get("application_id", [""])[0]), session=session, new_session=new_session)
                    return
                if path == "/api/v1/shortlist":
                    self._json(controller.cached_shortlist(session.session_id), session=session, new_session=new_session)
                    return
                if path == "/api/v1/career-profile":
                    self._json(controller.get_career_profile(), session=session, new_session=new_session)
                    return
                if path == "/api/v1/career-profile/export":
                    content = canonical_json(controller._resume_gateway().export_career_profile()).encode("utf-8")
                    self._send(HTTPStatus.OK, content, "application/json; charset=utf-8", session, new_session,
                        {"Content-Disposition": 'attachment; filename="career-profile.json"'})
                    return
                match = CAREER_IMPORT_PATH.fullmatch(path)
                if match:
                    self._json(controller.get_career_import(match.group(1)), session=session, new_session=new_session)
                    return
                if path == "/api/v1/resume-lab/standards":
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {"limit"} or len(query.get("limit", [])) > 1:
                        raise ContractError("unknown query parameter")
                    supplied_limit: Any = (
                        query["limit"][0] if query.get("limit") else MAX_STANDARD_RESUMES
                    )
                    limit = controller._integer(
                        {"limit": supplied_limit},
                        "limit",
                        MAX_STANDARD_RESUMES,
                        1,
                        MAX_STANDARD_RESUMES,
                    )
                    self._json(
                        controller.list_resume_standards(limit),
                        session=session,
                        new_session=new_session,
                    )
                    return
                match = RESUME_RUN_RESULT_PATH.fullmatch(path)
                if match:
                    self._json(
                        controller.get_resume_run_result(match.group(1)),
                        session=session,
                        new_session=new_session,
                    )
                    return
                match = RESUME_SELECTION_PATH.fullmatch(path)
                if match:
                    self._json(
                        controller.get_resume_selection(match.group(1)),
                        session=session,
                        new_session=new_session,
                    )
                    return
                match = RESUME_WORKSPACE_PATH.fullmatch(path)
                if match:
                    self._json(
                        controller.get_resume_workspace(match.group(1)),
                        session=session,
                        new_session=new_session,
                    )
                    return
                document_match = APPLICATION_DOCUMENT_PATH.fullmatch(path)
                if document_match:
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {"download"} or len(query.get("download", [])) > 1 or query.get("download", ["0"])[0] not in {"0", "1"}:
                        raise ContractError("invalid document query parameter")
                    artifact = application_document_content(controller.ledger, controller.resume_lab, document_match.group(1))
                    if not artifact.content or len(artifact.content) > 20 * 1024 * 1024:
                        raise ContractError("resume artifact content is invalid")
                    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", artifact.filename).strip(".-") or "recorded-resume.pdf"
                    disposition = "attachment" if query.get("download") == ["1"] else "inline"
                    headers = {"Content-Disposition": f'{disposition}; filename="{filename[:180]}"'}
                    self._send(HTTPStatus.OK, artifact.content, "application/pdf", session, new_session, headers)
                    return
                source_match = RESUME_SOURCE_PATH.fullmatch(path)
                standard_match = RESUME_STANDARD_DOCUMENT_PATH.fullmatch(path)
                match = RESUME_ARTIFACT_PATH.fullmatch(path) or source_match or standard_match
                if match:
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {"disposition"} or len(query.get("disposition", [])) > 1:
                        raise ContractError("unknown query parameter")
                    disposition = query.get("disposition", ["attachment"])[0]
                    if disposition not in {"attachment", "inline"}:
                        raise ContractError("artifact disposition is invalid")
                    artifact = (controller._resume_gateway().get_standard_document(match.group(1)) if standard_match
                        else controller.get_resume_source(match.group(1)) if source_match
                        else controller.get_resume_artifact(match.group(1)))
                    allowed_types = {
                        "application/pdf",
                        "application/json",
                        "text/plain",
                        "text/plain; charset=utf-8",
                    }
                    if artifact.content_type not in allowed_types:
                        raise ContractError("resume artifact content type is invalid")
                    if not artifact.content or len(artifact.content) > 20 * 1024 * 1024:
                        raise ContractError("resume artifact content is invalid")
                    filename = re.sub(
                        r"[^A-Za-z0-9._-]+", "-", artifact.filename
                    ).strip(".-") or f"resume-{artifact.artifact_id}"
                    headers = {
                        "Content-Disposition": f'{disposition}; filename="{filename[:180]}"'
                    }
                    if artifact.sha256 and re.fullmatch(
                        r"[a-fA-F0-9]{64}", artifact.sha256
                    ):
                        headers["X-Content-SHA256"] = artifact.sha256.lower()
                    self._send(
                        HTTPStatus.OK,
                        artifact.content,
                        artifact.content_type,
                        session,
                        new_session,
                        headers,
                    )
                    return
                if path.startswith('/api/v1/chief/'):
                    from .interactions.dashboard import read
                    self._json(read(controller.ledger,path[len('/api/v1/chief/'):],parse_qs(parsed_url.query,keep_blank_values=True)),session=session,new_session=new_session)
                    return
                history_match = re.fullmatch(r"/api/v1/lifecycle/history/(task|detail)/([A-Za-z0-9._:-]+)", path)
                if history_match:
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {"after_revision", "limit"} or any(len(v) != 1 for v in query.values()):
                        raise ContractError("invalid history query")
                    limit = controller._integer({"limit": query.get("limit", ["25"])[0]}, "limit", 25, 1, 100)
                    after = controller._integer({"after": query.get("after_revision", ["0"])[0]}, "after", 0, 0, 100000)
                    self._json(controller.ledger.lifecycle.get_record_history(history_match.group(1), history_match.group(2), limit=limit, after_revision=after), session=session, new_session=new_session)
                    return
                if path == "/api/v1/lifecycle/replays":
                    self._json({**controller.ledger.lifecycle.list_mail_replays(), "accounts": controller.ledger.lifecycle.list_mail_replay_accounts()}, session=session, new_session=new_session)
                    return
                if path == "/api/v1/lifecycle/reviews":
                    self._json({"items": controller.ledger.lifecycle.list_lifecycle_reviews()}, session=session, new_session=new_session)
                    return
                lifecycle_match = re.fullmatch(r"/api/v1/applications/([A-Za-z0-9._:-]+)/briefing", path)
                if lifecycle_match:
                    self._json(controller.ledger.lifecycle.get_application_briefing(lifecycle_match.group(1)), session=session, new_session=new_session)
                    return
                message_match = re.fullmatch(r"/api/v1/applications/([A-Za-z0-9._:-]+)/conversation/([A-Za-z0-9._:-]+)", path)
                if message_match:
                    self._json(controller.conversation_message(message_match.group(1),message_match.group(2)), session=session, new_session=new_session)
                    return
                conversation_match = re.fullmatch(r"/api/v1/applications/([A-Za-z0-9._:-]+)/conversation", path)
                if conversation_match:
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {"cursor", "limit"} or any(len(v) != 1 for v in query.values()):
                        raise ContractError("invalid conversation query")
                    limit = controller._integer({"limit": query.get("limit", ["25"])[0]}, "limit", 25, 1, 100)
                    self._json(controller.ledger.lifecycle.list_application_conversation(conversation_match.group(1), limit=limit, cursor=query.get("cursor", [None])[0]), session=session, new_session=new_session)
                    return
                if path == "/api/v1/applications":
                    self._json({"applications": [{**app, "browser_tracking": controller.browser_tracking.application_status(app["application_id"])} for app in controller.with_posting_dates(controller.ledger.list_applications())]}, session=session, new_session=new_session)
                    return
                if path == "/api/v1/shortlist/job":
                    query = parse_qs(urlsplit(self.path).query)
                    self._json(controller.shortlist_job(session.session_id, query.get("ats", [""])[0], query.get("id", [""])[0]), session=session, new_session=new_session)
                    return
                match = APPLICATION_JOB_HISTORY_PATH.fullmatch(path)
                if match:
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {'before'} or len(query.get('before', [])) > 1:
                        raise ContractError('unknown query parameter')
                    before = controller._integer({'before': query['before'][0]}, 'before', 1, 1, 2**63-1) if 'before' in query else None
                    self._json(controller.application_job_history(match.group(1), before), session=session, new_session=new_session)
                    return
                match = APPLICATION_WORKSPACE_PATH.fullmatch(path)
                if match:
                    self._json(controller.application_workspace(match.group(1)), session=session, new_session=new_session, exact_text=True)
                    return
                match = APPLICATION_PATH.fullmatch(path)
                if match:
                    self._json(controller.ledger.get_application_timeline(match.group(1)), session=session, new_session=new_session)
                    return
                if path == "/api/v1/attention":
                    from .review_recommendations import enrich_review_items
                    items = [*controller.ledger.list_attention_items(), *controller.ledger.lifecycle.list_lifecycle_reviews()]
                    self._json({"items": enrich_review_items(controller.ledger, controller.mail_source, items, controller.jobs,
                               can_analyze_archives=controller.review_classifier_factory is not None)},
                               session=session, new_session=new_session)
                    return
                if path == '/api/v1/mail-review/applications':
                    from .mail.review import MailReviewService
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {'search', 'after'} or any(len(values) != 1 for values in query.values()):
                        raise ContractError('invalid application search query')
                    self._json(MailReviewService(controller.ledger).applications(
                        query.get('search', [''])[0], 50, query.get('after', [''])[0]), session=session, new_session=new_session)
                    return
                if path == "/api/v1/attention/message":
                    from .review_messages import review_message
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {'kind', 'id', 'account_id', 'folder_ref', 'query_version'} or any(len(values) != 1 for values in query.values()):
                        raise ContractError('invalid review message query')
                    self._json(review_message(controller.ledger, controller.mail_source,
                        {key: values[0] for key, values in query.items()}), session=session, new_session=new_session, exact_text=True)
                    return
                if path == "/api/v1/mail-analyses":
                    query = parse_qs(parsed_url.query, keep_blank_values=True)
                    if set(query) - {"history", "limit"} or any(len(v) != 1 for v in query.values()):
                        raise ContractError("invalid mail analysis query")
                    history = query.get("history", ["false"])[0]
                    if history not in ("true", "false"):
                        raise ContractError("history must be true or false")
                    limit = controller._integer({"limit": query.get("limit", ["100"])[0]}, "limit", 100, 1, 100)
                    self._json({"analyses": controller.ledger.mail_understanding.list_reviews(history=history == "true", limit=limit)}, session=session, new_session=new_session, exact_text=True)
                    return
                match = MAIL_ANALYSIS_PATH.fullmatch(path)
                if match:
                    if parsed_url.query:
                        raise ContractError("mail analysis detail does not accept query parameters")
                    self._json(controller.ledger.mail_understanding.get(match.group(1)), session=session, new_session=new_session, exact_text=True)
                    return
                if path == "/api/v1/interviews":
                    self._json(
                        {"applications": controller.ledger.list_applications(("interviewing", "offer"))},
                        session=session,
                        new_session=new_session,
                    )
                    return
                if path == "/api/v1/actions":
                    self._json({"actions": controller.ledger.list_actions()}, session=session, new_session=new_session)
                    return
                match = ACTION_PATH.fullmatch(path)
                if match:
                    self._json(controller.ledger.get_action(match.group(1)), session=session, new_session=new_session)
                    return
                if path == "/api/v1/health":
                    self._json(controller.ledger.system_health(), session=session, new_session=new_session)
                    return
                if path == "/api/v1/ops/readiness":
                    self._json(controller.readiness_view(), session=session, new_session=new_session)
                    return
                if path == "/api/v1/ops/costs":
                    self._json(controller.cost_view(), session=session, new_session=new_session)
                    return
                if path == "/api/v1/ops/pipeline":
                    self._json(controller.pipeline_view(), session=session, new_session=new_session)
                    return
                if path == "/api/v1/ops/work":
                    self._json({"items": controller.recovery_work()}, session=session, new_session=new_session)
                    return
                if path == "/api/v1/ops":
                    self._json(controller.ops_view(), session=session, new_session=new_session)
                    return
                if path == "/api/v1/reminders":
                    self._json(
                        {"reminders": controller.reminder_summaries(100)},
                        session=session,
                        new_session=new_session,
                    )
                    return
                if path == "/api/v1/notifications":
                    self._json(
                        {"notifications": controller.notification_summaries(100)},
                        session=session,
                        new_session=new_session,
                    )
                    return
                if path == "/api/v1/browser/devices":
                    self._json({"devices": controller.browser_tracking.devices()}, session=session, new_session=new_session)
                    return
                if path == "/api/v1/settings":
                    self._json(controller.settings_view(), session=session, new_session=new_session)
                    return
                self._error(HTTPStatus.NOT_FOUND, "route not found", session, new_session)
            except Exception as exc:
                self._handle_exception(exc, session, new_session)

        def do_POST(self) -> None:
            if not self._valid_host():
                self._error(HTTPStatus.BAD_REQUEST, "invalid Host header")
                return
            path = urlsplit(self.path).path
            if path in EXTENSION_POST_PATHS:
                try:
                    self._cors_origin = validate_extension_origin(
                        str(self.headers.get("Origin") or "")
                    )
                except ContractError:
                    self._error(HTTPStatus.FORBIDDEN, "invalid extension Origin header")
                    return
                try:
                    body = self._read_json()
                    if path.startswith("/api/v1/extension/"):
                        tracker = controller.browser_tracking
                        if path == "/api/v1/extension/enroll":
                            result = tracker.enroll(body.get("pairing_code"), self._cors_origin, self._session_audience)
                        else:
                            device = tracker.authenticate(body.get("device_token"), self._cors_origin, self._session_audience)
                            if path.endswith("/resolve"):
                                result = tracker.resolve(body.get("page_url"))
                            elif path.endswith("/observations"):
                                result = tracker.observe(device, body)
                            elif path.endswith("/assignments"):
                                result = tracker.assignments(body.get("page_url"), body.get("fields"))
                            elif path.endswith("/resume"):
                                result = tracker.resume_attachment(body.get("page_url"))
                            elif path.endswith("/capture"):
                                result = tracker.stage_capture(device, body)
                            elif path.endswith("/answers"):
                                result = save_snapshot(tracker.path, device, body)
                            else:
                                tracker.maintain_captures()
                                result = tracker.attempt_status(device, body.get("attempt_id"))
                    elif path == "/api/v1/autofill/exchange":
                        fields = body.get("fields")
                        if not isinstance(fields, list):
                            raise ContractError("fields must be an array")
                        result = controller.autofill.exchange(
                            str(body.get("pairing_code") or ""),
                            self._cors_origin,
                            str(body.get("ats") or ""),
                            str(body.get("page_url") or ""),
                            fields,
                        )
                    elif path == "/api/v1/autofill/capture":
                        answers = body.get("answers")
                        if not isinstance(answers, list):
                            raise ContractError("answers must be an array")
                        result = controller.autofill.stage_capture(
                            str(body.get("submission_token") or ""),
                            self._cors_origin,
                            str(body.get("ats") or ""),
                            str(body.get("page_url") or ""),
                            answers,
                        )
                    else:
                        result = controller.autofill.mark_submitted(
                            str(body.get("submission_token") or ""),
                            self._cors_origin,
                            str(body.get("ats") or ""),
                            str(body.get("page_url") or ""),
                            self._idempotency(body),
                            str(body.get("resume_decision") or ""),
                        )
                    self._json(result)
                except Exception as exc:
                    self._handle_extension_exception(exc)
                return
            session, new_session = session_manager.resolve(
                str(self.headers.get("Cookie") or ""), self._session_audience
            )
            if not self._valid_origin():
                self._error(HTTPStatus.FORBIDDEN, "invalid Origin header", session, new_session)
                return
            supplied_csrf = str(self.headers.get("X-CSRF-Token") or "")
            if not secrets.compare_digest(supplied_csrf, session.csrf_token):
                self._error(HTTPStatus.FORBIDDEN, "invalid CSRF token", session, new_session)
                return
            try:
                if path == "/api/v1/career-profile/imports":
                    idempotency_key = self._idempotency({})
                    content, filename, content_type = self._read_career_document()
                    result = controller._resume_gateway().import_career_document(
                        content, filename=filename, content_type=content_type, idempotency_key=idempotency_key
                    )
                    self._json(result, status=HTTPStatus.ACCEPTED, session=session, new_session=new_session)
                    return
                body = self._read_json()
                if path in ('/api/v1/mail-review/preview', '/api/v1/mail-review/resolve'):
                    from .mail.review import MailReviewService
                    review = MailReviewService(controller.ledger)
                    allowed = {'decisions'} if path.endswith('/preview') else {'decisions', 'preview_hash', 'idempotency_key'}
                    if set(body) - allowed or 'decisions' not in body:
                        raise ContractError('invalid mail review request')
                    if path.endswith('/preview'):
                        result = review.preview(body['decisions'])
                    else:
                        result = review.apply(body['decisions'], body.get('preview_hash'),
                            self._mutation_context(self._idempotency(body), session, 'dashboard_mail_resolution'))
                    self._json(result, session=session, new_session=new_session, exact_text=True)
                    return
                match = MAIL_ANALYSIS_DECISIONS_PATH.fullmatch(path)
                if match:
                    if set(body) != {"revision", "decisions"} or not isinstance(body["revision"], str) or not body["revision"] or not isinstance(body["decisions"], list):
                        raise ContractError("mail decisions require revision and decisions")
                    command_id = self._idempotency({}) if self.headers.get("Idempotency-Key") else "mail-review:" + payload_sha256({"analysis_id": match.group(1), **body})
                    result = controller.ledger.mail_understanding.decide(match.group(1), body["revision"], body["decisions"], self._mutation_context(command_id, session, "dashboard_mail_review"))
                    self._json(result, session=session, new_session=new_session, exact_text=True)
                    return
                if path.startswith("/api/v1/job-reviews/"):
                    action = path[len("/api/v1/job-reviews/"):]
                    self._json(controller.job_reviews.call(action, body), session=session, new_session=new_session)
                    return
                idempotency_key = self._idempotency(body)
                if path.startswith('/api/v1/chief/'):
                    from .interactions.dashboard import mutate
                    result = mutate(controller.ledger,path[len('/api/v1/chief/'):],body,self._mutation_context(idempotency_key,session,'dashboard_chief'))
                    self._json(result,session=session,new_session=new_session)
                    return
                if path.startswith("/api/v1/lifecycle/"):
                    from .lifecycle.dashboard import mutate
                    if path == '/api/v1/lifecycle/discoveries/decide' and body.get('selected_job') is not None:
                        from .review_messages import review_message
                        from .review_recommendations import review_job_snapshot
                        if set(body) - {'idempotency_key', 'discovery_id', 'decision', 'selected_job'} or body.get('decision') != 'link_job':
                            raise ContractError('invalid catalog discovery decision')
                        content = review_message(controller.ledger, controller.mail_source,
                            {'kind': 'mail_discovery', 'id': body.get('discovery_id')})
                        snapshot = review_job_snapshot(controller.jobs, body['selected_job'], content)
                        result = controller.ledger.lifecycle.decide_discovery(
                            {key: value for key, value in body.items() if key != 'idempotency_key'},
                            self._mutation_context(idempotency_key, session, 'dashboard_lifecycle'),
                            review_job_snapshot=snapshot)
                        self._json(result, session=session, new_session=new_session)
                        return
                    result = mutate(controller.ledger.lifecycle, path[len("/api/v1/lifecycle/"):], body,
                        self._mutation_context(idempotency_key, session, "dashboard_lifecycle"))
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/ops/scan":
                    if controller.automation_config is None or controller.demo_mode:
                        raise ContractError("Job scanning is not configured in this dashboard.")
                    if set(body) - {"idempotency_key"}:
                        raise ContractError("Scan now uses the configured company boards and fixed collection settings.")
                    from .scanning import request_scan
                    result = request_scan(controller.automation_config, controller.ledger.store, idempotency_key)
                    self._json(result, status=HTTPStatus.ACCEPTED, session=session, new_session=new_session)
                    return
                if path == "/api/v1/browser/pairing":
                    self._json(controller.browser_tracking.issue_pairing(self._session_audience), session=session, new_session=new_session)
                    return
                if path == "/api/v1/browser/revoke":
                    self._json(controller.browser_tracking.revoke(str(body.get("device_id") or "")), session=session, new_session=new_session)
                    return
                if path == "/api/v1/automation":
                    self._json(controller.automation_decision(body,idempotency_key),session=session,new_session=new_session)
                    return
                notification_match = re.fullmatch(r"/api/v1/ops/notifications/([A-Za-z0-9._:-]+)/reconcile", path)
                if notification_match:
                    result = controller.reconcile_notification(
                        notification_match.group(1), body,
                        self._mutation_context(idempotency_key, session, "dashboard_notification_recovery"),
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                retry_match = re.fullmatch(r"/api/v1/ops/work/([A-Za-z0-9._:-]+)/retry", path)
                if retry_match:
                    self._json(controller.retry_work(retry_match.group(1), body, idempotency_key), session=session, new_session=new_session)
                    return
                if path == "/api/v1/career-profile":
                    self._json(controller.save_career_profile(body, idempotency_key), session=session, new_session=new_session)
                    return
                if path == "/api/v1/career-profile/approve":
                    self._json(controller.approve_career_profile(str(body.get("revision_id") or ""), idempotency_key), session=session, new_session=new_session)
                    return
                if path == "/api/v1/career-profile/import-standard":
                    self._json(controller.import_career_standard(str(body.get("standard_version_id") or ""), idempotency_key), session=session, new_session=new_session)
                    return
                match = CAREER_REGENERATE_PATH.fullmatch(path)
                if match:
                    self._json(controller.regenerate_career_run(match.group(1), body, idempotency_key), session=session, new_session=new_session)
                    return
                match = CAREER_RESEARCH_PATH.fullmatch(path)
                if match:
                    self._json(controller.start_research_comparisons(match.group(1), idempotency_key), session=session, new_session=new_session)
                    return
                if path == "/api/v1/shortlist":
                    result = controller.create_shortlist(
                        session.session_id,
                        body.get("options") if isinstance(body.get("options"), dict) else {},
                        idempotency_key,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/applications/start":
                    shortlist_session = str(body.get("session_id") or "")
                    validate_identifier(shortlist_session, "session_id")
                    try:
                        impression_id = int(body.get("impression_id"))
                    except (TypeError, ValueError) as exc:
                        raise ContractError("impression_id must be an integer") from exc
                    result = controller.start_application(
                        session.session_id,
                        shortlist_session,
                        impression_id,
                        idempotency_key,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/curated-shortlist/prepare":
                    result = controller.prepare_curated(str(body.get('list_id') or ''), str(body.get('ats') or ''), str(body.get('job_id') or ''), idempotency_key)
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/resume-lab/prepare":
                    shortlist_session = str(body.get("session_id") or "")
                    validate_identifier(shortlist_session, "session_id")
                    try:
                        impression_id = int(body.get("impression_id"))
                    except (TypeError, ValueError) as exc:
                        raise ContractError("impression_id must be an integer") from exc
                    result = controller.prepare_application(
                        session.session_id,
                        shortlist_session,
                        impression_id,
                        idempotency_key,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = RESUME_APPLICATION_PREPARE_PATH.fullmatch(path)
                if match:
                    result = controller.prepare_existing_application(
                        match.group(1), idempotency_key
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/resume-lab/runs":
                    result = controller.start_resume_run(
                        str(body.get("application_id") or ""),
                        str(body.get("standard_version_id") or ""),
                        idempotency_key,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = RESUME_RUN_RETRY_PATH.fullmatch(path)
                if match:
                    reconciliation_acknowledged = body.get(
                        "reconciliation_acknowledged", False
                    )
                    if not isinstance(reconciliation_acknowledged, bool):
                        raise ContractError(
                            "reconciliation_acknowledged must be a boolean"
                        )
                    result = controller.retry_resume_run(
                        match.group(1),
                        idempotency_key,
                        reconciliation_acknowledged,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = RESUME_RUN_APPROVE_PATH.fullmatch(path)
                if match:
                    result = controller.approve_resume_run(
                        match.group(1),
                        str(body.get("comparison_kind") or ""),
                        idempotency_key,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = RESUME_SELECTION_PATH.fullmatch(path)
                if match:
                    result = controller.select_resume(
                        match.group(1),
                        str(body.get("artifact_id") or ""),
                        str(body.get("evaluation_id") or ""),
                        idempotency_key,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/autofill/handoffs":
                    application_id = str(body.get("application_id") or "")
                    result = controller.issue_autofill_handoff(
                        session.session_id, application_id, idempotency_key
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = SUBMISSION_PATH.fullmatch(path)
                if match:
                    occurred_at = str(body.get("occurred_at") or utc_now())
                    result = controller.record_submission(
                        session.session_id,
                        match.group(1),
                        occurred_at,
                        idempotency_key,
                        str(body.get("resume_decision") or ""),
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/mail/failures/analyze":
                    if set(body) - {'idempotency_key', 'account_id', 'folder_ref', 'query_version', 'message_id'}:
                        raise ContractError('invalid archived mail review request')
                    if controller.review_classifier_factory is None:
                        raise ContractError('mail analysis is not configured')
                    from .review_recovery import recover_review
                    with controller._review_analysis_lock:
                        classifier, model_version = controller.review_classifier_factory()
                        result = recover_review(controller.ledger, controller.mail_source, body, classifier, model_version,
                            usage_limits=getattr(controller.automation_config, 'inference_usage_limits', None))
                    self._json(result, session=session, new_session=new_session)
                    return
                if path == "/api/v1/mail/failures/resolve":
                    result = controller.ledger.resolve_mail_failure(
                        str(body.get("account_id") or ""), str(body.get("folder_ref") or ""),
                        str(body.get("message_id") or ""), body.get("query_version"),
                        str(body.get("action") or ""),
                        self._mutation_context(idempotency_key, session, "dashboard_mail_review"),
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = PROPOSAL_DECISION_PATH.fullmatch(path)
                if match:
                    from .review_messages import review_message
                    # Resolve evidence on the server; browser-provided text never
                    # authorizes a different application association.
                    review_mail_content = review_message(controller.ledger, controller.mail_source,
                        {'kind': 'event_proposal', 'id': match.group(1)})
                    snapshot = None
                    if body.get('selected_job') is not None:
                        from .review_recommendations import review_job_snapshot
                        snapshot = review_job_snapshot(controller.jobs, body['selected_job'], review_mail_content)
                    result = controller.ledger.decide_event_proposal(
                        match.group(1),
                        str(body.get("decision") or ""),
                        str(body["selected_application_id"])
                        if body.get("selected_application_id")
                        else None,
                        str(body.get("reason") or ""),
                        self._mutation_context(idempotency_key, session, "dashboard_review"),
                        review_mail_content=review_mail_content,
                        review_job_snapshot=snapshot,
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = TEMPORAL_PROPOSAL_DECISION_PATH.fullmatch(path)
                if match:
                    result = controller.ledger.decide_temporal_proposal(
                        match.group(1),
                        str(body.get("decision") or ""),
                        str(body.get("reason") or ""),
                        self._mutation_context(
                            idempotency_key, session, "dashboard_temporal_review"
                        ),
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = ACTION_DECISION_PATH.fullmatch(path)
                if match:
                    approve = body.get("approve")
                    if not isinstance(approve, bool):
                        raise ContractError("approve must be a boolean")
                    result = controller.ledger.decide_action(
                        match.group(1),
                        approve,
                        str(body.get("payload_sha256") or ""),
                        self._mutation_context(idempotency_key, session, "dashboard_approval"),
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = ACTION_RECONCILE_PATH.fullmatch(path)
                if match:
                    result = controller.ledger.reconcile_action(
                        match.group(1),
                        str(body.get("resolution") or ""),
                        str(body.get("remote_id") or ""),
                        self._mutation_context(
                            idempotency_key, session, "dashboard_reconciliation"
                        ),
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                match = REMINDER_CANCEL_PATH.fullmatch(path)
                if match:
                    result = controller.ledger.cancel_reminder(
                        match.group(1),
                        self._mutation_context(
                            idempotency_key, session, "dashboard_reminder"
                        ),
                    )
                    self._json(result, session=session, new_session=new_session)
                    return
                self._error(HTTPStatus.NOT_FOUND, "route not found", session, new_session)
            except Exception as exc:
                self._handle_exception(exc, session, new_session)

    return DashboardHandler


class _RequestTooLarge(Exception):
    pass


class _DashboardNotFound(ContractError):
    http_status = int(HTTPStatus.NOT_FOUND)


def make_server(
    controller: DashboardController,
    port: int = 8766,
    sessions: Optional[SessionManager] = None,
    *, https_origin: str = "", allowed_tailscale_login: str = "",
) -> ThreadingHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    class TrackingHTTPServer(ThreadingHTTPServer):
        last_capture_maintenance = 0.0

        def service_actions(self):
            if time.monotonic() - self.last_capture_maintenance >= 60:
                self.last_capture_maintenance = time.monotonic()
                try:
                    controller.browser_tracking.maintain_captures()
                except Exception:
                    # Capture persistence is auxiliary; lifecycle events remain durable.
                    pass

    server = TrackingHTTPServer(
        ("127.0.0.1", port),
        make_handler(controller, sessions, https_origin=https_origin,
                     allowed_tailscale_login=allowed_tailscale_login),
    )
    server.daemon_threads = True
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local job-search dashboard")
    parser.add_argument("--job-db", type=Path, default=Path("job-boards.db"))
    parser.add_argument(
        "--preference-db", type=Path, default=Path("job-boards-preference.db")
    )
    parser.add_argument("--proxy-db", type=Path, default=Path("job-boards-proxy.db"))
    parser.add_argument("--application-db", type=Path, default=Path("job-search.db"))
    parser.add_argument(
        "--autofill-profile",
        type=Path,
        help="mode-0600 versioned JSON profile used only for scoped extension handoffs",
    )
    parser.add_argument(
        "--autofill-vault",
        type=Path,
        help="Keychain-backed encrypted vault for private and captured autofill answers",
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--timezone", default="America/Chicago")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    preferences = PreferenceGateway(
        PreferencePaths(args.job_db, args.preference_db, args.proxy_db)
    )
    settings = DashboardSettings(
        timezone=args.timezone,
        outlook_configured=has_outlook_config(os.environ),
        job_scraper_contact_configured=has_real_scraper_contact(os.environ),
    )
    ledger = JobSearchLedger(args.application_db)
    controller = DashboardController(
        ledger,
        preferences,
        settings,
        AutofillBroker(
            ledger,
            load_profile(args.autofill_profile),
            EncryptedAutofillVault(args.autofill_vault)
            if args.autofill_vault is not None
            else None,
        ),
    )
    server = make_server(controller, args.port)
    print(f"Job-search dashboard listening on http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
