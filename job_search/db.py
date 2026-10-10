"""SQLite initialization and ordered migrations for the private job-search ledger."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Iterable, Tuple


MIGRATION_001 = r"""
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);

CREATE TABLE command_results (
    command_name     TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    request_sha256   TEXT NOT NULL,
    response_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (command_name, idempotency_key)
);

CREATE TABLE applications (
    application_id                    TEXT PRIMARY KEY,
    ats                               TEXT NOT NULL,
    job_id                            TEXT NOT NULL,
    family_id                         TEXT NOT NULL DEFAULT '',
    title_snapshot                    TEXT NOT NULL,
    employer_snapshot                 TEXT NOT NULL,
    company_slug_snapshot             TEXT NOT NULL DEFAULT '',
    job_url_snapshot                  TEXT NOT NULL,
    recommendation_session_id         TEXT NOT NULL DEFAULT '',
    recommendation_impression_id      INTEGER,
    recommendation_model_run_id       TEXT NOT NULL DEFAULT '',
    recommendation_policy_id          TEXT NOT NULL DEFAULT '',
    recommendation_rank               INTEGER,
    semantic_score                    REAL,
    ranking_score                     REAL,
    current_phase                     TEXT NOT NULL CHECK (
        current_phase IN (
            'preparing', 'awaiting_confirmation', 'active',
            'interviewing', 'offer', 'terminal'
        )
    ),
    terminal_outcome                  TEXT CHECK (
        terminal_outcome IS NULL OR
        terminal_outcome IN ('accepted', 'rejected', 'withdrawn')
    ),
    started_at                        TEXT NOT NULL,
    submitted_at                      TEXT,
    confirmed_at                      TEXT,
    last_activity_at                  TEXT NOT NULL,
    last_event_seq                    INTEGER NOT NULL,
    projection_sha256                 TEXT NOT NULL,
    updated_at                        TEXT NOT NULL,
    UNIQUE (ats, job_id)
);
CREATE INDEX applications_phase
ON applications(current_phase, last_activity_at);

CREATE TABLE application_events (
    event_seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    application_id  TEXT NOT NULL,
    event_type      TEXT NOT NULL CHECK (
        event_type IN (
            'application_started', 'submission_observed',
            'submission_confirmed', 'recruiter_contact',
            'assessment_requested', 'assessment_completed',
            'interview_requested', 'interview_scheduled',
            'interview_completed', 'offer_received', 'offer_accepted',
            'rejection_received', 'withdrawn', 'manual_correction'
        )
    ),
    occurred_at     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    actor_kind      TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    source_ref      TEXT NOT NULL DEFAULT '',
    dedupe_key      TEXT NOT NULL UNIQUE,
    schema_version  INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(application_id)
);
CREATE INDEX application_events_timeline
ON application_events(application_id, event_seq);

CREATE TRIGGER application_events_no_update
BEFORE UPDATE ON application_events
BEGIN
    SELECT RAISE(ABORT, 'application events are append-only');
END;

CREATE TRIGGER application_events_no_delete
BEFORE DELETE ON application_events
BEGIN
    SELECT RAISE(ABORT, 'application events are append-only');
END;

CREATE TRIGGER applications_roots_are_immutable
BEFORE UPDATE ON applications
WHEN OLD.application_id IS NOT NEW.application_id
  OR OLD.ats IS NOT NEW.ats
  OR OLD.job_id IS NOT NEW.job_id
  OR OLD.family_id IS NOT NEW.family_id
  OR OLD.title_snapshot IS NOT NEW.title_snapshot
  OR OLD.employer_snapshot IS NOT NEW.employer_snapshot
  OR OLD.company_slug_snapshot IS NOT NEW.company_slug_snapshot
  OR OLD.job_url_snapshot IS NOT NEW.job_url_snapshot
  OR OLD.recommendation_session_id IS NOT NEW.recommendation_session_id
  OR OLD.recommendation_impression_id IS NOT NEW.recommendation_impression_id
  OR OLD.recommendation_model_run_id IS NOT NEW.recommendation_model_run_id
  OR OLD.recommendation_policy_id IS NOT NEW.recommendation_policy_id
  OR OLD.recommendation_rank IS NOT NEW.recommendation_rank
  OR OLD.semantic_score IS NOT NEW.semantic_score
  OR OLD.ranking_score IS NOT NEW.ranking_score
BEGIN
    SELECT RAISE(ABORT, 'application root and provenance are immutable');
END;

CREATE TABLE mail_evidence (
    evidence_id           TEXT PRIMARY KEY,
    account_id            TEXT NOT NULL,
    immutable_message_id  TEXT NOT NULL,
    conversation_id       TEXT NOT NULL DEFAULT '',
    sender                TEXT NOT NULL,
    subject               TEXT NOT NULL,
    received_at           TEXT NOT NULL,
    body_sha256           TEXT NOT NULL,
    excerpt               TEXT NOT NULL CHECK (length(excerpt) <= 2048),
    created_at            TEXT NOT NULL,
    UNIQUE (account_id, immutable_message_id)
);

CREATE TABLE event_proposals (
    proposal_id                    TEXT PRIMARY KEY,
    dedupe_key                     TEXT NOT NULL UNIQUE,
    evidence_id                    TEXT NOT NULL,
    proposed_application_id        TEXT,
    event_type                     TEXT NOT NULL,
    producer_kind                  TEXT NOT NULL CHECK (producer_kind IN ('rule', 'model')),
    producer_version               TEXT NOT NULL,
    confidence                     REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    candidate_application_ids_json TEXT NOT NULL,
    evidence_quote                 TEXT NOT NULL,
    span_start                     INTEGER,
    span_end                       INTEGER,
    payload_json                   TEXT NOT NULL,
    verification_json              TEXT NOT NULL DEFAULT '{}',
    automation_policy_id           TEXT NOT NULL DEFAULT '',
    status                         TEXT NOT NULL CHECK (
        status IN (
            'pending', 'auto_applied', 'accepted', 'rejected',
            'superseded', 'conflict'
        )
    ),
    applied_event_id               TEXT,
    created_at                     TEXT NOT NULL,
    decided_at                     TEXT,
    FOREIGN KEY (proposed_application_id) REFERENCES applications(application_id),
    FOREIGN KEY (applied_event_id) REFERENCES application_events(event_id)
);
CREATE INDEX event_proposals_review
ON event_proposals(status, created_at);

