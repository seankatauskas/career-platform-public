"""Career-profile operations mixed into the existing guarded resume gateway."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from job_search.resume_integration import ResumeArtifactContent

from .artifacts import ArtifactNamespace
from .career_composition import (CareerPageOverflow, artifact_claims, compose_content,
    select_composition, validate_rewrites)
from .career_runs import (CAREER_GROUNDING_REVISION, create_career_run,
    get_composition, save_composition)
from .contracts import (ArtifactInput, JobSnapshot, ResumeBoundaryError,
    ResumeConflictError, ResumeLabError, ResumePurpose, VariantKind,
    canonical_json, content_sha256, sha256_text, validate_identifier)
from .store import connect, _new_id, _now


class CareerGatewayMixin:
    @property
    def career_store(self):
        from .career_store import CareerStore
        if not hasattr(self, "_career_store"):
            self._career_store = CareerStore(self.service.store.db_path)
        return self._career_store

    def get_career_profile(self) -> Mapping[str, Any]:
        profile = dict(self.career_store.get_profile())
        with connect(self.service.store.db_path) as con:
            imports = con.execute("SELECT import_id,status,error,created_at FROM career_import_jobs ORDER BY rowid DESC LIMIT 10").fetchall()
        profile["imports"] = [dict(row) for row in imports]
        return profile

    def save_career_profile(self, content: Mapping[str, Any], *, expected_revision_id: str | None,
                            idempotency_key: str) -> Mapping[str, Any]:
        self.career_store.save_draft(content, expected_revision_id=expected_revision_id,
            provenance={"kind": "user_edit"}, idempotency_key=idempotency_key)
        return self.get_career_profile()

    def approve_career_profile(self, revision_id: str, *, idempotency_key: str) -> Mapping[str, Any]:
        self.career_store.approve_revision(revision_id, idempotency_key=idempotency_key)
        return self.get_career_profile()

    def export_career_profile(self) -> Mapping[str, Any]:
        return self.career_store.export_profile()

    def import_career_standard(self, standard_version_id: str, *, idempotency_key: str) -> Mapping[str, Any]:
        from .career_import import CareerImporter
        version = self.service.store.get_standard_version(standard_version_id)
        importer = CareerImporter(self.career_store, extract_content=self._extract_career_content)
        return importer.seed_standard(version, idempotency_key=idempotency_key)

    def _require_career_ready(self) -> None:
        from .gateway import ResumeSetupError
        if self.toolchain is None:
            raise ResumeSetupError("resume PDF toolchain is not configured")
        if self._enqueue_callback is None:
            raise ResumeSetupError("resume model queue is not configured")

    def prepare_from_career_profile(self, job: Mapping[str, Any], *, application_id: str,
                                   idempotency_key: str, pinned_fact_ids: Sequence[str] = (),
                                   excluded_fact_ids: Sequence[str] = (),
                                   revision_id: str | None = None) -> Mapping[str, Any]:
        from .tex import DEFAULT_TEMPLATE_VERSION
        snapshot = self._job(job)
        snapshot.validate()
        replay = self.service.get_run_by_idempotency_key(idempotency_key)
        if replay:
            self._validate_run_replay(replay, application_id, snapshot)
            if replay["source_mode"] != "career_profile":
                raise ResumeConflictError("request key belongs to an imported resume")
            with connect(self.service.store.db_path) as con:
                previous = get_composition(con, replay["composition_id"])["snapshot"]
            if (sorted(pinned_fact_ids) != previous["pinned_fact_ids"] or sorted(excluded_fact_ids) != previous["excluded_fact_ids"]
                    or revision_id is not None and revision_id != previous["profile_revision_id"]):
                raise ResumeConflictError("request key belongs to different career choices")
            return self.get_run_result(replay["run_id"])
        self._require_preparing_application(application_id, snapshot)
        self._require_career_ready()
        profile = self.career_store.get_profile()
        revision = self.career_store.get_revision(revision_id) if revision_id else profile.get("approved")
        if not revision:
            raise ResumeBoundaryError("approve your career profile before generating a resume")
        if revision_id and not self.career_store.is_approved(revision_id):
            raise ResumeBoundaryError("generation requires a previously approved career revision")
        frozen = select_composition(revision, snapshot, pinned_fact_ids=pinned_fact_ids,
            excluded_fact_ids=excluded_fact_ids, template_version=DEFAULT_TEMPLATE_VERSION)
        frozen["model_provenance"] = self._model_provenance()
        composition = save_composition(self.service.store, frozen)
        def create_and_dispatch(connection):
            run = create_career_run(self.service.store, application_id, snapshot, composition, idempotency_key)
            self._enqueue_run_while_locked(connection, run)
            return run
        run = self._while_application_preparing(application_id, snapshot, create_and_dispatch)
        return self._run_view(run)

    def regenerate_career_run(self, run_id: str, *, pinned_fact_ids: Sequence[str],
                              excluded_fact_ids: Sequence[str], idempotency_key: str,
                              use_latest_profile: bool = False) -> Mapping[str, Any]:
        if not isinstance(use_latest_profile, bool):
            raise ResumeLabError("use_latest_profile must be a boolean")
        run = self.service.get_run(run_id)
        if run["source_mode"] != "career_profile" or run["run_role"] != "primary":
            raise ResumeBoundaryError("only a factual career resume can be regenerated")
        with connect(self.service.store.db_path) as con:
            composition = get_composition(con, run["composition_id"])
        return self.prepare_from_career_profile(run["job_snapshot"], application_id=run["application_id"],
            revision_id=None if use_latest_profile else composition["profile_revision_id"], pinned_fact_ids=pinned_fact_ids,
            excluded_fact_ids=excluded_fact_ids, idempotency_key=idempotency_key)

    def start_research_comparisons(self, run_id: str, *, idempotency_key: str) -> Mapping[str, Any]:
        self._require_generation_ready()
        parent = self.service.get_run(run_id)
        if parent["source_mode"] != "career_profile" or parent["run_role"] != "primary" or parent["status"] != "succeeded":
            raise ResumeBoundaryError("finish a factual career resume before requesting research comparisons")
        with connect(self.service.store.db_path) as con:
            composition = get_composition(con, parent["composition_id"])
        job = JobSnapshot(**parent["job_snapshot"])
        def create_and_dispatch(connection):
            run = create_career_run(self.service.store, parent["application_id"], job, composition,
                idempotency_key, parent_run_id=run_id)
            self._enqueue_run_while_locked(connection, run)
            return run
        child = self._while_application_preparing(parent["application_id"], job, create_and_dispatch)
        return self._run_view(child)

    def _career_run_details(self, run: Mapping[str, Any]) -> Mapping[str, Any]:
        with connect(self.service.store.db_path) as con:
            composition = get_composition(con, run["composition_id"])
            children = con.execute("SELECT run_id FROM resume_runs WHERE parent_run_id=? ORDER BY rowid DESC LIMIT 10", (run["run_id"],)).fetchall()
        snapshot = composition["snapshot"]
        visible = {k: copy.deepcopy(snapshot[k]) for k in ("profile_revision_id", "template_version", "pinned_fact_ids", "excluded_fact_ids", "selected", "omitted", "page_target")}
        visible["composition_id"] = composition["composition_id"]
        visible["rewrites"] = []
        for item in run["items"]:
            if item["variant_kind"] == "grounded_rewrite" and item.get("artifact_id"):
                artifact = self.service.get_artifact(item["artifact_id"])
                metadata = artifact["metadata"]
                chosen = set(metadata["selected_fact_ids"])
                removed = [r for r in visible["selected"] if r["fact_id"] not in chosen]
                for row in removed:
                    row["reason"] = "Omitted to fit one page"
                visible["selected"] = [r for r in visible["selected"] if r["fact_id"] in chosen]
                visible["omitted"].extend(removed)
                visible["rewrites"] = metadata.get("changes", [])
                visible["wording_status"] = metadata.get("wording_status")
        return {"source_mode": "career_profile", "run_role": run["run_role"],
            "parent_run_id": run["parent_run_id"], "composition": visible,
            "research_runs": [self._run_view(self.service.get_run(row["run_id"])) for row in children]}

    def get_artifact_source(self, artifact_id: str) -> ResumeArtifactContent:
        artifact = self.service.get_artifact(artifact_id, include_content=True)
        source = artifact["tex_source"].encode("utf-8")
        return ResumeArtifactContent(artifact_id=artifact_id,
            filename="resume-" + artifact["variant_kind"].replace("_", "-") + ".tex",
            content_type="text/plain; charset=utf-8", content=source, sha256=hashlib.sha256(source).hexdigest())

    def _build_career_variant(self, run, job, graph, kind, assert_owned) -> str:
        from .career_model import rewrite_selected
        from .gateway import (GATEWAY_REVISION, SYNTHETIC_NOTICE, _score_mapping,
            _fidelity_scalar, _raise_if_runpod_reconciliation)
        with connect(self.service.store.db_path) as con:
            composition = get_composition(con, run["composition_id"])
        snapshot = composition["snapshot"]
        if snapshot.get("model_provenance") != self._model_provenance():
            raise ResumeConflictError("career generation model changed; create a new composition")
        synthetic = kind is not VariantKind.GROUNDED_REWRITE
        selected = [r["fact_id"] for r in snapshot["selected"]]
        rewrites: dict[str, str] = {}
        wording_status = "approved_wording"
        seed = int(hashlib.sha256(f"{run['run_id']}\0{kind.value}".encode()).hexdigest()[:8],16) % 2147483648
        if synthetic:
            parent = self.service.get_run(run["parent_run_id"])
            item = next(i for i in parent["items"] if i["variant_kind"] == "grounded_rewrite")
            base = self.service.get_artifact(item["artifact_id"])["metadata"]
            selected, rewrites = base["selected_fact_ids"], base.get("rewrites", {})
            content, mapping = compose_content(snapshot, selected, rewrites)
            standards = [{"standard_id": composition["composition_id"], "rank": 1, "content": content}]
            sources = [{"claim_id": r["fact_id"], "text": r["output_text"], "path": r["path"], "origin": "user_attested"} for r in mapping]
            output = self._generate_output(kind, job, graph, seed, standards, sources, self._guarded_terms(graph))
            assert_owned()
            build = self.toolchain.build(output["content"], allow_warning=False, visible_notice=SYNTHETIC_NOTICE,
                template_version=snapshot["template_version"])
            if not self._notice_survived(build.extracted.logical_text) or not self._notice_survived(build.extracted.layout_text):
                raise ResumeBoundaryError("synthetic research notice did not survive PDF extraction")
            claims = self._generated_claims(kind, output["claims"])
            changes = []
        else:
            if self.model is not None:
                try:
                    rewrites = rewrite_selected(self.model, snapshot, asdict(job))
                    wording_status = "source_checked_rewrite"
                except Exception as exc:
                    _raise_if_runpod_reconciliation(exc)
                    # A rejected rewrite never turns into an invented fact. The
                    # reviewed source wording remains a useful factual resume.
                    wording_status = "approved_wording_rewrite_unavailable"
            for attempt in range(6):
                assert_owned()
                content, mapping = compose_content(snapshot, selected, rewrites)
                overflow = False
                try:
                    build = self.toolchain.build(content, allow_warning=False,
                        template_version=snapshot["template_version"])
                    overflow = build.extracted.pages != 1
                except Exception as exc:
                    if type(exc).__name__ not in {"TexLayoutOverflowError", "ResumePageBudgetError"}:
                        raise
                    overflow = True
                if not overflow:
                    break
                candidates = [r for r in snapshot["selected"] if r["fact_id"] in selected and not r["pinned"]]
                pinned_parents = {r["entry_id"] for r in snapshot["selected"] if r["pinned"]}
                candidates = [r for r in candidates if r["kind"] != "entry" or r["entry_id"] not in pinned_parents]
                if not candidates or attempt == 5:
                    raise CareerPageOverflow("required content cannot fit one page; unpin or shorten content")
                candidates.sort(key=lambda r:(r["score"], r["kind"] == "entry", -len(r["text"]), r["fact_id"]))
                for row in candidates[:max(1, len(candidates)//4)]:
                    if row["fact_id"] not in selected:
                        continue
                    selected.remove(row["fact_id"])
                    if row["kind"] == "entry":
                        selected = [fid for fid in selected if not any(r["fact_id"] == fid and r["entry_id"] == row["entry_id"] for r in snapshot["selected"])]
                # Remove headings orphaned by pruning their originally selected
                # facts, while retaining deliberately selected heading-only roles.
                for row in snapshot["selected"]:
                    if row["kind"] == "entry" and row["section"] != "education" and row["fact_id"] in selected and not row["pinned"]:
                        originally_had_facts = any(r["kind"] != "entry" and r["entry_id"] == row["entry_id"] for r in snapshot["selected"])
                        if originally_had_facts and not any(r["kind"] != "entry" and r["entry_id"] == row["entry_id"] and r["fact_id"] in selected for r in snapshot["selected"]):
                            selected.remove(row["fact_id"])
                if not selected:
                    raise CareerPageOverflow("no career content fits one page; shorten the profile before regenerating")
            claims = artifact_claims(mapping, content)
            changes = mapping
        assert_owned()
        evaluation = self._score(build.extracted.logical_text, graph, [asdict(c) for c in claims])
        assert_owned()
        stored = self.artifacts.write_pdf(build.compiled.pdf_bytes,
            ArtifactNamespace.RESEARCH if synthetic else ArtifactNamespace.REAL)
        metadata = {**_score_mapping(evaluation), "source_mode": "career_profile",
            "composition_id": composition["composition_id"], "profile_revision_id": composition["profile_revision_id"],
            "template_version": snapshot["template_version"], "selected_fact_ids": selected,
            "rewrites": rewrites if not synthetic else {}, "changes": changes,
            "wording_status": wording_status, "grounding_validator_revision": CAREER_GROUNDING_REVISION if not synthetic else None,
            "model_provenance": self._model_provenance(), "research_only": synthetic,
            "visible_research_notice": SYNTHETIC_NOTICE if synthetic else None,
            "pages": build.extracted.pages, "fidelity": asdict(build.fidelity),
            "compiler": build.compiled.engine, "compiler_version": build.compiled.engine_version,
            "bundle_sha256": build.compiled.bundle_sha256, "parser": build.extracted.parser,
            "parser_version": build.extracted.parser_version, "gateway_revision": GATEWAY_REVISION}
        value = ArtifactInput(variant_kind=kind, purpose=ResumePurpose.SYNTHETIC_RESEARCH if synthetic else ResumePurpose.REAL_APPLICATION,
            job=job, tex_source=build.rendered.tex_source, intended_text=build.rendered.intended_text, claims=claims,
            managed_relative_path=stored.managed_relative_path, pdf_sha256=stored.sha256,
            parsed_text=build.extracted.logical_text, parse_fidelity=_fidelity_scalar(build.fidelity), parse_safe=True,
            generator_revision="career-gateway-v1", source_mode="career_profile", composition_id=composition["composition_id"],
            study_id="study_" + job.fingerprint[:24] if synthetic else None,
            pair_id="pair_" + run["run_id"] if synthetic else None,
            treatment=kind.value if synthetic else "", generation_seed=seed if synthetic else None, metadata=metadata)
        assert_owned()
        artifact = self.service.register_artifact(value)
        self.service.store.put_evaluation(artifact["artifact_id"], graph, evaluation)
        return artifact["artifact_id"]

    def import_career_document(self, content: bytes, *, filename: str, content_type: str,
                               idempotency_key: str) -> Mapping[str, Any]:
        from .gateway import ResumeSetupError
        if not isinstance(content, bytes) or not 0 < len(content) <= 5 * 1024 * 1024:
            raise ResumeLabError("career document must be between 1 byte and 5 MiB")
        if not isinstance(filename, str) or len(filename) > 200 or any(x in filename for x in ("/", "\\", "\x00")):
            raise ResumeLabError("career document filename is invalid")
        if content_type not in {"application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain", "application/json"}:
            raise ResumeLabError("import a PDF, DOCX, text, or exported career JSON document")
        validate_identifier(idempotency_key, "idempotency_key")
        if self._enqueue_callback is None:
            raise ResumeSetupError("resume model queue is not configured")
        request = content_sha256({"bytes": hashlib.sha256(content).hexdigest(), "filename":filename,"content_type":content_type})
        expected = self.career_store.get_profile().get("draft_revision_id")
        with connect(self.service.store.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM career_import_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if row["request_sha256"] != request:
                    raise ResumeConflictError("career import key belongs to different content")
                import_id = row["import_id"]
            else:
                import_id = _new_id("import_")
                con.execute("INSERT INTO career_import_jobs(import_id,idempotency_key,request_sha256,filename,content_type,source_bytes,expected_revision_id,status,created_at) VALUES (?,?,?,?,?,?,?,'queued',?)",
                    (import_id,idempotency_key,request,filename,content_type,content,expected,_now()))
        return self.get_career_import(import_id)

    def get_career_import(self, import_id: str) -> Mapping[str, Any]:
        validate_identifier(import_id, "import_id")
        with connect(self.service.store.db_path) as con:
            row = con.execute("SELECT * FROM career_import_jobs WHERE import_id=?", (import_id,)).fetchone()
        if not row:
            from .contracts import ResumeNotFoundError
            raise ResumeNotFoundError("career import was not found")
        if row["status"] in {"queued", "running"} and self._enqueue_callback is not None:
            # Same dispatch identity reconciles an import committed before enqueue.
            self._enqueue_callback(import_id, "career_import_" + import_id)
        return {"import_id": import_id, "status": row["status"], "error_code": row["error"] or None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None}

    def _extract_career_content(self, text: str) -> Mapping[str, Any]:
        from .career_import import EXTRACTION_SCHEMA
        from .gateway import ResumeSetupError
        invoke = getattr(self.model, "_invoke", None)
        if not callable(invoke):
            raise ResumeSetupError("configure a resume model to extract facts, or use the structured editor")
        return invoke("extract_career_profile", {"source_text": text}, EXTRACTION_SCHEMA)

    def _handle_career_import(self, import_id: str, context: Any) -> Mapping[str, Any]:
        from .career_import import CareerImporter
        from .gateway import _LeaseGuard, _LostLease, _safe_error_code, _raise_if_runpod_reconciliation
        from ..inference.usage import work_can_resume
        with connect(self.service.store.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM career_import_jobs WHERE import_id=?", (import_id,)).fetchone()
            if not row:
                raise ResumeLabError("career import was not found")
            if row["status"] in {"succeeded", "failed"}:
                return {"run_id":import_id,"status":row["status"]}
            if row["status"] == "running" and self._uses_queued_runpod_model() and not work_can_resume(self.application_db, str(context.work_id)):
                con.execute("UPDATE career_import_jobs SET status='failed',error='runpod_reconciliation_required' WHERE import_id=?", (import_id,))
                return {"run_id":import_id,"status":"failed"}
            owner = content_sha256({"work":str(context.work_id),"attempt":context.attempt})
            con.execute("UPDATE career_import_jobs SET status='running',attempt=attempt+1,owner_token=? WHERE import_id=?", (owner,import_id))
        with _LeaseGuard(context.heartbeat) as lease:
            try:
                def assert_owned():
                    lease.check(refresh=True)
                    with connect(self.service.store.db_path) as con:
                        current = con.execute("SELECT owner_token,status FROM career_import_jobs WHERE import_id=?", (import_id,)).fetchone()
                    if current["owner_token"] != owner or current["status"] != "running":
                        raise _LostLease("career import lease was lost")
                def extract(text):
                    output = self._extract_career_content(text)
                    assert_owned()
                    return output
                importer = CareerImporter(self.career_store, extract_content=extract,
                    pdf_extractor=getattr(self.toolchain, "extractor", None),
                    attachment_extractor=getattr(self, "career_attachment_extractor", None))
                assert_owned()
                result = importer.import_file(row["filename"], bytes(row["source_bytes"]),
                    expected_revision_id=row["expected_revision_id"], idempotency_key="career_import_result_"+import_id)
                assert_owned()
                with connect(self.service.store.db_path) as con:
                    con.execute("UPDATE career_import_jobs SET status='succeeded',result_json=? WHERE import_id=? AND owner_token=?", (canonical_json(result),import_id,owner))
                return {"run_id":import_id,"status":"succeeded"}
            except _LostLease:
                raise
            except Exception as exc:
                _raise_if_runpod_reconciliation(exc)
                with connect(self.service.store.db_path) as con:
                    con.execute("UPDATE career_import_jobs SET status='failed',error=? WHERE import_id=? AND owner_token=?", (_safe_error_code(exc),import_id,owner))
                return {"run_id":import_id,"status":"failed"}
