"""Additive application-ledger schema. Public job catalog remains read-only."""

SCHEMA = r"""
CREATE TABLE job_reviews (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 review_id TEXT NOT NULL UNIQUE,
 mode TEXT NOT NULL CHECK(mode IN ('recurring','custom')),
 window_start TEXT NOT NULL, window_end TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','published','abandoned')),
 context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL,
 metadata_json TEXT NOT NULL, receipt_json TEXT,
 version INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX job_reviews_one_recurring ON job_reviews(mode) WHERE mode='recurring' AND status='active';
CREATE TABLE job_review_items (
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id),
 ordinal INTEGER NOT NULL, ats TEXT NOT NULL, job_id TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,
 assessment_json TEXT, reviewer TEXT, check_json TEXT, checker TEXT,
 claim_owner TEXT, claim_until TEXT,
 PRIMARY KEY(review_id,ordinal), UNIQUE(review_id,ats,job_id)
);
CREATE TABLE job_review_revisions (
 review_id TEXT NOT NULL, ordinal INTEGER NOT NULL, revision INTEGER NOT NULL,
 kind TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, value_json TEXT NOT NULL,
 PRIMARY KEY(review_id,ordinal,revision),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
CREATE TRIGGER job_review_revisions_immutable BEFORE UPDATE ON job_review_revisions
BEGIN SELECT RAISE(ABORT, 'review revisions are immutable'); END;
CREATE TABLE job_review_reads (
 review_id TEXT NOT NULL, ordinal INTEGER NOT NULL, actor TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL, through_offset INTEGER NOT NULL,
 PRIMARY KEY(review_id,ordinal,actor,snapshot_sha256),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
CREATE TABLE job_review_commands (
 idempotency_key TEXT PRIMARY KEY, operation TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 response_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE job_review_publications (
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id),
 list_id TEXT NOT NULL UNIQUE REFERENCES curated_shortlists(list_id),
 kind TEXT NOT NULL, part INTEGER NOT NULL,
 PRIMARY KEY(review_id,kind,part)
);
CREATE TABLE job_review_feedback (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT REFERENCES job_reviews(review_id),
 ordinal INTEGER, note TEXT NOT NULL, created_at TEXT NOT NULL
);
PRAGMA user_version = 14;
"""
