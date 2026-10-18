"""Scaled, deterministic release-83 resolver delay; no Docker, AWS or providers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scale", type=float, default=.01)
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/hermes-scheduling.json")
    args = parser.parse_args()
    if not 1 <= args.runs <= 10 or not .001 <= args.scale <= 1:
        parser.error("runs must be 1–10; scale must be .001–1")
    helpers = runpy.run_path(str(ROOT / "scripts/validate-release.py"))
    observed = {"offline": 110.152, "native": 65.608, "hermes": 48.3 + 3.511}
    groups = [(name, [sys.executable, "-c", "import sys,time; time.sleep(float(sys.argv[1]))",
                      str(seconds * args.scale)]) for name, seconds in observed.items()]
    trials = []
    with tempfile.TemporaryDirectory(prefix="hermes-scheduling-") as directory:
        for number in range(args.runs):
            for parallel in (False, True):
                started = time.monotonic()
                receipt = helpers["run_groups"](groups if parallel else groups[:2],
                                                Path(directory) / f"{number}-{parallel}")
                if not receipt["passed"]:
                    raise RuntimeError("scheduling fixture failed")
                if not parallel:
                    subprocess.run(groups[2][1], check=True)
                trials.append({"parallel_hermes": parallel, "seconds": time.monotonic() - started})
    report = {"scope": "scaled sleeps using observed release-83 gate durations; no actual network/build workload",
              "scale": args.scale, "observed_seconds": observed, "trials": trials,
              "serial_hermes_median_seconds": statistics.median(t["seconds"] for t in trials if not t["parallel_hermes"]),
              "parallel_hermes_median_seconds": statistics.median(t["seconds"] for t in trials if t["parallel_hermes"])}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
