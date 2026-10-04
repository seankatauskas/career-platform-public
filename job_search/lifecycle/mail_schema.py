"""Durable observations, reviewed associations, and bounded replay checkpoints."""
SCHEMA = r'''
CREATE TABLE lifecycle_mail_folders (account_id TEXT NOT NULL,folder_id TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('inbox','sentitems','drafts')),PRIMARY KEY(account_id,role));
CREATE TABLE lifecycle_mail_observations (
 observation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, immutable_message_id TEXT NOT NULL,
 conversation_ref TEXT NOT NULL DEFAULT '', folder_ref TEXT NOT NULL DEFAULT '',
 direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound','draft','unknown')),
 sender TEXT NOT NULL DEFAULT '', recipients_json TEXT NOT NULL DEFAULT '[]', subject TEXT NOT NULL DEFAULT '',
 received_at TEXT, sent_at TEXT, source_at TEXT NOT NULL, modified_at TEXT NOT NULL,
 evidence_id TEXT REFERENCES mail_evidence(evidence_id), archive_id TEXT REFERENCES mail_archive(archive_id),
 action_id TEXT REFERENCES action_proposals(action_id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(account_id,immutable_message_id)
);
CREATE TABLE lifecycle_mail_revisions (
 revision_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES lifecycle_mail_observations(observation_id),
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER lifecycle_mail_revisions_no_update BEFORE UPDATE ON lifecycle_mail_revisions BEGIN SELECT RAISE(ABORT,'mail revisions are append-only'); END;
CREATE TRIGGER lifecycle_mail_revisions_no_delete BEFORE DELETE ON lifecycle_mail_revisions BEGIN SELECT RAISE(ABORT,'mail revisions are append-only'); END;
CREATE TABLE lifecycle_mail_direction_decisions (
 decision_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES lifecycle_mail_observations(observation_id),
 direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')), reason TEXT NOT NULL,
 actor_kind TEXT NOT NULL CHECK(actor_kind='user'), source_at TEXT NOT NULL, decided_at TEXT NOT NULL
);
CREATE TRIGGER lifecycle_mail_direction_no_update BEFORE UPDATE ON lifecycle_mail_direction_decisions BEGIN SELECT RAISE(ABORT,'direction decisions are append-only'); END;
CREATE TRIGGER lifecycle_mail_direction_no_delete BEFORE DELETE ON lifecycle_mail_direction_decisions BEGIN SELECT RAISE(ABORT,'direction decisions are append-only'); END;
CREATE TABLE lifecycle_mail_links (
 observation_id TEXT NOT NULL REFERENCES lifecycle_mail_observations(observation_id),
 application_id TEXT NOT NULL REFERENCES applications(application_id), confidence REAL NOT NULL,
 source TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(observation_id,application_id)
);
CREATE TABLE lifecycle_mail_link_history (
 change_id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL, from_application_id TEXT,
 to_application_id TEXT, actor_kind TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE lifecycle_discoveries (
 discovery_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL UNIQUE REFERENCES lifecycle_mail_observations(observation_id),
 status TEXT NOT NULL CHECK(status IN ('pending','linked','created','dismissed')),
 employer TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
 application_id TEXT REFERENCES applications(application_id), reviewed_by TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE lifecycle_mail_replay_decisions (
 decision_id TEXT PRIMARY KEY, replay_id TEXT NOT NULL, operation TEXT NOT NULL,
 actor_kind TEXT NOT NULL CHECK(actor_kind='user'), checkpoint INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER lifecycle_replay_decisions_no_update BEFORE UPDATE ON lifecycle_mail_replay_decisions BEGIN SELECT RAISE(ABORT,'replay decisions are append-only'); END;
CREATE TRIGGER lifecycle_replay_decisions_no_delete BEFORE DELETE ON lifecycle_mail_replay_decisions BEGIN SELECT RAISE(ABORT,'replay decisions are append-only'); END;
CREATE TABLE lifecycle_mail_replays (
 replay_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, since_at TEXT NOT NULL, until_at TEXT NOT NULL,
 query_version INTEGER NOT NULL, after_stage_rowid INTEGER NOT NULL DEFAULT 0, max_stage_rowid INTEGER NOT NULL,
 processed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
 last_error TEXT NOT NULL DEFAULT '', failure_count INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
'''
