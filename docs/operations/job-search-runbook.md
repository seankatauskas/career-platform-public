# Local job-search pipeline runbook

The deterministic core is the source of truth. The dashboard, Outlook connector,
browser helper, local classifiers, and a future Hermes process can only use narrow
service boundaries; none owns application state or writes the ledger database
directly.

## What is implemented

- Ranked shortlists with explicit recommendation sessions and application exclusions.
- A private append-only application/event ledger with rebuildable projections,
  idempotent commands, durable work queues, an outbox, and audited approvals.
- A loopback-only dashboard for shortlist review, application timelines, evidence
  review, action approval, and system health.
- Personal Outlook all-history delta synchronization across every discovered folder
  except Junk, Deleted Items, and their descendants; encrypted sanitized content and
  eligible PDF/DOCX/ICS text; recruiting screening, temporal/event proposal review,
  and lifecycle updates.
- Calendar availability over privacy-minimized `calendarView` blocks, reply-draft
  proposals, and private tentative holds. The connector cannot send mail, add attendees,
  or accept invitations.
- A five-minute bounded worker. The ATS is fetched at 02:00, 06:00, 10:00, 14:00,
  18:00, and 22:00 America/Chicago: the 02:00 run is authoritative and the other five
  are `--new-only`. Recent board discovery runs Sunday at 03:00.
- A capability-limited Hermes adapter with bounded job, application, sanitized-mail,
  reminder, status, and proposal tools. It has no action approval, action execution,
  Graph-token, SQL, shell, filesystem, or model capability.
- An optional bearer-authenticated MCP Streamable HTTP endpoint on `127.0.0.1`, plus a
  durable notification outbox whose delivery adapter invokes only the fixed
  `hermes send` argv. Telegram credentials and routing remain inside Hermes.
- An optional private resume workbench that scores up to 25 active hand-written
  standards and presents five choice groups: all standards, an approval-gated factual
  rewrite, and three non-selectable synthetic research comparisons. It recommends the
  best job-specific standard; only the adversarial comparison receives one bounded
  score-feedback pass. See
  [the resume lab runbook](../resumes/resume-lab-runbook.md) for setup and operation.

The application ledger is `job-search.db`; it is intentionally separate from
`job-boards.db`, the preference database, and the proxy-model database.

## Offline initialization

```bash
mkdir -p ~/.config/job-search
cp examples/job-search-config.example.json ~/.config/job-search/config.json
chmod 600 ~/.config/job-search/config.json
python3 -m job_search --config ~/.config/job-search/config.json init
python3 -m job_search --config ~/.config/job-search/config.json mcp-token-init
python3 -m job_search --config ~/.config/job-search/config.json status
python3 -m job_search --config ~/.config/job-search/config.json verify
```

`init` creates/migrates the ledger as mode `0600` and seeds schedules. ATS schedules
remain disabled until `JOB_SCRAPER_CONTACT` is a real address. Outlook schedules remain
disabled until `OUTLOOK_CLIENT_ID` is a GUID. This makes an unconfigured worker safe to
run without causing network traffic.

Start the private dashboard from the unified configuration:

```bash
python3 -m job_search.system --config ~/.config/job-search/config.json dashboard
```

Open <http://127.0.0.1:8766>. The server binds to loopback regardless of caller input,
uses process-local sessions and CSRF tokens, and does not expose SQLite.

Run one bounded background tick in each independent lane:

```bash
python3 -m job_search.worker --config ~/.config/job-search/config.json --lane core
python3 -m job_search.worker --config ~/.config/job-search/config.json --lane model
```

Plan launchd installation without writing or starting anything, inspect it, then use
the explicit `--apply` switch if desired:

```bash
python3 -m job_search --config ~/.config/job-search/config.json service install
python3 -m job_search --config ~/.config/job-search/config.json \
  service install --apply
python3 -m job_search --config ~/.config/job-search/config.json service status
```

