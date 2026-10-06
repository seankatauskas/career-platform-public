# Command reference

Run commands from the repository root. Python imports and background workers use the same package paths. Prefix commands with `uv run` when using uv to select the interpreter.

| Purpose | Command |
| --- | --- |
| Application ledger, dashboard, and operator tools | `python -m job_search --help` |
| Publish agent-selected jobs | `python -m job_search --config /path/to/config.json shortlist publish --input -` ([guide](curated-shortlists.md)) |
| Collect postings and discover boards | `python -m job_search.collection.boards --help` |
| Deduplicate job families | `python -m job_search.collection.dedupe --help` |
| Normalize locations | `python -m job_search.collection.locations --help` |
| Label jobs and review recommendations | `python -m job_search.ranking.labeler --help` |
| Train, evaluate, and refresh ranking models | `python -m job_search.ranking.model --help` |
| Refresh explicitly selected ranking policies | `python -m job_search.ranking.refresh --help` ([efficiency guide](operations/runtime-efficiency.md)) |
| Inspect runtime durations and queued work read-only | `python -m job_search.efficiency --db /path/to/job-search.db --days 7` |
| Estimate prepared ranking cache demand read-only | `python -m job_search.ranking.cost_estimate --help` ([limits and examples](operations/runtime-efficiency.md)) |
| Plan bounded selective candidates without inference | `python -m job_search.ranking.staged --help` ([budgets and scope](operations/runtime-efficiency.md)) |
| Derive sparse versions of broad and selective policies | `python -m job_search.ranking.sparse --help` ([migration steps](operations/runtime-efficiency.md#sparse-scoring-for-both-policies)) |
| Inspect current Linux memory counters | `python -m job_search.resource_usage` |
| LLM teacher labels and proxy models | `python -m job_search.ranking.proxy --help` |
| Salary extraction worker | `python -m job_search.salary.llm --help` |
| Salary training and review | `python -m job_search.salary.teacher --help`, `python -m job_search.salary.review --help` |
| Salary bounds model and review | `python -m job_search.salary.v3 --help`, `python -m job_search.salary.v3_review --help` |
| Inspect stored Greenhouse pay data | `python scripts/analyze_greenhouse_pay_sample.py --help` |
| Isolated demo | `uv run scripts/offline_system_demo.py --interactive --state-dir .cache/demo --port 8770` |
| Shared AWS release status | `python -m job_search.release_coordinator status` ([setup](operations/coordinated-releases.md)) |
| Propose the next reviewed release predecessor | `python -m job_search.release_coordinator propose-policy --output .cache/release-policy.proposed.json` |
| Offline verification | See [tests](../tests/README.md) |

## Updating older commands

The root Python scripts have moved; they are not compatibility wrappers. Update personal shell aliases or external automation that invoked those paths:

- `job_search_app.py` → `python -m job_search`
- `job_boards.py`, `job_dedupe.py`, `location_enrichment.py` → the collection commands above
- `job_labeler.py`, `preference_model.py`, `preference_proxy.py` → the ranking commands above
- `salary_<name>.py` → `python -m job_search.salary.<name>`

Keep existing command arguments after the module name. For example, `python -m job_search --config /path/to/config.json readiness` uses the same configuration and state as before. Repository-owned workers, Compose services, release tools, and runbooks already use the module commands.

The seed board list lives at `job_search/collection/boards.seed.json`. Existing defaults for databases, model artifacts, and the writable `boards.json` registry cache remain unchanged. Moving source files does not migrate or reset private state. Optional inference and training dependencies remain in `requirements/`.

## Agent job reviews

`python -m job_search review ACTION --dashboard-url ORIGIN --input FILE` uses the
private dashboard and the same operations as `review_*` MCP tools. Set
`CAREER_DASHBOARD_URL` to avoid repeating the origin; `--input -` accepts JSON stdin.
See [the review guide](agent-job-reviews.md) for scope, evidence, and publication rules.

The isolated AWS reviewer is launched on the host with
`python3 -m job_search.review_host login|readiness|run|status`; see the
[isolated-review runbook](operations/isolated-reviews.md). Its timer remains disabled
until a schedule is explicitly configured.
