"""Recovery contract tests, including actual child-process death during publication."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from job_search import aws_ops as ops
from job_search import maintenance
from job_search.operation_journal import Operation, read, require_idle, set_gate
from job_search.release_policy import check_transition, validate_policy, validate_evidence
from job_search.worker import Worker
from tests import test_job_search_aws_ops as ops_fixtures
from tests.test_job_search_automation import enqueue_work, make_db, NOW

ROOT = Path(__file__).resolve().parents[1]
REAL_DRAIN = ops.drain_workers
REAL_STOP_PROJECT = ops.stop_project
REAL_PROJECT_CONTAINERS = ops.project_containers


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ops_fixtures.OperationsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data, self.root = self.fixture.c, self.fixture.data, self.fixture.root

    def test_gate_blocks_missing_corrupt_and_wrong_initializer(self):
        path = self.data / 'maintenance/gate.json'
        with patch.dict(os.environ, {'JOB_SEARCH_MAINTENANCE_GATE': str(path)}, clear=False):
            with self.assertRaises(OSError): maintenance.require_start('core')
            self.assertTrue(maintenance.draining())
            set_gate(self.c, ['dashboard'], draining=True, initialize='expected')
            maintenance.require_start('dashboard')
            with self.assertRaises(RuntimeError): maintenance.require_start('hermes')
            with patch.dict(os.environ, {'JOB_SEARCH_INITIALIZE_OPERATION':'wrong'}):
                with self.assertRaises(RuntimeError): maintenance.require_start('initialize')
            with patch.dict(os.environ, {'JOB_SEARCH_INITIALIZE_OPERATION':'expected'}):
                maintenance.require_start('initialize')
            path.write_text('broken')
            self.assertTrue(maintenance.draining())

    def test_worker_finishes_one_task_and_does_not_claim_next(self):
        db = make_db(str(self.root))
        enqueue_work(db, 'first', 'fixture')
        enqueue_work(db, 'second', 'fixture')
        stop = threading.Event(); called = []
        def handler(payload, context):
            called.append(context.work_id); stop.set(); return {}
        worker = Worker(db, task_handlers={'fixture':handler}, max_work_per_tick=5, now_provider=lambda: NOW)
        result = worker.tick(now=NOW, should_stop=stop.is_set)
        self.assertEqual(len(called), 1)
        self.assertEqual(result['work']['succeeded'], 1)
        with ops.sqlite3.connect(db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM work_items WHERE status='queued'").fetchone()[0], 1)

    def test_drain_timeout_never_stops_or_kills_active_jobs(self):
        with patch.object(ops, 'compose') as compose:
            with self.assertRaisesRegex(ops.OpsError, 'deadline'):
                REAL_DRAIN(self.c, ['dashboard','core'], timeout=0)
        compose.assert_not_called()
        self.assertEqual(json.loads((self.data/'maintenance/gate.json').read_text())['allowed_services'], ['dashboard','core'])

    def test_journal_blocks_competing_mutations_and_is_idempotent(self):
        op = Operation.begin(self.c, 'backup', previous_release=ops.release_path(self.c).name)
        with self.assertRaisesRegex(ops.OpsError, op.id): require_idle(self.c)
        with self.assertRaises(ops.OpsError): Operation.begin(self.c, 'deploy')
        with patch.object(ops, 'stop_project'):
            result = ops.recover(self.c, op.id)
        self.assertEqual(result['status'], 'recovered_paused')
        with patch.object(ops, 'stop_project') as stop:
            self.assertEqual(ops.recover(self.c, op.id)['status'], 'already_complete')
            stop.assert_not_called()
        with self.assertRaises(ops.OpsError): ops.recover(self.c, '0'*32)
        require_idle(self.c)

    def test_status_distinguishes_held_maintenance_lock_from_interrupted_journal(self):
        op = Operation.begin(self.c, 'deploy', recovery_tool='/verified/candidate/scripts/job-search-ops')
        path = self.data / '.operations.lock'
        with patch.object(ops, 'preflight', return_value={'issues': []}), patch.object(ops, 'running_services', return_value=[]):
            result = ops.status(self.c)
            self.assertFalse(path.exists(), 'read-only status must not create a lock')
            self.assertTrue(result['recovery_required'])
            with patch.object(ops, 'verify_mount'), ops.lock(self.c):
                result = ops.status(self.c)
                self.assertEqual(result['status'], 'maintenance')
                self.assertTrue(result['maintenance_active'])
                self.assertFalse(result['recovery_required'])
                self.assertEqual(result['next_action'], 'wait_for_active_operation')
                self.assertFalse(read(self.c)['complete'])
            result = ops.status(self.c)
            self.assertTrue(result['recovery_required'])
            self.assertFalse(result['maintenance_active'])
            self.assertEqual(result['recovery_tool'], '/verified/candidate/scripts/job-search-ops')
            self.assertIn(op.id, result['next_action'])

    def test_status_lock_probe_never_follows_links_blocks_on_fifo_or_trusts_unsafe_permissions(self):
        path = self.data / '.operations.lock'
        outside = self.root / 'outside'; outside.write_text('untouched')
        path.symlink_to(outside)
        self.assertIsNone(ops.operation_lock_held(self.c))
        self.assertEqual(outside.read_text(), 'untouched')
        path.unlink(); os.mkfifo(path)
        self.assertIsNone(ops.operation_lock_held(self.c))
        path.unlink(); path.write_text(''); path.chmod(0o666)
        self.assertIsNone(ops.operation_lock_held(self.c))
        op = Operation.begin(self.c, 'deploy')
        with patch.object(ops, 'preflight', return_value={'issues': []}), patch.object(ops, 'running_services', return_value=[]):
            result = ops.status(self.c)
        self.assertTrue(result['recovery_required'])
        self.assertFalse(result['maintenance_active'])
        self.assertTrue(result['operation_lock_unverified'])
        self.assertEqual(read(self.c)['operation_id'], op.id)

    def test_postwrite_recovery_never_rewinds_database(self):
        op = Operation.begin(self.c, 'deploy', previous_release=ops.release_path(self.c).name)
        op.update('resuming', writes_possible=True, backup={'backup_id':'old','sha256':'a'*64})
        (self.data/'state/new.txt').write_text('new work')
        with patch.object(ops, 'stop_project'), patch.object(ops, 'restore_unlocked') as restore:
            ops.recover(self.c, op.id)
        restore.assert_not_called()
        self.assertEqual((self.data/'state/new.txt').read_text(), 'new work')
        self.assertEqual(json.loads((self.data/'maintenance/gate.json').read_text())['allowed_services'], [])

    def test_recovery_stops_one_shot_initializers_and_verifies_exit(self):
        for sticky in (False, True):
            stopped = False
            def command(argv, **kwargs):
                nonlocal stopped
                if argv[:2] == ['docker', 'stop']:
                    stopped = True
                    return ''
                if 'label=com.docker.compose.project=job-search' in argv:
                    return 'abc123def456' if sticky or not stopped else ''
                if 'label=org.career-platform.review.worker=true' in argv:
                    return ''
                raise AssertionError('unexpected recovery command')
            with patch.object(ops, 'project_containers', REAL_PROJECT_CONTAINERS), patch.object(ops, 'run', side_effect=command) as run:
                if sticky:
                    with self.assertRaisesRegex(ops.OpsError, 'still has writers'):
                        REAL_STOP_PROJECT(self.c)
                else:
                    REAL_STOP_PROJECT(self.c)
                calls = [call.args[0] for call in run.call_args_list]
                self.assertIn(['docker', 'stop', '--time', '4200', 'abc123def456'], calls)
                self.assertTrue(any('label=com.docker.compose.project=job-search' in call for call in calls))
                self.assertTrue(any('label=org.career-platform.review.worker=true' in call for call in calls))

    def test_restore_recovers_after_sigkill_at_each_directory_boundary(self):
        # Each trial is independent; child death bypasses every finally/except block.
        for directory in ops.BACKUP_DIRS:
            for boundary in ('save','publish'):
                with self.subTest(directory=directory, boundary=boundary):
                    fixture = ops_fixtures.OperationsTests(); fixture.setUp()
                    try:
                        fixture.original_data(); bundle = fixture.bundle()
                        payload = {'c':fixture.c, 'bundle':str(bundle), 'sha':ops.digest(bundle), 'name':directory, 'boundary':boundary}
                        code = '''
import json,os,signal,sys
from pathlib import Path
from job_search import aws_ops as ops
p=json.loads(sys.stdin.read()); original=ops.os.replace
ops.running_services=lambda c: []
ops.materialize_secrets=lambda *a,**k: None
ops.chown_runtime=lambda c: None
def replace(source,destination):
    original(source,destination)
    source,destination=Path(source),Path(destination)
    hit=(destination.name==p['name'] and ((p['boundary']=='save' and destination.parent.name.endswith('-saved')) or (p['boundary']=='publish' and source.parent.name.startswith('restore-stage-'))))
    if hit: os.kill(os.getpid(),signal.SIGKILL)
ops.os.replace=replace
ops.restore_unlocked(p['c'],Path(p['bundle']),p['sha'],replace=True)
'''
                        result = subprocess.run([sys.executable,'-c',code], input=json.dumps(payload), text=True, capture_output=True, cwd=ROOT, timeout=20)
                        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
                        operation = read(fixture.c)
                        self.assertFalse(operation['complete'])
                        with patch.object(ops, 'stop_project'), patch.object(ops, 'materialize_secrets'), patch.object(ops, 'chown_runtime'):
                            ops.recover(fixture.c, operation['operation_id'])
                        fixture.assert_original_data()
                        self.assertFalse(json.loads((fixture.data/'activation.json').read_text())['enabled'])
                    finally:
                        fixture.doCleanups()

    def test_deployment_sigkill_recovery_obeys_write_boundary(self):
        for boundary in ('prepared', 'snapshotting', 'snapshot_published', 'snapshotted', 'initializing', 'release_pointer', 'secrets', 'after_write'):
            with self.subTest(boundary=boundary):
                fixture = ops_fixtures.OperationsTests(); fixture.setUp()
                try:
                    fixture.original_data(); fixture.install_candidate()
                    (fixture.data/'materialized-secrets.json').write_text('{}')
                    payload = {'c':fixture.c, 'boundary':boundary}
                    code = """
