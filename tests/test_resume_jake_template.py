#!/usr/bin/env python3
"""Offline template checks and opt-in real, isolated PDF acceptance tests.

Run with --tectonic PATH --bundle PATH --output-dir .cache/resume-proof using
Python with requirements/resume.txt installed to exercise the actual engines.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
from pathlib import Path

from job_search.contracts import ContractError
from job_search.resume_lab.model import validate_standard_normalization_output
from job_search.resume_lab.pdf import PypdfExtractor
from job_search.resume_lab.tex import (
    DEFAULT_TEMPLATE_VERSION, LEGACY_TEMPLATE_VERSION, JAKE_TEMPLATE_PATH,
    TectonicCompiler, TexLayoutOverflowError, preflight_tex, render_resume_tex,
)
from job_search.resume_lab.toolchain import ResumeArtifactToolchain
from tests.test_resume_lab_toolchain import normalization_output, sample_content


def fixture() -> dict:
    """Synthetic data exercising every user-supplied template section."""
    return {
        "identity": {"name": "Alex García", "contact_line": "312-555-0100",
            "email": "alex@example.test", "linkedin": "linkedin.com/in/alex",
            "github": "github.com/alex"},
        "education": [{"institution": "Example University", "location": "Chicago, IL",
            "degree": "Bachelor of Science in Computer Science", "dates": "Aug. 2016 -- May 2020"}],
        "experience": [
            {"role": "Software Engineer", "company": "Example Research & Development",
                "location": "Chicago, IL", "dates": "June 2022 -- Present",
                "bullets": [
                    "Built Python and PostgreSQL services for a product analytics platform used by 12 internal teams.",
                    "Reduced database query latency by 35% through indexing and measured query optimization.",
                    "Improved automated test coverage and delivered monthly releases with a cross-functional team."]},
            {"role": "Junior Developer", "company": "Example Software", "location": "Remote",
                "dates": "June 2020 -- May 2022", "bullets": [
                    "Implemented REST API endpoints and accessible React interfaces for customer support workflows.",
                    "Investigated production issues using structured logs and documented recurring fixes."]},
        ],
        "projects": [{"name": "Personal Search", "context": "Python, SQLite, React",
            "dates": "2025 -- Present", "url": "https://example.test/search",
            "bullets": ["Built a searchable archive with source-linked results and reproducible imports.",
                "Added tests for C++ labels, underscores_like_this, and literal characters: #1, $5, {x}."]}],
        "skills": [
            {"category": "Languages", "items": ["Python", "TypeScript", "SQL", "C++"]},
            {"category": "Frameworks", "items": ["React", "FastAPI"]},
            {"category": "Developer Tools", "items": ["Git", "Docker", "PostgreSQL", "SQLite"]},
        ],
    }


def test_jake_layout_and_versioned_legacy():
    content = fixture()
    current = render_resume_tex(content)
    legacy = render_resume_tex(content, template_version=LEGACY_TEMPLATE_VERSION)
    assert current.template_version == DEFAULT_TEMPLATE_VERSION
    assert legacy.template_version == LEGACY_TEMPLATE_VERSION
    assert r"\documentclass[letterpaper,11pt]{article}" in current.tex_source
    assert r"\documentclass[letterpaper,10pt]{article}" in legacy.tex_source
    assert current.section_names == ("Education", "Experience", "Projects", "Technical Skills")
    assert legacy.section_names == ("Work Experience", "Projects", "Education", "Skills")
    assert current.tex_source.index(r"\section{Education}") < current.tex_source.index(r"\section{Experience}")
    assert r"\resumeSubheading{Software Engineer}{June 2022 -{}- Present}{Example Research \& Development}{Chicago, IL}" in current.tex_source
    assert r"\resumeSubheading{Example University}{Chicago, IL}{Bachelor of Science in Computer Science}{Aug. 2016 -{}- May 2020}" in current.tex_source
    assert "Jake Gutierrez" in JAKE_TEMPLATE_PATH.read_text()
    assert "Copyright (c) 2020 Jake Gutierrez" in JAKE_TEMPLATE_PATH.with_name("JAKE_TEMPLATE_MIT.txt").read_text()
    for output in (current, legacy):
        assert preflight_tex(output.tex_source).safe
        assert output.source_sha256 == hashlib.sha256(output.tex_source.encode()).hexdigest()
    try:
        render_resume_tex(content, template_version="untrusted.tex")
    except ContractError:
        pass
    else:
        raise AssertionError("unregistered templates must not load")


def test_optional_sections_and_research_notice():
    rendered = render_resume_tex({"identity": {"name": "Name"}}, visible_notice="SYNTHETIC RESEARCH")
    assert not rendered.section_names
    assert preflight_tex(rendered.tex_source).safe
    assert "SYNTHETIC RESEARCH" in rendered.intended_text


def test_template_fields_cannot_inject_tex_or_hidden_links():
    content = fixture()
    content["experience"][0]["bullets"] = [r"Use \input{/etc/passwd}, \write18{whoami}, 50% R&D"]
    content["projects"][0]["url"] = r"https://example.test/}\input{private}"
    rendered = render_resume_tex(content)
    assert r"\textbackslash{}input\{/etc/passwd\}" in rendered.tex_source
    assert "private" not in rendered.tex_source
    for attack in (r"\input{glyphtounicode}", r"\input{/etc/passwd}", r"\write18{id}"):
        assert not preflight_tex(rendered.tex_source + attack).safe


def test_normalization_supports_both_complete_layouts():
    content = sample_content()
    for version in (DEFAULT_TEMPLATE_VERSION, LEGACY_TEMPLATE_VERSION):
        rendered = render_resume_tex(content, template_version=version)
        normalized = validate_standard_normalization_output(
            normalization_output(content, rendered.intended_text), rendered.intended_text)
        assert normalized["content"]["education"] == content["education"]


def real_acceptance(executable: Path, bundle: Path, output_dir: Path) -> None:
    """Use production network-denied compiler/parser sandboxes, without mocks."""
    import subprocess
    version = subprocess.check_output([str(executable.resolve()), "--version"], text=True).strip()
    toolchain = ResumeArtifactToolchain(TectonicCompiler(executable, bundle, version), PypdfExtractor())
    output_dir.mkdir(parents=True, exist_ok=True)
    built = toolchain.build(fixture(), max_pages=1, allow_warning=False)
    assert built.extracted.pages == 1 and built.fidelity.status == "pass"
    (output_dir / "jake-resume.pdf").write_bytes(built.compiled.pdf_bytes)
    (output_dir / "jake-resume.tex").write_text(built.rendered.tex_source)
    (output_dir / "jake-resume.txt").write_text(built.extracted.logical_text)
    for version in (DEFAULT_TEMPLATE_VERSION, LEGACY_TEMPLATE_VERSION):
        basic = toolchain.build(sample_content(), template_version=version, max_pages=1, allow_warning=False)
        assert basic.fidelity.status == "pass"
    short = toolchain.build({"identity": {"name": "Alex García"}}, max_pages=1, allow_warning=False)
    assert short.extracted.pages == 1
    wide = fixture()
    wide["experience"][0]["company"] = "Unbreakable" * 35
    try:
        toolchain.build(wide, max_pages=1)
    except TexLayoutOverflowError:
        pass
    else:
        raise AssertionError("overfull headings must not be released")
    long = fixture()
    long["experience"] = [copy.deepcopy(long["experience"][0]) for _ in range(12)]
    try:
        toolchain.build(long, max_pages=1)
    except TexLayoutOverflowError:
        pass
    else:
        raise AssertionError("multiple pages must not pass a one-page budget")
    # Exercise the actual career database/approval/application path using the
    # production PDF engines as well as checking the renderer in isolation.
    from tempfile import TemporaryDirectory
    from tests.test_career_resume import gateway_at, JOB, Context
    with TemporaryDirectory(prefix="career-real-") as directory:
        gateway, application_id = gateway_at(Path(directory), toolchain=toolchain)
        prepared = gateway.prepare(JOB, application_id=application_id, idempotency_key="real-compose")
        result = gateway.handle_work({"run_id": prepared["run_id"]}, Context())
        view = gateway.get_run_result(prepared["run_id"])
        assert result["status"] == "succeeded", view
        candidate = view["comparisons"][0]
        gateway.approve_run(prepared["run_id"], comparison_kind="grounded_rewrite", idempotency_key="real-approve")
        selected = gateway.select_resume(application_id, job=JOB, artifact_id=candidate["artifact_id"],
            evaluation_id=candidate["evaluation_id"], idempotency_key="real-select")
        assert selected["selection"]["source_mode"] == "career_profile"
        artifact = gateway.service.get_artifact(candidate["artifact_id"], include_content=True)
        assert artifact["metadata"]["pages"] == 1 and artifact["metadata"]["fidelity"]["status"] == "pass"
        saved = gateway.artifacts.read_pdf(artifact["managed_relative_path"], artifact["pdf_sha256"])
        (output_dir / "career-end-to-end.pdf").write_bytes(saved.content)
    print(f"ok (real isolated PDF: {built.extracted.pages} page, {built.fidelity.status}; overflow gates verified)")
    print("ok (real career database -> composition -> PDF -> approval -> application selection)")
    print(f"PDF: {(output_dir / 'jake-resume.pdf').resolve()}")
    print(f"Bundle SHA256: {built.compiled.bundle_sha256}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tectonic", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".cache/resume-proof"))
    args = parser.parse_args()
    tests = [value for key, value in globals().items() if key.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} Jake template tests)")
    if bool(args.tectonic) != bool(args.bundle):
        parser.error("--tectonic and --bundle must be supplied together")
    if args.tectonic:
        real_acceptance(args.tectonic, args.bundle, args.output_dir)


if __name__ == "__main__":
    main()
