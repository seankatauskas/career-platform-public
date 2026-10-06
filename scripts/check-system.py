#!/usr/bin/env python3
"""Run the credential-free system suites; emit a reproducible JSON receipt."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def check(name: str, argv: list[str], timeout: int) -> dict:
    started = time.monotonic()
    try:
        process = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHON": sys.executable})
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Clean up browser and fixture-server descendants as well as the suite.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            return {"name": name, "status": "failed", "reason": "timeout", "seconds": timeout}
        return {"name": name, "status": "passed" if process.returncode == 0 else "failed",
                "exit_code": process.returncode, "seconds": round(time.monotonic() - started, 3),
                "output": (stdout + stderr)[-16000:]}

    except OSError as exc:
        return {"name": name, "status": "failed", "reason": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--browser", action="store_true", help="Include Chromium extension acceptance (install extension npm dependencies and Chromium first)")
    parser.add_argument("--match", default="", help="Filter suite filenames")
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/system-checks.json")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8 or not 1 <= args.timeout <= 1800:
        parser.error("jobs must be 1–8 and timeout 1–1800 seconds")
    suites = [(str(p.relative_to(ROOT)), [sys.executable, "-m", "tests." + p.stem])
              for p in sorted((ROOT / "tests").glob("test_*.py"))]
    suites.append(("scripts/check-private-files.py", [sys.executable, "scripts/check-private-files.py"]))
    suites.append(("extension/test_extension.js", [shutil.which("node") or "node", "extension/test_extension.js"]))
    suites.append(("extension/test_tracking_reliability.js", [shutil.which("node") or "node", "extension/test_tracking_reliability.js"]))
    if args.browser:
        suites.extend((name, [shutil.which("node") or "node", name]) for name in ("extension/test_browser.mjs", "extension/test_answer_capture.mjs", "tests/browser/test_ops_browser.mjs", "tests/browser/test_console_browser.mjs", "tests/browser/test_review_queue.mjs", "tests/browser/test_settings_views.mjs", "tests/browser/test_agent_reviews.mjs", "tests/browser/test_review_quality.mjs", "tests/browser/test_lifecycle_browser.mjs"))
    suites = [(name, argv) for name, argv in suites if args.match in name]
    if not suites:
        parser.error("no suites match")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        pending = {pool.submit(check, name, argv, args.timeout): name for name, argv in suites}
        for future in concurrent.futures.as_completed(pending):
            result = future.result()
            results.append(result)
            print(f"{result['status']:6} {result['name']}", flush=True)
            if result["status"] != "passed":
                print(result.get("output", result.get("reason", "")), flush=True)
    results.sort(key=lambda result: result["name"])
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                              capture_output=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, text=True,
                           capture_output=True).stdout != ""
    passed = all(result["status"] == "passed" for result in results)
    report = {"schema_version": 1, "source_sha": revision, "working_tree_dirty": dirty,
              "scope": "local fixtures; no live provider verification", "passed": passed,
              "suite_count": len(results), "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{sum(r['status'] == 'passed' for r in results)}/{len(results)} suites passed; receipt: {args.output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
