# Local resume lab runbook

For the career-database workflow, start with [Career bank and tailored resumes](career-resume-runbook.md).
An approved career profile supplies job-specific facts without requiring a handwritten
standard; newly generated PDFs use the bundled Jake Gutierrez template. The imported
standard comparison mode described below remains available and preserves older artifacts.

The resume lab is an optional, private comparison workbench attached to the job-search
dashboard. It scores the PDF text that an ATS would actually parse, recommends the
best hand-written resume for the exact job, and generates bounded comparisons only
after an application is opened. It is an explainable proxy, not an employer's score
or a prediction that a recruiter will advance the application.

## What the workbench shows

The workbench has five choice groups for each job:

1. **Hand-written standards:** every active resume authored and imported by the user,
   up to the enforced limit of 25, ranked by its job-specific ATS proxy score. Each is
   a real selectable choice. The highest-scoring standard is the recommendation.
   Manual rank breaks score ties; it does not override a higher job-specific score.
   Selection is always explicit.
2. **Grounded rewrite:** a factual keyword/terminology optimization of that same winning
   standard. Every editable slot must cite exactly one imported source claim, remain
   at that claim's original path, and preserve all of its canonical concepts in order.
   Only articles, punctuation, case, and reviewed equivalent terminology may change.
   Numbers also retain their local concept association; comparison/math operators,
   currency, ranges, negation, seniority, and scope cannot be altered or introduced.
   A cited global skill may be added only to the summary or skills—not projected into
   an employer or project claim. The dashboard exposes the PDF and complete path-labelled
   diff before explicit, run-specific user approval can be recorded.
3. **Standard exaggerated:** a synthetic comparison that keeps the winning standard's
   identity, employers, roles, dates, education, project headers, skill categories,
   and editable slot counts while permitting invented claim text inside those slots.
   Every editable slot has one provenance record: unchanged text cites its same-path
   source claim, while changed text is isolated as synthetic with no source citation
   or equivalent-term assertion.
4. **Market ideal:** an independent synthetic example of a strong candidate for the
   role.
5. **Keyword adversarial:** an independent synthetic attempt to maximize the proxy
   score, including one bounded score-feedback generation pass.

The last three are comparison-only local research artifacts. They are visibly marked
**SYNTHETIC RESEARCH BENCHMARK - NOT FOR APPLICATION**, stored in a separate artifact
namespace, and cannot be selected for an application. “Ideal” and “adversarial”
describe the generation objective; neither is guaranteed to be globally optimal or to
reproduce a particular vendor's private ranking system. The research notice is
verified in both PDF extraction views and excluded from the fit calculation.

The recommended option is always the highest-scoring hand-written standard, not a
generated artifact. Choosing any real resume remains an explicit click. Reopening a
workspace shows the exact immutable standard ranking that selected that run, even when
a standard is later revised or archived. After submission the workspace is read-only;
exact command replays return their recorded result, while new generation, approval,
retry, and selection commands fail closed.

## Current capability boundary

The implemented proxy:

- compiles an imported TeX standard, extracts logical and layout text from its exact
  PDF, and rejects material loss, duplication, or ordering damage;
- derives source-anchored job requirements with deterministic extraction plus an
  optional local-model pass;
- performs deterministic exact-term, alias, acronym, numeric, eligibility, and
  recruiter-search visibility scoring;
- optionally asks the local model to adjudicate only unresolved requirement/claim
  pairs, normally through the supported bounded batch-evidence task, with evidence
  constrained to exact imported-resume spans; and
- computes the final versioned score deterministically. The model does not choose or
  overwrite the numeric score.

Grounded PDFs are selectable only after the generated PDF and complete path-labelled
source diff have been reviewed and explicitly approved. Hidden model-provided link
targets are removed: real contact links can only be derived from visible, attested
contact labels, and generated project links are stripped. A research-only generation
failure does not discard a successfully completed grounded result; approval waits for
the comparison run to reach a terminal state and binds the exact grounded artifact.

Hermes receives a purpose-built compact summary: scores and eligibility for every
option, plus only the leading gaps for the winner and generated comparisons. Resume
wording, diffs, TeX, filesystem paths, and artifact locators do not cross that
read-only capability.

There is no embedding-retrieval stage in version 1. There is also no vendor-specific
Greenhouse, Ashby, Lever, or Workday formula: no public universal formula exists. The
score is useful for comparing these resume artifacts against the same job and for
explaining missing evidence, not as a hiring probability.

