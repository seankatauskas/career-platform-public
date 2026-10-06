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
```

`test_*.py` files are executable Python modules, discovered automatically by `scripts/check-system.py`. Add a new suite here and give it a `__main__` entry point that exits nonzero on failure. Import shared fixtures as `from tests.test_… import …`; use the repository root for source assets and `tests/fixtures/` for checked-in test data.

Dashboard browser checks live in `browser/`. Extension-specific checks remain beside their implementation in `extension/`; Terraform checks live in `infra/aws/tests/`. The system runner includes the extension unit suite and adds all browser checks with `--browser`. Infrastructure checks have their own workflow.

The runner writes a JSON receipt to `.cache/system-checks.json` by default. Screenshots and temporary databases are generated output and must not be committed.

## Release and recovery development

```bash
python3 -m tests.test_job_search_aws_ops
python3 -m tests.test_release_coordinator
python3 scripts/check-system.py --match release
uv run --with cryptography --with pypdf python scripts/release-transition-acceptance.py --local
```

Install the repository's fast release checks as a local hook with
`git config core.hooksPath .githooks` when no other hook manager is configured.
CI runs the full system/browser checks and native Linux Compose/transition gates;
the hook runs only the inexpensive operations and release contracts.
See [production acceptance](../docs/compose-acceptance.md) and
[deployment/recovery](../docs/operations/aws-deployment.md) for the Docker and live gates.

Browser demo fixtures use `requirements/demo.txt`. The system runner passes its Python interpreter to the demo and fixture commands; standalone browser runs use `uv` to provision the demo dependencies.

For a loopback preview of the complete v2 review flow, run
`python3 -m tests.fixtures.review_quality_demo` and open the printed URL plus a
printed list's `dashboard_path`. It uses fictional facts and postings, simulated
availability, and a temporary database. It exercises saved preferences, independent
assessment slots, calibration, publication, grouped cards, and visible conditions.
The corresponding browser check saves desktop/mobile screenshots under
`.cache/review-quality/` and performs no external requests.
