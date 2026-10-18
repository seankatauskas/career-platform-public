"""Capture, validation and service boundaries with real independent SQLite bytes."""
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from job_search import aws_ops as ops
from tests import test_job_search_aws_ops as fixtures

REAL_DRAIN = ops.drain_workers


class SnapshotPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OperationsTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data, self.root = self.fixture.c, self.fixture.data, self.fixture.root
        ops.write_json(self.data / 'materialized-secrets.json', {})
        self.databases = [self.data / 'state/catalog.db', self.data / 'state/application.db']
        for path in self.databases:
            db = sqlite3.connect(path)
            try:
                db.execute('CREATE TABLE state(value INTEGER, payload BLOB)')
                db.execute('INSERT INTO state VALUES(1, ?)', (bytes(1100000),))
                db.commit()
            finally: db.close()

    def update_sources(self, value):
        for path in self.databases:
            db = sqlite3.connect(path)
            try:
                db.execute('UPDATE state SET value=?', (value,)); db.commit()
            finally: db.close()

    def test_routine_backup_resumes_before_checks_and_keeps_cross_database_capture(self):
        events = []; checked = []; check = ops.sqlite_check
        def compose(c, *args, **kwargs):
            if args[0] == 'up':
                events.append('resumed')
                self.update_sources(2)
            return ''
        def verify(path):
            self.assertIn('resumed', events)
            self.assertNotIn('uploaded', events)
            checked.append(path.name)
            check(path)
        def upload(c, *args):
            if str(args[2]).endswith('backup.tar.gz'):
                self.assertEqual(set(checked), {'catalog.db', 'application.db'})
                restored = self.root / 'restored'
                restored.mkdir()
                ops.safe_extract(Path(args[2]), restored)
                # Restore performs its own complete validation after capture.
                with patch.object(ops, 'sqlite_check', check): manifest = ops.verify_snapshot(restored)
                for profile in manifest['capture_files'].values():
                    self.assertNotIn('sqlite_check', profile['paused_timings_seconds'])
                    self.assertIn('sqlite_check', profile['postresume_timings_seconds'])
                    self.assertEqual(profile['validation'], 'checked_after_resume')
                for source in self.databases:
                    con = sqlite3.connect(restored / 'state' / source.name)
                    try: self.assertEqual(con.execute('SELECT value FROM state').fetchone()[0], 1)
                    finally: con.close()
                events.append('uploaded')
            return ''
        with patch.object(ops, 'running_services', return_value=['dashboard']), \
                patch.object(ops, 'compose', side_effect=compose), \
                patch.object(ops, 'sqlite_check', side_effect=verify), \
                patch.object(ops, 'aws', side_effect=upload):
            result = ops.backup_unlocked(self.c)
        self.assertEqual(events, ['resumed', 'uploaded'])
        self.assertEqual(result['status'], 'backed_up')
        self.assertTrue((self.data / 'last-backup.json').exists())

    def test_failed_postresume_validation_keeps_previous_receipt_and_uploads_nothing(self):
        previous = b'{"backup_id":"previous-valid"}\n'
        (self.data / 'last-backup.json').write_bytes(previous)
        def reject(path):
            self.assertTrue(ops.read_operation(self.c)['complete'])
            self.assertEqual(ops.read_operation(self.c)['phase'], 'snapshot_finished')
            raise sqlite3.DatabaseError('corrupt captured database')
        with patch.object(ops, 'running_services', return_value=['dashboard']), \
                patch.object(ops, 'compose', return_value='') as compose, \
                patch.object(ops, 'sqlite_check', side_effect=reject), patch.object(ops, 'aws') as upload:
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'corrupt captured'):
                ops.backup_unlocked(self.c)
        upload.assert_not_called()
        self.assertEqual((self.data / 'last-backup.json').read_bytes(), previous)
        self.assertEqual(compose.call_args.args[1], 'up')
        self.assertFalse(list((self.data / 'backups').iterdir()))

    def test_sigkill_during_postresume_validation_never_publishes_a_backup(self):
        previous = b'{"backup_id":"previous-valid"}\n'
        (self.data / 'last-backup.json').write_bytes(previous)
        code = """
import json,os,signal,sys
from pathlib import Path
from job_search import aws_ops as ops
c=json.loads(sys.stdin.read())
ops.running_services=lambda c: ['dashboard']
ops.drain_workers=lambda *a: None
ops.project_containers=lambda: []
ops.wait_healthy=lambda *a: None
def compose(c,*args,**kw):
    if args[0]=='up': (Path(c['data_root'])/'resumed').write_text('yes')
    return ''
ops.compose=compose
def check(path):
    assert (Path(c['data_root'])/'resumed').exists()
    assert ops.read_operation(c)['complete']
    os.kill(os.getpid(),signal.SIGKILL)
ops.sqlite_check=check
ops.aws=lambda *a: (_ for _ in ()).throw(AssertionError('must not upload'))
ops.backup_unlocked(c)
"""
        child = subprocess.run([sys.executable, '-c', code], input=json.dumps(self.c),
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        self.assertEqual((self.data / 'last-backup.json').read_bytes(), previous)
        self.assertTrue(ops.read_operation(self.c)['complete'])
        self.assertFalse(list((self.data / 'backups').glob('*.tar.gz')))
        self.assertFalse(list((self.data / 'backups').glob('*.snapshot')))

    def test_real_corrupt_capture_is_rejected_after_services_resume(self):
        previous = b'{"backup_id":"previous-valid"}\n'
        (self.data / 'last-backup.json').write_bytes(previous)
        def compose(c, *args, **kwargs):
            if args[0] == 'up':
                copied = next((self.data / 'backups').glob('backup-*/payload/state/catalog.db'))
                with copied.open('r+b') as stream:
                    stream.seek(100); stream.write(b'\xff' * 64)
            return ''
        with patch.object(ops, 'running_services', return_value=['dashboard']), \
                patch.object(ops, 'compose', side_effect=compose), patch.object(ops, 'aws') as upload:
            with self.assertRaises((sqlite3.DatabaseError, ops.OpsError)):
                ops.backup_unlocked(self.c)
        upload.assert_not_called()
        self.assertEqual((self.data / 'last-backup.json').read_bytes(), previous)
        self.assertTrue(ops.read_operation(self.c)['complete'])
        for source in self.databases: ops.sqlite_check(source)

    def test_deployment_prepares_after_last_worker_write_before_dashboard_stop(self):
        target = self.fixture.install_candidate(); events = []
        prepare = ops.prepare_snapshot
        def drain(c, active):
            events.append('drained')
            self.update_sources(2)
        @contextmanager
        def prepared(*args, **kwargs):
            self.assertEqual(events, ['drained'])
            events.append('prepared')
            with prepare(*args, **kwargs) as value: yield value
        def compose(c, *args, **kwargs):
            if args[0] == 'stop' and 'dashboard' in args: events.append('stopped')
            return ''
        with patch.object(ops, 'running_services', return_value=['core', 'model', 'dashboard']), \
                patch.object(ops, 'drain_workers', side_effect=drain), \
                patch.object(ops, 'prepare_snapshot', side_effect=prepared), \
                patch.object(ops, 'compose', side_effect=compose), \
                patch.object(ops, 'preflight', return_value={'issues': []}), \
                patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'):
            result = ops.deploy(self.c, target.name)
        self.assertEqual(events, ['drained', 'prepared', 'stopped'])
        self.assertEqual(result['backup']['reuse']['reused_files'], 2)
        self.assertNotIn('sqlite_check', result['backup']['timings_seconds'])
        self.assertIn('snapshot_preparation', result['timings_seconds'])
        captured = ops.verify_snapshot(ops.local_backup_path(self.c, result['backup']))
        for profile in captured['capture_files'].values():
            self.assertEqual(profile['capture'], 'reused')
            self.assertIn('source_hash', profile['paused_timings_seconds'])
            self.assertIn('reuse_publish', profile['paused_timings_seconds'])
            self.assertNotIn('sqlite_check', profile['paused_timings_seconds'])

    def test_preparation_failure_restarts_workers_without_stopping_dashboard(self):
        target = self.fixture.install_candidate()
        with patch.object(ops, 'running_services', return_value=['core', 'dashboard']), \
                patch.object(ops, 'prepare_snapshot', side_effect=OSError('disk unavailable')), \
                patch.object(ops, 'compose', return_value='') as compose, \
                patch.object(ops, 'preflight', return_value={'issues': []}):
            with self.assertRaisesRegex(OSError, 'disk unavailable'): ops.deploy(self.c, target.name)
        self.assertFalse(any('stop' in call.args for call in compose.call_args_list))
        ups = [call.args[1:] for call in compose.call_args_list if call.args[1] == 'up']
        self.assertEqual(ups, [('up', '-d', '--no-deps', '--no-build', 'core')])
        self.assertEqual(ops.read_operation(self.c)['phase'], 'preparation_aborted')
        self.assertEqual(ops.release_path(self.c).name, 'a' * 40 + '-1')

    def test_all_worker_partitions_drain_before_stopping(self):
        active = ['dashboard', 'core', 'model', 'interactive-core', 'interactive-model']
        with patch.object(ops, 'compose', return_value='') as compose:
            REAL_DRAIN(self.c, active)
        identities = [call.args[-1] for call in compose.call_args_list if 'draincheck' in call.args]
        self.assertEqual(set(identities), set(active) - {'dashboard'})
        self.assertEqual(compose.call_args.args[1:], ('stop', '--timeout', '30', *active[1:]))
        gate = json.loads((self.data / 'maintenance/gate.json').read_text())
        self.assertEqual(gate['allowed_services'], ['dashboard'])

    def test_new_partition_services_start_on_upgrade_but_not_old_release(self):
        old = ops.release_path(self.c); target = self.fixture.install_candidate()
        (target / 'compose.cloud.yaml').write_text('services:\n  interactive-core:\n  interactive-model:\n')
        self.assertNotIn('interactive-core', ops.configured_services(self.c, release=old))
        self.assertIn('interactive-core', ops.configured_services(self.c, release=target))
        self.assertNotIn('interactive-core', ops.configured_services(self.c, release=target, review=True))
        with patch.object(ops, 'running_services', return_value=['dashboard', 'core', 'model']), \
                patch.object(ops, 'compose', return_value='') as compose, \
                patch.object(ops, 'preflight', return_value={'issues': []}), \
                patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'):
            ops.deploy(self.c, target.name)
        resume = [call.args for call in compose.call_args_list if 'up' in call.args and 'dashboard' in call.args][-1]
        self.assertIn('interactive-core', resume); self.assertIn('interactive-model', resume)

    def test_deployment_reuses_fresh_backup_validation_after_catalog_changes(self):
        target = self.fixture.install_candidate()
        with ops.prepare_snapshot(self.c) as prepared:
            receipt = ops.local_snapshot_unlocked(self.c, prepared=prepared)
        prior = ops.Operation.begin(self.c, 'deploy', backup=receipt,
                                    target_release=ops.release_path(self.c).name)
        prior.finish('deployed')
        self.update_sources(2)  # The previous deployment proof no longer matches.
        with patch.object(ops, 'running_services', return_value=['dashboard']), \
                patch.object(ops, 'compose', return_value=''), patch.object(ops, 'aws', return_value=''), \
                patch.object(ops, 'sqlite_check', wraps=ops.sqlite_check) as checks:
            ops.backup_unlocked(self.c)
        self.assertEqual(checks.call_count, 2)  # Routine backup still checks both databases.
        self.assertEqual(ops.read_operation(self.c)['kind'], 'backup')
        prepare = ops.prepare_snapshot
        @contextmanager
        def preparation(c, **kwargs):
            self.assertEqual(ops.read_operation(c)['phase'], 'preparing_snapshot')
            self.assertEqual(kwargs['verification_reference'][1], 'available_backup')
            with prepare(c, **kwargs) as prepared:
                self.assertTrue(all(p['validation'] == 'reused' for p in prepared['file_profiles'].values()))
                yield prepared
        with patch.object(ops, 'running_services', return_value=['dashboard']), \
                patch.object(ops, 'compose', return_value=''), \
                patch.object(ops, 'prepare_snapshot', side_effect=preparation), \
                patch.object(ops, 'preflight', return_value={'issues': []}), \
                patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'):
            result = ops.deploy(self.c, target.name)
        self.assertEqual(result['backup']['preparation_validation']['reused'], 2)

    def test_explicit_rollback_removes_new_worker_partitions(self):
        old = ops.release_path(self.c); current = self.fixture.install_candidate()
        (current / 'compose.cloud.yaml').write_text('services:\n  interactive-core:\n  interactive-model:\n')
        ops.point_current(self.c, current)
        with patch.object(ops, 'running_services', return_value=['dashboard', *sorted(ops.WORKER_SERVICES)]), \
                patch.object(ops, 'compose', return_value='') as compose, \
                patch.object(ops, 'preflight', return_value={'issues': []}), \
                patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'):
            ops.deploy(self.c, old.name, rollback=True)
        resume = [call.args for call in compose.call_args_list if 'up' in call.args and 'dashboard' in call.args][-1]
        self.assertNotIn('interactive-core', resume); self.assertNotIn('interactive-model', resume)
        self.assertEqual(set(ops.read_operation(self.c)['resumed_services']), {'dashboard', 'core', 'model'})

    def test_new_worker_health_failure_stops_target_workers_and_recovery_preserves_writes(self):
        target = self.fixture.install_candidate()
        (target / 'compose.cloud.yaml').write_text('services:\n  interactive-core:\n  interactive-model:\n')
        def health(c, expected):
            if 'interactive-model' in expected:
                (self.data / 'state/after-start.txt').write_text('new write')
                raise ops.OpsError('new worker unhealthy')
        with patch.object(ops, 'running_services', side_effect=[['dashboard', 'core', 'model'], []]), \
                patch.object(ops, 'compose', return_value='') as compose, \
                patch.object(ops, 'preflight', return_value={'issues': []}), \
                patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'), \
                patch.object(ops, 'wait_healthy', side_effect=health), patch.object(ops, 'restore_unlocked') as restore:
            with self.assertRaisesRegex(ops.OpsError, 'new worker unhealthy'): ops.deploy(self.c, target.name)
            restore.assert_not_called()
        self.assertIn('interactive-core', compose.call_args.args)
        self.assertIn('interactive-model', compose.call_args.args)
        operation = ops.read_operation(self.c)
        self.assertTrue(operation['writes_possible'])
        self.assertIn('interactive-model', operation['resumed_services'])
        self.assertNotIn('interactive-model', operation['active_services'])
        with patch.object(ops, 'stop_project') as stop, patch.object(ops, 'restore_unlocked') as restore:
            ops.recover(self.c, operation['operation_id'])
        stop.assert_called_once(); restore.assert_not_called()
        self.assertEqual((self.data / 'state/after-start.txt').read_text(), 'new write')
        self.assertEqual(ops.release_path(self.c), target)


if __name__ == '__main__': unittest.main()
