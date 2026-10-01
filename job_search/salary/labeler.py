#!/usr/bin/env python3
"""Local UI for auditing salary extraction, including parser-negative jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = Path(__file__).resolve().parent / "web/labeler"
MAX_BODY_BYTES = 64 * 1024
POSITIVE_VERDICTS = {"correct", "incorrect", "unclear", "non_salaried_pay"}
NEGATIVE_VERDICTS = {"no_salary", "missed_salary", "unclear", "non_salaried_pay"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS salary_validation_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ats              TEXT NOT NULL,
    job_id           TEXT NOT NULL,
    audit_type       TEXT NOT NULL CHECK (audit_type IN ('positive','negative')),
    stratum          TEXT NOT NULL,
    sampling_weight  REAL NOT NULL,
    position         INTEGER NOT NULL,
    seed             INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (ats, job_id)
);
CREATE INDEX IF NOT EXISTS salary_validation_queue_position
ON salary_validation_queue(position);

CREATE TABLE IF NOT EXISTS salary_validation_labels (
    ats          TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    labeled_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (ats, job_id)
);
CREATE INDEX IF NOT EXISTS salary_validation_labels_verdict
ON salary_validation_labels(verdict, updated_at);

CREATE TABLE IF NOT EXISTS salary_validation_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ats               TEXT NOT NULL,
    job_id            TEXT NOT NULL,
    previous_verdict  TEXT,
    previous_note     TEXT,
    happened_at       TEXT NOT NULL
);
"""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def prepare_validation(db_path: Path) -> None:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    with connect(db_path) as con:
        required = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('jobs','job_enrichment','job_compensation_ranges')"
        )}
        missing = {"jobs", "job_enrichment", "job_compensation_ranges"} - required
        if missing:
            raise ValueError(
                "database needs salary enrichment first; missing " + ", ".join(sorted(missing))
            )
        con.executescript(SCHEMA)


def _risk_stratum(description: str) -> str:
    lower = (description or "").lower()
    has_money = any(token in lower for token in ("$", "€", "£", " usd", " cad", " eur", " gbp"))
    has_pay = any(token in lower for token in (
        "salary", "pay range", "base pay", "compensation range", "annually", "per year"
    ))
    return "high" if has_money and has_pay else ("medium" if has_money or has_pay else "low")


def _stable_shuffle(rows: list[Any], seed: int, stratum: str) -> None:
    digest = int(hashlib.sha256(stratum.encode()).hexdigest()[:12], 16)
    random.Random(seed ^ digest).shuffle(rows)


