#!/usr/bin/env python3
"""Release packaging includes only committed runtime code, never working state."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile


spec = importlib.util.spec_from_file_location("aws_release_package", Path(__file__).parents[1] / "scripts/aws-release-package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def repository(root: Path) -> str:
    git(root, "init", "--quiet")
    for name in ("compose.cloud.yaml", "job_search/aws_ops.py", "scripts/job-search-ops",
                 "job_search/worker.py", "job_search/web/app.js", "compose.hermes.yaml",
                 "compose.applications.yaml", "job_search/secret.txt"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("committed runtime\n")
    (root / "scripts/job-search-ops").chmod(0o755)
    (root / "deploy").mkdir(exist_ok=True)
    (root / "deploy/release-policy.json").write_text(json.dumps({"version": 1, "schema_compatibility": "reviewed-db-v1", "test_baseline_sha": "0" * 40, "predecessor": None}))
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--quiet", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def evidence(source: str) -> dict:
    return {"schema_version": 1, "passed": True, "runtime": "docker", "source_sha": source, "baseline_sha": "0" * 40, "rollback_passed": False}


def metadata(source: str) -> dict:
    image = "123456789012.dkr.ecr.us-east-2.amazonaws.com/career/app@sha256:" + "a" * 64
    return dict(source_sha=source, release_id=source + "-1", app_image=image, hermes_image=image,
                hermes_base_image=image, tectonic_version="tectonic 0.15.0",
                schema_compatibility="reviewed-db-v1")


def test_package_uses_commit_blobs_preserves_modes_and_excludes_private_files() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = repository(root)
        (root / "job_search/worker.py").write_text("dirty local credentials must not enter\n")
        (root / "job_search/new.py").write_text("untracked data must not enter\n")
        result = package.build(root, root / "output", metadata(source), evidence=evidence(source))
        raw = (root / "output/release.tar.gz").read_bytes()
        assert result["bundle_sha256"] == hashlib.sha256(raw).hexdigest()
        with tarfile.open(root / "output/release.tar.gz") as archive:
            names = set(archive.getnames())
            assert "job_search/worker.py" in names and "compose.hermes.yaml" in names
            assert "compose.applications.yaml" in names
            assert not names.intersection({"job_search/secret.txt", "job_search/history.db", ".env", "personal-resume.pdf", "job_search/new.py"})
            assert archive.extractfile("job_search/worker.py").read() == b"committed runtime\n"
            assert archive.getmember("scripts/job-search-ops").mode == 0o755
            assert all(member.isfile() for member in archive.getmembers())
            internal = json.load(archive.extractfile("release.json"))
            assert "bundle_sha256" not in internal
            assert result == {**internal, "bundle_sha256": result["bundle_sha256"]}
        package.build(root, root / "output2", metadata(source), evidence=evidence(source))
        assert raw == (root / "output2/release.tar.gz").read_bytes()


def test_cost_monitor_code_and_units_are_in_the_release_allowlist() -> None:
    for name in ("job_search/cost_snapshot.py", "job_search/cost_collector.py",
                 "deploy/aws/job-search-costs.service", "deploy/aws/job-search-costs.timer",
                 "Dockerfile.codex-review", "Dockerfile.codex-review.dockerignore",
                 "job_search/review_host.py", "job_search/job_reviews/reviewer_rubric.md",
                 "deploy/aws/job-search-review.service", "deploy/aws/job-search-review.timer"):
        assert package.allowed(name), name
    for name in ("deploy/aws/billing-secret.key", "deploy/aws/snapshot.json",
                 "job_search/openrouter-api-key"):
        assert not package.allowed(name), name


def test_symlinks_and_unpinned_images_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = repository(root)
        (root / "job_search/unsafe.py").symlink_to("../../sensitive.py")
        git(root, "add", "job_search/unsafe.py")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--quiet", "-m", "unsafe")
        unsafe_source = git(root, "rev-parse", "HEAD")
        for values in (metadata(unsafe_source), {**metadata(source), "app_image": "app:latest"},
                       {**metadata(source), "release_id": "../../escape"},
                       {**metadata(source), "schema_compatibility": ""}):
            try:
                package.build(root, root / "output", values, evidence=evidence(values["source_sha"]))
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe release accepted")


def test_committed_personal_artifacts_block_release() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repository(root)
        (root / "personal-resume.pdf").write_bytes(b"%PDF-private")
        git(root, "add", "personal-resume.pdf")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--quiet", "-m", "private")
        source = git(root, "rev-parse", "HEAD")
        try:
            package.build(root, root / "output", metadata(source), evidence=evidence(source))
        except ValueError as error:
            assert "private artifacts" in str(error)
        else:
            raise AssertionError("private resume entered a release")
        assert not (root / "output").exists()


if __name__ == "__main__":
    test_package_uses_commit_blobs_preserves_modes_and_excludes_private_files()
    test_cost_monitor_code_and_units_are_in_the_release_allowlist()
    test_symlinks_and_unpinned_images_fail_closed()
    test_committed_personal_artifacts_block_release()
    print("ok (4 release packaging security tests)")
