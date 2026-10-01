# Runpod inference deployment

This directory is the infrastructure boundary for optional remote inference. The
job-search databases, Outlook state, TeX/PDF tooling, application decisions, and
deterministic validators stay in the control plane. Runpod receives only bounded
model requests and returns untrusted model output for local validation.

The supported low-operations deployment uses Runpod's `worker-vllm` for structured
generation and a tiny repository-owned derivative of `worker-infinity-embedding` for
BGE embeddings. The derivative exists because the stock worker does not attest its
loaded revision and can miss Runpod's lower-cased model-cache path. Each endpoint
scales to zero with one maximum worker. A long-lived GPU pod is not required.

## Prerequisites

1. Install `runpodctl` 2.10.0 or newer. Older releases can silently omit an explicit
   zero when updating `workers-min`; the wrappers reject them and read the policy back
   through Runpod's REST API after every applied create/wake.
2. Create a restricted Runpod API key and expose it only in the current shell as
   `RUNPOD_API_KEY`. Never place it in Git, an image, a Compose file, or the runtime
   database.
3. Select a vLLM-compatible Hugging Face checkpoint. An MLX checkpoint made for
   Apple Silicon is not CUDA-compatible. Pin the model URL to its immutable commit.
4. Size the GPU from model weights, quantization, context, and KV-cache headroom,
   then verify with representative requests. Do not infer NVIDIA VRAM from M4 unified
   memory alone.

Copy both environment examples to private owner-only files, fill them in, and source
the one for the endpoint you are operating. The examples intentionally contain no API
key.

```bash
chmod 600 /private/path/job-search-runpod.env
set -a
. /private/path/job-search-runpod.env
set +a
export RUNPOD_API_KEY='set-this-in-the-shell-or-secret-manager'
```

Resolve both upstream workers' reviewed release tags to immutable registry digests;
do not substitute `latest`. For example, with a current Docker Buildx installation:

```bash
docker buildx imagetools inspect \
  runpod/worker-v1-vllm:REVIEWED_RELEASE_TAG
docker buildx imagetools inspect \
  runpod/worker-infinity-embedding:REVIEWED_RELEASE_TAG
```

Use the vLLM digest directly as `runpod/worker-v1-vllm@sha256:...`. Build the embedding
derivative from the reviewed stock digest, push it to a registry Runpod can pull, then
resolve and record the derivative's digest:

```bash
docker buildx build --platform linux/amd64 \
  --build-arg EMBEDDING_BASE_IMAGE=runpod/worker-infinity-embedding@sha256:REVIEWED_BASE_DIGEST \
  -f Dockerfile.runpod-embedding \
  -t YOUR_REGISTRY/job-search-embedding:REVIEWED_VERSION --push .
docker buildx imagetools inspect \
  YOUR_REGISTRY/job-search-embedding:REVIEWED_VERSION
```

Set `RUNPOD_EMBEDDING_WORKER_IMAGE` to
`YOUR_REGISTRY/job-search-embedding@sha256:...`; the lifecycle wrapper rejects the
stock image. Resolve each Hugging Face model to a commit SHA and use that SHA in
`*_MODEL_REFERENCE` and the application inference profile. Each worker runs through a
private template whose exact ID, name, image digest, container disk, and complete
environment keyset are read back before endpoint creation.
The endpoint read-back also verifies that template attachment, the configured model
name, the exact singleton host-cache model reference, and the timeout policy. Runpod's
REST endpoint response does not expose model references, so the guard reads that field
through Runpod's GraphQL API and fails closed on an absent, additional, or different
reference. The vLLM template also derives `MODEL_REVISION` and
`TOKENIZER_REVISION` from that validated commit, forces Hugging Face/Transformers
offline cache resolution, and sets both the current and legacy vLLM request-logging
controls to their disabled values. The redundant logging controls keep privacy explicit
across reviewed worker releases; exact template read-back prevents either from silently
disappearing. The embedding derivative resolves only the exact canonical or lower-cased
`/runpod-volume/huggingface-cache/hub/.../snapshots/<commit>` directory, runs Hugging
Face and Transformers offline, disables remote model code and Infinity telemetry,
preserves the configured served-model name, and returns the loaded commit and worker
protocol in every embedding response.

## Safe lifecycle

Every external mutation is a dry run unless `--apply` is present. Both workers use the
same private-template-first sequence:

