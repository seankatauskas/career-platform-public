#!/usr/bin/env python3
"""Build a deterministic, data-free release from committed Git blobs."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_search.release_policy import validate_policy, validate_evidence
import tarfile


ROOT_FILES = frozenset({
    "deploy/release-policy.json", "compose.cloud.yaml", "compose.hermes.yaml", "compose.mail.yaml", "compose.chief.yaml", "compose.briefing.yaml", "Dockerfile", "Dockerfile.hermes", "Dockerfile.codex-review", "Dockerfile.codex-review.dockerignore", ".dockerignore", "requirements/cloud.txt",
    "job_search/collection/boards.seed.json", "job_search/job_reviews/reviewer_rubric.md", "scripts/job-search-ops",
    "scripts/job-search-seed",
})
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}")


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(p in {"..", ".git", "__pycache__"} for p in parts):
        return False
    if path in ROOT_FILES:
        return True
    # Filenames provide a second barrier even when a private file was accidentally
    # committed under a runtime directory. This is not a substitute for secret scanning.
    if any(p.startswith(".") or re.search(r"(?:secret|credential|token|private|\.db|\.sqlite|\.pem|\.key)", p, re.I)
           for p in parts):
        return path == "deploy/hermes/cont-init.d/018-job-search-mcp-token"
    if path.startswith("job_search/"):
        return PurePosixPath(path).suffix in {".py", ".html", ".js", ".css", ".tex", ".txt"}
    if path.startswith(("deploy/aws/", "deploy/hermes/")):
        return PurePosixPath(path).suffix in {"", ".py", ".sh", ".yaml", ".yml", ".tftpl", ".service", ".timer"}
    return False


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def build(repo: Path, output: Path, metadata: dict, *, evidence: dict | None = None) -> dict:
    source = metadata.get("source_sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", source):
        raise ValueError("source_sha must identify one commit")
    if not re.fullmatch(re.escape(source) + r"-[0-9]+", metadata.get("release_id", "")):
        raise ValueError("release_id must be source_sha-run_number")
    for key in ("app_image", "hermes_image", "hermes_base_image"):
        if not IMAGE.fullmatch(metadata.get(key, "")):
            raise ValueError("all images must be pinned to SHA256 digests")
    if "reviewer_image" in metadata and (not IMAGE.fullmatch(metadata["reviewer_image"]) or metadata["reviewer_image"].split("@", 1)[0] != metadata["app_image"].split("@", 1)[0]):
        raise ValueError("reviewer image must use the pinned application repository")
    policy = validate_policy(json.loads(git(repo, "show", source + ":deploy/release-policy.json")))
    validate_evidence(policy, source, evidence)
    if "schema_compatibility" in metadata and metadata["schema_compatibility"] != policy["schema_compatibility"]:
        raise ValueError("compatibility override differs from committed policy")
    metadata = {**metadata, "schema_compatibility": policy["schema_compatibility"],
                "operations_protocol": 1, "release_policy": policy, "transition_validation": evidence}
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,100}", metadata.get("tectonic_version", "")):
        raise ValueError("tectonic_version must be explicit")
    if git(repo, "rev-parse", source + "^{commit}").decode().strip() != source:
        raise ValueError("source_sha is not a commit")
    spec = importlib.util.spec_from_file_location("private_guard", Path(__file__).with_name("check-private-files.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    if guard.inspect_index(repo, source):
        raise ValueError("source commit contains private artifacts or credentials")
    manifest = {"version": 1, **metadata}
    files = []
    for record in git(repo, "ls-tree", "-rz", "--full-tree", source).split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = header.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if not allowed(path):
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("release may not contain symlinks or submodules: " + path)
        files.append((path, 0o755 if mode == "100755" else 0o644,
                      git(repo, "cat-file", "blob", object_id)))
    names = {path for path, _, _ in files}
    required = {"compose.cloud.yaml", "job_search/aws_ops.py", "scripts/job-search-ops"}
    if not required <= names:
        raise ValueError("source commit is missing required runtime files")
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / "release.tar.gz"
    if bundle.exists() or (output / "manifest.json").exists():
        raise ValueError("release output already exists")
    files.append(("release.json", 0o644, json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"))
    with bundle.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, mode, content in sorted(files):
                info = tarfile.TarInfo(name)
                info.mode = mode
                info.size = len(content)
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    manifest["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transition-evidence", type=Path, required=True)
    for field in ("release-id", "source-sha", "app-image", "hermes-image", "hermes-base-image",
                  "tectonic-version"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--reviewer-image", required=True)
    args = vars(parser.parse_args())
    repo, output = args.pop("repo"), args.pop("output")
    evidence = json.loads(args.pop("transition_evidence").read_text())
    result = build(repo, output, args, evidence=evidence)
    print(json.dumps({"release_id": result["release_id"], "bundle_sha256": result["bundle_sha256"]}))


if __name__ == "__main__":
    main()
