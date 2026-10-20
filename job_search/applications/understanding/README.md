# Understanding capability

This Applications capability writes analyses, findings, and pending proposals only.
It cannot accept facts, create tasks, or execute external actions. Review execution
belongs to the application workflow.

The current prompt 16 separates extraction, source support, verification and
identity through checkpointed stages. Extraction sees sources
without database targets. Independent semantic verification challenges each
extracted hypothesis against the complete source and flags possibly missing
substantive findings. Identity binding sees only supported findings and uses typed
schemas: receipts, associations and requests cannot select existing record IDs;
interviews, assessments and offers can select only their matching record kind in
the chosen application. Catalog targets are available only to associations.

Models select immutable evidence block IDs. Python supplies the complete exact
block text and source offsets, including repeated text, Unicode, whitespace and
bounded chunks of long lines. Models never transcribe evidence or count offsets.
Persisted evidence and approval contracts remain exact source spans. All supplied
source text remains untrusted evidence in every stage.

Unsupported hypotheses removed by verification are recorded as content-free
corrections in diagnostic version 3, separately from unresolved issues. Corrections
alone do not keep a processing warning open. Uncertain semantics, missing coverage,
malformed verification and failed bindings retain valid siblings and remain
visible as partial processing. A fact supported only by outgoing or earlier-thread
content cannot become a new incoming fact or request. The verifier is a separate
model judgment, not a factual guarantee; independently labeled replay still gates
qualification. No stage approves proposals or modifies accepted application state.

Each stage receives only its own output contract. Semantic verification can make
one correction request for malformed output, with already verified findings held
fixed. Only failed checks are requested again, while the full source and previous
verified findings remain available for coverage assessment. Successful extraction
and verification responses are checkpointed across an allowance deferral during
repair. Extraction, verification and identity each have at most one repair, so a
whole analysis has at most three correction calls. Identity and record correction
share one correction budget. Unresolved verifier siblings
remain unassigned without discarding independently verified findings.

Identity is selected once for the whole current email, matching the correspondence
owner's unique message-to-application association. The model returns one canonical
target and identity evidence. Code creates one association and propagates an
application target to every supported current-source fact and request. Catalog
targets keep application facts unassigned until a human creates or links a record.
Ambiguous identities abstain globally; explicit conflicting pursuits and contexts
with other than one current authored source remain partial. Earlier thread messages
remain supplemental context. A selected identity conflicting with a reviewed
message link cannot silently replace it.

Only interview, assessment and offer findings receive separate record selectors.
They never carry their own application target. Record-kind IDs are bounded by the
schema, ownership is checked against the canonical application, and one targeted
record repair can preserve the fixed identity and valid sibling selections.

Identity input deliberately omits extraction's forced-null targets, confidence
values and lexical retrieval reasons. Those fields do not express prior matching
judgments. Actual reviewed links remain explicit context. Every candidate remains
available for comparison; this change does not narrow retrieval or discard rivals.
Competition requires source-supported employer identity and compatible role
details. Unrelated substring matches and hypothetical unlisted applications do
not create ambiguity by themselves. Actual compatible competing roles still
require abstention, as do explicitly incomplete searches or evidence naming a
different unlisted role. These are proposal-matching rules, not auto-approval.

Contact or temporal hypotheses trigger a small source-only support inventory.
It receives complete sources and requested topics, without model claims, candidate
applications or confidence values. It distinguishes substantive personal exchange
from routine automated communication, and meaningful time commitments from generic
employer response estimates. Exact block evidence grounds its classifications.
Routine interpretations are removed as corrections; unclear support stays partial.
Real interview, assessment, offer and applicant deadline times remain eligible.
Inventory failures cannot silently confirm or discard a claim, and allowance
deferral reuses completed extraction checkpoints.

Strict wire-valid, exactly grounded hypotheses that fail domain mapping are
quarantined for semantic assessment. An unsupported interpretation can be rejected
deliberately and its mapping warning resolved. Supported or unclear quarantined
hypotheses retain their original diagnostic and never reach binding or persistence.
Positive independent source evidence prevents a normalization error from erasing a
real commitment. Malformed transport fields and unverifiable citations cannot use
this path to clear their errors. Valid sibling findings remain reviewable throughout.

Run `python3 -m tests.test_mail_typed_pipeline` for generalized schema, semantic,
evidence-boundary and checkpoint regressions. Existing runtime suites exercise
pending-only projection and recovery through the same domain contract.

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

## Block citations and staged analysis (prompt 10)

`evidence.py` partitions each exact source revision into addressable text blocks.
Every character is preserved; the block identity includes source ID, revision and
hash. Models return a block ID and verbatim quote, never character offsets. Code
resolves the quote within that block and preserves the existing exact-span domain
contract. Repeated subject/body wording is unambiguous across blocks. Ambiguous
repetition within one block, normalization and invented text still fail validation.

