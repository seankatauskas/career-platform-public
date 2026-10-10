# Agent execution

The implementation uses an isolated integration worktree on
`redesign/application-lifecycle`, based on `7a6fd17`. The user's original checkout
is preserved. Design files were imported explicitly; uncommitted runtime changes
were not imported. No production settings, database, mailbox or provider is used.

## Ownership

| Agent | Exclusive implementation scope |
| --- | --- |
| Main | Shared commands, cross-owner workflows, query assembly, runtime/transports, migration registration and conversion, architecture/acceptance tests |
| Applications | Application identity, submissions, notes, tasks, interviews, assessments, offers, reminders and their owner tests |
| Evidence | Correspondence and Applications/Understanding, analysis/proposal tests |
| External Actions | Action proposal/approval/execution/reconciliation and owner tests |

Each worker has a separate worktree. Shared contract changes come through Main.
Main integrates focused commits, tests the combined source and assigns repairs to
the owning worker. Workers never modify shared files or merge one another's work.
No implementation task authorizes release, live data conversion or provider effects.

## Gates

1. Shared trusted command contracts and executable ownership checks.
2. First activity creates an application once through the public operation.
3. Preserved evidence becomes reviewed requests and tasks.
4. Lifecycle workflows and exact external effects deliver durable results.
5. Corrections, combination and transport compatibility reach the same owners.
6. Offline historical conversion, integrated verification and a paused handoff.

Every handoff includes its commit, changed contracts, acceptance evidence and known
limitations. The acceptance matrix in document 05 remains authoritative.