CREATE TRIGGER event_proposals_payload_is_immutable
BEFORE UPDATE ON event_proposals
WHEN OLD.dedupe_key IS NOT NEW.dedupe_key
  OR OLD.evidence_id IS NOT NEW.evidence_id
  OR OLD.proposed_application_id IS NOT NEW.proposed_application_id
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.producer_kind IS NOT NEW.producer_kind
  OR OLD.producer_version IS NOT NEW.producer_version
  OR OLD.confidence IS NOT NEW.confidence
  OR OLD.candidate_application_ids_json IS NOT NEW.candidate_application_ids_json
  OR OLD.evidence_quote IS NOT NEW.evidence_quote
  OR OLD.span_start IS NOT NEW.span_start
  OR OLD.span_end IS NOT NEW.span_end
  OR OLD.payload_json IS NOT NEW.payload_json
BEGIN
    SELECT RAISE(ABORT, 'event proposal input is immutable');
END;

CREATE TABLE event_proposal_decisions (
    decision_id              TEXT PRIMARY KEY,
    proposal_id              TEXT NOT NULL UNIQUE,
    decision                 TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    selected_application_id  TEXT,
    actor_kind               TEXT NOT NULL,
    reason                   TEXT NOT NULL DEFAULT '',
    decided_at               TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES event_proposals(proposal_id),
    FOREIGN KEY (selected_application_id) REFERENCES applications(application_id)
);

CREATE TABLE classifier_automation_policies (
    policy_id                  TEXT PRIMARY KEY,
    event_type                 TEXT NOT NULL,
    producer_version           TEXT NOT NULL,
    threshold                  REAL NOT NULL CHECK (threshold BETWEEN 0 AND 1),
    example_count              INTEGER NOT NULL,
    observed_precision         REAL NOT NULL CHECK (observed_precision BETWEEN 0 AND 1),
    wrong_application_matches  INTEGER NOT NULL,
    evaluation_sha256          TEXT NOT NULL,
    enabled                    INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_at                 TEXT NOT NULL
);

CREATE TABLE action_proposals (
    action_id               TEXT PRIMARY KEY,
    application_id          TEXT,
    account_id              TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (
        kind IN ('outlook_reply_draft', 'calendar_tentative_hold')
    ),
    payload_json            TEXT NOT NULL,
    payload_sha256          TEXT NOT NULL,
    remote_idempotency_key  TEXT NOT NULL UNIQUE,
    status                  TEXT NOT NULL CHECK (
        status IN (
            'pending', 'approved', 'rejected', 'executing', 'executed',
            'failed', 'needs_reconciliation', 'superseded'
        )
    ),
    expires_at              TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(application_id)
);
CREATE INDEX action_proposals_review
ON action_proposals(status, created_at);

CREATE TRIGGER action_proposals_payload_is_immutable
BEFORE UPDATE ON action_proposals
WHEN OLD.application_id IS NOT NEW.application_id
  OR OLD.account_id IS NOT NEW.account_id
  OR OLD.kind IS NOT NEW.kind
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.payload_sha256 IS NOT NEW.payload_sha256
  OR OLD.remote_idempotency_key IS NOT NEW.remote_idempotency_key
  OR OLD.expires_at IS NOT NEW.expires_at
BEGIN
    SELECT RAISE(ABORT, 'action proposal payload is immutable');
END;

CREATE TABLE action_approval_decisions (
    decision_seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     TEXT NOT NULL UNIQUE,
    action_id       TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'revoke')),
    payload_sha256  TEXT NOT NULL,
    actor_kind      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    FOREIGN KEY (action_id) REFERENCES action_proposals(action_id)
);
CREATE INDEX action_approval_decisions_action
ON action_approval_decisions(action_id, decision_seq);

CREATE TABLE action_executions (
    execution_id          TEXT PRIMARY KEY,
    action_id             TEXT NOT NULL,
    approval_decision_id  TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    status                TEXT NOT NULL CHECK (
        status IN (
            'claimed', 'succeeded', 'retryable_failure',
            'permanent_failure', 'uncertain'
        )
    ),
    remote_id             TEXT,
    started_at            TEXT NOT NULL,
    completed_at          TEXT,
    error                 TEXT NOT NULL DEFAULT '',
    UNIQUE (action_id, attempt),
    FOREIGN KEY (action_id) REFERENCES action_proposals(action_id),
    FOREIGN KEY (approval_decision_id) REFERENCES action_approval_decisions(decision_id)
);

CREATE TABLE outbox_messages (
    outbox_id          TEXT PRIMARY KEY,
    topic              TEXT NOT NULL,
    source_event_id    TEXT NOT NULL UNIQUE,
    dedupe_key         TEXT NOT NULL UNIQUE,
    payload_json       TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (
        status IN ('pending', 'delivering', 'delivered', 'dead')
    ),
    attempts           INTEGER NOT NULL DEFAULT 0,
    available_at       TEXT NOT NULL,
    lease_owner        TEXT,
    lease_token        TEXT,
    lease_expires_at   TEXT,
    last_error         TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    delivered_at       TEXT,
    FOREIGN KEY (source_event_id) REFERENCES application_events(event_id)
);
CREATE INDEX outbox_messages_due
ON outbox_messages(status, available_at, created_at);

CREATE TABLE schedule_specs (
    schedule_key   TEXT PRIMARY KEY,
    task_kind      TEXT NOT NULL,
    schedule_json  TEXT NOT NULL,
    enabled        INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    coalesce       INTEGER NOT NULL CHECK (coalesce IN (0, 1)),
    next_due_at    TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE work_items (
    work_id           TEXT PRIMARY KEY,
    schedule_key      TEXT,
    task_kind         TEXT NOT NULL,
    dedupe_key        TEXT NOT NULL UNIQUE,
    payload_json      TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'dead', 'cancelled')
    ),
    priority          INTEGER NOT NULL DEFAULT 0,
    due_at            TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL,
    lease_owner       TEXT,
    lease_token       TEXT,
    lease_expires_at  TEXT,
    last_error        TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    completed_at      TEXT,
    FOREIGN KEY (schedule_key) REFERENCES schedule_specs(schedule_key)
);
CREATE INDEX work_items_due
ON work_items(status, due_at, priority DESC, created_at);

