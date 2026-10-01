"""HTTP preview paths cannot bypass the worker's remote inference governor."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from job_search.inference.usage import invocation_scope
from job_search.resume_lab.gateway import ResumeLabProductionGateway
from tests.test_resume_lab_gateway import FakeModel, JOB, make_gateway, start_application


class RemoteModel(FakeModel):
    config = SimpleNamespace(provider="runpod_serverless_vllm", producer_version="remote-fixture")

    def __init__(self):
        self.calls = []

    def extract_requirements(self, description):
        self.calls.append("requirements")
        return super().extract_requirements(description)

    def adjudicate_evidence(self, requirement, candidates):
        self.calls.append("evidence")
        return super().adjudicate_evidence(requirement, candidates)

    def normalize_standard_resume(self, tex_source, parsed_text):
        self.calls.append("normalization")
        return super().normalize_standard_resume(tex_source, parsed_text)


class ManagedPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = make_gateway(self.root)
        _ledger, self.application_id = start_application(self.root)
        self.gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        self.model = RemoteModel()
        self.gateway.model = self.model

    def test_http_prepare_ranks_and_enqueues_without_remote_calls(self):
        result = self.gateway.prepare(JOB, application_id=self.application_id,
                                      idempotency_key="managed-http-preview")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(result["ranked_standards"]), 1)
        self.assertEqual(self.model.calls, [])
        artifact = self.gateway.service.get_artifact(result["ranked_standards"][0]["artifact_id"])
        self.assertEqual(artifact["metadata"]["scoring_mode"], "deterministic")

    def test_unmanaged_import_retains_facts_and_reports_missing_normalization(self):
        imported = self.gateway.import_standard("Imported", 2, "Candidate Python systems")
        self.assertEqual(imported["normalization_status"], "needs_normalization")
        self.assertEqual(imported["normalization_error"], "managed_model_work_required")
        self.assertEqual(self.model.calls, [])
        version = self.gateway.service.store.get_standard_version(imported["standard_version_id"])
        self.assertTrue(version["claims"])
        self.assertTrue(version["import_metadata"]["parse_safe"])

    def test_explicit_standalone_opt_in_preserves_normalization(self):
        gateway = ResumeLabProductionGateway(self.gateway.service, self.gateway.artifacts,
            application_db=self.gateway.application_db, model=self.model,
            toolchain=self.gateway.toolchain, allow_unmanaged_remote_inference=True)
        imported = gateway.import_standard("Standalone", 2, "Candidate Python systems")
        self.assertEqual(imported["normalization_status"], "ready")
        self.assertEqual(self.model.calls, ["normalization"])

    def test_concurrent_http_preview_does_not_mutate_worker_model(self):
        job = self.gateway._job(JOB)
        def worker():
            with invocation_scope(self.gateway.application_db, "worker-fixture", 1):
                return self.gateway._extract_graph(job)[2]
        def http():
            return self.gateway._extract_graph(job)[2]
        with ThreadPoolExecutor(max_workers=2) as pool:
            managed, preview = pool.submit(worker), pool.submit(http)
            self.assertEqual(managed.result(), "local_model_plus_fallback")
            self.assertEqual(preview.result(), "deterministic_fallback")
        self.assertIs(self.gateway.model, self.model)
        self.assertEqual(self.model.calls, ["requirements"])
        with invocation_scope(self.root / "other-applications.db", "other-worker", 1):
            self.assertEqual(self.gateway._extract_graph(job)[2], "deterministic_fallback")
        self.assertEqual(self.model.calls, ["requirements"])

    def test_deterministic_preview_cache_cannot_skip_worker_scoring(self):
        ranked, graph = self.gateway._rank_standards(self.gateway._job(JOB))
        standard = self.gateway.service.list_active_standards()[0]
        version = self.gateway.service.store.get_standard_version(standard["active_version_id"])
        with invocation_scope(self.gateway.application_db, "worker-fixture", 1):
            with patch.object(self.gateway, "_score", wraps=self.gateway._score) as score:
                result = self.gateway._register_standard(standard, version, self.gateway._job(JOB),
                                                        graph, [], "deterministic_fallback")
                self.assertEqual(score.call_count, 1)
        self.assertNotEqual(result["artifact_id"], ranked[0]["artifact_id"])
        artifact = self.gateway.service.get_artifact(result["artifact_id"])
        self.assertEqual(artifact["metadata"]["scoring_mode"], "model_eligible")


if __name__ == "__main__":
    unittest.main()
