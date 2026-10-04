# Runtime efficiency and affordable ranking

The October 3, 2026 audit used read-only CloudWatch queries and bounded SQL on the
running host. No collection, inference, training, activation, or deployment was
started. Ranking and salary automation were disabled at the time of that audit.
Ranking was subsequently enabled with `sparse_cpu`; salary remains disabled.

## Measurements and limits

| Observation | Measurement | Interpretation |
| --- | --- | --- |
| EC2 capacity | `t3.large`; 10.9% mean of 73 hourly CPU averages since September 30 | Low average CPU alone does not justify downsizing. |
| Host memory | 94.8% maximum, with high readings on multiple days | Attribute peaks before reducing RAM; historical per-process memory is unavailable. |
| Latest completed collection | 236 seconds | Live ATS latency remains a separate cost; concurrency stays at 8. |
| Following location refresh | 163 seconds | This is the first optimization target supported by a recorded runtime. |
| Catalog coverage | 334,275 jobs; 6,972 jobs represented in 6,730 prepared families | 327,303 jobs were absent from prepared family metadata. A prepared-family sample is not a full-catalog cost estimate. |
| Embedding ledger | 1,197 completed requests; 30,210 cached texts | Request counts and cached texts are distinct quantities; neither is provider billing. |
| Recorded token reservations | 36,604,312 for embeddings, with observed tokens unavailable | Reservations are admission estimates, not measured usage or dollars. |
| Selected models | Broad: sparse weight 1.0. Selective: dense-linear weight 1.0. | Broad scoring can preserve its existing predictions without embeddings. Selective cannot. |
| Pending model work | 15 ranking items and 17 salary items | Deliberately paused work is not evidence of insufficient compute. |

These are snapshots, not current dashboards or a seven-day load test. Some older
work records have identical start/end timestamps; zero recorded duration must not
be interpreted as free processing. Durations cover only the most recent recorded
attempt of each work item. Container cgroup current/peak values include file cache
and refer to the current container lifetime, so they do not identify which process
caused earlier host peaks. September Cost Explorer returned no line items; no
dollar savings are claimed.

## Repeat the read-only runtime report

```bash
python -m job_search.efficiency --db /private/job-search.db --days 7
```

The report includes task/lane counts, latest-attempt duration quantiles, initial
dispatch delays, and current queued ages. It bounds rows and query time, reports
truncation, and never reads task payloads, error bodies, messages, or credentials.
It does not run migrations or alter paused automation. Older attempts have no
memory samples. New command attempts record bounded summaries on success and
failure; the report extracts only allowlisted numeric counters from them.
Raw audit receipts belong in ignored private state, not Git.

## First fix: reuse unchanged location enrichment

Previously each pass normalized every job, then made a separate SQL lookup to
discover whether its versioned source fingerprint had changed. Backfill now joins
the saved fingerprint/status once and normalizes only new or changed inputs.
Unchanged evidence, timestamps, status counts, and raw ATS data remain intact.
Normalizer-version changes still invalidate the cache. This does not change
collection filters, posting closure rules, or ETag eligibility.

An offline benchmark compared the baseline at commit `4820b58` with the change,
using 20,000 jobs distributed over six synthetic locations. Both paths ran against
the same already-enriched temporary SQLite database, with three alternating
measurements. Median time fell from 0.664 seconds to 0.058 seconds, an 11.4x
speedup for this unchanged-data fixture. This is not a projection of production
latency: real disk access, location variety, and changed-row fractions differ.

Reproduce this fixture from a Git checkout containing the baseline commit:

```bash
python -m tests.benchmark_location_backfill --jobs 20000 --repeats 3
```

## Why full ranking was expensive

The named-policy refresh always loaded both broad and selective models. It created
an encoder for every recorded revision and embedded each family's title and up to
four description chunks before scoring. The scorer then calculated dense-linear,
neighbor, and lexical components even when their final weights were zero.

