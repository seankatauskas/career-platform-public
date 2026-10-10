# Module ownership and operation contracts

Status: target design. The paths below are planned structure. Read
[Domain and state](01-domain-and-state.md) before changing a contract.

## 1. Target code organization

Keep the current Python application and deployment units. Introduce these focused
packages as old ownership is replaced:

```text
job_search/
  applications/
    api.py                 Public named operations and typed contracts
    workflows.py           Cross-owner application workflows
    identity.py            Local job identities and application creation
    submissions.py         Browser evidence and submission attempts
    tasks.py               Obligations and completion rules
    interviews.py          Interview rounds and scheduling consequences
    assessments.py         Assessment records and transitions
    offers.py              Offer terms and decisions
    reminders.py           One reminder model and due scheduling
    understanding/         Analysis contracts, context, validation, proposals
    queries.py             Application views assembled through owner queries
  correspondence/
    api.py                 Message/association/evidence contracts
    messages.py            Source identity, revisions, direction, ingestion
    associations.py        Reviewed message links and link history
    archive.py             Existing private archive behavior behind this owner
    queries.py             Bounded evidence and conversation reads
  external_actions/
    api.py                 Proposal, approval, execution-result contracts
    service.py             Shared action lifecycle
    reply.py               Draft/send-specific provider behavior
    calendar.py            Owned calendar-entry behavior
    reconciliation.py      Evidence-based resolution of uncertain effects
  commands/
    context.py             Trusted principal and request provenance
    authorization.py       Common permission enforcement
    transactions.py        Unit of work and command receipts
```

These are cohesive implementation homes, not a requirement to split every operation
into its own class or file. Keep persistence private to its owner; introduce a
private repository file only when it improves locality. Avoid mandatory handler /
service / mapper / repository layers for simple internal operations.

Keep provider adapters, existing durable-work infrastructure, document services,
and collection/ranking services. System composition supplies their public interfaces.
Do not turn `commands/` into a workflow engine or a domain-dependent global registry.

## 2. Dependency direction

- Transports and workers depend on public operations and queries.
- Application workflows depend on Applications internals and injected public
  Correspondence and External Actions interfaces.
- Understanding receives immutable evidence/context DTOs and returns validated
  interpretations and proposed inputs. It may persist analysis and pending proposal
  records through dedicated interfaces, but cannot import accepted-state mutation
  or external execution APIs.
- Correspondence and External Actions do not import Applications implementations.
  Cross-owner consequences are callbacks/jobs bound by system composition to named
  application workflows, not calls into lifecycle internals from a provider adapter.
- Domain owners use shared command mechanics and private persistence. Shared
  infrastructure cannot import domain implementations.
- Outlook adapters know Graph behavior and transport identity, not task completion
  or application stage rules.

Module operation calls can share a transaction. Separation of code ownership does
not require separate databases or queues between every pair of modules.

## 3. Command envelope and authority

Use explicit immutable Python dataclasses for commands and results, plus Protocols
at real adapter boundaries. Keep these independent of HTTP/MCP serialization.

| Common field | Contract |
| --- | --- |
| Request identity | Idempotency key scoped by principal and operation; retries return the recorded result, changed content conflicts |
| Principal | Trusted actor identity and capabilities constructed by the authenticated adapter; never accepted from model/request fields |
| Origin | Direct, inferred, scheduled, execution result, or migration; evidence-processing entry points cannot relabel inferred work as direct |
| Target | Existing application/record ID or exact job reference for create-on-first-action operations |
| Input | An operation-specific type with bounded fields; no arbitrary table/field update command |
| Evidence and causation | Optional source/finding references, initiating command, and decision/approval references |
| Relevant versions | Expected versions of records/associations the operation depends on, not a blanket global application revision |

Shared mechanics return explicit outcomes: `applied`, `pending_review`, or
`queued` with durable IDs and changed record versions. Domain errors include
`invalid_input`, `not_authorized`, `not_found`, `version_conflict`,
`dependency_unresolved`, `idempotency_conflict`, and `needs_reconciliation`.
Transport adapters map these to their existing error contracts without leaking
private evidence in errors or logs.

Version request/payload encoding explicitly. New exact-text action payloads hash
deterministic UTF-8 serialization without Unicode normalization of user content.
Persist the exact text reviewed and sent. Historical receipts, event payloads, and
approval hashes retain their original encoding/version and are never rehashed in
place. Do not use a display formatter as domain canonicalization.

Actor labels are attribution, not authority. Worker authority is a bounded
capability for ingestion, bookkeeping, or a previously authorized operation.
Human decisions come from the authenticated dashboard or trusted human interaction
boundary, never from the model tool channel.

## 4. Named public operations

Each entry is a behavior contract, implemented once. The APIs may group related
typed commands under a module facade; do not expose a universal SQL/event mutation
endpoint. Every command records provenance and a durable result.

