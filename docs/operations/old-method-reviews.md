# OLD METHOD async reviews

`old-method-v1` is an on-demand, single-lead review of supplied source inputs.
It uses the same canonical screen locally and on AWS. It does not read previous
shortlists or applications. An earlier recommendation or application does not
exclude a job; the dashboard still displays existing application status.

The trusted host freezes open catalog postings where `start < first_seen <= end`
and `0 <= first_seen - posted_at <= 24 hours`, along with approved career facts,
active resume text, saved search preferences and explicit feedback. A posting exactly
24 hours old at discovery qualifies. Missing/invalid dates and posting dates after
discovery do not qualify; updates and later collection dates do not qualify a job.
For custom runs, the caller supplies both window boundaries, with any duration. Adjacent
windows can use the previous end as the next start. Each window is independent and
can overlap. The on-demand generator below derives boundaries from its run history;
neither workflow compares previous list contents.
Starting a review does not collect jobs or run ranking.

New runs record `window_policy=first-seen-posted-24h-v1` in their metadata and frozen
packet. Previously saved runs without that field retain `start < posted_at <= end`
and their original packet hashes when resumed.

## Submit, inspect and resume

### Generate through now

The application Shortlist page has **Generate list**, **Refresh**, and
**Resume generation** controls. Refresh reloads saved lists and generation status;
active generation also refreshes its status automatically. On first use, choose the starting time in your
browser's local timezone. Subsequent requests use the last successfully published
generator run's `window_end` through the request's server timestamp.
New list names show their kind and coverage in Central time, for example
`Targeted · Oct 10, 2026 · 1:00 AM–12:00 PM CDT`. Multi-day windows show both
dates; daylight-saving transitions show both timezone abbreviations. Saved time
and internal workflow names are not part of the new title.

Codex can use the private dashboard CLI:

```bash
python3 -m job_search review generate --dashboard-url https://YOUR-HOST.ts.net --input - <<'JSON'
{"initial_start":"2026-10-10T01:00:00-05:00","idempotency_key":"generate-first-20261010"}
JSON
python3 -m job_search review generate --dashboard-url https://YOUR-HOST.ts.net --input - <<'JSON'
{"idempotency_key":"generate-next-20261010"}
JSON
python3 -m job_search review generation-status --dashboard-url https://YOUR-HOST.ts.net --input - <<'JSON'
{}
JSON
```

Telegram/Hermes and connected Codex clients use `review_generate`,
`review_generation_status`, and `review_generation_resume` through the existing
authenticated MCP service. Ask “Generate my job list”; on first use, supply the
starting date, time, and timezone. These tools require an explicit user request.
An explicitly configured Telegram tool allowlist must include their exact
`mcp_job_search_` names. The default career allowlist includes them.

The same operations are available at `POST /api/v1/job-reviews/generate`,
`generation-status`, and `generation-resume`; reads also support
`GET /api/v1/job-reviews/generation-status?review_id=REVIEW_ID`.
Writes require an `idempotency_key` and the existing dashboard session/CSRF checks.
Resume additionally requires `review_id`. Retries should reuse the original key
and arguments; a new intentional generation uses a new key.

Admission is transactional in the application database. `generation_stream=default`
marks these runs in `job_reviews.metadata_json`. Only published runs in this stream
advance coverage; custom windows, historic comparisons, and unpublished qualification
runs do not. A successful empty result advances coverage too. Concurrent requests
return the active run. Failed/interrupted runs retain their cutoff and saved work
until explicitly resumed; status includes `can_resume` and publication receipts.
Replaying an old request returns its run's current status, even after publication.

The host's `job-search-list-generator.timer` checks for queued requests every
30 seconds and invokes `drain-generation --workflow old-method-v1`. It never
creates a request or runs a recurring review. Normal release preparation installs
and enables these units. The existing runner lock, maintenance gate, image pinning,
checkpoints and publication checks still apply. The request returns before input
freezing and model execution; the timer also recovers a host crash with saved work.
Model failures require explicit resume, preventing repeated paid retries.

Input freezing waits for running managed collectors to finish and for the request
cutoff's timestamp second to pass. This includes discoveries assigned before the
cutoff whose collection transaction was still saving when the request arrived.
Newer discoveries wait for the next request. Run collectors through the managed
pipeline so their in-flight writes are visible to this barrier; an unregistered
direct catalog writer cannot participate in coordination. The remote reviewer
receives only the frozen window, postings and career context, without stream state
or previous-list/application comparisons.

### Explicit custom windows

Through the existing SSM administration path, from `/opt/job-search/current`:

```bash
python3 -m job_search.review_host submit --workflow old-method-v1 \
  --window-start 2026-10-08T00:00:00Z --window-end 2026-10-09T00:00:00Z
python3 -m job_search.review_host status --workflow old-method-v1 --review-id REVIEW_ID
python3 -m job_search.review_host resume --workflow old-method-v1 --review-id REVIEW_ID
```

Submission returns a durable run ID and launches a transient systemd service. The
service survives the caller disconnecting. Submission reports busy while another
review owns the shared runner lock; retry submission after that review finishes.
Maintenance/deployment gates and cancellation stop work without discarding accepted
checkpoints. Resume requires the recorded model profile and image. A sealed result resumes host
validation without repeating model inference. A changed selected
posting blocks publication and requires a new review with fresh source evidence.
Existing operational timeout configuration remains a recovery safeguard, not a quality
target or reason to exclude jobs. Custom runs do not advance the generator cutoff.

