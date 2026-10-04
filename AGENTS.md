<!-- OPENWIKI:START -->

## OpenWiki

This repository uses OpenWiki for recurring code documentation. Start with `openwiki/quickstart.md`, then follow its links to architecture, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

<!-- Hand-written; outside the OPENWIKI markers so regeneration preserves it. -->

## Repository layout

Start with `README.md` for the source map and `docs/README.md` for current guides.
Python suites belong in `tests/`, dashboard browser checks in `tests/browser/`,
configuration templates in `examples/`, and dependency groups in `requirements/`.
Application code belongs in `job_search/collection/`, `job_search/ranking/`,
`job_search/salary/`, or the existing service packages. Run commands with `python -m`
as listed in `docs/commands.md`; do not add root Python scripts.
Generated OpenWiki pages can contain historical paths;
use `tests/README.md` for current verification commands.

## Running the tests

The suite is offline, needs no network and no dependencies, and finishes in about a
second. Run it before and after any change — do not document behaviour as unverified
without trying both commands below.

```bash
uv run python -m tests.test_job_boards     # preferred: uv provisions Python 3.11
python3 -m tests.test_job_boards    # fallback: works on any Python >= 3.9, including
                              # macOS's system python3, with uv absent from PATH
```

The fallback exists specifically for tooling that runs with a minimal `PATH`. If `uv`
is not found, use the second command rather than reporting that the tests could not be
run. Both print `ok` on success.

## Do not run casually

`--refresh-boards` and `--all` are live network operations across 13,146 boards on three
platforms, taking tens of minutes. Never in a loop. Everything you need to verify a code
change is covered by the offline suite. `--limit 10` plus `--ats <one>` is the cheap way
to exercise a real request path.

`--refresh-recent` is the cheap discovery pass (~4 minutes) and `--since 7d` narrows to
recent postings — still live network calls, so the same rules apply.

`--grep` on Greenhouse requests full job descriptions, which is roughly **26x** the
bytes of a normal run. Do not add it casually to an unlimited run.

## Freshness: the fetch is live, the postings are not

Every run reads the platform APIs directly, so nothing is served from a cache. The
postings themselves are old: the median in a full pull is **62 days**, because companies
leave requisitions listed. It skews hard by platform, so "how stale is this row" depends
on which ATS it came from:

| ats | jobs | median age | >1yr |
|---|---|---|---|
| ashby | 54,591 | 48d | 3.9% |
| greenhouse | 180,915 | 60d | 13.0% |
| lever | 72,594 | **97d** | **26.2%** |
| **all** | 308,100 | **62d** | 15.6% |

**Lever's tail is upstream reality, not a parsing bug.** Palantir's board carries a
posting with `createdAt` **2009-12-05**, verified against the raw API. Do not "fix" a
normaliser because its dates look implausible — check the API response first.

You cannot lower the age of what exists, only choose what to collect, and the two flags
catch different things:

- **`--since 1d --sort recent`** is the freshest-first pairing that matters. A real run
  returned **5,980** postings from the last 24 hours, 133 within the previous hour, the
  freshest **6 minutes old**. `--sort` defaults to `board`, which groups by platform and
  company and buries the newest postings in the middle of the file. `--since 7d` gives
  35,490 rows at a 4-day median.
- **`--new-only`** is about the database, not the calendar: postings never seen before,
  at any age. It catches the 200-day-old requisition that appeared on a board today,
  which `--since` cannot. It compares `(ats, id)` keys, so it errors with `--no-db`.

No platform supports server-side date filtering — `updated_after` and friends are
silently ignored on all three — so `--since` never makes a run cheaper. Every board is
fetched in full and the window is applied locally. **Latency to a new posting is set by
how often the scraper runs, not how narrow the filter is**; ~4 minutes is the floor for a
full sweep.

Board *discovery* lags separately, by a median of **48 days**: a company is invisible
until the Internet Archive crawls its board, which `--refresh-recent` shortens via
urlscan.io. That is a one-time cost per company, not a staleness tax on jobs — for the
~13,000 already-known boards it is paid.

## Performance: what to change and what not to

This tool is network-bound. Parsing and normalisation are **0.2%** of a run, so
optimising them is wasted effort. Only round trips and connections matter.

Connections are pooled per thread against the three posting-API hosts, which took a full
run from 6m35s to 3m51s. Two consequences:

- **Add a new ATS host to `_POOLED_HOSTS`**, or it silently opts out of that. A test
  enforces this.
- **Read headers in lowercase.** The pooled path returns a plain dict, unlike urlopen's
  case-insensitive `Message`. These APIs already disagree — ashby and greenhouse send
  `etag`, lever sends `ETag` — and a case-sensitive lookup for `Content-Encoding` would
  return gzip bytes as if they were JSON.

**Do not raise `--concurrency` to make something faster.** It is the one lever that works
by moving cost onto Ashby, Greenhouse and Lever. 8 is a requirement, not a tunable.

## If you add a filter, update BOTH gates

Two functions in `job_search/collection/boards.py` decide what a run is entitled to conclude. A new filter
missing from either produces a normal-looking run that quietly corrupts data. These are
the single most damaging things to get wrong here.

**`may_close_postings()`** — `closed_at` asserts a posting is gone. Only a run that saw
every posting on a board may set it. A filter missing from this list means
`--all --since 7d` marks everything older than a week as closed.
Pinned by `test_only_an_unfiltered_run_may_close_postings`.

**`may_use_etags()`** — a `304` says the body is unchanged, and treating that as "no new
postings" also requires that the fetch which stored the ETag persisted *every* posting. A
filter missing from this list means a `--title` run stores an ETag after saving matching
rows only; a later run then skips that board on `304`, so its other postings stay
invisible even once a query matches them.
Pinned by `test_etags_are_only_trusted_on_an_unfiltered_run`.

Both fail silently and permanently. Add your filter to both functions and both tests in
the same change.

Set `JOB_SCRAPER_CONTACT` to a real address before any network run. Ask for one;
do not invent it.

## Read before changing behaviour

`docs/collector-guide.md` has a "For coding agents (LLMs)" section listing behaviours that look like
bugs and are deliberate, each pinned by a test. Read it before changing filter logic,
the database lifecycle, board validation, or any per-platform normaliser.

The normalisers in `job_search/collection/boards.py` are where platform differences live. Two that have
already caused silent, wrong-looking-correct output: Lever's job title is `text`, not
`title`, and its `createdAt` is epoch milliseconds rather than ISO.

## Publishing a caller-selected shortlist

For recurring broad/targeted reviews, use `skills/career-job-review/SKILL.md` and
`docs/agent-job-reviews.md`. Review tools preserve strict posting windows, private
evidence, and resumable coverage. They never invoke ranking or submit applications.

For other caller-selected lists, after collection finishes, an agent may query the configured jobs database read-only,
choose existing jobs, and publish them with `python -m job_search --config <config>
shortlist publish --input -`. See `docs/curated-shortlists.md` for the JSON contract.
Use the publishing API rather than writing shortlist tables directly. Selection and
SQL belong to the calling agent; the application only stores and displays its picks.
