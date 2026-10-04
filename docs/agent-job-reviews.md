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

## Coverage and dates

Each run fixes the requested posting window, candidate descriptions, profile facts,
preferences, and rubric revision. Selection uses only `posted_at`, with an exclusive
start and inclusive end. Undated jobs do not acquire an invented posting date. Late
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

Preview reconciles counts and rechecks collected posting status and application history.
It does not contact employers. Closed, submitted, and now-out-of-window selections are
omitted and reported. Use the preview fingerprint to publish; a changed preview requires
inspection again. Both list kinds, their parts, and the completion receipt share one
database transaction. Lists over 500 roles split into dated numbered parts.

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

Migration 14 adds private review tables in the application ledger. Existing list formats,
IDs, and publishing calls remain compatible. Review sources and assessments are covered
by existing private-state backups and must not be committed or used in public fixtures.
Use the existing prepared-release workflow and rollback snapshot procedure to deploy;
never replace production data with a developer test database. Migration 14 changes
the database compatibility contract: recovery to the preceding release requires its
consistent predeployment snapshot, rather than running older code against the upgraded ledger.

Run `python3 -m tests.test_agent_job_reviews` and the full system/browser suite. A private
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
