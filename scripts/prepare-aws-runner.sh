#!/usr/bin/env bash
# Ephemeral tools and configuration for trusted production jobs on the Linux VM.
set -euo pipefail
test "$(uname -s)" = Linux
test "$(uname -m)" = aarch64
python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
command -v gh >/dev/null
command -v jq >/dev/null
umask 077
career_runner_root=$(mktemp -d "${RUNNER_TEMP:?}/career-aws.XXXXXXXX")
printf 'CAREER_RUNNER_ROOT=%s\n' "$career_runner_root" >> "${GITHUB_ENV:?}"
mkdir "$career_runner_root/docker" "$career_runner_root/xdg" "$career_runner_root/gh"
touch "$career_runner_root/aws-config" "$career_runner_root/aws-credentials" "$career_runner_root/gitconfig"
printf '%s\n' \
  "DOCKER_CONFIG=$career_runner_root/docker" \
  "AWS_CONFIG_FILE=$career_runner_root/aws-config" \
  "AWS_SHARED_CREDENTIALS_FILE=$career_runner_root/aws-credentials" \
  'AWS_EC2_METADATA_DISABLED=true' 'AWS_PAGER=' \
  "GH_CONFIG_DIR=$career_runner_root/gh" \
  "GIT_CONFIG_GLOBAL=$career_runner_root/gitconfig" 'GIT_CONFIG_NOSYSTEM=1' \
  "XDG_CONFIG_HOME=$career_runner_root/xdg" >> "$GITHUB_ENV"
python3 -m venv "$career_runner_root/venv"
curl --fail --silent --show-error --location --retry 3 \
  https://awscli.amazonaws.com/awscli-exe-linux-aarch64-2.37.6.zip \
  --output "$career_runner_root/awscli.zip"
printf '%s  %s\n' 06b572cfd4b0397145a8173fb3456a6e6f0bea1bae50e9162a56be80a1e19618 "$career_runner_root/awscli.zip" | sha256sum --check
unzip -q "$career_runner_root/awscli.zip" -d "$career_runner_root"
"$career_runner_root/aws/install" --install-dir "$career_runner_root/aws-cli" --bin-dir "$career_runner_root/bin" >/dev/null
printf '%s\n' "$career_runner_root/bin" "$career_runner_root/venv/bin" >> "${GITHUB_PATH:?}"
"$career_runner_root/bin/aws" --version
