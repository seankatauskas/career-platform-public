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
| context | Optional `review_id`, `section` (`facts`, `preferences`, `feedback`), `offset`, `limit` (up to 20) |
| start | `mode`: `recurring` or `custom`; optional `window_end`, user-confirmed `preferences` string array; custom requires `window_start` |
| batch | `review_id`, optional `after`, `limit` (up to 20), `filter`: `all`, `pending`, `assessed` |
| claim | `review_id`, `actor`, optional `limit` (up to 20); mutation |
| job | `review_id`, `ordinal`, `actor`; optional `offset`, `limit` (up to 6,000 characters), `blind` |
| assessment | `review_id`, `ordinal`; optional `kind`: `primary`, `check`, `history`; `offset`, `limit` |
| assess | `review_id`, `ordinal`, `actor`, `expected_revision`, `assessment`, optional `kind`: `primary` or `check`; mutation |
| refresh-job | `review_id`, `ordinal`, `actor`, `expected_revision`; mutation |
| status | `review_id` |
| preview | `review_id` |
| publish | `review_id`, `preview_sha256`; mutation |
| abandon | `review_id`, `reason`; mutation |
| feedback | `note` supplied by user; optional `review_id`, `ordinal`; mutation |

The first recurring run bootstraps from a matching legacy broad/targeted pair. If no
pair exists, supply an explicit initial `window_start`. Custom reviews never advance
the recurring cutoff. Start requires a usable approved profile or active resume.

Read all pages using returned `next_after`/`next_offset`. `assessment` returns chunks
of serialized JSON under `text`; concatenate before parsing. Evidence is never silently
truncated; reduce page size if the response exceeds its bounded transport limit.

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
  "priority": 12
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

An independent check uses the same assessment shape and a distinct reviewer ID. Its
decision, alignment, and duplicate reference must agree with the primary before publication. Changing a primary clears
the check. Status returns remaining checks and disagreements in bounded groups; as
those are resolved the next group becomes visible.

Priority is compared across the whole review, not reset to 1 within each batch.
Identical priorities use stable candidate order as the tiebreaker. Changes use the
current item revision; stale revisions fail without overwriting another reviewer.
