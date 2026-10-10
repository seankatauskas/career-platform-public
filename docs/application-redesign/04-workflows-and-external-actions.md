# Workflows, external effects, and recovery

Status: target behavior. Apply the permission rules in
[Understanding and review](03-understanding-and-review.md) to every workflow.
All scenarios below are acceptance requirements, not claims about current behavior.

## 1. First activity and submission

Collection creates catalog postings only. An authorized `add_note`, `save_job`, or
task command resolves exact job identity and creates the application if absent,
then commits its state, history, and receipt. Concurrent first actions converge
through unique identity constraints under the same command lock.

A browser report of actual application activity creates/reuses the job/application
container and records its observation/attempt source identity. Passive page reads
do not. Extension retries reuse observation IDs. Captured answers preserve exact
Unicode and whitespace; document references describe the observed attempt.

The browser or email confirmation is preserved, then interpreted. A proposal shows
which submission attempt it would confirm and the evidence. A human accepts it
through `confirm_submission`. Multiple sources can corroborate one attempt without
duplicate submission feedback. Ambiguous attempt identity requires selection.

When a confirmation arrives before browser evidence, the reviewer can accept a
submission whose click time/documents are unknown. Later observations enrich its
evidence and propose any semantic changes; they cannot substitute today's selected
resume or fabricate a previous browser event.

## 2. A request, task, and reply

1. Correspondence preserves the incoming message/version.
2. Understanding records an association candidate and request findings with evidence.
3. Review accepts the association and selected request operation. Applications
   records the accepted request and exact task once.
4. `prepare_reply` uses the accepted request and authorized correspondence context
   to create an exact external-action proposal. Preparation itself sends nothing.
5. Review authorizes the message and any explicit linked task consequence.
6. External Actions records attempts, provider identities, and confirmation.
7. A durable result invokes `apply_action_result`, completing the authorized task
   consequence once if still applicable.

Creating a provider draft is a different requested effect from sending. A draft
success does not complete a reply task. Portal assessment work and booking requests
remain their actual task kinds; a polite email is not evidence they were completed.
No generic "latest outbound message" rule completes every task for an application.

## 3. Interviews and calendar entries

An accepted interview request can create a requested round with unknown time.
An accepted scheduling proposal supplies a round identity, complete interval/time
zone, source evidence, and expected versions. `schedule_interview` creates/revises
the round and the previewed attendance task/reminder settings atomically.

Availability conflicts are shown for review. A real employer-confirmed appointment
can be recorded despite a conflict if the human explicitly accepts that fact; the
system must not erase reality or create a calendar entry to resolve it silently.

`reschedule_interview` updates the same round, revises dependent scheduling, cancels
obsolete pending reminders, and invalidates incompatible unexecuted calendar
proposals. A new calendar operation needs its own exact approval. Failure of that
operation leaves the accepted interview intact and shows calendar divergence.

`cancel_interview` records an accepted cancellation or a user's decision not to
attend, distinguishing those reasons. It closes the attendance task and reminders.
Cancelling a remote entry is a separate external action. Passing the scheduled
time creates no automatic attendance/completion assertion.

## 4. Assessment and offer decisions

Accept an assessment request into an assessment record with required outcome and
channel. Create its task only as an explicitly previewed consequence. Accept a
deadline independently when known; an ambiguous "Friday" can remain original text
pending clarification. Submission and completion use distinct operations/records.

Offers keep terms and revisions. An accepted term update changes the offer and
the previewed decision-task details. It cannot overwrite a newer negotiation
revision. Accepting an offer and closing the application can be one named workflow
whose preview lists both consequences. Declining one of several offers does not
silently discard the others. Sending acceptance/decline to an employer remains
an exact external action separate from the internal decision.

## 5. Closing and reopening

`close_application` previews and commits a disposition/outcome, cancellation of
open tasks and pending reminders in the current pursuit, and invalidation of
pending inferred updates and unexecuted incompatible external approvals. Completed
work, evidence, decisions, and provider receipts remain intact.

Preserve interview/offer facts that occurred externally. Closing marks them as
historical to a closed pursuit and stops internal work; it does not invent employer
cancellation or delete external calendar entries. Offer decisions already part of
the close command are recorded explicitly.

An external write already in flight cannot be guaranteed to stop. Record the closure
and a cancellation request, reconcile its actual outcome, and surface any needed
follow-up. Workers check applicability immediately before writing, but the design
does not claim atomicity between a local database and Outlook.

`reopen_application` increments the pursuit number and starts with no revived work.
The user can explicitly carry forward selected records through new revisions or
operations. Resending an old approved message is never a reopening consequence.

A closed application can still receive notes and correspondence. A fresh post-close
message, such as a withdrawal acknowledgment, is permitted only through an exact
human-approved action that explicitly allows that closed context; old approvals
cannot use this exception.

## 6. Correcting associations and combining jobs

### Wrong application association

`correct_association` first produces a preview using source/association versions
and the causal links from the accepted interpretation to affected records. It
lists proposals, facts, tasks, schedules, and unexecuted actions to change.

The human selects the correct target and resolutions. Apply the correction in one
transaction through the respective owners: revise the link, retract invalid facts,
cancel/supersede affected unfulfilled work, refresh summaries, invalidate affected
approvals, and append the complete decision history. Do not automatically copy
accepted conclusions into the new application; prepare new target-specific
proposals, which the same reviewed bundle may explicitly accept.

