#!/usr/bin/env python3
"""Join independent release gates before any image publication is allowed."""
from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def run_groups(groups, output):
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def run(name, argv):
        begin = time.monotonic()
        log = output / (name + ".log")
        print(f"Starting {name}; log: {log}", flush=True)
        try:
            with log.open("w") as stream:
                result = subprocess.run(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
                                        stdout=stream, stderr=subprocess.STDOUT)
            code = result.returncode
        except OSError as error:
            log.write_text(str(error) + "\n")
            code = 127
        passed = code == 0
        print(f"{'Passed' if passed else 'FAILED'} {name} in {time.monotonic() - begin:.1f}s", flush=True)
        if not passed:
            print(log.read_text(errors="replace")[-16000:], flush=True)
        return {"name": name, "passed": passed, "exit_code": code,
                "seconds": round(time.monotonic() - begin, 3), "log": str(log)}

    # Do not fail fast: native acceptance must finish its own teardown even when
    # an offline suite fails. No publishing command exists in any lane.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pending = [pool.submit(run, name, argv) for name, argv in groups]
        results = [future.result() for future in concurrent.futures.as_completed(pending)]
    report = {"schema_version": 1, "passed": all(row["passed"] for row in results),
              "seconds": round(time.monotonic() - started, 3),
              "groups": sorted(results, key=lambda row: row["name"])}
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    initial_dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True))
    # The dedicated Linux release runner has six CPUs and 16 GiB of memory.
    groups = [
        ("offline", [sys.executable, "scripts/check-system.py", "--browser", "--jobs", "6",
                     "--output", ".cache/system-checks.json"]),
        ("native", ["bash", "scripts/validate-release-image.sh"]),
        ("hermes", [sys.executable, "scripts/validate-release-hermes.py"]),
    ]
    output = ROOT / ".cache/release-validation"
    report = run_groups(groups, output)
    report["source_sha"] = source
    final_source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report["working_tree_dirty"] = initial_dirty or bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True))
    if source != final_source or report["working_tree_dirty"]:
        report.update(passed=False, reason="source changed or working tree is dirty")
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
