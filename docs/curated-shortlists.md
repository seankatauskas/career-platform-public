# Publish Codex picks

A Codex session chooses jobs from the existing catalog, then publishes its ordered
selection to Shortlist. Each publication creates one durable database record with
ordered job entries. The app does not choose, rerank, or train on these selections.

For recurring, evidence-backed reviews, use the [agent review workspace](agent-job-reviews.md).
It adds coverage tracking, independent checks, and atomic broad/targeted publication.
The direct publishing contract below remains available for other caller-selected lists.

## Agent workflow

1. Wait for the requested collection refresh to finish. Use the configured `jobs_db`
   in read-only SQLite mode to inspect jobs and descriptions. The agent owns its SQL,
   date interpretation, suitability judgments, and requested number of picks.
2. Submit catalog `(ats, id)` identities in chosen order, with optional explanations.
3. Return the publisher's `dashboard_url` to the user. No upload or dashboard approval
   is required to publish a shortlist; publication does not start applications.

From this checkout, using the installation's existing private runtime config:

```bash
python -m job_search --config /path/to/private/config.json shortlist publish --input - <<'JSON'
{
  "title": "Today's best fits",
  "idempotency_key": "codex-picks-2026-09-28-run-1",
  "window_start": "2026-09-27T18:00:00Z",
  "window_end": "2026-09-28T18:00:00Z",
  "jobs": [
    {
      "ats": "ashby",
      "job_id": "replace-with-an-existing-catalog-id",
      "explanation": "Explain why this job fits the user's experience."
    }
  ]
}
JSON
```

The identifiers above are placeholders. Use actual catalog values. The same command
accepts `--input /path/to/list.json` for agent-created artifacts. Its stdin format is
also the argument object for the `publish_curated_shortlist` MCP tool.

The client discovers the loopback MCP port and owner-only token file from runtime
configuration. It sends authenticated requests to the existing `/mcp` endpoint and
prints a receipt containing `list_id`, title, count, creation time, and dashboard URL.
It never prints credentials. The dashboard and MCP processes must run this updated
code against the same application database. When integrating this branch, restart
those two services using the existing installation procedure; developing in a worktree
does not update an already-running installation.

## Publication contract

- `title`: required, 1–200 characters.
- `idempotency_key`: required identifier, at most 255 characters. Retry the same
  request with the same key. Use a new key for a changed selection or another run.
- `jobs`: required ordered array, 0–500 entries. Each contains `ats` (`ashby`,
  `greenhouse`, or `lever`), `job_id`, and optional `explanation` (up to 2,000 characters).
  Array position determines rank. An empty array records that no suitable jobs were found.
- `window_start` and `window_end`: optional pair of UTC timestamps ending in `Z`.
  These describe what the agent reviewed; the app does not filter or verify the selection
  against that window. A posting's modification date is not its original posting date.

Unknown identifiers, duplicate jobs, invalid fields, and changed content using an
existing idempotency key fail without partial publication. The entire MCP request must
fit within 64 KiB; shorten explanations when needed. A successful retry returns the
original saved list even if catalog data subsequently changes.

## Dashboard behavior

Shortlist defaults to the newest Codex list once one exists, with a switch to Model
picks. The dated selector retains older lists and multiple runs per day. Earlier lists
loads another page of history. The page checks for publications on entry and window
focus, and has an explicit Refresh saved lists button.

The app preserves the supplied order and explanation, showing job dates and current
closure/application status. Already-tracked jobs link to their draft or application. Open posting and reading
role details do not create records. Prepare application explicitly creates a draft using
the existing resume workflow. Closed jobs remain visible, with new preparation disabled.
Preparation records the source list and rank without creating model impressions or
scores. Drafts are accessible through the Applications page’s Show filter and are
excluded from its default list and count. Recorded submissions move into Applications.
Application submission remains manual.

Both Codex and Model picks offer a **Sort by** control: original list order/relevance,
posting date (newest or oldest first), or employer update date (newest or oldest first).
Sorting rearranges only the displayed list: membership, saved ranks, explanations,
and application provenance remain unchanged, and no collection or ranking is run.
The browser remembers the selected sort across lists and reloads. Missing or invalid
dates appear last in either direction, with original order retained for ties.
Discovery dates and ambiguous legacy Greenhouse dates are not used as publication dates.

Lists live in `curated_shortlists` and their entries in `curated_shortlist_items`, in
`application_db`. Schema migration 11 is additive; existing model shortlists retain their
existing behavior. Saved lists survive restarts and are covered by normal private-state
backups. Do not share production list records in screenshots or test fixtures.
