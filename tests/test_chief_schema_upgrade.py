"""Upgrade the deployed reviewer schema without losing authority or pause state."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_search import db


class ChiefSchemaUpgradeTests(unittest.TestCase):
    def test_schema19_mail_decisions_cancelled_tasks_and_delivered_briefs_are_preserved(self):
        original = db.MIGRATIONS
        stamp = '2026-10-03T12:00:00Z'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy19.db'
            with patch.object(db, 'MIGRATIONS', original[:19]):
                db.migrate(path, stamp)
            with closing(db.connect(path)) as con, con:
                def insert(table, **values):
                    con.execute('INSERT INTO ' + table + '(' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
                insert('applications', application_id='app', ats='ashby', job_id='job', title_snapshot='Engineer',
                       employer_snapshot='Fixture Co', job_url_snapshot='https://example.test/job', current_phase='active',
                       started_at=stamp, last_activity_at=stamp, last_event_seq=1, projection_sha256='a'*64, updated_at=stamp)
                insert('application_events', event_id='confirmed', application_id='app', event_type='submission_confirmed',
                       occurred_at=stamp, recorded_at=stamp, actor_kind='user', source_kind='dashboard_review',
                       dedupe_key='confirmed', schema_version=1, payload_json='{}')
                insert('mail_evidence', evidence_id='mail', account_id='account', immutable_message_id='message',
                       conversation_id='thread', sender='jobs@example.test', subject='Thanks for applying',
                       received_at=stamp, body_sha256='b'*64, excerpt='What happens next?', created_at=stamp)
                insert('lifecycle_mail_observations', observation_id='observation', account_id='account', immutable_message_id='message',
                       conversation_ref='thread', direction='inbound', subject='Thanks for applying', source_at=stamp,
                       modified_at=stamp, evidence_id='mail', created_at=stamp, updated_at=stamp)
                insert('lifecycle_mail_links', observation_id='observation', application_id='app', confidence=1.0, source='reviewed', created_at=stamp)
                for status, kind in [('accepted', 'submission_confirmed'), ('rejected', 'interview_requested'), ('pending', 'recruiter_contact')]:
                    insert('event_proposals', proposal_id=status, dedupe_key=status, evidence_id='mail', proposed_application_id='app',
                           event_type=kind, producer_kind='model', producer_version='legacy-model', confidence=.99,
                           candidate_application_ids_json='["app"]', evidence_quote='What happens next?', span_start=0, span_end=18,
                           payload_json='{}', status=status, applied_event_id='confirmed' if status == 'accepted' else None,
                           created_at=stamp, decided_at=None if status == 'pending' else stamp)
                    if status != 'pending':
                        insert('event_proposal_decisions', decision_id='decision-'+status, proposal_id=status, decision=status,
                               selected_application_id='app', actor_kind='user', reason='Reviewed email', decided_at=stamp)
                insert('lifecycle_tasks', task_id='cancelled-reply', application_id='app', kind='reply', owner='applicant',
                       status='cancelled', note='Incorrect rhetorical question task', source_time=stamp, evidence_id='mail',
                       policy_version='legacy-reply-rule', revision_no=2, created_at=stamp, updated_at=stamp)
                insert('lifecycle_task_revisions', revision_id='cancelled-revision', task_id='cancelled-reply', revision_no=2,
                       operation='cancel', state_json='{"status":"cancelled"}', actor_kind='user', source_kind='dashboard',
                       source_ref='legacy-correction', occurred_at=stamp, created_at=stamp)
                insert('attention_briefings', briefing_id='delivered-brief', slot='morning', local_date='2026-10-03',
                       timezone='America/Chicago', preference_revision=2, status='finalized', scheduled_for=stamp,
                       expires_at='2026-10-04T12:00:00Z', generation_deadline=stamp, snapshot_json='{"facts":[]}',
                       selected_refs_json='[]', title='Original delivered briefing', body='Original immutable delivered text',
                       renderer='deterministic', created_at=stamp, finalized_at=stamp)
                insert('notification_outbox', notification_id='delivered', dedupe_key='delivered', topic='chief.briefing',
                       policy_id='chief-of-staff-v1', title='Original delivered briefing', body='Original immutable delivered text',
                       status='delivered', max_attempts=3, attempts=1, available_at=stamp, created_at=stamp, delivered_at=stamp,
                       briefing_id='delivered-brief')
                con.execute("UPDATE attention_briefings SET notification_id='delivered' WHERE briefing_id='delivered-brief'")
                for capability, enabled, revision in [('mail', 1, 8), ('notifications', 0, 9), ('outlook_send', 0, 3)]:
                    con.execute('INSERT OR REPLACE INTO automation_controls VALUES (?,?,?,?)', (capability, enabled, revision, stamp))
                insert('command_results', command_name='legacy.correction', idempotency_key='cancel-reply', request_sha256='c'*64,
                       response_json='{"status":"cancelled"}', created_at=stamp)
                tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                before = {table: ([r[1] for r in con.execute('PRAGMA table_info(' + table + ')')],
                                  [dict(r) for r in con.execute('SELECT * FROM ' + table)]) for table in tables}
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 19)
            db.migrate(path, '2026-10-03T13:00:00Z')
            db.migrate(path, '2026-10-03T14:00:00Z')
            with closing(db.connect(path)) as con:
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], db.MIGRATIONS[-1][0])
                for table, (columns, rows) in before.items():
                    query = 'SELECT ' + ','.join(columns) + ' FROM ' + table
                    if table == 'schema_migrations':
                        query += ' WHERE version<=19'
                    self.assertCountEqual([dict(r) for r in con.execute(query)], rows, table)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM mail_understanding_ownership').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM mail_understanding_analyses').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM event_proposals WHERE understanding_finding_id IS NOT NULL').fetchone()[0], 0)
                self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_deployed_schema16_upgrades_without_rewriting_existing_migrations(self):
        # Installed source f4d0d22 owns migration16. Its exact SQL must remain stable.
        self.assertEqual(db._checksum(db.MIGRATION_016),
                         'bd936fc52e467209e35e1330a5974b1e35700462ad40fea7ea304e0504185612')
        original = db.MIGRATIONS
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'application.db'
            with patch.object(db, 'MIGRATIONS', original[:16]):
                db.migrate(path, '2026-10-03T12:00:00Z')
            with closing(db.connect(path)) as con, con:
                before = [tuple(row) for row in con.execute('SELECT * FROM schema_migrations ORDER BY version')]
                con.execute("INSERT INTO automation_controls VALUES ('notifications',0,4,'2026-10-03T12:00:00Z')")
                con.execute("INSERT INTO automation_controls VALUES ('mail',1,7,'2026-10-03T12:00:00Z')")
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 16)
            db.migrate(path, '2026-10-03T13:00:00Z')
            db.migrate(path, '2026-10-03T13:05:00Z')
            with closing(db.connect(path)) as con:
                self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM schema_migrations WHERE version<=16 ORDER BY version')], before)
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], db.MIGRATIONS[-1][0])
                self.assertEqual([tuple(row) for row in con.execute('SELECT version,name FROM schema_migrations WHERE version>16 ORDER BY version')],
                                 [(version, name) for version, name, _ in db.MIGRATIONS if version > 16])
                for table in ('job_review_grants', 'job_review_grant_items', 'attention_briefings', 'career_send_proposals', 'interaction_tickets'):
                    self.assertIsNotNone(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
                controls = {row['capability']: (row['enabled'], row['revision']) for row in con.execute('SELECT * FROM automation_controls')}
                self.assertEqual(controls['notifications'], (0, 4))
                self.assertEqual(controls['mail'], (1, 7))
                for capability in ('briefing_ai', 'outlook_send', 'calendar_commitments'):
                    self.assertEqual(controls[capability], (0, 0))


if __name__ == '__main__':
    unittest.main()
