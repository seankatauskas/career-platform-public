#!/usr/bin/env bash
# All native gates remain sequential within this lane; fixtures own their cleanup.
set -euo pipefail
docker build --platform linux/amd64 --build-arg SOURCE_REVISION="$GITHUB_SHA" --tag career-platform:release-candidate .
docker run --rm career-platform:release-candidate --help
docker build --platform linux/amd64 --file Dockerfile.codex-review --tag career-platform:reviewer-candidate .
reviewer_id=$(docker image inspect career-platform:reviewer-candidate --format '{{.Id}}')
python3 -m tests.probe_codex_review_container --image "$reviewer_id"
python3 -m tests.probe_codex_review_container --image "$reviewer_id" --screening
python3 -m tests.probe_codex_review_container --image "$reviewer_id" --adjudicator
sudo "$(command -v python3)" -B -m tests.probe_old_method_container --image "$reviewer_id" --require-host-runtime
python3 scripts/prepare-acceptance-toolchain.py --output .cache/acceptance-toolchain
python3 scripts/compose-acceptance.py --image career-platform:release-candidate --toolchain-dir .cache/acceptance-toolchain --output .cache/compose-acceptance --require-linux-host
python3 scripts/release-transition-acceptance.py --candidate-image career-platform:release-candidate
