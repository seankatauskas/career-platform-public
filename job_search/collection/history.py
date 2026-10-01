"""Append-only observations of public postings, separate from application events."""
from __future__ import annotations

import json
import sqlite3


DATE_FIELDS = ("posted_at", "source_updated_at", "publishedAt", "first_seen", "last_seen", "closed_at")
TRACKED_FIELDS = ("title", "company", "location", "department", "team", "employmentType",
                  "isRemote", "workplaceType", "jobUrl", "description")


def prepare_history(con: sqlite3.Connection) -> None:
    """Install atomic observation triggers; unchanged scans never add events.

    Existing first_seen/closed_at dates are read as legacy observations. We do not
    invent intermediate versions or claim that a scan time is an employer date.
    """
    con.executescript("""
        CREATE TABLE IF NOT EXISTS job_posting_history_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), started_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO job_posting_history_meta VALUES (1,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
        CREATE TABLE IF NOT EXISTS job_posting_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ats TEXT NOT NULL, job_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('opened','modified','closed','reopened')),
            observed_at TEXT NOT NULL, source_at TEXT,
            changes_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS posting_events_job ON job_posting_events(ats,job_id,event_id);
        CREATE TRIGGER IF NOT EXISTS posting_opened AFTER INSERT ON jobs BEGIN
            INSERT INTO job_posting_events(ats,job_id,event_type,observed_at,source_at)
            VALUES(NEW.ats,NEW.id,'opened',NEW.first_seen,NULLIF(NEW.posted_at,''));
        END;
        CREATE TRIGGER IF NOT EXISTS posting_closed AFTER UPDATE OF closed_at ON jobs
        WHEN OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL BEGIN
            INSERT INTO job_posting_events(ats,job_id,event_type,observed_at)
            VALUES(NEW.ats,NEW.id,'closed',NEW.closed_at);
        END;
        CREATE TRIGGER IF NOT EXISTS posting_reopened AFTER UPDATE OF closed_at ON jobs
        WHEN OLD.closed_at IS NOT NULL AND NEW.closed_at IS NULL BEGIN
            INSERT INTO job_posting_events(ats,job_id,event_type,observed_at,source_at)
            VALUES(NEW.ats,NEW.id,'reopened',NEW.last_seen,NULL);
        END;
    """)
    expressions = []
    conditions = []
    for field in (*TRACKED_FIELDS, "source_updated_at", "posted_at"):
        condition = f"COALESCE(OLD.{field},'')<>COALESCE(NEW.{field},'')"
        if field in ("source_updated_at", "posted_at"):
            # First acquisition of date provenance is enrichment, not evidence of an edit.
            condition += f" AND COALESCE(OLD.{field},'')<>'' AND COALESCE(NEW.{field},'')<>''"
        conditions.append(f"({condition})")
        values = ("json_object('before_length',length(OLD.description),'after_length',length(NEW.description))"
                  if field == "description" else f"json_object('before',OLD.{field},'after',NEW.{field})")
        expressions.append(f"CASE WHEN {condition} THEN json_object('{field}',{values}) ELSE '{{}}' END")
    changes = "'{}'"
    for expression in expressions:
        changes = f"json_patch({changes},{expression})"
    con.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS posting_modified AFTER UPDATE ON jobs
        WHEN {' OR '.join(conditions)} BEGIN
            INSERT INTO job_posting_events(ats,job_id,event_type,observed_at,source_at,changes_json)
            VALUES(NEW.ats,NEW.id,'modified',NEW.last_seen,
                CASE WHEN NEW.source_updated_at IS NOT OLD.source_updated_at THEN NULLIF(NEW.source_updated_at,'') END,
                {changes});
        END;
    """)


def read_history(con: sqlite3.Connection, job: dict, before: int | None = None, limit: int = 50) -> dict:
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = []
    started = None
    if "job_posting_events" in tables:
        rows = con.execute(
            "SELECT * FROM job_posting_events WHERE ats=? AND job_id=? "
            + ("AND event_id<? " if before is not None else "")
            + "ORDER BY event_id DESC LIMIT ?",
            (job['ats'], job['id'], *((before,) if before is not None else ()), limit + 1),
        ).fetchall()
        meta = con.execute("SELECT started_at FROM job_posting_history_meta WHERE singleton=1").fetchone()
        started = meta[0] if meta else None
    more = len(rows) > limit
    events = []
    for row in rows[:limit]:
        item = dict(row)
        item['changes'] = json.loads(item.pop('changes_json'))
        events.append(item)
    next_before = events[-1]['event_id'] if more else None
    if not more:
        opened = "job_posting_events" in tables and con.execute(
            "SELECT 1 FROM job_posting_events WHERE ats=? AND job_id=? AND event_type='opened' LIMIT 1",
            (job['ats'], job['id'])).fetchone()
        if job.get('first_seen') and not opened:
            events.append({'event_type': 'first_seen', 'observed_at': job['first_seen'], 'changes': {}})
    if before is None and job.get('closed_at'):
        closed = "job_posting_events" in tables and con.execute(
            "SELECT 1 FROM job_posting_events WHERE ats=? AND job_id=? AND event_type='closed' AND observed_at=?",
            (job['ats'], job['id'], job['closed_at'])).fetchone()
        if not closed:
            events.insert(0, {'event_type': 'closed', 'observed_at': job['closed_at'], 'changes': {}})
    return {'job': {key: job.get(key) for key in ('ats','id', *DATE_FIELDS)},
            'events': events, 'next_before': next_before, 'history_started_at': started,
            'history_note': 'Changes and closures are observed during scans. Earlier unrecorded changes are unavailable; a closed posting does not mean your application was rejected.'}
