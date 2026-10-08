"""Conservative application identity checks independent of classifier confidence."""

from __future__ import annotations

import re
from typing import Sequence

from .context import CandidateApplication


def _key(value: str) -> str:
    from .rules import _searchable
    return _searchable(value).replace(" ", "")


def _names(subject: str, text: str) -> tuple[str, ...]:
    # Only explicit recruiting constructions identify an employer. A shared ATS
    # sender, a role title, and arbitrary mentions elsewhere are not identity.
    patterns = (
        r"(?:thanks|thank you) for applying to ([^\n!?]{1,300})",
        r"your application (?:to|at) ([^\n!?]{1,300})",
        r"(?:role|position) at ([^\n!?]{1,300})",
    )
    names = []
    for pattern in patterns:
        for match in re.finditer(pattern, subject + "\n" + text, re.I):
            name = re.split(r"\.\s|\s+(?:for|has|was|is|we)\b|\s+[|–—]\s", match[1], maxsplit=1, flags=re.I)[0]
            name = name.strip(" .,:;\"'“”")
            # These are time/process phrases, not competing employer identities.
            if re.match(r"(?:this|that|the present|any) (?:time|stage|point)\b", name, re.I):
                continue
            # "Applying to the Engineer role at Acme" names Acme, not the role.
            role_employer = re.fullmatch(r"(?:the )?.+? (?:role|position) at (.+)", name, re.I)
            if role_employer:
                name = role_employer[1].strip(" .,:;")
            elif re.fullmatch(r"(?:the )?.+? (?:role|position)", name, re.I):
                # "Applying to the Engineer role" names a role, not an employer.
                # The company must come from separate evidence (often the subject).
                continue
            if name:
                names.append(name)
    return tuple(names)


def _same_employer(candidate: CandidateApplication, name: str) -> bool:
    return _key(name) in {_key(candidate.employer), _key(candidate.company_slug)} - {""}


def _contains(value: str, text: str) -> bool:
    from .rules import _searchable
    value, text = _searchable(value), _searchable(text)
    return len(value) >= 4 and bool(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text))


def _roles(message: str) -> list[str]:
    # Generic future-opening encouragement is not a conflicting role identity.
    roles = re.findall(r"\b(?:apply|applying|application) (?:to [^\n.!?]{1,100}? for|for|to) (?:the )?([^\n.!?]{1,100}?) (?:role|position)\b", message, re.I)
    return [role for role in roles if len(role.split()) <= 10
            and not re.match(r"(?:a|an|any|another|future|other|open|different|new)\b", role, re.I)
            and not re.search(r"\b(?:you|we|our|your|that|which)\b", role, re.I)]


def _conflicts(candidate: CandidateApplication, names, roles) -> bool:
    return (bool(names) and not all(_same_employer(candidate, name) for name in names)
            or bool(roles) and not all(_key(role) == _key(candidate.title) for role in roles))


def identity_conflicts(candidate: CandidateApplication, subject: str, text: str) -> bool:
    """Whether explicit employer/role evidence contradicts a saved selection."""
    return _conflicts(candidate, _names(subject, text), _roles(subject + '\n' + text))


def supported_candidates(
    candidates: Sequence[CandidateApplication], subject: str, text: str,
) -> tuple[CandidateApplication, ...]:
    """Return review choices with identity evidence, including ambiguous roles."""
    from .rules import employer_named
    names = _names(subject, text)
    message = subject + "\n" + text
    roles = _roles(message)
    supported = []
    for candidate in candidates:
        # A conflicting explicit employer overrides even an old thread link or ID.
        if _conflicts(candidate, names, roles):
            continue
        if (names or employer_named(candidate.employer, candidate.company_slug, message)
                or _contains(candidate.job_id, message)
                or "previously linked email conversation" in candidate.match_context):
            supported.append(candidate)
    return tuple(supported)


def _review_role_matches(title: str, role: str) -> bool:
    """Allow small role wording differences only for an explicit user review."""
    from .rules import _searchable
    if _key(title) == _key(role):
        return True
    modifiers = {'senior', 'sr', 'junior', 'jr', 'staff', 'principal', 'lead',
                 'i', 'ii', 'iii', 'iv', 'v', '1', '2', '3', '4', '5',
                 'remote', 'hybrid', 'onsite', 'the', 'a', 'of', 'and'}
    title_words = set(_searchable(title).split()) - modifiers
    role_words = set(_searchable(role).split()) - modifiers
    if not title_words or not role_words:
        return False
    if title_words == role_words:
        return True
    common = title_words & role_words
    return len(common) >= 2 and len(common) / len(title_words | role_words) >= 2 / 3


def review_supported_candidates(
    candidates: Sequence[CandidateApplication], subject: str, text: str,
) -> tuple[CandidateApplication, ...]:
    """Review-only employer matches with close role wording, never auto-linking.

    The stricter ``supported_candidates`` and ``supported_selection`` remain the
    automatic-mail gates. This helper only widens choices a user can inspect and
    explicitly approve; conflicting employers and unrelated roles stay excluded.
    """
    from .rules import employer_named
    names = _names(subject, text)
    message = subject + "\n" + text
    roles = _roles(message)
    supported = []
    for candidate in candidates:
        if names and not all(_same_employer(candidate, name) for name in names):
            continue
        if roles and not all(_review_role_matches(candidate.title, role) for role in roles):
            continue
        if (names or employer_named(candidate.employer, candidate.company_slug, message)
                or _contains(candidate.job_id, message)
                or "previously linked email conversation" in candidate.match_context):
            supported.append(candidate)
    return tuple(supported)


def unique_supported_application(
    candidates: Sequence[CandidateApplication], subject: str, text: str,
) -> str | None:
    """Resolve exact identity once for a complete candidate set, preserving ties."""
    supported = supported_candidates(candidates, subject, text)
    if len(supported) == 1:
        return supported[0].application_id
    message = subject + "\n" + text
    for matches in (
        [item for item in supported if _contains(item.job_id, message)],
        [item for item in supported if _contains(item.title, message)],
        [item for item in supported if "previously linked email conversation" in item.match_context],
    ):
        if matches:
            return matches[0].application_id if len(matches) == 1 else None
    return None


def supported_selection(
    candidates: Sequence[CandidateApplication], application_id: str | None,
    subject: str, text: str,
) -> str | None:
    """A model may select only a uniquely supported identity, never break a tie."""
    selected = unique_supported_application(candidates, subject, text)
    return selected if selected == application_id else None
