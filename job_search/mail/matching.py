"""Rank local application history before sending bounded candidates to a classifier."""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .rules import _searchable, employer_named


def rank_mail_candidates(rows: Sequence[Mapping[str, Any]], text: str) -> list[dict[str, Any]]:
    haystack = _searchable(text)
    words = set(haystack.split())
    ranked = []
    for original in rows:
        row = dict(original)
        reasons = []
        score = 0.0
        company = employer_named(str(row["employer_snapshot"]), str(row["company_slug_snapshot"]), text)
        if row.get("same_conversation"):
            score += 200; reasons.append("previously linked email conversation")
        job_id = _searchable(str(row["job_id"]))
        if len(job_id) >= 4 and re.search(r"(?<!\w)"+re.escape(job_id)+r"(?!\w)", haystack):
            score += 100; reasons.append("posting ID in message")
        if company:
            score += 60; reasons.append("company named in message")
        title = _searchable(str(row["title_snapshot"]))
        if len(title) >= 4 and title in haystack:
            score += 30; reasons.append("role title in message")
        else:
            role_words = set(title.split()) - {"the", "a", "of", "and", "i", "ii", "iii"}
            if role_words and words & role_words:
                score += 10 * len(words & role_words) / len(role_words)
                reasons.append("partial role wording")
        # ATS no-reply addresses are shared across unrelated employers.
        if row.get("same_sender"):
            score += 15; reasons.append("sender previously linked to application")
        row["mail_match_score"] = score
        row["mail_match_context"] = "; ".join(reasons)
        row["mail_company_named"] = company
        ranked.append(row)
    # Database order provides a stable recency tie-break, never evidence of identity.
    return sorted(ranked, key=lambda row: -row["mail_match_score"])
