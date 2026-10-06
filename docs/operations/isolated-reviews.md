# Isolated Codex reviews on AWS

The trusted host coordinator reviews a strict posting window using separate primary
and check workers, followed by a separate v2 finalizer. Each worker is a fresh Codex
session in a networkless, nonroot, read-only container. It receives approved career
facts and source postings through
assignment-scoped tools. Primary/check workers have five tools; finalizers have seven
tools limited to frozen context, reviewed recommendations, and order/group submission.
No worker can mount or query the ranking databases, application database, dashboard,
general MCP tools, AWS credentials, or host files.
This restriction applies to the dedicated reviewer, not to the trusted deployment
operator or coordinator, which retain administrative access.

The host coordinator reads approved career content and active resume text from a
read-only database snapshot. It does not open the application's PDF repository,
initialize resume stores, or repair their permissions. PDF ownership checks remain
enforced by the application services that actually read or write PDF artifacts.
When the coordinator runs as root and the database belongs to the application UID,
a bounded child process reads the snapshot under that owner. SQLite can create WAL
sidecars even for a read-only connection; this keeps those files application-owned
without changing the coordinator's identity or ignoring uncheckpointed evidence.

Sparse ranking and Codex reviews are independent workflows. Ranking can score both
broad and selective policies cheaply on CPU. It does not choose the review window,
order review jobs by suitability, or supply scores, policy labels, explanations,
previous assessments, or historical shortlist decisions to primary/check reviewers.
Only the finalizer can inspect completed primary assessments for cross-batch ordering.
Candidate coverage remains the full strict posting window, in chronological order. No
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

The gateway buffers and validates the complete bounded event stream before exposing
any output. A missing subscription `Content-Type` is accepted only for a valid,
completed SSE stream; an explicit non-SSE type still fails. When the completion's
output list is empty, validated `response.output_item.done` events supply the output.
Conflicting or duplicate items fail closed. Assistant `commentary` and `final_answer`
phases survive the native Codex round trip without expanding its tool permissions.
Native custom-tool result IDs are omitted when converting results back to upstream
function outputs; the original `call_id` preserves the tool-call relationship.

The release workflow builds and pins the reviewer image in the existing app ECR
repository. Deployment extracts and verifies the matching native login executable,
generates root-only host configuration for the current release, and installs the
oneshot service. It does not log in or enable scheduled reviews.

V2 requires a reviewer image with `review-contract-version=2`; trusted readiness checks
the capability on the pinned image. Rebuild and pin matching application/reviewer
digests through the existing release workflow. An older image cannot safely accept
v2 assessments or finalizer tools. Keep v1 assignment support for resumable reviews;
never downgrade a new review to bypass the finalizer requirement.

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

The deployed defaults use twenty jobs per assignment, two concurrent workers, and a
1,200-second assignment limit. The coordinator refills a free slot immediately; one
slow assignment does not hold an idle slot. Assignment timeouts retain completed
judgments and retry only outstanding work within the existing retry budget. A stop,
maintenance request, or invocation deadline still prevents new dispatch and terminates
active workers. The invocation limit remains one hour and progress remains resumable.

Before invoking Codex, the credentialless worker retrieves all frozen context pages
and complete assigned descriptions through its existing scoped API. It verifies
context and posting fingerprints, then supplies the packet and fixed rubric through
stdin. No previous list or another reviewer's judgment enters a primary/check packet.
The packet is limited to 1 MiB; oversized evidence uses the original paged workflow
without truncation. A fresh worker is still used for every assignment. Finalizers
preload their permitted calibration pages instead of original reviewer jobs.

`review_assessments` and `review_calibrations` accept up to twenty items per call.
Finalizers can instead use `review_order` after reading the complete selected set.
It accepts an ordered permutation of up to 6,000 selected ordinals and optional
related groups, then stages and seals that order in one transaction. The same
evidence basis, independent-check gates, visible-card validation and per-item
provenance receipts apply. Missing or duplicate membership, invalid groups or a
failed seal roll back the entire new submission; earlier saved work survives.
Identical retries are safe, while changed arguments cannot reuse saved receipts.
The request remains limited to 64 KiB. Larger orders or group payloads use the
existing twenty-item submissions and final seal. This reduces model/tool round
trips; it does not shorten card evidence, solve oversized finalizer context or
establish the twenty-minute generation target.
Each item keeps the existing validation and idempotency rules. A validation failure
leaves other valid items saved; scope or stale-evidence failures abort the entire call.
Calibration validates its evidence basis and required-check status once inside each bulk
write transaction, then retains each item’s authorization and replay checks. This state
is never reused across requests. Inspect every item receipt and retry only failed items.
Bulk requests are bounded at
256 KiB, while existing single-item requests retain their 64 KiB limit.