CREATE TABLE worker_leases (
    lease_name    TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    token         TEXT NOT NULL,
    acquired_at   TEXT NOT NULL,
    heartbeat_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

CREATE TABLE job_runs (
    run_id         TEXT PRIMARY KEY,
    work_id        TEXT NOT NULL UNIQUE,
    scheduled_for  TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    outcome        TEXT CHECK (
        outcome IS NULL OR outcome IN ('succeeded', 'failed', 'dead')
    ),
    result_json    TEXT NOT NULL DEFAULT '{}',
    error          TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (work_id) REFERENCES work_items(work_id)
);

PRAGMA user_version = 1;
"""


MIGRATION_002 = r"""
CREATE TABLE outlook_sync_cursors (
    account_id             TEXT NOT NULL,
    folder_ref             TEXT NOT NULL,
    query_version          INTEGER NOT NULL,
    committed_delta_link   TEXT,
    in_flight_next_link    TEXT,
    revision               INTEGER NOT NULL DEFAULT 0,
    needs_backfill         INTEGER NOT NULL DEFAULT 1 CHECK (needs_backfill IN (0, 1)),
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (account_id, folder_ref, query_version)
);

CREATE TABLE outlook_message_stage (
    account_id            TEXT NOT NULL,
    folder_ref            TEXT NOT NULL,
    immutable_message_id  TEXT NOT NULL,
    conversation_id       TEXT NOT NULL DEFAULT '',
    internet_message_id   TEXT NOT NULL DEFAULT '',
    sender                TEXT NOT NULL DEFAULT '',
    subject               TEXT NOT NULL DEFAULT '',
    received_at           TEXT,
    modified_at           TEXT,
    web_link              TEXT NOT NULL DEFAULT '',
    removed               INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
    processing_status     TEXT NOT NULL DEFAULT 'pending' CHECK (
        processing_status IN ('pending', 'ignored', 'processed', 'failed')
    ),
    last_error            TEXT NOT NULL DEFAULT '',
    first_seen_at         TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (account_id, folder_ref, immutable_message_id)
);
CREATE INDEX outlook_message_stage_pending
ON outlook_message_stage(processing_status, removed, received_at);

CREATE TABLE connector_health (
    connector_key    TEXT PRIMARY KEY,
    status           TEXT NOT NULL CHECK (
        status IN ('healthy', 'degraded', 'reauth_required', 'failed', 'disabled')
    ),
    detail           TEXT NOT NULL DEFAULT '',
    last_attempt_at  TEXT,
    last_success_at  TEXT,
    next_attempt_at  TEXT,
    updated_at       TEXT NOT NULL
);

PRAGMA user_version = 2;
"""


MIGRATION_003 = r"""
DROP TRIGGER event_proposals_payload_is_immutable;
DROP INDEX event_proposals_review;
ALTER TABLE event_proposal_decisions RENAME TO event_proposal_decisions_v2;
ALTER TABLE event_proposals RENAME TO event_proposals_v2;

CREATE TABLE event_proposals (
    proposal_id                    TEXT PRIMARY KEY,
    dedupe_key                     TEXT NOT NULL UNIQUE,
    evidence_id                    TEXT NOT NULL,
    proposed_application_id        TEXT,
    event_type                     TEXT NOT NULL,
    producer_kind                  TEXT NOT NULL CHECK (producer_kind IN ('rule', 'model')),
    producer_version               TEXT NOT NULL,
    confidence                     REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    candidate_application_ids_json TEXT NOT NULL,
    evidence_quote                 TEXT NOT NULL,
    span_start                     INTEGER,
    span_end                       INTEGER,
    payload_json                   TEXT NOT NULL,
    verification_json              TEXT NOT NULL DEFAULT '{}',
    automation_policy_id           TEXT NOT NULL DEFAULT '',
    status                         TEXT NOT NULL CHECK (
        status IN (
            'pending', 'auto_applied', 'accepted', 'rejected',
            'superseded', 'conflict'
        )
    ),
    applied_event_id               TEXT,
    created_at                     TEXT NOT NULL,
    decided_at                     TEXT,
    FOREIGN KEY (evidence_id) REFERENCES mail_evidence(evidence_id),
    FOREIGN KEY (proposed_application_id) REFERENCES applications(application_id),
    FOREIGN KEY (applied_event_id) REFERENCES application_events(event_id)
);
CREATE INDEX event_proposals_review
ON event_proposals(status, created_at);

CREATE TRIGGER event_proposals_payload_is_immutable
BEFORE UPDATE ON event_proposals
WHEN OLD.dedupe_key IS NOT NEW.dedupe_key
  OR OLD.evidence_id IS NOT NEW.evidence_id
  OR OLD.proposed_application_id IS NOT NEW.proposed_application_id
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.producer_kind IS NOT NEW.producer_kind
  OR OLD.producer_version IS NOT NEW.producer_version
  OR OLD.confidence IS NOT NEW.confidence
  OR OLD.candidate_application_ids_json IS NOT NEW.candidate_application_ids_json
  OR OLD.evidence_quote IS NOT NEW.evidence_quote
  OR OLD.span_start IS NOT NEW.span_start
  OR OLD.span_end IS NOT NEW.span_end
  OR OLD.payload_json IS NOT NEW.payload_json
BEGIN
    SELECT RAISE(ABORT, 'event proposal input is immutable');
END;

INSERT INTO event_proposals (
    proposal_id,dedupe_key,evidence_id,proposed_application_id,event_type,
    producer_kind,producer_version,confidence,candidate_application_ids_json,
    evidence_quote,span_start,span_end,payload_json,verification_json,
    automation_policy_id,status,applied_event_id,created_at,decided_at
)
SELECT
    proposal_id,dedupe_key,evidence_id,proposed_application_id,event_type,
    producer_kind,producer_version,confidence,candidate_application_ids_json,
    evidence_quote,span_start,span_end,payload_json,verification_json,
    automation_policy_id,status,applied_event_id,created_at,decided_at
FROM event_proposals_v2;

CREATE TABLE event_proposal_decisions (
    decision_id              TEXT PRIMARY KEY,
    proposal_id              TEXT NOT NULL UNIQUE,
    decision                 TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    selected_application_id  TEXT,
    actor_kind               TEXT NOT NULL,
    reason                   TEXT NOT NULL DEFAULT '',
    decided_at               TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES event_proposals(proposal_id),
    FOREIGN KEY (selected_application_id) REFERENCES applications(application_id)
);
INSERT INTO event_proposal_decisions (
    decision_id,proposal_id,decision,selected_application_id,actor_kind,reason,decided_at
)
SELECT
    decision_id,proposal_id,decision,selected_application_id,actor_kind,reason,decided_at
FROM event_proposal_decisions_v2;
DROP TABLE event_proposal_decisions_v2;
DROP TABLE event_proposals_v2;

DROP INDEX outlook_message_stage_pending;
ALTER TABLE outlook_message_stage RENAME TO outlook_message_stage_v2;
CREATE TABLE outlook_message_stage (
    account_id            TEXT NOT NULL,
    folder_ref            TEXT NOT NULL,
    query_version         INTEGER NOT NULL CHECK (query_version >= 1),
    immutable_message_id  TEXT NOT NULL,
    conversation_id       TEXT NOT NULL DEFAULT '',
    internet_message_id   TEXT NOT NULL DEFAULT '',
    sender                TEXT NOT NULL DEFAULT '',
    subject               TEXT NOT NULL DEFAULT '',
    received_at           TEXT,
    modified_at           TEXT,
    web_link              TEXT NOT NULL DEFAULT '',
    removed               INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
    processing_status     TEXT NOT NULL DEFAULT 'pending' CHECK (
        processing_status IN ('pending', 'ignored', 'processed', 'failed')
    ),
    last_error            TEXT NOT NULL DEFAULT '',
    first_seen_at         TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (account_id, folder_ref, query_version, immutable_message_id)
);
INSERT INTO outlook_message_stage (
    account_id,folder_ref,query_version,immutable_message_id,conversation_id,
    internet_message_id,sender,subject,received_at,modified_at,web_link,removed,
    processing_status,last_error,first_seen_at,updated_at
)
SELECT
    account_id,folder_ref,1,immutable_message_id,conversation_id,
    internet_message_id,sender,subject,received_at,modified_at,web_link,removed,
    processing_status,last_error,first_seen_at,updated_at
FROM outlook_message_stage_v2;
DROP TABLE outlook_message_stage_v2;
CREATE INDEX outlook_message_stage_pending
ON outlook_message_stage(query_version, processing_status, removed, received_at);

PRAGMA user_version = 3;
"""


MIGRATION_004 = r"""
CREATE TABLE outlook_folder_inventory (
    account_id         TEXT NOT NULL,
    folder_id          TEXT NOT NULL,
    parent_folder_id   TEXT NOT NULL DEFAULT '',
    is_hidden          INTEGER NOT NULL DEFAULT 0 CHECK (is_hidden IN (0, 1)),
    is_excluded        INTEGER NOT NULL DEFAULT 0 CHECK (is_excluded IN (0, 1)),
    exclusion_reason   TEXT NOT NULL DEFAULT '',
    active             INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    discovery_revision INTEGER NOT NULL,
    first_seen_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (account_id, folder_id)
);
CREATE INDEX outlook_folder_inventory_sync
ON outlook_folder_inventory(account_id, active, is_excluded, folder_id);

CREATE TABLE mail_archive (
    archive_id             TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL,
    immutable_message_id   TEXT NOT NULL,
    key_id                 TEXT NOT NULL,
    nonce                  BLOB NOT NULL CHECK (length(nonce) = 12),
    ciphertext             BLOB NOT NULL CHECK (length(ciphertext) >= 16),
    aad_sha256             TEXT NOT NULL CHECK (length(aad_sha256) = 64),
    sanitized_sha256       TEXT NOT NULL CHECK (length(sanitized_sha256) = 64),
    sanitized_chars        INTEGER NOT NULL CHECK (sanitized_chars >= 0),
    truncated              INTEGER NOT NULL CHECK (truncated IN (0, 1)),
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (account_id, immutable_message_id)
);

CREATE TABLE mail_archive_attachments (
    attachment_record_id     TEXT PRIMARY KEY,
    archive_id               TEXT NOT NULL,
    immutable_attachment_id  TEXT NOT NULL,
    mime_type                TEXT NOT NULL CHECK (
        mime_type IN (
            'application/pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'text/calendar'
        )
    ),
    source_size               INTEGER NOT NULL CHECK (source_size > 0),
    source_sha256             TEXT NOT NULL CHECK (length(source_sha256) = 64),
    extracted_sha256          TEXT NOT NULL CHECK (length(extracted_sha256) = 64),
    extracted_chars           INTEGER NOT NULL CHECK (extracted_chars >= 0),
    key_id                    TEXT NOT NULL,
    nonce                     BLOB NOT NULL CHECK (length(nonce) = 12),
    ciphertext                BLOB NOT NULL CHECK (length(ciphertext) >= 16),
    aad_sha256                TEXT NOT NULL CHECK (length(aad_sha256) = 64),
    created_at                TEXT NOT NULL,
    FOREIGN KEY (archive_id) REFERENCES mail_archive(archive_id),
    UNIQUE (archive_id, immutable_attachment_id)
);

CREATE TABLE temporal_proposals (
    temporal_proposal_id  TEXT PRIMARY KEY,
    dedupe_key            TEXT NOT NULL UNIQUE,
    archive_id            TEXT NOT NULL,
    attachment_record_id  TEXT,
    application_id        TEXT NOT NULL,
    kind                  TEXT NOT NULL CHECK (kind IN ('interview', 'deadline')),
    starts_at             TEXT,
    ends_at               TEXT,
    due_at                TEXT,
    time_zone             TEXT NOT NULL,
    confidence            REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    evidence_quote        TEXT NOT NULL CHECK (length(evidence_quote) BETWEEN 1 AND 512),
    span_start            INTEGER NOT NULL CHECK (span_start >= 0),
    span_end              INTEGER NOT NULL CHECK (span_end > span_start),
    source_sha256         TEXT NOT NULL CHECK (length(source_sha256) = 64),
    producer_version      TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'accepted', 'rejected', 'conflict')
    ),
    created_at            TEXT NOT NULL,
    decided_at            TEXT,
    FOREIGN KEY (archive_id) REFERENCES mail_archive(archive_id),
    FOREIGN KEY (attachment_record_id)
        REFERENCES mail_archive_attachments(attachment_record_id),
    FOREIGN KEY (application_id) REFERENCES applications(application_id),
    CHECK (
        (kind = 'interview' AND starts_at IS NOT NULL AND ends_at IS NOT NULL
            AND due_at IS NULL)
        OR
        (kind = 'deadline' AND starts_at IS NULL AND ends_at IS NULL
            AND due_at IS NOT NULL)
    )
);
CREATE INDEX temporal_proposals_review
ON temporal_proposals(status, created_at);

