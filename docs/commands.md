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
| LLM teacher labels and proxy models | `python -m job_search.ranking.proxy --help` |
| Salary extraction worker | `python -m job_search.salary.llm --help` |
| Salary training and review | `python -m job_search.salary.teacher --help`, `python -m job_search.salary.review --help` |
| Salary bounds model and review | `python -m job_search.salary.v3 --help`, `python -m job_search.salary.v3_review --help` |
| Inspect stored Greenhouse pay data | `python scripts/analyze_greenhouse_pay_sample.py --help` |
| Isolated demo | `uv run scripts/offline_system_demo.py --interactive --state-dir .cache/demo --port 8770` |
| Offline verification | See [tests](../tests/README.md) |

## Updating older commands

The root Python scripts have moved; they are not compatibility wrappers. Update personal shell aliases or external automation that invoked those paths:

- `job_search_app.py` → `python -m job_search`
- `job_boards.py`, `job_dedupe.py`, `location_enrichment.py` → the collection commands above
- `job_labeler.py`, `preference_model.py`, `preference_proxy.py` → the ranking commands above
- `salary_<name>.py` → `python -m job_search.salary.<name>`

Keep existing command arguments after the module name. For example, `python -m job_search --config /path/to/config.json readiness` uses the same configuration and state as before. Repository-owned workers, Compose services, release tools, and runbooks already use the module commands.

The seed board list lives at `job_search/collection/boards.seed.json`. Existing defaults for databases, model artifacts, and the writable `boards.json` registry cache remain unchanged. Moving source files does not migrate or reset private state. Optional inference and training dependencies remain in `requirements/`.
