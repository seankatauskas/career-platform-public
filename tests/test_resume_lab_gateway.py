#!/usr/bin/env python3
"""Offline end-to-end checks for the production resume-lab orchestrator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from job_search.contracts import (
    JobSnapshot as LedgerJobSnapshot,
    MutationContext,
    RecommendationProvenance,
)
from job_search.resume_lab.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ResumePdfArtifactRepository,
)
from job_search.resume_lab.contracts import (
    ArtifactInput,
    ClaimOrigin,
    ResumeBoundaryError,
    ResumeClaim,
    ResumeConflictError,
    ResumeLabError,
    ResumePurpose,
    StandardVersionInput,
    VariantKind,
)
from job_search.resume_lab.fidelity import FidelityView, PdfFidelityReport
from job_search.resume_lab.gateway import (
    ResumeLabProductionGateway,
    SYNTHETIC_NOTICE,
    enqueue_resume_run,
)
from job_search.resume_lab.model import (
    NORMALIZATION_VALIDATOR_REVISION,
    ResumeModelError,
    validate_variant_output,
)
from job_search.resume_lab.pdf import PdfExtraction
from job_search.resume_lab.runpod_model import RunpodReconciliationRequired
from job_search.resume_lab.requirements import extract_requirement_graph
from job_search.resume_lab.requirements import MAX_REQUIREMENTS
from job_search.resume_lab.service import ResumeLabService
from job_search.resume_lab.tex import CompiledPdf, render_resume_tex
from job_search.resume_lab.toolchain import ResumeArtifactBuild
from job_search.service import JobSearchLedger


JOB = {
    "ats": "ashby",
    "id": "job-1",
    "title": "Platform Engineer",
    "company": "Acme",
    "description": (
        "Required: Python and Kubernetes production experience. "
        "Preferred: PostgreSQL experience."
    ),
}


def _pdf(source: str) -> bytes:
    return b"%PDF-1.7\n" + source.encode("utf-8") + b"\n%%EOF"


class FakeCompiler:
    def compile(self, source: str) -> CompiledPdf:
        content = _pdf(source)
        return CompiledPdf(
            content,
            hashlib.sha256(content).hexdigest(),
            hashlib.sha256(source.encode()).hexdigest(),
            "fake-tectonic",
            "1.0",
            "a" * 64,
            "",
        )


class FakeExtractor:
    def extract(self, pdf: bytes) -> PdfExtraction:
        body = pdf.split(b"\n", 1)[1].rsplit(b"\n%%EOF", 1)[0].decode()
        digest = hashlib.sha256(pdf).hexdigest()
        return PdfExtraction(digest, "pypdf", "fake", 1, len(pdf), body, body)


class FakeToolchain:
    def __init__(self) -> None:
        self.compiler = FakeCompiler()
        self.extractor = FakeExtractor()

    def build(self, content, *, allow_warning=True, visible_notice=""):
        assert allow_warning is False
        rendered = render_resume_tex(content, visible_notice=visible_notice)
        compiled = self.compiler.compile(rendered.intended_text)
        extracted = self.extractor.extract(compiled.pdf_bytes)
        perfect = FidelityView(1.0, 1.0, 1.0, 0.0)
        fidelity = PdfFidelityReport("pass", perfect, perfect, (), ())
        return ResumeArtifactBuild(rendered, compiled, extracted, fidelity)


class FakeModel:
    config = SimpleNamespace(
        producer_version="fake-model-v1", model_sha256="a" * 64
    )

    @staticmethod
    def extract_requirements(description: str):
        required = "Required: Python and Kubernetes production experience."
        preferred = "Preferred: PostgreSQL experience."
        return {
            "requirements": [
                {
                    "requirement_id": "model-required",
                    "text": required,
                    "kind": "required",
                    "logic": "all",
                    "priority": 2,
                    "source_start": description.index(required),
                    "source_end": description.index(required) + len(required),
                },
                {
                    "requirement_id": "model-preferred",
                    "text": preferred,
                    "kind": "preferred",
                    "logic": "atomic",
                    "priority": 1,
                    "source_start": description.index(preferred),
                    "source_end": description.index(preferred) + len(preferred),
                },
            ]
        }

    @staticmethod
    def normalize_standard_resume(_tex_source: str, parsed_text: str):
        name, summary = parsed_text.split(maxsplit=1)
        content = {
            "identity": {"name": name},
            "summary": summary,
            "experience": [],
            "projects": [],
            "education": [],
            "skills": [],
        }
        summary_start = parsed_text.index(summary)
        return {
            "content": content,
            "fixed_fields": [
                {
                    "path": "/identity/name",
                    "text": name,
                    "source_start": 0,
                    "source_end": len(name),
                }
            ],
            "claims": [
                {
                    "claim_id": "source_summary",
                    "path": "/summary",
                    "text": summary,
                    "source_start": summary_start,
                    "source_end": len(parsed_text),
                    "allowed_equivalent_terms": [],
                }
            ],
        }

    @staticmethod
    def adjudicate_evidence(_requirement, _candidates):
        return {
            "status": "not_evidenced",
            "confidence": 1.0,
            "reason": "No additional semantic evidence.",
            "evidence": [],
        }

    @staticmethod
    def generate_variant(
        variant_kind,
        _job,
        standards,
        source_claims,
        _guarded_terms=(),
        *,
        generation_seed=None,
        optimization_input=None,
    ):
        assert isinstance(generation_seed, int)
        assert optimization_input is None or variant_kind == "keyword_adversarial"
        if variant_kind == "grounded_rewrite":
            text = source_claims[0]["text"]
            content = dict(standards[0]["content"])
            content["summary"] = text
            origin = "source_rewrite"
            sources = [source_claims[0]["claim_id"]]
        elif variant_kind == "standard_exaggerated":
            text = "Python Kubernetes PostgreSQL distributed systems"
            content = dict(standards[0]["content"])
            content["summary"] = text
            origin = "synthetic"
            sources = []
        else:
            text = "Python Kubernetes PostgreSQL distributed systems"
            content = {
                "identity": {"name": "Synthetic Candidate"},
                "summary": text,
                "experience": [],
                "projects": [],
                "education": [],
                "skills": [],
            }
            origin = (
                "keyword_adversarial"
                if variant_kind == "keyword_adversarial"
                else "synthetic"
            )
            sources = []
        return {
            "variant_kind": variant_kind,
            "base_standard_id": (
                standards[0]["standard_id"]
                if variant_kind in {"grounded_rewrite", "standard_exaggerated"}
                else None
            ),
            "synthetic": variant_kind != "grounded_rewrite",
            "research_only": variant_kind != "grounded_rewrite",
            "content": content,
            "claims": [
                {
                    "claim_id": "generated_summary",
                    "path": "/summary",
                    "text": text,
                    "origin": origin,
                    "source_claim_ids": sources,
                    "added_equivalent_terms": [],
                }
            ],
        }


class Context:
    @staticmethod
    def heartbeat():
        return True


def make_gateway(root: Path):
    return ResumeLabProductionGateway(
        ResumeLabService(root / "resume.db"),
        ResumePdfArtifactRepository(root / "artifacts"),
        application_db=root / "applications.db",
        model=FakeModel(),
        toolchain=FakeToolchain(),
    )


def test_import_revalidates_model_normalization_and_records_coverage_revision() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        imported = gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )
        version = gateway.service.store.get_standard_version(
            str(imported["standard_version_id"])
        )
        assert imported["normalization_status"] == "ready"
        assert imported["normalized"] is True
        assert (
            version["import_metadata"]["normalization_validator_revision"]
            == NORMALIZATION_VALIDATOR_REVISION
        )

        class OmittingModel(FakeModel):
            @staticmethod
            def normalize_standard_resume(_tex_source: str, parsed_text: str):
                name, retained, _omitted = parsed_text.split(maxsplit=2)
                return {
                    "content": {
                        "identity": {"name": name},
                        "summary": retained,
                        "experience": [],
                        "projects": [],
                        "education": [],
                        "skills": [],
                    },
                    "fixed_fields": [
                        {
                            "path": "/identity/name",
                            "text": name,
                            "source_start": 0,
                            "source_end": len(name),
                        }
                    ],
                    "claims": [
                        {
                            "claim_id": "retained_summary",
                            "path": "/summary",
                            "text": retained,
                            "source_start": parsed_text.index(retained),
                            "source_end": parsed_text.index(retained) + len(retained),
                        }
                    ],
                }

        omitting_gateway = ResumeLabProductionGateway(
            ResumeLabService(root / "omitting-resume.db"),
            ResumePdfArtifactRepository(root / "omitting-artifacts"),
            application_db=root / "omitting-applications.db",
            model=OmittingModel(),
            toolchain=FakeToolchain(),
        )
        rejected = omitting_gateway.import_standard(
            "Incomplete", 1, "Python Kubernetes production systems"
        )
        assert rejected["normalization_status"] == "needs_normalization"
        assert rejected["normalized"] is False


def test_update_standard_adds_and_activates_an_immutable_version() -> None:
    with tempfile.TemporaryDirectory() as directory:
        gateway = make_gateway(Path(directory))
        imported = gateway.import_standard(
            "Platform",
            3,
            "Candidate Python platform engineering",
        )
        standard_id = str(imported["standard_id"])
        original_version_id = str(imported["standard_version_id"])
        original = gateway.service.store.get_standard_version(original_version_id)

        updated = gateway.update_standard(
            standard_id,
            "Candidate Python Kubernetes platform engineering",
        )
        active_version_id = str(updated["standard_version_id"])
        current = gateway.service.store.get_standard_version(active_version_id)
        original_after = gateway.service.store.get_standard_version(
            original_version_id
        )
        standard = gateway.service.store.get_standard(standard_id)

        assert updated["standard_id"] == standard_id
        assert active_version_id != original_version_id
        assert standard["active_version_id"] == active_version_id
        assert standard["name"] == "Platform" and standard["manual_rank"] == 3
        assert current["version_number"] == original["version_number"] + 1
        assert current["tex_source"] == (
            "Candidate Python Kubernetes platform engineering"
        )
        assert original_after == original
        assert original_after["tex_source"] == "Candidate Python platform engineering"
        assert gateway.list_standards()["standards"][0][
            "standard_version_id"
        ] == active_version_id


def start_application(
    root: Path,
    *,
    job: dict = JOB,
    idempotency_key: str = "start-resume-application",
):
    ledger = JobSearchLedger(root / "applications.db")
    started = ledger.start_application(
        LedgerJobSnapshot(
            ats=str(job["ats"]),
            job_id=str(job["id"]),
            family_id="family-platform",
            title=str(job["title"]),
            employer=str(job["company"]),
            company_slug="acme",
            job_url=f"https://jobs.ashbyhq.com/acme/{job['id']}",
        ),
        RecommendationProvenance(),
        MutationContext(idempotency_key, "user", "dashboard"),
    )
    return ledger, str(started["application"]["application_id"])


def test_full_comparison_uses_best_handwritten_resume_and_enforces_boundaries() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        weak = gateway.import_standard("General", 1, "Excel reporting")
        strong = gateway.import_standard(
            "Platform", 2, "Python Kubernetes production systems"
        )
        assert weak["normalization_status"] == strong["normalization_status"] == "ready"

        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-1",
        )
        assert prepared["status"] == "queued"
        assert prepared["ranked_standards"][0]["name"] == "Platform"
        assert prepared["ranked_standards"][0]["primary"] is True
        assert prepared["selected_standard_id"] == strong["standard_id"]
        assert prepared["selected_standard_id"] != weak["standard_id"]

        with sqlite3.connect(root / "applications.db") as connection:
            row = connection.execute(
                "SELECT lane,task_kind,payload_json FROM work_items"
            ).fetchone()
        assert row[:2] == ("model", "resume.optimize")
        assert json.loads(row[2]) == {"run_id": prepared["run_id"]}
        assert "description" not in row[2] and "resume" not in row[2]

        outcome = gateway.handle_work(
            {"run_id": prepared["run_id"]},
            Context(),  # type: ignore[arg-type]
        )
        assert outcome == {
            "run_id": prepared["run_id"],
            "status": "succeeded",
            "completed": 4,
            "failed": 0,
        }
        result = gateway.get_run_result(prepared["run_id"])
        assert result["status"] == "succeeded"
        assert result["ranked_standards"][0]["name"] == "Platform"
        assert result["ranked_standards"][0]["score"] is not None
        assert [item["comparison_kind"] for item in result["comparisons"]] == [
            "grounded_rewrite",
            "standard_exaggerated",
            "market_ideal",
            "keyword_adversarial",
        ]
        assert all(item.get("artifact_id") for item in result["comparisons"])
        reopened = gateway.get_application_workspace(application_id)
        assert reopened["run_id"] == prepared["run_id"]
        assert reopened["ranked_standards"][0]["name"] == "Platform"
        hermes_view = gateway.compare_for_job("ashby", "job-1")
        assert hermes_view["ranked_standards"][0]["name"] == "Platform"
        synthetic = result["comparisons"][1]
        stored = gateway.service.get_artifact(
            synthetic["artifact_id"], include_content=True
        )
        assert stored["purpose"] == "synthetic_research"
        assert SYNTHETIC_NOTICE in stored["parsed_text"]
        try:
            gateway.select_resume(
                application_id,
                job=JOB,
                artifact_id=synthetic["artifact_id"],
                evaluation_id=synthetic["evaluation_id"],
                idempotency_key="select-synthetic",
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("synthetic research artifact became selectable")

        primary = prepared["ranked_standards"][0]
        selected = gateway.select_resume(
            application_id,
            job=JOB,
            artifact_id=primary["artifact_id"],
            evaluation_id=primary["evaluation_id"],
            idempotency_key="select-standard",
        )
        assert selected["selection"]["comparison_kind"] == "standard"

        grounded = result["comparisons"][0]
        try:
            gateway.select_resume(
                application_id,
                job=JOB,
                artifact_id=grounded["artifact_id"],
                evaluation_id=grounded["evaluation_id"],
                idempotency_key="select-unapproved",
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("unapproved grounded rewrite became selectable")
        approved = gateway.approve_run(
            prepared["run_id"],
            comparison_kind="grounded_rewrite",
            idempotency_key="approve-grounded",
        )
        assert approved["comparisons"][0]["approved"] is True
        grounded_selection = gateway.select_resume(
            application_id,
            job=JOB,
            artifact_id=grounded["artifact_id"],
            evaluation_id=grounded["evaluation_id"],
            idempotency_key="select-grounded",
        )
        assert grounded_selection["selection"]["comparison_kind"] == "grounded_rewrite"
        assert grounded_selection["selection"]["standard_id"] == strong["standard_id"]
        assert grounded_selection["selection"]["standard_version_id"] == strong[
            "standard_version_id"
        ]
        assert grounded_selection["selection"]["name"] == "Platform"
        downloaded = gateway.get_artifact(grounded["artifact_id"])
        assert downloaded.content.startswith(b"%PDF-")
        assert "/" not in downloaded.filename


def test_run_store_rejects_tampered_ranking_scores_order_and_duplicates() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        gateway.import_standard("General", 1, "Excel reporting")
        gateway.import_standard(
            "Platform", 2, "Python Kubernetes production systems"
        )
        job = gateway._job(JOB)
        ranked, graph = gateway._rank_standards(job)
        winner = ranked[0]
        standard_artifact = gateway.service.get_artifact(
            str(winner["artifact_id"]), include_content=True
        )
        metadata = standard_artifact["metadata"]

        def create(rows, key):
            return gateway.service.create_run(
                "application-ranking-integrity",
                job,
                key,
                standard_id=str(winner["standard_id"]),
                expected_base_version_id=str(winner["standard_version_id"]),
                ranked_standards=rows,
                requirement_graph=graph,
                requirement_clauses=tuple(metadata["requirement_clauses"]),
                requirement_extraction=str(metadata["requirement_extraction"]),
            )

        altered_score = json.loads(json.dumps(ranked))
        altered_score[0]["score"] = 999
        try:
            create(altered_score, "tampered-ranking-score")
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a caller-authored ranking score was persisted")

        altered_order = list(reversed(json.loads(json.dumps(ranked))))
        for index, item in enumerate(altered_order, 1):
            item["rank"] = index
            item["primary"] = index == 1
        try:
            create(altered_order, "tampered-ranking-order")
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("caller-authored ranking order was persisted")

        duplicate = [json.loads(json.dumps(ranked[0])) for _ in range(2)]
        for index, item in enumerate(duplicate, 1):
            item["rank"] = index
            item["primary"] = index == 1
        try:
            create(duplicate, "duplicate-ranking-row")
        except ResumeLabError:
            pass
        else:
            raise AssertionError("a duplicate ranking row was persisted")

        for label, mutate in (
            ("unknown-key", lambda row: row.__setitem__("private_payload", "x")),
            (
                "active-version",
                lambda row: row.__setitem__("active_version_id", "version-tampered"),
            ),
            ("normalized", lambda row: row.__setitem__("normalized", False)),
            ("parse-safe", lambda row: row.__setitem__("parse_safe", False)),
        ):
            altered = json.loads(json.dumps(ranked))
            mutate(altered[0])
            try:
                create(altered, f"tampered-ranking-{label}")
            except ResumeLabError:
                pass
            else:
                raise AssertionError(f"tampered ranking {label} was persisted")


def test_exact_standard_score_is_reused_with_model_provenance() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        original_score = gateway._score
        calls = {"count": 0}

        def count_score(*args, **kwargs):
            calls["count"] += 1
            return original_score(*args, **kwargs)

        gateway._score = count_score  # type: ignore[method-assign]
        first, first_graph = gateway._rank_standards(gateway._job(JOB))
        second, second_graph = gateway._rank_standards(gateway._job(JOB))
        assert calls["count"] == 1
        assert first_graph.fingerprint == second_graph.fingerprint
        assert first[0]["artifact_id"] == second[0]["artifact_id"]
        artifact = gateway.service.get_artifact(str(first[0]["artifact_id"]))
        provenance = artifact["metadata"]["model_provenance"]
        assert provenance["producer_version"] == "fake-model-v1"
        assert provenance["model_sha256"] == "a" * 64
        assert provenance["protocol_schema_version"] == 1
        assert len(provenance["config_sha256"]) == 64


def test_prepare_fails_closed_for_an_unbounded_requirement_graph() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        oversized_job = {
            **JOB,
            "description": "\n".join(
                f"Required Python capability marker {index}."
                for index in range(MAX_REQUIREMENTS + 1)
            ),
        }
        result = gateway.prepare(
            oversized_job,
            application_id=application_id,
            idempotency_key="oversized-prepare",
        )
        assert result["status"] == "blocked_setup"
        assert result["reason"] == "job_description_too_complex"
        assert result["comparisons"] == []


def test_prepare_is_idempotent_and_read_views_hide_sensitive_internals() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        first = gateway.prepare(
            JOB, application_id=application_id, idempotency_key="same-prepare"
        )
        second = gateway.prepare(
            JOB, application_id=application_id, idempotency_key="same-prepare"
        )
        assert first["run_id"] == second["run_id"]
        with sqlite3.connect(root / "applications.db") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
            )
        public = json.dumps(gateway.compare_for_job("ashby", "job-1"))
        for forbidden in (
            "managed_relative_path",
            "tex_source",
            "parsed_text",
            "claims",
            JOB["description"],
        ):
            assert forbidden not in public


def test_synthetic_lineage_records_the_seed_applied_to_generation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="seed-lineage-run",
        )
        applied: dict[str, list[int]] = {}

        class SeedRecordingModel(FakeModel):
            @staticmethod
            def generate_variant(variant_kind, *args, generation_seed=None, **kwargs):
                assert isinstance(generation_seed, int)
                applied.setdefault(variant_kind, []).append(generation_seed)
                return FakeModel.generate_variant(
                    variant_kind,
                    *args,
                    generation_seed=generation_seed,
                    **kwargs,
                )

        gateway.model = SeedRecordingModel()
        gateway.handle_work(
            {"run_id": prepared["run_id"]}, Context()  # type: ignore[arg-type]
        )
        result = gateway.get_run_result(str(prepared["run_id"]))
        for comparison in result["comparisons"]:
            kind = comparison["comparison_kind"]
            artifact = gateway.service.get_artifact(str(comparison["artifact_id"]))
            assert len(set(applied[kind])) == 1
            if comparison["research_only"]:
                assert artifact["generation_seed"] == applied[kind][0]
                assert 0 <= int(artifact["generation_seed"]) <= 2_147_483_647


def test_only_adversarial_variant_gets_actionable_second_pass_feedback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        graph = extract_requirement_graph(gateway._job(JOB))
        calls = []

        class FeedbackModel(FakeModel):
            @staticmethod
            def generate_variant(
                variant_kind,
                *args,
                generation_seed=None,
                optimization_input=None,
                **kwargs,
            ):
                calls.append((variant_kind, optimization_input))
                result = FakeModel.generate_variant(
                    variant_kind,
                    *args,
                    generation_seed=generation_seed,
                    optimization_input=optimization_input,
                    **kwargs,
                )
                if optimization_input is None:
                    result["content"]["summary"] = "Unrelated background"
                    result["claims"][0]["text"] = "Unrelated background"
                return result

        gateway.model = FeedbackModel()
        adversarial = gateway._generate_output(
            VariantKind.KEYWORD_ADVERSARIAL,
            gateway._job(JOB),
            graph,
            17,
            (),
            (),
            gateway._guarded_terms(graph),
        )
        assert len(calls) == 2
        feedback = calls[1][1]
        assert feedback["optimization_pass"] == 2
        assert feedback["prior_content"]["summary"] == "Unrelated background"
        assert feedback["remaining_gaps"]
        assert all(item["source_text"] for item in feedback["remaining_gaps"])
        assert all(
            "missing_term_groups" in item for item in feedback["remaining_gaps"]
        )
        assert adversarial["content"]["summary"] != "Unrelated background"

        calls.clear()
        gateway._generate_output(
            VariantKind.MARKET_IDEAL,
            gateway._job(JOB),
            graph,
            17,
            (),
            (),
            gateway._guarded_terms(graph),
        )
        assert len(calls) == 1 and calls[0][1] is None


def test_runpod_reconciliation_is_never_swallowed_by_optional_model_fallbacks() -> None:
    def assert_reconciliation(operation, job_id: str) -> None:
        try:
            operation()
        except RunpodReconciliationRequired as exc:
            assert exc.job_id == job_id
        else:
            raise AssertionError("Runpod reconciliation was swallowed by a fallback")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        job_id = "accepted-optional-job"

        class AmbiguousExtraction(FakeModel):
            @staticmethod
            def extract_requirements(_description):
                raise RunpodReconciliationRequired(job_id)

        gateway = make_gateway(root)
        gateway.model = AmbiguousExtraction()
        assert_reconciliation(lambda: gateway._extract_graph(gateway._job(JOB)), job_id)

        class AmbiguousNormalization(FakeModel):
            @staticmethod
            def normalize_standard_resume(_tex_source, _plain_text):
                raise RunpodReconciliationRequired(job_id)

        gateway.model = AmbiguousNormalization()
        assert_reconciliation(
            lambda: gateway.import_standard(
                "Ambiguous normalization", 1, "Candidate Python systems"
            ),
            job_id,
        )

        resume_text = "Built reliable distributed backend services."
        graph = extract_requirement_graph(gateway._job(JOB))
        claims = [{"claim_id": "claim-1", "text": resume_text}]

        class AmbiguousBatch:
            @staticmethod
            def adjudicate_evidence_batch(_requirements, _candidates):
                raise RunpodReconciliationRequired(job_id)

        gateway.model = AmbiguousBatch()
        assert_reconciliation(lambda: gateway._score(resume_text, graph, claims), job_id)

        class AmbiguousRow:
            @staticmethod
            def adjudicate_evidence(_requirement, _candidates):
                raise RunpodReconciliationRequired(job_id)

        gateway.model = AmbiguousRow()
        assert_reconciliation(lambda: gateway._score(resume_text, graph, claims), job_id)

        class AmbiguousSecondPass(FakeModel):
            @staticmethod
            def generate_variant(
                variant_kind,
                *args,
                generation_seed=None,
                optimization_input=None,
                **kwargs,
            ):
                if optimization_input is not None:
                    raise RunpodReconciliationRequired(job_id)
                result = FakeModel.generate_variant(
                    variant_kind,
                    *args,
                    generation_seed=generation_seed,
                    optimization_input=optimization_input,
                    **kwargs,
                )
                result["content"]["summary"] = "Unrelated background"
                result["claims"][0]["text"] = "Unrelated background"
                return result

        gateway.model = AmbiguousSecondPass()
        assert_reconciliation(
            lambda: gateway._generate_output(
                VariantKind.KEYWORD_ADVERSARIAL,
                gateway._job(JOB),
                graph,
                17,
                (),
                (),
                gateway._guarded_terms(graph),
            ),
            job_id,
        )


def test_resume_dispatch_reuses_active_work_and_generates_after_terminal_work() -> None:
    with tempfile.TemporaryDirectory() as directory:
        application_db = Path(directory) / "applications.db"
        first = enqueue_resume_run(application_db, "run-queue", "enqueue-key")
        assert enqueue_resume_run(
            application_db, "run-queue", "enqueue-key"
        )["work_id"] == first["work_id"]

        terminal_statuses = ("dead", "succeeded", "cancelled")
        previous = first
        for terminal_status in terminal_statuses:
            with sqlite3.connect(application_db) as connection:
                connection.execute(
                    "UPDATE work_items SET status=? WHERE work_id=?",
                    (terminal_status, previous["work_id"]),
                )
            current = enqueue_resume_run(
                application_db, "run-queue", "enqueue-key"
            )
            assert current["status"] == "queued"
            assert current["work_id"] != previous["work_id"]
            assert enqueue_resume_run(
                application_db, "run-queue", "enqueue-key"
            )["work_id"] == current["work_id"]
            previous = current

        with sqlite3.connect(application_db) as connection:
            rows = connection.execute(
                "SELECT status,dedupe_key,payload_json FROM work_items "
                "ORDER BY rowid"
            ).fetchall()
        assert [row[0] for row in rows] == [
            "dead",
            "succeeded",
            "cancelled",
            "queued",
        ]
        assert len({row[1] for row in rows}) == 4
        assert all(json.loads(row[2]) == {"run_id": "run-queue"} for row in rows)
        with sqlite3.connect(application_db) as connection:
            connection.execute(
                "UPDATE work_items SET status='running' WHERE work_id=?",
                (previous["work_id"],),
            )
        assert enqueue_resume_run(
            application_db, "run-queue", "enqueue-key"
        )["work_id"] == previous["work_id"]


def test_enqueue_callback_commit_then_raise_is_reconciled() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        application_db = root / "applications.db"
        calls = []

        def committed_enqueue(run_id: str, idempotency_key: str):
            calls.append((run_id, idempotency_key))
            enqueue_resume_run(application_db, run_id, idempotency_key)
            raise RuntimeError("callback failed after commit")

        gateway = ResumeLabProductionGateway(
            ResumeLabService(root / "resume.db"),
            ResumePdfArtifactRepository(root / "artifacts"),
            application_db=application_db,
            model=FakeModel(),
            toolchain=FakeToolchain(),
            enqueue=committed_enqueue,
        )
        dispatch = gateway._enqueue_run(
            {"run_id": "run-commit-reconcile", "status": "queued", "attempt": 0},
            "prepare-commit-then-raise",
        )
        assert dispatch is not None and dispatch["status"] == "queued"
        assert len(calls) == 1
        with sqlite3.connect(application_db) as connection:
            assert connection.execute(
                "SELECT status FROM work_items"
            ).fetchone()[0] == "queued"


def test_poll_reconciles_orphaned_queued_and_running_sidecar_runs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        enqueue = gateway._enqueue_run_while_locked

        def crash_before_enqueue(*_args, **_kwargs):
            raise SystemExit("simulated process exit after sidecar commit")

        gateway._enqueue_run_while_locked = (  # type: ignore[method-assign]
            crash_before_enqueue
        )
        try:
            gateway.prepare(
                JOB,
                application_id=application_id,
                idempotency_key="prepare-orphaned-run",
            )
        except SystemExit:
            pass
        else:
            raise AssertionError("simulated crash did not interrupt dispatch")
        gateway._enqueue_run_while_locked = enqueue  # type: ignore[method-assign]

        run = gateway.service.list_runs()[0]
        assert run["status"] == "queued"
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0

        first_poll = gateway.get_run_result(str(run["run_id"]))
        assert first_poll["status"] == "queued"
        gateway.get_run_result(str(run["run_id"]))
        with sqlite3.connect(root / "applications.db") as connection:
            first_work = connection.execute(
                "SELECT work_id FROM work_items"
            ).fetchone()[0]
            assert connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
            connection.execute(
                "UPDATE work_items SET status='cancelled' WHERE work_id=?",
                (first_work,),
            )

        gateway.service.start_run(str(run["run_id"]))
        running_poll = gateway.get_run_result(str(run["run_id"]))
        assert running_poll["status"] == "running"
        gateway.get_run_result(str(run["run_id"]))
        with sqlite3.connect(root / "applications.db") as connection:
            rows = connection.execute(
                "SELECT status FROM work_items ORDER BY rowid"
            ).fetchall()
        assert [row[0] for row in rows] == ["cancelled", "queued"]


def test_poll_does_not_dispatch_orphaned_queued_run_after_submission() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        enqueue = gateway._enqueue_run_while_locked

        def crash_before_enqueue(*_args, **_kwargs):
            raise SystemExit("simulated process exit after sidecar commit")

        gateway._enqueue_run_while_locked = (  # type: ignore[method-assign]
            crash_before_enqueue
        )
        try:
            gateway.prepare(
                JOB,
                application_id=application_id,
                idempotency_key="prepare-orphaned-before-submission",
            )
        except SystemExit:
            pass
        else:
            raise AssertionError("simulated crash did not interrupt dispatch")
        gateway._enqueue_run_while_locked = enqueue  # type: ignore[method-assign]
        run_id = str(gateway.service.list_runs()[0]["run_id"])

        ledger.record_submission(
            application_id,
            "2026-09-02T12:00:00Z",
            MutationContext("submit-before-orphan-poll", "user", "dashboard"),
        )
        result = gateway.get_run_result(run_id)
        assert result["status"] == "failed"
        assert result["reason"] == "application_not_preparing"
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


def test_poll_fences_orphaned_running_run_after_submission() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-running-before-submission",
        )
        run_id = str(prepared["run_id"])
        claimed = gateway.service.start_run(run_id, owner_token="orphan-owner")
        with sqlite3.connect(root / "applications.db") as connection:
            connection.execute(
                "UPDATE work_items SET status='cancelled' WHERE task_kind='resume.optimize'"
            )
        ledger.record_submission(
            application_id,
            "2026-09-02T12:00:00Z",
            MutationContext("submit-before-running-poll", "user", "dashboard"),
        )

        result = gateway.get_run_result(run_id)
        assert result["status"] == "failed"
        assert result["reason"] == "application_not_preparing"
        persisted = gateway.service.get_run(run_id)
        assert persisted["attempt"] == claimed["attempt"]
        with sqlite3.connect(root / "resume.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM resume_run_owners WHERE run_id=?", (run_id,)
            ).fetchone()[0] == 0
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE task_kind='resume.optimize'"
            ).fetchone()[0] == 1


def test_running_sidecar_gets_one_fresh_delivery_only_after_terminal_work() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _ledger, application_id = start_application(root)
        run_id = "run-running-reconcile"
        running = {
            "run_id": run_id,
            "application_id": application_id,
            "status": "running",
            "attempt": 1,
            "job_snapshot": {
                "ats": JOB["ats"],
                "job_id": JOB["id"],
                "title": JOB["title"],
                "description": JOB["description"],
                "employer": JOB["company"],
            },
            "selected_standard_id": "standard-id",
            "base_version_id": "standard-version-id",
            "items": [],
        }

        class RunningService:
            retry_commands = {}

            @staticmethod
            def get_run(requested_run_id: str):
                assert requested_run_id == run_id
                return running

            @classmethod
            def get_retry_command(cls, key: str):
                return cls.retry_commands.get(key)

            @classmethod
            def retry_run(
                cls,
                requested_run_id: str,
                key: str,
                *,
                actor_kind: str,
                reconciliation_acknowledged: bool = False,
            ):
                assert requested_run_id == run_id
                assert actor_kind == "user"
                assert reconciliation_acknowledged is False
                cls.retry_commands[key] = {"run_id": run_id}
                return {**running, "retry_replayed": False}

        gateway = ResumeLabProductionGateway(
            RunningService(),  # type: ignore[arg-type]
            ResumePdfArtifactRepository(root / "artifacts"),
            application_db=root / "applications.db",
            model=object(),
            toolchain=object(),
        )
        first = enqueue_resume_run(
            root / "applications.db", run_id, "enqueue-original-attempt"
        )
        with sqlite3.connect(root / "applications.db") as connection:
            connection.execute(
                "UPDATE work_items SET status='dead' WHERE work_id=?",
                (first["work_id"],),
            )

        retried = gateway.retry_run(
            run_id, idempotency_key="retry-running-reconcile"
        )
        assert retried["status"] == "running"
        gateway.retry_run(
            run_id, idempotency_key="retry-running-again"
        )
        with sqlite3.connect(root / "applications.db") as connection:
            rows = connection.execute(
                "SELECT work_id,status FROM work_items ORDER BY rowid"
            ).fetchall()
        assert [row[1] for row in rows] == ["dead", "queued"]

        with sqlite3.connect(root / "applications.db") as connection:
            connection.execute(
                "UPDATE work_items SET status='running' WHERE status='queued'"
            )
        gateway.retry_run(
            run_id, idempotency_key="retry-active-running"
        )
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM work_items"
            ).fetchone()[0] == 2


def test_grounded_validator_rejects_fabricated_rewrite_with_real_source_id() -> None:
    source_text = "Maintained weekly operational reports."
    fabricated = "Designed an award-winning quantum teleportation strategy."
    base_content = {
        "identity": {"name": "Test Candidate"},
        "summary": source_text,
        "experience": [],
        "projects": [],
        "education": [],
        "skills": [],
    }
    output = {
        "variant_kind": "grounded_rewrite",
        "base_standard_id": "standard-1",
        "synthetic": False,
        "research_only": False,
        "content": {**base_content, "summary": fabricated},
        "claims": [
            {
                "claim_id": "rewritten-summary",
                "path": "/summary",
                "text": fabricated,
                "origin": "source_rewrite",
                "source_claim_ids": ["source-summary"],
                "added_equivalent_terms": [],
            }
        ],
    }
    try:
        validate_variant_output(
            output,
            "grounded_rewrite",
            {"standard_id": "standard-1", "content": base_content},
            [
                {
                    "claim_id": "source-summary",
                    "text": source_text,
                    "allowed_equivalent_terms": [],
                }
            ],
            (),
        )
    except ResumeModelError:
        pass
    else:
        raise AssertionError("fabricated wording passed as a grounded rewrite")


def test_grounded_equivalents_never_treat_job_or_alternatives_as_synonyms() -> None:
    with tempfile.TemporaryDirectory() as directory:
        gateway = make_gateway(Path(directory))
        snapshot = gateway._job(
            {
                **JOB,
                "description": "Required experience with Python or Java and Kubernetes.",
            }
        )
        graph = extract_requirement_graph(snapshot)
        claims = gateway._source_claims(
            {
                "import_metadata": {
                    "normalization_claims": [
                        {
                            "claim_id": "source-platform",
                            "text": "Built Python services with Kubernetes.",
                            "allowed_equivalent_terms": ["java"],
                        }
                    ]
                }
            },
            graph,
        )
        allowed = set(claims[0]["allowed_equivalent_terms"])
        assert "java" not in allowed
        assert "k8s" in allowed


def test_selection_requires_an_explicit_artifact_evaluation_binding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")

        ranked, _graph = gateway._rank_standards(gateway._job(JOB))
        primary = ranked[0]
        other_job = {**JOB, "id": "job-2"}
        other_ranked, _other_graph = gateway._rank_standards(gateway._job(other_job))
        foreign_evaluation_id = str(other_ranked[0]["evaluation_id"])
        assert foreign_evaluation_id != primary["evaluation_id"]

        original = gateway.service.get_artifact(
            str(primary["artifact_id"]), include_content=True
        )
        forged = gateway.service.register_artifact(
            ArtifactInput(
                variant_kind=VariantKind.STANDARD,
                purpose=ResumePurpose.REAL_APPLICATION,
                job=gateway._job(JOB),
                tex_source=str(original["tex_source"]),
                intended_text=str(original["intended_text"]),
                claims=tuple(
                    ResumeClaim(
                        str(item["claim_id"]),
                        str(item["text"]),
                        ClaimOrigin(str(item["origin"])),
                        tuple(str(value) for value in item["source_fact_ids"]),
                    )
                    for item in original["claims"]
                ),
                managed_relative_path=str(original["managed_relative_path"]),
                pdf_sha256=str(original["pdf_sha256"]),
                parsed_text=str(original["parsed_text"]),
                parse_fidelity=float(original["parse_fidelity"]),
                parse_safe=True,
                generator_revision="forged-evaluation-link-v1",
                base_version_id=str(original["base_version_id"]),
                metadata={"evaluation_id": foreign_evaluation_id},
            )
        )
        try:
            gateway.select_resume(
                application_id,
                job=JOB,
                artifact_id=str(forged["artifact_id"]),
                evaluation_id=foreign_evaluation_id,
                idempotency_key="reject-unbound-evaluation",
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("an evaluation from another job was accepted")


def test_selection_identity_stays_bound_to_the_artifact_standard_version() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        imported = gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )
        ranked, _graph = gateway._rank_standards(gateway._job(JOB))
        primary = ranked[0]

        selected = gateway.select_resume(
            application_id,
            job=JOB,
            artifact_id=str(primary["artifact_id"]),
            evaluation_id=str(primary["evaluation_id"]),
            idempotency_key="select-frozen-standard-identity",
        )
        expected_identity = {
            "standard_id": imported["standard_id"],
            "standard_version_id": imported["standard_version_id"],
            "name": "Platform",
        }
        assert {
            key: selected["selection"][key] for key in expected_identity
        } == expected_identity

        updated = gateway.update_standard(
            str(imported["standard_id"]),
            "Python Kubernetes PostgreSQL production systems",
        )
        assert updated["standard_version_id"] != imported["standard_version_id"]
        reopened = gateway.get_selection(application_id)
        assert {
            key: reopened["selection"][key] for key in expected_identity
        } == expected_identity

        replayed = gateway.select_resume(
            application_id,
            job=JOB,
            artifact_id=str(primary["artifact_id"]),
            evaluation_id=str(primary["evaluation_id"]),
            idempotency_key="select-frozen-standard-identity",
        )
        assert {
            key: replayed["selection"][key] for key in expected_identity
        } == expected_identity


def test_selection_binds_the_exact_current_job_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-selection-fingerprint",
        )
        primary = prepared["ranked_standards"][0]
        try:
            gateway.select_resume(
                application_id,
                job={
                    **JOB,
                    "description": "A different posting reused the same ATS identifier.",
                },
                artifact_id=str(primary["artifact_id"]),
                evaluation_id=str(primary["evaluation_id"]),
                idempotency_key="reject-stale-job-artifact",
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a resume scored for a stale job snapshot was selected")


def test_gateway_binds_runs_to_the_authoritative_application_job() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        wrong_job = {**JOB, "id": "different-job"}
        try:
            gateway.prepare(
                wrong_job,
                application_id=application_id,
                idempotency_key="reject-wrong-application-job",
            )
        except (ResumeBoundaryError, ResumeConflictError):
            pass
        else:
            raise AssertionError("a run was created for the wrong application job")


def test_gateway_rechecks_application_phase_before_selection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-before-submission",
        )
        primary = prepared["ranked_standards"][0]
        ledger.record_submission(
            application_id,
            "2026-09-02T12:00:00Z",
            MutationContext("submit-before-resume-select", "user", "dashboard"),
        )
        try:
            gateway.select_resume(
                application_id,
                job=JOB,
                artifact_id=str(primary["artifact_id"]),
                evaluation_id=str(primary["evaluation_id"]),
                idempotency_key="reject-post-submission-selection",
            )
        except (ResumeBoundaryError, ResumeConflictError):
            pass
        else:
            raise AssertionError("resume selection changed after submission")


def test_prepare_rechecks_phase_after_slow_ranking_before_creating_a_run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        original_rank = gateway._rank_standards

        def rank_then_submit(job):
            ranked = original_rank(job)
            ledger.record_submission(
                application_id,
                "2026-09-02T12:00:00Z",
                MutationContext("submit-during-ranking", "user", "dashboard"),
            )
            return ranked

        gateway._rank_standards = rank_then_submit  # type: ignore[method-assign]
        try:
            gateway.prepare(
                JOB,
                application_id=application_id,
                idempotency_key="prepare-phase-race",
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("resume work was created after submission")
        assert gateway.service.list_runs() == ()
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


def test_worker_fails_queued_run_if_application_was_submitted_while_waiting() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-before-queued-submission",
        )
        generated = {"count": 0}

        class CountingModel(FakeModel):
            @staticmethod
            def generate_variant(*args, **kwargs):
                generated["count"] += 1
                return FakeModel.generate_variant(*args, **kwargs)

        gateway.model = CountingModel()
        ledger.record_submission(
            application_id,
            "2026-09-02T12:00:00Z",
            MutationContext("submit-before-worker", "user", "dashboard"),
        )
        result = gateway.handle_work(
            {"run_id": prepared["run_id"]}, Context()  # type: ignore[arg-type]
        )
        assert result["status"] == "failed"
        assert generated["count"] == 0
        run = gateway.service.get_run(str(prepared["run_id"]))
        assert run["error"] == "application_not_preparing"
        assert all(item["status"] == "pending" for item in run["items"])


def test_prepare_serializes_sidecar_creation_and_enqueue_with_submission() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        original_enqueue = gateway._enqueue_run_while_locked
        submission_started = threading.Event()
        submission_done = threading.Event()
        submission_thread = None

        def submit() -> None:
            submission_started.set()
            try:
                ledger.record_submission(
                    application_id,
                    "2026-09-02T12:00:00Z",
                    MutationContext("submit-at-enqueue", "user", "dashboard"),
                )
            finally:
                submission_done.set()

        def enqueue_while_submission_waits(connection, run):
            nonlocal submission_thread
            submission_thread = threading.Thread(target=submit)
            submission_thread.start()
            assert submission_started.wait(1)
            assert not submission_done.wait(0.05)
            return original_enqueue(connection, run)

        gateway._enqueue_run_while_locked = (  # type: ignore[method-assign]
            enqueue_while_submission_waits
        )
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-enqueue-submission-race",
        )
        assert submission_done.wait(2)
        assert submission_thread is not None
        submission_thread.join(timeout=1)
        run = gateway.service.get_run(str(prepared["run_id"]))
        assert run["status"] == "queued"
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
        reconciled = gateway.get_run_result(str(run["run_id"]))
        assert reconciled["status"] == "failed"
        assert reconciled["reason"] == "application_not_preparing"


def test_selection_serializes_with_authoritative_submission() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-selection-serialization",
        )
        primary = prepared["ranked_standards"][0]
        original_select = gateway.service.select_for_application
        attempted = threading.Event()
        submitted = threading.Event()

        def submit_concurrently() -> None:
            attempted.set()
            ledger.record_submission(
                application_id,
                "2026-09-02T12:00:00Z",
                MutationContext("concurrent-submit", "user", "dashboard"),
            )
            submitted.set()

        thread: threading.Thread | None = None

        def select_while_starting_submit(*args, **kwargs):
            nonlocal thread
            thread = threading.Thread(target=submit_concurrently, daemon=True)
            thread.start()
            assert attempted.wait(1)
            assert not submitted.wait(0.05)
            return original_select(*args, **kwargs)

        gateway.service.select_for_application = (  # type: ignore[method-assign]
            select_while_starting_submit
        )
        selected = gateway.select_resume(
            application_id,
            job=JOB,
            artifact_id=str(primary["artifact_id"]),
            evaluation_id=str(primary["evaluation_id"]),
            idempotency_key="serialized-selection",
        )
        assert selected["selection"]["artifact_id"] == primary["artifact_id"]
        assert thread is not None
        thread.join(timeout=2)
        assert submitted.is_set()


def test_grounded_approval_serializes_with_authoritative_submission() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-approval-serialization",
        )
        gateway.handle_work(
            {"run_id": prepared["run_id"]}, Context()  # type: ignore[arg-type]
        )
        original_approve = gateway.service.approve_run
        attempted = threading.Event()
        submitted = threading.Event()

        def submit_concurrently() -> None:
            attempted.set()
            ledger.record_submission(
                application_id,
                "2026-09-02T12:00:00Z",
                MutationContext("submit-during-approval", "user", "dashboard"),
            )
            submitted.set()

        thread: threading.Thread | None = None

        def approve_while_starting_submit(*args, **kwargs):
            nonlocal thread
            thread = threading.Thread(target=submit_concurrently, daemon=True)
            thread.start()
            assert attempted.wait(1)
            assert not submitted.wait(0.05)
            return original_approve(*args, **kwargs)

        gateway.service.approve_run = (  # type: ignore[method-assign]
            approve_while_starting_submit
        )
        approved = gateway.approve_run(
            str(prepared["run_id"]),
            comparison_kind="grounded_rewrite",
            idempotency_key="serialized-grounded-approval",
        )
        assert approved["comparisons"][0]["approved"] is True
        assert thread is not None
        thread.join(timeout=2)
        assert submitted.is_set()


def test_selection_rejects_tampered_or_missing_managed_pdf() -> None:
    failures = (
        ("tampered", ArtifactIntegrityError),
        ("missing", ArtifactNotFoundError),
    )
    for mode, expected_error in failures:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = make_gateway(root)
            _ledger, application_id = start_application(
                root, idempotency_key=f"start-{mode}-pdf-application"
            )
            gateway.import_standard(
                "Platform", 1, "Python Kubernetes production systems"
            )
            ranked, _graph = gateway._rank_standards(gateway._job(JOB))
            primary = ranked[0]
            artifact = gateway.service.get_artifact(
                str(primary["artifact_id"]), include_content=True
            )
            stored_pdf = root / "artifacts" / str(artifact["managed_relative_path"])
            if mode == "tampered":
                stored_pdf.write_bytes(stored_pdf.read_bytes() + b"tampered")
            else:
                stored_pdf.unlink()

            try:
                gateway.select_resume(
                    application_id,
                    job=JOB,
                    artifact_id=str(primary["artifact_id"]),
                    evaluation_id=str(primary["evaluation_id"]),
                    idempotency_key=f"reject-{mode}-pdf-selection",
                )
            except expected_error:
                pass
            else:
                raise AssertionError(f"{mode} PDF became the selected resume")
            assert gateway.service.current_application_selection(application_id) is None


def test_run_freezes_complete_handwritten_ranking_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        general = gateway.import_standard("General", 1, "Excel reporting")
        platform = gateway.import_standard(
            "Platform", 2, "Python Kubernetes production systems"
        )
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="freeze-ranking-history",
        )
        original = [
            (item["standard_id"], item["standard_version_id"], item["score"])
            for item in prepared["ranked_standards"]
        ]
        gateway.service.set_standard_active(
            str(general["standard_id"]), False, actor_kind="user"
        )
        gateway.import_standard("New current standard", 3, "PostgreSQL operations")

        reopened = gateway.get_application_workspace(application_id)
        assert [
            (item["standard_id"], item["standard_version_id"], item["score"])
            for item in reopened["ranked_standards"]
        ] == original
        assert reopened["selected_standard_id"] == platform["standard_id"]


def test_rank_winner_version_change_is_rejected_atomically() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        imported = gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )
        first = gateway.service.store.get_standard_version(
            str(imported["standard_version_id"])
        )
        claims = tuple(
            ResumeClaim(
                str(item["claim_id"]),
                str(item["text"]),
                ClaimOrigin(str(item["origin"])),
                tuple(str(value) for value in item["source_fact_ids"]),
            )
            for item in first["claims"]
        )
        second = gateway.service.add_standard_version(
            str(imported["standard_id"]),
            StandardVersionInput(
                tex_source=str(first["tex_source"]),
                plain_text=str(first["plain_text"]),
                claims=claims,
                normalized_content=first["normalized_content"],
                import_metadata={
                    **dict(first["import_metadata"]),
                    "revision_note": "concurrent replacement",
                },
            ),
            actor_kind="user",
            activate=False,
        )
        original_create = gateway.service.create_run

        def activate_then_create(*args, **kwargs):
            gateway.service.activate_standard_version(
                str(imported["standard_id"]),
                str(second["version_id"]),
                actor_kind="user",
            )
            return original_create(*args, **kwargs)

        gateway.service.create_run = activate_then_create  # type: ignore[method-assign]
        try:
            gateway.prepare(
                JOB,
                application_id=application_id,
                idempotency_key="reject-version-race",
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("run silently switched to an unranked active version")
        assert gateway.service.list_runs() == ()


def test_active_standard_set_change_is_rejected_atomically() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("General", 1, "Excel reporting")
        original_create = gateway.service.create_run

        def import_then_create(*args, **kwargs):
            gateway.import_standard(
                "Concurrent platform standard",
                2,
                "Python Kubernetes production systems",
            )
            return original_create(*args, **kwargs)

        gateway.service.create_run = import_then_create  # type: ignore[method-assign]
        try:
            gateway.prepare(
                JOB,
                application_id=application_id,
                idempotency_key="reject-active-set-race",
            )
        except ResumeConflictError as exc:
            assert "active standard set changed" in str(exc)
        else:
            raise AssertionError("a stale partial active-standard ranking was persisted")
        assert gateway.service.list_runs() == ()


def test_exact_mutation_replays_survive_submission_but_new_commands_do_not() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="replay-prepare",
        )
        gateway.handle_work(
            {"run_id": prepared["run_id"]}, Context()  # type: ignore[arg-type]
        )
        result = gateway.get_run_result(str(prepared["run_id"]))
        primary = result["ranked_standards"][0]
        gateway.select_resume(
            application_id,
            job=JOB,
            artifact_id=str(primary["artifact_id"]),
            evaluation_id=str(primary["evaluation_id"]),
            idempotency_key="replay-selection",
        )
        gateway.approve_run(
            str(prepared["run_id"]),
            comparison_kind="grounded_rewrite",
            idempotency_key="replay-approval",
        )
        ledger.record_submission(
            application_id,
            "2026-09-02T12:00:00Z",
            MutationContext("submit-before-replays", "user", "dashboard"),
        )

        assert gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="replay-prepare",
        )["run_id"] == prepared["run_id"]
        assert gateway.approve_run(
            str(prepared["run_id"]),
            comparison_kind="grounded_rewrite",
            idempotency_key="replay-approval",
        )["comparisons"][0]["approved"] is True
        assert gateway.select_resume(
            application_id,
            job=JOB,
            artifact_id=str(primary["artifact_id"]),
            evaluation_id=str(primary["evaluation_id"]),
            idempotency_key="replay-selection",
        )["selection"]["artifact_id"] == primary["artifact_id"]

        for action in (
            lambda: gateway.approve_run(
                str(prepared["run_id"]),
                comparison_kind="grounded_rewrite",
                idempotency_key="new-approval-after-submit",
            ),
            lambda: gateway.select_resume(
                application_id,
                job=JOB,
                artifact_id=str(primary["artifact_id"]),
                evaluation_id=str(primary["evaluation_id"]),
                idempotency_key="new-selection-after-submit",
            ),
        ):
            try:
                action()
            except ResumeConflictError:
                pass
            else:
                raise AssertionError("a new resume mutation ran after submission")
        try:
            gateway.prepare(
                {**JOB, "description": JOB["description"] + " Changed."},
                application_id=application_id,
                idempotency_key="replay-prepare",
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("changed prepare input reused an old command result")


def test_retry_command_is_durable_and_replay_does_not_redispatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-for-retry-replay",
        )
        gateway.service.fail_queued_run(
            str(prepared["run_id"]), error="injected_failure"
        )
        retried = gateway.retry_run(
            str(prepared["run_id"]), idempotency_key="durable-retry"
        )
        assert retried["run_id"] != prepared["run_id"]
        assert gateway.service.get_run(str(prepared["run_id"]))["status"] == "failed"
        with sqlite3.connect(root / "applications.db") as connection:
            before = connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
        ledger.record_submission(
            application_id,
            "2026-09-02T12:00:00Z",
            MutationContext("submit-after-retry", "user", "dashboard"),
        )
        replay = gateway.retry_run(
            str(prepared["run_id"]), idempotency_key="durable-retry"
        )
        assert replay["run_id"] == retried["run_id"]
        with sqlite3.connect(root / "applications.db") as connection:
            after = connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
        assert after == before
        try:
            gateway.retry_run(
                str(prepared["run_id"]), idempotency_key="new-retry-after-submit"
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a new retry command ran after submission")


def test_retry_replay_reconciles_successor_committed_before_enqueue() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-for-orphaned-retry",
        )
        original_run_id = str(prepared["run_id"])
        gateway.service.fail_queued_run(original_run_id, error="injected_failure")
        original_enqueue = gateway._enqueue_run_while_locked

        def crash_before_successor_enqueue(*_args, **_kwargs):
            raise SystemExit("simulated exit after retry successor commit")

        gateway._enqueue_run_while_locked = (  # type: ignore[method-assign]
            crash_before_successor_enqueue
        )
        try:
            gateway.retry_run(
                original_run_id,
                idempotency_key="orphaned-successor-retry",
            )
        except SystemExit:
            pass
        else:
            raise AssertionError("simulated retry crash did not interrupt enqueue")
        finally:
            gateway._enqueue_run_while_locked = original_enqueue  # type: ignore[method-assign]

        command = gateway.service.get_retry_command("orphaned-successor-retry")
        assert command is not None
        successor_id = str(command["result_run_id"])
        assert successor_id != original_run_id
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE payload_json=?",
                (json.dumps({"run_id": successor_id}, sort_keys=True, separators=(",", ":")),),
            ).fetchone()[0] == 0

        replay = gateway.retry_run(
            original_run_id,
            idempotency_key="orphaned-successor-retry",
        )
        assert replay["run_id"] == successor_id
        with sqlite3.connect(root / "applications.db") as connection:
            payloads = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM work_items"
                ).fetchall()
            ]
        assert sum(item == {"run_id": successor_id} for item in payloads) == 1


def test_grounded_result_can_be_approved_when_research_variant_fails() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard(
            "Platform", 1, "Python Kubernetes production systems"
        )

        class ResearchFailureModel(FakeModel):
            @staticmethod
            def generate_variant(variant_kind, *args, **kwargs):
                if variant_kind == "market_ideal":
                    raise ResumeModelError("injected research-only failure")
                return FakeModel.generate_variant(variant_kind, *args, **kwargs)

        gateway.model = ResearchFailureModel()
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="partial-grounded-run",
        )
        outcome = gateway.handle_work(
            {"run_id": prepared["run_id"]}, Context()  # type: ignore[arg-type]
        )
        assert outcome["status"] == "failed" and outcome["failed"] == 1
        approved = gateway.approve_run(
            str(prepared["run_id"]),
            comparison_kind="grounded_rewrite",
            idempotency_key="approve-partial-grounded",
        )
        assert approved["comparisons"][0]["approved"] is True


def test_hermes_summary_is_compact_and_never_contains_resume_rewrites() -> None:
    criteria = [
        {
            "requirement_id": f"requirement-{index}",
            "priority": "required",
            "status": "not_evidenced",
            "weight": 100 - index,
            "source_text": "A bounded job requirement",
        }
        for index in range(100)
    ]
    compact = ResumeLabProductionGateway._hermes_candidate(
        {
            "comparison_kind": "grounded_rewrite",
            "score": 71.5,
            "criteria": criteria,
            "changes": [
                {"source": "private source", "rewrite": "private rewrite"}
            ],
        },
        include_gaps=True,
    )
    encoded = json.dumps(compact)
    assert len(compact["top_gaps"]) == 3
    assert "private source" not in encoded and "private rewrite" not in encoded
    assert len(encoded.encode("utf-8")) < 4_096


def test_semantic_scoring_uses_one_bounded_batch_per_artifact() -> None:
    class BatchModel:
        def __init__(self) -> None:
            self.calls = 0

        def adjudicate_evidence_batch(self, requirements, _candidates):
            self.calls += 1
            assert len(requirements) > 1
            return {
                "adjudications": [
                    {
                        "requirement_id": item["requirement_id"],
                        "status": "not_evidenced",
                        "confidence": 1.0,
                        "reason": "No additional semantic evidence.",
                        "evidence": [],
                    }
                    for item in requirements
                ]
            }

        @staticmethod
        def adjudicate_evidence(_requirement, _candidates):
            raise AssertionError("the per-requirement model path was used")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        model = BatchModel()
        gateway = ResumeLabProductionGateway(
            ResumeLabService(root / "resume.db"),
            ResumePdfArtifactRepository(root / "artifacts"),
            model=model,
        )
        resume_text = "Built reliable distributed backend services."
        graph = extract_requirement_graph(gateway._job(JOB))
        gateway._score(
            resume_text,
            graph,
            [{"claim_id": "claim-1", "text": resume_text}],
        )
        assert model.calls == 1


def test_semantic_model_never_receives_labelled_protected_resume_fields() -> None:
    resume_text = "Built Python services.\nRace: Asian\nDisability: yes"
    claims = [
        {"claim_id": "work", "text": "Built Python services."},
        {"claim_id": "race", "text": "Race: Asian"},
        {"claim_id": "disability", "text": "Disability: yes"},
    ]
    candidates, locations = ResumeLabProductionGateway._semantic_candidates(
        resume_text, claims
    )
    assert [item["claim_id"] for item in candidates] == ["work"]
    assert set(locations) == {"work"}


def test_grounded_review_metadata_never_truncates_the_visible_diff() -> None:
    sources = [
        {"claim_id": f"source-{index}", "text": f"Original claim {index}"}
        for index in range(75)
    ]
    output = {
        "claims": [
            {
                "claim_id": f"rewrite-{index}",
                "path": f"/experience/0/bullets/{index}",
                "text": f"Rewritten claim {index}",
                "source_claim_ids": [f"source-{index}"],
                "added_equivalent_terms": [],
            }
            for index in range(75)
        ]
    }
    changes, _terms = ResumeLabProductionGateway._rewrite_metadata(output, sources)
    assert len(changes) == 75


def test_worker_finalizes_a_run_with_a_previously_persisted_failed_item() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-crash-recovery",
        )
        gateway.service.start_run(str(prepared["run_id"]))
        gateway.service.complete_run_item(
            str(prepared["run_id"]),
            VariantKind.GROUNDED_REWRITE,
            "failed",
            error="injected_failure_before_process_exit",
        )

        outcome = gateway.handle_work(
            {"run_id": prepared["run_id"]}, Context()  # type: ignore[arg-type]
        )
        assert outcome["status"] == "failed"
        recovered = gateway.service.get_run(str(prepared["run_id"]))
        assert recovered["status"] == "failed"
        assert recovered["items"][0]["status"] == "failed"


def test_runpod_worker_restart_requires_reconciliation_before_resubmission() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-runpod-restart",
        )
        run_id = str(prepared["run_id"])
        gateway.service.start_run(run_id, owner_token="orphaned-runpod-owner")
        gateway.model = SimpleNamespace(
            config=SimpleNamespace(provider="runpod_serverless_vllm")
        )

        outcome = gateway.handle_work(
            {"run_id": run_id}, Context()  # type: ignore[arg-type]
        )
        assert outcome == {
            "run_id": run_id,
            "status": "failed",
            "reconciliation_required": True,
        }
        failed = gateway.service.get_run(run_id)
        assert failed["error"] == "runpod_reconciliation_required"
        try:
            gateway.service.retry_run(
                run_id,
                "retry-before-runpod-reconciliation",
                actor_kind="user",
            )
        except ResumeConflictError as exc:
            assert "inspect" in str(exc)
        else:
            raise AssertionError("ambiguous Runpod work was retried without review")
        successor = gateway.service.retry_run(
            run_id,
            "retry-after-runpod-reconciliation",
            actor_kind="user",
            reconciliation_acknowledged=True,
        )
        assert successor["status"] == "queued"
        assert successor["run_id"] != run_id


def test_worker_immediately_terminalizes_when_submission_races_item_commit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-item-commit-phase-race",
        )
        original_phase_commit = gateway._while_application_preparing
        submission_started = threading.Event()
        submission_done = threading.Event()
        submission_thread = None

        def submit() -> None:
            submission_started.set()
            try:
                ledger.record_submission(
                    application_id,
                    "2026-09-02T12:00:00Z",
                    MutationContext(
                        "submit-at-item-commit", "user", "dashboard"
                    ),
                )
            finally:
                submission_done.set()

        def commit_then_release_to_submission(application, job, operation):
            nonlocal submission_thread
            if submission_thread is not None:
                return original_phase_commit(application, job, operation)

            def operation_then_race(connection):
                nonlocal submission_thread
                result = operation(connection)
                submission_thread = threading.Thread(target=submit)
                submission_thread.start()
                assert submission_started.wait(1)
                assert not submission_done.wait(0.05)
                return result

            result = original_phase_commit(application, job, operation_then_race)
            assert submission_done.wait(2)
            return result

        gateway._while_application_preparing = (  # type: ignore[method-assign]
            commit_then_release_to_submission
        )
        outcome = gateway.handle_work(
            {"run_id": prepared["run_id"]}, Context()  # type: ignore[arg-type]
        )
        assert submission_thread is not None
        submission_thread.join(timeout=1)
        assert submission_done.is_set()
        assert outcome["status"] == "failed"
        persisted = gateway.service.get_run(str(prepared["run_id"]))
        assert persisted["status"] == "failed"
        assert persisted["error"] == "application_not_preparing"


def test_worker_cannot_commit_an_item_after_its_lease_is_lost() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = make_gateway(root)
        _ledger, application_id = start_application(root)
        gateway.import_standard("Platform", 1, "Python Kubernetes production systems")
        prepared = gateway.prepare(
            JOB,
            application_id=application_id,
            idempotency_key="prepare-stale-worker",
        )
        lease = {"owned": True}

        class LeaseDroppingModel(FakeModel):
            @staticmethod
            def generate_variant(*args, **kwargs):
                generated = FakeModel.generate_variant(*args, **kwargs)
                lease["owned"] = False
                return generated

        class LeaseContext:
            @staticmethod
            def heartbeat():
                return lease["owned"]

        gateway.model = LeaseDroppingModel()
        try:
            gateway.handle_work(
                {"run_id": prepared["run_id"]},
                LeaseContext(),  # type: ignore[arg-type]
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("a stale resume worker completed normally")
        resumed = gateway.service.get_run(str(prepared["run_id"]))
        assert not any(item["status"] == "succeeded" for item in resumed["items"])


def main() -> None:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume-lab gateway tests)")


if __name__ == "__main__":
    main()