CREATE TRIGGER temporal_proposals_input_is_immutable
BEFORE UPDATE ON temporal_proposals
WHEN OLD.dedupe_key IS NOT NEW.dedupe_key
  OR OLD.archive_id IS NOT NEW.archive_id
  OR OLD.attachment_record_id IS NOT NEW.attachment_record_id
  OR OLD.application_id IS NOT NEW.application_id
  OR OLD.kind IS NOT NEW.kind
  OR OLD.starts_at IS NOT NEW.starts_at
  OR OLD.ends_at IS NOT NEW.ends_at
  OR OLD.due_at IS NOT NEW.due_at
  OR OLD.time_zone IS NOT NEW.time_zone
  OR OLD.confidence IS NOT NEW.confidence
  OR OLD.evidence_quote IS NOT NEW.evidence_quote
  OR OLD.span_start IS NOT NEW.span_start
  OR OLD.span_end IS NOT NEW.span_end
  OR OLD.source_sha256 IS NOT NEW.source_sha256
  OR OLD.producer_version IS NOT NEW.producer_version
BEGIN
    SELECT RAISE(ABORT, 'temporal proposal input is immutable');
END;

CREATE TABLE temporal_proposal_decisions (
    decision_id           TEXT PRIMARY KEY,
    temporal_proposal_id  TEXT NOT NULL UNIQUE,
    decision              TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    actor_kind            TEXT NOT NULL,
    reason                TEXT NOT NULL DEFAULT '',
    decided_at            TEXT NOT NULL,
    FOREIGN KEY (temporal_proposal_id)
        REFERENCES temporal_proposals(temporal_proposal_id)
);

