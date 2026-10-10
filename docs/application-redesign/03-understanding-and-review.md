# Understanding, authorization, and review

Status: target design. This document owns the initial permission policy and the
contract between evidence processing and accepted operations. See
[Modules and contracts](02-modules-and-contracts.md) for execution boundaries.

## 1. Processing is an application capability

Applications/Understanding consumes preserved observations and bounded application
context. It produces versioned interpretations and proposed operation inputs. It
does not mutate accepted application records, send messages, or approve itself.

Use deterministic interpretation for structured browser facts and a shared semantic
analysis for relevant incoming email. Model implementations remain injectable using
the existing configured provider mechanisms. No new provider or model is mandated.
Receipt patterns can supply hints but cannot short-circuit analysis of the rest of
an email. Independent reply/task/temporal classifiers must not remain competing
authorities for the same message.

Draft and sent messages are observations with direction-specific behavior. Sent
observations can reconcile our own authorized executions; they do not create new
incoming obligations. Calendar responses/polling used by action reconciliation are
also external evidence. An unrequested calendar change can propose an application
update but cannot bypass review.

## 2. Input and finding contracts

An analysis input contains the source revision(s), current authored message text,
bounded thread context, labeled attachments, candidate jobs/applications, relevant
accepted record versions, source times, and coverage information. Label quoted
history and previous inbound/outbound text separately from current authored text.

Retain the existing maximum of 20 candidates. Incomplete candidate retrieval,
missing attachments, unreadable sources, or truncated relevant content remain
explicit. Never interpret missing coverage or failed inference as "no action
needed." The authorized evidence API preserves privacy and bounded access.

Use one versioned strict analysis contract with these fields:

| Field | Meaning |
| --- | --- |
| `relevance` | Career-related, unrelated, or uncertain |
| `associations` | Candidate job/application relationships and their supporting evidence |
| `facts` | Submission, contact, assessment, interview, offer, or outcome assertions |
| `requests` | Requested outcome, responsible party, required/optional/unclear, requested channel, and evidence |
| `temporal_facts` | Source wording, supported normalized dates/intervals, and missing time-zone/cutoff information |
| `uncertainties` | Conflicts, ambiguous references, missing context, or unsupported conclusions |

Zero facts with a valid request is valid. A receipt plus an assessment request is
valid. Several requests can come from one message. A booking-link request is not
automatically a send-availability task. Optional support language creates no
required reply task. Quoted old requests cannot independently create new work.

The service assigns analysis/finding IDs, source hashes, input fingerprint,
schema/prompt/model versions, and processing times. Each proposed finding cites
source ID/revision plus exact quote and offsets when based on text. Validate against
the supplied sanitized source, not another rendering. A matching quote establishes
provenance, not semantic correctness.

Engineering limits for the initial contract: at most 20 entries per finding array,
10 uncertainties, 5 evidence spans per finding, and 512 characters per quote.
Use the existing configurable inference budget; calculate input allowance after
reserving space for this larger output contract. Mark omitted material as incomplete.
Reject unknown fields, invalid references, non-finite confidence, unsupported kinds,
and invalid spans. Record failure separately from a successful empty analysis.

Date interpretation never invents midnight, a time zone, or duration. A request can
be reviewed without a normalized deadline. Scheduling requires complete accepted
time details; a user can supply missing details as an attributed correction.

## 3. Persistence, retries, and reanalysis

Store validated analysis before projecting proposals. A durable processing claim
prevents concurrent work on the same input/version; uniqueness also protects commit.
Input identity includes source revision, context fingerprint, and analyzer version.
Transport uncertainty can cause another inference attempt, but retries cannot
create another set of proposals or accepted records for the same findings.

Persist each proposal's source finding and operation identity. Initial projection
is idempotent on finding/operation/target. A changed model version creates a new
analysis, not automatic new work. Reconcile it against prior findings and decisions:

- Exact-equivalent findings reuse their resolved lineage.
- Rejected, cancelled, or superseded work stays suppressed on replay.
- Materially changed findings become replacement suggestions, never silent updates.
- Ambiguous cross-version equivalence is held for comparison review, not treated as
  a new actionable request merely because quote offsets or model wording changed.
- A genuinely renewed request in new authored mail has new evidence and can produce
  a new proposal, showing prior task/decision history to the reviewer.

When current application/conversation context changes during analysis, retain the
analysis but validate proposal applicability against the new versions. A newer
message is not proof that every older obligation was satisfied. Historical replay
does not create a notification flood, restore revoked actions, or rewrite delivered
briefings.

