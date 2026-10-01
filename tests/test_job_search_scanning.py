"""Offline checks for manual scans, durable replays, and passive ranking coverage."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.db import connect
from job_search.ranking.progress import ranking_progress
from job_search.runtime import RuntimeConfigV1
from job_search.scanning import request_scan, scan_status
from job_search.scheduler import seed_default_schedules, materialize_due_schedules
from job_search.store import LedgerStore
from job_search.worker import Worker, TaskResult, FollowUpTask


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        boards = root / "boards.json"
        boards.write_text('{}')
        config = replace(RuntimeConfigV1.defaults(root), application_db=root/'app.db',
                         jobs_db=root/'jobs.db', preference_db=root/'ranking.db', proxy_db=root/'proxy.db',
                         board_registry_path=boards, scraper_contact='collector@company.dev')
        store = LedgerStore(config.application_db)
        seed_default_schedules(config.application_db, datetime.now(timezone.utc), config.environment({}))
        yield config, store


def test_concurrent_requests_coalesce_and_replay_after_completion():
    with fixture() as (config, store):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: request_scan(config, store, f'scan-{i}'), range(8)))
        assert len({r['work_id'] for r in results}) == 1
        assert sum(not r['coalesced'] for r in results) == 1
        with connect(config.application_db) as con:
            assert con.execute('SELECT COUNT(*) FROM workflow_runs').fetchone()[0] == 1
            con.execute("UPDATE work_items SET status='succeeded'")
        for i, result in enumerate(results):
            assert request_scan(config, store, f'scan-{i}') == result
        assert request_scan(config, store, 'next-scan')['work_id'] != results[0]['work_id']


def test_manual_scan_joins_scheduled_scan_and_preserves_schedule():
    with fixture() as (config, store):
        with connect(config.application_db) as con:
            due = con.execute("SELECT next_due_at FROM schedule_specs WHERE task_kind='ats.new_only' ORDER BY next_due_at LIMIT 1").fetchone()[0]
        materialize_due_schedules(config.application_db, datetime.fromisoformat(due.replace('Z','+00:00')))
        before = scan_status(config)['next_scan_at']
        result = request_scan(config, store, 'join-scheduled')
        assert result['coalesced']
        assert scan_status(config)['next_scan_at'] == before


def test_scan_blocks_paused_maintenance_and_unbounded_boards():
    with fixture() as (config, store):
        for changed, maintenance in [(replace(config, board_registry_path=None), False), (config, True)]:
            with patch('job_search.scanning.draining', return_value=maintenance):
                try: request_scan(changed, store, 'blocked')
                except ContractError: pass
                else: raise AssertionError('unsafe scan was accepted')
        with connect(config.application_db) as con:
            con.execute("INSERT INTO automation_controls VALUES ('collection',0,0,'now')")
        assert not scan_status(config)['available']
        try: request_scan(config, store, 'paused')
        except ContractError: pass
        else: raise AssertionError('paused scan was accepted')
        with connect(config.application_db) as con:
            assert con.execute('SELECT COUNT(*) FROM work_items').fetchone()[0] == 0


def test_scan_uses_normal_worker_and_keeps_workflow_lineage():
    with fixture() as (config, store):
        result = request_scan(config, store, 'pipeline')
        def collect(payload, context):
            assert payload['source'] == 'dashboard'
            return TaskResult({'collected': 1}, (FollowUpTask('opportunity.location_refresh', {},
                workflow_id=context.workflow_id, parent_work_id=context.work_id),))
        worker = Worker(config.application_db, task_handlers={'ats.new_only': collect}, max_work_per_tick=1)
        assert worker.tick()['work']['succeeded'] == 1
        with connect(config.application_db) as con:
            child = con.execute("SELECT * FROM work_items WHERE task_kind='opportunity.location_refresh'").fetchone()
            assert child['workflow_id'] == result['workflow_id'] and child['parent_work_id'] == result['work_id']
            assert con.execute("SELECT watermark_key FROM workflow_watermarks").fetchone()[0] == 'ats_ingested'


def test_progress_counts_current_families_per_policy_and_handles_missing_state():
    with fixture() as (config, store):
        assert not ranking_progress(config)['available']
        assert not config.jobs_db.exists()  # Reading never initializes missing databases.
        with sqlite3.connect(config.jobs_db) as con:
            con.executescript("CREATE TABLE jobs(ats,id,last_seen); INSERT INTO jobs VALUES ('a','1','2026-09-30T12:00:00Z'),('a','2','2026-09-30T12:00:00Z'); CREATE TABLE job_families(family_id,canonical_ats,canonical_job_id); INSERT INTO job_families VALUES ('f1','a','1'),('f2','a','2');")
        with sqlite3.connect(config.proxy_db) as con:
            con.executescript("CREATE TABLE proxy_students(policy_id,model_run_id,run_id,trained_at); CREATE TABLE proxy_runs(run_id,created_at); INSERT INTO proxy_runs VALUES ('p','now'); INSERT INTO proxy_students VALUES ('selective','s','p','now'),('broad','b','p','now');")
        with sqlite3.connect(config.preference_db) as con:
            con.executescript("CREATE TABLE preference_scores(run_id,family_id); INSERT INTO preference_scores VALUES ('s','f1'),('s','removed'),('b','f1'),('b','f2'); CREATE TABLE preference_state(key,value);")
        report = ranking_progress(config)
        assert report['available'] and report['total_families'] == 2 and report['postings'] == 2
        assert report['policies']['selective']['ranked_families'] == 1
        assert report['policies']['selective']['unranked_families'] == 1
        assert report['policies']['broad']['ranked_families'] == 2
        request_scan(config, store, 'state-test')
        with connect(config.application_db) as con:
            con.execute("UPDATE work_items SET task_kind='opportunity.preference_refresh',failure_kind='usage_deferred',due_at='2026-10-01T00:00:00Z'")
        assert ranking_progress(config)['state'] == 'waiting_allowance'
        with connect(config.application_db) as con:
            con.execute("UPDATE work_items SET status='running'")
        assert ranking_progress(config)['state'] == 'running'  # Old failure metadata is not current state.
        with connect(config.application_db) as con:
            con.execute("UPDATE work_items SET status='succeeded'")
        assert ranking_progress(config)['state'] == 'idle'


def main():
    tests = [v for k,v in globals().items() if k.startswith('test_')]
    for test in tests: test()
    print(f'ok ({len(tests)} manual scan and progress tests)')


if __name__ == '__main__': main()