The embedding cache already avoids purchasing identical text twice. It cannot
avoid the initial full-catalog workload or new/changed text, and a provider/model
revision defines a separate cache namespace. Changing to another endpoint can
therefore require another backfill even when its nominal model name looks similar.

Inspect prepared-family coverage and embedding cache demand without running the
pipeline:

```bash
python -m job_search.ranking.cost_estimate \
  --db /private/job-boards.db \
  --state-db /private/job-boards-preference.db \
  --proxy-db /private/job-boards-proxy.db \
  --policy broad --active-components-only --max-families 5000
```

Omit the policy/component flags to inspect the default full refresh. The estimator
reads model metadata without loading executable artifacts or contacting a provider.
It counts unique missing cache texts by model revision and reports characters and
bytes. Its token approximation is only characters divided by four, rounded up per
text; it is neither tokenizer output nor a dollar quote. A limited or incompletely
prepared corpus is explicitly marked partial. Even complete prepared coverage
does not verify that grouping metadata matches the latest source jobs.

## Broad ranking without embedding calls

The explicit CPU-only path uses the existing broad model; it does not retrain,
change weights, or pretend to reproduce selective predictions:

```bash
python -m job_search.ranking.refresh \
  --db /private/job-boards.db \
  --state-db /private/job-boards-preference.db \
  --proxy-db /private/job-boards-proxy.db \
  --policy broad --active-components-only --no-embeddings
```

This is a write operation that prepares families and publishes broad scores; it is
an example for a later intentional run, not a read-only audit command. The
`--no-embeddings` guard inspects the selected artifact before preparation and
refuses a model requiring any embedding component. It prevents a future model
replacement from silently turning this command into paid inference.
After those guards pass, the mutating refresh enables and verifies WAL on the
catalog and preference sidecar before taking long read snapshots. A busy or
unsupported conversion fails instead of continuing with a blocking rollback
journal snapshot. Conversion changes SQLite journal settings, not job contents.
WAL can grow while readers hold snapshots; include that in disk headroom.
SQLite busy/locked failures, including a preparation snapshot invalidated by a
collector commit, return retryable exit code 75 rather than a permanent failure.

For future scheduled use, the equivalent runtime settings are:

```json
{
  "shortlist_policy": "broad",
  "ranking_refresh_mode": "broad_cpu"
}
```

Merge those fields into the normal versioned configuration. `full` remains the
default mode and preserves the existing behavior. `broad_cpu` requires the broad
shortlist policy so notifications do not rely on a different, unrefreshed model.
Changing the mode does not enable ranking; activation remains a separate decision.
The existing Terraform/GitHub release workflow remains the deployment mechanism.

Only selected policies are scored, pruned, and certified complete. Other policies'
scores and receipts remain untouched. Sample runs never publish full coverage.
Omitted component diagnostics appear as unavailable, not as measured zero scores;
active-learning disagreement must not compare a calculated score with an omitted
one. Existing complete diagnostic rows remain reusable, while a later full pass
recomputes diagnostics omitted by the optimized pass.

Offline verification used real scikit-learn models with 64 synthetic training
documents and 1,024 synthetic scoring documents. The full and sparse-only paths
returned exactly equal final scores, and the sparse path made zero dense-vector,
dense-predictor, or neighbor calls. With embeddings already cached from a local
test encoder, median scoring time over three passes was 0.103 seconds versus
0.046 seconds. This fixture measures local scoring only; it does not measure
production model quality, provider savings, or full-catalog preparation.

CPU-only describes inference requirements, not unlimited local capacity. Full
family preparation still scans and groups the catalog. The follow-up work below
tests a larger archived catalog on an isolated local copy. Current production
models, host contention, and first-run preparation must still be checked before
activation; the historical 6,730-family subset alone cannot establish readiness.

## Sparse scoring for both policies

`sparse_cpu` refreshes both broad and selective across the complete prepared
catalog without constructing an embedding encoder. Each policy retains its own
trained sparse classifier and label criteria. The selective classifier is not
replaced with the broad classifier. Both policies remain available in the
dashboard, including compare mode. Champion mode is separate and is not supported
by this named-policy CPU workflow.

