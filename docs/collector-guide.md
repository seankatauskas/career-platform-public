# job-boards

Pull every public job posting from every **Ashby, Greenhouse and Lever** job board.
No API key, no account, no dependencies.

All three publish an unauthenticated posting API that is per-company, keyed by a board
slug, with no global search endpoint. This finds the boards — **13,146** of them across
the three platforms — then fans out across all of them. **~308,000 live postings.**

> **Engineers and coding agents:** the [`openwiki/`](../openwiki/quickstart.md) wiki is the
> map of the code — architecture, workflows, data model, runbook. Agents should start
> with [For coding agents](#for-coding-agents-llms) below.

This branch also contains the private deterministic application-tracking pipeline. See
[the career resume guide](resumes/career-resume-runbook.md) for the editable personal fact bank,
reviewed resume imports, job-specific selection, and one-page Jake-template PDF flow.
See [the job-search runbook](operations/job-search-runbook.md) for the local dashboard, four-hour ATS
cadence, Outlook lifecycle tracking, approved scheduling actions, browser handoff, and
the capability-limited future Hermes boundary. The
[runtime guide](operations/job-search-runtime.md) documents the versioned config, core/model worker
lanes, durable opportunity DAG, stable watermarks, and dry-run launchd management.
The [ATS proxy model](models/ats-proxy-model.md) defines the evidence boundary, hybrid
LLM/deterministic scoring pipeline, and the distinction between hand-written,
grounded-rewrite, and synthetic resume comparisons. The
[resume lab runbook](resumes/resume-lab-runbook.md) covers its optional local dependencies,
safe TeX subset, bundled offline JSON driver, standard-resume commands, and worker
flow. The workbench supports at most 25 active standards and presents five choice
groups: all hand-written standards, one approval-gated factual rewrite, and three
comparison-only synthetic variants. Local inference dependencies and model weights
are not bundled. [Portable inference](models/inference-providers.md) documents the bounded
Runpod/OpenAI-compatible provider switch and pre-travel preference-model migration;
[single-host cloud deployment](operations/cloud-deployment.md) covers the hardened Compose stack,
networkless document tools, encrypted Linux state, and optional supervised Hermes
overlay.

## Quick start

No dataset is bundled; you generate your own. The only prerequisite is
[uv](https://docs.astral.sh/uv/) — no `pip install`, no venv, no dependencies, and it
fetches its own Python.

```bash
git clone https://github.com/mherzog4/job-boards.git
cd job-boards

# Identify your traffic. Archive operators ask clients to do this, and it puts
# your address on your requests rather than someone else's.
export JOB_SCRAPER_CONTACT="you@example.com"

# Find every board on every platform, then pull every posting from all of them.
uv run python -m job_search.collection.boards --refresh-boards --all
```

That is the whole thing. You end up with:

| file | what it is |
|---|---|
| `job-boards.csv` | every posting and its plain-text description, UTF-8 BOM so Excel renders `–`/`•` correctly |
| `job-boards.json` | the same rows, including descriptions |
| `job-boards.db` | SQLite job history plus the recurring ATS-board registry and discovery audit |
| `boards.json` | active/unverified board slugs exported from SQLite for scraper compatibility |

Scheduled application scans pass `--no-export` to retain complete jobs in SQLite
without writing redundant CSV/JSON row exports. Run receipts and failed-board
retry lists are still written. The flag requires database storage and cannot be
combined with `--no-db` or `--discover-only`; ordinary CLI runs keep their existing
exports by default.

Every row carries an `ats` column and a normalized plain-text `description`. One CSV and
one database cover all three platforms, and `(ats, id)` links the description and other
metadata to a unique posting without duplicating it across runs.

Later runs reuse `boards.json`, so a re-scrape is just `uv run python -m job_search.collection.boards --all`.
Re-run `--refresh-boards` monthly, or `--refresh-recent` daily — see
[How recent is the data](#how-recent-is-the-data--and-how-to-make-it-fresher), which also
covers `--since` for a fresher dataset.

### Recurring board discovery without downloading jobs

Use `--discover-only` when the board/customer registry is the output you want. It
updates SQLite, regenerates `boards.json`, and exits before making posting `GET`
requests or writing job CSV/JSON files:

```bash
# First run and monthly full refresh.
uv run python -m job_search.collection.boards --discover-only --refresh-boards

# Daily additive pass over recent archive captures plus urlscan.io.
uv run python -m job_search.collection.boards --discover-only --refresh-recent

# Import/synchronize the bundled seed and current boards.json without archive traffic.
uv run python -m job_search.collection.boards --discover-only
```

The database is authoritative for recurring discovery:

- `ats_boards` is the current registry, keyed by `(ats, slug)`. It records the API
  URL, first/last discovery, validation timestamps, HTTP state and errors.
- `discovery_runs` records each import, full, or recent run and its totals.
- `discovery_observations` records every candidate seen in each run, its source(s),
  and whether validation returned active, 404, or a transient error.

A seed/cache entry begins `unverified` until this database observes a successful
`HEAD`. A success makes it `active`; only a confirmed 404 makes an existing board
`inactive`. Absence from an archive result and transient failures never downgrade a
known board. `boards.json` contains active and unverified boards, while inactive rows
remain in SQLite for history. The slug is an ATS board identifier, not necessarily a
canonical company name; do not key it without `ats`.

### Narrower searches

```bash
uv run python -m job_search.collection.boards --ats greenhouse --title "swe"  # one platform
uv run python -m job_search.collection.boards --ats ashby,lever --all         # a subset
uv run python -m job_search.collection.boards --title "software engineer"
uv run python -m job_search.collection.boards --title "software engineer" --match exact
uv run python -m job_search.collection.boards --title "product designer" --remote --limit 200
uv run python -m job_search.collection.boards --grep '\brust\b|\bgolang\b'    # search titles + descriptions
uv run python -m job_search.collection.boards --all --since 7d                # only the last week's postings
uv run python -m job_search.collection.boards --all --new-only                # only what the db has not seen
uv run python -m tests.test_job_boards                            # offline self-check
```

## Label jobs in a local web app

After collecting jobs, prepare opportunity families and the initial semantic embedding
cache before starting the guided label program. This makes the 70% diversity arm truly
embedding-driven from the first judgment:

```bash
python3 -m job_search.collection.dedupe --db job-boards.db prepare
python3 -m job_search.ranking.model --db job-boards.db embed
python3 -m job_search.ranking.labeler
```

Then open <http://127.0.0.1:8765>. The page runs a guided 1,000-decision program and
always shows the current stage, its quota, the number remaining, and overall progress.
Use **Pursue** / **Pass** / **Unsure or mixed** / **Skip unusable**, or the `I` / `N` /
`M` / `S` keyboard shortcuts. `U` removes the most recent label. The program selects
described, open jobs posted in the last 30 days, avoids already-labeled opportunity
families, and records whether each example came from diversity, uncertainty,
model-disagreement, high-score, or uniform sampling. The evaluation stage is
deterministic, score-hidden, and isolated from training leakage groups.

| stage | decisive jobs | purpose | stored role |
|---|---:|---|---|
| Calibration | 50 | establish a consistent Pursue/Pass judgment | training |
| Foundation | 150 | build broad preference coverage | training |
| Expansion | 300 | cover more role families | training |
| Refinement | 300 | add depth and less obvious examples | training |
| Evaluation | 200 | measure the eventual model on untouched judgments | evaluation |

Only Pursue and Pass advance a stage. Unsure preserves a genuinely mixed judgment, and
Skip records an unusable or unjudgeable posting; both remain in the database but do not
consume the decisive-label quota. At 1,000 decisive labels the UI stops and displays a
completion screen. Each row records `program_stage` and `sample_role`, so the first 800
decisions can train a model without leaking the final 200 evaluation decisions into it.

Current labels are stored in `job_preferences`, while every judgment also creates an
immutable row in `preference_examples` containing the exact text, metadata, family and
leakage identifiers, sampling strategy, and dataset version seen at label time. No JSON
export is needed, and later posting edits cannot rewrite historical training input. The
labeler binds only to localhost and makes **no ATS requests**. It shows the stored
description and metadata from the database and also links to the original posting.

The optional context panel keeps distinct questions from contaminating one another:

- **Qualification fit:** strong, plausible, stretch, unlikely, or unclear.
- **Primary reason:** role/work, domain, skills, growth/scope, company,
  location, job type, or compensation. Choose at most one.
- **Hard blockers:** location, compensation, work authorization, clearance, travel,
  schedule, or job type. Choose any that apply.
- **Skip reason:** insufficient information, duplicate, not a job, wrong language, or
  another unusable condition.

Interest remains the primary preference signal, with `maybe` preserving genuine
uncertainty rather than forcing noisy positive or negative training data. The prompt
explicitly says to ignore current résumé competitiveness and assume practical constraints
are workable. Qualification fit stays separate so an interesting stretch role remains a
positive example. Hard blockers record why an otherwise appealing role cannot work
without teaching a future preference ranker to reject that kind of work. All context
annotations remain optional, keeping the fast path to one key per job.

To use another database or port:

```bash
python3 -m job_search.ranking.labeler --db /path/to/jobs.db --port 9000
```

The labeler tests are also offline:

```bash
python3 -m tests.test_job_labeler
```

## Build and use the personalized ranker

The preference system keeps three questions separate: whether the work is interesting,
whether practical constraints work, and whether the posting is still a current
opportunity. Structured salary fields and blocker labels never become negative
interest features; they are applied later as shortlist constraints.

### Zero-label LLM preference proxy

The default personal workflow does not require manually labeling jobs. A local LLM
teacher reads a private profile plus compensation-redacted job text, scores semantic
role relevance, interesting work, and plausible qualification, and emits one validated
judgment. Deterministic rules derive two labels from that same judgment:

- **Selective** favors the strongest, most interesting credible matches.
- **Broad** keeps most software-related roles while rejecting clear mismatches.

Location and compensation never enter the teacher prompt. They remain explicit ranking
signals: proxy shortlists require high-confidence US eligibility, prefer the seven
configured metros over US-remote and other US locations, and treat missing salary as
neutral. Raw `jobs.location` is preserved; versioned enrichment lives in separate tables.

Copy the tracked schema to the ignored private profile, edit it, then prepare the
deduplicated and location-enriched candidate pool:

```bash
cp examples/preference-profile.example.json preference-profile.json
python3 -m job_search.collection.dedupe --db job-boards.db prepare
python3 -m job_search.collection.locations backfill --db job-boards.db
python3 -m job_search.ranking.proxy prepare --db job-boards.db \
  --sample-size 2400 --audit-size 400
python3 -m job_search.ranking.proxy estimate --db job-boards.db --split training
```

The queue is deterministic, resumable, capped by company, and keeps template/leakage
groups out of both splits. It snapshots its input and raw/validated teacher output in
the ignored `job-boards-proxy.db` sidecar. Run the local 27B teacher over training only;
failed jobs can be retried up to three attempts:

```bash
.venv-local-mlx/bin/python -m job_search.ranking.proxy run --db job-boards.db \
  --split training --until-empty
python3 -m job_search.ranking.proxy retry --db job-boards.db --split training --include-excluded
```

Distillation fits both policies using the existing semantic-embedding + TF-IDF ensemble,
scores the corpus, and records each policy's explicit model run. It automatically folds
in normal product actions once at least 10 balanced signals exist: Apply and Preference
Pass are strong, Save is weaker, and Blocked/Duplicate never teach taste.

```bash
python3 -m job_search.ranking.proxy distill --db job-boards.db

# Only after both students are frozen, reveal and score the protected audit.
.venv-local-mlx/bin/python -m job_search.ranking.proxy run --db job-boards.db \
  --split audit --until-empty
python3 -m job_search.ranking.proxy audit --db job-boards.db
python3 -m job_search.ranking.proxy status --db job-boards.db
```

The default 2,400/400 sizes are modeling defaults, not a product shortlist size. The UI
shortlist remains configurable from 1 to 100 jobs. Selective, Broad, and Compare modes
are available in the same local app; Compare alternates the independent rankings instead
of treating their probabilities as calibrated on one shared scale.

First build derived opportunity families. Exact long-description variants from the
same ATS/company/title collapse into one family; short descriptions remain singletons.
Exact-description leakage groups and conservative near-template clusters are retained
separately for model evaluation. The command is idempotent and never changes `jobs`:

```bash
python3 -m job_search.collection.dedupe --db job-boards.db prepare
python3 -m tests.test_job_dedupe
```

Before trusting fuzzy clusters, inspect the 200 largest multi-family groups. `audit` opens
SQLite read-only, orders deterministically by family/job count, and emits bounded canonical
description excerpts plus member IDs rather than full descriptions. It prints JSON by
default or writes the same JSON document to `--output`:

```bash
python3 -m job_search.collection.dedupe --db job-boards.db audit --limit 200
python3 -m job_search.collection.dedupe --db job-boards.db audit --limit 200 \
  --output dedupe-cluster-audit.json
```

Install the optional local-model runtime once and place the embedding model in the
local Hugging Face cache before the initial `embed` command above. Normal scraper and
labeler usage remains dependency-free:

```bash
python3 -m pip install -r requirements/preference.txt
```

The production default is `BAAI/bge-base-en-v1.5`. Embedding runs in strict offline
mode and reports a clear error instead of downloading weights implicitly. Production
training starts at 200 decisive labels and still requires both classes across enough
independent leakage groups for grouped cross-validation.

```bash
# Dependency-free sizing only: no model load, embedding, or network access.
python3 -m job_search.ranking.model estimate --db job-boards.db

python3 -m job_search.ranking.model --db job-boards.db embed
python3 -m job_search.ranking.model --db job-boards.db train
python3 -m job_search.ranking.model --db job-boards.db score

# Run at 200 decisive training labels, after each scrape, and then every 25 labels.
# It always refreshes the current champion first and prints any new candidate run ID.
python3 -m job_search.ranking.model --db job-boards.db refresh

# After the protected 100/50/50 evaluation queue is fully labeled.
python3 -m job_search.ranking.model --db job-boards.db evaluate
```

The first model that clears both baselines becomes the initial champion. Normally,
this requires strictly higher out-of-fold NDCG@20. An exact perfect-score tie with
the title baseline also qualifies when both have Precision@20=1, random NDCG@20
is below 1, and the candidate's average precision exceeds both baselines. Missing
or invalid tie-break evidence fails this exception. Later
`refresh` runs never replace it silently: they incrementally embed and score new or
changed jobs with that champion's exact recorded encoder, then create a candidate when
25 new decisive labels are available. Inspect the returned metrics and promote the
candidate explicitly:

```bash
python3 -m job_search.ranking.model promote --db job-boards.db \
  --run-id RUN_ID --reason "better grouped validation metrics"

# Restore any model that previously served as champion.
python3 -m job_search.ranking.model rollback --db job-boards.db \
  --run-id PREVIOUS_RUN_ID --reason "candidate regression"
```

To benchmark a different embedding encoder, run `embed --model MODEL --model-revision
REVISION`, then `train` and `score --run-id RUN_ID`. Once the frozen evaluation set is
complete, evaluate both the champion and candidate against those same 200 snapshots.
A cross-encoder promotion uses only `protected_top_ranked` as its acceptance slice: it
requires at least +0.02 NDCG@20, no worse than -0.02 Precision@20, and a measured
incremental embedding cost of at most 30 minutes. The uniform and company-holdout
slices are post-selection audits and cannot offset a failed top-ranked gate:

```bash
python3 -m job_search.ranking.model evaluate --db job-boards.db
python3 -m job_search.ranking.model evaluate --db job-boards.db --run-id RUN_ID
python3 -m job_search.ranking.model promote --db job-boards.db --run-id RUN_ID \
  --projected-incremental-minutes 18 --reason "protected evaluation passed"
```

`evaluate` uses the exact encoder revision recorded by each artifact and only embeds
missing frozen snapshots. It never substitutes the current default encoder. A
successful cross-encoder promotion consumes that protected set, so another encoder
cannot reuse it; a later comparison needs a separately collected future protected set.
`--force` bypasses reuse only for documented operational recovery and is recorded in
champion history.

Embeddings, model runs, component scores, and the champion pointer go to the sibling
`job-boards-preference.db` sidecar. Pickled classifiers and manifests go under
`.models/preference/`; both locations are ignored by Git. The operational scraper
database remains authoritative and contains no vectors or classifier artifacts.

Training combines three independently scored signals:

- a linear boundary over title/metadata and chunked semantic embeddings;
- cosine neighbors among liked and disliked examples;
- word and character TF-IDF for exact technologies, phrases, and role names.

Grouped cross-validation prevents exact descriptions and near templates from crossing
folds, including template groups whose membership changes after a later scrape. The
manifest reports NDCG@20, Precision@10/20, Recall@20, average precision,
title-only and random baselines, component ablations, selected weights, exact label
fingerprints, and artifact hashes. The simplest model within 0.01 NDCG@20 of the best
candidate wins. `--encoder hashing` exists only for deterministic smoke tests; it is
not a semantic production model.

Start the same local app to review the precomputed shortlist:

```bash
python3 -m job_search.ranking.labeler --db job-boards.db \
  --preference-db job-boards-preference.db \
  --proxy-db job-boards-proxy.db \
  --recommendation-limit 20 \
  --salary-floor 150000
```

The recommendation count is configurable. By default the app returns up to 20 current
family-level opportunities, reserves two slots for exploration, caps repeated companies
and titles, and chooses the best feasible posting variant. Missing or ambiguous salary
is shown as unknown and never treated as a failure. Apply, Save, Preference Pass,
Constraint Block, and Duplicate feedback are stored separately from human labels.
Apply and Preference Pass become strong passive signals, Save becomes a weaker positive,
and views only record exposure. Constraint blocks and duplicates never teach taste.

All new checks are offline:

```bash
python3 -m tests.test_job_dedupe
python3 -m tests.test_preference_model
python3 -m tests.test_job_labeler
python3 -m tests.test_location_enrichment
python3 -m tests.test_preference_proxy
```

## Salary enrichment and validation

Salary extraction is automatic on every normal job scan that writes SQLite. It does
not make a second ATS request for each posting:

- Ashby board requests add `includeCompensation=true` and read the returned
  `compensation.compensationTiers[].components[]` values, falling back to
  `summaryComponents`; tier titles are retained as location scopes.
- Lever's existing board response already contains `salaryRange`,
  `salaryDescription`, and `salaryDescriptionPlain`.
- Greenhouse has no separate normalized salary field in this API, so the parser reads
  the pay-transparency blocks in the `content=true` description.
- Generic descriptions are not parsed with fuzzy regular-expression rules. After the
  local pipeline is activated, a missing usable native value is durably queued for
  NuExtract instead.

Annual and hourly values are kept as published. Native monthly values may retain their
published period and annual normalization, but monthly, daily, weekly, per-class, and
one-time description values are outside the LLM target. Currency conversion is never
attempted, and an ambiguous native `$` remains `UNKNOWN` unless its ATS field supplies
the ISO currency.

Machine-derived values stay separate from the authoritative posting:

| table | purpose |
|---|---|
| `job_enrichment` | one parser result per `(ats, job_id)`, including version, status, source, and timestamp |
| `job_compensation_ranges` | one or more normalized ranges, source evidence, annual values, and first/last/removed history |
| `salary_llm_queue` | durable new/changed-job inference state, raw/normalized output, retries, validation, and timing |
| `salary_validation_queue` | the reproducible, weighted audit sample |
| `salary_validation_labels` | your verdicts, kept apart from both jobs and machine output |

The range table preserves components, nullable bounds, currencies, periods, evidence,
and source history. `value_kind` is derived from the bounds for LLM output. Source
precedence is `ats_structured`, `ats_rendered`, then `llm_description`. A usable native
primary cash value suppresses inference and supersedes an older LLM value; bonus,
equity, unknown-currency, or unsupported-period native data does not suppress fallback.

### Activate the recurring local fallback

Activation is explicit and idempotent. It retires old `description_rule` rows while
keeping their history, establishes the current database as a no-backfill baseline, and
queues nothing:

```bash
python3 -m job_search.salary.llm activate --no-backfill
python3 -m job_search.salary.llm status
```

After activation, every normal database-writing scan queues only newly seen jobs or
jobs whose description/native compensation fingerprint changed and which still lack
usable native compensation. The scan does not load the model or wait for inference.
Drain the separate local queue with one MLX worker:

```bash
.venv-local-mlx/bin/python -m job_search.salary.llm run --limit 100
.venv-local-mlx/bin/python -m job_search.salary.llm run --until-empty
python3 -m job_search.salary.llm retry --failed
```

Each job is committed independently. Interrupted workers resume from SQLite, runtime
failures retry up to three times, invalid deterministic outputs enter `needs_review`,
and closed/empty/superseded jobs are skipped. The queue status includes its oldest age
and a warning after one hour. Long descriptions are processed in overlapping bounded
chunks without a salary-keyword prefilter.

The old `job_search/salary/enrichment.py` description backfill is intentionally disabled. Existing
jobs are not queued by activation or by an unchanged later full scan.

### Legacy deterministic-parser audit

`job_search/salary/labeler.py` and its existing labels remain available for historical analysis,
but that deterministic parser is no longer a production compensation source:

```bash
python3 -m job_search.salary.labeler
```

Open <http://127.0.0.1:8766>. The default deterministic queue contains up to 500 jobs:
300 positive examples sourced from description/pay-block parsing and 200 jobs where the
parser found no salaried compensation. Repeated companies are capped at three in each
audit half, while
the queue stores stratum weights so summary estimates can account for sampling.

For a positive example choose **Correct**, **Incorrect**, **Unclear**, or **Hourly / other
pay**. For a negative audit choose **No salary**, **Missed salary**, **Unclear**, or
**Hourly / other pay**. Keys `1` through `4` select those choices; `Z` undoes the last
verdict. Non-salaried-pay labels are reported separately and do not enter the salaried
precision or miss-rate estimates. The UI shows normalized values,
source, exact evidence, full stored description, and a link to the original posting.
The header reports weighted positive precision and negative miss-rate estimates with
approximate 95% confidence intervals as decisive labels accumulate. It binds to
localhost and makes no ATS requests.

Queue creation is idempotent: restarting the server resumes the same queue and labels.
To intentionally discard those salary-validation labels and build another sample:

```bash
python3 -m job_search.salary.labeler --rebuild-queue --seed 20260830 \
  --positive 300 --negative 200 --company-cap 3
```

Offline checks for both parts:

```bash
python3 -m tests.test_salary_enrichment
python3 -m tests.test_salary_labeler
```

### Build a fresh LLM-reviewed salary gold set

The deterministic parser is not used as truth for this experiment. `job_search/salary/teacher.py`
creates a new 500-job sample,
stores each hosted model response unchanged, and keeps your final reviewed label in a
third table. Nothing is written into `jobs`, `job_enrichment`, or
`job_compensation_ranges`.

The sample is deterministic and balanced across ATS inside four targeted groups: 50%
likely USD annual pay, 20% likely USD hourly pay, 15% non-USD pay, and 15% no qualifying
pay signal. These are sampling hints, not labels; the hosted models and reviewer still
decide what the posting actually contains. It caps each company at three jobs and does
not pretend job count is company size. The first 100 positions form a calibration split
and the remaining 400 form an evaluation split. Sampling weights are stored with every
row.

API keys are read only from `FIREWORKS_API_KEY` and `OPENAI_API_KEY` in the current
shell. Environment variables are not tied to a directory, so it is safe to export them
from the workspace root and then `cd job-boards`. No key is copied into SQLite or a
project file.

To prove which Fireworks value is loaded without displaying it, and validate it with a
free authenticated metadata request:

```bash
python3 -m job_search.salary.teacher auth-check
```

This reports only the stripped value's character count, a short SHA-256 fingerprint,
and common paste mistakes such as literal quotes or `FIREWORKS_API_KEY=` being included.

For a first batch, prepare the queue with:

```bash
cd job-boards
python3 -m job_search.salary.teacher prepare --sample-size 500 --company-cap 3 --fresh
python3 -m job_search.salary.teacher status
```

Before replacing a reviewed batch, export it to a separate local, self-contained file.
Then use `--exclude-current` so all current job IDs are remembered and cannot appear in
the replacement:

```bash
python3 -m job_search.salary.batch_archive --out salary-archives/batch-name.jsonl
python3 -m job_search.salary.teacher prepare --sample-size 500 --company-cap 3 \
  --seed 20260831 --fresh --exclude-current
```

Every request is one job and commits immediately, so an interruption or exhausted
provider balance can be resumed. A quota error stops that provider only. There is no
automatic provider fallback because changing the model silently would invalidate the
evaluation.
Use `--split calibration` or `--split evaluation` for batch runs so resuming a partially
finished command cannot cross the locked 100/400 split boundary.

First inspect the no-cost estimate, then run exactly one paid Fireworks request:

```bash
python3 -m job_search.salary.teacher estimate --provider fireworks --limit 1
python3 -m job_search.salary.teacher predict --provider fireworks --limit 1 \
  --max-usd 0.10 --execute
```

Run the independent OpenAI result for that same first sample position:

```bash
python3 -m job_search.salary.teacher estimate --provider openai --limit 1
python3 -m job_search.salary.teacher predict --provider openai --limit 1 \
  --max-usd 0.10 --execute
```

`predict` is a dry run unless both `--execute` and `--max-usd` are supplied. It defaults
to serial requests with a one-second delay, retries transient failures, records actual
token usage and cost, and refuses a batch whose conservative estimate exceeds the
dollar ceiling. The configured models are Fireworks
`accounts/fireworks/models/qwen3p8-2p4t-a95b` and OpenAI `gpt-5.6-sol`.

Fireworks uses `reasoning_effort=low` and a 2,400-token output allowance because this
Qwen3.8 checkpoint requires thinking before its final JSON. OpenAI retains a 1,200-token
default. Empty or malformed successful responses are not automatically repeated: their
usage, cost, response ID, and finish metadata are recorded, then the row remains an
explicit error for a deliberate retry. `--max-output-tokens` can override either default.
Evidence validation compares quotes against both the stored source and rendered plain
text, so HTML boundaries and entity-encoded dashes do not create false warnings. After
a validator-only change, refresh stored statuses without calling either provider:

```bash
python3 -m job_search.salary.teacher revalidate
```

After predictions exist, review them locally:

```bash
python3 -m job_search.salary.review
```

Open <http://127.0.0.1:8767>. The left pane contains the full posting and the right
pane contains each available model draft plus editable gold-label JSON. Press `1` to
accept Fireworks, `2` to accept OpenAI, `E` to edit, or `U` to undo. Saving revalidates
amount bounds and requires each evidence quote to exist in the stored description.

The new tables are deliberately separate:

| table | purpose |
|---|---|
| `salary_labeling_queue` | deterministic sample, split, strata, and sampling weights |
| `salary_sample_history` | job IDs excluded from replacement batches; no descriptions or labels |
| `salary_model_predictions` | immutable-at-review model drafts, validation issues, token counts, spend, and resumable errors |
| `salary_gold_labels` | the human-confirmed structured result used for evaluation/training |
| `salary_gold_events` | undo history for gold-label edits |

Raw teacher predictions and gold labels retain annual/hourly ranges in every currency.
The primary local-model metric projects both sides to USD. It requires at least one exact
supported USD range on positive jobs, rejects unsupported predicted USD ranges, and
requires no USD prediction when gold has none. Missing additional valid ranges lowers
the separately reported value recall without failing this primary usability score. Base
salary, OTE, and target are equivalent when the numbers match. See
[docs/models/local-model-training.md](models/local-model-training.md) and the executable canonicalizer in
`job_search/salary/training_target.py`.

All hosted-pipeline tests are offline and make no paid request:

```bash
python3 -m tests.test_salary_teacher
python3 -m tests.test_salary_review
```

### Version 3: minimum and maximum salary bounds

The original 500-label experiment remains frozen. The separate v3 pipeline represents
`starts at` / `$200k+` as a minimum with a null upper bound and `up to` as a maximum
with a null lower bound. A fresh ATS-balanced 500-job batch and a 59-job audit of the
old one-sided cases are already prepared. See [docs/models/salary-v3.md](models/salary-v3.md) for teacher
and reviewer commands, and [docs/archive/fireworks-backfill-estimate.md](archive/fireworks-backfill-estimate.md)
for measured hosted-backfill cost and timing scenarios.

## How complete is this?

Measured by a real `--refresh-boards --all`, not estimated:

| platform | boards live | companies hiring | jobs |
|---|---|---|---|
| Ashby | 3,617 | 3,289 | 54,591 |
| Greenhouse | 6,797 | 5,660 | 180,915 |
| Lever | 2,718 | 2,113 | 72,594 |
| **total** | **13,132** | **11,062** | **308,100** |

That is the `--refresh-boards` figure; [`--refresh-recent`](#closing-the-discovery-gap---refresh-recent)
has since taken the cached board list to **13,146**, which is the count the runtimes below
are measured over.

A full `--refresh-boards --all` took **26 minutes** measured before connection pooling —
most of it discovery, which later runs skip. The scrape half of that is now 41% faster
(see [Performance](#performance)); the discovery half has not been re-timed since. Note the board/company gap: ~2,000 boards are real customers with nothing
currently listed, which is expected and not an error.

So it gets **every listed job on every board it knows about**. The honest limit is the
board list, not the scraping — and no vendor publishes a list of its customers to
check against, so completeness cannot be proven, only bounded.

Where boards can still be missed:

- **Discovery only sees what the Internet Archive captured.** A board that exists but was
  never crawled is invisible. This is real, not theoretical: `newtonx` was live but absent
  from 191k archived URLs. To stop that from costing you anything, `--refresh-boards`
  unions its results with `job_search/collection/boards.seed.json` and the previous `boards.json`, so a refresh
  never loses a board an earlier run knew about.
- **New customers of any of these platforms** appear before the archive notices them — a median of 48 days
  before, measured above. Re-run `--refresh-boards` monthly, or add the slug by hand.
- **The shape filter** drops candidates that cannot be slugs. Sampling 150 of the 2,463 it
  rejected on Ashby turned up zero real boards, so this looks safe, but it is a sample.

If you find a board this misses, add it to `job_search/collection/boards.seed.json` and it is permanent.

## How recent is the data — and how to make it fresher

Every run hits each platform's API directly, so the *data* is live. But the median
posting in a full pull is **62 days old**, because companies leave requisitions listed:

| ats | jobs | median age | >1yr | >3yr | oldest |
|---|---|---|---|---|---|
| ashby | 54,591 | 48d | 3.9% | 0.3% | 2,484d |
| greenhouse | 180,915 | 60d | 13.0% | 1.8% | 2,776d |
| lever | 72,594 | **97d** | **26.2%** | **14.1%** | **6,078d** |
| **all** | 308,100 | **62d** | 15.6% | 5.1% | — |

That tail is real upstream data, not a parsing bug. Palantir's Lever board carries a
"Forward Deployed Software Engineer" with `createdAt` **2009-12-05** — verified against
the raw API. Lever boards accumulate the most evergreen postings by a wide margin.

**You cannot lower the age of what exists, only choose what to collect.** Two flags do
that, and they catch different things:

```bash
uv run python -m job_search.collection.boards --all --since 1d --sort recent   # today's postings, newest first
uv run python -m job_search.collection.boards --all --since 7d     # published in the last week
uv run python -m job_search.collection.boards --all --new-only     # never seen by the database before
uv run python -m job_search.collection.boards --all --since 30d --new-only
```

**Today's jobs, today.** `--since 1d` over all 13,146 boards takes about **3m51s** and
returned **5,980 postings** on a real run — 133 of them published within the previous
hour, the freshest **6 minutes old**. Pair it with `--sort recent` so the newest are at
the top; the default `--sort board` groups by platform and company, which buries them.

| posted within | jobs |
|---|---|
| 1 hour | 133 |
| 3 hours | 686 |
| 6 hours | 1,909 |
| 12 hours | 4,441 |
| 24 hours | 5,980 |

None of the three APIs support server-side date filtering — `updated_after` and friends
are silently ignored, verified against all three — so every board is fetched and the
window is applied locally. ~4 minutes is therefore the floor for a full sweep, and the
only way to see a posting sooner is to run more often.

`--since` accepts `7d`, `2w`, `3m`, `1y`, or a bare number of days:

| `--since` | jobs | share | median age |
|---|---|---|---|
| 7d | 35,490 | 11.5% | **4.0d** |
| 14d | 58,294 | 18.9% | 6.0d |
| 30d | 95,780 | 31.1% | 12.0d |
| 90d | 183,228 | 59.5% | 27.0d |
| (none) | 308,100 | 100% | 62.0d |

`--new-only` catches what `--since` cannot: a 200-day-old requisition that only appeared
on a board today, or one on a board you only just discovered. It compares against the
`(ats, id)` keys already in the database, so it needs the database and errors with
`--no-db`.

**Neither ever marks a posting closed.** A run that filtered did not see what it filtered
out, so it cannot conclude those postings are gone — the same rule that already applies to
`--title` and `--grep`. Without it, one `--all --since 7d` would close everything older
than a week.

**They also differ in what they cost the platforms.** `--all --new-only` is the one
combination that may send `If-None-Match`, so an unchanged board transfers nothing at all
([conditional requests](#conditional-requests-on---all---new-only)). Adding `--since`
turns that off — `may_use_etags()` requires a run that is unfiltered apart from
`--new-only` — so every `--since` run re-downloads all 13,146 boards in full. If you are
scraping on a schedule and want the cheap pass, run `--all --new-only` and apply the date
window to the database afterwards:

```sql
SELECT company, title, jobUrl FROM jobs
WHERE first_seen > datetime('now', '-1 day')
ORDER BY publishedAt DESC;
```

**Board discovery lags by ~48 days.** Separately from posting age: a company that adopts
any of these platforms is invisible until the Internet Archive crawls its board. Comparing
each board's first archive capture against its oldest surviving posting:

| percentile | lag before the archive first saw the board |
|---|---|
| p25 | 20 days |
| **p50** | **48 days** |
| p75 | 110 days |
| p90 | 257 days |

The archive is actively crawling — 348 of the 3,617 Ashby boards were captured within
the last week, some the same day — but a brand-new customer typically waits about seven weeks to
become discoverable.

**Why the lag matters less than it looks.** A board only has to be discovered once; after
that every scrape reads live data from it. The lag is a one-time cost per company, not a
staleness tax on jobs, and it only applies to companies that adopted their ATS in
the last couple of months. For the other ~13,000 it is already paid.

### Closing the discovery gap: `--refresh-recent`

The archive is thorough but slow. urlscan.io indexes scans people ran *today*, so it
surfaces boards the archive has not reached yet — a measured run added 14 boards a full
Wayback crawl had missed, including `headway`, `lab37` and `eltropyinc`.

| | `--refresh-boards` | `--refresh-recent` |
|---|---|---|
| Wayback | full crawl, 2.9M URLs | last 30 days only, ~17k URLs |
| urlscan.io | — | recent public scans |
| runtime | ~26 min | **~4 min** | (both pre-pooling)
| cadence | monthly | daily |

```bash
uv run python -m job_search.collection.boards --refresh-recent --all --since 7d   # a daily fresh-jobs run
```

It is purely additive — a measured run went 13,132 → 13,146 boards and lost none, because
discovery unions with the seed and the previous cache. urlscan's anonymous API allows 30
searches/minute and this makes four, so no key is needed.

If you need one specific new company immediately, skip discovery entirely — add its slug
to `job_search/collection/boards.seed.json` and it is permanent from the next run.

## Match modes

`--match fuzzy` (default) — either string contains the other, so it works in both
directions. A short query finds longer titles, and a long query still finds the short
title inside it:

| `--title` | matches |
|---|---|
| `software engineer` | Software Engineer, Senior Software Engineer, Backend, SOFTWARE ENGINEER II |
| `senior software engineer, backend` | Senior Software Engineer, Backend, **and** Software Engineer |

The reverse direction requires the title to be **at least two words**. Without that guard,
querying `senior software engineer` also matches every job titled just `Engineer`,
`Software`, or `Senior`. An empty `--title` matches nothing rather than everything.

`--match exact` — the whole title must equal the query (case- and whitespace-insensitive).
`software engineer` matches only `Software Engineer`.

## Searching descriptions with `--grep`

Titles are a weak filter — they miss "Software Development Engineer" and tell you nothing
about the stack. `--grep` runs a case-insensitive regex against the job title *and*
description and puts the surrounding context in a `matched` column, so a hit can be
judged without opening the posting.

```bash
uv run python -m job_search.collection.boards --grep '\brust\b|\bgolang\b'          # title and description
uv run python -m job_search.collection.boards --title engineer --grep '\bkubernetes\b'  # both must match
```

The two fields are searched separately, not concatenated, so a pattern cannot match
across the seam between them and report a hit that is in neither.

`--title` and `--grep` are ANDed. Giving `--grep` alone drops the title filter entirely
rather than silently ANDing the default `software engineer` onto it.

**Use `\b`.** Without word boundaries a pattern matches inside longer words, and job
descriptions are full of boilerplate that will catch you:

| pattern | jobs matched (26 Ashby boards) |
|---|---|
| `rust\|golang` | **1350** — `rust` matches "t**rust**", which is in nearly every description |
| `\brust\b\|\bgolang\b` | **72** |

That is an 18x false-positive rate with no visible symptom, so the script warns on stderr
when a `--grep` pattern contains no `\b`.

Every result keeps its full normalized plain-text description. `matched` additionally
keeps the short context windows that explain why a `--grep` result matched.

**Greenhouse costs ~26x more on every job scan.** Ashby and Lever return descriptions in
their ordinary responses. Greenhouse only returns them with `?content=true`, which takes
one board from 25KB to 653KB gzipped — measured on `stripe`. The script now requests that
representation on every Greenhouse posting scan so descriptions are never selectively
missing. A full Greenhouse sweep is multiple gigabytes; scope live experiments with
`--ats greenhouse --limit 10` unless you intend a full collection.

Fuzzy is much wider than exact: on a 26-board Ashby sample, `software engineer` returned
**268** jobs fuzzy against **2** exact, because most companies prefix with Senior/Staff.

## Boards that fail, and retrying just those

A board can error rather than answer: a throttle, a timeout, a connection dropped
mid-run. The `N err` figure in the progress line counts those, and each one prints to
stderr — but a count is not a recovery plan, and re-running a 13,000-board sweep to
recover eight of them is absurd.

So the failures are written to `<out>.failed.json`, in the same shape as `boards.json`:

```json
{ "greenhouse": ["yotpo", "yieldmo", "yld"] }
```

`--boards-from` reads that shape, so the retry is the same command with one flag
swapped:

```bash
uv run python -m job_search.collection.boards --ats greenhouse --all         # ... 8 boards error
uv run python -m job_search.collection.boards --all --boards-from job-boards.failed.json   # just those 8
```

The file is written only when something failed, and deleted when nothing did, so a
stale one from an earlier run cannot be mistaken for this run's result. 404s are not
included — a dead slug is an expected answer rather than a failure, and it is already
pruned from `boards.json` automatically.

`--boards-from` restricts which *boards* are scanned, not which postings are kept, so
it does not touch either gate: closing is already scoped to the boards a run actually
visited, the same rule that keeps `--limit 10` from closing postings at companies it
never opened. It does disable the 404 self-prune, since a caller-supplied subset must
not be written back as though it were the whole discovered list.

## The database

Every run also upserts into `job-boards.db` (SQLite, stdlib, no setup). The CSV is a
snapshot of one query; the database accumulates across runs and is what lets you ask
questions a snapshot can't answer.

Rows are keyed on **`(ats, id)`**, with `first_seen` preserved and `last_seen` refreshed.
The composite key is deliberate: Greenhouse posting ids are integers while Ashby and Lever
use UUIDs, so a bare `id` risks a collision that would silently overwrite one platform's
posting with another's. Everything else is overwritten each run, since titles and
locations do get edited in place on live postings. The exception is `matched`: a later
title-only run won't blank out `--grep` context an earlier search found.

Upgrading an older single-platform database is automatic — the table is rebuilt with the
new key and every existing row is labelled `ats = 'ashby'`, preserving `first_seen` and
`closed_at`.

```bash
uv run python -m job_search.collection.boards --title "software engineer"     # writes job-boards.db
uv run python -m job_search.collection.boards --db ~/jobs.db                  # somewhere else
uv run python -m job_search.collection.boards --no-db                         # CSV/JSON only
uv run python -m job_search.collection.boards --out weekly                    # weekly.csv/.json instead
```

`--out` renames the CSV and JSON outputs; `--concurrency` (default 8) caps parallel
requests and is the one knob you should leave alone — see
[Being a good citizen](#being-a-good-citizen).

The run summary reports `N new, M already seen`, so a scheduled scrape tells you what
changed without diffing anything.

```sql
-- postings that showed up in the last day
SELECT ats, company, title, jobUrl FROM jobs
WHERE first_seen > datetime('now', '-1 day');

-- how the platforms compare
SELECT ats, COUNT(*) jobs, COUNT(DISTINCT company) companies FROM jobs GROUP BY ats;

-- postings that have since disappeared (see the section below)
SELECT company, title, first_seen, closed_at FROM jobs
WHERE closed_at IS NOT NULL ORDER BY closed_at DESC;

-- who is hiring hardest
SELECT company, COUNT(*) n FROM jobs GROUP BY company ORDER BY n DESC LIMIT 10;

-- roles whose description mentioned your --grep term, with the context
SELECT company, title, matched FROM jobs WHERE matched != '';

-- training text linked to a preference label by the composite job key
SELECT j.ats, j.id, j.title, j.description, p.interest
FROM jobs j JOIN job_preferences p ON p.ats=j.ats AND p.job_id=j.id;
```

### Detecting when a posting disappears

`last_seen` alone can't tell you a job is gone, because it only advances when a run's
filters happen to match. On a `--title` run, "filled last week" and "didn't match this
time" look identical.

So closing a posting is reserved for **unfiltered `--all` runs**, which are the only ones
that saw everything. After such a run, any posting on a scanned board that wasn't seen
gets a `closed_at` stamp; anything reposted has it cleared. Filtered runs never touch it,
and the closing is scoped to boards actually scanned, so `--limit` can't close jobs at
companies it skipped.

```
54581 jobs -> ... (54581 new, 0 already seen, 0 closed)
54578 jobs -> ... (0 new, 54578 already seen, 3 closed)
```

Schedule `--all` (daily or weekly) and the database becomes a real fill-rate signal:

```sql
-- how long postings stay open
SELECT AVG(julianday(closed_at) - julianday(first_seen)) AS avg_days_open
FROM jobs WHERE closed_at IS NOT NULL;

-- currently open roles only
SELECT company, title, jobUrl FROM jobs WHERE closed_at IS NULL;

-- companies filling roles fastest
SELECT company, COUNT(*) filled,
       ROUND(AVG(julianday(closed_at) - julianday(first_seen))) avg_days
FROM jobs WHERE closed_at IS NOT NULL
GROUP BY company HAVING filled >= 5 ORDER BY avg_days LIMIT 20;
```

Until you've run `--all` at least twice, `closed_at` is null everywhere — one sweep
establishes the baseline, the next detects what left.

## How it works

Every supported ATS publishes a **per-company** API keyed by a board slug, with no global
search endpoint. So this is two phases, run per platform.

| platform | posting API | archive domain(s) |
|---|---|---|
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | `jobs.ashbyhq.com` |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs` | `boards.greenhouse.io`, `job-boards.greenhouse.io` |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | `jobs.lever.co` |

**Phase 1 — discover slugs.** Query the **Wayback Machine's** CDX index for everything
archived under each platform's domains, take the first path segment of each URL as a
candidate slug, drop the ones that can't be slugs, then validate the rest against that
platform's posting API. Cached to `boards.json` and skipped on later runs unless
`--refresh-boards`.

Measured funnels:

```
ashby       191,117 archived URLs  ->  7,463 candidates  ->  3,617 live boards
greenhouse  1,348,314              -> 14,430             ->  6,797 live boards
lever       1,302,426              ->  8,681             ->  2,718 live boards
```

Three details make that work:

- **The Wayback Machine, not Common Crawl.** Common Crawl was the obvious index and it is
  the wrong default: narrower coverage (~3,400 estimated) and it sheds requests under load
  for hours at a time. The Internet Archive returned 191k URLs in 34 seconds. Common Crawl
  is still there as an automatic fallback.
- **A shape filter before validating.** Archived "path segments" include tracking blobs,
  compensation strings like `$10.2K`, and JS fragments. On Ashby that cuts 7,463
  candidates to 5,000 probes; sampling 150 of the rejects found zero real boards.
- **HEAD, not GET.** A live board returns 200 with a zero-length body under HEAD, so
  validating 5,000 candidates costs nothing. GET would have downloaded ~220KB per live
  board — most of a gigabyte purely to learn which slugs are real.

**Phase 2 — fetch + filter.** Thread pool of 8 over every `(platform, slug)` pair. Each
platform's response is run through a normaliser that maps it onto one row shape, so
filters, CSV and SQLite stay platform-agnostic. Because Phase 1 already validated, a
healthy run sees zero 404s; any that do appear get pruned from `boards.json`.

### Normalisation, and two traps

| row field | Ashby | Greenhouse | Lever |
|---|---|---|---|
| `title` | `title` | `title` | **`text`** |
| `location` | `location` | `location.name` | `categories.location` |
| `employmentType` | `employmentType` | — | `categories.commitment` |
| `isRemote` | `isRemote` | inferred from location | `workplaceType == "remote"` |
| `publishedAt` | `publishedAt` ISO | `first_published` ISO | **`createdAt` epoch-ms** |
| `jobUrl` | `jobUrl` | `absolute_url` | `hostedUrl` |
| `description` | always present | requested with `content=true`, 26x bytes | always present |

The two bolded cells are the ones that fail quietly. Reading `title` on Lever yields an
empty column rather than an error, and treating its `createdAt` as ISO makes every Lever
posting sort wrongly against the other two. Both are pinned by tests.

Lever descriptions are assembled from several fields. `descriptionPlain` is already the
combined opening and body and is used once; when it is empty, the HTML `description`
field is the fallback. Named `lists` sections (requirements, benefits, and similar) and
the `additionalPlain`/`additional` closing section are appended separately. This matters
because real Lever payloads sometimes leave every plain opening/body field empty while
carrying the complete posting in HTML and structured lists.

Greenhouse exposes no remote flag on this endpoint, so `isRemote` is inferred from the
location label containing "remote" — weaker than the other two, and worth knowing before
you trust `--remote` there.

## Facts verified against live endpoints (2026-07-27)

| Fact | Note |
|---|---|
| All three posting APIs | 200, no auth, no account |
| Invalid slug, all three | 404 — this is the validator |
| `HEAD`, all three | 200 with a 0-byte body — free validation |
| Ashby slugs may contain spaces | `A1%20Garage%20Door%20Service` → 200 |
| gzip, Ashby | 1.73MB → 220KB, **8x** |
| gzip, Greenhouse | 317KB → 25KB, **12x** |
| Greenhouse `?content=true` | 25KB → 653KB, **26x** — opt-in upstream, enabled by every job scan here |
| Lever payload | a bare JSON array, not `{"jobs": [...]}` |
| Wayback CDX | 191k (ashby) + 1.35M (greenhouse) + 1.30M (lever) URLs |
| Posting data | live, uncached — 946 jobs published the same day |
| Archive lag for a new board | median 48 days (p90 257) — distinct from posting age |
| urlscan.io | newest scan 1 day old; 30 searches/min anonymous |
| Median posting age | 62 days across all three; Lever alone is 97 |
| Common Crawl CDX | 502/504 on essentially every request; see below |

## Why Common Crawl is only the fallback

Common Crawl is the usual answer to "give me every URL under a domain", and it was this
project's first implementation. It loses on both axes that matter.

**Reliability.** Over an afternoon it returned 502/504 on essentially every request, and
the failure is server-side, not anything you can fix:

- `url=example.com`, a trivially cheap exact lookup, 504s identically to an expensive
  wildcard — so it isn't query cost.
- Indexes that don't exist (`CC-MAIN-2026-18`) 504 the same way — so it isn't a bad query.
- `collinfo.json`, on the same host, returns 200 in 45ms — the static file server is fine;
  the index backend is what times out.

Their docs explain it: the index server handles several million requests/day and sheds
requests on **queue overflow**. Everyone hits this at the same odds.

**Coverage.** For Ashby its estimate was ~3,400 candidate slugs; the Wayback Machine
yielded 3,617 *validated live* boards from a far larger URL set, and 13,132 across all
three platforms.

If you do fall through to it, the status codes differ in meaning. **502/504 = server
overloaded**, retry later, nothing you did. **503 = you are going too fast**; per their
docs a repeatedly-abusive IP can be blocked for 24 hours, so the script raises a distinct
error with that guidance rather than burning retries. CDX requests are throttled to
1/second, and `showNumPages` is avoided because it is the most expensive query they offer.

`job_search/collection/boards.seed.json` (60 verified boards across the three platforms) is bundled regardless,
so **Phase 1 is always optional** and a fresh clone works without either index.

## For coding agents (LLMs)

If you have been pointed at this repository by a human, read this section first, then
[`openwiki/quickstart.md`](../openwiki/quickstart.md).

**Orientation.** `README.md` is the user-facing narrative. `openwiki/` is the
engineer-facing map — [architecture](../openwiki/architecture/overview.md),
[board discovery](../openwiki/workflows/board-discovery.md),
[job scrape](../openwiki/workflows/job-scrape.md),
[data model](../openwiki/architecture/data-model.md),
[runbook](../openwiki/operations/runbook.md), [testing](../openwiki/testing.md).
The whole tool is one file, `job_search/collection/boards.py`, zero dependencies. Per-platform differences
live in the `SOURCES` table and the three `normalize_*` functions; everything else is
platform-agnostic. Adding an ATS should mean one `SOURCES` entry and one normaliser, not
changes scattered through the pipeline.

**Before you run anything network-facing:**

```bash
export JOB_SCRAPER_CONTACT="the-user@example.com"   # ask; do not invent an address
uv run python -m tests.test_job_boards                             # offline, no network, ~1s
python3 -m tests.test_job_boards                            # same suite without uv (>= 3.9)
```

If `uv` is not on your `PATH`, use the second command — it runs the identical suite on
any Python 3.9+, including macOS's system `python3`. Never report the tests as
unrunnable without trying it.

The test suite is the fast feedback loop — it covers every filter, all three normalisers,
the SQLite lifecycle and the archive-parsing paths without touching the network. Run it
before and after any change. A full `--refresh-boards --all` spans 13,146 boards across
three platforms and takes tens of minutes; do not run it casually, and never in a loop. `--ats <one> --limit 10` is
the cheap way to exercise a real request path.

**Things that look like bugs but are load-bearing.** Each is pinned by a test; if you
"fix" one, a test will fail and it is telling you the truth:

| Looks wrong | Why it is correct |
|---|---|
| `--since`/`--new-only` never set `closed_at` | A filtered run did not see what it skipped, so it cannot call those postings gone |
| Lever reads `text`, not `title` | That is Lever's field name. Reading `title` gives an empty column, not an error |
| Lever's `publishedAt` is converted from a number | `createdAt` is epoch **milliseconds**; left raw it sorts wrongly against the other platforms |
| The primary key is `(ats, id)`, not `id` | Greenhouse ids are integers, Ashby/Lever UUIDs — a bare key risks silent cross-platform overwrites |
| Greenhouse job scans always add `?content=true` | It is ~26x the bytes, but keeps every platform's rows complete for storage and preference modeling |
| Fuzzy match requires a ≥2-word title in the reverse direction | Without it, `--title "senior software engineer"` matches every job titled `Engineer` |
| Only `--all` runs set `closed_at` | A filtered run cannot distinguish "gone" from "did not match my filter" |
| Closing is scoped to boards actually scanned | Otherwise `--limit 10` would "close" postings at thousands of unvisited companies |
| Validation uses `HEAD`, not `GET` | `GET` would download hundreds of KB per board — gigabytes per refresh |
| The User-Agent is stripped to ASCII | HTTP headers are latin-1; one em-dash made *every* request fail |
| `boards.json` is gitignored, `job_search/collection/boards.seed.json` is committed | A full crawl is effectively three vendors' customer lists and must not be published |
| Only `--all --new-only` sends `If-None-Match` | A `304` means "body unchanged", which is only "no new postings" if the fetch that stored the ETag persisted every posting |
| Response headers are read in lowercase | The pooled path returns a plain dict, not urlopen's case-insensitive `Message` — and Lever sends `ETag` where the others send `etag` |
| Lever postings dated 2009 are kept | Upstream data, not a parse error. Palantir's board really does carry a `createdAt` of `2009-12-05` |

**Do not commit:** `boards.json`, `*.csv`, `*.json` outputs, `*.db`. `.gitignore` denies
these by default and allows only `job_search/collection/boards.seed.json`. If you add an output format, add it
to `.gitignore` in the same change.

**Do not hand-edit `openwiki/`.** Those pages are generated. Change the source or the
README and let OpenWiki regenerate them (`openwiki --update`).

**Network etiquette is a requirement, not a style preference.** Concurrency is capped at
8, Common Crawl is throttled to its stated 1 request/second, and every request identifies
itself. Do not raise these to make something finish faster. A `429` or `403` backs off
exponentially and honours `Retry-After` when the server sends one, capped at 30 seconds;
backing off is the polite response to being throttled, so do not shorten it either.

**If you add a platform, add its posting-API host to `_POOLED_HOSTS`.** Connection
pooling is opt-in per host, so a new host silently keeps opening a fresh TLS connection
per request — no error, just the pre-pooling cost back for that platform. Pinned by
`test_every_posting_api_host_is_pooled`. Optimising the parsing is not worth doing at
all: it is 0.2% of a run.

**If you add a filter, update both gates.** Two functions decide what a run is entitled
to conclude, and a filter missing from either produces a normal-looking run that quietly
corrupts data. Both fail silently and permanently, so add your filter to both functions
and both tests in the same change.

| gate | what it decides | what a missing filter does |
|---|---|---|
| `may_close_postings()` | may this run stamp `closed_at`? | `--all --since 7d` closes every posting older than a week — the fill-rate signal is corrupted. Pinned by `test_only_an_unfiltered_run_may_close_postings` |
| `may_use_etags()` | may this run store and trust a `304`? | a `--title` run stores an ETag after saving matching rows only; a later run skips that board on `304`, so its other postings stay invisible even once a query matches them. Pinned by `test_etags_are_only_trusted_on_an_unfiltered_run` |

**If you are adding a search mode,** note that `--grep` patterns without `\b` are a
documented footgun (`rust` matches "t**rust**": 1350 hits vs 72). The script warns about
it. Keep that warning.

## Skipped, and when to add

- **Merging Wayback with Common Crawl** — the union would add slugs one index missed.
  Wayback alone already validates thousands per platform, so this buys little.
- **Token title matching** — fuzzy still misses "Software Development Engineer", where
  the words are present but not contiguous. `--grep` covers most of this need already.
- **Per-board caching** — postings change daily; caching mostly serves staleness.
- **Rate-limit backoff** — no 429s observed on any platform. Add on first sighting.
- **More ATS platforms** — SmartRecruiters and Workable expose similar public APIs and
  would each be one `SOURCES` entry plus a normaliser. Workday is per-tenant and would
  need real work.

## Keeping the wiki in sync

`openwiki/` is generated, and it goes stale fast — last time code moved without it, four
of its claims were wrong within the hour, including its own note about not being able to
run the tests.

The split is deliberate: **CI enforces that you regenerated; a human does the
regenerating.**

| workflow | trigger | what it does |
|---|---|---|
| `openwiki-drift-check.yml` | a PR touching `job_search/collection/boards.py` or the tests | **fails** the PR if `openwiki/` was not updated too |
| `openwiki-update.yml` | manual (`workflow_dispatch`) | full refresh in CI, opens a PR — needs an API key |

So the loop is:

```bash
openwiki --update
git add openwiki AGENTS.md CLAUDE.md
git commit -m "docs: sync OpenWiki"
```

It watches source only. The wiki documents mechanisms rather than prose or data, so
README edits and `job_search/collection/boards.seed.json` additions cannot invalidate it and do not trigger the
check. Put `[skip-wiki]` in the PR title to bypass it for a source change that genuinely
does not affect the wiki — a comment, a rename.

**Why the drift check doesn't just regenerate for you.** Regenerating needs provider
credentials, and OpenWiki can authenticate with a ChatGPT subscription — an OAuth
access/refresh pair scoped to the *whole account*, which expires and rotates. In a public
repository's Actions secrets that would be an account-level credential that also breaks
silently on expiry, so the check needs no credentials at all instead. A scoped
`OPENAI_API_KEY` or `OPENROUTER_API_KEY` is safe to add if you want CI to do the
regeneration — that is what `openwiki-update.yml` uses.

Both workflows keep `actions/checkout` on `head.sha` rather than a branch name, pass
every untrusted value through `env:` instead of interpolating it into a `run:` block, and
pin all actions to commit SHAs. The drift check runs with `contents: read` only.

## Performance

The tool is network-bound: json parsing and normalisation are **0.2%** of a run. So the
only levers are how many round trips it makes and how many connections it opens.

**Connections are pooled per thread.** Every posting-API request used to open a fresh
TLS connection — 13,146 handshakes in a full run. Now each of the 8 workers keeps one
connection per host open:

| | connections (300 boards) | full `--since 1d` run |
|---|---|---|
| before | 300 | 6m 35s |
| after | **23** | **3m 51s** |
| | 13x fewer | **41% faster** |

Measured by alternating A/B over 4 rounds, because wall clock on a network-bound tool
has ~56% run-to-run spread and a single comparison is not evidence. Pooling won every
round.

This is also the polite direction: 13x fewer TLS handshakes is less work for Ashby,
Greenhouse and Lever, not more. **Raising `--concurrency` is not on the table** — 8 is a
requirement, not a tunable, and it is the one knob that would speed things up by pushing
cost onto someone else's servers.

### Conditional requests on `--all --new-only`

All three APIs honour `If-None-Match` and answer `304` when a board has not changed.
`--all --new-only` stores each board's `ETag` and sends it back on the next run, so an
unchanged board transfers **nothing at all**:

```
450/450 boards | 0 404 | 0 err | 450 unchanged | 0 matches
```

Measured over 180 boards on a repeat pass: **17.23 MB → 0 MB, 100% of bytes eliminated.**
Wall clock improves less than that suggests, because a `304` still costs a round trip and
this workload is latency-bound — but it is a large reduction in what the platforms have
to serve.

**Only `--all --new-only` uses them, and that restriction is load-bearing.** A `304` says
the body is unchanged; concluding "no new postings" from that *also* requires that the
fetch which stored the ETag persisted every posting. A `--title` run stores matching rows
only, so trusting its ETag later would skip a board whose non-matching postings were
never recorded — they would stay invisible even once a later query did match them.
Storing and using ETags share one gate, `may_use_etags()`, so an ETag in the database
always came from a full, persisted fetch.

The three APIs disagree on header casing — Ashby and Greenhouse send `etag`, Lever sends
`ETag`. A case-sensitive lookup silently returns nothing, which is what made a first
measurement here report 1 of 3 platforms supporting `304` when all 3 do. Header names are
normalised to lowercase for exactly this reason.

## Being a good citizen

This reads only public, unauthenticated posting APIs — the same data any visitor
sees on a company's job board page. Requests are capped at 8 concurrent, Common Crawl is
throttled to their stated 1/second, and every request identifies itself via
`JOB_SCRAPER_CONTACT`. Please keep it that way if you fork.

## Author

Built by Matt Herzog.

- LinkedIn — [mtmherzog](https://www.linkedin.com/in/mtmherzog)
- X — [@mattherzogx](https://x.com/mattherzogx)
- YouTube — [@mattherzogtv](https://www.youtube.com/@mattherzogtv)

## License

MIT — see [LICENSE](../LICENSE).

## Description formatting for previews

The collector keeps `description` as normalized plain text for search and models,
and stores the original layout in `description_html` for the dashboard preview.
Ashby supplies HTML directly, Greenhouse may entity-encode it, and Lever splits
the body, lists, and closing text across fields. Existing databases gain the new
column automatically; older rows retain their text until their next board refresh.
The ETag representation version changes so an old conditional response cannot
prevent that first formatting update. The preview sanitizes markup on read, retains
structural formatting, and omits scripts, embedded media, and source styles.
