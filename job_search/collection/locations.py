#!/usr/bin/env python3
"""Conservative, offline location enrichment for preference ranking.

Raw ATS location fields remain authoritative in ``jobs``.  This module maintains
regenerable, versioned rows that answer only the high-confidence questions needed by
the recommender: work arrangement, explicit US eligibility, and preferred metro.
Ambiguous remote scope is unknown rather than silently treated as US-compatible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


NORMALIZER_VERSION = "location-us-v1"
ARRANGEMENTS = {"onsite", "hybrid", "remote", "mixed", "unknown"}
US_ELIGIBILITY = {"eligible", "ineligible", "unknown"}

PREFERRED_METROS: dict[str, tuple[str, ...]] = {
    "sf_bay_area": (
        "san francisco", "sf bay area", "bay area", "san jose", "palo alto",
        "mountain view", "sunnyvale", "menlo park", "redwood city", "san mateo",
        "south san francisco", "oakland", "berkeley", "cupertino",
    ),
    "new_york_city": (
        "new york city", "new york, ny", "nyc", "manhattan", "brooklyn",
        "queens, ny", "jersey city",
    ),
    "seattle": ("seattle", "bellevue, wa", "redmond, wa"),
    "boston": ("boston", "cambridge, ma", "somerville, ma"),
    "austin": ("austin",),
    "los_angeles": (
        "los angeles", "santa monica", "culver city", "pasadena, ca",
        "burbank, ca", "el segundo",
    ),
    "chicago": ("chicago",),
}

_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    "louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    "missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    "virginia|washington|west virginia|wisconsin|wyoming|district of columbia"
)
_STATE_NAME = re.compile(rf"\b(?:{_US_STATES})\b", re.IGNORECASE)
_STATE_CODE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
    r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
    r"TX|UT|VT|VA|WA|WV|WI|WY|DC)(?:\b|\s|$)",
    re.IGNORECASE,
)
_US_SCOPE = re.compile(
    r"\b(?:united states(?: of america)?|us[- ](?:only|based)|"
    r"us time ?zones?|north america|worldwide|anywhere|global)\b",
    re.IGNORECASE,
)
_UPPER_US_SCOPE = re.compile(r"(?<!\w)(?:US|USA|U\.S\.?|U\.S\.A\.?)(?!\w)")
_NON_US = re.compile(
    r"\b(?:canada|mexico|united kingdom|uk|england|scotland|ireland|france|germany|"
    r"spain|italy|portugal|netherlands|belgium|switzerland|austria|sweden|norway|"
    r"denmark|finland|poland|romania|bulgaria|czechia|czech republic|ukraine|"
    r"israel|india|singapore|japan|china|hong kong|taiwan|south korea|korea|"
    r"australia|new zealand|brazil|argentina|colombia|chile|peru|south africa|"
    r"emirates|uae|saudi arabia|qatar|egypt|turkey|philippines|indonesia|malaysia|"
    r"thailand|vietnam)\b",
    re.IGNORECASE,
)
_REMOTE = re.compile(r"\b(?:remote|home[- ]based|work from home|wfh)\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_ONSITE = re.compile(r"\b(?:on[- ]?site|in[- ]office|office[- ]based)\b", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


SCHEMA = """
CREATE TABLE IF NOT EXISTS job_location_enrichment (
    ats                    TEXT NOT NULL,
    job_id                 TEXT NOT NULL,
    normalizer_version     TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (
                               status IN ('complete','partial','ambiguous','empty')
                           ),
    arrangement            TEXT NOT NULL CHECK (
                               arrangement IN ('onsite','hybrid','remote','mixed','unknown')
                           ),
    remote_eligible        INTEGER,
    us_eligibility         TEXT NOT NULL CHECK (
                               us_eligibility IN ('eligible','ineligible','unknown')
                           ),
    preferred_metro_code   TEXT,
    source_fingerprint     TEXT NOT NULL,
    evidence_json          TEXT NOT NULL,
    processed_at           TEXT NOT NULL,
    PRIMARY KEY (ats, job_id),
    FOREIGN KEY (ats, job_id) REFERENCES jobs(ats,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS job_location_enrichment_filter
ON job_location_enrichment(us_eligibility,preferred_metro_code,arrangement);

CREATE TABLE IF NOT EXISTS job_location_targets (
    ats              TEXT NOT NULL,
    job_id           TEXT NOT NULL,
    ordinal          INTEGER NOT NULL,
    role             TEXT NOT NULL CHECK (
                         role IN ('physical_site','remote_eligibility','unspecified')
                     ),
    kind             TEXT NOT NULL CHECK (
                         kind IN ('metro','country','worldwide','unresolved')
                     ),
    raw_fragment     TEXT NOT NULL,
    display_name     TEXT NOT NULL,
    metro_code       TEXT,
    country_code     TEXT,
    resolution       TEXT NOT NULL CHECK (
                         resolution IN ('exact','alias','ambiguous','unresolved')
                     ),
    evidence_json    TEXT NOT NULL,
    PRIMARY KEY (ats,job_id,ordinal),
    FOREIGN KEY (ats,job_id) REFERENCES jobs(ats,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS job_location_targets_place
ON job_location_targets(country_code,metro_code,role);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any) -> str:
    text = unicodedata.normalize("NFKC", value if isinstance(value, str) else "")
    return _SPACE.sub(" ", text).strip()


def source_fingerprint(
    ats: Any, location: Any, is_remote: Any, workplace_type: Any,
) -> str:
    payload = json.dumps(
        [str(ats or ""), str(location or ""), str(is_remote or ""), str(workplace_type or "")],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256((NORMALIZER_VERSION + "\0" + payload).encode()).hexdigest()


def _truthy(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


def _metro_matches(text: str) -> list[tuple[str, str]]:
    folded = text.casefold()
    matches: list[tuple[str, str]] = []
    for code, aliases in PREFERRED_METROS.items():
        for alias in aliases:
            if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded):
                matches.append((code, alias))
                break
    return matches


def _us_scope_match(text: str) -> re.Match[str] | None:
    """Match US scope without interpreting the pronoun "us" as a country."""
    return _US_SCOPE.search(text) or _UPPER_US_SCOPE.search(text)


def normalize_location(row: dict[str, Any]) -> dict[str, Any]:
    raw = _clean(row.get("location"))
    workplace = _clean(row.get("workplaceType")).casefold()
    remote_flag = _truthy(row.get("isRemote"))
    evidence: list[dict[str, Any]] = []

    raw_remote = bool(_REMOTE.search(raw))
    raw_hybrid = bool(_HYBRID.search(raw))
    raw_onsite = bool(_ONSITE.search(raw))
    if workplace in {"remote", "hybrid", "onsite", "on-site"}:
        arrangement = "onsite" if workplace in {"onsite", "on-site"} else workplace
        evidence.append({"field": "workplaceType", "value": row.get("workplaceType")})
    elif raw_hybrid:
        arrangement = "hybrid"
        evidence.append({"field": "location", "rule": "hybrid_term"})
    elif raw_remote:
        arrangement = "remote"
        evidence.append({"field": "location", "rule": "remote_term"})
    elif raw_onsite:
        arrangement = "onsite"
        evidence.append({"field": "location", "rule": "onsite_term"})
    elif remote_flag is True:
        arrangement = "remote"
        evidence.append({"field": "isRemote", "value": True})
    else:
        arrangement = "unknown"

    metros = _metro_matches(raw)
    # An explicit remote alternative plus a physical metro is materially mixed.
    if arrangement == "remote" and metros and re.search(r"[;|]|\bor\b", raw, re.I):
        arrangement = "mixed"
    remote_eligible = True if arrangement in {"remote", "hybrid", "mixed"} else (
        False if arrangement == "onsite" else remote_flag
    )

    scope_match = _us_scope_match(raw)
    us_evidence = bool(scope_match or _STATE_NAME.search(raw) or _STATE_CODE.search(raw) or metros)
    non_us_evidence = bool(_NON_US.search(raw))
    if us_evidence:
        us_eligibility = "eligible"
    elif non_us_evidence:
        us_eligibility = "ineligible"
    else:
        us_eligibility = "unknown"

    targets: list[dict[str, Any]] = []
    for metro_code, alias in metros:
        targets.append({
            "role": "physical_site" if arrangement != "remote" else "unspecified",
            "kind": "metro", "raw_fragment": alias,
            "display_name": metro_code.replace("_", " ").title(),
            "metro_code": metro_code, "country_code": "US",
            "resolution": "alias", "evidence": [{"alias": alias}],
        })
    if scope_match:
        worldwide = bool(re.search(r"\b(?:worldwide|anywhere|global)\b", raw, re.I))
        targets.append({
            "role": "remote_eligibility" if arrangement in {"remote", "mixed"} else "unspecified",
            "kind": "worldwide" if worldwide else "country",
            "raw_fragment": scope_match.group(0),
            "display_name": "Worldwide" if worldwide else "United States",
            "metro_code": None, "country_code": None if worldwide else "US",
            "resolution": "exact", "evidence": [{"rule": "explicit_us_scope"}],
        })
    elif us_evidence and not metros:
        targets.append({
            "role": "unspecified", "kind": "country", "raw_fragment": raw,
            "display_name": "United States", "metro_code": None,
            "country_code": "US", "resolution": "alias",
            "evidence": [{"rule": "state_or_us_place"}],
        })
    elif raw and not targets:
        targets.append({
            "role": "unspecified", "kind": "unresolved", "raw_fragment": raw,
            "display_name": raw, "metro_code": None, "country_code": None,
            "resolution": "unresolved", "evidence": [],
        })

    if not raw:
        status = "empty"
    elif us_eligibility == "unknown":
        status = "ambiguous"
    elif non_us_evidence and us_evidence:
        status = "partial"
    else:
        status = "complete"
    preferred = metros[0][0] if metros else None
    return {
        "status": status,
        "arrangement": arrangement,
        "remote_eligible": remote_eligible,
        "us_eligibility": us_eligibility,
        "preferred_metro_code": preferred,
        "source_fingerprint": source_fingerprint(
            row.get("ats"), row.get("location"), row.get("isRemote"), row.get("workplaceType")
        ),
        "evidence": evidence,
        "targets": targets,
    }


def prepare_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def backfill(db_path: Path, limit: int | None = None) -> dict[str, int]:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    processed = changed = 0
    statuses: dict[str, int] = {}
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        prepare_schema(con)
        query = (
            "SELECT ats,id,location,isRemote,workplaceType FROM jobs "
            "ORDER BY ats,id" + (" LIMIT ?" if limit is not None else "")
        )
        rows: Iterable[sqlite3.Row] = con.execute(query, (() if limit is None else (limit,)))
        for row in rows:
            result = normalize_location(dict(row))
            processed += 1
            statuses[result["status"]] = statuses.get(result["status"], 0) + 1
            existing = con.execute(
                "SELECT source_fingerprint FROM job_location_enrichment WHERE ats=? AND job_id=?",
                (row["ats"], row["id"]),
            ).fetchone()
            if existing and existing[0] == result["source_fingerprint"]:
                continue
            changed += 1
            now = _now()
            con.execute(
                "INSERT INTO job_location_enrichment "
                "(ats,job_id,normalizer_version,status,arrangement,remote_eligible,"
                "us_eligibility,preferred_metro_code,source_fingerprint,evidence_json,processed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ats,job_id) DO UPDATE SET "
                "normalizer_version=excluded.normalizer_version,status=excluded.status,"
                "arrangement=excluded.arrangement,remote_eligible=excluded.remote_eligible,"
                "us_eligibility=excluded.us_eligibility,"
                "preferred_metro_code=excluded.preferred_metro_code,"
                "source_fingerprint=excluded.source_fingerprint,"
                "evidence_json=excluded.evidence_json,processed_at=excluded.processed_at",
                (
                    row["ats"], row["id"], NORMALIZER_VERSION, result["status"],
                    result["arrangement"], result["remote_eligible"],
                    result["us_eligibility"], result["preferred_metro_code"],
                    result["source_fingerprint"], json.dumps(result["evidence"], sort_keys=True), now,
                ),
            )
            con.execute(
                "DELETE FROM job_location_targets WHERE ats=? AND job_id=?",
                (row["ats"], row["id"]),
            )
            con.executemany(
                "INSERT INTO job_location_targets "
                "(ats,job_id,ordinal,role,kind,raw_fragment,display_name,metro_code,"
                "country_code,resolution,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        row["ats"], row["id"], ordinal, target["role"], target["kind"],
                        target["raw_fragment"], target["display_name"], target["metro_code"],
                        target["country_code"], target["resolution"],
                        json.dumps(target["evidence"], sort_keys=True),
                    )
                    for ordinal, target in enumerate(result["targets"], 1)
                ],
            )
    return {"processed": processed, "changed": changed, **statuses}


def compatibility_score(row: sqlite3.Row | dict[str, Any]) -> float | None:
    if str(row["us_eligibility"]) != "eligible":
        return None
    if row["preferred_metro_code"]:
        return 1.0
    if str(row["arrangement"]) in {"remote", "mixed"}:
        return 0.8
    return 0.55


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backfill", "status"))
    parser.add_argument("--db", type=Path, default=Path("job-boards.db"))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "backfill":
            result = backfill(args.db.resolve(), args.limit)
        else:
            uri = "file:" + str(args.db.resolve()) + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                result = dict(con.execute(
                    "SELECT status,COUNT(*) FROM job_location_enrichment GROUP BY status"
                ))
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
