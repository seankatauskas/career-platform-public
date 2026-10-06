"""Offline publishing, persistence, and dashboard integration tests."""
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from job_search.contracts import ContractError
from job_search.curated import CuratedShortlists
from job_search.curated_client import publish
from job_search.dashboard import DashboardController, make_server
from job_search.hermes import HermesAdapter
from job_search.hermes_mcp import make_mcp_server
from job_search.integration import LocalJobCatalog
from job_search.preference import PreferenceGateway, PreferencePaths
from job_search.runtime import RuntimeConfigV1
from job_search.service import JobSearchLedger
from tests.test_job_search_dashboard import FakePreferences, post, request, session
from tests.test_job_search_hermes_runtime import minimal_adapter, mcp_request, TOKEN


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class CuratedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs.db'
        with closing(sqlite3.connect(self.jobs)) as con, con:
            con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,title TEXT,company TEXT,description TEXT,jobUrl TEXT,posted_at TEXT,source_updated_at TEXT,closed_at TEXT,PRIMARY KEY(ats,id))')
            con.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)', [('ashby', str(i), f'Role {i}', 'Example', 'Python production systems', f'https://example.test/{i}', '2026-09-28T00:00:00Z', None, None) for i in range(1, 502)])
        self.ledger = JobSearchLedger(self.root / 'ledger.db')
        self.catalog = LocalJobCatalog(self.jobs)
        self.saved = CuratedShortlists(self.ledger.store.db_path, self.catalog)
        self.payload = {'title': 'Daily picks', 'idempotency_key': 'daily-1', 'jobs': [
            {'ats': 'ashby', 'job_id': '2', 'explanation': '<script>untrusted text</script>'},
            {'ats': 'ashby', 'job_id': '1', 'explanation': 'Strong Python fit'}]}
        self.controller = DashboardController(self.ledger, FakePreferences(), jobs=self.catalog)

    def test_atomic_validation_and_retry(self):
        invalids = [dict(self.payload, jobs=self.payload['jobs'] + [{'ats': 'ashby', 'job_id': 'missing'}]),
                    dict(self.payload, jobs=[self.payload['jobs'][0]] * 2),
                    dict(self.payload, jobs=[{'ats': 'external', 'job_id': '1'}]),
                    dict(self.payload, title=''), dict(self.payload, jobs='1'),
                    dict(self.payload, window_start='2026-09-28T00:00:00Z'),
                    dict(self.payload, window_start='2026-09-28T00:00:00Z', window_end='2026-09-27T00:00:00Z'),
                    dict(self.payload, jobs=[{'ats': 'ashby', 'job_id': '1', 'explanation': 'x' * 2001}]),
                    dict(self.payload, jobs=[{'ats': 'ashby', 'job_id': str(i)} for i in range(1, 502)])]
        for payload in invalids:
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                self.saved.publish(payload)
            self.assertEqual(self.saved.lists()['lists'], [])
        receipt = self.saved.publish(self.payload)
        with patch.object(self.catalog, 'get_job', side_effect=AssertionError('replay must not re-resolve')):
            self.assertEqual(self.saved.publish(self.payload), receipt)
        with self.assertRaisesRegex(ContractError, 'reused'):
            self.saved.publish(dict(self.payload, title='Changed'))
        with closing(sqlite3.connect(self.ledger.store.db_path)) as con, con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM curated_shortlist_items').fetchone()[0], 2)
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE curated_shortlists SET title='Changed'")

    def test_review_cards_use_batched_summaries_and_keep_legacy_explanations(self):
        receipt = self.saved.publish(self.payload)
        legacy = self.controller.curated_list(receipt['list_id'])
        self.assertTrue(all('review_summary' not in job for job in legacy['recommendations']))
        summaries = {('ashby', '2'): {'decision': 'close', 'alignment': 'core',
                                    'gaps': ['Production scale'], 'unknowns': ['Eligibility unknown']}}
        reviews = Mock()
        reviews.publication_ordinals.return_value = {('ashby', '2'): 7}
        reviews.publication_summaries.return_value = summaries
        reviews.publication_summary.return_value = {'review_id': 'review-1', 'status': 'published'}
        self.controller.job_reviews = reviews
        result = self.controller.curated_list(receipt['list_id'])
        reviews.publication_summaries.assert_called_once_with(receipt['list_id'])
        reviews.publication_ordinals.assert_called_once_with(receipt['list_id'])
        first, second = result['recommendations']
        self.assertEqual(first['review_summary'], summaries[('ashby', '2')])
        self.assertNotIn('eligibility', first['review_summary'])
        self.assertEqual(first['explanation'], '<script>untrusted text</script>')
        self.assertEqual(first['review_ordinal'], 7)
        self.assertEqual([job['rank'] for job in result['recommendations']], [1, 2])
        self.assertNotIn('review_summary', second)

    def test_review_brief_routes_share_existing_session_protection(self):
        self.controller.job_reviews = Mock()
        self.controller.job_reviews.call.return_value = {'revision': 0, 'brief': {}, 'saved_at': None}
        with running(make_server(self.controller, port=0)) as server:
            status, _, body = request(server, 'GET', '/api/v1/job-reviews/brief')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['revision'], 0)
            self.controller.job_reviews.call.assert_called_once_with('brief')
            path = '/api/v1/job-reviews/save-brief'
            self.assertEqual(request(server, 'POST', path, {'brief': {}})[0], 403)
            cookie, csrf = session(server)
            supplied = {'brief': {}, 'expected_revision': 0, 'idempotency_key': 'save-brief-1'}
            status, _, _ = post(server, path, supplied, cookie, csrf)
            self.assertEqual(status, 200)
            self.controller.job_reviews.call.assert_called_with('save-brief', supplied)

    def test_upgrade_preserves_existing_lists_and_constraints(self):
        from job_search import db
        path = self.root / 'old-ledger.db'
        with patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:11]):
            db.migrate(path, '2026-09-28T00:00:00Z')
        saved = CuratedShortlists(path, self.catalog)
        receipt = saved.publish(self.payload)
        before = saved.get(receipt['list_id'])
        db.migrate(path, '2026-09-30T00:00:00Z')
        self.assertEqual(saved.get(receipt['list_id']), before)
        self.assertEqual(saved.publish(self.payload), receipt)
        broad = saved.publish(dict(self.payload, idempotency_key='expanded', jobs=[
            {'ats': 'ashby', 'job_id': str(i)} for i in range(1, 322)]))
        self.assertEqual(broad['job_count'], 321)
        with closing(db.connect(path)) as con:
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual([row[0] for row in con.execute('SELECT sequence FROM curated_shortlists ORDER BY sequence')], [1, 2])
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE curated_shortlists SET title='Changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE curated_shortlist_items SET explanation='Changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO curated_shortlist_items VALUES ('missing',1,'ashby','1','','{}')")

    def test_five_hundred_jobs_and_snapshot_survive_catalog_edits(self):
        payload = dict(self.payload, jobs=[{'ats': 'ashby', 'job_id': str(i)} for i in range(1, 501)])
        receipt = self.saved.publish(payload)
        self.assertEqual(receipt['job_count'], 500)
        with closing(sqlite3.connect(self.jobs)) as con, con:
            con.execute("UPDATE jobs SET title='Changed title',closed_at='2026-09-29T00:00:00Z' WHERE id='1'")
        result = self.controller.curated_list(receipt['list_id'])
        self.assertEqual(result['recommendations'][0]['title'], 'Role 1')
        self.assertTrue(result['recommendations'][0]['closed_at'])
        self.assertEqual(result['recommendations'][-1]['rank'], 500)

    def test_concurrent_retry_persistence_order_history_and_empty(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: self.saved.publish(self.payload), range(4)))
        self.assertTrue(all(row == receipts[0] for row in receipts))
        restarted = CuratedShortlists(self.ledger.store.db_path, self.catalog)
        result = restarted.get(receipts[0]['list_id'])
        self.assertEqual([row['id'] for row in result['recommendations']], ['2', '1'])
        self.assertEqual([row['rank'] for row in result['recommendations']], [1, 2])
        self.assertNotIn('ranking_score', result['recommendations'][0])
        for index in range(101):
            restarted.publish({'title': 'No fits', 'idempotency_key': f'empty-{index}', 'jobs': []})
        first = restarted.lists()
        second = restarted.lists(first['next_before'])
        self.assertEqual(len(first['lists']), 100)
        self.assertEqual(len(second['lists']), 2)
        self.assertEqual(second['lists'][-1]['list_id'], receipts[0]['list_id'])
        self.assertEqual(restarted.get(first['lists'][0]['list_id'])['recommendations'], [])

    def test_agent_api_client_and_dashboard_restart(self):
        adapter = minimal_adapter()
        adapter = HermesAdapter(replace(adapter._capabilities, publish_curated_shortlist=self.saved.publish))
        token_file = self.root / 'token'
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)
        with running(make_mcp_server(adapter, TOKEN, 0)) as mcp:
            config = RuntimeConfigV1(version=1, project_root=self.root, application_db=self.ledger.store.db_path,
                jobs_db=self.jobs, preference_db=self.root/'absent-model.db', proxy_db=self.root/'absent-proxy.db',
                mcp_port=mcp.server_address[1], mcp_token_file=token_file)
            status, _ = mcp_request(mcp, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'publish_curated_shortlist', 'arguments': self.payload}}, token='wrong')
            self.assertEqual(status, 401)
            self.assertEqual(self.saved.lists()['lists'], [])
            status, definitions = mcp_request(mcp, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            tool = next(tool for tool in definitions['result']['tools'] if tool['name'] == 'publish_curated_shortlist')
            self.assertFalse(tool['annotations']['readOnlyHint'])
            self.assertTrue(tool['annotations']['idempotentHint'])
            self.assertFalse(tool['annotations']['destructiveHint'])
            self.assertEqual(tool['inputSchema']['properties']['jobs']['maxItems'], 500)
            broad = dict(self.payload, idempotency_key='broad-321', jobs=[
                {'ats': 'ashby', 'job_id': str(i)} for i in range(1, 322)])
            broad_receipt = publish(config, broad)
            self.assertEqual(broad_receipt['job_count'], 321)
            self.assertEqual(publish(config, broad), broad_receipt)
            self.assertEqual(len(self.saved.get(broad_receipt['list_id'])['recommendations']), 321)
            receipt = publish(config, self.payload)
            self.assertIn('/#shortlist/curated_', receipt['dashboard_url'])
            self.assertEqual(publish(config, self.payload), receipt)
            with patch('sys.stdin') as stdin, patch('job_search.cli.load_runtime_config', return_value=config), patch('job_search.cli._json') as output:
                from job_search.cli import main
                stdin.buffer.read.return_value = json.dumps(self.payload).encode()
                self.assertEqual(main(['shortlist', 'publish']), 0)
                self.assertEqual(output.call_args.args[0], receipt)
        for _ in range(2):
            controller = DashboardController(JobSearchLedger(self.ledger.store.db_path), FakePreferences(), jobs=self.catalog)
            with running(make_server(controller, 0)) as dashboard:
                status, _, body = request(dashboard, 'GET', '/api/v1/curated-shortlists')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['lists'][0]['list_id'], receipt['list_id'])
                status, _, body = request(dashboard, 'GET', '/api/v1/curated-shortlist?list_id=' + receipt['list_id'])
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['recommendations'][0]['id'], '2')
        self.assertFalse(config.preference_db.exists())
        self.assertFalse(config.proxy_db.exists())

    def test_prepare_membership_closure_existing_application_and_feedback(self):
        list_id = self.saved.publish(self.payload)['list_id']
        with self.assertRaisesRegex(ContractError, 'not in'):
            self.controller.prepare_curated(list_id, 'ashby', '3', 'bad-member')
        with closing(sqlite3.connect(self.jobs)) as con, con:
            con.execute("UPDATE jobs SET closed_at='2026-09-29T00:00:00Z' WHERE id='1'")
        with self.assertRaisesRegex(ContractError, 'closed'):
            self.controller.prepare_curated(list_id, 'ashby', '1', 'closed')
        self.assertTrue(self.controller.curated_list(list_id)['recommendations'][1]['closed_at'])
        gateway = Mock()
        gateway.prepare.return_value = {'configured': True}
        self.controller.resume_lab = gateway
        with running(make_server(self.controller, 0)) as dashboard:
            cookie, csrf = session(dashboard)
            payload = {'list_id': list_id, 'ats': 'ashby', 'job_id': '2', 'idempotency_key': 'prepare-curated'}
            self.assertEqual(request(dashboard, 'POST', '/api/v1/curated-shortlist/prepare', payload)[0], 403)
            status, _, body = post(dashboard, '/api/v1/curated-shortlist/prepare', payload, cookie, csrf)
            self.assertEqual(status, 200, body)
            application = json.loads(body)['application']
        self.assertEqual(application['recommendation_session_id'], list_id)
        self.assertEqual(application['recommendation_rank'], 1)
        self.assertEqual(application['recommendation_policy_id'], 'curated')
        self.assertIsNone(application['recommendation_impression_id'])
        self.assertIsNone(application['ranking_score'])
        gateway.prepare.assert_called_once()
        self.assertEqual(self.controller.curated_list(list_id)['recommendations'][0]['application_id'], application['application_id'])
        self.assertEqual(self.controller.curated_list(list_id)['recommendations'][0]['application_phase'], 'preparing')
        result = self.controller.prepare_curated(list_id, 'ashby', '2', 'another-prepare')
        self.assertFalse(result['created'])
        preference = PreferenceGateway(PreferencePaths(self.jobs, self.root/'no-model', self.root/'no-proxy'))
        result = preference.deliver_applied_feedback({'policy_id': 'curated'}, source_event_id='submission-1')
        self.assertEqual(result['reason'], 'curated_selection')
        self.assertFalse(result['preference_label_changed'])
        self.assertEqual(self.ledger.verify_projections(), [])


if __name__ == '__main__':
    unittest.main()
