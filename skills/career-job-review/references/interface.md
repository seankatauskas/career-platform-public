# Review interface

All operations use `python3 -m job_search review ACTION --dashboard-url ORIGIN --input FILE`
(or `--input -` for JSON stdin). `CAREER_DASHBOARD_URL` supplies the default origin.
MCP names replace hyphens with underscores and prefix `review_`.

Assessments must not use production model scores, rank positions, model labels, or
model explanations from this or any other interface. Candidate order follows posting
time and identity, not model preference. `priority` is the reviewing agent's independent
judgment. Explicit user feedback is permitted evidence and remains separate from model
predictions and agent-authored assessments.

Mutation actions require a stable `idempotency_key`; retry the identical request after
an uncertain outcome. Changed content requires a new key. Store private payloads outside
the repository. Only approved career facts and active resume text are returned.

| Action | Arguments in addition to mutation key where required |
|---|---|
| list | Optional `before`, `limit` (up to 30) |
| brief | No arguments; saved search brief or unsaved suggestions |
| save-brief | Complete `brief` object and `expected_revision`; mutation |
| context | Optional `review_id`, `section` (`facts`, `preferences`, `feedback`), `offset`, `limit` (up to 20) |
| start | `mode`: `recurring` or `custom`; optional `window_end`, user-confirmed `preferences` string array, `rubric_version`; custom requires `window_start` |
| batch | `review_id`, optional `after`, `limit` (up to 20), `filter`: `all`, `pending`, `assessed` |
| claim | `review_id`, `actor`, optional `limit` (up to 20); mutation |
| job | `review_id`, `ordinal`, `actor`; optional `offset`, `limit` (up to 6,000 characters), `blind` |
| assessment | `review_id`, `ordinal`; optional `kind`: `primary`, `check`, `history`; `offset`, `limit` |
| assess | `review_id`, `ordinal`, `actor`, `expected_revision`, `assessment`, optional `kind`: `primary` or `check`; mutation |
| refresh-job | `review_id`, `ordinal`, `actor`, `expected_revision`; mutation |
| status | `review_id` |
| calibration | `review_id`; optional `after`, `limit` (default 5, maximum 20); v2 only |
| calibrate | `review_id`, `ordinal`, `basis_sha256`, `position`; optional `related_group`, `actor`; mutation, v2 only |
| finalize | `review_id`, `basis_sha256`; optional `actor`; mutation, v2 only |
| verify-availability | `review_id`; mutation, v2 only; bounded official-board requests after finalization |
| preview | `review_id` |
| publish | `review_id`, `preview_sha256`; mutation |
| abandon | `review_id`, `reason`; mutation |
| feedback | `note` supplied by user; optional `review_id`, `ordinal`; mutation |

The first recurring run bootstraps from a matching legacy broad/targeted pair. If no
pair exists, supply an explicit initial `window_start`. Custom reviews never advance
the recurring cutoff. Start requires a usable approved profile or active resume.
New reviews default to `job-review-v2`. Existing reviews keep their frozen version;
explicit `job-review-v1` is retained for compatibility, with its original assessment
and publication contract. Do not downgrade a new review to skip v2 quality checks.

Read all pages using returned `next_after`/`next_offset`. `assessment` returns chunks
of serialized JSON under `text`; concatenate before parsing. Evidence is never silently
truncated; reduce page size if the response exceeds its bounded transport limit.

## Context and saved search brief

`brief` and `save-brief` return `{revision, brief, saved_at}`. Revision zero and a null
`saved_at` indicate unsaved suggestions, not user preferences. Saving requires the
current `expected_revision` and returns a new immutable revision. Stale revisions
fail without replacing the current brief. Idempotent retries return the original
response. Saved scope is independent of review status and recurring cutoffs.

The complete `brief` object has these fields:

| Field | Values |
|---|---|
| `broad_geography`, `targeted_geography` | `us` or `worldwide`; scopes are independent |
| `targeted_scope` | `software_building` or `all_technical` |
| `adjacent_roles` | `broad_only`, `targeted`, or `exclude` |
| `conditional_order` | `technical_fit` or `after_actionable` |
| `stretch_policy` | User-confirmed text, 1–1,000 characters |
| `eligibility_facts`, `notes` | Up to twenty user-confirmed statements each, 1–500 characters per statement |

Geographic scope includes remote work only when the posting permits work from the
chosen geography. `software_building` includes applied AI and FDE responsibilities;
title alone does not determine scope. Eligibility statements do not replace the
posting's citizenship, clearance, cohort, or work-location requirements.

