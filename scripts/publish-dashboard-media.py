#!/usr/bin/env python3
"""Copy only verified dashboard media to repository documentation and a portfolio."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
STILLS = {"shortlist.png", "preview.png", "applications.png", "application-history.png",
          "review.png", "messages.png", "reply.png", "documents.png", "career-profile.png",
          "operations.png", "posting-history.png"}


def publish(capture: Path, portfolio: Path | None = None) -> dict:
    capture = capture.resolve()
    receipt = json.loads((capture / "results.json").read_text())
    if not receipt.get("passed") or receipt.get("scenario") != "portfolio":
        raise ValueError("Only a successful portfolio-scenario capture can be published")
    if receipt.get("browserErrors") or receipt.get("requestViolations"):
        raise ValueError("Capture contains browser errors or external requests")
    indexed = {asset["name"]: asset for asset in receipt["assets"]}
    names = STILLS | {"walkthrough.mp4"}
    if not names <= indexed.keys():
        raise ValueError("Capture is missing required media")
    # Validate every input before copying any output. Raw frames, failure
    # screenshots, configuration, receipts and state are never published.
    for name in names:
        source = capture / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Invalid asset: {name}")
        if hashlib.sha256(source.read_bytes()).hexdigest() != indexed[name]["sha256"]:
            raise ValueError(f"Asset changed after capture: {name}")
    copies = []
    for name in sorted(names):
        image = name in STILLS
        destinations = [ROOT / "docs" / ("images" if image else "media") / name]
        if portfolio:
            destinations.append(portfolio.resolve() / "public" / ("images" if image else "videos") / "career-platform" / name)
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(capture / name, destination)
            copies.append({"name": name, "destination": str(destination), "sha256": indexed[name]["sha256"]})
    result = {"sourceRevision": receipt["sourceRevision"], "scenario": receipt["scenario"], "copies": copies}
    (capture / "publish-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=ROOT / ".cache/dashboard-film")
    parser.add_argument("--portfolio", type=Path)
    args = parser.parse_args()
    result = publish(args.capture, args.portfolio)
    print(f"Published {len(result['copies'])} verified media copies")
