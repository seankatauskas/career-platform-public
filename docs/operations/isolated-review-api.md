# Isolated reviewer API and tools

The coordinator creates an assignment through `ReviewAuthority` and serves a private
Unix socket for that assignment. The credential remains in the coordinator process.
The reviewer container receives only its own socket file. It does not receive a
bearer token, application database, dashboard session, or general MCP connection.

The worker tool process uses the Python standard library:

```bash
python -m job_search.job_reviews.reviewer_mcp --socket /review/review.sock
```

This is a stdio MCP server. Tool discovery follows the server-bound assignment role
and frozen rubric revision. Primary/check assignments advertise five tools:

| Tool | Arguments | Result |
| --- | --- | --- |
| `review_assignment` | None | Assigned jobs and frozen revisions. |
| `review_context` | Optional `section`, `offset`, `limit` | A page of facts, preferences, or explicit feedback. |
| `review_job` | `ordinal`; optional `offset`, `limit` | Source posting fields and one description page. |
| `review_assessment` | `ordinal`, `assessment` | Saved review, job ordinal, and revision. |
| `review_assessments` | `assessments` array of `{ordinal, assessment}` | One saved receipt or validation error per item. |

A v2 primary grant with immutable purpose `screening` instead advertises
`review_assignment`, `review_context`, `review_job`, and `review_routes`. The routing
tool accepts up to twenty unique entries per call. `{ordinal, route: "detailed"}`
defers the job without completing its assessment. An exclusion also supplies
`reason_code` (`non_technical` or `location`), a concise `explanation`, and exact
source `evidence` quotes. Description evidence and complete description-read coverage
are mandatory. Location exclusions additionally need location evidence,
`brief_revision`, `family`, and `alignment`, and require the saved US-only broad and
targeted scope. Trusted code expands fixed exclusion metadata; it never synthesizes
a recommendation. Qualification exclusions and duplicate decisions require detailed
review. Invalid dispositions remain pending.

Screening grants may cover up to 200 explicitly bound jobs, reduced by complete-input
and response-size bounds. Detailed/check grants remain limited to twenty. Scope,
snapshot, context, actor, idempotency, and partial-validation behavior match the bulk
assessment boundary. A detailed/check worker cannot inspect routing rationale, and
a screening worker cannot call the assessment or finalization tools.

Use `next_offset` to retrieve every page of all three context sections and each job's
complete description before a detailed assessment. Selected roles require a description
quote linked to an approved `fact_id`, a detailed assessment, and agent-assigned
recommendation priority. All judgments require source evidence. Qualification exclusions
also require detailed review. The assessment schema is advertised through MCP tool discovery.

Context pages include the frozen `search_brief` revision and `source_inventory` when
available. Read all career-bank and resume facts, not just the first page. Pending
drafts never enter the evidence. V2 assessment discovery includes eligibility,
eligibility condition, next step, and category; v1 discovery retains the old schema.

V2 finalizer assignments advertise seven tools:

| Tool | Arguments | Result |
| --- | --- | --- |
| `review_assignment` | None | Finalizer role and bound context/evidence basis. |
| `review_context` | Optional `section`, `offset`, `limit` | Frozen context pages. |
| `review_calibration` | Optional `after`, `limit` | Selected postings, effective checked assessments, staged order/groups, and next page. |
| `review_calibrate` | `ordinal`, `position`; optional `related_group` | Saved order/group entry for the assignment's bound basis. |
| `review_calibrations` | `calibrations` array of `{ordinal, position, related_group?}` | One saved receipt or validation error per item. |
| `review_finalize` | None | Complete revision-bound calibration receipt. |
| `review_order` | Complete `ordinals` permutation; optional `groups` with `id`, `label`, `ordinals` | Atomic staging and final seal using existing per-item provenance. |

`review_order` accepts up to 6,000 selected ordinals within the unchanged 64 KiB
request body. No membership may be omitted or repeated. An invalid entry or failed
seal rolls back the whole new submission, preserving prior saved work. Larger orders
use the existing bounded calibration tools. This does not reduce the evidence the
finalizer must read or change any card's fit label or caveats.

V2 adjudicators receive `review_assignment`, `review_context`, `review_job`,
`review_disagreement`, and `review_resolutions`. They are separate from blind checkers.
`review_disagreement` accepts an assigned `ordinal` and optional `offset`/`limit`.
Small responses contain both current-run judgments and their differing dimensions.
Oversized responses return canonical JSON `content` pages with `payload_sha256`,
`total_chars`, `next_offset`, ordinal and basis. Read every contiguous page, concatenate
the exact content, verify the digest and parse the pair. Complete source, frozen context
and paired-judgment reads are required before resolution.

