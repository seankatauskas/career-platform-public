"""Read-only installation diagnostics composed from the active owners."""
from contextlib import closing
import sqlite3

from .commands.installation import restore_required
from .external_actions.api import ExternalActionOperations
from .applications.api import ApplicationOperations
from .readiness import _capability


def application_readiness(config):
    try:
        with closing(sqlite3.connect(config.application_db.resolve().as_uri() + '?mode=ro', uri=True)) as operational:
            binding = operational.execute('SELECT owner_path,owner_identity FROM application_owner_binding WHERE singleton=1').fetchone()
        if not binding or binding[0] != str(config.application_owner_db.resolve()):
            raise ValueError('installation_binding_invalid')
        with closing(sqlite3.connect(config.application_owner_db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            identity = con.execute('SELECT identity FROM installation_identity WHERE singleton=1').fetchone()
            if not identity or identity[0] != binding[1]:
                raise ValueError('installation_binding_invalid')
            paused, revision = con.execute('SELECT paused,revision FROM installation_control WHERE singleton=1').fetchone()
            restore = restore_required(con) is not None
            blockers = con.execute("SELECT COUNT(*) FROM migration_issues WHERE severity='blocker'").fetchone()[0]
            actions = ExternalActionOperations.readiness_summary(con)
            reminders = ApplicationOperations.notification_recovery_summary(con)['restore_quarantined_reminders']
            pending = [dict(row) for row in con.execute("SELECT owner,kind,COUNT(*) AS count FROM command_work WHERE status='pending' GROUP BY owner,kind")]
        uncertain = actions['uncertain'] + reminders
        result = {**actions, 'paused': bool(paused), 'revision': revision,
                  'uncertain': uncertain, 'restore_quarantined_reminders': reminders,
                  'restore_review_required': restore, 'conversion_blockers': blockers,
                  'pending_work': pending}
        state = 'blocked' if blockers or restore else 'ready'
        reason = 'conversion_review_required' if blockers else 'restore_review_required' if restore else 'bound_owner_state'
        execution = 'blocked' if uncertain or restore else 'paused' if paused else 'configured_unverified'
        execution_reason = 'external_reconciliation_required' if uncertain else 'restore_review_required' if restore else 'installation_paused' if paused else 'exact_approval_required'
        return result, [
            _capability('applications', state, reason=reason, action='inspect_application_installation' if state == 'blocked' else 'none'),
            _capability('application_execution', execution, enabled=not paused,
                        reason=execution_reason, action='inspect_application_installation' if execution != 'configured_unverified' else 'none'),
        ]
    except (OSError, sqlite3.Error, ValueError, AttributeError):
        return {'available': False}, [_capability('applications', 'blocked', reason='owner_state_unavailable', action='inspect_application_installation')]
