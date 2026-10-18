# Offline mixed-work capacity benchmark

Run the synthetic fixture before considering a smaller host:

```sh
python -m tests.benchmark_medium_host --families 3000 > medium-small.json
```

The default is bounded to 3,000 fictional postings, approximately 4 KiB of
description text each, and a 16 MiB snapshot payload. All databases are generated
in a temporary directory and removed afterward. It accepts no existing database,
credential, provider, or user-data path. No collection requests, model downloads,
training, inference, AWS calls, or service lifecycle operations run.

Use a locally available application image for the shared-budget experiment. Build
or obtain the image before the offline run. This example uses the local
`career-medium:baseline` tag; record the image digest with the results.

```sh
docker image inspect career-medium:baseline --format '{{.Id}}'
docker run --rm --network none --memory 4g --memory-swap 4g --cpus 2 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --workdir /workspace --env PYTHONPATH=/workspace \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python \
  career-medium:baseline -m tests.benchmark_medium_host \
  --families 50000 --snapshot-mib 64 > medium-50k.json
```

Run from the repository root. The read-only source bind includes the benchmark
module, which is intentionally absent from production image packaging. The
container's writable temporary directory holds the synthetic databases; allow
disk space for the catalog, independent snapshot source and destination, two score
states, indexes, and WAL. The output reports final fixture disk usage. Equal
memory and memory-swap limits disable container swap. The parent, interactive
child, snapshot child, and their threads share **one** 4 GiB / 2 CPU cgroup budget.
Do not run each workload in a separately limited container.

After the smaller fixture passes, use `--families 350000 --snapshot-mib 128` for a
catalog of hundreds of thousands of postings. The maximum is 400,000 families;
`--description-bytes` accepts 128–65,536 and `--snapshot-mib` accepts 1–1,024.
Large runs deliberately perform a full rebuild as a correctness oracle and can
take minutes and several gigabytes of disk. Run one experiment at a time and keep
the complete JSON and command exit status. Do not hide a nonzero Docker exit
behind a successful shell pipeline. A killed container may produce no JSON.

## What is exercised

1. Real bounded collector persistence in batches of at most 2,000 postings.
2. Full family preparation, feature construction, and two-policy score storage.
3. An unchanged ranking refresh, which must check zero families and preserve scores.
4. An authoritative-style observation update across the catalog, one metadata edit,
   and one description edit. Ranking must check exactly two families. Snapshot
   capture and integrity validation run in another process concurrently with this
   work, using an independent frozen copy of the fictional catalog.
5. Full family preparation and a fresh score database as the parity oracle. Family
   membership, templates, fingerprints, and all score values must match incremental
   results. Selected posting columns and snapshot payload hashes must match their source.

The scorer uses the existing sparse policy test contract with input-dependent
deterministic predictions. It does **not** measure scikit-learn model inference,
provider latency, or ranking quality. Hermes, PDF rendering, and production
sidecars are absent. Fixture instrumentation uses plain functions
rather than Mock call histories that would retain the entire catalog in RAM.
Collection uses actual persistence and trigger code, but no ATS response buffering
or network parsing. Snapshot coverage is capture plus SQLite validation and content
verification; it does not include archive compression, S3 upload, or service drain.
The frozen snapshot fixture avoids claiming atomicity across concurrently changing
production databases. Each fictional posting has a distinct title and family;
duplicate-heavy grouping and historical lineage distributions require separate tests.
Snapshot work starts before collection and may finish before ranking starts. The
mixed phase reports monotonic stage intervals and actual snapshot overlap seconds
with collection and ranking separately. A mixed-phase result does not establish
simultaneous snapshot and ranking pressure when that overlap is zero.

An interactive child commits an indexed request update and verifies its read every
100 ms. Its latency includes scheduling delay. The report includes an idle baseline
and p50, p95, and maximum latency by background phase. This is a contention probe,
not HTTP/dashboard latency, workflow execution, or an inference SLA. Very short
phases may have no probes; absent samples do not establish low latency.

