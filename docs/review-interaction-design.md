# Review interaction proposal

Status: **common one-click updates, System attention, and application picker implemented; deployment pending**.
Receipt, confirmation, request, assessment, interview, and closure cards now
include a valid pending email link in their primary action. Independent updates
remain separate. Technical inputs are disclosed on demand; closure consequences
remain visible. Group digests bind the explicit decision bundle atomically.
System issues are separate from decision counts. Application clarification uses a
searchable picker with preserved selection and a pending preview before acceptance.
Sender/subject metadata, accepted-association correction UI, browser preference relocation,
and the unified activity view below remain proposed. Current behavior is documented in
[grouped evidence review](operations/grouped-evidence-review.md).

## Intended experience

One evidence card should explain one coherent proposed result and provide one
primary action. The user approves the resulting application update; the system
handles its required linking steps. Individual decisions, evidence, versions,
and reasons remain auditable beneath that simpler interaction.

Previously, grouping was largely visual: a receipt and its prerequisite link each had
a checkbox and Accept/Reject buttons, with another selected-changes button.
Raw fields and dependency instructions asked the user to operate the internal
workflow. The common path now removes that burden while preserving consequences.

Each card shows, in reading order:

1. The proposed result, application identity, and source sender/date/subject.
2. A short supporting quote and the meaningful resulting changes.
3. One primary action, **Change**, and **Dismiss**.
4. **View email** and **Review details** as secondary disclosures.

The primary action authorizes exactly the changes described on the card. It
never accepts unspecified pending operations or newly arriving proposals.

## Representative cards

The names and messages below are fictional examples.

### Receipt

> **Application received**  
> Software Engineer · Example Robotics  
> Hiring team · October 11 · Your application  
> “Thank you for applying to Example Robotics.”  
> Record this receipt and attach the email to this application.  
> **Record receipt** · Change · Dismiss

The link is an included prerequisite, not another decision. A generic receipt
does not select or invent a browser submission attempt.

### Rejection

> **Application rejected**  
> Software Engineer · Example Robotics  
> “We will not be moving forward with your application.”  
> Close this application as rejected and attach the email.  
> This will cancel 2 open tasks and 1 reminder. [View affected work]  
> **Record rejection** · Change · Dismiss

The closure consequence stays visible before approval. The affected records
expand on demand. Dismiss means disregard the proposed update, not record an
employer rejection; the email and earlier interpretation remain preserved.

### Multiple meaningful changes

> **Interview invitation**  
> Software Engineer · Example Robotics  
> Record the interview invitation and add a task to send availability by Friday.  
> Attach this email to the application.  
> **Record invitation and task** · Change · Dismiss

Change allows selective acceptance of the invitation or task. Required linking
follows whichever update is selected. Omitted changes remain pending unless the
user explicitly dismisses them; the same card says “1 update still needs review.”
No email reply is sent or calendar event created by this approval.

### Genuine clarification

> **Which application is this email about?**  
> “Thank you for your interest in joining our engineering team.”  
> Two tracked applications at Example Robotics could match.  
> Software Engineer / Platform Engineer / None of these  
> **Continue**

After selection, show the resulting update before approval. Search must allow
other existing applications. Never default an unresolved identity to the first
option. Missing record IDs and quote-validation failures are system problems,
not clarification questions the user should answer.

## Exceptions and auditability

- Wrong target: Change application prepares a revised preview. Moving an already
  accepted association uses the existing correction workflow and its consequences.
- Partly blocked evidence: explain the unresolved part; Change can approve a
  valid subset without suggesting the whole email has been resolved.
- Processing failures: show a separate, visible **System attention** section.
  Retry analysis records the user's action without requiring a written essay.
  Manual resolution offers a specific disposition and optional explanation;
  neither operation approves business changes.
- Evidence with both proposals and a processing issue keeps one source identity;
  show a system-status link within its card and a recovery row in System attention.
  The source evidence is displayed once, with links between updates and recovery.
- External actions retain their separate exact-content and recipient approvals.
- A lost response checks the original command result before offering another
  action. Stale previews refresh explicitly and preserve unsaved user choices.
- Review details expose all included/excluded changes, original quotes, record
  identities, versions, actor, reasons, and processing attempts. Activity links
  should make completed decisions and automatic recording discoverable.
- Do not offer generic Undo unless an audited correction can reverse the specific
  consequences. Preserving history is preferable to pretending a closure vanished.

## Phased scope

**Phase 1: simplify the common path.** Add deterministic presentation mappings for
known proposal combinations, compact cards, descriptive action labels, and the
Change/details disclosures. Add safe sender/subject/date metadata to grouped
responses. Use existing atomic `review_changes` with explicit proposal versions
and required dependencies; add a group-digest check to bind the displayed plan.
Unknown operation combinations keep an explicit advanced-review fallback.

Separate processing attention from actionable review counts. Count each evidence
group once; avoid counting every internal operation as another user decision.
Move browser recording preferences to Settings, with a quiet link from Review.
Keep source deep links, responsive layout, and keyboard navigation working.

**Phase 2: improve exception handling.** Add the focused application picker,
clear partial-resolution behavior, and a guided route for genuinely untracked
applications. Integrate accepted-association correction rather than bypassing it.
Give retry and resolution distinct labels and preserve bounded recovery rules.

**Phase 3: unify the audit view.** Add a source-centered activity view across
manual decisions, automatic recording, superseded proposals, corrections, and
processing attempts. This is additional read-model work, not a reason to delay
the simpler common path. No automatic approval of email outcomes is proposed.

## Acceptance criteria

- An ordinary receipt requires one click after reading its card, with no
  prerequisite selection, technical identifiers, or mandatory text entry.
- Every primary button describes its actual effect; closure consequences and
  meaningful independent changes are visible before approval.
- The submitted bundle contains only explicitly previewed proposal versions and
  their disclosed dependencies. Stale groups fail atomically without side effects.
- Selective acceptance preserves omitted work; dismissing one change cannot
  silently accept another or delete evidence.
- Genuine uncertainty asks a concrete question; technical failures provide a
  recovery action. Counts distinguish human decisions from system attention.
- Evidence, application identity, and completed decision history remain reachable
  from the card or its stable source link after the pending card disappears.
- Browser tests cover receipts, closure, mixed changes, wrong/missing targets,
  partial blockers, uncertain responses, stale previews, and narrow layouts.
