"""Operator-only transition between application owners; operational storage stays put.

Freezing legacy writers is irreversible through application APIs. Reversal after new
writes requires reconciliation, never an old database restored over new state.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .commands import DomainError
from .db import connect

LEGACY_TASKS = frozenset({
    'system.worker_tick', 'outlook.reminders.publish', 'outlook.mail.sync',
    'outlook.mail.replay', 'outlook.calendar.sync', 'outlook.actions.execute',
    'career.mail.reconcile', 'career.reply.context', 'career.reply.prepare',
    'career.actions.execute', 'career.calendar.sync', 'attention.tick',
    'briefing.compose', 'notification.reminders_due', 'mail.understanding',
})
LEGACY_TABLES = frozenset({
    'applications','application_events','mail_evidence','event_proposals',
    'event_proposal_decisions','action_proposals','action_approval_decisions',
    'action_executions','temporal_proposals','temporal_proposal_decisions',
    'accepted_interview_schedules','local_reminders','reminders',
    'application_notes','application_answer_snapshots','browser_jobs','browser_attempts',
    'browser_observations','browser_finalizations',
})
LEGACY_PREFIXES = ('lifecycle_', 'interview_', 'career_', 'mail_understanding_')


def protected_tables(con):
    return sorted(row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0] in LEGACY_TABLES or row[0].startswith(LEGACY_PREFIXES))


def binding(operational_db):
    with closing(connect(operational_db)) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='application_owner_binding'").fetchone():
            return None
        row = con.execute('SELECT * FROM application_owner_binding WHERE singleton=1').fetchone()
        return dict(row) if row else None


def require_backend(config):
    """Fail closed if a stale config tries to start the old application writer."""
    state = binding(config.application_db)
    if config.application_backend == 'legacy':
        if state:
            raise DomainError('version_conflict', 'Legacy application writers are frozen; use the bound owners configuration')
    elif not state or Path(state['owner_path']).resolve() != config.application_owner_db.resolve():
        raise DomainError('version_conflict', 'Owners backend requires an operator-created database binding')
    else:
        try:
            with closing(sqlite3.connect(config.application_owner_db.resolve().as_uri()+'?mode=ro',uri=True)) as con:
                identity=con.execute('SELECT identity FROM installation_identity WHERE singleton=1').fetchone()
                manifest=con.execute('SELECT report_json FROM migration_manifest WHERE id=1').fetchone()
            if not identity or identity[0]!=state.get('owner_identity') or not manifest:
                raise DomainError('version_conflict','Bound application database identity changed')
        except sqlite3.Error:
            raise DomainError('version_conflict','Bound application database is missing its installation identity') from None


def freeze_legacy(operational_db, runtime, *, operator, report):
    """Run while all application services are stopped, after verified conversion.

    The legacy fence commits first. A crash leaves both external execution and the
    legacy domain paused; restarting cannot silently fall back to the old writer.
    """
    if not operator or not report.get('ready_for_review'):
        raise DomainError('invalid_input', 'A blocker-free conversion report and operator are required')
    if Path(report['candidate_path']).resolve() != runtime.executor.path.resolve():
        raise DomainError('invalid_input', 'Conversion report targets another database')
    archive = Path(report['archive_path'])
    if hashlib.sha256(archive.read_bytes()).hexdigest() != report['archive_sha256']:
        raise DomainError('version_conflict', 'Historical archive changed')
    if not runtime.executor.activation_status()['paused']:
        raise DomainError('version_conflict', 'Pause external dispatch before binding')
    from .application_migration import _inventory
    from .commands import digest
    with closing(sqlite3.connect(runtime.executor.path,timeout=30,isolation_level=None)) as owner_con:
        owner_con.row_factory=sqlite3.Row
        owner_con.execute("BEGIN IMMEDIATE")
        if not runtime.executor.activation_status(owner_con)["paused"]:
            raise DomainError("version_conflict","Pause dispatch before binding")
        tables={r[0] for r in owner_con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'migration_manifest' not in tables:
            raise DomainError('invalid_input','Owners database has no conversion manifest')
        manifest=owner_con.execute('SELECT report_json FROM migration_manifest WHERE id=1').fetchone()
        if not manifest or json.loads(manifest[0])!=report:
            raise DomainError('version_conflict','Conversion report differs from the stored manifest')
        inventory=_inventory(owner_con)
        names=sorted(t for t in inventory['counts'] if t.startswith(('app_','corr_','understand_','action_')))
        actual=digest({'counts':{t:inventory['counts'][t] for t in names},'hashes':{t:inventory['table_sha256'][t] for t in names}})
        if actual!=report['candidate_domain_sha256']:
            raise DomainError('version_conflict','Converted application state changed before binding')
        owner_identity=runtime.executor.installation_identity(owner_con)
        with closing(connect(operational_db)) as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                if con.execute('SELECT 1 FROM worker_leases WHERE expires_at>?', (runtime.executor.clock(),)).fetchone():
                    raise DomainError('version_conflict', 'Stop workers before binding application ownership')
                # Reject a stale conversion even when unrelated operational rows changed.
                from .application_migration import _inventory
                inventory = _inventory(con)
                for table in protected_tables(con):
                    if inventory['table_sha256'][table] != report['source_table_sha256'].get(table):
                        raise DomainError('version_conflict', 'Application state changed since conversion')
                con.execute('CREATE TABLE IF NOT EXISTS application_owner_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner_path TEXT NOT NULL, owner_identity TEXT NOT NULL, operator TEXT NOT NULL, recorded_at TEXT NOT NULL)')
                existing = con.execute('SELECT owner_path,owner_identity FROM application_owner_binding WHERE singleton=1').fetchone()
                owner = str(runtime.executor.path.resolve())
                if existing and (existing[0] != owner or existing[1] != owner_identity):
                    raise DomainError('version_conflict', 'Another owners database is already bound')
                con.execute('INSERT OR IGNORE INTO application_owner_binding VALUES(1,?,?,?,?)', (owner,owner_identity,operator,runtime.executor.clock()))
                for table in protected_tables(con):
                    if not re.fullmatch('[a-z_]+', table):
                        raise DomainError('invalid_input', 'Unexpected application table name')
                    for operation in ('INSERT','UPDATE','DELETE'):
                        con.execute(f"CREATE TRIGGER IF NOT EXISTS owner_fence_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'legacy application writes are frozen'); END")
                placeholders = ','.join('?' for _ in LEGACY_TASKS)
                kinds = sorted(LEGACY_TASKS)
                con.execute(f'UPDATE schedule_specs SET enabled=0 WHERE task_kind IN ({placeholders})', kinds)
                con.execute(f"UPDATE work_items SET status='cancelled',completed_at=?,last_error=CASE WHEN external_outcome IN ('unknown','in_flight') THEN last_error ELSE 'quarantined by application ownership transition' END,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE task_kind IN ({placeholders}) AND status IN ('queued','running')", (runtime.executor.clock(),*kinds))
                con.execute("UPDATE outbox_messages SET status='dead',last_error='quarantined by application ownership transition' WHERE topic='notification.application_event' AND status IN ('pending','delivering')")
                # A stopped worker may already have crossed the provider-write
                # boundary. Quarantine its delivery for reconciliation instead
                # of overwriting that uncertainty with an ordinary cancellation.
                con.execute("UPDATE notification_outbox SET status='dead',last_error='delivery_reconciliation_required',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE (application_id IS NOT NULL OR topic IN ('reminder.due','attention.required','mail.recruiter_update')) AND (status='delivering' OR (status='pending' AND last_error='delivery_reconciliation_required'))")
                con.execute("UPDATE notification_outbox SET status='cancelled',last_error='quarantined by application ownership transition' WHERE (application_id IS NOT NULL OR topic IN ('reminder.due','attention.required','mail.recruiter_update')) AND status='pending'")
                con.execute("UPDATE interaction_tickets SET status='cancelled' WHERE status='pending'")
                con.commit()
            except Exception:
                con.rollback()
                raise
    return {'bound': True, 'external_dispatch_paused': True, 'owner_path': owner}


def readiness(config, runtime):
    require_backend(config)
    with runtime.executor.read() as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'migration_issues' not in tables or 'migration_manifest' not in tables:
            raise DomainError('version_conflict','Conversion manifest is required')
        blockers = con.execute("SELECT COUNT(*) FROM migration_issues WHERE severity='blocker'").fetchone()[0] if 'migration_issues' in tables else 0
        pending = con.execute("SELECT owner,kind,COUNT(*) AS count,MIN(recorded_at) AS oldest FROM command_work WHERE status='pending' GROUP BY owner,kind").fetchall()
        return {'backend':'owners','activation':runtime.executor.activation_status(con),
                'restore':runtime.executor.restore_status(con),
                'conversion_blockers':blockers,'pending_work':[dict(r) for r in pending],
                'schema_identity':dict(con.execute('SELECT owner,checksum FROM command_schema_versions'))}


def add_arguments(parser):
    parser.add_argument('action',choices=('status','bind','pause','activate','acknowledge-restore'))
    parser.add_argument('--report',type=Path)
    parser.add_argument('--expected-revision',type=int)
    parser.add_argument('--expected-restore-revision',type=int)
    parser.add_argument('--operator')
    parser.add_argument('--reason')


def command(config,args):
    from .application_runtime import ApplicationRuntime
    if config.application_owner_db is None:
        raise DomainError('invalid_input','Configure a separate application_owner_db first')
    runtime=ApplicationRuntime(config.application_owner_db)
    if args.action=='bind':
        if args.report is None:
            raise DomainError('invalid_input','Provide the verified conversion report')
        return freeze_legacy(config.application_db,runtime,operator=args.operator,
            report=json.loads(args.report.read_text()))
    if args.action=='status':
        return readiness(config,runtime)
    if args.action=='acknowledge-restore':
        readiness(config,runtime)
        if args.expected_restore_revision is None or not args.operator or not args.reason:
            raise DomainError('invalid_input','Provide expected restore revision, operator and reconciliation reason')
        return runtime.executor.acknowledge_restore(expected_restore_revision=args.expected_restore_revision,
            operator=args.operator,reason=args.reason)
    if args.expected_revision is None or not args.operator or not args.reason:
        raise DomainError('invalid_input','Provide expected revision, operator and reason')
    if args.action=='activate':
        state=readiness(config,runtime)
        if state['conversion_blockers']:
            raise DomainError('dependency_unresolved','Resolve conversion blockers before activating')
        # Validate the selected identity with the same trusted adapter used for writes.
        from .application_production import configured_provider_resolver
        configured_provider_resolver(config,config.environment())(config.outlook_account_id).verify_binding()
    return runtime.executor.set_activation(paused=args.action=='pause',expected_revision=args.expected_revision,
        operator=args.operator,reason=args.reason)