The four generated LaunchAgents invoke the core and model ticks every five minutes and
keep the dashboard and loopback MCP servers running. They contain no secrets or
environment variables; dashboard/MCP composition goes through the runtime config:

```bash
python3 -m job_search.system --config ~/.config/job-search/config.json dashboard
python3 -m job_search.system --config ~/.config/job-search/config.json mcp
```

The checked-in single-worker plist remains a legacy reference; new installs use
deterministic generation. See `docs/operations/job-search-runtime.md` for configuration, DAG,
watermark, and recovery contracts.

## Chromium application helper

Job sites frequently block framing, and browser security rules make a same-site iframe
unreliable. The implemented flow therefore opens the real ATS application page in a
normal tab and gives the local Chromium extension a one-time scoped handoff.

1. Copy `extension/autofill-profile.example.json` outside the repository, fill only the
   fields you want reused, and set its mode to `0600`.
2. Install `requirements/autofill.txt` and start the dashboard with both
   `--autofill-profile /absolute/path/profile.json` and
   `--autofill-vault /absolute/path/autofill-vault.bin`.
3. Follow [the extension setup](../../extension/README.md) to load the unpacked Manifest V3
   extension and pair its generated extension ID with the dashboard.
4. Click **Apply with extension** on one shortlist item, open its real ATS page, and use
   the extension popup. The handoff expires after five minutes and can be consumed once.
5. Review every field and submit manually. Stored private demographic, authorization,
   and sponsorship answers fill when the form offers one unambiguous choice. Uploads,
   compensation, credentials, CAPTCHA, signatures/attestations, consent, and submit
   controls remain manual. Click **Mark submitted** only after the ATS accepts the form;
   that click also commits eligible final answers to the encrypted vault.

The submission receipt is limited to the same application, ATS, job ID, and page origin,
expires after 30 minutes, and calls the existing idempotent ledger submission command.
No browser cookies or job-site credentials are sent to the dashboard.
Private values are never passed to a model, Hermes, logs, lifecycle events, or browser
storage. Custom prose is captured as history but is not automatically reused.

## ATS scheduling

Set `scraper_contact` to a real address in the owner-only runtime JSON before enabling
live scraping, then reseed schedules:

```bash
python3 -m job_search --config ~/.config/job-search/config.json init
```

An interactive `JOB_SCRAPER_CONTACT` environment override also works for a manual run,
but launchd does not persist shell exports. Re-running `init` updates enabled flags
without duplicating schedules. The scraper still uses concurrency 8. A full/`--all` or
discovery run is never launched by tests.

## Personal Outlook setup

Install the connector and secure archive/attachment dependencies:

```bash
python3 -m pip install -r requirements/outlook.txt \
  -r requirements/mail-archive.txt
```

Create a Microsoft public-client application for personal accounts, then set
`outlook_client_id` in the owner-only runtime JSON. The connector uses the `consumers`
authority and encrypted macOS persistence; it refuses a plaintext token cache.

```bash
# Read-only mail/calendar consent.
python3 -m job_search --config ~/.config/job-search/config.json outlook-auth

# Explicitly consent to either or both narrowly bounded write capabilities.
python3 -m job_search --config ~/.config/job-search/config.json \
  outlook-auth --enable-drafts --enable-holds

# On an SSH-only/headless host, display a device code instead of opening a browser.
python3 -m job_search --config ~/.config/job-search/config.json \
  outlook-auth --device-code --enable-drafts --enable-holds

# Enable schedules after configuration.
python3 -m job_search --config ~/.config/job-search/config.json init
```

The baseline scopes are `User.Read`, `Mail.Read`, and `Calendars.Read`. Drafts add
`Mail.ReadWrite`; holds add `Calendars.ReadWrite`. `Mail.Send` is rejected. The worker
uses silent authentication only; if consent or login is needed, health changes to
`reauth_required` and the user reruns `outlook-auth` interactively.

The personal Outlook live setup returned `403 ErrorAccessDenied` for
`Calendars.ReadBasic` even after consent. The connector now requests
`Calendars.Read`; existing installations need renewed consent. This permission
allows event details to be read, but the availability query still requests only
scheduling metadata, excluding subjects and bodies. Calendar writes and mail
sending are not included in this read grant.

