# Deterministic runtime and opportunity pipeline

`job_search.runtime` is the composition root for scheduled job-search work. It reads
one owner-only, versioned configuration file, constructs fixed handlers, and starts one
bounded worker lane per invocation. The application ledger remains the source of truth;
runtime work and workflow lineage live in the same private database, while derived job
and model state remains in its existing sidecars.

## Configuration

Copy `examples/job-search-config.example.json` to
`~/.config/job-search/config.json`, replace its absolute project root, and protect it:

```bash
mkdir -p ~/.config/job-search
cp examples/job-search-config.example.json ~/.config/job-search/config.json
chmod 600 ~/.config/job-search/config.json
```

`RuntimeConfigV1` rejects unknown fields and any version other than `1`. Relative paths
are resolved beneath `project_root`. It contains the core database paths, timezone,
dashboard/MCP ports, log directory, optional autofill paths, Outlook public client and
local account/folder identifiers, scraper contact, local classifier path, shortlist and
notification defaults, and optional Hermes container/image/data/workspace/executable/
delivery target. It also accepts the optional inference profile, portable encryption
key, resume-lab database/artifact root and model config, document-tool socket, and
pinned Tectonic executable/bundle/version documented in `docs/resumes/resume-lab-runbook.md`.
`remote_mail_inference_enabled` is a strict boolean and defaults to `false`: configuring
the shared inference profile alone never permits mail or attachment text to leave the
runtime. Set it to `true` only after approving that egress. An explicit
`mail_classifier_config` still takes precedence and keeps mail classification local.
Notification delivery selects exactly one transport: an absolute `hermes_executable`
for local launchd, or an owner-only `hermes_notification_socket` for the cloud overlay.
`mcp_token_file`, `portable_encryption_key_file`, and provider credential fields are
only paths to separate owner-only files; bearer tokens, OAuth credentials, Telegram bot
tokens, passwords, and model secrets are deliberately not config values.

Production mail synchronization discovers every eligible mailbox folder and excludes
the Junk/Deleted subtrees. `outlook_mail_folders` and the legacy singular
`OUTLOOK_MAIL_FOLDER` override remain only for the bounded compatibility mode used by
older callers and focused diagnostics.

The old explicit worker flags remain valid and override the file:

```bash
python3 -m job_search.worker \
  --project-root "$PWD" \
  --db job-search.db \
  --jobs-db job-boards.db \
  --preference-db job-boards-preference.db \
  --proxy-db job-boards-proxy.db \
  --lane core
```

## Worker lanes and opportunity DAG

Launchd wakes both worker lanes every five minutes. SQLite leases prevent overlap
within a lane while allowing one core task and one local-model task to run concurrently.

- `core`: ATS scrape, location refresh, notification evaluation, secure all-folder
  Outlook sync, accepted temporal-reminder publishing, and approved Outlook actions.
  It also materializes schedules and drains the feedback outbox.
- `model`: preference refresh, compensation-model batches, and configured
  `resume.optimize` comparison runs. It neither materializes schedules nor drains the
  outbox.

Every scheduled item receives a stable `workflow_id`; every follow-up persists that ID,
its `parent_work_id`, and its lane. The code-owned graph is:

```text
ats.authoritative / ats.new_only
  ├─> opportunity.location_refresh [core]
  │     └─> opportunity.preference_refresh [model]
  │             └─> notification.shortlist_evaluate [core]
  └─> opportunity.salary_drain [model, low priority, batches of 25]
          ├─> opportunity.salary_drain [when actionable work remains]
          └─> notification.shortlist_evaluate [after the queue drains]
```

`ats.refresh_recent` changes only the board registry and intentionally has no derived
follow-up. The scraper cadence remains 02:00/06:00/10:00/14:00/18:00/22:00
America/Chicago, with the 02:00 run authoritative and all others `--new-only`.
Scraper concurrency remains exactly 8.

The shortlist notification handler uses the same configured preference policy and
application exclusions as the dashboard. It alerts only on job keys absent from prior
alerted shortlist sessions, enforces the configured minimum and inclusive cooldown,
and durably records the alert before delivery.

