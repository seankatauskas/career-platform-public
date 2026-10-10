# Understanding capability

This Applications capability writes analyses, findings, and pending proposals only.
It cannot accept facts, create tasks, or execute external actions. Review execution
belongs to the application workflow.

1. Build bounded `AnalysisInput` from authorized evidence and candidate context.
2. Claim the versioned input through `claim_analysis`.
3. Run injectable `SharedAnalyzer` outside a write transaction using the existing
   structured generation-provider interface. Record failures with a bounded code.
4. Persist via `store_analysis`, then separately project the persisted findings with
   `project_requests` or typed `project_analysis` recipes. Interrupted projection
   resumes without another model call.
   The Applications workflow compares projected closures and submissions with
   current owner records in the same transaction. Exact restatements become
   `superseded` with an `Already recorded` reason and retain their evidence and
   audit trail. This applies only to unblocked proposals with unchanged context
   versions and, for submissions, the same pursuit, status, and timestamp.
   Conflicting outcomes, changed timestamps, uncertain associations, and other
   operations remain pending. No lifecycle state or external approval changes.
5. Read `list_pending`; human review calls `resolve` in the same transaction as the
   owning operation. `replace` preserves old decisions and creates edited proposals.

`project_requests` maps all supported lifecycle fact kinds into named pending
operations. Missing typed details, interval/timezone, identity or consequence
previews remain blocked review rows; the mapper never guesses from prose.
Existing interview changes require trusted `context.interview_previews`, and
outcomes require trusted `context.closure_previews`. New fact proposals do not
implicitly create tasks or enable reminders. Separate request findings create
their own independent task proposals.

`record_source_failure` records unavailable evidence without fabricated text.
`coverage` supplies bounded operational summaries and validated finding excerpts.
Replacement proposals keep their blockers unless the human explicitly names each
resolved blocker and supplies an attributed reason.

Finding quotes are exact validated source spans. Full message text is transient.
Duplicate lineage reuses prior decisions across model versions; changed wording on
the same evidence requires comparison review. Renewed authored messages have new
source identity. Incomplete coverage stays visible and blocks acceptance.

Verify with `python3 -m tests.test_redesign_evidence`.
