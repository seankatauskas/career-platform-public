# Portable inference providers

The deterministic application, ranking, salary validation, and preference-model
pipelines remain local processes. An optional inference profile moves only structured
generation and embedding calls to an explicitly selected model endpoint. The example
uses Runpod's asynchronous queue so a scale-to-zero cold start can outlive one HTTP
request. There is no automatic provider or model fallback.

## Configure a travel or cloud profile

Copy `examples/inference-profile.example.json` outside the repository, create the referenced raw
API-key file, replace every placeholder with a pinned endpoint/model/deployment value,
and protect both files:

```bash
umask 077
mkdir -p ~/.config/job-search
cp examples/inference-profile.example.json ~/.config/job-search/inference.json
set -o noclobber
test ! -e ~/.config/job-search/runpod-api-key  # stop if this fails
touch ~/.config/job-search/runpod-api-key
chmod 600 ~/.config/job-search/inference.json
chmod 600 ~/.config/job-search/runpod-api-key
export JOB_SEARCH_INFERENCE_CONFIG="$HOME/.config/job-search/inference.json"
```

Populate `runpod-api-key` from an interactive password-manager/editor workflow; do not
put the literal token in a shell command or shell history. The profile remains invalid
until that owner-only file contains exactly the intended credential.

The config and credential must be regular files owned by the current user with no
group/world permissions. Relative `credential_file` paths resolve beside the profile.
Credentials are loaded only into the Authorization header and are excluded from stored
provenance and error messages.

### Use an Intel Mac without Docker

The travel laptop does not require Apple silicon, CUDA, Docker, or a local language
model **after the remote endpoints exist**. Before departure, use the M4 Mac or a
reviewed CI/cloud builder to build the embedding derivative, deploy and cold-start
smoke both Runpod endpoints, and complete the preference-champion migration below.
Building that custom image for the first time is not supported from a Docker-free
vacation laptop.

