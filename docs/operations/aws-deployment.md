# Career Platform on AWS: deployment and recovery runbook

This deployment keeps the existing Docker Compose architecture on one AWS host.
Hermes uses hosted conversational inference and Telegram; background embeddings
and salary extraction use separately configured remote endpoints. The EC2 host
does not run a GPU model.

**Deployment status:** the implementation and offline tests do not establish a
running AWS installation. AWS provisioning, account enrollment, real model
migration, Outlook authentication, replacement-host recovery, and the seven-day
soak have not been performed. Do not describe the deployment or recovery targets
as measured resume accomplishments until the live acceptance record is complete.

## 1. Prepare accounts and the host

Follow [the infrastructure instructions](../../infra/aws/README.md) to create the state
bucket, move bootstrap state into S3, configure the main backend, and review/apply
the Terraform plan. The initial operation uses the owner's AWS identity with MFA;
later GitHub Actions use OIDC. Keep the generated plans and state private.

Required owner-controlled accounts and configuration:

- AWS account, a confirmed SNS alert email, and Systems Manager access.
- Private GitHub repository `seankatauskas/career-platform`, a `production`
  environment limited to `main`, and an explicitly allowed release branch.
  The current single-owner plan uses manual owner-only deployment and infrastructure
  workflow dispatches (including reruns). Require successful PR checks and review the
  exact commit before dispatch. Enable required environment review and branch
  protection when supported; the manual control does not enforce branch protection.
- Tailscale account with access limited to the owner and enrolled devices.
- Telegram bot and the owner's exact Telegram user/chat IDs.
- OpenRouter credentials and a reviewed tool-capable Hermes model. The proposed
  starting model is `qwen/qwen3-30b-a3b-instruct-2507`; verify provider availability
  and behavior during the live pilot. Do not enable an unreviewed paid fallback.
- Runpod credentials plus pinned embedding and extraction endpoints. Configure
  zero minimum workers and at most one worker per endpoint for the initial pilot.
- A Microsoft application/client ID compatible with the existing Outlook device
  authorization flow. Authenticate the account afresh on AWS.

Defaults are `us-east-2`, Ubuntu 24.04 amd64, `t3.large`, an encrypted 30 GiB root
disk and a separate encrypted 100 GiB data disk. These are starting allocations,
not measured requirements. Pin the resolved AMI for later plans. Keep the
application UID/GID at 10001 so the metadata firewall and file ownership agree.

Host layout:

| Host path | Purpose |
| --- | --- |
| `/etc/job-search/operations.json` | Root-only AWS identifiers and operation settings |
| `/opt/job-search/releases/<release-id>` | Verified immutable release files |
| `/opt/job-search/current` | Current release symlink |
| `/var/lib/job-search/state` | Application data; mounted as `/var/lib/job-search` in app containers |
| `/var/lib/job-search/private` | Individually mounted secret/config files |
| `/var/lib/job-search/hermes` | Hermes persistent state |
| `/var/lib/job-search/toolchain` | Verified Linux Tectonic binary and offline bundle |
| `/var/lib/job-search/runtime` | Ephemeral inter-service sockets |
| `/var/lib/job-search/backups` | Local predeployment rollback snapshots |
| `/var/lib/job-search/operations` | Root-only durable operation journal and interrupted restore directories |
| `/var/lib/job-search/maintenance` | Root-owned, read-only container startup/drain gate; no credentials |

Hermes backups omit its rebuildable `home/.cache/uv` tree and the known Unix
sockets `gateway.sock` and `state/gateway.loop-tick.<pid>.sock`. Databases and
other state remain included; unknown sockets and links outside that cache still
fail closed. Ownership repair also avoids following uv cache links. The private
configuration startup hook restores mode `0600` after upstream's `0640` hook,
before profile reconciliation. Preflight continues to require owner-only files.

Use SSM for administration; there is no SSH ingress. Verify
`job-search-data.service` and `job-search-metadata-firewall.service` succeeded
before installing releases. Never bypass a missing disk or volume marker by
creating an empty data directory.

## 2. Prepare configuration, secrets, and the PDF toolchain

Terraform creates secret containers only. Upload values from owner-only files
through Secrets Manager; do not put credentials in Terraform, Git, process
arguments, CI variables, or logs. Obtain exact secret ARNs from Terraform outputs.

