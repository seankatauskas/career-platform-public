#!/usr/bin/env python3
"""Generate resumable, auditable LLM weak supervision for job preferences.

The teacher judges semantic relevance, interesting work, and qualification fit from a
private profile.  Salary and location are intentionally absent from the teacher prompt;
they are deterministic ranking signals.  Successful predictions yield both a selective
and a broad policy without creating fake rows in the human preference tables.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import itertools
import json
import math
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence
from urllib.parse import quote

from job_search.inference import (
    EmbeddingProvider,
    InferenceTransportError,
    StructuredGenerationProvider,
    build_structured_provider,
    configured_inference_path,
    load_inference_config,
)

from job_search.ranking.model import (
    DEFAULT_MODEL,
    FeatureDocument,
    PreferenceModelError,
    TrainingExample,
    build_feature_document,
    default_artifact_dir,
    default_state_db,
    description_chunks,
    embed_documents,
    encoder_for_recorded_revision,
    make_encoder,
    metric_summary,
    predict_documents,
    score_model,
    source_documents,
    train_model,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "job-boards.db"
DEFAULT_PROFILE = ROOT / "preference-profile.json"
PROFILE_VERSION = "software-engineer-profile-v1"
PROMPT_VERSION = "semantic-proxy-v1"
TEXT_VERSION = "proxy-text-v1"
DEFAULT_TEACHER = "qwen3.8-27b"
DEFAULT_SEED = 20260901
DEFAULT_SAMPLE_SIZE = 2400
DEFAULT_AUDIT_SIZE = 400
MEASURED_SECONDS_PER_JOB = 20.86
MAX_OUTPUT_TOKENS = 700

SOFTWARE_ROLES = {"core_swe", "adjacent_technical", "non_swe", "unclear"}
SENIORITIES = {
    "intern",
    "entry",
    "mid",
    "senior",
    "staff_plus",
    "manager",
    "executive",
    "unknown",
}
EVIDENCE_DIMENSIONS = {"software_relevance", "interesting_work", "qualification_fit"}
POLICY_IDS = ("selective", "broad")

_SPACE = re.compile(r"\s+")
_BOILERPLATE = re.compile(
    r"\b(?:equal opportunity employer|reasonable accommodation|applicant privacy notice|"
    r"privacy policy|eeo statement)\b",
    re.IGNORECASE,
)
_LIKELY_SWE = re.compile(
    r"\b(?:software|developer|development engineer|backend|back[- ]end|frontend|front[- ]end|"
    r"full[- ]?stack|mobile|ios|android|platform|infrastructure|site reliability|sre|"
    r"devops|machine learning engineer|ml engineer|ai engineer|data engineer|security engineer|"
    r"cloud engineer|systems engineer|embedded|firmware|devtools|developer tools)\b",
    re.IGNORECASE,
)
_LEVEL_BOUNDARY = re.compile(
    r"\b(?:staff|principal|distinguished|manager|director|head of|vice president|vp|lead)\b",
    re.IGNORECASE,
)
_ADJACENT = re.compile(
    r"\b(?:data scientist|analyst|product manager|program manager|solutions|support|"
    r"quality assurance|\bqa\b|test engineer|it engineer|architect|research scientist|"
    r"sales engineer|consultant)\b",
    re.IGNORECASE,
)


DDL = """
CREATE TABLE IF NOT EXISTS proxy_profiles (
    profile_fingerprint TEXT PRIMARY KEY,
    profile_version     TEXT NOT NULL,
    profile_json        TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proxy_runs (
    run_id              TEXT PRIMARY KEY,
    profile_fingerprint TEXT NOT NULL REFERENCES proxy_profiles(profile_fingerprint),
    prompt_version      TEXT NOT NULL,
    text_version        TEXT NOT NULL,
    teacher_model       TEXT NOT NULL,
    seed                INTEGER NOT NULL,
    sample_size         INTEGER NOT NULL,
    audit_size          INTEGER NOT NULL,
    source_fingerprint  TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (
                            status IN ('prepared','running','complete','failed')
                        ),
    created_at          TEXT NOT NULL,
    completed_at        TEXT
);

CREATE TABLE IF NOT EXISTS proxy_queue (
    queue_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL REFERENCES proxy_runs(run_id) ON DELETE CASCADE,
    position             INTEGER NOT NULL,
    split                TEXT NOT NULL CHECK (split IN ('training','audit')),
    stratum              TEXT NOT NULL CHECK (
                             stratum IN ('likely_swe','adjacent','level_boundary','uniform')
                         ),
    company_holdout      INTEGER NOT NULL DEFAULT 0,
    family_id            TEXT NOT NULL,
    ats                  TEXT NOT NULL,
    job_id               TEXT NOT NULL,
    company_snapshot     TEXT NOT NULL,
    title_snapshot       TEXT NOT NULL,
    description_snapshot TEXT NOT NULL,
    semantic_text        TEXT NOT NULL,
    metadata_json        TEXT NOT NULL,
    template_cluster_id  TEXT NOT NULL,
    leakage_group_id     TEXT NOT NULL,
    source_fingerprint   TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending' CHECK (
                             status IN ('pending','running','complete','excluded','failed')
                         ),
    attempts             INTEGER NOT NULL DEFAULT 0,
    started_at           TEXT,
    completed_at         TEXT,
    error                TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id,position),
    UNIQUE (run_id,family_id)
);
CREATE INDEX IF NOT EXISTS proxy_queue_work
ON proxy_queue(run_id,split,status,position);

