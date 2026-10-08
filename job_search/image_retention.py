"""Conservative local Docker image retention after a completed deployment.

The caller holds the operations lock. Only digests named by older installed
releases are eligible; ECR and application data are never mutated.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Callable


IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
ECR_IMAGE = re.compile(
    r"(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9/_-]+)@(?P<digest>sha256:[a-f0-9]{64})\Z"
)
IMAGE_KEYS = ("app_image", "hermes_image", "reviewer_image", "hermes_base_image")
IMAGE_FORMAT = '{"Id":{{json .Id}},"RepoTags":{{json .RepoTags}},"RepoDigests":{{json .RepoDigests}}}'


def _refs(manifest: dict) -> set[str]:
    return {manifest[key] for key in IMAGE_KEYS if manifest.get(key)}


def _ids(raw: str) -> set[str]:
    values = set(raw.split())
    if any(not IMAGE_ID.fullmatch(value) for value in values):
        raise ValueError("invalid Docker image identity")
    return values


def _inventory(run: Callable) -> list[dict]:
    ids = sorted(_ids(run(["docker", "image", "ls", "--quiet", "--no-trunc"])))
    images = []
    for offset in range(0, len(ids), 100):
        raw = run(["docker", "image", "inspect", "--format", IMAGE_FORMAT, *ids[offset:offset + 100]])
        images.extend(json.loads(line) for line in raw.splitlines())
    if {image["Id"] for image in images} != set(ids):
        raise ValueError("incomplete Docker image inventory")
    for image in images:
        for key in ("RepoTags", "RepoDigests"):
            values = image[key]
            if values is not None and (not isinstance(values, list) or not all(isinstance(v, str) for v in values)):
                raise ValueError("invalid Docker image references")
    return images


def _container_images(run: Callable) -> set[str]:
    containers = run(["docker", "ps", "--all", "--quiet", "--no-trunc"]).split()
    if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in containers):
        raise ValueError("invalid Docker container inventory")
    images = set()
    for offset in range(0, len(containers), 100):
        values = run(["docker", "container", "inspect", "--format", "{{.Image}}", *containers[offset:offset + 100]])
        if len(values.split()) != len(containers[offset:offset + 100]):
            raise ValueError("incomplete Docker container inventory")
        images.update(_ids(values))
    return images


def _available(refs: set[str], region: str, aws: Callable) -> set[str]:
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for ref in sorted(refs):
        match = ECR_IMAGE.fullmatch(ref)
        if not match or match["region"] != region:
            continue
        groups.setdefault((match["account"], match["repository"]), []).append((ref, match["digest"]))
    available = set()
    for (account, repository), entries in groups.items():
        for offset in range(0, len(entries), 100):
            batch = entries[offset:offset + 100]
            value = json.loads(aws("ecr", "batch-get-image", "--registry-id", account,
                                   "--repository-name", repository, "--image-ids",
                                   json.dumps([{"imageDigest": digest} for _, digest in batch])))
            found = {item["imageId"]["imageDigest"] for item in value["images"]}
            available.update(ref for ref, digest in batch if digest in found)
    return available


def prune(c: dict, *, current: Path, previous: Path | None,
          read_manifest: Callable, run: Callable, aws: Callable) -> dict:
    """Remove only verified older release images; preserve aliases and containers."""
    protected = _refs(read_manifest(current))
    if previous is None:
        return {"status": "retained", "removed_refs": 0}
    protected.update(_refs(read_manifest(previous)))
    repositories = {ref.split("@", 1)[0] for ref in protected}
    older = set()
    # Preserve newer/staged directories, including forward releases after rollback.
    # Filesystem timestamps only narrow eligibility; they never authorize an
    # unknown image or substitute for current/previous release protection.
    cutoff = min(current.stat().st_mtime_ns, previous.stat().st_mtime_ns)
    for path in current.parent.iterdir():
        # The installer keeps its .install-* staging directory until deploy
        # returns. It is not an installed release and must not block retention.
        if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
            continue
        refs = _refs(read_manifest(path))
        if path in (current, previous) or path.stat().st_mtime_ns >= cutoff:
            protected.update(refs)
        else:
            older.update(refs)
    older = {ref for ref in older - protected if ref.split("@", 1)[0] in repositories}
    if not older:
        return {"status": "retained", "removed_refs": 0}
    images = _inventory(run)
    protected_ids = _container_images(run)
    for image in images:
        if protected.intersection((image["RepoDigests"] or []) + (image["RepoTags"] or [])):
            protected_ids.add(image["Id"])
    candidates = []
    for image in images:
        refs = set(image["RepoDigests"] or [])
        # Some Docker/containerd versions repeat digest references in RepoTags.
        # Actual custom tags or foreign aliases make the entire image ineligible.
        if (image["Id"] not in protected_ids and refs and refs <= older
                and set(image["RepoTags"] or []) <= refs):
            candidates.append((image["Id"], refs))
    available = _available(set().union(*(refs for _, refs in candidates)), c["aws_region"], aws)
    removed = 0
    unavailable = 0
    for identity, refs in candidates:
        if not refs <= available:
            unavailable += 1
            continue
        # A reviewer may start after the first inventory. Never force removal;
        # Docker also rejects removal when a container starts after this check.
        if identity in _container_images(run):
            continue
        for ref in sorted(refs):
            run(["docker", "image", "rm", "--no-prune", ref])
            removed += 1
    return {"status": "retained", "removed_refs": removed,
            "preserved_unavailable_images": unavailable}
