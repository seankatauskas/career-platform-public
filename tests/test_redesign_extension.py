"""Real paired authentication plus offline candidate evidence queue translation."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from job_search.application_extension import PairedExtensionAdapter
from job_search.application_runtime import ApplicationRuntime
from job_search.commands import DomainError
from job_search.contracts import utc_now
from tests.test_browser_tracking import fixture, observation, URL
from tests.test_job_search_autofill import EXTENSION_ORIGIN


class PairedExtensionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger, self.tracker, self.device, _, self.enrollment = fixture(self.tmp.name)
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / "candidate.sqlite")
        self.adapter = PairedExtensionAdapter(self.runtime, self.tracker.authenticate, "local")
        self.headers = {"Origin": EXTENSION_ORIGIN}

    def send(self, body, route="observations", **options):
        return options.get("adapter", self.adapter).handle(
            "/api/v1/extension/" + route, options.get("headers", self.headers),
            {"device_token": self.enrollment["device_token"], **body})

    def records(self, kind):
        with self.runtime.executor.read() as con:
            return self.runtime.applications.query_records(con, kind)["items"]

    def test_existing_queue_replay_is_durable_and_contains_no_credentials(self):
        first = observation(title="Engineer", employer="Acme", resume_sha256="a" * 64)
        first_result = self.send(first)
        self.assertEqual(self.send(first), first_result)
        restarted = PairedExtensionAdapter(ApplicationRuntime(Path(self.tmp.name) / "candidate.sqlite"), self.tracker.authenticate, "local")
        self.assertEqual(self.send(first, adapter=restarted), first_result)
        with self.assertRaises(DomainError):
            self.send({**first, "title": "Changed bytes"})
        for kind in ("request_sent", "request_completed", "failed", "site_acknowledged"):
            self.send(observation(kind, first["attempt_id"]))
        rows = self.records("observations")
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({r["application_id"] for r in rows}), 1)
        self.assertNotIn(self.enrollment["device_token"], json.dumps(rows))
        self.assertEqual(rows[[r["source"]["kind"] for r in rows].index("attempted")]["documents"][0]["sha256"], "a" * 64)
        self.assertEqual(self.records("submissions"), [])
        self.assertEqual(first_result["status"], "pending_review")
        pending = self.runtime.queries.review_queue()["items"]
        self.assertTrue(pending)
        self.assertEqual({p["status"] for p in pending}, {"pending"})
        self.assertEqual(self.ledger.list_applications(), [])

    def test_real_authenticator_rejects_wrong_origin_audience_revocation_and_spoofing(self):
        body = observation()
        for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "chrome-extension://" + "b" * 32}):
            with self.assertRaises(DomainError) as caught:
                self.send(body, headers=headers)
            self.assertEqual(caught.exception.code, "not_authorized")
        with self.assertRaises(DomainError):
            self.send({**body, "device_token": "fictional-invalid-token"})
        wrong_audience = PairedExtensionAdapter(self.runtime, self.tracker.authenticate, "another-host")
        with self.assertRaises(DomainError):
            self.send(body, adapter=wrong_audience)
        for key in ("device_id", "actor_kind", "application_id", "capabilities"):
            with self.assertRaises(DomainError):
                self.send({**body, key: "human"})
        self.tracker.revoke(self.device)
        with self.assertRaises(DomainError):
            self.send(body)
        self.assertEqual(self.records("observations"), [])

    def test_late_answer_queue_keeps_exact_typed_snapshot_without_accepting_submission(self):
        first = observation(resume_sha256="f" * 64)
        started = self.send(first)
        self.send(observation("site_acknowledged", first["attempt_id"]))
        snapshot = {"version": 1, "fields": [
            {"field_key": "why", "prompt": "Pourquoi?", "section": "Résumé", "control": "textarea", "value": "  cafe\u0301\n東京 🚀  "},
            {"field_key": "yes", "prompt": "Yes?", "section": "", "control": "checkbox", "value": False},
            {"field_key": "options", "prompt": "Choose", "section": "", "control": "select", "value": ["é", "e\u0301"]},
            {"field_key": "files", "prompt": "Uploaded", "section": "", "control": "file", "value": ["résumé.pdf"]},
        ], "omitted_fields": 1, "truncated_values": 2}
        capture = {"capture_id": uuid.uuid4().hex, "attempt_id": first["attempt_id"], "page_url": URL,
                   "captured_at": utc_now(), "snapshot": snapshot}
        result = self.send(capture, "answers")
        self.assertEqual(self.send(capture, "answers"), result)
        self.assertEqual(result["application_id"], started["application_id"])
        self.assertTrue(result["saved"])
        self.assertEqual(result["capture_id"], capture["capture_id"])
        self.assertEqual(result["field_count"], 4)
        row = next(r for r in self.records("observations") if r["activity"] == "answer_capture")
        self.assertEqual(row["source"]["answer_snapshot"], snapshot)
        self.assertEqual(row["answers"]["why"], snapshot["fields"][0]["value"])
        with self.assertRaises(DomainError):
            self.send({**capture, "snapshot": {**snapshot, "omitted_fields": 0}}, "answers")
        self.assertEqual(self.records("submissions"), [])

    def test_attempt_identity_and_capture_prerequisites_are_owned_atomically(self):
        first = observation()
        with self.assertRaises(DomainError):
            self.send(observation("site_acknowledged", first["attempt_id"]))
        self.send(first)
        with self.assertRaises(DomainError):
            self.send(observation("failed", first["attempt_id"], URL.replace("12345", "98765")))
        with self.assertRaises(DomainError):
            self.send(observation("failed", first["attempt_id"], URL.replace("/acme/", "/other/")))
        second = self.tracker.enroll(self.tracker.issue_pairing("local")["pairing_code"], EXTENSION_ORIGIN, "local")
        with self.assertRaises(DomainError):
            self.send({**observation("failed", first["attempt_id"]), "device_token": second["device_token"]})
        capture = {"capture_id": uuid.uuid4().hex, "attempt_id": uuid.uuid4().hex, "page_url": URL,
                   "captured_at": utc_now(), "snapshot": {"version": 1, "fields": [], "omitted_fields": 0, "truncated_values": 0}}
        with self.assertRaises(DomainError):
            self.send(capture, "answers")
        self.assertEqual(len(self.records("observations")), 1)

    def test_malformed_input_and_unrelated_extension_routes_do_not_write(self):
        for patch in ({"metadata": {"authority": "human"}}, {"occurred_at": "tomorrow"},
                      {"resume_sha256": "bad"}, {"kind": []}, {"page_url": "https://evil.example/job"},
                      {"kind": "site_acknowledged", "metadata": {"signal": "guess"}}):
            with self.assertRaises(DomainError):
                self.send({**observation(), **patch})
        with self.assertRaises(DomainError):
            self.send({}, "capture")
        self.assertEqual(self.records("observations"), [])


if __name__ == "__main__":
    unittest.main()
