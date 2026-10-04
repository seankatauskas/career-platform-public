"""Offline regressions for bounded catch-up and claim eligibility."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.cloud import run_worker_loop
from job_search.contracts import JobSnapshot, MutationContext, RecommendationProvenance
from job_search.db import connect
from job_search.runtime import RuntimeConfigV1
from job_search.scheduler import utc_stamp
from job_search.service import JobSearchLedger
from job_search.worker import Worker, RetryableTaskError, acquire_worker_lease
from tests.test_job_search_automation import NOW, enqueue_work, make_db


class Clock:
    def __init__(self):
        self.seconds = 0
        self.stopped = False
        self.on_wait = lambda: None

    def now(self):
        return NOW + timedelta(seconds=self.seconds)

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.seconds += seconds
        self.on_wait()
        return self.stopped


class CatchUpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = make_db(self.temp.name)
        self.clock = Clock()

    def worker(self, **kwargs):
        return Worker(self.db, now_provider=self.clock.now, **kwargs)

    def loop(self, worker, *, stop_after=None, once=False, emit_hook=lambda: None):
        reports, starts = [], []

        def tick(**kwargs):
            starts.append(self.clock.seconds)
            return worker.tick(**kwargs)

        def emit(raw):
            reports.append(json.loads(raw)['report'])
            emit_hook()
            if (stop_after and len(reports) >= stop_after) or (not stop_after and not reports[-1].get('more_due')):
                self.clock.set()

        with patch('job_search.cloud.time.monotonic', lambda: self.clock.seconds):
            code = run_worker_loop(
                RuntimeConfigV1.defaults(self.root), lane=worker.lane,
                max_work=worker.max_work_per_tick, max_outbox=worker.max_outbox_per_tick,
                health_dir=self.root / 'health', stop=self.clock, once=once,
                runtime_factory=lambda *_args, **_kwargs: SimpleNamespace(tick=tick),
                now_provider=self.clock.now, emit=emit,
            )
        self.assertEqual(code, 0)
        return starts, reports

    def test_shortlist_runs_in_next_bounded_batch_without_five_minute_wait(self):
        handled = []
        for index in range(10):
            enqueue_work(self.db, f'recurring-{index}', 'recurring')
        for index in range(3):
            enqueue_work(self.db, f'shortlist-{index}', 'notification.shortlist_evaluate')
        with connect(self.db) as con:
            con.execute("UPDATE work_items SET priority=80 WHERE task_kind='recurring'")
        handler = lambda _payload, context: handled.append(context.task_kind) or {}
        worker = self.worker(task_handlers={'recurring': handler, 'notification.shortlist_evaluate': handler},
                             max_work_per_tick=10, max_outbox_per_tick=0)
        starts, reports = self.loop(worker)
        self.assertEqual(starts, [0, 1])
        self.assertEqual([r['work']['succeeded'] for r in reports], [10, 3])
        self.assertEqual(handled, ['recurring'] * 10 + ['notification.shortlist_evaluate'] * 3)
        with connect(self.db) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM worker_leases').fetchone()[0], 0)

    def test_sustained_backlog_returns_to_normal_interval_after_three_extra_batches(self):
        for index in range(60):
            enqueue_work(self.db, f'work-{index}', 'test')
        starts, reports = self.loop(self.worker(task_handlers={'test': lambda *_: {}}, max_outbox_per_tick=0), stop_after=5)
        self.assertEqual(starts, [0, 1, 2, 3, 303])
        self.assertTrue(all(r['work']['succeeded'] == 10 for r in reports))

    def test_ineligible_work_does_not_trigger_catch_up(self):
        enqueue_work(self.db, 'future', 'test', due=NOW + timedelta(hours=1))
        enqueue_work(self.db, 'paused', 'opportunity.salary_drain')
        enqueue_work(self.db, 'deferred', 'test.deferred')
        with connect(self.db) as con:
            con.execute("INSERT INTO automation_controls VALUES ('salary',0,1,?)", (utc_stamp(NOW),))
            con.execute("INSERT INTO work_items (work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,created_at,lane) "
                        "VALUES ('other-lane','test.other','other-lane','{}','queued',?,3,?,'model')", (utc_stamp(NOW), utc_stamp(NOW)))
        worker = self.worker(deferred_task_kinds=('test.deferred',), max_outbox_per_tick=0)
        starts, reports = self.loop(worker, stop_after=2)
        self.assertEqual(starts, [0, 300])
        self.assertTrue(all(not r['more_due'] for r in reports))
        with connect(self.db) as con:
            self.assertEqual(con.execute('SELECT SUM(attempts) FROM work_items').fetchone()[0], 0)

    def test_retry_backoff_and_zero_work_budget_do_not_trigger_catch_up(self):
        enqueue_work(self.db, 'retry', 'test')

        def retry(*_):
            raise RetryableTaskError('later', retry_after_seconds=600)

        worker = self.worker(task_handlers={'test': retry}, max_outbox_per_tick=0)
        report = worker.tick(now=NOW)
        self.assertEqual(report['work']['retried'], 1)
        self.assertFalse(report['more_due'])
        enqueue_work(self.db, 'due', 'test')
        self.assertFalse(self.worker(max_work_per_tick=0, max_outbox_per_tick=0).tick(now=NOW)['more_due'])

    def test_outbox_catch_up_respects_lane_budget_and_delivery_due_time(self):
        service = JobSearchLedger(self.db)
        for index in range(2):
            started = service.start_application(
                JobSnapshot('ashby', f'job-{index}', f'family-{index}', 'Engineer', 'Acme', 'acme',
                            f'https://example.test/job-{index}'),
                RecommendationProvenance(), MutationContext(f'start-{index}', 'user', 'dashboard'),
            )
            service.record_submission(started['application']['application_id'], utc_stamp(NOW),
                                      MutationContext(f'submit-{index}', 'user', 'dashboard'))
        future = NOW + timedelta(days=3650)
        worker = self.worker(max_work_per_tick=0, max_outbox_per_tick=1,
                             outbox_handlers={'recommendation.applied': lambda *_: {}})
        self.assertFalse(self.worker(lane='model', max_work_per_tick=0).tick(now=future)['more_due'])
        self.assertFalse(self.worker(max_work_per_tick=0, max_outbox_per_tick=0).tick(now=future)['more_due'])
        first = worker.tick(now=future)
        self.assertEqual(first['outbox']['delivered'], 1)
        self.assertTrue(first['more_due'])
        with connect(self.db) as con:
            con.execute("UPDATE outbox_messages SET available_at=? WHERE status='pending'", (utc_stamp(future + timedelta(hours=1)),))
        self.assertFalse(worker.tick(now=future)['more_due'])
        self.assertFalse(worker.tick(now=future + timedelta(hours=2))['more_due'])

    def test_competing_lease_does_not_trigger_a_busy_loop(self):
        enqueue_work(self.db, 'due', 'test')
        acquire_worker_lease(self.db, owner='other', now=NOW, lease_seconds=1000)
        starts, reports = self.loop(self.worker(max_outbox_per_tick=0), stop_after=2)
        self.assertEqual(starts, [0, 300])
        self.assertTrue(all(not r['acquired'] for r in reports))

    def test_once_and_shutdown_never_run_an_extra_batch(self):
        for index in range(3):
            enqueue_work(self.db, f'due-{index}', 'test')
        worker = self.worker(task_handlers={'test': lambda *_: {}}, max_work_per_tick=1, max_outbox_per_tick=0)
        starts, _ = self.loop(worker, once=True)
        self.assertEqual(starts, [0])
        self.clock.stopped = False
        starts, _ = self.loop(worker, emit_hook=self.clock.set)
        self.assertEqual(starts, [0])

    def test_maintenance_interrupts_catch_up_before_next_claim(self):
        for index in range(3):
            enqueue_work(self.db, f'due-{index}', 'test')
        worker = self.worker(task_handlers={'test': lambda *_: {}}, max_work_per_tick=1, max_outbox_per_tick=0)
        maintenance = [False]
        self.clock.on_wait = self.clock.set
        with patch('job_search.cloud.draining', lambda: maintenance[0]):
            starts, _ = self.loop(worker, emit_hook=lambda: maintenance.__setitem__(0, True))
        self.assertEqual(starts, [0])
        with connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM work_items WHERE status='queued'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
