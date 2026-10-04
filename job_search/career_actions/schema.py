"""Migration 18: exact send approvals, calendar ownership, complete agenda snapshots."""
SCHEMA = r'''
CREATE TABLE career_reply_sources (
 evidence_id TEXT PRIMARY KEY REFERENCES mail_evidence(evidence_id), application_id TEXT NOT NULL,
 source_json TEXT NOT NULL, source_hash TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE career_send_proposals (
 proposal_id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
 account_id TEXT NOT NULL, evidence_id TEXT NOT NULL REFERENCES mail_evidence(evidence_id),
 payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, source_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired','executing','accepted','uncertain','observed_sent','failed')),
 expires_at TEXT NOT NULL, remote_id TEXT NOT NULL DEFAULT '', send_requested_at TEXT,
 observed_evidence_id TEXT, error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TRIGGER career_send_immutable BEFORE UPDATE OF application_id,account_id,evidence_id,payload_json,payload_hash,source_hash,expires_at ON career_send_proposals BEGIN SELECT RAISE(ABORT,'send proposal content is immutable'); END;
CREATE TABLE career_action_audit (
 audit_id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL, operation TEXT NOT NULL,
 actor_kind TEXT NOT NULL, source_ref TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER career_audit_no_update BEFORE UPDATE ON career_action_audit BEGIN SELECT RAISE(ABORT,'action audit is append-only'); END;
CREATE TRIGGER career_audit_no_delete BEFORE DELETE ON career_action_audit BEGIN SELECT RAISE(ABORT,'action audit is append-only'); END;
CREATE TABLE career_agenda_snapshots (
 account_id TEXT PRIMARY KEY, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
 items_json TEXT NOT NULL, checked_at TEXT NOT NULL, last_attempt_at TEXT NOT NULL,
 error_code TEXT NOT NULL DEFAULT ''
);
CREATE TABLE career_confirmation_checks (
 proposal_id TEXT NOT NULL REFERENCES career_send_proposals(proposal_id), evidence_id TEXT NOT NULL REFERENCES mail_evidence(evidence_id),
 checked_at TEXT NOT NULL, PRIMARY KEY(proposal_id,evidence_id)
);
CREATE TABLE career_commitments (
 commitment_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES career_send_proposals(proposal_id),
 confirmation_evidence_id TEXT NOT NULL REFERENCES mail_evidence(evidence_id), round_id TEXT REFERENCES interview_rounds(round_id),
 starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL,
 remote_id TEXT NOT NULL DEFAULT '', etag TEXT NOT NULL DEFAULT '', transaction_id TEXT NOT NULL,
 organizer TEXT NOT NULL, conflict_json TEXT NOT NULL DEFAULT '[]', details_json TEXT NOT NULL DEFAULT '{}',
 owned INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(proposal_id,confirmation_evidence_id)
);
INSERT OR IGNORE INTO automation_controls VALUES ('outlook_send',0,0,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
INSERT OR IGNORE INTO automation_controls VALUES ('calendar_commitments',0,0,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
'''