import json,os,signal,sys
from pathlib import Path
from job_search import aws_ops as ops
p=json.loads(sys.stdin.read()); active=True
ops.preflight=lambda c: {'issues':[]}
ops.chown_runtime=lambda c: None
ops.wait_healthy=lambda *a: None
ops.model_readiness=lambda c: None
ops.drain_workers=lambda *a: None
ops.running_services=lambda c: ['dashboard'] if active else []
ops.project_containers=lambda: ['abc123def456'] if active else []
def kill(): os.kill(os.getpid(),signal.SIGKILL)
def compose(c,*args,**kwargs):
    global active
    if args[0]=='stop': active=False
    if args[0]=='run' and 'initialize' in args:
        (Path(c['data_root'])/'state/candidate.txt').write_text('migration')
    if args[0]=='up' and 'dashboard' in args:
        active=True
        (Path(c['data_root'])/'state/user-after.txt').write_text('new work')
        if p['boundary']=='after_write': kill()
    return ''
ops.compose=compose
update=ops.Operation.update
def checkpoint(self,phase,**fields):
    update(self,phase,**fields)
    if phase==p['boundary']: kill()
ops.Operation.update=checkpoint
replace=ops.os.replace
def publish(source,destination):
    replace(source,destination)
    if p['boundary']=='snapshot_published' and str(destination).endswith('.snapshot'): kill()
