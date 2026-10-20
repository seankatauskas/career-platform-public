# Native t3.medium capacity rehearsal - October 19, 2026

## Decision

**The isolated native four-GiB rehearsal passed all predeclared acceptance targets.**
A `t3.medium` fits the measured workload with ranking disabled and the current
bounded collector. Memory retained a useful margin through the heavy overlap.
The tradeoffs are longer disk work and slower startup/input preparation when CPU
credits are nearly depleted.

This supports the medium as the next production sizing candidate. The
[capacity qualification guide](capacity-qualification.md)'s representative-week
observation remains outstanding; this short rehearsal does not complete that gate.
Production was not resized. Finish that observation, then use the Terraform and
release/recovery runbook for a controlled resize with a return path to `t3.large`.
Retain Standard credit mode and verify the actual credit balance after the size
change before overlapping heavy maintenance with interactive work.

## Workload results

| Measure | Baseline | Full extra-reserve interval | Entire collection/backup overlap |
| --- | ---: | ---: | ---: |
| Minimum MemAvailable | 2.080 GiB | 0.898 GiB (919 MiB) | 0.898 GiB |
| Mean CPU busy from guest counters | 19.71% | 32.33% | 21.10% |
| Health endpoint p95 | 78 ms | 142 ms | 107 ms |
| Health endpoint maximum | 94 ms | 337 ms | 337 ms |
| Successful requests across three endpoints | 768 / 768 | 474 / 474 | 1,620 / 1,620 |
| OOM kills / swap used / unexpected restarts | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |

All ten containers were healthy in the corresponding sampled observations: 17 baseline samples, 10 during the full extra-reserve interval and 36 across the entire overlap. The full extra-reserve interval lasted about 331 seconds; the whole overlap lasted 1,127 seconds. These intervals overlap and their requests must not be added as independent coverage.

The historical input checks all passed exact saved-payload, ordering and fingerprint comparisons. Actual `Store._snapshot` timings against the restored current catalog were:

| Historical UTC window | Original saved inputs | Current eligible inputs | Baseline preparation | Overlap preparation |
| --- | ---: | ---: | ---: | ---: |
| Oct 17 15:26:07.095446 to 22:34:59.021089 | 122 | 118 | 0.101 s | 0.430 s |
| Oct 17 04:06:12.794846 to 15:26:07.095446 | 473 | 466 | 0.307 s | 0.670 s |
| Oct 13 01:27:16.683894 to 23:31:03.211553 | 6,237 | 5,736 | 4.938 s | 19.010 s |

A separate large-window pass after baseline persistence took 8.942 seconds. This shows cache sensitivity; 4.938 seconds is not a guaranteed cold-read time. The performance tradeoff is concentrated in large input preparation and disk work during overlap. Historical frozen replay proves correctness for the original inputs; the timed current-catalog query has fewer eligible rows and changed fields.

| Saved collection fixture | Baseline | With backup and memory reserve |
| --- | ---: | ---: |
| Active postings / batches | 338,122 / 170 | 338,122 / 170 |
| Application persistence wall time | 168.363 s | 306.253 s |
| Saved-fixture loading wall time | 329.431 s | 810.057 s |
| Complete fixture wall time | 502.130 s | 1,122.553 s |
| Complete fixture CPU time | 110.947 s | 112.391 s |
| Process peak RSS | 214.4 MiB | 983.0 MiB, including 768 MiB reserve |

The complete fixture includes extra disk reads that a live collector receiving API responses does not perform. Its total duration must not be presented as a measured live collection duration. The persistence function itself took 1.82 times its baseline wall time under this overlap. No new-posting, enrichment or board-closure performance claim follows from this existing-row test.

## Complementary production evidence

The preceding production assessment covered 20.42 hours after release: mean CPU 9.45%, CPU credits consumed 11.45/hour, and a conservative sampled peak of 2.15 GiB total-minus-available memory. A medium earns 24 credits/hour. The requested real generation used 729 frozen inputs, prepared them in 0.861 seconds, and completed its worker in 337.8 seconds. That run had no heavy collection/backup overlap.

Those observations support the CPU budget, while this native rehearsal covers the reduced memory and worst overlap more directly. Neither is a representative week. Credit balance and EBS burst balance should continue to be monitored after any approved resize; do not assume a large's accrued credit balance transfers unchanged across instance types. See the [AWS credit documentation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html).

The October 19 Price List API observation priced Ohio Linux shared On-Demand compute at $60.74/month for a large and $30.37/month for a medium, using 730 hours. The potential saving is $30.37/month in EC2 compute; storage, backups, networking and observability remain separate.

## Backup and cold-start tradeoffs

| Backup measure | Recent production large | Rehearsal medium |
| --- | ---: | ---: |
| Planned stopped-service window | 217.044 s | 250.584 s |
| Complete backup | 1,867.322 s (31m 7s) | 2490.698 s (41m 31s) |

