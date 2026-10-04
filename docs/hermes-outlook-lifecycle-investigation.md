# Hermes application lifecycle tracking through Outlook

Investigation date: 2026-10-03. Source: `4820b58`, branch
`codex/hermes-lifecycle-investigation`. This is a proposed build sequence, not an
implementation or a statement about the live mailbox. The follow-up
[implementation guide](hermes-lifecycle-implementation.md) records the completed changes.

The main missing capability is a durable account of **who owes the next step and
what evidence would complete it**. There is already substantial mail ingestion,
application history, review, scheduling, and notification infrastructure. Extend
those services so Hermes and the dashboard can give the same evidence-backed answer.

Scope: application discovery through email, confirmation, recruiter conversations,
assessments, interviews, offers, and closure. Outlook supplies evidence; phone calls,
portal-only updates, and actions with no email need explicit user records. Missing
mail must remain unknown rather than implying an employer decision. Post-acceptance
onboarding would be an additional product scope: the present ledger ends at acceptance.

## What already exists

| Capability | Current implementation | Boundary |
| --- | --- | --- |
| Application state | Six phases, submission timestamps, append-only events, projection verification, manual correction | The phase is coarse: assessment work and recruiter contact both remain `active`. Terminal outcomes are accepted, rejected, withdrawn. |
| Outlook ingestion | Folder discovery, all-history delta traversal, durable cursors, staged processing and retries | Junk/Deleted subtrees are excluded. Runtime activation can limit processing to mail received after activation; traversal alone does not mean history has been analyzed. |
| Mail association | Local retrieval across submission-bearing application history; conversation, posting ID, company, title and sender signals; bounded model candidates | Identity matching already exists. It does not create an application from an unfamiliar recruiter thread. |
| Evidence and review | Sanitized evidence, encrypted archive, supported attachment extraction, deterministic confirmation rules and model proposals | Terminal outcomes require review; model auto-apply requires its evaluation gate. Mail lifecycle proposals have empty payloads. |
| Dates and reminders | Review-only interview/deadline extraction; accepted interviews create schedules and 24-hour/1-hour reminders | Extracting a date does not model a complete interview round or task. |
| Outlook actions | Exact-content approval, worker execution, reply drafts, private tentative calendar holds, uncertain-write reconciliation | Draft creation is not evidence of sending; a private hold is not employer confirmation. |
| Hermes | Applications, bounded timelines, status explanations, archive search, resume context, interview lists, action status, private reminders and proposal tools | No composed application workspace, application mail-list tool, lifecycle-correction proposal tool, or complete task view. |

Source anchors: [contracts](../job_search/contracts.py),
[reducer](../job_search/reducer.py), [sync coordinator](../job_search/sync.py),
[runtime assembly](../job_search/runtime.py),
[candidate retrieval](../job_search/store.py) (`list_mail_candidates`),
[matching](../job_search/mail/matching.py), [policy](../job_search/mail/policy.py),
[secure ingestion](../job_search/mail/secure_ingest.py),
[actions](../job_search/actions.py), [Hermes tools](../job_search/hermes.py).

## Correctness work to do first

These findings concern code at the reviewed revision. They do not establish that
production records have been affected.

| Priority | Finding and consequence | Proposed change and verification |
| --- | --- | --- |
| P0 | `BODY_SELECT` requests `isDraft`, but `OutlookMailCoordinator.process_pending` never checks it. All-history folder discovery does not exclude Drafts. Draft text can reach lifecycle and temporal extraction. | Reject drafts as lifecycle evidence before either extractor runs. Preserve an explicit processing reason; test draft edits and the later sent version. |
| P0 | `decide_event_proposal` defaults `occurred_at` to the review timestamp. The mail validator requires an empty payload, while `auto_apply_event_proposal` uses the evidence receipt time. The same old email therefore gets different event dates depending on approval route. | Use a validated source-event time, with receipt time as the defined fallback, and preserve separate ingestion/review times. Test delayed review and replay through both paths. Plan explicit corrections for existing records rather than rewriting ledger history. |
| P1 | `list_interviews` says “upcoming,” but `list_interview_schedules` orders all records by start time and limits them without date/status filters. Old records can fill the result and hide future interviews. | Filter before applying the limit; add bounded date/status/application queries and continuation. Test more old schedules than the limit. |
| P1 | Interview/deadline reminders live in `local_reminders`; Hermes `list_reminders` reads `reminders`. Hermes can report no reminders even when accepted interview reminders exist. | Add a unified read model with source-qualified IDs and explicit completion semantics. Keep mutation routing to the correct underlying service. |
| P1 | The ledger timeline includes `email_evidence`; Hermes' `_TIMELINE_EVENT_FIELDS` drops it and `source_ref`. `search_mail` examines at most the 200 most recently updated archive records, without continuation. | Expose safe evidence/archive references and bounded per-application mail history. Return search coverage and continuation so a partial search cannot imply no history. Do not expose raw transport IDs just to solve navigation. |