If dependent records have subsequent independent edits, the preview requires an
explicit keep/correct resolution for each affected record. Any version change since
preview aborts the bundle. Correction cannot blindly erase later human work.
Sent messages, completed actions, original evidence, and original decision records
remain. Any external repair is a new exact proposal.

### Reviewed duplicate jobs

The initial operation combines one source job into one selected canonical job.
It is never triggered automatically by model similarity or ranking-family changes.
The preview lists both applications' state and requires resolution of conflicting
current pursuits/dispositions. Do not offer a "last write wins" merge.

Preserve old job/application IDs as aliases and preserve original IDs in historical
records. Transfer current ownership through audited associations into the canonical
application, retain separate submission attempts and source identities, and keep
tasks/rounds/offers distinct unless the reviewer explicitly identifies duplicates.
New commands resolve aliases to the canonical target and expose that resolution.

Revoke unexecuted approvals whose application/context binding changes. Block final
combination while affected external executions are in flight or uncertain; resolve
those first. Completed executions and immutable payloads retain their original
identity and appear in the canonical history. This restriction avoids making result
reconciliation depend on a simultaneous identity rewrite.

## 7. External-action contract

Keep one shared lifecycle with action-specific handlers. Initial kinds are
`create_reply_draft`, `send_reply`, `create_calendar_entry`,
`update_calendar_entry`, and `cancel_calendar_entry`. Calendar writes are limited
to platform-owned private entries without attendees. Reading/linking employer
invitations is retained; accepting invitations or modifying employer-owned events
is outside the initial effect set. Employer application submission remains manual.

An immutable action revision contains:

- Kind, account, target identifiers, exact typed payload, and payload digest.
- Related application/pursuit and relevant task/interview/source versions.
- Source identity/hash, intended recipients or calendar target, and effect details.
- Optional exact internal consequence, including its target/version/completion rule.
- Proposal expiry and a stable external operation identity.

The human approval binds the entire semantic envelope, not just the email body.
Initial approval lifetime is 15 minutes, capped by proposal expiry, retaining the
current policy. Replacement content/context needs a new revision and approval.
Reconciliation remains permitted after approval expiry because it establishes what
already happened; it does not authorize another uncertain effect.

Track authorization separately from execution:

| Record | States and meaning |
| --- | --- |
| Authorization | `pending`, `approved`, `rejected`, `revoked`, `expired` |
| Execution | `not_started`, `queued`, `executing`, `awaiting_confirmation`, `succeeded`, `failed`, `uncertain`, `cancelled` |
| Attempt | Claim/fence identity, start time, request checkpoint, remote IDs, response classification, end time |
| Result delivery | Pending/applied/conflict for the separately authorized internal consequence |

Authorization plus pending durable work commit together. Claim atomically, verify
permission/expiry/relevant versions, then perform remote preflight outside the
transaction and recheck local applicability before the write. Persist the attempt
and intent before any non-idempotent effect. Never hold a local write transaction
during provider I/O.

Replies bind verified account/message/recipients and exact content. A `202` or draft
ID alone does not prove sending. Preserve the existing Sent-observation reconciliation
and content/recipient checks. Sent content that differs is recorded as observed
reality but does not satisfy the approved action's exact-success/task contract;
surface a mismatch for human resolution.

Calendar handlers use provider-supported stable request IDs and remote versions
where available, preserve ownership markers, and verify the resulting remote item.
Never claim provider idempotency without an implemented contract. An unrelated
calendar event at the same time is not proof of our operation.

Retries are type-specific. Share bounded attempt recording (initial maximum five
attempts), scheduling, and provider backoff, not a universal retry-on-timeout rule.
Known pre-effect transient failure can retry under valid approval. A write whose
outcome may have happened enters `uncertain` or `awaiting_confirmation`; reconcile
before retry. Lease expiry, missing local result, or user pressing retry is not
proof that resending is safe. Proven nonexecution can produce another authorized
attempt; expired approval requires a fresh approval before a new write.

## 8. Durable result handoff and notification rules

Save a provider result and a pending application consequence together. A worker
invokes `apply_action_result(result_id)` through Applications. The result ID is
consumed once; a crash between result persistence and task completion resumes this
handoff, not the provider call.

Applications verifies the preauthorized consequence and relevant target version.
On conflict, keep external success and show unresolved internal work. Do not reverse
success, overwrite a changed task, or infer an alternative consequence.

Reminder schedules refer to domain records; delivery uses the existing durable
notification mechanisms and owner-authorized destinations. Recheck relevance at
claim/delivery and fence cancelled leases. Delivery already in flight may complete;
record it honestly. Sending a reminder never completes the referenced task.

## 9. Failure boundaries to demonstrate

| Failure | Required result |
| --- | --- |
| Duplicate observation/command | Same source record or stored command result; no duplicate work |
| Different payload reuses command key | Explicit conflict, no writes |
| Analysis unavailable/invalid/truncated | Visible incomplete processing; no accepted inference |
| Old proposal accepted after relevant change | Conflict, no partial domain updates |
| Internal error after task/reminder write | Entire internal command and decision roll back |
| Provider timeout after possible effect | Uncertain outcome; no blind replay |
| Success persisted before application process dies | Result handoff resumes idempotently |
| Application closes during execution | Preserve actual result, suppress incompatible internal follow-up, expose unresolved consequences |
| Evidence reassignment after independent user edit | Reviewed conflict resolution required |
| Read/view rebuild repeated | No new proposals, tasks, notifications, or effects |