`SharedAnalyzer` first extracts findings without candidate or existing-record IDs.
A separate identity request receives the validated findings and bounded candidate,
record, catalog and thread context. It can bind identities, but cannot rewrite the
facts. Matching evidence becomes an independent link prerequisite. Generic receipts
never acquire an invented browser-attempt identity. Ambiguous identities stay null.

Each phase gets at most one corrective call. Extraction repair receives only the
failed findings and content-free validation diagnostics; verified siblings are
preserved. Identity repair cannot replace verified extraction. Successful findings
are persisted even when another finding remains invalid. Such an analysis produces
pending proposals and a separate `partial_analysis` processing issue, with category,
position, stage and reason. An unresolved fact is not silently discarded as success.
Partial issues do not trigger repeated whole-email automatic retries. Explicit
human retry remains available and retains all earlier analysis/decision history.

Every inferred application change still requires human approval, including routine
receipts and email links. This change does not enable receipt auto-approval or alter
the independent paired-browser recording policy. Exact previously recorded facts
continue through the existing reconciliation workflow.

The new `understand_stage_checkpoints` table retains bounded schema fields between
provider stages. It never stores prompts, complete source inputs, unknown response
fields or credentials. Results are fenced by the input fingerprint, model/prompt,
request digest and current claim lease. Allowance deferral or lease recovery reuses
completed stages; terminal persistence clears the checkpoints. A fresh explicit
retry can request new model output. A crash between a remote synchronous response
and its checkpoint still uses the existing inference reconciliation mechanism.

This is a migration release under `docs/operations/deployment-modes.md`. The explicit
owner migration adds the checkpoint table and preserves prior findings, proposals,
claims and decisions. Production startup only checks state; it cannot create it.
The feature does not change release policy or authorize an installation.

Verification: `python3 -m tests.test_mail_evidence_blocks`, the existing evidence,
receipt, catalog, processing-recovery and inference-deferral suites, and the full
system runner. `python -m tests.probe_receipt_analysis --help` describes an opt-in
live-model replay using private cases and a configured provider. It creates only
temporary application databases and never writes to the production dashboard.

## Reliability contract and semantic evaluation (prompt 11)

The unit of recovery is an individual finding or identity binding. Provider stages
remain checkpointed and lease-fenced. Schema errors identify a bounded field path,
such as `value.status`, alongside the failed rule. Repair receives this path;
unknown model keys and values never enter persistent error diagnostics. Nullable
alternatives retain the useful value error. Unexpected identity bindings are
rejected and repaired instead of silently disappearing. Existing diagnostics remain
readable. Partial results remain visible even when every retained finding is valid.

`evaluation.py` measures correctness separately from valid output. Each case supplies
exact source revisions/hashes, model-visible candidate/catalog/thread/current-record
context, and independently reviewed expected findings and targets. One-to-one
matching counts missing findings, unexpected findings, duplicates, wrong targets and
unresolved processing. A correct receipt assigned to the wrong application fails.
Schema-valid empty output fails when a fact is expected. Ambiguous cases can require
null targets. Expected structured values can require `status: confirmed` or
`outcome: rejected`. Label the complete email: a thanks-for-applying subject can
introduce a rejection.

Run the live evaluator explicitly against a private corpus and configured profile:

```sh
python -m tests.probe_mail_reliability --cases /private/cases.json \
  --config /private/inference.json --output /private/new-report.json --repeat 3
```

This makes billable model calls, validates the corpus before inference, and requires
a new output file. It never connects to the production application or writes
application state. Reports identify model, prompt, input fingerprints, finding
types/targets, diagnostics, token counts and stage latency. Source bodies, quotes
and arbitrary provider usage are excluded. Any semantic failure or incomplete
result causes a nonzero exit. Repeated runs do not inflate independent message count.

Offline contracts are enforced by `python3 -m tests.test_mail_analysis_reliability`,
discovered by the system runner used in CI. They cover wrong targets, omissions,
ambiguity, duplicates, precise correction, unknown bindings, partial results and
resuming a deferred repair without repeating extraction. Existing runtime suites
verify pending-only projection, lease fencing and preserved decisions.

A passing replay of known failures establishes recovery of those examples; it does
not establish a production accuracy percentage. Qualification needs a frozen,
independently reviewed holdout covering receipts, rejections, recruiter mail, mixed
receipts/requests, competing roles, catalog-only roles, quoted history and unrelated
mail. Inspect precision, recall, wrong targets, unresolved processing, repair rate
and latency by class; require zero observed wrong targets in that set. Record exact
corpus/model/prompt versions and rerun when they change. Keep development and holdout
examples separate. The evaluator supplies no auto-approval policy or installation.