Before a new run, inspect the context source inventory and saved search brief. A
seeded career bank can still be pending approval even when an active standard resume
exists. Correct that through the ordinary Career profile approval flow; reviewers
cannot approve facts or infer preferences from unsaved brief suggestions. Every worker
receives the same frozen approved bank, resume evidence, scope, and explicit feedback.

After complete primary coverage and independent agreement, v2 dispatches a dedicated
finalizer against the current context/evidence digest. It reads all selected assessments
in bounded pages, stages one global order and related groups, then seals the artifact.
The selected set is not limited to twenty jobs; each page remains bounded. It cannot
rewrite judgments, hide selected postings, or bypass independent checks. If a correction
is needed, the run remains available for coordinator follow-up and recalibration.

The coordinator then performs bounded official-board availability checks for targeted
selections and stores dated open/absent/unknown observations. Missing configured contact
or a failed check stays unknown and visible. Only confirmed absence is omitted. Worker
containers gain no network access. Calibration/check failures prevent publication;
incomplete progress survives restart. A changed evidence digest requires a fresh finalization.

A coordinator lock prevents overlapping manual and scheduled invocations. Startup
reconciles interrupted grants and known runtime containers. Assignment and invocation
limits bound execution. Only known transient failures are retried; fatal model,
authentication, or protocol failures stop the invocation. Disagreements or missing
checks prevent publication and remain available for operator follow-up. Publication
rechecks cancellation, maintenance, and the exact validated preview fingerprint.

## Bounded production benchmarks

The trusted host can create an explicitly sampled experimental review from an
existing isolated review's frozen inputs. This uses the production ledger, scoped
APIs, and normal coordinator; it does not substitute a fixture catalog or context.
Only approved context and selected posting snapshots are copied. Assessments,
read receipts, grants, calibration, and publication history are never copied.

```bash
python3 -m job_search.job_reviews.benchmark start \
  --source-review-id REVIEW_ID --ordinals 1,2,3,4,5 \
  --label 'bounded performance experiment' --idempotency-key UNIQUE_KEY
python3 -m job_search.job_reviews.benchmark run --review-id BENCHMARK_REVIEW_ID
```

For a marked benchmark, `run` also accepts `--benchmark-model`,
`--benchmark-reasoning-effort low|medium|high`, `--benchmark-batch-size` (1–20),
and `--benchmark-concurrency` (2, 4, 8, 16, or 32). The 32-worker option is experimental and
restricted to explicitly selected, nonscheduled marked benchmarks, including when
set directly in runner configuration. Production still defaults to two workers;
ordinary reviews retain the previous maximum of 16. The frozen policy prevents
changing concurrency on resume. Capacity checks and a smaller benchmark must pass
before a 32-worker test; this option does not establish the 6,000-job/20-minute target.
Collection and availability concurrency remain unchanged. Benchmark overrides change
the scoped authority and runtime for that invocation only. The journal records the effective model, effort, batch
size, concurrency, preload setting, and time budgets. Overrides are rejected for
ordinary reviews, scheduled runs, new reviews, and non-run actions. Select a model
exposed by the dedicated AWS account and measure its actual behavior; metadata
availability and quota windows do not establish sustained inference throughput.

Independent checking can use a separate profile through `--benchmark-check-model`
and `--benchmark-check-reasoning-effort low|medium|high`. These flags have the same
benchmark-only restrictions. The optional trusted runner fields are `check_model`
and `check_reasoning_effort`; each unset field inherits its detailed-review value.
Existing production configuration and defaults are unchanged. The resolved profile
applies to ordinary independent checks and `--benchmark-check-all` checks. Screening
keeps its screening profile; primary review and finalization keep the detailed profile.
Grant authorization, worker configuration and the model gateway all bind the selected
profile. This does not change required audits, evidence access or agreement gates.