Use [docs/operations/cloud-deployment.md](cloud-deployment.md) for the runtime configuration,
portable key, Hermes MCP/Telegram configuration, and encrypted Outlook setup.
Review every configured path: application paths are container paths, not host
paths. In particular:

```json
{
  "application_db": "/var/lib/job-search/job-search.db",
  "jobs_db": "/var/lib/job-search/job-boards.db",
  "preference_db": "/var/lib/job-search/job-boards-preference.db",
  "proxy_db": "/var/lib/job-search/job-boards-proxy.db",
  "resume_lab_db": "/var/lib/job-search/resume-lab.db",
  "resume_artifact_root": "/var/lib/job-search/resume-artifacts",
  "dashboard_https_origin": "https://YOUR-HOST.YOUR-TAILNET.ts.net",
  "dashboard_allowed_tailscale_login": "YOUR-TAILSCALE-LOGIN",
  "hermes_notification_socket": "/run/job-search-notifications/hermes.sock",
  "hermes_telegram_target": "telegram",
  "hermes_executable": null,
  "remote_mail_inference_enabled": false,
  "shortlist_notifications_enabled": false
}
```

This is a fragment to merge into the existing version-1 configuration, not a
complete configuration. Set a real scraper contact address. Preserve existing
mail-processing permissions; this infrastructure work does not grant additional
mail analysis, draft creation, or calendar permissions.

Provide all mounted files: `config.json`, `inference.json`, `resume-model.json`,
`portable-master-key`, `mcp-token`, `runpod-api-key`, `hermes.env`, and
`hermes.yaml`. An unconfigured resume model must stay unconfigured in runtime
settings; supply a harmless `{}` placeholder for the mandatory Compose file
mount rather than enabling a new generation model. The Hermes `.env` and
`config.yaml` are materialized from `hermes.env` and `hermes.yaml`. Keep Telegram
access restricted to the owner. Tailscale enrollment material is root-only and
never mounted into a container.

Generate a **new** portable encryption key for this fresh installation. Preserve
the corresponding secret version; losing it makes encrypted mail and token data
unreadable. Changing a key file under existing ciphertext is not rotation and is
rejected. Do not copy the laptop's Keychain, OAuth cache, or another installation's
portable secrets.

The deployment does **not** download Tectonic automatically. Before the first
deployment, install an exact reviewed **Linux amd64** Tectonic executable and its
complete compatible offline bundle at:

```text
/var/lib/job-search/toolchain/tectonic
/var/lib/job-search/toolchain/tectonic.bundle
```

Verify the upstream executable checksum, record the bundle checksum and source,
and verify the executable's actual version. Use that exact version for
`resume_tectonic_version` and the GitHub `TECTONIC_VERSION` variable. A macOS
executable from the laptop cannot be reused. The executable and bundle must be
root-owned and not writable by UID 10001. Validate compilation and PDF extraction
through the networkless tools container before activation.

### Board coverage, discovery, and collection-only operation

The fresh seed below excludes the old job corpus and discovered company-board list.
Before enabling production collection, transfer the existing active/unverified board
list into `/var/lib/job-search/state/boards.json` on the host (the container sees
`/var/lib/job-search/boards.json`). Merge with existing boards rather than replacing
AWS-only entries, then run `python -m job_search.collection.boards --discover-only
--db /var/lib/job-search/job-boards.db` in the application container to import the
cache into the durable registry without archive requests. Back up the prior cache,
verify the transfer checksum, and keep the configured `board_registry_path`, if
set, pointed at this same container path. Do not bake a private registry into Git
or an image. The normal state backup includes the registry and cache.

Recent board discovery runs at 05:00 and 17:00 **America/Chicago**, following daylight
saving time. The existing 06:00 and 18:00 collection windows use the updated list;
10:00, 14:00, and 22:00 scans and the 02:00 authoritative scan are unchanged.
Discovery and collection use the same serialized core worker. Overdue discovery
has higher priority than a new-postings scan, so it finishes first when both are
ready. Discovery failures back off; collection continues with the known boards.
Scheduled posting scans use `--no-export`: SQLite retains complete postings and
history, while the small collection receipt and failed-board retry list remain on
disk. This avoids redundant multi-gigabyte CSV/JSON exports and oversized backups;
ordinary CLI scans still export rows unless the flag is supplied.
No extra full scan is scheduled after each discovery. Initialization retires the
former Sunday discovery schedule and any unstarted work from that schedule.

