# Coordinated releases

Feature sessions work in separate branches/worktrees and hand off tested PRs.
The release owner selects a batch of merged changes and prepares it once. A
separate manual installation switches production to that exact candidate.

```text
feature PRs -> reviewed main commit -> Prepare AWS release -> prepared receipt
                                                               |
                                        explicit Deploy prepared AWS release
```

## One-time activation

This change requires infrastructure setup before the updated preparation or
deployment workflows can run. Existing application containers continue running.

1. Follow [the AWS runbook](aws-deployment.md) and [Terraform instructions](../../infra/aws/README.md)
   to review the plan for the new parameterless `${name}-release-status` SSM
   document and the deployment role's additional permissions. The document reads
   only release identity and maintenance metadata. It cannot accept shell or path
   arguments and does not read secrets or application databases.
2. Apply the reviewed infrastructure change with the owner/bootstrap identity.
   The normal infrastructure automation role cannot change IAM roles/policies,
   including this deployment role's permissions. Do not widen that role to bypass
   this boundary. Inspect the whole plan for unrelated pending infrastructure
   changes before applying; do not replace the host just to enable coordination.
3. Set the GitHub `production` environment variable `AWS_RELEASE_STATUS_DOCUMENT`
   to Terraform's `release_status_document_name` output. Existing bucket, host,
   region, and deployment role variables stay in use. `AWS_WORKFLOW_REF` must be
   the repository variable `refs/heads/main` for preparation.
4. Once the code is merged, run **Inspect AWS release status** on `main`. Confirm
   that the host identity matches the expected installed release. An empty
   `latest_prepared` is normal before the first coordinated preparation; old
   GitHub artifacts are not silently adopted as authoritative candidates.

The IAM addition permits `ssm:SendCommand` on the fixed status document alongside
the existing installer document, retaining the tagged-host restriction. It also
permits `s3:ListBucket` only for `releases/coordination/*` to distinguish a missing
receipt from denied access. Existing object permissions cover receipt reads and
writes. No live infrastructure change is implied by tests or merging this code.

## Status and predecessor proposal

For status without local AWS configuration, dispatch the **Inspect AWS release
status** workflow. Its summary/artifact reports the installed identity, host
maintenance/recovery state, most recently selected prepared candidate, and active
preparation/deployment/infrastructure runs. It has no production concurrency
group, so it can inspect an installation while that workflow holds its lock.

For local CLI use, use the normal AWS CLI session and authenticated `gh`. Set the
nonsecret `AWS_REGION`, `AWS_RELEASE_BUCKET`, `AWS_INSTANCE_ID`, and
`AWS_RELEASE_STATUS_DOCUMENT` from the same deployment configuration. Then run:

```sh
python -m job_search.release_coordinator status
python -m job_search.release_coordinator propose-policy \
  --output .cache/release-policy.proposed.json
```

`propose-policy` preserves the checked-in compatibility identifier and replaces
the predecessor/source baseline with the observed installed identity. Review the
proposal, copy its reviewed contents to `deploy/release-policy.json`, and commit
and merge that update through a PR. For the first installation the predecessor is
null and the existing reviewed fixture baseline is retained. The proposal command
does not push, merge, build, install, or modify the policy unless its output path
is explicitly set to that file. Readiness is checked again during preparation.

Status is an observation, not a lease or an application health check. A busy host
returns `maintenance` with no stable installed identity. An interrupted journal
returns `recovery_required`; missing/unreadable evidence returns `unknown`.
These states block preparation and installation prechecks. Use the host's normal
operations status/recovery commands to resolve them. Workflow activity and host
state are separate: cancelling a GitHub run does not establish an SSM outcome.

## Prepare one batch

1. Merge the changes selected for this batch, including the reviewed predecessor
   update when needed. Other agents can continue working in their own branches.
2. Dispatch **Prepare AWS release** (`aws-release.yml`) on `main`:

   ```sh
   gh workflow run aws-release.yml --ref main
   ```

3. The workflow freezes `GITHUB_SHA` at dispatch, checks the committed policy
   against live production, and selects a candidate. Commits merged while it is
   waiting/running are not included. Its summary identifies the selected SHA.
4. Requests are serialized by `career-platform-release-build`. A later request
   with the same source, policy, host, and build settings reuses the stored
   candidate after verifying its manifest digest and transition evidence. It
   skips builds/tests/uploads and returns the original release ID and checksum.
   An already installed matching source/settings is reported without rebuilding.
5. A new candidate runs all existing offline, browser, Compose, transition, and
   Hermes checks. Before publishing its reusable receipt, it checks production's
   predecessor again. A deployment during preparation can make it stale; it then
   fails without publishing a reusable receipt or changing production.

S3 stores immutable selection receipts beneath
`releases/coordination/<instance>/candidates/`. `latest.json` points to the most
recently selected prepared receipt; it is a convenience for status, never an
automatic deployment target. Receipts survive GitHub artifact expiry. Reuse
verifies the actual manifest, not only the index. Failed or unpublished builds
are not reusable; resolve the failure before requesting a new preparation.
The original release ID uses the workflow run number, so after partial image
publication use a new dispatch rather than rerunning that same run number.

GitHub's default concurrency retains one running and one pending request; another
pending request replaces the older pending one. This is not a FIFO delivery queue
or a guarantee every dispatch executes. A cancelled pending request did not build
or install anything. Do not change the group or bypass the workflow with concurrent
`select`/`publish` commands: serialization is provided by Actions, not S3 alone.

## Install explicitly

Run **Deploy prepared AWS release** with the exact `release_id` and
`manifest_sha256` from the selected receipt. It checks the live predecessor before
sending the installer command. The existing installer independently checks the
transition under its locks before stopping services, closing the precheck race.
Already-installed releases still use the installer's healthy-idempotency check.

There is no push, schedule, or preparation-completion trigger for installation.
Later merged changes stay out of this release until another preparation is
requested. Deployment and Terraform retain their shared production concurrency
group; never cancel an active installation to prioritize another candidate.

If production changed, inspect status, propose/review/merge a fresh predecessor
policy, and prepare again. Do not edit an existing manifest, substitute its
checksum, or bypass transition tests. A timed-out or cancelled installer needs
its existing SSM command and host operation inspected before another install.

Batching reduces the number of maintenance pauses. The installer also prepares
large rollback files while services are available, then proves equality against
the stopped source before reuse. Worker draining, migration gates, recovery and
service health requirements remain in force. See [deployment performance](deployment-performance.md)
for measured snapshot costs and the distinction between total time and downtime.