The repository includes the guarded JSON adapter and one-shot offline driver at
`job_search/resume_lab/local_model_driver.py`. It does **not** bundle a model backend,
weights, or an inference server. A version-1 model config runs CPU inference through
`llama-cpp-python` with one local GGUF file. A version-2 config uses a pinned Runpod
Serverless worker-vLLM endpoint through async `/run` plus bounded `/status` polling, so
scale-to-zero cold starts do not depend on a long synchronous HTTP connection. Both
paths feed the same task-specific grounding and output validators.

The local driver also has unit-tested MLX adapter code, but MLX/Metal behavior inside
the deny-default macOS sandbox has not been validated; an `mlx-lm` production config
therefore reports `model_backend_unvalidated` and fails closed. Hermes receives only
bounded read-only resume metadata through its capability interface; it cannot generate,
approve, select, or download resume contents.

## Install the PDF dependency

Install the optional parser into the same Python environment used for the dashboard
and workers:

```bash
python3 -m pip install -r requirements/resume.txt
```

`requirements/resume.txt` pins `pypdf==6.16.2`. The main scraper remains
dependency-free; this package is needed only for resume PDF extraction. It deliberately
does not install a local inference backend or download model weights. The cloud image
installs it in the dedicated networkless document-tool service as well.

## Pin Tectonic for offline compilation

Supply a known Tectonic executable and a complete local bundle file. Neither is
bundled in this repository. Record the exact executable release in
`resume_tectonic_version` and do not replace either file in place. Each artifact also
records the actual bundle SHA-256.

Compilation uses `--untrusted`, `--only-cached`, the explicit bundle, a temporary
directory, resource limits, and actively capped combined process output. On macOS the
compiler and parser use deny-network `sandbox-exec` profiles with narrowly scoped read
paths. A Linux cloud deployment instead configures `tool_service_socket`: compiler and
parser execution moves to a read-only, no-capability, networkless container that has no
database or secret mount. The application revalidates exact schemas, hashes, PDF
signatures, parser bounds, and engine version across the private Unix socket. Both
paths fail closed when their isolation boundary is unavailable.

An imported source is not arbitrary LaTeX. It must be one regular UTF-8 file no larger
than 500 KB, with exactly one `article` document and one document environment. The
only accepted packages are `geometry`, `enumitem`, `hyperref`, and `titlesec`.
File inclusion, images, bibliography inputs, shell execution, dynamic file I/O, and
similar commands are rejected. Convert an existing resume to this safe single-file
subset before import; do not weaken the preflight to accommodate an untrusted package.

## Configure the lab and model

Add these fields to the owner-only runtime config. Relative paths resolve beneath
`project_root`; private state is better kept outside the Git checkout.

```json
{
  "resume_lab_db": "/absolute/private/path/resume-lab.db",
  "resume_artifact_root": "/absolute/private/path/resume-artifacts",
  "resume_model_config": "/absolute/private/path/resume-model.json",
  "resume_tectonic_executable": "/absolute/path/to/tectonic",
  "resume_tectonic_bundle": "/absolute/path/to/tectonic-bundle-file",
  "resume_tectonic_version": "the-pinned-release-label"
}
```

For travel/cloud inference, copy `examples/resume-model.runpod.example.json` outside the
repository, fill its endpoint ID, exact Hugging Face commit, deployed worker-image
digest, and owner-only API-key path, then use that file as `resume_model_config`.
`deploy/runpod/README.md` contains the safe dry-run-first endpoint lifecycle. The
revision and image digest are operator-pinned provenance; the response independently
confirms the configured model ID, while promotion still requires the repository evals.

On a Linux Compose host, omit `resume_tectonic_executable` and
`resume_tectonic_bundle` from the application config and set:

```json
{
  "tool_service_socket": "/run/job-search-tools/tools.sock",
  "resume_tectonic_version": "the-pinned-release-label"
}
```

The networkless service owns the executable and bundle mounts described in
`docs/operations/cloud-deployment.md`; local macOS configuration continues to use the two direct paths.

`resume_lab_db` and `resume_artifact_root` must be configured together. The three
Tectonic fields must likewise be complete. The artifact repository creates owner-only
directories and separates `real/` from `research/`; the SQLite sidecar is mode `0600`.

Create a dedicated private virtual environment and install one supported backend. For
example, the checked-in JSON template uses `llama-cpp-python` with one local GGUF file:

```bash
python3 -m venv /ABSOLUTE/PRIVATE/resume-model-venv
/ABSOLUTE/PRIVATE/resume-model-venv/bin/python3 -m pip install llama-cpp-python
```