For collection without inference costs, pause both **ranking** and **salary** using
the persistent automation controls. These controls prevent queued work from being
claimed and survive restarts/deployments; already-running requests must finish or
be reconciled separately. Leave collection enabled. Job descriptions, posting
dates, trained models, existing scores, and queued work remain available for later
ranking. Verify no inference is running before starting a large catch-up scan.

## 3. Import a fresh seed before initialization

The seed intentionally excludes application history, mail, OAuth tokens, the old
job corpus, old job scores, embedding caches, and passive feedback. It carries the
current career facts, selected standard resume files, the two selected trained
rankers, and their teacher training/audit provenance.

Export from a reviewed checkout with access to the local source files:

```sh
python3 -m job_search.aws_seed export \
  --career-db /ABSOLUTE/PRIVATE/resume-lab.db \
  --standard-dir /ABSOLUTE/PRIVATE/current-standard \
  --preference-db /ABSOLUTE/PRIVATE/job-boards-preference.db \
  --proxy-db /ABSOLUTE/PRIVATE/job-boards-fireworks-proxy.db \
  --model-root /ABSOLUTE/PRIVATE/.models/preference \
  --run-id run_f8b97b1e92125bd987e0440e \
  --run-id run_d2398f907b04f9a841182134 \
  --output /ABSOLUTE/PRIVATE/aws-seed.tar
```

The standard directory contains `Sean_Katauskas_Resume.pdf`, the matching `.tex`
and `.txt`, `resume-content.json`, and `resume-provenance.json`. It does not need
historical resume archives. Export prints a SHA-256: retain it separately from
the archive. If normal read-only SQLite access cannot open a source, stop its
writers and checkpoint it, then use `--offline-sources`. That explicit mode
rejects any `-wal` or `-journal` file; never delete those files to make it pass.

Sources must be owned regular files, not symlinks or group/world-writable files.
Exports and imported files are private. The import never unpickles models. Only
import your trusted bundle; its manifest hashes provide integrity, not third-party
model trust.

Before the first installation, run **Prepare AWS release** from the reviewed
commit and record the application image digest from its published manifest.
Preparation does not initialize host databases. Pull that digest on the host
through ECR authentication to use the seed tools below. Finish importing the
seed before running **Deploy prepared AWS release** for the first time.

Transfer the private seed through the backup bucket under a unique
`backups/seed-<ID>/seed.tar` key, not the release bucket or GitHub artifacts. The
host can read that prefix; the GitHub deployment role cannot read personal
backups. Download it into a private staging directory and verify the separately
recorded SHA-256. Record the actual target embedding identity from the final
inference configuration's `embeddings.embedding_identity`; it includes provider
and deployment identity, not just the model's name. Do not substitute the old
local BGE identity to suppress migration warnings.

Create `/var/lib/job-search/seed-input` and
`/var/lib/job-search/seed-staging` as mode 0700, UID/GID 10001. Put the archive at
`seed-input/seed.tar`, mode 0600, UID/GID 10001. Keep application containers stopped.
With `APP_IMAGE`, `SEED_SHA256`, and `TARGET_EMBEDDING_IDENTITY` set to reviewed,
nonsecret values, run:

```sh
sudo docker run --rm --network none --read-only \
  --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777 \
  --mount type=bind,src=/var/lib/job-search/seed-input,dst=/seed-input,readonly \
  --mount type=bind,src=/var/lib/job-search/seed-staging,dst=/seed \
  --entrypoint python "$APP_IMAGE" -m job_search.aws_seed import \
  --archive /seed-input/seed.tar --sha256 "$SEED_SHA256" \
  --destination /seed/state --runtime-root /var/lib/job-search \
  --embedding-identity "$TARGET_EMBEDDING_IDENTITY"
```

`--runtime-root` rebases model registry paths to the final application mount;
it does not create or modify that path inside the seed container. An existing
nonempty destination is always rejected. `--allow-empty-destination` permits only
an explicitly chosen private, empty directory.

Inspect the receipt at `seed-staging/state/seed-receipt.json`. Remove the host's
empty bootstrap `state` directory with `rmdir` and rename `seed-staging/state` to
`state` only after confirming it is empty and no application is running. If
`rmdir` fails, stop and inspect; do not remove initialized or live databases.
Imported paths now match the final container mount.

