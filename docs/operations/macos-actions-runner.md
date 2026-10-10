# Mac mini GitHub Actions runner

Routine private-repository checks run on an Apple Silicon Mac mini. GitHub still
queues jobs and displays results. Only runner computation moves locally; Actions
artifact storage and hosted Linux jobs can still incur charges.

## Work placement

| Work | Runner |
| --- | --- |
| Private application, security, Python and browser suites | Mac mini |
| Terraform formatting, validation and mocked-provider tests | Mac mini |
| Production Compose, reviewer container and release-transition acceptance | Dedicated Linux VM on the Mac mini |
| Release preparation, deployment and Terraform plan/apply | Hosted Ubuntu, manual dispatch |
| Nightly public snapshot publishing | Hosted Ubuntu |
| Public repository application/browser checks | Hosted Ubuntu |

`public-checks.yml` selects the Mac only for `seankatauskas/career-platform`.
The exported public workflow selects Ubuntu, including on public pull requests.
Fork pull requests in the private repository do not run on the Mac. Run only
trusted repository code on this persistent machine: jobs have the permissions of
its macOS account. A separate checkout does not isolate the account's files.
Application checks use temporary Git/XDG configuration so fixture repositories
do not inherit the owner's global ignore rules or Git hooks. CLI fixtures pass
their own temporary runtime configuration.

The private source's application suite runs in `public-checks.yml`;
`aws-checks.yml` retains the Linux container gates without repeating that suite.
See [Linux runner operations](linux-actions-runner.md) for the separate Lima VM.
The public-checks workflow also validates a separately generated public export on
the Mac, preserving the publication guard. Require both workflows' checks before
merging. Release preparation still repeats its own full validation before
publication.

## Installed runner

- Repository: `seankatauskas/career-platform` (private).
- Runner name: `career-platform-m4-mini`.
- Labels: `self-hosted`, `macOS`, `ARM64`, `career-platform`.
- Installation: `~/.local/share/github-actions/career-platform`.
- Job checkouts: the installation's `_work` directory, separate from the developer checkout.
- Service: the current user's `launchd` LaunchAgent, installed with GitHub's `svc.sh`.

The workflow routing changes take effect only after they are committed and pushed
to the branches that GitHub runs. Registering the runner alone does not move jobs.

## Setup on a replacement Mac

1. Install Git, Node 22, and `uv`. The workflows provision Python 3.12 and
   Terraform 1.13.5. Docker and AWS credentials are not needed for these Mac jobs.
   The Mac uses its locally managed `uv`; hosted Ubuntu installs the workflow's
   pinned version. Python dependencies remain pinned by the requirements files.
2. In the private repository, open **Settings → Actions → Runners → New self-hosted
   runner**, choose **macOS / ARM64**, and follow GitHub's download and checksum
   verification instructions. Keep the installation outside the source checkout.
3. Register with the name and labels above. Keep registration credentials out of
   source files, chat, and logs. Leave automatic runner updates enabled.
4. From the runner installation directory, run `./svc.sh install` and
   `./svc.sh start` without `sudo`. The service captures its executable search path;
   ensure Homebrew and Node are available when configuring the runner.
5. Confirm the runner is **Idle** in GitHub and perform the local checks below
   before enabling routing.

Official instructions: [register a runner](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
and [configure its service](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application).

## Local validation

Use an isolated checkout of the intended commit, with no production configuration.

```sh
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
npm ci --prefix extension
npx --prefix extension playwright-core install chromium
uv run --python 3.12 \
  --with-requirements requirements/cloud.txt \
  --with-requirements requirements/demo.txt \
  python scripts/check-system.py --browser
```

Run the exact Terraform checks in `aws-checks.yml` as well. They use
`init -backend=false`, validation and mocked-provider tests; do not substitute a
production `plan` or `apply`. A clean checkout avoids reusing a developer's
initialized backend or local variable files. These checks download providers but
do not need an AWS login.

Production acceptance explicitly requires Linux with native Docker and AMD64
images. Passing the Mac checks does not replace that evidence. Docker Desktop's
development mode is documented in [Compose acceptance](../compose-acceptance.md).

## Availability and maintenance

Keep the Mac connected and prevent system sleep while plugged in; the display can
sleep. The LaunchAgent runs in the logged-in user's session. After reboot, sign
in to that account; do not assume it runs before login or after logout. The setup
does not enable automatic login or change FileVault.

```sh
cd ~/.local/share/github-actions/career-platform
./svc.sh status
./svc.sh stop
./svc.sh start
```

GitHub reports runner status under the repository's Actions settings. Jobs wait
when the Mac is unavailable; there is no automatic hosted fallback. A queued job
fails after 24 hours. Application checks cancel superseded runs on the same ref.
One runner executes one job at a time.

Keep macOS and installed tools updated. Monitor free disk space: Python caches,
Chromium downloads, runner diagnostics and job checkouts persist. Stop the runner
and confirm no job is active before cleaning its own files. Do not clean the
developer checkout or production state as part of runner maintenance. Uploaded
application receipts have seven-day retention.

To revert routing, change only the private Mac jobs back to `ubuntu-24.04` and
commit/push that change. To retire the runner, stop and uninstall the service,
then remove its registration through GitHub's runner settings. Do not delete an
active job's installation.

The private repository's scheduled public snapshot workflow also uses this Mac
runner. It validates the export before receiving the existing publishing key and
removes its temporary key files on exit. The publishing schedule and source
allowlist are unchanged. AWS release workflows use the separate Linux VM; see
[Linux runner operations](linux-actions-runner.md).
