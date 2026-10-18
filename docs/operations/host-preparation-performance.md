# Host preparation performance

For preparation copy/hash overlap and the mapped-read experiment rejected after
a production probe, see [snapshot preparation measurements](sqlite-snapshot-performance.md).

The October 17 [validation continuity fix](#validation-continuity-across-routine-backups)
preserves completed deployment proofs across routine backups. It supersedes the
historical limitation below that any scheduled backup forces cold preparation.

For the October 17 follow-up that moves preparation after worker draining and
routine-backup validation after service resumption, see
[snapshot pause performance](snapshot-pause-performance.md). The measurements
and behavior below describe the October 10 implementation.

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

## Validation continuity across routine backups

The October 17 production sequence exposed a metadata problem: a routine backup
replaced `operations/current.json`, so the next deployment lost access to the
preceding deployment's verification proof even though its private archived journal
and snapshot were still retained. The observed backup spent 1,234 seconds checking
captured SQLite files, and the next deployment spent 1,002 seconds preparing its
rollback snapshot. These are separate operations; neither entire interval is
claimed as savings from this change.

A backup journal now carries `verification_operation_id`, an explicit reference
to the archived successful deploy/rollback journal that `Operation.begin` already
flushes before replacing `current.json`. Consecutive completed backups preserve
the reference. No history scan, additional mutable cache, payload reuse, or new
retention policy is introduced. Existing snapshots and archived journals remain
the proof; missing retained evidence simply requires fresh checks.

The current backup must be complete in `snapshot_finished`, its installed release
must match the referenced deployment target, and that archive must have the exact
referenced operation ID and a successful `deployed` or `deployed_paused` phase.
The snapshot's release must match the deployment's predecessor (or initial target).
The release IDs bind these records to the installed source release. The manifest
digest, full copied-file hashes and lengths, verification method, and SQLite engine
version remain mandatory. Metadata reads reject unsafe ownership, permissions,
symlinks, hard links and oversized records. Archiving cannot normalize unsafe
current metadata into a trusted proof: its original permissions are checked first.

An interrupted operation, recovery, restore, reverted deployment, changed release,
missing archive, tampered manifest, or unknown validation contract forces a cold
preparation. Deploy/rollback start a new chain and earn a new successful anchor.
An interrupted backup does not grant reuse, even if its inherited pointer exists.
A failed archive upload after writers resume may retain the old deployment proof;
it cannot certify the failed backup or advance the last-successful-backup receipt.

Routine backups still run one complete SQLite quick check per captured database
before upload. Inspection found no duplicate integrity invocation within that
archive path to remove safely. This fix reduces **deployment preparation after a
backup**, not the routine backup's own 20-minute validation. Every fresh copy is
independent; changed bytes are checked, journaled cutover data is recaptured, and
restore always checks all bytes and databases again. A release adding database
indexes changes database contents and may miss reuse on its next deployment.

### Bounded verification and measurement

The full backup-to-deployment integration fixture checks both captured databases
during the routine backup, then reuses both previous deployment checks during
preparation. Regression coverage also includes three interleaved backups, changed
source bytes, damaged/missing/unsafe archived evidence, mismatched releases,
interrupted/recovered operations, and successful paused/active rollback anchors.

Three trials on a 512 MiB fictional fixture on macOS ARM64, Python 3.12 / SQLite
3.53.1, measured median preparation after a backup journal transition at **0.298 s
before and 0.231 s after**. All four SQLite checks ran in each baseline trial;
all four were reused in each candidate trial. Full restore verification passed
after every snapshot. These small, cached, shared-host measurements establish
that the lost reuse is recovered, not an EBS speedup or downtime forecast. The
benchmark isolates the backup journal transition; actual backup capture/upload
time is excluded and is covered separately by the integration fixture.

[Raw fictional results](evidence/validation-after-backup-2026-10-17.json) include
individual trials, source hashes, check counts and restore-verification timings.
Reproduce the bounded case with:

```sh
uv run --python 3.12 python -m tests.benchmark_host_preparation --megabytes 512 \
  --runs 3 --modes cold after_backup --output .cache/after-backup-preparation.json
```

The next production release should retain `preparation_validation` counts and
private per-file preparation profiles. Actual savings depend on how many costly
databases retain identical bytes between deployments; collection and migrations
can invalidate those checks even when the proof survives a routine backup.

## Fresh verification from a completed routine backup

A routine backup can now supply newer validation evidence than the preceding
deployment. The earlier continuity fix preserved the old deployment proof, but
could not certify catalog changes since that deployment. SQLite's backup API also
changes destination header bytes even when a stopped source is unchanged, so its
validated output commonly cannot match the next independent copy of the source.
A small fictional fixture observed changes at header offsets 27, 43 and 95 in
otherwise equivalent DELETE and checkpointed WAL databases.

During routine backup capture, **after every writer has stopped together**, a
SQLite main file with no nonempty WAL or rollback journal is copied verbatim into
independent storage. This is an explicit `quiesced_raw_sqlite` caller contract;
`copy_snapshot` keeps its online SQLite backup behavior by default. The existing
`paused=True` backup API is likewise an explicit assertion that all writers are
already stopped. Sidecar symlinks are rejected. A nonempty WAL or rollback journal
uses SQLite's backup API, including its existing fail-closed behavior for a hot
journal requiring write recovery. Immutable predecessor checks remain mandatory.

Services resume before the captured files are checked, hashed and compressed.
Every captured database still receives a complete SQLite check. Neither the
`snapshot_finished` journal phase nor successful resumption proves validation:
that phase precedes checking and upload. Only after the archive and its receipt
have both uploaded, and the successful-backup receipt is recorded, may the operation
publish a private verification manifest at
`operations/last-backup-verification.json`. Its SHA-256, backup ID, release ID,
operation ID, archive SHA-256 and SQLite verification contract are then bound into
the **same completed backup journal**. The fixed file is bounded to 64 MiB when
read; it contains file hashes and sizes, not payloads or secret values.

Preparation first considers that current backup's own proof. Its fresh independent
copy must match the complete recorded SHA-256 and byte count before reusing a
SQLite check. Current journal, directory and proof permissions/ownership are checked;
symlinks, hard links and oversized metadata are rejected. Missing, damaged,
incompatible or stale fresh evidence falls back to the separately retained
trusted deployment anchor, or a full check. No history search is added.

The next backup does **not** inherit this fresh proof. It must earn its own after
publication; a failed or interrupted attempt cannot claim success using the
previous fixed file. Existing deployment-anchor continuity is preserved. Therefore
one atomically replaced metadata file suffices: when a later backup writes it,
the previous backup's fresh proof is no longer the current journal's anchor.
A crash between proof publication and journal binding leaves unanchored metadata.
A proof-publication failure does not invalidate a successfully uploaded backup
and does not overwrite the deployment snapshot or its archived journal.

This proof is not a recovery artifact. Routine archives retain their complete,
self-contained format; restore always verifies archive/file hashes and every
SQLite database independently. Restore, recovery, release changes and interrupted
operations cannot promote a stale fresh proof into trusted evidence.

### Expected benefit and limits

An exact match for the observed 7.589 GB catalog could avoid its roughly
641-second preparation check. That is a conditional opportunity, not a measured
future deployment reduction. A collection, index migration or other write to the
catalog invalidates the byte match. Scheduled backups are due at 08:00 and 20:00
UTC; collection runs every four hours in America/Chicago. There is therefore only
a limited interval after each backup before the next catalog mutation. In
particular, release 82 began preparation after the 18:00 local collection, so a
20:00 UTC backup would likely **not** have avoided that release's catalog check.
Raw copying may separately shorten routine backup capture; its checks and upload
still happen in full afterward.

The integration test changes both databases after the deployment anchor, performs
a complete routine backup, then deploys and reuses both newly earned checks.
Coverage includes subsequent changes, WAL commits, hot rollback journals, unsafe
metadata, identity/contract mismatches, capture/check/upload failures, optional
proof/journal write failures, and actual process death between proof write and
journal binding. None of those failures grants fresh validation.

### Raw capture measurement

Three alternating trials captured a 1,075,138,560-byte fictional indexed SQLite
database on a local ARM64 Linux volume, Python 3.12 / SQLite 3.40.1, limited to
two CPUs and 1 GiB memory. Fixture-only advisory eviction and `mincore` established
zero resident source pages before each capture. The timed interval includes
capture and fsync; full SQLite checks, content comparison and hashes ran afterward
for every output. No production state or service pause was involved.

| Capture | Median | Range | Exact source bytes |
| --- | ---: | ---: | --- |
| SQLite backup API | 2.843 s | 0.985–3.232 s | No, header differs |
| Quiesced raw copy | 0.702 s | 0.691–2.867 s | Yes, every trial |

The shared filesystem had substantial flush-time variability, including a raw
copy slower than two API trials. These medians are observations, not a reliable
percentage speedup or an EC2 prediction. The exact byte identity is the necessary
property for fresh-proof reuse; safe fallback and post-resumption verification are
more important than a particular copy timing. [Raw results](evidence/backup-raw-capture-2026-10-17.json)
retain every trial and physical I/O count.

Reproduce only on a bounded local Linux Docker volume with `TMPDIR` on that
volume, `--cpus 2 --memory 1g --network none`:

```sh
python -m tests.benchmark_backup_raw_capture --megabytes 1024 --runs 3 \
  --evict --output .cache/backup-raw-capture.json
```