The import leaves career facts **pending user review** and rankers **inactive**.
The seed receipt records whether the source standard was registered. Exact registered
standards retain their registration through the service interface. Otherwise the
files remain preserved under `state/standard-resume`; import the standard through
the dashboard after the PDF tools are available. The seed does not invent an active
standard, user approval, or a new champion.

Keep the seed archive and receipt private until restoration has been verified.
Remove staging copies when no longer needed; normal S3 backup retention applies
to the uploaded seed prefix.

## 4. Install the first release, still paused

Configure GitHub variables from Terraform outputs. `AWS_WORKFLOW_REF` must be a
**repository variable**, since the job's branch guard runs before environment
variables are available. The other settings may be `production` environment
variables:

| Variable | Value |
| --- | --- |
| `AWS_REGION` | `us-east-2` or the selected region |
| `AWS_WORKFLOW_REF` | Explicit allowed branch, e.g. `refs/heads/main` after merge |
| `AWS_APP_REPOSITORY`, `AWS_HERMES_REPOSITORY` | ECR repository URLs |
| `AWS_INSTANCE_ID`, `AWS_DEPLOY_DOCUMENT` | Host and constrained SSM document |
| `AWS_RELEASE_BUCKET`, `AWS_DEPLOY_ROLE_ARN` | Release storage and OIDC deploy role |
| `HERMES_BASE_IMAGE` | Reviewed upstream image pinned to a SHA-256 digest |
| `TECTONIC_VERSION` | Exact installed Linux executable version |
| `AWS_STATE_BUCKET`, `AWS_INFRASTRUCTURE_ROLE_ARN` | Terraform backend and separate infrastructure role |
| `AWS_TERRAFORM_VARS_JSON` | Reviewed nonsecret main-module input object |

Push the reviewed commit to the approved repository/branch when authorized. Run
**Prepare AWS release** (`aws-release.yml`). This publishes an update without
installing it or interrupting the live application. The committed
`deploy/release-policy.json` identifies the compatibility contract, full test-baseline
commit, and exact predecessor release/source. The first installation uses a null
predecessor. Before a later release, update that policy from the last successful
production receipt in the same reviewed change. An unexpected installed predecessor
is rejected before production is stopped. The workflow tests the predecessor and
candidate images against fictional persisted data, builds once, and publishes
immutable image digests and a checksummed release. The workflow summary and
prepared-release artifact contain the release ID and manifest SHA-256.

At a convenient maintenance time, run **Deploy prepared AWS release**
(`aws-deploy.yml`) on the approved branch with that exact release ID and checksum.
It verifies the published manifest and test evidence, then invokes the constrained
SSM installer without rebuilding. The installer still verifies the installed
predecessor before stopping services. Do not rerun an unknown timed-out SSM
operation until its final status is known. Preparing another candidate never
automatically installs it.

The first release remains paused. Host operations run as root from the verified
release:

```sh
sudo /opt/job-search/current/scripts/job-search-ops preflight
sudo /opt/job-search/current/scripts/job-search-ops status
```

The default operations configuration is `/etc/job-search/operations.json`.
If initial setup is incomplete, the installer returns `blocked_setup` and a
`staged_path` without switching `current`. Run that staged release's
`scripts/job-search-ops preflight`, complete the reported prerequisites, and retry
the installation; staging alone does not initialize databases or start workers.
`preflight`, `status`, `secrets`, `deploy`, `review`, `pause`, `activate`, `backup`, `restore`,
`recover`, and `rollback` return redacted JSON. Mutating operations serialize on the data
volume lock. Avoid ad-hoc Compose writes while an operation is running.

### Routine updates and interrupted operations

Keep development data separate from production. Use the
[local development preview](../development.md) while using AWS for real applications.
After a reviewed feature is merged, use **Prepare AWS release**, then choose when
to run **Deploy prepared AWS release**. Infrastructure changes still use the separate
Terraform workflow. The installation summary reports the release, SSM command, backup and phase
timestamps; an unknown SSM result must be inspected before another deployment.
A repeat request for an already installed healthy release does not reinstall it.

