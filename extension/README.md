# Job Search Desk Autofill

This Manifest V3 extension recognizes and tracks applications on Ashby,
Greenhouse, and Lever, using a local or private Tailscale dashboard. Connect the
browser once in dashboard Settings. See **Automatic tracking (version 1.1)** below
for the current flow, evidence states, recovery behavior, and pilot checks.

## Application answer history (version 1.3)

With a connected browser, manually submitting a supported application saves its
questions and answers to the private application database. Open **Applications →
the application → Answers** to read or copy them later. Open-text responses appear
first, with paragraph breaks preserved. Each submission attempt has its own
timestamped history; failed or unconfirmed attempts can also have saved answers.
An answer snapshot is not proof that the employer accepted those values.

Capture is separate from autofill: it records readable text inputs, textareas,
rich-text editors, dropdown selections, radio buttons, checkboxes, and uploaded
file names, including fields autofill intentionally leaves manual. Files themselves
are not copied by this feature. Passwords, hidden fields, CAPTCHA, authentication
codes, payment-card fields, and Social Security numbers are excluded. Supported
ATS frames and open shadow roots are covered; closed shadow roots and custom
controls without readable input/accessibility semantics may not be readable.

Edits are captured as you type and merged across form steps. Pending drafts and
submit snapshots are encrypted in trusted extension storage using the browser's
pairing credential. They survive worker/browser restarts, and submit snapshots
retry until the dashboard acknowledges saving them. Encryption is not protection
against someone with access to the entire browser profile and its credential.
Unsubmitted local drafts expire after seven days; queued submission snapshots do
not expire. Disconnecting or uninstalling clears unsynced local data. Opening or
typing into a form alone does not create an application record.

Capture allows up to 400 fields, 64,000 characters per value, and 1 MiB of field
data per snapshot, prioritizing prose over other controls. The popup and Answers
tab warn when these limits omit fields or shorten values. Storage failures are
reported in the popup; do not disconnect while it reports answers waiting to sync.
No model/provider call is used. Historical applications cannot be reconstructed
retroactively from pages that are no longer available.

Deploy the matching dashboard migration/API before installing extension 1.3.
Reload the unpacked extension and refresh already-open application pages. Existing
browser pairing remains valid; a new pairing code is not normally needed.

## Resume attachment (version 1.2)

With a connected browser, **Fill application** fills supported fields and supplies
the application's selected PDF, or the preferred active, parse-safe standard PDF
when no application selection exists. This reads the existing private artifact;
it does not generate a resume or create an application record. The PDF is verified
by SHA-256 on both sides and never stored in extension storage. The attachment is
named `First_Last_Resume.pdf` using your private autofill contact profile.

Only one clearly identified resume file input is filled. Cover letters, ambiguous
upload widgets, unsupported formats, and existing attachments are left alone.
The popup reports whether the file was supplied or needs manual attachment.
Supplying it may start the employer's upload immediately; check the website's
upload result before submitting. The extension never clicks Submit. No filesystem
permission or native file picker is needed. The legacy handoff still uses manual
uploads. Run `npm run test:resume` for upload-specific Chromium checks and
`npm run test:browser` for the connected-browser flow on all three ATS fixtures.

## Legacy per-application handoff

The original handoff remains available for existing workflows. The dashboard
issues a five-minute code for one application; paste that code on its matching
job page and click Fill application.

The extension fills contact details, work history, approved reusable answers,
and private answers learned from applications you marked submitted. Private
answers include common demographic, disability, veteran, work-authorization,
and sponsorship fields. Equivalent unambiguous choices are reused across the
three supported ATS platforms without per-site configuration.

Legal attestations, compensation, credentials, CAPTCHA, uploads, signatures,
and final-submit controls remain manual. The extension never clicks Submit.
After submitting yourself, use **Mark submitted** to record that action and
commit eligible final form answers to the encrypted local vault. The popup shows
the tracked resume selected for the application when available. If none is shown,
you must explicitly confirm recording the submission without a tracked resume.

## Local profile

Copy `autofill-profile.example.json` outside the repository, replace the
synthetic values, and make it owner-only:

```sh
chmod 600 /absolute/path/to/autofill-profile.json
```

Start the dashboard with:

```sh
python3 -m pip install -r requirements/autofill.txt
python3 -m job_search.dashboard \
  --autofill-profile /absolute/path/to/autofill-profile.json \
  --autofill-vault "$HOME/.local/share/job-search/autofill-vault.bin"
```

The profile is versioned. Reusable answers require the exact normalized form
prompt and an explicit ATS allowlist. Sensitive prompt classes remain rejected
from plaintext `approved_answers`. Private values use Keychain-backed encrypted
persistence with no plaintext fallback. The extension receives assignments only
for fields detected on the active form, never the full profile or vault.

The legacy handoff snapshots eligible final values when you manually submit, but
does not persist that legacy snapshot in browser storage. Values are staged in dashboard
memory and become durable only when you click **Mark submitted**. Private
fixed-choice answers can be reused automatically. Custom prose is retained as
history for a later answer assistant and is not automatically inserted.

