# Tests

Run commands from the repository root. The suites use fictional data and local fixtures; they do not submit applications or contact live providers.

```bash
# Dependency-free collector regression suite
uv run python -m tests.test_job_boards
# Fallback when uv is unavailable
python3 -m tests.test_job_boards

# All offline Python suites and extension unit tests
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py

# Include dashboard and extension browser checks
npm ci --prefix extension
npx --prefix extension playwright-core install chromium
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --browser

# One area, using the same runner
uv run --with cryptography --with pypdf --with reportlab python scripts/check-system.py --match career
node tests/browser/test_console_browser.mjs
node tests/browser/test_ops_browser.mjs
node tests/browser/test_review_queue.mjs
node tests/browser/test_settings_views.mjs
node tests/browser/test_review_quality.mjs
node tests/browser/test_owner_dashboard.mjs
node tests/browser/test_owner_application_view.mjs
node tests/browser/test_owner_review_view.mjs
```

`test_*.py` files are executable Python modules, discovered automatically by `scripts/check-system.py`. Add a new suite here and give it a `__main__` entry point that exits nonzero on failure. Import shared fixtures as `from tests.test_… import …`; use the repository root for source assets and `tests/fixtures/` for checked-in test data.

Dashboard browser checks live in `browser/`. Extension-specific checks remain beside their implementation in `extension/`; Terraform checks live in `infra/aws/tests/`. The system runner includes the extension unit suite and adds all browser checks with `--browser`. Infrastructure checks have their own workflow.

The runner writes a JSON receipt to `.cache/system-checks.json` by default. Screenshots and temporary databases are generated output and must not be committed.

## Release and recovery development

```bash
python3 -m tests.test_job_search_aws_ops
python3 -m tests.test_prepared_snapshots
python3 -m tests.test_preparation_reference
python3 -m tests.test_snapshot_pause_boundaries
python3 -m tests.test_backup_archive
python3 -m tests.test_release_recovery
python3 -m tests.test_release_coordinator
python3 -m tests.test_schema_lifecycle
python3 -m tests.test_optional_schema_lifecycle
uv run --python 3.12 --with cryptography --with pypdf python -m tests.test_release_startup_boundary
python3 -m tests.test_deployment_modes
python3 -m tests.test_deployment_contract
python3 scripts/check-system.py --match release
uv run --with cryptography --with pypdf python scripts/release-transition-acceptance.py --local
```

Install the repository's fast release checks as a local hook with
`git config core.hooksPath .githooks` when no other hook manager is configured.
CI runs the full system/browser checks and native Linux Compose/transition gates;
the hook runs only the inexpensive operations and release contracts.
See [deployment modes](../docs/operations/deployment-modes.md),
[production acceptance](../docs/compose-acceptance.md) and
[deployment/recovery](../docs/operations/aws-deployment.md) for the Docker and live gates.
The deployment-mode tests verify conservative fallback and application-data
preservation without invoking snapshot/migration work on an eligible edge; their
host adapters do not establish production timing. Contract tests fence runtime
inputs from the static-asset/documentation exception and protocol 2's exact audited
`runtime_readiness.py` source. Invalid/stale audit declarations, changed helpers,
schemas, dependencies, runtime files and executable modes remain conservative.
Startup tests cover optional configured/present schemas and reject missing/drifted
state without preparing it. Transition diagnostics execute paused/enabled,
summary/detailed and snapshot/provided/local observations against legacy and
converted-owner fixtures; an intentionally injected schema write must fail the
read-only gate. Candidate writes must still survive predecessor reads and writes.
A clean, exact-source Docker transition receipt remains required for production eligibility;
local subprocess receipts cannot grant it. Full restore and scheduled-backup checks
remain separate obligations.

Browser demo fixtures use `requirements/demo.txt`. The system runner passes its Python interpreter to the demo and fixture commands; standalone browser runs use `uv` to provision the demo dependencies.

For a loopback preview of the complete v2 review flow, run
`python3 -m tests.fixtures.review_quality_demo` and open the printed URL plus a
printed list's `dashboard_path`. It uses fictional facts and postings, simulated
availability, and a temporary database. It exercises saved preferences, independent
assessment slots, calibration, publication, grouped cards, and visible conditions.
The corresponding browser check saves desktop/mobile screenshots under
`.cache/review-quality/` and performs no external requests.

