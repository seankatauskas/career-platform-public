-- Historical schema from integration commit 9924c65, for migration regression tests.

CREATE TABLE IF NOT EXISTS resume_lab_schema (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_standards (
    standard_id        TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    manual_rank        INTEGER NOT NULL CHECK (manual_rank > 0),
    active             INTEGER NOT NULL CHECK (active IN (0, 1)),
    active_version_id  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS resume_standards_active_rank
ON resume_standards(manual_rank) WHERE active = 1;

CREATE TABLE IF NOT EXISTS resume_standard_versions (
    version_id       TEXT PRIMARY KEY,
    standard_id      TEXT NOT NULL,
    version_number   INTEGER NOT NULL CHECK (version_number > 0),
    tex_source       TEXT NOT NULL,
    plain_text       TEXT NOT NULL,
    claims_json      TEXT NOT NULL,
    normalized_content_json TEXT,
    import_metadata_json TEXT,
    content_sha256   TEXT NOT NULL CHECK (length(content_sha256) = 64),
    authored_by      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (standard_id, version_number),
    UNIQUE (standard_id, content_sha256),
    FOREIGN KEY (standard_id) REFERENCES resume_standards(standard_id)
);

CREATE TRIGGER IF NOT EXISTS resume_standard_versions_no_update
BEFORE UPDATE ON resume_standard_versions
BEGIN
    SELECT RAISE(ABORT, 'standard resume versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_standard_versions_no_delete
BEFORE DELETE ON resume_standard_versions
BEGIN
    SELECT RAISE(ABORT, 'standard resume versions are immutable');
END;

CREATE TABLE IF NOT EXISTS resume_artifacts (
    artifact_id            TEXT PRIMARY KEY,
    purpose                TEXT NOT NULL CHECK (
        purpose IN ('real_application', 'synthetic_research')
    ),
    variant_kind           TEXT NOT NULL CHECK (
        variant_kind IN (
            'standard', 'grounded_rewrite', 'standard_exaggerated',
            'market_ideal', 'keyword_adversarial'
        )
    ),
    ats                    TEXT NOT NULL,
    job_id                 TEXT NOT NULL,
    job_fingerprint        TEXT NOT NULL CHECK (length(job_fingerprint) = 64),
    base_version_id        TEXT,
    tex_source             TEXT NOT NULL,
    intended_text          TEXT NOT NULL,
    parsed_text            TEXT NOT NULL,
    claims_json            TEXT NOT NULL,
    managed_relative_path  TEXT NOT NULL,
    pdf_sha256             TEXT NOT NULL CHECK (length(pdf_sha256) = 64),
    content_sha256         TEXT NOT NULL CHECK (length(content_sha256) = 64),
    parse_fidelity         REAL NOT NULL CHECK (parse_fidelity BETWEEN 0 AND 1),
    parse_safe             INTEGER NOT NULL CHECK (parse_safe IN (0, 1)),
    generator_revision     TEXT NOT NULL,
    study_id               TEXT,
    pair_id                TEXT,
    treatment              TEXT NOT NULL DEFAULT '',
    generation_seed        INTEGER,
    metadata_json          TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    UNIQUE (purpose, content_sha256),
    FOREIGN KEY (base_version_id) REFERENCES resume_standard_versions(version_id),
    CHECK (
        (purpose = 'real_application' AND artifact_id LIKE 'real\_%' ESCAPE '\'
         AND study_id IS NULL AND pair_id IS NULL AND generation_seed IS NULL)
        OR
        (purpose = 'synthetic_research' AND artifact_id LIKE 'syn\_%' ESCAPE '\'
         AND study_id IS NOT NULL AND pair_id IS NOT NULL AND generation_seed IS NOT NULL)
    )
);

CREATE TRIGGER IF NOT EXISTS resume_artifacts_no_update
BEFORE UPDATE ON resume_artifacts
BEGIN
    SELECT RAISE(ABORT, 'resume artifacts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifacts_no_delete
BEFORE DELETE ON resume_artifacts
BEGIN
    SELECT RAISE(ABORT, 'resume artifacts are immutable');
END;

CREATE TABLE IF NOT EXISTS resume_evaluation_cache (
    cache_key                       TEXT PRIMARY KEY,
    artifact_id                     TEXT NOT NULL,
    job_fingerprint                 TEXT NOT NULL CHECK (length(job_fingerprint) = 64),
    requirement_graph_fingerprint   TEXT NOT NULL CHECK (length(requirement_graph_fingerprint) = 64),
    scorer_revision                 TEXT NOT NULL,
    result_json                     TEXT NOT NULL,
    created_at                      TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);

CREATE TRIGGER IF NOT EXISTS resume_evaluation_cache_no_update
BEFORE UPDATE ON resume_evaluation_cache
BEGIN
    SELECT RAISE(ABORT, 'resume evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_evaluation_cache_no_delete
BEFORE DELETE ON resume_evaluation_cache
BEGIN
    SELECT RAISE(ABORT, 'resume evaluations are immutable');
END;

-- One content-addressed evaluation may validly apply to multiple artifacts with
-- identical parsed text.  Persist every artifact/evaluation binding explicitly so a
-- caller cannot attach a score from another job or artifact during selection.
CREATE TABLE IF NOT EXISTS resume_artifact_evaluations (
    artifact_id   TEXT NOT NULL,
    cache_key     TEXT NOT NULL,
    linked_at     TEXT NOT NULL,
    PRIMARY KEY (artifact_id, cache_key),
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id),
    FOREIGN KEY (cache_key) REFERENCES resume_evaluation_cache(cache_key)
);
CREATE TRIGGER IF NOT EXISTS resume_artifact_evaluations_no_update
BEFORE UPDATE ON resume_artifact_evaluations
BEGIN
    SELECT RAISE(ABORT, 'resume artifact evaluation links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifact_evaluations_no_delete
BEFORE DELETE ON resume_artifact_evaluations
BEGIN
    SELECT RAISE(ABORT, 'resume artifact evaluation links are immutable');
END;

CREATE TABLE IF NOT EXISTS application_resume_selections (
    selection_seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_id     TEXT NOT NULL UNIQUE,
    application_id   TEXT NOT NULL,
    artifact_id      TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    selected_by      TEXT NOT NULL CHECK (selected_by = 'user'),
    selected_at      TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);
CREATE INDEX IF NOT EXISTS application_resume_selections_current
ON application_resume_selections(application_id, selection_seq DESC);

CREATE TRIGGER IF NOT EXISTS application_resume_selections_real_only
BEFORE INSERT ON application_resume_selections
WHEN NOT EXISTS (
    SELECT 1 FROM resume_artifacts a
    WHERE a.artifact_id = NEW.artifact_id
      AND a.purpose = 'real_application'
      AND a.parse_safe = 1
      AND a.variant_kind IN ('standard', 'grounded_rewrite')
)
BEGIN
    SELECT RAISE(ABORT, 'only parse-safe real artifacts may be selected');
END;
CREATE TRIGGER IF NOT EXISTS application_resume_selections_no_update
BEFORE UPDATE ON application_resume_selections
BEGIN
    SELECT RAISE(ABORT, 'application resume selections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS application_resume_selections_no_delete
BEFORE DELETE ON application_resume_selections
BEGIN
    SELECT RAISE(ABORT, 'application resume selections are append-only');
END;

CREATE TABLE IF NOT EXISTS resume_runs (
    run_id              TEXT PRIMARY KEY,
    application_id      TEXT NOT NULL,
    ats                 TEXT NOT NULL,
    job_id              TEXT NOT NULL,
    job_snapshot_json   TEXT NOT NULL,
    job_fingerprint     TEXT NOT NULL CHECK (length(job_fingerprint) = 64),
    selected_standard_id TEXT NOT NULL,
    base_version_id     TEXT NOT NULL,
    base_fingerprint    TEXT NOT NULL CHECK (length(base_fingerprint) = 64),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed')
    ),
    attempt             INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    error               TEXT NOT NULL DEFAULT '',
    result_fingerprint  TEXT,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (selected_standard_id) REFERENCES resume_standards(standard_id),
    FOREIGN KEY (base_version_id) REFERENCES resume_standard_versions(version_id)
);
CREATE INDEX IF NOT EXISTS resume_runs_status
ON resume_runs(status, created_at DESC);

-- SQLite timestamps are intentionally human-readable and only precise to a second.
-- Keep a separate insertion sequence so two runs created in that same second still
-- have one durable, unambiguous order.  A separate table permits additive upgrades of
-- existing sidecars because SQLite cannot add an AUTOINCREMENT column in place.
CREATE TABLE IF NOT EXISTS resume_run_order (
    run_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL UNIQUE,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_order_no_update
BEFORE UPDATE ON resume_run_order
BEGIN
    SELECT RAISE(ABORT, 'resume run order is immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_order_no_delete
BEFORE DELETE ON resume_run_order
BEGIN
    SELECT RAISE(ABORT, 'resume run order is immutable');
END;

-- The exact requirement analysis used to rank the winning standard belongs to the
-- run.  Looking up "the latest" analysis at worker time would make an already queued
-- experiment change underneath us.
CREATE TABLE IF NOT EXISTS resume_run_analyses (
    run_id                         TEXT PRIMARY KEY,
    requirement_graph_fingerprint TEXT NOT NULL CHECK (
        length(requirement_graph_fingerprint) = 64
    ),
    extraction_source             TEXT NOT NULL,
    clauses_json                  TEXT NOT NULL,
    created_at                    TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_analyses_no_update
BEFORE UPDATE ON resume_run_analyses
BEGIN
    SELECT RAISE(ABORT, 'resume run analyses are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_analyses_no_delete
BEFORE DELETE ON resume_run_analyses
BEGIN
    SELECT RAISE(ABORT, 'resume run analyses are immutable');
END;

-- Preserve the exact standard-resume leaderboard that selected the run base.  Product
-- views must never rebuild historical rankings from today's active standards.
CREATE TABLE IF NOT EXISTS resume_run_ranking_snapshots (
    run_id           TEXT PRIMARY KEY,
    rankings_json    TEXT NOT NULL,
    rankings_sha256  TEXT NOT NULL CHECK (length(rankings_sha256) = 64),
    created_at       TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_ranking_snapshots_no_update
BEFORE UPDATE ON resume_run_ranking_snapshots
BEGIN
    SELECT RAISE(ABORT, 'resume run ranking snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_ranking_snapshots_no_delete
BEFORE DELETE ON resume_run_ranking_snapshots
BEGIN
    SELECT RAISE(ABORT, 'resume run ranking snapshots are immutable');
END;

-- Retrying is a user command, not merely a status mutation.  Recording its key first
-- makes an exact replay safe even after the retried generation has completed.
CREATE TABLE IF NOT EXISTS resume_run_retry_commands (
    idempotency_key  TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    result_run_id    TEXT,
    run_attempt      INTEGER NOT NULL CHECK (run_attempt >= 0),
    requested_by     TEXT NOT NULL CHECK (requested_by = 'user'),
    reconciliation_acknowledged INTEGER NOT NULL DEFAULT 0
        CHECK (reconciliation_acknowledged IN (0, 1)),
    requested_at     TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id),
    FOREIGN KEY (result_run_id) REFERENCES resume_runs(run_id)
);
CREATE TRIGGER IF NOT EXISTS resume_run_retry_commands_no_update
BEFORE UPDATE ON resume_run_retry_commands
BEGIN
    SELECT RAISE(ABORT, 'resume retry commands are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_retry_commands_no_delete
BEFORE DELETE ON resume_run_retry_commands
BEGIN
    SELECT RAISE(ABORT, 'resume retry commands are immutable');
END;

-- A work-item delivery claims a resume run with an opaque owner token.  A recovered
-- delivery replaces this row and increments resume_runs.attempt, so a stale worker can
-- no longer attach an artifact to the run after losing its lease.
CREATE TABLE IF NOT EXISTS resume_run_owners (
    run_id       TEXT PRIMARY KEY,
    run_attempt  INTEGER NOT NULL CHECK (run_attempt > 0),
    owner_token  TEXT NOT NULL,
    claimed_at   TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id)
);

CREATE TABLE IF NOT EXISTS resume_run_items (
    run_item_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    variant_kind      TEXT NOT NULL CHECK (
        variant_kind IN (
            'grounded_rewrite', 'standard_exaggerated',
            'market_ideal', 'keyword_adversarial'
        )
    ),
    purpose           TEXT NOT NULL CHECK (
        (variant_kind = 'grounded_rewrite' AND purpose = 'real_application')
        OR
        (variant_kind IN ('standard_exaggerated','market_ideal','keyword_adversarial')
         AND purpose = 'synthetic_research')
    ),
    base_version_id  TEXT,
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    status            TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed')),
    artifact_id       TEXT,
    output_fingerprint TEXT,
    error             TEXT NOT NULL DEFAULT '',
    attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    completed_at      TEXT,
    UNIQUE (run_id, variant_kind),
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id),
    FOREIGN KEY (base_version_id) REFERENCES resume_standard_versions(version_id),
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);

CREATE TRIGGER IF NOT EXISTS resume_runs_lineage_is_immutable
BEFORE UPDATE ON resume_runs
WHEN OLD.application_id IS NOT NEW.application_id
  OR OLD.ats IS NOT NEW.ats
  OR OLD.job_id IS NOT NEW.job_id
  OR OLD.job_snapshot_json IS NOT NEW.job_snapshot_json
  OR OLD.job_fingerprint IS NOT NEW.job_fingerprint
  OR OLD.selected_standard_id IS NOT NEW.selected_standard_id
  OR OLD.base_version_id IS NOT NEW.base_version_id
  OR OLD.base_fingerprint IS NOT NEW.base_fingerprint
  OR OLD.request_fingerprint IS NOT NEW.request_fingerprint
  OR OLD.idempotency_key IS NOT NEW.idempotency_key
BEGIN
    SELECT RAISE(ABORT, 'resume run lineage is immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_runs_no_delete
BEFORE DELETE ON resume_runs
BEGIN
    SELECT RAISE(ABORT, 'resume runs are append-only');
END;

CREATE TRIGGER IF NOT EXISTS resume_run_items_lineage_is_immutable
BEFORE UPDATE ON resume_run_items
WHEN OLD.run_id IS NOT NEW.run_id
  OR OLD.variant_kind IS NOT NEW.variant_kind
  OR OLD.purpose IS NOT NEW.purpose
  OR OLD.base_version_id IS NOT NEW.base_version_id
  OR OLD.input_fingerprint IS NOT NEW.input_fingerprint
BEGIN
    SELECT RAISE(ABORT, 'resume run item lineage is immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_items_succeeded_are_immutable
BEFORE UPDATE ON resume_run_items
WHEN OLD.status = 'succeeded'
BEGIN
    SELECT RAISE(ABORT, 'succeeded resume run items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_items_terminal_are_immutable
BEFORE UPDATE ON resume_run_items
WHEN OLD.status IN ('succeeded', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'terminal resume run items are immutable');
END;
DROP TRIGGER IF EXISTS resume_run_items_parent_terminal_are_immutable;
CREATE TRIGGER resume_run_items_parent_terminal_are_immutable
BEFORE UPDATE ON resume_run_items
WHEN EXISTS (
    SELECT 1 FROM resume_runs r
    WHERE r.run_id = OLD.run_id AND r.status IN ('succeeded', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'terminal resume run items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_run_items_no_delete
BEFORE DELETE ON resume_run_items
BEGIN
    SELECT RAISE(ABORT, 'resume run items are append-only');
END;
CREATE TRIGGER IF NOT EXISTS resume_runs_succeeded_are_immutable
BEFORE UPDATE ON resume_runs
WHEN OLD.status = 'succeeded'
BEGIN
    SELECT RAISE(ABORT, 'succeeded resume runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_runs_terminal_are_immutable
BEFORE UPDATE ON resume_runs
WHEN OLD.status IN ('succeeded', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'terminal resume runs are immutable');
END;

CREATE TABLE IF NOT EXISTS resume_artifact_approvals (
    approval_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    artifact_id       TEXT NOT NULL,
    pdf_sha256        TEXT NOT NULL CHECK (length(pdf_sha256) = 64),
    content_sha256    TEXT NOT NULL CHECK (length(content_sha256) = 64),
    idempotency_key   TEXT NOT NULL UNIQUE,
    approved_by       TEXT NOT NULL CHECK (approved_by = 'user'),
    approved_at       TEXT NOT NULL,
    UNIQUE (run_id, artifact_id),
    FOREIGN KEY (run_id) REFERENCES resume_runs(run_id),
    FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
);

DROP TRIGGER IF EXISTS resume_artifact_approvals_exact_grounded;
CREATE TRIGGER resume_artifact_approvals_exact_grounded
BEFORE INSERT ON resume_artifact_approvals
WHEN NOT EXISTS (
    SELECT 1
    FROM resume_runs r
    JOIN resume_run_items i
      ON i.run_id = r.run_id
     AND i.variant_kind = 'grounded_rewrite'
     AND i.purpose = 'real_application'
     AND i.status = 'succeeded'
     AND i.artifact_id = NEW.artifact_id
    JOIN resume_artifacts a
      ON a.artifact_id = i.artifact_id
     AND a.variant_kind = 'grounded_rewrite'
     AND a.purpose = 'real_application'
     AND a.parse_safe = 1
     AND a.pdf_sha256 = NEW.pdf_sha256
     AND a.content_sha256 = NEW.content_sha256
     AND json_extract(a.metadata_json, '$.grounding_validator_revision')
         = 'grounding-boundary-v5-relational'
     AND json_extract(a.metadata_json, '$.grounding_equivalence_revision')
         = 'grounding-equivalences-v3'
    WHERE r.run_id = NEW.run_id
      AND r.status IN ('succeeded', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'approval must bind a succeeded run grounded artifact');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifact_approvals_no_update
BEFORE UPDATE ON resume_artifact_approvals
BEGIN
    SELECT RAISE(ABORT, 'resume artifact approvals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS resume_artifact_approvals_no_delete
BEFORE DELETE ON resume_artifact_approvals
BEGIN
    SELECT RAISE(ABORT, 'resume artifact approvals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS application_resume_selections_grounded_approved
BEFORE INSERT ON application_resume_selections
WHEN EXISTS (
    SELECT 1 FROM resume_artifacts a
    WHERE a.artifact_id = NEW.artifact_id
      AND a.variant_kind = 'grounded_rewrite'
)
AND NOT EXISTS (
    SELECT 1
    FROM resume_artifact_approvals p
    JOIN resume_runs r ON r.run_id = p.run_id
    WHERE p.artifact_id = NEW.artifact_id
      AND r.application_id = NEW.application_id
)
BEGIN
    SELECT RAISE(ABORT, 'grounded artifact requires exact run approval');
END;
DROP TRIGGER IF EXISTS application_resume_selections_current_grounding;
CREATE TRIGGER application_resume_selections_current_grounding
BEFORE INSERT ON application_resume_selections
WHEN EXISTS (
    SELECT 1 FROM resume_artifacts a
    WHERE a.artifact_id = NEW.artifact_id
      AND a.variant_kind = 'grounded_rewrite'
      AND (
          json_extract(a.metadata_json, '$.grounding_validator_revision')
              IS NOT 'grounding-boundary-v5-relational'
          OR json_extract(a.metadata_json, '$.grounding_equivalence_revision')
              IS NOT 'grounding-equivalences-v3'
      )
)
BEGIN
    SELECT RAISE(ABORT, 'grounded artifact safety revision is stale');
END;