### Optional local mail classifier

Known provider templates are handled by deterministic rules. To classify other
recruiting messages with a local model, copy `examples/mail-classifier.example.json` outside the
repository, set a fixed JSON-in/JSON-out command, and make the config owner-only:

```bash
cp examples/mail-classifier.example.json "$HOME/.job-search-mail-classifier.json"
chmod 600 "$HOME/.job-search-mail-classifier.json"
export JOB_SEARCH_MAIL_CLASSIFIER_CONFIG="$HOME/.job-search-mail-classifier.json"
```

The command runs without a shell, inherited secrets, or network access inside a
deny-by-default macOS sandbox. It receives only the same 2,048-character sanitized
excerpt retained for review and at most 20 minimal candidate records. Any configured
read paths must be absolute and should contain only the selected local model/runtime.
The fixed command must dispatch on the JSON request `task` field: the same config also
drives review-only interview/deadline extraction over encrypted-archive input.
Invalid configuration disables mail processing visibly while leaving approved Outlook
actions available. Model proposals remain review-only unless that exact producer
version and event class later pass the locked offline evaluation gate; an incomplete
candidate set is always review-only.

To remove cached accounts:

```bash
python3 -m job_search --config ~/.config/job-search/config.json outlook-disconnect
```

## Failure and recovery behavior

- Delta cursors are checkpointed page by page and committed only at a terminal delta
  link. Query version 2 starts without a date cutoff for every eligible folder; a 410
  resets only that folder's cursor.
- Transient Graph errors remain pending and honor `Retry-After`. Exhausted/invalid mail
  appears in Review with its failure details, an Outlook link when available, and
  Retry processing / Dismiss controls. Retry queues the exact message and query version
  for the next sync; Dismiss only ignores local processing and never deletes Outlook mail.
- Recognized ATS application verification-code emails are ignored before body retrieval
  and classification. They are not application-status evidence.
- Submission-confirmation templates auto-apply with aligned Microsoft DMARC/composite
  authentication, a complete candidate set, and either employer plus role/posting ID
  or one same-company application attempted/submitted within the 15 minutes before
  email arrival. Verification codes, timing alone, and multiple recent matches are
  insufficient. Company comparison tolerates punctuation and compact board slugs.
- Other recruiting correspondence retrieves candidates from all submitted applications
  and recorded browser attempts, including closed applications. Local ranking uses
  previously linked conversations, posting IDs, company names, role wording, and
  previously linked senders before limiting classifier context to 20. Known conversation
  replies are checked even without recruiting words in the subject. Uncertain model
  matches remain pending review; truncated candidate sets cannot auto-apply.
- When a new deterministic rule resolves an older pending proposal for the same
  application, event type, and email, the older proposal is superseded in the audit trail.
- Exact action approvals expire after 15 minutes. A crash after a reply draft ID is
  checkpointed resumes the patch; a crash before that checkpoint becomes
  `needs_reconciliation`. The dashboard lets the user attest that the item exists in
  Outlook, retry after confirming that no item exists, or abandon it.
- `python3 -m job_search rebuild` is a read-only projection preview. Add `--apply`
  to perform an explicit, idempotent repair.
- Notification delivery is leased and crash-recoverable. A failed `hermes send` is
  retried with exponential backoff up to the row's bounded attempt limit; permanent or
  exhausted failures remain visible in the dashboard Ops view. The cloud bridge
  records an attempt before launching `hermes send`. An interrupted or unsuccessful
  launched command has an unknown delivery outcome and stops automatic retries with
  `delivery_reconciliation_required`; this does not guarantee exactly-once Telegram
  delivery. A user must confirm delivered, not delivered, or abandoned before the
  exact notification can be acknowledged, retried, or cancelled.
  `NotificationRecoveryService` lists only redacted receipt identifiers and status.
  Reconciliation requires the current receipt attempt and payload fingerprint, plus
  a user mutation context. It records the exact intent before crossing the bridge
  and the result afterward in `command_results`; replay completes an interrupted
  bridge/application commit. A confirmed non-delivery starts a fresh bounded outbox
  retry cycle for the same immutable payload. The bridge's attempt counter stays
  monotonic and the previous outbox count remains in the audit result. The handler
  never invokes a send; normal worker delivery resumes after the transaction.
