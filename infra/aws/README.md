# Single-host AWS foundation

This module prepares the Career Platform host; it does not enroll accounts,
populate secrets, start the application, or enable mail processing. No cloud
resources were provisioned during development. The provider tests use mocks.

## Before the first apply

Use Terraform 1.10+ and an AWS operator identity (prefer AWS IAM Identity Center
with MFA). The checked-in provider lockfiles must be kept. Region defaults to
`us-east-2`; the host is Ubuntu 24.04 amd64 on `t3.large`, with a 30 GiB encrypted
root disk and a separately retained 100 GiB encrypted data disk. CPU credits use
standard mode to avoid surprise unlimited-credit charges.

1. Create the state bucket from `bootstrap/` using an operator identity:
   `terraform -chdir=infra/aws/bootstrap init`, then
   `terraform -chdir=infra/aws/bootstrap apply -var='bucket_name=YOUR-UNIQUE-BUCKET'`.
   Keep the resulting local bootstrap state private until it is migrated.
2. In `bootstrap/`, create a local `backend.tf` containing
   `terraform { backend "s3" {} }`. Create a local backend configuration with the
   bucket name, region, `key = "bootstrap/terraform.tfstate"`, `encrypt = true`,
   and `use_lockfile = true`. Run `terraform init -migrate-state -backend-config=...`
   from that directory. Keep `backend.tf` locally (it is git-ignored); it is bootstrap configuration,
   not an application release.
   The bootstrap state key is intentionally outside the CI infrastructure role's
   allowed state prefix.
3. Copy `backend.hcl.example` and `terraform.tfvars.example` to ignored local
   filenames; set your state bucket and real alert email. The private repository
   is `seankatauskas/career-platform`, not the upstream fork.
4. Use an existing GitHub OIDC provider ARN if one already exists in the account.
   Limit the `production` environment to `main`. Enable required review and branch
   protection when the GitHub plan supports them. On the current single-owner
   private repository plan, releases and infrastructure changes instead require an
   explicit owner-triggered workflow run from the allowed branch, including reruns.
   Review the PR checks and exact commit before dispatch. This is a manual release
   control, not enforced branch protection. Revisit it before adding collaborators.
   The OIDC trust accepts only that repository and environment. Check
   `gh api repos/OWNER/REPO/actions/oidc/customization/sub` before the first deployment.
   For `use_immutable_subject: true`, set `github_oidc_ids` using the owner ID and
   repository ID reported by `gh api repos/OWNER/REPO`; retain this value in
   `AWS_TERRAFORM_VARS_JSON` too. GitHub enables the ID-bearing subject format for
   new repositories created after July 15, 2026. Leave the variable null only for
   a verified legacy subject. See the [GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims).
5. Review the first `terraform plan`, then apply using the operator identity.
   Terraform creates only Secrets Manager containers, never secret values.

Example main-module commands:

```sh
terraform -chdir=infra/aws init -backend-config=backend.hcl
terraform -chdir=infra/aws plan -out=reviewed.tfplan
terraform -chdir=infra/aws apply reviewed.tfplan
terraform -chdir=infra/aws output
```

The initial apply and later IAM-policy changes require the operator identity.
The infrastructure workflow role deliberately cannot create or rewrite IAM roles,
including its own role. Its `iam:PassRole` is restricted to the host role and EC2.
Routine non-IAM changes use the infrastructure workflow; releases use the separate
deploy role. Neither requires long-lived GitHub AWS access keys.

Pin the resolved `ami_id` output before later plans so a Canonical AMI refresh does
not unexpectedly replace the host. Changing user data also replaces the host;
review the plan and perform replacements in a maintenance window after a backup.

## First boot and persistent storage