The medium remained below the declared 350-second pause and 3,600-second total
limits. It captured in 192.943 seconds, spent 1,553.698 seconds on SQLite checks,
and recorded 463.005 seconds in compression. These phase counters can nest and
must not simply be added. All 1,262 captured files, totaling 12,998,119,198 bytes,
passed the shipped backup pipeline. The resulting local archive was about 5.55 GB.
It was not uploaded as a production backup.

This is not a strictly paired comparison: the clone had an extra Telegram stub,
its copied catalog had undergone the saved-row workload, and cache/credit conditions
differed. The local archive copy also replaces the production S3-upload tail.
Longer backup completion can extend the maintenance-lock wait for a deployment;
a concurrent deployment was not benchmarked.

Across the full post-resume backup-validation period, all 3,111 HTTP requests
succeeded. Health p95 was 409 ms and maximum 1.442 seconds. All ten containers were
healthy in 69 sampled observations. In the later backup-only interval, health p95
rose to 642 ms as credits approached zero; no request failed.

Reported CPU-credit balance fell from roughly 4.2 credits to 0.037, with no surplus
credit balance during qualification. EBS I/O balance stayed at or above 93%, and
byte balance at or above 95% during the measured workload. The preceding production
CPU budget and this new host's small initial reserve are different questions:
a low long-term average does not guarantee immediate cold-start burst capacity.
Monitoring and read-only diagnostic work are included in these measurements.

The host reboot changed its boot ID and all ten services were verified healthy
within 166.7 seconds of the recorded restart preparation.
This includes orchestration/polling time and is an upper bound, not a precise
externally measured outage. During startup, the local monitor recorded four
expected failed requests across the three endpoints; the monitor cannot observe
the whole host-down interval. Several five-second container health checks also
timed out transiently and subsequently cleared.

Post-reboot schema checks, host/bridge egress-denial checks and the large historical
input replay passed. Preparing its 5,736 current jobs took **44.162 seconds** with a
cold cache and near-zero credits. During this replay, all 141 HTTP requests
succeeded, health p95 was 1.433 seconds, and no sampled container became unhealthy.
Guest CPU steal was elevated during startup/replay, consistent with credit
throttling. The subsequent 120-second idle check had all 174 requests succeed,
health p95 65 ms, all health-check failure streaks cleared, and no OOM or restart.

The retained manifest identifies a possible future backup investigation:
`toolchain/tectonic.bundle` plus `state/job-boards.json.gz` and `state/job-boards.csv.gz`
account for 2.72 GB of captured bytes. Review whether the exports remain necessary
and whether the pinned toolchain belongs in a separately recoverable artifact.
Their retention and recovery behavior were not changed here.

## Scope and provenance

An isolated x86 t3.medium ran the full nine-service production composition plus a local Telegram stub. Linux reported 4,018,425,856 bytes of RAM; no swap was configured. Production remained t3.large in Standard mode. This is a capacity and data-recovery rehearsal, not an external-provider or encrypted-credential cutover.

- Release: `c09fe394cfa5250b4cfd3443019fef036cc97a76-86`.
- Backup: `20261018T200051-f8edbe19`, created before the latest live generation.
- Archive: 5,033,501,063 bytes; SHA256 `8a496255023f0c357ff2e19a3436ea16e1dbb6b347c30ec52a0f7b14dffd6979`.
- Restored: 1,259 files, 12,996,515,847 bytes; jobs catalog 7,647,858,688 bytes.
- Exact application image: `sha256:ce094f38889abe8b1208dbc426ff94662b15d18f45b34e54d0b04c0a85dbc5c8`.
- Exact Hermes image: `sha256:76804ea0d26cb2a522a2e7964cb3a584c3dfbf4d63bd9b7cfa5c82bca530b95b`.
- Disposable encrypted disks: 30 GiB root, 100 GiB gp3 data, with 3,000 IOPS and 125 MiB/s matching production. They are not production disks.

Application egress was blocked for host-networked processes and through an internal bridge. Runtime connections from both network modes were tested. Secrets inventory was empty, credentials were synthetic, copied schedules were disabled and the worker maintenance gate remained draining. The real Telegram adapter used a local stub. Backup output stayed local. Ranking stayed disabled. No collection API, model, notification delivery, recommendation publication or production activation ran. The result assumes the current bounded collector and paused-ranking configuration; it does not qualify re-enabling ranking.

The new Standard instance began without accrued CPU credits. Unlimited was used only for cold setup, from 01:31:35 to 01:44:30 UTC, and Standard was restored before workload qualification. Those setup measurements are excluded from workload results. No surplus balance remained on returning to Standard. Production credit settings were unchanged.

