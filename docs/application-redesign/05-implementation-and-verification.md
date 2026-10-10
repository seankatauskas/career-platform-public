# Implementation sequence and verification

Status: implementation specification. Implementation was subsequently authorized;
see [implementation evidence](implementation-status.md) for results and limitations.
Read the [overview](README.md) and the four contracts documents first.

## 1. Scope and working baseline

Build from the then-current application baseline in an isolated branch/worktree.
Record its exact revision and inventory local work before starting; the inspection
baseline for this package is recorded in the overview. Do not overwrite the user's
working tree or silently import its uncommitted changes. Reconcile needed fixes
through focused commits when implementation is authorized.

The replacement covers application identity/lifecycle, connected correspondence,
Understanding, review, external career actions, and their views/entry points.
Collection, ranking, shortlist selection, profile editing, document generation,
document libraries, infrastructure, and unrelated operations stay outside the
redesign except for required public-interface integration.

No new ATS scan, live inference, mailbox replay, provider write, production database
access, merge, deployment, or cutover is part of this implementation verification.
Use fictional fixtures and copied state only when explicitly supplied for that use.

## 2. Current source map and ownership replacement

These links identify current code to inspect, not interfaces to preserve indefinitely.

| Current source | Target disposition |
| --- | --- |
| [Ledger store](../../job_search/store.py), [service facade](../../job_search/service.py), [reducer](../../job_search/reducer.py) | Move business rules into owners and explicit workflows; retain thin compatibility reads/commands and historical event decoding |
| [Lifecycle service](../../job_search/lifecycle/service.py), [core](../../job_search/lifecycle/core.py), [interviews](../../job_search/lifecycle/interviews.py) | Replace mixin composition, implicit cleanup, and duplicate task/schedule writers with focused Applications internals |
| [Lifecycle mail](../../job_search/lifecycle/mail.py), [archive ingestion](../../job_search/mail/archive_source.py), [mail pipeline](../../job_search/mail/pipeline.py) | Separate Correspondence ingestion/associations from Applications/Understanding and reviewed application consequences |
| [Browser tracking](../../job_search/browser_tracking.py), [answer capture](../../job_search/application_answers.py), [documents](../../job_search/application_documents.py) | Retain pairing/capture/evidence fidelity behind Applications submission operations and existing document interfaces |
| [Draft/hold executor](../../job_search/actions.py), [career actions](../../job_search/career_actions/service.py), [commitment projection](../../job_search/career_actions/lifecycle.py) | Consolidate effect ownership, exact approvals, provider attempts, and explicit result handoff |
| [Attention](../../job_search/attention/service.py), [portfolio view](../../job_search/attention/portfolio.py), [briefing](../../job_search/lifecycle/briefing.py) | Consume shared accepted records/proposals/coverage; retain delivery preferences and history without independent mail interpretation |
| [Dashboard](../../job_search/dashboard.py), [Hermes](../../job_search/hermes.py), [human interactions](../../job_search/interactions/service.py), [system composition](../../job_search/system.py) | Thin typed adapters and trusted authority boundaries; inject the new public operations |

### Reuse from PR #39

