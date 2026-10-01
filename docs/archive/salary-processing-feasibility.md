# Local salary-processing feasibility

Snapshot date: 2026-08-31. These figures come from `job-boards.db` and the frozen
NuExtract3 evaluation run.

## Important measurement caveat

The database was initially populated on August 30–31, so `first_seen` does not yet
contain enough history to measure normal recurring ingestion. Recent workload estimates
therefore use ATS `publishedAt` timestamps from the currently visible job inventory.
Counts close to the snapshot should be useful operational estimates, but older days
have survivor bias because jobs posted and closed before the initial scrape are absent.

## Inventory and recent volume

- Total jobs: 320,533.
- ATS mix: 187,856 Greenhouse, 72,674 Lever, and 60,003 Ashby.
- Active boards: 6,755 Greenhouse, 3,740 Ashby, and 2,665 Lever.
- Published August 1–30: 100,850 jobs.
- Mean calendar-day volume: 3,362; median: 4,190.
- Mean weekday volume: 4,857; maximum weekday/day overall: 6,810.
- Daily p95: 6,394.
- Mean weekend volume: 372; weekend maximum: 635.
- Maximum one-hour bucket: 699; non-empty hourly p95: 419.

The upper-bound volume is concentrated. The largest recent board, Jobgether on Lever,
contributed 4,268 jobs (4.23%). The top 10 company boards contributed 14.89%, and the
top 50 contributed 27.13%. Filtering aggregators or clearly irrelevant high-volume
boards could reduce work materially, but is not required for local feasibility.

All 320,533 jobs have existing rule/API enrichment. There are 69,025 jobs with an
authoritative ATS salary source. In the recent 30-day inventory, 22,603 of 100,850 jobs
had authoritative ATS values. If those bypass NuExtract, the recent LLM workload is
78,247 jobs: 2,608/day on average, a 5,202 daily p95, a 5,556 maximum day, and a 634
maximum hour.

## Description sizes

For the 100,850 recent jobs:

- median: 5,922 characters;
- p90: 11,866;
- p95: 16,028;
- p99: 26,329;
- maximum: 496,987;
- 624 descriptions exceed 30,000 characters, 56 exceed 50,000, and eight exceed
  100,000.

The 400-job evaluation median was 5,225 characters and its p95 was 11,525, so normal
production descriptions are somewhat longer in the tail. Before production backfill,
add a deterministic long-document policy: render HTML, retain compensation-relevant
windows plus necessary job metadata, and impose a token ceiling. Do not simply retain
only the beginning because compensation commonly appears near the end.

## Measured local capacity

NuExtract3 4B MLX 4-bit on the 64 GB M4 Pro completed the locked 400-job evaluation in
1,143.77 seconds:

- mean: 2.859 seconds/job;
- median: 2.589 seconds/job;
- maximum: 22.264 seconds/job;
- peak memory: 6.173 GB;
- measured throughput: about 1,259 jobs/hour or 30,218/day continuously.

Use 1,000 jobs/hour as a conservative planning rate to cover longer descriptions and
operational overhead. At the measured rate:

| Workload | Jobs | Processing time |
| --- | ---: | ---: |
| Average calendar day, all jobs | 3,362 | 2.67 hours |
| Average weekday, all jobs | 4,857 | 3.86 hours |
| Daily p95, all jobs | 6,394 | 5.08 hours |
| Maximum observed day, all jobs | 6,810 | 5.41 hours |
| Maximum observed hour, all jobs | 699 | 33 minutes |
| Average day excluding authoritative ATS values | 2,608 | 2.07 hours |
| Maximum day excluding authoritative ATS values | 5,556 | 4.41 hours |
| Initial 320,533-job backfill | 320,533 | 10.61 continuous days |
| Backfill excluding 69,025 authoritative ATS jobs | 251,508 | 8.32 continuous days |

A 20% safety factor puts the worst observed all-job day near 6.5 hours and the peak
hour near 40 minutes. An hourly recurring queue can therefore keep up on one worker,
including the observed peak. The initial backfill is feasible but should be treated as
a resumable multi-day operation.

MLX concurrency should remain one. Two NuExtract workers fit in memory but each runs at
approximately half speed, producing no meaningful aggregate throughput gain. Multiple
Qwen3.8 workers were also slower. The GPU and unified-memory bandwidth, not RAM
capacity, are the constraint.

## Recurring-system requirements

1. Queue only newly seen jobs or jobs whose description/source fingerprint changed.
2. Never reprocess every open job on every scrape.
3. Commit each result independently and resume from persisted status.
4. Prefer authoritative ATS ranges; use NuExtract for missing values or as an optional
   audit layer.
5. Keep failed, malformed, and validator-flagged outputs in retry/review states.
6. Apply the long-description policy before inference.
7. Track queue age and alert if the oldest pending job approaches one hour during normal
   hourly operation.

## One-sided salary policy and labeling

A high-signal heuristic found explicit minimum-only or maximum-only compensation
language in approximately 5,554 of 100,850 recent jobs (5.51%). The completed v3 audit
contains 59 of the existing 500 labels; the earlier rough count of 44 omitted some
plus-sign forms such as `$200k+`. These are candidate estimates, not gold prevalence.

Do not reinterpret one-sided amounts as exact pay:

- exact: `min_value == max_value`, with an explicit exact amount;
- range: both bounds are present and unequal;
- minimum: `min_value` is present and `max_value` is null;
- maximum: `min_value` is null and `max_value` is present.

Add an explicit `value_kind` (`exact`, `range`, `minimum`, `maximum`) so missing bounds
are intentional rather than malformed. Examples such as `starting at $175,000` and `up
to $20/hour` then remain distinct and truthful.

Do not create another purely random 500-job batch. First re-audit the 59 one-sided
candidates in the existing 500 under the versioned policy. Then use the new,
company-capped, ATS-balanced, US-focused 500-job v3 batch targeted approximately as:

- 200 lower-bound candidates;
- 150 upper-bound candidates;
- 50 exact-pay controls;
- 50 ordinary two-sided ranges;
- 50 hard negatives such as bonuses, benefits, experience requirements, revenue, and
  per-unit/weekly/monthly amounts.

The prepared `targeted-v3-20260831` batch uses 300 for training, 100 for calibration,
and 100 for a new untouched evaluation split. It spans 359 companies with a maximum of
three jobs each. The current v2 labels and locked evaluation remain untouched. The
previous two-provider 500-job teacher run cost about $6.27, so a similar v3 draft run
should be in that general range before human-review time. See `docs/models/salary-v3.md` and
`docs/archive/fireworks-backfill-estimate.md` for commands and hosted-backfill planning.
