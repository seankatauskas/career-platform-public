# Start locally, then move the same installation

Use the private `seankatauskas/career-platform` repository as the source of truth.
The dashboard can use saved rankings and an existing standard PDF before Outlook,
Hermes or hosted inference is configured. Account setup is a separate step; adding
credentials does not enable recurring work in a newly enrolled installation.

## Private Mac enrollment

```bash
uv run --with cryptography --with pypdf python -m job_search setup initialize
uv run --with cryptography --with pypdf python -m job_search \
  --config ~/.local/share/career-platform/config.json setup inspect
```

Enrollment creates owner-only configuration, application and career databases,
a portable encryption key, and an MCP token outside the checkout. Repeating it
inspects the same installation without resetting its state. Existing legacy config
under `~/.config/job-search` is unchanged; always pass the new configuration path.
All seven automation groups start paused. Readiness reports local configuration
and observed receipts; it does not claim that external services have been tested.

Import the exact PDF, its LaTeX source and matching text from a private directory:

```bash
uv run --with cryptography --with pypdf python -m job_search \
  --config ~/.local/share/career-platform/config.json resume import-existing \
  --name Standard --rank 1 --pdf /private/path/resume.pdf \
  --tex /private/path/resume.tex --text /private/path/resume.txt
```

The importer checks both extraction views against the text and stores the PDF
unchanged. It does not compile, rewrite or call a model. Optional `--content` and
`--provenance` paths preserve structured source information. A repeated import of
the same named PDF and source reuses the registered version. An extraction mismatch
must be investigated, not bypassed by lowering the fidelity gate.

`resume_mode: "standard"` selects the registered, parse-safe standard with the
lowest manual rank when preparing an application. The PDF remains downloadable
and reviewable. Preparing an application does **not** submit it. Profile approval
and tailoring are separate; an unapproved career draft does not block the existing
standard. `resume_mode: "tailored"` restores the optional generation flow and its
model, compiler, profile-approval and automation prerequisites.

Start the real dashboard:

```bash
uv run --with cryptography --with pypdf python -m job_search.system \
  --config ~/.local/share/career-platform/config.json dashboard
```

Open `http://127.0.0.1:8766`. This command starts only the dashboard. Workers and MCP
are separate services. See [runtime configuration](job-search-runtime.md) for
launchd installation after checking the selected configuration and interpreter.

### macOS background runtime

Keep the background runtime and its virtual environment outside Documents,
Desktop and Downloads. A launch agent can be denied access to those folders even
when the interactive terminal can run the same checkout. Installation now rejects
those locations before changing any services. Do not resolve this by granting
Documents access or Full Disk Access to Python.

Use a dedicated copy under `~/.local/share/career-platform/runtime`, with its own
venv, and set private config's `project_root` to that directory. Copy only source
files and required assets, never the checkout's credentials or databases. Install
from the runtime's `.venv/bin/python` so launchd retains the environment. Keep a
source hash manifest with the private deployment records. Runtime copies do not
automatically follow edits in a development worktree; validate, copy, and restart
the affected services after an update. Database and resume paths stay in the
separate private state directory.

For an imported large catalog, stop its writers once and enable SQLite WAL mode
on the jobs, preference and policy databases before starting backfills. This lets
dashboard readers continue during model/cache writes. Use SQLite's backup API
for live backups; copying only the main `.db` file can omit committed WAL data.

The four Python login items are the dashboard, MCP server, core worker and model
worker. Hermes has its own supervised gateway. Dashboard and MCP restart after an
unexpected exit; the workers run every five minutes. This is local operation:
the Mac must be awake and the user logged in. Missed schedules coalesce on resume.

## Collection and ranking

Set `scraper_contact` to the real contact address the operator chooses before any
network collection. Keep concurrency at eight. Begin with a bounded collector
check rather than scanning every board. Existing selective and broad policy
mappings refer to their original trained models; scheduled refresh embeds changed
catalog documents and scores **both** policies without retraining or promotion.

Policy readiness exposes score counts, source observation time and refresh
receipts. Old saved scores remain usable, with a stale-catalog notice. A partial
refresh preserves a successfully completed policy and the other policy's earlier
scores. Missing models, inference failures and stale data remain visible.

Set `board_registry_path` to the private full registry. Collection receipts beside
the exported catalog distinguish complete, partial and limited runs. A failed
board response cannot close that board's existing postings.

For hybrid inference, an owner-only generation profile can use
`structured_generation.kind: "openrouter"` with `embeddings: null`. Required
generation fields are `model`, `credential_file`, `timeout_seconds`,
`max_response_bytes`, `max_input_tokens`, and `default_max_output_tokens`.
Use at least 4096 output tokens for the mail adapters. The OpenRouter adapter
requires schema support and data-collection denial, disables reasoning for the
bounded JSON extraction tasks, and never supplies a fallback model list. Its
provenance identifies a hosted model ID, not immutable weights. Generation-only
profiles leave the ranker's recorded local embedding model and revision intact.

