"""Publish only the immutable Hermes image whose native contract passed."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / "scripts/validate-release-hermes.py"))
SOURCE = "a" * 40
BASE = "fixture/hermes@sha256:" + "b" * 64
IMAGE = "sha256:" + "c" * 64


class HermesValidationTests(unittest.TestCase):
    def test_receipt_requires_exact_image_source_base_and_success(self):
        proof = {"passed": True, "source_sha": SOURCE, "hermes_base_image": BASE, "image_id": IMAGE}
        self.assertEqual(helpers["validated_image_id"](proof, source=SOURCE, base=BASE), IMAGE)
        for key, value in (("passed", False), ("passed", 1), ("source_sha", "d" * 40),
                           ("hermes_base_image", "different/hermes@sha256:" + "b" * 64),
                           ("image_id", "mutable:tag"), ("image_id", IMAGE + "\n")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                helpers["validated_image_id"]({**proof, key: value}, source=SOURCE, base=BASE)

    def exercise(self, root, *, failure=None):
        receipt = root / ".cache/hermes-healthcheck.json"
        receipt.parent.mkdir()
        receipt.write_text(json.dumps({"passed": True, "image_id": "stale:tag"}))
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            self.assertTrue(kwargs["check"])
            if argv[:2] == ["docker", "build"]:
                self.assertNotIn("--tag", argv)
                self.assertEqual(argv[-1], ".")
                self.assertIn("HERMES_BASE_IMAGE=" + BASE, argv)
                self.assertFalse(receipt.exists())
                if failure == "build":
                    raise subprocess.CalledProcessError(1, argv)
                Path(argv[argv.index("--iidfile") + 1]).write_text(IMAGE)
            else:
                self.assertEqual(argv[-2:], ["--image", IMAGE])
                receipt.write_text(json.dumps({"passed": failure != "native",
                                               "image_id": IMAGE if failure != "identity" else "sha256:" + "e" * 64}))
                if failure == "native":
                    raise subprocess.CalledProcessError(1, argv)
        with patch.dict(helpers["main"].__globals__, ROOT=root), \
                patch.dict(helpers["os"].environ, HERMES_BASE_IMAGE=BASE), \
                patch.object(helpers["sys"], "argv", ["validate-release-hermes.py"]), \
                patch.object(helpers["subprocess"], "check_output", return_value=SOURCE), \
                patch.object(helpers["subprocess"], "run", side_effect=run):
            result = helpers["main"]()
        return result, json.loads(receipt.read_text()), calls

    def test_build_and_native_check_bind_the_same_immutable_id(self):
        with tempfile.TemporaryDirectory() as directory:
            result, receipt, calls = self.exercise(Path(directory))
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(helpers["validated_image_id"](receipt, source=SOURCE, base=BASE), IMAGE)

    def test_build_native_or_identity_failure_cannot_reuse_old_success(self):
        for failure in ("build", "native", "identity"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises((subprocess.CalledProcessError, ValueError)):
                    self.exercise(root, failure=failure)
                receipt = root / ".cache/hermes-healthcheck.json"
                if receipt.exists():
                    with self.assertRaises(ValueError):
                        helpers["validated_image_id"](json.loads(receipt.read_text()), source=SOURCE, base=BASE)


if __name__ == "__main__":
    unittest.main()