The always-enabled `system.worker_tick` has one narrow job beyond materialization: it
publishes due accepted temporal reminders to the durable notification outbox. Outlook's
scheduled mail task discovers the folder tree, excludes Junk/Deleted subtrees, syncs
query-version-2 all-history cursors, and processes only count-bearing task results.

## Stable health and recovery

Migration 5 adds immutable lane/workflow/parent lineage, `workflow_runs`, and
`workflow_watermarks`. Completion records the work result and its SHA-256 watermark in
one transaction. Stable keys are:

- `ats_ingested`
- `locations_ready`
- `recommendations_stable`
- `shortlist_evaluated`
- `salary_drained`

`recommendations_stable` is the critical stability boundary. Salary is an independent,
low-priority branch and can never block it. A workflow remains `stable` while its
notification evaluation is queued and becomes `completed` after the critical-path
notification evaluation. Critical dead work marks the workflow `failed`; salary
failure does not rewrite the recommendation watermark.

`workflow_health()` returns a versioned shape with fixed lane status counters, overdue
counts, the latest value for every watermark key, and the most recent 20 workflows.
The operator `status` command includes this under `automation.workflow`.

## Launchd service management

Service changes are dry-run by default:

```bash
python3 -m job_search --config ~/.config/job-search/config.json mcp-token-init
python3 -m job_search --config ~/.config/job-search/config.json service install
python3 -m job_search --config ~/.config/job-search/config.json service uninstall
```

The result shows the four target plists, content hashes, and exact `launchctl` commands
without writing or starting anything. Add `--apply` only for an intentional change:

```bash
python3 -m job_search --config ~/.config/job-search/config.json \
  service install --apply
python3 -m job_search --config ~/.config/job-search/config.json service status
python3 -m job_search --config ~/.config/job-search/config.json \
  service uninstall --apply
```

Generated plists contain no environment variables or private values. They point to the
owner-only config. The core and model agents run `job_search.worker --lane core|model`
every five minutes. The dashboard and loopback MCP agents run the production composition
commands below as persistent services; launchd throttles an unexpected exit before
restarting them:

```bash
python3 -m job_search.system --config ~/.config/job-search/config.json dashboard
python3 -m job_search.system --config ~/.config/job-search/config.json mcp
```

Applied installation refuses to start the four-service group until the separate MCP
token exists, is owned by the current user, and is not readable by group or other users.
When notification delivery is enabled, installation also requires either the configured
absolute Hermes path to name an executable file or the configured private delivery
socket to answer the expected target/version health probe.

Logs use the configured private log directory. Reapplying an installation safely
reloads all four agents. A failed replacement restores every prior plist file and
loaded service; repeated uninstall is safe and also tolerates an agent that is already
absent.

## Linux and cloud service management

`job_search.cloud` provides the equivalent long-running, signal-aware worker and HTTP
loops for a single Linux host without changing the launchd path. The production CPU
image and five-service Compose topology, private mount layout, healthchecks, SSH tunnel,
SQLite single-replica boundary, and Linux secret-store limitations are documented in
[`docs/operations/cloud-deployment.md`](cloud-deployment.md). The optional Hermes overlay is a sixth
long-running container and keeps the official Hermes s6 supervision model intact.

## Offline verification

```bash
python3 -m tests.test_job_search_runtime
python3 -m tests.test_job_search_automation
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
```

Tests use injected command runners and never launch a scraper, local model, Outlook,
launchd service, notification, MCP server, or Hermes process.

Resume runs use a private monotonic insertion sequence rather than timestamp/UUID sort.
The sidecar also records immutable ranking snapshots and retry-command idempotency, so
same-second runs and post-completion command replays remain deterministic. Retrying a
failed resume run appends a successor with the predecessor's frozen analysis/ranking
inputs and four fresh items; it never rewrites the terminal predecessor. A queued or
running retry only re-requests idempotent delivery for the existing run.