Use `--no-publish` at submission for a frozen-window qualification. Its completed
phase is `qualified`, and that run can never publish. Inspect its private result
before running a publishable window. Model inference uses the existing dedicated
AWS Codex login; do not copy desktop credentials or configure API billing fallbacks.

## Worker and result

The worker receives a read-only input bundle and a private writable workspace.
It has Python and native shell execution inside the existing networkless, nonroot
Docker boundary. Only the fixed model gateway socket is mounted; it has no review
database socket, AWS credentials, application records or old recommendation files.
This single worker has a 3 GiB memory limit to accommodate a full posting window;
the launcher releases its input copy before starting native Codex tools.
The runtime uses pinned Codex with Astra high. There are no helper/checker stages.

The screen preserves the local title-category order, seniority labels and location
classification. Explicit software engineer/developer, DevOps engineer and site
reliability engineer titles bypass the two-description-keyword rule. Ambiguous
categories retain it. Screen failures remain available for lead-chosen searches.
The location classifier supplies navigation labels, not work-authorization decisions.

Codex saves selected assessments with fit labels, exact source quotes linked to
approved facts, and visible material caveats. It chooses restorations, recommendations
and order. It need not assess every rejected job. Search/restoration traces and jobs
without saved assessments remain distinguishable from explicit model rejections;
source delivery is not proof of model understanding. No selection quota is imposed.

The host revalidates the sealed result against the immutable input, checks current
selected source identity, the frozen window policy and discovery time, then checks
every selected official board.
Only confirmed-open jobs publish. Absent, closed and unverified jobs are reported as
omitted. Both list kinds and their receipt commit atomically, with the targeted list
a subset of the broad list. Retries of the same run return the saved receipt.

## Shared local interface

Export a frozen packet with the host's `export --workflow old-method-v1 --review-id
REVIEW_ID --output /PRIVATE/input.json` operation. Use the same released package locally:

```bash
python3 -m job_search.job_reviews.old_method screen --input /PRIVATE/input.json --output /PRIVATE/screen.json
```

Local sessions may import `Review` from `job_search.job_reviews.old_method.workspace`,
passing explicit `inputs` and `output` paths. Its `index`, `search`, `restore`, `sources`,
`save`, `remove`, `progress` and `finish` methods are the same helpers used remotely.
Do not copy scripts out of previous dated review folders. Historic private review
folders remain evidence, not the implementation source. The remote host remains the
publication authority; exporting a packet does not submit local files for publication.

## Diagnosing gateway failures

Every new AWS attempt writes host-only, mode-0600 diagnostics beside `codex.jsonl`
and `stderr.log` under `state/review-runner/old-method/REVIEW_ID/attempt_*/`:

- `gateway.jsonl` records request start/finish timestamps, a per-attempt request
  sequence, request bytes, durations, numeric usage, outcome, and HTTP status.
  Native timing offsets distinguish connection, headers, first byte, full body,
  and validation. A start without a finish identifies an outstanding request.
- `gateway-failure.json` atomically retains the latest failed request's record.
  Rejections include the validation code, offending event index and structure,
  known event/output types, and allowlisted upstream error or incomplete codes.
  This file survives worker exit and is updated even if the JSONL cap is reached.

Read these files through the existing SSM administration path; they are not mounted
in the worker or displayed on shortlist cards. A resume gets a new attempt directory;
older diagnostic files stay associated with their original attempt. The JSONL is
capped at 4 MiB plus a small truncation marker. Structural diagnostics retain at most
100 event summaries plus the rejected event, with bounded output/content summaries.
Credentials, headers, prompt/source text, tool arguments, model reasoning, raw error
messages, and response identifiers are never logged. Unrecognized type names are
represented as `unknown` with a SHA-256 fingerprint for correlation, not raw text.
Unrecognized error codes are represented as `unknown`.

`response.failed` and `error` now count as `upstream_failed`;
`response.incomplete` counts as `incomplete_stream`. Unsupported events or output
kinds remain `unsupported_capability`. The worker-facing error stays generic and
these changes do not automatically retry requests or admit new tool capabilities.
A failed logging callback cannot change the model response or rejection decision.
These records diagnose future attempts only; omitted details from earlier failures
cannot be reconstructed retroactively.

Native subscription streams may include `response.metadata` before completion.
The gateway accepts this informational event with an object-valued `metadata`
payload and forwards the original stream unchanged. Metadata never supplies output,
declares tools, or satisfies completion; normal tool, identity, and stream validation
still applies. Failed generation is displayed as **Generation failed**, separately
from **Generation interrupted**. Both retain the explicit resume control.

## Deployment and validation

Use the normal prepared AWS release and deployment workflows. The reviewer image
must attest `org.career-platform.review.old-method-version=1`. Release validation
includes a synthetic, credential-free native-shell continuation and isolation probe.
No new infrastructure or database migration is required: runs use the existing review
ledger with an immutable workflow marker and custom windows. Ordinary managed-review
mutations cannot change these runs, and existing managed workflows retain their gates.

Private state, worker transcripts and checkpoints live under `state/review-runner` and
use existing backup coverage. Status reports phase, saved-assessment count, omissions
and publication receipts. Keep input bundles and model output out of Git.

Run `python3 -m tests.test_old_method`, the native protocol and existing review suites,
and `python3 -m tests.probe_old_method_container --image IMMUTABLE_IMAGE`. Inspect a real
unpublished window against original descriptions and approved facts before routine use.
Offline or synthetic checks do not establish real model recommendation quality.
