# Secure Outlook mail and scheduling stream

The core worker composes this module without exposing Graph tokens, archive keys, raw
attachments, or message bodies to task payloads/results. The existing Outlook schedule
now runs all-folder history synchronization and secure ingestion. Launchd and Hermes
remain separate process boundaries.

## Durable mailbox coverage

`GraphMailClient.list_folder_tree()` enumerates top-level, hidden, and child folders
with page, folder-count, and cycle bounds. Discovery resolves the well-known
`junkemail` and `deleteditems` IDs independently. `SQLiteOutlookState` persists only
folder IDs, parent IDs, hidden/exclusion flags, and discovery revisions—never custom
folder names. Junk, Deleted Items, and every descendant are excluded.

`OutlookMailCoordinator.sync_all_history()` uses query version 2 and an unfiltered
messages delta for every eligible folder. Its cursor is independent of the retained
query-version-1 Inbox/90-day compatibility behavior. Page changes are staged before a next link is
checkpointed, terminal delta links are committed only after their changes, replay is
idempotent, and a 410 resets only the affected folder cursor. Callers drain those rows
with `process_pending(query_version=ALL_HISTORY_QUERY_VERSION)`. Removed/moved messages
that were archived remain represented there rather than being cascade-deleted.

## Encrypted content

`KeychainArchiveKeyProvider` stores one random 256-bit key through encrypted
`msal-extensions` persistence and refuses non-macOS or plaintext persistence. The key
does not enter SQLite. `EncryptedMailArchive` applies AES-256-GCM with a fresh 96-bit
nonce and identity-bound canonical AAD. SQLite stores ciphertext, nonce, digests,
bounded sizes, and non-sensitive identity metadata. Authentication or digest failure
is fatal; there is no plaintext fallback.

Full message text is HTML-stripped, Unicode-normalized, control-filtered, and bounded
to 1,000,000 characters before encryption. Existing `mail_evidence.excerpt` behavior
remains exactly 2,048 characters. `SecureMailIngestor` may be injected into the current
coordinator to archive recruiting and non-recruiting messages while preserving that
review contract. Eligible PDF, DOCX, and ICS attachment text is encrypted for both;
only recruiting content is sent to temporal extraction.

## Mail classification and manual review

Mail proposals must cite the exact retained subject or body text and its character
offsets. The remote adapter resolves a unique verbatim quote locally instead of
trusting model character counts. If a model changes only whitespace (for example,
turning HTML paragraph breaks into spaces), the adapter can resolve a unique match
and restore the original source slice and offsets. It does not repair changed words,
case, punctuation, or ambiguous matches. Framing labels and quotes spanning the
subject/body boundary still fail proposal validation.

Trusted ATS receipt rules recognize “thank you for applying,” “thanks for applying,”
and “we received,” “we have received,” “we've received,” and “we’ve received your
application.” Sender authentication, application identity, rejection precedence,
and recent-submission checks still apply. Other messages use model classification.
Archived recovery uses these same rules before invoking the configured model.

A received answer with invalid JSON, unsupported fields, or unverifiable evidence
creates **Classification needs review**. The email remains accessible in Review,
but no model event, quote, confidence, or application assignment is accepted.
Choose an interpretation and exact supporting words, keep the message without
changing application status, or dismiss it. Preview and explicit user resolution
are required. The CLI `mail-review list`, `message --proposal-id <review_id>`,
`preview`, and `apply` support the same records; decisions may specify `review_id`
instead of `proposal_id`. Shared mail mode retains a message-level uncertainty
finding and discards all findings from the unusable answer.

Saving that review completes mail processing. Pending human review does not itself
block readiness or trigger repeated model requests. Provider usage is still counted.
Network/authentication failures, unavailable model commands, failed persistence,
lost leases, and unknown provider outcomes retain the operational failure or
reconciliation path. Raw rejected answers are not stored in the review or logs.

For an existing failed email, use **Find suggested action** to analyze its archive
with the corrected rules and fallback, or **Retry processing** to requeue mailbox
processing. Deploying code does not reset failed rows. Archived recovery completes
its existing work owner only after the proposal or manual review is saved. If a
separate retry has already processed the email, `work-resolve-mail` can resolve its
obsolete owner using the saved proposal or manual review and terminal invocation
evidence; uncertain provider calls still require reconciliation.

## Attachments

Only non-inline Microsoft Graph `fileAttachment` objects are considered. PDF, DOCX,
and ICS require an exact MIME/extension pairing, a declared and downloaded size no
larger than 5 MiB, and format signatures. Other attachment types are rejected before
download. DOCX ZIP structure is checked before extraction and again inside the worker.

`SandboxedAttachmentExtractor` runs a fixed Python helper with no shell, a scrubbed
environment, a deny-by-default macOS sandbox containing `(deny network*)`, a timeout,
CPU/file-descriptor/output limits, DOCX expansion/ratio limits, and a 200-page PDF
limit. Production fails closed if sandboxing or the optional parser is unavailable.
Only normalized extracted text is retained, encrypted under the archive key; raw files
exist only in a mode-0600 temporary directory that is destroyed after extraction.

## Temporal review and accepted schedules

`LocalTemporalExtractor` is a fixed JSON-in/JSON-out, no-tools, no-network command in
the same model sandbox. It receives authenticated sanitized text plus at most twenty
minimal candidate applications. `validate_temporal_output()` requires an exact output
shape, an in-context application, a real IANA zone, second-precision UTC values, an
exact source substring/span, bounded confidence/count/horizon, and either:

- one interview start/end interval of no more than 12 hours; or
- one deadline timestamp.

Every temporal result is review-only and appears in the dashboard Attention list.
Accept/reject requests use a CSRF-protected user mutation context through
`/api/v1/temporal-proposals/{id}/decision`. Accepted interviews append
`interview_scheduled`, persist one
queryable accepted schedule, and create 24-hour and 1-hour local reminder records.
Accepted deadlines create one local reminder. The existing five-minute core tick turns
due rows into policy-gated `reminder.due` records in `notification_outbox`, then marks a
local reminder complete only after the durable enqueue. A crash between those writes is
safe: the notification dedupe key is replayed, then completion resumes. Hermes delivery
remains the separately leased notification-outbox step.

## Schema and integration note

This branch reserves migration **4**, `secure_mail_archive_and_temporal_scheduling`,
on base commit `69fa23e` (whose latest migration is 3). It creates:

- `outlook_folder_inventory`
- `mail_archive`
- `mail_archive_attachments`
- `temporal_proposals` and `temporal_proposal_decisions`
- `accepted_interview_schedules`
- `local_reminders`

The planned generic `notification_outbox` belongs to migration 6 and is intentionally
absent here. If the integration branch has already assigned migration 4, renumber this
single ordered migration before cherry-picking; never rewrite an applied migration or
its checksum.

Optional live dependencies are listed in `requirements/mail-archive.txt`. All tests use
fakes or isolated local fixtures and make no network requests.

`build_archive_mail_source()` is the concrete read-only `HermesSources.mail` adapter.
It lists only opaque archive metadata, decrypts at most 200 recent messages per search,
returns at most 25 bounded matches, and uses `archive_id` as the public `message_id`.
Exact retrieval returns one bounded sanitized subject/excerpt view. No Graph object,
token provider, account/message identifier, archive key, or ciphertext crosses that
boundary, and plaintext is never persisted after decryption.
