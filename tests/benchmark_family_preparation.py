"""Offline family-preparation benchmark on disposable copies of an existing catalog.

Each implementation runs in a fresh child process. The parent monitors RSS and
wall time, terminating a child if it exceeds either explicit resource budget.
Only aggregate counts, timing, memory and content digests are printed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time

from job_search.collection import dedupe

BASELINE = "0f74b76"
TABLES = ("job_families", "job_family_members", "job_template_clusters", "job_template_cluster_lineage")


def _digest(con, tables):
    digest = hashlib.sha256()
    for table in tables:
        digest.update(table.encode())
        for row in con.execute(f"SELECT * FROM {table} ORDER BY 1,2"):
            digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _worker(db, implementation, baseline_path):
    module = dedupe
    if implementation == "baseline":
        spec = importlib.util.spec_from_file_location("baseline_dedupe", baseline_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    start = time.perf_counter()
    summary = module.prepare_families(db)
    seconds = time.perf_counter() - start
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024)
    start = time.perf_counter()
    current = module.prepared_families_are_current(db)
    validation_seconds = time.perf_counter() - start
    with sqlite3.connect(db) as con:
        derived_digest = _digest(con, TABLES)
        source_digest = _digest(con, ("jobs",))
    print(json.dumps(dict(implementation=implementation, summary=summary,
                         prepare_seconds=seconds, peak_rss_bytes=peak_bytes,
                         current=current, validation_seconds=validation_seconds,
                         derived_digest=derived_digest, source_digest=source_digest)))


def benchmark(db: Path, max_rss_mib=4096, timeout_seconds=900, prepared_db: Path | None = None,
              implementation: str = "both"):
    if not db.is_file():
        raise FileNotFoundError(db)
    if prepared_db is not None and prepared_db.exists():
        raise FileExistsError(prepared_db)
    if implementation not in {"both", "baseline", "optimized"}:
        raise ValueError("implementation must be both, baseline, or optimized")
    if not 128 <= max_rss_mib <= 8192 or not 1 <= timeout_seconds <= 3600:
        raise ValueError("RSS budget must be 128–8192 MiB; timeout 1–3600 seconds")
    root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(["git", "show", f"{BASELINE}:job_search/collection/dedupe.py"], cwd=root)
    runs = []
    with tempfile.TemporaryDirectory(prefix="family-benchmark-") as directory:
        directory = Path(directory)
        baseline_path = directory / "baseline_dedupe.py"
        baseline_path.write_bytes(source)
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as original:
            original.execute("BEGIN")
            original_digest = _digest(original, ("jobs",))
            for name in (("baseline", "optimized") if implementation == "both" else (implementation,)):
                copied = directory / f"{name}.db"
                with sqlite3.connect(copied) as target:
                    original.backup(target)
                with tempfile.TemporaryFile(mode="w+") as output:
                    child = subprocess.Popen([sys.executable, "-m", "tests.benchmark_family_preparation",
                                              "--worker", name, "--db", str(copied),
                                              "--baseline-path", str(baseline_path)], cwd=root,
                                             stdout=output, stderr=subprocess.STDOUT)
                    start = time.monotonic()
                    failure = None
                    observed_peak = 0
                    try:
                        while child.poll() is None:
                            measured = subprocess.run(["ps", "-o", "rss=", "-p", str(child.pid)],
                                                      text=True, capture_output=True)
                            rss = int(measured.stdout.strip() or 0) * 1024
                            observed_peak = max(observed_peak, rss)
                            if rss > max_rss_mib * 1024 ** 2:
                                failure = "rss_budget_exceeded"
                                break
                            if time.monotonic() - start > timeout_seconds:
                                failure = "timeout"
                                break
                            time.sleep(0.5)
                    finally:
                        if child.poll() is None:
                            child.kill()
                        child.wait()
                    output.seek(0)
                    text = output.read()
                    if failure or child.returncode:
                        runs.append(dict(implementation=name, failed=failure or "worker_failed",
                                         exit_code=child.returncode, observed_peak_rss_bytes=observed_peak,
                                         elapsed_seconds=time.monotonic() - start))
                        if not failure:
                            raise RuntimeError(text)
                    else:
                        result = json.loads(text)
                        if result["source_digest"] != original_digest:
                            raise AssertionError("source rows changed")
                        runs.append(result)
                if name == "optimized" and prepared_db is not None and "failed" not in runs[-1]:
                    # Exclusively create the optional output, never replace an
                    # existing catalog. SQLite backup also works across volumes.
                    with prepared_db.open("xb"):
                        pass
                    prepared_db.chmod(0o600)
                    with sqlite3.connect(copied) as prepared, sqlite3.connect(prepared_db) as target:
                        prepared.backup(target)
                copied.unlink()
    successful = [run for run in runs if "failed" not in run]
    parity = (successful[0]["derived_digest"] == successful[1]["derived_digest"]
              if len(successful) == 2 else None)
    if parity is False:
        raise AssertionError("derived metadata changed")
    return dict(baseline_revision=BASELINE, network_calls=0,
                python_version=sys.version.split()[0], platform=sys.platform,
                source_preserved=bool(successful) and all(run["source_digest"] == original_digest for run in successful),
                exact_derived_parity=parity, max_rss_mib=max_rss_mib,
                timeout_seconds=timeout_seconds, runs=runs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--max-rss-mib", type=int, default=4096)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--prepared-db", type=Path, help="optional new path for the optimized prepared copy")
    parser.add_argument("--implementation", choices=("both", "baseline", "optimized"), default="both")
    parser.add_argument("--worker", choices=("baseline", "optimized"), help=argparse.SUPPRESS)
    parser.add_argument("--baseline-path", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        _worker(args.db, args.worker, args.baseline_path)
    else:
        print(json.dumps(benchmark(args.db, args.max_rss_mib, args.timeout_seconds,
                                   args.prepared_db, args.implementation), indent=2))


if __name__ == "__main__":
    main()
