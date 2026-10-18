"""Actual strict startup must preserve schema, data and post-upgrade writes."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.fixtures.release_transition import main, startup_probe, startup_snapshot, diagnostics_probe, verify_application_owners, snapshot, assert_preserved


class StartupTransitionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        with patch('sys.argv', ['fixture', 'seed', str(self.root)]), redirect_stdout(io.StringIO()):
            main()
        startup_probe('startup-prepare', self.root)

    def test_actual_startup_and_roundtrip_preserve_new_records(self):
        before = startup_snapshot(self.root)
        self.assertTrue(startup_probe('startup-check', self.root)['startup_read_only'])
        self.assertEqual(before, startup_snapshot(self.root))
        startup_probe('startup-write', self.root)
        startup_probe('startup-rollback', self.root)
        data = startup_snapshot(self.root)['databases']['data/applications.db']
        self.assertIn('strict-startup-write', json.dumps(data))
        self.assertIn('strict-rollback-write', json.dumps(data))

    def test_diagnostics_preserve_records_schema_and_candidate_writes(self):
        startup_probe('startup-write', self.root)
        before = startup_snapshot(self.root)
        result = diagnostics_probe('diagnostics-check', self.root)
        self.assertTrue(result['diagnostics_read_only'])
        self.assertEqual(len(result['scenarios']), 12)
        self.assertEqual(before, startup_snapshot(self.root))
        startup_probe('startup-rollback', self.root)
        verify_application_owners(self.root)
        result = diagnostics_probe('diagnostics-owners-check', self.root)
        self.assertTrue(result['diagnostics_read_only'])
        self.assertEqual(result['backend'], 'owners')

    def test_diagnostics_mutation_cannot_earn_read_only_evidence(self):
        from job_search.runtime_readiness import runtime_readiness
        def mutating_report(config, **kwargs):
            result = runtime_readiness(config, **kwargs)
            with sqlite3.connect(config.jobs_db) as con:
                con.execute('CREATE INDEX IF NOT EXISTS hidden_diagnostics_change ON jobs(title)')
            return result
        with patch('job_search.runtime_readiness.runtime_readiness', side_effect=mutating_report):
            with self.assertRaisesRegex(AssertionError, 'diagnostics changed persisted state'):
                diagnostics_probe('diagnostics-check', self.root)

    def test_verified_source_signature_reseal_only_allowed_during_explicit_preparation(self):
        from job_search.collection import dedupe_incremental
        from job_search.collection.dedupe import prepare_families
        before = snapshot(self.root)
        with patch.object(dedupe_incremental, 'signature', return_value='f' * 64):
            prepare_families(self.root / 'data/jobs.db')
            assert_preserved(self.root, before, allow_preparation=True)
            with self.assertRaisesRegex(AssertionError, 'persisted row lost'):
                assert_preserved(self.root, before)
            with sqlite3.connect(self.root / 'data/jobs.db') as con:
                con.execute("UPDATE family_preparation_state SET signature='unverified'")
            with self.assertRaisesRegex(AssertionError, 'not trustworthy'):
                assert_preserved(self.root, before, allow_preparation=True)

    def test_valid_reseal_cannot_hide_changed_business_rows(self):
        from job_search.collection.dedupe import prepare_families
        before = snapshot(self.root)
        with sqlite3.connect(self.root / 'data/applications.db') as con:
            con.execute("UPDATE work_items SET payload_json='{}'")
            con.execute("UPDATE work_items SET attempts=attempts+1")
        prepare_families(self.root / 'data/jobs.db')
        with self.assertRaisesRegex(AssertionError, 'persisted row lost'):
            assert_preserved(self.root, before, allow_preparation=True)

    def test_schema_only_startup_mutation_cannot_earn_read_only_evidence(self):
        from job_search.runtime import build_runtime
        changed = False
        def mutating_constructor(config, **kwargs):
            nonlocal changed
            value = build_runtime(config, **kwargs)
            if not changed:
                con = sqlite3.connect(config.jobs_db)
                try:
                    con.execute('CREATE INDEX hidden_startup_change ON jobs(title)')
                    con.execute('PRAGMA user_version=999')
                    con.commit()
                finally:
                    con.close()
                changed = True
            return value
        with patch('job_search.runtime.build_runtime', side_effect=mutating_constructor):
            with self.assertRaisesRegex(AssertionError, 'startup changed persisted state'):
                startup_probe('startup-check', self.root)


if __name__ == '__main__':
    unittest.main()
