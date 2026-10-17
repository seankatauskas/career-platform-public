# Grouped evidence review and backlog migration

Review presents one card per email or paired-browser application attempt that still
needs a decision. Processing issues appear separately in **System attention**, with
links between an issue and any pending updates from the same source.
Automatically recorded evidence creates no pending card. External actions retain
separate exact approvals. One email can contain several independently selectable
changes; accepting a card is never an implicit blanket approval.

Common email updates show a result and one primary action: **Record receipt**,
**Add task**, **Record interview**, or **Record rejection**, for example. Accepting
an update includes its pending email-link prerequisite in the same transaction.
The card explicitly says that it also attaches the email. No separate link click
or checkbox is needed. **Review details** retains the individual proposal inputs
and versions, and **View email** opens the preserved source.

Independent updates keep separate actions. Dismissing an update rejects its
included link only when no other pending proposal needs that link. It never
records an employer rejection or deletes the email. Closure consequences remain
visible before approval. Unresolved targets, blocked or unknown operations, and
dependencies on another business decision keep the detailed review controls.
Selected detailed changes also include eligible email links automatically.

Each card submits explicit proposal versions and the complete group digest.
Changed sources, proposals, associations, or current state invalidate the preview
atomically; a lost response retries the same command key. Business approval still
uses the existing `review_changes` workflow and separate audit entries. Older
callers may continue using explicit decision bundles without a group preview.
When supplying a preview, both `group_id` and `expected_digest` are required and
every decision must belong to that group. Counts come from the
complete bounded inventory, and pagination keeps evidence groups intact. Existing
proposal links locate the containing card, including beyond the first page.

## Recording and interpretation

Browser recording defaults to **review first**. The owner can enable automatic
recording from Review. Policy `paired-browser-v1` accepts only deterministic
recipes from paired observations with unchanged hashes, one device/attempt,
current pursuit, complete coverage, and no conflicts or dependencies. Website
confirmation additionally requires a success DOM or route signal. Late captured
answers attach to the same submission and replay does not duplicate records.
Disabling the preference affects future recording; existing accepted facts remain.
A future policy version requires a new opt-in.

Email changes still require human review, including terminal rejection outcomes.
An application receipt is its own record and can establish employer receipt
without inventing a submission click, selecting a browser attempt, or copying
its answers. Attempt-specific confirmation requires an actual record in that
application. Employer names, posting identifiers, and complete role titles of at
least three words can supply candidate context. Complete title matches help when
an ATS stores an employer slug instead of its display name; they never establish
an association by themselves. Generic one-word titles do not retrieve candidates.
The structured submission schema constrains status to the supported vocabulary,
so a receipt cannot become an unrecognized model-invented status.

The analyzer prompt distinguishes automated acknowledgment, team signatures,
marketing, verification codes, completed portal steps, employer response estimates,
and actual applicant requests. Projection guards suppress narrowly recognizable
non-actionable findings while retaining the analysis. Wrong targets, missing
records, unsupported mappings, and incomplete evidence remain visible blockers.
Unresolved application identity opens **Which role matches this email?** with no
preselected application. Matching candidates appear first; search includes paginated
tracked applications and matches employer, title, and location. **None of these**
opens the existing catalog/unlisted-role workflow when no application is linked.
**Change application** opens the same picker on a proposed update. Search and
selection survive rerenders and refreshes. **Continue** records the user's selection
as the correction reason and prepares replacement proposals; it accepts no update.
The revised card shows the selected application and resulting action for approval.
**Receipt options** retains the explicit choice to remove attempt-specific links.
Previously accepted associations cannot be moved through this picker: the UI explains
that association correction must review earlier consequences.

The navigation badge and application notices count decision groups, excluding
processing-only evidence. System attention keeps exact-source retry and manual
resolution controls. **Retry processing** needs no written reason; **Resolve without
retrying** requires a disposition and permits an optional explanation. Neither
accepts application changes. Technical diagnostics and earlier attempts remain
available in disclosures/history. A lost response freezes the submitted selection
and reuses the same request/key; stale reviews require refresh while retaining choices.

