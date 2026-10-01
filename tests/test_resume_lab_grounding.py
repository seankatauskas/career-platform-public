#!/usr/bin/env python3
"""Focused offline checks for the real-artifact grounding boundary."""

from __future__ import annotations

from copy import deepcopy

from job_search.resume_lab.contracts import ClaimOrigin, ResumeClaim
from job_search.resume_lab.grounding import (
    GroundingValidationError,
    curated_source_claims,
    validate_generated_wording_claim,
    validate_grounded_artifact_boundary,
)
from job_search.resume_lab.tex import render_resume_tex


def expect_error(call, message: str) -> None:
    try:
        call()
    except GroundingValidationError as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError("expected GroundingValidationError")


def base_content():
    return {
        "identity": {"name": "Ada Example", "contact_line": "Chicago, IL"},
        "summary": "Backend engineer",
        "experience": [
            {
                "company": "Example Corp",
                "role": "Software Engineer",
                "location": "Remote",
                "dates": "2022 - Present",
                "bullets": [
                    "Built Kubernetes services and cut latency 25%"
                ],
            }
        ],
        "projects": [],
        "education": [],
        "skills": [],
    }


def source_claims():
    return [
        {
            "claim_id": "source-summary",
            "path": "/summary",
            "text": "Backend engineer",
            # This untrusted/job-derived alias must always be ignored.
            "allowed_equivalent_terms": ["java"],
        },
        {
            "claim_id": "source-services",
            "path": "/experience/0/bullets/0",
            "text": "Built Kubernetes services and cut latency 25%",
            "allowed_equivalent_terms": ["global"],
        },
    ]


def grounded_bundle():
    content = base_content()
    content["summary"] = "Backend engineer."
    content["experience"][0]["bullets"][0] = (
        "Built k8s services and cut latency 25%."
    )
    output = {
        "variant_kind": "grounded_rewrite",
        "base_standard_id": "standard-1",
        "synthetic": False,
        "research_only": False,
        "content": content,
        "claims": [
            {
                "claim_id": "rewrite-summary",
                "path": "/summary",
                "text": content["summary"],
                "origin": "source_rewrite",
                "source_claim_ids": ["source-summary"],
                "added_equivalent_terms": [],
            },
            {
                "claim_id": "rewrite-services",
                "path": "/experience/0/bullets/0",
                "text": content["experience"][0]["bullets"][0],
                "origin": "source_rewrite",
                "source_claim_ids": ["source-services"],
                "added_equivalent_terms": ["k8s"],
            },
        ],
    }
    persisted = (
        ResumeClaim(
            "rewrite-summary",
            content["summary"],
            ClaimOrigin.GENERATED_WORDING,
            ("source-summary",),
        ),
        ResumeClaim(
            "rewrite-services",
            content["experience"][0]["bullets"][0],
            ClaimOrigin.GENERATED_WORDING,
            ("source-services",),
        ),
    )
    return output, persisted


def validate(output=None, persisted=None, intended_text=None):
    default_output, default_persisted = grounded_bundle()
    output = output or default_output
    persisted = persisted or default_persisted
    intended_text = (
        render_resume_tex(output["content"]).intended_text
        if intended_text is None
        else intended_text
    )
    return validate_grounded_artifact_boundary(
        output,
        {"standard_id": "standard-1", "content": base_content()},
        source_claims(),
        persisted,
        intended_text,
    )


def test_curated_aliases_ignore_input_and_derive_only_from_fact_text():
    curated = curated_source_claims(source_claims())
    assert curated[0]["allowed_equivalent_terms"] == []
    assert "java" not in curated[0]["allowed_equivalent_terms"]
    assert "global" not in curated[1]["allowed_equivalent_terms"]
    aliases = set(curated[1]["allowed_equivalent_terms"])
    assert {"k8s", "kubernetes", "build", "built"} <= aliases
    assert "developed" not in aliases


def test_complete_grounded_bundle_binds_structure_claims_and_exact_render():
    result = validate()
    assert result.output["content"]["summary"] == "Backend engineer."
    assert result.rendered.intended_text == render_resume_tex(
        result.output["content"]
    ).intended_text
    assert len(result.claims) == 2
    assert len(result.intended_text_sha256) == 64


def test_job_derived_alias_cannot_authorize_a_new_skill():
    output, persisted = grounded_bundle()
    output["content"]["summary"] = "Java engineer"
    output["claims"][0]["text"] = "Java engineer"
    output["claims"][0]["added_equivalent_terms"] = ["java"]
    persisted = (
        ResumeClaim(
            "rewrite-summary",
            "Java engineer",
            ClaimOrigin.GENERATED_WORDING,
            ("source-summary",),
        ),
        persisted[1],
    )
    expect_error(lambda: validate(output, persisted), "allowed equivalent")


