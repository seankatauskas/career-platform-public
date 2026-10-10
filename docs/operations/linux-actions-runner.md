# Linux checks and AWS releases on the Mac mini

The private repository's `AWS infrastructure checks / offline` job runs in a
dedicated Ubuntu 24.04 Lima VM on the Mac mini. The existing macOS runner handles
application/browser and Terraform checks. Private Linux checks and AWS workflows use this runner without a hosted fallback.

The guest uses an ARM64 Linux kernel and rootful Docker. Rosetta executes the
existing Linux AMD64 application, reviewer and predecessor images. Source
checkouts, Unix sockets, SQLite databases, fixture files and Docker storage live
on the guest's Linux disk. No Mac home directory or source checkout is mounted.
Docker Desktop and its existing images are separate from this VM.

The workflow retains every container probe, `--require-host-runtime`,
`--require-linux-host`, backup/restore check and predecessor transition check.
It uses a fresh Python virtual environment per job. The 45-minute job limit allows
for initial image builds under translation; it does not skip slow checks.
The root-only probe disables Python bytecode writes with `-B`, so it cannot leave
root-owned caches that prevent the next job from cleaning its checkout.
The GitHub runner is ARM64 and has honest architecture labels even though the
tested images remain AMD64. These checks establish Linux behavior under Rosetta,
not native x86 performance or AWS host/disk behavior.

## Provision the guest

Install Lima 2.2+ with Homebrew, then run from the source checkout:

```sh
brew install lima
limactl validate examples/linux-check-runner.lima.yaml
limactl start --tty=false --name=career-platform-linux examples/linux-check-runner.lima.yaml
```

The checked-in template allocates six CPUs, 16 GiB of RAM and a 100 GiB disk. It
pins the Ubuntu cloud image by SHA-256, installs Docker Engine, Buildx, Compose,
Python 3.12 and runner prerequisites from Ubuntu's package repositories, and
enables Rosetta's binary-format handler. Rosetta must be installed on the Mac.
The guest can reach the network for package downloads and GitHub; application
ports are not automatically forwarded to the Mac.

Verify the guest:

```sh
limactl shell career-platform-linux uname -sm
limactl shell career-platform-linux docker info
limactl shell career-platform-linux docker compose version
limactl shell career-platform-linux cat /proc/sys/fs/binfmt_misc/rosetta
```

## Register and start the runner

In the private repository's **Settings → Actions → Runners → New self-hosted
runner**, select **Linux / ARM64**. Follow GitHub's current download and checksum
verification instructions inside the guest. Use a directory on the guest disk,
such as `~/actions-runner`, with these settings:

- Name: `career-platform-linux-mini`.
- Additional label: `career-platform-linux`.
- Default labels: `self-hosted`, `Linux`, `ARM64`.
- Work directory: `_work` under the runner installation.
- Automatic runner updates: enabled.

Keep registration credentials in the registration process; never put them in the
template, repository, or logs. Install and start the runner's systemd service
using `sudo ./svc.sh install` and `sudo ./svc.sh start` from its guest directory.
Run the service as the guest's normal user, which belongs to the Docker group and
has passwordless guest sudo for the existing root-only acceptance probe. The
machine is dedicated to trusted private-repository code; fork pull requests do
not route to it. No permanent AWS credentials are installed. Production jobs obtain short-lived
credentials through the existing GitHub OIDC role and production environment.

On the Mac, enable VM startup at login:

```sh
limactl autostart enable career-platform-linux
```

This user-level registration requires signing in after a Mac reboot. The guest's
systemd runner starts when the VM boots. Keep the Mac awake and connected.
Stopping the VM queues Linux jobs until it returns; there is no paid hosted
fallback.

## Verification and maintenance

Use a pull request to exercise the actual GitHub job. Confirm `runner_name` is
`career-platform-linux-mini` and inspect its uploaded `system-validation-*`
artifact, including the Compose and transition receipts. Native macOS success
does not replace this Linux job.

```sh
limactl list career-platform-linux
limactl shell career-platform-linux df -h / /var/lib/docker
limactl shell career-platform-linux docker system df
```

One runner executes one job at a time. Docker layers persist for later builds.
Monitor disk use and clean only this guest's unused images/cache between jobs;
do not prune the Mac's Docker Desktop state. Update Ubuntu packages and Lima as
part of runner maintenance. Before stopping or restarting, confirm the GitHub
runner is idle.

```sh
limactl stop career-platform-linux
limactl start career-platform-linux
```

To revert routing, change this job back to `ubuntu-24.04` and restore its hosted
Python setup; that requires an available GitHub-hosted allowance. To retire the
guest, remove its GitHub registration and disable autostart before deleting its
VM disk. Do not delete an active runner.

## Production workflows

Release preparation, deployment, release status and Terraform operations use this
Linux runner. Daily public export uses the existing macOS runner. The public
repository's own checks retain standard hosted runners; private workflows have
no hosted fallback. Keep the Mac and Linux VM online when dispatching a release.
A queued status workflow shares the single Linux runner and can wait behind a
deployment; direct read-only SSM status remains available while that job runs.

Manual dispatch, owner/main restrictions, the production environment, scoped OIDC
roles, immutable manifests, predecessor checks and SSM installation remain intact.
Runner migration does not run Terraform apply or enable recurring model work.
AWS trust continues to match the repository and production environment; no IAM
policy expansion is required. The VM now runs trusted production jobs as well as
trusted private checks; do not grant untrusted contributors access to this runner.

`scripts/prepare-aws-runner.sh` creates a private per-job directory for Python,
the checksum-pinned ARM64 AWS CLI 2.37.6, empty AWS configuration files, Git/GitHub
configuration and Docker logins. Each OIDC step clears inherited credentials.
The workflow removes its own directory even on failure and never deletes the
runner's ordinary Docker configuration. No long-lived AWS keys are installed.
Use GitHub CLI 2.97.0 on the VM; Ubuntu's 2.45 package lacks `gh api --slurp`
and leaves the workflow-activity part of release status unknown. The checked-in
template installs the official ARM64 Debian package with a pinned SHA-256.
For an existing VM, run that template's download, checksum and `dpkg` commands.
Tool setup runs before AWS authentication.

All production images, including the reviewer, explicitly target linux/amd64 for
EC2. Linux checks still run under Rosetta. Preparation retains the native worker,
Compose, upgrade and rollback probes before publishing images; deployment verifies
the selected manifest before installing it. Validate the first migrated release by
checking the workflow's runner identity, OIDC assumption, prepared receipt and
successful SSM deployment. GitHub artifact storage and AWS usage are separate from
hosted-runner compute.

References: [Lima Rosetta support](https://lima-vm.io/docs/config/multi-arch/),
[Lima automatic startup](https://lima-vm.io/docs/usage/autostart/), and
[GitHub runner registration](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).