The existing selective artifact may select dense components. Convert its trained
sparse component into an explicitly versioned model before using `sparse_cpu`:

```bash
python -m job_search.ranking.sparse \
  --state-db /private/job-boards-preference.db \
  --proxy-db /private/job-boards-proxy.db \
  --artifact-dir /private/models
```

This command writes candidate model artifacts and metadata, but does not change
policy mappings, train, score, or call inference. It verifies source hashes and
policy training provenance first. An already sparse-only broad model is reused.
Selective receives a deterministic new run ID, with its old scores and original
artifact preserved. Repeating the conversion reuses the registered version.
The new manifest retains sparse component cross-validation evidence and training
lineage, but does not inherit the dense model's selected or protected evaluation
claims.

For the intentional migration, keep ranking automation paused, back up the state
through the normal operations workflow, and rerun the same command with
`--activate`. That flag changes the named-policy mappings atomically after
checking they have not changed during conversion. It does not promote the
champion, enable automation, or refresh scores. A converted policy stays
unavailable for new recommendations until a matching full refresh completes;
existing saved shortlist sessions retain their historical provenance.

Then run the guarded full refresh:

```bash
python -m job_search.ranking.refresh \
  --db /private/job-boards.db \
  --state-db /private/job-boards-preference.db \
  --proxy-db /private/job-boards-proxy.db \
  --policy broad --policy selective --active-components-only --no-embeddings
```

For subsequent scheduled runs, merge these fields into the versioned runtime
configuration:

```json
{
  "shortlist_policy": "selective",
  "ranking_refresh_mode": "sparse_cpu"
}
```

`shortlist_policy` may also be `broad` or `compare`. Configuration changes do not
enable paused automation. The default `full` mode still computes embedding-based
diagnostics; select `sparse_cpu` explicitly. The no-embedding guard rejects future
mapped models requiring dense components before modifying ranking state, and the
CPU command does not resolve provider configuration. Model retraining, research
audits, salary extraction, and other inference features are separate workflows.

The archived 2,000-example selective training evaluation recorded sparse average
precision of 0.8293 versus 0.8644 for dense linear scoring. Broad sparse average
precision was 0.9507. These are proxy-label cross-validation measurements used
during selection, not independent human-preference accuracy. Selecting the sparse
component changes selective predictions and requires a new quality assessment;
it does not change the already sparse broad predictions. The bounded selective
stage remains optional when both policies can be scored on CPU.

Verification used the archived trained artifacts and a distributed 800-family
sample from the 349,827-family catalog. Both policies passed CPU scoring while
encoder creation, embedding, dense-vector loading, and training were explicitly
forbidden. Conversion reused the broad ID, created a distinct selective ID, and
preserved all source model records and existing score rows. This was an isolated
local-copy compatibility check, not a full two-policy production benchmark or
an independent quality evaluation.

## Skip redundant sparse refreshes

Each collection workflow schedules a ranking refresh. A backlog can therefore
contain several refreshes for the same catalog. Previously every queued refresh
rebuilt feature documents for all families to check their saved fingerprints,
even if the preceding refresh had already covered the same inputs.

Embedding-free refreshes now record a complete-pass receipt tied to catalog
mutation counters, per-model score counters, verified model artifacts, scoring
components, and implementation/dependency versions. SQLite triggers observe
ordinary SQL writers, including older application versions. The next unchanged
refresh checks this evidence before family preparation or document iteration and
finishes successfully with `reused: true`, reason `already_current`, and zero
checked or recomputed families. It still completes its original work item and
preserves downstream workflow lineage.

Changed source data, changed models, missing or modified scores, replaced
databases, and damaged tracking invalidate reuse. Samples and incomplete passes
cannot certify coverage. Ordinary scoring passes retain resumable checkpoints
for committed batches. A forced repair of legacy or corrupted score state must
finish before its cache is trusted; interruption requires restarting that repair.
The feature fingerprint includes the entire cleaned description used by sparse
scoring, so edits outside the sampled embedding chunks also invalidate scores.

