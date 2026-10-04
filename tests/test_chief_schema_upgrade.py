"""Upgrade the deployed reviewer schema without losing authority or pause state."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_search import db


class ChiefSchemaUpgradeTests(unittest.TestCase):
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
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 19)
                self.assertEqual([tuple(row) for row in con.execute('SELECT version,name FROM schema_migrations WHERE version>16 ORDER BY version')],
                                 [(17, 'chief_of_staff_attention'), (18, 'career_actions_agenda'), (19, 'trusted_career_interactions')])
                for table in ('job_review_grants', 'job_review_grant_items', 'attention_briefings', 'career_send_proposals', 'interaction_tickets'):
                    self.assertIsNotNone(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
                controls = {row['capability']: (row['enabled'], row['revision']) for row in con.execute('SELECT * FROM automation_controls')}
                self.assertEqual(controls['notifications'], (0, 4))
                self.assertEqual(controls['mail'], (1, 7))
                for capability in ('briefing_ai', 'outlook_send', 'calendar_commitments'):
                    self.assertEqual(controls[capability], (0, 0))


if __name__ == '__main__':
    unittest.main()
