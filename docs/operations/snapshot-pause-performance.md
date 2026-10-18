# Snapshot pause performance

The October 17 investigation found routine backups paused services for a median
13.3 minutes. The sampled deployment paused them for 16.7 minutes, including
898 seconds of snapshot work and 686 seconds of SQLite checks. Preparation had
already checked the catalog, but subsequent worker writes made that copy obsolete.
Those private production observations motivated the changes below; the local
benchmark does not establish the resulting production downtime.

## Capture and verification boundaries

Routine off-host backups still stop all writers together and copy every database
and retained file into independent private staging. SQLite capture retains the
backup API, including committed WAL and rollback-journal handling. Services resume
as soon as capture finishes. Database quick checks, full hashes and compression
then run on those independent captured bytes. No archive upload or receipt
publication occurs before validation succeeds. Corruption, process death or
upload failure leaves the previous successful receipt intact. A process killed
after resumption can leave unpublished private staging; it is never a recovery
receipt. Scheduled-attempt monitoring continues to identify abandoned attempts.

Deployments have a different boundary: every recovery file must be validated and
durably published before any migration or secret update. They now drain and stop
the background worker partitions first, then prepare large checked copies while
the dashboard remains available. This avoids catalog changes from a collector or
ranker finishing during preparation. Dashboard or other remaining writers can
still change files; the final all-service stop, complete source hash, sidecar
checks and fallback capture remain mandatory. Full-content equality, rather than
timestamps, authorizes reuse. Cross-database consistency still comes from final
global quiescence and re-enumeration, not the speculative preparation.

The operation journal now starts before draining/preparation. Trusted validation
metadata is loaded from the preceding completed operation before its journal is
replaced. An ordinary preparation failure resumes drained workers without stopping
the dashboard. A killed preparation has an incomplete operation and requires
explicit recovery, which preserves live data because migration has not started.
Existing kill tests cover both preparation and post-migration write boundaries.

Preparation pauses background automation, including interactive worker partitions.
This tradeoff improves dashboard availability and snapshot reuse; it does not make
deployments free of worker disruption. `snapshot_preparation` is reported separately
from `drain` and dashboard `downtime`. Routine backup manifests and receipts report
capture, checks, hashes and the operation's stop-to-health interval. Nested capture
timings must not be summed as independent elapsed intervals.
Private manifests also contain `capture_files`, recording each file's reuse or
recapture outcome and paused copy/check/hash/publish timings. Routine backups
separate each file's subsequent verification/hash timings into
`postresume_timings_seconds`. Public receipts expose only aggregate timings.

## Bounded local benchmark

Run against fictional state only:

```sh
uv run --python 3.11 python -m tests.benchmark_backup_pause --megabytes 128 --runs 3
```

The October 17 run used a 135,430,144-byte indexed SQLite database on the local
macOS ARM64 filesystem, Python 3.11.16. The shared host's cache was not evicted.
Every capture was checked, and deployment snapshots additionally passed full
restore verification. No production files, services, network or delay injection
were used. Median snapshot intervals over three trials:

| Scenario | Before | After |
| --- | ---: | ---: |
| Routine backup, captured bytes while paused | 0.160 s | 0.120 s |
| Deployment, final worker write before global stop | 0.245 s | 0.049 s |

Routine capture plus validation still took about 0.16 seconds: the change moves
verification outside the service pause rather than omitting it. The deployment
fixture prepares before or after one final worker write. The earlier preparation
recaptured the database in all three trials; the later preparation reused it in
all three. These are small local snapshot measurements, excluding drain, service
stop/start, image pulls, migration, network upload and production EBS behavior.
They show the phase change, not a forecast of AWS savings or downtime.

## Verification and production follow-up

`tests.test_snapshot_pause_boundaries` exercises resumed writes against a coherent
captured pair of databases, real staged corruption, failed validation, actual
SIGKILL after resumption, preparation order, ordinary preparation failure, prior
validation anchoring and all four worker drain identities. Existing snapshot and
recovery suites retain committed-WAL, hot-journal, same-size/mtime mutation,
durability, corruption and subprocess-death coverage. Full restore validation
continues to ignore speculative validation caches.
Explicit rollback tests verify old releases do not start unsupported worker
partitions. A new-worker health failure stops target services, records the resumed
service set, and preserves new writes on recovery. Recovery enumerates all Docker
project containers rather than relying on the predecessor's service list.

The release owner should compare the next deployment's snapshot reuse counts,
`sqlite_check`, preparation, stop-to-health interval and worker interruption.
Inspect the next scheduled backup's capture and verification timing separately.
Preparation can still miss reuse because of remaining live writers or nonempty
journals, and a large independent capture still requires time and disk space.
Never bypass missing equality evidence or recovery checks to improve a timing.