Workers stop claiming work between jobs and acknowledge a drain while the dashboard
stays available. The drain deadline is 70 minutes; exceeding it cancels the update
before data changes and clears the drain request. Once drained, all writers stop,
state is snapshotted, and the release is initialized. The local rollback archive
uses gzip level 1 to reduce compression work during this pause; scheduled off-host
backups retain their normal compression after services resume. Archive verification
and recovery behavior are unchanged. Initialization is restricted
to the recorded operation. Downloads and capacity checks precede the outage.
The target is under five minutes between `stopping` and successful service health;
drain time is measured separately. This remains a target until measured on AWS.

A root-owned journal records intent before migration, secret publication, directory
replacement and release switching. Container startup checks a separate read-only
gate; incomplete destructive maintenance keeps application and Hermes writers out.
Never manually clear that gate or delete a journal to make startup succeed.

If `status` reports `recovery_required`, run the indicated exact operation:

```sh
sudo /opt/job-search/current/scripts/job-search-ops status
sudo /opt/job-search/current/scripts/job-search-ops recover --operation OPERATION_ID
```

If `current` is missing or points at an unusable release, invoke the same commands
from `/opt/job-search/releases/VERIFIED_STAGED_RELEASE/scripts/job-search-ops`.
Use the verified hardened release staged by the installer, not downloaded scripts.
Recovery serializes with maintenance, stops project containers including one-shot
initializers, validates its evidence, and is safe to repeat with the same operation
ID. It always leaves the recovered system paused. Other mutations are blocked
while recovery is unresolved.

Before writes resume, an interrupted deployment can recover its previous snapshot
and release. After writes may have resumed, recovery preserves current data; use a
forward fix or a tested compatible application rollback. A partially published
restore recovers its original directory set. A committed restore keeps the restored
data. Missing evidence or unavailable secret versions require investigation; they
never cause the tool to invent empty state. Interrupted restore staging and saved
directories remain private under the data root for inspection. Do not prune them
until recovery and a new off-host backup have been verified.

Image rollback requires both the reviewed compatibility identifier and a passing
rollback test against that exact predecessor. Legacy releases that predate
the startup gate cannot be used as routine rollback targets. Keep the existing
newer-schema rejection; schema-changing releases may need a forward fix or explicit
backup restoration, which can discard work after the snapshot.

## 5. Connect privately and align the remote models

Enroll Tailscale through the owner account. Enable private HTTPS Serve to the
loopback dashboard on port 8766; keep Funnel disabled. Restrict tailnet access to
the owner. The dashboard checks its configured HTTPS origin, Tailscale login,
Host, CSRF, and session cookies. Test an enrolled laptop and phone plus a rejected
identity before trusting the boundary. Direct public ports remain closed.

For enrollment and interactive verification, start review mode:

```sh
sudo /opt/job-search/current/scripts/job-search-ops review
```

Review mode starts tools, dashboard, MCP, and Hermes while the scheduled core and
model workers remain stopped. It is **not read-only**: the dashboard can save
changes, and messages to Hermes can call a paid hosted model and propose actions.
Only initiate the bounded tests you intend to run. Pause again before migration,
backup recovery, or other exclusive maintenance.

Use the existing one-shot Compose recipes in
[docs/operations/cloud-deployment.md](cloud-deployment.md) with the current release files and the
environment assembled by `job_search.aws_ops.compose`. This ensures one-shots use
the same image digests, UID, mounts, and secret paths as the installed release.
Run them under the operations lock with regular workers stopped. The host Python
needs no application dependencies; execute application commands in the image.

Authenticate Outlook using the documented device-code flow. Explicitly select
the intended mailbox and requested draft/hold capabilities. Do not enable two
installations to emit the same mailbox notifications; disable those schedules on
the previous system before AWS takes over. No emails are sent automatically by
this deployment.

**Remote model migration is a required gate.** The local encoder identity is
different from the remote provider identity even if both use BGE weights. Do not
edit model manifests or overwrite their identity fields. Keep the original model
artifacts and proxy audit records for comparison. The supported migration path is
an explicit distillation against the archived teacher labels using the configured
remote encoder. The source snapshots already contain the necessary semantic text;
the old jobs corpus and old embeddings are not required.

Run the following arguments inside the installed model image, with its normal
config/key mounts and no background worker:

