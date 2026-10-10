# Email processing and recovery

Review separates application decisions from processing problems. Understanding
owns one current processing issue for an exact email revision; Correspondence
continues to own the preserved email and which revision is current. Previous
analysis attempts remain immutable history and do not inflate the pending count.

An analysis is not complete merely because the model returned valid JSON. Its
projection must finish before the processing issue succeeds. Projection failures
remain visible and can reuse an existing valid analysis. Uncertain relevance
requires clarification. An unrelated result creates no application operations;
it can finish while retaining an explicit record of unavailable attachments.

## Recovery commands

`retry_processing` and `resolve_processing` accept `issue_id`, `expected_version`,
`expected_analysis_id`, and a nonempty `reason`. The HTTP gateway verifies the
source account; the workflow verifies the current source revision and hash.
Normal command authorization, idempotency, audit records, and transactions apply.
Command connections initialize SQLite's built-in JSON row reader before enabling
the owner write guard. This handles SQLite builds that construct the virtual
table lazily, while keeping cross-owner and schema writes prohibited.

- Retry queues a new durable work item for the existing model worker. It does not
  send mail, approve a proposal, or authorize an external action. A new failure
  becomes the current problem; successful analysis and projection resolve it.
  An explicit retry creates a fresh analysis attempt even when a previous valid
  result was uncertain. Redelivery of that same retry keeps its attempt identity.
  An inference allowance wait or an accepted job awaiting polling releases only
  the exact analysis claim. It creates no failed analysis and leaves the retry
  pending; the worker retains the due time and any accepted provider job identity.
  Previously misclassified waits can be retried through this same command without
  reopening old work or changing usage limits.
- Manual resolution records who resolved the problem and why. It makes no claim
  that analysis succeeded or that the email was unrelated. It fences queued and
  in-flight results so they cannot undo that decision.
- A newer email revision supersedes an older processing issue. The source and all
  prior attempts remain readable in history.

The mail dispatcher also reconciles obsolete work against the exact owner work
reference and current source issue. It retires superseded projection work and
cancels corresponding terminal failed worker tasks with an audit receipt, keeping
their attempts, errors, and model receipts. Another email's success never clears
a failure. Scheduled mail dispatch also gives its children an inherited workflow
ID. Recovery accepts that label only when a bounded chain of succeeded dispatch
parents leads to the scheduled mail-dispatch root, with no managed workflow or
watermark records. The receipt preserves that lineage; parents and siblings stay
unchanged. Active leases, unknown provider outcomes, scheduled work itself, and
managed workflows require their existing reconciliation path.

## Model input and diagnostics

The model receives complete source text, candidate job identities and summaries,
and current semantic records. Execution versions, mutation previews, copied job
snapshots, submission answers, and documents remain in the persisted projection
context instead of consuming the model's input budget. No-match retrieval returns
an empty candidate list rather than arbitrary applications. A bounded retrieval
that omits genuine matches still records incomplete candidate coverage.

Domain evidence validation remains exact. At the model adapter, an incorrect
character offset can be anchored to the single exact occurrence of its quoted
text in the supplied source revision. Repeated quotes with invalid offsets,
changed text, and incorrect source identities are rejected. Unicode and whitespace
are never normalized to manufacture a match.

Failure codes distinguish context budget limits, malformed JSON, invalid evidence,
truncated responses, provider failures, and projection failures. Diagnostics use
fixed safe descriptions; raw responses and private email text do not enter error
receipts. Prompt version 2 separates this interpretation contract from older
successful analyses.

## Ownership and verification

- `applications/understanding/processing.py`: current issues, attempt history
  linkage, recovery decisions, and projection outcomes.
- `application_runtime.py` and `application_mail.py`: compose source ownership,
  model inference, durable dispatch, and projection without provider calls inside
  owner write transactions.
- `application_mail_recovery.py`: proves obsolete work against current owner
  state; `recovery.py` owns the corresponding operational cancellation and
  immutable `work_owner_resolutions` receipt. This additive table is installed
  under the database migration lock without changing the existing core schema
  version, so the predecessor can still open and write its tables during rollback.
- `applications/queries.py` and `web/owner-review-view.js`: current problems,
  decisions, and independently paginated history.

`tests.test_owner_processing_recovery` covers migration, repeated attempts,
projection failure, exact retry decisions, account authorization, manual-resolution
races, and newer revisions. `tests.test_redesign_evidence` covers compact input and
exact quote anchoring. `tests.test_redesign_mail` covers worker dispatch and
preserved diagnostics. The owner Review browser test covers the recovery controls,
unknown-response retries, and separation of current issues from history.
`tests.test_owner_mail_work_recovery` covers exact work lineage, unknown provider
outcomes, account and lease guards, crash replay, paging, and the additive
operational schema migration.