Inspect the pinned candidate `2074cc4dfbe695c294ba5a289372ebb45d2abb90` through
[the PR](https://github.com/seankatauskas/career-platform/pull/39).
Reuse selectively: explicit command transactions, receipt replay, rollback tests,
source-preserving document/answer handling, and adapter ownership checks.

Do not merge the entire PR as a prerequisite. Its Python/HTTP/TypeScript migration,
generation retirements, research split, release packaging, and cutover assumptions
are separate decisions. Its event-to-task inference and draft-only product limits
do not override this design or the current approved-send capability.

The current [shared email-understanding design](../mail-understanding-design.md)
is useful for fixtures and coverage requirements. Replace its automatic acceptance
policy with review of every inferred update. Remove the interim independent reply
detector from the migrated path rather than retaining another inference authority.

## 3. Ordered implementation slices

Each slice includes its public contract, focused tests, current-source replacement,
and a documented compatibility adapter where still needed. Keep the alternative
installation isolated until all slices pass; no slice changes production routing.

### Slice 1: command foundation and first application activity

Implement trusted request context, receipt-backed transactions, owner-bound
transaction participants, local job identities/aliases, application creation,
notes, explicit saves, and direct tasks. Deliver a dashboard and agent fixture
calling the same public operation. Establish architecture checks over the new code.

Acceptance: concurrent first operations create one application; different receipt
payloads conflict; denied/failed commands leave no partial application/task state;
reads create no application. Delegate no authority from caller actor strings.

### Slice 2: evidence to reviewed request, end to end

Move mail identity/revisions/archive access behind Correspondence. Implement
Understanding's versioned multi-finding contract, processing claims, proposal
projection, and grouped per-operation review. Deliver the full fictional email →
interpretation → reviewed association/request → task → workspace flow.

Include a receipt plus mandatory assessment fixture and an optional-support-only
receipt. Ensure every inferred update is pending until reviewed. Restore processing
from persisted analysis after an interrupted projection without another required
semantic call. Remove rule-short-circuit and independent task-inference behavior
for messages owned by this path.

### Slice 3: submissions and remaining lifecycle records

Route browser capture into preserved observations and reviewed submission commands.
Preserve answer bytes, document identities, pairing, and pending extension queues.
Implement interview, assessment, offer, task/reminder, closure/reopening, and pure
stage-summary contracts. Consolidate duplicate active schedule/reminder writers.

Acceptance: out-of-order browser/email confirmations, one submission feedback
receipt, exact submitted-document resolution, requested versus scheduled interview,
rescheduling rollback, task deadline versus snooze, and explicit reopening without
revived old work. A changed phase label must not silently change the API contract;
translate legacy phase values in compatibility adapters.

### Slice 4: external actions and result handoff

Consolidate provider draft, exact approved send, and owned calendar changes into
External Actions. Preserve current provider safeguards and improve context binding.
Implement atomic authorization/enqueue, fenced claims, per-kind recovery, and durable
`apply_action_result` handoff. Bind optional task consequences in the exact preview.

Acceptance: proposal edits require new approval; agents cannot authorize; a `202`
is not sent confirmation; wrong-account/content results cannot complete tasks;
uncertain writes do not retry blindly; late result processing does not resend.
Test post-close exceptions only through fresh exact human authorization.

### Slice 5: corrections, combination, and interface parity

Implement previewed evidence reassignment and reviewed job combination with causal
references, relevant version checks, preservation of independent edits, and alias
resolution. Route dashboard, extension, MCP/CLI, trusted human interactions, and
workers to the same owners. Keep old transport names only as thin adapters.

Workspace, review, briefings, and attention use shared queries. Expose incomplete
coverage and unresolved execution honestly. Delete retired cross-owner private
calls and semantic detectors. Every removed path must have replacement behavior
coverage; do not retain a second writer to satisfy an old implementation-shaped test.

### Slice 6: isolated conversion, full verification, and handoff

Implement an offline snapshot conversion into a separate paused installation and
run the acceptance matrix against it. Produce source/destination semantic receipts,
missing-reference reports, and verification output. Complete operator-facing
documentation of differences and remaining live prerequisites. Stop before any
production activation or release action.

## 4. Data conversion and compatibility decisions

Reuse the application's SQLite database boundary and checksummed migration chain.
Do not edit shipped migration bytes. Add versioned owner tables/read adapters where
existing shapes cannot express the new records. The converter uses isolated
snapshots; a fresh demo seed is not a substitute for historical-state conversion.

Preserve application/source IDs, document bytes/hashes, exact answers, message
revisions, proposals/decisions, provider request IDs, attempts, command receipts,
notification delivery history, and unresolved external outcomes. Import old events
as read-only history and translate accepted current state into owner records with
explicit migration provenance, including progress facts when no detailed record
exists. Do not fabricate missing evidence or pretend old approvals met new contracts.

Map legacy interview schedules to existing rounds using recorded links. Do not
merge two rounds merely because times match. Carry unresolved duplicate candidates
as migration review issues. Convert general/local/interview reminders into one
model with stable legacy references; preserve sent/dismissed state and avoid double
delivery. Missing source IDs or ambiguous active references block candidate acceptance
unless explicitly represented as unresolved and inert.

Imported accepted historical facts stay accepted. Imported pending internal
proposals stay pending and are revalidated before decision. Old external approvals
are retained as historical authority but not executable under the new effect model;
unstarted effects require fresh exact approval. Executing/uncertain effects retain
their identifiers and uncertainty for reconciliation, never reset to queued.

The candidate starts paused, with external dispatch disabled. A copied lease or
outbox must not run merely because the new process starts. Reconcile uncertain
work before any future separately authorized activation. Read-time expiration may
be displayed without mutating stored state; expiry transitions are explicit worker
bookkeeping, not a side effect of viewing the application.

Exactly one implementation owns each migrated record's mutations. In the candidate,
legacy writers delegate or reject; they never run alongside new writers. Comparing
read-only/shadow results is allowed, dual delivery or dual task creation is not.
Migration/import may access historical layouts through dedicated conversion code;
ordinary runtime workflows cannot use that exception.

Source snapshots remain intact. Live recovery, installed predecessor discovery,
maintenance windows, release approval, and post-write recovery are future operational
work. The existing [deployment guide](../operations/aws-deployment.md) remains the
project's authority for those separately authorized activities.

## 5. Acceptance matrix

Add owner-level tests with temporary databases and fake providers, plus workflow
and transport acceptance. Test public behavior rather than private method layout.

| ID | Scenario and required evidence |
| --- | --- |
| ID-1 | Catalog/view leaves no application; first authorized state creates exactly one under concurrent requests |
| ID-2 | Exact source identity reuse, external job discovery, same-job reapplication, and reviewed aliases preserve history |
| EV-1 | Duplicate/out-of-order mail/browser revisions cannot overwrite newer evidence or duplicate business records |
| EV-2 | Submission attempt/confirmation distinction; exact Unicode answers and historical document identity survive changes |
| UN-1 | Receipt plus assessment produces independent findings; optional contact/rhetorical text produces no obligation |
| UN-2 | Missing body/attachment/candidate coverage and invalid output remain visible, never accepted as no work |
| UN-3 | Reanalysis cannot recreate rejected/cancelled work; renewed authored requests remain reviewable |
| AU-1 | Every inferred update requires review, including deterministic receipt/browser rules; actor spoofing cannot bypass it |
| AU-2 | Scoped direct human/delegated commands execute once; model tools cannot approve their own proposals |
| RV-1 | Partial review, explicit dependencies, edited replacements, stale versions, and all-or-nothing selected bundles |
| LC-1 | Interview reschedule updates dependent internal scheduling atomically; injected failures roll back all of it |
| LC-2 | Notification delivery/time passing do not complete tasks; snooze does not alter deadline |
| LC-3 | Offer decisions, closure, and reopening preserve external facts and never resurrect old actions/work |
| FX-1 | Exact account/recipient/content/context approval; edited, expired, revoked, or superseded effects cannot execute |
| FX-2 | Provider timeout, process death after intent, and lease loss retain uncertainty without duplicate effects |
| FX-3 | Success before task-update crash resumes only internal result delivery; changed target becomes conflict |
| CR-1 | Association correction preserves evidence/history, detects independent edits, and requires review of new-target facts |
| CR-2 | Reviewed job combination resolves state conflicts; affected uncertain executions block combination |
| VW-1 | Dashboard, agent, and briefing agree on accepted/pending/uncertain records; reads and rebuilds are side-effect free |
| BD-1 | Architecture fixtures prove forbidden imports, cross-owner SQL/private access, and Understanding effects are rejected |
| MG-1 | Snapshot conversion preserves identities/hashes/counts/decisions and remains paused with no external dispatch |
| MG-2 | Legacy transport adapters reach the same owners; retired auto-apply and duplicate writer paths cannot execute |

Use existing suites as behavior references: [ledger](../../tests/test_job_search_ledger.py),
[lifecycle core](../../tests/test_lifecycle_core.py),
[lifecycle integration](../../tests/test_lifecycle_integration.py),
[lifecycle mail](../../tests/test_job_search_lifecycle_mail.py),
[browser tracking](../../tests/test_browser_tracking.py),
[career actions](../../tests/test_job_search_career_actions.py),
[human interactions](../../tests/test_career_interactions.py),
[mail identity](../../tests/test_mail_identity.py),
[attention](../../tests/test_attention.py), and
[Outlook](../../tests/test_job_search_outlook.py).
Preserve meaningful guarantees; update assertions deliberately where this blueprint
changes policy, particularly automatic acceptance and implicit task production.

## 6. Commands and validation evidence

Current commands, verified against [the testing guide](../../tests/README.md):

```bash
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --match lifecycle
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --match career
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py
```

For integrated browser acceptance, provision the documented local test dependencies:

```bash
npm ci --prefix extension
npx --prefix extension playwright-core install chromium
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --browser
```

New Python suites belong in `tests/` and are discovered by the existing runner.
Wire boundary and contract checks into that runner and CI; demonstrate each new
guard with a failing fixture. Browser suites remain in their documented locations.
Do not use #39's `--tier` flags on the current runner, which does not support them.

This documentation change runs both collector commands before/after editing and
checks Markdown links, source paths, and whitespace. It does not run or claim the
future acceptance matrix, full release acceptance, or live provider verification.

## 7. Definition of done

- All named workflows and permission rules work through the shared public owners.
- Current state has one writer per concept; old event reducers, legacy schedules,
  and separate reply classifiers are not parallel business authorities.
- Source evidence, exact approvals, and execution uncertainty remain inspectable.
- The acceptance matrix and integrated offline/browser checks pass on the combined
  source, with receipts that name the revision and limitations.
- An engineer can locate rescheduling in interviews, interpretation in Understanding,
  and send recovery in External Actions without tracing private cross-owner calls.
- Owner READMEs name public operations, affected records, and focused test commands.
- Isolated migration reports differences and leaves the candidate paused.
- A final implementation report states what changed, tests run, preserved behavior,
  intentional policy changes, and any remaining operational prerequisites.

No checklist item here authorizes deployment. A later production plan must be
reviewed against the real installed state and the project's release workflow.
