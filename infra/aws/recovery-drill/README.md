# Replacement-host recovery drill

This separate Terraform root creates one temporary instance, one encrypted 100 GiB
data volume, and their attachment. It reads the production instance's reviewed AMI,
size, subnet, security groups and instance profile. It does not manage the production
instance, production volume, IAM roles, secrets or buckets. Use a separate S3 state
key such as `recovery-drill/20260930/terraform.tfstate`; never reuse the main backend key.

Supply the production instance ID, backup/release bucket names and secret **ARNs**
from the main module's outputs. Set `initialize_data_volume=true` only for the new
drill volume. The shared bootstrap retains its exact-volume formatting protections;
unrecognized raw blocks still require verified creation provenance and a short-lived
operator grant. After initialization, restore the input to false before another boot.
Do not apply a changed bootstrap to an active recovery test: it replaces the drill host.

For an explicitly authorized capacity rehearsal, set `instance_type="t3.medium"`
in this separate root. Omitting it retains the source size. This changes only the
new drill instance; it does not resize production. Keep Standard CPU credits and
record the initial credit balance, since a new host's cold setup can exhaust its
credits before workload measurements begin. See the
[capacity qualification guide](../../../docs/operations/capacity-qualification.md)
for the workload and acceptance requirements.

If cold setup is credit-starved, `cpu_credits="unlimited"` can separate restore
preparation from the workload test. It can incur surplus-credit charges. Review
the plan to confirm that only the drill instance changes in place, record the
interval, and apply `cpu_credits="standard"` again before collecting qualification
results. Never combine these measurements without identifying the credit mode.

A full-service capacity rehearsal needs additional isolation before starting any
copied service: disable external automation, use synthetic provider credentials,
block application egress (including host-networked processes), and make container
bridge networks internal. Keep scheduled backup/status/cost timers disabled and
write rehearsal backups locally. A restored catalog and synthetic credentials do
not prove recovery of encrypted credentials or external-provider integrations.
The ordinary replacement-host recovery procedure below still keeps workers stopped.

Start the recovery clock before applying a saved, reviewed plan. Confirm it contains
only the three drill resources and no production changes. Follow the [AWS recovery
runbook](../../../docs/operations/aws-deployment.md#7-backup-rollback-and-replacement-host-recovery):
stage the exact release named by the backup, verify the separately recorded archive
hash, restore through that release's `job-search-ops`, and verify restored databases,
model/resume hashes and encrypted state. A missing compiler can leave the installer
at `blocked_setup`; its verified staged operations entrypoint can perform the restore
that supplies the backed-up toolchain. Inspect any existing bootstrap files before
using the restore command's explicit `--replace` option.

Keep core/model workers and Hermes stopped throughout the drill. Do not run `review`
or `activate` on a clone while the source uses the same Telegram bot or mailbox.
The bootstrap disables its backup and application-status timers; instance metrics
use the temporary instance ID. Private browser testing requires a separate Tailscale
identity and deliberate handling of the configured HTTPS origin. Distinguish data
recovery verification from a complete private-access cutover when recording timings.

Retain the redacted validation record, then review and apply a destroy plan from
**this root and its separate backend only**. These temporary resources are intentionally
deletable. Do not delete the production volumes, backup objects or secret versions.
