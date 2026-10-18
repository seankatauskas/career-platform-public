"""The parallel release gates must all finish and fail closed."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / "scripts/validate-release.py"))


class ReleaseValidationTests(unittest.TestCase):
    def test_source_change_or_dirty_checkout_cannot_publish_success(self):
        for initial_dirty, final_sha, final_dirty, expected in (
            ("", "a" * 40, "", 0), ("", "b" * 40, "", 1),
            (" M Dockerfile", "a" * 40, "", 1), ("", "a" * 40, " M Dockerfile", 1),
            ("", "a" * 40, "?? job_search/untracked.py", 1),
        ):
            with self.subTest(initial_dirty=initial_dirty, final_sha=final_sha, final_dirty=final_dirty):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / ".cache/release-validation").mkdir(parents=True)
                    def checked(*args):
                        return {"passed": True}
                    with patch.dict(helpers["main"].__globals__, ROOT=root, run_groups=checked), \
                            patch.object(helpers["subprocess"], "check_output",
                                         side_effect=["a" * 40, initial_dirty, final_sha, final_dirty]):
                        self.assertEqual(helpers["main"](), expected)
                    report = json.loads((root / ".cache/release-validation/results.json").read_text())
                    self.assertEqual(report["passed"], expected == 0)

    def test_gates_overlap_and_a_failed_gate_does_not_cancel_teardown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Each lane must observe the other starting before it can finish.
            # A sequential implementation fails this handshake.
            source = """
import pathlib, sys, time
root = pathlib.Path(sys.argv[1]); name = sys.argv[2]
(root / name).touch()
deadline = time.monotonic() + 5
while not all((root / lane).exists() for lane in ("offline", "native", "hermes")):
    if time.monotonic() >= deadline:
        raise SystemExit(90)
    time.sleep(.01)
if name == 'offline':
    raise SystemExit(7)
time.sleep(.05)
(root / (name + '-cleanup')).touch()
"""
            groups = [(name, [sys.executable, "-c", source, str(root), name])
                      for name in ("offline", "native", "hermes")]
            report = helpers["run_groups"](groups, root / "logs")
            self.assertFalse(report["passed"])
            self.assertTrue((root / "native-cleanup").exists())
            self.assertTrue((root / "hermes-cleanup").exists())
            self.assertEqual({r["name"]: r["exit_code"] for r in report["groups"]},
                             {"offline": 7, "native": 0, "hermes": 0})
            self.assertEqual(json.loads((root / "logs/results.json").read_text()), report)

    def test_success_requires_every_gate_and_missing_executable_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success = [sys.executable, "-c", "print('checked')"]
            self.assertTrue(helpers["run_groups"]([("one", success), ("two", success)], root)["passed"])
            report = helpers["run_groups"]([("one", success), ("two", [str(root / "absent")])], root)
            self.assertFalse(report["passed"])
            self.assertFalse(json.loads((root / "results.json").read_text())["passed"])

    def test_native_lane_retains_all_runtime_and_transition_gates(self):
        source = (ROOT / "scripts/validate-release-image.sh").read_text()
        for required in ("set -euo pipefail", "SOURCE_REVISION=", "--platform linux/amd64",
                         "tests.probe_codex_review_container", "--screening", "--adjudicator",
                         "tests.probe_old_method_container", "--require-host-runtime",
                         "prepare-acceptance-toolchain.py", "compose-acceptance.py",
                         "--require-linux-host", "release-transition-acceptance.py"):
            self.assertIn(required, source)
        self.assertNotIn("docker push", source)


if __name__ == "__main__":
    unittest.main()
