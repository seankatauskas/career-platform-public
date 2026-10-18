# Deployment startup performance

Release 82 spent 53.282 seconds resuming services after validation. The
interaction broker started at `2026-10-18T00:02:02.965Z` and Hermes at
`00:02:33.907Z`. Hermes depends on broker health, and the broker's health check
had a 30-second interval with no startup grace interval. This establishes a
30.94-second startup gap consistent with waiting for its first scheduled probe;
the original first-probe logs had already expired when inspected.

## Changes

- Probe every two seconds during startup. Existing 30/60-second steady intervals,
  health commands, five-second timeouts, and three-failure thresholds remain.
  The broker now has a 20-second startup grace period; other services retain their
  existing grace periods. A successful real health check is still required.
- Force-create the target tools container during validation and keep that same
  container for final resumption. Its networkless process has no application
  state mount. Final health verification still includes tools. Paused installs
  stop it, and recovery stops the project before restoring state.
- Export and verify the digest-pinned reviewer executable before draining, into
  its existing root-owned immutable cache. This step does not update runtime
  configuration, service units, activation, or application data. Cutover verifies
  the cached executable's hash, permissions, ownership and version again before
  preparing the installed release.

These startup settings require Docker Engine 25 or newer and Compose 2.20.2 or
newer. The observed production versions were Engine 29.1.3 and Compose 2.40.3.
Docker documents [startup probe scheduling](https://docs.docker.com/reference/dockerfile/#healthcheck)
and the [Compose healthcheck fields](https://docs.docker.com/reference/compose-file/services/#healthcheck).

## Measurements and verification

Two paired trials of an always-ready, networkless fictional process on Docker
Desktop Engine 27.4.0 measured **30.225 / 30.165 seconds** to healthy with the
broker's old schedule, versus **2.119 / 2.197 seconds** with the new schedule.
Both modes ran the same successful probe; no readiness check was omitted.
This isolates probe scheduling. It does not measure application initialization,
production dependency ordering, native Linux behavior, or an AWS outage.
[Raw fixture results](evidence/deployment-health-2026-10-17.json) preserve the
image digest and first-probe timestamps.

```sh
uv run --python 3.12 python -m tests.benchmark_deployment_health --runs 2 \
  --output .cache/deployment-health.json
```

The benchmark requires an already present `python:3.12-slim-bookworm` image
(override with `--image`). It freezes the local image digest, never pulls, and
creates/removes only uniquely named fixture containers. Application acceptance
remains the native Compose and upgrade/rollback CI gates on the exact candidate.
The next live deployment must compare its startup phase and actual readiness
timestamps; the fixture result is not a predicted production saving.

Offline operations tests cover retaining one exact target tools instance through
both health checks, paused installs, stopping tools before rollback, reviewer
export failure before drain, and numeric-only timing receipts. Existing process
death/recovery and prepared-snapshot suites retain the original safety boundaries.

## Remaining costs

The new `cutover_steps_seconds` map in the deployment result and completed
operation journal measures secret materialization, ownership repair, reviewer
configuration, preflight, initialization, tools creation/health, optional tools
stop and model readiness, and service creation/health separately. These nested
measurements explain existing phase totals; do not add them to those totals.
They contain fixed operation names and durations only.

The previous 27.506-second runtime preparation lacked this breakdown, so further
changes to secret resolution should wait for measurements. Its 76.570-second
initialization included a one-time discovery index migration and is not a routine
baseline. Full stopped-source equality hashing remains mandatory; this change
does not reduce snapshot verification or change the rollback boundary.
