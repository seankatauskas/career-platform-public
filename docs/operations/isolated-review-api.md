# Isolated reviewer API and tools

The coordinator creates an assignment through `ReviewAuthority` and serves a private
Unix socket for that assignment. The credential remains in the coordinator process.
The reviewer container receives only its own socket file. It does not receive a
bearer token, application database, dashboard session, or general MCP connection.

The worker tool process uses the Python standard library:

```bash
python -m job_search.job_reviews.reviewer_mcp --socket /review/review.sock
```

This is a stdio MCP server. It advertises exactly four tools:

| Tool | Arguments | Result |
| --- | --- | --- |
| `review_assignment` | None | Assigned jobs and frozen revisions. |
| `review_context` | Optional `section`, `offset`, `limit` | A page of facts, preferences, or explicit feedback. |
| `review_job` | `ordinal`; optional `offset`, `limit` | Source posting fields and one description page. |
| `review_assessment` | `ordinal`, `assessment` | Saved review, job ordinal, and revision. |

Use `next_offset` to retrieve every page of all three context sections and each job's
complete description before a detailed assessment. Selected roles require a description
quote linked to an approved `fact_id`, a detailed assessment, and agent-assigned
recommendation priority. All judgments require source evidence. Qualification exclusions
also require detailed review. The assessment schema is advertised through MCP tool discovery.

The server binds identity, primary/check role, posting membership, snapshot, expected
revision, and submission idempotency to the assignment. These are not tool arguments.
If a submission response is lost, retry the identical arguments. A changed assessment
requires coordinator handling rather than a new caller-selected command identity.

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

`make_api_server(socket_path, authority)` creates the corresponding bearer-authenticated
Unix HTTP server when a trusted caller needs that interface. It is not a public TCP
listener. `make_assignment_server(socket_path, authority, token, ready=None)` creates
an assignment-bound listener without starting its serving loop; `assignment_proxy`
starts and cleans up that listener as a context manager.

Both interfaces reject unknown paths, query strings, arbitrary actions, duplicated
content lengths, chunked bodies, non-finite JSON, duplicated JSON keys, and attempts
to override scope. Requests are limited to 64 KiB, responses to 56 KiB, and eight
connections may be served concurrently. Each connection handles one request. Context
pages contain at most twenty entries and description pages at most 6,000 characters.
The MCP envelope also has a 56 KiB limit; request smaller pages if the envelope exceeds
that limit.

The per-assignment listener rejects any supplied Authorization header. This prevents
a worker from substituting another credential. Responses use a second field projection
independent of the authority; historical assessments, prior lists, application histories,
and model diagnostics are never included. Free-text approved facts and user feedback
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
overrides, route guessing, secret-safe failures, ranking sentinel exclusion, unsafe socket
paths, malformed/oversized frames, launch readiness, and no redirect following. Container
network/filesystem isolation and authoritative grant/ledger behavior have separate suites.
