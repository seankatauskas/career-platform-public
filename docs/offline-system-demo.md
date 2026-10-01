# Complete local system walkthrough

Run this from a checkout with the secure-mail dependency `cryptography` installed
(included in `requirements/cloud.txt` and `requirements/mail-archive.txt`):

```bash
python3 scripts/offline_system_demo.py --state-dir .cache/system-demo
```

The state directory must be empty. The runner creates isolated fictional state and
writes `acceptance.json`; it does not read the user's runtime configuration, account
credentials, mailbox, resume bank, or existing databases. No provider setup is needed.
It exits on a failed assertion and closes its local services.

Add `--serve --port 8770` to leave the actual dashboard and workers running at
`http://127.0.0.1:8770` after acceptance. Stop them with Ctrl-C. Use another empty
directory for the next run; the runner never clears existing state for you.

## What runs

The scenario uses the production shortlist policy, recommendation impressions,
dashboard HTTP/CSRF boundary, career bank, resume gateway, leased core/model workers,
application ledger, Graph clients and transport policy, encrypted mail archive, MCP
HTTP server, notification outbox, and Unix delivery bridge.

It prepares a fictional applicant's resume, approves and selects it through the
dashboard API, records a submission, ingests paginated recruiter mail, reviews the
application event, proposes a reply over MCP, approves the exact draft in the
dashboard, and lets the worker create the draft. MCP retrieves the frozen submitted
resume and archived mail. The worker delivers the resulting notification through the
local bridge. Delta replay, Graph throttling, and worker reconstruction verify that
retries preserve the application history and do not create another draft.

The fictional user approvals are performed by the acceptance client. The MCP agent
has no approval capability. Neither this scenario nor the production Graph client
sends email.

## Fixture boundaries

Ranking inputs are deterministic hashing-similarity scores. The actual shortlist
policy filters, ranks, records impressions, and records application feedback, but
this is not validation of a trained personal ranker. Resume and mail model responses
are fixed fixtures. Graph's HTTP edge is in-process fake data. The Telegram sender
is a local executable that succeeds without contacting Telegram. The default
document tool is a fixture, so its PDF bytes are not intended for visual review.
`acceptance.json` records these distinctions explicitly.

For a valid PDF and local visual review, supply an installed, pinned Tectonic binary
and its offline bundle, and use a Python environment containing both `cryptography`
and `pypdf`:

```bash
python3 scripts/offline_system_demo.py \
  --state-dir .cache/system-demo-real-pdf --serve \
  --tectonic /absolute/path/to/tectonic \
  --bundle /absolute/path/to/resume.bundle.zip \
  --tectonic-version 'exact output from tectonic --version'
```

The runner does not download a compiler, bundle, model, or Python package. Its
portable arguments avoid assuming a developer's home directory or tool cache.

## Regression commands

```bash
python3 -m tests.test_job_search_offline_system
python3 -m tests.test_job_search_resume_context
python3 -m tests.test_job_search_delivery_recovery
```

The latter suites cover exact submitted-resume binding, explicit unavailable states,
MCP content bounds, interrupted notification sends, payload conflicts, receipt
migration, and audited delivered/not-delivered/abandoned reconciliation. A confirmed
non-delivery requeues the existing immutable notification; reconciliation itself
does not send it. Live Outlook consent, Telegram owner routing, provider inference,
and cloud deployment still require their separate live acceptance checks.

## Interactive dashboard walkthrough

For a staged browser walkthrough with a valid fictional PDF and manual approvals, use `--interactive`. See [the demo guide](demo.md). This mode has inline uv dependencies and fake external services; its default PDF is a ReportLab fixture, not a TeX compiler result. The complete acceptance mode above retains its original document fixture unless you supply the real toolchain.