If this is a different Mac, do not copy the application database or autofill vault
directly: their encrypted rows are bound to the source Mac's Keychain. While every
source writer is stopped, use the no-clobber `portable-state-export` procedure in
[`docs/operations/cloud-deployment.md`](../operations/cloud-deployment.md#export-existing-mac-private-state-before-moving-it),
transfer the exported database, vault, and **exact same** portable master key over an
encrypted channel, and set `portable_encryption_key_file` in the laptop's owner-only
runtime configuration to that key. Never generate a replacement key for migrated
ciphertext. The Outlook token cache is intentionally not portable; complete the
documented device-code authorization on the travel Mac after installing the key.

Then create a clean Python 3.12 environment and install the same CPU control-plane set
used by the cloud image:

```bash
mkdir -p "$HOME/.venvs"
python3.12 -m venv "$HOME/.venvs/job-search-travel"
. "$HOME/.venvs/job-search-travel/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements/cloud.txt
cp examples/resume-model.runpod.example.json ~/.config/job-search/resume-model.json
chmod 600 ~/.config/job-search/resume-model.json
python -m job_search --config /PRIVATE/PATH/runtime.json status
```

For scheduled operation, put both absolute paths in the owner-only runtime JSON; do
not rely on a shell export:

```json
{
  "inference_config": "/Users/YOU/.config/job-search/inference.json",
  "resume_model_config": "/Users/YOU/.config/job-search/resume-model.json",
  "portable_encryption_key_file": "/Users/YOU/.config/job-search/portable-master-key"
}
```

Include `portable_encryption_key_file` only when using the migrated portable state
described above; it must name that export's exact key.

Fill and validate both provider files first. Then, while
`.venvs/job-search-travel` is activated,
re-render and apply launchd so every plist records that environment's Intel-compatible
Python executable:

```bash
unset JOB_SEARCH_INFERENCE_CONFIG
python -m job_search --config /PRIVATE/PATH/runtime.json status
python -m job_search --config /PRIVATE/PATH/runtime.json service install
python -m job_search --config /PRIVATE/PATH/runtime.json service install --apply
python -m job_search --config /PRIVATE/PATH/runtime.json service status
```

Inspect the dry-run plan before `--apply` and confirm its program path ends in
`.venvs/job-search-travel/bin/python`. Generated launchd files intentionally contain no inherited
shell environment, so `JOB_SEARCH_INFERENCE_CONFIG` is suitable only for manual
commands. Keeping the absolute profile path in runtime JSON prevents a scheduled worker
from silently falling back to MLX or Sentence Transformers after the terminal closes.

Keep the dashboard, SQLite state, deterministic validation, and schedulers on the
laptop; only model requests cross the selected provider boundary. The slim dependency
set intentionally omits MLX, PyTorch, and Sentence Transformers. Resume PDF generation
still needs the separately pinned Tectonic executable/bundle described in the resume
runbook, but the language-model work can use Runpod. This is also the recovery path if
Docker Desktop is unavailable while traveling.

The example's explicit `runpod_queued` kind accepts an endpoint ID, never a URL. The
client constructs only `https://api.runpod.ai/v2/<endpoint-id>/run` and matching status
URLs. It submits each job once, then polls the accepted job with bounded response sizes,
request timeouts, poll interval, overall deadline, and poll count. Redirects are
rejected. A timeout, 429, or 5xx during the submission POST has an ambiguous outcome:
the provider marks it non-retryable and requires the operator to inspect the endpoint's
job list before manually retrying. It never blindly submits a duplicate. Status GETs
are idempotent and safe to repeat under the same bounds. Within platform worker tasks,
an accepted ID is persisted before polling, so an expired polling deadline defers the
task and a later attempt polls that same ID. A malformed identity/status or permanent
status lookup failure requires reconciliation. Standalone calls without the platform
worker scope retain the explicit manual-reconciliation behavior. Terminal `FAILED` and `TIMED_OUT`
jobs are retryable by a caller's bounded retry policy; `CANCELLED` is not. Model CLI
exit codes preserve that distinction so the durable outer work queue cannot turn an
ambiguous result into a second submission.

## Platform usage limits and restart recovery

Runtime configuration accepts optional `inference_usage_limits`:

```json
{
  "inference_usage_limits": {
    "daily_requests": 100,
    "daily_tokens": 1000000,
    "max_inflight": 2
  }
}
```

These are illustrative limits, not recommended provider spending settings. Omitted
limits or `null` keep existing configurations compatible and impose no ceiling for
that dimension. Every enabled worker must use the same runtime configuration and
application database. The governor covers calls made by platform-managed work through
the shared generation/embedding providers and the resume Runpod adapter, including
model subprocesses. Standalone model scripts outside that worker scope, other apps,
and the Hermes agent's own upstream model calls are outside its coverage.

SQLite reserves one request, a conservative token allowance, and an in-flight slot
before any POST. All providers share those counters; concurrent workers cannot reserve
the final slot twice. Daily request/token limits use UTC dates. Unsubmitted reservations
are rechecked and rebooked before a POST after midnight. Accepted or uncertain work
keeps its original day and continues consuming an in-flight slot across midnight and
process restarts. A definitive terminal outcome releases the slot. Deferred work stays
queued without using up its retry attempts and becomes eligible at the next UTC day,
or after five minutes for an in-flight limit/polling wait.

Token reservations use serialized request bytes plus the requested output maximum and
protocol allowance; reported token usage can increase the charge but never refund a
reservation. They are conservative admission estimates, not actual billed usage. No
provider prices, account balances, dollar ledger, or cross-provider $25 cap are inferred.
Provider-side limits and explicit operational pauses remain necessary for dollar spend.

The durable ledger stores hashes, capability, retrieval contract, counters, timestamps,
and safe provider IDs; it does not store prompts, credentials, endpoint URLs, or result
bodies. A crash before the submission marker can reuse its reservation. A crash after
that marker without a saved accepted ID requires an explicit review and cannot repost.
Saved Runpod IDs can be polled after restart or database restoration, including retrieval
of completed output while the provider retains it. Changed request/provider identity
cannot bypass an unresolved invocation from an earlier attempt. Permanent missing-ID or
invalid status responses become visible reconciliation entries, rather than endless
polling or fresh POSTs.

Synchronous OpenAI-compatible APIs have no portable result-retrieval contract. The
preference embedding cache checkpoints each validated batch before requesting the next,
so a later batch can wait for quota and resume from the missing texts. A successful
synchronous call followed by a crash before a domain checkpoint, or a multi-stage caller
without such a checkpoint, needs explicit reconciliation. The ledger cannot recreate
that result; it records `inference_result_not_checkpointed` and refuses an automatic
repost. Use queued Runpod calls for multi-stage work that must retrieve prior outputs.

Inspect the shared report and pending invocations without contacting any provider:

```sh
python -m job_search --config /path/to/runtime.json inference-usage
python -m job_search --config /path/to/runtime.json inference-recovery
```

After reviewing a provider outcome, the authenticated user can issue
`inference-reconcile INVOCATION_ID --expected-updated-at TIMESTAMP --resolution
absent|failed|completed --provider-job-id JOB_ID --idempotency-key COMMAND_ID` using
the same runtime config. Omit the provider ID for a confirmed absent submission.
An accepted ID cannot be changed or declared absent. `completed` requires a valid,
retrievable Runpod ID; it is rejected for synchronous requests. A reviewed `failed`
resolution authorizes future bounded work without pretending a result can be retrieved.
Never declare failure merely because a provider is slow or unreachable.

Reconciliation does not send requests or automatically requeue work. It rejects active
leases, fences an expired worker, writes an immutable user-only audit entry, and retains
the daily request/token reservation. Inspect `next_action`, then use the existing work
recovery or resume-run workflow. Duplicate commands are idempotent; stale state and
conflicting command IDs are rejected. `readiness` exposes usage/reconciliation separately
from process liveness. Back up the application SQLite database with the rest of platform
state; deleting the ledger removes its duplicate-submission protection.

For `resume_same_work`, work recovery permits the narrow case where every invocation has
a completed retrievable Runpod result and the user review is recorded. The resume worker
then applies its usual application/version checks and polls those saved IDs. For
`retry_resume_after_reconciliation`, use the existing resume retry with explicit
reconciliation acknowledgment; it can close an orphaned running sidecar only after all
of its dead work's inference outcomes are terminal and a failed/absent outcome has been
audited. It creates the usual new run. `retry_career_import` means resubmit the source
through the existing import flow with a fresh idempotency key after that review.

The two capabilities deliberately pin different, versioned worker protocols:

- `structured_generation.worker_protocol` must be `worker_vllm_proxy_v1`. Its endpoint
  uses worker-vLLM's generic input with exactly `route`, `method`, and `body`, targeting
  `/v1/chat/completions`, and returns one OpenAI-compatible response in job `output`.
- `embeddings.worker_protocol` must be `job_search_infinity_exact_v1`. Its separate,
  digest-pinned derivative of worker-infinity-embedding receives the native queue input
  with exactly `model` and `input`; it does **not** receive worker-vLLM's route wrapper.
  Its job output must be the OpenAI-shaped embedding result plus the exact model-commit
  and protocol attestations enforced by the client.

See the official [worker-vLLM](https://github.com/runpod-workers/worker-vllm) and
[worker-infinity-embedding](https://github.com/runpod-workers/worker-infinity-embedding)
projects for the underlying contracts. Build the repository's embedding derivative as
documented in `deploy/runpod/README.md`; the lifecycle wrapper rejects the unmodified
stock embedding worker. Do not point one capability at the other worker or at a model
it does not serve.

For queued `numind/NuExtract3`, the salary adapter sends the frozen local extraction
template and instructions through vLLM's `chat_template_kwargs`, with thinking
disabled, user-only posting text, temperature zero, and 512 output tokens. It does
not impose an additional JSON grammar; the existing salary normalizer and evidence
validator still decide whether a result can be used. Other models retain the generic
JSON-schema request. The provider rejects NuExtract calls without an explicit
template rather than silently running a different extraction task. Pin and evaluate
the CUDA weights independently of the local MLX checkpoint before activation.

Hosted salary chunking reserves room for the request envelope, schema or extraction
template, and output. Input estimates include JSON escaping so quoted or multiline
postings cannot exceed the request limit merely when serialized.

For a continuously available service, `kind: "openai_compatible"` remains supported.
Its `base_url` is the service's `/v1` base; HTTPS is mandatory except for an explicit
loopback development endpoint. Its requests and responses remain bounded and redirects
remain disabled. This synchronous kind cannot bridge a scale-to-zero cold start that
outlives its single request, which is why it is not used by the Runpod example. Because
there is no portable provider-side idempotency contract, no failed synchronous POST is
automatically retried; inspect the provider before manually retrying ambiguous work.

## Commands

After the preference preflight below is compatible, the non-secret environment
variable above is the one-switch travel/cloud mode. An explicit CLI path takes
precedence:

```bash
python3 -m job_search.salary.llm --db job-boards.db run --inference-config ~/.config/job-search/inference.json
python3 -m job_search.ranking.proxy prepare --db job-boards.db --inference-config ~/.config/job-search/inference.json
python3 -m job_search.ranking.proxy run --db job-boards.db --inference-config ~/.config/job-search/inference.json
python3 -m job_search.ranking.proxy distill --db job-boards.db --inference-config ~/.config/job-search/inference.json
python3 -m job_search.ranking.proxy audit --db job-boards.db --inference-config ~/.config/job-search/inference.json
python3 -m job_search.ranking.model embed --db job-boards.db --inference-config ~/.config/job-search/inference.json
python3 -m job_search.ranking.model refresh --db job-boards.db --inference-config ~/.config/job-search/inference.json
python3 -m job_search.ranking.model evaluate --db job-boards.db --inference-config ~/.config/job-search/inference.json
```

Without `--inference-config` or `JOB_SEARCH_INFERENCE_CONFIG`, salary extraction and the
preference teacher retain their local MLX defaults and BGE retains local
Sentence Transformers. When a profile is selected, preference embedding defaults to
`remote`. The `--encoder` switch applies to `embed`; `evaluate` and `refresh` recreate
the encoder recorded by the model artifact and enforce exact identity. To evaluate a
local champion, unset `JOB_SEARCH_INFERENCE_CONFIG` and omit `--inference-config` as in
the migration procedure below—passing `--encoder sentence-transformers` to those two
commands does not override a configured remote provider.

For `runpod_queued`, `model_revision` must be the configured model followed by an actual
40- or 64-character lowercase Hugging Face commit, and `deployment_revision` must be the
resolved `sha256:<64 lowercase hex>` worker image digest. Record the commit resolved by
Hugging Face and the digest reported by the deployed image; branch names, tags, and
placeholder revision strings fail closed. These values remain operator declarations
for generation. The queued embedding path additionally requires the repository worker
to return its exact loaded commit and protocol with every response; missing or different
evidence fails closed. The ordinary `openai_compatible` kind still treats its revision
strings as operator-declared provenance rather than attestation.
The deployment wrappers in `deploy/runpod/README.md` close that configuration gap for
the supported Runpod workers: they read back the private template's digest-pinned
image and exact model environment, the endpoint's bound template and timeout policy,
and its exact singleton host-cache model reference before reporting success. The vLLM
template independently applies that same commit to model and tokenizer resolution,
forces offline cache use, and disables vLLM request logging using both current and
legacy worker controls. The embedding worker resolves the exact cached snapshot path,
handles Runpod's canonical/lower-cased cache layouts, disables network fallback and
remote model code, and is accepted only after a live revision-attesting smoke test.

When pulling the embedding image from private ECR through a Runpod delegation, use
`REGISTRY/REPOSITORY:IMMUTABLE_TAG@sha256:DIGEST`. A live Serverless pilot rejected
the digest-only URI with `no basic auth credentials`, while the tag-plus-digest URI
authenticated successfully. The digest still fixes the image bytes. Register the
delegation only for the dedicated embedding repository and verify the cold pull;
do not grant access to the application or Hermes repositories.

The embedding provider derives its cache/model-artifact revision from the weights
revision plus provider, endpoint, deployment, protocol, and dimensions. Consequently,
changing a worker image, provider, endpoint, or vector contract cannot reuse vectors
created by the earlier deployment. A remote refresh/audit fails closed unless this full
derived identity exactly matches the recorded artifact revision.

### Migrate an existing preference champion before travel

Do this while the scheduled model worker is stopped and while the Mac can still load
the current local encoder. First run the redacted preflight through the same runtime
configuration that the worker will use:

```bash
python3 -m job_search --config /PRIVATE/PATH/runtime.json status
```

Inspect `dependencies.inference.preference_embeddings.status`. `ready` means the
champion already uses the configured remote identity, `no_champion` means there is no
champion to migrate, and `migration_required` means the model worker must remain
stopped. The report contains only short identity fingerprints; it never prints the
endpoint, credential, configured identity, or champion revision.

For `migration_required`, use a new candidate end to end. Do not relabel the existing
artifact or combine its local vectors with remote vectors:

```bash
export JOB_SEARCH_JOBS_DB="/PRIVATE/PATH/job-boards.db"
export JOB_SEARCH_PREFERENCE_DB="/PRIVATE/PATH/job-boards-preference.db"
export JOB_SEARCH_PREFERENCE_ARTIFACTS="/PRIVATE/PATH/preference-artifacts"
export JOB_SEARCH_REMOTE_INFERENCE="/PRIVATE/PATH/inference.json"

# Complete or refresh the current champion's protected evaluation locally first.
env -u JOB_SEARCH_INFERENCE_CONFIG python3 -m job_search.ranking.model evaluate \
  --db "$JOB_SEARCH_JOBS_DB" \
  --state-db "$JOB_SEARCH_PREFERENCE_DB" \
  --artifacts "$JOB_SEARCH_PREFERENCE_ARTIFACTS"

# Build a separate candidate using only the configured remote embedding identity.
python3 -m job_search.ranking.model embed --encoder remote \
  --db "$JOB_SEARCH_JOBS_DB" \
  --state-db "$JOB_SEARCH_PREFERENCE_DB" \
  --inference-config "$JOB_SEARCH_REMOTE_INFERENCE"
python3 -m job_search.ranking.model train \
  --db "$JOB_SEARCH_JOBS_DB" \
  --state-db "$JOB_SEARCH_PREFERENCE_DB" \
  --artifacts "$JOB_SEARCH_PREFERENCE_ARTIFACTS" \
  --inference-config "$JOB_SEARCH_REMOTE_INFERENCE"

export JOB_SEARCH_REMOTE_RUN_ID="COPY_THE_RUN_ID_FROM_TRAIN_OUTPUT"
python3 -m job_search.ranking.model score \
  --db "$JOB_SEARCH_JOBS_DB" \
  --state-db "$JOB_SEARCH_PREFERENCE_DB" \
  --artifacts "$JOB_SEARCH_PREFERENCE_ARTIFACTS" \
  --run-id "$JOB_SEARCH_REMOTE_RUN_ID" \
  --inference-config "$JOB_SEARCH_REMOTE_INFERENCE"
python3 -m job_search.ranking.model evaluate \
  --db "$JOB_SEARCH_JOBS_DB" \
  --state-db "$JOB_SEARCH_PREFERENCE_DB" \
  --artifacts "$JOB_SEARCH_PREFERENCE_ARTIFACTS" \
  --run-id "$JOB_SEARCH_REMOTE_RUN_ID" \
  --inference-config "$JOB_SEARCH_REMOTE_INFERENCE"
python3 -m job_search.ranking.model promote \
  --state-db "$JOB_SEARCH_PREFERENCE_DB" \
  --artifacts "$JOB_SEARCH_PREFERENCE_ARTIFACTS" \
  --run-id "$JOB_SEARCH_REMOTE_RUN_ID" \
  --projected-incremental-minutes REPLACE_WITH_MEASURED_MINUTES \
  --reason "validated pre-travel remote embedding migration"
```

Promotion remains explicit and subject to the existing cross-encoder quality,
protected-set, and runtime gates. If it fails because the remote candidate is merely
equivalent rather than better, do not silently bypass the gate. Keep the local model
worker available or use `--force` only as the already documented, audit-recorded
operational-recovery exception after personally reviewing the candidate and recording
the reason. Re-run the status command and start the remote model worker only when the
preflight says `ready`. If the required protected labels or exact local model are not
available, complete that setup before leaving; there is no automatic migration path.

The operator `status` command is intentionally side-effect free. With a valid remote
profile it reports `healthy_unprobed`, and the inference/resume dependency reports use
`configuration_ready`; neither label claims that a GPU worker has cold-started or that
the endpoint is reachable. Run the explicit endpoint smoke checks above before relying
on a travel or cloud cutover.

Structured generation exposes a similarly derived identity that binds provider,
protocol, endpoint, deployment image, model weights, and configured decoding contract.
The preference teacher stores this full identity in its run ID and guard, so judgments
from an endpoint or image replacement cannot be mixed into an earlier run even when the
declared model weights are unchanged. Ordinary `model_revision` remains the separately
recorded operator declaration. Remote preference estimates report marginal API cost as
unknown (`null`) because this profile contains no pricing assertion; local execution may
report zero marginal API cost.

Salary extraction sends a bounded job-posting chunk. Preference teaching sends the
redacted semantic job text plus the private preference profile fields already defined
by that workflow. Embedding sends title/department/team text and bounded description
chunks. Operational databases and deterministic scoring do not move to the inference
endpoint.

Configuring the shared inference profile does not authorize mail-content egress. The
version-1 runtime config must also set `remote_mail_inference_enabled` to the boolean
`true` before Outlook mail or attachment text can use `structured_generation`; the
default is `false`. An explicit `mail_classifier_config` has precedence even when the
remote opt-in is true and selects the existing local, sandboxed command.

With that opt-in active, classification sends only the sanitized 2,048-character
evidence excerpt. Temporal extraction sends at most the first 24,000 characters of an
eligible archived mail or attachment plus at most twenty candidate applications. Both
adapters request strict JSON, give the model no tools, treat all message content as
untrusted data, and pass output through the existing exact-span, candidate-ID,
timestamp, and policy validators before any proposal can affect application state.
Retryable provider failures remain in the durable mail queue for the worker's bounded
retry policy.
