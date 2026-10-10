# Implementation evidence

For the later production integration phase, see [production readiness](08-production-readiness.md).
The evidence below records the earlier isolated implementation and walkthrough.

The replacement is implemented in the isolated `redesign/application-lifecycle`
worktree. Final combined verification is recorded below. Production routing remains
unchanged; candidate and legacy databases reject each other's writers.

## Baseline

- Source: `7a6fd17`; design baseline: `c192fe6`.
- The original dirty checkout was preserved. Only the approved design documents
  were copied into the isolated worktree; uncommitted runtime changes were not.
- Both required collector commands passed 80 tests each before implementation.
- Full offline baseline: 120/121 suites passed. Two existing reminder fixtures
  depended on the wall clock; their creation time is now explicitly fixed before
  their October deadline. No production reminder rule changed.

## Delivered boundaries

- **Applications:** identity, pursuits, immutable submission evidence, notes, tasks,
  interviews, assessments, offers, progress, reminders, exact schedules, closure,
  reopening, corrections, and reviewed combination.
- **Correspondence:** preserved message revisions, private archive references,
  explicit associations, and correction history.
- **External Actions:** exact proposals/approvals, provider contracts, fenced
  attempts, retries, uncertainty/reconciliation, and durable result handoff.
- **Shared mechanics:** trusted principals, exact delegation, transactional receipt
  replay, serialization rollback, immutable history, durable work, and owner write
  guards. Business rules remain in owners.

Understanding belongs inside Applications. All inferred lifecycle changes become
review proposals, including deterministic browser confirmations. Direct internal
commands follow authority rules; external effects require a separate exact approval.
A delivered reminder or draft never completes a task. Only a verified send can apply
its explicitly approved task consequence.

## Acceptance coverage

| Design acceptance | Focused evidence |
| --- | --- |
| ID-1, ID-2, EV-1, EV-2 | Applications, workflows, extension, compatibility, migration suites |
| UN-1, UN-2, UN-3, AU-1 | Evidence and workflow suites: exact quotes, incomplete coverage, replay, rejection lineage |
| AU-2, RV-1 | Commands, workflows, transport, agent-tools suites: trusted actors, scoped grants, atomic review, replacements |
| LC-1, LC-2, LC-3 | Applications and scheduling suites: dependent versions, rollback, reminder receipts, closure/reopening |
| FX-1, FX-2, FX-3 | External-actions, preparation and workflow suites: exact context, fencing, uncertainty, result-only recovery |
| CR-1, CR-2 | Applications, workflows and views: explicit causal correction, alias identity, unresolved effects |
| VW-1, BD-1 | Views, boundaries, commands and browser acceptance: shared reads, broken-rule fixtures, SQL ownership |
| MG-1, MG-2 | Migration, isolation, compatibility and authenticated transport suites: retained snapshots, inert old work, one writer |

Python suites are named `tests/test_redesign_*.py`. The real HTTP browser check is
`tests/browser/test_application_candidate.mjs`. The existing MCP host is exercised
with the candidate tool registry; extension tests use the real pairing validator
against a separate fictional authentication database. Provider checks use fakes.

## Verification receipts

- Verified runtime revision: `f903769c9996e32d71fe259a86d43a7eeef7a516`; working tree clean for the run.
- Full offline and browser run: **145/145 suites passed**.
- All **15 redesign suites** passed, covering owner, workflow, transport, recovery,
  isolation, and conversion behavior.
- Both required collector commands passed **80 tests each** after implementation.
- Nine design/handoff documents have no missing local links; whitespace checks pass.
- Full receipt: `.cache/redesign-final-browser.json` in this worktree. Browser
  screenshots and results: `.cache/application-candidate-browser/`.
- The final handoff commit changes documentation only after this verified runtime
  revision; normal pre-commit checks also run for that commit.

## Deliberate operational limits

The candidate host and CLI are explicit opt-ins. Authorized archive/model readers,
paired-extension authentication, and read-only reply context are injected by trusted
composition. The standalone local host configures none by default. Incomplete
attachment evidence remains blocked; no missing data is silently inferred.

The offline converter retains a complete predecessor snapshot alongside operational
owner records and a review report. Some legacy-only history stays in that immutable
archive. Ambiguous mappings are reported and inert. Tests used fictional temporary
state, not the user's production database or mailbox. Old approvals and imported
reminders do not become executable simply by opening the candidate.

No merge to main, deployment, production conversion, live inference, provider action,
notification delivery, or cutover occurred. Future activation requires a separate
operational plan against the actual installation. See the
[local candidate guide](07-candidate-guide.md) for usage and module navigation.
