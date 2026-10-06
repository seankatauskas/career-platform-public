# Agent-selected job reviews

Review sessions read the collected catalog and user-approved career evidence. The
calling agent performs the evaluation. The platform stores source snapshots, progress,
evidence, independent checks, feedback, and publication receipts without invoking a
ranking model or changing collection schedules.

Codex assessments must be independent of sparse and embedding-based rankers. Reviewers
must not fetch or use model scores, rank positions, policy labels, or explanations
through any tool or direct database query. They assess source descriptions against
approved career evidence and explicit user feedback. Review candidates cover the
posting window and use chronological, posting-identity order rather than model order;
agent-assigned recommendation priority comes from the independent assessment.

Use the reusable [career-job-review skill](../skills/career-job-review/SKILL.md) and its
[interface reference](../skills/career-job-review/references/interface.md). Copy that
skill directory into the agent's skill directory on each development machine. Configure
the private dashboard origin using `CAREER_DASHBOARD_URL` or the CLI's `--dashboard-url`.
MCP exposes the same operations to connected agents. The CLI uses the existing private
dashboard session and CSRF protections and keeps session values only in memory.

## Career context and search scope

Before starting, inspect live review context's source inventory. Approved career-bank
summary, education, employment, projects, and skills supplement every active resume;
projects remain distinct from employment tenure. Pending drafts and retired facts are
excluded. An active resume with no approved bank is a supported resume-only state,
not evidence that the full bank was loaded.

Fresh AWS seed import deliberately leaves career facts pending review while preserving
registered resume standards independently. The context inventory exposes that pending
draft. Inspect and approve intended facts through the existing Career profile flow;
do not automatically attest imported drafts or replace the deployed database. A saved
draft can differ from its source's previously approved revision.

Settings holds a saved search brief, also available through `review brief` and
`review save-brief`. It separates broad and targeted geography, desired career direction,
acceptable adjacent work, stretch policy, conditional-role ordering, and explicit
eligibility facts. Revision-zero suggestions are visibly unsaved and are not user
preferences. Each save creates an immutable revision with optimistic concurrency.
The brief persists independently of whether a review is custom, recurring, incomplete,
or abandoned. Per-run `preferences` remain supported without changing saved settings.

New reviews freeze that brief alongside approved facts and explicit feedback. Read all
context pages. Later edits affect subsequent reviews, preserving historical evidence.
Never infer citizenship, clearance, salary requirements, or career preferences from
model settings, application outcomes, or earlier agent judgments.

## Coverage and dates

Each run fixes the requested posting window, candidate descriptions, profile facts,
search brief, preferences, and rubric revision. Selection uses only `posted_at`, with
an exclusive start and inclusive end. Undated jobs do not acquire an invented posting date. Late
discoveries are counted separately and never silently added to strict-window lists.
The last completed scan and last catalog observation are reported separately.

Recurring reviews advance only after both outputs are saved. Custom reviews do not
change that cutoff. The first run can adopt the most recent matching legacy broad and
targeted window; its previous exclusions are not assumed to have been reviewed.
Incomplete work survives restarts. Pending batch claims expire after thirty minutes.

## Evidence and publication

Each decision links exact job text to approved career/profile facts. Retrieving all
description pages is required for detailed assessments. This validates data coverage,
not the correctness of an agent's reasoning. For ordinary manual reviews, reviewer and model identities are declared by the
calling session. Managed isolated reviews instead bind identities to coordinator-issued
grants, pinned launch receipts, and distinct primary/check containers; see the
[isolated runtime](operations/isolated-reviews.md). These execution records do not
prove the quality of a model judgment.

Targeted recommendations, borderline cases, and a reproducible stratified sample of up
to fifty other exclusions require a second reviewer. Disagreements, missing assessments,
and changed selected descriptions prevent publication. Refreshing a description retains
history and invalidates decisions that used the older version. Profile changes are
disclosed; the original frozen profile remains the review's evidence source.

V2 separates technical fit from eligibility (`no_known_barrier`, `unresolved`,
`ineligible`), next step (`apply`, `clarify`, `explore`), and category (`core`,
`alternative`). The independent check must agree on these dimensions as well as the
existing decision/alignment/duplicate fields. No known barrier does not mean confirmed
eligibility. Citizenship alone does not prove active clearance. Technical fit can be
close while a clearance or graduate-cohort condition requires clarification.