## Method and limitations

The shipped input-selection implementation ran before the full-scan comparison oracle for each window. Frozen historical snapshots were checked for exact field values, saved fingerprints and ordering. Current-catalog old/new selectors compared within one read transaction; drift from historical snapshots was reported separately. Historical input counts must not be confused with current eligible counts or historical catalog timing.

The saved collection fixture loaded all 338,122 active rows in batches of 2,000 through the real `boards.save` implementation. Fixture loading and application persistence were timed separately. Row counts and source identity hashes were retained. This exercises existing-posting upserts, not live fetching, new-posting enrichment, board closure or every collector code path.

Overlap began only after backup capture had ended and all services resumed, while the shipped backup implementation validated, hashed and compressed its independent snapshot. Replays and persistence ran concurrently. Touched byte arrays reserved another 768 MiB for collector overhead and 256 MiB for reviewer overhead, based on prior observed production peaks. The reviewer reserve remained for at least 330 seconds. This is an intentionally conservative simulated memory workload, not a second model generation. The replay correctness oracle adds memory beyond ordinary input selection.

Host memory, VM counters, CPU, pressure stalls, container states and three local HTTP endpoints were sampled roughly every two seconds. Docker states were sampled roughly every 30 seconds. HTTP requests timed out after three seconds. Planned backup and restart outages are reported separately. Local HTTP timing does not measure Tailscale, browser rendering or Internet latency. Service/API health here does not clear pre-existing discovery or stale-automation readiness alerts. The absence of sampled OOM/restarts is corroborated by kernel counters and final container checks.

Predeclared targets: no OOM or unexpected restarts; at least 512 MiB available during overlap; steady HTTP p95 below one second and backup-validation p95 below two; backup downtime below 350 seconds and total below 3,600 seconds. A short rehearsal does not replace the representative-week observation requested in the capacity guide.

## Excluded harness attempts

Initial standalone harness attempts used the wrong review-ledger database and omitted a Store constructor field. Both were corrected before the valid run. The first full persistence attempt used a fixture query that selected the closed_at index and sorted all active rows repeatedly; it was stopped and replaced with explicit primary-key range iteration. The production save function was unchanged. Its incomplete timing is excluded. Early health-probe calibration used a nonexistent route; only the corrected `/api/v1/health` is included in workload results.

## Recovery preparation

The exact release's safe extraction took 190.157 seconds. Full snapshot verification, including hashes and SQLite integrity checks, took 1,033.736 seconds. These were setup measurements spanning the temporary Unlimited interval, not a zero-credit Standard recovery-time guarantee. They completed before the restored services were started. All nine production services and the stub became healthy; the require-current-v2 schema check passed. Real encryption-key recovery and live external-provider authentication were deliberately not exercised.

## Evidence, cleanup and repository validation

Private audit evidence is retained at
`s3://career-platform-622946928150-us-east-2-backups/backups/validation/medium-host-20261019/evidence.tar.gz`.
Its SHA256 is `ca33e5a61f9f03c619c178d6ac1d50adf2ca188fd907c67ffcb9a412e9d03ec0`.
The 40-file archive contains raw samples, phase receipts, exact replay/persistence
harnesses and the generated backup manifest. Acceptance, CloudWatch, assessment,
cleanup and validation summaries are retained under the same prefix's `assessment/`.
Local working evidence is under `/private/tmp/career-medium-evidence-20261019`.
It contains private operational artifacts and is not a repository fixture.

Terraform destroyed exactly the three reviewed temporary resources. AWS read-back
at 03:14:17 UTC confirmed instance `i-02fbfce290d3f27d5` terminated and both
`vol-065f07524d34b39ba` (data) and `vol-0bec23ddabb0997ea` (root) absent.
Production `i-0fa677e47dee0134c` remained running as a Standard `t3.large`, with
its original root/data volumes attached. Its nine persistent services were healthy
and the health endpoint returned HTTP 200. The audit objects and separate, empty
Terraform state are intentionally retained; the rehearsal archive/data disks were
removed. No production backup, volume or secret version was deleted.

Repository validation commands:

```sh
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
uv run python -m tests.test_review_input_replay
terraform -chdir=infra/aws/recovery-drill fmt -check -recursive
terraform -chdir=infra/aws/recovery-drill test
```

The replay suite uses the preferred Python 3.11 environment. An earlier optional
run of that suite under the host's system Python 3.9 hit a read-only WAL fixture
error; both required collector commands passed on their respective interpreters.
The final command results are retained in the private validation receipt.

The isolated branch is `ops/medium-rehearsal-20261019`, based on the exact production
commit. Changes add a disposable-host size/credit override, pin its safe defaults
in Terraform tests, and record capacity/recovery guidance. Application behavior
and the user's dirty primary worktree were not changed.