Legacy completion receipts require one upgraded validation pass before they can
support this shortcut. Existing scores without a trusted cache record are
recomputed during that pass. Subsequent unchanged queued refreshes skip the
catalog scan. The shortcut applies only to embedding-free policies; dense paths
retain their existing refresh behavior.
Source tracking is deliberately conservative: even a collection sweep that only
updates `last_seen` triggers validation. The shortcut removes duplicate queued
passes over the same catalog; it does not eliminate validation after collection.

The dashboard separates catalog coverage from current activity. “Checking for
ranking updates” counts families examined, while recomputed counts describe
scores actually produced for each policy. A reused refresh has no percentage bar.
Progress belongs to the current work attempt; an old completed journal is not
shown as progress for a newly queued or started task. Coverage counts still mean
saved scores exist, not that a new score was calculated during the current pass.

## Full-catalog preparation and scoring measurements

The follow-up benchmark used disposable copies of an archived September 20
catalog containing 410,225 jobs and 349,827 families. It used the archived trained
models with an explicit benchmark-only broad/selective mapping. These are real
catalog/model measurements on local macOS, not measurements of the current EC2
deployment or its newer models.

| Operation | Elapsed | Peak process RSS | Verification |
| --- | --- | --- | --- |
| Baseline preparation (`0f74b76`) | 157 seconds | 5.33 GiB | Completed under an 8 GiB monitored allowance; exceeded the initial 4 GiB allowance. |
| Final preparation with persistent-volume scratch | 153 seconds | 1.49 GiB | Source and all four derived-table digests exactly matched the baseline. |
| Full broad scoring, batch size 256 | 501 seconds | 251 MiB | All 349,827 families scored; encoder construction and embedding calls explicitly forbidden. |
| Unchanged broad refresh | 133 seconds | 232 MiB | All 349,827 scores reused; both scoring and embedding calls explicitly forbidden. |

Preparation reduces peak process RSS by approximately 72%. It stages source text
in a private temporary SQLite database next to the catalog, on its persistent
volume. Production mounts `/tmp` as tmpfs, so relying on default SQLite temporary
file placement would consume RAM. Scratch directories are removed on normal exit
and exceptions. An abrupt process/host termination can leave a private
`.family-preparation-*` directory; remove such an abandoned directory only when
no preparation process owns it. Reserve disk space for a catalog-sized scratch
copy and SQLite journaling.

Text processing now holds one `(ATS, company, title)` comparison block at a time;
compact lineage/member metadata still grows with the catalog. The largest block
in this archive contained 584 families. A catalog dominated by one much larger
block can use more memory and comparison time. The read snapshot and atomic
publication preserve rollback behavior if ingestion changes concurrently.

The scoring run agreed with 252,276 matching archived feature fingerprints to a
maximum absolute score difference of 7.8e-16. Preparation comparisons used the
same Python 3.9.6 interpreter for both implementations; scoring used Python 3.12
and the artifact's recorded numpy/scipy/scikit-learn versions. Keep the production
runtime pinned: three historical family rows differed when re-normalized across
Python versions, independently of this change. Preparation and scoring peaks
were measured in separate processes and exclude host page cache and other
workloads; they are not a combined production memory guarantee.

Reproduce preparation parity without changing the supplied catalog:

```bash
python -m tests.benchmark_family_preparation \
  --db /private/archived-job-boards.db --max-rss-mib 8192 \
  --timeout-seconds 900 --prepared-db /private/new-prepared-copy.db
```

The harness creates disposable copies, monitors child RSS/time, and verifies
source/derived digests. Its optional output must be a new path. Local benchmark
receipts and database copies remain ignored private artifacts.

## Bounded selective candidates

The staged path requires complete broad scores and checks source membership and
every current feature fingerprint before choosing candidates. Only families with
at least one open posting qualify. It chooses the highest broad scores, optionally
reserving slots for deterministic exploration outside that group.

