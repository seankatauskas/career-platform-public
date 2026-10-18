"""Queue isolation, predecessor compatibility, and shared limits without live I/O."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search import cloud
from job_search.db import connect
from job_search.inference.usage import begin_invocation
from job_search.runtime import RuntimeConfigV1
from job_search.scheduler import utc_stamp, workflow_health
from job_search.worker import Worker, acquire_worker_lease, release_worker_lease, worker_lease_name
from tests.test_job_search_automation import NOW, make_db


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = make_db(self.temp.name)
        self.enqueue('existing-root', 'ats.authoritative', status='succeeded')
        with connect(self.db) as con:
            con.execute("INSERT INTO workflow_runs(workflow_id,workflow_kind,root_work_id,trigger_task_kind,scheduled_for,status,started_at) "
                        "VALUES('existing-flow','opportunity_refresh','existing-root','ats.authoritative',?,'running',?)",
                        (utc_stamp(NOW), utc_stamp(NOW)))

    def enqueue(self, identifier, kind, lane='core', **extra):
        with connect(self.db) as con:
            con.execute(
                "INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,priority,due_at,"
                "attempts,max_attempts,created_at,lane,workflow_id) VALUES(?,?,?,'{}','queued',0,?,0,3,?,?,'existing-flow')",
                (identifier, kind, 'existing:' + identifier, utc_stamp(NOW), utc_stamp(NOW), lane),
            )
            if extra:
                con.execute('UPDATE work_items SET ' + ','.join(key + '=?' for key in extra) + ' WHERE work_id=?',
                            (*extra.values(), identifier))

    def worker(self, **options):
        return Worker(self.db, now_provider=lambda: NOW, max_outbox_per_tick=0, **options)

    def row(self, identifier):
        with connect(self.db) as con:
            return dict(con.execute('SELECT * FROM work_items WHERE work_id=?', (identifier,)).fetchone())

    def test_mail_and_briefing_progress_while_ats_and_ranking_are_blocked(self):
        with connect(self.db) as con:
            con.execute("UPDATE automation_controls SET enabled=1 WHERE capability='briefing_ai'")
        for lane, batch_kind, interactive_kinds in (
            ('core', 'ats.authoritative', ('applications.mail.dispatch', 'applications.briefing.tick')),
            ('model', 'opportunity.preference_refresh', ('applications.mail.understand', 'applications.briefing.compose')),
        ):
            with self.subTest(lane=lane):
                started, finish = threading.Event(), threading.Event()
                self.enqueue(lane + '-batch', batch_kind, lane)
                for number, kind in enumerate(interactive_kinds):
                    self.enqueue(lane + str(number), kind, lane)
                before = self.row(lane + '0')

                def blocked(_payload, context):
                    self.assertTrue(context.heartbeat())
                    started.set()
                    self.assertTrue(finish.wait(10), 'fixture did not release batch worker')
                    self.assertTrue(context.heartbeat())
                    return {}

                batch = self.worker(lane=lane, partition='batch', task_handlers={batch_kind: blocked})
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(batch.tick)
                    try:
                        self.assertTrue(started.wait(5))
                        # Exact partition lease exclusion and unpartitioned CLI exclusion.
                        self.assertFalse(self.worker(lane=lane, partition='batch').tick()['acquired'])
                        self.assertFalse(self.worker(lane=lane).tick()['acquired'])
                        handled = []
                        handlers = {kind: lambda _p, c: handled.append(c.task_kind) or {} for kind in interactive_kinds}
                        result = self.worker(lane=lane, partition='interactive', task_handlers=handlers).tick()
                        self.assertEqual(result['work']['succeeded'], 2, [self.row(lane + str(i)) for i in range(2)])
                        self.assertCountEqual(handled, interactive_kinds)
                        self.assertFalse(future.done())
                        after = self.row(lane + '0')
                        for key in ('work_id', 'lane', 'dedupe_key', 'workflow_id', 'created_at'):
                            self.assertEqual(before[key], after[key])
                        self.assertEqual(after['attempts'], 1)
                        leases = workflow_health(self.db, NOW)['lanes'][lane]['worker_partitions']
                        self.assertIsNotNone(leases['batch'])
                    finally:
                        finish.set()
                    self.assertEqual(future.result()['work']['succeeded'], 1)
                with connect(self.db) as con:
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM worker_leases').fetchone()[0], 0)

    def test_partition_guard_excludes_the_actual_predecessor_acquisition_protocol(self):
        batch = acquire_worker_lease(self.db, owner='batch', now=NOW, partition='batch')
        interactive = acquire_worker_lease(self.db, owner='interactive', now=NOW, partition='interactive')
        self.assertTrue(batch and interactive)
        # Predecessor binaries know only this row: an active guard must be present
        # even after batch exits while interactive continues.
        release_worker_lease(self.db, batch, partition='batch')
        with connect(self.db) as con:
            con.execute('BEGIN IMMEDIATE')
            previous = con.execute('SELECT * FROM worker_leases WHERE lease_name=?', (worker_lease_name('core'),)).fetchone()
            self.assertGreater(previous['expires_at'], utc_stamp(NOW))
            con.rollback()
        self.assertIsNone(acquire_worker_lease(self.db, owner='all', now=NOW))
        release_worker_lease(self.db, interactive, partition='interactive')
        legacy = acquire_worker_lease(self.db, owner='all', now=NOW)
        self.assertTrue(legacy)
        for partition in ('batch', 'interactive'):
            self.assertIsNone(acquire_worker_lease(self.db, owner=partition, now=NOW, partition=partition))
        release_worker_lease(self.db, legacy)
        self.assertTrue(acquire_worker_lease(self.db, owner='rollback', now=NOW))

    def test_wrong_token_cannot_release_or_extend_another_partition(self):
        first = acquire_worker_lease(self.db, owner='a', now=NOW, partition='interactive')
        worker = self.worker(partition='interactive')
        worker._worker_token = first
        self.assertTrue(worker._heartbeat())
        release_worker_lease(self.db, 'wrong-token', partition='interactive')
        self.assertIsNone(acquire_worker_lease(self.db, owner='b', now=NOW, partition='interactive'))
        next_time = NOW + timedelta(minutes=10)
        second = acquire_worker_lease(self.db, owner='b', now=next_time, partition='interactive')
        self.assertTrue(second)
        self.assertFalse(worker._heartbeat())
        release_worker_lease(self.db, first, partition='interactive')
        self.assertIsNone(acquire_worker_lease(self.db, owner='c', now=next_time))
        release_worker_lease(self.db, second, partition='interactive')
        self.assertTrue(acquire_worker_lease(self.db, owner='c', now=next_time))

    def test_expired_partition_cannot_steal_a_live_unpartitioned_lease(self):
        batch = acquire_worker_lease(self.db, owner='batch', now=NOW, partition='batch')
        interactive = acquire_worker_lease(self.db, owner='interactive', now=NOW, partition='interactive')
        later = NOW + timedelta(minutes=10)
        legacy = acquire_worker_lease(self.db, owner='legacy', now=later)
        self.assertTrue(legacy)
        delayed = Worker(self.db, partition='interactive', now_provider=lambda: later)
        delayed._worker_token = interactive
        self.assertFalse(delayed._heartbeat())
        # Releasing one stale partition while its stale sibling remains must
        # not replace or delete the new all-worker ownership token either.
        release_worker_lease(self.db, batch, partition='batch')
        with connect(self.db) as con:
            self.assertEqual(con.execute('SELECT token FROM worker_leases WHERE lease_name=?',
                                         (worker_lease_name('core'),)).fetchone()[0], legacy)
        release_worker_lease(self.db, interactive, partition='interactive')
        self.assertIsNone(acquire_worker_lease(self.db, owner='new', now=later, partition='batch'))
        release_worker_lease(self.db, legacy)

    def test_disabled_work_and_backoff_remain_ineligible_in_both_partitions(self):
        self.enqueue('salary', 'opportunity.salary_drain', 'model')
        self.enqueue('mail', 'applications.mail.understand', 'model')
        self.enqueue('later', 'applications.briefing.compose', 'model', due_at=utc_stamp(NOW + timedelta(hours=1)))
        with connect(self.db) as con:
            for capability in ('salary', 'mail'):
                con.execute('INSERT INTO automation_controls VALUES (?,0,1,?)', (capability, utc_stamp(NOW)))
        for partition in ('batch', 'interactive'):
            report = self.worker(lane='model', partition=partition).tick()
            self.assertEqual(report['work'], {'succeeded': 0, 'retried': 0, 'dead': 0})
            self.assertFalse(report['more_due'])
        self.assertTrue(all(self.row(name)['attempts'] == 0 for name in ('salary', 'mail', 'later')))

    def test_expired_predecessor_work_is_recovered_by_its_partition_once(self):
        for identifier, kind in (('batch', 'ats.new_only'), ('mail', 'applications.mail.dispatch')):
            self.enqueue(identifier, kind, status='running', attempts=1, lease_owner='old', lease_token='old',
                         lease_expires_at=utc_stamp(NOW - timedelta(seconds=1)))
        handlers = {'ats.new_only': lambda *_: {}, 'applications.mail.dispatch': lambda *_: {}}
        report = self.worker(partition='interactive', task_handlers=handlers).tick()
        self.assertEqual(report['recovered']['work'], 1)
        self.assertEqual(self.row('batch')['status'], 'running')
        self.assertEqual(self.row('mail')['attempts'], 2)
        report = self.worker(partition='batch', task_handlers=handlers).tick()
        self.assertEqual(report['recovered']['work'], 1)
        self.assertEqual(self.row('batch')['attempts'], 2)
        self.assertEqual(self.worker(task_handlers=handlers).tick()['work']['succeeded'], 0)

    def test_inference_limit_is_shared_across_concurrent_partitions(self):
        self.enqueue('batch', 'opportunity.salary_drain', 'model')
        self.enqueue('mail', 'applications.mail.understand', 'model')
        started, finish = threading.Event(), threading.Event()
        policy = {'max_inflight': 1, 'daily_tokens': 200}

        def reserve(_payload, context):
            invocation = begin_invocation('fixture', 'generation', context.work_id.encode(), reserved_tokens=60)
            invocation.submitting()
            if context.work_id == 'batch':
                started.set()
                self.assertTrue(finish.wait(10))
            invocation.terminal('completed')
            return {}

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.worker(lane='model', partition='batch', task_handlers={'opportunity.salary_drain': reserve},
                                             inference_usage_limits=policy).tick)
            try:
                self.assertTrue(started.wait(5))
                result = self.worker(lane='model', partition='interactive', task_handlers={'applications.mail.understand': reserve},
                                     inference_usage_limits=policy).tick()
                self.assertEqual(result['work']['retried'], 1)
                self.assertEqual(self.row('mail')['attempts'], 0)
                with connect(self.db) as con:
                    self.assertEqual(con.execute('SELECT COUNT(*),SUM(reserved_tokens) FROM inference_invocations').fetchone()[:], (1, 60))
            finally:
                finish.set()
            self.assertEqual(future.result()['work']['succeeded'], 1)
        policy['daily_tokens'] = 100
        # Even after the in-flight slot is released, the daily token allowance
        # still belongs to both workers, so it cannot be doubled by partitioning.
        with connect(self.db) as con:
            con.execute('UPDATE work_items SET due_at=? WHERE work_id=?', (utc_stamp(NOW), 'mail'))
        result = self.worker(lane='model', partition='interactive', task_handlers={'applications.mail.understand': reserve},
                             inference_usage_limits=policy).tick()
        self.assertEqual(result['work']['retried'], 1)
        self.assertEqual(self.row('mail')['attempts'], 0)

    def test_shutdown_finishes_current_work_without_claiming_next(self):
        for identifier in ('a', 'b'):
            self.enqueue(identifier, 'applications.mail.dispatch')
        stop = threading.Event()
        def handler(_payload, context):
            stop.set()
            self.assertTrue(context.heartbeat())
            return {}
        report = self.worker(partition='interactive', task_handlers={'applications.mail.dispatch': handler}).tick(should_stop=stop.is_set)
        self.assertEqual(report['work']['succeeded'], 1)
        self.assertEqual(self.row('b')['status'], 'queued')
        with connect(self.db) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM worker_leases').fetchone()[0], 0)

    def test_interactive_loop_reports_its_own_health_and_drain_identity(self):
        stop = threading.Event()
        calls = []
        def factory(_config, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(tick=lambda **_: {'acquired': True})
        def emit(_):
            stop.set()
        code = cloud.run_worker_loop(RuntimeConfigV1.defaults(self.root), lane='model', partition='interactive',
                                     interval_seconds=15, stop=stop, health_dir=self.root / 'health',
                                     runtime_factory=factory, emit=emit, now_provider=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]['partition'], 'interactive')
        path = self.root / 'health' / 'worker-interactive-model.json'
        self.assertEqual(json.loads(path.read_text())['lane'], 'interactive-model')
        cloud._write_health(path, cloud._worker_health_record('interactive-model', 'drained', NOW))
        self.assertEqual(cloud.main(['draincheck', 'interactive-model', '--health-dir', str(path.parent)]), 0)
        stop.clear()
        calls.clear()
        # A maintenance gate acknowledges drain without entering another tick.
        class DrainStop:
            def is_set(self): return stop.is_set()
            def set(self): stop.set()
            def wait(self, _seconds):
                self.assert_drained = json.loads(path.read_text())['state']
                stop.set()
        drain_stop = DrainStop()
        with patch('job_search.cloud.draining', return_value=True):
            cloud.run_worker_loop(RuntimeConfigV1.defaults(self.root), lane='model', partition='interactive',
                                  stop=drain_stop, health_dir=path.parent, runtime_factory=factory,
                                  now_provider=lambda: NOW, emit=lambda _: self.fail('claimed while draining'))
        self.assertEqual(drain_stop.assert_drained, 'drained')


if __name__ == '__main__':
    unittest.main()
