#!/usr/bin/env python3
"""Validate Hermes independently and retain the exact local image for publication."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def validated_image_id(receipt, *, source, base):
    if (receipt.get("passed") is not True or receipt.get("source_sha") != source
            or receipt.get("hermes_base_image") != base
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(receipt.get("image_id", "")))):
        raise ValueError("Hermes validation does not match this source and pinned base")
    return receipt["image_id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validated-image-id", action="store_true")
    args = parser.parse_args()
    base = os.environ["HERMES_BASE_IMAGE"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}", base):
        raise ValueError("Hermes base must be pinned by digest")
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    receipt_path = ROOT / ".cache/hermes-healthcheck.json"
    if args.validated_image_id:
        print(validated_image_id(json.loads(receipt_path.read_text()), source=source, base=base))
        return 0

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.unlink(missing_ok=True)
    image_path = ROOT / ".cache/hermes-candidate.id"
    image_path.unlink(missing_ok=True)
    subprocess.run(["docker", "build", "--platform", "linux/amd64", "--file", "Dockerfile.hermes",
                    "--build-arg", "HERMES_BASE_IMAGE=" + base, "--iidfile", str(image_path), "."],
                   cwd=ROOT, check=True)
    image_id = image_path.read_text().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("Hermes build did not return an immutable image ID")
    subprocess.run([sys.executable, "scripts/hermes-healthcheck-acceptance.py", "--image", image_id],
                   cwd=ROOT, check=True)
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("image_id") != image_id:
        raise ValueError("Hermes native checks did not validate the built image")
    receipt.update(source_sha=source, hermes_base_image=base)
    validated_image_id(receipt, source=source, base=base)
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