```bash
python -m job_search.ranking.staged \
  --db /private/job-boards.db \
  --state-db /private/job-boards-preference.db \
  --proxy-db /private/job-boards-proxy.db \
  --candidate-limit 100 --exploration-count 20 \
  --max-missing-texts 500 --max-missing-characters 1000000
```

This command is read-only by default: it loads metadata, not executable model
artifacts or providers. It requires the catalog to already use WAL and refuses a
non-WAL catalog without changing it; run the guarded broad refresh on the intended
working copy first. Preference-score reads use short transactions, and a changed
broad completion receipt aborts planning before inference. The command reports
exact unique uncached text/character demand for
the selected model revision. Exceeding either cap returns `budget_exceeded`.
For an intentional inference run, add `--execute --stage-db /private/staged.db`.
Execution checks the budget before constructing an encoder and enforces it again
on every submitted batch. An all-cached run never constructs a provider encoder.
Text/character limits do not promise a dollar cap: transport retries, provider
tokenization and billing remain governed by the existing inference usage controls.

Results and completion status live in separate `staged_runs`/`staged_scores`
tables. The full selective scores and coverage receipt are never overwritten;
failed stages remain explicitly failed, with completed embedding cache work
available to a retry. This path is an explicit evaluation command, not an enabled
scheduled pipeline or the dashboard's complete selective ranking.

An optional `--evaluation-db` reports descriptive candidate recall for matching
held-out reviews. It excludes training overlap across both models, template
lineage aliases, changed snapshots, and closed families. Missing training lineage
makes evaluation unavailable. Review slices are kept separate and do not establish
catalog-wide recall or selective ranking quality. Candidate pruning can miss
roles that the selective model would favor; validate that tradeoff before rollout.

On the same archived catalog, a read-only plan selected 100 candidates from
281,421 open families, reserving 20 slots for exploration. It excluded 68,406
fully closed families. Of 298 unique required texts, 222 were already cached;
the remaining 76 totaled 144,168 characters, below the example's caps. No model
artifact or provider was loaded. This measures bounded inference demand, not
candidate quality: the archive had no eligible held-out review examples.

## Memory attribution after deployment

Worker-owned subprocesses now sample process RSS, cgroup anonymous memory and file
cache, and host availability every two seconds. At most 3,600 samples and eight
command summaries are retained per attempt, with no process names, arguments,
environment values or command payloads in telemetry. Sampling failures cannot fail
work. Failed commands keep their samples; retries clear the previous attempt's
result. The runtime report aggregates measured numeric extrema only.

Process RSS excludes descendants. Cgroup counters include other processes in that
cgroup and charged file cache. Sampled maxima can miss short peaks, and kernel
lifetime high-water marks can predate the task. These distinctions are preserved
in the stored summaries. Operational status also reports current host counters;
no additional CloudWatch metrics or dimensions are introduced.

The follow-up read-only host probe found about 6.4 GiB available out of 7.6 GiB.
The dashboard's approximately 1.67 GiB cgroup charge included approximately
1.59 GiB of file cache and 77 MiB of anonymous memory. Current cgroup OOM counters
were zero. These observations do not explain the historical October 1 peak:
only host percentage was recorded then, and overlapping work records often had
zero-duration timestamps. Keep the current instance size until new samples
identify real workload peaks.

## Further work

Before activation, verify the deployed Linux runtime, current model artifacts,
WAL/disk headroom, and peak memory alongside normal collection. The local archived
catalog measurements establish scale, not a production soak. Validate staged
candidate recall with eligible held-out reviews before using it for selective
recommendations; the available archive contains no such review examples. Keep
partial selective coverage explicitly separate from a complete refresh.

Continue to observe memory during normal collection, ranking preparation, backups,
and deployments. Keep current host capacity until those peaks are understood.
Changing the model path is preferable to assuming a larger host or GPU will make
unnecessary embedding requests affordable.
