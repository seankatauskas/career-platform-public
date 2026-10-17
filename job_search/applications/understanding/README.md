# Understanding capability

This Applications capability writes analyses, findings, and pending proposals only.
It cannot accept facts, create tasks, or execute external actions. Review execution
belongs to the application workflow.

1. Build bounded `AnalysisInput` from authorized evidence and candidate context.
2. Claim the versioned input through `claim_analysis`.
3. Run injectable `SharedAnalyzer` outside a write transaction using the existing
   structured generation-provider interface. Record failures with a bounded code.
4. Persist via `store_analysis`, then separately project the persisted findings with
   `project_requests` or typed `project_analysis` recipes. Interrupted projection
   resumes without another model call.
   The Applications workflow compares projected closures and submissions with
   current owner records in the same transaction. Exact restatements become
   `superseded` with an `Already recorded` reason and retain their evidence and
   audit trail. This applies only to unblocked proposals with unchanged context
   versions and, for submissions, the same pursuit, status, and timestamp.
   Conflicting outcomes, changed timestamps, uncertain associations, and other
   operations remain pending. No lifecycle state or external approval changes.
5. Read `list_pending`; human review calls `resolve` in the same transaction as the
   owning operation. `replace` preserves old decisions and creates edited proposals.

`project_requests` maps all supported lifecycle fact kinds into named pending
operations. Missing typed details, interval/timezone, identity or consequence
previews remain blocked review rows; the mapper never guesses from prose.
Existing interview changes require trusted `context.interview_previews`, and
outcomes require trusted `context.closure_previews`. New fact proposals do not
implicitly create tasks or enable reminders. Separate request findings create
their own independent task proposals.

`record_source_failure` records unavailable evidence without fabricated text.
`coverage` supplies bounded operational summaries and validated finding excerpts.
Replacement proposals keep their blockers unless the human explicitly names each
resolved blocker and supplies an attributed reason.

Finding quotes are exact validated source spans. Full message text is transient.
Duplicate lineage reuses prior decisions across model versions; changed wording on
the same evidence requires comparison review. Renewed authored messages have new
source identity. Incomplete coverage stays visible and blocks acceptance.

Prompt version 9 treats routine review wording in a thanks-for-applying email
(for example, "Our team is looking over your application") as part of the receipt.
It supplies no separate review status, progress, contact, assessment, or outcome.
Independent applicant requests and substantive developments still need extraction.
The structured outcome schema permits only `accepted`, `rejected`, `withdrawn`,
and `stopped_pursuing`; acceptance means an accepted offer or employment, not
receipt of an application. Projection also guards this terminal vocabulary for
older stored analyses and providers that ignore the schema. Unsupported outcomes
remain in analysis history but produce no business proposal, including closure.
Valid receipts in the same analysis are preserved without a processing failure.
The reviewed backlog migration supersedes existing nonterminal closure proposals
without accepting receipts, changing application state, or deleting history.
Verify with `python3 -m tests.test_receipt_outcomes`.

Mail candidate retrieval uses an inclusive lexical search (`lexical_recall_v2`).
It combines posting IDs, whole employer names, partial employer/board-name overlap,
multiword role titles and earlier thread hints. Partial matches fold case, accents
and punctuation and accept an email word of at least four characters anywhere in
the compact employer name, or the compact name inside an email word. This admits
prefixes, suffixes and spelling-format variants without a list of allowed affixes.
It deliberately also admits unrelated employers; arbitrary abbreviations, typos
and aliases with no lexical overlap can still be missed.

Every candidate retains its original employer and `retrieval_reasons`. The versioned
method, scan/match/selection counts and completeness go into `candidate_retrieval`.
These fields persist with the analysis and reach the model with the complete saved
subject and body. No matched email words or bodies are copied into retrieval metadata.
The model must assess employer identity using the full email, distinguish authored
text from incidental/quoted mentions, compare competing applications, and abstain
when the evidence is ambiguous. Inclusion and rank are never identity proof.

Retrieval supplies at most 20 candidates from a scan of at most 200 applications.
Overflow is explicit and continues to block projection acceptance; the prompt also
requires null targets when retrieval is incomplete. Complete search coverage does
not establish semantic recall. Retrieval changes no job identity or accepted link;
all proposed links and receipts remain subject to human review.

For an existing receipt blocked by an empty candidate list, deploy the retrieval
fix before using **Refresh analysis** on that evidence group. Refresh rebuilds
candidate context and replaces pending proposals through the existing reviewed
workflow; it does not accept the link or tie a generic receipt to a browser attempt.
Earlier analyses and decisions remain preserved. Verify this path with
`python3 -m tests.test_review_context_refresh`.

Creating an application from review, accepting an email link, or correcting its
application queues a retry when that email revision has an unresolved processing
failure. The link and retry commit atomically. The worker rebuilds context using
the reviewed identity; the warning remains until analysis and projection succeed.
Failed attempts stay in history, and extracted receipts and other facts still
require review. A repeated command does not queue another retry. A new identity
invalidates an older queued retry and drops obsolete refresh replacement context.
Successful, manually resolved, superseded, and currently projecting analyses are
left alone. Accepting an unrelated proposal does not dismiss a processing failure.
For links created before this behavior was deployed, use **Retry processing** once
to recover the existing failure with the already accepted identity.

Verify with `python3 -m tests.test_owner_processing_recovery` and
`python3 -m tests.test_review_application_discovery`.

Prompt version 8 also receives up to 20 separate `catalog_candidates`: collected
roles without a recorded application. Catalog retrieval scans at most 20,000
board identities using the company index, then metadata for at most 30 matching
boards and 1,000 roles per board. Employer matching is inclusive; exact titles
narrow roles within a board when present in the email. Same-title locations and
closed postings remain competitors. Exact posting IDs also use indexed lookups.
Descriptions are not scanned. Every bound and lookup failure is explicit and
incomplete catalog coverage blocks proposal acceptance.

Catalog presence establishes neither application nor submission. The prompt
explains that browser tracking may have missed an application without asserting
that an extension failure occurred. A supported catalog match is a `job`
association, never an application ID; facts and requests remain unassigned.
The schema and validator enforce that separation. Review shows the candidates,
locations, closed status and model suggestion. **Use this role and attach email**
binds the source-group digest and exact candidate, rechecks catalog identity,
then creates or reuses the ATS application and attaches the email atomically.
Receipts and other proposed updates still need review. No browser evidence is
manufactured, and analysis itself creates no application. Refresh old review
groups after deployment to obtain this context.

Verify with `python3 -m tests.test_catalog_understanding` and
`node tests/browser/test_owner_review_view.mjs`.

Verify with `python3 -m tests.test_redesign_evidence`.
