"""Additive, immutable adjudication provenance, separate from blind reviews."""
SCHEMA = r"""
CREATE TABLE job_review_adjudicator_grants (
 grant_id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL UNIQUE,
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id), actor TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind='adjudicator'), context_sha256 TEXT NOT NULL,
 runtime_id TEXT NOT NULL UNIQUE, runtime_json TEXT NOT NULL, launch_json TEXT,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE job_review_adjudicator_items (
 grant_id TEXT NOT NULL REFERENCES job_review_adjudicator_grants(grant_id),
 review_id TEXT NOT NULL, ordinal INTEGER NOT NULL, basis_sha256 TEXT NOT NULL,
 judgments_read INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(grant_id,ordinal), UNIQUE(review_id,ordinal,basis_sha256),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
CREATE TABLE job_review_adjudicator_reads (
 grant_id TEXT NOT NULL REFERENCES job_review_adjudicator_grants(grant_id),
 section TEXT NOT NULL, through_offset INTEGER NOT NULL,
 PRIMARY KEY(grant_id,section)
);
CREATE TABLE job_review_resolutions (
 review_id TEXT NOT NULL, ordinal INTEGER NOT NULL, basis_sha256 TEXT NOT NULL,
 grant_id TEXT NOT NULL REFERENCES job_review_adjudicator_grants(grant_id),
 actor TEXT NOT NULL, choice TEXT NOT NULL CHECK(choice IN ('primary','check','unresolved')),
 request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(review_id,ordinal,basis_sha256),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
CREATE TRIGGER job_review_resolutions_no_update BEFORE UPDATE ON job_review_resolutions
BEGIN SELECT RAISE(ABORT, 'review resolutions are immutable'); END;
CREATE TRIGGER job_review_resolutions_no_delete BEFORE DELETE ON job_review_resolutions
BEGIN SELECT RAISE(ABORT, 'review resolutions are immutable'); END;
CREATE INDEX job_review_adjudicator_grants_review ON job_review_adjudicator_grants(review_id,expires_at);
PRAGMA user_version = 23;
"""
