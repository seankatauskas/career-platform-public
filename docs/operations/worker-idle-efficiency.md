# Worker idle cost

Long-running cloud workers poll at their existing intervals, but their tick output
contains only this tick's claim, recovery, completion and remaining-work counters.
They no longer rebuild the historical automation report on every poll. The detailed
report remains available through the existing diagnostic and readiness interfaces.
Direct `Worker.tick()` / `Runtime.tick()` callers and an explicit cloud `--once`
retain the detailed output by default.

The interactive core worker publishes its dependency snapshot on its first tick
and then every five minutes, using a monotonic deadline. The batch core worker does
not duplicate it. An unsplit cloud core worker uses the same five-minute cadence.
Process restart resets the deadline, so new configuration is observed immediately.
An explicit core `--once` also observes dependencies.

A failed local notification bridge observation schedules at most three earlier
observations, after 30, 60 and 120 seconds on the next normal worker tick. Production
interactive core polls every 15 seconds, so a bridge that starts just after the
initial snapshot can be observed at the 30-second refresh. Persistent failures
return to five-minute refreshes; successful or disabled observations reset the
retry budget. An unsplit worker with a longer tick interval still waits for that
tick. These observations use the existing local configuration/socket checks;
they do not call a model, send a notification, or bypass execution checks. The
snapshot remains blocked until another observation changes it.

This snapshot describes diagnostics; it does not authorize a task. Provider,
credential, activation, artifact and lease checks in task handlers still run.
Snapshot validity remains 15 minutes. A changed dependency may take up to five
minutes to appear in the diagnostic snapshot; stopped or blocked workers do not
keep a snapshot artificially fresh. The dependency/readiness endpoints retain
their existing unavailable and expired states. Normal deployment of configuration
and secrets restarts the workers.

## Reproduce the offline measurement

```bash
uv run --python 3.11 python -m tests.benchmark_worker_idle --rows 11000 --output .cache/worker-idle-11000.json
uv run --python 3.11 python -m tests.benchmark_worker_idle --rows 100000 --output .cache/worker-idle-100000.json
```

The benchmark creates a temporary SQLite database containing completed work and
job-run rows across 20 fictional task kinds. Results contain 1,024 fixture
characters each. Seven alternating detailed/compact ticks measure process CPU,
wall time and serialized report length. It never reads application data or calls
a provider. Temporary state is deleted afterward.

On macOS arm64, Python 3.11.16:

| Completed work and run rows | Detailed CPU / wall | Compact CPU / wall | Report bytes, detailed / compact |
| --- | --- | --- | --- |
| 11,000 each | 99.793 / 99.855 ms | 5.163 / 5.211 ms | 28,995 / 325 |
| 100,000 each | 974.154 / 974.411 ms | 5.073 / 5.125 ms | 28,997 / 325 |

These are local fixture medians, not production CPU savings or an EC2 sizing
measurement. They exclude dependency probes, container health checks and active
work. Removing the report reduced CPU for this idle path by about 95% at 11,000
rows and 99.5% at 100,000. The growing historical query cost no longer runs every
15 seconds in both interactive workers. Dependency checks independently fall from
up to 240 per hour in interactive core, plus batch checks, to approximately 12.

The focused suite verifies that compact ticks still execute tasks, report pending
work, preserve dependency failures and retain legacy detailed output. It checks
15-second polling alongside five-minute dependency refresh, restart behavior,
unsplit workers, batch exclusion and explicit one-shot inspection:

```bash
uv run --python 3.11 python -m tests.test_worker_idle_efficiency
uv run --python 3.11 python -m tests.test_worker_catch_up
uv run --python 3.11 python -m tests.test_worker_partitions
```

## Routine readiness cost

Dashboard `/api/v1/ops/readiness`, MCP health and `readiness --monitor` omit detailed
ranking policy scans. Explicit CLI `readiness` and the dashboard's separate
pipeline/progress data retain those details. No ranking activation changes are
needed to use the cheap diagnostic path.

```bash
uv run --python 3.12 python -m tests.benchmark_readiness --rows 425000 --description-bytes 1024 --output .cache/readiness-efficiency.json
```

On macOS arm64/Python 3.12, seven warm-cache fixture samples over 425,000 fictional
rows (585 MB SQLite catalog) had median detailed readiness of 74.728 ms wall / 74.729
ms CPU and routine readiness of 3.955 ms wall / CPU. The fixture has no trained
ranking artifacts or saved scores; it demonstrates avoiding the catalog scan,
not production endpoint latency. A dependency observation with a missing local
bridge socket and other optional dependencies absent took 0.061 ms wall / CPU.
That number does not represent configured toolchain/credential checks or socket
timeouts. Recovery adds at most three early observations per failure sequence;
it never adds probes to each 15-second tick. Measure production endpoints after
release before claiming actual host savings.
