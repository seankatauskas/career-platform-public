"""Bounded catalog recall for understanding; never creates application records."""
import re
import sqlite3
import unicodedata
from contextlib import closing

from job_search.contracts import ContractError


def fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
                   if not unicodedata.combining(c))


def words(value):
    return re.findall(r"[^\W_]+", fold(value))


def employer_reasons(name, text, *, normalized=None, word_set=None):
    tokens = words(name)
    while tokens and tokens[-1] in {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation"}:
        tokens.pop()
    compact = "".join(tokens)
    normalized = fold(text) if normalized is None else normalized
    if len(compact) >= 3 and re.search(r"\b" + r"[\W_]*".join(map(re.escape, compact)) + r"\b", normalized):
        return ["current_employer_name"]
    compact = "".join(words(name))
    word_set = words(text) if word_set is None else word_set
    if len(compact) >= 4 and any(word in compact or compact in word for word in word_set if len(word) >= 4):
        return ["current_employer_partial"]
    return []


def role_mentioned(title, text, *, normalized=None):
    tokens = words(title)
    return bool(tokens) and bool(re.search(r"\b" + r"[\W_]+".join(map(re.escape, tokens)) + r"\b", fold(text) if normalized is None else normalized))


def catalog_candidates(catalog, text, *, limit=20, excluded_sources=()):
    """Scan the small company index, then bounded metadata for relevant boards.

    Exact titles narrow a board only when present in the email; identical titles
    at different locations and closed postings remain competing candidates.
    No descriptions, recency preference, or inferred identity uniqueness.
    """
    if not isinstance(text, str) or type(limit) is not int or not 1 <= limit <= 20:
        raise ContractError("Invalid understanding catalog query")
    if not catalog.jobs_db.is_file():
        raise ContractError("normalized jobs database is unavailable")
    board_limit, role_limit = 20000, 1000
    normalized, word_set = fold(text), set(words(text))
    complete, rows = True, {}
    identifiers = sorted({v.lower() for v in re.findall(r"\b(?:\d{6,}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})\b", text)})
    complete = len(identifiers) <= 100
    with closing(sqlite3.connect(catalog.jobs_db.as_uri() + "?mode=ro", uri=True, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN")
        columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        if not {"ats", "id", "company", "title"} <= columns:
            raise ContractError("normalized jobs schema is incompatible")
        fields = [f for f in ("ats", "id", "company", "title", "location", "jobUrl", "closed_at") if f in columns]
        projection = ",".join(fields)
        # jobs_company(ats,company) is covering: avoid the multi-GB descriptions.
        boards = con.execute("SELECT DISTINCT ats,company FROM jobs ORDER BY ats,company LIMIT ?", (board_limit + 1,)).fetchall()
        complete &= len(boards) <= board_limit
        matched = [(b, employer_reasons(b["company"], text, normalized=normalized, word_set=word_set)) for b in boards[:board_limit]]
        matched = [(b, reasons) for b, reasons in matched if reasons]
        complete &= len(matched) <= 30
        for board, reasons in matched[:30]:
            found = [dict(r) for r in con.execute(f"SELECT {projection} FROM jobs WHERE ats=? AND company=? LIMIT ?",
                                                 (board["ats"], board["company"], role_limit + 1))]
            complete &= len(found) <= role_limit
            found = found[:role_limit]
            exact = [r for r in found if role_mentioned(r["title"], text, normalized=normalized)]
            for row in exact or found:
                row["retrieval_reasons"] = reasons + (["current_role_title"] if exact else [])
                rows[(row["ats"], row["id"])] = row
        # Composite-key lookups also find named postings without employer text.
        for ats in ("ashby", "greenhouse", "lever"):
            if not identifiers:
                break
            args = identifiers[:100]
            for found in con.execute(f"SELECT {projection} FROM jobs WHERE ats=? AND id IN ({','.join('?' for _ in args)})", (ats, *args)):
                row = dict(found)
                key = (row["ats"], row["id"])
                row["retrieval_reasons"] = list(dict.fromkeys(rows.get(key, {}).get("retrieval_reasons", []) + ["current_posting_id"]))
                rows[key] = row
    excluded = set(excluded_sources)
    eligible = [row for key, row in rows.items() if key not in excluded]
    eligible.sort(key=lambda r: (-sum({"current_posting_id": 8, "current_employer_name": 4,
        "current_employer_partial": 2, "current_role_title": 1}[reason] for reason in r["retrieval_reasons"]), r["ats"], r["id"]))
    complete &= len(eligible) <= limit
    candidates = [{"id": f"catalog:{r['ats']}:{r['id']}", "kind": "catalog_role",
        "job": {"ats": r["ats"], "source_id": r["id"], "employer": r["company"], "title": r["title"],
                "location": r.get("location"), "job_url": r.get("jobUrl"), "closed_at": r.get("closed_at")},
        "retrieval_reasons": r["retrieval_reasons"], "application_recorded": False,
        "browser_submission_observed": False} for r in eligible[:limit]]
    return {"candidates": candidates, "retrieval": {"method": "catalog_lexical_v1", "complete": bool(complete),
        "boards_scanned": min(len(boards), board_limit), "board_scan_limit": board_limit,
        "matched_boards": len(matched), "board_match_limit": 30, "roles_per_board_limit": role_limit,
        "matched": len(eligible), "selected": len(candidates), "limit": limit}}