Live `context` includes `source_inventory`: career/resume/total fact counts,
`facts_by_source`, and `pending_career_draft`. Approved summary, education, experience,
projects, and skills remain distinct evidence sources alongside active resume lines.
Pending or retired facts are excluded. Review context freezes the complete
`search_brief` snapshot with its revision. Existing `preferences` remain supported
as per-review statements; supplying them does not save a new search brief.

Do not infer geography or authorization from unsaved suggestions. Resolve material
scope uncertainties before starting. Brief/profile changes do not rewrite an active
review's frozen evidence or a prior publication.

## Assessments

Example detailed assessment (fictional evidence; replace it with retrieved facts):

```json
{
  "stage": "detailed",
  "decision": "slight_stretch",
  "family": "backend",
  "alignment": "core",
  "reason_code": "transferable_experience",
  "explanation": "API development matches the profile; the requested production tenure is a stretch.",
  "evidence": [
    {"field": "description", "quote": "Build backend APIs", "fact_id": "retrieved-fact-id"}
  ],
  "strengths": ["Demonstrated API development"],
  "gaps": ["Less professional tenure than requested"],
  "unknowns": ["Work authorization requirements are unspecified"],
  "borderline": true,
  "priority": 12,
  "eligibility": "unresolved",
  "eligibility_condition": "Confirm whether the stated work-location requirement can be met.",
  "next_step": "clarify",
  "category": "core"
}
```

All assessments require stage, decision, family, alignment, reason_code, explanation,
evidence, strengths, gaps, unknowns, and borderline. `alignment` is core, adjacent,
unrelated, or unknown. Selected decisions require a positive integer priority and
description evidence linked to a frozen profile fact. Detailed decisions require all
description pages retrieved by that actor; retrieval records demonstrate coverage,
not comprehension. Screening can only exclude non_technical, location, or duplicate
cases, or record needs_info. Duplicate exclusions require `duplicate_of` pointing to
a retained ordinal. Optional `model` is declarative provenance; do not invent it.

V2 additionally requires `eligibility` (`no_known_barrier`, `unresolved`, `ineligible`),
`eligibility_condition` (text, nonempty for unresolved/ineligible), `next_step`
(`apply`, `clarify`, `explore`), and `category` (`core`, `alternative`). Unresolved
eligibility cannot suggest `apply`; confirmed ineligibility uses `explore` and cannot
be targeted. Alternatives require the frozen brief's explicit targeted opt-in to
appear in targeted recommendations. `no_known_barrier` is not confirmed eligibility.
V1 assessments omit these four fields and retain the earlier combined fit rubric.

An independent check uses the same assessment shape and a distinct reviewer ID. Its
decision, alignment, and duplicate reference must agree before publication. V2 also
requires agreement on eligibility, next step, and category. Changing a primary clears
the check. Status returns remaining checks and disagreements in bounded groups; as
those are resolved the next group becomes visible.

Priority is compared across the whole review, not reset to 1 within each batch.
Identical priorities use stable candidate order as the tiebreaker. Changes use the
current item revision; stale revisions fail without overwriting another reviewer.

## V2 calibration and availability

After primary coverage and independent agreement, read all `calibration` pages via
`next_after`. Responses include `basis_sha256`, selected/staged counts, completion
state, and each selected posting's primary assessment and staged order/group.
The basis binds frozen context, job snapshots, and primary/check revisions.

Stage every selected ordinal with `calibrate`, using that basis and a unique
`position` from 1 through the selected count. Optional `related_group` is
`{"id":"same-team-role","label":"Related openings on the same team"}`; shared IDs
must have consistent labels. The global order supplies broad and targeted views.
It preserves distinct requisitions and their application links.

`finalize` validates complete membership, unique positions, visible explanations,
and group consistency. It cannot change membership, fit, eligibility, alignment, or
caveats. Changed assessments/sources invalidate calibration; reread the new basis
after reassessment/checks. A finalized basis cannot be reordered in place.

`verify-availability` checks targeted selections on official ATS boards once per
board through trusted application code. Observations bind to the posting snapshot
and contain `status` (`open`, `absent`, `unknown`), `checked_at`, `source`, and `reason`.
The command returns checked/open/absent/unknown counts. Reuse a key for a retry; use a
new key for a new observation. Missing contact, timeouts, malformed responses, and
incomplete board responses remain unknown. Collector state and ETags stay unchanged.
Broad-only postings do not acquire an official-board verification claim.

V2 preview requires a current finalized order and dated availability observations
for targeted selections. Unknown remains visible with a caveat; confirmed absence
is omitted and counted. The exact preview fingerprint remains required for atomic
publication. V1 publication does not acquire these new requirements.