ops.os.replace=publish
point=ops.point_current
def point_current(c,path):
    point(c,path)
    if p['boundary']=='release_pointer': kill()
ops.point_current=point_current
materialize=ops.materialize_secrets
def secrets(*args,**kwargs):
    materialize(*args,**kwargs)
    if p['boundary']=='secrets': kill()
ops.materialize_secrets=secrets
ops.deploy(p['c'],'b'*40+'-2')
"""
                    result=subprocess.run([sys.executable,'-c',code],input=json.dumps(payload),text=True,capture_output=True,cwd=ROOT,timeout=20)
                    self.assertEqual(result.returncode,-signal.SIGKILL,result.stderr)
                    record=read(fixture.c)
                    with patch.object(ops,'stop_project'), patch.object(ops,'running_services',return_value=[]), patch.object(ops,'chown_runtime'):
                        ops.recover(fixture.c,record['operation_id'])
                    if boundary=='after_write':
                        self.assertEqual((fixture.data/'state/user-after.txt').read_text(),'new work')
                        self.assertEqual(ops.release_path(fixture.c).name,'b'*40+'-2')
                    else:
                        self.assertFalse((fixture.data/'state/candidate.txt').exists())
                        self.assertEqual(ops.release_path(fixture.c).name,'a'*40+'-1')
                    self.assertFalse(json.loads((fixture.data/'activation.json').read_text())['enabled'])
                finally:
                    fixture.doCleanups()

    def test_directory_restore_recovers_after_sigkill_at_each_publication_boundary(self):
        for directory in ops.BACKUP_DIRS:
            for boundary in ('save', 'publish'):
                with self.subTest(directory=directory, boundary=boundary):
                    fixture = ops_fixtures.OperationsTests(); fixture.setUp()
                    try:
                        receipt, snapshot = fixture.local_snapshot()
                        for name in ops.BACKUP_DIRS:
                            (fixture.data / name / 'original.txt').write_text('current ' + name)
                        payload = {'c': fixture.c, 'snapshot': str(snapshot), 'sha': receipt['sha256'],
                                   'name': directory, 'boundary': boundary}
                        code = '''
import json,os,signal,sys
from pathlib import Path
from job_search import aws_ops as ops
p=json.loads(sys.stdin.read()); original=ops.os.replace
ops.running_services=lambda c: []
ops.materialize_secrets=lambda *a,**k: None
ops.chown_runtime=lambda c: None
def replace(source,destination):
    original(source,destination)
    source,destination=Path(source),Path(destination)
    hit=(destination.name==p['name'] and ((p['boundary']=='save' and destination.parent.name.endswith('-saved')) or (p['boundary']=='publish' and source.parent.name.startswith('restore-stage-'))))
    if hit: os.kill(os.getpid(),signal.SIGKILL)
ops.os.replace=replace
ops.restore_unlocked(p['c'],Path(p['snapshot']),p['sha'],replace=True)
'''
                        result = subprocess.run([sys.executable, '-c', code], input=json.dumps(payload),
                                                text=True, capture_output=True, cwd=ROOT, timeout=20)
                        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
                        record = read(fixture.c)
                        with patch.object(ops, 'stop_project'), patch.object(ops, 'materialize_secrets'):
                            ops.recover(fixture.c, record['operation_id'])
                        for name in ops.BACKUP_DIRS:
                            self.assertEqual((fixture.data / name / 'original.txt').read_text(), 'current ' + name)
                        ops.verify_snapshot(snapshot)
                    finally: fixture.doCleanups()

    def test_restore_after_committed_publication_keeps_restored_data(self):
        self.fixture.original_data(); bundle = self.fixture.bundle()
        with patch.object(ops,'running_services',return_value=[]), patch.object(ops,'materialize_secrets'), patch.object(ops,'chown_runtime'):
            op = Operation.begin(self.c,'restore',target_release=ops.release_path(self.c).name)
            ops.restore_unlocked(self.c,bundle,ops.digest(bundle),replace=True,_operation=op)
            with patch.object(ops,'stop_project'):
                ops.recover(self.c,op.id)
        self.assertEqual((self.data/'state/restored.txt').read_text(),'new state')

    def test_backup_retry_schedule_is_bounded_and_first_failure_alerts(self):
        now = datetime(2026,9,29,8,tzinfo=timezone.utc)
        self.c['notification_topic_arn']='fixture-topic'
        with patch.object(ops,'backup_unlocked',side_effect=ops.OpsError('failed')) as backup, patch.object(ops,'aws') as aws:
            for attempt in range(4):
                with self.assertRaises(ops.OpsError): ops.scheduled_backup(self.c,now=now+timedelta(minutes=30*attempt))
                self.assertEqual(ops.scheduled_backup(self.c,now=now+timedelta(minutes=30*attempt+1))['status'],'scheduled_wait')
            self.assertEqual(backup.call_count,4)
            self.assertEqual(aws.call_count,1)
            self.assertEqual(ops.scheduled_backup(self.c,now=now+timedelta(hours=12))['status'],'scheduled_wait')
        with patch.object(ops,'backup_unlocked',return_value={'status':'backed_up'}) as backup:
            ops.scheduled_backup(self.c,now=now+timedelta(days=1))
            ops.scheduled_backup(self.c,now=now+timedelta(days=1,hours=1))
            self.assertEqual(backup.call_count,1)

    def test_insufficient_capacity_refuses_before_stopping_production(self):
        self.fixture.install_candidate()
        from types import SimpleNamespace
        with patch.object(ops,'compose'), patch.object(ops,'preflight',return_value={'issues':[]}), patch.object(ops.shutil,'disk_usage',return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ops.OpsError,'disk space'):
                ops.deploy(self.c,'b'*40+'-2')
        self.assertIsNone(read(self.c))

    def test_transition_requires_exact_predecessor_and_tested_rollback(self):
        target = self.fixture.install_candidate()
        old, new = ops.manifest(ops.release_path(self.c)), ops.manifest(target)
        check_transition(new,old)
        check_transition(old,new,rollback=True)
        new['transition_validation']['rollback_passed']=False
        with self.assertRaisesRegex(ops.OpsError,'rollback edge'): check_transition(old,new,rollback=True)
        new['release_policy']['predecessor']['release_id']='a'*40+'-99'
        with self.assertRaisesRegex(ops.OpsError,'differs'): check_transition(new,old)

    def test_local_or_dirty_evidence_cannot_authorize_a_release(self):
        target=self.fixture.install_candidate(); release=ops.manifest(target)
        for change in ({'runtime':'python'}, {'working_tree_dirty':True}, {'baseline_sha':'f'*40}, {'passed':False}):
            with self.assertRaises(ValueError):
                validate_evidence(release['release_policy'],release['source_sha'],{**release['transition_validation'],**change})

    def test_reinstalling_completed_release_is_read_only(self):
        with patch.object(ops,'compose') as compose:
            result=ops.deploy(self.c,'a'*40+'-1')
        self.assertTrue(result['already_installed'])
        self.assertIsNone(read(self.c))
        compose.assert_not_called()

    def test_unknown_ssm_result_never_reports_success(self):
        spec=importlib.util.spec_from_file_location('release_summary', ROOT/'scripts/release-summary.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        env={'COMMAND_ID':'fixture','JOB_STATUS':'success'}
        self.assertEqual(module.summary(env)['deployment_status'],'unknown')
        self.assertEqual(module.summary(env,{'Status':'Success','StandardOutputContent':'not json'})['deployment_status'],'unknown')
        for malformed in ('[]', 'null', '42'):
            self.assertEqual(module.summary(env,{'Status':'Success','StandardOutputContent':malformed})['deployment_status'],'unknown')
        self.assertEqual(module.summary(env,{'Status':'Success','StandardOutputContent':'{"status":"deployed_paused"}'})['deployment_status'],'deployed_paused')


if __name__ == '__main__':
    unittest.main()