def test_claim_validator_rejects_new_metric_seniority_scope_and_lexical_tokens():
    source = [
        {
            "claim_id": "fact",
            "path": "/summary",
            "text": "Built Python APIs and cut latency 25%",
        }
    ]
    cases = (
        ("Built Python APIs and cut latency 50%", "metric"),
        ("Senior built Python APIs and cut latency 25%", "seniority"),
        ("Built production Python APIs and cut latency 25%", "scope"),
        ("Built scalable Python APIs and cut latency 25%", "lexical tokens"),
    )
    for text, message in cases:
        claim = ResumeClaim(
            "rewrite",
            text,
            ClaimOrigin.GENERATED_WORDING,
            ("fact",),
        )
        expect_error(
            lambda claim=claim: validate_generated_wording_claim(claim, source),
            message,
        )


def test_equivalence_is_a_replacement_not_keyword_duplication():
    claim = ResumeClaim(
        "rewrite",
        "Built Kubernetes k8s services",
        ClaimOrigin.GENERATED_WORDING,
        ("fact",),
    )
    expect_error(
        lambda: validate_generated_wording_claim(
            claim,
            [{"claim_id": "fact", "text": "Built Kubernetes services"}],
        ),
        "lexical tokens",
    )


def test_stronger_action_verbs_are_not_treated_as_factual_equivalents() -> None:
    for source_text, rewrite in (
        ("Streamlined manual onboarding", "Automated manual onboarding"),
        ("Improved algorithm performance", "Optimized algorithm performance"),
        ("Collaborated with sales", "Partnered with sales"),
        ("Analyzed model quality", "Evaluated model quality"),
        ("Built a service", "Implemented a service"),
    ):
        expect_error(
            lambda source_text=source_text, rewrite=rewrite: (
                validate_generated_wording_claim(
                    ResumeClaim(
                        "rewrite",
                        rewrite,
                        ClaimOrigin.GENERATED_WORDING,
                        ("fact",),
                    ),
                    [{"claim_id": "fact", "text": source_text}],
                )
            ),
            "lexical tokens",
        )
def test_boundary_rejects_persisted_claim_provenance_or_origin_drift():
    output, persisted = grounded_bundle()
    wrong_source = (
        persisted[0],
        ResumeClaim(
            "rewrite-services",
            persisted[1].text,
            ClaimOrigin.GENERATED_WORDING,
            ("source-services", "source-summary"),
        ),
    )
    expect_error(
        lambda: validate(output, wrong_source),
        "differ from generated provenance",
    )

    wrong_origin = (
        ResumeClaim(
            persisted[0].claim_id,
            persisted[0].text,
            ClaimOrigin.USER_ATTESTED,
            persisted[0].source_fact_ids,
        ),
        persisted[1],
    )
    expect_error(
        lambda: validate(output, wrong_origin),
        "generated-wording origin",
    )


def test_boundary_rejects_render_or_fixed_base_shape_drift():
    expect_error(
        lambda: validate(intended_text="injected artifact text"),
        "exactly match",
    )

    output, persisted = grounded_bundle()
    changed = deepcopy(output)
    changed["content"]["experience"][0]["role"] = "Principal Engineer"
    expect_error(
        lambda: validate(changed, persisted),
        "fixed identity",
    )


def test_grounded_claims_cannot_move_merge_or_reuse_source_slots():
    output, persisted = grounded_bundle()
    moved = deepcopy(output)
    moved["claims"][0]["source_claim_ids"] = ["source-services"]
    moved_persisted = (
        ResumeClaim(
            persisted[0].claim_id,
            persisted[0].text,
            ClaimOrigin.GENERATED_WORDING,
            ("source-services",),
        ),
        persisted[1],
    )
    expect_error(lambda: validate(moved, moved_persisted), "source path")

    merged = deepcopy(output)
    merged["claims"][0]["source_claim_ids"] = [
        "source-summary",
        "source-services",
    ]
    expect_error(lambda: validate(merged, persisted), "supporting sources")

    reused = deepcopy(output)
    reused["claims"][1]["source_claim_ids"] = ["source-summary"]
    reused["claims"][1]["text"] = "Backend engineer."
    reused["content"]["experience"][0]["bullets"][0] = "Backend engineer."
    reused_persisted = (
        persisted[0],
        ResumeClaim(
            persisted[1].claim_id,
            "Backend engineer.",
            ClaimOrigin.GENERATED_WORDING,
            ("source-summary",),
        ),
    )
    expect_error(lambda: validate(reused, reused_persisted), "used by only one")


def test_grounded_claim_cannot_invert_truth_by_deletion_or_reordering():
    source = [
        {
            "claim_id": "fact",
            "path": "/summary",
            "text": "Never led production migrations",
        }
    ]
    for text in ("Led production migrations", "Production migrations never led"):
        expect_error(
            lambda text=text: validate_generated_wording_claim(
                ResumeClaim(
                    "rewrite",
                    text,
                    ClaimOrigin.GENERATED_WORDING,
                    ("fact",),
                ),
                source,
            ),
            "negation, qualification, or ownership",
        )