Pin and audit the backend version appropriate to the machine. The bundled driver keeps
an MLX adapter for tests and future sandbox work, but MLX is not admitted by the
version-1 production adapter. The job-search runtime never downloads weights; provision
the selected GGUF file separately.

Copy the model-config template outside the repository, replace every uppercase
placeholder with an absolute path, and protect it:

```bash
cp examples/resume-model.example.json ~/.config/job-search/resume-model.json
chmod 600 ~/.config/job-search/resume-model.json
chmod 600 ~/.config/job-search/config.json
```

Hash the final GGUF bytes and copy the lowercase digest into `model_sha256` before
starting the dashboard or worker. Do not replace that file in place. A deliberate
model upgrade should use a new immutable file, digest, and `producer_version`.

```bash
shasum -a 256 /ABSOLUTE/PRIVATE/models/resume-model.gguf
```

The model config is an exact version-1 JSON object. `command` is a fixed argv array,
never a shell string. Invoke the bundled driver with the dedicated environment's
absolute Python, isolated mode (`-I`), an absolute driver path, and explicit backend,
model, context, and generation limits:

```json
{
  "version": 1,
  "producer_version": "local-resume-gguf-v1",
  "model_sha256": "REPLACE_WITH_64_CHARACTER_LOWERCASE_GGUF_SHA256",
  "command": [
    "/ABSOLUTE/PRIVATE/resume-model-venv/bin/python3",
    "-I",
    "/ABSOLUTE/PROJECT/job-search-integration/job_search/resume_lab/local_model_driver.py",
    "--backend", "llama-cpp-python",
    "--model-path", "/ABSOLUTE/PRIVATE/models/resume-model.gguf",
    "--context-size", "32768",
    "--max-tokens", "8192"
  ],
  "allowed_read_paths": [
    "/ABSOLUTE/PRIVATE/resume-model-venv",
    "/ABSOLUTE/PROJECT/job-search-integration/job_search/resume_lab/local_model_driver.py",
    "/ABSOLUTE/PRIVATE/models/resume-model.gguf"
  ],
  "timeout_seconds": 120
}
```

At process startup the adapter hashes the GGUF once and rejects a digest mismatch.
Before and after every inference it compares a device/inode/size/mtime/ctime fingerprint,
so ordinary mutation is detected without rereading all model bytes. Dashboard and model
worker processes also share an owner-only file lock next to this private config, while
threads share an in-process lock. The lock covers the entire one-shot model process and
uses `timeout_seconds` as a bounded admission wait; contention therefore fails visibly
instead of allowing simultaneous weight loads or waiting forever. Public status and
artifact provenance expose the configured digest and version, never model/config paths.

All three read paths are intentional: the sandbox must be able to read the virtual
environment/runtime, the checked-in driver, and the exact model file. Do
not allowlist the project root, home directory, or a general models directory. If the
virtual environment's Python resolves to a runtime outside that environment and outside
the sandbox's system paths, allowlist only that resolved runtime tree as an additional
path.

For each invocation the command reads one JSON object from standard input and writes
one JSON object to standard output. It must dispatch on the request's `task` field and
obey the included `output_schema`. The tasks are:

- `extract_job_requirements`
- `normalize_standard_resume`
- `adjudicate_ambiguous_resume_evidence`
- `adjudicate_resume_evidence_batch`
- `generate_structured_resume_variant`

The bundled production path uses `adjudicate_resume_evidence_batch` to resolve a bounded
set of ambiguous requirements in one isolated invocation; the singular task remains a
compatibility contract for custom version-1 adapters. The adapter invokes the command
without a shell or inherited secrets, denies network access, bounds time and combined
output capture, terminates an overflowing process group, and validates every response.
Keep diagnostics on standard error; standard output must contain only the response JSON.

## Verify setup and import standards

Use the same config for the CLI, dashboard, and workers:

```bash
python3 -m job_search --config ~/.config/job-search/config.json resume doctor
python3 -m job_search --config ~/.config/job-search/config.json resume import \
  --name "Backend" --rank 1 --tex /absolute/path/to/backend-resume.tex
python3 -m job_search --config ~/.config/job-search/config.json resume list
```

`doctor` is side-effect-free: it checks configured paths, digest syntax, executability,
parser availability, model-config permissions, and sandbox availability. It does not
hash the model, compile a sample, or invoke inference. Building the dashboard/model
worker performs the full digest verification. Inspect both `ready_for_import` and
`ready_for_generation`; the command exits with status 2 until full generation is
ready. The first import is the real end-to-end compiler/parser check.

