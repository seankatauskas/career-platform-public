# Production Compose acceptance

This check starts the actual `compose.cloud.yaml` with generated owner-only
configuration, synthetic application state, and no configured external providers.
It needs Docker Compose v2, Python 3.11+, and an already-built Linux amd64 image.
It never loads an owner's configuration or enrolls a live account.

Run on a native Linux Docker host for release acceptance:

```sh
python3 scripts/prepare-acceptance-toolchain.py --output .cache/acceptance-toolchain
docker build --platform linux/amd64 --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" --tag career-platform:acceptance .
python3 scripts/compose-acceptance.py \
  --image career-platform:acceptance \
  --toolchain-dir .cache/acceptance-toolchain \
  --output .cache/compose-acceptance \
  --require-linux-host
```

Commit source changes before running acceptance. The image revision label and
runtime revision must both match the clean checkout used for the host-side helpers.
The check freezes the image ID at startup and creates a unique temporary Compose
project. It starts tools, dashboard, MCP, core, and model; checks their production
health probes; calls the dashboard and authenticated MCP endpoint; compiles and
extracts a real synthetic PDF through the networkless Unix-socket tool service;
then kills both workers and verifies an expired local-work lease completes once
after restart. It stops all writers, uses the production backup/restore functions,
rejects a corrupted archive checksum, and restores into a fresh paused directory.
The restored dashboard must read the same application event and PDF bytes while
both worker lanes stay stopped.

Exit zero requires every check and cleanup to pass. `results.json` contains the
source SHA, frozen image ID, toolchain hashes, PDF hash, recovery result, and
explicit limitations. `acceptance.pdf` contains only synthetic text. On failure,
`compose.log` contains the fixture containers' recent logs. Temporary containers,
volumes, state, and generated credentials are removed; only these receipts remain.

## Public, reproducible toolchain

`prepare-acceptance-toolchain.py` fetches the official Tectonic 0.15.0 Linux musl
release and verifies pinned hashes for both its archive and executable. It builds
a deterministic ZIP from 312 public TeX/font files in the official Tectonic
`tlextras-2022.0r0` bundle. `scripts/acceptance-tex-files.tsv` pins the byte ranges,
sizes, and SHA256 of each file; bounded range requests fetch about 15 MB instead of
the entire archive. The subset receives its own cache identity. Every cached file
is verified again, and any missing or altered file is fetched and checked before
it enters the new bundle. The toolchain needs no network during compilation.

The first preparation downloads about 30 MB including the engine. Later runs
reuse verified files from the output directory. `--seed-bundle /path/to/bundle`
can accelerate preparation from an existing ZIP; it cannot bypass hash checks.
`receipt.json` records the toolchain provenance. The subset supports the acceptance
document; it is not a claim that every possible resume template is supported.

## CI integration

Run the three commands above on `ubuntu-latest` before publishing a release,
and on pull requests that change runtime, deployment, migrations, or document
tools. Use a job timeout of 20 minutes, install Docker Compose v2 if absent, and
cache `.cache/acceptance-toolchain` using the hashes of both preparation files.
Upload `.cache/compose-acceptance` and the toolchain `receipt.json` even on failure.
Do not upload the temporary fixture directory or configure provider credentials
for this job. No AWS or GitHub registry login is needed for the test itself.

## Local Docker Desktop check

Omit `--require-linux-host` to run a development check on Docker Desktop. A
generated override puts the shared tool socket and SQLite state in VM-native named
volumes because macOS file sharing cannot preserve Unix-socket permissions or
reliably coordinate SQLite WAL locking between containers. Original and restored
state use separate volumes; only services that already receive state in production
receive these mounts. The restore helper also substitutes fixture ownership
operations for Linux `chown`.

Backup reads the stopped original state volume directly. Restore keeps its normal
atomic directory replacement in a quiescent staging directory, then copies the
successfully restored files, preserving their contents and modes, into an empty
restored VM volume owned by the fixture user. All writers remain stopped during
this transfer. Containers start against the restored volume only after the copy
finishes. The synthetic PDF artifact is copied from its container. These fixture
transport and ownership exceptions are recorded in the receipt. The native Linux
gate retains production state bindings and the unmodified restore/ownership path.

Neither run verifies Tailscale enrollment, Outlook consent, Telegram delivery,
live ATS ingestion, paid inference, IAM, monitoring delivery, or AWS disk recovery.
Secret-manager recovery uses generated fixtures with an empty external secret
inventory. Those checks still require their corresponding external configuration.

## Release transition and interruption checks

The AWS checks and release workflow also run:

```sh
python3 scripts/release-transition-acceptance.py --candidate-image career-platform:ci
```

The command builds the committed policy's predecessor, creates fictional state in
an isolated Docker volume, upgrades it, checks retained rows across all configured
databases, verifies configuration/artifact hashes and encrypted mail, and writes new
application/resume records. When both versions share the hardened compatibility
contract, the predecessor must then read the upgraded state and retain those writes.
Legacy or different-contract predecessors are explicitly ineligible for image rollback.
Both images run without networking. Volumes and temporary images are removed afterward.
A local `--local` mode uses Python subprocesses for fast feedback; its receipt cannot
be packaged as production transition evidence. Dirty working-tree receipts are also
rejected. Commit the exact source before final image acceptance.

`tests/test_release_recovery.py` kills real child processes at journal, release-pointer,
secret-publication and directory-replacement boundaries and exercises recovery twice.
It also tests worker draining, incompatible rollback, capacity refusal and bounded
backup retries. These use local host-operation adapters and prove process-death
handling, not AWS reboot, physical disk durability or secret-manager availability.

The native Linux Compose receipt, Docker transition receipt, and system/browser
receipt are separate required CI evidence. Hermes' upstream s6 startup gate and
live credentials require the configured upstream image in the AWS pilot; the
credential-free Compose fixture does not run the paid conversational integration.
