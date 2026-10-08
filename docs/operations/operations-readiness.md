# Domain readiness and safe recovery

The domain report answers whether enabled work is progressing. Container/process
health remains a separate liveness signal: a dashboard showing failed work must
remain available so the failure can be inspected.

`job_search.readiness.readiness_report(application_db, now=..., dependencies=...,
automation_enabled=True)` returns schema version 1, a UTC check time, a summary
status, capability observations, and numeric metrics. Each capability has `id`,
`status`, `configured`, `enabled`, `last_attempt_at`, `last_success_at`,
`reason_code`, and `next_action`. Actions are stable string codes, not commands.
The read uses SQLite `mode=ro`; it never initializes or migrates a database,
refreshes authentication, contacts a provider, or runs work. The composition root
supplies its separately collected dependency observations.

Statuses are `disabled`, `paused`, `configured_unverified`, `ready`, `stale`, and
`blocked`. A valid model profile or reachable Telegram bridge remains externally
unverified until an actual operation succeeds. Mail authorization problems use
the existing connector health observations. A successful empty scrape/mail sync
counts as success; posting and message counts are not health thresholds.

Explicitly paused ranking also pauses its per-policy health checks. Saved score
freshness remains visible in ranking details, but does not generate a stale-work
alarm until ranking is enabled again. A mixed ready/paused domain is not itself a
host failure. `job-search-ops status --publish` returns success when the report and
metrics were published, including reports of unhealthy work; failed publication
still exits nonzero. Plain `status` retains its nonzero attention result.

Discovery retries malformed archive JSON up to three times per response, retaining
the normal request backoff and concurrency of eight. Common Crawl pagination ends
on an empty/404 response or its exact, verified out-of-range HTTP 400 after a
successful page; other HTTP 400s remain failures. A failed platform does not prevent
the remaining platforms from being checked. Verified observations and previously
known boards are retained, while the discovery run remains failed until a complete
run succeeds. Rate limiting still stops discovery. Scraper failures retain a
redacted error tail, and task completion/retry times include actual elapsed work.

Freshness follows the existing schedule definitions, including their Chicago
calendar timezone. Core/mail/notification schedules get a 15-minute completion
grace after an expected occurrence; ATS schedules and downstream recommendation
workflows get two hours. The additive schema-7 `enabled_since` value survives
ordinary schedule reseeding and process restarts. Disabling and enabling a
schedule starts a new grace period and advances its next due time past the
disabled interval. Historical failures remain inspectable; a newer successful
occurrence resolves their current operational health.

The host monitor invokes the running core image's read-only `readiness` CLI and
publishes `DomainReady`, `DomainStaleCapabilities`, `DomainUnresolvedWork`, and
`DomainPendingReconciliation`. `Healthy` remains the independent container/file
preflight metric. The domain probe never starts a stopped worker. Paused hosts do
not accumulate domain freshness alarms. Terraform defines domain alarms locally;
actual CloudWatch/SNS delivery still requires deployment and account configuration.

## Scan now and ranking progress

Open **Settings → Connections and background work**, or follow **Scan jobs and
check ranking** from Shortlist. **Scan now** queues a new-jobs scan of the configured
company board list. Collection must be enabled, with a collector contact and board
list configured. Scanning is unavailable during maintenance and in the local demo.

The request uses the existing core worker, fixed collection concurrency of eight,
and normal location/ranking workflow. It does not change the daily schedule, expand
the company list, enable salary extraction, raise inference limits, or bypass
notification controls. Concurrent clicks join outstanding collection work. A lost
HTTP response can be retried using the same idempotency key, including after that
scan finishes. The HTTP endpoint is `POST /api/v1/ops/scan`, accepts only
`idempotency_key`, and uses the dashboard's existing origin and CSRF checks.

The progress panel updates every 30 seconds while Operations is visible. It shows
collected postings, distinct families, and coverage separately for the selective
and broad policies. Coverage counts stored scores for current families; a freshness
notice appears when the latest scan still needs a verified refresh. The current
pass counter measures families processed in that pass, including reused cached work.
An allowance pause shows the next queued attempt. These counters do not predict
GPU charges: the inference allowance uses conservative request estimates rather
than measured model tokens. Unranked jobs remain saved but are excluded from
automatic model picks; refresh the shortlist when ranking completes.

## Work recovery

The container worker processes bounded batches (10 work items by default). If
eligible work or application-outbox deliveries remain due in its lane, it waits
one second and runs another batch, up to three extra batches before returning to
the configured idle interval. This lets shortlist evaluations catch up behind
recurring tasks without waiting five minutes between every batch. Each batch
releases and reacquires its lease; maintenance, shutdown, activation switches,
retry due times, inference limits, and one-shot execution still apply. Paused,
future-dated, and other-lane work do not trigger catch-up. The tick report's
`more_due` field records eligible backlog at the end of that batch.

