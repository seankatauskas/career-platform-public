# Local salary model target

The extractor stores every explicit annual or hourly primary cash-pay value, regardless
of currency. The primary model-selection metric is narrower: it asks whether the model
found the correct USD annual/hourly numbers needed by the US-job filter.

## Stored output

Each job may contain an unordered list such as:

```json
[
  {
    "currency": "USD",
    "period": "year",
    "min_value": 120000,
    "max_value": 160000
  },
  {
    "currency": "CAD",
    "period": "year",
    "min_value": 130000,
    "max_value": 175000
  }
]
```

`period` may be `year` or `hour`. Equal bounds mean an exact amount; unequal bounds
mean a range. Multiple values remain separate when a posting publishes distinct pay
zones. Values are preserved as published: hourly pay is not annualized, and currencies
are neither converted nor discarded from the raw prediction or human gold label.

## Primary USD correctness score

Before primary scoring, both the prediction and gold label are projected to their USD
`(USD, period, min_value, max_value)` tuples. A positive gold label passes when at least
one predicted USD tuple matches and every predicted USD tuple is supported by gold. A
gold label with no USD tuples passes only when the prediction also has no USD tuples.
This means:

- One supported prediction can pass when gold contains several valid USD ranges.
- A correct range plus an unsupported USD guess fails.
- `base_salary`, OTE, and target terminology are equivalent when the numbers match.
- A mixed USD/CAD posting retains both ranges, but only its USD range affects the
  primary pass/fail score.
- A non-USD-only posting retains its ranges. Its USD projection is empty, so it acts as
  a negative example for the primary USD metric.
- Incorrect non-USD extraction can be reported as a secondary all-currency metric, but
  does not change the primary USD score.

Location scope, evidence wording, confidence, and notes are not scored. Explicit bonus,
equity, commission-only, stipend, monthly, weekly, daily, and one-time amounts are
outside the target. Both bounds are required; a single missing bound is not guessed.
Unrelated values such as benefits, 401(k) matches, revenue, and years of experience are
false positives if emitted as pay.

That paragraph is the frozen v2 contract. Salary v3 adds intentional minimum-only and
maximum-only values without changing v2. The v3 canonical scorer includes
`value_kind` and the nullable bounds, so `starts at $175,000` matches only
`(minimum, 175000, null)`, not an exact value. Its prepared batches, commands, and
review workflow are documented in `docs/models/salary-v3.md`.

The selected production-facing target is `v3-simple`. It removes `value_kind` from the
model output because nullable bounds already encode the shape:

- equal bounds represent one fixed figure;
- two different bounds represent a range;
- only `min_value` represents a lower bound;
- only `max_value` represents an upper bound.

For primary selection, a positive job passes when at least one predicted USD numeric
figure matches any gold USD figure with the same period. Extra candidates are tolerated
on positive jobs because a useful salary is preferable to rejecting the whole result.
A gold label with no USD pay still requires no predicted USD figure. All original v3
labels are retained unchanged and transformed only during local-model scoring.

## Evaluation

Report:

- job-level primary usability accuracy using supported USD overlap;
- USD value-level precision, recall, and F1;
- USD annual versus hourly results separately;
- false-positive rate on jobs with no target USD pay;
- optionally, a secondary all-currency exact-set score for observability.

Do not tune a model on the locked evaluation split. The 100-job calibration split may
be used to settle policy and prompts; the 400-job evaluation split stays untouched until
the candidate and scoring contract are frozen.

The Apple-Silicon evaluation runner uses the isolated `.venv-local-mlx` environment.
Its direct dependencies are recorded in `requirements/local-mlx.txt`. NuExtract raw
responses remain in the JSONL output. The runner records any narrow model adapter under
`normalizations`; currently this only converts an unambiguous singleton period enum such
as `["year"]` to the canonical scalar `"year"`.

The executable contracts are `canonical_all_pay()` for retained values and
`canonical_usd_pay()` / `usd_primary_match()` for primary scoring in
`job_search/salary/training_target.py`. Inspect the gold-set composition with:

```bash
python3 -m job_search.salary.training_target --db job-boards.db
```

Production and evaluation share the v3-simple prompt, template, normalization,
validation, and chunking contract from `job_search/salary/model_contract.py`. Do not edit a copy
in the worker: version the shared prompt and rerun calibration before changing
production behavior.

## Calibration benchmark: 2026-08-31

These results use only the 100-job calibration split. The 400-job evaluation split
remains locked.

| Model | Quantization | Primary accuracy | USD precision | USD recall | USD F1 | Mean/job | Peak memory |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NuExtract3 4B | MLX 4-bit | 99% | 96.55% | 96.55% | 96.55% | 3.23 s | 5.96 GB |
| Qwen3.8 27B | MLX 4-bit | 98% | 98.25% | 96.55% | 97.39% | 20.86 s | 20.95 GB |

Both runs completed 100 jobs without runtime errors, validation failures, or truncated
generations. NuExtract had the better primary score and was about 6.5 times faster;
Qwen3.8 had slightly better value precision and F1.

Parallel MLX processes fit in 64 GB unified memory but reduced throughput because they
contended for the same GPU and memory bandwidth. One Qwen plus one NuExtract took 30
seconds in parallel versus about 27.7 seconds sequentially. Two Qwen3.8 workers took 52
seconds for two jobs versus about 48.4 seconds sequentially. Therefore the measured
throughput-optimal MLX concurrency is one; two Qwen3.8 workers are memory-safe but slower.

## Locked evaluation: 2026-08-31

After selecting NuExtract3 from calibration, the frozen model, prompt, adapter, and
scorer were run once on all 400 evaluation jobs:

- primary job accuracy: 97.50% (390/400; 95% Wilson interval 95.46%–98.64%);
- USD value precision: 96.10%;
- USD value recall: 92.49%;
- USD value F1: 94.26%;
- annual values: 132 TP, 5 FP, 7 FN;
- hourly values: 65 TP, 3 FP, 9 FN;
- strict output success: 95.50%, with one recorded malformed non-target weekly output
  and 17 validator-flagged records;
- no truncated generations;
- 19 minutes 4 seconds total, 2.86 seconds/job mean, and 6.17 GB peak memory.

The ten primary failures include missed pay, weekly pay labeled hourly, a sign-on bonus
treated as salary, an hourly/year period error, and unrelated amounts combined into a
range. The gold set also has a one-sided-pay inconsistency: positions 329 and 339 treat
`$175k + ...` and `Starting at $20/hour` as exact amounts, while position 471 labels
`starts at $175,000` as no qualifying pay. The locked score above is unchanged. Settle
that policy and version/correct the affected gold labels before using this corpus for
fine-tuning; do not rerun or tune against this evaluation split in place.

## Simplified v3 benchmark: 2026-08-31

NuExtract3 4B MLX 4-bit achieved 100/100 on calibration and 98/100 on the untouched
evaluation split under `v3-simple`. Evaluation figure precision was 96.23%, recall was
90.27%, and F1 was 93.15%. It completed 100 evaluation jobs in 302.93 seconds (3.03
seconds/job) with no runtime errors and one validator warning. See `docs/models/salary-v3.md` for
the frozen rules and failure analysis.
