# Fireworks salary backfill estimate

Snapshot: 2026-08-31. This is a planning estimate; no deployment or paid backfill was
started.

## Measured workload

The locked 400-job NuExtract evaluation averaged 1,734 input tokens and 38.9 generated
tokens per job. Applying that measured mix to the database gives:

| Workload | Jobs | Input tokens | Output tokens | Total tokens |
| --- | ---: | ---: | ---: | ---: |
| Every stored job | 320,533 | 555.8M | 12.5M | 568.3M |
| Exclude 69,025 authoritative ATS jobs | 251,508 | 436.1M | 9.8M | 445.9M |

These are more useful than estimating from description words because they include the
actual local template/tokenizer behavior. Add a contingency for the production
long-description policy and retries.

## Dedicated custom deployment

The local folder is an MLX 4-bit Qwen3.5-family checkpoint. It cannot be assumed to be
a Fireworks deployment artifact. Fireworks' documented upload format is a standard
Hugging Face checkpoint, followed by architecture validation. Its public quantization
workflow documents FP8 on H100—not MLX affine 4-bit. Therefore first upload the original
NuExtract Hugging Face checkpoint and confirm Fireworks reports it `READY`; if its
architecture is rejected, use a supported similarly sized extraction model.

Fireworks charges dedicated deployments per active GPU-second. The public price is
$7/H100-hour through August 31 and $8/H100-hour beginning September 1, so ongoing
planning should use $8. The actual throughput must be measured with a 1,000-job load
test. For all 320,533 jobs, plausible planning points are:

| Sustained throughput per H100 | One-H100 time | Cost at $8/hour | Four-replica wall time* | Four-replica total cost* |
| ---: | ---: | ---: | ---: | ---: |
| 2 jobs/s | 44.5 h | $356 | 11.1 h | $356 |
| 5 jobs/s | 17.8 h | $142 | 4.5 h | $142 |
| 7.5 jobs/s | 11.9 h | $95 | 3.0 h | $95 |
| 10 jobs/s | 8.9 h | $71 | 2.2 h | $71 |

\*Ideal linear scaling before autoscaling, retry, and tail-latency overhead. Four
replicas make the bill accrue four times faster but finish about four times sooner, so
total GPU cost is approximately unchanged. A realistic budget envelope is therefore
roughly **$80–$400 and 2–12 hours with four H100 replicas**, after a pilot shows 2–10
jobs/s per replica. Use 10–20% cost/time contingency.

Skipping authoritative ATS salaries reduces the workload by 21.5%. At 5 jobs/s that is
14.0 one-H100 hours, $112, or about 3.5 hours across four replicas. This is the preferred
backfill because those 69,025 jobs already have stronger source data.

Send multiple concurrent HTTP requests to each replica so continuous batching can
fill the GPU. Do not equate client concurrency with replicas: increasing in-replica
concurrency can improve utilization at no added hourly rate, while adding replicas
increases the hourly rate. Sweep concurrency (for example 8, 16, 32, 64) during the
pilot and retain the best error-free throughput.

## Serverless alternatives

If a suitable 4B–16B model is actually available on Fireworks serverless, the current
size-based rate is $0.20 per million tokens for both input and output. The measured full
workload would cost about **$113.66 standard** or **$56.83 through Batch API** because
batch is half price. This is a pricing comparison, not confirmation that private
NuExtract is serverless- or batch-eligible.

Using the current Qwen3.8 Max teacher is much more expensive. The 500 completed calls
averaged 1,772.5 input and 209 output tokens per job. At $2/M input and $6/M output, the
full database projects to approximately **$1,538 standard** or **$769 batch**. It is a
good teacher/evaluation model, not the economical production backfill model.

## Recommended rollout

1. Finish v3 labels and re-evaluate NuExtract under the one-sided contract.
2. Confirm Fireworks accepts the original Hugging Face architecture and FP8 quality.
3. Run a 1,000-job dedicated pilot on one H100, sweeping client concurrency.
4. Compare accuracy to local MLX and record jobs/s, request failures, and actual GPU
   active time.
5. Backfill only the 251,508 non-authoritative jobs, with resumable writes.
6. Use one replica for recurring volume; autoscale to zero when idle. Use several
   replicas only for the initial backfill deadline.