`review_resolutions` takes up to twenty entries containing ordinal, the returned
`basis_sha256`, `choice` (`primary`, `check`, `unresolved`), `checked_dimensions`,
`explanation`, and exact source `evidence`. It can choose only an entire existing
assessment. Selected choices need an approved fact reference. The authority verifies
the basis; callers cannot change it. Immutable resolutions preserve both original
judgments and their history. A stale, unresolved or interrupted adjudication cannot
authorize publication, and an already consumed basis cannot automatically redispatch.

Read every calibration page before ranking the whole selection. Positions must cover
every selected ordinal exactly once, with unique positions from 1 through the selected
count. Related groups have an `id` and `label`; they do not remove requisitions. A
finalizer cannot alter primary/check assessments or selected membership, inspect model
scores, fetch arbitrary jobs, check websites, or publish lists. The trusted coordinator
performs availability verification and publication after finalization.

The server binds identity, assignment role, posting membership, snapshot, expected
revision, and submission idempotency to the assignment. These are not tool arguments.
If a submission response is lost, retry the identical arguments. A changed assessment
requires coordinator handling rather than a new caller-selected command identity.

Bulk submissions contain one to twenty unique ordinals. Each item retains the
single-item validation and idempotency contract. A validation error leaves valid
siblings saved; scope, authorization, or stale-evidence failures roll back the entire
request. Inspect every item result and retry failed items using the single-item tool
or another bounded bulk call. Never infer that the whole batch succeeded from HTTP 200.

The coordinator opens the transport through:

```python
from threading import Event
from job_search.job_reviews.reviewer_api import assignment_proxy

ready = Event()
with assignment_proxy(socket_path, authority, grant_token, ready=ready):
    # Launch the isolated worker, persist the authority's launch receipt, then:
    ready.set()
    # Monitor the worker and preserve/revoke its assignment through the authority.
```

The optional launch barrier waits up to ten seconds per request and returns an
unavailable response until launch provenance is saved. The barrier itself is not
execution attestation; the authority independently enforces grant state.

## HTTP boundary

The assignment socket implements exactly these routes:

- `GET /v1/assignment`
- `POST /v1/context`
- `POST /v1/job`
- `POST /v1/assessment`
- `POST /v1/assessments`
- `POST /v1/routes` (screening primary only)
- `POST /v1/calibration` (finalizer only)
- `POST /v1/calibrate` (finalizer only)
- `POST /v1/calibrations` (finalizer only)
- `POST /v1/finalize` (finalizer only)
- `POST /v1/order` (finalizer only)
- `POST /v1/disagreement` (adjudicator only)
- `POST /v1/resolutions` (adjudicator only)

The role boundary rejects finalizer operations from primary/check grants and rejects
job/assessment operations from finalizer grants. Finalization requires the current
context/evidence basis, independent checks with no unresolved disagreements, and
complete staged membership/order.
The authority binds `basis_sha256`, reviewer identity, and idempotency; isolated workers
cannot supply those fields as arguments. General review CLI/MCP callers supply the
basis explicitly through the ordinary service interface instead.

`make_api_server(socket_path, authority)` creates the corresponding bearer-authenticated
Unix HTTP server when a trusted caller needs that interface. It is not a public TCP
listener. `make_assignment_server(socket_path, authority, token, ready=None)` creates
an assignment-bound listener without starting its serving loop; `assignment_proxy`
starts and cleans up that listener as a context manager.

Both interfaces reject unknown paths, query strings, arbitrary actions, duplicated
content lengths, chunked bodies, non-finite JSON, duplicated JSON keys, and attempts
to override scope. Single-item requests are limited to 64 KiB and bulk requests to
256 KiB; responses remain limited to 56 KiB, and eight
connections may be served concurrently. Each connection handles one request. Context
pages contain at most twenty entries and description pages at most 6,000 characters.
The MCP envelope also has a 56 KiB limit; request smaller pages if the envelope exceeds
that limit.

The per-assignment listener rejects any supplied Authorization header. This prevents
a worker from substituting another credential. Responses use a second field projection
independent of the authority. Primary/check responses exclude historical assessments,
prior lists, application histories, and model diagnostics. Finalizers receive only
the current selected effective assessments needed for ordering; prior lists and model
diagnostics remain excluded. Free-text approved facts and user feedback
remain source evidence, including user-authored words such as “ranking.”

Socket directories must exist, be owned by the serving/worker UID, and have mode 0700.
The service creates sockets with mode 0600 and refuses existing sockets, symlinks, and
non-socket destinations. Mount only the individual assignment socket file into the worker;
same-UID filesystem permissions alone cannot isolate adjacent assignments if their
parent directory is also mounted. No credentials or private source values are logged.

## Verification

```bash
python3 -m tests.test_reviewer_api
```

The offline suite uses real Unix sockets and a subprocess stdio client. It checks scope
overrides, role-specific tools, stale finalizer bases, route guessing, secret-safe failures,
ranking sentinel exclusion, unsafe socket paths, malformed/oversized frames, launch
readiness, and no redirect following. Container
network/filesystem isolation and authoritative grant/ledger behavior have separate suites.