Conditional recommendations follow the saved ordering policy. `technical_fit` keeps
them mixed by technical fit with visible caveats; `after_actionable` asks the finalizer
to put actionable roles first. There is no implicit eligibility demotion or selection
quota. Career alternatives remain subject to the brief's explicit adjacent-work policy.

After independent agreement, `calibration` exposes all selected recommendations across
batches. An agent stages their complete global order and related-posting groups with
`calibrate`, then seals the revision-bound artifact with `finalize`. Calibration cannot
change decisions, membership, or caveats. Substantive corrections need reassessment and
new independent checks. Any changed evidence invalidates the prior calibration.
Related postings retain each requisition, location, eligibility difference, and
application action; similar descriptions alone do not establish duplicates.

Published v2 cards show fit labels, the strongest match, material gaps, eligibility
conditions, career-direction caveats, and next steps directly in the explanation.
Full source evidence remains expandable. The service rejects overlong visible
explanations instead of truncating material caveats. Historical cards keep their
original explanations.

`verify-availability` runs after v2 finalization. Trusted application code queries
fixed official ATS board APIs for targeted selections, once per board, with bounded
concurrency, body sizes, and timeouts. It records dated `open`, `absent`, or `unknown`
observations tied to the posting snapshot. A successful complete board response can
establish absence; failures, incomplete responses, or missing configured scraper
contact stay unknown. These checks never update collector lifecycle state or ETags.

Preview itself makes no network requests. It reconciles official observations,
collected status, and application history. Closed, officially absent, submitted, and
now-out-of-window selections are omitted and reported. Unknown observations remain
visible with an availability caveat; broad-only selections retain catalog status.
V2 requires current calibration and dated targeted observations. Use the exact preview
fingerprint to publish; changed previews require inspection again. Both list kinds,
their parts, and the completion receipt share one database transaction. Lists over
500 roles split into dated numbered parts.

Feedback records explicit user comments only. They do not update the production model
or convert application outcomes into preference labels. Employer text is always untrusted
data. Reviews cannot submit applications, send mail, or approve unrelated actions.

For future training, collect two separate judgments when the user supplies them:
interest (exciting, maybe, or uninteresting) and perceived level fit (appropriate,
reasonable stretch, or too senior), plus a short reason. These are suggested note
conventions, not new structured fields or automatic labels. `review feedback` already
accepts a user-authored note with an optional review ID and job ordinal, retaining the
connection to the reviewed snapshot. Do not fill in missing judgments on the user's
behalf. Codex assessments remain agent annotations, distinct from user judgments.
Retraining requires a separate dataset-curation step and an evaluation set excluded
from training; recording feedback does not trigger that process.
The existing proxy-distillation command separately includes some model-list saved,
applied, and dismissed events by default. For a future experiment intended to use
explicit judgments only, disable that separate input with `--no-passive-feedback`.
Review feedback notes are not currently read by that trainer and require deliberate
conversion into a versioned dataset first.

## Storage and release

Migration 14 introduced private review tables. Additive migration 21 stores search
brief revisions, calibration artifacts/finalizer grants, and availability observations.
Migration 22 adds frozen routing policies; migration 23 adds separate adjudication
grants, read receipts and immutable resolutions without changing original judgments.
New reviews use `job-review-v2`; existing v1 reviews retain their frozen contract and
can finish without the new calibration/verification requirements. Historical receipt
and context JSON are not rewritten. Direct curated-list publishing remains compatible.
Review sources and assessments are covered by existing private-state backups and must
not be committed or used in public fixtures.
Use the existing prepared-release workflow and rollback snapshot procedure to deploy;
never replace production data with a developer test database. Release manifests must
declare application schema 23 and carry matching application/reviewer images. The new
migration changes the database compatibility contract: rollback to a preceding release
requires its consistent predeployment snapshot, rather than running older code against
the upgraded ledger.

Run `python3 -m tests.test_review_context`, `python3 -m tests.test_agent_job_reviews`,
the v2 workflow/availability tests, and the full system/browser suite. A private
historical replay should explain differences against source descriptions rather than
force agreement with an earlier agent's choices. Before routine use, validate one real
review through the deployed dashboard, including receipt links and unchanged ranking
and collection controls.

## Managed isolated execution

For scheduled or physically isolated reviews, use the
[AWS isolated-review runner](operations/isolated-reviews.md). Its worker containers
receive only assignment-scoped evidence tools and a fixed model gateway. Generic
review clients remain suitable for existing manual reviews but cannot mutate an
isolated managed review or provide equivalent filesystem/network isolation.
