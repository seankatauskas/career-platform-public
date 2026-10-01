# TODO

## Complete Lever description coverage

The description migration is incomplete for Lever. At the time this was measured,
`job-boards.db` contained 319,483 jobs and 7,143 live Lever rows had an empty
`description`. Ashby and Greenhouse were at 100% coverage; Lever was at 90.08%.
The missing Lever rows spanned 637 companies, with `jobgether` accounting for 3,970.

The database constraint does not guarantee actual content:
`description TEXT NOT NULL DEFAULT ''` rejects `NULL` while allowing an empty string.

### Root cause

`normalize_lever()` previously joined `descriptionPlain` and `descriptionBodyPlain`.
According to the [Lever Postings API fields](https://github.com/lever/postings-api/blob/master/README.md#get-a-list-of-job-postings):

- `descriptionPlain` already combines the opening and body, so appending
  `descriptionBodyPlain` can duplicate content.
- Requirements, benefits, and similar sections can be stored in `lists` entries.
- Closing content can be stored in `additionalPlain` or `additional`.

The issue was confirmed against a live affected payload: both plain fields were empty,
while `description` contained the opening/body HTML and `lists` contained requirements
and qualifications. An empty plain field does not mean the employer published no
description.

### Work required

- [x] Update `normalize_lever()` to use `descriptionPlain` once when available.
- [x] Fall back to `openingPlain` plus `descriptionBodyPlain` when the combined field
      is absent.
- [x] Append every `lists` section name and its HTML content before the existing
      normalization to
      normalized plain text.
- [x] Append `additionalPlain`, falling back to normalized `additional` HTML.
- [x] Add regression fixtures where the main description fields are empty but
      `lists` contains the actual posting content.
- [x] Add a regression test proving the body is not duplicated when both
      `descriptionPlain` and `descriptionBodyPlain` are present.
- [x] After the adapter is fixed, run one unfiltered job refresh to backfill existing
      rows. Do not launch that live full-board operation as part of an offline test.
- [x] Recheck coverage and manually inspect a small sample from each ATS. Treat any
      remaining empty descriptions as explicit, source-verified exceptions.

### Backfill result

The Lever refresh completed at `2026-08-30T04:37:06+00:00`. Missing Lever descriptions
dropped from 7,143 to 694, raising Lever coverage from 90.08% to 99.04% and overall
coverage to 99.78% (318,789 of 319,483 rows).

The residual rows are upstream exceptions rather than another known adapter shape:

- 453 are from Lever's `levertest` board and 9 are from `leverdemo`.
- `jobgether`, previously responsible for 3,970 missing rows, now has zero missing.
- Three residual postings were checked against their live JSON. Every documented
  description source (`opening*`, `description*`, `lists`, and `additional*`) was empty;
  one contained only empty HTML (`<div><br></div>`).

Do not manufacture descriptions from the title merely to reach 100%. Keep these as
empty source-data exceptions unless Lever later publishes actual content.

Coverage query:

```sql
SELECT
    ats,
    COUNT(*) AS jobs,
    SUM(CASE WHEN TRIM(COALESCE(description, '')) = '' THEN 1 ELSE 0 END) AS missing,
    ROUND(
        100.0 * SUM(CASE WHEN TRIM(COALESCE(description, '')) <> '' THEN 1 ELSE 0 END)
        / COUNT(*),
        2
    ) AS populated_pct
FROM jobs
GROUP BY ats
ORDER BY ats;
```

## LLM salary gold-set pipeline

- [x] Keep hosted-model drafts separate from authoritative jobs and human gold labels.
- [x] Build a deterministic ATS/salary-signal sample with a three-job company cap.
- [x] Preserve salary, hourly, and other-pay classifications plus multiple pay zones.
- [x] Add Fireworks Qwen3.8 and OpenAI GPT-5.6 Sol structured-output clients.
- [x] Add per-job commits, resume state, retries, quota stops, spend records, and a hard
      preflight budget ceiling.
- [x] Add a local side-by-side review UI with editable JSON and evidence validation.
- [x] Run the one-job Fireworks and OpenAI smoke tests from the credentialed shell.
- [x] Review all 500 sampled jobs, including the 100-job calibration split and locked
      400-job evaluation split.
- [x] Benchmark local NuExtract3 4B and Qwen3.8 27B on the calibration split, including
      real MLX speed, peak-memory, and concurrency measurements.
- [ ] Decide whether the lightweight GLiNER candidate adds enough value to implement,
      for future comparisons.
- [x] Freeze NuExtract3 4B and run the locked 400-job evaluation once: 97.50% primary
      accuracy and 94.26% USD value F1, using one throughput-optimal MLX worker.
- [x] Resolve the inconsistent one-sided-pay gold policy before fine-tuning: positions
      329 and 339 treat starting/minimum language as exact pay while position 471 does
      not. Version any corrected gold set rather than overwriting the locked result.
- [x] Add a versioned salary v3 schema with explicit `exact`, `range`, `minimum`, and
      `maximum` value kinds; audit existing one-sided candidates and build a targeted
      US-focused v3 train/calibration/evaluation batch.
- [x] Complete all 500 v3 gold labels, then simplify the local model contract by
      deriving bound shape instead of predicting `value_kind`. NuExtract3 4B scored
      100% on the 100-job calibration split and 98% on the untouched 100-job
      evaluation split (93.15% USD figure F1) in about 3.03 seconds/job.
- [x] Add a resumable NuExtract production queue for new or changed non-authoritative
      jobs, plus long-description compaction and queue-age monitoring. Capacity findings
      are recorded in `docs/archive/salary-processing-feasibility.md`.
- [x] Retain all-currency annual/hourly values in raw outputs and gold labels, while
      freezing the primary local-model score to supported overlap in the USD
      `(period,min,max)` projection. Require one exact supported match, reject unsupported
      USD predictions, and require no USD output when gold has none. Treat base salary,
      OTE, and target terminology as equivalent when numbers match.
- [x] Preserve that strict v3 benchmark for comparison while selecting `v3-simple`
      for production-facing evaluation: one matching USD figure and period is useful,
      extra candidates are tolerated on positive jobs, and negative jobs still require
      no invented USD pay.
