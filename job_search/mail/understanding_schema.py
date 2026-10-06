"""Durable, authenticated shared mail analyses and their projection provenance."""

SCHEMA = r"""
CREATE TABLE mail_understanding_analyses (
 analysis_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL UNIQUE,
 account_id TEXT NOT NULL, immutable_message_id TEXT NOT NULL,
 evidence_id TEXT NOT NULL REFERENCES mail_evidence(evidence_id),
 observation_id TEXT NOT NULL REFERENCES lifecycle_mail_observations(observation_id),
 mode TEXT NOT NULL CHECK(mode IN ('shared','shadow','replay')), replay_id TEXT,
 request_json TEXT NOT NULL, relevance TEXT,
 state TEXT NOT NULL CHECK(state IN ('claimed','saved','projected','failed','uncertain')),
 claim_token TEXT, lease_expires_at TEXT, attempts INTEGER NOT NULL DEFAULT 1,
 last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, saved_at TEXT
);
CREATE INDEX mail_understanding_message ON mail_understanding_analyses(account_id,immutable_message_id,created_at);
CREATE TABLE mail_understanding_sources (
 analysis_id TEXT NOT NULL REFERENCES mail_understanding_analyses(analysis_id), source_id TEXT NOT NULL,
 key_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
 aad_sha256 TEXT NOT NULL, plaintext_sha256 TEXT NOT NULL, plaintext_chars INTEGER NOT NULL,
 PRIMARY KEY(analysis_id,source_id)
);
CREATE TABLE mail_understanding_ownership (
 evidence_id TEXT PRIMARY KEY REFERENCES mail_evidence(evidence_id),
 analysis_id TEXT REFERENCES mail_understanding_analyses(analysis_id), created_at TEXT NOT NULL
);
CREATE TABLE mail_understanding_findings (
 finding_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES mail_understanding_analyses(analysis_id),
 type TEXT NOT NULL CHECK(type IN ('event','action','temporal','uncertainty')),
 ordinal INTEGER NOT NULL, value_json TEXT NOT NULL,
 replacement_of TEXT REFERENCES mail_understanding_findings(finding_id), created_at TEXT NOT NULL,
 UNIQUE(analysis_id,type,ordinal)
);
CREATE TABLE mail_understanding_projections (
 finding_id TEXT NOT NULL REFERENCES mail_understanding_findings(finding_id),
 kind TEXT NOT NULL CHECK(kind IN ('event_proposal','temporal_proposal','task')),
 target_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(finding_id,kind)
);
CREATE INDEX mail_understanding_projection_target ON mail_understanding_projections(kind,target_id);
CREATE TABLE mail_understanding_decisions (
 decision_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL UNIQUE REFERENCES mail_understanding_findings(finding_id),
 decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected','held')),
 application_id TEXT REFERENCES applications(application_id), actor_kind TEXT NOT NULL,
 reason TEXT NOT NULL, policy_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE mail_understanding_inference_work (
 analysis_id TEXT NOT NULL REFERENCES mail_understanding_analyses(analysis_id),
 claim_token TEXT NOT NULL, work_id TEXT NOT NULL REFERENCES work_items(work_id),
 work_revision INTEGER NOT NULL, before_invocations_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(analysis_id,claim_token)
);
CREATE TABLE mail_understanding_inference_results (
 analysis_id TEXT NOT NULL, claim_token TEXT NOT NULL,
 invocation_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(analysis_id,claim_token),
 FOREIGN KEY(analysis_id,claim_token) REFERENCES mail_understanding_inference_work(analysis_id,claim_token)
);
ALTER TABLE event_proposals ADD COLUMN understanding_finding_id TEXT REFERENCES mail_understanding_findings(finding_id);
ALTER TABLE temporal_proposals ADD COLUMN understanding_finding_id TEXT REFERENCES mail_understanding_findings(finding_id);
CREATE TRIGGER mail_understanding_request_immutable BEFORE UPDATE ON mail_understanding_analyses
WHEN OLD.input_sha256 IS NOT NEW.input_sha256 OR OLD.request_json IS NOT NEW.request_json
 OR OLD.account_id IS NOT NEW.account_id OR OLD.immutable_message_id IS NOT NEW.immutable_message_id
 OR OLD.evidence_id IS NOT NEW.evidence_id OR OLD.observation_id IS NOT NEW.observation_id
 OR OLD.mode IS NOT NEW.mode OR OLD.replay_id IS NOT NEW.replay_id
 OR (OLD.saved_at IS NOT NULL AND (OLD.saved_at IS NOT NEW.saved_at OR OLD.relevance IS NOT NEW.relevance))
BEGIN SELECT RAISE(ABORT,'mail understanding input is immutable'); END;
CREATE TRIGGER event_understanding_provenance_immutable BEFORE UPDATE ON event_proposals
WHEN OLD.understanding_finding_id IS NOT NEW.understanding_finding_id
BEGIN SELECT RAISE(ABORT,'mail understanding provenance is immutable'); END;
CREATE TRIGGER temporal_understanding_provenance_immutable BEFORE UPDATE ON temporal_proposals
WHEN OLD.understanding_finding_id IS NOT NEW.understanding_finding_id
BEGIN SELECT RAISE(ABORT,'mail understanding provenance is immutable'); END;
"""

for _table in ('mail_understanding_sources', 'mail_understanding_ownership',
               'mail_understanding_findings', 'mail_understanding_projections',
               'mail_understanding_decisions', 'mail_understanding_inference_work',
               'mail_understanding_inference_results'):
    for _operation in ('UPDATE', 'DELETE'):
        SCHEMA += f"CREATE TRIGGER {_table}_no_{_operation.lower()} BEFORE {_operation} ON {_table} BEGIN SELECT RAISE(ABORT,'mail understanding history is append-only'); END;\n"
