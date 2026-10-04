"""Read-only chief-of-staff diagnostics with no private message content."""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3


def chief_status(config):
    from .readiness import _capability
    result, capabilities = {}, []
    with closing(sqlite3.connect(config.application_db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        controls = dict(con.execute('SELECT capability,enabled FROM automation_controls'))
        row = con.execute('SELECT values_json FROM attention_preferences WHERE singleton=1').fetchone()
        preferences = json.loads(row[0]) if row else {}
        result.update(mode=preferences.get('mode', 'important_developments'), shadow=preferences.get('shadow', True))
        result['active_attention'] = con.execute("SELECT COUNT(*) FROM attention_candidates WHERE status IN ('active','snoozed')").fetchone()[0]
        result['uncertain_sends'] = con.execute("SELECT COUNT(*) FROM career_send_proposals WHERE status='uncertain'").fetchone()[0]
        result['calendar_reviews'] = con.execute("SELECT COUNT(*) FROM career_commitments WHERE status='needs_review'").fetchone()[0]
        result['briefings'] = dict(con.execute('SELECT status,COUNT(*) FROM attention_briefings GROUP BY status'))
        result['agenda_last_success'] = con.execute("SELECT MAX(checked_at) FROM career_agenda_snapshots WHERE error_code=''").fetchone()[0]
        configurations = {
            'briefing_ai': bool(config.briefing_ai_enabled and config.briefing_inference_config),
            'outlook_send': bool(config.outlook_client_id),
            'calendar_commitments': bool(config.outlook_client_id),
        }
        for identity, configured in configurations.items():
            enabled = bool(controls.get(identity, False))
            state = 'paused' if not enabled else 'configured_unverified' if configured else 'disabled'
            reason = 'explicitly_paused' if not enabled else 'awaiting_verified_operation' if configured else 'not_configured'
            if identity == 'outlook_send' and result['uncertain_sends']:
                state, reason = 'blocked', 'external_reconciliation_required'
            capabilities.append(_capability(identity,state,configured=configured,enabled=enabled,reason=reason,
                action='inspect_reconciliation' if state=='blocked' else 'review_activation' if not enabled else 'review_configuration'))
        result['interactions_configured'] = bool(config.interaction_token_file)
    return result, capabilities
