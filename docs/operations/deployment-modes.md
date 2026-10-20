# Deployment modes and migration boundaries

Read this guide before changing release policy, initialization, persisted formats,
or recovery. Use [coordinated releases](coordinated-releases.md) to prepare and
install a reviewed batch and [the AWS runbook](aws-deployment.md) for host operations.
This contract retains one EC2 host, Docker Compose, immutable releases and the
existing Terraform/GitHub workflows. It does not introduce filesystem snapshots.

## Choose the release contract

| Operation | Persistent-state work | Recovery boundary |
| --- | --- | --- |
| Compatible application deployment | Validate installed schemas without migration; preserve current data and installed runtime configuration/secret versions | Restore the tested application version where allowed; retain current data |
| Migration deployment | Publish a verified application-data checkpoint, then explicitly initialize/migrate and validate | Before writes resume, recover the checkpoint; afterward preserve new writes and use tested application rollback or a forward fix |
| Scheduled backup | Quiesce writers, capture independent data, resume services, fully verify and publish off-host | Deliberate disaster recovery; restoring older data can discard later work |

`deploy/release-policy.json` accepts the optional `deployment_mode` field:

- `"migration"`, or an omitted field, requests the existing conservative procedure.
- `"compatible"` requests the lighter procedure, subject to all eligibility gates.
- An unknown mode is invalid; it is not an escape hatch from validation.

The first release introducing startup protocol 2 must use migration mode. A
protocol-1 or legacy predecessor cannot establish the expanded contract. This
rollout changes schema/startup/runtime code and deliberately remains a migration
release.

Protocol 2 permits static dashboard assets (`.js`, `.css`, `.html`), documentation,
and a narrow backend scope: exact reviewed versions of
`job_search/runtime_readiness.py`. All other Python, including persisted writers,
schema helpers, diagnostics dependencies and the eligibility implementation itself,
remains fingerprinted along with deployment/runtime inputs and executable modes.
Ordinary business-logic, schema, dependency and configuration changes therefore
still use migration mode. Broader backend eligibility is future work.

The committed `deploy/compatible-backend.json` records the protocol and SHA256 of
the reviewed diagnostics source. Both predecessor and candidate need a complete,
current declaration. A missing, malformed, unknown-path or stale declaration cannot
grant compatibility. Updating this declaration is a deliberate code-review action:
review all callers, imports and persisted effects before approving the new bytes;
do not automatically refresh the digest to make CI pass. The allowed path set lives
in fingerprinted code, so a declaration cannot approve arbitrary modules. Updating
an already-reviewed source hash does not itself change the runtime identity.

The initial audit covers report composition only. Its SQLite access is read-only;
usage, verification receipts, owner/chief status, ranking inspection and dependency
observations use existing helpers that remain fingerprinted. `readiness.py` is
intentionally excluded from the exemption because its helpers also produce the
persisted dependency-snapshot format. Moving a writer or schema change into the
reviewed module violates this contract even if a test fixture misses the branch.
The declaration is an auditable review record, not a sandbox or proof of every
possible input.

Transition acceptance additionally executes both images' diagnostics on legacy and
converted-owner state, with paused/enabled automation, summary/detailed ranking and
snapshot/provided/local dependency observations. Full rows, schema objects, schema
versions and artifact/configuration contents must stay unchanged. Startup checks,
source/image/predecessor identity and candidate-write/predecessor-read tests remain
mandatory. A filename, source hash declaration or passing read-only probe alone
never authorizes a snapshot-free release.

Later releases may request compatible mode only after reviewing their persisted
state, startup and runtime configuration behavior. A filename allowlist alone does
not prove compatibility. Secret rotation, configuration changes,
Hermes upgrades and persisted-format changes need their own reviewed transition;
they must not be hidden inside a declaration of application compatibility.

The host verifies the exact installed predecessor and existing transition evidence
before selecting a procedure. Both releases must advertise the startup schema
protocol; the runtime compatibility contract must match; the source-bound Docker
transition evidence must establish the compatible path; and a read-only probe of
the live configured schemas must pass. Missing eligibility falls back to migration
before state changes and records `deployment_mode_reason`. Invalid policy, stale
predecessor or untrusted overall transition evidence still rejects the release.
A failed schema probe is not proof that migration can repair the data: migration
retains its normal checks and recovery behavior and can also fail.

There is no operator `skip-backup` shortcut. Inspect `deployment_mode` and
`deployment_mode_reason` in the deployment receipt rather than assuming the requested
mode ran. A successful compatible receipt has `backup: null` and
`local_backup_retention: "unchanged_no_snapshot"`; it retains existing local backups. The release
owner reviews policy changes through the same PR/preparation process as other
changes. Candidate reuse does not relax any source, predecessor or image checks.

## Explicit migrations and read-only startup checks

Production services use `JOB_SEARCH_SCHEMA_MODE=require_current`: core, owner and
collector schemas, including collector enrichment and location metadata, must already exist. The explicit boundary also covers configured
resume/career and owner-mail archive schemas, plus optional ranking, salary and deduplication schemas already present or explicitly enabled in the installation. Their
preparation entry points validate in strict mode; workers skip schedule seeding.
Migration upgrades present optional components and prepares enabled features without
activating ranking or creating every unused model database. Configured stores
required by runtime are initialized explicitly. To prepare a currently disabled
feature during maintenance, use `migrate --prepare-feature ranking` or
`migrate --prepare-feature salary` (repeat the flag for both). This creates schema
and tracking epochs only; it does not run models, compute families, enqueue work or
change activation. Strict feature activation rejects missing state before changing
the activation control and points to this explicit command. `init` supports the same
flag as an alias; `check-schema` does not initialize optional features.

