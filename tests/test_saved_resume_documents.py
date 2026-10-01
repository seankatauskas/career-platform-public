"""Saved resume downloads read the identified immutable PDF, without generation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_search.resume_lab.artifacts import ArtifactIntegrityError, ArtifactNamespace, InvalidArtifactError
from job_search.resume_lab.contracts import ResumeBoundaryError, ResumeNotFoundError
from tests.test_resume_lab_gateway import make_gateway
from tests.test_resume_lab_integration import browser_session, request, resume_dashboard


class SavedResumeDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = make_gateway(self.root)
        self.saved = self.gateway.import_standard("My resume", 1, "Python infrastructure engineer")
        self.version_id = self.saved["standard_version_id"]
        self.version = self.gateway.service.store.get_standard_version(self.version_id)

    def test_public_urls_and_read_are_exact_without_generation_or_preferred_fallback(self):
        base = f"/api/v1/resume-lab/standards/{self.version_id}/document"
        standard = self.gateway.list_standards()["standards"][0]
        self.assertEqual(standard["document_url"], base)
        self.assertEqual(standard["preview_url"], base + "?disposition=inline")
        self.assertNotIn("managed_relative_path", standard)
        with patch.object(self.gateway, "_build_standard_version", side_effect=AssertionError("must not generate")), patch.object(self.gateway, "get_autofill_resume", side_effect=AssertionError("must not use preferred resume")):
            document = self.gateway.get_standard_document(self.version_id)
        self.assertEqual(document.artifact_id, self.version_id)
        self.assertEqual(document.sha256, self.version["import_metadata"]["pdf_sha256"])
        self.assertIn(b"Python infrastructure engineer", document.content)
        self.assertEqual(document.content_type, "application/pdf")

    def test_http_document_disposition_and_errors(self):
        expected = self.gateway.get_standard_document(self.version_id)
        base = f"/api/v1/resume-lab/standards/{self.version_id}/document"
        with resume_dashboard(self.gateway) as (server, _controller, _gateway):
            cookie, _csrf = browser_session(server)
            for suffix, disposition in [("", "attachment"), ("?disposition=attachment", "attachment"), ("?disposition=inline", "inline")]:
                status, headers, content = request(server, "GET", base + suffix, headers={"Cookie": cookie})
                self.assertEqual(status, 200)
                self.assertEqual(content, expected.content)
                self.assertEqual(headers["content-type"], "application/pdf")
                self.assertTrue(headers["content-disposition"].startswith(disposition + ";"))
            for suffix in ["?disposition=unexpected", "?disposition=inline&disposition=attachment", "?unknown=value"]:
                status, _headers, _content = request(server, "GET", base + suffix, headers={"Cookie": cookie})
                self.assertEqual(status, 400)
            status, _headers, _content = request(server, "GET", "/api/v1/resume-lab/standards/missing-version/document", headers={"Cookie": cookie})
            self.assertEqual(status, 404)

    def test_expected_digest_must_match_this_version(self):
        expected = self.version["import_metadata"]["pdf_sha256"]
        self.assertEqual(self.gateway.get_standard_document(self.version_id, expected_sha256=expected).sha256, expected)
        with self.assertRaises(ResumeBoundaryError):
            self.gateway.get_standard_document(self.version_id, expected_sha256="0" * 64)

    def test_old_version_stays_available_after_replacement(self):
        original = self.gateway.get_standard_document(self.version_id)
        changed = self.gateway.update_standard(self.saved["standard_id"], "Updated Python engineer with Kubernetes")
        self.assertNotEqual(changed["standard_version_id"], self.version_id)
        self.assertEqual(self.gateway.get_standard_document(self.version_id), original)
        self.assertNotEqual(self.gateway.get_standard_document(changed["standard_version_id"]).sha256, original.sha256)

    def test_missing_pdf_metadata_has_no_download_url_or_fallback(self):
        version = {**self.version, "import_metadata": {}}
        public = self.gateway._public_standard(self.gateway.service.list_active_standards()[0], version)
        self.assertNotIn("document_url", public)
        self.assertNotIn("preview_url", public)
        with patch.object(self.gateway.service.store, "get_standard_version", return_value=version):
            with self.assertRaises(ResumeNotFoundError):
                self.gateway.get_standard_document(self.version_id)
        with self.assertRaises(ResumeNotFoundError):
            self.gateway.get_standard_document("missing-version")

    def test_corrupted_pdf_is_rejected(self):
        location = self.root / "artifacts" / self.version["import_metadata"]["managed_relative_path"]
        location.write_bytes(b"%PDF-1.7\nwrong document\n%%EOF")
        with self.assertRaises(ArtifactIntegrityError):
            self.gateway.get_standard_document(self.version_id)

    def test_locator_traversal_is_rejected(self):
        metadata = {**self.version["import_metadata"], "managed_relative_path": "../outside.pdf"}
        with patch.object(self.gateway.service.store, "get_standard_version", return_value={**self.version, "import_metadata": metadata}):
            with self.assertRaises(InvalidArtifactError):
                self.gateway.get_standard_document(self.version_id)

    def test_research_namespace_cannot_be_served_as_a_saved_resume(self):
        saved = self.gateway.artifacts.write_pdf(b"%PDF-1.7\nresearch fixture\n%%EOF", ArtifactNamespace.RESEARCH)
        metadata = {"managed_relative_path": saved.managed_relative_path, "pdf_sha256": saved.sha256}
        with patch.object(self.gateway.service.store, "get_standard_version", return_value={**self.version, "import_metadata": metadata}):
            with self.assertRaises(ResumeBoundaryError):
                self.gateway.get_standard_document(self.version_id)


if __name__ == "__main__":
    unittest.main()
