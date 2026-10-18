# Backup archive efficiency

Routine backups validate the globally quiesced capture after services resume.
They previously reread every captured file for its checksum, compressed the whole
tree at gzip level 9, and then reread the completed archive for its checksum.
Those extra reads matter when the catalog is larger than available file cache.

The archive builder now checks each independent captured database, hashes its
bytes as tar consumes them, and appends `backup.json` after all payload hashes are
known. A separate streaming hash covers the compressed output, including gzip's
final trailer. Both hash inputs are the actual bytes passed through their streams;
neither file size nor modification time substitutes for a checksum. No speculative
validation cache is introduced. Changed and unchanged captured databases both
receive the existing SQLite quick check, and full restore validation is unchanged.

The output is the existing self-contained `tar.gz` format. Older extractors do not
depend on the manifest's order within the archive. Database/WAL capture, the
cross-database stop boundary, staged ownership, filesystem synchronization and
archive-before-receipt publication remain unchanged. Archive checks, finalization
and synchronization must all succeed before upload. A partial write, failed
compression, corruption or failed upload cannot advance the local success receipt.

## Compression choice and local measurements

Gzip level 3 balances CPU consumption and archive size in the fictional fixture.
Level 1 was slightly faster but larger; levels 6 and 9 saved relatively little
space for their additional CPU. This choice is not a measured production optimum.

Three trials per case used fresh subprocesses and independent captured files on
macOS ARM64, Python 3.11.16. The 251,999,386-byte fixture contains indexed SQLite
postings, text history and incompressible binary assets. Its contents and ratios
are fictional; no production or private data was copied. File cache was not
evicted. Medians include checks, archive building and synchronization:

| Implementation | Gzip | CPU seconds | Elapsed seconds | Archive bytes |
| --- | ---: | ---: | ---: | ---: |
| Previous separate hash passes | 9 | 1.757 | 1.758 | 58,792,248 |
| Streaming | 1 | 1.019 | 1.026 | 60,654,802 |
| Streaming | 3 | 1.036 | 1.037 | 60,499,859 |
| Streaming | 6 | 1.250 | 1.253 | 58,796,955 |
| Streaming | 9 | 1.713 | 1.717 | 58,793,759 |

The selected level used 41% less CPU and produced a 2.9% larger archive than the
previous pipeline in this fixture. Explicit checksum rereads fell from 310,791,634
bytes to zero; source validation, packing reads and the eventual upload read still
exist. Peak process RSS was about 43 MB for streaming level 3 versus 36 MB for the
previous implementation. Streaming uses bounded buffers, including a 1 MiB tar
copy buffer; memory does not scale with the database's byte size. File manifests
and profiles still scale with the number of files.

Same-level streaming performance on this cached fixture changed little. The
main expected I/O benefit is fewer complete reads when a database exceeds cache,
which requires a separate constrained Linux measurement. These are archive-only
observations: they exclude capture, service stop/start, provider workloads, S3
upload and AWS EBS behavior. They do not establish that the full service fits a
4 GiB instance.

A separate Linux ARM64 run used the existing Python 3.12 Bookworm image, two
CPUs, a 1 GiB memory/swap limit, a Docker volume and no network. It requested
per-file cache eviction and ran the same five cases three times:

| Implementation | Gzip | Median CPU seconds | Median elapsed seconds | Archive bytes |
| --- | ---: | ---: | ---: | ---: |
| Previous separate hash passes | 9 | 2.283 | 3.052 | 58,759,451 |
| Streaming | 1 | 1.426 | 2.203 | 60,623,500 |
| Streaming | 3 | 1.415 | 2.280 | 60,466,276 |
| Streaming | 6 | 1.655 | 2.425 | 58,764,884 |
| Streaming | 9 | 2.163 | 2.925 | 58,759,902 |

Level 3 reduced measured CPU by 38% and elapsed time by 25%, with a 2.9% larger
archive. A single confirmation trial in the existing `career-medium:baseline`
application image under the same limits measured 3.464 → 2.548 seconds elapsed,
2.661 → 1.593 seconds CPU, and 37.5 → 40.5 MB process peak RSS for old level 9
versus streaming level 3. All archives passed full restore verification. Docker
shared the development host; these runs do not measure EBS limits or establish
physical cache residency after advisory eviction.

## Phase evidence

Successful local receipts and command results report `archive_build`,
`compression`, `sqlite_check`, `destination_hash`, `archive_hash`, `sync`,
`archive_upload` and `receipt_upload`. Compression measures gzip writes and
finalization, including compressed-output hashing and writes; it is elapsed time,
not CPU time. The benchmark separately measures process CPU.

These intervals overlap: `archive_build` contains checks, hashing and compression;
`compression` includes compressed-output hashing. Do not add them to derive total
elapsed time. The manifest is appended before archive finalization, so it contains
only timings already known then. The remote receipt includes completed archive
build/sync/upload measurements. Its own upload time is available only after it is
uploaded, in the local receipt and final command output.

## Reproduction and checks

```sh
uv run --python 3.11 python -m tests.benchmark_backup_archive \
  --megabytes 256 --runs 3 --output .cache/backup-archive.json
python3 -m tests.test_backup_archive
python3 -m tests.test_snapshot_pause_boundaries
python3 -m tests.test_job_search_aws_ops
python3 -m tests.test_release_recovery
```

The benchmark bounds fixtures to 4–256 MiB and trials to 1–5. `--evict` requests
per-file `POSIX_FADV_DONTNEED` on Linux after syncing the fictional capture;
unsupported platforms report that eviction was unavailable. It never drops the
host's global caches. Each archive receives independent checksum, extraction,
payload-equality and full SQLite restore verification outside measured work.

Regression tests cover streamed/archive hash equality, manifest-last restoration,
corruption rejection, growing/truncated input, partial archive writes, failed gzip
finalization, failed sync, failed receipt upload, unchanged previous success and
unsafe archive members. Existing subprocess-death and WAL/hot-journal tests keep
covering capture and publication boundaries.

For the later 4 GiB qualification, use a separate recovery-drill host/volume,
verify a complete restore, and keep all external automation disabled. Run the
same archived catalog through baseline/candidate backup builds and full restores,
alongside controlled local dashboard and sparse-ranking activity. Record available
memory, anonymous/file-cache memory, OOM events, EBS latency/IOPS/throughput, CPU
credits, archive size and per-phase timings. A short burst-only run or warm
small fixture cannot establish sustained medium-instance capacity.
