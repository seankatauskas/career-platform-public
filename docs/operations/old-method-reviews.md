# OLD METHOD async reviews

`old-method-v1` is an on-demand, single-lead review of supplied source inputs.
It uses the same canonical screen locally and on AWS. It does not read previous
shortlists or applications. An earlier recommendation or application does not
exclude a job; the dashboard still displays existing application status.

The trusted host freezes open catalog postings where `start < posted_at <= end`,
approved career facts, active resume text, saved search preferences and explicit
feedback. Updated and collection dates do not qualify a posting. Starting a review
does not collect jobs or run ranking. Each window is independent and can overlap.

## Submit, inspect and resume

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
target or reason to exclude jobs. There is no timer or recurring cutoff.

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
selected source identity and posting dates, then checks every selected official board.
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