```text
python -m job_search.ranking.proxy distill
  --db /var/lib/job-search/job-boards.db
  --proxy-db /var/lib/job-search/job-boards-proxy.db
  --state-db /var/lib/job-search/job-boards-preference.db
  --artifacts /var/lib/job-search/.models/preference
  --run-id proxy_3d50586fa9690cb698e8
  --encoder remote --inference-config /run/job-search/inference.json
  --no-score --no-passive-feedback

python -m job_search.ranking.proxy audit
  --db /var/lib/job-search/job-boards.db
  --proxy-db /var/lib/job-search/job-boards-proxy.db
  --state-db /var/lib/job-search/job-boards-preference.db
  --run-id proxy_3d50586fa9690cb698e8
  --inference-config /run/job-search/inference.json
```

These are billable operator-triggered tasks, not part of import. Measure a bounded
endpoint pilot first and set account spending controls before processing all 2,000
training and 400 audit records. Capture latency/cost, new manifest hashes, provider
identity, dependency versions, and selective/broad regression results. Existing
labels and audit sets are reused for compatibility/regression evidence; do not
describe them as fresh independent human accuracy evaluation.

Only after reviewing the comparison should the operator promote the selected
new selective model with `job_search/ranking/model.py promote --run-id <NEW_RUN_ID>` and
the same explicit database/artifact paths. Normal promotion gates remain in force;
do not add `--force` merely to bypass migration checks. At the out-of-fold score
ceiling, a candidate can clear the title baseline only when both have exactly
NDCG@20=1 and Precision@20=1, the random baseline has NDCG@20 below 1, and the
candidate's average precision strictly exceeds both baselines. Missing or invalid
tie-break metrics fail closed. This narrow exception is shared by selection and
promotion; cross-encoder protected-evaluation gates remain unchanged.
Inference readiness must
report the configured identity matching the selected model before activation.
Keep automatic retraining and unconfigured resume generation disabled initially.

## 6. Activate only after the live gates pass

Run a small discovery sample with the existing concurrency and contact rules,
produce a shortlist, and verify salary extraction with bounded requests. Verify
Hermes can retrieve an application's context, propose a draft/hold, and persist
its conversation across restart. Check owner-only Telegram delivery and dashboard
approval. Confirm container metadata requests fail from both bridge and host
network modes while root host AWS operations work.

