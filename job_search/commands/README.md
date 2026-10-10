# Shared command mechanics

`CommandExecutor.run` checks trusted authority, validates receipt reuse, starts one
SQLite transaction, invokes the owner callback, serializes the response, and commits
state/history/work/receipt together. Serialization failure rolls back everything.
Replaying the same principal/operation/key and input returns the original result;
changing the input conflicts. Private source text should never enter command inputs
that are persisted as receipts.

`CommandContext` comes from the authenticated transport or bounded worker, not a
request body's actor fields. Human decisions cannot be delegated to a model tool.
An agent's direct internal command needs an exact, unexpired human delegation.
Inferred operations remain proposals until reviewed.

Each owner enters `tx.scope(owner)` for its writes. SQLite rejects writes outside
that owner's tables; immutable history and receipts also have trigger protection.
Only command mechanics own receipts and the durable work queue. Business validation,
lifecycle transitions, and provider recovery belong to their respective owners.

Owner schema checksums protect the isolated candidate; neither legacy databases nor
unexpected schema changes are silently adopted. This is not a production migration
or an event-sourcing framework.

Run `uv run python -m tests.test_redesign_commands` and
`uv run python -m tests.test_redesign_boundaries` for focused verification.
