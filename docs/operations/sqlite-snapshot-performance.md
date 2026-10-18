# Snapshot preparation I/O measurements

Preparation now overlaps large-file copying with bounded hashing, as described
[below](#overlap-copying-with-hashing). The mapped-read SQLite experiment was
**rejected after a production probe**. Production retains the existing read-only,
immutable connection and complete `PRAGMA quick_check`, with no new mmap setting.

## Rejected mapped-read experiment

The October 17 deployment spent 642.041 seconds checking SQLite copies during
snapshot preparation. Its 7.589 GB collected-jobs database accounted for 641.565
seconds; the other two checked databases together took under half a second.
CloudWatch recorded roughly 1,600–1,830 small reads per second during that check,
with 8–14 MB/s throughput and a queue near one. Increasing provisioned throughput
would not address that serial, small-read pattern.

A candidate requested up to 2 GiB of memory-mapped reads while preserving the
complete check. Local fictional fixtures improved substantially, including a
fixture larger than both the mapping and the container memory limit. Those results
justified a single read-only probe of the retained production snapshot, without
changing live databases, stopping services or evicting production caches.

| Retained production snapshot probe | Result |
| --- | ---: |
| Database size | 7,588,659,200 bytes |
| Effective mapping limit | 2,147,418,112 bytes |
| Complete quick check | `ok` |
| Elapsed check time | 663.806 s |
| Process CPU | 48.921 s |
| Peak RSS | 2,113,424 KiB |
| Physical bytes read | 8,825,921,536 |
| Physical bytes written | 0 |

The earlier deployment checked these same captured database contents in 641.565
seconds. The probe took about 22 seconds longer, but cache conditions and the
preceding workload differed: this is **not a controlled regression measurement**.
It establishes no production win that would justify adopting the extra mapping.
The [aggregate probe evidence](evidence/sqlite-mapped-production-2026-10-17.json)
contains no database rows or schema.

The host's SQLite 3.45.1 is compiled with a roughly 2 GiB maximum mapping, so only
the first portion of this 7.589 GB file can use mapped reads. The remaining bytes
still use ordinary reads. [SQLite's documentation](https://www.sqlite.org/mmap.html)
confirms that this compile-time maximum cannot be increased at runtime. A custom
SQLite build would add a separate maintenance and recovery dependency and is not
part of this optimization.

The candidate also raised resident memory and had an operational disadvantage:
a mapped I/O error can terminate the process with `SIGBUS`, whereas ordinary
reads can return a catchable SQLite error. File-backed pages are reclaimable,
but still consume resident memory and can displace other cached data. With no
convincing host improvement, those costs are not justified. The production mmap
setting and its candidate-only fault tests were removed; existing recovery tests
and unchanged full SQLite verification remain in place.

Content hashes, private journal-bound proof checks, WAL/journal rejection,
stopped-source equality, independent copies, durable publication and complete
restore verification remain mandatory. No partial-check shortcut was adopted.
The benchmark remains available as an explicitly experimental tool to reproduce
the evidence or evaluate a future platform.

## Bounded local measurements

The fixture uses the actual collector table and indexes, fictional descriptions,
updates that grow overflow records, and deleted rows. Each check ran in a fresh
process on a local ARM64 Linux Docker volume, Python 3.12 / SQLite 3.40.1, with
two CPUs and a 1 GiB memory limit. Only fixture files were evicted with advisory
`POSIX_FADV_DONTNEED`; `mincore` confirmed zero resident fixture pages before every
check. No global caches or production files were touched. These are shared-machine
observations, not EBS or deployment forecasts.

| Database | Default median | Mapped median | Default peak RSS | Mapped peak RSS |
| --- | ---: | ---: | ---: | ---: |
| 770 MB, fits mapping | 2.540 s | 0.415 s | 40 MB | 801 MB |
| 3.081 GB, exceeds mapping and memory limit | 10.513 s | 6.070 s | 42 MB | 1,056 MB |

Each median covers three trials. Both paths completed full checks and left the
fixture checksum unchanged. The smaller fixture read 770 MB physically in both
modes. The larger fixture exposed a cost: physical reads increased from about
3.26 GB to 5.23 GB with mapping under tight memory. Its mapped pages were reclaimed
under the 1 GiB cgroup limit; observed cgroup OOM and OOM-kill counters were zero.
The larger case demonstrates successful checking beyond the mapping limit, but
its extra I/O makes extrapolation to a busy EC2 volume inappropriate.

An exploratory 64 MiB SQLite heap-cache trial did not improve the original
924 MiB fixture (roughly 5.0 seconds versus 4.9 seconds). That tuning was rejected.
Mapping the original complete fixture took roughly 0.52 seconds; the more realistic
collector-schema trials above supersede that fixture as the comparison evidence.

Raw fictional results: [770 MB](evidence/sqlite-mapped-770mb-2026-10-17.json) and
[3.081 GB](evidence/sqlite-mapped-3gb-2026-10-17.json).

Reproduce on a bounded local Linux volume with `TMPDIR` on that volume:

```sh
python -m tests.benchmark_sqlite_snapshot_check --megabytes 512 --runs 3 \
  --evict --output .cache/sqlite-mapped-small.json
python -m tests.benchmark_sqlite_snapshot_check --megabytes 2048 --runs 3 \
  --evict --output .cache/sqlite-mapped-large.json
```

Use `--cpus 2 --memory 1g --network none` for the reported Docker resource bounds.
The megabytes argument sets the initial description payload; updates, table pages
and indexes increase the resulting database size. The benchmark reports actual
bytes, effective mapping size, per-process peak RSS, CPU and physical reads.

## Overlap copying with hashing

Preparation copies of at least 32 MiB now use one SHA-256 worker. The producer
writes the next 1 MiB block while the previous block is hashed, then joins that
hash before submitting another. There is at most one outstanding hash task and
only a few MiB of buffering, regardless of source size. Small files retain serial
copying. The hash covers the exact immutable bytes accepted by the destination;
it is never a separately timed read of the source.

A short write aborts preparation. Early source EOF reports only copied bytes,
which cannot match a larger stopped source at cutover. Worker errors propagate,
and the worker is joined on all exits before scratch cleanup. Existing final
hash equality, SQLite checks and fsync requirements remain unchanged.
`hash_in_copy` remains nested inside `copy_and_hash`; its elapsed work can now
overlap read/write time and must not be added to the outer interval.

The first 924 MiB exploratory fixture showed roughly 27% lower copy/hash wall
time with the bounded pipeline. A subsequent actual-helper 1 GiB run had variable
filesystem flush latency (serial 0.98–3.23 seconds, overlapping 0.78–2.82 seconds).
Neither is a production speedup claim. Five cold trials of the actual helper on
2 GiB, with the same two-CPU/1 GiB limits, measured median **2.725 seconds serial
versus 2.159 seconds overlapping** (21% lower). Hashes and physical read/write
bytes matched. Process CPU rose from a median 1.58 to 1.75 seconds. Both paths
had substantial filesystem tails: 2.18–7.71 seconds serial, 1.55–8.56 seconds
with overlap. Other local verification ran on the shared machine during this
experiment. The bounded pipeline is retained for its measured median improvement,
with no claim of reduced CPU work or predictable EC2 savings. Production phase
receipts remain the acceptance evidence for actual preparation time.

[Raw copy/hash results](evidence/snapshot-copy-overlap-2026-10-17.json) retain all
trials, including the outliers.

Reproduce with the same bounded local Linux volume:

```sh
python -m tests.benchmark_snapshot_copy --megabytes 2048 --runs 5 \
  --evict --output .cache/snapshot-copy.json
```
