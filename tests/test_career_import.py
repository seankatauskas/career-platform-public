#!/usr/bin/env python3
"""Offline source-backed career import, duplicate, and format regressions."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from job_search.resume_lab.career_import import CareerImporter, validate_extraction, MAX_IMPORT_BYTES
from job_search.resume_lab.career_store import CareerStore
from job_search.resume_lab.contracts import ResumeBoundaryError, ResumeConflictError, ResumeLabError
from tests.test_career_store import expect


TEXT = "Morgan Example\nmorgan@example.test\nExample University\nBS Computer Science\n2022\nExample Company\nSoftware Engineer\n2023 -- Present\nBuilt Python services that reduced processing time by 25%.\nLanguages\nPython\nSQL\n"
CONTENT = {"identity": {"name": "Morgan Example", "email": "morgan@example.test"},
    "education": [{"institution": "Example University", "degree": "BS Computer Science", "dates": "2022"}],
    "experience": [{"company": "Example Company", "role": "Software Engineer", "dates": "2023 -- Present",
        "bullets": ["Built Python services that reduced processing time by 25%."]}],
    "skills": [{"category": "Languages", "items": ["Python", "SQL"]}]}


def extraction(content=CONTENT, text=TEXT):
    rows = []
    def visit(value, path=""):
        if isinstance(value, str) and value:
            # Choose skill occurrence after its category, not inside job history.
            offset = text.index("Languages") if path.startswith("/skills/") else 0
            start = text.index(value, offset)
            rows.append({"path": path, "start": start, "end": start + len(value)})
        elif isinstance(value, dict):
            for key, val in value.items():
                visit(val, path + "/" + key)
        elif isinstance(value, list):
            for i, val in enumerate(value):
                visit(val, path + "/" + str(i))
    visit(content)
    return {"content": copy.deepcopy(content), "source_spans": rows}


def test_import_keeps_exact_evidence_and_requires_user_attestation():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        draft = CareerImporter(store, extract_content=lambda text: extraction()).import_text(TEXT)
        assert store.get_profile()["approved"] is None
        assert not store.is_approved(draft["revision_id"])
        span = next(s for s in draft["provenance"]["source_spans"] if s["path"] == "/experience/0/bullets/0")
        assert TEXT[span["start"]:span["end"]] == span["text"]
        assert span["fact_id"] == draft["content"]["experience"][0]["bullets"][0]["fact_id"]
        assert draft["provenance"]["source_text"] == TEXT
        store.approve_revision(draft["revision_id"])
        assert store.is_approved(draft["revision_id"])


def test_unsupported_facts_and_missing_or_invalid_spans_fail_before_saving():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        bad = extraction()
        bad["content"]["experience"][0]["bullets"][0] = "Led AWS migrations and reduced costs by 90%."
        expect(ResumeBoundaryError, lambda: CareerImporter(store, extract_content=lambda _: bad).import_text(TEXT))
        bad = extraction()
        bad["source_spans"].pop()
        expect(ResumeBoundaryError, lambda: validate_extraction(bad, TEXT))
        bad = extraction()
        bad["source_spans"][0]["start"] = True
        expect(ResumeBoundaryError, lambda: validate_extraction(bad, TEXT))
        assert store.get_profile()["draft"] is None


def test_exact_words_cannot_be_reassigned_to_another_employer():
    text = "Morgan\nAlpha Co\nEngineer\nBuilt Python services.\nBeta Co\nDesigner\nDesigned product interfaces.\n"
    content = {"identity": {"name": "Morgan"}, "experience": [
        {"company": "Alpha Co", "role": "Engineer", "bullets": ["Designed product interfaces."]},
        {"company": "Beta Co", "role": "Designer", "bullets": ["Built Python services."]}]}
    expect(ResumeBoundaryError, lambda: validate_extraction(extraction(content, text), text))


def test_duplicate_import_keeps_ids_and_does_not_reactivate_retired_facts():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        importer = CareerImporter(store, extract_content=lambda _: extraction())
        first = importer.import_text(TEXT)
        content = copy.deepcopy(first["content"])
        fid = content["skills"][0]["items"][1]["fact_id"]
        content["skills"][0]["items"][1]["retired"] = True
        store.save_draft(content, expected_revision_id=first["revision_id"])
        second = importer.import_text(TEXT)
        assert second["content"]["experience"][0]["entry_id"] == first["content"]["experience"][0]["entry_id"]
        assert len(second["content"]["experience"][0]["bullets"]) == 1
        assert second["content"]["skills"][0]["items"][1] == {"fact_id": fid, "text": "SQL", "retired": True}
        assert any(r["kind"] == "duplicate_fact" for r in second["provenance"]["import_review"])


def test_import_conflicts_preserve_current_contacts_for_explicit_review():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        store.save_draft({"identity": {"name": "Morgan Example", "email": "new@example.test"}})
        draft = CareerImporter(store, extract_content=lambda _: extraction()).import_text(TEXT)
        assert draft["content"]["identity"]["email"] == "new@example.test"
        review = draft["provenance"]["import_review"]
        assert any(r["kind"] == "conflicting_field" and r["incoming"] == "morgan@example.test" for r in review)
        span = next(s for s in draft["provenance"]["source_spans"] if s["path"] == "/identity/email")
        assert span["applied"] is False


def test_import_replay_skips_model_and_handles_changed_head():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        calls = []
        importer = CareerImporter(store, extract_content=lambda text: calls.append(text) or extraction())
        first = importer.import_file("resume.txt", TEXT.encode(), idempotency_key="file1")
        store.save_draft(first["content"], expected_revision_id=first["revision_id"])
        assert importer.import_file("resume.txt", TEXT.encode(), idempotency_key="file1") == first
        assert len(calls) == 1
        expect(ResumeConflictError, lambda: importer.import_file("resume.txt", (TEXT + "extra").encode(), idempotency_key="file1"))
        expect(ResumeConflictError, lambda: importer.import_text(TEXT, expected_revision_id=None))
        assert len(calls) == 1


def test_pdf_docx_adapters_are_reused_and_replay_does_not_extract_again():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        class Pdf:
            calls = 0
            def extract(self, data):
                self.calls += 1
                return SimpleNamespace(logical_text=TEXT, parser="pypdf", parser_version="test")
        class Docx:
            calls = 0
            def extract(self, descriptor, data):
                self.calls += 1
                assert descriptor.name == "resume.docx"
                return SimpleNamespace(sanitized_text=TEXT, truncated=False)
        pdf, docx = Pdf(), Docx()
        importer = CareerImporter(store, extract_content=lambda _: extraction(), pdf_extractor=pdf, attachment_extractor=docx)
        first = importer.import_file("resume.pdf", b"%PDF-1.7\nfake\n%%EOF", idempotency_key="pdf1")
        assert importer.import_file("resume.pdf", b"%PDF-1.7\nfake\n%%EOF", idempotency_key="pdf1") == first
        assert pdf.calls == 1
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        importer.import_file("resume.docx", data.getvalue())
        assert docx.calls == 1
        expect(ResumeLabError, lambda: importer.import_file("../resume.txt", b"x"))
        expect(ResumeLabError, lambda: importer.import_file("resume.txt", b"x" * (MAX_IMPORT_BYTES + 1)))
        expect(ResumeLabError, lambda: importer.import_file("resume.txt", b"\xff"))


def test_existing_standard_seeds_without_model_and_never_imports_synthetic_claims():
    with tempfile.TemporaryDirectory() as directory:
        store = CareerStore(Path(directory) / "resume.db")
        output = extraction()
        version = {"version_id": "version_1", "standard_id": "standard_1", "plain_text": TEXT,
            "normalized_content": CONTENT, "claims": [{"claim_id": "source1", "origin": "user_attested"}],
            "import_metadata": {"fixed_fields": [], "normalization_claims": [
                {"path": r["path"], "source_start": r["start"], "source_end": r["end"], "claim_id": "source" + str(i)}
                for i, r in enumerate(output["source_spans"])]}}
        importer = CareerImporter(store)
        first = importer.seed_standard(version, idempotency_key="seed1")
        assert first == importer.seed_standard(version, idempotency_key="seed1")
        assert first["provenance"]["source"]["version_id"] == "version_1"
        version["claims"][0]["origin"] = "synthetic_generated"
        expect(ResumeBoundaryError, lambda: importer.seed_standard(version))


def test_portable_json_restores_draft_facts_without_importing_approval():
    with tempfile.TemporaryDirectory() as directory:
        original = CareerStore(Path(directory) / "original.db")
        revision = original.save_draft(CONTENT)
        original.approve_revision(revision["revision_id"])
        exported = json.dumps(original.export_profile()).encode()
        restored = CareerStore(Path(directory) / "restored.db")
        result = CareerImporter(restored).import_file("career.json", exported, idempotency_key="json1")
        assert restored.get_profile()["approved"] is None
        assert not restored.is_approved(result["revision_id"])
        assert result["content"]["experience"][0]["bullets"][0]["text"] == CONTENT["experience"][0]["bullets"][0]
        assert result["provenance"]["attestation"] == "pending_user_review"


def test_json_source_pointers_survive_retired_entries_and_fact_objects():
    with tempfile.TemporaryDirectory() as directory:
        original = CareerStore(Path(directory) / "original.db")
        first = original.save_draft(CONTENT)
        content = copy.deepcopy(first["content"])
        content["experience"][0]["retired"] = True
        second = original.save_draft(content, expected_revision_id=first["revision_id"])
        exported = original.export_profile()
        restored = CareerStore(Path(directory) / "restored.db")
        result = CareerImporter(restored).import_file("career.json", json.dumps(exported).encode())
        assert result["content"]["experience"][0]["retired"] is True
        span = next(s for s in result["provenance"]["source_spans"] if s["path"] == "/experience/0/bullets/0")
        assert span["source_pointer"] == "/profile/draft/content/experience/0/bullets/0/text"
        assert span["text"] == second["content"]["experience"][0]["bullets"][0]["text"]


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} career import tests)")