For a bounded fictional SQLite/file snapshot profile, run
`python3 -m tests.benchmark_deployment_phases --megabytes 128 --runs 1`.
Use `--megabytes 11264 --runs 3 --output .cache/snapshot-profile.json` explicitly
for the approximately 11 GiB fixture; allow at least 40 GiB of temporary space.
This optional disk benchmark is not part of the fast suite and does not measure
AWS downtime. The release-transition and Compose acceptance receipts also record
fixture execution and service-health timings.

`python3 -m tests.benchmark_host_preparation --megabytes 128 --output .cache/preparation.json`
compares cold and reusable verification references, changed data, WAL and invalid
metadata on fictional state. On a local Linux volume, `--evict` evicts only the
fixture's file pages and records their residency with `mincore`. A reusable
verification reference does not imply a warm OS page cache. The optional
`--check-workers 2` is a benchmark-only hypothesis, not a production setting.
See [host preparation evidence](../docs/operations/host-preparation-performance.md)
for resource limits, measured cases and limitations. Never run it on production.

## Incremental ranking

`uv run python -m unittest tests.test_incremental_families tests.test_policy_refresh`
checks full-pass parity, inserts/deletions/moves, historical template lineage,
metadata edits, observation-only updates, missing journals, rollback, concurrent
collection, and partial scoring recovery. Run `uv run python -m tests.test_job_dedupe`
for the existing full-rebuild contracts.

`uv run python -m tests.benchmark_incremental_ranking --families 3000` measures
bounded fictional preparation and feature/scoring work with deterministic stub
predictions, no model inference, and no network. It reports checked/recomputed
families and phase timings; it does not predict production elapsed time.

Per-attempt recommendation resource evidence is verified by
`uv run python -m tests.test_review_resource_monitor` and the OLD METHOD lifecycle
suite. Release preparation also exercises host/cgroup capture through the native
`tests.probe_old_method_container --require-host-runtime` probe, without paid calls.

`uv run --python 3.12 python -m tests.test_review_input_index` checks indexed input
selection against the original full-scan algorithm, including alternate timestamps,
strict boundaries, index migration, and concurrent writes during snapshot reads.
`uv run --python 3.12 python -m tests.benchmark_review_inputs --rows 360000 --description-bytes 2048`
builds a temporary fictional catalog (about 1.5 GB) and compares original input
selection, the legacy scan with the new index, and indexed candidate selection.
It reports index build time/storage and the query plan. This is a warm local
benchmark without cache eviction or production timing claims; the default fixture
uses 100,000 historical rows and smaller descriptions.

On the local Python 3.12 run on 2026-10-17, the 360,000-row historical fixture
plus 1,000 recent discoveries occupied 1,490,579,456 bytes. Both implementations
selected the same 122 eligible jobs. Median times over three warm runs were
0.534 seconds before indexing, 0.353 seconds for the old scan with the new covering
index, and 0.00194 seconds for indexed candidate selection. Both candidate-query
branches used covering index searches. Building the index took 0.385 seconds and
added 27,328,512 bytes. A separate 10,000-row unchanged date/status update proxy
took 0.0179 seconds before indexing and 0.1055 seconds afterward, illustrating the
additional write cost. These fictional local measurements do not predict EC2
input-freezing time, disk-cache behavior, collector duration or installation pause.

For an explicitly requested historical input replay, run
`python -m tests.probe_review_input_replay --app-db /path/to/application.db --review-id REVIEW_ID`.
The probe loads the saved window and ordered snapshot payloads, verifies their stored
fingerprints, and reproduces their complete payload/order using the old selector and
the actual current `Store` against a private temporary indexed catalog. It runs no
models or publication. Add `--jobs-db /path/to/jobs.db --compare-current` explicitly
to compare both selectors against the current catalog in one read transaction;
this includes a full historical-date scan and can be expensive. Current drift is
reported separately from algorithm parity. Source databases use read-only/query-only
connections, and the probe never installs a live index. Output contains counts,
timings, digests and the review/window identifiers without job contents or job IDs.
Run `uv run --python 3.12 python -m tests.test_review_input_replay` for its offline
corruption, concurrency, privacy and historical/current separation regressions.
