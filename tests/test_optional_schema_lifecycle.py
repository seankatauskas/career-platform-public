"""Optional stores cannot migrate as a side effect of production execution."""
from contextlib import closing
from dataclasses import replace
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.collection import boards, dedupe, dedupe_incremental, locations
from job_search.ranking import labeler, model, proxy, refresh_freshness
from job_search.resume_lab.store import ResumeLabStore
from job_search.resume_lab.career_store import CareerStore
from job_search.runtime import RuntimeConfigV1
from job_search.salary import enrichment, llm, teacher, v3, labeler as salary_labeler
from job_search.schema_lifecycle import (check_config, check_optional_path,
    explicit_migration, migrate_config)


class OptionalSchemaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = RuntimeConfigV1.defaults(self.root)
        self.strict = patch.dict(os.environ, {"JOB_SEARCH_SCHEMA_MODE": "require_current"})
        self.strict.start()
        self.addCleanup(self.strict.stop)
        migrate_config(self.config)

    def state(self):
        gc.collect()
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file() and not p.name.endswith(('-shm', '-wal'))}

    def install_optional(self):
        with explicit_migration():
            labeler.prepare_preferences(self.config.jobs_db)
            model.prepare_state(self.config.preference_db)
            proxy.prepare_schema(self.config.proxy_db)
            with closing(sqlite3.connect(self.config.jobs_db)) as con, con:
                enrichment.prepare_enrichment(con)
                llm.prepare_schema(con)
                locations.prepare_schema(con)
            teacher.prepare_schema(self.config.jobs_db)
            v3.prepare_schema(self.config.jobs_db)
            salary_labeler.prepare_validation(self.config.jobs_db)
            dedupe.prepare_families(self.config.jobs_db)
            refresh_freshness.ensure_tracking(self.config.jobs_db, 'source')
            refresh_freshness.ensure_tracking(self.config.preference_db, 'scores')

    def test_migration_does_not_activate_absent_optional_features(self):
        self.assertFalse(self.config.preference_db.exists())
        self.assertFalse(self.config.proxy_db.exists())
        with closing(sqlite3.connect(self.config.jobs_db)) as con:
            names = {row[0] for row in con.execute('SELECT name FROM sqlite_master')}
        self.assertNotIn('job_preferences', names)
        self.assertNotIn('salary_llm_settings', names)
        before = self.state()
        check_config(self.config, offline=True)
        self.assertEqual(before, self.state())

    def test_fresh_strict_collector_can_save_native_enrichment_and_refresh_locations(self):
        row = {field: '' for field in boards.FIELDS}
        row.update(ats='ashby', id='fictional', company='Example', title='Engineer')
        result = boards.save([row], self.config.jobs_db, '2026-10-18T00:00:00Z', enrichments=[])
        self.assertEqual(result[0], 1)
        locations.backfill(self.config.jobs_db)
        check_config(self.config)

    def test_explicit_feature_preparation_creates_no_ranked_jobs_or_activation(self):
        with patch('job_search.collection.dedupe.prepare_families', side_effect=AssertionError('family computation')):
            migrate_config(self.config, prepare_features=('ranking', 'salary'))
        with closing(sqlite3.connect(self.config.application_db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM automation_controls WHERE capability IN ('ranking','salary')").fetchone()[0], 0)
        with closing(sqlite3.connect(self.config.preference_db)) as con:
            for table in ('preference_model_runs', 'preference_scores', 'preference_embedding_cache'):
                self.assertEqual(con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        with closing(sqlite3.connect(self.config.jobs_db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_families').fetchone()[0], 0)
            self.assertIsNone(con.execute("SELECT value FROM salary_llm_settings WHERE key='activated_at'").fetchone())
        from job_search.schema_lifecycle import check_feature_schemas
        check_feature_schemas(self.config, 'ranking')
        check_feature_schemas(self.config, 'salary')
        # The first ordinary refresh may compute derived rows, but cannot create schema.
        dedupe.prepare_families(self.config.jobs_db)
        refresh_freshness.ensure_tracking(self.config.jobs_db, 'source')
        refresh_freshness.ensure_tracking(self.config.preference_db, 'scores')

    def test_enabled_feature_is_prepared_but_disabled_feature_is_not_enabled(self):
        with closing(sqlite3.connect(self.config.application_db)) as con, con:
            con.execute("INSERT INTO automation_controls VALUES ('ranking',1,7,'prior')")
            con.execute("INSERT INTO automation_controls VALUES ('salary',0,9,'prior')")
        migrate_config(self.config)
        self.assertTrue(self.config.preference_db.exists())
        check_config(self.config)
        with closing(sqlite3.connect(self.config.application_db)) as con:
            self.assertEqual(list(con.execute("SELECT capability,enabled,revision FROM automation_controls WHERE capability IN ('ranking','salary') ORDER BY capability")),
                             [('ranking', 1, 7), ('salary', 0, 9)])

    def test_strict_activation_requires_prepared_schema_and_failed_enable_has_no_writes(self):
        from job_search.activation import set_control
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'migrate --prepare-feature ranking'):
            set_control(self.config, 'ranking', True, expected_revision=0, command_id='enable-ranking')
        self.assertEqual(before, self.state())
        # Disabling must remain possible even when feature state does not exist.
        disabled = set_control(self.config, 'ranking', False, expected_revision=0, command_id='disable-ranking')
        self.assertFalse(disabled['enabled'])
        migrate_config(self.config, prepare_features=('ranking',))
        enabled = set_control(self.config, 'ranking', True, expected_revision=1, command_id='enable-ranking')
        self.assertTrue(enabled['enabled'])

    def test_cli_migrate_and_init_accept_feature_preparation_but_check_does_not(self):
        from job_search.cli import main, build_parser
        for command in ('migrate', 'init'):
            with self.subTest(command=command):
                root = self.root / command
                root.mkdir()
                config = root / 'config.json'
                config.write_text(json.dumps({'version': 1, 'project_root': str(root)}))
                config.chmod(0o600)
                with patch('sys.stdout', new_callable=io.StringIO):
                    self.assertEqual(main(['--config', str(config), command, '--prepare-feature', 'ranking']), 0)
                self.assertTrue((root / 'job-boards-preference.db').exists())
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            build_parser().parse_args(['check-schema', '--prepare-feature', 'ranking'])

    def test_legacy_existing_ranking_store_prepares_first_refresh_without_activation(self):
        with explicit_migration():
            model.prepare_state(self.config.preference_db)
        migrate_config(self.config)
        check_config(self.config)
        refresh_freshness.ensure_tracking(self.config.jobs_db, 'source')
        refresh_freshness.ensure_tracking(self.config.preference_db, 'scores')
        dedupe.prepare_families(self.config.jobs_db)
        with closing(sqlite3.connect(self.config.application_db)) as con:
            self.assertIsNone(con.execute("SELECT enabled FROM automation_controls WHERE capability='ranking'").fetchone())

    def test_all_family_tracking_removed_fails_activation_validation(self):
        migrate_config(self.config, prepare_features=('ranking',))
        with closing(sqlite3.connect(self.config.jobs_db)) as con:
            objects = list(con.execute("SELECT type,name FROM sqlite_master WHERE name GLOB 'family_preparation_*' ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'index' THEN 1 ELSE 2 END"))
            for kind, name in objects:
                con.execute('DROP ' + kind.upper() + ' "' + name + '"')
        before = self.state()
        from job_search.schema_lifecycle import check_feature_schemas
        with self.assertRaisesRegex(RuntimeError, 'family preparation tracking mismatch'):
            check_feature_schemas(self.config, 'ranking')
        self.assertEqual(before, self.state())

    def test_uninstalled_salary_status_is_inactive_and_read_only_but_partial_schema_fails(self):
        before = self.state()
        self.assertIsNone(llm.status(self.config.jobs_db)['activated_at'])
        self.assertEqual(before, self.state())
        with closing(sqlite3.connect(self.config.jobs_db)) as con:
            con.execute('CREATE TABLE salary_llm_settings (key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)')
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'missing table'):
            llm.status(self.config.jobs_db)
        self.assertEqual(before, self.state())

    def test_missing_optional_paths_are_rejected_without_creating_files(self):
        for operation in (model.prepare_state, proxy.prepare_schema, ResumeLabStore, CareerStore):
            with self.subTest(operation=operation):
                missing = self.root / 'absent' / 'missing.db'
                with self.assertRaises(sqlite3.OperationalError):
                    operation(missing)
                self.assertFalse(missing.parent.exists())

    def test_optional_runtime_preparation_is_metadata_only(self):
        self.install_optional()
        before = self.state()
        labeler.prepare_preferences(self.config.jobs_db)
        model.prepare_state(self.config.preference_db)
        proxy.prepare_schema(self.config.proxy_db)
        teacher.prepare_schema(self.config.jobs_db)
        v3.prepare_schema(self.config.jobs_db)
        salary_labeler.prepare_validation(self.config.jobs_db)
        with closing(sqlite3.connect(self.config.jobs_db)) as con:
            enrichment.prepare_enrichment(con)
            llm.prepare_schema(con)
            locations.prepare_schema(con)
            dedupe_incremental.install(con)
        refresh_freshness.ensure_tracking(self.config.jobs_db, 'source')
        refresh_freshness.ensure_tracking(self.config.preference_db, 'scores')
        check_config(self.config)
        self.assertEqual(before, self.state())

    def test_configured_resume_is_explicitly_initialized_and_checks_do_not_backfill(self):
        config = replace(self.config, resume_lab_db=self.root / 'resume.db', resume_artifact_root=self.root / 'artifacts')
        migrate_config(config)
        before = self.state()
        ResumeLabStore(config.resume_lab_db)
        CareerStore(config.resume_lab_db)
        check_config(config, offline=True)
        self.assertEqual(before, self.state())
        with closing(sqlite3.connect(config.resume_lab_db)) as con, con:
            con.execute('DELETE FROM career_profile_state')
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'career profile state is missing'):
            CareerStore(config.resume_lab_db)
        self.assertEqual(before, self.state())
        migrate_config(config)
        check_config(config)

    def test_newer_resume_version_fails_without_rewriting(self):
        path = self.root / 'resume.db'
        with explicit_migration():
            ResumeLabStore(path)
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("INSERT INTO resume_lab_schema VALUES (99,'future')")
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'resume schema version mismatch'):
            ResumeLabStore(path)
        self.assertEqual(before, self.state())

    def test_removed_optional_anchor_is_still_detected_from_remaining_objects(self):
        self.install_optional()
        with closing(sqlite3.connect(self.config.jobs_db)) as con, con:
            con.execute('DROP TABLE salary_v3_queue')
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'missing table salary_v3_queue'):
            check_config(self.config)
        self.assertEqual(before, self.state())
        migrate_config(self.config)
        check_config(self.config)

    def test_legacy_ranking_migration_requires_explicit_maintenance(self):
        # A genuine old embedding layout, not just changed version metadata.
        with closing(sqlite3.connect(self.config.preference_db)) as con:
            con.executescript(model.STATE_SCHEMA.replace('vector_blob BLOB NOT NULL', 'vector_json TEXT NOT NULL'))
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'vector_blob'):
            model.prepare_state(self.config.preference_db)
        self.assertEqual(before, self.state())
        migrate_config(self.config)
        model.prepare_state(self.config.preference_db)

    def test_ranking_cleanup_runs_only_during_explicit_migration(self):
        with explicit_migration():
            model.prepare_state(self.config.preference_db)
        with closing(sqlite3.connect(self.config.preference_db)) as con, con:
            con.execute("INSERT INTO preference_feature_documents VALUES "
                        "('job','test','family','fingerprint','legacy title','legacy body','[]','','','now')")
        before = self.state()
        model.prepare_state(self.config.preference_db)
        self.assertEqual(before, self.state())
        migrate_config(self.config)
        with closing(sqlite3.connect(self.config.preference_db)) as con:
            row = con.execute('SELECT title_metadata_text,description_text FROM preference_feature_documents').fetchone()
        self.assertEqual(row, ('', ''))

    def test_tracking_drift_is_rejected_without_resetting_epoch_or_revisions(self):
        self.install_optional()
        with closing(sqlite3.connect(self.config.jobs_db)) as con, con:
            con.execute('DROP TRIGGER ranking_refresh_source_jobs_insert')
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'tracking mismatch'):
            refresh_freshness.ensure_tracking(self.config.jobs_db, 'source')
        with self.assertRaisesRegex(RuntimeError, 'tracking mismatch'):
            check_config(self.config)
        self.assertEqual(before, self.state())
        migrate_config(self.config)
        check_config(self.config)

    def test_family_rebuild_does_not_silently_create_persistent_schema(self):
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'missing table job_families'):
            dedupe.prepare_families(self.config.jobs_db)
        self.assertEqual(before, self.state())
        self.install_optional()
        # Ordinary derived-record writes remain allowed after metadata validation.
        result = dedupe.prepare_families(self.config.jobs_db)
        self.assertEqual(result['jobs'], 0)
        check_config(self.config)

    def test_existing_owner_mail_archive_is_checked_without_constructor_ddl(self):
        from job_search.mail.revision_archive import ImmutableMailArchive, SCHEMA
        with closing(sqlite3.connect(self.config.application_db)) as con:
            con.executescript(SCHEMA)
        before = self.state()
        with patch('job_search.mail.revision_archive.EncryptedMailArchive.__init__', return_value=None):
            ImmutableMailArchive(self.config.application_db, None)
        check_config(self.config)
        self.assertEqual(before, self.state())
        with closing(sqlite3.connect(self.config.application_db)) as con:
            con.execute('DROP TRIGGER owner_mail_archive_no_delete')
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'missing trigger'):
            check_config(self.config)
        self.assertEqual(before, self.state())


if __name__ == '__main__':
    unittest.main()
