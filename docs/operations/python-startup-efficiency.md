# Python startup cost

The application image precompiles its Python standard library and application
modules at build time. The caches use `checked-hash` invalidation: Python checks
the source hash before using cached bytecode. `PYTHONDONTWRITEBYTECODE=1` and the
read-only production filesystem remain in place. Pip already compiles installed
dependencies, so the extra standard-library pass excludes `site-packages`.

This matters because Docker health probes start a new Python interpreter every
30 or 60 seconds. The previous image contained no application bytecode caches and
no caches in the standard library's root `__pycache__` directory. Each probe
compiled the same imports again. The probes, their intervals, authentication,
freshness windows and failure behavior are unchanged.

## Measurement

On October 17, 2026, one read-only probe invocation on the production t3.large
used 1.380 CPU-seconds for worker health and 0.825 CPU-seconds for tools health.
These single observations motivated an isolated image comparison; they are not
an estimate of sustained utilization or a production before/after result.

Eleven invocations per command in Linux arm64 Docker on macOS, with a read-only
root filesystem, no network, two CPUs, a 4 GiB memory ceiling and no swap:

| Command | Original median CPU | Precompiled median CPU |
| --- | ---: | ---: |
| `python -m job_search.cloud healthcheck core` | 283.386 ms | 58.232 ms |
| `python -m job_search.tool_service --help` | 163.080 ms | 34.397 ms |

The worker probe used a generated fresh health record. The tools command measures
CLI startup only, not its socket round trip. Each comparison used the same pinned
Python base and dependencies. The experimental cache layer added 7,484,234 bytes
to the uncompressed image. Architecture, CPU and filesystem differ from EC2;
measure production again before converting these savings into host sizing.

Run the reproducible `tests.benchmark_python_startup` module inside each image
under the same container constraints. Candidate Compose acceptance verifies
source-checked caches, immutable ownership and rejection of stale cached code.
The regular Compose health checks still exercise the actual service probes.

Python documents the cache mode in
[compileall](https://docs.python.org/3.12/library/compileall.html).

## Relation to a 4 GiB host

This complements [bounded worker diagnostics](worker-idle-efficiency.md),
incremental ranking and streaming backup compression. The week ending October 17
showed average EC2 CPU of 25.8% after the collector memory fix, above a t3.medium's
20% baseline. Its Standard credit balance could therefore run out even when RAM
fits. Observed application memory also excludes unobserved workload combinations.

Use the [constrained workload benchmark](medium-host-benchmark.md) for regression
evidence, then observe ordinary collection, ranking, backup and interactive work
in production. Preserve latency, OOM, credit and EBS measurements. A synthetic
benchmark does not establish production t3.medium readiness by itself.
