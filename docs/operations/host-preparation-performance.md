# Host preparation performance

This follow-up starts from `b935259` on main, including deployment preparation
from PR #85 and rollback-journal handling from PR #87. It changes host snapshot
preparation only. The release owner retains the predecessor update, batch selection,
preparation workflow and deployment; this feature does not dispatch those workflows.

## Bottleneck and evidence

The supplied successful production deployment measured 699.221 seconds of online
preparation: 132.549 copying, 491.293 SQLite checks, 71.909 hashing and 3.147 syncing.
SQLite checks account for 70% of preparation and hashing another 10%. Services
remained available. Its approximately 182-second cutover included an 85-second
final snapshot; those are separate from preparation, not time saved by this PR.

Existing receipts have aggregate timings, not per-database timings. A bounded
read-only observation compared two retained snapshot manifests (at most 4 MiB
each), without reading live database contents, acquiring maintenance locks, or
modifying production. The snapshots were taken at 03:54 and 05:07 UTC on
2026-10-10. The largest database was `state/job-boards.db` (6.724 GB), followed by
`state/job-boards-preference.db` (0.810 GB). Their recorded hashes differed; the
1.481 GB toolchain bundle and 0.083 GB immutable predecessor archive matched.
The captures used different copy paths, and SQLite backup can produce different
physical bytes. Two snapshots do **not** establish the frequency of live content
changes or prove a cache hit rate. No repeated multi-GB production hash scan was
run to obtain one. Production per-file validation cost remains unmeasured.

The fictional fixture uses four indexed SQLite databases, an immutable predecessor
with its original digest, binary assets, and small text files. In the first cold
2 GiB Linux baseline trial the largest database's check took 3.405 seconds; the
other three checks totaled 0.148 seconds. This supports measuring and avoiding
repeated validation of large exact contents, without claiming those ratios apply
to the production schema or storage.

## Decision and trust boundary

| Hypothesis | Decision | Tradeoff |
| --- | --- | --- |
| Hash while copying | Implement | Hashes exactly the bytes written; eliminates a destination read, but hashing still consumes CPU. |
| Reuse successful validation by full-content hash | Implement using the existing journal-bound snapshot manifest | Avoids a new persistent cache and retention policy. Requires a compatible retained snapshot and exact content equality. |
| Reuse timestamps, lengths or unanchored manifests | Reject | None proves that these contents passed the required integrity check. |
| Reuse or hard-link an old payload | Reject for this change | Fresh independent bytes preserve existing ownership, retention and recovery behavior. |
| Two concurrent integrity checks | Keep experimental | Local throughput results cannot establish contention or request latency on the production EBS volume and active workers. |
| Seed results from old manifests | Defer | Old manifests do not record the verification contract; the first run must earn that evidence. |

The host operations module owns copying, verification, cache interpretation and
publication. No new service, configuration switch, infrastructure or writable
hard link is introduced. Every candidate is freshly copied into a private scratch
directory. The streaming hash covers the copied bytes, including any torn live
copy, rather than trusting a prior stat or a separately timed live-file digest.

Only a completed deploy/rollback journal can anchor reused validation. The referenced
directory-v1 manifest must match the journal's SHA-256, backup identity, format,
verification method and SQLite engine version. Its directory, the operations
directory, and metadata ownership/permissions must be private to the operations
user. Metadata reads reject symlinks, nonregular files, multiple hard links and
oversized files. Missing, incompatible or invalid evidence is a cache miss, never
a reason to omit validation. The journal is the existing host trust boundary;
the optimization does not defend against an administrator rewriting trusted
operation evidence.

For each database, the new copy's full SHA-256 and byte count must equal the
manifest entry before reusing its check result. Otherwise the new copy receives
the existing SQLite quick check. The manifest proves which contents were checked;
old payload bytes are neither read nor adopted, and damaged old payloads are still
detected by full restore verification if that backup is restored.

At cutover, all existing rules remain: enumerate stopped state, hash the **entire
stopped source**, reject reuse on a nonempty WAL or rollback journal, and use the
SQLite backup API plus checks for changed/journaled databases. The #87 journal
ordering fix remains in force. The complete independent snapshot and manifest are
flushed and durably journaled before initialization. Speculative preparation never
publishes a cache record or recovery point. A crash can leave harmless scratch;
it cannot certify unchecked bytes or advance the deployment journal.

## Measurements

[Raw fictional measurements](evidence/host-preparation-2026-10-10.json) include
per-file checks, process CPU, physical I/O, probe latency, receipts and source hashes.

Three 2 GiB trials per serial implementation ran in a local ARM64 Linux Docker
volume, Python 3.12 / SQLite 3.40.1, limited to two CPUs and 1 GiB memory. Source
file pages were evicted with `POSIX_FADV_DONTNEED`, without global cache eviction.
Medians in seconds:

| Case | Main preparation | Candidate preparation | Main paused snapshot | Candidate paused snapshot |
| --- | ---: | ---: | ---: | ---: |
| No compatible verification reference | 8.260 | 6.799 | 1.543 | 1.356 |
| Unchanged with reusable reference | 8.513 | 2.944 | 1.482 | 1.282 |
| One smaller database changed before preparation | 7.801 | 2.308 | 1.542 | 1.349 |
| Database changed after preparation | 8.898 | 4.263 | 1.788 | 1.475 |
| Committed WAL state | 9.318 | 4.361 | 1.780 | 1.398 |
| Damaged reference manifest | 6.653 | 5.098 | 1.502 | 1.172 |

