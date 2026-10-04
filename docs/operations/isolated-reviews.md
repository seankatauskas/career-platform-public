# Isolated Codex reviews on AWS

The trusted host coordinator reviews a strict posting window using separate primary
and check workers. Each worker is a fresh Codex session in a networkless, nonroot,
read-only container. It receives approved career facts and source postings through
four assignment-scoped tools. It cannot mount or query the ranking databases,
application database, dashboard, general MCP tools, AWS credentials, or host files.
This restriction applies to the dedicated reviewer, not to the trusted deployment
operator or coordinator, which retain administrative access.

Sparse ranking and Codex reviews are independent workflows. Ranking can score both
broad and selective policies cheaply on CPU. It does not choose the review window,
order review jobs by suitability, or supply scores, policy labels, explanations,
previous assessments, or historical shortlist decisions to reviewers. Candidate
coverage remains the full strict posting window, in chronological order. No
training-label UI is added.

## Runtime and credentials

The pinned reviewer image contains Codex 0.160.0, the immutable fit/seniority rubric,
and the narrow review client. The initial model is `gpt-6-astra` with `high` reasoning.
Model and effort are explicit configuration and must agree across the coordinator,
authority, and model gateway. An unavailable model fails the run; there is no
fallback to another model or API billing.

A native Codex authentication owner stores a dedicated AWS login at
`/var/lib/job-search/operations/review-auth`. It uses the existing Codex subscription
login and native refresh lifecycle. This directory is root-only, excluded from
backups, and never mounted into workers. Do not copy a laptop authentication cache.
Workers receive a fixed-purpose model socket; the gateway adds authentication
outside their namespace and permits only the reviewed inference protocol. They
cannot choose an upstream host or use general web or shell tools.

The release workflow builds and pins the reviewer image in the existing app ECR
repository. Deployment extracts and verifies the matching native login executable,
generates root-only host configuration for the current release, and installs the
oneshot service. It does not log in or enable scheduled reviews.

## Manual operation

Run these commands through the existing SSM administration path from
`/opt/job-search/current`. Never capture or print the authentication cache.

```bash
python3 -m job_search.review_host login
python3 -m job_search.review_host readiness
python3 -m job_search.review_host status
```

The login command displays the native device authorization instructions. Complete
that flow in your own browser using the intended account. Readiness verifies the
pinned runtime and whether login state exists; a bounded live review is still
required to verify subscription access to the selected model.

For the first live trial, choose a strict posting window containing at most twenty
jobs from the existing catalog:

```bash
python3 -m job_search.review_host run --mode custom \
  --window-start 2026-10-01T12:00:00Z --window-end 2026-10-01T12:05:00Z \
  --max-jobs 20
```

These times are examples. The coordinator fails before dispatch if the frozen
window exceeds the cap; it never silently discards jobs. Starting a review does
not run collection. A new recurring review requires a recent completed collection.
Custom trials do not advance the recurring posting cutoff.

Resume an interrupted managed review with its durable ID:

```bash
python3 -m job_search.review_host run --review-id REVIEW_ID
```

Generic dashboard/CLI review mutations cannot impersonate managed workers. The
trusted authority controls grant issuance, claim ownership, frozen job revisions,
launch receipts, and stable submission receipts. Completed assessments survive
interruption; a resumed worker receives only outstanding work. Primary and check
assignments use different fresh containers and checks do not see primary judgments.

A coordinator lock prevents overlapping manual and scheduled invocations. Startup
reconciles interrupted grants and known runtime containers. Assignment and invocation
limits bound execution. Only known transient failures are retried; fatal model,
authentication, or protocol failures stop the invocation. Disagreements or missing
checks prevent publication and remain available for operator follow-up. Publication
rechecks cancellation, maintenance, and the exact validated preview fingerprint.

## Schedule, deployment and recovery

The initial configuration is `schedule_enabled: false` and `schedule_calendar: null`.
The timer template has no selected cadence and is not installed/enabled. A later
schedule decision must explicitly choose a calendar; it is not inferred from the
collection schedule. Manual trials remain possible while the timer is disabled.

Deployment and backups close the shared maintenance gate, drain the coordinator,
and stop labeled transient workers before snapshotting state. Review progress and
receipts live in the application database; private coordinator journals live in
`state/review-runner` and are backed up with state. Authentication remains separate
and must be established again after replacement-host recovery. Per-release generated
configuration follows the current release symlink during rollback.

Follow [the AWS release runbook](aws-deployment.md), including immutable image
verification, predeployment backups, schema compatibility and predecessor transition
evidence. Do not deploy by editing a live Compose file or by substituting an image tag.

## Verification

Offline suites cover authority scope, source projections, socket transport, durable
receipts, retry/resume behavior, independent checks, and publication cancellation.
Native Linux runtime probes also check the pinned Codex tool registry and physical
namespace restrictions. Neither establishes live subscription/model access without
the authenticated bounded trial. See [the API contract](isolated-review-api.md) for
request limits and exact worker tools.