Relevant code: [mail fields](../job_search/outlook/mail.py),
[processing](../job_search/sync.py),
[store methods](../job_search/store.py) (`decide_event_proposal`,
`auto_apply_event_proposal`, `list_interview_schedules`, `list_reminders`,
`list_application_mail`), [mail proposal validation](../job_search/mail/proposals.py),
[Hermes filtering](../job_search/hermes.py),
[archive search](../job_search/mail/archive_source.py).

## Proposed build sequence

### 1. Application briefing shared by Hermes and the dashboard

Create a read service that composes the application, stage-setting evidence, linked
messages, pending reviews, schedules, actions, both reminder sources, and mail
coverage. The dashboard already composes much of this in
[`application_workspace`](../job_search/dashboard.py); extract shared service logic
and give MCP an explicitly sanitized, bounded view.

Suggested `get_application_briefing(application_id)` response:

- Recorded phase and terminal outcome, with the event that establishes them.
- Last inbound message, last observed sent message, pending drafts, and associated
  evidence references. Until direction tracking exists, report those fields as unknown.
- Next obligations, owner, due time, completion status, and unresolved questions.
- Active interview rounds, deadlines, pending decisions, and review/action links.
- Mail processing cutoff, last successful processing, backlog/failures, and whether
  the returned history is complete or truncated.

`explain_status` currently uses a fixed phrase for each phase, including “awaiting
the next employer update” for every active application. An assessment request can
require the applicant to act instead. Derive this wording from recorded obligations;
do not infer responsibility from phase alone.

Acceptance: a single tool call explains an application with a pending assessment,
an unsent draft, an interview reminder, and a mail-processing failure without
misstating its next step. Dashboard and Hermes agree on the underlying facts.

### 2. Conversation history and observed sending

Preserve a typed mail observation: inbound/outbound/draft/unknown, source times,
account and folder provenance, participants, safe thread references, and linkage
confidence. The current mail field selections omit `sentDateTime` and recipients;
the event pipeline treats mail as a receipt-oriented stream. Scanning Sent Items
does not by itself implement outbound-message tracking.

Build a bounded application/thread reader with pagination. Separate “this message
belongs to this application” from “this message changes the lifecycle.” Currently
`list_application_mail` joins through event proposals, and `analyze_mail` produces
at most one lifecycle proposal. A routine reply can belong in the conversation
without asserting a new phase. One email may also contain several independent
facts or obligations; preserve them separately with evidence spans.

Observe manual sending and reconcile it to a draft proposal when identity is
strong enough. Edits, deletion and uncertain correlation must remain explicit.
Do not mark a response sent just because the draft worker succeeded, and do not
assume outgoing candidate language is an employer decision.

Acceptance: recruiter asks for availability; Hermes prepares an approved draft;
the user edits and sends it in Outlook; the application changes from waiting on
the applicant to waiting on the recruiter only after sent evidence is observed.
Replays and folder moves produce no duplicate facts or notifications.

### 3. Obligations and follow-up policy

Add durable work items associated with an application and source evidence. Useful
fields are kind, owner (`applicant`, `employer`, `unknown`), status, due time,
source time, completed evidence, superseded item, and policy version. Support
several simultaneous obligations; a single `next_action` column is insufficient.

Initial kinds: reply, send availability, complete assessment, attend interview,
send requested document, make offer decision, and optional follow-up. Support
completion, cancellation, supersession and snooze through audited service methods.

Existing reminders notify about a time; their `completed` state can mean that the
notification was durably published. That must not mean the underlying assessment
or reply was completed. Keep delivery state separate from task completion.

Use the existing worker and notification outbox for due work, with configurable
follow-up intervals, dedupe, and cancellation when new evidence resolves the task.
Compute response age from meaningful communication, not every ledger event or
review timestamp. Silence may warrant a follow-up; it must not become a rejection.

Acceptance: sending an availability reply closes its reply obligation once;
receiving a reschedule supersedes old obligations; terminal closure stops obsolete
prompts; retries do not duplicate notifications. All proposed outgoing content
continues through the existing exact-payload approval path.

### 4. Interview rounds and Outlook reconciliation

Keep availability planning and actual interviews distinct. The current
[calendar adapter](../job_search/outlook/calendar.py) reads availability blocks in
bounded 14-day windows. It does not maintain application-linked employer meetings.
The accepted-schedule schema permits cancelled/completed statuses, but the current
store has no schedule transition operation that implements those workflows.

Add an interview identity and revision history with round/type, time and zone,
participants, location/join information, linked invitation/calendar evidence, and
status. Support proposed, confirmed, rescheduled, cancelled and completed explicitly.
Preserve uncertainty about attendance; a past end time is not proof of completion.

