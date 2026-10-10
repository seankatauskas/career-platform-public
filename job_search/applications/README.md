# Applications owner

`api.ApplicationOperations` is the public transaction participant. It creates job
identity on the first operation, preserves browser evidence, and owns accepted
submissions, obligations, interview rounds, assessments, offers, reminders and
progress. `contracts.py` contains typed inputs. Named operations enforce relevant
record versions; notes do not advance an application's disposition version.

The command executor supplies authorization, one transaction, append-only history
and durable handoffs. Operations never commit, call a provider, or write another
owner's tables. The private `_store.py` helpers are not an integration interface.
Use public `get_application`, `get_record`, `list_records`, `stage`,
`preview_correction` and `causal_records` with the runtime's read connection.

`identity.py` owns exact identities, disposition and reviewed combination;
`tasks.py` owns completion and snooze; `interviews.py` owns round scheduling and its
task/reminder consequences; `submissions.py` keeps observations separate from
accepted attempts. `progress.py` derives the displayed stage from current records.
Application workflows coordinate corresponding proposal/action invalidation through
the other owners, and must call those participants in the same outer transaction.

Run `uv run python -m tests.test_redesign_applications` for owner acceptance.
Fixtures cover concurrent creation, exact evidence, rescheduling rollback, snooze,
closure/reopening, and correction after independent edits. No provider is contacted.

Correction discovery is bounded through `affected_by_evidence` and
`affected_by_causation`. A truncated result must block a complete correction;
the workflow must gather its full context before applying changes. Each selected
record binds its own version and explicit keep/retract resolution. Dependent tasks
cannot be silently cancelled by retracting their interview or assessment.

`import_application` and `import_record` require migration worker authority. They
preserve historical IDs, times, state and exact payloads with provenance, append
history, and enqueue nothing. Imported accepted submissions reserve their feedback
identity so later corroboration cannot produce duplicate feedback.

Interview updates/cancellation require `expected_related_versions` from
`preview_interview_change` whenever open tasks or pending reminders will change.
Reviewed combination similarly binds every affected record using `expected_records`
from `preview_combination`. The owner rejects missing, changed, or newly added
dependencies instead of filling versions during execution.