CREATE TABLE IF NOT EXISTS proxy_predictions (
    queue_id          INTEGER PRIMARY KEY REFERENCES proxy_queue(queue_id) ON DELETE CASCADE,
    model_revision    TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    raw_output        TEXT NOT NULL,
    result_json       TEXT NOT NULL,
    policy_json       TEXT NOT NULL,
    validation_json   TEXT NOT NULL,
    usage_json        TEXT NOT NULL,
    latency_seconds   REAL NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proxy_students (
    run_id             TEXT NOT NULL REFERENCES proxy_runs(run_id) ON DELETE CASCADE,
    policy_id          TEXT NOT NULL CHECK (policy_id IN ('selective','broad')),
    model_run_id       TEXT NOT NULL,
    model_revision     TEXT NOT NULL,
    training_examples  INTEGER NOT NULL,
    state_db           TEXT NOT NULL,
    artifact_dir       TEXT NOT NULL,
    score_json         TEXT NOT NULL,
    trained_at         TEXT NOT NULL,
    PRIMARY KEY (run_id,policy_id)
);

CREATE TABLE IF NOT EXISTS proxy_student_audits (
    run_id          TEXT NOT NULL REFERENCES proxy_runs(run_id) ON DELETE CASCADE,
    policy_id       TEXT NOT NULL CHECK (policy_id IN ('selective','broad')),
    model_run_id    TEXT NOT NULL,
    audit_json      TEXT NOT NULL,
    evaluated_at   TEXT NOT NULL,
    PRIMARY KEY (run_id,policy_id,model_run_id)
);
"""


class ProxyError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def default_proxy_db(source_db: Path) -> Path:
    return source_db.with_name(f"{source_db.stem}-proxy.db")


def connect_proxy(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def connect_source(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ProxyError(f"source database does not exist: {path}")
    uri = "file:" + quote(str(path.resolve())) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def prepare_schema(path: Path) -> None:
    with connect_proxy(path) as con:
        con.executescript(DDL)


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    required = {
        "profile_id",
        "experience_years",
        "current_employer",
        "education",
        "capability_assumption",
        "role_scope",
        "max_seniority",
        "country_required",
        "preferred_metros",
        "remote_policy",
        "missing_salary_policy",
        "compensation",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise ProxyError("profile is missing: " + ", ".join(missing))
    if (
        not isinstance(profile["experience_years"], (int, float))
        or profile["experience_years"] < 0
    ):
        raise ProxyError("experience_years must be non-negative")
    if profile["country_required"] != "US":
        raise ProxyError("this proxy version requires country_required=US")
    if profile["remote_policy"] != "city_preferred":
        raise ProxyError("this proxy version requires remote_policy=city_preferred")
    if profile["missing_salary_policy"] != "neutral":
        raise ProxyError("this proxy version requires missing_salary_policy=neutral")
    if not isinstance(profile["role_scope"], list) or not profile["role_scope"]:
        raise ProxyError("role_scope must be a non-empty list")
    if (
        not isinstance(profile["preferred_metros"], list)
        or not profile["preferred_metros"]
    ):
        raise ProxyError("preferred_metros must be a non-empty list")
    compensation = profile["compensation"]
    if not isinstance(compensation, dict):
        raise ProxyError("compensation must be an object")
    low = compensation.get("low_usd")
    high = compensation.get("high_usd")
    if (
        not all(isinstance(value, (int, float)) for value in (low, high))
        or not 0 <= low < high
    ):
        raise ProxyError("compensation needs numeric 0 <= low_usd < high_usd")
    return profile


def load_profile(path: Path) -> tuple[dict[str, Any], str]:
    try:
        profile = validate_profile(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProxyError(f"could not read profile {path}: {exc}") from exc
    fingerprint = sha256(PROFILE_VERSION + "\0" + canonical_json(profile))
    return profile, fingerprint


def proxy_text(description: str, salary_evidence: Sequence[str] = ()) -> str:
    text = _SPACE.sub(" ", description or "").strip()
    for evidence in sorted(
        {value.strip() for value in salary_evidence if value.strip()},
        key=len,
        reverse=True,
    ):
        text = re.sub(
            re.escape(evidence), " [COMPENSATION REDACTED] ", text, flags=re.IGNORECASE
        )
    boilerplate = _BOILERPLATE.search(text)
    if boilerplate and boilerplate.start() >= 500:
        text = text[: boilerplate.start()].rstrip()
    chunks = description_chunks(text)
    return "\n\n".join(chunks)


def teacher_schema() -> dict[str, Any]:
    evidence_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "dimension": {"type": "string", "enum": sorted(EVIDENCE_DIMENSIONS)},
            "quote": {"type": "string"},
        },
        "required": ["dimension", "quote"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "software_role": {"type": "string", "enum": sorted(SOFTWARE_ROLES)},
            "specialties": {"type": "array", "items": {"type": "string"}},
            "seniority": {"type": "string", "enum": sorted(SENIORITIES)},
            "software_relevance": {"type": "integer", "minimum": 0, "maximum": 4},
            "interesting_work": {"type": "integer", "minimum": 0, "maximum": 4},
            "qualification_fit": {"type": "integer", "minimum": 0, "maximum": 4},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 4},
            "reason_codes": {"type": "array", "items": {"type": "string"}},
            "positive_evidence": {"type": "array", "items": evidence_item},
            "negative_evidence": {"type": "array", "items": evidence_item},
        },
        "required": [
            "software_role",
            "specialties",
            "seniority",
            "software_relevance",
            "interesting_work",
            "qualification_fit",
            "confidence",
            "reason_codes",
            "positive_evidence",
            "negative_evidence",
        ],
    }


SYSTEM_PROMPT = """You are a consistent preference proxy for one software engineer.

Judge only semantic role appeal and plausible qualification. Ignore employer prestige,
location, compensation, benefits, authorization, and other practical constraints even
when they appear in the posting. Do not require an exact programming-language match.
Assume the candidate is in the top 1% among engineers with the same experience and can
learn adjacent stacks quickly, but do not invent Staff-level leadership or management
experience. Hands-on roles through Senior may be plausible stretches.

Score every dimension from 0 to 4 using these anchors:
- software_relevance: 4 core hands-on software building; 2 adjacent technical; 0 non-SWE.
- interesting_work: 4 unusually substantive building/technical ownership; 2 ordinary;
  0 mostly coordination, support, sales, or repetitive non-building work.
- qualification_fit: 4 credible now; 3 credible stretch; 2 substantial stretch;
  1 implausible seniority/domain gate; 0 incompatible.
- confidence: confidence in the judgment, not enthusiasm.

Evidence quotes must be short exact text copied from the supplied title or semantic job
text. Return only one JSON object matching the schema. Never output policy labels; the
application derives both policies deterministically from these dimensions.
"""


def job_prompt(profile: dict[str, Any], row: sqlite3.Row | dict[str, Any]) -> str:
    public_profile = {
        "experience_years": profile["experience_years"],
        "current_employer_context": profile["current_employer"],
        "education": profile["education"],
        "capability_assumption": profile["capability_assumption"],
        "role_scope": profile["role_scope"],
        "max_seniority": profile["max_seniority"],
    }
    return (
        "CANDIDATE PROFILE\n"
        + json.dumps(public_profile, ensure_ascii=False, sort_keys=True)
        + "\n\nJOB TITLE\n"
        + str(row["title_snapshot"] or "")
        + "\n\nDEPARTMENT / TEAM\n"
        + str(json.loads(row["metadata_json"] or "{}").get("department", ""))
        + " / "
        + str(json.loads(row["metadata_json"] or "{}").get("team", ""))
        + "\n\nSEMANTIC JOB TEXT\n"
        + str(row["semantic_text"] or "")
        + "\n\nJSON SCHEMA\n"
        + json.dumps(teacher_schema(), separators=(",", ":"))
    )


def _evidence_in_source(quote_text: str, source: str) -> bool:
    quote_value = _SPACE.sub(" ", quote_text).strip().casefold()
    haystack = _SPACE.sub(" ", source).casefold()
    return bool(quote_value) and quote_value in haystack


def validate_teacher_result(result: Any, source_text: str) -> list[str]:
    if not isinstance(result, dict):
        return ["result must be an object"]
    expected = set(teacher_schema()["required"])
    issues = []
    missing = expected - set(result)
    extra = set(result) - expected
    if missing:
        issues.append("missing fields: " + ", ".join(sorted(missing)))
    if extra:
        issues.append("unexpected fields: " + ", ".join(sorted(extra)))
    if result.get("software_role") not in SOFTWARE_ROLES:
        issues.append("invalid software_role")
    if result.get("seniority") not in SENIORITIES:
        issues.append("invalid seniority")
    for field in (
        "software_relevance",
        "interesting_work",
        "qualification_fit",
        "confidence",
    ):
        value = result.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 4:
            issues.append(f"{field} must be an integer from 0 to 4")
    for field in ("specialties", "reason_codes"):
        value = result.get(field)
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            issues.append(f"{field} must be an array of strings")
    for field in ("positive_evidence", "negative_evidence"):
        values = result.get(field)
        if not isinstance(values, list):
            issues.append(f"{field} must be an array")
            continue
        for index, item in enumerate(values):
            if not isinstance(item, dict) or set(item) != {"dimension", "quote"}:
                issues.append(f"{field}[{index}] has invalid shape")
            elif item["dimension"] not in EVIDENCE_DIMENSIONS:
                issues.append(f"{field}[{index}] has invalid dimension")
            elif not _evidence_in_source(str(item["quote"]), source_text):
                issues.append(f"{field}[{index}] quote is not in source")
    return issues


def derive_policies(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    relevance = result["software_relevance"] / 4.0
    interesting = result["interesting_work"] / 4.0
    qualification = result["qualification_fit"] / 4.0
    selective_score = 0.35 * relevance + 0.40 * interesting + 0.25 * qualification
    broad_score = 0.55 * relevance + 0.30 * interesting + 0.15 * qualification
    selective_positive = (
        relevance >= 0.70
        and qualification >= 0.45
        and selective_score >= 0.68
        and result["seniority"] not in {"staff_plus", "manager", "executive"}
    )
    broad_positive = (
        relevance >= 0.50
        and qualification >= 0.20
        and broad_score >= 0.50
        and result["seniority"] not in {"staff_plus", "manager", "executive"}
    ) or selective_positive
    confidence = 0.5 + result["confidence"] * 0.125
    return {
        "selective": {
            "score": round(selective_score, 6),
            "label": "interested" if selective_positive else "not_interested",
            "sample_weight": round(confidence, 6),
        },
        "broad": {
            "score": round(broad_score, 6),
            "label": "interested" if broad_positive else "not_interested",
            "sample_weight": round(confidence, 6),
        },
    }


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ProxyError("teacher output did not contain a JSON object")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ProxyError("teacher output must be a JSON object")
    return value


def _hash_rank(seed: int, *parts: str) -> str:
    return sha256("\0".join([str(seed), *parts]))


def _stratum(title: str, family_id: str, seed: int) -> str:
    if _LEVEL_BOUNDARY.search(title):
        return "level_boundary"
    if _LIKELY_SWE.search(title):
        return "likely_swe"
    if _ADJACENT.search(title):
        return "adjacent"
    return "uniform"


def _source_tables(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }


def _candidate_rows(source_db: Path, seed: int) -> list[dict[str, Any]]:
    with connect_source(source_db) as con:
        required = {
            "jobs",
            "job_families",
            "job_family_members",
            "job_template_clusters",
            "job_location_enrichment",
        }
        missing = required - _source_tables(con)
        if missing:
            raise ProxyError("source database is missing " + ", ".join(sorted(missing)))
        rows = con.execute(
            "SELECT f.family_id,j.ats,j.id AS job_id,"
            "j.company,j.title,j.department,j.team,j.employmentType,j.publishedAt,j.jobUrl,"
            "m.source_fingerprint,t.template_cluster_id,t.leakage_group_id "
            "FROM job_families f JOIN job_family_members m ON m.family_id=f.family_id "
            "JOIN jobs j ON j.ats=m.ats AND j.id=m.job_id "
            "JOIN job_template_clusters t ON t.family_id=f.family_id "
            "JOIN job_location_enrichment l ON l.ats=j.ats AND l.job_id=j.id "
            "WHERE j.closed_at IS NULL AND LENGTH(TRIM(COALESCE(j.description,'')))>0 "
            "AND datetime(j.publishedAt)>=datetime('now','-90 days') "
            "AND l.us_eligibility='eligible' "
            "ORDER BY f.family_id,datetime(j.publishedAt) DESC,j.ats,j.id"
        ).fetchall()
    candidates = []
    seen_families: set[str] = set()
    for raw in rows:
        row = dict(raw)
        if str(row["family_id"]) in seen_families:
            continue
        seen_families.add(str(row["family_id"]))
        row["stratum"] = _stratum(
            str(row.get("title") or ""), str(row["family_id"]), seed
        )
        row["company_key"] = _SPACE.sub(
            " ", str(row.get("company") or "").casefold()
        ).strip()
        row["rank"] = _hash_rank(seed, str(row["family_id"]))
        candidates.append(row)
    return candidates


def _select_sample(
    candidates: Sequence[dict[str, Any]],
    sample_size: int,
    audit_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    if sample_size < 10 or audit_size < 2 or audit_size >= sample_size:
        raise ProxyError("sample_size must be >=10 and 2 <= audit_size < sample_size")
    if len(candidates) < sample_size:
        raise ProxyError(
            f"need {sample_size} eligible families; found {len(candidates)}"
        )
    ordered = sorted(candidates, key=lambda row: (row["rank"], row["family_id"]))
    holdout_count = audit_size // 2
    company_first: dict[str, dict[str, Any]] = {}
    for row in ordered:
        key = row["company_key"] or row["family_id"]
        company_first.setdefault(key, row)
    holdout_companies = {
        key
        for key, _ in sorted(
            company_first.items(),
            key=lambda item: (_hash_rank(seed, "company", item[0]), item[0]),
        )[:holdout_count]
    }
    used_templates: set[str] = set()
    used_leakage: set[str] = set()
    company_counts: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []

    def take(
        rows: Iterable[dict[str, Any]], count: int, split: str, company_holdout: bool
    ) -> None:
        pool = list(rows)
        before = len(selected)

        def accept(raw: dict[str, Any]) -> bool:
            template = str(raw["template_cluster_id"])
            leakage = str(raw["leakage_group_id"])
            company = raw["company_key"] or raw["family_id"]
            if (
                template in used_templates
                or leakage in used_leakage
                or company_counts[company] >= 3
            ):
                return False
            if company_holdout != (company in holdout_companies):
                return False
            row = dict(raw)
            row["split"] = split
            row["company_holdout"] = int(company_holdout)
            selected.append(row)
            used_templates.add(template)
            used_leakage.add(leakage)
            company_counts[company] += 1
            return True

        weights = {
            "likely_swe": 0.50,
            "level_boundary": 0.20,
            "adjacent": 0.20,
            "uniform": 0.10,
        }
        quotas = {name: int(count * weight) for name, weight in weights.items()}
        for name in ("likely_swe", "level_boundary", "adjacent", "uniform"):
            if sum(quotas.values()) < count:
                quotas[name] += 1
        for name, quota in quotas.items():
            accepted = 0
            for raw in pool:
                if accepted >= quota:
                    break
                if raw["stratum"] == name and accept(raw):
                    accepted += 1
        # Sparse strata should not make a run impossible; deterministically fill any
        # remainder from all eligible groups while retaining the recorded stratum.
        for raw in pool:
            if len(selected) - before >= count:
                break
            accept(raw)
        if len(selected) - before != count:
            raise ProxyError(
                f"could select only {len(selected)-before} of {count} {split} rows"
            )

    take(ordered, holdout_count, "audit", True)
    take(ordered, audit_size - holdout_count, "audit", False)
    take(ordered, sample_size - audit_size, "training", False)
    selected.sort(
        key=lambda row: (row["split"] != "training", row["rank"], row["family_id"])
    )
    return selected


def _snapshot_rows(
    source_db: Path, rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_key = {(str(row["ats"]), str(row["job_id"])): dict(row) for row in rows}
    with connect_source(source_db) as con:
        has_compensation = "job_compensation_ranges" in _source_tables(con)
        for keys in _chunks(list(by_key), 300):
            conditions = " OR ".join("(j.ats=? AND j.id=?)" for _ in keys)
            params = tuple(value for key in keys for value in key)
            if has_compensation:
                query = (
                    "SELECT j.*,json_group_array(DISTINCT r.evidence_text) "
                    "AS salary_evidence_json "
                    "FROM jobs j LEFT JOIN job_compensation_ranges r "
                    "ON r.ats=j.ats AND r.job_id=j.id AND r.removed_at IS NULL "
                    f"WHERE {conditions} GROUP BY j.ats,j.id"
                )
            else:
                query = f"SELECT j.*,'[]' AS salary_evidence_json FROM jobs j WHERE {conditions}"
            source_rows = con.execute(query, params).fetchall()
            for raw in source_rows:
                target = by_key[(str(raw["ats"]), str(raw["id"]))]
                evidence = [
                    str(item)
                    for item in json.loads(raw["salary_evidence_json"] or "[]")
                    if item
                ]
                target["description"] = str(raw["description"] or "")
                target["semantic_text"] = proxy_text(target["description"], evidence)
    return list(by_key.values())


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def prepare_run(
    source_db: Path,
    proxy_db: Path,
    profile_path: Path,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    audit_size: int = DEFAULT_AUDIT_SIZE,
    seed: int = DEFAULT_SEED,
    teacher_model: str = DEFAULT_TEACHER,
) -> dict[str, Any]:
    profile, profile_fingerprint = load_profile(profile_path)
    candidates = _candidate_rows(source_db, seed)
    selected = _snapshot_rows(
        source_db, _select_sample(candidates, sample_size, audit_size, seed)
    )
    source_spec = [
        [row["family_id"], row["source_fingerprint"], row["split"]]
        for row in sorted(selected, key=lambda item: item["family_id"])
    ]
    source_fingerprint = sha256(canonical_json(source_spec))
    run_id = (
        "proxy_"
        + sha256(
            canonical_json(
                [
                    profile_fingerprint,
                    PROMPT_VERSION,
                    TEXT_VERSION,
                    teacher_model,
                    seed,
                    sample_size,
                    audit_size,
                    source_fingerprint,
                ]
            )
        )[:20]
    )
    prepare_schema(proxy_db)
    with connect_proxy(proxy_db) as con:
        existing = con.execute(
            "SELECT status FROM proxy_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            return status(proxy_db, run_id)
        stamp = now()
        con.execute(
            "INSERT OR IGNORE INTO proxy_profiles VALUES (?,?,?,?)",
            (profile_fingerprint, PROFILE_VERSION, canonical_json(profile), stamp),
        )
        con.execute(
            "INSERT INTO proxy_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                run_id,
                profile_fingerprint,
                PROMPT_VERSION,
                TEXT_VERSION,
                teacher_model,
                seed,
                sample_size,
                audit_size,
                source_fingerprint,
                "prepared",
                stamp,
            ),
        )
        # The explicit column list protects this frozen contract from schema additions.
        con.executemany(
            "INSERT INTO proxy_queue "
            "(run_id,position,split,stratum,company_holdout,family_id,ats,job_id,"
            "company_snapshot,title_snapshot,description_snapshot,semantic_text,metadata_json,"
            "template_cluster_id,leakage_group_id,source_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    position,
                    row["split"],
                    row["stratum"],
                    row["company_holdout"],
                    row["family_id"],
                    row["ats"],
                    row["job_id"],
                    row.get("company") or "",
                    row.get("title") or "",
                    row["description"],
                    row["semantic_text"],
                    canonical_json(
                        {
                            "department": row.get("department") or "",
                            "team": row.get("team") or "",
                            "employmentType": row.get("employmentType") or "",
                            "publishedAt": row.get("publishedAt") or "",
                            "jobUrl": row.get("jobUrl") or "",
                        }
                    ),
                    row["template_cluster_id"],
                    row["leakage_group_id"],
                    row["source_fingerprint"],
                )
                for position, row in enumerate(selected, 1)
            ],
        )
    return status(proxy_db, run_id)


def active_run_id(proxy_db: Path) -> str:
    with connect_proxy(proxy_db) as con:
        row = con.execute(
            "SELECT run_id FROM proxy_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise ProxyError("proxy database has no run; run prepare first")
    return str(row[0])


def status(proxy_db: Path, run_id: Optional[str] = None) -> dict[str, Any]:
    prepare_schema(proxy_db)
    selected = run_id or active_run_id(proxy_db)
    with connect_proxy(proxy_db) as con:
        run = con.execute(
            "SELECT * FROM proxy_runs WHERE run_id=?", (selected,)
        ).fetchone()
        if not run:
            raise ProxyError(f"unknown proxy run {selected}")
        counts = {
            f"{row['split']}:{row['status']}": int(row["count"])
            for row in con.execute(
                "SELECT split,status,COUNT(*) AS count FROM proxy_queue "
                "WHERE run_id=? GROUP BY split,status",
                (selected,),
            )
        }
        labels = {
            f"{row['policy']}:{row['label']}": int(row["count"])
            for row in con.execute(
                "SELECT p.key AS policy,json_extract(p.value,'$.label') AS label,COUNT(*) AS count "
                "FROM proxy_predictions x, json_each(x.policy_json) p "
                "JOIN proxy_queue q ON q.queue_id=x.queue_id WHERE q.run_id=? "
                "GROUP BY p.key,label",
                (selected,),
            )
        }
        students = {
            str(row["policy_id"]): {
                "model_run_id": str(row["model_run_id"]),
                "training_examples": int(row["training_examples"]),
                "trained_at": str(row["trained_at"]),
            }
            for row in con.execute(
                "SELECT policy_id,model_run_id,training_examples,trained_at "
                "FROM proxy_students WHERE run_id=? ORDER BY policy_id",
                (selected,),
            )
        }
        audits = {
            str(row["policy_id"]): json.loads(row["audit_json"])
            for row in con.execute(
                "SELECT policy_id,audit_json FROM proxy_student_audits WHERE run_id=?",
                (selected,),
            )
        }
    return {
        "run": dict(run),
        "queue": counts,
        "labels": labels,
        "students": students,
        "audits": audits,
    }


class LocalQwenTeacher:
    def __init__(self, model_name: str = DEFAULT_TEACHER) -> None:
        try:
            from mlx_vlm import apply_chat_template, generate, load
            from job_search.salary.local_eval import MODELS, resolve_model_path
        except ImportError as exc:
            raise ProxyError(
                "run local inference with .venv-local-mlx/bin/python -m job_search.ranking.proxy"
            ) from exc
        if model_name not in MODELS:
            raise ProxyError(f"unknown local teacher {model_name}")
        config = MODELS[model_name]
        path = resolve_model_path(config)
        if path is None:
            raise ProxyError(f"local teacher is not downloaded: {config['repo']}")
        self.model, self.processor = load(str(path))
        self.apply_chat_template = apply_chat_template
        self.generate_fn = generate
        self.model_revision = f"{config['repo']}@{path.name}"

    def generate(self, user_prompt: str) -> tuple[str, dict[str, Any]]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        prompt = self.apply_chat_template(
            self.processor,
            self.model.config,
            messages,
            enable_thinking=False,
        )
        response = self.generate_fn(
            self.model,
            self.processor,
            prompt,
            max_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.0,
            verbose=False,
        )
        return response.text, {
            "prompt_tokens": response.prompt_tokens,
            "generation_tokens": response.generation_tokens,
            "prompt_tps": response.prompt_tps,
            "generation_tps": response.generation_tps,
            "peak_memory_gb": response.peak_memory,
            "finish_reason": response.finish_reason,
            "inference_provenance": {
                "provider": "local-mlx",
                "protocol": "in-process",
                "model_revision": self.model_revision,
            },
        }


class HostedPreferenceTeacher:
    """Use the same teacher prompt and validators through a hosted provider."""

    def __init__(self, provider: StructuredGenerationProvider) -> None:
        self.provider = provider
        # A teacher run is bound to the full serving and decoding contract, not
        # only to the declared model weights. This prevents an endpoint/image
        # replacement from silently contributing judgments to an older run.
        self.model_revision = str(provider.generation_identity)

    def generate(self, user_prompt: str) -> tuple[str, dict[str, Any]]:
        result = self.provider.generate(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            json_schema=teacher_schema(),
            schema_name="preference_teacher",
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.0,
        )
        usage = dict(result.usage)
        usage["inference_provenance"] = dict(result.provenance)
        return result.text, usage


def run_teacher(
    proxy_db: Path,
    profile_path: Path,
    split: str = "training",
    limit: int = 100,
    run_id: Optional[str] = None,
    teacher: Any = None,
    inference_config: Path | None = None,
    inference_provider: StructuredGenerationProvider | None = None,
) -> dict[str, Any]:
    prepare_schema(proxy_db)
    if split not in {"training", "audit"}:
        raise ProxyError("split must be training or audit")
    if limit < 1:
        raise ProxyError("limit must be positive")
    if teacher is not None and (
        inference_config is not None or inference_provider is not None
    ):
        raise ProxyError("teacher and remote inference provider are mutually exclusive")
    if inference_config is not None and inference_provider is not None:
        raise ProxyError(
            "inference_config and inference_provider are mutually exclusive"
        )
    profile, profile_fingerprint = load_profile(profile_path)
    selected = run_id or active_run_id(proxy_db)
    created_teacher = teacher is None
    processed = complete = excluded = failed = 0
    with connect_proxy(proxy_db) as con:
        run = con.execute(
            "SELECT * FROM proxy_runs WHERE run_id=?", (selected,)
        ).fetchone()
        if not run:
            raise ProxyError(f"unknown proxy run {selected}")
        if run["profile_fingerprint"] != profile_fingerprint:
            raise ProxyError("profile does not match the prepared proxy run")
        if split == "audit":
            student_count = con.execute(
                "SELECT COUNT(DISTINCT policy_id) FROM proxy_students WHERE run_id=?",
                (selected,),
            ).fetchone()[0]
            if student_count != len(POLICY_IDS):
                raise ProxyError(
                    "fit both students before generating the protected audit judgments"
                )
        rows = con.execute(
            "SELECT * FROM proxy_queue WHERE run_id=? AND split=? AND status='pending' "
            "ORDER BY position LIMIT ?",
            (selected, split, limit),
        ).fetchall()
        if rows and created_teacher:
            if inference_config is not None:
                inference_provider = build_structured_provider(
                    load_inference_config(inference_config)
                )
            if inference_provider is not None:
                teacher = HostedPreferenceTeacher(inference_provider)
                if teacher.model_revision != str(run["teacher_model"]):
                    raise ProxyError(
                        f"configured teacher revision {teacher.model_revision!r} does not "
                        f"match prepared run {run['teacher_model']!r}"
                    )
            else:
                teacher = LocalQwenTeacher(str(run["teacher_model"]))
        con.execute(
            "UPDATE proxy_runs SET status='running' WHERE run_id=?", (selected,)
        )
        for row in rows:
            processed += 1
            started = time.perf_counter()
            stamp = now()
            con.execute(
                "UPDATE proxy_queue SET status='running',attempts=attempts+1,started_at=?,error='' "
                "WHERE queue_id=?",
                (stamp, row["queue_id"]),
            )
            con.commit()
            raw_output = ""
            try:
                prompt = job_prompt(profile, row)
                raw_output, usage = teacher.generate(prompt)
                result = _json_object(raw_output)
                source = (
                    str(row["title_snapshot"] or "")
                    + "\n"
                    + str(row["semantic_text"] or "")
                )
                issues = validate_teacher_result(result, source)
                if issues:
                    repair = (
                        prompt
                        + "\n\nYour previous JSON failed validation:\n- "
                        + "\n- ".join(issues)
                        + "\nReturn a corrected JSON object only."
                    )
                    raw_output, usage = teacher.generate(repair)
                    result = _json_object(raw_output)
                    issues = validate_teacher_result(result, source)
                if issues:
                    excluded += 1
                    con.execute(
                        "UPDATE proxy_queue SET status='excluded',completed_at=?,error=? "
                        "WHERE queue_id=?",
                        (now(), "; ".join(issues), row["queue_id"]),
                    )
                else:
                    policies = derive_policies(result)
                    con.execute(
                        "INSERT OR REPLACE INTO proxy_predictions "
                        "(queue_id,model_revision,prompt_version,raw_output,result_json,policy_json,"
                        "validation_json,usage_json,latency_seconds,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["queue_id"],
                            str(teacher.model_revision),
                            PROMPT_VERSION,
                            raw_output,
                            canonical_json(result),
                            canonical_json(policies),
                            "[]",
                            canonical_json(usage),
                            time.perf_counter() - started,
                            now(),
                        ),
                    )
                    con.execute(
                        "UPDATE proxy_queue SET status='complete',completed_at=?,error='' "
                        "WHERE queue_id=?",
                        (now(), row["queue_id"]),
                    )
                    complete += 1
            except Exception as exc:
                from job_search.inference.usage import UsageDeferred, InvocationReconciliationRequired
                if isinstance(exc, (UsageDeferred, InvocationReconciliationRequired)):
                    con.execute("UPDATE proxy_queue SET status='pending',attempts=MAX(0,attempts-1),completed_at=NULL,error='' WHERE queue_id=?", (row["queue_id"],))
                    con.commit()
                    raise
                failed += 1
                con.execute(
                    "UPDATE proxy_queue SET status='failed',completed_at=?,error=? WHERE queue_id=?",
                    (now(), f"{type(exc).__name__}: {exc}"[:2000], row["queue_id"]),
                )
            con.commit()
        remaining = con.execute(
            "SELECT COUNT(*) FROM proxy_queue WHERE run_id=? AND status IN ('pending','running')",
            (selected,),
        ).fetchone()[0]
        if remaining == 0:
            con.execute(
                "UPDATE proxy_runs SET status='complete',completed_at=? WHERE run_id=?",
                (now(), selected),
            )
    return {
        "run_id": selected,
        "split": split,
        "processed": processed,
        "complete": complete,
        "excluded": excluded,
        "failed": failed,
    }


def retry_failed(
    proxy_db: Path,
    split: Optional[str] = None,
    run_id: Optional[str] = None,
    include_excluded: bool = False,
) -> dict[str, Any]:
    if split not in {None, "training", "audit"}:
        raise ProxyError("split must be training or audit")
    selected = run_id or active_run_id(proxy_db)
    where = " AND split=?" if split else ""
    params: tuple[Any, ...] = (selected, split) if split else (selected,)
    statuses = "('failed','excluded')" if include_excluded else "('failed')"
    with connect_proxy(proxy_db) as con:
        changed = con.execute(
            "UPDATE proxy_queue SET status='pending',started_at=NULL,completed_at=NULL,error='' "
            f"WHERE run_id=? AND status IN {statuses} AND attempts<3" + where,
            params,
        ).rowcount
        if changed:
            con.execute(
                "UPDATE proxy_runs SET status='running',completed_at=NULL WHERE run_id=?",
                (selected,),
            )
    return {"run_id": selected, "split": split or "all", "retried": changed}


def load_proxy_examples(
    proxy_db: Path,
    run_id: str,
    policy_id: str,
    split: str = "training",
) -> list[TrainingExample]:
    """Read immutable proxy snapshots without manufacturing human preference rows."""
    if policy_id not in POLICY_IDS:
        raise ProxyError(f"unknown policy {policy_id}")
    if split not in {"training", "audit"}:
        raise ProxyError("split must be training or audit")
    with connect_proxy(proxy_db) as con:
        rows = con.execute(
            "SELECT q.*,x.policy_json FROM proxy_queue q JOIN proxy_predictions x "
            "ON x.queue_id=q.queue_id WHERE q.run_id=? AND q.split=? "
            "AND q.status='complete' ORDER BY q.position",
            (run_id, split),
        ).fetchall()
    examples = []
    for raw in rows:
        row = dict(raw)
        policy = json.loads(row["policy_json"])[policy_id]
        metadata = json.loads(row["metadata_json"] or "{}")
        subject_id = f"{run_id}:{row['queue_id']}"
        document = build_feature_document(
            {
                "family_id": row["family_id"],
                "title": row["title_snapshot"],
                "department": metadata.get("department") or "",
                "team": metadata.get("team") or "",
                # This is compensation-redacted semantic text, not the raw snapshot.
                "description": row["semantic_text"],
                "template_cluster_id": row["template_cluster_id"],
                "leakage_group_id": row["leakage_group_id"],
            },
            subject_type="proxy_example",
            subject_id=subject_id,
        )
        examples.append(
            TrainingExample(
                example_id=subject_id,
                document=document,
                target=1 if policy["label"] == "interested" else 0,
                dataset_version=run_id,
                source_fingerprint=str(row["source_fingerprint"]),
                selection_strategy=f"proxy_{row['stratum']}",
                sample_weight=float(policy["sample_weight"]),
            )
        )
    return examples


def load_passive_feedback_examples(source_db: Path) -> list[TrainingExample]:
    """Convert normal shortlist actions into weighted, non-human-label examples."""
    with connect_source(source_db) as con:
        if "recommendation_feedback" not in _source_tables(con):
            return []
        columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(recommendation_feedback)")
        }
        required = {
            "feedback_id",
            "ats",
            "job_id",
            "family_id",
            "action",
            "implicit_weight",
            "title_snapshot",
            "description_snapshot",
            "metadata_json",
            "template_cluster_id",
            "leakage_group_id",
            "source_fingerprint",
            "created_at",
        }
        if not required <= columns:
            return []
        rows = con.execute(
            "SELECT * FROM recommendation_feedback WHERE implicit_weight<>0 "
            "AND family_id<>'' AND title_snapshot<>'' AND description_snapshot<>'' "
            "ORDER BY created_at,feedback_id"
        ).fetchall()
        salary_evidence: dict[tuple[str, str], list[str]] = defaultdict(list)
        if "job_compensation_ranges" in _source_tables(con):
            for evidence in con.execute(
                "SELECT ats,job_id,evidence_text FROM job_compensation_ranges "
                "WHERE removed_at IS NULL AND evidence_text<>''"
            ):
                salary_evidence[(str(evidence["ats"]), str(evidence["job_id"]))].append(
                    str(evidence["evidence_text"])
                )
    # A later weak Save cannot reverse an earlier Apply/Pass. Among strong actions,
    # the latest one wins so a genuine change of mind is still learnable.
    latest = {str(row["family_id"]): row for row in rows}
    latest_strong = {
        str(row["family_id"]): row
        for row in rows
        if abs(float(row["implicit_weight"])) >= 1.0
    }
    latest.update(latest_strong)
    examples = []
    for family_id, raw in sorted(latest.items()):
        row = dict(raw)
        metadata = json.loads(row["metadata_json"] or "{}")
        subject_id = f"feedback:{row['feedback_id']}"
        document = build_feature_document(
            {
                "family_id": family_id,
                "title": row["title_snapshot"],
                "department": metadata.get("department") or "",
                "team": metadata.get("team") or "",
                "description": proxy_text(
                    str(row["description_snapshot"]),
                    salary_evidence.get((str(row["ats"]), str(row["job_id"])), []),
                ),
                "template_cluster_id": row["template_cluster_id"] or family_id,
                "leakage_group_id": row["leakage_group_id"] or family_id,
            },
            subject_type="implicit_feedback",
            subject_id=subject_id,
        )
        weight = float(row["implicit_weight"])
        examples.append(
            TrainingExample(
                example_id=subject_id,
                document=document,
                target=1 if weight > 0 else 0,
                dataset_version="passive-feedback-v1",
                source_fingerprint=str(row["source_fingerprint"] or ""),
                selection_strategy=f"implicit_{row['action']}",
                sample_weight=abs(weight),
            )
        )
    return examples


def _usable_passive_feedback(
    examples: Sequence[TrainingExample]
) -> list[TrainingExample]:
    class_counts = Counter(example.target for example in examples)
    return (
        list(examples)
        if len(examples) >= 10 and class_counts[0] >= 3 and class_counts[1] >= 3
        else []
    )


def distill_students(
    source_db: Path,
    proxy_db: Path,
    state_db: Path,
    artifact_dir: Path,
    run_id: Optional[str] = None,
    encoder_kind: str = "sentence-transformers",
    model: str = DEFAULT_MODEL,
    model_revision: Optional[str] = None,
    device: str = "auto",
    min_training_labels: int = 200,
    score_corpus: bool = True,
    include_passive_feedback: bool = True,
    inference_config: Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Fit the two cheap retrieval policies to completed training-only teacher output."""
    prepare_schema(proxy_db)
    selected = run_id or active_run_id(proxy_db)
    by_policy = {
        policy_id: load_proxy_examples(proxy_db, selected, policy_id, "training")
        for policy_id in POLICY_IDS
    }
    passive = _usable_passive_feedback(
        load_passive_feedback_examples(source_db) if include_passive_feedback else []
    )
    if passive:
        passive_families = {example.document.family_id for example in passive}
        for policy_id in POLICY_IDS:
            by_policy[policy_id] = [
                example
                for example in by_policy[policy_id]
                if example.document.family_id not in passive_families
            ] + passive
    counts = {policy_id: len(values) for policy_id, values in by_policy.items()}
    if min(counts.values(), default=0) < min_training_labels:
        raise ProxyError(
            f"distillation needs {min_training_labels} completed training judgments; "
            f"found {min(counts.values(), default=0)}"
        )
    # One snapshot document can serve both policies; the labels differ, not the features.
    proxy_documents = list(
        {
            example.document.subject_id: example.document
            for example in by_policy[POLICY_IDS[0]]
        }.values()
    )
    encoder = make_encoder(
        encoder_kind,
        model,
        model_revision,
        device,
        inference_config,
        embedding_provider,
    )
    documents: Iterable[FeatureDocument] = proxy_documents
    if score_corpus:
        documents = itertools.chain(source_documents(source_db), proxy_documents)
    embedded = embed_documents(state_db, documents, encoder)
    students: dict[str, Any] = {}
    for policy_id in POLICY_IDS:
        trained = train_model(
            source_db,
            state_db,
            artifact_dir,
            min_training_labels,
            training_examples=by_policy[policy_id],
            training_source=f"proxy:{selected}:{policy_id}",
            auto_promote=False,
        )
        score = (
            score_model(source_db, state_db, trained["run_id"])
            if score_corpus
            else {"status": "not_scored"}
        )
        students[policy_id] = {
            "model_run_id": trained["run_id"],
            "training_examples": len(by_policy[policy_id]),
            "class_counts": trained["class_counts"],
            "out_of_fold": trained["out_of_fold"],
            "score": score,
        }
        with connect_proxy(proxy_db) as con:
            con.execute(
                "INSERT INTO proxy_students "
                "(run_id,policy_id,model_run_id,model_revision,training_examples,state_db,"
                "artifact_dir,score_json,trained_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,policy_id) DO UPDATE SET "
                "model_run_id=excluded.model_run_id,model_revision=excluded.model_revision,"
                "training_examples=excluded.training_examples,state_db=excluded.state_db,"
                "artifact_dir=excluded.artifact_dir,score_json=excluded.score_json,"
                "trained_at=excluded.trained_at",
                (
                    selected,
                    policy_id,
                    trained["run_id"],
                    encoder.model_revision,
                    len(by_policy[policy_id]),
                    str(state_db.resolve()),
                    str(artifact_dir.resolve()),
                    canonical_json(score),
                    now(),
                ),
            )
    return {
        "run_id": selected,
        "embed": embedded,
        "students": students,
        "passive_feedback_examples": len(passive),
    }


def audit_students(
    proxy_db: Path,
    state_db: Path,
    run_id: Optional[str] = None,
    device: str = "auto",
    inference_config: Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Measure student fidelity on the protected proxy audit after fitting is frozen."""
    prepare_schema(proxy_db)
    selected = run_id or active_run_id(proxy_db)
    with connect_proxy(proxy_db) as con:
        run = con.execute(
            "SELECT audit_size FROM proxy_runs WHERE run_id=?",
            (selected,),
        ).fetchone()
        students = {
            str(row["policy_id"]): dict(row)
            for row in con.execute(
                "SELECT * FROM proxy_students WHERE run_id=?",
                (selected,),
            )
        }
    if not run or set(students) != set(POLICY_IDS):
        raise ProxyError("fit both students before auditing")
    by_policy = {
        policy_id: load_proxy_examples(proxy_db, selected, policy_id, "audit")
        for policy_id in POLICY_IDS
    }
    found = min((len(values) for values in by_policy.values()), default=0)
    minimum_audit = int(run["audit_size"]) - max(
        5, math.ceil(int(run["audit_size"]) * 0.05)
    )
    if found < minimum_audit:
        raise ProxyError(
            f"audit needs at least {minimum_audit} valid judgments; found {found}"
        )
    revision = str(students[POLICY_IDS[0]]["model_revision"])
    if any(str(row["model_revision"]) != revision for row in students.values()):
        raise ProxyError("student embedding revisions disagree")
    encoder = encoder_for_recorded_revision(
        revision,
        device,
        inference_config,
        embedding_provider,
    )
    audit_documents = [example.document for example in by_policy[POLICY_IDS[0]]]
    embedded = embed_documents(
        state_db,
        audit_documents,
        encoder,
        activate_revision=False,
    )
    reports: dict[str, Any] = {}
    for policy_id in POLICY_IDS:
        examples = by_policy[policy_id]
        predictions = predict_documents(
            state_db,
            str(students[policy_id]["model_run_id"]),
            [example.document for example in examples],
        )
        targets = [example.target for example in examples]
        scores = [float(row["final"]) for row in predictions]
        report = {
            "count": len(examples),
            "class_counts": {
                "interested": sum(targets),
                "not_interested": len(targets) - sum(targets),
            },
            "metrics": metric_summary(targets, scores),
            "agreement_at_0_5": round(
                sum(
                    (score >= 0.5) == bool(target)
                    for score, target in zip(scores, targets)
                )
                / len(targets),
                6,
            ),
        }
        reports[policy_id] = report
        with connect_proxy(proxy_db) as con:
            con.execute(
                "INSERT INTO proxy_student_audits VALUES (?,?,?,?,?) "
                "ON CONFLICT(run_id,policy_id,model_run_id) DO UPDATE SET "
                "audit_json=excluded.audit_json,evaluated_at=excluded.evaluated_at",
                (
                    selected,
                    policy_id,
                    students[policy_id]["model_run_id"],
                    canonical_json(report),
                    now(),
                ),
            )
    return {"run_id": selected, "embed": embedded, "policies": reports}


def estimate(
    proxy_db: Path,
    split: Optional[str] = None,
    run_id: Optional[str] = None,
    *,
    remote_inference: bool = False,
) -> dict[str, Any]:
    selected = run_id or active_run_id(proxy_db)
    where = " AND split=?" if split else ""
    params: tuple[Any, ...] = (selected, split) if split else (selected,)
    with connect_proxy(proxy_db) as con:
        pending = int(
            con.execute(
                "SELECT COUNT(*) FROM proxy_queue WHERE run_id=? AND status='pending'"
                + where,
                params,
            ).fetchone()[0]
        )
        characters = int(
            con.execute(
                "SELECT COALESCE(SUM(LENGTH(semantic_text)+LENGTH(title_snapshot)),0) "
                "FROM proxy_queue WHERE run_id=? AND status='pending'" + where,
                params,
            ).fetchone()[0]
        )
    seconds = pending * MEASURED_SECONDS_PER_JOB
    return {
        "run_id": selected,
        "split": split or "all",
        "pending_jobs": pending,
        "estimated_input_tokens": math.ceil(characters / 4),
        "measured_seconds_per_job": MEASURED_SECONDS_PER_JOB,
        "estimated_hours": round(seconds / 3600, 2),
        # Remote price depends on endpoint hardware, worker time, and current
        # provider pricing. No such price is asserted by inference profile v1.
        "marginal_cost_usd": None if remote_inference else 0.0,
    }


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("prepare", "estimate", "run", "retry", "distill", "audit", "status"),
    )
    parser.add_argument("--db", type=_path, default=DEFAULT_DB)
    parser.add_argument("--proxy-db", type=_path)
    parser.add_argument("--profile", type=_path, default=DEFAULT_PROFILE)
    parser.add_argument("--run-id")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--audit-size", type=int, default=DEFAULT_AUDIT_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--teacher-model",
        help=(
            "local teacher alias; remote runs use the configured generation identity; "
            "defaults to qwen3.8-27b"
        ),
    )
    parser.add_argument(
        "--inference-config",
        type=_path,
        help=(
            "owner-only portable inference profile; defaults to "
            "JOB_SEARCH_INFERENCE_CONFIG"
        ),
    )
    parser.add_argument("--split", choices=("training", "audit"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--until-empty", action="store_true")
    parser.add_argument("--include-excluded", action="store_true")
    parser.add_argument("--state-db", type=_path)
    parser.add_argument("--artifacts", type=_path)
    parser.add_argument(
        "--encoder",
        choices=("sentence-transformers", "hashing", "remote"),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-revision")
    parser.add_argument(
        "--device", default="auto", choices=("auto", "cpu", "mps", "cuda")
    )
    parser.add_argument("--min-training-labels", type=int, default=200)
    parser.add_argument("--no-score", action="store_true")
    parser.add_argument("--no-passive-feedback", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.inference_config = configured_inference_path(args.inference_config)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    args.encoder = args.encoder or (
        "remote" if args.inference_config is not None else "sentence-transformers"
    )
    proxy_db = (args.proxy_db or default_proxy_db(args.db)).resolve()
    state_db = (args.state_db or default_state_db(args.db)).resolve()
    artifact_dir = (args.artifacts or default_artifact_dir(args.db)).resolve()
    try:
        if proxy_db == args.db.resolve():
            raise ProxyError("--proxy-db must differ from --db")
        if args.command == "prepare":
            teacher_model = args.teacher_model or DEFAULT_TEACHER
            if args.inference_config is not None:
                configured_teacher = str(
                    build_structured_provider(
                        load_inference_config(args.inference_config)
                    ).generation_identity
                )
                if (
                    args.teacher_model is not None
                    and args.teacher_model != configured_teacher
                ):
                    raise ProxyError(
                        "--teacher-model must match the configured remote generation identity"
                    )
                teacher_model = configured_teacher
            result = prepare_run(
                args.db.resolve(),
                proxy_db,
                args.profile.resolve(),
                args.sample_size,
                args.audit_size,
                args.seed,
                teacher_model,
            )
        elif args.command == "estimate":
            result = estimate(
                proxy_db,
                args.split,
                args.run_id,
                remote_inference=args.inference_config is not None,
            )
        elif args.command == "run":
            limit = args.limit
            if args.until_empty:
                limit = max(
                    1,
                    estimate(proxy_db, args.split or "training", args.run_id)[
                        "pending_jobs"
                    ],
                )
            result = run_teacher(
                proxy_db,
                args.profile.resolve(),
                args.split or "training",
                limit,
                args.run_id,
                inference_config=args.inference_config,
            )
        elif args.command == "retry":
            result = retry_failed(
                proxy_db,
                args.split,
                args.run_id,
                args.include_excluded,
            )
        elif args.command == "distill":
            result = distill_students(
                args.db.resolve(),
                proxy_db,
                state_db,
                artifact_dir,
                args.run_id,
                args.encoder,
                args.model,
                args.model_revision,
                args.device,
                args.min_training_labels,
                not args.no_score,
                not args.no_passive_feedback,
                args.inference_config,
            )
        elif args.command == "audit":
            result = audit_students(
                proxy_db,
                state_db,
                args.run_id,
                args.device,
                args.inference_config,
            )
        else:
            result = status(proxy_db, args.run_id)
    except InferenceTransportError as exc:
        if getattr(exc, "defer_without_attempt", False):
            print(json.dumps({"type": "inference_usage_deferred", "reason": exc.reason_code, "retry_at": exc.retry_at}), file=sys.stderr)
            return 76
        print(f"error: {exc}")
        return 75 if exc.retryable else 78
    except (ProxyError, PreferenceModelError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