Initial budget targets are $80–100/month for AWS and a **combined $25/month** for
model providers, not
price guarantees. Confirm the account's actual limits, alerts, payment settings,
and provider behavior. Disable automatic credit replenishment for the initial
pilot. Use prepaid balances and per-key spending limits where the actual provider
supports them; verify their behavior and do not assume one account's limit covers
another provider. Record spend across OpenRouter and Runpod manually against the
combined $25 allowance, and pause paid work before that allowance is exhausted.
Keep recurring processing paused until spend is understood. AWS budget
alerts are notifications, not a spending cap. The platform's optional
`inference_usage_limits` atomically reserves UTC daily request/token allowances and
in-flight capacity across its managed generation, embedding, and resume requests.
Work waits when a configured limit is reached; accepted Runpod jobs retain their IDs
across restarts, and uncertain submissions require audited user reconciliation.
Token reservations are conservative estimates, not billing totals. Hermes's upstream
model calls and other account activity remain outside this governor. **There is no
cross-provider dollar ledger or automatic $25 spending cap.** Provider limits and
explicit operational pauses are still required; see
[`docs/models/inference-providers.md`](../models/inference-providers.md#platform-usage-limits-and-restart-recovery).

Complete the host's activation checks, verify the intended Telegram/mailbox owner,
and review the account spending settings before enabling regular services. The
activation command requires configured inference and a matching, ready embedding
model; imported inactive or mismatched models do not pass. There is no handwritten
approval file or automatic retraining hidden in this step:

```sh
sudo /opt/job-search/current/scripts/job-search-ops activate
sudo /opt/job-search/current/scripts/job-search-ops status --publish
```

To stop automation and all application services:

```sh
sudo /opt/job-search/current/scripts/job-search-ops pause
```

Approval of an infrastructure release does not approve pending career facts,
email drafts, or calendar proposals. Complete those through the existing UI.

## 7. Backup, rollback, and replacement-host recovery

The timer checks every 30 minutes for the daily 08:00 UTC backup and retries a
failed attempt up to three times, 30 minutes apart. It pauses writers, snapshots
SQLite consistently, captures
model/resume/Hermes state, resumes the previous services, and uploads a checksummed
bundle to S3. Encryption keys are retained separately in Secrets Manager. Record
the backup's exact secret versions and release ID. A backup failing to upload
must not update the successful-backup timestamp. The receipt distinguishes
`snapshot_at` from `uploaded_at`; freshness uses the snapshot. The first failed
scheduled attempt sends an SNS alert and persists a failure metric. An unresolved
maintenance operation blocks automatic retries until recovery. The backup-age
alarm threshold is 24 hours, with normal CloudWatch evaluation latency. A sustained
backup failure can exceed the 24-hour recovery-point target.

```sh
sudo /opt/job-search/current/scripts/job-search-ops backup
```

Normal deployments take a predeployment rollback snapshot; successful deployments
retain the two newest local archives. The release tracks
the previous image and backup. Once normal writes resume, do not automatically
replace the database with the old snapshot. For a reviewed schema-compatible
image rollback:

```sh
sudo /opt/job-search/current/scripts/job-search-ops rollback --release PREVIOUS_RELEASE_ID
```

For a replacement-host restore, prepare a verified data volume and install the
exact release named by the backup. Download the backup bundle and its separately
stored receipt through the operator/host role, verify the recorded SHA-256, and
keep all application services stopped. Ensure every recorded secret version is
available before replacing data:

```sh
sudo /opt/job-search/current/scripts/job-search-ops pause
sudo /opt/job-search/current/scripts/job-search-ops restore \
  --bundle /PRIVATE/backup.tar.gz --sha256 RECORDED_SHA256
```

An occupied target requires explicit `--replace`; inspect it first. Recovery
preserves the prior directory set separately and leaves the restored deployment
paused. Verify database integrity, model/resume hashes, decryption, and account
identity before activation. Do not delete the EBS identity marker or restore a
marker from another volume. Do not run the source and restored mailbox workers
simultaneously. Retire the temporary recovery host after recording the drill.

Targets are no more than 24 hours of data loss and restoration within one hour.
They remain unmeasured targets until a real replacement-instance restore is timed.

## 8. Acceptance and evidence

Run the checked-in tests and Terraform mocked-provider tests before release.
Offline tests do not substitute for the following live record:

| Scenario | Evidence to retain |
| --- | --- |
| First deploy and restart | Release/image digests, successful health checks, persistent state |
| Private access | Laptop/phone access; unauthorized identity and public access rejected |
| Agent boundary | Metadata blocked, no AWS credentials/Docker socket available to Hermes |
| Job/model pilot | Bounded inputs, shortlist, salary output, provider identities, measured cost |
| Mail workflow | Correct account, status update, draft/hold approval, no duplicate notifications |
| Interrupted worker | Recovery and subsequent queue progress |
| Failed deployment | Previous version recovery without losing subsequent user writes |
| Failed backup upload | Error/alert received; previous backup retained; services resumed |
| Replacement restore | Exact backup/hash/secret versions, integrity checks, measured duration |
| Seven-day operation | Memory/disk/CPU-credit peaks, queue progress, alerts, costs and backup ages |

Confirm SNS alarm delivery independently of Hermes. Monitor missing metrics as
failures, backup age, instance health, memory, disk, and CPU credits. Preserve
redacted evidence privately; exclude mail bodies, prompts, profile contents, and
credentials from CloudWatch and public reports. After the seven-day soak, adjust
the instance size or schedules from measurements. Update the resume only with
the deployed and demonstrated capabilities.

### New-account completion record

The current implementation work finishes code and fixture verification first.
AWS account setup, production environment/variables, secrets, provisioning and live
acceptance are a separate next step; no local test receipt establishes those gates.

Before personal cutover, run the table above with fictional data on AWS, including
an interrupted deployment followed by reboot. Restore an S3 backup to a separate
empty-volume host with its own Tailscale identity and outbound automation disabled.
Measure recovery from starting replacement provisioning until the dashboard and
restored data are verified; the target is one hour. Confirm backup-failure and
unhealthy-release alerts reach the owner.

Then use the existing fresh-seed import, retain the local history as a private
archive, and disable overlapping local schedules before activation. Observe seven
days of backups, queue progress, alerts, memory/disk/CPU credits and costs. Restore
one personal backup privately to verify encrypted-data recovery. Remove only the
temporary drill resources after retaining private evidence; do not remove retained
production disks, backups or secret versions. Missed targets remain open gates.
