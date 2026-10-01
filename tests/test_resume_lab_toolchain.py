#!/usr/bin/env python3
"""Offline tests for secure resume rendering, parsing, and local-model adapters."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from job_search.contracts import ContractError
from job_search.resume_lab.career_ops import (
    TEMPLATE_PATH,
    UPSTREAM_COMMIT,
    escape_latex,
    normalize_ats_tokens,
    sanitize_ats_text,
    sanitize_url,
)
from job_search.resume_lab.fidelity import evaluate_pdf_fidelity
from job_search.resume_lab.model import (
    MAX_MODEL_OUTPUT_BYTES,
    LocalJsonResumeModel,
    LocalResumeModelConfig,
    ResumeModelError,
    ResumeNormalizationRequired,
    load_resume_model_config,
    macos_model_sandbox,
    model_artifact_sha256,
    resume_model_status,
    select_base_standard,
    validate_evidence_batch_output,
    validate_requirement_output,
    validate_standard_normalization_output,
    validate_variant_output,
    variant_generation_status,
)
from job_search.resume_lab.pdf import (
    PdfExtraction,
    PdfExtractionError,
    PypdfExtractor,
    macos_pdf_sandbox,
)
from job_search.resume_lab.process import (
    ProcessOutputLimitError,
    run_bounded_process,
)
from job_search.resume_lab.tex import (
    TectonicCompiler,
    TexToolchainError,
    attest_real_resume_links,
    macos_tectonic_sandbox,
    preflight_tex,
    render_resume_tex,
)
from job_search.resume_lab.toolchain import PdfFidelityError, ResumeArtifactToolchain


def sample_content(summary="Backend engineer"):
    return {
        "identity": {
            "name": "Alex Example",
            "contact_line": "Chicago, IL",
            "email": {"display": "alex@example.test", "url": "alex@example.test"},
            "linkedin": {
                "display": "linkedin.com/in/alex",
                "url": "https://linkedin.com/in/alex",
            },
        },
        "summary": summary,
        "experience": [
            {
                "company": "Example Co",
                "role": "Software Engineer",
                "location": "Remote",
                "dates": "2022 - Present",
                "bullets": ["Built Python APIs and cut latency 25%"],
            }
        ],
        "projects": [],
        "education": [
            {
                "institution": "Example University",
                "degree": "B.S. Computer Science",
                "location": "Chicago, IL",
                "dates": "2022",
                "details": "Coursework: Distributed Systems",
            }
        ],
        "skills": [{"category": "Languages", "items": ["Python"]}],
    }


def source_claims():
    return [
        {
            "claim_id": "source-summary",
            "path": "/summary",
            "text": "Backend engineer",
            "allowed_equivalent_terms": [],
        },
        {
            "claim_id": "source-api",
            "path": "/experience/0/bullets/0",
            "text": "Built Python APIs and cut latency 25%",
            "allowed_equivalent_terms": [],
        },
        {
            "claim_id": "source-course",
            "path": "/education/0/details",
            "text": "Coursework: Distributed Systems",
            "allowed_equivalent_terms": [],
        },
        {
            "claim_id": "source-python",
            "path": "/skills/0/items/0",
            "text": "Python",
            "allowed_equivalent_terms": [],
        },
    ]


def grounded_output(content=None):
    content = content or sample_content()
    return {
        "variant_kind": "grounded_rewrite",
        "base_standard_id": "standard-best",
        "synthetic": False,
        "research_only": False,
        "content": content,
        "claims": [
            {
                "claim_id": "claim-summary",
                "path": "/summary",
                "text": content["summary"],
                "origin": "source_rewrite",
                "source_claim_ids": ["source-summary"],
                "added_equivalent_terms": [],
            },
            {
                "claim_id": "claim-api",
                "path": "/experience/0/bullets/0",
                "text": content["experience"][0]["bullets"][0],
                "origin": "source_rewrite",
                "source_claim_ids": ["source-api"],
                "added_equivalent_terms": [],
            },
            {
                "claim_id": "claim-course",
                "path": "/education/0/details",
                "text": content["education"][0]["details"],
                "origin": "source",
                "source_claim_ids": ["source-course"],
                "added_equivalent_terms": [],
            },
            {
                "claim_id": "claim-python",
                "path": "/skills/0/items/0",
                "text": "Python",
                "origin": "source",
                "source_claim_ids": ["source-python"],
                "added_equivalent_terms": [],
            },
        ],
    }


def standards():
    return [
        {
            "standard_id": "standard-second",
            "rank": 2,
            "content": sample_content("Other"),
        },
        {"standard_id": "standard-best", "rank": 1, "content": sample_content()},
    ]


def normalization_output(content, parsed_text):
    fixed = [
        ("/identity/name", content["identity"]["name"]),
        ("/identity/contact_line", content["identity"]["contact_line"]),
        ("/identity/email/display", content["identity"]["email"]["display"]),
        ("/identity/linkedin/display", content["identity"]["linkedin"]["display"]),
        ("/experience/0/company", content["experience"][0]["company"]),
        ("/experience/0/role", content["experience"][0]["role"]),
        ("/experience/0/location", content["experience"][0]["location"]),
        ("/experience/0/dates", content["experience"][0]["dates"]),
        ("/education/0/institution", content["education"][0]["institution"]),
        ("/education/0/degree", content["education"][0]["degree"]),
        ("/education/0/location", content["education"][0]["location"]),
        ("/education/0/dates", content["education"][0]["dates"]),
        ("/skills/0/category", content["skills"][0]["category"]),
    ]
    claims = [
        ("source-summary", "/summary", content["summary"]),
        (
            "source-experience-1",
            "/experience/0/bullets/0",
            content["experience"][0]["bullets"][0],
        ),
        (
            "source-education-1",
            "/education/0/details",
            content["education"][0]["details"],
        ),
        ("source-skill-1", "/skills/0/items/0", content["skills"][0]["items"][0]),
    ]

    used_spans = set()

    def anchored(path, text):
        offset = 0
        while True:
            start = parsed_text.find(text, offset)
            if start < 0:
                raise AssertionError(f"fixture text missing for {path}")
            span = (start, start + len(text))
            if all(span[1] <= used[0] or span[0] >= used[1] for used in used_spans):
                used_spans.add(span)
                break
            offset = start + 1
        return {
            "path": path,
            "text": text,
            "source_start": start,
            "source_end": start + len(text),
        }

    return {
        "content": json.loads(json.dumps(content)),
        "fixed_fields": [anchored(path, text) for path, text in fixed],
        "claims": [
            {"claim_id": claim_id, **anchored(path, text)}
            for claim_id, path, text in claims
        ],
    }


def expect(exception, callback, contains=""):
    try:
        callback()
    except exception as exc:
        if contains:
            assert contains in str(exc), str(exc)
    else:
        raise AssertionError(f"expected {exception.__name__}")


def test_vendored_career_ops_notice_and_escape_contract():
    notice = TEMPLATE_PATH.with_name("CAREER_OPS_MIT.txt").read_text()
    assert UPSTREAM_COMMIT == "1c866a9761f141bf341b10fadb38d1e5dc4c3b7d"
    assert UPSTREAM_COMMIT in notice and "MIT License" in notice
    escaped = escape_latex(r"p99 <100ms & $5 #1 a_b {x} C:\tmp | 2x")
    for value in (
        r"\textless{}",
        r"\&",
        r"\$",
        r"\#",
        r"\_",
        r"\{",
        r"\}",
        r"\textbackslash{}",
        r"\textbar{}",
    ):
        assert value in escaped
    assert sanitize_url("person@example.test") == "mailto:person@example.test"
    assert sanitize_url("javascript:alert(1)") == ""
    assert (
        sanitize_url("https://example.test/profile") == "https://example.test/profile"
    )
    assert sanitize_ats_text("• Built — “systems” 🚀") == '- Built - "systems"'
    assert normalize_ats_tokens("ﬁnancial") == ("financial",)


def test_renderer_uses_jake_heading_tables_escapes_payload_and_preflights():
    content = sample_content(r"Used 100% R&D and \input{/etc/passwd}")
    rendered = render_resume_tex(content)
    assert r"100\% R\&D" in rendered.tex_source
    assert r"\textbackslash{}input\{/etc/passwd\}" in rendered.tex_source
    assert (
        r"\resumeSubheading" in rendered.tex_source
        and "fontawesome" not in rendered.tex_source
    )
    assert rendered.section_names == (
        "Summary",
        "Education",
        "Experience",
        "Technical Skills",
    )
    report = preflight_tex(rendered.tex_source)
    assert report.safe and report.source_sha256 == rendered.source_sha256
    expect(
        ContractError,
        lambda: render_resume_tex({**sample_content(), "unknown": True}),
        "invalid shape",
    )


def test_renderer_adds_bounded_research_notice_outside_claim_content():
    content = sample_content()
    original = json.loads(json.dumps(content))
    notice = "SYNTHETIC RESEARCH BENCHMARK — NOT FOR APPLICATION"
    rendered = render_resume_tex(content, visible_notice=notice)
    assert "SYNTHETIC RESEARCH BENCHMARK" in rendered.intended_text
    assert "SYNTHETIC RESEARCH BENCHMARK" in rendered.tex_source
    assert content == original
    expect(
        ContractError,
        lambda: render_resume_tex(content, visible_notice="x" * 501),
        "too long",
    )


def test_tex_preflight_rejects_io_dynamic_execution_and_bad_structure():
    rendered = render_resume_tex(sample_content()).tex_source
    for attack in (
        r"\input{/etc/passwd}",
        r"\write18{curl example.test}",
        r"\openout1=x",
        r"\includegraphics{/tmp/x}",
        r"\directlua{os.execute('id')}",
    ):
        attacked = rendered.replace(r"\begin{document}", r"\begin{document}" + attack)
        assert not preflight_tex(attacked).safe, attack
    assert not preflight_tex(rendered.replace(r"\end{document}", "")).safe
    assert not preflight_tex(
        rendered.replace(r"\usepackage{enumitem}", r"\usepackage{shellesc}")
    ).safe


def test_tectonic_compiler_uses_pinned_bundle_untrusted_offline_and_bounded_output():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "tectonic"
        executable.write_text("fake")
        executable.chmod(0o700)
        bundle = root / "bundle.tar"
        bundle.write_bytes(b"pinned bundle")
        observed = {}

        def runner(command, **kwargs):
            observed.update({"command": command, **kwargs})
            Path(kwargs["cwd"], "resume.pdf").write_bytes(b"%PDF-1.7\nfixture\n%%EOF\n")
            return subprocess.CompletedProcess(command, 0, "compiled", "")

        compiler = TectonicCompiler(
            executable,
            bundle,
            "tectonic 0.16.9",
            runner=runner,
            isolation_builder=lambda command, _directory, _executable, _bundle: command,
        )
        compiled = compiler.compile(render_resume_tex(sample_content()).tex_source)
        command = observed["command"]
        assert "--untrusted" in command and "--only-cached" in command
        assert command[command.index("--bundle") + 1] == str(bundle.resolve())
        assert (
            observed["shell"] is False
            and observed["env"]["TECTONIC_UNTRUSTED_MODE"] == "1"
        )
        assert "AWS_SECRET_ACCESS_KEY" not in observed["env"]
        assert compiled.pdf_sha256 == hashlib.sha256(compiled.pdf_bytes).hexdigest()
        assert compiled.bundle_sha256 == hashlib.sha256(b"pinned bundle").hexdigest()


def test_tectonic_compiler_fails_closed_on_preflight_process_and_output_errors():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "tectonic"
        executable.write_text("fake")
        executable.chmod(0o700)
        bundle = root / "bundle.tar"
        bundle.write_bytes(b"bundle")
        compiler = TectonicCompiler(
            executable,
            bundle,
            "test",
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 9, "", "private"
            ),
            isolation_builder=lambda command, _directory, _executable, _bundle: command,
        )
        expect(
            TexToolchainError,
            lambda: compiler.compile(render_resume_tex(sample_content()).tex_source),
            "status 9",
        )
        expect(
            TexToolchainError,
            lambda: compiler.compile(
                render_resume_tex(sample_content()).tex_source + r"\input{x}"
            ),
            "preflight",
        )
        expect(
            ContractError,
            lambda: TectonicCompiler(root / "missing", bundle, "test"),
            "executable",
        )


def test_pypdf_adapter_isolated_contract_and_schema_validation():
    pdf = b"%PDF-1.7\nfixture\n%%EOF\n"
    observed = {}

    def runner(command, **kwargs):
        observed.update({"command": command, **kwargs})
        Path(command[-1]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "parser": "pypdf",
                    "parser_version": "6.16.2",
                    "pages": 1,
                    "content_stream_bytes": 123,
                    "logical_text": "Alex Example Backend engineer",
                    "layout_text": "Alex Example Backend engineer",
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    extractor = PypdfExtractor(
        runner=runner, isolation_builder=lambda command, _directory: command
    )
    value = extractor.extract(pdf)
    assert value.pdf_sha256 == hashlib.sha256(pdf).hexdigest()
    assert value.parser_version == "6.16.2" and value.pages == 1
    assert observed["shell"] is False and "AWS_SECRET_ACCESS_KEY" not in observed["env"]
    assert observed["command"][1] == "-I"
    expect(ContractError, lambda: extractor.extract(b"not pdf"), "signature")

    def bad_runner(command, **kwargs):
        Path(command[-1]).write_text('{"schema_version":1}')
        return subprocess.CompletedProcess(command, 0, "", "")

    expect(
        PdfExtractionError,
        lambda: PypdfExtractor(
            runner=bad_runner, isolation_builder=lambda command, _directory: command
        ).extract(pdf),
        "schema",
    )


def extraction(logical, layout=None, pdf=b"%PDF-1.7\nfixture\n%%EOF\n"):
    return PdfExtraction(
        hashlib.sha256(pdf).hexdigest(),
        "pypdf",
        "6.16.2",
        1,
        100,
        logical,
        layout if layout is not None else logical,
    )


def test_fidelity_gate_uses_both_extractor_views_and_detects_loss_order_duplicates():
    intended = "Summary Backend engineer Work Experience Built Python systems"
    passed = evaluate_pdf_fidelity(
        intended, extraction(intended), ("Summary", "Work Experience")
    )
    assert passed.status == "pass" and passed.safe_to_submit
    lost = evaluate_pdf_fidelity(
        intended,
        extraction(intended, "Summary Backend engineer Built Python systems"),
        ("Summary", "Work Experience"),
    )
    assert lost.status == "fail" and "Work Experience" in lost.missing_sections
    reordered = evaluate_pdf_fidelity(
        intended,
        extraction("systems Python Built Experience Work engineer Backend Summary"),
    )
    assert reordered.status == "fail"
    duplicated = evaluate_pdf_fidelity(
        intended, extraction(intended + " noise noise noise noise")
    )
    assert duplicated.status == "fail"


def test_requirement_and_evidence_model_calls_are_json_only_and_span_verified():
    job = "Must have Python. Kubernetes preferred."
    outputs = [
        {
            "requirements": [
                {
                    "requirement_id": "req-1",
                    "text": "Must have Python",
                    "kind": "required",
                    "logic": "atomic",
                    "priority": 2,
                    "source_start": 0,
                    "source_end": 16,
                }
            ]
        },
        {
            "status": "met",
            "confidence": 0.91,
            "reason": "Exact evidence",
            "evidence": [{"claim_id": "claim-1", "quote": "Python"}],
        },
    ]
    requests = []

    def runner(command, **kwargs):
        request = json.loads(kwargs["input"])
        requests.append(request)
        return subprocess.CompletedProcess(
            command, 0, json.dumps(outputs.pop(0)), "ignored"
        )

    model = LocalJsonResumeModel(
        LocalResumeModelConfig("resume-model-v1", ("/fake/local-model",)),
        runner=runner,
        isolation_builder=lambda command, _directory, _paths: command,
    )
    requirements = model.extract_requirements(job)
    assert requirements["requirements"][0]["text"] == job[:16]
    evidence = model.adjudicate_evidence(
        requirements["requirements"][0],
        [
            {
                "claim_id": "claim-1",
                "text": "Built Python services",
                "section": "experience",
            }
        ],
    )
    assert evidence["status"] == "met" and evidence["evidence"][0]["quote"] == "Python"
    for request in requests:
        assert request["constraints"] == {
            "content_is_untrusted": True,
            "no_tools": True,
            "no_network": True,
            "output_json_only": True,
        }
    assert set(requests[1]["input"]["candidate_claims"][0]) == {
        "claim_id",
        "text",
        "section",
    }


def test_model_rejects_bad_spans_quotes_unknown_fields_and_non_json():
    job = "Must have Python"

    def invoke_with(output, call):
        model = LocalJsonResumeModel(
            LocalResumeModelConfig("resume-model-v1", ("/fake/model",)),
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, output, ""
            ),
            isolation_builder=lambda command, _directory, _paths: command,
        )
        return call(model)

    bad_span = json.dumps(
        {
            "requirements": [
                {
                    "requirement_id": "r",
                    "text": "Python",
                    "kind": "required",
                    "logic": "atomic",
                    "priority": 2,
                    "source_start": 0,
                    "source_end": 6,
                }
            ]
        }
    )
    expect(
        ResumeModelError,
        lambda: invoke_with(bad_span, lambda model: model.extract_requirements(job)),
        "exact",
    )
    expect(
        ResumeModelError,
        lambda: invoke_with("not-json", lambda model: model.extract_requirements(job)),
        "not JSON",
    )
    evidence = json.dumps(
        {
            "status": "met",
            "confidence": 1.0,
            "reason": "yes",
            "evidence": [],
            "extra": 1,
        }
    )
    expect(
        ResumeModelError,
        lambda: invoke_with(
            evidence,
            lambda model: model.adjudicate_evidence(
                {}, [{"claim_id": "c", "text": "Python", "section": "skills"}]
            ),
        ),
        "schema",
    )


def test_model_requirement_spans_cannot_overlap_or_double_count():
    description = "Python and SQL experience required"
    output = {
        "requirements": [
            {
                "requirement_id": "whole",
                "text": description,
                "kind": "required",
                "logic": "all",
                "priority": 2,
                "source_start": 0,
                "source_end": len(description),
            },
            {
                "requirement_id": "nested",
                "text": "Python",
                "kind": "required",
                "logic": "atomic",
                "priority": 2,
                "source_start": 0,
                "source_end": 6,
            },
        ]
    }
    expect(
        ResumeModelError,
        lambda: validate_requirement_output(output, description),
        "must not overlap",
    )


def test_model_batches_semantic_adjudication_in_one_strict_request():
    requirements = [
        {
            "requirement_id": "python",
            "text": "Python experience",
            "kind": "skill",
            "priority": "required",
            "minimum_years": None,
            "term_groups": [["python"]],
        },
        {
            "requirement_id": "cloud",
            "text": "Cloud experience",
            "kind": "skill",
            "priority": "preferred",
            "minimum_years": None,
            "term_groups": [["cloud"]],
        },
    ]
    candidates = [
        {"claim_id": "claim-1", "text": "Built Python APIs", "section": "resume"}
    ]
    response = {
        "adjudications": [
            {
                "requirement_id": "cloud",
                "status": "not_evidenced",
                "confidence": 0.8,
                "reason": "No cited evidence",
                "evidence": [],
            },
            {
                "requirement_id": "python",
                "status": "met",
                "confidence": 0.95,
                "reason": "Direct evidence",
                "evidence": [{"claim_id": "claim-1", "quote": "Python"}],
            },
        ]
    }
    requests = []

    def runner(command, **kwargs):
        requests.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    model = LocalJsonResumeModel(
        LocalResumeModelConfig("resume-model-v1", ("/fake/model",)),
        runner=runner,
        isolation_builder=lambda command, _directory, _paths: command,
    )
    result = model.adjudicate_evidence_batch(requirements, candidates)
    assert len(requests) == 1
    assert requests[0]["task"] == "adjudicate_resume_evidence_batch"
    assert [item["requirement_id"] for item in result["adjudications"]] == [
        "python",
        "cloud",
    ]

    duplicate = {
        "adjudications": [response["adjudications"][1]] * len(requirements)
    }
    expect(
        ResumeModelError,
        lambda: validate_evidence_batch_output(duplicate, requirements, candidates),
        "exactly once",
    )


def test_owner_only_resume_model_config_and_redacted_status():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "model"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        weights = root / "weights.gguf"
        weights.write_bytes(b"fixed-test-weights")
        weights_digest = hashlib.sha256(weights.read_bytes()).hexdigest()
        path = root / "resume-model.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "producer_version": "local-resume-v1",
                    "command": [
                        str(executable),
                        "--backend",
                        "llama-cpp-python",
                        "--model-path",
                        str(weights),
                        "--json",
                    ],
                    "allowed_read_paths": [str(weights)],
                    "timeout_seconds": 45,
                    "model_sha256": weights_digest,
                }
            )
        )
        os.chmod(path, 0o600)
        config = load_resume_model_config(path)
        assert config.version == 1 and config.command[-1] == "--json"
        assert config.allowed_read_paths == (weights.resolve(),)
        assert config.model_sha256 == weights_digest
        assert config.admission_lock_path == (
            root / ".resume-model.json.admission.lock"
        ).resolve()
        assert config.build().config.model_sha256 == weights_digest
        report = resume_model_status(config)
        assert report["producer_version"] == "local-resume-v1"
        assert report["model_sha256"] == weights_digest
        assert report["command_available"] is True
        assert str(executable) not in json.dumps(report)
        missing = resume_model_status(None)
        assert missing["status"] == "blocked_setup"
        assert missing["reason"] == "model_config_missing"

        missing_digest_path = root / "missing-digest.json"
        missing_digest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "producer_version": "local-resume-v1",
                    "command": [
                        str(executable),
                        "--backend",
                        "llama-cpp-python",
                        "--model-path",
                        str(weights),
                    ],
                    "allowed_read_paths": [str(weights)],
                    "timeout_seconds": 45,
                }
            )
        )
        os.chmod(missing_digest_path, 0o600)
        expect(
            ContractError,
            lambda: load_resume_model_config(missing_digest_path),
            "requires model_sha256",
        )

        os.chmod(path, 0o644)
        expect(ContractError, lambda: load_resume_model_config(path), "owner-only")
        os.chmod(path, 0o600)
        link = root / "config-link.json"
        link.symlink_to(path)
        expect(ContractError, lambda: load_resume_model_config(link), "owner-only")

        link.unlink()
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "producer_version": "local-resume-v1",
                    "command": [
                        str(executable),
                        "--backend",
                        "llama-cpp-python",
                        "--model-path",
                        str(weights),
                    ],
                    "allowed_read_paths": [str(Path.home())],
                    "timeout_seconds": 45,
                    "model_sha256": weights_digest,
                }
            )
        )
        expect(ContractError, lambda: load_resume_model_config(path), "too broad")
        for broad in (
            Path.home(),
            Path("/System/Volumes/Data"),
            Path("/System/Volumes/Data") / Path.home().resolve().relative_to("/"),
            Path("/Volumes"),
            Path("/var"),
            Path("/etc"),
        ):
            expect(
                ContractError,
                lambda broad=broad: LocalJsonResumeModel(
                    LocalResumeModelConfig(
                        "local-resume-v1", (str(executable),), (broad,)
                    )
                ),
                "too broad",
            )

        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            expect(ResumeModelError, lambda: macos_model_sandbox(
                (str(executable),), root / "sandbox", (weights,)
            ), "isolation is unavailable")
            return
        profile = macos_model_sandbox(
            (str(executable),), root / "sandbox", (weights,)
        )[2]
        assert '(literal "/")' in profile
        assert '(subpath "/usr")' not in profile
        assert '(subpath "/Library")' not in profile
        assert '(subpath "/usr/lib")' in profile


def test_model_digest_is_verified_once_and_mutation_fails_before_inference():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        weights = root / "model.gguf"
        weights.write_bytes(b"immutable-model-v1")
        digest = hashlib.sha256(weights.read_bytes()).hexdigest()
        lock_path = root / "model.lock"
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, '{"requirements":[]}', "")

        config = LocalResumeModelConfig(
            producer_version="resume-model-v1",
            command=(
                sys.executable,
                "--backend",
                "llama-cpp-python",
                "--model-path",
                str(weights),
            ),
            allowed_read_paths=(weights,),
            model_sha256=digest,
            admission_lock_path=lock_path,
        )
        model = LocalJsonResumeModel(
            config,
            runner=runner,
            isolation_builder=lambda command, _directory, _paths: command,
        )
        assert model.config.model_sha256 == digest
        weights.write_bytes(b"mutated-model-v2-and-a-new-size")
        expect(
            ResumeModelError,
            lambda: model.extract_requirements("Python required"),
            "changed after startup verification",
        )
        assert calls == []

        expect(
            ResumeModelError,
            lambda: LocalJsonResumeModel(
                LocalResumeModelConfig(
                    **{
                        **config.__dict__,
                        "model_sha256": "0" * 64,
                    }
                )
            ),
            "does not match",
        )


def test_model_tree_digest_is_canonical_bounded_and_link_free():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "mlx-model"
        (root / "nested").mkdir(parents=True)
        (root / "tokenizer.json").write_text('{"kind":"test"}')
        (root / "nested" / "weights.safetensors").write_bytes(b"weights")
        first = model_artifact_sha256(root)
        assert first == model_artifact_sha256(root)
        (root / "nested" / "weights.safetensors").write_bytes(b"changed")
        assert model_artifact_sha256(root) != first
        (root / "model-link").symlink_to(root / "tokenizer.json")
        expect(
            ResumeModelError,
            lambda: model_artifact_sha256(root),
            "cannot contain links",
        )


def test_mlx_production_adapter_is_reported_and_rejected_as_unvalidated():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        model_dir = root / "mlx-model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")
        digest = model_artifact_sha256(model_dir)
        config = LocalResumeModelConfig(
            producer_version="resume-mlx-v1",
            command=(
                sys.executable,
                "--backend",
                "mlx-lm",
                "--model-path",
                str(model_dir),
            ),
            allowed_read_paths=(model_dir,),
            model_sha256=digest,
            admission_lock_path=root / "model.lock",
        )
        report = resume_model_status(config)
        assert report["status"] == "blocked_setup"
        assert report["reason"] == "model_backend_unvalidated"
        assert report["model_sha256"] == digest
        assert str(model_dir) not in json.dumps(report)
        expect(
            ContractError,
            lambda: LocalJsonResumeModel(config),
            "production isolation is not validated",
        )


def test_model_admission_is_serialized_in_process_and_across_processes():
    response = subprocess.CompletedProcess((), 0, '{"requirements":[]}', "")
    state_lock = threading.Lock()
    active = 0
    maximum = 0
    failures = []

    def runner(command, **_kwargs):
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return subprocess.CompletedProcess(command, 0, response.stdout, "")

    model = LocalJsonResumeModel(
        LocalResumeModelConfig("resume-model-v1", ("/fake/model",)),
        runner=runner,
        isolation_builder=lambda command, _directory, _paths: command,
    )

    def invoke():
        try:
            model.extract_requirements("Python required")
        except (ContractError, ResumeModelError) as exc:
            failures.append(exc)

    threads = [threading.Thread(target=invoke) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == [] and maximum == 1

    with tempfile.TemporaryDirectory() as directory:
        lock_path = Path(directory) / "cross-process.lock"
        holder_script = (
            "import fcntl,os,sys,time; "
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); "
            "os.fchmod(fd,0o600); fcntl.flock(fd,fcntl.LOCK_EX); "
            "print('locked',flush=True); time.sleep(10)"
        )
        holder = subprocess.Popen(
            (
                sys.executable,
                "-c",
                holder_script,
                str(lock_path),
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert holder.stdout is not None and holder.stdout.readline() == "locked\n"
            locked_model = LocalJsonResumeModel(
                LocalResumeModelConfig(
                    "resume-model-v1",
                    ("/fake/model",),
                    timeout_seconds=1,
                    admission_lock_path=lock_path,
                ),
                runner=runner,
                isolation_builder=lambda command, _directory, _paths: command,
            )
            expect(
                ResumeModelError,
                lambda: locked_model.extract_requirements("Python required"),
                "admission timed out",
            )
        finally:
            holder.terminate()
            holder.wait(timeout=5)


def test_real_macos_sandboxes_launch_symlinks_and_write_temp_files():
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        return
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        python_link = root / "python-link"
        python_link.symlink_to(Path(sys.executable).resolve())

        for command in (
            macos_pdf_sandbox(
                (str(python_link), "-I", "-c", 'print("pdf-ok")'), root
            ),
            macos_model_sandbox(
                (str(python_link), "-I", "-c", 'print("model-ok")'),
                root,
                (Path(sys.base_prefix),),
            ),
        ):
            completed = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
                cwd=str(root),
            )
            assert completed.returncode == 0, completed.stderr

        bundle = root / "bundle"
        bundle.write_bytes(b"offline-test-bundle")
        output = root / "sandbox-write"
        command = macos_tectonic_sandbox(
            ("/usr/bin/touch", str(output)),
            root,
            Path("/usr/bin/touch"),
            bundle,
        )
        assert '(subpath "/usr")' not in command[2]
        assert '(subpath "/Library")' not in command[2]
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert output.is_file()


def test_toolchain_process_capture_has_an_active_combined_output_cap():
    with tempfile.TemporaryDirectory() as name:
        expect(
            ProcessOutputLimitError,
            lambda: run_bounded_process(
                (
                    sys.executable,
                    "-c",
                    "import sys;sys.stdout.write('x'*4096);sys.stdout.flush()",
                ),
                timeout=5,
                cwd=name,
                env={"PATH": "/usr/bin:/bin"},
                preexec_fn=lambda: None,
                max_output_bytes=512,
            ),
            "output exceeded",
        )


def test_standard_normalization_requires_exact_pdf_spans_and_complete_coverage():
    content = sample_content()
    parsed_text = render_resume_tex(content).intended_text
    output = normalization_output(content, parsed_text)
    seen = {}

    def runner(command, **kwargs):
        seen.update(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

    model = LocalJsonResumeModel(
        LocalResumeModelConfig("resume-model-v1", ("/fake/model",)),
        runner=runner,
        isolation_builder=lambda command, _directory, _paths: command,
    )
    normalized = model.normalize_standard_resume(
        r"\documentclass{moderncv}\begin{document}Imported\end{document}",
        parsed_text,
    )
    assert seen["task"] == "normalize_standard_resume"
    assert normalized["content"] == attest_real_resume_links(content)
    assert normalized["claims"][0]["allowed_equivalent_terms"] == []

    incomplete = normalization_output(content, parsed_text)
    incomplete["fixed_fields"].pop()
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(incomplete, parsed_text),
        "every visible fixed field",
    )
    false_span = normalization_output(content, parsed_text)
    false_span["claims"][0]["source_start"] += 1
    false_span["claims"][0]["source_end"] += 1
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(false_span, parsed_text),
        "exact parsed-PDF span",
    )

    duplicate_span = normalization_output(content, parsed_text)
    duplicate_span["claims"][0]["source_start"] = duplicate_span["fixed_fields"][0][
        "source_start"
    ]
    duplicate_span["claims"][0]["source_end"] = duplicate_span["fixed_fields"][0][
        "source_end"
    ]
    duplicate_span["claims"][0]["text"] = duplicate_span["fixed_fields"][0]["text"]
    duplicate_span["content"]["summary"] = duplicate_span["fixed_fields"][0]["text"]
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(duplicate_span, parsed_text),
        "non-overlapping",
    )

    misplaced = normalization_output(content, parsed_text)
    first = misplaced["claims"][0]
    second = misplaced["claims"][1]
    first["source_start"], second["source_start"] = (
        second["source_start"],
        first["source_start"],
    )
    first["source_end"], second["source_end"] = second["source_end"], first["source_end"]
    first["text"], second["text"] = second["text"], first["text"]
    misplaced["content"]["summary"] = first["text"]
    misplaced["content"]["experience"][0]["bullets"][0] = second["text"]
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(misplaced, parsed_text),
        "path order",
    )

    omitted_section_text = (
        parsed_text + "\nCertifications\nAWS Certified Solutions Architect"
    )
    omitted_section = normalization_output(content, omitted_section_text)
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(
            omitted_section, omitted_section_text
        ),
        "meaningful parsed-PDF text unrepresented",
    )

    repeated_unrepresented_text = parsed_text + "\nBackend engineer"
    repeated_unrepresented = normalization_output(
        content, repeated_unrepresented_text
    )
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(
            repeated_unrepresented, repeated_unrepresented_text
        ),
        "meaningful parsed-PDF text unrepresented",
    )

    omitted_known_section = normalization_output(content, parsed_text)
    omitted_known_section["content"]["education"] = []
    omitted_known_section["fixed_fields"] = [
        item
        for item in omitted_known_section["fixed_fields"]
        if not item["path"].startswith("/education/")
    ]
    omitted_known_section["claims"] = [
        item
        for item in omitted_known_section["claims"]
        if not item["path"].startswith("/education/")
    ]
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(
            omitted_known_section, parsed_text
        ),
        "meaningful parsed-PDF text unrepresented",
    )


def test_standard_normalization_allows_only_present_deterministic_heading_aliases():
    content = sample_content()
    parsed_text = render_resume_tex(content).intended_text.replace(
        "Work Experience", "PROFESSIONAL EXPERIENCE"
    )
    normalized = validate_standard_normalization_output(
        normalization_output(content, parsed_text), parsed_text
    )
    assert normalized["content"] == attest_real_resume_links(content)

    no_projects_heading = parsed_text + "\nProjects"
    expect(
        ResumeModelError,
        lambda: validate_standard_normalization_output(
            normalization_output(content, no_projects_heading),
            no_projects_heading,
        ),
        "meaningful parsed-PDF text unrepresented",
    )


def test_standard_normalization_strips_unattested_hidden_link_targets():
    content = sample_content()
    content["identity"]["linkedin"]["url"] = (
        "https://attacker.test/private-contact-data"
    )
    content["projects"] = [
        {
            "name": "Attested Project",
            "context": "Open source",
            "dates": "2024",
            "url": "https://attacker.test/private-project-data",
            "bullets": [],
        }
    ]
    parsed_text = render_resume_tex(content).intended_text
    output = normalization_output(content, parsed_text)
    for path, text in (
        ("/projects/0/name", "Attested Project"),
        ("/projects/0/context", "Open source"),
        ("/projects/0/dates", "2024"),
    ):
        start = parsed_text.index(text)
        output["fixed_fields"].append(
            {
                "path": path,
                "text": text,
                "source_start": start,
                "source_end": start + len(text),
            }
        )

    normalized = validate_standard_normalization_output(output, parsed_text)
    assert normalized["content"]["identity"]["linkedin"] == {
        "display": "linkedin.com/in/alex"
    }
    assert "url" not in normalized["content"]["projects"][0]
    rendered = render_resume_tex(normalized["content"])
    assert "attacker.test" not in rendered.tex_source
    assert r"\href{https://linkedin.com/in/alex}" in rendered.tex_source


def test_local_model_capture_terminates_oversized_stdout_and_stderr():
    for stream in ("stdout", "stderr"):
        script = (
            "import sys; sys.stdin.buffer.read(); "
            f"sys.{stream}.buffer.write(b'x' * {MAX_MODEL_OUTPUT_BYTES + 65536}); "
            f"sys.{stream}.buffer.flush()"
        )
        model = LocalJsonResumeModel(
            LocalResumeModelConfig(
                "resume-model-v1",
                (sys.executable, "-c", script),
                timeout_seconds=10,
            ),
            isolation_builder=lambda command, _directory, _paths: command,
        )
        expect(
            ResumeModelError,
            lambda model=model: model.extract_requirements("Must have Python"),
            "too large",
        )

def test_rank_one_is_the_only_allowed_standard_base():
    assert select_base_standard(standards())["standard_id"] == "standard-best"
    expect(
        ContractError,
        lambda: select_base_standard(
            [{"standard_id": "only", "rank": 2, "content": sample_content()}]
        ),
        "rank 1",
    )
    expect(
        ContractError,
        lambda: select_base_standard(
            [
                {"standard_id": "a", "rank": 1, "content": sample_content()},
                {"standard_id": "b", "rank": 1, "content": sample_content()},
            ]
        ),
        "unique",
    )
    unnormalized = [{"standard_id": "tex-only", "rank": 1, "tex_source": "..."}]
    assert variant_generation_status("grounded_rewrite", unnormalized) == {
        "status": "blocked_setup",
        "reason": "needs_normalization",
        "standard_id": "tex-only",
    }
    expect(
        ResumeNormalizationRequired,
        lambda: select_base_standard(unnormalized),
        "needs_normalization",
    )
    assert variant_generation_status("market_ideal", []) == {
        "status": "ready",
        "reason": None,
        "base_standard_id": None,
    }


def test_grounded_rewrite_is_source_preserving_and_allows_only_equivalent_jd_terms():
    output = grounded_output()
    validated = validate_variant_output(
        output, "grounded_rewrite", standards()[1], source_claims(), ["Kubernetes"]
    )
    assert validated["synthetic"] is False and validated["research_only"] is False

    equivalent_content = sample_content()
    equivalent_content["experience"][0]["bullets"][
        0
    ] = "Built k8s services and cut latency 25%"
    equivalent = grounded_output(equivalent_content)
    equivalent["claims"][1]["added_equivalent_terms"] = ["k8s"]
    equivalent_sources = source_claims()
    equivalent_sources[1]["text"] = "Built Kubernetes services and cut latency 25%"
    equivalent_sources[1]["allowed_equivalent_terms"] = ["kubernetes", "k8s"]
    validate_variant_output(
        equivalent, "grounded_rewrite", standards()[1], equivalent_sources, ["k8s"]
    )

    reordered_content = sample_content()
    reordered_content["experience"][0]["bullets"][
        0
    ] = "Python APIs: built; latency cut 25%."
    reordered = grounded_output(reordered_content)
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            reordered, "grounded_rewrite", standards()[1], source_claims(), ()
        ),
        "metric",
    )

    new_metric = grounded_output(sample_content())
    new_metric["content"]["experience"][0]["bullets"][
        0
    ] = "Built Python APIs and cut latency 50%"
    new_metric["claims"][1]["text"] = "Built Python APIs and cut latency 50%"
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            new_metric, "grounded_rewrite", standards()[1], source_claims(), []
        ),
        "new metric",
    )

    new_tool = grounded_output(sample_content())
    new_tool["content"]["experience"][0]["bullets"][0] += " using Kubernetes"
    new_tool["claims"][1]["text"] += " using Kubernetes"
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            new_tool,
            "grounded_rewrite",
            standards()[1],
            source_claims(),
            ["Kubernetes"],
        ),
        "unapproved tool",
    )

    new_scope = grounded_output(sample_content())
    new_scope["content"]["experience"][0]["bullets"][
        0
    ] = "Led Python APIs and cut latency 25%"
    new_scope["claims"][1]["text"] = "Led Python APIs and cut latency 25%"
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            new_scope, "grounded_rewrite", standards()[1], source_claims(), []
        ),
        "scope",
    )

    invented = grounded_output(sample_content())
    invented["content"]["experience"][0]["bullets"][
        0
    ] = "Built Python APIs and invented quantum teleportation and cut latency 25%"
    invented["claims"][1]["text"] = invented["content"]["experience"][0][
        "bullets"
    ][0]
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            invented, "grounded_rewrite", standards()[1], source_claims(), ()
        ),
        "lexical tokens",
    )

    new_function_word = grounded_output(sample_content())
    new_function_word["content"]["experience"][0]["bullets"][
        0
    ] = "Built Python APIs for latency and cut 25%"
    new_function_word["claims"][1]["text"] = new_function_word["content"][
        "experience"
    ][0]["bullets"][0]
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            new_function_word,
            "grounded_rewrite",
            standards()[1],
            source_claims(),
            (),
        ),
        "metric",
    )

    changed_title = grounded_output(sample_content())
    changed_title["content"]["experience"][0]["role"] = "Principal Engineer"
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            changed_title, "grounded_rewrite", standards()[1], source_claims(), []
        ),
        "fixed identity",
    )
    changed_category = grounded_output(sample_content())
    changed_category["content"]["skills"][0]["category"] = "Infrastructure"
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            changed_category, "grounded_rewrite", standards()[1], source_claims(), []
        ),
        "fixed identity",
    )


def synthetic_output(kind, base_id):
    output = {
        "variant_kind": kind,
        "base_standard_id": base_id,
        "synthetic": True,
        "research_only": True,
        "content": sample_content("Ideal synthetic candidate"),
        "claims": [
            {
                "claim_id": "synthetic-summary",
                "path": "/summary",
                "text": "Ideal synthetic candidate",
                "origin": "keyword_adversarial"
                if kind == "keyword_adversarial"
                else "synthetic",
                "source_claim_ids": [],
                "added_equivalent_terms": [],
            }
        ],
    }
    if kind in {"market_ideal", "keyword_adversarial"}:
        origin = "keyword_adversarial" if kind == "keyword_adversarial" else "synthetic"
        for claim_id, path, text in (
            (
                "synthetic-experience",
                "/experience/0/bullets/0",
                "Built Python APIs and cut latency 25%",
            ),
            (
                "synthetic-education",
                "/education/0/details",
                "Coursework: Distributed Systems",
            ),
            ("synthetic-skill", "/skills/0/items/0", "Python"),
        ):
            output["claims"].append(
                {
                    "claim_id": claim_id,
                    "path": path,
                    "text": text,
                    "origin": origin,
                    "source_claim_ids": [],
                    "added_equivalent_terms": [],
                }
            )
    elif kind == "standard_exaggerated":
        for claim_id, path, text, source_id in (
            (
                "unchanged-experience",
                "/experience/0/bullets/0",
                "Built Python APIs and cut latency 25%",
                "source-api",
            ),
            (
                "unchanged-education",
                "/education/0/details",
                "Coursework: Distributed Systems",
                "source-course",
            ),
            (
                "unchanged-skill",
                "/skills/0/items/0",
                "Python",
                "source-python",
            ),
        ):
            output["claims"].append(
                {
                    "claim_id": claim_id,
                    "path": path,
                    "text": text,
                    "origin": "source",
                    "source_claim_ids": [source_id],
                    "added_equivalent_terms": [],
                }
            )
    return output


def test_synthetic_variant_lineage_is_explicit_and_independent_modes_have_no_base():
    child = validate_variant_output(
        synthetic_output("standard_exaggerated", "standard-best"),
        "standard_exaggerated",
        standards()[1],
        source_claims(),
        (),
    )
    assert child["base_standard_id"] == "standard-best" and child["research_only"]
    omitted_exaggerated = synthetic_output(
        "standard_exaggerated", "standard-best"
    )
    omitted_exaggerated["claims"].pop()
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            omitted_exaggerated,
            "standard_exaggerated",
            standards()[1],
            source_claims(),
            (),
        ),
        "every exaggerated editable slot",
    )
    extra_exaggerated = synthetic_output(
        "standard_exaggerated", "standard-best"
    )
    extra_exaggerated["claims"].append(
        {
            "claim_id": "synthetic-fixed-name",
            "path": "/identity/name",
            "text": "Alex Example",
            "origin": "synthetic",
            "source_claim_ids": [],
            "added_equivalent_terms": [],
        }
    )
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            extra_exaggerated,
            "standard_exaggerated",
            standards()[1],
            source_claims(),
            (),
        ),
        "every exaggerated editable slot",
    )
    wrong_path_exaggerated = synthetic_output(
        "standard_exaggerated", "standard-best"
    )
    wrong_path_exaggerated["claims"][1]["source_claim_ids"] = ["source-python"]
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            wrong_path_exaggerated,
            "standard_exaggerated",
            standards()[1],
            source_claims(),
            (),
        ),
        "match its source path",
    )
    contaminated_change = synthetic_output(
        "standard_exaggerated", "standard-best"
    )
    contaminated_change["claims"][0]["source_claim_ids"] = ["source-summary"]
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            contaminated_change,
            "standard_exaggerated",
            standards()[1],
            source_claims(),
            (),
        ),
        "isolated synthetic content",
    )
    mislabeled_unchanged = synthetic_output(
        "standard_exaggerated", "standard-best"
    )
    mislabeled_unchanged["claims"][1]["origin"] = "synthetic"
    mislabeled_unchanged["claims"][1]["source_claim_ids"] = []
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            mislabeled_unchanged,
            "standard_exaggerated",
            standards()[1],
            source_claims(),
            (),
        ),
        "unchanged exaggerated slot",
    )
    changed_history = synthetic_output("standard_exaggerated", "standard-best")
    changed_history["content"]["experience"][0]["role"] = "Principal Engineer"
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            changed_history,
            "standard_exaggerated",
            standards()[1],
            source_claims(),
            (),
        ),
        "fixed identity",
    )
    for kind in ("market_ideal", "keyword_adversarial"):
        value = validate_variant_output(
            synthetic_output(kind, None),
            kind,
            None,
            (),
            (),
        )
        assert value["base_standard_id"] is None and value["synthetic"]
        omitted = synthetic_output(kind, None)
        omitted["claims"].pop()
        expect(
            ResumeModelError,
            lambda omitted=omitted, kind=kind: validate_variant_output(
                omitted, kind, None, (), ()
            ),
            "every editable independent synthetic slot",
        )
        extra = synthetic_output(kind, None)
        extra["claims"].append(
            {
                "claim_id": "synthetic-fixed-name",
                "path": "/identity/name",
                "text": "Alex Example",
                "origin": "keyword_adversarial"
                if kind == "keyword_adversarial"
                else "synthetic",
                "source_claim_ids": [],
                "added_equivalent_terms": [],
            }
        )
        expect(
            ResumeModelError,
            lambda extra=extra, kind=kind: validate_variant_output(
                extra, kind, None, (), ()
            ),
            "every editable independent synthetic slot",
        )
    contaminated = synthetic_output("market_ideal", None)
    contaminated["claims"][0]["source_claim_ids"] = ["source-summary"]
    expect(
        ResumeModelError,
        lambda: validate_variant_output(
            contaminated, "market_ideal", None, source_claims(), ()
        ),
        "cannot cite",
    )


def test_generate_variant_sends_only_selected_base_and_enforces_mode_contract():
    response = grounded_output()
    seen = {}

    def runner(command, **kwargs):
        seen.update(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    model = LocalJsonResumeModel(
        LocalResumeModelConfig("resume-model-v1", ("/fake/model",)),
        runner=runner,
        isolation_builder=lambda command, _directory, _paths: command,
    )
    value = model.generate_variant(
        "grounded_rewrite",
        {"description": "Python role"},
        standards(),
        source_claims(),
        ["Kubernetes"],
        generation_seed=77,
    )
    assert value["base_standard_id"] == "standard-best"
    assert seen["input"]["base_standard"]["rank"] == 1
    assert "Never add an unsupported tool, fact, metric" in seen["input"][
        "constraint"
    ]
    assert "Preserve every non-article source concept" in seen["input"]["constraint"]
    assert "summary and skill slots" in seen["input"]["constraint"]
    assert seen["constraints"]["generation_seed"] == 77
    content_contract = seen["output_schema"]["content"]
    assert set(content_contract) == {
        "identity",
        "summary",
        "experience",
        "projects",
        "education",
        "skills",
    }
    assert set(content_contract["experience"][0]) == {
        "company",
        "role",
        "location",
        "dates",
        "bullets",
    }


def test_adversarial_optimization_input_is_bounded_and_explicit() -> None:
    response = synthetic_output("keyword_adversarial", None)
    seen = {}

    def runner(command, **kwargs):
        seen.update(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    model = LocalJsonResumeModel(
        LocalResumeModelConfig("resume-model-v1", ("/fake/model",)),
        runner=runner,
        isolation_builder=lambda command, _directory, _paths: command,
    )
    optimization = {
        "optimization_pass": 2,
        "prior_score": 42.0,
        "prior_content": sample_content("Prior draft"),
        "remaining_gaps": [
            {
                "requirement_id": "requirement-python",
                "source_text": "Required experience building Python services",
                "status": "not_evidenced",
                "weight": 10.0,
                "missing_term_groups": [["python"]],
            }
        ],
    }
    model.generate_variant(
        "keyword_adversarial",
        {"description": "Python role"},
        standards(),
        (),
        (),
        generation_seed=77,
        optimization_input=optimization,
    )
    assert seen["input"]["optimization"] == optimization
    expect(
        ContractError,
        lambda: model.generate_variant(
            "market_ideal",
            {"description": "Python role"},
            standards(),
            (),
            (),
            generation_seed=77,
            optimization_input=optimization,
        ),
        "keyword_adversarial",
    )


def test_composed_toolchain_checks_pdf_identity_and_fidelity():
    content = sample_content()
    rendered = render_resume_tex(content)
    pdf = b"%PDF-1.7\nfixture\n%%EOF\n"

    class Compiler:
        def compile(self, source):
            assert source == rendered.tex_source
            from job_search.resume_lab.tex import CompiledPdf

            return CompiledPdf(
                pdf,
                hashlib.sha256(pdf).hexdigest(),
                rendered.source_sha256,
                "tectonic",
                "test",
                "b" * 64,
                "",
            )

    class Extractor:
        def extract(self, value):
            assert value == pdf
            return extraction(rendered.intended_text, pdf=value)

    result = ResumeArtifactToolchain(Compiler(), Extractor()).build(content)
    assert result.fidelity.status == "pass"

    class WrongExtractor:
        def extract(self, _value):
            value = extraction(rendered.intended_text)
            return PdfExtraction(
                "0" * 64,
                value.parser,
                value.parser_version,
                value.pages,
                value.content_stream_bytes,
                value.logical_text,
                value.layout_text,
            )

    expect(
        PdfFidelityError,
        lambda: ResumeArtifactToolchain(Compiler(), WrongExtractor()).build(content),
        "differs",
    )


def main():
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume lab toolchain tests)")


if __name__ == "__main__":
    main()
