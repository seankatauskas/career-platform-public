#!/usr/bin/env python3
"""Export a self-contained salary labeling batch to an immutable local JSONL file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_VERSION = 1
JSON_COLUMNS = {"result_json", "validation_json", "response_meta_json"}


def _decoded(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for column in JSON_COLUMNS & value.keys():
        raw = value[column]
        value[column.removesuffix("_json")] = json.loads(raw) if raw else None
        del value[column]
    return value


def archive_batch(db_path: Path, output_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    if output_path.exists():
        raise ValueError(f"archive already exists and will not be overwritten: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".partial")
    if temporary.exists():
        raise ValueError(f"partial archive already exists: {temporary}")

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        required = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('jobs','salary_labeling_queue','salary_model_predictions','salary_gold_labels')"
            )
        }
        missing = {
            "jobs", "salary_labeling_queue", "salary_model_predictions", "salary_gold_labels",
        } - required
        if missing:
            raise ValueError("database is missing: " + ", ".join(sorted(missing)))
        queue_rows = list(con.execute(
            "SELECT q.*,j.* FROM salary_labeling_queue q "
            "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id ORDER BY q.position"
        ))
        predictions: dict[int, list[dict[str, Any]]] = {}
        for row in con.execute(
            "SELECT * FROM salary_model_predictions ORDER BY queue_id,provider"
        ):
            predictions.setdefault(int(row["queue_id"]), []).append(_decoded(row))
        gold = {
            int(row["queue_id"]): _decoded(row)
            for row in con.execute("SELECT * FROM salary_gold_labels ORDER BY queue_id")
        }
        provider_counts = {
            f"{row['provider']}:{row['status']}": int(row["count"])
            for row in con.execute(
                "SELECT provider,status,COUNT(*) AS count FROM salary_model_predictions "
                "GROUP BY provider,status"
            )
        }

    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "type": "salary_batch_manifest",
        "archive_version": ARCHIVE_VERSION,
        "exported_at": exported_at,
        "queue_count": len(queue_rows),
        "prediction_count": sum(len(rows) for rows in predictions.values()),
        "gold_count": len(gold),
        "prediction_statuses": provider_counts,
    }
    digest = hashlib.sha256()
    record_count = 0
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            for value in (manifest, *(
                {
                    "type": "salary_batch_item",
                    "queue": {
                        key: row[key]
                        for key in (
                            "id", "ats", "job_id", "position", "stratum",
                            "sampling_weight", "seed", "split", "created_at",
                        )
                    },
                    "job": {
                        key: row[key]
                        for key in row.keys()
                        if key not in {
                            "id", "job_id", "position", "stratum", "sampling_weight",
                            "seed", "split", "created_at",
                        }
                    } | {"id": row["job_id"]},
                    "predictions": predictions.get(int(row["id"]), []),
                    "gold": gold.get(int(row["id"])),
                }
                for row in queue_rows
            )):
                encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                handle.write(encoded)
                digest.update(encoded.encode())
                record_count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return {
        **manifest,
        "records": record_count,
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="job-boards.db")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    db_path = Path(args.db)
    output_path = Path(args.out)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    try:
        print(json.dumps(archive_batch(db_path, output_path), indent=2))
    except (ValueError, sqlite3.Error, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
