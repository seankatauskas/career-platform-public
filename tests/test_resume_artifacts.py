#!/usr/bin/env python3
"""Offline security checks for content-addressed resume PDF storage."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from job_search.resume_lab.artifacts import (
    ArtifactIntegrityError,
    ArtifactNamespace,
    ArtifactNotFoundError,
    ArtifactRepositoryError,
    ArtifactSecurityError,
    InvalidArtifactError,
    ResumePdfArtifactRepository,
    VerifiedResumePdf,
)


PDF_ONE = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
PDF_TWO = b"%PDF-1.7\n1 0 obj<</Type/Pages/Count 0>>endobj\n%%EOF\n"


def expect(error_type, operation, path_fragment: str = ""):
    try:
        operation()
    except error_type as exc:
        if path_fragment:
            assert path_fragment not in str(exc)
        return exc
    raise AssertionError(f"expected {error_type.__name__}")


def artifact_path(root: Path, locator: str) -> Path:
    # Test-only inspection: production callers retain the locator and let the
    # repository resolve it beneath its private root.
    return root.joinpath(*locator.split("/"))


def test_content_addressing_separates_namespaces_and_sets_private_modes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "resume-artifacts"
        repository = ResumePdfArtifactRepository(root)
        real = repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL)
        duplicate = repository.write_pdf(PDF_ONE, "real")
        research = repository.write_pdf(PDF_ONE, ArtifactNamespace.RESEARCH)

        digest = hashlib.sha256(PDF_ONE).hexdigest()
        assert real == duplicate
        assert real.sha256 == real.pdf_sha256 == digest
        assert real.managed_relative_path == (
            f"real/{digest[:2]}/{digest[2:4]}/{digest}.pdf"
        )
        assert research.managed_relative_path.startswith("research/")
        assert real.managed_relative_path != research.managed_relative_path
        assert len(tuple(root.rglob("*.pdf"))) == 2

        for managed_directory in (
            root,
            *[item for item in root.rglob("*") if item.is_dir()],
        ):
            assert stat.S_IMODE(os.lstat(managed_directory).st_mode) == 0o700
        for stored_file in (item for item in root.rglob("*") if item.is_file()):
            assert stat.S_IMODE(os.lstat(stored_file).st_mode) == 0o600

        loaded = repository.read_pdf(real.managed_relative_path, real.sha256)
        assert loaded.content == PDF_ONE
        assert loaded.namespace is ArtifactNamespace.REAL
        assert loaded.content_type == "application/pdf"
        assert all("path" not in item.name for item in fields(VerifiedResumePdf))
        assert str(root) not in repr(repository)


def test_locators_are_canonical_bounded_and_cannot_cross_roots() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        saved = repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL)
        digest = saved.sha256

        invalid = (
            f"../real/{digest}.pdf",
            f"/real/{digest}.pdf",
            f"real\\{digest}.pdf",
            f"real/ff/{digest[2:4]}/{digest}.pdf",
            f"real/{digest[:2]}/{digest[2:4]}/../{digest}.pdf",
            "real/short.pdf",
        )
        for locator in invalid:
            expect(
                InvalidArtifactError,
                lambda value=locator: repository.read_pdf(value, digest),
                str(root),
            )
        expect(
            InvalidArtifactError,
            lambda: repository.read_pdf(saved.managed_relative_path, digest.upper()),
            str(root),
        )
        expect(
            ArtifactIntegrityError,
            lambda: repository.read_pdf(saved.managed_relative_path, "0" * 64),
            str(root),
        )
        crossed = saved.managed_relative_path.replace("real/", "research/", 1)
        expect(
            ArtifactNotFoundError,
            lambda: repository.read_pdf(crossed, digest),
            str(root),
        )


def test_reads_reject_tampering_symlinks_nonregular_files_and_hardlinks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "artifacts"
        repository = ResumePdfArtifactRepository(root)

        tampered = repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL)
        tampered_path = artifact_path(root, tampered.managed_relative_path)
        replacement = PDF_ONE.replace(b"Catalog", b"Catxlog")
        assert len(replacement) == len(PDF_ONE)
        tampered_path.write_bytes(replacement)
        os.chmod(tampered_path, 0o600)
        expect(
            ArtifactIntegrityError,
            lambda: repository.read_pdf(
                tampered.managed_relative_path, tampered.sha256
            ),
            str(root),
        )

        linked = repository.write_pdf(PDF_TWO, ArtifactNamespace.REAL)
        linked_path = artifact_path(root, linked.managed_relative_path)
        extra_link = root / "outside-link.pdf"
        os.link(linked_path, extra_link)
        expect(
            ArtifactSecurityError,
            lambda: repository.read_pdf(linked.managed_relative_path, linked.sha256),
            str(root),
        )
        extra_link.unlink()

        linked_path.chmod(0o644)
        expect(
            ArtifactSecurityError,
            lambda: repository.read_pdf(linked.managed_relative_path, linked.sha256),
            str(root),
        )
        linked_path.chmod(0o600)

        outside = Path(directory) / "outside.pdf"
        outside.write_bytes(PDF_TWO)
        linked_path.unlink()
        linked_path.symlink_to(outside)
        expect(
            ArtifactSecurityError,
            lambda: repository.read_pdf(linked.managed_relative_path, linked.sha256),
            str(root),
        )
        expect(
            ArtifactSecurityError,
            lambda: repository.write_pdf(PDF_TWO, ArtifactNamespace.REAL),
            str(root),
        )

        linked_path.unlink()
        linked_path.mkdir(mode=0o700)
        expect(
            ArtifactSecurityError,
            lambda: repository.read_pdf(linked.managed_relative_path, linked.sha256),
            str(root),
        )


def test_repository_rejects_symlinked_roots_and_compromised_directories() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        outside = base / "outside"
        outside.mkdir(mode=0o700)
        linked_root = base / "linked-root"
        linked_root.symlink_to(outside, target_is_directory=True)
        expect(
            ArtifactSecurityError,
            lambda: ResumePdfArtifactRepository(linked_root),
            str(linked_root),
        )

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "artifacts"
        root.mkdir(mode=0o700)
        outside = base / "outside"
        outside.mkdir(mode=0o700)
        (root / "real").symlink_to(outside, target_is_directory=True)
        expect(
            ArtifactSecurityError,
            lambda: ResumePdfArtifactRepository(root),
            str(root),
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        saved = repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL)
        os.chmod(root / "real", 0o755)
        expect(
            ArtifactSecurityError,
            lambda: repository.read_pdf(saved.managed_relative_path, saved.sha256),
            str(root),
        )

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        digest = hashlib.sha256(PDF_ONE).hexdigest()
        outside = base / "outside"
        outside.mkdir(mode=0o700)
        (root / "real" / digest[:2]).symlink_to(outside, target_is_directory=True)
        expect(
            ArtifactSecurityError,
            lambda: repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL),
            str(root),
        )

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        outside = base / "outside-lock"
        outside.write_bytes(b"")
        os.chmod(outside, 0o600)
        (root / ".write.lock").unlink()
        (root / ".write.lock").symlink_to(outside)
        expect(
            ArtifactSecurityError,
            lambda: repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL),
            str(root),
        )


def test_repository_pins_root_identity_across_operations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        saved = repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL)

        displaced = base / "displaced-artifacts"
        root.rename(displaced)
        replacement = ResumePdfArtifactRepository(root)
        replacement.write_pdf(PDF_ONE, ArtifactNamespace.REAL)

        expect(
            ArtifactSecurityError,
            lambda: repository.read_pdf(saved.managed_relative_path, saved.sha256),
            str(root),
        )


def test_atomic_failure_leaves_no_partial_target_or_temporary_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        with patch(
            "job_search.resume_lab.artifacts.os.link",
            side_effect=OSError("private filesystem detail"),
        ):
            error = expect(
                ArtifactRepositoryError,
                lambda: repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL),
                str(root),
            )
        assert "private filesystem detail" not in str(error)
        assert not tuple(root.rglob("*.pdf"))
        assert not tuple(root.rglob(".resume-pdf-*.tmp"))


def test_concurrent_writers_publish_one_verified_immutable_object() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        results = []
        errors = []

        def write() -> None:
            try:
                local_repository = ResumePdfArtifactRepository(root)
                results.append(
                    local_repository.write_pdf(PDF_ONE, ArtifactNamespace.REAL)
                )
            except Exception as exc:  # surfaced below with the original type
                errors.append(exc)

        threads = [threading.Thread(target=write) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not errors
        assert len(results) == 8 and len(set(results)) == 1
        assert len(tuple((root / "real").rglob("*.pdf"))) == 1
        loaded = repository.read_pdf(
            results[0].managed_relative_path, results[0].sha256
        )
        assert loaded.content == PDF_ONE


def test_non_pdf_inputs_and_unknown_namespaces_fail_before_writing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "artifacts"
        repository = ResumePdfArtifactRepository(root)
        for value in ("%PDF-1.7", b"", b"not a pdf"):
            expect(
                InvalidArtifactError,
                lambda candidate=value: repository.write_pdf(
                    candidate, ArtifactNamespace.REAL  # type: ignore[arg-type]
                ),
                str(root),
            )
        expect(
            InvalidArtifactError,
            lambda: repository.write_pdf(PDF_ONE, "private"),
            str(root),
        )
        assert not tuple(root.rglob("*.pdf"))


def main() -> None:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} resume artifact repository tests)")


if __name__ == "__main__":
    main()
