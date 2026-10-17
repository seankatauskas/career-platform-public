# Browser application catalog metadata

PR #80 (`0a0fc49`, October 9, 2026) introduced `PairedExtensionAdapter` when
switching production to the Applications owner. The adapter retained browser
titles and employer labels as job metadata without the legacy catalog resolution
and company-board check. The compatibility projection also preferred those labels
over available catalog metadata. Existing tests covered exact evidence, authority,
and retries, but did not inject a catalog into the paired ingress.

The replacement ingress now resolves the exact ATS/posting ID inside the command
transaction, after checking its replay receipt. It validates a recognizable
catalog URL against the observed board and captures the scanned title, company,
and posting URL through the Applications owner. Original browser evidence and
its digest stay unchanged. A custom employer URL remains supported. An absent
posting permits browser fallback; a catalog error leaves the durable browser
queue retryable instead of pretending the posting is absent.

The same omission left browser-created records without retained catalog snapshots.
They depended on later catalog availability for their posting URL and metadata.
Capture now retains that context. Existing saved snapshots and recommendation
provenance are preserved, and combined job identities are not overwritten by a
secondary source. Unsnapshotted application cards prefer catalog labels while
their durable metadata is repaired.

## Existing-record repair

Use the configured `application_owner_db`, not the retired `applications` table
in the operational `application_db`. Preview is read-only and takes explicit IDs:

```sh
python -m job_search.application_catalog --config /run/job-search/config.json \
  --application-id app_EXAMPLE > /tmp/catalog-repair-plan.json
```

Review the before/after values. Only records with paired-browser evidence, one
unambiguous source, no existing snapshot, and a matching scanned posting qualify.
The plan binds each application version and exact catalog snapshot digest.

```sh
python -m job_search.application_catalog --config /run/job-search/config.json \
  --apply-plan /tmp/catalog-repair-plan.json
```

Run the approved plan after deploying this release. Keep a consistent private
backup first. Repairs use the command executor and owner API, append an audit
event, and preserve application IDs, lifecycle versions, activity dates,
submissions, reviews, answers, and browser evidence. A retry of the same plan
returns its existing receipt. If interrupted, the batch may be partially applied;
replay the same plan. Changed versions or catalog metadata require a new preview.
The snapshot records catalog metadata at repair time, not an invented historical
capture. No applications are submitted and no messages are sent.

## Verification

`python -m tests.test_application_catalog` covers metadata precedence, snapshot
retention, Greenhouse/Ashby/Lever board mismatches, custom landing pages, missing
and unavailable catalogs, queue replay after upstream changes, preservation of
saved recommendation provenance, and version-bound audited repairs. The gateway,
owner-boundary, command, and release suites cover integration and packaging.