The reusable-reference preparation median fell 65%; cold preparation fell 18%.
Reusable-case process CPU fell from 3.623 to 1.626 seconds and physical reads from
2.526 to 2.002 GiB. Every produced snapshot completed full checksum/SQLite restore
verification after its measured capture. No service startup or downtime is measured
by this fixture. Preparation varies with host/cache conditions; the case rows are
separate measurements, not fixed costs to sum.

An approximately 11 GiB fixture under the same limits produced this single-trial
comparison. All source pages were nonresident before preparation:

| Implementation / reference | Preparation | Process CPU | Paused snapshot |
| --- | ---: | ---: | ---: |
| Main / cold | 69.011 s | 27.761 s | 6.435 s |
| Candidate / cold | 65.642 s | 24.718 s | 6.290 s |
| Candidate / reusable | 19.406 s | 8.925 s | 6.229 s |

The reusable case checked zero databases and reused all four prior checks. Its
preparation was 72% shorter than the main cold measurement; the cold candidate
improved only 5%. Main does not reuse verification evidence. The baseline large
benchmark was deliberately stopped after its first completed, fully verified cold
case to bound local resource use; no additional large baseline trials are claimed.

A final separate 2 GiB edge run measured 7.073 seconds cold and 4.906 seconds
reusable. Changing the largest database before preparation took 6.330 seconds
(one fresh check, three reused); a PERSIST rollback-journal case took 2.203 seconds
(one fresh check, three reused), followed by the required final SQLite backup.
Their paused captures took 1.154 and 1.396 seconds respectively. Each snapshot
passed full restore verification. These single trials illustrate cache outcomes,
not a stable speedup when the largest database changes.

A single exploratory baseline run with two concurrent check workers measured
4.668 seconds cold preparation (2.583 CPU seconds, 2.705 GiB physical reads).
The simple same-volume SQLite read probe had a 0.108 ms p95, versus 0.098 ms for
serial baseline cold preparation. This is promising throughput evidence, but the
probe is a tiny cached read every 20 ms, not the production dashboard/model/mail
workload. It does not validate parallel I/O against production IOPS limits or
worker latency; no worker pool was added to deployment code.

The machine is shared. Initial candidate trials overlapped local offline tests;
these are exploratory measurements, not isolated hardware performance claims.
Initial baseline/parallel probes used advisory eviction without recording residency.
Later candidate probes recorded `mincore` residency: zero fixture pages before
the first cold/reusable cases, except a few pages retained by the open WAL case.
The larger comparison also records residency for both implementations.

OS page-cache state and availability of a trusted validation reference
are independent: the reusable-reference cases also evict the fixture's file cache.
All numbers are local observations, not forecasts of production preparation or
end-user downtime. The paused measurement covers snapshot capture only.

Reproduce the fixture from this checkout (never on production):

```sh
python3 -m tests.benchmark_host_preparation --megabytes 128 \
  --output .cache/preparation.json
# On a bounded local Linux volume, including the largest-database miss and journal:
python3 -m tests.benchmark_host_preparation --megabytes 2048 --runs 3 --evict \
  --modes cold reusable changed changed_largest changed_after wal rollback invalid \
  --output .cache/preparation-linux.json
```

For comparisons, save `git show b935259:job_search/aws_ops.py` to an ignored local
file and pass `--ops-source PATH`; the benchmark executes that implementation
against the same fictional fixture. `--check-workers 2` is an experimental benchmark
mode only. Use a fresh local Docker volume, `--cpus 2 --memory 1g --network none`,
the Python 3.12 slim Bookworm image, and `TMPDIR` inside that volume to match the
reported resource bounds. Allow at least three payloads plus the prior snapshot.

Regression checks include matching hashes, same-size/mtime mutations, changed
cutover state, committed WAL rows, real PERSIST and hot rollback journals, legacy
or wrong-engine contracts, unanchored/incomplete evidence, bad manifest hashes,
invalid entries, unsafe metadata ownership/permissions/links, source I/O errors,
post-publication corruption detection, and actual SIGKILL during warm preparation.
The normal release recovery tests still kill processes at publication and write
boundaries. Baseline verification passed both required collector commands and
186/186 offline suites. Candidate verification passed 187/187 offline suites,
including 15 new reference regression tests, both required collector commands,
and local upgrade/rollback acceptance. CI additionally exercises the Linux Compose
transition.

## Audit and operational limitations

The private `backup.json` now records `sqlite_verification` and per-file
`preparation.files`: copied size, validation outcome, cutover outcome, and timings.
Public receipts contain only aggregate validation counts. `hash_in_copy` is nested
inside `copy_and_hash`; it is not an additional read or additive elapsed interval.
Full restore verification does not consult this cache and still reads/checks every
retained file and SQLite database.

Only the most recent completed deployment's anchor is considered. An old release,
SQLite upgrade, scheduled backup, recovery operation, missing retained snapshot or
changed database can require cold validation. There is no history scan or speculative
cache to maintain. Large databases that change regularly still pay their full check
cost; files that change after preparation still pay the existing cutover fallback.
This PR does not promise a shorter cutover or a fixed production preparation time.

The release owner can inspect private per-file profiles after a later authorized
deployment, compare full manifest entries across compatible snapshots, and assess
whether the costly database actually remains unchanged between releases. That
evidence is needed before deciding whether a broader cache or parallel validation
is justified. Do not run benchmark fixtures or global cache eviction on production.