CREATE TABLE accepted_interview_schedules (
    interview_schedule_id TEXT PRIMARY KEY,
    temporal_proposal_id  TEXT NOT NULL UNIQUE,
    application_id        TEXT NOT NULL,
    starts_at             TEXT NOT NULL,
    ends_at               TEXT NOT NULL,
    time_zone             TEXT NOT NULL,
    application_event_id  TEXT NOT NULL UNIQUE,
    status                TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'cancelled', 'completed')
    ),
    created_at            TEXT NOT NULL,
    FOREIGN KEY (temporal_proposal_id)
        REFERENCES temporal_proposals(temporal_proposal_id),
    FOREIGN KEY (application_id) REFERENCES applications(application_id),
    FOREIGN KEY (application_event_id) REFERENCES application_events(event_id)
);
CREATE INDEX accepted_interview_schedules_upcoming
ON accepted_interview_schedules(status, starts_at);

CREATE TABLE local_reminders (
    reminder_id           TEXT PRIMARY KEY,
    temporal_proposal_id  TEXT NOT NULL,
    interview_schedule_id TEXT,
    application_id        TEXT NOT NULL,
    kind                  TEXT NOT NULL CHECK (
        kind IN ('interview_24h', 'interview_1h', 'deadline')
    ),
    due_at                TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'completed', 'dismissed')
    ),
    created_at            TEXT NOT NULL,
    completed_at          TEXT,
    FOREIGN KEY (temporal_proposal_id)
        REFERENCES temporal_proposals(temporal_proposal_id),
    FOREIGN KEY (interview_schedule_id)
        REFERENCES accepted_interview_schedules(interview_schedule_id),
    FOREIGN KEY (application_id) REFERENCES applications(application_id),
    UNIQUE (temporal_proposal_id, kind)
);
CREATE INDEX local_reminders_due
ON local_reminders(status, due_at);

PRAGMA user_version = 4;
"""


# Migration 4 is reserved for the mail/evidence stream.  Runtime lineage is kept in
# version 5 so the independently developed migrations can be reconciled in order.
MIGRATION_005 = r"""
ALTER TABLE work_items ADD COLUMN lane TEXT NOT NULL DEFAULT 'core'
    CHECK (lane IN ('core', 'model'));
ALTER TABLE work_items ADD COLUMN workflow_id TEXT NOT NULL DEFAULT '';
ALTER TABLE work_items ADD COLUMN parent_work_id TEXT REFERENCES work_items(work_id);

CREATE INDEX work_items_lane_due
ON work_items(lane, status, due_at, priority DESC, created_at);
CREATE INDEX work_items_workflow
ON work_items(workflow_id, created_at, work_id);

CREATE TRIGGER work_items_lineage_is_immutable
BEFORE UPDATE ON work_items
WHEN OLD.lane IS NOT NEW.lane
  OR OLD.workflow_id IS NOT NEW.workflow_id
  OR OLD.parent_work_id IS NOT NEW.parent_work_id
BEGIN
    SELECT RAISE(ABORT, 'work item lineage is immutable');
END;

CREATE TABLE workflow_runs (
    workflow_id       TEXT PRIMARY KEY,
    workflow_kind     TEXT NOT NULL CHECK (workflow_kind IN ('opportunity_refresh')),
    root_work_id      TEXT NOT NULL UNIQUE,
    trigger_task_kind TEXT NOT NULL CHECK (
        trigger_task_kind IN ('ats.authoritative', 'ats.new_only')
    ),
    scheduled_for     TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (
        status IN ('running', 'stable', 'completed', 'failed')
    ),
    started_at        TEXT NOT NULL,
    stable_at         TEXT,
    completed_at      TEXT,
    last_error        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (root_work_id) REFERENCES work_items(work_id)
);
CREATE INDEX workflow_runs_status
ON workflow_runs(status, scheduled_for, workflow_id);

CREATE TABLE workflow_watermarks (
    workflow_id     TEXT NOT NULL,
    watermark_key   TEXT NOT NULL CHECK (
        watermark_key IN (
            'ats_ingested', 'locations_ready', 'recommendations_stable',
            'shortlist_evaluated', 'salary_drained'
        )
    ),
    work_id         TEXT NOT NULL,
    reached_at      TEXT NOT NULL,
    result_sha256   TEXT NOT NULL,
    PRIMARY KEY (workflow_id, watermark_key),
    FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id),
    FOREIGN KEY (work_id) REFERENCES work_items(work_id)
);
CREATE INDEX workflow_watermarks_latest
ON workflow_watermarks(watermark_key, reached_at, workflow_id);

