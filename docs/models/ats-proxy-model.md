# ATS proxy model

The resume lab implements a modern applicant-screening **proxy**, not a claim that
an employer or vendor exposes a universal resume score.  The proxy models the
observable stages shared by applicant tracking systems, recruiting search products,
and ranking assistants while keeping every scoring decision inspectable.

## Evidence boundary

The following behaviours are directly observable in ATS documentation, recruiter
workflows, exported candidate records, and recruiting search products:

- resumes are parsed into text and structured fields before search or review;
- recruiters can filter or search by exact terms, Boolean expressions, titles,
  skills, credentials, location, and experience;
- some products add synonym, semantic, or AI-assisted matching;
- eligibility or knockout answers are distinct from relevance ranking; and
- the parsed artifact can differ materially from the source document.

No public evidence establishes one shared proprietary formula or score scale across
Greenhouse, Ashby, Lever, Workday, or the matching products employers connect to
them.  The proxy's weights, partial-credit rules, semantic thresholds, and combined
0-100 score are therefore explicit, versioned engineering inferences.  They must be
calibrated as a ranking model, never described as an employer's actual score or as a
probability of being hired.

## Version 1 pipeline

`ats_proxy_v1` evaluates the exact rendered PDF that could be selected for an
application:

1. Extract logical and layout text and record page count, missing anchors, duplicate
   blocks, character damage, and source-to-PDF loss.
2. Convert the job description into a source-grounded requirement graph.  A local
   LLM may classify clauses and AND/OR relationships, but every returned quotation
   must occur in the input description and model spans may not overlap. Graphs are
   capped at 200 distinct criteria; an oversized description fails closed instead of
   silently dropping requirements or starting unbounded scoring work.
3. Resolve objective constraints and exact, alias, acronym, title, tool, credential,
   numeric, and Boolean matches deterministically.
4. Present a bounded set of source-anchored resume claims for possible non-literal
   evidence. The bundled local driver supports one batch task for the unresolved
   requirements, while retaining a singular compatibility task. Every adjudication
   must cite an exact substring of one of the supplied claims; a malformed or incomplete
   batch contributes no semantic overrides. Version 1 does not implement embedding
   retrieval.
5. Compute the score with versioned deterministic arithmetic.  The LLM never returns
   or overrides the overall score.

The full hybrid path binds the model producer/scorer revision, requirement graph, job,
parsed resume text, and PDF fingerprints to persisted artifacts and cached evaluations.
When a local model is unavailable or optional adjudication fails, the lexical path
still produces a clearly labelled `deterministic_fallback` result.

Each job-scoped run also freezes the complete hand-written leaderboard and its exact
artifact/evaluation bindings. Reopening a historical run never recomputes its winner
from today's active standards. Failed-run retry is append-only: it creates a successor
with the same frozen inputs and four fresh items rather than rewriting the failed run.

## Score semantics

- Must/basic criteria have weight 2.
- Preferred criteria and core responsibilities have weight 1.
- Boilerplate has weight 0.
- Met, partial, absent, and contradicted evidence contribute 1.0, 0.5, 0.0,
  and 0.0 respectively.
- Requirement evidence contributes 80 percent of `parsed_fit`; bounded recruiter
  search visibility contributes 20 percent.
- Eligibility is reported separately and cannot be hidden by a high fit score.
- Keyword repetition is capped.  A separate adversarial benchmark measures how much
  the proxy can still be gamed.

Protected demographic information, compensation, job preference score, and posting
freshness are never resume-fit features.  Fit bands are comparison aids, not hiring
predictions.

## Resume comparisons

For each prepared job, all active user-authored baseline resumes are scored first, with
an enforced maximum of 25 active standards. The five workbench choice groups are all
of those individually selectable standards, one approval-gated factual rewrite, and
three comparison-only synthetic variants. The highest-ranked baseline is the
recommendation and anchors the two standard-derived comparisons; the other two are
independent research controls:

- `grounded_rewrite`: keeps each source claim in its original field and conserves its
  ordered canonical concepts, allowing only article, punctuation, case, and reviewed
  equivalent terminology changes such as `Kubernetes`/`k8s`; semantic operators,
  numbers and their neighboring concept associations, currency, ranges, negation,
  seniority, and scope remain conserved. Global skill claims can enrich only summary
  or skill fields and can never be projected into an employer or project bullet;
- `standard_exaggerated`: retains that baseline's career skeleton but contains
  explicitly synthetic claims;
- `market_ideal`: a coherent fictional ideal for the role; and
- `keyword_adversarial`: a visible-text score-maximizing attempt with one bounded
  score-feedback generation pass.

Score feedback is exposed only to `keyword_adversarial`: the worker may produce a
second draft from the first draft's source-anchored remaining gaps and retains the
better valid result. The grounded rewrite, standard exaggeration, and market-ideal
variant each receive a single generation pass.

Only active user-authored baselines and explicitly approved grounded rewrites can be
selected for a real application.  Synthetic artifacts stay in the local comparison
boundary and are visibly marked as non-submittable. The highest job-specific baseline
score is recommended first; manual rank is only its deterministic tie-break, and the
user still explicitly selects the artifact used for an application.

## Calibration

Tests use controlled perturbations: remove a required skill, substitute a synonym,
change a numeric threshold, damage PDF extraction, or repeat a keyword.  Each change
must move only its expected features.  Production observations may later measure
whether the proxy ordering correlates with recruiter screens and interviews, but
must not train on protected attributes or retroactively present the score as causal.