Link updates to the same round, cancel obsolete reminders atomically, and check
current availability when evaluating a new schedule. An overlapping revision of
the same interview must not conflict with its own obsolete schedule. Preserve
explicit handling for a genuine conflict with another interview.

Acceptance: schedule, change time, cancel, and replay the changes in different
arrival orders. Hermes shows one current interview and only its valid reminders.
Verify the Graph identity/update contract against official documentation during
implementation; this investigation did not exercise the live API.

### 5. Outlook-first application discovery and repair

Add a reviewable “untracked application or recruiter opportunity” record when
mail has strong recruiting evidence but no known application. Offer linking to an
existing application or creating a minimal external-role snapshot. Do not invent
a catalog match, job URL, submission date, or claim that outreach means applied.

`list_mail_candidates` currently requires a submission timestamp, confirmation,
or browser attempt, so a recruiter-first opportunity cannot simply reuse that
selection rule unchanged. `JobSnapshot` also requires identifiers and an HTTP(S)
job URL; define an explicit external identity contract instead of inserting fake values.

Add correction proposals Hermes can create for user review: wrong application
association, merged/reused conversation, duplicate application, superseded fact,
and reopened process. The existing manual-correction event can change phase, but
Hermes has no proposal surface for it. Preserve old evidence and the correction trail.

Acceptance: an email about a role applied to outside the extension becomes a
reviewable candidate and then an application; another role at the same company
stays separate; a corrected match reprojects obligations and schedules safely.

### 6. Assessment, offer, and closure details

After the shared task model exists, add typed assessment and offer records rather
than additional free-text phase explanations. Assessment details include due date,
submission evidence and completion. Offer details include document versions,
decision deadline and outcome; acceptance, decline, expiry, employer withdrawal,
and negotiation need explicit semantics. Do not conflate employer withdrawal with
the candidate withdrawing. Decide whether new outcomes or reviewed detail records
best preserve the existing contract and migration compatibility.

Keep posting closure separate from an applicant rejection. Surface the existing
posting history as context. Explicit acceptance remains the end of the current
application lifecycle; start-date and onboarding tracking can follow separately.

Acceptance: an offer revision changes its terms/deadline without creating a second
application; a reviewed final outcome resolves obsolete tasks while retaining the
original offer and its evidence.

## Suggested implementation cuts

1. **Correctness and visibility:** draft gate, consistent event dating, upcoming
   filtering, safe evidence references, unified reminder read view, coverage flags.
2. **Useful end-to-end slice:** shared briefing, linked inbound/outbound conversation,
   one reply obligation, manual-send observation, and deterministic next-owner display.
3. **Interview reliability:** round identity, invitation/update reconciliation,
   supersession, conflicts, cancellation and reminder repair.
4. **Coverage expansion:** Outlook-first discovery, historical replay, reviewed
   corrections, assessments, offers and optional post-acceptance work.

Historical replay should be explicit, checkpointed, bounded and versioned. Include
cutoff/completeness metadata, preserve original dates, deduplicate prior evidence,
and avoid flooding the phone with old deadlines. The normal activation cutoff
must not be silently widened to implement a user-requested historical review.

Persist authoritative state in the deterministic services. Hermes should retrieve
and explain it and submit proposals; its conversation memory should not become the
only record of a promised follow-up. Retain current review and execution boundaries.

## Verification performed

All work used the isolated worktree and temporary fixture databases. No live Outlook
mail, production databases, provider calls, or deployment changes were used. Existing
uncommitted changes in the original checkout were not included in this baseline.

- Collector baseline: `uv run python -m tests.test_job_boards` passed, 80 tests.
- Focused runner groups `hermes`, `mail`, `sync`, `ledger`, and `outlook`: 11/11
  offline suites passed using `uv run --with cryptography --with pypdf --with
  reportlab python scripts/check-system.py --match <group>`.
- A synthetic probe added `isDraft=True` to the existing authenticated-confirmation
  fixture. Its event still auto-applied. This demonstrates the absent draft gate,
  not that a real outgoing draft carries that fixture's authentication headers.
- A reviewed-event probe used an empty payload, receipt time September 1, and
  review time October 3. Its event `occurred_at` became October 3, matching review.
- The accepted-interview fixture returned its September 3 schedule when queried
  without a time bound; the investigation date is October 3. At that point the
  Hermes-backed reminder store contained zero reminders while the temporal-reminder
  query returned two due reminders.

The probes reused the sync, ledger, and secure-mail fixtures with temporary
in-process overrides; they made no source edits. Passing suites establish the
existing tested behavior, not coverage of the proposed features. Follow-up patches
should turn these cases into persistent regressions alongside their fixes.
