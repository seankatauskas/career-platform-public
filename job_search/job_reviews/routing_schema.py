"""Additive conservative screening ledger; never alter after release."""

SCHEMA = r"""
ALTER TABLE job_review_grants ADD COLUMN purpose TEXT NOT NULL DEFAULT 'detailed'
 CHECK(purpose IN ('detailed','screening') AND (purpose='detailed' OR kind='primary'));
CREATE TRIGGER job_review_grant_purpose_immutable BEFORE UPDATE OF purpose ON job_review_grants
WHEN NEW.purpose <> OLD.purpose
BEGIN SELECT RAISE(ABORT, 'review grant purpose is immutable'); END;

CREATE TABLE job_review_execution_policies (
 review_id TEXT PRIMARY KEY REFERENCES job_reviews(review_id),
 policy_json TEXT NOT NULL, policy_sha256 TEXT NOT NULL, frozen_at TEXT NOT NULL
);
CREATE TRIGGER job_review_execution_policies_no_update BEFORE UPDATE ON job_review_execution_policies
BEGIN SELECT RAISE(ABORT, 'review execution policy is immutable'); END;
CREATE TRIGGER job_review_execution_policies_no_delete BEFORE DELETE ON job_review_execution_policies
BEGIN SELECT RAISE(ABORT, 'review execution policy is immutable'); END;

CREATE TABLE job_review_routes (
 review_id TEXT NOT NULL, ordinal INTEGER NOT NULL, expected_revision INTEGER NOT NULL,
 snapshot_sha256 TEXT NOT NULL, context_sha256 TEXT NOT NULL,
 route TEXT NOT NULL CHECK(route IN ('detailed','exclude')),
 grant_id TEXT REFERENCES job_review_grants(grant_id), actor TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, value_json TEXT NOT NULL, response_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(review_id,ordinal,expected_revision,snapshot_sha256,context_sha256),
 UNIQUE(grant_id,ordinal),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
CREATE TRIGGER job_review_routes_no_update BEFORE UPDATE ON job_review_routes
BEGIN SELECT RAISE(ABORT, 'review routing receipts are immutable'); END;
CREATE TRIGGER job_review_routes_no_delete BEFORE DELETE ON job_review_routes
BEGIN SELECT RAISE(ABORT, 'review routing receipts are immutable'); END;
PRAGMA user_version = 22;
"""
