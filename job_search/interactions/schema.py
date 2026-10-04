"""Durable review tickets and authenticated chat command receipts."""
SCHEMA = """
CREATE TABLE interaction_tickets (
 ticket_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('send','attention')),
 target_id TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL,
 source_version TEXT NOT NULL,
 bot_id TEXT NOT NULL, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 review_json TEXT NOT NULL,
 message_id TEXT,
 notification_id TEXT UNIQUE REFERENCES notification_outbox(notification_id),
 delivery_state TEXT NOT NULL DEFAULT 'queued' CHECK(delivery_state IN ('queued','claimed','sent','unknown','cancelled')),
 delivery_started_at TEXT,
 delivery_revision INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','consumed','cancelled')),
 command_id TEXT,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX interaction_pending ON interaction_tickets(status,created_at);
CREATE UNIQUE INDEX interaction_review_message ON interaction_tickets(bot_id,chat_id,message_id) WHERE message_id IS NOT NULL;
CREATE TABLE interaction_commands (
 command_id TEXT PRIMARY KEY,
 envelope_sha256 TEXT NOT NULL,
 ticket_id TEXT,
 envelope_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','completed')),
 result_json TEXT,
 created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE interaction_delivery_decisions (
 decision_id TEXT PRIMARY KEY,
 idempotency_key TEXT NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL,
 ticket_id TEXT NOT NULL REFERENCES interaction_tickets(ticket_id),
 decision TEXT NOT NULL CHECK(decision IN ('received','abandon')),
 expected_revision INTEGER NOT NULL,
 actor_kind TEXT NOT NULL CHECK(actor_kind='user'),
 source_ref TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TRIGGER interaction_delivery_decisions_no_update BEFORE UPDATE ON interaction_delivery_decisions
BEGIN SELECT RAISE(ABORT,'interaction delivery decisions are append-only'); END;
CREATE TRIGGER interaction_delivery_decisions_no_delete BEFORE DELETE ON interaction_delivery_decisions
BEGIN SELECT RAISE(ABORT,'interaction delivery decisions are append-only'); END;
"""