PRAGMA user_version = 5;
"""


# Versions 4 and 5 are owned by the mail archive and runtime integration lanes.
# This migration is intentionally independent so those commits can be integrated in
# version order without either lane needing to rewrite this schema.
MIGRATION_006 = r"""
CREATE TABLE reminders (
    reminder_id       TEXT PRIMARY KEY,
    application_id    TEXT NOT NULL,
    note              TEXT NOT NULL CHECK (
        length(note) BETWEEN 1 AND 500
    ),
    due_at             TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (
        status IN ('scheduled', 'cancelled', 'completed')
    ),
    idempotency_key    TEXT NOT NULL UNIQUE,
    created_at         TEXT NOT NULL,
    cancelled_at       TEXT,
    completed_at       TEXT,
    FOREIGN KEY (application_id) REFERENCES applications(application_id)
);
CREATE INDEX reminders_due
ON reminders(status, due_at, created_at);

CREATE TRIGGER reminders_input_is_immutable
BEFORE UPDATE ON reminders
WHEN OLD.reminder_id IS NOT NEW.reminder_id
  OR OLD.application_id IS NOT NEW.application_id
  OR OLD.note IS NOT NEW.note
  OR OLD.due_at IS NOT NEW.due_at
  OR OLD.idempotency_key IS NOT NEW.idempotency_key
  OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'reminder input is immutable');
END;

CREATE TABLE notification_outbox (
    notification_id   TEXT PRIMARY KEY,
    dedupe_key        TEXT NOT NULL UNIQUE,
    topic             TEXT NOT NULL,
    policy_id         TEXT NOT NULL,
    application_id    TEXT,
    title             TEXT NOT NULL CHECK (
        length(title) BETWEEN 1 AND 200
    ),
    body              TEXT NOT NULL CHECK (
        length(body) BETWEEN 1 AND 2000
    ),
    context_json      TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL CHECK (
        status IN ('pending', 'delivering', 'delivered', 'dead', 'cancelled')
    ),
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
    available_at      TEXT NOT NULL,
    lease_owner       TEXT,
    lease_token       TEXT,
    lease_expires_at  TEXT,
    last_error        TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    delivered_at      TEXT,
    FOREIGN KEY (application_id) REFERENCES applications(application_id)
);
CREATE INDEX notification_outbox_due
ON notification_outbox(status, available_at, created_at);

CREATE TRIGGER notification_outbox_message_is_immutable
BEFORE UPDATE ON notification_outbox
WHEN OLD.notification_id IS NOT NEW.notification_id
  OR OLD.dedupe_key IS NOT NEW.dedupe_key
  OR OLD.topic IS NOT NEW.topic
  OR OLD.policy_id IS NOT NEW.policy_id
  OR OLD.application_id IS NOT NEW.application_id
  OR OLD.title IS NOT NEW.title
  OR OLD.body IS NOT NEW.body
  OR OLD.context_json IS NOT NEW.context_json
  OR OLD.max_attempts IS NOT NEW.max_attempts
  OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'notification message is immutable');
END;

PRAGMA user_version = 6;
"""


MIGRATION_007 = r"""
ALTER TABLE schedule_specs ADD COLUMN enabled_since TEXT;
UPDATE schedule_specs SET enabled_since=updated_at WHERE enabled=1;

ALTER TABLE work_items ADD COLUMN failure_kind TEXT NOT NULL DEFAULT 'legacy_unknown';
ALTER TABLE work_items ADD COLUMN failure_retryable INTEGER
    CHECK (failure_retryable IS NULL OR failure_retryable IN (0, 1));
ALTER TABLE work_items ADD COLUMN external_outcome TEXT NOT NULL DEFAULT 'none'
    CHECK (external_outcome IN ('none', 'in_flight', 'unknown', 'terminal'));
