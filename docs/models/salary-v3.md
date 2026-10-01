# Salary bounds v3

Salary v3 is a separate labeling dataset. It does not modify the original 500 v2
labels, predictions, or locked evaluation.

## Value contract

The extractor stores all explicit currencies, but the primary model score considers
only USD annual and hourly values. Base salary, OTE, target cash, and total cash are
treated alike when their published values match.

| `value_kind` | `min_value` | `max_value` | Meaning |
| --- | ---: | ---: | --- |
| `exact` | value | same value | one fixed amount |
| `range` | lower | upper | complete two-sided range |
| `minimum` | value | `null` | starts at/from/at least/value+ |
| `maximum` | `null` | value | up to/maximum/not to exceed |

The model must not invent a missing bound, annualize hourly values, or convert
currencies. Weekly, monthly, daily, one-time, equity, benefit, and commission-only
amounts remain outside this target.

The original strict v3 benchmark retains `value_kind` and exact bound shape so its
results remain reproducible. The selected production-facing local target is now
`v3-simple`: the model does not emit `value_kind`; bound shape is derived from nullable
`min_value` and `max_value`. Its primary score requires at least one matching USD
figure with the same annual/hourly period, tolerates additional candidates on positive
jobs, and still requires no USD prediction on a true USD-negative job.

## Prepared datasets

`targeted-v3-20260831` contains 500 fresh US-focused jobs from 359 companies:

- 200 minimum candidates;
- 150 maximum candidates;
- 50 exact controls;
- 50 ordinary ranges;
- 50 hard negatives.

It contains 300 training, 100 calibration, and 100 untouched evaluation items. The ATS
mix is 168 Ashby, 165 Greenhouse, and 167 Lever, with at most three jobs per company.
Candidate kinds are sampling hints, not labels.

`v2-one-sided-audit` contains 59 original v2 jobs whose postings include one-sided
language. It retains each old gold result as read-only reviewer context. The earlier
rough count of 44 did not include every plus-sign form such as `$200k+`.

## Teacher workflow

The hosted teacher pass and human review are complete: all 500 jobs have gold labels
(300 training, 100 calibration, and 100 evaluation). The commands below remain as a
record of the resumable workflow. Estimate a future batch first:

```bash
python3 -m job_search.salary.v3 estimate --provider fireworks --limit 500
python3 -m job_search.salary.v3 estimate --provider openai --limit 500
```

Run the two providers in separate credentialed terminals if desired. The commands are
resumable and persist each result independently:

```bash
python3 -m job_search.salary.v3 predict --provider fireworks --limit 500 --max-usd 10 --execute
python3 -m job_search.salary.v3 predict --provider openai --limit 500 --max-usd 20 --execute
```

Those budgets satisfy the conservative preflight ceilings; they are not expected
charges. Based on the previous 500-job run, actual combined spend should remain around
$6–$8, but the new, deliberately difficult descriptions can vary. Re-run a command
after any transient failures; complete predictions are skipped. Inspect progress with:

```bash
python3 -m job_search.salary.v3 status
```

Review generated drafts at a new port so it cannot be confused with the v2 reviewer:

```bash
python3 -m job_search.salary.v3_review
# open http://127.0.0.1:8768
```

The existing one-sided audit can be reviewed separately without calling either API:

```bash
python3 -m job_search.salary.v3_review --audit-v2 --port 8769
# open http://127.0.0.1:8769
```

For an audit item, use the old v2 label only as reference and edit the v3 JSON. The
editor starts with `{"ranges": []}` because automatically translating an old exact
label into a one-sided v3 label would contaminate the audit.

## Simplified local-model result

The original NuExtract v3 calibration scored 90% because the model had to classify
`exact`, `range`, `minimum`, and `maximum`, and an otherwise useful result failed when
it included an extra candidate. Removing the redundant field and using the
figure-overlap rule raised the same stored predictions to 97%.

NuExtract3 4B was then rerun with the simplified schema. Calibration was used to freeze
three deterministic, auditable cleanup rules: discard entries with no numeric bound,
exclude postings explicitly stating `commission-only`, and treat an unsuffixed ATS pay
field under 500 as hourly. Results:

| Split | Jobs | Primary accuracy | Figure precision | Figure recall | Figure F1 | Strict output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Calibration | 100 | 100% | 98.20% | 99.09% | 98.64% | 99%* |
| Evaluation | 100 | 98% | 96.23% | 90.27% | 93.15% | 99% |

`*` The calibration JSONL contains one warning generated before the boundless-entry
cleanup was frozen; new inference removes that entry.

The evaluation split was run once after the contract was frozen. Its 98/100 job score
has a 95% Wilson interval of approximately 93.0%–99.4%. Every evaluation job with
labeled USD pay had at least one correct figure; both primary failures were false USD
pay on negative jobs: an annual sales quota and per-class compensation. These are
documented errors, not post-evaluation tuning targets for this benchmark.

Run or rescore the selected target with:

```bash
.venv-local-mlx/bin/python -m job_search.salary.local_eval run \
  --dataset v3-simple --model nuextract3-4b --split calibration --limit 100
.venv-local-mlx/bin/python -m job_search.salary.local_eval score \
  --dataset v3-simple --model nuextract3-4b --split evaluation
```

## Production fallback

The shared contract now lives in `job_search/salary/model_contract.py` and is consumed by both
the evaluator and `job_search/salary/llm.py`. Production does not run the fuzzy generic
description parser. A scan stores native ATS values first and queues a new or changed
description only when it lacks usable annual/hourly primary cash compensation.

The recurring system intentionally has no historical backfill:

```bash
python3 -m job_search.salary.llm activate --no-backfill
python3 -m job_search.salary.llm status
.venv-local-mlx/bin/python -m job_search.salary.llm run --until-empty
```

Activation soft-retires legacy `description_rule` rows, establishes existing jobs as
the baseline, and queues zero work. Native and LLM values share
`job_compensation_ranges`; their `source_type` and source-specific historical keys keep
provenance distinct.