New execution policies explicitly freeze the resolved `check` model/effort before
dispatch. A changed resolved checker profile on resume, including an override's
removal when that changes the profile, fails before grant recovery or worker launch.
Equivalent explicit and inherited settings remain compatible. Historical policies without `check` resolve it to their
own saved detailed profile and retain their original JSON and hash. Existing work
without a frozen policy can only retain its original detailed checker profile; use
a fresh benchmark for a different profile. Older executables that do not recognize
the new policy field cannot resume newly frozen reviews; they must not reinterpret
or rewrite that policy. No database migration or historical grant rewrite is needed.

Panels are limited to 320 explicit source ordinals. Their metadata identifies
the source, selected ordinals, and panel fingerprint; they do not represent complete
window coverage. Every publication path rejects benchmark reviews, and their custom
mode cannot advance the recurring cutoff. Independent disagreements still produce
`needs_review`; an otherwise complete benchmark returns `benchmark_complete`.

Private `<run_id>.assignments.jsonl` receipts retain assignment launch/worker timings,
numeric model request/token counters, and scoped API call timings and response sizes.
Every rejected response retains the aggregate `gateway_response_rejected_count` and
adds one fixed `gateway_response_rejected_<reason>_count`: `upstream_failed`,
`incomplete_stream`, `framing`, `output_consistency`, `unsupported_tool`,
`unsupported_capability`, `size`, or `validation`. Truncated or malformed SSE framing
uses `framing`; a framed stream without completion uses `incomplete_stream`.
These counters identify validation branches, never upstream error strings, payloads,
headers, or tool arguments. Unknown exceptions use `validation`. They do not change
retries, accepted responses, or publication gates.
They also count upstream HTTP response classes and rate-limit responses without retaining
response bodies. Started requests are counted before transport; completed-attempt counts and
token usage arrive only when an attempt returns or fails. A stopped worker can therefore
have more started than completed requests. Fixed failure counters distinguish request
validation, response validation, authentication, transport and gateway capacity failures.
They do not contain prompts, descriptions, headers, credentials, or model text.
Upstream request time includes transport and inference; it is not a measurement of
server compute alone. Use these receipts with complete evidence and card-quality
checks before extrapolating to a full review. Deployment/backup time is separate from
review generation time.

The performance acceptance target is 6,000 collected postings through primary review,
independent checks, calibration, and availability in twenty minutes, with thirty
minutes as the maximum acceptable runtime. This is a target, not a measured result.
Collection and deployment are separate operations. Small panels establish per-stage
cost and quality; they cannot establish full-volume performance or account capacity.
Retain the previous experiment and require a representative load test before claiming
this target is met. Missing assessments, pending checks, or unresolved disagreements
never count as a completed list merely because the runtime budget expired.

`--benchmark-check-all` independently checks every assessed panel job, including
exclusions outside the normal sample. This stronger diagnostic setting is frozen
with the execution policy and cannot be removed on resume. Report its extra checking
cost separately; it does not change production's required audit policy.

## Optional conservative screening

Screening is disabled in the generated production configuration. A benchmark may
explicitly select `--benchmark-screening-model`,
`--benchmark-screening-reasoning-effort`, and `--benchmark-screening-batch-size`
(1–200). Detailed review and independent checks retain their separate model/effort
and assignments of at most twenty jobs. One concurrency limit covers every phase.
Higher worker counts are experimental bounds, not demonstrated capacity. Measure
worker memory, host headroom, account throughput, and contention with collection
before increasing concurrency. A container memory limit is not a reservation.

The screening worker receives the complete frozen context and original descriptions.
Its only decisions are an evidence-backed nontechnical/location exclusion or a route
to detailed review. Every plausible technical role, allowed alternative, ambiguous
case, qualification concern, or unresolved eligibility condition goes to detailed
review. A location exclusion requires exact location and description evidence plus
a saved US-only broad and targeted brief; other geography settings conservatively
go to detailed review. This restriction does not infer country meaning from a regex.
Quoted evidence still requires independent semantic checking.

An escalation is an immutable routing receipt, not an assessment. It leaves the job
pending. A fresh detailed worker receives original evidence without the screening
rationale. Ordinary blind checks, fit labels, visible caveats, finalization, and
publication requirements remain intact. Routing receipts bind source, context, and
revision; refreshed evidence makes an old route inapplicable. Missing or invalid
screening outputs never become default exclusions.

The coordinator freezes the execution policy before dispatch, including all model
profiles and batch/concurrency settings. Resumption must use the same policy. Legacy
work can retain a matching detailed-only profile; screening cannot adopt assessments
or grants from an earlier experiment. A fresh benchmark copies frozen evidence but
does not inherit the source review's execution policy or routing decisions.