A validation failure can queue at most two additional automatic attempts per
source revision and model/prompt version. Each retry preserves the earlier attempt
and exact source/hash; manual resolution and new revisions fence delayed workers.
Unavailable evidence, incomplete coverage, and exhausted budgets remain visible.
Quote-validation diagnostics contain only category, finding position, and a
bounded reason, without adding source text to diagnostic records. Inference usage
limits and provider deferrals retain their existing behavior.

## Migrate existing open work

Feature changes do not deploy themselves. Follow [coordinated releases](coordinated-releases.md)
and the existing verified backup/restore process. Schema upgrades are additive
and checksum-bound. Opening an owners database with the new runtime installs the
new receipt, policy, recovery-budget, and diagnostic tables; preview changes no
business decisions. Old binaries will reject the new schema, so rollback requires
the existing release restore process rather than opening the upgraded database
with old application code.

After the release is installed, the release operator should first exercise this
procedure on a consistent restored copy of the owners database. Use the existing
application-owner database path, not the operational/jobs database. Retain a
verified pre-migration backup and put the following private artifacts outside git.

```sh
python -m job_search.review_migration --database /path/to/owners.db preview \
  --enable-browser-recording --output /private/path/review-preview.json

python -m job_search.review_migration --database /path/to/owners.db apply \
  --manifest /private/path/review-preview.json \
  --actor '<authenticated owner/operator identity>' \
  --output /private/path/review-results.json
```

Omit `--enable-browser-recording` to preserve the current preference. With the flag,
preview records the requested opt-in; apply makes the version-checked preference
change before processing groups. Review the manifest's inputs, blockers,
dependencies, source revisions, and dispositions before applying it. Files are
written with owner-only permissions. Neither command sends mail or accepts email
business changes. Queued recovery runs later through the existing account-scoped
model worker.

Each evidence group runs in its own transaction. Its preview digest binds pending
versions, sources, associations, current application state, and prerequisite state.
The manifest also binds installation and recording-policy versions. Apply returns
a durable result for each group and continues past stale groups without changing
them. Repeating the same manifest and actor replays completed command receipts.
If `needs_refresh` is nonzero, take a fresh preview and review its changes before
applying it; do not edit stale digests or reset proposal versions. Earlier migrated
groups may legitimately change another group's application progress.

| Existing item | Migration treatment |
| --- | --- |
| Eligible paired-browser proposals | Reproject exact preserved observations and record under the opted-in policy. |
| Conflicting, historical, incomplete, or unpaired browser evidence | Keep a grouped exception for review. |
| Clearly non-actionable contact, verification-confirmation, or completed-portal proposals | Supersede with an audit reason; retain evidence and findings. |
| Receipt without a specific submission | Replace with a pending application-receipt proposal and preserve dependencies. |
| Exact restatement of accepted state | Supersede only when current versions, target, status, and time agree. |
| Rejection outcomes | Keep pending, with association and closure consequences visible. |
| Wrong employer or missing submission target | Keep blocked; use target/scope correction, then review. |
| Retryable processing failure | Queue bounded recovery; retain every attempt. |
| Already queued, incomplete, unavailable, or exhausted processing | Preserve its current state and required intervention. |

The audited backlog's four rejection outcomes must remain manual. Its three
quote-validation failures should pass through bounded recovery. Missing attempt
references and the known employer mismatch must not be guessed. Nearby browser
attempts remain distinct even when they occur within a minute. Verify the current
manifest rather than treating the historical 64/65-decision count as a migration
precondition; incoming evidence may change it.

After apply, verify the result receipt for every manifest group, review totals,
remaining blockers, and processing history. Confirm that automatic browser entries
appear in application submissions/answers, mail receipts remain separate from
attempts, and no terminal email outcomes were accepted by migration. If workers
produce new proposals, those return to the same source card. Synthetic tests verify
guardrails; they are not a private holdout measurement of the model's accuracy.