`RecoveryService.list_work()` returns redacted dead-work summaries. The only
mutation is `retry(work_id, expected_revision=..., command_id=..., actor_kind='user')`.
It requires a current revision, a fresh user decision, an allowlisted task, a
positively classified retryable failure, an enabled schedule when applicable,
and no unknown remote outcome or newer successful replacement. Commands are
idempotent and their before/result audit rows are immutable. Retrying resets the
bounded attempt counter while preserving work identity, workflow lineage, and
the recovery audit. Reusing a command for a different decision is a conflict.

Unknown legacy errors are inspect-only. Error prose is never used to decide
whether recovery is safe. Outlook actions, notification sends, and resume runs
retain their existing domain-specific recovery and approval flows. Hermes cannot
authorize this operator mutation. Notification bridge error code
`delivery_reconciliation_required` is displayed as an uncertain external action;
there is no generic resend button.

An archived-mail review can leave a failed `mail.understanding` owner after the
same email is successfully processed by mailbox sync. `work-list` reports
`resolution_allowed: true` and `resolution_reason: mail_review_already_processed`
only when the original message has a retained replacement proposal, its staged
copies are processed/ignored, and every provider invocation has a known terminal
outcome. An operator can then close that obsolete owner without another model call:

```sh
python -m job_search --config CONFIG work-resolve-mail WORK_ID \
  --expected-revision CURRENT_REVISION --idempotency-key UNIQUE_DECISION_KEY
```

The command rechecks the evidence and revision in one transaction, marks only
that owner `cancelled`, and appends an immutable user recovery audit. It retains
the original failure, attempt count, provider usage, and proposal decision,
including a rejected proposal. It does not claim the old model call succeeded,
requeue mail, or send anything. Pending/failed messages, active owners, mismatched
identities, and uncertain inference remain blocked. Resolve uncertain inference
through `inference-reconcile` first after reviewing its outcome. Repeating the
same decision key is safe; generic `work-retry` remains unavailable for this task.

Workers persist failure classification and increment a recovery revision on each
state transition. Repeated crashes still consume the maximum attempt count.
Before a remotely side-effectful operation, its adapter must durably set
`work_items.external_outcome` to `in_flight` (or `unknown` for an uncertain
submission), and only set `terminal` with positive terminal evidence. Lease
expiry or a generic retryable exception cannot replay such unresolved work.
Schema 8 adds durable inference reservations. Lease recovery resumes accepted provider
IDs only through the same-ID polling path; no-ID uncertainty stays blocked. After an
explicit inference reconciliation, `resume.optimize` has one narrow work-retry
exception: every invocation must have a completed, retrievable Runpod ID, and a
user reconciliation audit must exist. Ordinary resume retries still use their domain
flow. Reviewed failed/absent inference can use the existing explicit resume retry
with reconciliation acknowledgment; its application and version guards still apply.
See `docs/models/inference-providers.md` for quota accounting and synchronous-result limitations.

Schema 7 is additive; migrations 1–6 retain their exact checksums. Recovery audit
and work-state fields are inside the application database, so existing SQLite
snapshot and portable-export paths include them. Offline tests cover migration,
backup/restore, real concurrent decisions, stale revisions, schedule grace,
workflow watermarks, unknown outcomes, and mocked host metrics. These checks do
not establish live connector authorization or alert delivery.

## Shared diagnostics and delivery decisions

The CLI `readiness`, dashboard Ops view and MCP health capability share the same
schema. The core process records a sanitized dependency observation before each
tick. The dashboard and MCP read this snapshot from the application database;
they do not need the inference configuration or provider credentials to diagnose
worker readiness. Only capability codes, booleans, observation time and release
revision are persisted. Missing observations are unverified. Observations older
than 15 minutes, from another release, or implausibly in the future are stale.
The read path never creates state. The production image reports its built source
revision and the database schema version separately from workflow readiness.

The dashboard receives the private notification bridge socket for explicit human
reconciliation. MCP does not receive it. Dashboard mutations retain session,
Origin and CSRF checks, and record a user audit context. Notification recovery is
also available with `notification-recovery` and `notification-reconcile` in the
operator CLI. After checking the actual Telegram conversation, record `delivered`,
`not_delivered`, or `abandoned` with the observed attempt and payload fingerprint.
`not_delivered` queues the original payload for the worker; the reconciliation
handler itself never sends. An interrupted decision can be retried with the same
idempotency key. A newer delivery attempt or changed payload requires a new review.

## Unified local checks

Run `python3 scripts/check-system.py` for all root Python suites and extension unit
checks. For browser acceptance, run `npm ci --prefix extension`,
`npx --prefix extension playwright-core install chromium`, then
`python3 scripts/check-system.py --browser`. Linux CI installs browser system
dependencies with `--with-deps`. Both pull-request checks and the release workflow
use this same entry point. Its JSON receipt records source revision, dirty working
tree status, individual suite results and the explicit fixture-only verification
scope. Browser screenshots and receipts are stored under `extension/test-results`.