def test_skill_claims_cannot_be_projected_into_job_history() -> None:
    sources = [
        {
            "claim_id": "fact",
            "path": "/experience/0/bullets/0",
            "text": "Built backend services using Python",
        },
        {
            "claim_id": "skill-rest",
            "path": "/skills/0/items/0",
            "text": "REST APIs",
        },
    ]
    expect_error(
        lambda: validate_generated_wording_claim(
            ResumeClaim(
                "rewrite",
                "Developed backend services using Python and REST APIs",
                ClaimOrigin.GENERATED_WORDING,
                ("fact", "skill-rest"),
            ),
            sources,
        ),
        "cannot be projected into historical experience",
    )

    validate_generated_wording_claim(
        ResumeClaim(
            "summary-rewrite",
            "Backend engineer with REST APIs",
            ClaimOrigin.GENERATED_WORDING,
            ("summary", "skill-rest"),
        ),
        [
            {
                "claim_id": "summary",
                "path": "/summary",
                "text": "Backend engineer with APIs",
            },
            sources[1],
        ],
    )


def test_grounded_claim_rejects_role_and_metric_association_inversions() -> None:
    cases = (
        (
            "Migrated Python service to Java",
            "Migrated Java service to Python",
            "conserved source relationships",
        ),
        (
            "Reduced latency and increased errors",
            "Increased latency and reduced errors",
            "conserved source relationships",
        ),
        (
            "Customer reduced our costs",
            "Reduced customer costs",
            "conserved source relationships",
        ),
        (
            "2 years Python and 10 years Java",
            "10 years Python and 2 years Java",
            "metric",
        ),
        (
            "Managed 3 engineers and supported 20 services",
            "Managed 20 engineers and supported 3 services",
            "metric",
        ),
    )
    for source_text, rewrite, message in cases:
        expect_error(
            lambda source_text=source_text, rewrite=rewrite: (
                validate_generated_wording_claim(
                    ResumeClaim(
                        "rewrite",
                        rewrite,
                        ClaimOrigin.GENERATED_WORDING,
                        ("fact",),
                    ),
                    [{"claim_id": "fact", "text": source_text}],
                )
            ),
            message,
        )


def test_grounded_claim_preserves_semantic_operators_and_their_positions():
    cases = (
        ("Kept latency > 20 ms", "Kept latency < 20 ms"),
        ("Held error rate <= 2%", "Held error rate >= 2%"),
        ("Supported 20-30 services", "Supported 20 30 services"),
        ("Maintained a 3:1 ratio", "Maintained a 3 1 ratio"),
        ("A > B, C", "A, B > C"),
    )
    for source_text, rewrite in cases:
        expect_error(
            lambda source_text=source_text, rewrite=rewrite: (
                validate_generated_wording_claim(
                    ResumeClaim(
                        "rewrite",
                        rewrite,
                        ClaimOrigin.GENERATED_WORDING,
                        ("fact",),
                    ),
                    [{"claim_id": "fact", "text": source_text}],
                )
            ),
            "operator",
        )


def test_grounded_claim_rejects_metric_symbols_and_invisible_controls():
    expect_error(
        lambda: validate_generated_wording_claim(
            ResumeClaim(
                "rewrite",
                "Reduced cost to 20",
                ClaimOrigin.GENERATED_WORDING,
                ("fact",),
            ),
            [{"claim_id": "fact", "text": "Reduced cost to $20"}],
        ),
        "metric",
    )
    for hidden in ("\u202e", "\u2066", "\u200b", "\x1f", "\ufe0f"):
        expect_error(
            lambda hidden=hidden: validate_generated_wording_claim(
                ResumeClaim(
                    "rewrite",
                    f"Built{hidden} Python APIs",
                    ClaimOrigin.GENERATED_WORDING,
                    ("fact",),
                ),
                [{"claim_id": "fact", "text": "Built Python APIs"}],
            ),
            "control, invisible",
        )


def test_grounded_claim_still_allows_harmless_punctuation_and_curated_aliases():
    validate_generated_wording_claim(
        ResumeClaim(
            "rewrite",
            "Built, k8s services.",
            ClaimOrigin.GENERATED_WORDING,
            ("fact",),
        ),
        [{"claim_id": "fact", "text": "Built Kubernetes services"}],
    )


def test_grounded_boundary_strips_unattested_hidden_targets():
    output, persisted = grounded_bundle()
    output["content"]["identity"]["linkedin"] = {
        "display": "linkedin.com/in/ada",
        "url": "https://attacker.test/private-data",
    }
    base = {"standard_id": "standard-1", "content": deepcopy(base_content())}
    base["content"]["identity"]["linkedin"] = deepcopy(
        output["content"]["identity"]["linkedin"]
    )
    intended = render_resume_tex(output["content"]).intended_text
    result = validate_grounded_artifact_boundary(
        output,
        base,
        source_claims(),
        persisted,
        intended,
    )
    assert result.output["content"]["identity"]["linkedin"] == {
        "display": "linkedin.com/in/ada"
    }
    assert "attacker.test" not in result.rendered.tex_source
    assert r"\href{https://linkedin.com/in/ada}" in result.rendered.tex_source


def main():
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume lab grounding tests)")


if __name__ == "__main__":
    main()
