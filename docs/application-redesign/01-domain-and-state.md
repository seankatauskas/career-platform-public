# Domain records and authoritative state

Status: target design. Start with the [overview](README.md). Ownership and public
operations are specified in [Modules and contracts](02-modules-and-contracts.md).

## 1. Job and application identity

**Job** means one identifiable hiring opportunity. External posting identities
describe where it was encountered. **Application** means our state and activity
for that job, including activity before submission.

Introduce stable local job identity in the application database with unique source
references such as `(ats, posting_id)`. The collector continues to own its catalog;
its rows and ranking families are not the authority for application identity.
Use a catalog read interface to resolve references and capture descriptive snapshots.
Do not infer an identity merge from a shared employer, title, or ranking family.

- One canonical job has at most one canonical application.
- Repeated delivery of an exact source identity reuses that job.
- The first job-specific mutation creates the application and requested state in
  one transaction. Examples include a note, user save, preparation task, explicitly
  targeted proposal, and a browser observation of user application activity.
- Creating this container and storing observations does not accept an inferred
  submission, interview, or outcome. An unresolved inferred target remains in
  unassigned review; candidate matching must not create applications for candidates.
- Passive collection, posting refreshes, shortlist impressions, and page views do
  not create applications. A user's explicit save does.
- An accepted discovery can create a job with known employer/role information and
  no posting URL. Unknown details remain unknown. A later catalog posting becomes
  a source alias only through verified exact identity or reviewed combination.
- Reapplication to the same job creates a new submission attempt. Returning to a
  closed application requires explicit reopening, retaining the same application ID.
- A different requisition remains a different job unless reviewed as a duplicate.

Applications owns the local identity mapping. Correspondence owns message links
to application IDs. Combining jobs uses the correction workflow in
[Workflows](04-workflows-and-external-actions.md); original IDs remain resolvable.

## 2. Records and ownership

| Record | Meaning and canonical owner |
| --- | --- |
| Application | Identity, pursuit disposition, notes, and lifecycle context; Applications |
| Submission attempt | One attempted submission, captured answers/documents, and accepted outcome; Applications/submissions |
| Browser observation | Immutable source report about browser activity; Applications/submissions |
| Message and message revision | Account-scoped provider identity and observed source versions; Correspondence |
| Message association | Reviewed relationship between a message and an application, with history; Correspondence |
| Interpretation and finding | Versioned understanding of evidence, including unknowns; Applications/Understanding |
| Internal proposal | Exact proposed operation and dependencies; the module owning that operation |
| Decision | Immutable acceptance, rejection, authorization, or correction with actor and reason; deciding owner |
| Task | An outstanding obligation with completion rules; Applications/tasks |
| Interview | One interview round and revisions to its accepted details; Applications/interviews |
| Assessment | One assignment and its requirements, deadline, and progress; Applications/assessments |
| Offer | One offer, its terms, revisions, and decision; Applications/offers |
| Reminder | A notification schedule referencing a task, interview, or explicit standalone reminder; Applications/reminders |
| External action revision | Immutable proposed provider effect; External Actions |
| Execution attempt/result | Durable external request progress and provider evidence; External Actions |
| History entry | Immutable record of an owning operation's accepted changes; that owner |

Evidence is a shared concept and reference contract, not a new universal blob
store. Email bodies and attachments stay with Correspondence. Browser evidence
stays with submissions. Document bytes stay with the existing document owner.
Evidence references identify owner, source ID, source revision/hash, source time,
and optional verified excerpt offsets. An evidence reference never grants access.

Keep three times distinct: source occurrence time, observation/ingestion time, and
decision/application time. Preserve source offsets/time zones where supplied;
normalized instants use UTC. A missing time zone or cutoff is not silently filled.

## 3. Application disposition and stage

Store disposition as `open` or `closed`. Closing requires an explicit outcome:
`accepted`, `rejected`, `withdrawn`, or `stopped_pursuing`. Employer offer withdrawal
is an offer fact; it does not silently become application rejection.

Track a `pursuit_no` starting at 1. Reopening increments it. Current obligations
and progress belong to a pursuit; notes, source identities, and full history remain
attached to the application. Reopening does not restore old reminders, approvals,
tasks, interviews, or external work. This keeps reapplication in one application
without treating old interviews as its current stage.

The displayed stage is a pure summary, with this precedence within the current
pursuit. Lower rows apply only if no higher row applies:

| Condition | Stage |
| --- | --- |
| Disposition is closed | Closed, with outcome |
| An offer is offered or negotiating | Offer |
| An accepted interview request or interview round remains relevant, including a completed round | Interviewing |
| Submission is confirmed, recruiter contact is accepted, or an assessment is accepted | Active |
| A submission is accepted as attempted, without confirmation | Submitted; confirmation pending |
| Otherwise | Tracking |

Retain explicit, versioned accepted progress facts for recruiter contact and
interview requests that have no detailed round yet. Cancelling/retracting the last
interview request/round removes its stage contribution. No state transition or
authorization may use the displayed stage as its only guard.

