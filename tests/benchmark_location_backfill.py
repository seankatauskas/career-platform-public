"""Compare location backfill against its pre-optimization implementation, offline."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import tempfile
import time

from job_search.collection import locations


def benchmark(jobs: int = 20000, repeats: int = 3) -> dict:
    """Use only synthetic data; baseline is the immutable parent revision."""
    if not 1 <= jobs <= 100000 or not 1 <= repeats <= 10:
        raise ValueError("jobs must be 1–100000 and repeats 1–10")
    root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(
        ["git", "show", "4820b58:job_search/collection/locations.py"], cwd=root,
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        old_source = path / "baseline_locations.py"
        old_source.write_bytes(source)
        spec = importlib.util.spec_from_file_location("baseline_locations", old_source)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        db = path / "synthetic.db"
        texts = ("Remote - United States", "Chicago, IL (Hybrid)",
                 "Toronto, Canada; New York, NY", "Remote", "San Francisco, CA; Seattle, WA", "")
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE jobs(ats TEXT,id TEXT,location TEXT,isRemote TEXT,workplaceType TEXT,PRIMARY KEY(ats,id))")
            con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", (
                ("ashby", str(i), texts[i % len(texts)], str(i % 2), "hybrid" if i % 3 == 0 else "")
                for i in range(jobs)
            ))
        initial = baseline.backfill(db)
        expected = {**initial, "changed": 0}
        durations = {"baseline": [], "optimized": []}
        for _ in range(repeats):
            for name, module in (("baseline", baseline), ("optimized", locations)):
                start = time.perf_counter()
                result = module.backfill(db)
                durations[name].append(time.perf_counter() - start)
                if result != expected:
                    raise AssertionError("backfill results changed")
        medians = {name: statistics.median(values) for name, values in durations.items()}
        return {"baseline_revision": "4820b58", "fixture_jobs": jobs,
                "scenario": "unchanged catalog; six synthetic locations", "network_calls": 0,
                "runs_seconds": durations, "median_seconds": medians,
                "speedup": medians["baseline"] / medians["optimized"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=20000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.jobs, args.repeats), indent=2))


if __name__ == "__main__":
    main()
