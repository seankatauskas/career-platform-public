#!/usr/bin/env python3
"""Offline checks for the isolated resume-lab domain core."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from dataclasses import replace
from pathlib import Path

from job_search.resume_lab import (
    ArtifactInput,
    ClaimOrigin,
    EvidenceStatus,
    JobSnapshot,
    MAX_REQUIREMENTS,
    RequirementKind,
    RequirementPriority,
    ResumeBoundaryError,
    ResumeClaim,
    ResumeConflictError,
    ResumeLabError,
    ResumeLabService,
    ResumePurpose,
    StandardVersionInput,
    VariantKind,
    extract_requirement_graph,
    merge_requirement_clauses,
    requirement_graph_from_clauses,
    score_ats_proxy,
)
from job_search.resume_lab.store import connect
from job_search.resume_lab.grounding import (
    GROUNDING_EQUIVALENCE_REVISION,
    GROUNDING_VALIDATOR_REVISION,
)
from job_search.resume_lab.tex import render_resume_tex


JOB = JobSnapshot(
    ats="greenhouse",
    job_id="job-42",
    employer="Acme",
    title="Senior Platform Engineer",
    description="""
        <h2>Required Qualifications</h2>
        <ul>
          <li>At least 3+ years of experience building Python services with AWS or GCP.</li>
          <li>Bachelor's degree or equivalent experience.</li>
        </ul>
        <h2>Preferred Qualifications</h2>
        <ul><li>Experience with Kubernetes and Docker is preferred.</li></ul>
        <h2>Responsibilities</h2>
        <ul><li>Build observability for production data pipelines.</li></ul>
        <p>Must be authorized to work in the United States.</p>
        <p>We are an equal opportunity employer regardless of race, gender, or disability.</p>
    """,
)


def claim(
    identifier: str = "claim-1",
    text: str = "Built Python services on AWS.",
    origin: ClaimOrigin = ClaimOrigin.USER_ATTESTED,
    facts=("fact-1",),
) -> ResumeClaim:
    return ResumeClaim(identifier, text, origin, tuple(facts))


def version(
    marker: str = "one", claims=None, *, normalized=True
) -> StandardVersionInput:
    values = tuple(claims or (claim(),))
    plain_text = (
        "Senior Platform Engineer\n"
        "5 years building Python services on AWS.\n"
        "Built Python services on AWS.\n"
        "Bachelor's degree in Computer Science.\n"
        "Kubernetes, Docker, observability, and data pipelines.\n"
        "Authorized to work in the United States.\n" + marker
    )
    source_start = plain_text.index(values[0].text)
    return StandardVersionInput(
        tex_source=r"\documentclass{article}\begin{document}"
        + marker
        + r"\end{document}",
        plain_text=plain_text,
        claims=values,
        normalized_content=(
            {
                "identity": {"name": "Test Candidate"},
                "summary": values[0].text,
                "experience": [],
                "projects": [],
                "education": [],
                "skills": [],
            }
            if normalized
            else None
        ),
        import_metadata=(
            {
                "managed_relative_path": "real/standards/imported.pdf",
                "pdf_sha256": "d" * 64,
                "parsed_text": plain_text,
                "parse_fidelity": 1.0,
                "normalization_claims": [
                    {
                        "claim_id": values[0].claim_id,
                        "path": "/summary",
                        "text": values[0].text,
                        "source_start": source_start,
                        "source_end": source_start + len(values[0].text),
                        "allowed_equivalent_terms": [],
                    }
                ],
            }
            if normalized
            else None
        ),
    )


def make_service(directory: str) -> tuple[Path, ResumeLabService]:
    path = Path(directory) / "private" / "resume-lab.db"
    return path, ResumeLabService(path)


def create_standard(
    service: ResumeLabService, name: str = "Primary", rank: int = 1, marker: str = "one"
):
    return service.create_standard(
        name, rank, version(marker), actor_kind="user"
    )


def real_artifact(
    base_version: dict,
    *,
    variant=VariantKind.STANDARD,
    text=None,
    parse_safe=True,
    path="real/job-42/resume.pdf",
    claims=None,
) -> ArtifactInput:
    values = tuple(
        claims
        or (
            claim(
                "claim-rewrite",
                "Built Python services on AWS.",
                ClaimOrigin.GENERATED_WORDING,
                (str(base_version["claims"][0]["claim_id"]),),
            ),
        )
    )
    if variant is VariantKind.STANDARD:
        values = tuple(
            ResumeClaim(
                item["claim_id"],
                item["text"],
                ClaimOrigin(item["origin"]),
                tuple(item["source_fact_ids"]),
            )
            for item in base_version["claims"]
        )
        intended = text or base_version["plain_text"]
        tex = base_version["tex_source"]
        metadata = {}
    else:
        content = dict(base_version["normalized_content"])
        content["summary"] = values[0].text
        rendered = render_resume_tex(content)
        intended = rendered.intended_text
        tex = rendered.tex_source
        metadata = {
            "grounding_output": {
                "variant_kind": "grounded_rewrite",
                "base_standard_id": str(base_version["standard_id"]),
                "synthetic": False,
                "research_only": False,
                "content": content,
                "claims": [
                    {
                        "claim_id": item.claim_id,
                        "path": "/summary",
                        "text": item.text,
                        "origin": "source_rewrite",
                        "source_claim_ids": list(item.source_fact_ids),
                        "added_equivalent_terms": [],
                    }
                    for item in values
                ],
            },
            "grounding_validator_revision": GROUNDING_VALIDATOR_REVISION,
            "grounding_equivalence_revision": GROUNDING_EQUIVALENCE_REVISION,
        }
    return ArtifactInput(
        variant_kind=variant,
        purpose=ResumePurpose.REAL_APPLICATION,
        job=JOB,
        tex_source=tex,
        intended_text=intended,
        claims=values,
        managed_relative_path=path,
        pdf_sha256="a" * 64,
        parsed_text=intended,
        parse_fidelity=1.0,
        parse_safe=parse_safe,
        generator_revision="renderer-v1",
        base_version_id=base_version["version_id"],
        metadata=metadata,
    )


def synthetic_artifact(
    *,
    variant=VariantKind.MARKET_IDEAL,
    base_version_id=None,
    path="research/study-1/ideal.pdf",
    run_id=None,
) -> ArtifactInput:
    return ArtifactInput(
        variant_kind=variant,
        purpose=ResumePurpose.SYNTHETIC_RESEARCH,
        job=JOB,
        tex_source=r"\documentclass{article}\begin{document}ideal\end{document}",
        intended_text="10 years Python AWS GCP Kubernetes Docker observability data pipelines.",
        claims=(
            claim(
                "synthetic-claim",
                "10 years Python AWS GCP Kubernetes Docker.",
                ClaimOrigin.SYNTHETIC_GENERATED,
                (),
            ),
        ),
        managed_relative_path=path,
        pdf_sha256="b" * 64,
        parsed_text="10 years Python AWS GCP Kubernetes Docker observability data pipelines.",
        parse_fidelity=1.0,
        parse_safe=True,
        generator_revision="synthetic-v1",
        base_version_id=base_version_id,
        study_id=("study_" + JOB.fingerprint[:24]) if run_id else "study-1",
        pair_id=("pair_" + run_id) if run_id else "pair-1",
        treatment=variant.value if run_id else "ceiling",
        generation_seed=7,
    )


def complete_successful_run(
    service: ResumeLabService,
    base_version: dict,
    *,
    application_id="application-1",
    idempotency_key="complete-run",
):
    run = service.create_run(application_id, JOB, idempotency_key)
    service.start_run(run["run_id"])
    inputs = {
        VariantKind.GROUNDED_REWRITE: real_artifact(
            base_version,
            variant=VariantKind.GROUNDED_REWRITE,
            path="real/job-42/grounded.pdf",
        ),
        VariantKind.STANDARD_EXAGGERATED: synthetic_artifact(
            variant=VariantKind.STANDARD_EXAGGERATED,
            base_version_id=base_version["version_id"],
            path="research/study-1/exaggerated.pdf",
            run_id=run["run_id"],
        ),
        VariantKind.MARKET_IDEAL: synthetic_artifact(
            variant=VariantKind.MARKET_IDEAL,
            path="research/study-1/market.pdf",
            run_id=run["run_id"],
        ),
        VariantKind.KEYWORD_ADVERSARIAL: synthetic_artifact(
            variant=VariantKind.KEYWORD_ADVERSARIAL,
            path="research/study-1/adversarial.pdf",
            run_id=run["run_id"],
        ),
    }
    artifacts = {}
    for kind, artifact_input in inputs.items():
        artifact = service.register_artifact(artifact_input)
        artifacts[kind] = artifact
        service.complete_run_item(
            run["run_id"], kind, "succeeded", artifact_id=artifact["artifact_id"]
        )
    return service.complete_run(run["run_id"], "succeeded"), artifacts


def test_sidecar_is_private_and_rejects_a_symlink() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, _service = make_service(directory)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        with connect(path) as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert (
                connection.execute("SELECT version FROM resume_lab_schema").fetchone()[
                    0
                ]
                == 1
            )
        link = Path(directory) / "resume-link.db"
        link.symlink_to(path)
        try:
            ResumeLabService(link)
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("symbolic-link sidecar was accepted")


def test_sidecar_detects_path_replacement_after_identity_is_pinned() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, _service = make_service(directory)
        replacement = Path(directory) / "replacement.db"
        with sqlite3.connect(replacement) as connection:
            connection.execute("CREATE TABLE substituted(value TEXT)")
        os.chmod(replacement, 0o600)
        os.replace(replacement, path)
        try:
            connect(path)
        except ResumeBoundaryError as exc:
            assert "identity changed" in str(exc)
        else:
            raise AssertionError("a substituted resume-lab database was accepted")


def test_standards_are_hand_written_versioned_and_manually_ranked() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        lower = create_standard(service, "Systems", 2, "systems")
        highest = create_standard(service, "ML", 1, "ml")
        active = service.list_active_standards()
        assert [item["name"] for item in active] == ["ML", "Systems"]
        selected, reason = service.resolve_standard()
        assert selected["standard_id"] == highest["standard"]["standard_id"]
        assert reason == "highest_manual_rank"
        explicit, reason = service.resolve_standard(lower["standard"]["standard_id"])
        assert explicit["name"] == "Systems" and reason == "manual_override"
        try:
            service.set_standard_rank(
                lower["standard"]["standard_id"], 1, actor_kind="model"
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("a model changed the manual standard ranking")


def test_tex_only_standard_scores_but_derived_run_requires_normalized_content() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        created = service.create_standard(
            "TeX only", 1, version(normalized=False), actor_kind="user"
        )
        assert created["version"]["normalized_content"] is None
        artifact = service.register_artifact(real_artifact(created["version"]))
        assert service.evaluate_artifact(artifact["artifact_id"], JOB)["parsed_fit"] > 0
        try:
            service.create_run("application-1", JOB, "tex-only-run")
        except ResumeConflictError as exc:
            assert "normalized_content" in str(exc)
        else:
            raise AssertionError("derived generation began without normalized content")


def test_exact_comparison_has_all_baselines_and_exactly_four_derived_slots() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        lower = create_standard(service, "Systems", 2, "systems")
        highest = create_standard(service, "ML", 1, "ml")
        manifest = service.build_comparison_manifest(JOB)
        standards = [
            slot for slot in manifest.slots if slot.variant_kind is VariantKind.STANDARD
        ]
        derived = [
            slot
            for slot in manifest.slots
            if slot.variant_kind is not VariantKind.STANDARD
        ]
        assert [slot.label for slot in standards] == ["ML", "Systems"]
        assert [slot.variant_kind for slot in derived] == [
            VariantKind.GROUNDED_REWRITE,
            VariantKind.STANDARD_EXAGGERATED,
            VariantKind.MARKET_IDEAL,
            VariantKind.KEYWORD_ADVERSARIAL,
        ]
        assert manifest.selected_standard_id == highest["standard"]["standard_id"]
        assert all(
            slot.base_version_id == highest["version"]["version_id"]
            for slot in derived[:2]
        )
        assert all(slot.base_version_id is None for slot in derived[2:])
        overridden = service.build_comparison_manifest(
            JOB, standard_id=lower["standard"]["standard_id"]
        )
        assert overridden.selection_reason == "manual_override"
        assert overridden.selected_standard_id == lower["standard"]["standard_id"]


def test_standard_versions_are_immutable_and_activation_is_manual() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        created = create_standard(service)
        standard_id = created["standard"]["standard_id"]
        first = created["version"]
        replay = service.add_standard_version(
            standard_id, version("one"), actor_kind="user"
        )
        assert replay["version_id"] == first["version_id"]
        second = service.add_standard_version(
            standard_id, version("two"), actor_kind="user", activate=False
        )
        assert second["version_number"] == 2
        assert (
            service.store.get_standard(standard_id)["active_version_id"]
            == first["version_id"]
        )
        try:
            service.activate_standard_version(
                standard_id, second["version_id"], actor_kind="model"
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("model activated a standard version")
        service.activate_standard_version(
            standard_id, second["version_id"], actor_kind="user"
        )
        assert (
            service.store.get_standard(standard_id)["active_version_id"]
            == second["version_id"]
        )
        with connect(path) as connection:
            try:
                connection.execute(
                    "UPDATE resume_standard_versions SET plain_text='changed' WHERE version_id=?",
                    (first["version_id"],),
                )
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc)
            else:
                raise AssertionError("immutable standard version was updated")


def test_artifact_contract_enforces_kind_purpose_claim_and_path_boundaries() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        base = create_standard(service)["version"]
        standard = service.register_artifact(real_artifact(base))
        assert standard["artifact_id"].startswith("real_")
        assert standard["purpose"] == "real_application"

        grounded = service.register_artifact(
            real_artifact(base, variant=VariantKind.GROUNDED_REWRITE)
        )
        assert grounded["variant_kind"] == "grounded_rewrite"

        exaggerated = service.register_artifact(
            synthetic_artifact(
                variant=VariantKind.STANDARD_EXAGGERATED,
                base_version_id=base["version_id"],
                path="research/study-1/exaggerated.pdf",
            )
        )
        assert exaggerated["artifact_id"].startswith("syn_")

        bad_values = (
            replace(
                synthetic_artifact(),
                purpose=ResumePurpose.REAL_APPLICATION,
                study_id=None,
                pair_id=None,
                treatment="",
                generation_seed=None,
                managed_relative_path="real/bad.pdf",
            ),
            real_artifact(
                base,
                variant=VariantKind.GROUNDED_REWRITE,
                claims=(
                    claim(
                        "ungrounded",
                        "Invented rewrite.",
                        ClaimOrigin.GENERATED_WORDING,
                        (),
                    ),
                ),
            ),
            real_artifact(
                base,
                variant=VariantKind.GROUNDED_REWRITE,
                claims=(
                    claim(
                        "unknown-source",
                        "Reworded claim.",
                        ClaimOrigin.GENERATED_WORDING,
                        ("missing-source-claim",),
                    ),
                ),
            ),
            replace(synthetic_artifact(), managed_relative_path="../escape.pdf"),
            replace(synthetic_artifact(), managed_relative_path="real/wrong-root.pdf"),
        )
        for bad in bad_values:
            try:
                service.register_artifact(bad)
            except (ResumeBoundaryError, ValueError):
                pass
            else:
                raise AssertionError("artifact crossed a purpose/provenance boundary")


def test_persistence_rejects_fabricated_grounded_text_with_a_real_source_id() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        base = create_standard(service)["version"]
        fabricated = claim(
            "fabricated-rewrite",
            "Architected global quantum systems.",
            ClaimOrigin.GENERATED_WORDING,
            (str(base["claims"][0]["claim_id"]),),
        )
        try:
            service.register_artifact(
                real_artifact(
                    base,
                    variant=VariantKind.GROUNDED_REWRITE,
                    claims=(fabricated,),
                )
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("fabricated grounded wording crossed persistence")


def test_persistence_uses_immutable_normalization_path_for_grounding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        value = version()
        metadata = dict(value.import_metadata or {})
        rows = [dict(item) for item in metadata["normalization_claims"]]
        rows[0]["path"] = "/experience/0/bullets/0"
        metadata["normalization_claims"] = rows
        base = service.create_standard(
            "Misattributed",
            1,
            replace(value, import_metadata=metadata),
            actor_kind="user",
        )["version"]
        try:
            service.register_artifact(
                real_artifact(base, variant=VariantKind.GROUNDED_REWRITE)
            )
        except ResumeBoundaryError as exc:
            assert "source path" in str(exc)
        else:
            raise AssertionError("grounded wording moved from its normalized source path")


def test_standard_artifact_must_exactly_reproduce_the_version() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        base = create_standard(service)["version"]
        changed = real_artifact(base, text=base["plain_text"] + "\nchanged")
        try:
            service.register_artifact(changed)
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("modified standard artifact was accepted")


def test_application_selection_is_manual_real_parse_safe_and_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        base = create_standard(service)["version"]
        real = service.register_artifact(real_artifact(base))
        synthetic = service.register_artifact(synthetic_artifact())
        unsafe = service.register_artifact(
            real_artifact(base, variant=VariantKind.GROUNDED_REWRITE, parse_safe=False)
        )
        selected = service.select_for_application(
            "application-1", real["artifact_id"], "selection-1", actor_kind="user"
        )
        replay = service.select_for_application(
            "application-1", real["artifact_id"], "selection-1", actor_kind="user"
        )
        assert selected == replay
        assert (
            service.current_application_selection("application-1")["artifact_id"]
            == real["artifact_id"]
        )
        for artifact_id, actor in (
            (synthetic["artifact_id"], "user"),
            (unsafe["artifact_id"], "user"),
            (real["artifact_id"], "model"),
        ):
            try:
                service.select_for_application(
                    "application-2",
                    artifact_id,
                    "select-" + artifact_id,
                    actor_kind=actor,
                )
            except ResumeBoundaryError:
                pass
            else:
                raise AssertionError(
                    "unsafe application artifact selection was accepted"
                )
        with connect(path) as connection:
            try:
                connection.execute(
                    "INSERT INTO application_resume_selections "
                    "(selection_id,application_id,artifact_id,idempotency_key,selected_by,selected_at) "
                    "VALUES ('sel_direct','app-direct',?,'direct-key','user','2026-01-01T00:00:00Z')",
                    (synthetic["artifact_id"],),
                )
            except sqlite3.IntegrityError as exc:
                assert "parse-safe real" in str(exc)
            else:
                raise AssertionError(
                    "SQLite boundary accepted a synthetic application artifact"
                )


def test_requirement_graph_preserves_priority_or_logic_and_omits_eeo() -> None:
    graph = extract_requirement_graph(JOB)
    assert graph.job_fingerprint == JOB.fingerprint
    assert graph.requirements
    assert all(
        "equal opportunity" not in item.source_text.casefold()
        for item in graph.requirements
    )
    python = next(item for item in graph.requirements if "Python" in item.source_text)
    assert python.priority is RequirementPriority.REQUIRED
    assert python.kind is RequirementKind.EXPERIENCE
    assert python.minimum_years == 3.0
    assert any("aws" in group and "gcp" in group for group in python.term_groups)
    preferred = next(
        item for item in graph.requirements if "Kubernetes" in item.source_text
    )
    assert preferred.priority is RequirementPriority.PREFERRED
    assert len(preferred.term_groups) == 2
    eligibility = [
        item for item in graph.requirements if item.kind is RequirementKind.ELIGIBILITY
    ]
    assert len(eligibility) == 1


def test_requirement_graph_fails_closed_before_unbounded_scoring_work() -> None:
    description = "\n".join(
        f"Required Python capability marker {index}."
        for index in range(MAX_REQUIREMENTS + 1)
    )
    oversized = JobSnapshot(
        ats="ashby",
        job_id="oversized-requirements",
        title="Platform Engineer",
        description=description,
    )
    try:
        extract_requirement_graph(oversized)
    except ResumeLabError as exc:
        assert "requirement graph limit" in str(exc)
    else:
        raise AssertionError("an unbounded deterministic requirement graph was accepted")


def test_proxy_score_is_deterministic_explainable_and_exposes_parsed_fit() -> None:
    graph = extract_requirement_graph(JOB)
    text = version().plain_text
    first = score_ats_proxy(text, graph)
    second = score_ats_proxy(text, graph)
    assert first == second
    assert first.cache_key == second.cache_key
    assert first.parsed_fit == first.screening_readiness
    assert 70 <= first.parsed_fit <= 100
    assert first.eligibility_status.value == "clear"
    python = next(item for item in first.criteria if "Python" in item.source_text)
    assert python.status is EvidenceStatus.MET
    assert python.evidence
    for evidence in python.evidence:
        assert text[evidence.start : evidence.end] == evidence.text


def test_missing_duration_is_partial_and_explicit_eligibility_conflict_is_separate() -> (
    None
):
    graph = extract_requirement_graph(JOB)
    result = score_ats_proxy(
        "Built Python services on AWS. Bachelor's degree. Not authorized to work.",
        graph,
    )
    python = next(item for item in result.criteria if "Python" in item.source_text)
    assert python.status is EvidenceStatus.PARTIAL
    assert result.eligibility_status.value == "blocked"
    assert all(
        item.weight == 0
        for item in result.criteria
        if item.kind is RequirementKind.ELIGIBILITY
    )


def test_numeric_duration_is_bound_to_the_matched_skill_clause() -> None:
    job = JobSnapshot(
        ats="ashby",
        job_id="duration-association",
        employer="Acme",
        title="Python Engineer",
        description="Required qualifications:\n- 5+ years of Python experience.",
    )
    graph = extract_requirement_graph(job)
    requirement = next(item for item in graph.requirements if "Python" in item.source_text)
    for resume in (
        "2 years Python, 10 years Java.",
        "Python. 10 years Java.",
    ):
        result = score_ats_proxy(resume, graph)
        criterion = next(
            item
            for item in result.criteria
            if item.requirement_id == requirement.requirement_id
        )
        assert criterion.status is EvidenceStatus.PARTIAL


def test_eligibility_evidence_is_scoped_to_each_requirement_concept() -> None:
    job = JobSnapshot(
        ats="greenhouse",
        job_id="eligibility-association",
        employer="Acme",
        title="Field Engineer",
        description=(
            "Required qualifications:\n"
            "- Must hold an active security clearance.\n"
            "- Must be located in Chicago, IL.\n"
            "- Must be authorized to work in the United States.\n"
            "- Must be willing to travel 50%."
        ),
    )
    graph = extract_requirement_graph(job)
    eligibility = [
        item for item in graph.requirements if item.kind is RequirementKind.ELIGIBILITY
    ]
    assert len(eligibility) == 4

    authorization_only = score_ats_proxy(
        "Authorized to work in the United States.", graph
    )
    statuses = {
        item.source_text: item.status
        for item in authorization_only.criteria
        if item.kind is RequirementKind.ELIGIBILITY
    }
    assert (
        statuses[next(text for text in statuses if "clearance" in text)]
        is EvidenceStatus.UNKNOWN
    )
    assert (
        statuses[next(text for text in statuses if "Chicago" in text)]
        is EvidenceStatus.UNKNOWN
    )
    assert (
        statuses[next(text for text in statuses if "authorized" in text)]
        is EvidenceStatus.MET
    )
    assert authorization_only.eligibility_status.value == "unknown"

    unrelated_negative = score_ats_proxy("I require sponsorship.", graph)
    travel = next(
        item
        for item in unrelated_negative.criteria
        if item.kind is RequirementKind.ELIGIBILITY and "travel" in item.source_text
    )
    authorization = next(
        item
        for item in unrelated_negative.criteria
        if item.kind is RequirementKind.ELIGIBILITY
        and "authorized" in item.source_text
    )
    assert travel.status is EvidenceStatus.UNKNOWN
    assert authorization.status is EvidenceStatus.CONTRADICTED

    complete = score_ats_proxy(
        "Active security clearance.\n"
        "Location: Chicago, IL.\n"
        "Authorized to work in the United States.\n"
        "Travel: 50%.",
        graph,
    )
    assert all(
        item.status is EvidenceStatus.MET
        for item in complete.criteria
        if item.kind is RequirementKind.ELIGIBILITY
    )
    assert complete.eligibility_status.value == "clear"

    location_variants = JobSnapshot(
        ats="lever",
        job_id="location-variants",
        employer="Acme",
        title="Engineer",
        description=(
            "Required qualifications:\n"
            "- Must be based in New York, NY.\n"
            "- Must be located within 50 miles of Austin, TX."
        ),
    )
    location_graph = extract_requirement_graph(location_variants)
    assert len(location_graph.requirements) == 2
    assert all(
        item.kind is RequirementKind.ELIGIBILITY
        for item in location_graph.requirements
    )
    location_score = score_ats_proxy(
        "Location: New York, NY.\nLocation: Austin, TX.", location_graph
    )
    assert location_score.eligibility_status.value == "clear"

    clearance = next(
        item for item in eligibility if "clearance" in item.source_text
    )
    irrelevant = "Built secure distributed systems."
    try:
        score_ats_proxy(
            irrelevant,
            graph,
            semantic_overrides={
                clearance.requirement_id: {
                    "status": "met",
                    "confidence": 1.0,
                    "evidence": [
                        {
                            "text": irrelevant,
                            "start": 0,
                            "end": len(irrelevant),
                        }
                    ],
                }
            },
        )
    except ResumeLabError as exc:
        assert "cannot resolve an eligibility" in str(exc)
    else:
        raise AssertionError("semantic evidence cleared an eligibility gate")


def test_keyword_repetition_and_protected_attributes_do_not_change_score() -> None:
    graph = extract_requirement_graph(JOB)
    base = version().plain_text
    ordinary = score_ats_proxy(base, graph)
    stuffed = score_ats_proxy(base + "\n" + " Python AWS" * 500, graph)
    protected = score_ats_proxy(
        base + "\nGender: female\nRace: Asian\nDisability: yes", graph
    )
    assert stuffed.requirement_evidence == ordinary.requirement_evidence
    assert stuffed.search_visibility == ordinary.search_visibility
    assert stuffed.parsed_fit == ordinary.parsed_fit
    assert protected.parsed_fit == ordinary.parsed_fit


def test_protected_identity_requirements_never_enter_fit_or_gain_from_resume_fields() -> None:
    job = JobSnapshot(
        ats="ashby",
        job_id="protected-criterion",
        title="Engineer",
        employer="Acme",
        description="Required qualifications:\n- Asian candidate",
    )
    graph = extract_requirement_graph(job)
    assert graph.requirements == ()
    ordinary = score_ats_proxy("Python engineer", graph)
    labelled = score_ats_proxy("Python engineer\nRace: Asian", graph)
    assert ordinary.parsed_fit == labelled.parsed_fit == 0.0

    text = "Asian candidate"
    start = job.description.index(text)
    clause = {
        "text": text,
        "source_start": start,
        "source_end": start + len(text),
        "kind": "required",
    }
    try:
        requirement_graph_from_clauses(job, [clause], include_fallback=False)
    except ResumeLabError as exc:
        assert "non-screening boilerplate" in str(exc)
    else:
        raise AssertionError("a model reintroduced a protected identity requirement")


def test_non_fit_sections_remain_ignored_until_a_scoring_heading() -> None:
    description = (
        "Required Qualifications\n"
        "Python experience\n"
        "Compensation\n"
        "Kubernetes stipend: $200,000\n"
        "Benefits\n"
        "AWS certification reimbursement\n"
        "Preferred Qualifications\n"
        "PostgreSQL experience preferred"
    )
    job = JobSnapshot(
        ats="lever",
        job_id="ignored-sections",
        title="Engineer",
        employer="Acme",
        description=description,
    )
    graph = extract_requirement_graph(job)
    sources = "\n".join(item.source_text for item in graph.requirements)
    assert "Python experience" in sources
    assert "PostgreSQL experience" in sources
    assert "Kubernetes stipend" not in sources
    assert "AWS certification reimbursement" not in sources

    hidden = "AWS certification reimbursement"
    start = description.index(hidden)
    clause = {
        "text": hidden,
        "source_start": start,
        "source_end": start + len(hidden),
        "kind": "required",
    }
    try:
        requirement_graph_from_clauses(job, [clause], include_fallback=False)
    except ResumeLabError as exc:
        assert "non-screening boilerplate" in str(exc)
    else:
        raise AssertionError("a model clause escaped an ignored benefits section")


def test_non_fit_policy_preserves_legitimate_domain_language() -> None:
    descriptions = (
        (
            "Required Qualifications\n"
            "Experience debugging race conditions in distributed Python systems",
            "Python",
        ),
        (
            "Responsibilities\n"
            "Build accessible products for people with disabilities using React",
            "React",
        ),
        (
            "Required Qualifications\n"
            "Experience with gender identity data privacy systems in Python",
            "Python",
        ),
        (
            "About the company\nWe make software\n"
            "What You Bring\n5+ years of Python experience",
            "Python",
        ),
        (
            "About Us\nWe make software\n"
            "The Opportunity\nYou will build distributed systems",
            "distributed systems",
        ),
        (
            "Benefits\nGreat insurance\n"
            "Who You Are\nRequired experience with Kubernetes",
            "Kubernetes",
        ),
        (
            "Required Qualifications\n"
            "Experience building compensation systems with Python",
            "Python",
        ),
        (
            "Required Qualifications\n"
            "Experience building employee benefits platforms in Java",
            "Java",
        ),
        (
            "Required Qualifications\n"
            "Experience building reimbursement claims systems using SQL",
            "SQL",
        ),
        (
            "Required Qualifications\n"
            "Experience building pay transparency analytics with Python",
            "Python",
        ),
        (
            "Required Qualifications\n"
            "Experience building stock options pricing systems in C++",
            "C++",
        ),
        (
            "Required Qualifications\nSeeking strong Python candidates.",
            "Python",
        ),
        (
            "About Us\nWe make developer tools.\n"
            "As a platform engineer, you will build Python services.",
            "Python",
        ),
        (
            "About the company\nAcme builds software.\n"
            "You will design distributed systems in Go.",
            "distributed systems",
        ),
    )
    for index, (description, expected) in enumerate(descriptions):
        job = JobSnapshot(
            ats="ashby",
            job_id=f"legitimate-language-{index}",
            title="Engineer",
            employer="Acme",
            description=description,
        )
        graph = extract_requirement_graph(job)
        assert any(
            expected.casefold() in item.source_text.casefold()
            for item in graph.requirements
        ), description


def test_artifact_evaluation_is_cached_and_job_snapshot_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        base = create_standard(service)["version"]
        artifact = service.register_artifact(real_artifact(base))
        first = service.evaluate_artifact(artifact["artifact_id"], JOB)
        second = service.evaluate_artifact(artifact["artifact_id"], JOB)
        assert first["cached"] is False and second["cached"] is True
        assert first["cache_key"] == second["cache_key"]
        assert first["parsed_fit"] == first["screening_readiness"]
        changed = replace(JOB, description=JOB.description + "\nRequired: Rust.")
        try:
            service.evaluate_artifact(artifact["artifact_id"], changed)
        except ResumeConflictError:
            pass
        else:
            raise AssertionError(
                "artifact was evaluated against a changed job snapshot"
            )


def test_evaluation_persistence_binds_exact_artifact_text_and_job_graph() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        create_standard(service)
        artifact = service.register_artifact(synthetic_artifact())
        graph = extract_requirement_graph(JOB)
        evaluation = score_ats_proxy(str(artifact["parsed_text"]), graph)

        wrong_text = replace(evaluation, artifact_text_sha256="0" * 64)
        try:
            service.store.put_evaluation(artifact["artifact_id"], graph, wrong_text)
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a score for different text was linked to an artifact")

        other_job = replace(JOB, job_id="different-job")
        other_graph = extract_requirement_graph(other_job)
        other_evaluation = score_ats_proxy(str(artifact["parsed_text"]), other_graph)
        try:
            service.store.put_evaluation(
                artifact["artifact_id"], other_graph, other_evaluation
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a score for a different job was linked to an artifact")

        forged_graph = replace(graph, fingerprint="f" * 64)
        forged_evaluation = replace(
            evaluation, requirement_graph_fingerprint="f" * 64
        )
        try:
            service.store.put_evaluation(
                artifact["artifact_id"], forged_graph, forged_evaluation
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a forged requirement graph fingerprint was persisted")


def test_artifact_evaluation_and_selection_rows_are_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        base = create_standard(service)["version"]
        artifact = service.register_artifact(real_artifact(base))
        evaluation = service.evaluate_artifact(artifact["artifact_id"], JOB)
        selection = service.select_for_application(
            "application-1",
            artifact["artifact_id"],
            "immutable-selection",
            actor_kind="user",
        )
        with connect(path) as connection:
            attempts = (
                (
                    "UPDATE resume_artifacts SET parsed_text='changed' WHERE artifact_id=?",
                    artifact["artifact_id"],
                ),
                (
                    "DELETE FROM resume_evaluation_cache WHERE cache_key=?",
                    evaluation["cache_key"],
                ),
                (
                    "UPDATE application_resume_selections SET application_id='changed' "
                    "WHERE selection_id=?",
                    selection["selection_id"],
                ),
            )
            for sql, identifier in attempts:
                try:
                    connection.execute(sql, (identifier,))
                except sqlite3.IntegrityError as exc:
                    assert "immutable" in str(exc) or "append-only" in str(exc)
                else:
                    raise AssertionError("immutable resume-lab history was changed")


def test_run_creation_is_idempotent_and_snapshots_exactly_four_items() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        lower = create_standard(service, "Systems", 2, "systems")
        highest = create_standard(service, "ML", 1, "ml")
        run = service.create_run("application-1", JOB, "run-request-1")
        replay = service.create_run("application-1", JOB, "run-request-1")
        assert replay["run_id"] == run["run_id"]
        assert run["status"] == "queued"
        assert run["application_id"] == "application-1"
        assert run["job_snapshot"]["description"] == JOB.description
        assert run["selected_standard_id"] == highest["standard"]["standard_id"]
        assert run["base_version_id"] == highest["version"]["version_id"]
        assert [item["variant_kind"] for item in run["items"]] == [
            "grounded_rewrite",
            "standard_exaggerated",
            "market_ideal",
            "keyword_adversarial",
        ]
        assert [item["purpose"] for item in run["items"]] == [
            "real_application",
            "synthetic_research",
            "synthetic_research",
            "synthetic_research",
        ]
        manual = service.create_run(
            "application-2",
            JOB,
            "run-request-2",
            standard_id=lower["standard"]["standard_id"],
        )
        assert manual["selected_standard_id"] == lower["standard"]["standard_id"]
        changed = replace(JOB, description=JOB.description + "\nRequired: Rust.")
        try:
            service.create_run("application-1", changed, "run-request-1")
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("run idempotency key accepted changed input")


def test_run_requirement_analysis_must_reconstruct_exactly_from_its_clauses() -> None:
    required_text = (
        "At least 3+ years of experience building Python services with AWS or GCP."
    )
    source_start = JOB.description.index(required_text)
    clauses = (
        {
            "source_span": {
                "start": source_start,
                "end": source_start + len(required_text),
                "text": required_text,
            },
            "kind": "required",
        },
    )
    graph = requirement_graph_from_clauses(JOB, clauses, include_fallback=True)

    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        create_standard(service)
        run = service.create_run(
            "application-1",
            JOB,
            "model-analysis",
            requirement_graph=graph,
            requirement_clauses=clauses,
            requirement_extraction="local_model_plus_fallback",
        )
        assert run["requirement_analysis"]["requirement_graph_fingerprint"] == (
            graph.fingerprint
        )
        assert run["requirement_analysis"]["clauses"] == list(clauses)

        # A claimed valid fingerprint cannot conceal a different graph body.
        forged = replace(graph, requirements=())
        try:
            service.create_run(
                "application-2",
                JOB,
                "forged-analysis",
                requirement_graph=forged,
                requirement_clauses=clauses,
                requirement_extraction="local_model_plus_fallback",
            )
        except ResumeConflictError as exc:
            assert "exactly reconstruct" in str(exc)
        else:
            raise AssertionError("a forged requirement graph body was persisted")

        changed_clauses = (
            {
                **clauses[0],
                "kind": "preferred",
            },
        )
        try:
            service.create_run(
                "application-3",
                JOB,
                "mismatched-analysis",
                requirement_graph=graph,
                requirement_clauses=changed_clauses,
                requirement_extraction="local_model_plus_fallback",
            )
        except ResumeConflictError as exc:
            assert "exactly reconstruct" in str(exc)
        else:
            raise AssertionError("mismatched graph and clauses were persisted")


def test_run_requirement_analysis_reconstructs_clause_only_and_empty_fallback() -> None:
    required_text = (
        "At least 3+ years of experience building Python services with AWS or GCP."
    )
    source_start = JOB.description.index(required_text)
    clauses = (
        {
            "start": source_start,
            "end": source_start + len(required_text),
            "text": required_text,
            "kind": "required",
        },
    )
    expected = requirement_graph_from_clauses(JOB, clauses, include_fallback=True)

    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        create_standard(service)
        clause_run = service.create_run(
            "application-1",
            JOB,
            "clause-only-analysis",
            requirement_clauses=clauses,
            requirement_extraction="local_model_plus_fallback",
        )
        assert clause_run["requirement_analysis"][
            "requirement_graph_fingerprint"
        ] == expected.fingerprint

        fallback = extract_requirement_graph(JOB)
        fallback_run = service.create_run(
            "application-2",
            JOB,
            "empty-fallback-analysis",
            requirement_graph=fallback,
            requirement_clauses=(),
        )
        assert fallback_run["requirement_analysis"][
            "requirement_graph_fingerprint"
        ] == fallback.fingerprint
        assert fallback_run["requirement_analysis"]["clauses"] == []


def test_run_lifecycle_retries_into_an_append_only_successor() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        base = create_standard(service)["version"]
        run = service.create_run("application-1", JOB, "run-lifecycle")
        started = service.start_run(run["run_id"])
        replay = service.start_run(run["run_id"])
        assert started["status"] == "running" and replay["attempt"] == 1

        grounded = service.register_artifact(
            real_artifact(base, variant=VariantKind.GROUNDED_REWRITE)
        )
        service.complete_run_item(
            run["run_id"],
            VariantKind.GROUNDED_REWRITE,
            "succeeded",
            artifact_id=grounded["artifact_id"],
        )
        failed = service.complete_run_item(
            run["run_id"],
            VariantKind.STANDARD_EXAGGERATED,
            "failed",
            error="model failed at /private/path",
        )
        assert "/" not in failed["error"]
        failed_run = service.complete_run(
            run["run_id"], "failed", error="one comparison failed"
        )
        assert failed_run["status"] == "failed"
        retried = service.retry_run(
            run["run_id"], "retry-run-lifecycle", actor_kind="user"
        )
        assert retried["run_id"] != run["run_id"]
        statuses = {item["variant_kind"]: item["status"] for item in retried["items"]}
        assert set(statuses.values()) == {"pending"}
        replayed_retry = service.retry_run(
            run["run_id"], "retry-run-lifecycle", actor_kind="user"
        )
        assert replayed_retry["run_id"] == retried["run_id"]
        assert replayed_retry["retry_replayed"] is True
        original = service.get_run(run["run_id"])
        assert original["status"] == "failed"
        assert {
            item["variant_kind"]: item["status"] for item in original["items"]
        }["grounded_rewrite"] == "succeeded"
        service.start_run(retried["run_id"])

        artifacts = {
            VariantKind.GROUNDED_REWRITE: real_artifact(
                base, variant=VariantKind.GROUNDED_REWRITE
            ),
            VariantKind.STANDARD_EXAGGERATED: synthetic_artifact(
                variant=VariantKind.STANDARD_EXAGGERATED,
                base_version_id=base["version_id"],
                path="research/study-1/exaggerated-run.pdf",
                run_id=retried["run_id"],
            ),
            VariantKind.MARKET_IDEAL: synthetic_artifact(
                variant=VariantKind.MARKET_IDEAL,
                path="research/study-1/market-run.pdf",
                run_id=retried["run_id"],
            ),
            VariantKind.KEYWORD_ADVERSARIAL: synthetic_artifact(
                variant=VariantKind.KEYWORD_ADVERSARIAL,
                path="research/study-1/adversarial-run.pdf",
                run_id=retried["run_id"],
            ),
        }
        for kind, artifact_input in artifacts.items():
            artifact = service.register_artifact(artifact_input)
            service.complete_run_item(
                retried["run_id"],
                kind,
                "succeeded",
                artifact_id=artifact["artifact_id"],
            )
        completed = service.complete_run(retried["run_id"], "succeeded")
        assert completed["status"] == "succeeded"
        assert completed["attempt"] == 1
        assert completed["result_fingerprint"]
        assert all(item["status"] == "succeeded" for item in completed["items"])
        listed = service.list_runs(("succeeded",))
        assert [item["run_id"] for item in listed] == [retried["run_id"]]
        assert service.get_run(run["run_id"])["status"] == "failed"
        try:
            service.retry_run(
                retried["run_id"], "retry-succeeded-run", actor_kind="user"
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a succeeded resume run was retried")


def test_ambiguous_runpod_job_requires_an_audited_retry_acknowledgment() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        create_standard(service)
        run = service.create_run("application-1", JOB, "ambiguous-runpod-job")
        service.start_run(run["run_id"])
        error_code = "runpod_reconciliation_required:job-accepted"
        service.complete_run_item(
            run["run_id"],
            VariantKind.GROUNDED_REWRITE,
            "failed",
            error=error_code,
        )
        service.complete_run(run["run_id"], "failed", error=error_code)

        try:
            service.retry_run(
                run["run_id"],
                "retry-without-reconciliation",
                actor_kind="user",
            )
        except ResumeConflictError as exc:
            assert "inspect the accepted Runpod job" in str(exc)
        else:
            raise AssertionError("an ambiguous Runpod job was blindly retried")

        retried = service.retry_run(
            run["run_id"],
            "retry-after-reconciliation",
            actor_kind="user",
            reconciliation_acknowledged=True,
        )
        assert retried["run_id"] != run["run_id"]
        command = service.get_retry_command("retry-after-reconciliation")
        assert command is not None
        assert command["reconciliation_acknowledged"] == 1
        replay = service.retry_run(
            run["run_id"],
            "retry-after-reconciliation",
            actor_kind="user",
            reconciliation_acknowledged=True,
        )
        assert replay["run_id"] == retried["run_id"]
        try:
            service.retry_run(
                run["run_id"],
                "retry-after-reconciliation",
                actor_kind="user",
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("a retry acknowledgment changed during replay")


def test_recovered_run_owner_fences_a_stale_worker_attempt() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        create_standard(service)
        run = service.create_run("application-1", JOB, "owned-run")
        first = service.start_run(run["run_id"], owner_token="owner-first")
        recovered = service.start_run(run["run_id"], owner_token="owner-recovered")
        assert recovered["attempt"] == first["attempt"] + 1
        try:
            service.complete_run_item(
                run["run_id"],
                VariantKind.GROUNDED_REWRITE,
                "failed",
                error="stale",
                run_attempt=first["attempt"],
                owner_token="owner-first",
            )
        except ResumeConflictError as exc:
            assert "ownership was lost" in str(exc)
        else:
            raise AssertionError("a stale worker mutated a recovered resume run")
        service.complete_run_item(
            run["run_id"],
            VariantKind.GROUNDED_REWRITE,
            "failed",
            error="current",
            run_attempt=recovered["attempt"],
            owner_token="owner-recovered",
        )


def test_run_item_rejects_wrong_artifact_and_early_success() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        create_standard(service)
        run = service.create_run("application-1", JOB, "run-boundary")
        service.start_run(run["run_id"])
        synthetic = service.register_artifact(synthetic_artifact())
        try:
            service.complete_run_item(
                run["run_id"],
                VariantKind.GROUNDED_REWRITE,
                "succeeded",
                artifact_id=synthetic["artifact_id"],
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("synthetic artifact completed the grounded run item")
        try:
            service.complete_run(run["run_id"], "succeeded")
        except ResumeConflictError:
            pass
        else:
            raise AssertionError("run succeeded before all four items")


def test_synthetic_run_item_rejects_cross_run_study_lineage() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, service = make_service(directory)
        create_standard(service)
        first = service.create_run("application-1", JOB, "lineage-first")
        second = service.create_run("application-2", JOB, "lineage-second")
        service.start_run(first["run_id"])
        service.start_run(second["run_id"])
        artifact = service.register_artifact(
            synthetic_artifact(
                variant=VariantKind.MARKET_IDEAL,
                run_id=first["run_id"],
            )
        )
        try:
            service.complete_run_item(
                second["run_id"],
                VariantKind.MARKET_IDEAL,
                "succeeded",
                artifact_id=artifact["artifact_id"],
            )
        except ResumeBoundaryError as exc:
            assert "study lineage" in str(exc)
        else:
            raise AssertionError("a synthetic artifact crossed resume runs")


def test_run_lineage_and_item_cardinality_are_database_enforced() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        create_standard(service)
        run = service.create_run("application-1", JOB, "run-immutable")
        with connect(path) as connection:
            try:
                connection.execute(
                    "UPDATE resume_runs SET job_id='other' WHERE run_id=?",
                    (run["run_id"],),
                )
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc)
            else:
                raise AssertionError("resume run lineage was changed")
            try:
                connection.execute(
                    "UPDATE resume_run_analyses SET clauses_json='[]' WHERE run_id=?",
                    (run["run_id"],),
                )
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc)
            else:
                raise AssertionError("resume run requirement analysis was changed")
            first = run["items"][0]
            try:
                connection.execute(
                    "UPDATE resume_run_items SET purpose='synthetic_research' "
                    "WHERE run_item_id=?",
                    (first["run_item_id"],),
                )
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc)
            else:
                raise AssertionError("run item crossed its purpose boundary")
            try:
                connection.execute(
                    "INSERT INTO resume_run_items "
                    "(run_item_id,run_id,variant_kind,purpose,base_version_id,input_fingerprint,"
                    "status,error,attempts) VALUES "
                    "('item_extra',?,'market_ideal','synthetic_research',NULL,?,'pending','',0)",
                    (run["run_id"], "f" * 64),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("a fifth comparison item was inserted")


def test_terminal_run_items_are_immutable_and_revalidated_on_read() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        base = create_standard(service)["version"]
        completed, artifacts = complete_successful_run(
            service, base, idempotency_key="terminal-run-integrity"
        )
        grounded_item = next(
            item
            for item in completed["items"]
            if item["variant_kind"] == VariantKind.GROUNDED_REWRITE.value
        )
        market = artifacts[VariantKind.MARKET_IDEAL]
        with connect(path) as connection:
            for statement in (
                "UPDATE resume_run_items SET artifact_id=? WHERE run_item_id=?",
                "UPDATE resume_runs SET result_fingerprint=? WHERE run_id=?",
            ):
                try:
                    connection.execute(
                        statement,
                        (
                            market["artifact_id"]
                            if "artifact_id" in statement
                            else "f" * 64,
                            grounded_item["run_item_id"]
                            if "run_items" in statement
                            else completed["run_id"],
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    assert "immutable" in str(exc)
                else:
                    raise AssertionError("terminal resume history was changed")

        # Defense in depth: even if a future migration accidentally removed the
        # terminal triggers, read-side lineage validation must not trust the row.
        with connect(path) as connection:
            connection.execute("DROP TRIGGER resume_run_items_succeeded_are_immutable")
            connection.execute("DROP TRIGGER resume_run_items_terminal_are_immutable")
            connection.execute(
                "DROP TRIGGER resume_run_items_parent_terminal_are_immutable"
            )
            connection.execute(
                "UPDATE resume_run_items SET artifact_id=?,output_fingerprint=? "
                "WHERE run_item_id=?",
                (
                    market["artifact_id"],
                    market["content_sha256"],
                    grounded_item["run_item_id"],
                ),
            )
        try:
            service.get_run(str(completed["run_id"]))
        except ResumeConflictError as exc:
            assert "artifact binding" in str(exc)
        else:
            raise AssertionError("tampered terminal run item passed read validation")


def test_pending_items_freeze_when_their_parent_run_becomes_terminal() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        create_standard(service)
        run = service.create_run("application-1", JOB, "failed-queued-parent")
        failed = service.fail_queued_run(
            str(run["run_id"]), error="queue_enqueue_failed"
        )
        pending = failed["items"][0]
        try:
            with connect(path) as connection:
                connection.execute(
                    "UPDATE resume_run_items SET status='failed',error='tampered',"
                    "attempts=99,completed_at='2026-09-02T12:00:00Z' "
                    "WHERE run_item_id=?",
                    (pending["run_item_id"],),
                )
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("a pending item changed under a terminal parent run")
        assert service.get_run(str(run["run_id"]))["items"][0] == pending


def test_standard_import_provenance_is_optional_validated_and_immutable() -> None:
    metadata = {
        "managed_relative_path": "real/standards/imported-primary.pdf",
        "pdf_sha256": "d" * 64,
        "parsed_text": "Imported resume text.",
        "parse_fidelity": 0.99,
        "compiler_revision": "latexmk-v1",
    }
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        created = service.create_standard(
            "Imported",
            1,
            replace(version(), import_metadata=metadata),
            actor_kind="user",
        )
        saved = created["version"]
        assert saved["import_metadata"] == metadata
        replay = service.add_standard_version(
            created["standard"]["standard_id"],
            replace(version(), import_metadata=metadata),
            actor_kind="user",
        )
        assert replay["version_id"] == saved["version_id"]
        with connect(path) as connection:
            try:
                connection.execute(
                    "UPDATE resume_standard_versions SET import_metadata_json='{}' "
                    "WHERE version_id=?",
                    (saved["version_id"],),
                )
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc)
            else:
                raise AssertionError("compiled import provenance was changed")
    for bad in (
        {**metadata, "managed_relative_path": "../outside.pdf"},
        {**metadata, "pdf_sha256": "short"},
        {**metadata, "parse_fidelity": 2.0},
    ):
        try:
            replace(version(), import_metadata=bad).validate()
        except ResumeLabError:
            pass
        else:
            raise AssertionError("invalid standard import provenance was accepted")


def test_model_clause_converter_uses_exact_spans_and_local_term_derivation() -> None:
    required_text = (
        "At least 3+ years of experience building Python services with AWS or GCP."
    )
    eligibility_text = "Must be authorized to work in the United States."
    required_start = JOB.description.index(required_text)
    eligibility_start = JOB.description.index(eligibility_text)
    clauses = (
        {
            "source_span": {
                "start": required_start,
                "end": required_start + len(required_text),
                "text": required_text,
            },
            "kind": "required",
        },
        {
            "start": eligibility_start,
            "end": eligibility_start + len(eligibility_text),
            "text": eligibility_text,
            "kind": "eligibility",
        },
    )
    graph = requirement_graph_from_clauses(JOB, clauses, include_fallback=False)
    assert len(graph.requirements) == 2
    required, eligibility = graph.requirements
    assert required.priority is RequirementPriority.REQUIRED
    assert required.kind is RequirementKind.EXPERIENCE
    assert required.minimum_years == 3.0
    assert any("aws" in group and "gcp" in group for group in required.term_groups)
    assert required.source_start == required_start
    assert required.extraction_origin == "model_clause"
    assert eligibility.kind is RequirementKind.ELIGIBILITY
    assert eligibility.priority is RequirementPriority.REQUIRED
    assert graph == requirement_graph_from_clauses(JOB, clauses, include_fallback=False)

    merged = merge_requirement_clauses(JOB, clauses)
    assert len(merged.requirements) > len(graph.requirements)
    assert any("Kubernetes" in item.source_text for item in merged.requirements)
    assert requirement_graph_from_clauses(JOB, ()) == extract_requirement_graph(JOB)

    hallucinated = ({**clauses[0], "text": "Invented requirement"},)
    try:
        requirement_graph_from_clauses(JOB, hallucinated)
    except ResumeLabError:
        pass
    else:
        raise AssertionError(
            "model-authored requirement text escaped source validation"
        )

    nested_text = "Python services"
    nested_start = JOB.description.index(nested_text)
    overlapping = (
        clauses[0],
        {
            "start": nested_start,
            "end": nested_start + len(nested_text),
            "text": nested_text,
            "kind": "required",
        },
    )
    try:
        requirement_graph_from_clauses(JOB, overlapping, include_fallback=False)
    except ResumeLabError as exc:
        assert "must not overlap" in str(exc)
    else:
        raise AssertionError("overlapping model clauses were double-counted")

    combined = "Required: 5+ years of Python and Kubernetes."
    nested_job = JobSnapshot(
        ats="lever",
        job_id="nested-model-fallback",
        title="Platform Engineer",
        description=combined,
    )
    nested_source = "5+ years of Python"
    nested_start = combined.index(nested_source)
    merged_nested = requirement_graph_from_clauses(
        nested_job,
        (
            {
                "start": nested_start,
                "end": nested_start + len(nested_source),
                "text": nested_source,
                "kind": "required",
            },
        ),
        include_fallback=True,
    )
    assert len(merged_nested.requirements) == 1
    assert "Kubernetes" in merged_nested.requirements[0].source_text


def test_semantic_overrides_only_resolve_unmatched_criteria_and_recompute_fit() -> None:
    graph = extract_requirement_graph(JOB)
    kubernetes = next(
        item for item in graph.requirements if "Kubernetes" in item.source_text
    )
    resume = "Ran workloads on a resilient production cluster manager."
    lexical = score_ats_proxy(resume, graph)
    before = next(
        item
        for item in lexical.criteria
        if item.requirement_id == kubernetes.requirement_id
    )
    assert before.status is EvidenceStatus.NOT_EVIDENCED
    evidence = {"text": resume, "start": 0, "end": len(resume)}
    semantic = score_ats_proxy(
        resume,
        graph,
        semantic_overrides={
            kubernetes.requirement_id: {
                "status": "met",
                "evidence": [evidence],
                "confidence": 0.91,
            }
        },
    )
    after = next(
        item
        for item in semantic.criteria
        if item.requirement_id == kubernetes.requirement_id
    )
    assert after.status is EvidenceStatus.MET
    assert after.match_method == "semantic" and after.confidence == 0.91
    assert after.evidence[0].text == resume
    assert semantic.requirement_evidence > lexical.requirement_evidence
    assert semantic.parsed_fit > lexical.parsed_fit
    assert semantic.search_visibility == lexical.search_visibility
    assert semantic.cache_key != lexical.cache_key
    assert semantic == score_ats_proxy(
        resume,
        graph,
        semantic_overrides={
            kubernetes.requirement_id: {
                "status": "met",
                "evidence": [evidence],
                "confidence": 0.91,
            }
        },
    )


def test_literal_and_numeric_results_take_precedence_over_semantic_overrides() -> None:
    graph = extract_requirement_graph(JOB)
    python = next(item for item in graph.requirements if "Python" in item.source_text)
    literal_resume = "5 years building Python services with AWS."
    exact = score_ats_proxy(
        literal_resume,
        graph,
        semantic_overrides={
            python.requirement_id: {
                "status": "contradicted",
                "evidence": [
                    {"text": literal_resume, "start": 0, "end": len(literal_resume)}
                ],
                "confidence": 1.0,
            }
        },
    )
    criterion = next(
        item for item in exact.criteria if item.requirement_id == python.requirement_id
    )
    assert criterion.status is EvidenceStatus.MET
    assert criterion.match_method == "literal"

    semantic_resume = "Built related cloud backends for 2 years."
    numeric = score_ats_proxy(
        semantic_resume,
        graph,
        semantic_overrides={
            python.requirement_id: {
                "status": "met",
                "evidence": [
                    {
                        "text": semantic_resume,
                        "start": 0,
                        "end": len(semantic_resume),
                    }
                ],
                "confidence": 0.95,
            }
        },
    )
    criterion = next(
        item
        for item in numeric.criteria
        if item.requirement_id == python.requirement_id
    )
    assert criterion.status is EvidenceStatus.PARTIAL
    assert criterion.match_method == "semantic"

    try:
        score_ats_proxy(
            semantic_resume,
            graph,
            semantic_overrides={
                python.requirement_id: {
                    "status": "met",
                    "evidence": [{"text": "wrong", "start": 0, "end": 5}],
                    "confidence": 0.9,
                }
            },
        )
    except ResumeLabError:
        pass
    else:
        raise AssertionError("semantic evidence not present in the resume was accepted")


def test_grounded_approval_binds_exact_succeeded_run_before_selection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, service = make_service(directory)
        base = create_standard(service)["version"]
        run = service.create_run("application-1", JOB, "approval-run")
        service.start_run(run["run_id"])
        grounded = service.register_artifact(
            real_artifact(base, variant=VariantKind.GROUNDED_REWRITE)
        )
        service.complete_run_item(
            run["run_id"],
            VariantKind.GROUNDED_REWRITE,
            "succeeded",
            artifact_id=grounded["artifact_id"],
        )
        try:
            service.approve_run(
                run["run_id"],
                grounded["artifact_id"],
                "too-early",
                actor_kind="user",
            )
        except ResumeConflictError:
            pass
        else:
            raise AssertionError(
                "approval occurred before the comparison run succeeded"
            )

        remaining = {
            VariantKind.STANDARD_EXAGGERATED: synthetic_artifact(
                variant=VariantKind.STANDARD_EXAGGERATED,
                base_version_id=base["version_id"],
                run_id=run["run_id"],
            ),
            VariantKind.MARKET_IDEAL: synthetic_artifact(
                variant=VariantKind.MARKET_IDEAL,
                run_id=run["run_id"],
            ),
            VariantKind.KEYWORD_ADVERSARIAL: synthetic_artifact(
                variant=VariantKind.KEYWORD_ADVERSARIAL,
                run_id=run["run_id"],
            ),
        }
        synthetic = None
        for kind, artifact_input in remaining.items():
            artifact = service.register_artifact(artifact_input)
            synthetic = artifact
            service.complete_run_item(
                run["run_id"], kind, "succeeded", artifact_id=artifact["artifact_id"]
            )
        service.complete_run(run["run_id"], "succeeded")
        try:
            service.select_for_application(
                "application-1",
                grounded["artifact_id"],
                "before-approval",
                actor_kind="user",
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("unapproved grounded artifact was selectable")
        with connect(path) as connection:
            try:
                connection.execute(
                    "INSERT INTO application_resume_selections "
                    "(selection_id,application_id,artifact_id,idempotency_key,selected_by,selected_at) "
                    "VALUES ('sel_unapproved','application-1',?,'direct-unapproved','user',"
                    "'2026-01-01T00:00:00Z')",
                    (grounded["artifact_id"],),
                )
            except sqlite3.IntegrityError as exc:
                assert "exact run approval" in str(exc)
            else:
                raise AssertionError("SQLite accepted an unapproved grounded artifact")
        try:
            service.approve_run(
                run["run_id"],
                grounded["artifact_id"],
                "model-approval",
                actor_kind="model",
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("non-user actor approved a grounded rewrite")
        try:
            service.approve_run(
                run["run_id"],
                synthetic["artifact_id"],
                "synthetic-approval",
                actor_kind="user",
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("synthetic artifact was approved for application")
        with connect(path) as connection:
            try:
                connection.execute(
                    "INSERT INTO resume_artifact_approvals "
                    "(approval_id,run_id,artifact_id,pdf_sha256,content_sha256,"
                    "idempotency_key,approved_by,approved_at) VALUES "
                    "('approval_direct',?,?,?,?,?,'user','2026-01-01T00:00:00Z')",
                    (
                        run["run_id"],
                        synthetic["artifact_id"],
                        synthetic["pdf_sha256"],
                        synthetic["content_sha256"],
                        "direct-synthetic-approval",
                    ),
                )
            except sqlite3.IntegrityError as exc:
                assert "succeeded run grounded artifact" in str(exc)
            else:
                raise AssertionError("SQLite accepted a synthetic approval")

        approved = service.approve_run(
            run["run_id"],
            grounded["artifact_id"],
            "user-approval",
            actor_kind="user",
        )
        replay = service.approve_run(
            run["run_id"],
            grounded["artifact_id"],
            "user-approval",
            actor_kind="user",
        )
        assert replay == approved
        assert approved["pdf_sha256"] == grounded["pdf_sha256"]
        assert approved["content_sha256"] == grounded["content_sha256"]
        assert approved["approved_by"] == "user"
        assert service.get_run(run["run_id"])["grounded_approval"] == approved
        selected = service.select_for_application(
            "application-1",
            grounded["artifact_id"],
            "after-approval",
            actor_kind="user",
        )
        assert selected["artifact_id"] == grounded["artifact_id"]
        try:
            service.select_for_application(
                "application-2",
                grounded["artifact_id"],
                "wrong-application",
                actor_kind="user",
            )
        except ResumeBoundaryError:
            pass
        else:
            raise AssertionError("approval escaped its application/run boundary")
        with connect(path) as connection:
            try:
                connection.execute(
                    "UPDATE resume_artifact_approvals SET pdf_sha256=? WHERE approval_id=?",
                    ("e" * 64, approved["approval_id"]),
                )
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc)
            else:
                raise AssertionError("approval hash snapshot was changed")


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume-lab core tests)")


if __name__ == "__main__":
    main()
