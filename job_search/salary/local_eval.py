#!/usr/bin/env python3
"""Run and score local MLX salary extractors against the reviewed gold set."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_search.salary.teacher import (
    RESULT_SCHEMA as V2_RESULT_SCHEMA,
    SYSTEM_PROMPT as V2_SYSTEM_PROMPT,
    _validate_result as validate_v2,
)
from job_search.salary.model_contract import (
    PROMPT as SHARED_SIMPLE_V3_PROMPT,
    RESULT_SCHEMA as SHARED_SIMPLE_V3_SCHEMA,
    TEMPLATE as SHARED_NUEXTRACT_TEMPLATE_V3_SIMPLE,
    normalize_result as normalize_simple_v3,
    validate_result as validate_simple_v3,
)
from job_search.salary.training_target import (
    canonical_figure_key_v3_simple,
    canonical_key,
    canonical_key_v3,
    usd_primary_match,
    usd_primary_match_v3,
    usd_primary_match_v3_simple,
)
from job_search.salary.v3 import (
    DEFAULT_BATCH as V3_DEFAULT_BATCH,
    RESULT_SCHEMA as V3_RESULT_SCHEMA,
    SYSTEM_PROMPT as V3_SYSTEM_PROMPT,
    validate_result as validate_v3,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "job-boards.db"
OUTPUT_DIR = ROOT / ".local-evals"

MODELS = {
    "nuextract3-4b": {
        "path": ROOT / ".models/numind--NuExtract3-mlx-4bits",
        "repo": "numind/NuExtract3-mlx-4bits",
        "kind": "nuextract",
        "runtime": ".venv-local-mlx",
    },
    "qwen3.8-27b": {
        "path": ROOT / ".models/mlx-community--Qwen3.8-27B-4bit",
        "repo": "mlx-community/Qwen3.8-27B-4bit",
        "kind": "instruct",
        "runtime": ".venv-local-mlx",
    },
}

NUEXTRACT_TEMPLATE = {
    "ranges": [{
        "currency": "currency",
        "period": ["year", "hour"],
        "min_value": "number",
        "max_value": "number",
        "evidence_text": "verbatim-string",
    }]
}

NUEXTRACT_TEMPLATE_V3 = {
    "ranges": [{
        "value_kind": ["exact", "range", "minimum", "maximum"],
        "currency": "currency",
        "period": ["year", "hour"],
        "min_value": "number-or-null",
        "max_value": "number-or-null",
        "evidence_text": "verbatim-string",
    }]
}

def validate_v3_simple(result: dict[str, Any], description: str) -> list[str]:
    return validate_simple_v3(result, description)


# The aliases keep older imports stable while production and evaluation now consume
# the exact same versioned model contract.
SIMPLE_V3_SCHEMA = SHARED_SIMPLE_V3_SCHEMA
SIMPLE_V3_PROMPT = SHARED_SIMPLE_V3_PROMPT
NUEXTRACT_TEMPLATE_V3_SIMPLE = SHARED_NUEXTRACT_TEMPLATE_V3_SIMPLE

DATASETS = {
    "v2": {
        "expected": {"calibration": 100, "evaluation": 400},
        "schema": V2_RESULT_SCHEMA,
        "prompt": V2_SYSTEM_PROMPT,
        "template": NUEXTRACT_TEMPLATE,
        "validator": validate_v2,
    },
    "v3": {
        "expected": {"calibration": 100, "evaluation": 100},
        "schema": V3_RESULT_SCHEMA,
        "prompt": V3_SYSTEM_PROMPT,
        "template": NUEXTRACT_TEMPLATE_V3,
        "validator": validate_v3,
    },
    "v3-simple": {
        "expected": {"calibration": 100, "evaluation": 100},
        "schema": SIMPLE_V3_SCHEMA,
        "prompt": SIMPLE_V3_PROMPT,
        "template": NUEXTRACT_TEMPLATE_V3_SIMPLE,
        "validator": validate_v3_simple,
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def output_path(model: str, split: str, value: str | None, dataset: str = "v2") -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else ROOT / path
    suffix = "" if dataset == "v2" else f"-{dataset}"
    return OUTPUT_DIR / f"{model}{suffix}-{split}.jsonl"


def _complete_model_path(path: Path) -> bool:
    """Return whether a local model directory contains all indexed weight shards."""
    if not path.is_dir() or not (path / "config.json").is_file():
        return False
    index_path = path / "model.safetensors.index.json"
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            shards = set(index["weight_map"].values())
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return False
        return bool(shards) and all((path / shard).is_file() for shard in shards)
    return any(path.glob("*.safetensors"))


def _cached_snapshot(repo: str) -> Path | None:
    repo_cache = Path.home() / ".cache/huggingface/hub" / (
        "models--" + repo.replace("/", "--")
    )
    ref = repo_cache / "refs/main"
    try:
        revision = ref.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    snapshot = repo_cache / "snapshots" / revision
    return snapshot if _complete_model_path(snapshot) else None


def resolve_model_path(config: dict[str, Any]) -> Path | None:
    local_path = Path(config["path"])
    if _complete_model_path(local_path):
        return local_path
    return _cached_snapshot(str(config["repo"]))


def _recorded_model_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def rows_for_split(database: Path, split: str, dataset: str = "v2") -> list[sqlite3.Row]:
    with sqlite3.connect(str(database)) as con:
        con.row_factory = sqlite3.Row
        if dataset in {"v3", "v3-simple"}:
            return list(con.execute(
                "SELECT q.id AS queue_id,q.position,q.ats,q.job_id,q.split,j.company,j.title,"
                "j.location,j.employmentType,j.description,g.result_json AS gold_json "
                "FROM salary_v3_queue q JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
                "JOIN salary_v3_gold_labels g ON g.queue_id=q.id "
                "WHERE q.batch=? AND q.split=? ORDER BY q.position",
                (V3_DEFAULT_BATCH, split),
            ))
        return list(con.execute(
            "SELECT q.id AS queue_id,q.position,q.ats,q.job_id,q.split,j.company,j.title,"
            "j.location,j.employmentType,j.description,g.result_json AS gold_json "
            "FROM salary_labeling_queue q "
            "JOIN jobs j ON j.ats=q.ats AND j.id=q.job_id "
            "JOIN salary_gold_labels g ON g.queue_id=q.id "
            "WHERE q.split=? ORDER BY q.position",
            (split,),
        ))


def _job_text(row: sqlite3.Row) -> str:
    return (
        f"Company: {row['company']}\nTitle: {row['title']}\nLocation: {row['location']}\n"
        f"Employment type: {row['employmentType']}\n\nJOB POSTING\n{row['description']}"
    )


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1].strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("model output did not contain a JSON object")


def _normalize_nuextract_result(
    result: dict[str, Any], dataset: str = "v2", description: str = ""
) -> tuple[dict[str, Any], list[str]]:
    """Adapt narrow NuExtract representation errors to the canonical result shape.

    NuExtract's documented enum output is a scalar, but the MLX 4-bit checkpoint can
    emit a one-item list for an enum nested inside an array of objects. Preserve the
    raw model text separately and only make deterministic representation repairs.
    """
    changes: list[str] = []
    ranges = result.get("ranges")
    if not isinstance(ranges, list):
        return result, changes
    if dataset == "v3-simple":
        return normalize_simple_v3(result, description)
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            continue
        period = item.get("period")
        if (
            isinstance(period, list)
            and len(period) == 1
            and period[0] in {"year", "hour"}
        ):
            item["period"] = period[0]
            changes.append(f"ranges[{index}].period: singleton enum list to scalar")
        value_kind = item.get("value_kind")
        if (
            dataset == "v3"
            and isinstance(value_kind, list)
            and len(value_kind) == 1
            and value_kind[0] in {"exact", "range", "minimum", "maximum"}
        ):
            item["value_kind"] = value_kind[0]
            changes.append(f"ranges[{index}].value_kind: singleton enum list to scalar")
        if dataset == "v3":
            minimum, maximum = item.get("min_value"), item.get("max_value")
            kind = item.get("value_kind")
            numeric_bounds = all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in (minimum, maximum)
            )
            # These are equivalent representations under the v3 contract: equal
            # endpoints are one exact value, while distinct endpoints are a range.
            # The unmodified output remains available in raw_output for auditing.
            if numeric_bounds and minimum == maximum and kind == "range":
                item["value_kind"] = "exact"
                changes.append(f"ranges[{index}].value_kind: equal bounds imply exact")
            elif numeric_bounds and minimum < maximum and kind == "exact":
                item["value_kind"] = "range"
                changes.append(f"ranges[{index}].value_kind: distinct bounds imply range")
    return result, changes


def _load_existing(path: Path) -> set[int]:
    if not path.exists():
        return set()
    completed: set[int] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
                completed.add(int(record["queue_id"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid result file line {line_number}: {exc}") from exc
    return completed


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_mlx(
    database: Path,
    model_name: str,
    split: str,
    limit: int,
    destination: Path,
    max_tokens: int,
    dataset: str = "v2",
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    config = MODELS[model_name]
    model_path = resolve_model_path(config)
    if model_path is None:
        raise ValueError(
            f"model is not downloaded or is incomplete: {config['repo']}"
        )
    try:
        from mlx_vlm import apply_chat_template, generate, load
    except ImportError as exc:
        raise ValueError(
            "run with .venv-local-mlx/bin/python -m job_search.salary.local_eval"
        ) from exc

    dataset_config = DATASETS[dataset]
    rows = rows_for_split(database, split, dataset)
    expected_jobs = int(dataset_config["expected"][split])
    if len(rows) != expected_jobs:
        raise ValueError(f"expected a complete reviewed {split} split, found {len(rows)}")
    completed = _load_existing(destination)
    pending = [row for row in rows if int(row["queue_id"]) not in completed][:limit]
    if not pending:
        return {"model": model_name, "split": split, "processed": 0, "remaining": 0}

    model, processor = load(str(model_path))
    processed = errors = 0
    started = time.perf_counter()
    for row in pending:
        item_started = time.perf_counter()
        raw = ""
        prediction: dict[str, Any] | None = None
        normalizations: list[str] = []
        error: str | None = None
        usage: dict[str, Any] = {}
        try:
            if config["kind"] == "nuextract":
                messages = [{"role": "user", "content": _job_text(row)}]
                prompt = apply_chat_template(
                    processor,
                    model.config,
                    messages,
                    template=json.dumps(dataset_config["template"], separators=(",", ":")),
                    instructions=str(dataset_config["prompt"]),
                    enable_thinking=False,
                )
            else:
                messages = [
                    {"role": "system", "content": str(dataset_config["prompt"])},
                    {
                        "role": "user",
                        "content": _job_text(row)
                        + "\n\nReturn only JSON matching this JSON Schema: "
                        + json.dumps(dataset_config["schema"], separators=(",", ":")),
                    },
                ]
                prompt = apply_chat_template(
                    processor, model.config, messages, enable_thinking=False
                )
            response = generate(
                model,
                processor,
                prompt,
                max_tokens=max_tokens,
                temperature=0.0,
                verbose=False,
            )
            raw = response.text
            prediction = _json_object(raw)
            if config["kind"] == "nuextract":
                prediction, normalizations = _normalize_nuextract_result(
                    prediction, dataset, row["description"] or ""
                )
            validation = dataset_config["validator"](prediction, row["description"])
            usage = {
                "prompt_tokens": response.prompt_tokens,
                "generation_tokens": response.generation_tokens,
                "prompt_tps": response.prompt_tps,
                "generation_tps": response.generation_tps,
                "peak_memory_gb": response.peak_memory,
                "finish_reason": response.finish_reason,
            }
        except Exception as exc:  # Save every failure so long runs remain resumable.
            error = f"{type(exc).__name__}: {exc}"
            validation = []
            errors += 1
        record = {
            "format_version": 1,
            "dataset": dataset,
            "model": model_name,
            "model_path": _recorded_model_path(model_path),
            "split": split,
            "queue_id": int(row["queue_id"]),
            "position": int(row["position"]),
            "ats": row["ats"],
            "job_id": row["job_id"],
            "prediction": prediction,
            "raw_output": raw,
            "normalizations": normalizations,
            "validation": validation,
            "error": error,
            "usage": usage,
            "elapsed_seconds": round(time.perf_counter() - item_started, 4),
            "created_at": now(),
        }
        _append(destination, record)
        processed += 1
        state = "error" if error else ("needs_review" if validation else "complete")
        print(
            f"{model_name} {processed}/{len(pending)} position={row['position']} {state} "
            f"{record['elapsed_seconds']:.1f}s",
            flush=True,
        )
    return {
        "model": model_name,
        "split": split,
        "dataset": dataset,
        "processed": processed,
        "errors": errors,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "output": str(destination),
        "remaining": len(rows) - len(completed) - processed,
    }


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def score(
    database: Path, split: str, source: Path, dataset: str = "v2"
) -> dict[str, Any]:
    split_rows = rows_for_split(database, split, dataset)
    gold = {int(row["queue_id"]): json.loads(row["gold_json"]) for row in split_rows}
    descriptions = {int(row["queue_id"]): row["description"] or "" for row in split_rows}
    records: list[dict[str, Any]] = []
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("split") == split:
                records.append(record)
    primary_correct = positives = positive_hits = negatives = negative_correct = 0
    tp = fp = fn = 0
    period_counts = {
        "year": {"tp": 0, "fp": 0, "fn": 0},
        "hour": {"tp": 0, "fp": 0, "fn": 0},
    }
    value_kind_counts = {
        kind: {"tp": 0, "fp": 0, "fn": 0}
        for kind in ("exact", "range", "minimum", "maximum")
    }
    errors = validation_failures = strict_output_failures = 0
    for record in records:
        expected_result = gold.get(int(record["queue_id"]))
        if expected_result is None:
            continue
        has_recorded_error = bool(record.get("error"))
        has_validation_failure = bool(record.get("validation"))
        if has_recorded_error:
            errors += 1
        if has_validation_failure:
            validation_failures += 1
        if has_recorded_error or has_validation_failure:
            strict_output_failures += 1
        prediction = record.get("prediction")
        if not isinstance(prediction, dict):
            prediction = {"ranges": []}
            if not has_recorded_error:
                errors += 1
        if dataset == "v3-simple":
            prediction, _ = _normalize_nuextract_result(
                prediction, dataset, descriptions[int(record["queue_id"])]
            )
        if dataset == "v3-simple":
            key_function = canonical_figure_key_v3_simple
            match_function = usd_primary_match_v3_simple
        elif dataset == "v3":
            key_function = canonical_key_v3
            match_function = usd_primary_match_v3
        else:
            key_function = canonical_key
            match_function = usd_primary_match
        predicted = set(key_function(prediction))
        expected = set(key_function(expected_result))
        matched = predicted & expected
        if match_function(prediction, expected_result):
            primary_correct += 1
        if expected:
            positives += 1
            if matched:
                positive_hits += 1
        else:
            negatives += 1
            if not predicted:
                negative_correct += 1
        tp += len(matched)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        for period in period_counts:
            p_values = {item for item in predicted if item[1] == period}
            g_values = {item for item in expected if item[1] == period}
            period_counts[period]["tp"] += len(p_values & g_values)
            period_counts[period]["fp"] += len(p_values - g_values)
            period_counts[period]["fn"] += len(g_values - p_values)
        if dataset == "v3":
            for kind in value_kind_counts:
                p_values = {item for item in predicted if item[2] == kind}
                g_values = {item for item in expected if item[2] == kind}
                value_kind_counts[kind]["tp"] += len(p_values & g_values)
                value_kind_counts[kind]["fp"] += len(p_values - g_values)
                value_kind_counts[kind]["fn"] += len(g_values - p_values)
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "split": split,
        "dataset": dataset,
        "scored_jobs": len(records),
        "expected_jobs": len(gold),
        "errors": errors,
        "validation_failures": validation_failures,
        "strict_output_success_rate": _safe_ratio(
            len(records) - strict_output_failures, len(records)
        ),
        "primary_accuracy": _safe_ratio(primary_correct, len(records)),
        "positive_supported_hit_rate": _safe_ratio(positive_hits, positives),
        "usd_negative_accuracy": _safe_ratio(negative_correct, negatives),
        "usd_value_precision": precision,
        "usd_value_recall": recall,
        "usd_value_f1": f1,
        "usd_values": {"tp": tp, "fp": fp, "fn": fn},
        "usd_metric_unit": "figures" if dataset == "v3-simple" else "records",
        "by_period": period_counts,
        "by_value_kind": value_kind_counts if dataset == "v3" else None,
    }


def model_status() -> dict[str, Any]:
    status: dict[str, Any] = {}
    for name, config in MODELS.items():
        resolved = resolve_model_path(config)
        status[name] = {
            "downloaded": resolved is not None,
            "path": str(resolved or Path(config["path"])),
            "repo": config["repo"],
            "runtime": config["runtime"],
        }
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    run = commands.add_parser("run")
    run.add_argument("--model", choices=sorted(MODELS), required=True)
    run.add_argument("--dataset", choices=sorted(DATASETS), default="v2")
    run.add_argument("--split", choices=("calibration", "evaluation"), default="calibration")
    run.add_argument("--limit", type=int, default=1)
    run.add_argument("--max-tokens", type=int, default=512)
    run.add_argument("--output")
    scoring = commands.add_parser("score")
    scoring.add_argument("--model", choices=sorted(MODELS), required=True)
    scoring.add_argument("--dataset", choices=sorted(DATASETS), default="v2")
    scoring.add_argument("--split", choices=("calibration", "evaluation"), default="calibration")
    scoring.add_argument("--input")
    args = parser.parse_args()
    database = db_path(args.db)
    try:
        if args.command == "status":
            print(json.dumps(model_status(), indent=2))
        elif args.command == "run":
            destination = output_path(args.model, args.split, args.output, args.dataset)
            print(json.dumps(run_mlx(
                database, args.model, args.split, args.limit, destination, args.max_tokens,
                args.dataset,
            ), indent=2))
        else:
            source = output_path(args.model, args.split, args.input, args.dataset)
            print(json.dumps(score(database, args.split, source, args.dataset), indent=2))
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
