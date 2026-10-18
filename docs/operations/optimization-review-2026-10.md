# October 2026 optimization review

The October 17 investigation compared the deployed source with bounded AWS
inventory, billing, metrics, and aggregate operational records. Private evidence
is kept outside Git. Release 77 was confirmed installed and idle through the
release-status workflow at 16:02 UTC; all 11 CloudWatch alarms were OK at the
follow-up read. These observations are time-bounded, not ongoing health claims.

## Work ownership and release order

One release owner coordinates three isolated implementation branches: interactive
worker isolation, snapshot pause reduction, and incremental ranking. The owner
integrates and reviews their commits, checks the combined source, merges through
a PR, prepares one release against the observed predecessor, and deploys the exact
prepared receipt. Feature agents do not mutate production. Follow
[coordinated releases](coordinated-releases.md) for status and recovery.

The budget correction has its own bounded Terraform scope, `monthly-budget`.
It preserves the existing limit and notifications while excluding credits and
refunds. Review the plan before applying it through the infrastructure workflow.
The saved-plan guard rejects changes to the host or any other managed resource.

## Evidence and acceptance measurements

| Area | Observed baseline | Measurement after release |
| --- | --- | --- |
| Interactive work | Mail understanding p95 runtime 13 seconds; first-claim delay p95 2,041 seconds. Several long waits overlapped ranking. | Compare first-claim latency during overlapping scheduled collection/ranking, separating maintenance and retries. Proposed target: p95 under 60 seconds. |
| Backups | Fourteen maintenance windows had median 13.34 minutes. | Compare paused capture time and post-resume validation separately; require a verified receipt and successful restore checks. |
| Deployment | Release 76 snapshot took 898.647 seconds during maintenance; SQLite checks accounted for 685.823 seconds. | Record per-file reuse/recapture, paused checks, full maintenance duration, and background-worker drain duration. |
| Ranking | Latest substantive audit sample checked 361,165 families in 35 minutes 46 seconds. | Compare incremental output with a full pass; report changed blocks/families and phase timings. Do not confuse checked families with rescored families. |
| Cost signal | October 1–16 usage charges were $45.7321, fully offset by credits; budget actual spend was zero. | Verify the live budget excludes credits/refunds. Billing updates can lag the configuration change. |

Use normal scheduled workloads for production measurements. Do not run an
unbounded collector or trigger model jobs solely to manufacture a benchmark.
Fixture improvements are not evidence of the same production speedup.

## Capacity decision

Retain the current `t3.large` while gathering a representative week after the
October 15 collector memory fix and this concurrency change. The earlier
14-day memory maximum of 95.52% predates the fix; October 15 and 16 maxima were
41.84% and 30.17%. Two days do not cover concurrent interactive workers, ranking,
collection, and maintenance. The 14-day CPU-credit minimum was 568.26 with no
surplus-credit charges. Current evidence supports neither an urgent CPU upgrade
nor immediate downsizing.

For the next sizing review, retain daily peak memory, minimum available memory,
CPU-credit minimum, process RSS, queue latency, backup duration, and disk queue
metrics. Include at least one complete collection/ranking overlap and backup.
Compare a smaller x86 host on an isolated restored copy before scheduling a host
replacement. Check memory headroom under the new simultaneous worker load and
the applicable instance CPU-credit baseline. A smaller memory footprint alone
does not establish equivalent CPU capacity.

Do not increase EBS IOPS based on snapshot duration alone. The audit saw elevated
reads and queueing but no IOPS/throughput-exceeded datapoints. Phase timings and
an isolated storage benchmark must establish the bottleneck first. Compute
Optimizer and Cost Optimization Hub were not enrolled during the audit.

## Preserved storage decision

Retain the unattached encrypted 100 GiB volume. Its matching preserved-data state
confirms intentional retention; it is not an orphan established safe to delete.
Observed gp3 billing implies approximately $8/month before credits. No contents
were mounted or inspected during the audit. Before disposition, establish the
data owner, retained recovery purpose, newest independently verified backup,
and successful restore evidence. Follow
[the preservation workflow](../../infra/aws/preserved-data/README.md).

Keep the root and live data volumes unchanged. The audit's latest disk samples
were 47.57% and 57.67%, respectively; past root pressure is not current fullness.
