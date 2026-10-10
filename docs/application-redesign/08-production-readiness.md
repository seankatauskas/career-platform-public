# Production integration and release gate

Production uses `application_backend: "owners"` following the completed
2026-10-10 UTC cutover. The bound owner database is authoritative; old application
writers are fenced and there is no active legacy fallback. The local walkthrough
remains a separate fictional installation.

## Production cutover record

Release `0a0fc49ffc7bdd3aeb927db60cd0c8eee3344e4b-53` was prepared and deployed
successfully: [Prepare AWS release, run 38008945063](https://github.com/seankatauskas/career-platform/actions/runs/38008945063)
and [Deploy prepared AWS release, run 38009588549](https://github.com/seankatauskas/career-platform/actions/runs/38009588549).
Post-cutover inspection recorded:

- **53 applications**, **zero conversion blockers**, and all legacy writer fences
  present; zero enabled retired schedules and zero active retired work.
- Owners active at installation revision **1**, with **zero uncertain effects**
  and **zero restore quarantine**. The verified backup taken while writers were
  stopped is retained privately.
- The configured inbox and existing watermark were preserved. All email in that
  scope reaches Understanding, with an **8,192-output-token budget**. The old
  recruiting filter is explicitly false and is ignored by the owners backend.

No live test emails were sent or calendar actions performed. Provider execution
remains `configured_unverified` / `exact_approval_required`; activation does not
verify provider effects or authorize a particular action. The remaining code cleanup
is deletion of unused legacy implementations, not removal of an active fallback.

## What is connected

| Boundary | Production entrypoint | Ownership |
| --- | --- | --- |
| Dashboard, browser observations, job saves and submissions | `system.py`, `application_gateway.py`, `dashboard.py` | Applications gateway; existing authentication and CSRF |
| Resume selection and submission snapshot | `resume_lab/gateway.py` | Applications lock first, operational queue second |
| Agent tools | `application_agent_host.py` | Owner tools plus an explicit list of retained job/review/resume capabilities |
| Email capture and interpretation | `application_mail.py`, `mail/revision_archive.py` | Account-scoped immutable encrypted evidence; durable model-lane work |
| External execution and notifications | `application_execution.py` | Exact approval, installation fence, provider account binding, recovery |
| Submission feedback | `application_feedback.py` | Captured posting/provenance, destination receipt, then owner acknowledgment |
| Runtime and installation | `application_production.py`, `application_installation.py` | Composition, queue bridging, operator controls; no inferred business decisions |

The operational `application_db` still holds collection schedules, worker leases,
mail staging/archive, browser pairing, reviews, and shortlist data. The separate
`application_owner_db` holds the authoritative application modules. Opening either
database with the wrong schema owner is rejected. SQL fences in the operational
database reject old application writes even through callers that bypass the UI.

New configuration fields:

```json
{
  "application_backend": "owners",
  "application_owner_db": "/var/lib/job-search/application-owners/candidate.sqlite",
  "outlook_home_account_id": "<the explicitly selected Microsoft account identity>"
}
```

Keep the operational database path unchanged. The owner directory and its
`predecessor.sqlite` and `conversion-report.json` must live together under the
persistent state volume. Keep account identity separate from the logical
`outlook_account_id` label. It is checked before provider reads and writes.

`compose.applications.yaml` is selected only for the owners backend with a configured
mail profile. It gives the model worker the dedicated mail profile and archive key.
The remote-mail permission and 8,192-output-token budget remain explicit. The new
Understanding profile is selected independently of legacy local classifier, temporal,
and status-only model settings. Attachments remain visible incomplete evidence
until their text is processed through a supported evidence adapter.

The owners backend ignores `mail_recruiting_only` and legacy classifier-selection
settings. All mail within the configured folders and new-message watermark reaches
Understanding, including newsletters and verification messages. Only Understanding
decides relevance. Intake preserves quoted context and records incomplete coverage
where content cannot be processed. Connector scope and explicit remote-model
permission still apply; provider binding uses the exact selected account.

The same precedence applies outside mail:

- Applications defines document editing through open/closed disposition; accepted
  submissions retain their immutable document snapshots.
- Saved and note-only applications remain eligible for job review. An accepted
  submission in the current pursuit or a closed outcome excludes them.
- Browser evidence does not automatically promote every capture into profile facts
  merely because an application has a submission. Profile review remains explicit.
- Owner reminders use their exact accepted instruction and installation activation.
  Legacy attention controls cannot cancel or authorize them. Unrelated shortlist
  notification controls retain their own scope.
- Health and agent queries use owner state. Retired queue entries cannot be retried
  through generic recovery; unresolved provider outcomes remain visible as history
  requiring reconciliation. Success for an independent job cannot hide a failure.

## Local verification

Run from the repository checkout, using fictional data:

```bash
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --browser
uv run --with cryptography --with pypdf python scripts/release-transition-acceptance.py --local
```

The transition fixture now tests conversion, the real runtime composition, rejection
of old writes, an accepted new note, preserved predecessor bytes, and paused restore.
The `--local` result is development evidence. The release workflow also requires the
Docker transition receipt for the exact committed image and reviewed predecessor.
The compatibility label changes because old application binaries are not a supported
rollback target after owner activation.

Regression tests include replacement-database rejection, stale conversion rejection,
resume/submission serialization, account isolation, independently readable revisions,
provider usage deferrals, exact approval and lease fences, uncertain-send recovery,
notification recovery after retries exhaust, and backup of read-only SQLite files
with committed WAL data.

## Rehearsal with current data

A private local rehearsal used the verified production backup from
2026-10-09 20:30 UTC. The compressed backup and original operational snapshot were
preserved; only separate local copies were bound. No production writer was stopped
or changed. No provider credentials were loaded and network calls were prohibited
while exercising the application composition.

Results:

- All **53 applications** converted with **zero blockers** and valid foreign keys.
  Conversion queued no runtime effects and kept external execution paused.
- **42 confirmations** retain an unresolved browser-attempt link, and **7 legacy
  automatic finalizations** remain unreviewed captures. Their unknown click times,
  answers, and document associations were not invented. One narrowly proven
  historical reassociation was preserved with correction provenance. These 50
  warnings remain in the report and application records; they are not erased.
- Every application workspace loaded through the production dashboard controller,
  including both list pages. Agent reads, resume-standard reads, and core/model
  worker composition succeeded without starting workers or provider calls.
- A local note survived reopening the production composition. Old SQL writes and
  stale legacy configuration were rejected. Existing review, shortlist, and browser
  pairing records remained unchanged in the copied operational store.
- Backup/restore preserved the predecessor bytes, paused execution, and required
  operator review. The original operational snapshot remained unchanged.

This establishes conversion and local host behavior for that snapshot. It does not
verify decrypted historical mail, resume artifact downloads, live model behavior,
Microsoft Graph permissions, or real notification delivery. The earlier complete
suite and production-architecture Docker transition passed; exact final revision
receipts and CI results are recorded in the readiness PR.

The user authorized production cutover and declined a separate live integration
pilot. A designated test mailbox and recipient are therefore not release gates.
The unverified live behaviors above remain limitations of the available evidence.
Required automated release checks still run. Do not send test messages or create
calendar entries without exact authorization merely to complete cutover.

## Cutover procedure

Use the repository's Terraform/GitHub release workflow and
[deployment guide](../operations/aws-deployment.md). The user has authorized these operator steps. Complete them as one coordinated
release with distinct recoverable phases; do not request a second activation
approval. The production record above documents the completed cutover; the
procedure remains here for reference and recovery planning.

1. Stop and drain every old application writer, including dashboard and workers.
   Preserve a verified backup and the installed release identity.
2. Convert the final consistent operational snapshot. Do not permit old writers to
   resume while preparing the new installation. Rehearsal conversion is not reused
   if application state has changed.
3. Configure the separate owner path, then run `application-installation bind`
   with the exact report and operator identity. Binding verifies the stored manifest,
   current domain digest, source application tables, and predecessor bytes. It holds
   the owner lock while installing the old-writer fences and quarantining old work.
   A failure before commit leaves the old database unbound. A crash after binding
   leaves old writers fenced and external execution paused.
4. Select the owners backend, configure the exact home account identity and new
   Understanding profile, and set `mail_recruiting_only: false` to make the intended
   configuration explicit (owners already ignore it). Preserve the configured folders
   and watermark. Start the reviewed release. Inspect
   `application-installation status`. Startup verifies the bound installation identity;
   replacing a database at the same path is rejected. Review application state through
   the normal dashboard's Applications workspace.
5. Within the same authorized cutover, activate external execution with an exact
   current revision, operator,
   and reason. Normal application commands and agent tools cannot activate it.
   Check queue age, processing failures, uncertain effects, and pending consequences.

Operator CLI examples, with the already prepared private runtime config:

```bash
python -m job_search --config /run/job-search/config.json application-installation bind --report /var/lib/job-search/application-owners/conversion-report.json --operator <operator>
python -m job_search --config /run/job-search/config.json application-installation status
python -m job_search --config /run/job-search/config.json application-installation activate --expected-revision <revision> --operator <operator> --reason <verified-release>
python -m job_search --config /run/job-search/config.json application-installation pause --expected-revision <revision> --operator <operator> --reason <incident>
python -m job_search --config /run/job-search/config.json application-installation acknowledge-restore --expected-restore-revision <revision> --operator <operator> --reason <reconciliation-record>
```

Pending old Telegram review buttons are retired at binding. They cannot approve new
owner operations. New approvals are made through the authenticated Applications
workspace; the old ingress returns an explicit conflict for stale buttons.

## Recovery

Pausing blocks new provider write intents; an already in-flight request still needs
reconciliation. Unknown outcomes are never interpreted as proof of failure. Retry
batches retain the same action/delivery identity and use persisted receipts. Exhausted
notification batches can reconcile on a later hourly batch or activation revision
without blindly sending again.

Restore verifies the predecessor hash, preserves its read-only mode, and increments
the installation fence while pausing external execution. It also requires a separate
operator review and quarantines actions and reminders pending in the snapshot: an
effect may have happened after that snapshot. Acknowledging the review does not
release quarantined sends or activate execution. Preserve those records for positive
provider/receipt reconciliation; create replacement work only after resolving the
original outcome. Ordinary SQLite files use a
consistent SQLite backup even when their filesystem mode is read-only; mode alone is
not proof that WAL data is absent.

Single-database portable rekey export and the separate state-transfer command reject
owner installations. They cannot currently transfer the owner database, installation
binding, and immutable historical archive together. Use the complete installation
backup with its matching encryption key; rekeying only the operational archive would
make historical correspondence unreadable. This restriction also applies when an old
configuration points at an already-bound operational database.

After new state or external effects exist, do not restore an old pre-cutover database
or select the old backend as a shortcut. Preserve the new database, pause execution,
reconcile effects, and repair forward. The release policy deliberately provides no
automatic rollback edge to the old application design.
