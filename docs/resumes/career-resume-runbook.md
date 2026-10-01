# Career bank and tailored resumes

The **Career profile** page holds reusable, user-reviewed career facts. An approved
profile can produce a job-specific resume without an imported handwritten standard.
Every new generated resume uses the bundled Jake Gutierrez layout: US letter,
11-point base font, centered contact header, Education, Experience, Projects, and
Technical Skills. Empty sections and the optional summary are omitted in career mode.

## Import once, expand over time

1. Open **Career profile** in the dashboard. Import a text-based PDF, DOCX, text file,
   or paste resume text. Existing imported standards can also seed the bank.
2. The model worker extracts a **draft**, preserving source spans. Review the extracted
   fields and any duplicate/conflict notices. Fix mistakes in the structured editor.
3. Save and approve the facts. Approval is distinct from saving: unapproved model
   extraction never becomes evidence for a generated application resume.
4. Add projects, accomplishments, or skills later through the same editor. Edits
   create another draft; the previously approved revision remains available until
   the new draft is approved. Retired entries remain in historical revisions.

PDF/DOCX/text imports are capped at 5 MiB. Text extraction uses the existing isolated
document processors. Scanned PDFs without extractable text require a text-based
version; OCR is not included. Automatic extraction needs a configured resume model.
Structured editing and JSON import/export do not require model inference.

The bank, import sources, revisions, and composition snapshots are private state in
the configured resume SQLite database. They do not belong in Git or container images.
Use the existing private-state backup and portability procedures. JSON exports contain
personal information; save them outside the repository with owner-only permissions.

## Generate and review

Opening an application uses the approved career profile when one exists. Without one,
the existing imported-standard workflow remains available. The model worker generates
the factual resume first; research comparisons run only when requested.

The run freezes the approved profile revision, job, template, and model identity.
Selection matches career facts against job requirements, retains chronological
employment/education, and records both included and omitted facts. Pin an entry to
retain its content, or pin individual bullets. Exclude information that should not be
used for that job, then regenerate. Regeneration creates a new run and does not change
an already selected PDF. To use newly approved profile facts, choose **Use latest approved profile** or start a new career
generation request; retrying an old run preserves its original facts.

Proposed wording is tied to individual source facts. Quantities, qualifications,
ownership, terminology, and employer/project associations are checked; an additional
model assessment rejects unsupported rewrites. Review remains necessary: model
judgment does not prove factual equivalence. Rejected/unavailable rewriting falls back
to the approved wording and reports that condition in the workspace.

The one-page fitter removes lower-priority optional content, displays what was omitted,
and recompiles for at most six attempts. It does not shrink the template font or remove
pinned facts. If required content still overflows, shorten or unpin it and regenerate.
No overflowing result becomes selectable.

Review the complete composition, source-to-output wording, PDF, and explainable ATS
proxy score. **Approve** the exact factual PDF, then explicitly **Select** it for the
application. Download the PDF and, if needed, its exact stored LaTeX source. Submission
records the selected artifact and its career revision/composition identity. Uploading
the PDF to an external application remains manual.

The three synthetic research comparisons are separately labeled and cannot be selected
or imported as career facts. Their linked runs never replace the primary factual
workspace or its approval. Hermes receives bounded status and scores, not the bank or
resume wording.

## CLI

Use the same private runtime configuration as the dashboard and workers:

```bash
python3 -m job_search --config /PRIVATE/runtime.json resume career show
python3 -m job_search --config /PRIVATE/runtime.json resume career import --file /PRIVATE/resume.pdf
python3 -m job_search --config /PRIVATE/runtime.json resume career import-status --import-id import_ID
python3 -m job_search --config /PRIVATE/runtime.json resume career approve --revision-id career_revision_ID
python3 -m job_search --config /PRIVATE/runtime.json resume career generate \
  --application-id APPLICATION_ID --ats ashby --job-id JOB_ID
python3 -m job_search --config /PRIVATE/runtime.json resume career research --run-id run_ID
```

Use returned IDs, not the placeholders above. Imports and generation run on the normal
model worker; their queue messages contain only opaque IDs. `resume career save --file
/PRIVATE/profile.json --expected-revision-id REVISION_ID` saves a structured draft with
optimistic concurrency. Omit the expected revision only for the first draft. Mutating
CLI commands accept `--idempotency-key` for deliberate retries. `resume career export`
writes portable JSON to stdout; `seed-standard --standard-version-id VERSION_ID`
creates a reviewable draft from an existing normalized real standard.

## Toolchain, compatibility, and verification

Configure Tectonic, its pinned offline bundle, the PDF parser, and optional local or
Runpod model as described in [the resume runbook](resume-lab-runbook.md). On Linux,
document extraction and compilation use the networkless document service. Both local
and remote generation adapters support the career extraction/rewrite tasks.

The bundled Jake template adapts its pdfTeX-specific Unicode setup for Tectonic's
engine. Arbitrary file inclusion and runtime downloads remain prohibited; every
personal/model-provided field is escaped before insertion into the template.

Schema initialization transactionally migrates legacy runs to the imported-standard
source mode and preserves their versions, approval records, selections, and ordering.
Old artifacts retain their exact bytes and template identity. Do not run an older
application binary against a database after it has been upgraded; restore a stopped
deployment's complete backup if rollback is needed.

The focused offline suite is `python3 -m tests.test_career_resume`, alongside the career
store/import and dashboard tests. It exercises the complete approval/selection flow,
revision freezing, source checks, one-page fitting, research isolation, recovery, and
populated legacy migration. A real compiler/PDF test additionally verifies that the
bundled layout survives both logical and layout text extraction.