- Accepted interview/deadline reminders are checked by the existing five-minute core
  tick. Each reminder is completed only after its `reminder.due` notification is
  durable; a crash between enqueue and completion replays without duplicating the
  notification.

## Optional Hermes MCP and Telegram surface

`job_search.hermes.build_hermes_capabilities()` is the only supported assembly point.
It accepts `HermesSources`: injected job search, shortlist, ledger, sanitized mail, and
proposal interfaces. The concrete mail hooks decrypt bounded views from the local
encrypted archive; they are deliberately not an Outlook transport and never expose
Graph identifiers, tokens, keys, or ciphertext.
The proposal hooks can create an exact reply-draft or interview-slot proposal, but they
cannot approve or execute one. Reminder creation/cancellation is the only direct Hermes
mutation and cannot change application state or Outlook.

`get_application_resume_content(application_id)` reads up to 12,000 characters of
persisted factual resume text, with an explicit truncation flag and immutable
artifact/evaluation provenance. It resolves the recorded submission snapshot once
submitted, or the current selected resume while preparing. An untracked or legacy
submission returns an explicit unavailable reason; it never falls back to the newest
career profile. The tool cannot access arbitrary artifacts, research documents,
career drafts, paths, or generation services. `get_application_resume` retains its
existing compact selection metadata response.

