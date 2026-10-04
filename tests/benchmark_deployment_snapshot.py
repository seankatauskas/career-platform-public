"""Bounded offline comparison of local rollback capture pipelines.

Run: python3 -m tests.benchmark_deployment_snapshot --megabytes 64 --runs 2
Only synthetic bytes in a temporary directory are used. No Docker, network,
credentials, real databases, or production state are accessed. Timings describe
this machine and fixture, not AWS downtime or a deployment service-level target.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import tarfile
import tempfile
import time

from job_search import aws_ops as ops


def legacy_capture(c: dict) -> dict:
    """Previous paused gzip-level-1 pipeline, including durable final copy."""
    ops.available_space(c)
    data = Path(c["data_root"]); backups = ops.backup_root(c)
    with tempfile.TemporaryDirectory(prefix="legacy-benchmark-", dir=backups) as temporary:
        root = Path(temporary); stage = root / "payload"; stage.mkdir(mode=0o700)
        versions = ops.secret_versions(c)
        for name in ops.BACKUP_DIRS: ops.copy_snapshot(data / name, stage / name, hermes=name == "hermes")
        files = {str(p.relative_to(stage)): {"sha256": ops.digest(p), "size": p.stat().st_size}
                 for p in stage.rglob("*") if p.is_file()}
        ops.write_json(stage / "backup.json", {"version": 1, "backup_id": "benchmark",
                       "release_id": "benchmark", "secrets": versions, "files": files})
        archive = root / "backup.tar.gz"
        with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
            for item in sorted(stage.rglob("*")):
                tar.add(item, arcname=str(item.relative_to(stage)), recursive=False)
        ops.sync_tree(root)
        checksum = ops.digest(archive)
        destination = backups / ("legacy-" + os.urandom(4).hex() + ".tar.gz")
        shutil.copy2(archive, destination)
        with destination.open("rb") as stream: os.fsync(stream.fileno())
        ops.sync_directory(backups)
        return {"sha256": checksum}


def benchmark(megabytes: int = 64, runs: int = 2) -> dict:
    if not 1 <= megabytes <= 256 or not 1 <= runs <= 5:
        raise ValueError("benchmark requires 1–256 MiB and 1–5 runs")
    with tempfile.TemporaryDirectory(prefix="career-deployment-benchmark-") as temporary:
        data = Path(temporary).resolve()
        for name in ops.BACKUP_DIRS: (data / name).mkdir(mode=0o700)
        ops.write_json(data / "materialized-secrets.json", {})
        c = {"data_root": str(data), "secret_arns": {}}
        # A reproducible mix of incompressible and compressible bytes; results
        # are intentionally not presented as representative of a real corpus.
        block = random.Random(0).randbytes(512 * 1024) + bytes(512 * 1024)
        with (data / "state/synthetic-corpus.bin").open("wb") as stream:
            for _ in range(megabytes): stream.write(block)
        for name in ("hermes", "toolchain"):
            (data / name / "fixture.txt").write_text("synthetic retained state\n")
        measurements = []
        for trial in range(runs):
            order = ("legacy_tar_level1", "directory_v1") if trial % 2 == 0 else ("directory_v1", "legacy_tar_level1")
            for implementation in order:
                started = time.monotonic()
                if implementation == "legacy_tar_level1": legacy_capture(c)
                else: ops.local_snapshot_unlocked(c, release_id="benchmark")
                measurements.append({"implementation": implementation, "seconds": round(time.monotonic() - started, 6)})
        medians = {name: round(statistics.median(row["seconds"] for row in measurements if row["implementation"] == name), 6)
                   for name in ("legacy_tar_level1", "directory_v1")}
        return {"scope": "offline synthetic local-filesystem fixture; not measured AWS downtime",
                "synthetic_bytes": megabytes * 1024**2, "runs_per_implementation": runs,
                "measurements": measurements, "median_seconds": medians}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--megabytes", type=int, default=64)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.megabytes <= 256 or not 1 <= args.runs <= 5:
        parser.error("use 1–256 MiB and 1–5 runs")
    print(json.dumps(benchmark(args.megabytes, args.runs), indent=2))


if __name__ == "__main__":
    main()
