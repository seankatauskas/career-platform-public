"""Lifecycle migration fragment; finalized before migration 15 ships."""
SCHEMA = r"""
CREATE TABLE interview_rounds (
 round_id TEXT PRIMARY KEY,
 application_id TEXT NOT NULL REFERENCES applications(application_id),
 round_kind TEXT NOT NULL DEFAULT 'interview',
 status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','rescheduled','cancelled','completed')),
 starts_at TEXT NOT NULL DEFAULT '', ends_at TEXT NOT NULL DEFAULT '',
 time_zone TEXT NOT NULL DEFAULT 'UTC', details_json TEXT NOT NULL DEFAULT '{}',
 calendar_account_id TEXT NOT NULL DEFAULT '', calendar_event_id TEXT NOT NULL DEFAULT '',
 calendar_uid TEXT NOT NULL DEFAULT '', calendar_modified_at TEXT NOT NULL DEFAULT '',
 calendar_change_key TEXT NOT NULL DEFAULT '', current_revision_id TEXT,
 legacy_schedule_id TEXT UNIQUE REFERENCES accepted_interview_schedules(interview_schedule_id),
 task_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX interview_calendar_identity ON interview_rounds(calendar_account_id,calendar_event_id)
 WHERE calendar_account_id<>'' AND calendar_event_id<>'';
CREATE INDEX interview_rounds_upcoming ON interview_rounds(status,starts_at,round_id);
CREATE TABLE interview_revisions (
 revision_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES interview_rounds(round_id),
 application_id TEXT NOT NULL REFERENCES applications(application_id),
 dedupe_key TEXT NOT NULL UNIQUE, details_json TEXT NOT NULL, base_revision_id TEXT,
 base_round_status TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','stale','conflict')),
 source_kind TEXT NOT NULL, source_ref TEXT NOT NULL DEFAULT '', source_at TEXT NOT NULL,
 created_at TEXT NOT NULL, decided_at TEXT, decision_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE interview_revision_decisions (
 decision_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES interview_revisions(revision_id),
 decision TEXT NOT NULL, resulting_status TEXT NOT NULL, actor_kind TEXT NOT NULL,
 reason TEXT NOT NULL, availability_json TEXT NOT NULL, decided_at TEXT NOT NULL
);
CREATE INDEX interview_revisions_review ON interview_revisions(status,created_at);
CREATE TRIGGER interview_revision_input_immutable BEFORE UPDATE ON interview_revisions
WHEN OLD.revision_id IS NOT NEW.revision_id OR OLD.round_id IS NOT NEW.round_id
 OR OLD.application_id IS NOT NEW.application_id OR OLD.dedupe_key IS NOT NEW.dedupe_key
 OR OLD.details_json IS NOT NEW.details_json OR OLD.base_revision_id IS NOT NEW.base_revision_id
 OR OLD.base_round_status IS NOT NEW.base_round_status
 OR OLD.source_kind IS NOT NEW.source_kind OR OLD.source_ref IS NOT NEW.source_ref
 OR OLD.source_at IS NOT NEW.source_at OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT,'interview revision input is immutable'); END;
CREATE TRIGGER interview_decisions_no_update BEFORE UPDATE ON interview_revision_decisions
BEGIN SELECT RAISE(ABORT,'interview decisions are append-only'); END;
CREATE TRIGGER interview_decisions_no_delete BEFORE DELETE ON interview_revision_decisions
BEGIN SELECT RAISE(ABORT,'interview decisions are append-only'); END;
CREATE TABLE interview_reminders (
 reminder_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES interview_rounds(round_id),
 revision_id TEXT NOT NULL REFERENCES interview_revisions(revision_id),
 application_id TEXT NOT NULL REFERENCES applications(application_id),
 kind TEXT NOT NULL CHECK(kind IN ('interview_24h','interview_1h')),
 due_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','completed','dismissed')),
 created_at TEXT NOT NULL, completed_at TEXT, UNIQUE(revision_id,kind)
);
CREATE INDEX interview_reminders_due ON interview_reminders(status,due_at);
"""