def _sample_stratified(
    candidates: list[dict[str, Any]], quota: int, seed: int, company_cap: int
) -> list[dict[str, Any]]:
    """Sample strata, then companies, with inverse-probability-style weights."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        groups[item["stratum"]].append(item)
    company_groups: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}
    capacity: dict[str, int] = {}
    for stratum, rows in groups.items():
        companies: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            companies[(row["ats"], row["company"].lower())].append(row)
        company_groups[stratum] = companies
        capacity[stratum] = sum(min(len(rows), company_cap) for rows in companies.values())

    # Equal round-robin allocation across strata. Weights below restore population
    # proportions; the balanced queue improves the odds of seeing rare failure modes.
    targets = {key: 0 for key in groups}
    active = sorted(groups)
    while sum(targets.values()) < quota and active:
        for key in list(active):
            if targets[key] >= capacity[key]:
                active.remove(key)
                continue
            targets[key] += 1
            if sum(targets.values()) == quota:
                break

    selected: list[dict[str, Any]] = []
    for stratum in sorted(groups):
        if targets[stratum] == 0:
            continue
        companies = company_groups[stratum]
        company_keys = sorted(companies)
        _stable_shuffle(company_keys, seed, stratum + "|companies")
        for company_key, rows in companies.items():
            _stable_shuffle(rows, seed, stratum + "|" + "|".join(company_key))

        chosen: list[dict[str, Any]] = []
        for round_number in range(company_cap):
            eligible = [key for key in company_keys if len(companies[key]) > round_number]
            _stable_shuffle(eligible, seed + round_number, stratum + "|round")
            for company_key in eligible:
                chosen.append(companies[company_key][round_number])
                if len(chosen) == targets[stratum]:
                    break
            if len(chosen) == targets[stratum]:
                break

        picked_by_company = Counter(
            (item["ats"], item["company"].lower()) for item in chosen
        )
        sampled_companies = len(picked_by_company)
        company_probability_inverse = (
            len(companies) / sampled_companies
            if sampled_companies and sampled_companies < len(companies) else 1.0
        )
        for item in chosen:
            company_key = (item["ats"], item["company"].lower())
            item["sampling_weight"] = (
                company_probability_inverse
                * len(companies[company_key])
                / picked_by_company[company_key]
            )
        selected.extend(chosen)
    return selected


def build_queue(
    db_path: Path,
    positive_quota: int = 300,
    negative_quota: int = 200,
    seed: int = 20260830,
    company_cap: int = 3,
    rebuild: bool = False,
) -> int:
    """Build a deterministic positive/negative validation sample."""
    prepare_validation(db_path)
    with connect(db_path) as con:
        existing = con.execute("SELECT COUNT(*) FROM salary_validation_queue").fetchone()[0]
        if existing and not rebuild:
            return int(existing)
        if rebuild:
            con.execute("DELETE FROM salary_validation_events")
            con.execute("DELETE FROM salary_validation_labels")
            con.execute("DELETE FROM salary_validation_queue")

        positives = [dict(row) for row in con.execute(
            "SELECT j.ats,j.id AS job_id,j.company,e.preferred_source,"
            "COALESCE(MIN(r.currency),'UNKNOWN') AS currency,"
            "COALESCE(MIN(r.component),'unknown') AS component "
            "FROM jobs j JOIN job_enrichment e ON e.ats=j.ats AND e.job_id=j.id "
            "JOIN job_compensation_ranges r ON r.ats=j.ats AND r.job_id=j.id "
            "AND r.removed_at IS NULL AND r.is_preferred=1 "
            "WHERE e.status='complete' AND e.preferred_source IN "
            "('description_rule','ats_rendered') "
            "GROUP BY j.ats,j.id"
        )]
        for item in positives:
            item["audit_type"] = "positive"
            item["stratum"] = "|".join((
                "positive", item["ats"], item["preferred_source"],
                item["currency"], item["component"],
            ))

        negatives = [dict(row) for row in con.execute(
            "SELECT j.ats,j.id AS job_id,j.company,j.description "
            "FROM jobs j JOIN job_enrichment e ON e.ats=j.ats AND e.job_id=j.id "
            "WHERE e.status IN ('no_compensation','non_salary_compensation')"
        )]
        for item in negatives:
            item["audit_type"] = "negative"
            item["stratum"] = "|".join(("negative", item["ats"], _risk_stratum(item["description"])))

        selected = _sample_stratified(positives, positive_quota, seed, company_cap)
        selected += _sample_stratified(negatives, negative_quota, seed + 1, company_cap)
        random.Random(seed).shuffle(selected)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con.executemany(
            "INSERT INTO salary_validation_queue "
            "(ats,job_id,audit_type,stratum,sampling_weight,position,seed,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    item["ats"], item["job_id"], item["audit_type"], item["stratum"],
                    item["sampling_weight"], position, seed, now,
                )
                for position, item in enumerate(selected, 1)
            ],
        )
        return len(selected)


def _range_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    try:
        result["evidence"] = json.loads(result.pop("evidence_json"))
    except (json.JSONDecodeError, TypeError):
        result["evidence"] = []
    return result


def get_item(db_path: Path, queue_id: int | None = None) -> dict[str, Any] | None:
    with connect(db_path) as con:
        if queue_id is None:
            row = con.execute(
                "SELECT q.*,j.company,j.title,j.location,j.publishedAt,j.jobUrl,j.description,"
                "l.verdict,l.note,l.updated_at FROM salary_validation_queue q "
                "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
                "LEFT JOIN salary_validation_labels l ON l.ats=q.ats AND l.job_id=q.job_id "
                "WHERE l.job_id IS NULL ORDER BY q.position LIMIT 1"
            ).fetchone()
        else:
            row = con.execute(
                "SELECT q.*,j.company,j.title,j.location,j.publishedAt,j.jobUrl,j.description,"
                "l.verdict,l.note,l.updated_at FROM salary_validation_queue q "
                "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
                "LEFT JOIN salary_validation_labels l ON l.ats=q.ats AND l.job_id=q.job_id "
                "WHERE q.id=?",
                (queue_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["ranges"] = [_range_dict(r) for r in con.execute(
            "SELECT component,value_kind,currency,period,min_value,max_value,"
            "annual_min_value,annual_max_value,location_scope,source_type,source_field,"
            "evidence_text,evidence_json,parser_rule,is_preferred,is_corroborated "
            "FROM job_compensation_ranges WHERE ats=? AND job_id=? AND removed_at IS NULL "
            "ORDER BY is_preferred DESC,id",
            (item["ats"], item["job_id"]),
        )]
        return item


def stats(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as con:
        total = con.execute("SELECT COUNT(*) FROM salary_validation_queue").fetchone()[0]
        labeled = con.execute("SELECT COUNT(*) FROM salary_validation_labels").fetchone()[0]
        verdicts = {row[0]: row[1] for row in con.execute(
            "SELECT verdict,COUNT(*) FROM salary_validation_labels GROUP BY verdict"
        )}
        weighted = {row[0]: row[1] for row in con.execute(
            "SELECT l.verdict,ROUND(SUM(q.sampling_weight),2) "
            "FROM salary_validation_labels l JOIN salary_validation_queue q "
            "ON q.ats=l.ats AND q.job_id=l.job_id GROUP BY l.verdict"
        )}
        audits = {
            row[0]: {"total": row[1], "labeled": row[2]}
            for row in con.execute(
                "SELECT q.audit_type,COUNT(*),COUNT(l.job_id) "
                "FROM salary_validation_queue q LEFT JOIN salary_validation_labels l "
                "ON l.ats=q.ats AND l.job_id=q.job_id GROUP BY q.audit_type"
            )
        }
        weighted_rows = list(con.execute(
            "SELECT q.audit_type,l.verdict,q.sampling_weight "
            "FROM salary_validation_labels l JOIN salary_validation_queue q "
            "ON q.ats=l.ats AND q.job_id=l.job_id"
        ))
        positive_correct = sum(
            row["sampling_weight"] for row in weighted_rows
            if row["audit_type"] == "positive" and row["verdict"] == "correct"
        )
        positive_wrong = sum(
            row["sampling_weight"] for row in weighted_rows
            if row["audit_type"] == "positive" and row["verdict"] == "incorrect"
        )
        negative_missed = sum(
            row["sampling_weight"] for row in weighted_rows
            if row["audit_type"] == "negative" and row["verdict"] == "missed_salary"
        )
        negative_clean = sum(
            row["sampling_weight"] for row in weighted_rows
            if row["audit_type"] == "negative" and row["verdict"] == "no_salary"
        )

        def estimate_detail(audit_type: str, positive: str, negative: str) -> dict[str, Any] | None:
            observations = [
                (row["sampling_weight"], row["verdict"] == positive)
                for row in weighted_rows
                if row["audit_type"] == audit_type
                and row["verdict"] in {positive, negative}
            ]
            if not observations:
                return None
            weight_sum = sum(weight for weight, _ in observations)
            estimate = sum(weight for weight, outcome in observations if outcome) / weight_sum
            effective_n = weight_sum * weight_sum / sum(weight * weight for weight, _ in observations)
            # Wilson interval using Kish effective sample size. This is an estimate,
            # not a substitute for reviewing enough examples in every stratum.
            z = 1.959963984540054
            denominator = 1 + z * z / effective_n
            center = (estimate + z * z / (2 * effective_n)) / denominator
            margin = z * math.sqrt(
                estimate * (1 - estimate) / effective_n
                + z * z / (4 * effective_n * effective_n)
            ) / denominator
            return {
                "estimate": estimate,
                "ci95": [max(0.0, center - margin), min(1.0, center + margin)],
                "effective_n": effective_n,
            }
        return {
            "total": total, "labeled": labeled, "remaining": total - labeled,
            "verdicts": verdicts, "weighted_verdicts": weighted, "audits": audits,
            "estimates": {
                "positive_precision": (
                    positive_correct / (positive_correct + positive_wrong)
                    if positive_correct + positive_wrong else None
                ),
                "negative_miss_rate": (
                    negative_missed / (negative_missed + negative_clean)
                    if negative_missed + negative_clean else None
                ),
            },
            "estimate_details": {
                "positive_precision": estimate_detail(
                    "positive", "correct", "incorrect"
                ),
                "negative_miss_rate": estimate_detail(
                    "negative", "missed_salary", "no_salary"
                ),
            },
        }


def label_item(db_path: Path, queue_id: int, verdict: str, note: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as con:
        item = con.execute(
            "SELECT ats,job_id,audit_type FROM salary_validation_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not item:
            raise ApiError("unknown queue item", 404)
        allowed = POSITIVE_VERDICTS if item["audit_type"] == "positive" else NEGATIVE_VERDICTS
        if verdict not in allowed:
            raise ApiError(f"invalid verdict for {item['audit_type']} audit")
        previous = con.execute(
            "SELECT verdict,note FROM salary_validation_labels WHERE ats=? AND job_id=?",
            (item["ats"], item["job_id"]),
        ).fetchone()
        con.execute(
            "INSERT INTO salary_validation_events "
            "(ats,job_id,previous_verdict,previous_note,happened_at) VALUES (?,?,?,?,?)",
            (
                item["ats"], item["job_id"], previous["verdict"] if previous else None,
                previous["note"] if previous else None, now,
            ),
        )
        con.execute(
            "INSERT INTO salary_validation_labels "
            "(ats,job_id,verdict,note,labeled_at,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(ats,job_id) DO UPDATE SET verdict=excluded.verdict,"
            "note=excluded.note,updated_at=excluded.updated_at",
            (item["ats"], item["job_id"], verdict, note.strip()[:2000], now, now),
        )


def undo_last(db_path: Path) -> dict[str, Any] | None:
    with connect(db_path) as con:
        event = con.execute(
            "SELECT * FROM salary_validation_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not event:
            return None
        if event["previous_verdict"] is None:
            con.execute(
                "DELETE FROM salary_validation_labels WHERE ats=? AND job_id=?",
                (event["ats"], event["job_id"]),
            )
        else:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            con.execute(
                "UPDATE salary_validation_labels SET verdict=?,note=?,updated_at=? "
                "WHERE ats=? AND job_id=?",
                (
                    event["previous_verdict"], event["previous_note"] or "", now,
                    event["ats"], event["job_id"],
                ),
            )
        queue = con.execute(
            "SELECT id FROM salary_validation_queue WHERE ats=? AND job_id=?",
            (event["ats"], event["job_id"]),
        ).fetchone()
        con.execute("DELETE FROM salary_validation_events WHERE id=?", (event["id"],))
        return {"queue_id": queue["id"] if queue else None}


def _handler(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: Any, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _asset(self, path: str) -> None:
            name = "index.html" if path == "/" else path.lstrip("/")
            target = (ASSET_DIR / name).resolve()
            if ASSET_DIR.resolve() not in target.parents and target != ASSET_DIR.resolve():
                raise ApiError("not found", 404)
            if not target.is_file():
                raise ApiError("not found", 404)
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            # This is a local development UI. Never let a browser keep an older
            # shortcut handler after the Python process has been restarted.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ApiError("invalid content length")
            if length > MAX_BODY_BYTES:
                raise ApiError("request too large", 413)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ApiError("invalid JSON")

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/item":
                    raw = parse_qs(parsed.query).get("id", [None])[0]
                    self._json({"item": get_item(db_path, int(raw) if raw else None)})
                elif parsed.path == "/api/stats":
                    self._json(stats(db_path))
                else:
                    self._asset(parsed.path)
            except ApiError as exc:
                self._json({"error": str(exc)}, exc.status)
            except (ValueError, sqlite3.Error) as exc:
                self._json({"error": str(exc)}, 400)

        def do_POST(self) -> None:  # noqa: N802
            try:
                body = self._body()
                if self.path == "/api/label":
                    label_item(
                        db_path, int(body.get("queue_id", 0)),
                        str(body.get("verdict", "")), str(body.get("note", "")),
                    )
                    self._json({"ok": True, "item": get_item(db_path), "stats": stats(db_path)})
                elif self.path == "/api/undo":
                    undone = undo_last(db_path)
                    item = get_item(db_path, undone["queue_id"]) if undone else get_item(db_path)
                    self._json({"ok": bool(undone), "item": item, "stats": stats(db_path)})
                else:
                    raise ApiError("not found", 404)
            except ApiError as exc:
                self._json({"error": str(exc)}, exc.status)
            except (ValueError, sqlite3.Error) as exc:
                self._json({"error": str(exc)}, 400)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="job-boards.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--positive", type=int, default=300)
    parser.add_argument("--negative", type=int, default=200)
    parser.add_argument("--company-cap", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument(
        "--rebuild-queue", action="store_true",
        help="replace the queue and its existing validation labels",
    )
    args = parser.parse_args()
    db = Path(args.db)
    if not db.is_absolute():
        db = ROOT / db
    try:
        count = build_queue(
            db, args.positive, args.negative, args.seed, args.company_cap,
            args.rebuild_queue,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    server = ThreadingHTTPServer((args.host, args.port), _handler(db))
    print(f"salary validation queue: {count} jobs")
    print(f"open http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