```bash
scripts/runpod-vllm-endpoint check
scripts/runpod-vllm-endpoint template
scripts/runpod-vllm-endpoint template --apply
export RUNPOD_TEMPLATE_ID='the-verified-template-id'
scripts/runpod-vllm-endpoint deploy
scripts/runpod-vllm-endpoint deploy --apply

export RUNPOD_ENDPOINT_ID='the-created-id'
scripts/runpod-vllm-endpoint status
scripts/runpod-vllm-endpoint smoke
scripts/runpod-vllm-endpoint smoke --apply
```

Create the embedding worker's private, digest-pinned Serverless template first, copy
the verified template ID into `RUNPOD_EMBEDDING_TEMPLATE_ID`, then create and test its
endpoint:

```bash
scripts/runpod-embedding-endpoint check
scripts/runpod-embedding-endpoint template
scripts/runpod-embedding-endpoint template --apply
export RUNPOD_EMBEDDING_TEMPLATE_ID='the-verified-template-id'
scripts/runpod-embedding-endpoint deploy
scripts/runpod-embedding-endpoint deploy --apply
export RUNPOD_EMBEDDING_ENDPOINT_ID='the-verified-endpoint-id'
scripts/runpod-embedding-endpoint status
scripts/runpod-embedding-endpoint smoke --apply
```

After either create call is attempted, the wrapper retries only the exact readback for
up to roughly 30 seconds to accommodate API propagation—even if `runpodctl` returned a
nonzero status after a possible remote success. It never repeats the create. If
verification still fails, inspect Runpod for the named template or endpoint and
reconcile its ID before rerunning anything; a second create may leave an orphan and
incur cost.

Each template read-back verifies its exact ID, private/serverless flags, image digest,
container disk, and complete environment keyset. Each endpoint read-back requests the
embedded template and verifies its ID and configuration again, plus the endpoint name,
GPU shape, `workersMin=0`, `workersMax=1`, idle timeout, and execution timeout. The
guard also checks the endpoint's exact singleton model reference through GraphQL. The
vLLM smoke does not trust a `COMPLETED` job status by itself: it rejects embedded
worker/proxy errors, malformed or truncated responses, a different response model, and
empty assistant content. The
embedding smoke test submits the worker's native `{model,input}` queue shape and rejects
a response whose model, exact commit attestation, worker protocol, indexes, finite
numeric values, or configured vector dimension differ. Do not enable the model lane
until this live cold-start smoke succeeds for the exact `BAAI/...` model reference;
static template read-back alone cannot prove that Runpod populated the cache correctly.
This is deliberately separate from the chat smoke test.

The first request after scale-to-zero can take minutes. Production clients therefore
submit through `/run` and poll `/status/<job-id>` instead of assuming a synchronous
request will survive a cold start.

Runpod can set a long-unused endpoint's maximum workers to zero. Before the first
session after dormancy, restore the bounded scale-to-zero policy explicitly:

```bash
scripts/runpod-vllm-endpoint wake --apply
scripts/runpod-embedding-endpoint wake --apply
```

`wake` keeps minimum workers at zero, so it does not itself keep a GPU billing while
idle. It restores a maximum of one so the next request is allowed to cold-start. The
current CLI cannot change an endpoint's execution timeout during update, so `wake`
validates that configured value before the dry run and verifies the unchanged remote
value after an applied update.

To remove the endpoint, first inspect `RUNPOD_ENDPOINT_ID`; deletion is explicit:

```bash
scripts/runpod-vllm-endpoint delete
scripts/runpod-vllm-endpoint delete --apply
scripts/runpod-vllm-endpoint delete-template --apply
scripts/runpod-embedding-endpoint delete --apply
scripts/runpod-embedding-endpoint delete-template --apply
```

## Model acceptance

Do not promote a remote model because a smoke prompt succeeded. Run the repository's
salary gold-set, preference, and resume-contract fixtures against the exact endpoint,
model revision, worker release, prompt version, context, and decoding settings. Store
those immutable identities in the owner-only inference configurations. A model change
is a new evaluation candidate; it must never silently inherit a prior embedding or
preference-model revision.

The vLLM worker is configured with `ENABLE_LOG_REQUESTS=false` and the legacy
`DISABLE_LOG_REQUESTS=true`; preserve those exact read-back requirements when
refreshing its pinned image. Runpod still receives queued
request content as the selected infrastructure provider, so mail inference remains a
separate explicit opt-in. Keep `workers-min=0`, `workers-max=1`, bounded
execution/retention deadlines, and one client-side request at a time. Use the separate
embedding endpoint because one generative model cannot stand in for the exact BGE
revision recorded by the preference model. Copy both verified endpoint IDs, both model
commits, and the reviewed worker-image digests into the private inference profile
before switching the application.
