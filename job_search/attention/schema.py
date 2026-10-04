"""Versioned notification attention decisions, briefings, and source provenance."""
SCHEMA = r"""
CREATE TABLE attention_preferences (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
 values_json TEXT NOT NULL, activated_at TEXT, baseline_seq INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE attention_preference_history (
 revision INTEGER PRIMARY KEY, values_json TEXT NOT NULL, actor_kind TEXT NOT NULL,
 source_ref TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE attention_candidates (
 candidate_seq INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE,
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
 group_key TEXT NOT NULL, application_id TEXT REFERENCES applications(application_id),
 topic TEXT NOT NULL, source_at TEXT NOT NULL, observed_at TEXT NOT NULL,
 due_at TEXT, expires_at TEXT, payload_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','acknowledged','snoozed','resolved')),
 snoozed_until TEXT, revision_no INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(source_kind,source_id,source_revision)
);
CREATE INDEX attention_candidates_due ON attention_candidates(status,due_at,candidate_seq);
CREATE TRIGGER attention_candidate_source_immutable BEFORE UPDATE ON attention_candidates
WHEN OLD.candidate_id IS NOT NEW.candidate_id OR OLD.candidate_seq IS NOT NEW.candidate_seq
 OR OLD.source_kind IS NOT NEW.source_kind OR OLD.source_id IS NOT NEW.source_id
 OR OLD.source_revision IS NOT NEW.source_revision OR OLD.group_key IS NOT NEW.group_key
 OR OLD.application_id IS NOT NEW.application_id OR OLD.topic IS NOT NEW.topic
 OR OLD.source_at IS NOT NEW.source_at OR OLD.observed_at IS NOT NEW.observed_at
 OR OLD.due_at IS NOT NEW.due_at OR OLD.expires_at IS NOT NEW.expires_at
 OR OLD.payload_json IS NOT NEW.payload_json OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT,'attention source is immutable'); END;
CREATE TABLE attention_decisions (
 decision_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES attention_candidates(candidate_id),
 preference_revision INTEGER NOT NULL, route TEXT NOT NULL CHECK(route IN ('urgent','briefing','defer','suppress')),
 reason TEXT NOT NULL, relevance_json TEXT NOT NULL, decision_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL
);
CREATE TABLE attention_interactions (
 interaction_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES attention_candidates(candidate_id),
 operation TEXT NOT NULL, until_at TEXT, actor_kind TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE attention_alerts (
 alert_id TEXT PRIMARY KEY, group_key TEXT NOT NULL, alert_kind TEXT NOT NULL,
 candidate_id TEXT NOT NULL REFERENCES attention_candidates(candidate_id),
 decision_id TEXT NOT NULL REFERENCES attention_decisions(decision_id),
 notification_id TEXT UNIQUE REFERENCES notification_outbox(notification_id),
 created_at TEXT NOT NULL, UNIQUE(group_key,alert_kind)
);
CREATE TABLE attention_briefings (
 briefing_id TEXT PRIMARY KEY, slot TEXT NOT NULL CHECK(slot IN ('morning','evening')),
 local_date TEXT NOT NULL, timezone TEXT NOT NULL, preference_revision INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','finalized','expired')),
 scheduled_for TEXT NOT NULL, expires_at TEXT NOT NULL, generation_deadline TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, selected_refs_json TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, renderer TEXT NOT NULL,
 generation_json TEXT, notification_id TEXT UNIQUE REFERENCES notification_outbox(notification_id),
 created_at TEXT NOT NULL, finalized_at TEXT, UNIQUE(slot,local_date,timezone)
);
CREATE TABLE attention_briefing_items (
 briefing_id TEXT NOT NULL REFERENCES attention_briefings(briefing_id),
 fact_ref TEXT NOT NULL, source_revision TEXT NOT NULL,
 PRIMARY KEY(briefing_id,fact_ref)
);
CREATE TABLE attention_generation_results (
 result_id TEXT PRIMARY KEY, briefing_id TEXT NOT NULL REFERENCES attention_briefings(briefing_id),
 outcome TEXT NOT NULL, result_sha256 TEXT NOT NULL, provenance_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
ALTER TABLE notification_outbox ADD COLUMN attention_decision_id TEXT REFERENCES attention_decisions(decision_id);
ALTER TABLE notification_outbox ADD COLUMN briefing_id TEXT REFERENCES attention_briefings(briefing_id);
ALTER TABLE notification_outbox ADD COLUMN expires_at TEXT;
ALTER TABLE notification_outbox ADD COLUMN activation_revision INTEGER;
CREATE INDEX notification_attention_source ON notification_outbox(attention_decision_id,briefing_id);
CREATE TRIGGER notification_attention_provenance_immutable BEFORE UPDATE ON notification_outbox
WHEN OLD.attention_decision_id IS NOT NEW.attention_decision_id OR OLD.briefing_id IS NOT NEW.briefing_id
 OR OLD.expires_at IS NOT NEW.expires_at OR OLD.activation_revision IS NOT NEW.activation_revision
BEGIN SELECT RAISE(ABORT,'notification attention provenance is immutable'); END;
"""
for _table in ('attention_preference_history','attention_decisions','attention_interactions','attention_generation_results','attention_briefing_items'):
    for _op in ('UPDATE','DELETE'):
        SCHEMA += f"CREATE TRIGGER {_table}_no_{_op.lower()} BEFORE {_op} ON {_table} BEGIN SELECT RAISE(ABORT,'attention audit is append-only'); END;\n"
SCHEMA += "INSERT OR IGNORE INTO automation_controls VALUES ('briefing_ai',0,0,'1970-01-01T00:00:00Z');\nPRAGMA user_version = 17;\n"
