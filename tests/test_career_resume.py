#!/usr/bin/env python3
"""Offline end-to-end career composition, migration and selection regressions."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from job_search.resume_lab.artifacts import ResumePdfArtifactRepository
from job_search.resume_lab.career_composition import (
    select_composition, validate_rewrites, compose_content,
)
from job_search.resume_lab.career_runs import save_composition
from job_search.resume_lab.contracts import ResumeBoundaryError, ResumeConflictError, JobSnapshot
from job_search.resume_lab.fidelity import FidelityView, PdfFidelityReport
from job_search.resume_lab.gateway import ResumeLabProductionGateway
from job_search.resume_lab.pdf import PdfExtraction
from job_search.resume_lab.service import ResumeLabService
from job_search.resume_lab.store import ResumeLabStore
from job_search.resume_lab.tex import render_resume_tex, CompiledPdf
from job_search.resume_lab.toolchain import ResumeArtifactBuild
from tests.test_resume_lab_gateway import JOB, start_application, FakeModel


PROFILE = {
    "identity": {"name":"Alex Example", "contact_line":"Chicago, IL · 555-0100", "email":"alex@example.test"},
    "summary":"", "education":[{"institution":"Example University","degree":"BS Computer Science","dates":"2018 – 2022","location":"Chicago, IL"}],
    "experience":[{"company":"Example Systems","role":"Software Engineer","dates":"2022 – Present","location":"Chicago, IL",
        "bullets":["Built Python APIs with PostgreSQL for production services.", "Reduced Kubernetes deployment failures by 20%."]}],
    "projects":[{"name":"Community Dashboard","context":"Python, PostgreSQL","dates":"2024",
        "bullets":["Created Python data pipelines for community reports."]}],
    "skills":[{"category":"Languages and tools","items":["Python","PostgreSQL","Kubernetes"]}],
}


class Toolchain:
    def __init__(self, page_limit=None):
        self.calls = []
        self.page_limit = page_limit

    def build(self, content, *, allow_warning=False, visible_notice="", template_version="jake-v1"):
        rendered = render_resume_tex(content, visible_notice=visible_notice, template_version=template_version)
        self.calls.append(content)
        pdf = b"%PDF-1.7\n" + rendered.intended_text.encode() + b"\n%%EOF"
        digest = hashlib.sha256(pdf).hexdigest()
        pages = 2 if self.page_limit is not None and len(rendered.intended_text) > self.page_limit else 1
        compiled = CompiledPdf(pdf,digest,rendered.source_sha256,"fixture","1","b"*64,"")
        extracted = PdfExtraction(digest,"pypdf","fixture",pages,len(pdf),rendered.intended_text,rendered.intended_text)
        perfect = FidelityView(1,1,1,0)
        return ResumeArtifactBuild(rendered,compiled,extracted,PdfFidelityReport("pass",perfect,perfect,(),()))


class Context:
    work_id = "career-fixture-work"
    attempt = 1
    @staticmethod
    def heartbeat():
        return True


class CareerModel(FakeModel):
    def _invoke(self, task, payload, _schema):
        if task == "rewrite_career_facts":
            return {"rewrites":[{"fact_id":f["fact_id"],"text":f["text"]} for f in payload["facts"]]}
        raise AssertionError(task)

    def generate_variant(self, kind, job, standards, source_claims, guarded_terms=(), *, generation_seed=None, optimization_input=None):
        if kind != "standard_exaggerated":
            return super().generate_variant(kind,job,standards,source_claims,guarded_terms,
                generation_seed=generation_seed,optimization_input=optimization_input)
        return {"variant_kind":kind,"base_standard_id":standards[0]["standard_id"],
            "synthetic":True,"research_only":True,"content":copy.deepcopy(standards[0]["content"]),
            "claims":[{"claim_id":"synthetic_"+str(i),"path":row["path"],"text":row["text"],
                "origin":"source","source_claim_ids":[row["claim_id"]],"added_equivalent_terms":[]}
                for i,row in enumerate(source_claims)]}


def gateway_at(root, *, model=None, toolchain=None):
    gateway = ResumeLabProductionGateway(ResumeLabService(root/"resume.db"),
        ResumePdfArtifactRepository(root/"artifacts"), application_db=root/"applications.db",
        model=model, toolchain=toolchain or Toolchain())
    _, application_id = start_application(root)
    saved = gateway.save_career_profile(copy.deepcopy(PROFILE),expected_revision_id=None,idempotency_key="initial-draft")
    gateway.approve_career_profile(saved["draft_revision_id"],idempotency_key="initial-approval")
    return gateway, application_id


def expect_error(error, operation):
    try:
        operation()
    except error:
        return
    raise AssertionError("expected " + error.__name__)


def test_career_without_handwritten_standard_freezes_revision_and_requires_approval():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        gateway, application_id = gateway_at(root)
        prepared = gateway.prepare(JOB,application_id=application_id,idempotency_key="compose")
        assert prepared["source_mode"] == "career_profile"
        assert prepared["selected_standard_id"] is None and prepared["ranked_standards"] == []
        assert len(prepared["comparisons"]) == 1
        original = gateway.get_career_profile()
        draft = copy.deepcopy(original["draft"]["content"])
        draft["experience"][0]["bullets"][0]["text"] = "Built Rust services."
        gateway.save_career_profile(draft,expected_revision_id=original["draft_revision_id"],idempotency_key="next-draft")
        result=gateway.handle_work({"run_id":prepared["run_id"]},Context())
        assert result["status"] == "succeeded", gateway.get_run_result(prepared["run_id"])
        view=gateway.get_run_result(prepared["run_id"])
        candidate=view["comparisons"][0]
        assert view["composition"]["profile_revision_id"] == original["approved_revision_id"]
        artifact=gateway.service.get_artifact(candidate["artifact_id"],include_content=True)
        assert "Rust" not in artifact["intended_text"]
        assert "Python" in artifact["intended_text"]
        assert "11pt" in artifact["tex_source"]
        select=lambda: gateway.select_resume(application_id,job=JOB,artifact_id=candidate["artifact_id"],evaluation_id=candidate["evaluation_id"],idempotency_key="select")
        expect_error(ResumeBoundaryError,select)
        gateway.approve_run(prepared["run_id"],comparison_kind="grounded_rewrite",idempotency_key="approve")
        assert select()["selection"]["source_mode"] == "career_profile"
        assert gateway.get_artifact_source(candidate["artifact_id"]).content.decode() == artifact["tex_source"]
        reopened=gateway.get_application_workspace(application_id)
        assert reopened["selection"]["artifact_id"] == candidate["artifact_id"]
        assert reopened["composition"]["profile_revision_id"] == original["approved_revision_id"]
        with sqlite3.connect(root/"applications.db") as con:
            for payload, in con.execute("SELECT payload_json FROM work_items WHERE task_kind='resume.optimize'"):
                assert set(json.loads(payload)) == {"run_id"}


def test_selection_pin_exclude_and_grounding_block_invented_facts():
    with tempfile.TemporaryDirectory() as directory:
        gateway, app = gateway_at(Path(directory))
        revision=gateway.get_career_profile()["approved"]
        bullet=revision["content"]["experience"][0]["bullets"][0]["fact_id"]
        other=revision["content"]["projects"][0]["entry_id"]
        snapshot=select_composition(revision,JobSnapshot(**gateway._job(JOB).__dict__),pinned_fact_ids=[bullet],excluded_fact_ids=[other])
        assert next(r for r in snapshot["selected"] if r["fact_id"]==bullet)["pinned"]
        assert all(r["entry_id"] != other for r in snapshot["selected"])
        expect_error(ResumeBoundaryError,lambda: validate_rewrites(snapshot,{bullet:"Led Rust systems for 50 clients."}))
        expect_error(ResumeBoundaryError,lambda: validate_rewrites(snapshot,{"unknown":"Built Python."}))
        source=next(r["text"] for r in snapshot["selected"] if r["fact_id"]==bullet)
        assert validate_rewrites(snapshot,{bullet:source})[bullet] == source
        forged=copy.deepcopy(snapshot)
        forged["profile_content"]["identity"]["name"]="Not the approved person"
        expect_error(ResumeBoundaryError,lambda:save_composition(gateway.service.store,forged))


def test_one_page_prunes_optional_content_but_never_pins():
    with tempfile.TemporaryDirectory() as directory:
        gateway,app=gateway_at(Path(directory),toolchain=Toolchain(page_limit=470))
        run=gateway.prepare(JOB,application_id=app,idempotency_key="prune")
        outcome=gateway.handle_work({"run_id":run["run_id"]},Context())
        assert outcome["status"]=="succeeded",gateway.get_run_result(run["run_id"])
        view=gateway.get_run_result(run["run_id"])
        assert any(r["reason"]=="Omitted to fit one page" for r in view["composition"]["omitted"])
        assert len(gateway.toolchain.calls)>1
    with tempfile.TemporaryDirectory() as directory:
        gateway,app=gateway_at(Path(directory),toolchain=Toolchain(page_limit=50))
        revision=gateway.get_career_profile()["approved"]
        pins=[e["entry_id"] for name in ("experience","education","projects","skills") for e in revision["content"][name]]
        run=gateway.prepare_from_career_profile(JOB,application_id=app,idempotency_key="pinned",pinned_fact_ids=pins)
        outcome=gateway.handle_work({"run_id":run["run_id"]},Context())
        assert outcome["status"]=="failed"
        view=gateway.get_run_result(run["run_id"])
        assert view["comparisons"][0]["error_code"]=="career_one_page_overflow"
        assert not view["comparisons"][0].get("artifact_id")


def test_failed_career_retry_preserves_source_and_item_set():
    with tempfile.TemporaryDirectory() as directory:
        gateway,app=gateway_at(Path(directory),toolchain=Toolchain(page_limit=50))
        original=gateway.prepare(JOB,application_id=app,idempotency_key="overflow")
        gateway.handle_work({"run_id":original["run_id"]},Context())
        gateway.toolchain=Toolchain()
        retry=gateway.retry_run(original["run_id"],idempotency_key="retry")
        assert retry["run_id"] != original["run_id"]
        assert retry["composition"]["composition_id"]==original["composition"]["composition_id"]
        assert len(retry["comparisons"])==1
        assert gateway.handle_work({"run_id":retry["run_id"]},Context())["status"]=="succeeded"
        assert gateway.get_run_result(original["run_id"])["status"]=="failed"


def test_research_children_never_replace_approved_factual_workspace():
    with tempfile.TemporaryDirectory() as directory:
        gateway,app=gateway_at(Path(directory),model=CareerModel())
        parent=gateway.prepare(JOB,application_id=app,idempotency_key="factual")
        assert gateway.handle_work({"run_id":parent["run_id"]},Context())["status"]=="succeeded"
        factual=gateway.get_run_result(parent["run_id"])["comparisons"][0]
        gateway.approve_run(parent["run_id"],comparison_kind="grounded_rewrite",idempotency_key="approve-factual")
        gateway.select_resume(app,job=JOB,artifact_id=factual["artifact_id"],evaluation_id=factual["evaluation_id"],idempotency_key="choose-factual")
        child=gateway.start_research_comparisons(parent["run_id"],idempotency_key="research")
        assert child["parent_run_id"]==parent["run_id"]
        outcome=gateway.handle_work({"run_id":child["run_id"]},Context())
        assert outcome["status"]=="succeeded",gateway.get_run_result(child["run_id"])
        reopened=gateway.get_application_workspace(app)
        assert reopened["run_id"]==parent["run_id"]
        assert reopened["comparisons"][0]["approved"]
        assert reopened["selection"]["artifact_id"]==factual["artifact_id"]
        assert reopened["research_runs"][0]["run_id"]==child["run_id"]
        for candidate in reopened["research_runs"][0]["comparisons"]:
            assert candidate["research_only"]
            expect_error(ResumeBoundaryError,lambda c=candidate:gateway.select_resume(app,job=JOB,
                artifact_id=c["artifact_id"],evaluation_id=c["evaluation_id"],idempotency_key="cannot-select-research"))
        # Compact Hermes-facing summaries contain no career content or profile data.
        public=json.dumps(gateway.compare_for_job("ashby","job-1"))
        assert "profile_content" not in public and "source_text" not in public and "Alex Example" not in public


def test_populated_legacy_schema_migrates_without_changing_approvals_or_selections():
    with tempfile.TemporaryDirectory() as directory:
        database=Path(directory)/"resume.db"
        with sqlite3.connect(database) as con:
            con.executescript((Path(__file__).parents[1]/"tests/fixtures/resume_lab_v3.sql").read_text())
            def insert(table, **values):
                con.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",tuple(values.values()))
            stamp="2026-09-03T00:00:00Z"
            insert("resume_standards",standard_id="std_legacy",name="Old standard",manual_rank=1,active=1,active_version_id="version_legacy",created_at=stamp,updated_at=stamp)
            claims=json.dumps([{"claim_id":"claim_old","text":"Built Python services.","origin":"user_attested","source_fact_ids":[]}])
            insert("resume_standard_versions",version_id="version_legacy",standard_id="std_legacy",version_number=1,tex_source="old-source",plain_text="Built Python services.",claims_json=claims,content_sha256="a"*64,authored_by="user",created_at=stamp)
            metadata=json.dumps({"grounding_validator_revision":"grounding-boundary-v5-relational","grounding_equivalence_revision":"grounding-equivalences-v3"})
            insert("resume_artifacts",artifact_id="real_legacy",purpose="real_application",variant_kind="grounded_rewrite",ats="ashby",job_id="job-1",job_fingerprint="b"*64,base_version_id="version_legacy",tex_source="old-artifact-source",intended_text="Built Python services.",parsed_text="Built Python services.",claims_json=claims,managed_relative_path="real/aa/bb/"+"c"*64+".pdf",pdf_sha256="c"*64,content_sha256="d"*64,parse_fidelity=1,parse_safe=1,generator_revision="old",treatment="",metadata_json=metadata,created_at=stamp)
            insert("resume_runs",run_id="run_legacy",application_id="app_old",ats="ashby",job_id="job-1",job_snapshot_json=json.dumps({"ats":"ashby","job_id":"job-1","title":"Engineer","description":"Python","employer":"Acme"}),job_fingerprint="b"*64,selected_standard_id="std_legacy",base_version_id="version_legacy",base_fingerprint="a"*64,request_fingerprint="e"*64,idempotency_key="legacy-key",status="failed",attempt=1,error="research_failure",created_at=stamp,updated_at=stamp,completed_at=stamp)
            for kind in ("grounded_rewrite","standard_exaggerated","market_ideal","keyword_adversarial"):
                real=kind=="grounded_rewrite"
                insert("resume_run_items",run_item_id="item_"+kind,run_id="run_legacy",variant_kind=kind,purpose="real_application" if real else "synthetic_research",base_version_id="version_legacy" if kind in {"grounded_rewrite","standard_exaggerated"} else None,input_fingerprint="e"*64,status="succeeded" if real else "failed",artifact_id="real_legacy" if real else None,output_fingerprint="d"*64 if real else None,error="" if real else "research_failure",attempts=1,completed_at=stamp)
            insert("resume_artifact_approvals",approval_id="approval_legacy",run_id="run_legacy",artifact_id="real_legacy",pdf_sha256="c"*64,content_sha256="d"*64,idempotency_key="approve-old",approved_by="user",approved_at=stamp)
            insert("application_resume_selections",selection_id="selection_legacy",application_id="app_old",artifact_id="real_legacy",idempotency_key="selection-old",selected_by="user",selected_at=stamp)
        store=ResumeLabStore(database)
        run=store.get_run("run_legacy")
        assert run["source_mode"]=="standard" and run["composition_id"] is None
        assert run["grounded_approval"]["approval_id"]=="approval_legacy"
        assert store.current_application_selection("app_old")["artifact_id"]=="real_legacy"
        assert store.get_artifact("real_legacy",include_content=True)["tex_source"]=="old-artifact-source"
        with sqlite3.connect(database) as con:
            assert con.execute("PRAGMA foreign_key_check").fetchall()==[]
            columns={row[1]:row for row in con.execute("PRAGMA table_info(resume_runs)")}
            assert not columns["selected_standard_id"][3] and not columns["base_version_id"][3]
        # Reopening must be idempotent, including historical ordering and triggers.
        assert ResumeLabStore(database).get_latest_application_run("app_old")["run_id"]=="run_legacy"


def test_async_source_import_is_reviewable_idempotent_and_drives_generation():
    from tests.test_career_import import TEXT, extraction
    class ImportModel(CareerModel):
        def _invoke(self, task, payload, schema):
            if task == "extract_career_profile":
                assert payload["source_text"] == TEXT
                return extraction()
            return super()._invoke(task,payload,schema)
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        gateway=ResumeLabProductionGateway(ResumeLabService(root/"resume.db"),
            ResumePdfArtifactRepository(root/"artifacts"),application_db=root/"applications.db",
            toolchain=Toolchain(),model=ImportModel())
        _, app=start_application(root)
        imported=gateway.import_career_document(TEXT.encode(),filename="resume.txt",content_type="text/plain",idempotency_key="import")
        assert imported["status"]=="queued"
        assert gateway.get_career_profile()["approved"] is None
        outcome=gateway.handle_work({"run_id":imported["import_id"]},Context())
        assert outcome["status"]=="succeeded",gateway.get_career_import(imported["import_id"])
        result=gateway.get_career_import(imported["import_id"])["result"]
        assert result["provenance"]["source_spans"]
        assert gateway.get_career_profile()["approved"] is None
        replay=gateway.import_career_document(TEXT.encode(),filename="resume.txt",content_type="text/plain",idempotency_key="import")
        assert replay["import_id"]==imported["import_id"] and replay["status"]=="succeeded"
        gateway.approve_career_profile(result["revision_id"],idempotency_key="approve-import")
        prepared=gateway.prepare(JOB,application_id=app,idempotency_key="imported-resume")
        outcome=gateway.handle_work({"run_id":prepared["run_id"]},Context())
        assert outcome["status"]=="succeeded",gateway.get_run_result(prepared["run_id"])


def test_new_profile_action_differs_from_retry_or_pin_regeneration():
    with tempfile.TemporaryDirectory() as directory:
        gateway, app=gateway_at(Path(directory))
        original=gateway.prepare(JOB,application_id=app,idempotency_key="old-profile")
        profile=gateway.get_career_profile()
        content=copy.deepcopy(profile["draft"]["content"])
        content["projects"].append({"name":"New API", "context":"Python", "dates":"2026", "bullets":["Built Python services."]})
        saved=gateway.save_career_profile(content,expected_revision_id=profile["draft_revision_id"],idempotency_key="expand")
        gateway.approve_career_profile(saved["draft_revision_id"],idempotency_key="approve-expanded")
        old=gateway.regenerate_career_run(original["run_id"],pinned_fact_ids=[],excluded_fact_ids=[],idempotency_key="old-again")
        new=gateway.regenerate_career_run(original["run_id"],pinned_fact_ids=[],excluded_fact_ids=[],use_latest_profile=True,idempotency_key="new-profile")
        assert old["composition"]["profile_revision_id"]==original["composition"]["profile_revision_id"]
        assert new["composition"]["profile_revision_id"]==saved["draft_revision_id"]


def main():
    tests=[v for k,v in globals().items() if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} career composition tests)")


if __name__=="__main__":
    main()