Screening assignments shrink deterministically to preserve the 56 KiB assignment
response and a 180,000-byte complete evidence budget. UTF-8 bytes conservatively bound
input tokens, leaving headroom within the model's default context for instructions,
tools, and output. No description or context is truncated. An oversized singleton
is routed by trusted code to full detailed review with paged evidence. Screening
itself requires complete preloaded evidence and a reviewer image advertising
`org.career-platform.review.screening-version=1`.

The additive schema-22 migration preserves existing reviews and grants. Export
`job_review_routes` and `job_review_execution_policies` with the usual assessment,
grant, and journal records when auditing a screening benchmark. Rollback to schema-21
binaries requires the consistent predeployment state snapshot.

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
V2 introduced application schema 21; screening adds schema 22 and adjudication adds
schema 23. These additive
migrations preserve earlier reviews and receipts, but rollback to an older binary requires that release's consistent
predeployment state snapshot. Do not run the older application against the upgraded ledger.

## Verification

Offline suites cover authority scope, source projections, socket transport, durable
receipts, retry/resume behavior, independent checks, finalizer scope/completeness,
availability failure handling, v1 compatibility, and publication cancellation.
Native Linux runtime probes also check the pinned Codex tool registry and physical
namespace restrictions. Neither establishes live subscription/model access without
the authenticated bounded trial. See [the API contract](isolated-review-api.md) for
request limits and exact worker tools.


### Evidence-bound adjudication

A v2 review with differing primary/check judgments dispatches a fresh adjudicator
before finalization. Its networkless runtime receives complete frozen context and
posting evidence plus both judgments from this review only. It is explicitly an
adjudicator, not an independent blind checker. Historical lists, prior-run judgments,
ranking data and parent-side comparisons remain unavailable.

The scoped `review_disagreement` and `review_resolutions` tools permit only a whole
`primary`, whole `check`, or `unresolved` choice. A rationale must cover every differing
dimension and cite exact source evidence; selected choices require an approved fact
reference. Complete context, full-description and paired-judgment read receipts are
required. Oversized judgment pairs use bounded canonical JSON pages under the same
56 KiB response limit; follow every `next_offset`, concatenate the content, verify its
SHA-256 and parse the complete pair. Read receipts advance only across contiguous
pages. Preload reconstructs the exact pair and verifies its ordinal, basis and hash.
Small pairs keep the existing object response. Primary/check assessments and their
revision history remain unchanged.
Immutable resolution records and submission receipts bind context, rubric, original
snapshot, revision and both complete judgments. Changed evidence makes a resolution
stale. Neither stale nor unresolved decisions can authorize calibration/publication.

Status retains original `counts` and `disagreement_count`, and additionally reports
`effective_counts`, `resolved_disagreement_count`, and `unresolved_disagreement_count`.
Calibration, availability, visible cards and publication use whole effective judgments
from valid resolutions. The calibration basis includes resolution provenance; changes
invalidate ordering. Duplicate targets are revalidated against effective membership.
The original reproducible exclusion sample and check obligations remain intact. Any
effective targeted or borderline judgment also requires a check. Broad-only rows not
marked borderline retain the existing audit limitation.

Adjudicators use the frozen checker model/effort, so a faster primary does not lower
the adjudication profile. With checker settings omitted, this resolves to the original
detailed profile; historical policies retain that same fallback without changing
their stored policy or hash. Each ordinal/evidence basis receives
at most one adjudicator grant, persisted before dispatch. Interrupted or unresolved
attempts remain blocked on resume rather than repeatedly sampling until agreement.
Status separates pending, active, incomplete (expired or revoked without a saved result),
and completed unresolved attempts. The runner reports `adjudication_attempt_incomplete`
for a consumed attempt without a result, and `adjudication_unresolved` for an explicit
unresolved decision. A launch failure or interruption can therefore require trusted
operator correction; ordinary resume cannot retry that evidence basis.
The worker can retry an identical saved submission after a lost response but cannot
revise it, combine fields or propose a new judgment. Further source or judgment
correction requires the existing trusted reassessment workflow and independent check.
Images must attest `org.career-platform.review.adjudication-version=1` before launching
v2 workers, so an older image fails before spending on a new review. Complete preload/byte packing includes both judgments; an oversized
single posting uses the existing complete paged evidence workflow without truncation.