## 4. Authorization policy

These rules implement the user's explicit selections. There is no initial automatic
acceptance gate for inferred changes, including deterministic rules.

| Input/operation | Initial authority |
| --- | --- |
| Capture evidence, record analysis, or propose an operation | Bounded ingestion/analysis capability; no accepted domain change |
| Any evidence-derived lifecycle update or task | Human review required, regardless of confidence or producer |
| New inferred message association or job combination | Human review required; known immutable identity reuse is bookkeeping |
| Direct human command | Existing authenticated authority; execute after validation without a second identical confirmation |
| Agent request | Reads and proposals by default; a direct mutation requires an explicit scoped human delegation |
| Exact external career effect | Human authorization of the particular action revision and its previewed consequences |
| Scheduled work | Execute only the already authorized schedule; new inferred obligations remain proposals |
| External execution result | Record automatically; apply only internal consequences included in the original authorization |

Use trusted ingress to establish principal, origin, and delegation. An agent tool
argument cannot declare `actor_kind=user`, change inferred origin to direct, or
grant itself permission. A model's free-text claim that the user approved something
does not establish delegation. For the initial implementation, delegation binds a
named operation and exact input or an explicit, bounded schedule; no blanket grant
for all lifecycle edits. Agent-authored speculation is a proposal, not an accepted
fact disguised as a note.

Recording application containers, browser evidence, and pending proposals is not
acceptance of their inferred facts. Migration imports historical accepted decisions
with their provenance; it does not apply the new policy retroactively by asking the
user to reapprove every historical fact.

The exact-effect policy covers employer-facing mail and personal-calendar writes,
including provider draft creation. Sending may use intermediate draft calls only
as disclosed steps of the exact approved send operation. Existing opt-in reminders
and owner-facing operational/review notifications are governed by their explicitly
configured delivery schedules and destinations; they cannot send to employers or
create calendar entries. New inferred tasks cannot start reminders until reviewed.

A schedule can authorize a predetermined internal operation, such as creating a
follow-up task on a specified date. A timer deciding that silence from an employer
means a new follow-up is needed is an inference and produces a proposal. Record
the schedule's exact operation and conditions so workers can distinguish the two.

## 5. Review behavior

Group related proposals by source or connecting workflow. Each row shows the
proposed operation, exact target, evidence, accepted before/after values, dependent
internal changes, and blockers. A group is a presentation, not blanket authority.

- The user can accept some operations and reject others. Accepting a receipt does
  not accept an assessment request from the same message.
- Show prerequisite associations explicitly. Accepting an update with an unresolved
  target requires selecting the association decision too, or deciding it first.
- Submit one decision bundle for the selected internal operations. Validate all
  versions and dependencies first; if any conflict, apply none of the bundle and
  return the conflicting rows. Otherwise commit all decisions/changes atomically.
- Editing creates a replacement proposal. A trusted human can edit and accept in
  one command, provided it binds the exact edited input and expected versions.
- Rejected operations retain their decision, reason, and lineage. Reprocessing does
  not resurface them as fresh work. Stale proposals are shown as needing refresh,
  not silently recalculated under an old approval.
- External actions have a separate exact preview and decision even when adjacent
  to internal proposals in the same review screen.

For replies, show account, recipient(s), subject, full body, referenced message,
and optional task-completion consequence. For calendar writes, show operation,
account/calendar, interval/time zone, exact fields, affected remote item, and any
expected remote version. Dates, recipients, and content are never hidden behind
an "approve all" summary.

Keep rejected/resolved history accessible while default review lists contain only
pending, stale, or blocked decisions. Group uncertain execution separately from new
interpretation review so "retry" cannot accidentally authorize another send.

## 6. Completion and briefing behavior

A confirmed send can complete a particular task without another review only when
the approval explicitly included `complete task T on verified success` and the
task's relevant revision remains applicable. This is a preauthorized deterministic
consequence. Modified sent content, a different recipient, or an unlinked outgoing
message does not qualify. Keep the external success and propose/flag the internal
resolution rather than overwriting newer task state.

An interview time passing, a calendar entry appearing, or an employer responding
does not by itself prove task completion. Interpretations of those observations
follow the same review policy. A user can directly complete a task with attribution.

Briefings and attention views consume accepted records, pending proposals, and
coverage. They cannot reinterpret email or invent obligations. Show pending findings
as "Review: possible request," not "You owe a reply." Related facts can share one
presentation while retaining their own identities and decisions. A model used for
wording may summarize supplied facts only.
