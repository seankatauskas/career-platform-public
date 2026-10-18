# Release preparation performance

Release preparation keeps every offline/browser suite, reviewer probe, native
Compose backup/restore check, predecessor upgrade/rollback check, and Hermes
contract check. The production installation remains a separate explicit workflow.

## Observed baseline

The successful release 82 preparation run
[39079640170](https://github.com/seankatauskas/career-platform/actions/runs/39079640170)
waited 134 seconds for the Linux runner and executed for 307 seconds. Its major
steps were:

| Step | Seconds |
| --- | ---: |
| Isolated production tool installation | 10 |
| Offline/browser validation and Python dependencies | 154 |
| Native image, Compose, upgrade and rollback validation | 91 |
| Hermes validation and image publication | 30 |

The application and reviewer image layers were already cached by the preceding
main-branch checks. In that preceding
[run 39079626168](https://github.com/seankatauskas/career-platform/actions/runs/39079626168),
the dependency installation, package check and standard-library bytecode layer
rebuilt for 27.6 seconds even though the dependencies were unchanged. The
application push then took 21.47 seconds, including the rebuilt dependency layer
(319 MB uncompressed). Stable dependency layers avoid that repeated transfer in
subsequent releases; the first release adopting this recipe still builds them.

## Changes

`scripts/validate-release.py` runs three independent lanes on the six-CPU, 16 GiB
Linux runner. One runs the complete offline/browser suite with six workers.
Another builds and checks the application/reviewer images, then runs native
Compose and transition
acceptance sequentially. The third builds Hermes from its exact pinned base and
runs its unchanged native contract checks. Each lane owns separate logs and
fixture outputs. The coordinator waits for all three, allowing native teardown
to finish even if another suite fails, and returns failure if any lane failed.
The workflow cannot publish images or release receipts until this join succeeds. Per-lane timing and
logs are retained in the existing validation artifact.

The application Dockerfile declares its source revision after all filesystem
layers. The final image still carries the exact commit label and runtime revision
environment variable. Changing a commit no longer invalidates user creation,
dependency installation, or bytecode compilation solely through build metadata.
Source and dependency changes still invalidate the corresponding filesystem
layers. This follows Docker's documented
[build-argument cache behavior](https://docs.docker.com/reference/dockerfile/#impact-on-build-caching).

## Fixture measurements

On the existing six-CPU, 16 GiB Linux VM, with warm images and toolchain caches,
all 231 offline/browser suites and every native Compose/upgrade/rollback gate
passed in both configurations:

| Concurrent gate configuration | Offline lane | Native lane | Joined elapsed |
| --- | ---: | ---: | ---: |
| Three offline suite workers | 155.277 s | 96.306 s | 155.277 s |
| Six offline suite workers (selected) | 99.852 s | 93.058 s | 99.852 s |

The six-worker run retained at least 12.8 GiB of available guest memory. The
native lane now occupies 93 of the 100 seconds; more offline workers could save
at most about seven seconds at this native cost, so the release keeps concurrency
bounded to the runner's six CPUs. Historical release 82 spent 245 seconds in its
serial gate steps. That is context, not a controlled before/after comparison:
its commit differed, it included dependency setup, and the shared physical Mac
ran other local checks during part of this fixture measurement. The next actual
release preparation is the operational validation.

A separate revision-only image build completed in 0.237 seconds and retained all
10 identical filesystem layers. Both the image label and runtime environment
changed to the requested fixture revision. Raw aggregate evidence is in
[release-preparation-benchmark.json](release-preparation-benchmark.json). These
measurements use fictional data and invoke no recommendation models.

## Release 83 and Hermes registry latency

[Release 83 preparation](https://github.com/seankatauskas/career-platform/actions/runs/39084883419)
completed in 232 seconds, compared with 307 seconds for release 82. Parallel offline/native validation took 110.153 seconds (offline
110.152, native 65.608), but image publication increased from approximately 30 to
81 seconds. The logs attribute almost the entire increase to Hermes Docker Hub
base resolution: 39.8 seconds loading metadata and 8.5 seconds resolving the
same pinned digest, compared with approximately 0.4 seconds previously. Hermes
filesystem steps and all application build layers were cached. The application
push took 22.60 seconds versus 21.47 seconds previously; it did not cause the
large increase. The first publication after the Docker recipe change does not
establish future upload savings.

The third validation lane overlaps this variable registry latency with the other
checks. It uses Docker's `--iidfile` output directly, without a mutable candidate
tag. The unchanged Hermes native checks receive that exact image ID. Their
receipt then records the source commit and pinned base. After every lane and the
existing source guards pass, publication rechecks these bindings and tags that
validated ID. No image push happens inside a validation lane. This avoids adding
cache trust rules or changing the reviewed base image.

A deterministic fixture uses scaled sleeps for the observed release-83 lane
lengths, including its 48.3-second resolver delay. At scale 0.01, three trials
measured median **1.689 seconds** with serial Hermes versus **1.136 seconds** with
three lanes. This demonstrates the scheduling behavior, not real network or
production performance. Reproduce with
`python -m tests.benchmark_release_validation_scheduling`.

A real Linux comparison then ran both arrangements on the same clean commit
`a2ae61233bb8810b2e2427e46a34fc2024a8c79c`, with no concurrent GitHub jobs.
Both passed all 234 offline/browser suites and the complete native and Hermes
gates. Application images and the verified toolchain were warm. The serial case
ran first; Hermes rebuilt its small filesystem steps in that case and reused
them in the second. Upstream metadata resolution was fast in both (0.4 and 0.0
seconds), so this pair measures the ordinary path rather than a registry stall.

| Arrangement | Offline | Native | Hermes | Total | Minimum available guest memory |
| --- | ---: | ---: | ---: | ---: | ---: |
| Two lanes, then Hermes | 105.392 s | 59.119 s | 4.660 s | 110.053 s | 12.62 GiB |
| Three lanes | 105.970 s | 58.474 s | 5.462 s | 105.970 s | 12.84 GiB |

The observed reduction was 4.083 seconds (3.7%). Offline and native durations
were similar, with ample memory remaining. This single sequential pair on the
shared physical host does not establish a precise production speedup; it shows
no material contention regression in this workload. Raw results and the scaled
fixture trials are in [hermes-validation-benchmark.json](hermes-validation-benchmark.json).
No images were published and no production data or recommendation models were
used by these fixtures.

## Boundaries and remaining opportunities

These changes reduce preparation time and runner occupancy; they do not shorten
the production service pause directly. Native transition checks still verify the
image's exact commit identity. Existing candidate reuse remains bound to the
source, policy, host and build settings and verifies the actual manifest.

Reusing successful push-CI test results during release preparation could remove
more repeated work, but the current receipts are insufficient for that trust
decision. The public offline/browser suite runs on macOS, while release checks
run on Linux; native results would also need authenticated workflow version,
runner platform, exact image and settings bindings. This change does not treat a
green run or a mutable runner cache as release evidence. Image caches reuse build
layers while all required gates execute again.

Other small costs remain: isolated tools protect production configuration,
the Hermes native contract checks themselves took about 3.25 seconds, while
registry publication records the actual immutable digests. The runner queue includes required checks for the
newly merged commit. Additional runners or a separately designed evidence
attestation contract could reduce that queue; neither is needed for the bounded
parallelism implemented here.
