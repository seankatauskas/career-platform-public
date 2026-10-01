"""A prepared release cannot silently select different or untested code."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepared-release.py"
spec = importlib.util.spec_from_file_location("prepared_release", SCRIPT)
prepared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepared)


def fixture():
    source = "a" * 40
    image = "123456789012.dkr.ecr.us-east-2.amazonaws.com/career/app@sha256:" + "b" * 64
    return {
        "version": 1, "operations_protocol": 1, "release_id": source + "-9", "source_sha": source,
        "bundle_sha256": "c" * 64, "schema_compatibility": "state-v1",
        "app_image": image, "hermes_image": image, "hermes_base_image": image,
        "release_policy": {"version": 1, "schema_compatibility": "state-v1",
                           "test_baseline_sha": "d" * 40,
                           "predecessor": {"release_id": "d" * 40 + "-8", "source_sha": "d" * 40}},
        "transition_validation": {"schema_version": 1, "runtime": "docker", "passed": True,
                                  "source_sha": source, "baseline_sha": "d" * 40, "rollback_passed": True},
    }


class PreparedReleaseTests(unittest.TestCase):
    def verify(self, manifest):
        raw = json.dumps(manifest).encode()
        return prepared.verify(raw, "a" * 40 + "-9", hashlib.sha256(raw).hexdigest())

    def test_prepared_receipt_identifies_exact_code_and_never_claims_installation(self):
        result = self.verify(fixture())
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["source_sha"], "a" * 40)
        self.assertEqual(result["expected_predecessor"], fixture()["release_policy"]["predecessor"])
        self.assertNotIn("ssm_command_id", result)

    def test_modified_bytes_or_wrong_selection_are_rejected(self):
        raw = json.dumps(fixture()).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        for content, release in ((raw + b" ", "a" * 40 + "-9"), (raw, "a" * 40 + "-10")):
            with self.subTest(release=release), self.assertRaises(ValueError):
                prepared.verify(content, release, checksum)

    def test_untested_wrong_source_and_unpinned_images_are_rejected(self):
        for field, value in (("source_sha", "f" * 40), ("app_image", "app:latest"),
                             ("bundle_sha256", ""), ("schema_compatibility", "different"),
                             ("operations_protocol", 0)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify({**fixture(), field: value})
        for field, value in (("passed", False), ("working_tree_dirty", True),
                             ("runtime", "python"), ("baseline_sha", "f" * 40),
                             ("source_sha", "f" * 40)):
            manifest = fixture()
            manifest["transition_validation"][field] = value
            with self.subTest(evidence=field), self.assertRaises(ValueError):
                self.verify(manifest)

    def test_input_validation_rejects_paths_and_environment_injection(self):
        for release in ("../release", "latest", "a" * 40 + "-9\nCOMMAND_ID=bad", "$(echo bad)"):
            with self.subTest(release=release), self.assertRaises(ValueError):
                prepared.validate_selection(release, "b" * 64)
        for checksum in ("", "b" * 63, "b" * 64 + "\nOTHER=bad"):
            with self.assertRaises(ValueError):
                prepared.validate_selection("a" * 40 + "-9", checksum)

    def test_cli_selection_needs_no_cloud_access_or_application_state(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "selection", "--release-id",
                                 "a" * 40 + "-9", "--sha256", "b" * 64],
                                env={}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_oversized_or_nonobject_manifests_fail_closed(self):
        for raw in (b" " * (64 * 1024 + 1), b"[]", b"null"):
            with self.assertRaises(ValueError):
                prepared.verify(raw, "a" * 40 + "-9", hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
