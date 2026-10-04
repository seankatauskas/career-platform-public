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
            if name:
                names.append(name)
    return tuple(names)


def _same_employer(candidate: CandidateApplication, name: str) -> bool:
    return _key(name) in {_key(candidate.employer), _key(candidate.company_slug)} - {""}


def _contains(value: str, text: str) -> bool:
    from .rules import _searchable
    value, text = _searchable(value), _searchable(text)
    return len(value) >= 4 and bool(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text))


def supported_candidates(
    candidates: Sequence[CandidateApplication], subject: str, text: str,
) -> tuple[CandidateApplication, ...]:
    """Return review choices with identity evidence, including ambiguous roles."""
    from .rules import employer_named
    names = _names(subject, text)
    message = subject + "\n" + text
    roles = re.findall(r"\b(?:apply|applying|application) for (?:the )?([^\n.!?]{1,160}?) (?:role|position)\b", message, re.I)
    supported = []
    for candidate in candidates:
        # A conflicting explicit employer overrides even an old thread link or ID.
        if names and not all(_same_employer(candidate, name) for name in names):
            continue
        if roles and not all(_key(role) == _key(candidate.title) for role in roles):
            continue
        if (names or employer_named(candidate.employer, candidate.company_slug, message)
                or _contains(candidate.job_id, message)
                or "previously linked email conversation" in candidate.match_context):
            supported.append(candidate)
    return tuple(supported)


def supported_selection(
    candidates: Sequence[CandidateApplication], application_id: str | None,
    subject: str, text: str,
) -> str | None:
    """A model may select only a uniquely supported identity, never break a tie."""
    supported = supported_candidates(candidates, subject, text)
    if application_id not in {item.application_id for item in supported}:
        return None
    if len(supported) == 1:
        return application_id
    message = subject + "\n" + text
    for matches in (
        [item for item in supported if _contains(item.job_id, message)],
        [item for item in supported if _contains(item.title, message)],
        [item for item in supported if "previously linked email conversation" in item.match_context],
    ):
        if matches:
            return application_id if len(matches) == 1 and matches[0].application_id == application_id else None
    return None
