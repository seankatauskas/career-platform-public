"""Published demo assets must match a successful isolated capture receipt."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("media_publish", Path(__file__).resolve().parents[1] / "scripts/publish-dashboard-media.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class MediaPublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.capture = self.root / "capture"
        self.capture.mkdir()
        self.old_root = publisher.ROOT
        publisher.ROOT = self.root / "repo"
        self.receipt = {"passed": True, "scenario": "portfolio", "sourceRevision": "fixture-sha",
                        "browserErrors": [], "requestViolations": [], "assets": []}
        for name in publisher.STILLS | {"walkthrough.mp4"}:
            data = ("safe demo fixture " + name).encode()
            (self.capture / name).write_bytes(data)
            self.receipt["assets"].append({"name": name, "sha256": hashlib.sha256(data).hexdigest()})
        (self.capture / "private-config.json").write_text('{"fixture_secret":"not-for-publication"}')
        self.save_receipt()

    def tearDown(self):
        publisher.ROOT = self.old_root
        self.temp.cleanup()

    def save_receipt(self):
        (self.capture / "results.json").write_text(json.dumps(self.receipt))

    def test_exact_same_allowlisted_assets_reach_both_repositories(self):
        result = publisher.publish(self.capture, self.root / "portfolio")
        self.assertEqual(len(result["copies"]), 24)
        for copy in result["copies"]:
            self.assertEqual(Path(copy["destination"]).read_bytes(), (self.capture / copy["name"]).read_bytes())
        self.assertFalse(list(publisher.ROOT.rglob("*.json")))
        self.assertFalse(list((self.root / "portfolio").rglob("*.json")))

    def test_changed_asset_prevents_all_publication(self):
        (self.capture / "review.png").write_bytes(b"changed after privacy review")
        with self.assertRaisesRegex(ValueError, "changed after capture"):
            publisher.publish(self.capture)
        self.assertFalse(publisher.ROOT.exists())

    def test_failed_capture_or_external_request_prevents_publication(self):
        for field, value in (("passed", False), ("requestViolations", [{"origin": "https://example.test"}])):
            old = self.receipt[field]
            self.receipt[field] = value
            self.save_receipt()
            with self.assertRaises(ValueError):
                publisher.publish(self.capture)
            self.assertFalse(publisher.ROOT.exists())
            self.receipt[field] = old

    def test_symlink_asset_is_rejected_before_any_copy(self):
        image = self.capture / "shortlist.png"
        data = image.read_bytes()
        image.unlink()
        target = self.root / "outside.png"
        target.write_bytes(data)
        image.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "Invalid asset"):
            publisher.publish(self.capture)
        self.assertFalse(publisher.ROOT.exists())


if __name__ == "__main__":
    unittest.main()
