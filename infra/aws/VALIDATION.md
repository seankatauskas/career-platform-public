# Local validation — September 19, 2026

These results cover the infrastructure implementation on `codex/aws-infrastructure`.
They are local checks, not a record of a deployed AWS service.

| Check | Result |
| --- | --- |
| Terraform 1.13.5 formatting and main/bootstrap validation | Passed |
| Terraform mocked single-host security plan | Passed |
| Bootstrap, workflow IAM contract, backend consistency | 5 tests passed |
| Deployment, backup, restore, secret versions, activation | 25 tests passed |
| Fresh seed and container path rebasing | 14 tests passed |
| Release packaging and hostile archive protection | 2 tests passed |
| Release installer, existing-host upgrade, installation lock | 7 tests passed |
| Private dashboard identity/proxy boundary | 6 tests passed |
| Existing dashboard, autofill, runtime, cloud tests | 9, 11, 14, 14 passed |
| Scraper baseline, both `uv run` and `python3` | 73 tests passed each |
| GitHub workflows, actionlint | Passed |
| Linux amd64 Docker build and dependency consistency | Passed |
| Read-only-root, networkless, non-root container initialization | Passed; mailbox schedules disabled without configuration |
| Whitespace/error checks | `git diff --check` passed |

Recovery tests use real temporary SQLite databases, including committed WAL data,
and inject stop, upload, secret retrieval, directory replacement, and startup
failures. Cloud calls and Compose operations are mocked. These tests establish
local behavior; they do not establish live AWS IAM permissions or recovery time.

Pending account-backed acceptance: provisioning/bootstrap, Tailscale and Telegram
enrollment, hosted model pilot and ranking model migration, fresh Outlook login,
compiler/PDF verification, alarm delivery, replacement-instance restore, and seven
days of operation. No real source database was modified or exported in this task.
No cloud resource was created and no public port was opened.

The $25 combined inference allowance is not an application-enforced dollar cap.
Provider limits, disabled automatic replenishment, manual reconciliation, and
explicit pauses are required until cross-provider accounting is implemented.
See [the runbook](../../docs/operations/aws-deployment.md) for the remaining steps and evidence.
