# Deployment performance

For the subsequent investigation of the 699-second **online host preparation**,
including streaming hashes, exact-content verification reuse and per-file
measurements, see [host preparation performance](host-preparation-performance.md).
The measurements below describe the original #85 change.

The change prepares large private file copies while the application is available,
then proves each reusable copy matches the stopped source. It keeps the existing
rollback format, cross-database/file quiescence boundary, journal, startup gate,
secret-version inventory, archive hashes and restore verification. No production
configuration, deployment, Terraform, storage format or IAM change is included.

## Measurement boundaries

The reported approximately 20-minute production backup of about 11 GB is an
operator observation, not a stage profile. No production operation was inspected,
interrupted or benchmarked for this change. Local measurements cannot identify
which EBS/CPU/cache condition caused that incident or establish its resolution.

`downtime` covers stopping through healthy services; it excludes image pulls,
preparation, worker drain, and retention cleanup. The dashboard remains available
while workers drain. Actual external availability may differ from container health.
The snapshot benchmark measures only the snapshot portion of that interval.
Compose and transition checks separately time fictional initialization and startup;
their timings must not be added to an unrelated disk benchmark as measured downtime.

The fixture contains approximately 11 GiB of indexed fictional SQLite rows across
three mutable databases (60%), a read-only predecessor archive with its original
SHA-256 report (15%), and model/toolchain/history files (25%), plus small documents.
It is fully written, not sparse. It exercises the real capture, quick checks,
hashing, flush, manifest publication and full restore verification. It does not
replicate the owner's database schemas, fragmented disk, workload or EBS limits.
Cache is not evicted. The `changed` case changes one database after preparation;
unit tests separately retain committed WAL rows and same-size/mtime file mutations.

## Local measurements (2026-10-09)

Baseline: `6702737599e71f969ee22e0ada9a85b5e23eaa2f`. Candidate capture:
`13af3d26df5c7744e26f76445cc3c7ac9e0a8b11`. Payload: 11,831,380,318 bytes
(about 11.02 GiB). Mac ARM64, Python 3.9 / SQLite 3.51.0; three trials per mode.
These are exploratory local timings: the host was shared, caches were not evicted,
and some candidate trials overlapped offline suites and a Linux VM benchmark.

| Pipeline | Median online preparation | Median paused snapshot | Median total snapshot work |
| --- | ---: | ---: | ---: |
| Baseline, all work paused | 0 s | 31.100 s | 31.100 s |
| Candidate, no preparation (locality change alone) | 0 s | 15.698 s | 15.698 s |
| Candidate, unchanged large files | 11.649 s | 5.294 s | 16.943 s |
| Candidate, one database changed after preparation | 13.875 s | 7.619 s | 21.494 s |

Baseline median stages were 19.219 s SQLite quick checks, 5.691 s SQLite backup,
1.350 s ordinary file copying, 4.761 s hashing and 0.003 s explicit tree flush.
These measure API boundaries: SQLite backup also performs internal writes/flushes.
Checking each freshly copied database immediately substantially improved locality.
With unchanged prepared files the paused work was dominated by a 5.198 s source
hash pass (middle trial); seven files totaling 11,831,283,712 bytes were reused.
The paused snapshot range was 5.074–5.478 s, versus baseline 30.852–32.082 s.
For the changed case, six large files were reused and the changed database took
its normal SQLite backup/check/hash path. Every produced snapshot passed complete
restore verification (including all checksums and SQLite checks); no verification
was disabled for the benchmark.

This is an 83% reduction in the **local snapshot portion** of downtime for the
unchanged fixture. It is not an 83% production deployment improvement, a measured
full deployment outage, or a prediction of EBS behavior.

The Linux ARM64 Docker Desktop volume (Linux 6.10.14, SQLite 3.40.1,
approximately 7.65 GiB VM RAM) gave the following **single-trial** observations.
This is a VM-native Docker volume, not an EBS volume or native-host AWS gate;
container builds/acceptance and other local tests shared the host during the run.

| Pipeline | Online preparation | Paused snapshot | Total snapshot work |
| --- | ---: | ---: | ---: |
| Baseline | 0 s | 128.589 s | 128.589 s |
| Candidate, no preparation | 0 s | 58.733 s | 58.733 s |
| Candidate, unchanged large files | 55.830 s | 7.082 s | 62.912 s |
| Candidate, one changed database | 50.248 s | 7.802 s | 58.051 s |

Linux baseline stages were 105.972 s integrity checks, 9.089 s SQLite backup,
2.899 s file copying, 10.428 s hashing and 0.155 s explicit tree flush. Candidate
unchanged preparation spent 40.077 s checking databases while services could be
available; its paused capture spent 6.996 s hashing the stopped source. Complete
snapshot verification also ran after each timed capture (22.7–35.0 s for the
candidate). Neither disk synchronization nor hashing explained the baseline delay
in these fixtures; SQLite checks did. Production can have a different bottleneck.

Container acceptance at `e4bfe3a7f69bc5b6f0ffdaf052dee48fd83e671e` passed all
10 Compose checks. Its small generated application state reached initial service
health in 33.501 s, including initialization. The snapshot helper took 1.153 s,
restore 1.374 s, and restored interactive services reached health in 13.089 s.
The separate clean-tree Docker transition check passed predecessor seed, candidate
upgrade (2.180 s), predecessor rollback (1.967 s) and owner restore verification
(2.684 s); total including predecessor build was 41.631 s. These fixture phases
include assertions/process overhead, are not pure migration timings, and are not
an end-to-end production outage. Native Linux acceptance still runs in PR CI.