## Reading the report

Each phase records wall time, parent process user/system CPU, and process lifetime
peak RSS. The snapshot and interactive children report their own CPU/RSS. The
whole-run CPU total adds those three process profiles once; nested phase counters
must not be summed. This excludes interpreter startup before instrumentation.
`two_vcpu_average_percent` divides that measured CPU by two times whole-run wall
time. It is benchmark utilization, **not** a forecast of daily CPU or T3 credits.

On Linux, one-second sampling records process memory, minimum host available
memory, cgroup memory/cache and limit, and OOM event counters. Missing counters
are omitted. Sampled maxima can miss short spikes; RSS and cgroup lifetime peaks
may predate a phase. Host available memory describes the Docker host or VM, not
the container budget. Cgroup memory includes children and charged file cache.
macOS native runs retain CPU/RSS metrics but usually have no Linux memory counters.
Sampling is capped at 3,600 observations per phase; a report whose
`sample_limit_reached` flag is true cannot establish memory peaks or detect OOM
counter changes for the remainder of that phase. Do not accept a run longer than
this approximately one-hour sampling window as complete memory evidence. Use an
external deadline for large experiments and retain timeout attempts alongside
completed results. Reaching a wall-time cutoff alone does not show a resource failure.

A changed OOM event counter, child failure or resource kill, SQLite error, or
incorrect parity fails the run. `ok: true` means the synthetic assertions passed;
it does not impose a latency or memory-headroom acceptance threshold. Review those
metrics explicitly. A 4 GiB Docker limit does not reserve space for an EC2 guest OS,
Docker daemon, production sidecars, other services, or production growth. CPU quota
also does not reproduce T3 CPU-credit throttling or host storage performance.
The report records the OS and machine architecture: an ARM64 Docker run on a
developer machine does not predict native x86 EC2 execution time. Record image
architecture and digest, host hardware, and the exact invocation with each result.

Use this evidence alongside sustained production CPU credits, memory headroom,
actual mixed-work timings, and end-user latency before resizing. A passing fixture
alone does not establish that t3.medium is sustainable.
See [capacity qualification](capacity-qualification.md) for production observations
and the remaining gates before resizing.

## Recorded large fixture

On October 17, 2026, the final harness completed 300,000 distinct postings/families,
2 KiB descriptions and a 128 MiB snapshot payload in 333.9 seconds. It used Linux
arm64 Docker, Python 3.12.14 and SQLite 3.40.1, sharing the 4 GiB/two-CPU budget
above with swap and networking disabled. All three parity checks passed.

| Measurement | Result |
| --- | ---: |
| Full ranking, 300,000 families checked | 129.06 s |
| Unchanged ranking, zero families checked | 18.4 ms |
| Two-edit incremental ranking, two families checked | 4.98 s |
| Actual snapshot overlap with incremental ranking | 4.99 s |
| Interactive database latency, overall p95 / maximum | 22.3 / 611.5 ms |
| Interactive database latency, mixed phase p95 / maximum | 12.3 / 157.4 ms |

The cgroup's lifetime peak charge was 3,879.8 MiB of 4,096 MiB. Sampled file-cache
and anonymous-memory peaks were 3,612.6 and 908.7 MiB respectively; these peaks
need not occur together and must not be added. Parent lifetime peak RSS was
897.7 MiB. Recorded OOM counters stayed zero. File cache is reclaimable, so the
peak charge is not an anonymous-RAM requirement. This container budget still does
not reserve memory for the guest OS, Docker or excluded production services.

An earlier attempt hit a 300-second external deadline and produced no completed
receipt. The successful final attempt had a 720-second limit and included the
distinct-write and measured-overlap corrections. These are synthetic ARM results,
not native x86 EC2 performance or a sustained T3 credit forecast.

Verification:

```sh
python -m tests.test_medium_host_benchmark
python -m tests.test_collector_streaming
python -m tests.test_policy_refresh
python -m tests.test_job_boards
```
