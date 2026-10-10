# Correspondence owner

`CorrespondenceOperations` owns account-scoped message identity, immutable source
revisions, reviewed associations and bounded conversation/evidence reads. Text stays
in the existing encrypted archive; `evidence()` accepts its public `read_message`
interface and verifies the stored hash and allowed account. No provider calls or
application mutations belong here.

Use `record_message` for ingestion, `link_message` for a human-reviewed initial
association, and `correct_association` for later changes. Opaque provider version
tokens cannot establish time order: ingestion marks known older revisions with
`make_current=False`. Numeric versions are compared automatically. Replaying an
existing revision never changes the current pointer.

Mutation participants take the orchestrator's transaction; public reads take its
read snapshot. Full-body plaintext is absent from rows, receipts and history.

Verify with `python3 -m tests.test_redesign_evidence`.