Normal application data writes remain allowed after startup. Local default
bootstrapping remains available outside strict mode. This is not a claim that every
tool in the repository is migration-free: separate Hermes delivery storage and
standalone output/replay tools still have their own lifecycle. Their runtime code
remains unchanged for compatible releases. Any expansion needs its own inventory,
persisted-format review and transition coverage.

The application CLI separates these operations:

```sh
python -m job_search --config /path/to/config.json check-schema
python -m job_search --config /path/to/config.json migrate
```

`check-schema` inspects configured state without initializing missing databases or
running migrations. Its normal SQLite read-only connections honor live WAL contents;
SQLite may still perform transient WAL/SHM sidecar bookkeeping. This does not change
logical application records. After every writer stops, the installer repeats the
check with `--offline`: it rejects nonempty WAL/rollback journals and nonregular
databases/sidecars before using immutable reads, without creating sidecars. Never
use `--offline` against a live application.

Application-owner bootstrap and explicit migration enable WAL journaling so review
snapshots can coexist with worker and user commits. Strict service startup preserves
the installed journal policy. Owner connections bound lock waits to two seconds;
contention rolls back the command and returns `database_busy` (HTTP 503). Retry a
user action with its original idempotency key. Workers classify this as retryable
storage contention, never as malformed model output. Snapshot and recovery retain
their existing stopped-writer and sidecar checks; do not copy or remove live WAL
files to resolve contention.

A failed live precheck selects migration mode before cutover. A failed stopped
recheck instead reverts/resumes the predecessor without snapshot or initialization;
it does not attempt a new migration after selecting the compatible procedure.
`migrate` is the explicit mutating operation; `init` remains an
alias for callers that need the existing initialization behavior. On production,
use the journaled deployment workflow to run migrations under maintenance rather
than executing the second command against a running application.

Compose's `initialize` service performs explicit migration. Its `schema-check`
service performs the read-only check used for compatible installation. First
installation and restored-state preparation remain explicit operations. Keep local
fixtures separate: a successful empty-fixture initialization does not establish
compatibility with production state.

## Compatible deployment and rollback

An eligible compatible deployment still pulls verified images, holds the operations
lock, drains workers/reviewers, stops writers, journals release switching, validates
the target, and waits for healthy services. It preserves the installed runtime
configuration and secret-version references. It omits the application-data snapshot,
full data hash pass, ownership rewrite and migration initializer. It therefore
creates no new data-restoration checkpoint.

Recovery is governed by the operation journal and the point at which target writes
may have resumed. Before that boundary it can return to the recorded predecessor
while preserving data. After that boundary it preserves target data and leaves the
installation paused for deliberate recovery. Never delete a journal or startup gate
to force containers to run. Use the exact `recovery_tool` reported by operations
status and follow the [interrupted-operation runbook](aws-deployment.md#routine-updates-and-interrupted-operations).

Application rollback requires an explicitly tested edge to the exact predecessor.
It preserves collected jobs, user activity and other writes made since deployment.
Equal compatibility labels alone are insufficient. Even an additive migration can
be incompatible with an older binary that rejects newer schemas. Restoring a data
checkpoint is a different operation with possible data loss; it must not become
an automatic response to a failed health check after normal writes resume.

## Verification and rollout evidence

Release acceptance must bind the clean candidate source and immutable images to the
exact predecessor. Exercise production startup and both diagnostics modes on legacy and owner state,
check that schema validation/reporting does not mutate state, write representative data with the candidate, and prove the
predecessor can retain and use those writes before claiming application rollback.
Test missing/incompatible schemas, runtime-contract changes, invalid evidence and
fallback as well as the successful compatible path. Recovery tests must cover
process death before and after release switching and before and after writes may
resume. See [Compose acceptance](../compose-acceptance.md) and
[the test commands](../../tests/README.md#release-and-recovery-development).

First deploy the boundary with migration mode. On a later eligible release, retain
its selected mode/reason, source/image identities, health outcome, operation journal
and observed downtime privately. Verify application records and the scheduled backup
state afterward. Neither fictional tests nor the older snapshot benchmarks establish
production speed, availability or recovery time for this path. Do not remove backup
retention, full restore verification or maintenance alarms to improve timing.

## Replacement-host recovery drill

The compatible procedure does not replace disaster recovery. Keep scheduled backup
capture, full validation, upload and freshness monitoring independent of releases.
A local deployment checkpoint is not a substitute for an off-host backup.

For an authorized drill, follow [replacement-host recovery](aws-deployment.md#7-backup-rollback-and-replacement-host-recovery)
on an isolated host and empty verified volume:

1. Record the backup's snapshot/upload times, release ID, archive checksum and secret
   version references without recording secret values. Start the recovery timer
   before replacement provisioning, not after downloads are complete.
2. Keep outbound automation and all mailbox/reviewer workers disabled. Prevent the
   restored host from sharing the active host's delivery schedules or identity.
3. Install the backup's exact release, retrieve the archive and receipt through the
   normal operator role, and use the supported restore command. Retain full archive,
   file and SQLite checks; leave the restored application paused.
4. Verify representative job/list/application records, stored artifacts, encrypted
   data access and account identity. Confirm the dashboard can read restored state
   without starting duplicate external work. Record time to verified availability.
5. Retain a private redacted receipt with failures and manual steps. Remove only the
   temporary drill resources after confirming production is unaffected; retain
   production backups, volumes and required secret versions.

The existing targets are at most 24 hours of lost data and recovery within one hour.
They remain targets until a complete replacement-host drill measures them. Local
process-death tests do not establish EBS durability, IAM access, secret availability
or provisioning time. The fixture and live acceptance records must state these
limits separately.