The final durability review added a parent-directory flush after each reusable
file move and tests for that flush failing before initialization. Snapshot
publication already flushes the destination tree; both rename sides are now
explicitly flushed. The final offline suite includes 12 preparation/timing tests,
60 existing operations tests, and the subprocess-death recovery suite. Both
collector commands and the full 186-suite offline runner pass. A final 11 GiB Mac
trial with the additional flush measured 4.854 s paused / 14.911 s total snapshot
work unchanged, and 6.893 s paused / 16.836 s total with a changed database. The
extra rename-directory flushes totaled 0.002 s in the unchanged trial. Its fully
paused capture was 28.016 s, illustrating shared-host/cache variability.

[Raw benchmark and acceptance receipts](evidence/deployment-snapshot-2026-10-09.json)
retain the individual trials, stage counters, exact tested source identities and
limitations. Offline-suite receipts describe development worktrees and are not
release authorization; the container transition receipt identifies its clean
committed source.

## Alternatives and decision

| Approach | Benefit | Cost or limitation |
| --- | --- | --- |
| Check/hash each fresh copy immediately | Better locality; less rereading cold data | Still all inside downtime when every byte must be copied |
| Prepare copies and checks online, verify full source hashes when stopped | Moves large copies, integrity checks and most flush work outside downtime; no new infrastructure | Extra online I/O; changed/WAL-backed databases fall back; still one full source read |
| Copy stopped SQLite main files directly | Faster sequential copy in a small local probe | Must handle hot journals and WAL correctly; retain the existing SQLite backup path for all fresh database captures |
| Trust size/mtime or omit integrity checks | Less work | Rejected: same-size changes and corruption would silently compromise recovery |
| Hard-link live assets or reuse unverified prior snapshots | Fewer writes | Rejected: live mutation would compromise retained rollback bytes, or metadata alone would not prove equality |
| Filesystem/volume snapshots | Potentially very short capture pause | Current host formats ext4; adding another filesystem/LVM or EBS restore coordination changes provisioning, capacity/recovery and evidence contracts |
| Per-database online backups only | Little or no database pause | Rejected as the rollback point: individually consistent databases do not establish consistency across databases and files |

The selected implementation combines the first two approaches. It does not change
source files, issue live checkpoints, hard-link bytes, reuse old unverified
receipts, or defer mandatory verification until after initialization. Reuse uses
full stopped-source SHA-256, not a live listing or timestamp. A nonempty WAL or
rollback journal forces a fresh SQLite backup, even when the main-file hash matches.
The final tree is enumerated after all writers stop; prepared copies never define
its membership. Small files use ordinary capture to avoid extra preparation work.

SQLite's [backup documentation](https://www.sqlite.org/backup.html) describes the
consistency provided by its backup API. Its [WAL documentation](https://www.sqlite.org/wal.html)
explains why the main database alone is insufficient with pending WAL state.
AWS likewise recommends pausing writes for [consistent EBS snapshots](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-creating-snapshot.html);
using them here would also require a tested journal-aware volume recovery path.

## Reproduce and validate

From a development checkout with sufficient temporary disk capacity:

```sh
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
python3 -m tests.test_prepared_snapshots
python3 -m tests.test_release_recovery
python3 -m tests.benchmark_deployment_phases --megabytes 11264 --runs 3 \
  --output .cache/snapshot-profile.json
uv run --with cryptography --with pypdf --with reportlab \
  python scripts/check-system.py
uv run --with cryptography --with pypdf \
  python scripts/release-transition-acceptance.py --local
```

For the baseline, import `job_search` from an isolated archive of
`6702737599e71f969ee22e0ada9a85b5e23eaa2f` using `PYTHONPATH`, then execute the same
benchmark script by filename. The script detects whether preparation exists;
`paused` uses the unchanged fully paused capture, `prepared` exercises unchanged
state, and `changed` forces one database to miss reuse. Verification and fixture
seeding are outside the snapshot timing. Detailed profile counters are nested:
receipt capture includes SQLite backup/check/hash costs and must not be added to
them. The profiling wrapper's copy counter excludes preparation's streamed copies;
those have their own preparation receipt counter.

Run production-image Compose and predecessor/candidate transition acceptance on
Linux through the existing PR/release gates. Local `--local` transition evidence
is explicitly not release authorization. Failure tests kill actual subprocesses
during speculative flush, reusable-file move, durable publication, initialization,
release switching and restore directory swaps. Recovery preserves the old state
before writes resume and preserves new user work after the write boundary.

## Remaining production gate

On the next separately authorized release, preserve the deployment result and
operation journal. Compare preparation, source hash, fresh SQLite backup/check,
flush, initialization, validation and service startup separately. Record total time
and the stopping-to-health interval; use an external dashboard probe if end-user
availability must be established. Inspect reused bytes to distinguish an idle
code-only update from a busy collector/mail run. Targets remain 2–5 minutes routine
and ideally 1–2 minutes code-only, not an SLA or a result of these local tests.

Preparation requires conservative free-space headroom of three payloads plus
256 MiB. Large changing databases may increase total I/O and still miss the downtime
target. Long drains remain outside UI downtime but can dominate total deployment
time. Abandoned `snapshot-preparing-*` directories remain private scratch evidence
for inspection while the operations lock is idle; they never authorize recovery.
