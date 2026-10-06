# Develop while using the live platform

Use the AWS dashboard and its paired browser extension for real applications.
Develop on a feature branch with a separate local preview. Local edits, test runs,
and prepared releases do not change the code installed on AWS.

| Activity | Where it runs | Effect on the live application |
| --- | --- | --- |
| Edit code and try the preview | Your Mac, fictional data | None |
| Run tests and review a change | Local or GitHub CI, fixture databases | None |
| Prepare a release | GitHub runner, immutable images and release package | None; the running release stays installed |
| Deploy a prepared release | AWS host, at your chosen time | Brief maintenance pause, then the same persistent application data returns |

## Daily development

Create a branch for the change, then start the preview:

```sh
git switch -c feature/my-change
uv run scripts/dev-dashboard.py
```

Open `http://127.0.0.1:8775`. The preview has sample jobs, applications, messages,
documents, and review items. Mail, Telegram delivery, and model responses use the
existing offline fixtures. It never loads the production runtime configuration.
Each launch creates a fresh session under `.cache/development/` in this checkout;
it does not reuse or reset your real databases. Session files remain available
after stopping for debugging and can be removed when no longer needed.

Refresh the page after editing HTML, CSS, or JavaScript. After editing Python,
stop the preview with Ctrl-C and run the command again. Use `--port 8778` if another
preview occupies the default port. Separate Git worktrees can run their own
previews on separate ports.

Keep the AWS dashboard bookmarked for actual applications. Use a separate browser
profile when developing or pairing a test copy of the extension, so its preview
connection does not replace the extension connection you use to apply for jobs.
The real application stays available while the preview is running or restarting.

Run the relevant checks, including both collector smoke suites required by
`AGENTS.md`. Push, merge, and publish only when that work is authorized. Local
preview does not require a push, AWS credentials, or a deployment.

## Prepare now, install when convenient

Follow [coordinated releases](operations/coordinated-releases.md) for shared status,
prerequisite setup, and the release-owner handoff.

1. The release owner collects completed changes on `main` and checks shared
   release status. Once per batch, propose and review `deploy/release-policy.json`
   against the live installed predecessor; commit/merge it before preparation.
   Feature sessions do not independently update this policy or dispatch releases.
2. After the batch reaches `main`, run **Prepare AWS release**
   (`aws-release.yml`). It runs the tests, production
   container/browser checks and release-transition checks, then publishes immutable
   images and a checksummed package. A matching candidate is reused without
   rebuilding. The dispatch SHA is frozen; later merges wait for another batch.
   You can keep using the live app throughout.
3. Save the **prepared release receipt** from the workflow summary or artifact.
   It identifies the exact release ID, manifest SHA-256 and expected predecessor.
   A prepared receipt means the update is available, not installed.
4. When you are ready for a brief pause, finish any dashboard edits or approvals.
   Run **Deploy prepared AWS release** (`aws-deploy.yml`) and enter that release ID
   and manifest SHA-256. This installs the already tested images without rebuilding.
5. Check the deployment receipt, then reload the live dashboard. If another release
   was installed in the meantime, the predecessor check rejects the stale candidate
   before stopping production. Prepare a newly tested release for the new baseline.

The current SQLite/Compose design uses one host. The actual installation still
needs a maintenance pause while writers stop, a consistent rollback snapshot is
taken, and the new release initializes. Workers drain before that pause, while the
dashboard remains available. Build and image downloads happen before the pause.
Predeployment rollback snapshots are checksummed directories published atomically,
so compression and an extra archive copy are no longer part of the pause. Regular
off-host backups remain compressed archives. Recovery verifies the entire snapshot
before changing data, and preserves writes made after a release resumes. This is
not a guarantee of uninterrupted access or a fixed outage length. The deployment
receipt records snapshot-phase timings and the measured pause.

For an application submitted on an employer's site during a dashboard outage,
the paired extension durably queues captured tracking observations and replays
them after connectivity returns. Version 1.3 also queues encrypted application
answer snapshots for the application's Answers tab. This has browser acceptance
coverage including multi-step prose and browser restart. It is not a substitute
for confirming submission on the employer
site, and it does not preserve unsaved dashboard form edits. Plan the installation
between dashboard actions.

Do not repeat an installation with an unknown SSM outcome. Inspect the existing
command and host operation first. Retrying a confirmed completed, healthy release
is idempotent. Use the [deployment and recovery runbook](operations/aws-deployment.md)
for failed updates, compatible rollback, and restoration. Preparing or installing
code never imports a developer's fixture database over production data.
