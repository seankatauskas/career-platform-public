"""Ownership cutover fences real legacy writes and preserves operational queues."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from job_search.application_installation import freeze_legacy, require_backend, readiness
from job_search.application_migration import convert_snapshot, CANDIDATE_NAME
from job_search.application_runtime import ApplicationRuntime
from job_search.commands import DomainError
from job_search.db import connect
from job_search.runtime import RuntimeConfigV1
from tests.test_job_search_ledger import make_service, start, context, stamp

class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path,self.ledger=make_service(self.tmp.name)
        self.app=start(self.ledger)['application']['application_id']
        self.report=convert_snapshot(self.path,self.root/'converted')
        self.runtime=ApplicationRuntime(self.root/'converted'/CANDIDATE_NAME)
        self.config=replace(RuntimeConfigV1.defaults(self.root),application_db=self.path,
            application_backend='owners',application_owner_db=self.runtime.executor.path)

    def test_freeze_blocks_legacy_but_keeps_operational_storage(self):
        with self.assertRaises(DomainError):require_backend(self.config)
        freeze_legacy(self.path,self.runtime,operator='operator',report=self.report)
        require_backend(self.config)
        with self.assertRaises(DomainError):require_backend(replace(self.config,application_backend='legacy'))
        with self.assertRaises(sqlite3.IntegrityError):
            with connect(self.path) as con:
                con.execute("UPDATE applications SET updated_at='changed'")
        with connect(self.path) as con:
            con.execute("INSERT INTO browser_pairings VALUES('code','audience',1234567890)")
        self.assertTrue(readiness(self.config,self.runtime)['activation']['paused'])

    def test_stale_conversion_is_rejected_without_partial_fence(self):
        self.ledger.record_submission(self.app,stamp(),context('new-submission'))
        with self.assertRaises(DomainError):freeze_legacy(self.path,self.runtime,operator='operator',report=self.report)
        require_backend(replace(self.config,application_backend='legacy'))

    def test_activation_is_durable_versioned_and_not_business_command(self):
        executor=self.runtime.executor
        self.assertEqual(executor.activation_status(),{'paused':True,'revision':0})
        executor.set_activation(paused=False,expected_revision=0,operator='operator',reason='verified staging')
        self.assertEqual(ApplicationRuntime(executor.path).executor.activation_status(),{'paused':False,'revision':1})
        with self.assertRaises(DomainError):executor.set_activation(paused=True,expected_revision=0,operator='operator',reason='stale')
        executor.set_activation(paused=True,expected_revision=1,operator='operator',reason='stop')
        self.assertTrue(executor.activation_status()['paused'])

    def test_owner_database_must_be_distinct(self):
        with self.assertRaises(ValueError):replace(self.config,application_owner_db=self.path).validate()

    def test_snapshot_restore_preserves_archive_bytes_and_pauses_execution(self):
        from job_search.aws_ops import copy_snapshot, prepare_application_restore
        import hashlib
        executor=self.runtime.executor
        executor.set_activation(paused=False,expected_revision=0,operator='operator',reason='test installation')
        target=self.root/'restore'/'state'/'converted'
        copy_snapshot(self.root/'converted',target)
        original=(self.root/'converted'/'predecessor.sqlite').read_bytes()
        self.assertEqual((target/'predecessor.sqlite').read_bytes(),original)
        self.assertEqual((target/'predecessor.sqlite').stat().st_mode & 0o777,0o400)
        prepare_application_restore(self.root/'restore')
        restored=ApplicationRuntime(target/CANDIDATE_NAME)
        self.assertTrue(restored.executor.activation_status()['paused'])
        self.assertEqual(restored.executor.activation_status()['revision'],2)
        self.assertTrue(restored.executor.restore_status()['required'])
        with self.assertRaises(DomainError):
            restored.executor.set_activation(paused=False,expected_revision=2,operator='operator',reason='unreviewed restore')
        restored.executor.acknowledge_restore(expected_restore_revision=2,operator='operator',reason='reviewed snapshot and provider history')
        self.assertFalse(restored.executor.restore_status()['required'])
        self.assertTrue(restored.executor.activation_status()['paused'])
        self.assertEqual((target/'predecessor.sqlite').read_bytes(),original)

if __name__=='__main__':unittest.main()
