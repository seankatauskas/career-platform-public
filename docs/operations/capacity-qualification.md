# Qualifying a smaller EC2 host

The current host remains a `t3.large` (two vCPUs, 8 GiB). A `t3.medium`
provides two vCPUs and 4 GiB. Downsizing needs evidence from the complete system,
including the guest OS, Docker, Hermes and other sidecars. The
[constrained fixture](medium-host-benchmark.md) is useful regression evidence but
does not qualify the production host by itself.

## October 19, 2026 native rehearsal

The [native t3.medium rehearsal](native-medium-rehearsal-20261019.md) passed its
predeclared targets with all nine production services, an isolated Telegram stub,
three historical input windows, full saved-row persistence, backup overlap and a
host reboot. Minimum available memory during the extra 1 GiB reserve interval was
919 MiB. Backup downtime was 251 seconds and total duration 41.5 minutes.

Large input preparation took 19 seconds during overlap and 44 seconds immediately
after a cold reboot with nearly depleted credits. This is a meaningful capacity
result, with measured latency tradeoffs. Production remains a Standard `t3.large`.
The representative-week gate below is still outstanding; the native rehearsal
covers the four-GiB workload and recovery checks, not a week-long soak or live
provider/credential cutover. See the report for exclusions, exact provenance,
acceptance checks and cleanup evidence.

## October 18, 2026 observation

Read-only EC2 and CloudWatch queries completed at 03:21 UTC. EC2 reported
**Standard** CPU-credit mode. Keep that setting in the capacity model: exhausting
credits would constrain sustained CPU rather than automatically buying surplus
credits. No instance size or credit setting was changed.

| Complete metric periods | Mean CPU | Highest reported memory use | Minimum credit balance |
| --- | ---: | ---: | ---: |
| Previous week, 167 hourly periods | 24.19% | 95.52% | 766.15 |
| After ranking was paused, 23:15–03:20 UTC, 49 five-minute periods | 12.55% | 19.87% | 847.94 |

Ranking was paused at 23:10:11 UTC on October 17. The weekly window includes
earlier collector memory behavior and multiple release/maintenance activities.
The shorter window is encouraging, but neither isolates the effect of pausing
ranking nor includes a demonstrated representative large-list workload. The latest
release had only two complete five-minute periods at observation time, too little
to establish its capacity impact.

CPU means weight CloudWatch averages by sample count. Partial boundary buckets
were excluded; the week used hourly buckets and the shorter window used five-minute
buckets. Maxima are maxima of reported observations, not continuous measurement or
percentiles. Memory is the configured `CareerPlatform/mem_used_percent` metric;
it is not a sum of process RSS or a complete model of reclaimable page cache.
Raw queries and calculated summaries are retained privately under
`.cache/capacity-evidence.json` and `.cache/capacity-summary.json` in the investigation
worktree; these files are not repository artifacts.

The shorter window consumed 61.50 credits over approximately 4.08 hours, or
15.06 credits/hour. The week consumed 4,847.49 over 167 hours, or 29.03/hour.
A medium earns 24/hour (20% baseline across two vCPUs), compared with the large's
36/hour. Thus the historical weekly workload would consume a medium's reserve over
time, while this short recent sample would replenish it. A large's current credit
balance does not establish the medium's sustainability. See AWS's
[burstable instance credit table](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html).

## Evidence required before resizing

1. Collect a representative week with ranking paused and the current optimizations
   installed. Record collection, list-input preparation, backups, deployments and
   interactive activity alongside CPU, credit usage/balance, available memory,
   swap, OOM events and container restarts. Include failures and maintenance gaps.
2. Model the medium's credit balance from credit usage and its 24-credit/hour earn
   rate, respecting its maximum balance. Check long busy intervals as well as the
   weekly mean. A positive observed balance on the large is not this simulation.
3. Rehearse on isolated native x86 capacity using a restored catalog and the full
   service set within a four-GiB system budget. Prevent external mailbox delivery,
   model calls and duplicate collection. Replay historical list-input queries from
   at least two windows; do not rerun Codex recommendation generation. Exercise
   collection persistence and backup overlap with interactive requests, then test
   startup and recovery. A four-GiB container on a larger machine is insufficient
   if the OS and excluded services use memory outside its limit.
4. Agree on measured acceptance targets before the rehearsal: no OOM/restarts,
   stable memory headroom including growth, sustainable credit use, and acceptable
   interactive/list-input latency during overlap. Retain baseline comparisons and
   exact source/image, architecture, workload and timing receipts. Passing database
   integrity checks alone does not establish acceptable performance.
5. Only then schedule a Terraform-managed resize with a tested return path to the
   previous instance type, preserved data and normal release/recovery checks.

The October 18 production metrics and earlier synthetic ARM fixture alone did not
satisfy these gates. The October 19 native rehearsal supplies the later workload
evidence described above. The representative-week observation remains required;
no production resize has been performed.

## October 19 migration decision

The owner approved a controlled production resize after the native rehearsal and
a fresh 72-hour history review, including 41.4 hours on the optimized release.
Mean CPU was 9.64%, credit consumption 11.61/hour, and peak sampled host
total-minus-available memory was 2.29 GiB. Eleven collections and four backups
succeeded. Two discovery failures were upstream HTTP 504s.
This explicitly shortens the representative-week pre-resize observation; it does
not claim that gate was completed. Keep ranking disabled and Standard credits,
observe the first collection, generation and backup after resizing, and monitor
for 24-48 hours with a return path to large. See the infrastructure README's
instance-size workflow. A new generation was still active during the assessment.

## Production migration - October 19, 2026

Production instance `i-0fa677e47dee0134c` was resized in place to `t3.medium`
in Standard credit mode through the guarded `instance-size` workflow.
Plan run `39337432832` and apply run `39337785594` succeeded from commit
`c122bfb155c90a29738cbde724dcff3109fc71cd`. Terraform changed one resource,
created none and destroyed none. Both original root/data volume IDs were verified
unchanged. Application release `c09fe394cfa5250b4cfd3443019fef036cc97a76-86`
remained installed. The earlier statements about production being large describe
the assessment/rehearsal, not the post-migration state.

Active recommendation work published before maintenance. Workers drained, then
all services were paused at 22:50:57 UTC and active again at 22:53:39 UTC.
The fully paused interval was 162.6 seconds; drain began 12.1 seconds earlier.
The data-volume marker, metadata firewall and preflight passed before activation.
All nine services passed startup health checks. The verified S3 backup
`20261019T200051-94edf01b` remains available.

Continue the 24-48 hour observation and check the first post-resize collection,
generation and backup. The short acceptance check does not complete this soak.
For rollback, set production `AWS_TERRAFORM_VARS_JSON.instance_type` to `t3.large`,
run `scope=instance-size, operation=plan`, drain/pause, then run the same scope
with `operation=apply` and verify/activate. Restore no application data merely
to change instance size. Pending cloud-init drift still requires a separate
replacement decision; do not substitute an unreviewed full Terraform apply.

Private migration evidence: `s3://career-platform-622946928150-us-east-2-backups/`
`backups/validation/medium-migration-20261019/`.