Review controls in System, or use revision-checked CLI commands:

```bash
python -m job_search --config /private/config.json automation list
python -m job_search --config /private/config.json automation enable ranking \
  --expected-revision 0 --idempotency-key enable-ranking-1
```

Use the revision returned by `list`, not a hardcoded revision on later changes.
`disable` uses the same arguments. Pausing stops new schedule dispatch and prevents
already queued matching work from being claimed. Work already running can finish.
Configuration and dependencies still gate execution after a group is enabled.

## Outlook and Hermes enrollment

When these are not configured, leave mail, Outlook actions and notifications
paused. The local shortlist, profile, standard resume and application ledger work
without them.

For Outlook, register the personal-account-compatible Microsoft application, save
its non-secret client ID in private config, then run `outlook-auth` interactively.
Follow the existing [Outlook runbook](job-search-runbook.md#personal-outlook-setup) for
scopes and token persistence. Do not put API keys or tokens in chat, Git or config
examples. Restart services after changing configuration.

With `outlook_new_messages_only: true`, first explicit mail activation records a
persistent UTC cutoff. Only mail received at or after that time is eligible for
body retrieval. Initial synchronization can page through older metadata to build
cursors, but does not request body previews there. Bounded passes retain their
continuation cursor. `mail_recruiting_only: true` also excludes unrelated mail
before body retrieval, attachment extraction, archiving or model classification.
This relevance filter can miss unusual recruiter subjects; inspect metadata-only
ignored records when diagnosing a missing conversation. Pause/resume and transfer
retain the original cutoff instead of silently enabling historical processing.

Hermes needs its own model provider configuration and the owner's Telegram bot
routing. Use the [Hermes setup](cloud-deployment.md) and
[MCP runbook](job-search-runbook.md#optional-hermes-mcp-and-telegram-surface).
Connect Hermes to the authenticated MCP endpoint; verify a read-only tool call
before enabling notifications. Test one recruiting email, a reviewed reply draft
and an interview proposal before recurring use. No email sending or application
submission is part of enrollment.

Live enrollment checks store sanitized, configuration-bound receipts under the
private log directory. These expire after 24 hours; status reads do not generate
paid inference calls or refresh OAuth. A passed check means an observed operation
succeeded, not that a provider is continuously available. CLI, dashboard and Hermes
separate configured connectors, explicitly paused automation, and observed work.

Before enabling shortlist alerts on an imported catalog, set
`shortlist_notification_start_at` to the UTC activation time (ending in `Z`).
Only opportunities first observed at or after that cutoff can create alerts;
missing observation dates are excluded. Existing catalog jobs remain visible in
the dashboard. Preserve this cutoff across restarts and deployments.

Use a self-addressed setup email for the live draft rehearsal. Execute a reviewed
test draft and private hold through the action ledger, verify their remote
properties, and remove those exact test objects afterward. Keep the rehearsal
ledger separate from actual applications. The normal application cannot send mail
or invite calendar attendees.

Load the extension from the runtime's `extension/` directory in the browser's
extension developer page. Review ten fresh matches, choose three real roles, and
prepare each using the standard PDF. Connect the browser once from Settings,
review all fields, upload the PDF manually and submit yourself. Check the automatic
submission evidence and subsequent confirmation email; no Mark submitted action
is needed. Include one role per supported ATS, then expand the pilot after those
three flows have been verified.

## Move an installation with history

`aws_seed` remains a **fresh-install seed**. It deliberately excludes application
history. Use `state_transfer` after real usage instead.

Stop dashboard, MCP, workers and every other database writer first. Resolve running
or uncertain operations; the exporter rejects running work and delivering outbox
items. Its `--writers-stopped` flag is an operator assertion, not a process killer.
The transfer uses SQLite backups, preserves full application/career histories,
resume selections and artifacts, ranking state/models, and Outlook cursors/cutoff.

```bash
python -m job_search.state_transfer export --config /private/config.json \
  --output /private/installation.tar.gz --writers-stopped
python -m job_search.state_transfer import --archive /private/installation.tar.gz \
  --sha256 CHECKSUM_FROM_EXPORT --destination /private/new-state \
  --portable-key /private/separately-transferred-master-key \
  --runtime-root /var/lib/job-search \
  --embedding-identity EXACT_CONFIGURED_ENCODER_IDENTITY
```

The runtime root is the final **container-visible state directory**, not the host
staging directory. Follow the mounts in [AWS deployment](aws-deployment.md).
Import validates all checksums, remaps model paths, rejects an existing destination
and pauses every schedule/control. It does not retrain a model when its embedding
identity changes. Verify the exact encoder or follow the remote-model migration
procedure before refreshing scores.

Transfer the matching portable master key separately through private secret
storage. The archive contains personal data and belongs in private storage; it
contains no OAuth cache, runtime configuration, provider credentials or master key.
Recreate target config using the imported database filenames, point to the imported
`resume-artifacts`, supply the same encryption key, create a new MCP bearer token
and authenticate Outlook again. Optional autofill vaults are separate from this
transfer and must be moved using their documented portable-state export.

Before enabling the target, compare application/event counts, selected PDF hashes,
career revisions, model manifests and the mail cutoff. Keep the Mac workers stopped
while AWS is active. Existing AWS operations provide subsequent quiesced backups,
restores and image rollback. Enrollment alone is not evidence of a live deployment.

## Test and recover a ranking refresh

Before a full refresh, exercise the installed models on distributed samples:

```sh
python -m job_search.ranking.refresh --db /private/jobs.db \
  --state-db /private/preferences.db --proxy-db /private/policies.db \
  --sample-size 32 --batch-size 16
python -m job_search.ranking.refresh --db /private/jobs.db \
  --state-db /private/preferences.db --proxy-db /private/policies.db \
  --sample-size 512 --batch-size 128
```

Samples use the real encoder and both existing policies. They can fill missing
embedding-cache entries, but never publish scores, prune existing scores, or write
full-refresh completion receipts. A sample covers existing prepared families;
the full refresh first rebuilds family metadata to include newly collected jobs.
If existing family metadata exactly matches the stored source fingerprints and
normalization version, it is reused. The check also detects edits made without a
timestamp change, missing jobs, and deleted jobs; it does not rely on dates alone.

Without `--sample-size`, refresh processes 256 families per batch by default.
Each batch embeds its exact documents, scores both policies, and commits both
policies together. It reads a single SQLite catalog snapshot; concurrent ingestion
is picked up by a later refresh. Completed batches survive interruption, and a
retry skips score computation for unchanged fingerprints. Only a successful full
pass prunes obsolete scores and writes both completion receipts. No retraining
occurs in this operation.
Database schema preparation runs once per refresh rather than once per batch.
The ranking worker allows up to two hours for a large backfill; collector and
location task deadlines remain unchanged. Retrying ranking retains completed
score batches and reuses verified grouping metadata instead of repeating it.
On macOS the scheduled services use launchd's background process priority. For a
supervised one-time backfill, an operator can run a due model-worker tick from a
terminal with `--lane model --max-work 1 --max-outbox 0`; the normal lane lease
still prevents another model worker from executing concurrently. Do not start a
second standalone refresh while a worker already owns the running refresh.

Inspect `preference_state` key `policy_refresh_progress` for the stage, completed
family count, total family count, batch count, timestamps, and updated score counts.
Sample progress uses the separate `policy_sample_progress` key. A saved batch is
not proof of full completion: require both `policy_refresh:selective` and
`policy_refresh:broad` receipts and a successful worker result. The receipt's
source watermark must match the current catalog before claiming it is fully fresh.
The CLI retains the specific failure reason in worker diagnostics.

Offline regression coverage: `python -m unittest tests.test_policy_refresh`.

## Git boundary

Resumes, databases and journals, models, private state, credentials and transfer
archives stay outside Git and Docker build contexts. `scripts/check-private-files.py`
checks indexed filenames and staged credential patterns. Offline checks and public
export run it; release packaging checks the requested commit before building.
Only the two generic LaTeX templates are permitted as tracked `.tex` files.

### Browser submission tracking

After loading or reloading `runtime/extension`, open dashboard Settings and use
**Connect a browser**. Exchange that code in the popup once. The extension can then
identify and track supported applications opened outside the shortlist. Application
attempts remain separate from confirmed submissions, including their contribution
to recommendation feedback. The existing manual recording route remains available
for exceptions. Browser access can be revoked from Settings.

Migration 10 adds browser registrations, minimal discovered jobs, attempts, and
observations to the private application ledger. Back up the ledger using SQLite's
backup API before updating a running deployment; deploy the matching dashboard and
worker code together. Older code must not run against the migrated ledger. Keep the
runtime outside Documents and keep all private state outside Git.

For verification, run `python -m tests.test_browser_tracking`, the existing extension
unit/browser checks, and the full offline system check. The browser suite uses only
loopback fixtures, including for network submission evidence. Real-site confidence
is established during Sean's manually submitted pilot applications, one per ATS.