ALTER TABLE work_items ADD COLUMN recovery_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE work_recovery_commands (
    command_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES work_items(work_id),
    expected_revision INTEGER NOT NULL,
    actor_kind TEXT NOT NULL CHECK (actor_kind='user'),
    requested_at TEXT NOT NULL,
    before_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX work_recovery_work ON work_recovery_commands(work_id, requested_at);
CREATE TRIGGER work_recovery_commands_no_update BEFORE UPDATE ON work_recovery_commands
BEGIN SELECT RAISE(ABORT, 'work recovery audit is immutable'); END;
CREATE TRIGGER work_recovery_commands_no_delete BEFORE DELETE ON work_recovery_commands
BEGIN SELECT RAISE(ABORT, 'work recovery audit is immutable'); END;

PRAGMA user_version = 7;
"""


MIGRATION_008 = r"""
CREATE TABLE inference_invocations (
    invocation_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES work_items(work_id),
    request_sha256 TEXT NOT NULL,
    provider_fingerprint TEXT NOT NULL,
    capability TEXT NOT NULL,
    retrieval_kind TEXT NOT NULL DEFAULT 'none' CHECK(retrieval_kind IN ('none','runpod_job')),
    work_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved','submitting','accepted','completed','failed','cancelled','unknown')),
    provider_job_id TEXT NOT NULL DEFAULT '',
    reconciliation_reason TEXT NOT NULL DEFAULT '',
    reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens>=0),
    observed_tokens INTEGER,
    budget_day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(work_id,request_sha256,provider_fingerprint,work_revision)
);
CREATE INDEX inference_work_request ON inference_invocations(work_id,request_sha256,created_at);
CREATE INDEX inference_day_state ON inference_invocations(budget_day,state);
CREATE TABLE inference_usage_policy (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    policy_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE runtime_dependency_snapshot (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    observed_at TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL
);
CREATE TABLE inference_recovery_commands (
    command_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL REFERENCES inference_invocations(invocation_id),
    expected_updated_at TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK(resolution IN ('absent','failed','completed')),
    provider_job_id TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK(actor_kind='user'),
    requested_at TEXT NOT NULL,
    before_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE TRIGGER inference_recovery_no_update BEFORE UPDATE ON inference_recovery_commands
BEGIN SELECT RAISE(ABORT,'inference recovery audit is immutable'); END;
CREATE TRIGGER inference_recovery_no_delete BEFORE DELETE ON inference_recovery_commands
BEGIN SELECT RAISE(ABORT,'inference recovery audit is immutable'); END;
PRAGMA user_version = 8;
"""


MIGRATION_009 = r"""
CREATE TABLE automation_controls (
 capability TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 revision INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE automation_decisions (
 command_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, result_json TEXT NOT NULL,
 actor_kind TEXT NOT NULL CHECK(actor_kind='user'), created_at TEXT NOT NULL
);
CREATE TRIGGER automation_decisions_no_update BEFORE UPDATE ON automation_decisions
BEGIN SELECT RAISE(ABORT,'automation audit is immutable'); END;
CREATE TRIGGER automation_decisions_no_delete BEFORE DELETE ON automation_decisions
BEGIN SELECT RAISE(ABORT,'automation audit is immutable'); END;
CREATE TABLE outlook_activation (account_id TEXT PRIMARY KEY, started_at TEXT NOT NULL);
PRAGMA user_version = 9;
"""


MIGRATION_010 = r"""
CREATE TABLE browser_pairings (
 code_hash TEXT PRIMARY KEY, audience TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE browser_devices (
 device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
 extension_origin TEXT NOT NULL, audience TEXT NOT NULL,
 created_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE browser_jobs (
 ats TEXT NOT NULL, job_id TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(ats,job_id)
);
CREATE TABLE browser_attempts (
 attempt_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES browser_devices(device_id),
 application_id TEXT NOT NULL REFERENCES applications(application_id),
 status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 resume_sha256 TEXT NOT NULL DEFAULT '', resume_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX browser_attempt_application ON browser_attempts(application_id,created_at);
CREATE TABLE browser_observations (
 observation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES browser_attempts(attempt_id),
 kind TEXT NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE browser_finalizations (
 application_id TEXT PRIMARY KEY REFERENCES applications(application_id),
 source_ref TEXT NOT NULL, finalized_at TEXT NOT NULL
);
PRAGMA user_version = 10;
"""


MIGRATION_011 = r"""
CREATE TABLE curated_shortlists (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 list_id TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL,
 window_start TEXT, window_end TEXT,
 idempotency_key TEXT NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL,
 job_count INTEGER NOT NULL CHECK(job_count BETWEEN 0 AND 100)
);
CREATE TABLE curated_shortlist_items (
 list_id TEXT NOT NULL REFERENCES curated_shortlists(list_id),
 rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 100),
 ats TEXT NOT NULL, job_id TEXT NOT NULL,
 explanation TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 PRIMARY KEY(list_id,rank), UNIQUE(list_id,ats,job_id)
);
CREATE TRIGGER curated_lists_no_update BEFORE UPDATE ON curated_shortlists
BEGIN SELECT RAISE(ABORT, 'saved shortlists are immutable'); END;
CREATE TRIGGER curated_items_no_update BEFORE UPDATE ON curated_shortlist_items
BEGIN SELECT RAISE(ABORT, 'saved shortlist items are immutable'); END;
PRAGMA user_version = 11;
"""


MIGRATION_012 = r"""
CREATE TABLE curated_shortlists_expanded (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 list_id TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL,
 window_start TEXT, window_end TEXT,
 idempotency_key TEXT NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL,
 job_count INTEGER NOT NULL CHECK(job_count BETWEEN 0 AND 500)
);
INSERT INTO curated_shortlists_expanded SELECT * FROM curated_shortlists;
CREATE TABLE curated_shortlist_items_expanded (
 list_id TEXT NOT NULL REFERENCES curated_shortlists_expanded(list_id),
 rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 500),
 ats TEXT NOT NULL, job_id TEXT NOT NULL,
 explanation TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 PRIMARY KEY(list_id,rank), UNIQUE(list_id,ats,job_id)
);
INSERT INTO curated_shortlist_items_expanded SELECT * FROM curated_shortlist_items;
DROP TABLE curated_shortlist_items;
DROP TABLE curated_shortlists;
ALTER TABLE curated_shortlists_expanded RENAME TO curated_shortlists;
ALTER TABLE curated_shortlist_items_expanded RENAME TO curated_shortlist_items;
CREATE TRIGGER curated_lists_no_update BEFORE UPDATE ON curated_shortlists
BEGIN SELECT RAISE(ABORT, 'saved shortlists are immutable'); END;
CREATE TRIGGER curated_items_no_update BEFORE UPDATE ON curated_shortlist_items
BEGIN SELECT RAISE(ABORT, 'saved shortlist items are immutable'); END;
PRAGMA user_version = 12;
"""


MIGRATION_013 = r"""
CREATE TABLE application_answer_snapshots (
 capture_id TEXT PRIMARY KEY,
 application_id TEXT NOT NULL REFERENCES applications(application_id),
 attempt_id TEXT NOT NULL REFERENCES browser_attempts(attempt_id),
 captured_at TEXT NOT NULL, recorded_at TEXT NOT NULL, page_url TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL
);
CREATE INDEX application_answers_history ON application_answer_snapshots(application_id,captured_at);
CREATE TRIGGER application_answers_no_update BEFORE UPDATE ON application_answer_snapshots
BEGIN SELECT RAISE(ABORT, 'application answer snapshots are immutable'); END;
PRAGMA user_version = 13;
"""


from .job_reviews.schema import SCHEMA as MIGRATION_014
from .lifecycle.schema import SCHEMA as MIGRATION_015
from .attention.schema import SCHEMA as MIGRATION_017
from .career_actions.schema import SCHEMA as MIGRATION_018
from .interactions.schema import SCHEMA as MIGRATION_019
from .mail.understanding_schema import SCHEMA as _UNDERSTANDING_SCHEMA
from .mail.understanding_replay import SCHEMA as _UNDERSTANDING_REPLAY_SCHEMA
MIGRATION_020 = _UNDERSTANDING_SCHEMA + _UNDERSTANDING_REPLAY_SCHEMA

MIGRATION_016 = r"""
CREATE TABLE job_review_grants (
 grant_id TEXT PRIMARY KEY,
 token_sha256 TEXT NOT NULL UNIQUE,
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id),
 actor TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN ('primary','check')),
 context_sha256 TEXT NOT NULL,
 runtime_id TEXT NOT NULL UNIQUE,
 runtime_json TEXT NOT NULL,
 launch_json TEXT,
 created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 revoked_at TEXT
);
CREATE INDEX job_review_grants_review ON job_review_grants(review_id,expires_at);
CREATE TABLE job_review_grant_items (
 grant_id TEXT NOT NULL REFERENCES job_review_grants(grant_id),
 ordinal INTEGER NOT NULL,
 expected_revision INTEGER NOT NULL,
 snapshot_sha256 TEXT NOT NULL,
 PRIMARY KEY(grant_id,ordinal)
);
PRAGMA user_version = 16;
"""


from .job_reviews.quality_schema import SCHEMA as MIGRATION_021
from .job_reviews.routing_schema import SCHEMA as MIGRATION_022
from .job_reviews.adjudication_schema import SCHEMA as MIGRATION_023


from .mail.classification_review import SCHEMA as MIGRATION_024


MIGRATIONS: Tuple[Tuple[int, str, str], ...] = (
    (1, "initial_job_search_ledger", MIGRATION_001),
    (2, "outlook_sync_state", MIGRATION_002),
    (3, "outlook_evidence_and_replay_hardening", MIGRATION_003),
    (4, "secure_mail_archive_and_temporal_scheduling", MIGRATION_004),
    (5, "runtime_lanes_and_workflow_lineage", MIGRATION_005),
    (6, "hermes_reminders_and_notifications", MIGRATION_006),
    (7, "domain_readiness_and_safe_work_recovery", MIGRATION_007),
    (8, "durable_platform_inference_usage", MIGRATION_008),
    (9, "explicit_automation_activation", MIGRATION_009),
    (10, "browser_application_tracking", MIGRATION_010),
    (11, "curated_shortlists", MIGRATION_011),
    (12, "larger_curated_shortlists", MIGRATION_012),
    (13, "application_answer_history", MIGRATION_013),
    (14, "agent_job_reviews", MIGRATION_014),
    (15, "outlook_application_lifecycle", MIGRATION_015),
    (16, "isolated_review_authority", MIGRATION_016),
    (17, "chief_of_staff_attention", MIGRATION_017),
    (18, "career_actions_agenda", MIGRATION_018),
    (19, "trusted_career_interactions", MIGRATION_019),
    (20, "shared_mail_understanding", MIGRATION_020),
    (21, "review_quality_and_context", MIGRATION_021),
    (22, "conservative_review_routing", MIGRATION_022),
    (23, "basis_bound_review_adjudication", MIGRATION_023),
    (24, "manual_mail_classification_review", MIGRATION_024),
)


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # Inspect the database format before changing an existing file, including
        # its permissions. Candidate databases belong to a different writer.
        return
    # Closing any descriptor for an open SQLite inode drops this process's
    # POSIX locks, including locks held by other connections. Publish a closed,
    # private file atomically instead of opening/closing the live database.
    descriptor, temporary = tempfile.mkstemp(prefix=".sqlite-create-", dir=path.parent)
    try:
        os.close(descriptor)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # Another process or thread already created the database.
    finally:
        os.unlink(temporary)


def _reject_application_candidate(con: sqlite3.Connection) -> None:
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('command_schema_versions','command_installation') LIMIT 1").fetchone():
        raise RuntimeError("The application candidate database cannot be opened by legacy ledger writers")


def connect(path: Path) -> sqlite3.Connection:
    """Open a configured connection; callers control transaction boundaries."""

    _ensure_private_file(path)
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        _reject_application_candidate(con)
        os.chmod(path, 0o600)
    except Exception:
        con.close()
        raise
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 10000")
    deadline = time.monotonic() + 10
    while True:
        try:
            con.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold() or time.monotonic() >= deadline:
                con.close()
                raise
            # journal_mode can report SQLITE_BUSY without honoring busy_timeout
            # while another cold-start connection is enabling WAL.
            time.sleep(0.01)
    try:
        con.execute("PRAGMA synchronous = FULL")
    except Exception:
        con.close()
        raise
    return con


def _applied_migrations(con: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return ()
    return con.execute(
        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
    ).fetchall()


def _migration_statements(sql: str) -> Iterable[str]:
    """Split a migration into complete SQLite statements without leaving a transaction."""

    pending: list[str] = []
    for line in sql.splitlines(keepends=True):
        pending.append(line)
        candidate = "".join(pending)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement:
                yield statement
            pending = []
    if "".join(pending).strip():
        raise RuntimeError("migration contains an incomplete SQL statement")


def migrate(path: Path, applied_at: str) -> None:
    """Apply all ordered migrations, rejecting drift or a newer database."""

    with connect(path) as con:
        try:
            # Serialize before reading the migration ledger. Both launchd lanes may
            # start together, and a stale pre-lock snapshot would make the loser
            # replay ALTER TABLE statements which the winner just committed.
            con.execute("BEGIN IMMEDIATE")
            # Recheck after taking the schema-migration lock: another initializer
            # may have claimed a previously empty file since connect's inspection.
            _reject_application_candidate(con)
            applied = {int(row["version"]): row for row in _applied_migrations(con)}
            if applied and max(applied) > MIGRATIONS[-1][0]:
                raise RuntimeError("job-search database schema is newer than this code")
            for version, name, sql in MIGRATIONS:
                checksum = _checksum(sql)
                row = applied.get(version)
                if row:
                    if row["name"] != name or row["checksum"] != checksum:
                        raise RuntimeError(f"migration {version} checksum mismatch")
                    continue
                for statement in _migration_statements(sql):
                    con.execute(statement)
                con.execute(
                    "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                    "VALUES (?,?,?,?)",
                    (version, name, checksum, applied_at),
                )
                applied[version] = {
                    "version": version,
                    "name": name,
                    "checksum": checksum,
                }
            # A reserved migration can legitimately be filled after a higher version
            # has already shipped. Its own PRAGMA must not regress schema metadata.
            highest_applied = max(applied)
            con.execute(f"PRAGMA user_version = {highest_applied}")
            # Recovery's additive audit records must remain readable and writable
            # beside predecessor core tables without advancing their schema version.
            from .recovery import install_owner_resolution_schema
            install_owner_resolution_schema(con)
            from .inference.usage import install_allowance_schema
            install_allowance_schema(con)
            con.commit()
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
    os.chmod(path, 0o600)


def prepare_database(path: Path, applied_at: str) -> None:
    migrate(Path(path), applied_at)