| Operation | Required business input | Owner and consequences |
| --- | --- | --- |
| `add_note`, `save_job`, `create_task` | Job/application reference and exact note/task fields | Applications; ensure identity/application, then perform the authorized change |
| `record_browser_observation` | Device, source observation/attempt identity, occurrence time, captured references | Submissions; record evidence and queue understanding; no inferred acceptance |
| `record_message` | Account-scoped immutable message identity, source version, direction/times, private content refs | Correspondence; preserve revision and durable processing intent |
| `analyze_observation` | Evidence refs and bounded context fingerprint | Understanding; record analysis/findings/proposals, never accepted domain state |
| `review_changes` | Exact proposal revisions, per-proposal decisions, relevant versions | Application workflow; validate selected dependencies and apply internal operations atomically |
| `confirm_submission` | Attempt or reviewed attempt association and accepted evidence | Submissions; accepted outcome, history, one feedback receipt |
| `record_request` | Accepted request kind/outcome/channel/party and optional due time | Applications; create or explicitly refine the specific obligation, with visible dependent task changes |
| `schedule_interview`, `reschedule_interview`, `cancel_interview` | Round identity, exact accepted details/revision, evidence/reason | Interviews; update round, related task scheduling, reminders, and invalidate incompatible unexecuted actions |
| `record_assessment`, `update_assessment`, `record_offer`, `decide_offer` | Typed domain values and versions | Corresponding owner; apply the previewed record/task consequences |
| `complete_task`, `cancel_task`, `snooze_task` | Task ID/version and reason, evidence, or verified result authority | Tasks; update obligation/reminder state; snooze does not alter deadline |
| `close_application`, `reopen_application`, `correct_progress` | Target/version and exact reviewed consequences | Application workflow; apply lifecycle rules described in the workflow document |
| `link_message`, `correct_association`, `combine_jobs` | Exact source/target identities, versions, and conflict resolutions | Connecting workflow; calls correspondence/identity/domain owners under one transaction |
| `prepare_reply`, `prepare_calendar_change` | Exact target and proposed effect, evidence, optional task consequence | External Actions; save immutable proposal; no provider write |
| `authorize_action`, `reject_action`, `revoke_action` | Action revision/hash, trusted decision, context dependencies | External Actions; authorize exact effect or record decision; authorized work is enqueued atomically |
| `execute_action`, `reconcile_action` | Authorized action/attempt or recorded reconciliation evidence | External Actions; provider-specific execution and durable outcome |
| `apply_action_result` | Result ID and original authorized internal consequence | Application workflow; idempotent task/domain consequence without another provider call |

An internal accepted operation may have several named consequences, such as
rescheduling a round and its reminders. The review preview and result list those
consequences. Generating a new external-action proposal is allowed; authorizing it
is always separate.

## 5. Transaction ownership

The outer command executor owns one `BEGIN IMMEDIATE` transaction for a local
mutation. It checks the receipt and relevant versions after taking the lock.
Its unit of work supplies transaction-bound public module operations. Those
operations reuse the same connection, enforce owner rules, and never commit or
open a second write connection. SQL stays in the owning module.

Commit together: accepted domain changes, decisions, revisions, proposal resolution,
command receipt, and pending durable work. Response encoding failure also rolls
back. A cross-owner workflow calls the public transaction participants; it does
not receive a license to write their tables itself.

Perform model inference, provider preflight/network calls, and document parsing
outside write transactions. Capture versions before the call and check the relevant
versions afterward. A query or projection rebuild never calls a mutation operation.

Use a durable outbox/work record for each required background handoff. Delivery is
at least once; consumption uses the stable result/command identity. Save external
results and their pending application consequences in the same transaction. Leases
fence competing workers; expired leases do not make uncertain external writes safe.

## 6. Queries and transport compatibility

Provide typed bounded queries for application workspace, conversation, evidence,
tasks, history, pending reviews, and action execution. Include stable pagination,
coverage/truncation, record versions, and permitted next operations. Missing or
unavailable evidence is explicit, not an empty history.

Compound views use owner query participants within a single read snapshot when
consistency matters. The assembler may combine DTOs but not repeat business rules
or cross-query private tables. Persisted briefing delivery/history is operational
state; its application facts still come from the shared query surface.

Retain existing `/api/v1` routes and MCP tool names where their meaning is compatible,
using thin adapters into the new operations. Add typed request/result contracts for
new commands; generate/distribute transport schemas from those contracts. Reject
legacy auto-apply calls under the new review policy rather than silently allowing
them. Do not keep old writers behind a compatibility endpoint.

Neither adopting FastAPI nor converting the dashboard to TypeScript is required.

Keep the existing dashboard layout and its Applications, Shortlist, and Review
sections. Applications presents owner records through the existing Overview,
Messages, Answers, and Documents tabs. Review presents proposed changes, exact
external-action approvals, and processing problems across applications, including
unassociated evidence; successful processing belongs in collapsed history. The
module boundaries determine who reads and changes state, not the navigation layout.
The isolated candidate interface is for local development only.

Keep existing authentication, CSRF, paired-extension, and bounded-agent protections.
Add capability reads exposing allowed operation names and reasons, not unrestricted
database access or executable handler names supplied by a model.

## 7. Boundaries that must be executable checks

Add a targeted architecture suite over the new owners and migrated callers:

- Transports, views, provider adapters, and workers cannot import domain repositories
  or directly execute domain SQL.
- Cross-owner imports must target public contracts. Calls to another owner's private
  methods, `.store`, or database path are rejected.
- Understanding cannot import accepted-state mutation/execution implementations or
  issue effects; analysis/proposal persistence is its bounded write surface.
- Shared command infrastructure cannot import domain implementations.
- Domain dependency cycles are rejected. System composition is the explicit wiring point.
- Each owner declares its tables; tests reject writes through non-owning participants.

Use explicit file/path ownership rules, not fragile searches for a few method names.
Keep compatibility exceptions narrowly listed with the slice that removes them;
new code cannot add exceptions to make checks pass. Run these checks in the existing
system runner and CI. Prove they fail using fixture snippets or temporary source
trees, without leaving intentionally broken production files.

An agent adding an operation should find its owner, define its typed input/result,
implement its rules and transaction consequences there, expose it through thin
adapters, and add owner/acceptance tests. It should not need to modify a global
business switch statement or duplicate behavior for each transport.
