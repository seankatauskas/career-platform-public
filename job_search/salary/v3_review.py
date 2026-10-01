#!/usr/bin/env python3
"""Local reviewer for version-3 salary bounds and one-sided audit items."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from job_search.salary.v3 import (
    AUDIT_BATCH,
    DEFAULT_BATCH,
    connect,
    now,
    prepare_schema,
    status,
    validate_result,
)


ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = Path(__file__).resolve().parent / "web/review"
MAX_BODY_BYTES = 128 * 1024
SOURCES = {"fireworks", "openai", "human"}


class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _prediction_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["result"] = json.loads(result.pop("result_json")) if result["result_json"] else None
    result["validation"] = json.loads(result.pop("validation_json") or "[]")
    return result


def get_item(
    db_path: Path, batch: str = DEFAULT_BATCH, queue_id: int | None = None,
) -> dict[str, Any] | None:
    with connect(db_path) as con:
        base = (
            "SELECT q.*,j.company,j.title,j.location,j.employmentType,j.publishedAt,"
            "j.jobUrl,j.description,g.result_json AS gold_result_json,g.chosen_source,"
            "g.note,g.updated_at FROM salary_v3_queue q JOIN jobs j "
            "ON j.ats=q.ats AND j.id=q.job_id LEFT JOIN salary_v3_gold_labels g "
            "ON g.queue_id=q.id "
        )
        if queue_id is None:
            row = con.execute(
                base + "WHERE q.batch=? AND g.queue_id IS NULL AND "
                "(q.split='audit' OR EXISTS (SELECT 1 FROM salary_v3_predictions p "
                "WHERE p.queue_id=q.id AND p.result_json IS NOT NULL)) "
                "ORDER BY q.position LIMIT 1", (batch,),
            ).fetchone()
        else:
            row = con.execute(base + "WHERE q.batch=? AND q.id=?", (batch, queue_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        raw_gold = item.pop("gold_result_json")
        raw_legacy = item.pop("legacy_result_json")
        item["gold_result"] = json.loads(raw_gold) if raw_gold else None
        item["legacy_result"] = json.loads(raw_legacy) if raw_legacy else None
        item["schema_version"] = "cash-pay-bounds-v3"
        item["predictions"] = {
            prediction["provider"]: prediction
            for prediction in (
                _prediction_dict(prediction_row)
                for prediction_row in con.execute(
                    "SELECT provider,model,status,result_json,validation_json,input_tokens,"
                    "output_tokens,cost_usd,error FROM salary_v3_predictions "
                    "WHERE queue_id=? ORDER BY provider", (item["id"],),
                )
            )
        }
        return item


def review_stats(db_path: Path, batch: str = DEFAULT_BATCH) -> dict[str, Any]:
    result = status(db_path, batch)
    with connect(db_path) as con:
        result["ready_to_review"] = int(con.execute(
            "SELECT COUNT(*) FROM salary_v3_queue q WHERE q.batch=? AND NOT EXISTS "
            "(SELECT 1 FROM salary_v3_gold_labels g WHERE g.queue_id=q.id) AND "
            "(q.split='audit' OR EXISTS (SELECT 1 FROM salary_v3_predictions p "
            "WHERE p.queue_id=q.id AND p.result_json IS NOT NULL))", (batch,),
        ).fetchone()[0])
        result["chosen_sources"] = dict(con.execute(
            "SELECT g.chosen_source,COUNT(*) FROM salary_v3_gold_labels g "
            "JOIN salary_v3_queue q ON q.id=g.queue_id WHERE q.batch=? "
            "GROUP BY g.chosen_source", (batch,),
        ).fetchall())
    return result


def save_gold(
    db_path: Path, batch: str, queue_id: int, source: str,
    result: dict[str, Any] | None, note: str = "",
) -> None:
    if source not in SOURCES:
        raise ApiError("source must be fireworks, openai, or human")
    with connect(db_path) as con:
        job = con.execute(
            "SELECT j.description FROM salary_v3_queue q JOIN jobs j "
            "ON j.ats=q.ats AND j.id=q.job_id WHERE q.batch=? AND q.id=?",
            (batch, queue_id),
        ).fetchone()
        if not job:
            raise ApiError("queue item not found", 404)
        if result is None:
            prediction = con.execute(
                "SELECT result_json FROM salary_v3_predictions WHERE queue_id=? "
                "AND provider=? AND result_json IS NOT NULL", (queue_id, source),
            ).fetchone()
            if not prediction:
                raise ApiError(f"no {source} result exists for this job")
            result = json.loads(prediction[0])
        if not isinstance(result, dict):
            raise ApiError("result must be a JSON object")
        problems = validate_result(result, job["description"])
        if problems:
            raise ApiError("cannot save invalid v3 gold label: " + "; ".join(problems))
        encoded = json.dumps(result, separators=(",", ":"))
        previous = con.execute(
            "SELECT result_json,chosen_source,note FROM salary_v3_gold_labels WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        con.execute(
            "INSERT INTO salary_v3_gold_events "
            "(queue_id,previous_result_json,previous_chosen_source,previous_note,happened_at) "
            "VALUES (?,?,?,?,?)",
            (
                queue_id, previous["result_json"] if previous else None,
                previous["chosen_source"] if previous else None,
                previous["note"] if previous else None, now(),
            ),
        )
        timestamp = now()
        con.execute(
            "INSERT INTO salary_v3_gold_labels "
            "(queue_id,result_json,chosen_source,note,reviewed_at,updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(queue_id) DO UPDATE SET "
            "result_json=excluded.result_json,chosen_source=excluded.chosen_source,"
            "note=excluded.note,updated_at=excluded.updated_at",
            (queue_id, encoded, source, note[:4000], timestamp, timestamp),
        )


def undo_last(db_path: Path, batch: str) -> int | None:
    with connect(db_path) as con:
        event = con.execute(
            "SELECT e.* FROM salary_v3_gold_events e JOIN salary_v3_queue q "
            "ON q.id=e.queue_id WHERE q.batch=? ORDER BY e.id DESC LIMIT 1", (batch,),
        ).fetchone()
        if not event:
            return None
        if event["previous_result_json"] is None:
            con.execute("DELETE FROM salary_v3_gold_labels WHERE queue_id=?", (event["queue_id"],))
        else:
            con.execute(
                "UPDATE salary_v3_gold_labels SET result_json=?,chosen_source=?,note=?,updated_at=? "
                "WHERE queue_id=?",
                (
                    event["previous_result_json"], event["previous_chosen_source"],
                    event["previous_note"], now(), event["queue_id"],
                ),
            )
        con.execute("DELETE FROM salary_v3_gold_events WHERE id=?", (event["id"],))
        return int(event["queue_id"])


def _handler(db_path: Path, batch: str):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: Any, status_code: int = 200) -> None:
            data = json.dumps(value).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiError("invalid Content-Length") from exc
            if length < 1 or length > MAX_BODY_BYTES:
                raise ApiError("invalid request size")
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ApiError("invalid JSON") from exc
            if not isinstance(body, dict):
                raise ApiError("request body must be an object")
            return body

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/item":
                    query = parse_qs(parsed.query)
                    queue_id = int(query["id"][0]) if "id" in query else None
                    self._json({
                        "item": get_item(db_path, batch, queue_id),
                        "stats": review_stats(db_path, batch),
                    })
                    return
                if parsed.path == "/api/stats":
                    self._json(review_stats(db_path, batch))
                    return
                relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
                target = (ASSET_DIR / relative).resolve()
                if ASSET_DIR.resolve() not in target.parents and target != ASSET_DIR.resolve():
                    raise ApiError("not found", 404)
                if not target.is_file():
                    raise ApiError("not found", 404)
                data = target.read_bytes()
                self.send_response(200)
                self.send_header(
                    "Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (ApiError, ValueError) as exc:
                self._json({"error": str(exc)}, getattr(exc, "status_code", 400))

        def do_POST(self) -> None:
            try:
                if self.path == "/api/label":
                    body = self._body()
                    save_gold(
                        db_path, batch, int(body.get("queue_id")), str(body.get("source", "")),
                        body.get("result"), str(body.get("note", "")),
                    )
                    self._json({
                        "ok": True, "item": get_item(db_path, batch),
                        "stats": review_stats(db_path, batch),
                    })
                elif self.path == "/api/undo":
                    queue_id = undo_last(db_path, batch)
                    self._json({
                        "ok": queue_id is not None,
                        "item": get_item(db_path, batch, queue_id) if queue_id else get_item(db_path, batch),
                        "stats": review_stats(db_path, batch),
                    })
                else:
                    raise ApiError("not found", 404)
            except (ApiError, ValueError, TypeError, sqlite3.Error) as exc:
                self._json({"error": str(exc)}, getattr(exc, "status_code", 400))

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="job-boards.db")
    parser.add_argument("--batch", default=DEFAULT_BATCH)
    parser.add_argument("--audit-v2", action="store_true", help="review the v2 one-sided audit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    batch = AUDIT_BATCH if args.audit_v2 else args.batch
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    try:
        prepare_schema(db_path)
    except ValueError as exc:
        raise SystemExit(str(exc))
    server = ThreadingHTTPServer((args.host, args.port), _handler(db_path, batch))
    print(f"salary v3 review ({batch}): {review_stats(db_path, batch)['ready_to_review']} ready")
    print(f"open http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