`initialize_data_volume = true` is an explicit first-install authorization to
format the exact Terraform-managed EBS volume **only if it has no partitions,
signatures, filesystem, or nonzero bytes**. The default is false. A first-volume
zero scan can take several minutes. AWS also permits new EBS volumes to contain
[cryptographically pseudorandom bytes](https://docs.aws.amazon.com/ebs/latest/userguide/EBSFeatures.html).
Such a volume requires the operator procedure below; a failed zero comparison
alone does not establish previous use. Existing ext4 volumes must carry the matching
root-owned `.job-search-volume-id`; other filesystems and missing/mismatched
markers fail closed. There is no automatic recovery by creating an empty data
folder. Docker cannot start without the data verification service.

Set initialization back to false for the next planned host replacement (this
changes user data and therefore schedules replacement). This is not an urgent
in-place toggle: the original script cannot reformat a disk containing any data.
If a failed first initialization left an ext4 disk without its marker, inspect it
through the operator's SSM session and verify its identity and contents before
manually completing initialization; do not delete or reformat it to bypass the
check. Restore into a freshly initialized target through the documented operations
command rather than overwriting its volume marker.

### Newly created volume with nonzero bytes

Do not keep replacing volumes or remove the disk checks. Before authorizing
initialization, verify the exact volume ID against the saved Terraform creation
plan, EC2 `DescribeVolumes`, and CloudTrail creation/attachment history. Require
an empty snapshot source, the intended account and host, and no prior application
use. If provenance is uncertain, preserve the volume and investigate instead.
Never authorize an imported, restored, previously used, or unidentified disk.

For a verified new disk, an operator may create the following root-owned file
through SSM, substituting the **reviewed exact volume ID**. The initialization
boolean must still be true. No filesystem, partition, signature, or read-error
check is bypassed:

```sh
sudo bash -c 'umask 077; set -o noclobber; printf "%s %s\n" "vol-REVIEWED_ID" "$(($(date +%s) + 900))" > /etc/job-search/initialize-empty-volume'
sudo systemctl restart job-search-data.service
sudo systemctl is-active --quiet job-search-data.service
```

The grant must be owned by root, mode 0600, not linked, match the exact disk,
and expire within one hour. It is durably renamed to `.used` before formatting,
so a failure or interrupted initialization cannot consume it a second time.
Do not remove `.used` to retry; inspect the disk and complete recovery explicitly.
Once mounted, verify the volume marker before starting Docker and completing
any bootstrap services skipped by the earlier failure. Keep the provenance and
operation result with the private deployment receipt.

Terraform prevents destruction of the data volume, secret containers, and storage
buckets. Review any change that proposes moving the host to a different AZ: EBS
volumes are AZ-bound. Replacement hosts in the same AZ reattach the retained disk.

## Host and secret boundaries

The security group has no ingress rules and no SSH key. Use AWS Systems Manager
for operator access. Tailscale uses outbound relay connections; enroll the host
and configure private Serve HTTPS after setup. Do not enable Funnel.

Bootstrap installs Docker/Compose from the signed Ubuntu archive, Tailscale from
its signed package archive, AWS CLI v2.35.21 from AWS's native Linux installer
(pinned SHA256, verified against AWS's signing key), SSM via Canonical's signed snap, and a pinned CloudWatch
agent package verified by SHA256. The default CloudWatch package is
`1.300072.0b1766`, obtained from its versioned official AWS distribution on
2026-09-19. For upgrades, review the new version and checksum together.
SSM receives Snap refreshes; AWS CLI upgrades require reviewing its version and
checksum together. The AWS CLI link in `/usr/local/bin`
also makes it available to systemd services without relying on interactive PATH.

The host's root processes use its instance role. IMDSv2 is required; IPv6 metadata
is disabled; firewall rules deny metadata access for UID 10001 (host-network app
containers) and all forwarded Docker workloads, including container root. The
rules are installed before Docker starts and reapplied on Docker restarts.
Keep application UID 10001. Do not add privileged or root host-network containers.

`/etc/job-search/operations.json` is root-only and contains resource identifiers,
not credentials. The `private` directory is root-owned and narrowly traversable
by the application group. Each credential must be an explicit per-service mount;
the Tailscale enrollment key stays root-only and is not mounted into any container.
The installer and release files are root-owned. GitHub deploy authority is trusted
code-execution authority for this single application host: protect workflow and
release changes accordingly.

Secret identifiers exposed by `secret_arns`:

- `config.json`, `inference.json`, `resume-model.json`: reviewed runtime configuration.
- `portable-master-key`, `mcp-token`, `runpod-api-key`: narrowly mounted credentials.
- `hermes.env`, `hermes.yaml`: Hermes provider/Telegram settings and tool configuration.
- `tailscale-auth-key`: one-time owner-controlled host enrollment credential.
- `interaction-token`: separate bearer for the owner-bound Telegram interaction broker.
- `briefing-inference.json`, `briefing-api-key`: explicitly configured briefing/reply
  model profile and credential, mounted only into the model worker.

Populate values through Secrets Manager or an operator CLI from protected files;
never put them in Terraform variables, Git, workflow logs, or shell command lines.
No secret versions are created by Terraform. Application setup must verify actual
file access modes and provider functionality before enabling recurring work.

Existing hosts must use the [chief-of-staff provisioning sequence](../../docs/operations/aws-deployment.md#chief-of-staff-secrets-on-an-existing-host).
The additional secret ARNs change generated cloud-init data, and a full apply can
replace the EC2 host because `user_data_replace_on_change` is enabled. The bounded
operator apply described there creates the secret containers and updates the host
read policy without applying a host replacement. It does not change the existing
host's operations file automatically.

## Release and monitoring interfaces

The deploy role may publish only to the two ECR repositories and release bucket,
and invoke only the custom SSM document on this project's tagged instance. It
cannot invoke `AWS-RunShellScript`. The document accepts a regex-validated release
ID and manifest SHA256 as environment variables; the root-owned installer checks
those against the downloaded release before activating it.

The operations configuration names the backup/release buckets, instance and
volume IDs, secret ARNs, and the SNS topic. The status timer calls
`job-search-ops --config /etc/job-search/operations.json status --publish` every
five minutes. The backup timer runs daily at approximately 08:00 UTC. Both skip
cleanly until the release executable exists. Alarms will still report missing
health/backup metrics during enrollment, so complete setup before relying on them.

CloudWatch retains redacted operations logs for 14 days. Alarm inputs are instance
status, memory, disk, CPU credits, `Healthy`, and `BackupAgeSeconds`; absent metrics
are failures. Confirm the SNS subscription email. Budget notifications at $80 and
$100 are account-wide alerts, not a spending cap; inference-provider spend is
separate. S3 backup retention expires current backups after 14 days and noncurrent
versions after a further 14 days. Versioned release bundles and tagged images stay
available for rollback until deliberately retired.

## Local checks and live acceptance

### Optional unified cost monitor

The dashboard's **Settings → Operations → Costs and credits** view reads a
sanitized, host-published snapshot. It never calls billing APIs or receives AWS or
OpenRouter management credentials. Set `cost_monitor_enabled = true` in a reviewed
Terraform change to add only `ce:GetCostAndUsage` to the host role and opt a newly
bootstrapped host into collection. This does not enable Cost Explorer in an account
where it is disabled; complete that account setup separately. No billing permission
is added by default. Review the plan; do not recreate an existing host merely to
update cloud-init. **The host has `user_data_replace_on_change = true`; a full
apply of changed bootstrap inputs would replace it. For an existing host, limit
the reviewed infrastructure change to the separate cost-monitor IAM policy, then
install the units/configuration in place as below.** Host replacement is a
separate, explicitly approved recovery workflow, not part of cost enablement.

For an existing host, after installing a verified release containing the monitor:

The cost service requires the native AWS CLI, not the Snap launcher: Snap needs
to create `/root/snap`, which the service's `ProtectHome`/`ProtectSystem` sandbox
intentionally forbids. Existing Snap-based hosts can migrate the CLI in place
using the pinned native archive and checksum in `templates/cloud-init.sh.tftpl`.
Preserve the old launcher target for recovery and retain the Snap package until
the native CLI is verified. Do not run cloud-init again or weaken the service
sandbox. Verify `aws --version` under the unit's restrictions before collection.

1. Apply the reviewed, opt-in IAM policy through an authorized operator identity.
   The deployment role cannot grant itself billing access.
2. Preserve the existing root-owned `/etc/job-search/operations.json` and add
   `"costs": {"enabled": true}`. Provider keys stay in private host files; do not
   copy credentials into the runtime JSON, Compose, dashboard, or a chat.
3. Install the units from that verified release:

   ```sh
   sudo install -o root -g root -m 0644 /opt/job-search/current/deploy/aws/job-search-costs.service /etc/systemd/system/job-search-costs.service
   sudo install -o root -g root -m 0644 /opt/job-search/current/deploy/aws/job-search-costs.timer /etc/systemd/system/job-search-costs.timer
   sudo systemctl daemon-reload
   sudo systemctl enable --now job-search-costs.timer
   sudo systemctl start job-search-costs.service
   sudo systemctl status job-search-costs.service --no-pager
   ```

The hourly timer only checks whether the daily snapshot is due; the collector
reserves each attempt durably and allows no more than one per 24 hours. It shares
the operations lock and skips collection while an operation needs recovery.
Cost Explorer bills each API page; account data can lag more than a day. Disabled
collection makes no provider calls. Errors and missing credentials are visible as
unavailable data, not zero spending. Recovery-drill hosts disable this timer.
Turning collection off is an explicit host configuration change and does not
delete previous billing figures. No new AWS resources, IAM policies, timers, or
provider requests are applied just by building this branch.

See [cost dashboard](../../docs/operations/cost-dashboard.md) for metric scope,
OpenRouter account-versus-key access, optional warnings, and freshness semantics.

### Verification commands

```sh
terraform -chdir=infra/aws fmt -check -recursive
terraform -chdir=infra/aws init -backend=false
terraform -chdir=infra/aws validate
terraform -chdir=infra/aws test
terraform -chdir=infra/aws/bootstrap init -backend=false
terraform -chdir=infra/aws/bootstrap validate
python3 infra/aws/tests/test_bootstrap.py
```

Provider mocks verify the planned security baseline without AWS access. They do
not establish live IAM sufficiency, Linux bootstrap success, network reachability,
recovery time, or cost. Live acceptance requires a reviewed AWS plan and apply,
SSM access, a failed application metadata-access probe from both network modes,
private dashboard access, deployment/rollback, alarm delivery, and a replacement
host restore drill. Record those results before making AWS reliability claims.
