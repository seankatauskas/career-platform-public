#!/usr/bin/env python3
"""Verify an immutable, tested release before a separately requested installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from .aws_ops import IMAGE
from .release_policy import validate_evidence, validate_policy


def validate_selection(release_id: str, sha256: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}-[0-9]+", release_id):
        raise ValueError("select the exact release ID from a prepared release receipt")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ValueError("select the manifest SHA-256 from the same receipt")


def verify(raw: bytes, release_id: str, sha256: str) -> dict:
    validate_selection(release_id, sha256)
    if len(raw) > 64 * 1024 or hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("published manifest does not match the selected checksum")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or (
        manifest.get("version") != 1
        or manifest.get("operations_protocol") != 1
        or manifest.get("release_id") != release_id
        or manifest.get("source_sha") != release_id[:40]
    ):
        raise ValueError("published manifest does not identify the selected hardened release")
    for field in ("app_image", "hermes_image", "hermes_base_image"):
        if not IMAGE.fullmatch(str(manifest.get(field, ""))):
            raise ValueError("release images must be pinned by digest")
    if "reviewer_image" in manifest and (not IMAGE.fullmatch(str(manifest["reviewer_image"])) or manifest["reviewer_image"].split("@", 1)[0] != manifest["app_image"].split("@", 1)[0]):
        raise ValueError("reviewer image must use the pinned application repository")
    if not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("bundle_sha256", ""))):
        raise ValueError("release bundle checksum is missing")
    policy = validate_policy(manifest.get("release_policy"))
    validate_evidence(policy, manifest["source_sha"], manifest.get("transition_validation"))
    if manifest.get("schema_compatibility") != policy["schema_compatibility"]:
        raise ValueError("release compatibility differs from its reviewed policy")
    return {
        "release_id": release_id, "manifest_sha256": sha256,
        "source_sha": manifest["source_sha"], "status": "prepared",
        **({"reviewer_image": manifest["reviewer_image"]} if "reviewer_image" in manifest else {}),
        "expected_predecessor": policy["predecessor"],
        "next_action": "Run Deploy prepared AWS release when ready for the brief maintenance pause.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selection", "verify"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        validate_selection(args.release_id, args.sha256)
        if args.command == "selection":
            return
        if args.manifest is None:
            parser.error("verify requires --manifest")
        with args.manifest.open("rb") as stream:
            result = verify(stream.read(64 * 1024 + 1), args.release_id, args.sha256)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    content = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write("### Prepared release\n\nBuilding this release does not install it.\n\n```json\n" + content + "```\n")
    print(content, end="")


if __name__ == "__main__":
    main()
