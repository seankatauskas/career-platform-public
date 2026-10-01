"""Recorded documents must retain their submission identity across preference changes."""
import unittest
from unittest.mock import Mock

from job_search.application_documents import (
    ApplicationDocumentUnavailable, application_document_content, application_documents,
)
from job_search.resume_integration import ResumeArtifactContent


class ApplicationDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Mock()
        self.gateway = Mock()
        self.pdf = ResumeArtifactContent("old-artifact", "resume.pdf", "application/pdf", b"%PDF-old")
        self.gateway.get_artifact.return_value = self.pdf
        self.gateway.get_selection.return_value = {"selection": {"artifact_id": "new-artifact"}}
        self.timeline = {"application": {"current_phase": "active", "submitted_at": "2026-09-01"}, "events": []}
        self.ledger.get_application_timeline.return_value = self.timeline

    def event(self, snapshot, kind="submission_observed", seq=1):
        return {"event_type": kind, "event_seq": seq, "occurred_at": "2026-09-01T12:00:00Z", "payload": {"resume": snapshot}}

    def test_submission_snapshot_wins_over_current_selection(self):
        self.timeline["events"] = [self.event({"decision": "selected", "artifact_id": "old-artifact", "name": "Original resume"})]
        row = application_documents(self.ledger, self.gateway, "app-1")[0]
        self.assertTrue(row["available"])
        self.assertEqual(row["name"], "Original resume")
        self.assertEqual(row["preview_url"], "/api/v1/applications/app-1/documents/resume")
        self.gateway.get_artifact.assert_called_once_with("old-artifact")
        self.gateway.get_selection.assert_not_called()

    def test_missing_or_opted_out_snapshot_does_not_use_current_selection(self):
        for events in ([], [self.event(None)], [self.event({"decision": "not_tracked"})]):
            self.timeline["events"] = events
            self.assertEqual(application_documents(self.ledger, self.gateway, "app-1"), [])
        self.gateway.get_selection.assert_not_called()
        self.gateway.get_artifact.assert_not_called()

    def test_observed_snapshot_wins_over_later_email(self):
        self.timeline["events"] = [self.event({"decision": "selected", "artifact_id": "wrong"}, "submission_confirmed", 2), self.event({"decision": "selected", "artifact_id": "old-artifact"})]
        application_document_content(self.ledger, self.gateway, "app-1")
        self.gateway.get_artifact.assert_called_once_with("old-artifact")

    def test_matched_upload_reads_exact_historical_version_and_digest(self):
        self.timeline["events"] = [self.event({"decision": "matched_upload", "standard_version_id": "historical-version", "sha256": "saved-digest", "name": "Uploaded resume"})]
        self.gateway.get_standard_document.return_value = ResumeArtifactContent(
            "historical-version", "saved.pdf", "application/pdf", b"%PDF-upload", "saved-digest")
        result = application_document_content(self.ledger, self.gateway, "app-1")
        self.assertEqual(result.content, b"%PDF-upload")
        self.gateway.get_standard_document.assert_called_once_with("historical-version", expected_sha256="saved-digest")
        self.gateway.get_autofill_resume.assert_not_called()

    def test_changed_or_missing_file_preserves_identity_without_download(self):
        self.timeline["events"] = [self.event({"decision": "selected", "artifact_id": "old-artifact", "name": "Original"})]
        self.gateway.get_artifact.side_effect = OSError("private path")
        row = application_documents(self.ledger, self.gateway, "app-1")[0]
        self.assertEqual(row["name"], "Original")
        self.assertFalse(row["available"])
        self.assertIsNone(row["url"])
        self.assertNotIn("private", row["reason"])
        with self.assertRaisesRegex(ApplicationDocumentUnavailable, "unavailable"):
            application_document_content(self.ledger, self.gateway, "app-1")

    def test_mismatched_upload_digest_does_not_read_pdf(self):
        self.timeline["events"] = [self.event({"decision": "matched_upload", "standard_version_id": "historical", "sha256": "saved"})]
        self.gateway.get_standard_document.side_effect = ValueError("digest mismatch")
        self.assertFalse(application_documents(self.ledger, self.gateway, "app-1")[0]["available"])
        self.gateway.artifacts.read_pdf.assert_not_called()

    def test_saved_draft_can_show_own_selection_without_mutation(self):
        self.timeline["application"] = {"current_phase": "preparing", "submitted_at": None}
        row = application_documents(self.ledger, self.gateway, "draft-1")[0]
        self.assertEqual(row["source"], "Saved with draft")
        self.gateway.get_artifact.assert_called_once_with("new-artifact")

    def test_unconfigured_storage_preserves_recorded_document_identity(self):
        self.timeline["events"] = [self.event({"decision": "selected", "artifact_id": "old", "name": "Known resume"})]
        row = application_documents(self.ledger, None, "app-1")[0]
        self.assertEqual(row["name"], "Known resume")
        self.assertFalse(row["available"])


if __name__ == "__main__":
    unittest.main()