A direct stage correction is a `correct_progress` workflow: preview the accepted
facts/records to add or retract, then commit that correction. Do not introduce an
independently editable stage override. Retain old phase names only in compatibility
responses: Tracking maps to `preparing`, Submitted to `awaiting_confirmation`,
Closed to `terminal`; the other labels map to their existing lowercase values.

Document preparation and selection remain available while an application is open,
including after an accepted submission or interview. Each accepted submission
keeps its own immutable document snapshot; preparing a later document cannot
replace it. Serialize document selection and submission capture through the
Applications owner so a submission captures one coherent selection.

A tracking record, note, save, or pending proposal does not mean a job has been
applied to. Default recommendation exclusion uses an accepted attempted/confirmed
submission in the current pursuit, or an explicitly closed application. Reopening
starts a new pursuit and does not inherit the prior submission exclusion.

## 4. Submission, task, and appointment behavior

### Submissions

Store source observations immediately; accepted submission state is
`unreviewed`, `attempted`, `confirmed`, `failed`, or `retracted`. An unreviewed
observation does not contribute a submitted stage or recommendation feedback.
Accepting `confirmed` does not invent an earlier click or website acknowledgment.

Each attempt has its own identity. A browser acknowledgment and confirmation email
can corroborate the same attempt; link them explicitly. When several attempts are
plausible, review the relationship. Never choose the newest attempt solely by time.
Newly accepted submission feedback is unique per application/pursuit, rather than
per corroborating message. Preserve imported feedback receipts to avoid duplication.

Submission snapshots retain exact answer text, document version/hash, captured
source identity, and whether a document selection was actually observed. Changes to
today's preferred resume cannot replace submitted evidence. Missing evidence is
shown as missing. A candidate selection is not proof that it was uploaded.

### Tasks and reminders

Tasks have `open`, `completed`, `cancelled`, or `superseded` status, with responsible
party `applicant`, `employer`, or `unknown`. Retain the existing kinds and add an
explicit `book_interview` kind for portal scheduling. An unsupported request becomes
an `other` proposal needing a user description; do not mislabel it as a follow-up.

Task identity follows the accepted request or user command, not just a message ID
or task kind. One message can legitimately create several tasks. A task records
origin, due date, completion rule, and optional related interview/assessment/action.
The initial completion rules are explicit user decision and an exact linked
external-action success previously authorized to complete that task.

Attendance is never completed merely because the appointment time passed. An
unrelated outgoing message does not satisfy a reply task. A reviewed request can
close or replace specific prior tasks, with that consequence shown in review.

Snooze affects next notification time, not the real deadline. One reminder model
supports task, interview, and standalone reminder references. Task deadline reminders
use its accepted due date; interview reminders default to 24 hours and 1 hour before
the accepted time, subject to the user's notification settings. Past due times
appear in the workspace; replay/import does not immediately broadcast old alerts.
Delivery attempts are operational records, never task completion.

### Interviews, assessments, and offers

Interview statuses are `requested`, `scheduled`, `completed`, and `cancelled`.
Rescheduling is a revision of the same round, not a second interview. Requested
rounds may lack a time; scheduled rounds require a valid interval and time zone.
Retain employer confirmation separately from our personal calendar-entry status.

Assessment statuses are `requested`, `submitted`, `completed`, and `cancelled`.
Submission and employer acknowledgment remain distinguishable. Offer statuses are
`offered`, `negotiating`, `accepted`, `declined`, `expired`, and `employer_withdrawn`.
Terms retain versioned structured values plus evidence; unknown terms stay unknown.

An operation preview includes dependent task changes. Accepting a request does not
approve every other request from the same message. Accepting an offer decision can
include closing the application with outcome `accepted`, explicitly shown as part
of that operation. Declining/expiring one offer does not implicitly close a pursuit
with other outstanding offers. Closing is always an explicit consequence.

## 5. Proposals, versions, and history

An internal proposal has immutable operation input, evidence references,
interpretation/finding references, target, relevant base versions, and provenance.
Its resolution is `pending`, `applied`, `rejected`, `superseded`, or `stale`.
Missing prerequisites are recorded as blockers on a pending proposal. Invalid
analysis is a processing failure, not a valid empty proposal.

Editing creates a replacement proposal linked to its predecessor. Accepting it
records a decision and all domain changes together. Direct authorized requests
record their command and changes without a fictitious pending proposal.

Versions apply to the records an operation depends on. Adding a note must not
invalidate an interview proposal; rescheduling that interview must. History includes
command/decision ID, actor, origin, source time, recorded time, record IDs, versions,
before/after values or explicit changes, and reasons. Keep bodies/private evidence
in their owning private stores and reference them rather than duplicating them in logs.

Current records are the operational authority. History is append-only and explains
them. The historical event stream is retained during migration but is not a second
writer or a requirement to replay every event on every read. Derived view rebuilding
has no command, notification, or provider side effects.

External-action authorization and execution have separate states, specified in
[Workflows and external actions](04-workflows-and-external-actions.md).
