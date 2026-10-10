# External Actions

This owner stores exact proposals, human decisions, execution checkpoints and
provider results. It does not import Applications or change lifecycle/task state.
Use `api.ExternalActionOperations` within a shared command transaction. Queries
accept the caller's read snapshot. `SCHEMA` belongs to `external_actions`.

`prepare_reply` and `prepare_calendar_change` accept `ActionEnvelope` or the same
named fields. Reply payloads contain exact `recipients`, `subject`, `body`; targets
contain internal `message_id`/`source_hash` plus the separate immutable
`provider_message_id` and `provider_source_hash` computed by `reply.source_digest`.
These bind correspondence evidence and remote preflight independently.
`preparation.prepare_reply_context` obtains the provider hash and exact recipients
through an injected read-only client outside the command transaction. It returns
visible blockers instead of inventing missing context. Composition rechecks the
captured correspondence/association/application versions when saving the proposal.
Calendar payloads contain `starts_at`, `ends_at`, optional `location` and `join_url`.
Creation targets must be `{}` and mean the approved account's **default calendar**;
selected calendar routing is not supported. Extra target fields are rejected, so
an approved calendar identifier can never be silently ignored. Deletion payloads
are empty because deletion has no editable event fields.
Updates/deletion require `remote_id`, `transaction_id`, `etag` backed by this
owner's verified creation receipt. No attendees or employer invitations are writable.

`authorize_action(tx, action_id, expected_digest, applicability=...)` checks human
authority and binds the full immutable envelope for at most fifteen minutes.
System composition supplies `applicability(tx, envelope)` using public owner APIs.
The same callback runs in `begin_write` after provider preflight and before each
effect. A false result or typed owner conflict persists cancellation before any
write. Approval-time conflicts leave the proposal unapproved. Reconciliation checks
the provider account again before reading remote evidence. Closure/corrections call
`invalidate_context`; combination checks
`has_unresolved`, which includes imported uncertain history.

`ExternalActionWorker` keeps network I/O outside transactions. Its context factory
takes `(operation, unique_key)` and returns a trusted worker context granting only
that operation. Worker operations are `claim_action`, `begin_write`,
`finish_attempt`, `reconcile_result`, `expire_actions`, and `acknowledge_result`.
Use `expire_actions` to invoke `expire_leases`; lost leases never reset durable
effect intent. Retryable preflight failures back off from thirty seconds to five
minutes with a five-attempt ceiling. Work records include `available_at`; claims
enforce it even when a scheduler delivers early.

Every ambiguous effect is reconciled before retry. Reply creation, body update,
send intent and Sent confirmation are distinct checkpoints. A draft or accepted
send response cannot complete a task. Calendar creation preserves a stable provider
transaction ID, and updates/deletion require verified private ownership and ETag.
Failed deletion with no receipt remains uncertain; a later missing item is not
enough to establish whether deletion or access loss occurred.

Successful results and pending task consequences commit together. Applications
consumes `get_result` via a named workflow, then invokes `acknowledge_result` in
the same transaction with `applied` or `conflict`. Delivery conflict preserves
provider success. Exact consequence fields are `operation: complete_task`,
`task_id`, `expected_version`, `completion_rule: verified_send`.

The isolated candidate still dispatches only explicitly enabled test providers.
Production composition additionally requires persistent `activation_status()` and
`activation_guard(tx, expected_revision)` callbacks. Every claimed production
attempt preserves the activation revision in `action_activation_fences`; renewal
and `begin_write` compare it in the same transaction as the effect checkpoint.
Pausing or pausing and resuming during preflight prevents that attempt's write.
Startup and restored state remain paused until the operator enables dispatch.
Configuration alone does not grant that authority, and imported old approvals
remain inert.

`ConfiguredOutlookProvider` binds a logical mailbox to the explicitly selected
MSAL home-account identity. The callback is checked before preparation, preflight,
execution and reconciliation. Its Sent-folder resolver is lazy and reads through
the same account. Read-only Graph transient failures become pre-effect retries;
write exceptions remain uncertain. Production uses 180-second action leases,
renewed around each bounded provider step, and retains the core worker heartbeat.
An expired or lost claim never permits replay of a persisted write intent.

`application_execution.ProductionExecution` exposes core handlers
`application.dispatch`, `application.execute_action`,
`application.reconcile_action`, and `application.deliver_owner_notification`.
Dispatch materializes bounded, paginated `FollowUpTask` records. The operational
worker acknowledges the original owner work only when `work_complete` is true.
Pending owner work gets a fresh operational retry batch each hour or after an
activation revision change. Exhausted batches keep their error history; owner
fences and stable Hermes delivery IDs prevent those recovery batches from
repeating effects.
Reconciliation has a fresh five-minute round key so unresolved effects continue
to receive read-only checks without resending. Notification handoffs use exact
Hermes receipt IDs and payload fingerprints; uncertain delivery needs positive
reconciliation before another send. Delivery receipts never complete tasks.

`SCHEMA_MIGRATIONS` exports exact `(owner, old_checksum, new_checksum)` registry
entries for the command executor. Register it when opening an older owner database;
unknown schemas fail closed.

Restores require an operator review before reactivation. The restore hook records
immutable quarantine identities for nonterminal actions and pending reminders;
acknowledgment permits new work but never releases those historical records for
provider writes. Their approval/attempt history stays intact. Read-only action
reconciliation and positive notification receipt recovery remain possible after
review and activation. A new effect requires a separately reviewed new action or
reminder; do not interpret the missing snapshot outcome as proof of nonexecution.

Run `python3 -m tests.test_redesign_external_actions` for offline failure-boundary
and provider-contract fixtures and `python3 -m tests.test_redesign_execution` for
activation, account binding, dispatch and notification recovery. Graph behavior reuses existing narrow
`GraphOutlookClient` APIs; these tests make no live provider calls.
