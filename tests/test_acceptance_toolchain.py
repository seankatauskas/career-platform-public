"""Offline integrity tests for the public acceptance toolchain assembler."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("acceptance_toolchain", Path(__file__).parents[1] / "scripts/prepare-acceptance-toolchain.py")
toolchain = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toolchain)


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.value = b"public fixture content"
        self.binary = b"fixture engine"
        (self.root / "tectonic").write_bytes(self.binary)
        (self.root / "acceptance-tex-files.tsv").write_text("fixture.tex\t100\t" + str(len(self.value)) + "\t" + toolchain.digest(self.value) + "\n")

    def prepare(self, fetch):
        with patch.object(toolchain, "__file__", str(self.root / "prepare.py")), patch.object(toolchain, "BINARY_SHA", toolchain.digest(self.binary)), patch.object(toolchain, "fetch", side_effect=fetch), patch("sys.argv", ["prepare", "--output", str(self.root)]), redirect_stdout(io.StringIO()):
            toolchain.main()
        return json.loads((self.root / "receipt.json").read_text())

    def test_verified_cache_is_deterministic_and_needs_no_network(self):
        def fetch(url, maximum, byte_range):
            self.assertEqual(byte_range, (100, 100 + len(self.value)))
            self.assertEqual(maximum, len(self.value))
            return self.value
        first = self.prepare(fetch)
        second = self.prepare(lambda *args: self.fail("valid cache should not download"))
        self.assertEqual(first, second)
        with zipfile.ZipFile(self.root / "tectonic.bundle") as archive:
            self.assertEqual(archive.namelist(), ["SHA256SUM", "fixture.tex"])
            self.assertEqual(archive.read("fixture.tex"), self.value)

    def test_tampered_cache_is_refetched_and_provider_mismatch_is_rejected(self):
        bundle = self.root / "tectonic.bundle"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("fixture.tex", b"x" * len(self.value))
        original = bundle.read_bytes()
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.prepare(lambda *args: b"y" * len(self.value))
        self.assertEqual(bundle.read_bytes(), original)
        self.prepare(lambda *args: self.value)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(archive.read("fixture.tex"), self.value)

    def test_path_escape_in_manifest_is_rejected_before_download(self):
        manifest = self.root / "acceptance-tex-files.tsv"
        manifest.write_text("../escape\t0\t1\t" + "0" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "invalid bundle filename"):
            self.prepare(lambda *args: self.fail("invalid manifest should not download"))

    def test_range_response_and_download_bounds_are_enforced(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}
        with patch.object(toolchain.urllib.request, "urlopen", side_effect=lambda *a, **kw: Response(b"too large")), patch.object(toolchain.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "did not honor"):
                toolchain.fetch("https://example.test/public", 1, (1, 2))
            with self.assertRaisesRegex(RuntimeError, "exceeded its bound"):
                toolchain.fetch("https://example.test/public", 1)


if __name__ == "__main__":
    unittest.main()
