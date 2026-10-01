# Interactive demo and media capture

The portfolio scenario runs the production dashboard, application ledger, review services, workers, encrypted message archive, and MCP server against sample records. Model responses and external transports are fixtures. It never loads production configuration, connects to a real mailbox, or contacts an employer.

## Start the populated scenario

```bash
uv run scripts/offline_system_demo.py --interactive --scenario portfolio --state-dir .cache/portfolio-demo --port 8775
```

Open `http://127.0.0.1:8775/#shortlist`. Use a new empty state directory for each run. Ctrl-C stops the demo.

The scenario contains 24 job postings across three ATS platforms, three saved daily shortlists, applications across the submission and interview lifecycle, and reviewable correspondence and actions. Sean's first name is paired with sample experience and contact details. Company names, roles, conversations, and documents belong to the same scenario.

Browse these views:

- **Shortlist:** saved lists, selection explanations, posting dates, and the shared job-description preview. Switch to model picks to inspect the other source.
- **Applications:** filter the list by stage or pending review; open a record's Overview, Messages, and Documents tabs.
- **Review:** inspect proposed updates, uncertain correspondence, draft replies, and interview scheduling proposals before deciding.
- **Settings:** view the sample career profile, saved resumes, historical drafts, connections, and operations.

Opening a preview does not create an application. An application's current stage is independent of whether its source posting is still open. Documents resolve the file recorded for submission, even if the preferred resume later changes.

The demo starts with pending review items, including the Northstar Labs interview update and a reply proposal. Confirm the update in Review, inspect the application's Messages, then review and approve the exact reply text. Execute approved actions against the fake provider:

```bash
uv run scripts/offline_system_demo.py --state-dir .cache/portfolio-demo --advance execute
```

`--advance mail` replays mailbox synchronization, `--advance reply` retrieves the existing reply proposal, and `--advance curated` republishes the saved selection with its stable idempotency key. Repeating these commands does not add duplicate messages, drafts, or lists.

The scenario receipt identifies the lead application and its review items. External fixture events advance separately from decisions made through the visible interface. Approval creates only a simulated unsent draft or private calendar hold; it never sends an email or invitation.

## What is simulated

| Component | Boundary |
| --- | --- |
| Catalog and rankings | Sample postings and fixed scores flow through the real discovery services. Personal trained models and datasets are not bundled. |
| Saved shortlists | Ordered selections are published through the actual curated-list interface. |
| Browser tracking | Fixture submission observations enter the tracking service; no real employer application is submitted. |
| Outlook | Fake HTTP responses exercise the production Graph client, message ingestion, review, and action execution. |
| Hermes | Scripted calls use the real MCP server and fixed reply content. This is not a live model or Telegram conversation. |
| Documents | Valid sample PDFs show versioned document behavior; they do not evaluate LaTeX compilation fidelity. |
| Operations | Isolated scenario activity and fixture receipts demonstrate inspection and recovery without running a live collection sweep. |

The original end-to-end acceptance scenario remains the default:

```bash
uv run scripts/offline_system_demo.py --state-dir .cache/acceptance
```

It checks pagination, throttling, worker recovery, durable delivery, and immutable resume context. Add `--tectonic`, `--bundle`, and `--tectonic-version` for the pinned offline compiler described in [the acceptance guide](offline-system-demo.md).

## Record the overview

Install the browser dependencies and make FFmpeg available, or set `FFMPEG_BIN` to its executable:

```bash
npm ci --prefix extension
npx --prefix extension playwright-core install chromium
CONSOLE_OUTPUT=.cache/portfolio-film node scripts/record_dashboard.mjs
```

The recorder creates its own scenario and isolated browser. Its ordinary 800 × 640 responsive viewport uses the app's graphite/brass dark theme. Eight short segments cover shortlist, preview, applications, application history, review, messages, reply approval, and the updated application record. Operations is covered by a separate still. Setup and navigation between segments are cut out; the recorded interactions use actual controls and services.

The final film is approximately 30 seconds. Browser compositor frames retain their timestamps before H.264 encoding; a 30fps output alone is not treated as evidence of smooth capture. High-density PNG stills are captured independently after view readiness checks. Detailed component crops preserve readable text without changing the UI.

Outputs include `walkthrough.mp4`, screenshots, and a receipt with scenario/revision information, dimensions, shot timing, frame statistics, browser errors, and asset hashes. Raw frames and state directories are local build artifacts, not public assets.

## Verify the demo

```bash
uv run python -m tests.test_job_boards
python3 -m tests.test_job_boards
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --browser
```

Capture is separate from regression tests, so test reloads, light-mode checks, malicious-input fixtures, and failure injection cannot appear in the film. Browser requests to external hosts are blocked. Review every published frame/caption and scan the final images for unintended personal information before copying assets into documentation or the portfolio.

After reviewing the capture, publish only the verified media files:

```bash
python3 scripts/publish-dashboard-media.py --capture .cache/portfolio-film --portfolio /path/to/portfolio-website
```

The publisher requires a successful capture receipt, rejects external requests and browser errors, and verifies every asset hash before copying. It leaves raw frames, runtime state, and receipts in the ignored capture directory.

Additional stills cover the [job preview](images/preview.png), [messages](images/messages.png), [submitted documents](images/documents.png), [career profile](images/career-profile.png), and [posting history](images/posting-history.png).

The blog and README use the same generated media. Images remain within the article's reading column; their captions describe the supported behavior and the sample-data boundary.