The extension can then be loaded unpacked from this `extension/` directory in a
Chromium browser's extension developer page. For local use, enter a loopback
address with its port, such as `http://127.0.0.1:8766`.

## Private cloud dashboard

Use your existing `https://machine.tailnet.ts.net` dashboard address. The browser
device must already be connected to Tailscale as the configured owner. The server
keeps its loopback listener and trusts only its configured Tailscale Serve
identity boundary; no public listener, browser bearer token, or local bridge is
added by the extension.

Paste the dashboard's one-time code while viewing its matching ATS application,
then click **Fill application**. Chromium asks permission for that exact dashboard
origin. The optional manifest pattern permits this request; it does not grant
access to every Tailscale site. The saved address is updated only after a successful
handoff. HTTP cloud addresses, alternate HTTPS ports, credentials, paths, query
strings, and redirects are rejected. Requests omit browser cookies.

Removing the dashboard's site permission discards its saved address and active
handoffs. To reconnect, enter the address again and use a new code. A dashboard
restart also invalidates process-local codes and receipts. If recording submission
loses its response, retry **Mark submitted**: the same idempotency key is used and
the extension does not attempt to restage a completed capture.

## Offline checks

```sh
node extension/test_extension.js
python3 -m tests.test_job_search_extension_cloud
npm ci --prefix extension
npx --prefix extension playwright-core install chromium
node extension/test_answer_capture.mjs
node extension/test_browser.mjs
```

On Linux CI, install Chromium's system dependencies with
`npx --prefix extension playwright-core install --with-deps chromium`. The browser
check also needs Python 3.9+ and OpenSSL. It creates a temporary browser profile,
certificate, real dashboard, synthetic ATS pages, and local TLS identity proxy.
All fixture hostnames resolve to loopback; other hostnames are blocked. The
generated certificate is trusted only by its public-key fingerprint in that
temporary Chromium process. No system trust settings or real accounts change.

The real extension is loaded unchanged. To avoid a native headless permission
dialog, the harness preapproves one host through Chromium's extension settings
before using the actual popup permission request. Node checks separately cover
denial and synchronous user-gesture handling. Browser checks cover exact-origin
grant, autofill, manual submission, expired/reused codes, wrong identity, redirect
blocking, and actual permission revocation. Results and screenshots are written to
`extension/test-results/`; set `EXTENSION_TEST_OUTPUT` to choose an artifact folder
and `PYTHON` to select the fixture interpreter. These checks do not certify a live
Tailscale deployment or current ATS form layout.

## Automatic tracking (version 1.1)

In dashboard **Settings → Connect a browser**, generate a five-minute code.
Paste it into the extension and choose **Connect browser** once. Registration
survives dashboard and browser restarts and can be revoked in Settings. Reload
an unpacked extension after upgrading, and refresh application pages that were
already open. The legacy per-application handoff is retained under its own
collapsed section for existing workflows.

Open an Ashby, Greenhouse, or Lever posting from anywhere. The extension resolves
its URL to the ATS posting ID, including supported ATS frames embedded in company
pages. Opening a posting does not create an application. A submission attempt
creates or reuses a private application record; missing postings are saved as
browser discoveries without changing collector coverage or freshness.

The popup and dashboard distinguish **Submission attempted**, **Request sent ·
unconfirmed**, **Submitted · awaiting email**, **Email confirmed**, and failures.
Only a recognized success page/message or a matching verified email establishes
submission. Greenhouse’s `/jobs/<id>/confirmation` redirect retains the original
job identity; its success message completes a previously recorded attempt.
HTTP 200 alone is insufficient. Unresolved attempts appear for review
after ten minutes. Final submission, signatures and CAPTCHA remain yours.
Autofill is optional and requires clicking **Fill application**.

Tracking stores bounded job identity, timestamps and signal metadata. It does not
read network request bodies, cookies or authorization headers. Credentials are
restricted to trusted extension contexts. A fingerprint of an explicitly selected
resume upload may identify an existing private artifact; an unobserved upload is
recorded as unknown. Version 1.3 separately preserves application answer history,
including an encrypted browser retry queue, as described above. Eligible reusable
autofill answers can still be staged encrypted on the dashboard for up to 24 hours
and are learned only after submission succeeds. Saving history does not grant
permission to autofill legal attestations, compensation, or arbitrary prose.

The extension queues observations through dashboard outages and browser restarts,
retrying with a one-to-five-minute backoff. It never retries employer requests.
Disconnecting clears local tracking state; revoke a browser in Settings to invalidate
its server credential. Unsupported portals and custom forms that do not use a
supported ATS frame are outside this version's coverage.

Local Chromium fixtures exercise all three platforms, an embedded form, failed
requests, uncertain HTTP success, confirmation events, and offline replay after a
full browser restart. They also verify exact long/multi-step answers, intentional
blank edits, excluded secrets, and safe dashboard history rendering after reload.
These tests are not certification of every live ATS layout.
During the real pilot, choose one genuine application on each platform, submit it
yourself, and compare the popup, dashboard timeline, and confirmation email. No
live applications are submitted by automated tests.