An embedding process can pass those sources and an owner-generated secret of at least
32 non-space characters to
`job_search.hermes_mcp.make_mcp_server_from_sources()`. The production embedding is
`python3 -m job_search.system --config CONFIG mcp`; it reads the bearer token from the
owner-only `mcp_token_file` named by that config. The optional server implements
the [MCP `2025-11-25` Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
JSON-RPC initialization and tool calls at `/mcp`; it always binds
`127.0.0.1`, checks Host and Origin, and requires `Authorization: Bearer ...` on every
request. There is intentionally no default token, non-loopback bind, SQL tool,
filesystem tool, or model tool.

Point Hermes at the MCP endpoint with its environment-variable interpolation rather
than copying the bearer value into project files. A minimal Hermes MCP entry is:

```yaml
mcp_servers:
  job_search:
    url: http://127.0.0.1:8767/mcp
    headers:
      Authorization: "Bearer ${JOB_SEARCH_MCP_TOKEN}"
    supports_parallel_tool_calls: false
    tools:
      prompts: false
      resources: false
```

Start Hermes with `JOB_SEARCH_MCP_TOKEN` populated from the owner-only token file. Set
`hermes_executable` to the absolute path printed by `command -v hermes`, then set
`hermes_telegram_target` in runtime JSON to the Hermes delivery target you configured,
for example `telegram`, `telegram:<chat_id>`, or `ntfy`. A target without an executable
is rejected, and applied service installation also verifies that the file exists and
is executable. The deterministic worker sends only selected allowlisted lifecycle,
reminder, attention, system, and new-shortlist events; Hermes owns provider credentials
and phone routing. Despite the legacy field name, the target is not restricted to
Telegram.

Other streams enqueue notifications through `DurableNotificationPublisher` and a
topic allowlist. The runtime may inject
`NotificationOutboxHandler.handle_task`; extra `lane` and `workflow` payload fields are
ignored so orchestration metadata can evolve independently. The shortlist evaluation
task-kind contract is `notification.shortlist_evaluate` and the runtime composes it
after stable preference output or a drained salary branch. Delivery serializes the
bounded envelope to stdin and invokes exactly
`(configured_absolute_hermes, "send", "--to", configured_target)` with `shell=False`.
The target may be `telegram`, `telegram:<chat_id>`, `ntfy`, or another Hermes-configured
delivery target; credentials remain in Hermes.

Schema migration 6 is isolated to `reminders` and `notification_outbox`. Versions 4 and
5 remain reserved for the mail archive and runtime integration lanes. The migration has
no dependency on their tables, so it can be cherry-picked before or after them and the
migrator will apply any missing version later.

## Verification

```bash
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
for test_file in test_job_search_*.py; do python3 "$test_file"; done
```

All commands are offline. Do not use `--refresh-boards`, `--refresh-recent`, or `--all`
as a test.

## Extending Hermes

`job_search.hermes` is a chief-of-staff boundary, not a second orchestrator or database
owner. Connect an always-on runtime only through `HermesSources` and the concrete
capability factory. The dashboard/user still approves exact Outlook payload hashes,
while the deterministic worker performs approved writes. Replacing Hermes therefore
cannot change the application ledger, scheduling rules, or Outlook safety envelope.

The resume workspace remains reopenable after submission in read-only mode. At that
point the resume gateway returns exact previously recorded command results but rejects
new generation, retry, approval, or selection commands. The submission event binds the
exact selected real resume version and evaluation, or records a user-confirmed
`not_tracked` decision; both dashboard and extension require that choice explicitly.

Use `job_search.mail.build_archive_mail_source(ledger)` for the `HermesSources.mail`
member. It opens the same Keychain-backed encrypted archive and exposes only bounded
read methods: `search_mail(query, limit)` scans at most 200 recent archives and
`get_mail_message(message_id)` reads one archive where `message_id` is the opaque
`archive_id`. Results contain sanitized subject/excerpt text only; Graph message IDs,
account IDs, token providers, key metadata, and ciphertext are not part of the adapter
contract.


## Posting dates and employer-side history

The dashboard shortlist defaults to a 30-day window and orders eligible open
postings by relevance. The visible window control also supports 24 hours, 7, 14,
and 90 days. The filter uses `publishedAt`; Greenhouse historically falls back to
`updated_at` when `first_published` is missing. Collection time and first discovery
are not posting dates.

New scans retain `posted_at` and `source_updated_at` separately. Legacy Greenhouse
rows are labeled “Posted or updated” until source provenance is collected. Ashby
provides a publication date and Lever a creation date; neither adapter invents an
update timestamp. Posting dates appear on shortlist cards, role details,
application rows and the application workspace.

The **Job history** application tab reads the exact ATS posting identity. Collector
schema preparation installs `job_posting_events` triggers for first observation,
content/metadata changes, authoritative closure and reopening. Events commit with
the job update, and unchanged scans do not add events. Existing filter and board
coverage rules still govern closures. Description edits are recorded as changes
with before/after lengths; full previous descriptions are not archived. Other
changed fields include their previous and new values.

History paginates through every recorded event. First-seen and current closure
observations predating tracking are retained, but intermediate unrecorded edits or
reopenings cannot be reconstructed. “Observed” is the scan time; “Employer
timestamp” appears only when supplied by the source. Closing a posting never
changes application status or implies rejection.

Offline checks: `python3 -m tests.test_posting_history` and
`node tests/browser/test_console_browser.mjs`, alongside the collector suites.


### Shortlist refresh latency

Refreshing the shortlist reads previously saved model scores; it does not start
collection or embedding. The UI displays the score timestamp separately from the
latest catalog check, shows loading feedback, and times out the browser request
after 20 seconds without clearing the previous results.

Family-member lookups deliberately use a SQLite `CROSS JOIN` to keep the selected
families as the outer lookup. Letting the planner start at `jobs_closed_at` scans
nearly every open posting (including descriptions) for each score page. A bounded
SQLite instruction-budget regression covers this with a large open catalog.

Shortlist cards put posting dates beside the title. Today/yesterday are local
calendar dates; older dates include the year. Exact timestamps remain available
on hover, and employer update dates stay distinct from publication dates.