Import preserves the user-authored TeX and exact compiled PDF as an immutable standard
version. With no working model it can still create and lexically score the standard,
but derived comparisons are blocked as `needs_normalization`. Imports are immutable,
so configure the model before import or activate a new version with `resume update`
after changing the source or fixing model setup. The update keeps the stable standard
ID, name, and manual rank while preserving every older version.

Manage active standards with the IDs returned by `import` or `list`:

```bash
python3 -m job_search --config ~/.config/job-search/config.json resume rank \
  --standard-id std_ID --rank 2
python3 -m job_search --config ~/.config/job-search/config.json resume update \
  --standard-id std_ID --tex /absolute/path/to/updated-resume.tex
python3 -m job_search --config ~/.config/job-search/config.json resume archive \
  --standard-id std_ID
python3 -m job_search --config ~/.config/job-search/config.json resume activate \
  --standard-id std_ID
```

At most 25 hand-written standards may be active. Active manual ranks are unique
positive integers. `rank` changes only the score tie-break order. `archive` removes a
standard from future job comparisons without deleting its immutable versions or prior
artifacts; `activate` returns it to the active set if its rank is available.

## Dashboard and worker flow

Start the dashboard and open <http://127.0.0.1:8766>:

```bash
python3 -m job_search.system --config ~/.config/job-search/config.json dashboard
```

When **Open application** is used for an exact job, the dashboard creates the
application in `preparing`, scores every active standard's imported PDF, marks the
highest score as primary, and queues one `resume.optimize` item containing only the
opaque run ID. Requirement extraction and scoring occur during that bounded dashboard
request; local-model failures fall back to deterministic extraction/scoring.

The generated comparisons run only on the `model` worker lane:

```bash
python3 -m job_search.worker --config ~/.config/job-search/config.json --lane model
```

The model worker resumes the durable run, generates and validates the four derived
artifacts, compiles and re-extracts their PDFs, scores them, and completes or visibly
fails each run item. The dashboard polls queued/running results every two seconds.
Launchd wakes the model lane every five minutes, so a newly queued comparison otherwise
waits for the next tick.

Only `keyword_adversarial` receives score feedback: after its first valid draft, the
worker may make one bounded second generation call containing the prior structured
draft and source-anchored remaining gaps, then keeps the better valid score. The factual
rewrite, standard exaggeration, and market-ideal comparison each receive one generation
pass and never see proxy-score feedback.

The `core` worker does not invoke resume generation. It continues to own schedules,
application lifecycle work, Outlook, reminders, and notifications; it cannot consume
the model-lane resume task. Resume approval and selection are explicit dashboard
requests while the application is still `preparing`, and only a parse-safe standard or
that application's approved grounded rewrite can cross into the real selection
boundary.

Prepared applications expose **Resume options** in the application list, so the latest
durable run, its handwritten ranking, any approval, and the current selection can be
reopened after a dashboard reload. Polling also reconciles the narrow crash window
between committing a resume run and committing its model-lane queue item.

Marking an application submitted requires either the current real selection or an
explicit **no tracked resume** choice. The submission event atomically snapshots the
selected artifact, evaluation, comparison kind, immutable standard version, and
human-readable standard name. The Chromium extension shows that name and kind from a
content-free handoff hint; when no selection is present, its popup asks for explicit
confirmation before recording the opt-out. Synthetic research artifacts can never
enter this path.

Retry does not rewrite a failed experiment. A retry of a terminal failed run appends a
successor run with the same frozen job analysis, hand-written ranking, and base version,
plus four fresh pending items; the failed predecessor remains intact. Retrying a queued
or running run only requests another idempotent queue delivery for that same run.

`runpod_reconciliation_required` is different. It means a submission response was lost,
an accepted job stopped producing trustworthy status, or the local worker restarted
while a remote job might still exist. Never retry while that Runpod job is queued or
running. Inspect the exact endpoint and the displayed job ID; for an ambiguity without
an ID, correlate the endpoint job list by submission time. Retry through **I reconciled
Runpod · retry** only after confirming that the prior work is terminal or absent. That
explicit acknowledgement is stored with the retry command; it is not an automatic
recovery switch.

Run both lanes normally for the complete application system:

```bash
python3 -m job_search.worker --config ~/.config/job-search/config.json --lane core
python3 -m job_search.worker --config ~/.config/job-search/config.json --lane model
```
