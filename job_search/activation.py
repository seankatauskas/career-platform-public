"""Explicit, persistent automation switches; absent switches preserve legacy behavior."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from .contracts import canonical_json, utc_now
from .db import connect

GROUPS = {
    'collection': ('ats.authoritative', 'ats.new_only', 'ats.refresh_recent', 'opportunity.location_refresh'),
    'ranking': ('opportunity.preference_refresh',),
    'mail': ('outlook.mail.sync',),
    'outlook_actions': ('outlook.actions.execute',),
    'notifications': ('notification.deliver', 'notification.reminders_due', 'notification.shortlist_evaluate'),
    'salary': ('opportunity.salary_drain',),
    'resume_generation': ('resume.optimize',),
}

def task_group(task: str) -> str | None:
    return next((name for name, tasks in GROUPS.items() if task in tasks), None)

def disabled_tasks(con) -> tuple[str, ...]:
    return tuple(task for row in con.execute('SELECT capability FROM automation_controls WHERE enabled=0')
                 for task in GROUPS.get(row[0], ()))

def controls(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='automation_controls'").fetchone():
            return []
        return [dict(r) for r in con.execute('SELECT * FROM automation_controls ORDER BY capability')]

def initialize_paused(path: Path) -> None:
    with connect(path) as con:
        for name in GROUPS:
            con.execute('INSERT OR IGNORE INTO automation_controls VALUES (?,0,0,?)', (name, utc_now()))

def set_control(config, capability: str, enabled: bool, *, expected_revision: int, command_id: str) -> dict:
    if (capability not in GROUPS or not isinstance(enabled, bool)
            or type(expected_revision) is not int or expected_revision < 0
            or not isinstance(command_id, str) or not command_id or len(command_id)>256):
        raise ValueError('invalid automation decision')
    request = canonical_json({'capability':capability,'enabled':enabled,'revision':expected_revision})
    stamp = utc_now()
    with connect(config.application_db) as con:
        con.execute('BEGIN IMMEDIATE')
        replay = con.execute('SELECT request_json,result_json FROM automation_decisions WHERE command_id=?',(command_id,)).fetchone()
        if replay:
            if replay[0] != request: raise ValueError('automation command conflicts with an earlier decision')
            return json.loads(replay[1])
        old = con.execute('SELECT * FROM automation_controls WHERE capability=?',(capability,)).fetchone()
        revision = old['revision'] if old else 0
        if revision != expected_revision: raise ValueError('automation settings changed; refresh before deciding')
        if enabled and capability == 'mail':
            if not config.outlook_client_id: raise ValueError('configure Outlook before activation')
            con.execute('INSERT OR IGNORE INTO outlook_activation VALUES (?,?)',(config.outlook_account_id,stamp))
        result = {'capability':capability,'enabled':enabled,'revision':revision+1,'updated_at':stamp}
        con.execute('INSERT INTO automation_controls VALUES (?,?,?,?) ON CONFLICT(capability) DO UPDATE SET enabled=excluded.enabled,revision=excluded.revision,updated_at=excluded.updated_at',
                    (capability,int(enabled),revision+1,stamp))
        # Stop schedule creation immediately. Re-enabling reseeds future occurrences,
        # and credentials remain a separate prerequisite.
        tasks = GROUPS[capability]
        marks = ','.join('?' for _ in tasks)
        con.execute(f'UPDATE schedule_specs SET enabled=0,enabled_since=NULL WHERE task_kind IN ({marks})',tasks)
        con.execute('INSERT INTO automation_decisions VALUES (?,?,?,?,?)',(command_id,request,canonical_json(result),'user',stamp))
    from .scheduler import seed_default_schedules
    from datetime import datetime, timezone
    import os
    seed_default_schedules(config.application_db,datetime.now(timezone.utc),config.environment(os.environ))
    return result

def mail_start(path: Path, account: str) -> str | None:
    with connect(path) as con:
        row=con.execute('SELECT started_at FROM outlook_activation WHERE account_id=?',(account,)).fetchone()
    return row[0] if row else None
