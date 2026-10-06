# Shared mail understanding

The shared analyzer produces grounded event, action and temporal findings together.
Schema 20 and the code are installed independently of activation. Existing configs
stay in `legacy` mode. No historical model run starts during migration.

## Configure the mail lane

The trusted core mail worker uses the existing configured mail provider. Its local
classifier configuration must be version 2 (`understand_job_application_email`), or
remote mail inference must already be explicitly enabled. The shared adapter needs
at least 8,192 configured output tokens, with input budget remaining for its schema
and sources. Existing usage limits still apply. An invalid explicit profile does
not fall back to another provider.

```json
{
  "mail_understanding_mode": "shadow",
  "mail_understanding_source_scope": "thread_attachments",
  "mail_understanding_evaluation_report": null
}
```

Modes are `legacy`, `shadow`, `shared`, and `paused`. The source scope must be
explicit for shadow/shared mode: `current`, `thread`, or `thread_attachments`.
Remote attachments additionally require `remote_mail_temporal_enabled`. An excluded
source remains a coverage gap. Source manifests are frozen before model invocation;
full source snapshots remain encrypted, and diagnostics contain no email bodies.

Shadow results do not enter public review counts, tasks, or notifications. Switching
to shared mode makes grouped findings reviewable. Pausing stops new understanding
without handing previously shared-owned messages back to legacy task detectors.
Reply sending retains its separate exact-content approval.

The dashboard Review page shows independent event/action/time decisions in one
message card. Corrections retain original model findings and citations. Historical
reanalysis has a separate history view. Telegram briefings link to these reviews;
they do not introduce an additional classification-approval channel. Hermes uses its
existing briefing and attention tools to consume the new read models. Their tool
instructions distinguish pending/held findings from confirmed events and obligations,
and direct grouped email decisions to the dashboard; the Hermes model is unchanged.

## Evaluation

Automatic acceptance is disabled without a private version-matched report. Reports
bind the schema, prompt, validator, policy and model producer version to a reviewed
holdout fingerprint. Each event/action class must independently meet the gate:
50 high-confidence predictions across at least 50 distinct messages, at least
99% observed precision, zero wrong
application assignments, and threshold at least 0.90. Temporal facts and terminal
outcomes remain review-only. Synthetic fixtures do not establish semantic accuracy.

The offline scorer accepts private JSON cases. Each case contains `case_id`,
`expected`, and `predicted` arrays; findings have `kind` (`event` or `action`),
`application_id`, and `label` (event/action kind). Predictions add `confidence`.
Optional nonnegative counters include `invalid_outputs`, `coverage_abstentions`,
`review_items`, `latency_ms`, and `cost_microusd`. Reviewed labels must come from an
independent holdout, not the model's own predictions. Duplicate predictions count
against precision; missing findings count against recall.

```bash
uv run python -m job_search --config CONFIG mail-understanding evaluate \
  --input PRIVATE_CASES --output PRIVATE_REPORT \
  --producer-version EXACT_PRODUCER_VERSION --dataset-kind reviewed_private_holdout
```

The scorer writes a new owner-only report and prints diagnostic metrics. It refuses
to overwrite existing reports. Configure `mail_understanding_evaluation_report` to
that report only after reviewing the result. Missing, invalid, public-readable or
mismatched reports keep processing in review-only mode. A new producer version needs
new evaluation evidence.

## All linked history

History processing uses archived linked inbound messages and prior outbound context.
It does not fetch live mailbox bodies or send notifications. Membership is fixed at
job creation, independent of messages arriving later. Each resume processes at most
50 messages. Results never automatically apply historical status changes or tasks.
Unavailable archives and uncertain inference outcomes remain inspectable.

```bash
uv run python -m job_search --config CONFIG mail-understanding history preview
uv run python -m job_search --config CONFIG mail-understanding history start \
  --idempotency-key UNIQUE_START_KEY
uv run python -m job_search --config CONFIG mail-understanding history resume \
  --replay-id REPLAY_ID --limit 50
uv run python -m job_search --config CONFIG mail-understanding history inspect \
  --replay-id REPLAY_ID
uv run python -m job_search --config CONFIG mail-understanding history cancel \
  --replay-id REPLAY_ID --idempotency-key UNIQUE_CANCEL_KEY
```

Repeat bounded resume calls until complete; budget deferrals preserve pending work.
`--retry-failed` retries safely failed or unavailable items after their cause is
resolved. An uncertain provider request must follow existing `inference-recovery`
and `inference-reconcile` commands. After an audited `absent` or `failed` resolution,
resume with `--retry-failed` to use the original frozen input. Starting another
history job cannot bypass an unresolved outcome. Validated analyses are checkpointed before
projection, so a later projection failure does not cause another model call.

Historical archives created by the old sanitizer can lack quoted text. The replay
records that limitation rather than claiming it recovered the original full thread.
Rejected findings and cancelled tasks remain suppressed; correcting a legacy false
task requires an audited lifecycle transition.

## Release and recovery

Use the existing [AWS release workflow](aws-deployment.md). Schema 20 keeps migrations
1–19 and their checksums unchanged, and uses compatibility `career-state-20-v1`.
Take the existing pre-upgrade backup. A schema-19 image cannot read schema 20;
ordinary image rollback is unsupported. Before resumed writes, restore the complete
pre-upgrade snapshot if needed. After new writes, prefer a forward fix; restoring a
backup is an explicit recovery decision that loses post-backup changes.

Local fixture and transition receipts are development evidence, not authorization
to activate production automation or proof of live model accuracy. Production
rollout starts with shadow comparisons, followed by shared review-only processing,
then separately gated automatic classes.
