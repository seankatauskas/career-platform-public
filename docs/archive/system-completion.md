# Career Platform completion candidate

This branch joins the career bank, application ledger, resume workflow, Outlook
processing, MCP boundary, notification delivery, browser extension, and operating
controls into one locally reviewable system. Application and draft approvals stay
in the authenticated dashboard. Telegram and MCP can provide context and propose
work; they do not approve applications, drafts, or operational recovery.

## What changed

| Area | Behavior |
| --- | --- |
| Readiness | CLI, Ops, MCP and host monitoring distinguish configuration, actual progress, stale work and blocked work from process liveness. |
| Recovery | Work retries require a known safe failure, current revision, explicit user decision and durable audit. |
| Notifications | A durable receipt exists before sending. An uncertain outcome requires review; it cannot silently resend after a crash. |
| Resume context | MCP can retrieve bounded factual text from the exact selected/submitted artifact, with immutable provenance. |
| Private browser access | The extension pairs directly with a permitted private Tailscale HTTPS dashboard, requests only that origin, and survives a lost submission acknowledgement. |
| Inference | Platform-managed requests reserve usage before submission and persist accepted remote job IDs. Known IDs resume polling; uncertain submissions require reconciliation. |
| Operations UI | Readiness evidence and guarded recovery actions appear together, including explicit Telegram outcome review. |
| Verification | One runner executes the Python suites and browser checks. A complete local fixture exercises the actual service boundaries and workers. |

`docs/operations/operations-readiness.md`, `docs/offline-system-demo.md`, and the inference usage
implementation describe the individual contracts. Source revision and schema
version appear in Ops independently of workflow readiness.

## Local review

Install the existing Python runtime dependencies in an isolated environment, then:

```bash
npm ci --prefix extension
npx --prefix extension playwright-core install chromium
python3 scripts/check-system.py --browser
python3 scripts/offline_system_demo.py --state-dir .cache/new-demo --serve --port 8771
```

The demo requires an empty state directory and uses fictional data. It never reads
personal runtime configuration. Optional real-PDF arguments are documented in
`docs/offline-system-demo.md`. The acceptance receipt names every simulated external
edge: fixed model outputs, synthetic ranking inputs, fake Graph HTTP and a local
notification executable. The domain services, approval boundary, ledger, queue,
workers, encryption and MCP HTTP are real.

The unified check receipt records source SHA, working-tree state, individual suite
results and verification scope. Browser screenshots live under
`extension/test-results/`. CI and the release workflow use the same check entry
point and retain fixture-only evidence. The release workflow still requires its
existing production settings and deployment authorization.

## Activation work intentionally left separate

No live account changes, paid model requests, deployment, mailbox actions or real
Telegram sends are part of this completion effort. Existing credentials may already
be configured; the local checks do not establish that they currently work.

Before claiming live operation, verify the actual Outlook authorization and mailbox
selection, Telegram destination and sender, provider/model identity and chosen usage
limits, approved career facts, and trained ranker compatibility. An embedding change
still requires the existing explicit model migration and promotion process.

Provider usage counters cover platform-managed inference and conservatively reserve
tokens. They are not currency estimates or the provider's billing ledger. The
conversational Hermes model's independent requests remain outside that coverage.

The existing deployment runbook's live alarm test, replacement-host restore drill,
and operating trial remain acceptance work for the real environment. Local fixtures
and Docker tests must not be described as those production measurements. No source
branch or image is published by the local implementation work.

Legacy `resume import` and `resume update` use deterministic normalization by
default for remote providers. Their explicit `--allow-unmanaged-remote-inference`
option permits standalone normalization outside worker usage limits. Normal
dashboard previews never enable that exception.

A synchronous provider does not offer a portable way to retrieve a past response.
If its successful response was not checkpointed before a later failure, recovery
requires an explicit review instead of another automatic paid request. Embedding
batches checkpoint into their existing cache as each batch finishes. Queued Runpod
requests can retrieve their saved job IDs; an expired or missing remote result is
shown for reconciliation rather than polled forever.
