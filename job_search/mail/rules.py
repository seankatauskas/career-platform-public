"""Deterministic rules for known ATS submission-confirmation templates."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Mapping, Sequence

from job_search.contracts import ApplicationEventType, EventProposalInput, ProducerKind, parse_utc

from .context import CandidateApplication, bounded_candidates
from .proposals import build_proposal
from .sanitizer import SanitizedMail


RULE_PRODUCER_VERSION = "mail-rules-v3-recent-submission"
SUBMISSION_CONFIRMATION_WINDOW_SECONDS = 15 * 60


@dataclass(frozen=True)
class KnownTemplate:
    template_id: str
    sender_domains: tuple[str, ...]
    ats_values: tuple[str, ...]
    subject_pattern: re.Pattern[str]
    evidence_pattern: re.Pattern[str]


@dataclass(frozen=True)
class RuleMatch:
    template_id: str
    proposal: EventProposalInput
    candidate_match: str


def _template(
    template_id: str,
    domains: tuple[str, ...],
    ats_values: tuple[str, ...],
) -> KnownTemplate:
    return KnownTemplate(
        template_id=template_id,
        sender_domains=domains,
        ats_values=ats_values,
        subject_pattern=re.compile(
            r"\b(?:thank you for applying|application (?:was )?received|"
            r"we (?:have )?received your application)\b",
            re.I,
        ),
        evidence_pattern=re.compile(
            r"\b(?:we (?:have )?received your application|"
            r"your application (?:has been|was) received|"
            r"thank you for applying(?: to [^\n.!]{1,120})?)\b",
            re.I,
        ),
    )


KNOWN_TEMPLATES = (
    _template("greenhouse-submission-v1", ("greenhouse.io", "greenhouse-mail.io"), ("greenhouse",)),
    _template("ashby-submission-v1", ("ashbyhq.com",), ("ashby",)),
    _template("lever-submission-v1", ("lever.co",), ("lever",)),
    _template("workday-submission-v1", ("workday.com", "myworkday.com"), ("workday",)),
)


def _sender_domain(sender_address: str) -> str:
    address = parseaddr(sender_address)[1].strip().casefold()
    if address.count("@") != 1:
        return ""
    return address.rsplit("@", 1)[1].rstrip(".")


def _trusted_domain(domain: str, allowed: tuple[str, ...]) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in allowed)


def is_application_verification_email(sender: str, subject: str) -> bool:
    domain = _sender_domain(sender)
    trusted = any(_trusted_domain(domain, template.sender_domains) for template in KNOWN_TEMPLATES)
    return trusted and bool(re.search(
        r"^(?:security|verification|authentication|one[- ]time) code (?:for|to) (?:your )?application\b",
        subject.strip(), re.I,
    ))


def _searchable(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def employer_named(employer: str, company_slug: str, text: str) -> bool:
    """Match whole company names, allowing board slugs to omit spaces/punctuation."""
    haystack = _searchable(text)
    for value in (employer, company_slug):
        name = _searchable(value)
        compact = name.replace(" ", "")
        if len(compact) < 3:
            continue
        pattern = r"(?<!\w)" + r"\s*".join(re.escape(c) for c in compact) + r"(?!\w)"
        if re.search(pattern, haystack):
            return True
    return False


def _recent_submission(item: CandidateApplication, received_at: str) -> bool:
    if item.phase not in {"preparing", "awaiting_confirmation"}:
        return False
    try:
        received = parse_utc(received_at)
    except (ValueError, TypeError):
        return False
    for stamp in (item.submitted_at, item.submission_attempted_at):
        try:
            elapsed = (received - parse_utc(stamp)).total_seconds()
        except (ValueError, TypeError):
            continue
        if 0 <= elapsed <= SUBMISSION_CONFIRMATION_WINDOW_SECONDS:
            return True
    return False


def _select_candidate(
    candidates: tuple[CandidateApplication, ...],
    template: KnownTemplate,
    mail: SanitizedMail,
    received_at: str = "",
) -> tuple[str | None, str, bool]:
    eligible = tuple(
        item for item in candidates
        if item.phase != "terminal" and (not item.ats or item.ats.casefold() in template.ats_values)
    )
    haystack = _searchable(mail.subject + "\n" + mail.body)
    named: list[tuple[CandidateApplication, bool]] = []
    for item in eligible:
        employer_match = employer_named(item.employer, item.company_slug, haystack)
        title = _searchable(item.title)
        job_id = _searchable(item.job_id)
        strong_match = employer_match and (
            (len(title) >= 4 and title in haystack)
            or (len(job_id) >= 4 and job_id in haystack)
        )
        if employer_match:
            named.append((item, strong_match))
    strong = [item for item, matches in named if matches]
    if len(strong) == 1:
        return strong[0].application_id, "unique_strong_identity", True
    if len(strong) > 1:
        return None, "ambiguous_strong_identity", False
    recent = [item for item, _ in named if _recent_submission(item, received_at)]
    if len(recent) == 1:
        return recent[0].application_id, "unique_employer_recent_submission", True
    if len(recent) > 1:
        return None, "ambiguous_recent_submissions", False
    if len(named) == 1:
        item, strong_match = named[0]
        return (
            item.application_id,
            "unique_strong_identity" if strong_match else "unique_employer_candidate",
            strong_match,
        )
    # A sole ATS candidate is useful for review, but is not enough evidence for an
    # automatic lifecycle mutation: the message may concern another employer.
    if len(eligible) == 1:
        return eligible[0].application_id, "sole_ats_candidate_for_review", False
    return None, "ambiguous_candidate", False


def match_known_template(
    *,
    evidence_id: str,
    sender_address: str,
    mail: SanitizedMail,
    candidates: Sequence[CandidateApplication | Mapping[str, object]],
    received_at: str = "",
    sender_authenticated: bool = False,
    candidate_context_complete: bool = True,
) -> RuleMatch | None:
    bounded = bounded_candidates(candidates)
    domain = _sender_domain(sender_address)
    for template in KNOWN_TEMPLATES:
        if not _trusted_domain(domain, template.sender_domains):
            continue
        if not template.subject_pattern.search(mail.subject):
            continue
        evidence = template.evidence_pattern.search(mail.body)
        if not evidence:
            continue
        application_id, candidate_match, strong_identity = _select_candidate(
            bounded, template, mail, received_at
        )
        body_start = mail.body_range[0]
        quote = evidence.group(0)
        proposal = build_proposal(
            evidence_id=evidence_id,
            mail=mail,
            candidates=bounded,
            event_type=ApplicationEventType.SUBMISSION_CONFIRMED,
            application_id=application_id,
            producer_kind=ProducerKind.RULE,
            producer_version=RULE_PRODUCER_VERSION,
            # Exact confidence is the durable auto-apply gate.  Authentication,
            # complete candidate enumeration, and strong or recent identity are all
            # required; otherwise the same useful rule result remains review-only.
            confidence=(
                1.0
                if sender_authenticated and candidate_context_complete and strong_identity
                else 0.99
            ),
            evidence_quote=quote,
            span_start=body_start + evidence.start(),
            span_end=body_start + evidence.end(),
        )
        return RuleMatch(template.template_id, proposal, candidate_match)
    return None
