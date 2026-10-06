"""Additive review quality ledger migration; never alter after release."""

SCHEMA = r"""

CREATE TABLE job_review_search_briefs (
 revision INTEGER PRIMARY KEY AUTOINCREMENT,
 brief_json TEXT NOT NULL,
 saved_at TEXT NOT NULL
);
CREATE TRIGGER job_review_search_briefs_no_update BEFORE UPDATE ON job_review_search_briefs
BEGIN SELECT RAISE(ABORT, 'search brief revisions are immutable'); END;
CREATE TRIGGER job_review_search_briefs_no_delete BEFORE DELETE ON job_review_search_briefs
BEGIN SELECT RAISE(ABORT, 'search brief revisions are immutable'); END;

CREATE TABLE job_review_calibration_entries (
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id),
 basis_sha256 TEXT NOT NULL, ordinal INTEGER NOT NULL,
 position INTEGER NOT NULL, related_group_json TEXT, actor TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(review_id,basis_sha256,ordinal),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
CREATE TABLE job_review_calibrations (
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id),
 basis_sha256 TEXT NOT NULL, artifact_json TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(review_id,basis_sha256)
);
CREATE TABLE job_review_finalizer_grants (
 grant_id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL UNIQUE,
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id), actor TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind='finalizer'), context_sha256 TEXT NOT NULL,
 basis_sha256 TEXT NOT NULL, runtime_id TEXT NOT NULL UNIQUE,
 runtime_json TEXT NOT NULL, launch_json TEXT,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE INDEX job_review_finalizer_grants_review ON job_review_finalizer_grants(review_id,expires_at);

CREATE TABLE job_review_availability (
 review_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL,
 snapshot_sha256 TEXT NOT NULL,
 result_json TEXT NOT NULL,
 PRIMARY KEY(review_id,ordinal),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);

PRAGMA user_version = 21;
"""
