# Interactive and batch workers

The cloud deployment runs two execution partitions within each existing `core`
and `model` queue lane. Mail, briefings, approved application actions and outbox
handling can progress while collection or ranking is executing on the same host.
Each process still executes one task at a time.

| Service | Lane | Partition | Work |
| --- | --- | --- | --- |
| `core` | core | batch | ATS discovery/collection, location refresh, shortlist evaluation |
| `model` | model | batch | Ranking, salary batches, resume optimization |
| `interactive-core` | core | interactive | Mail sync/dispatch/projection, briefing scheduling, application work, notifications and outbox delivery |
| `interactive-model` | model | interactive | Mail understanding, briefing composition and reply preparation |

Interactive workers check for work every 15 seconds when idle. Batch workers keep
the existing 300-second interval and bounded catch-up behavior. Scheduling remains
transactional and coalesced, so either core partition can materialize due schedules
without creating duplicate work. Automation switches and future retry times still
exclude work in every partition. This change does not enable disabled capabilities.

Long-running cloud ticks use compact counters. The interactive core worker refreshes
its diagnostic dependency snapshot every five minutes; execution checks remain in
the task handlers. See [worker idle cost](worker-idle-efficiency.md) for measurements
and the compatibility contract.

`BATCH_TASK_KINDS` in `job_search/worker.py` is the execution routing policy. New
kinds use the interactive partition unless added there. Add long catalog or document
jobs explicitly and test their placement. Persisted lane assignment continues to
define runtime composition and workflow lineage; task producers do not need a new
argument. The local CLI and launchd default to `--partition all` and retain their
existing behavior.

## Ownership and compatibility

Each partition holds its own exact-token SQLite worker lease, and individual work
claims retain their existing transaction and lease tokens. A compatibility guard
under the old lane lease name remains active until the latest partition lease
expires. This blocks predecessor binaries, which know only that original name.
A live unpartitioned lease blocks both new partitions. A delayed partition heartbeat
or release cannot overwrite a subsequently acquired unpartitioned lease.

No schema or queued-row migration is needed. Existing queued/retry records keep
their IDs, lanes, payloads, dedupe keys, lineage and attempt counts, and become
eligible for the corresponding partition immediately. Expired running records
are recovered only by their partition, through existing external-outcome and
attempt-limit checks. Shared inference reservations, daily limits and in-flight
limits still use the same application database; adding a process does not multiply
the allowance. Saturated provider concurrency can still defer interactive inference.

The workflow health report exposes `worker_partitions` under each lane. Its `all`
entry can be the compatibility guard (`owner: partitioned-workers`); the batch and
interactive entries identify the executing processes. The existing `worker_lease`
summary remains available.

## Deployment, drain and rollback

Use the normal coordinated release workflow. The new services have independent
health records and `healthcheck` / `draincheck` identities matching their Compose
service names. All four workers observe the maintenance gate, finish their current
task and acknowledge drain before state capture. The operations layer discovers
services from the selected release, so a predecessor without interactive services
can still be inspected, stopped and restored.

Rollback drains all workers and restores the earlier release using the existing
release procedure. Its unpartitioned workers can consume the same persisted
queues. Do not hand-edit lanes or purge leases to accelerate a transition.

Dedicated mail and briefing profiles are mounted explicitly into the interactive
workers that use them. The core batch service retains its mail profile for the
existing one-shot aggregate readiness command. The interactive model service gets
the mail archive key only through the owners-backend overlay. MCP and the
networkless document-tool service retain their existing mounts.

Each new interactive container has a 768 MiB memory ceiling, 128-process ceiling,
one-CPU ceiling and a higher CPU share when competing for CPU. These are ceilings,
not reservations or a latency guarantee. All workers still share the host, SQLite
files and providers; inspect task memory, DB contention and inference deferrals
alongside queue wait after deployment. In particular, measure another representative
week before downsizing the host.

```bash
uv run --python 3.11 python -m tests.test_worker_partitions
uv run --python 3.11 python -m tests.test_worker_catch_up
uv run --python 3.11 python -m tests.test_job_search_cloud
```

The partition suite blocks ATS and ranking in real concurrent worker threads while
mail and briefings finish, verifies predecessor lease exclusion and expired-lease
handoff, preserves disabled work and backoff, checks shared inference limits, and
exercises drain/shutdown. Native Compose acceptance starts and checks all four
services and recovers an interrupted interactive work item before the normal
backup/restore checks.
