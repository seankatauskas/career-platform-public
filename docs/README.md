# Documentation

Start with the [project README](../README.md), [demo](demo.md), and [system design](system.md).

| Topic | Guides |
| --- | --- |
| Development | [Develop alongside production](development.md), [repository map](../README.md#repository-map), [command reference](commands.md), [testing](../tests/README.md), [architecture and contracts](architecture.md) |
| Agent-selected shortlists | [OLD METHOD async reviews](operations/old-method-reviews.md), [Agent review workflow](agent-job-reviews.md), [isolated AWS reviewers](operations/isolated-reviews.md), [Publish Codex picks](curated-shortlists.md) |
| Collection and ranking | [Collector guide](collector-guide.md), [model training](models/local-model-training.md), [ATS approximation](models/ats-proxy-model.md), [inference providers](models/inference-providers.md), [salary model](models/salary-v3.md) |
| Running locally | [Application runbook](operations/job-search-runbook.md), [runtime and scheduling](operations/job-search-runtime.md), [isolated system demo](offline-system-demo.md) |
| Deployment | [Coordinated releases](operations/coordinated-releases.md), [AWS](operations/aws-deployment.md), [Docker Compose](operations/cloud-deployment.md), [Compose acceptance](compose-acceptance.md), [workflow setup](workflow-setup.md) |
| Operations | [Readiness and recovery](operations/operations-readiness.md), [runtime efficiency and affordable ranking](operations/runtime-efficiency.md), [costs and credits](operations/cost-dashboard.md), [secure mail](operations/job-search-secure-mail.md) |
| Chief of staff | [Briefings, Telegram reply approval, and personal calendar](hermes-chief-of-staff.md), [shared email-understanding design](mail-understanding-design.md) |
| Career information and resumes | [Career database](resumes/career-resume-runbook.md), [resume lab](resumes/resume-lab-runbook.md) |
| Configuration | [Examples](../examples/README.md), [dependency groups](../requirements/README.md) |

[Roadmap](roadmap.md) tracks planned work. `archive/` preserves historical estimates and completion notes; those are records of earlier decisions, not setup instructions.

[Hermes and Outlook lifecycle investigation](hermes-outlook-lifecycle-investigation.md)
records the baseline gaps. [Lifecycle implementation](hermes-lifecycle-implementation.md)
describes the completed service, dashboard and Hermes workflows, validation, and coverage limits.

The generated [OpenWiki](../openwiki/quickstart.md) describes the original collector. Some generated file paths predate the repository reorganization; use this index and the testing guide for current commands. Its scheduled refresh owns updates to those pages.

## Interface

- [Resolve email reviews](mail-review.md): manual corrections, application matching,
  missing records, explicit next steps, and agent-assisted batch resolution.

- [Design language](design-language.md): palette, typography, component hierarchy, and media capture conventions.

- [Local-first setup and full-state migration](operations/local-first-setup.md) — enroll paused, use an unchanged resume, then connect accounts.
