# Implementation contract: shared mail understanding v1

This supplements the accepted design with the interfaces used by parallel implementation lanes.

## Frozen boundaries

Modules stay in `job_search/mail/` with `understanding_` prefixes.

- `understanding_contracts.py`: `SCHEMA_VERSION = "mail_understanding_v1"`, `validate_analysis(raw, request) -> dict`, `analysis_schema(candidate_ids) -> dict`.
- A request is JSON data: `schema_version`, `account_id`, `immutable_message_id`, `observation_id`, `evidence_id`, `received_at`, `sources`, `candidates`, `candidate_context_complete`, `coverage`, `producer_version`. Optional `archive_id`, `replay_id`.
- Each source has `source_id`, `kind` (`current`, `quoted`, `prior_inbound`, `prior_outbound`, `attachment`), `text`, `source_at`, `sha256`, `truncated`, and optional archive/attachment IDs. `text` is the actual supplied slice, offsets relative to that slice. Source IDs and hashes are service prepared. Coverage is an array of `{source_id, reason}` objects.
- Each candidate uses the existing `CandidateApplication.model_context()` shape. Metadata and source texts are untrusted; no tools are exposed.
- Model result: exactly `relevance`, `events`, `actions`, `temporal_facts`, `uncertainties`. Every event/action/temporal has `application_id`, `confidence`, `evidence` (array of `{source_id,quote,start,end}`). Events add `event_type`. Actions add `kind`, `description`, `actor` (applicant/employer/unknown), `obligation` (required/optional/unclear), `channel` (email/portal/other/unknown), `temporal_index` (null or index). Temporal facts add `kind` (interview/deadline), `wording`, `starts_at`, `ends_at`, `due_at`, `time_zone` (all normalized values nullable). Uncertainties have `reason`, `description`, `finding_type` (event/action/temporal/message), `finding_index` (nullable for message).
- Bounds: each array <=8, evidence per finding 1..3, quote/description/wording <=512, uncertainty descriptions <=256. No unknown fields. Existing mail event enums. Action kinds reply/send_availability/complete_assessment/offer_decision/other. Unknown action mappings require review.

## Analyzer and preparation (analysis lane)

- `understanding_sources.build_request(...)` uses keyword arguments for identity metadata above plus subject/body/body_kind, candidates, candidate_context_complete, producer_version, prior_messages=(), attachments=(), coverage=(). Returns the request. Prior messages/attachments are source dictionaries; root runtime retrieves them. Segmentation and deterministic source ceilings happen here.
- `RemoteMailUnderstandingAnalyzer(provider)` / local equivalent have `producer_version`, `prepare(request)->request` (final budgeted source manifest), and `analyze(request)->raw dict`. Analyze never trims after prepare. Local version2 adapter uses `understand_job_application_email`.
- `validate_analysis` checks exact schema/source evidence and guards supported identities against current source + trusted candidates. It returns normalized JSON data, not persistence IDs. Unsupported identity becomes null with uncertainty; syntactically invalid IDs/citations fail.
- Source preparation must include whether authored content/history/attachments were omitted. Sources beyond retained legacy excerpts are supported.

## Durable service (store lane)

`MailUnderstandingService(ledger, archive=None)` in `understanding_store.py`, exposed as `ledger.mail_understanding` (read/review usable without archive). Core runtime can instantiate with encrypted archive.

- `claim(request, context, *, mode="shared", lease_seconds=300) -> {analysis_id, claim_token, state}`. States claimed/busy/saved/uncertain. Fingerprint includes exact prepared request and versions. Saves encrypted supplied sources through archive; no plaintext full source in ledger. Shadow/replay do not take active projection ownership; shared live claims do, before legacy producers can run.
- `save(analysis_id, claim_token, raw, context) -> detail`: independently validates against saved sources; fences claim token and lease; stores immutable normalized findings before projection.
- `fail(analysis_id, claim_token, reason, context)`: bounded identifier reason, releases a safely failed attempt; uncertain provider outcomes remain reconciliation required and use existing usage guards.
- `project(analysis_id, context, *, evaluation_report=None) -> detail`: idempotent projections. Shadow never creates public proposals/tasks. Replay never auto-applies and is excluded from routine attention/briefs.
- `get(analysis_id) -> detail`, `list_reviews(*, history=False, limit=100) -> list[detail]`.
- `decide(analysis_id, revision, decisions, context) -> detail`: atomic per-message batch, user actor only, exact revision; decisions list `{finding_id, decision: accepted|rejected, application_id: nullable, reason}` plus optional `replacement` for reviewed corrections. Existing event/temporal decisions remain authorities. Ledger methods may delegate to this facade.
- Detail: `analysis_id`, `account_id`, `immutable_message_id`, `evidence_id`, `mode`, `replay_id`, `state`, `revision` (opaque hash/string), `relevance`, `coverage`, `created_at`, `subject`, `application_id` (nullable), `candidate_application_ids`, `findings`. Finding: `finding_id`, `type` (event/action/temporal/uncertainty), `value` (validated original finding), `status` (pending/accepted/rejected/held), `projection` (nullable `{kind,id}`), optional `replacement_of`. Dashboard treats revision as opaque.
- Keep existing per-event/temporal API consumers compatible. Suppress independent reply/event/deadline tasks for shared-owned mail. Action + temporal acceptance both needed before assigning due_at. `other` acceptance requires reviewed replacement using supported task kind, never silently generic follow_up.
- Define schema in `understanding_schema.py`; primary integrator registers migration20 in db.py and updates release metadata. Store lane may modify store.py/service.py/lifecycle/core.py/career_actions/service.py and own their tests. No db.py edits.

## Review lane

Own dashboard routes/UI, attention portfolio/tests, browser tests. Use the above service only; do not write store.py/service.py. Add GET `/api/v1/mail-analyses/{id}` and POST `/api/v1/mail-analyses/{id}/decisions` with body `{revision,decisions}`. Group review entries `kind=mail_analysis`, `id=analysis_id`, detail from service. Hide equivalent legacy standalone proposals by projection links. The store handles attention_items integration; coordinate table/link shape. History is a separate queue/filter and never regular briefings. Independent explicit choices, stale revision conflict, immutable replacements. Telegram gets deep links only.

## Integrator lane

Own runtime.py/sync.py/CLI, understanding_runtime/evaluation/replay, db migration registration, release compatibility, packaging, docs, final integration tests. Defaults legacy; modes legacy/shadow/shared/paused. Retain remote opt-in and explicit source scope. Evaluation report API is `allows(finding_type, finding, request_or_provenance)->bool`; missing report means no auto-apply. Replay all linked inbound messages with fixed membership and batches50, archived sources only, no actual production execution in this task.