## Verification

### Refresh older email interpretations

Prompt version 5 loads up to ten earlier incoming/outgoing messages from the same
account and provider thread, within 60,000 additional characters. Sources retain
their exact hashes; drafts, other accounts and future messages are excluded.
Thread material is contextual and cannot independently create new facts or tasks.
Missing or bounded-out history is reported separately as incomplete supplemental
context; it does not block an otherwise self-contained current email. The analyzer
must leave unsupported fields unset when interpretation needs missing history. Candidate
retrieval also considers reviewed thread links and employer names with legal
suffixes removed; these hints never constitute accepted associations.

Current submission, receipt, interview, assessment, offer and task records are
supplied. Structured record-ID choices are restricted to supplied records and
projection verifies ownership. A receipt with an unusable attempt ID becomes a
pending application receipt. A uniquely selected, evidence-backed fact target
can supply the pending email-link prerequisite even when the analyzer omitted a
separate association finding. Conflicting targets remain unresolved.

After installing this release, use a fresh private backup and preview:

```sh
python -m job_search.review_migration --database /path/to/owners.db refresh-preview \
  --output /private/path/refresh-preview.json
python -m job_search.review_migration --database /path/to/owners.db apply \
  --manifest /private/path/refresh-preview.json --actor '<owner/operator identity>' \
  --output /private/path/refresh-results.json
```

The preview selects open email groups last analyzed under an older prompt,
including processing failures. Apply queues an explicit analysis refresh using
the current model worker and account grants. It never approves email changes.
The old pending proposals remain until a complete new analysis projects its
replacements in the same transaction that supersedes the old rows. History is
retained. Failed/incomplete analysis preserves the old proposals. Concurrent user
decisions fence replacement; already accepted/rejected findings remain decided.
Retries carry the original replacement scope. Inspect results and processing
history after workers finish; do not claim success from queueing alone.

The card's **Refresh analysis** action uses the same version-checked workflow.
Technical preparation failures no longer display the blanket “Needs clarification”
label. Missing application identity and time information use specific questions.
The common one-click path is implemented; the remaining interaction work is
tracked in [review interaction design](../review-interaction-design.md).

### Create an application for an unlisted role

Unassigned incoming-email cards offer **Track an unlisted role**. A unique employer
and role in current authored text can prefill the form; ATS sender addresses and
quoted history do not identify a role. When wording is unclear, read the email,
enter its employer and role, and choose **Check for existing role**.

Creation is suggested only after complete checks of tracked applications and the
jobs catalog, including closed postings. Possible matches remain choices to review;
they are never merged based on employer/title similarity. Missing catalog or source
coverage keeps creation unavailable. This is conservative identity matching with a
human decision, not a calibrated model-confidence claim.

**Create application record** binds the displayed source-group version, employer,
role, and matching preview. The command rechecks under the owner transaction and
uses Applications' stable discovery identity plus Correspondence's reviewed link.
It creates a **Tracking** application and attaches the email. Unresolved proposals
are replaced with attributed proposals for that application; receipts without an
attempt become application-receipt proposals. Those facts, tasks, outcomes, and
processing problems remain pending. Creation never completes an obligation or
authorizes an external action. Changed previews fail atomically; an uncertain
response retries the same receipt-backed command.

### Checks

```sh
uv run python -m unittest tests.test_grouped_evidence_review tests.test_grouped_review_semantics
uv run python -m tests.test_review_context_refresh
uv run python -m tests.test_review_bundles
uv run python -m unittest discover -s tests -p 'test_redesign*.py'
uv run python -m unittest discover -s tests -p 'test_owner*.py'
node tests/browser/test_owner_review_view.mjs
node tests/browser/test_owner_dashboard.mjs
node tests/browser/test_owner_application_view.mjs
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
```
