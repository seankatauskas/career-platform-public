"""Offline checks for manual scans, durable replays, and passive ranking coverage."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
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


def test_progress_sql_coverage_preserves_orphan_and_duplicate_family_semantics():
    with progress_fixture() as config:
        with sqlite3.connect(config.jobs_db) as con:
            con.execute("INSERT INTO job_families VALUES ('orphan','a','missing')")
            con.execute("INSERT INTO job_families VALUES ('f1','a','1')")
        with sqlite3.connect(config.preference_db) as con:
            con.execute("INSERT INTO preference_scores VALUES ('s','orphan')")
            con.execute("INSERT INTO preference_scores VALUES ('s','f1')")
            con.execute("INSERT INTO preference_scores VALUES ('b','removed')")
        fingerprints = {path: path.read_bytes() for path in (config.jobs_db, config.preference_db, config.proxy_db)}
        report = ranking_progress(config)
        assert report['available'] and report['total_families'] == report['postings'] == 2
        for counts in report['policies'].values():
            assert counts['ranked_families'] == 2 and counts['unranked_families'] == 0
        assert all(path.read_bytes() == original for path, original in fingerprints.items())


def test_progress_reuses_complete_request_policy_status_and_rechecks_missing_or_partial_status():
    with progress_fixture() as config:
        complete = {'selective': {'status': 'ready'}, 'broad': {'status': 'stale'}}
        with patch('job_search.ranking.progress.inspect_policies', side_effect=AssertionError('repeated inspection')):
            report = ranking_progress(config, policy_status=complete)
        assert report['policies']['selective']['freshness'] == 'ready'
        assert report['policies']['broad']['freshness'] == 'stale'
        for incomplete in (None, {}, {'selective': {'status': 'ready'}}, {'selective': {}, 'broad': {}}):
            with patch('job_search.ranking.progress.inspect_policies', return_value=complete) as inspect:
                assert ranking_progress(config, policy_status=incomplete)['available']
            assert inspect.call_count == 1


@contextmanager
def progress_fixture():
    with fixture() as (config, store):
        with sqlite3.connect(config.jobs_db) as con:
            con.executescript("CREATE TABLE jobs(ats,id,last_seen); INSERT INTO jobs VALUES ('a','1','2026-09-30T12:00:00Z'),('a','2','2026-09-30T12:00:00Z'); CREATE TABLE job_families(family_id,canonical_ats,canonical_job_id); INSERT INTO job_families VALUES ('f1','a','1'),('f2','a','2');")
        with sqlite3.connect(config.proxy_db) as con:
            con.executescript("CREATE TABLE proxy_students(policy_id,model_run_id,run_id,trained_at); CREATE TABLE proxy_runs(run_id,created_at); INSERT INTO proxy_runs VALUES ('p','now'); INSERT INTO proxy_students VALUES ('selective','s','p','now'),('broad','b','p','now');")
        with sqlite3.connect(config.preference_db) as con:
            con.executescript("CREATE TABLE preference_scores(run_id,family_id); INSERT INTO preference_scores VALUES ('s','f1'),('s','f2'),('b','f1'),('b','f2'); CREATE TABLE preference_state(key PRIMARY KEY,value);")
        work = request_scan(config, store, 'progress-attempt')
        with connect(config.application_db) as con:
            # A retried work item retains its first start; job_runs tracks the active attempt.
            con.execute("UPDATE work_items SET task_kind='opportunity.preference_refresh',status='running',recovery_revision=3,started_at='2026-09-20T12:00:00Z'")
            con.execute("INSERT INTO job_runs(run_id,work_id,scheduled_for,started_at) VALUES ('attempt',?,'2026-09-20T12:00:00Z','2026-09-30T12:00:00Z')", (work['work_id'],))
        yield config


def save_progress(config, **changes):
    with connect(config.application_db) as con:
        work_id = con.execute("SELECT work_id FROM work_items").fetchone()[0]
    progress = {'status': 'scoring', 'started_at': '2026-09-30T12:00:01Z',
                'updated_at': '2026-09-30T12:00:02Z', 'checked_families': 1,
                'total_families': 2, 'reused': False, 'invocation_work_id': work_id, 'invocation_revision': 3,
                'policies': {'selective': {'recomputed_families': 0, 'reused_families': 1},
                             'broad': {'recomputed_families': 1, 'reused_families': 0}}}
    progress.update(changes)
    with sqlite3.connect(config.preference_db) as con:
        con.execute("INSERT OR REPLACE INTO preference_state VALUES ('policy_refresh_progress',?)", (json.dumps(progress),))


def test_progress_separates_checks_from_updates_and_never_writes():
    with progress_fixture() as config:
        save_progress(config)
        paths = [config.application_db, config.jobs_db, config.preference_db, config.proxy_db]
        before = {p: p.read_bytes() for p in paths}
        files = set(config.jobs_db.parent.iterdir())
        for _ in range(3):
            report = ranking_progress(config)
            assert report['available'] and report['state'] == 'running'
            assert report['policies']['selective']['unranked_families'] == 0
            current = report['current_pass']
            assert current['checked_families'] == 1 and current['total_families'] == 2
            assert current['policies']['selective'] == {'recomputed_families': 0, 'reused_families': 1}
            assert current['policies']['broad'] == {'recomputed_families': 1, 'reused_families': 0}
            assert report['last_pass'] is None
        assert {p: p.read_bytes() for p in paths} == before
        assert set(config.jobs_db.parent.iterdir()) == files


def test_progress_rejects_stale_attempts_and_invalid_journals_without_losing_coverage():
    with progress_fixture() as config:
        for changes in [
            {'started_at': '2026-09-20T12:00:01Z'},  # Same work item, earlier attempt.
            {'status': 'succeeded', 'started_at': '2026-09-20T12:00:01Z'},
            {'started_at': None}, {'updated_at': '2026-09-30T11:59:59Z'},
            {'updated_at': '2999-01-01T00:00:00Z'}, {'updated_at': 'invalid'},
            {'updated_at': '2026-09-30T12:00:02'}, {'status': 'failed'},
            {'sample': True}, {'checked_families': 3}, {'reused': True},
            {'invocation_work_id': 'different-work'}, {'invocation_revision': 2},
            {'invocation_revision': None}, {'invocation_revision': True},
        ]:
            save_progress(config, **changes)
            report = ranking_progress(config)
            assert report['available'] and report['current_pass'] is None, changes
        for raw in ['broken json', '[]', 'null']:
            with sqlite3.connect(config.preference_db) as con:
                con.execute("UPDATE preference_state SET value=?", (raw,))
            report = ranking_progress(config)
            assert report['available'] and report['current_pass'] is None
        save_progress(config, policies=['invalid'])
        assert ranking_progress(config)['current_pass']['policies'] == {}


def test_progress_requires_current_running_attempt_and_ignores_queued_journal():
    with progress_fixture() as config:
        save_progress(config)
        with connect(config.application_db) as con:
            con.execute("UPDATE work_items SET status='queued'")
        report = ranking_progress(config)
        assert report['state'] == 'queued' and report['current_pass'] is None and report['last_pass'] is None
        with connect(config.application_db) as con:
            con.execute("UPDATE work_items SET status='running'")
            con.execute("UPDATE job_runs SET completed_at='2026-09-30T12:00:03Z',outcome='succeeded'")
        assert ranking_progress(config)['current_pass'] is None
        with connect(config.application_db) as con:
            con.execute("DELETE FROM job_runs")
        assert ranking_progress(config)['current_pass'] is None


def test_completed_reuse_has_zero_scanned_families_and_only_matches_its_own_attempt():
    with progress_fixture() as config:
        save_progress(config, status='succeeded', checked_families=0, reused=True,
                      policies={p: {'recomputed_families': 0, 'reused_families': 2} for p in ('selective', 'broad')})
        with connect(config.application_db) as con:
            con.execute("UPDATE work_items SET status='succeeded',recovery_revision=4")
            con.execute("UPDATE job_runs SET completed_at='2026-09-30T12:00:03Z',outcome='succeeded'")
        report = ranking_progress(config)
        assert report['state'] == 'idle' and report['current_pass'] is None
        assert report['last_pass']['checked_families'] == 0 and report['last_pass']['reused'] is True
        assert report['last_pass']['policies']['selective']['reused_families'] == 2
        save_progress(config, status='succeeded', updated_at='2026-09-30T12:00:03.900000Z')
        assert ranking_progress(config)['last_pass'] is not None  # Worker stamp truncates fractions.
        save_progress(config, status='succeeded', updated_at='2026-09-30T12:00:04Z')
        assert ranking_progress(config)['last_pass'] is None  # Cannot belong to this completed attempt.
        with connect(config.application_db) as con:
            con.execute("UPDATE work_items SET status='running'")
            con.execute("UPDATE job_runs SET started_at='2026-09-30T12:00:05Z',completed_at=NULL,outcome=NULL")
        assert ranking_progress(config)['current_pass'] is None


def main():
    tests = [v for k,v in globals().items() if k.startswith('test_')]
    for test in tests: test()
    print(f'ok ({len(tests)} manual scan and progress tests)')


if __name__ == '__main__': main()
