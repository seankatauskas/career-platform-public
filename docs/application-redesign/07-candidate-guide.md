# Working with the isolated candidate

The candidate is a separate composition in the existing Python repository. The
normal dashboard, workers, and configuration keep their existing routing. The
candidate requires its own database, rejects legacy databases, and cannot dispatch
real external actions. Legacy writers also reject a candidate database.

## Start a local review workspace

From the implementation worktree:

```bash
uv run python -m job_search candidate serve --database .cache/application-candidate.sqlite
```

Open the loopback address printed by the command. Add a fictional job, add notes
or tasks, review proposals, inspect lifecycle records, or preview closure. The
workspace shows exact external-action approvals, but starting it never starts a
provider worker. Do not point this command at the current installation's database.

Read-only and proposal CLI calls use the same agent adapter:

```bash
uv run python -m job_search candidate list --database .cache/application-candidate.sqlite
uv run python -m job_search candidate workspace --database .cache/application-candidate.sqlite --application-id APPLICATION_ID
```

`candidate propose` reads a bounded JSON proposal from stdin and
requires `--idempotency-key`. Its shape is `{operation, input, application_id}`;
the input follows the named operation's typed contract. It creates pending review
work. It cannot approve proposals or external actions.

## Where to change behavior

| Change | Start here | Verification |
| --- | --- | --- |
| First activity, closing, reopening, combining jobs | `job_search/applications/identity.py` | `tests.test_redesign_applications` |
| Tasks and completion rules | `job_search/applications/tasks.py` | `tests.test_redesign_applications` |
| Interview timing and dependent records | `job_search/applications/interviews.py` | `tests.test_redesign_applications` |
| One-shot schedules and notification handoffs | `job_search/applications/scheduling.py` | `tests.test_redesign_scheduling` |
| Submitted answers and browser observations | `job_search/applications/submissions.py` | `tests.test_redesign_applications` |
| Preserved email revisions and associations | `job_search/correspondence/api.py` | `tests.test_redesign_evidence` |
| Findings, proposed operations, review blockers | `job_search/applications/understanding/` | `tests.test_redesign_evidence` |
| Exact external authorization, retries, reconciliation | `job_search/external_actions/` | `tests.test_redesign_external_actions` |
| Result delivery, reviewed corrections, cross-owner coordination | `job_search/applications/workflows.py` | `tests.test_redesign_workflows` |
| Shared reads and compatibility names | `job_search/applications/queries.py`, `job_search/application_compatibility.py` | `tests.test_redesign_views`, `tests.test_redesign_compatibility` |
| Authority, transactions, receipt replay | `job_search/commands/` | `tests.test_redesign_commands` |
| Composition and authenticated entry points | `job_search/application_runtime.py`, `job_search/application_transport.py` | `tests.test_redesign_transport` |

Call public owner operations with the existing transaction; do not create another
connection inside an operation. Cross-owner workflows call public operations in
one command transaction. Queries never mutate state. Shared command mechanics do
not decide lifecycle rules. `tests.test_redesign_boundaries` enforces these limits
alongside SQLite's write-scope guard.

## Explicit worker composition

`ApplicationRuntime.analyze_message` loads authorized source evidence, captures
versions, runs the injected analyzer outside a transaction, and persists analysis
before projecting review proposals. A persisted analysis resumes without another
model call. Missing bodies or attachments stay incomplete and block acceptance;
they do not become a conclusion that no work exists. Archive readers and model
providers must be injected explicitly. The local host configures neither.

`process_scheduled()` executes only due, exact, previously authorized one-shot
tasks and queues owner-notification handoffs. It sends no notification. Delivery
receipts require a trusted worker and the original reminder/version; a late
receipt cannot complete a task or overwrite a changed reminder.

`process_results()` applies recorded external outcomes once. Changed application,
message, association, or task context produces a visible conflict, preserving the
external result. The candidate's provider worker accepts test-only providers;
the production provider adapters are verified with fictional responses only.

## Agent and extension hosts

`make_candidate_mcp_server(runtime, bearer_token)` in `application_candidate.py`
uses the existing authenticated MCP host with `ApplicationAgentTools`. Tool names
remain compatible, mutations create proposals, and exact Unicode is preserved in
responses. The authenticated host supplies access; request fields cannot grant
human authority. Read-only reply preparation resolves private account/provider
identities inside trusted composition and returns blockers if unavailable.

The optional `PairedExtensionAdapter` mounts observation and submitted-answer
routes on the candidate host. It delegates authentication to the existing pairing
validator in a separate auth store, translates queued legacy requests, and records
evidence through Applications. The candidate never invokes the legacy observation
writer. Pairing/enrollment, profile autofill, and profile-fact capture remain owned
by their existing services. The local CLI leaves this integration unconfigured
until an explicit host composition supplies the pairing validator.

## Isolated historical conversion

Conversion is an explicit command, never a startup migration:

```bash
uv run python -m job_search candidate convert --source EXPLICITLY_APPROVED_COPY.sqlite --destination NEW_EMPTY_DIRECTORY
```

The destination contains `predecessor.sqlite`, `candidate.sqlite`, and
`conversion-report.json`. The source is opened read-only. The predecessor snapshot
retains historical tables, exact payloads, documents, attempts, and receipts;
accepted current records are mapped through public owner import operations.
Ambiguous or unmappable rows produce review issues. `historical_rows` in
`application_migration.py` provides bounded inspection of retained history.

A confirmation with no proven browser-attempt link remains a separate confirmed
record. Its answers and documents stay empty; the captured attempts remain
unreviewed, and the record and report expose the unresolved link. A receipt time
does not establish a submit-click time. The old automatic finalization that selected
the latest attempt after email confirmation is preserved as unreviewed evidence.
These missing links are warnings, while conflicting attempt identities remain
blockers.

One historical correction has a deterministic conversion: an explicitly authorized
association repair retaining the source application's active phase, with independent
source confirmation, accepted confirmation for the destination, and a sole current
reviewed correction link to the same evidence. The incorrect source confirmation
becomes retracted with correction provenance; the archive retains every original
event and decision. Other manual corrections still require semantic review.

Historical approvals never become executable approvals. Imported uncertain effects
remain uncertain and block unsafe combination. Imported reminders cannot deliver
until explicitly reenabled. Missing references and unresolved issues remain visible.
A successful conversion receipt is evidence about that snapshot, not permission
to activate it. The later production integration also passed a private production-copy
rehearsal; see [production readiness](08-production-readiness.md) for its results
and remaining live-provider checks.

## Before a future activation

Activation remains a separate task: reconcile the actual installed state, review
the conversion report, configure authorized private archive/model/provider access,
reconcile uncertain effects, and select exactly one routing path for each input.
Real notification delivery and provider dispatch require their own operational
verification. No release, live mailbox replay, current extension queue migration,
or production traffic switch has happened here.

## Run the combined checks

```bash
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --match redesign
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --browser
```

Browser dependencies follow the existing testing guide. The pre-commit hook runs
the redesign suites; the existing system runner and CI discover the Python suites
and include the candidate browser acceptance check.
