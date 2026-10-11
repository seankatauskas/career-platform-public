"""Offline generation admission, continuity, transport and host execution contracts."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.db import connect
from job_search.dashboard import DashboardController, make_server
from job_search.hermes import HermesAdapter
from job_search.hermes_mcp import make_mcp_server
from job_search.job_reviews.client import ReviewClient
from job_search.job_reviews.old_method.host import drain_generation, execution
from job_search.job_reviews.old_method.workspace import Review, write_json
from job_search.job_reviews.runner_config import RunnerConfig
from job_search.interactions.telegram import DEFAULT_ALLOWED_TOOLS, tool_policy
from tests import test_old_method as fixture
from tests.test_curated_shortlists import running
from tests.test_job_search_dashboard import FakePreferences, request
from tests.test_job_search_hermes_runtime import minimal_adapter, mcp_request, TOKEN


class GeneratorTests(unittest.TestCase):
    setUp = fixture.OldMethodTests.setUp
    insert = fixture.OldMethodTests.insert
    start = fixture.OldMethodTests.start
    workspace = fixture.OldMethodTests.workspace
    finish = fixture.OldMethodTests.finish
    observations = fixture.OldMethodTests.observations

    def generate(self, key='generate-1', **args):
        return self.service.call('generate', {'idempotency_key': key, **args})

    def first(self):
        return self.generate(initial_start='2026-10-08T00:00:00Z')['review_id']

    def freeze(self, rid):
        self.service.now = lambda: '2026-10-09T12:01:00Z'
        self.assertTrue(self.store.freeze_generation(rid, execution=self.execution))

    def test_first_run_requires_explicit_start_and_rejects_invalid_arguments(self):
        self.assertTrue(self.service.call('generation-status')['needs_initial_start'])
        for args in ({}, {'initial_start': 'bad'}, {'initial_start': '2026-10-10T00:00:00Z'},
                     {'initial_start': '2026-10-08T00:00:00'}, {'window_end': '2026-10-09T00:00:00Z'}):
            with self.subTest(args=args), self.assertRaises(ContractError):
                self.generate(**args)
        rid = self.first()
        self.assertEqual(self.store.metadata(rid)['phase'], 'awaiting_inputs')
        with self.assertRaises(ContractError):
            self.generate(initial_start='2026-10-07T00:00:00Z')  # changed command payload

    def test_concurrent_interfaces_join_one_run_and_retries_survive_publication(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n: self.generate('concurrent-'+str(n), initial_start='2026-10-08T00:00:00Z'), range(8)))
        self.assertEqual(len({r['review_id'] for r in results}), 1)
        rid = results[0]['review_id']
        self.freeze(rid); self.finish(rid)
        self.store.publish(rid, self.observations(rid))
        replay = self.generate('concurrent-0', initial_start='2026-10-08T00:00:00Z')
        self.assertEqual(replay['review_id'], rid)
        self.assertEqual(replay['status'], 'published')

    def test_next_run_uses_published_window_not_completion_and_ignores_custom_runs(self):
        custom = self.start(); self.finish(custom)
        self.store.publish(custom, self.observations(custom))
        with self.assertRaises(ContractError):
            self.generate()  # custom publication did not bootstrap this stream
        rid = self.first(); self.freeze(rid); self.finish(rid)
        self.store.publish(rid, self.observations(rid))
        result = self.generate('next')
        self.assertEqual(result['window_start'], '2026-10-09T12:00:00.000000Z')
        self.assertEqual(result['window_end'], '2026-10-09T12:01:00.000000Z')
        self.assertEqual(result['covered_through'], result['window_start'])

    def test_failed_run_requires_explicit_resume_and_keeps_window_and_checkpoint(self):
        rid = self.first(); self.freeze(rid); self.finish(rid)
        previous = copy.deepcopy(self.store.metadata(rid)['result'])
        self.store.update(rid, phase='failed', error='temporary failure')
        status = self.generate('another')
        self.assertEqual(status['review_id'], rid)
        self.assertTrue(status['can_resume'])
        self.assertIsNone(status['covered_through'])
        resumed = self.service.call('generation-resume', {'review_id': rid, 'idempotency_key': 'resume'})
        self.assertEqual(resumed['phase'], 'queued')
        self.assertEqual(resumed['window_end'], status['window_end'])
        self.assertEqual(self.store.metadata(rid)['result'], previous)
        with self.assertRaises(ContractError):
            self.service.call('generation-resume', {'review_id': self.start(), 'idempotency_key': 'wrong'})

    def test_empty_publication_advances_and_input_failure_can_resume(self):
        rid = self.first()
        self.store.update(rid, phase='failed')
        self.assertEqual(self.service.call('generation-resume', {'review_id':rid,'idempotency_key':'retry'})['phase'], 'awaiting_inputs')
        self.freeze(rid); self.finish(rid, {})
        receipt = self.store.publish(rid, [])
        self.assertEqual([r['job_count'] for r in receipt['lists']], [0, 0])
        self.assertEqual(self.generate('next')['window_start'], '2026-10-09T12:00:00.000000Z')

    def test_freeze_waits_for_collection_commit_and_preserves_request_cutoff(self):
        rid = self.first()
        self.assertFalse(self.store.freeze_generation(rid, execution=self.execution))
        self.service.now = lambda: '2026-10-09T12:05:00Z'
        with closing(connect(self.ledger.store.db_path)) as con, con:
            con.execute("INSERT INTO work_items(work_id,task_kind,dedupe_key,payload_json,status,due_at,max_attempts,created_at) VALUES('scan','ats.new_only','scan','{}','running','2026-10-09T11:55:00Z',5,'2026-10-09T11:55:00Z')")
        self.assertFalse(self.store.freeze_generation(rid, execution=self.execution))
        self.insert('just-saved', first_seen='2026-10-09T11:59:59Z')
        self.insert('later', first_seen='2026-10-09T12:00:01Z')
        with closing(connect(self.ledger.store.db_path)) as con, con:
            con.execute("UPDATE work_items SET status='succeeded' WHERE work_id='scan'")
        self.assertTrue(self.store.freeze_generation(rid, execution=self.execution))
        packet = self.store.packet(rid)
        self.assertEqual([j['id'] for j in packet['jobs']], ['a', 'just-saved'])
        self.assertEqual(packet['window_end'], '2026-10-09T12:00:00.000000Z')
        self.assertNotIn('generation_stream', packet)
        self.assertNotIn('previous_lists', packet)

    def test_host_consumes_request_and_publishes_without_scheduling_more_runs(self):
        app = self.root/'app.json'
        app.write_text(json.dumps({'version':1, 'scraper_contact':'test@example.test'})); app.chmod(0o600)
        config = RunnerConfig(application_config=app, state_dir=self.root/'state', runtime_dir=self.root/'runtime',
                              auth_home=self.root/'auth', model_image='example/app@sha256:'+'a'*64)
        self.assertEqual(drain_generation(config, self.store)['phase'], 'idle')
        rid = self.first()
        self.service.now = lambda: '2026-10-09T12:01:00Z'
        @contextmanager
        def launch(config, packet, root, previous, telemetry):
            write_json(root/'input.json', packet)
            review = Review(root/'input.json', root/'output')
            review.sources([1]); review.save([{'ordinal':1,'assessment':fixture.assessment()}]); review.finish([1])
            class Handle:
                output_directory = review.directory
                def poll(self): return 0
                def wait(self): return 0
            yield Handle()
        result = drain_generation(config, self.store, launch=launch, availability=lambda *args, **kw: self.observations(rid))
        self.assertEqual(result['status'], 'published')
        self.assertEqual(drain_generation(config, self.store)['phase'], 'idle')

    def test_http_cli_and_telegram_mcp_share_the_same_run(self):
        controller = DashboardController(self.ledger, FakePreferences(), jobs=self.service.catalog)
        controller.job_reviews = self.service
        with running(make_server(controller, port=0)) as server:
            client = ReviewClient(f'http://127.0.0.1:{server.server_address[1]}')
            first = client.call('generate', {'initial_start':'2026-10-08T00:00:00Z','idempotency_key':'cli'})
            code, _, _ = request(server, 'POST', '/api/v1/job-reviews/generate', body={'idempotency_key':'csrf-missing'})
            self.assertEqual(code, 403)
            code, _, value = request(server, 'GET', '/api/v1/job-reviews/generation-status')
            self.assertEqual(code, 200)
        adapter = HermesAdapter(replace(minimal_adapter()._capabilities, review_call=self.service.call))
        for name in ('review_generate', 'review_generation_status', 'review_generation_resume'):
            self.assertIn('mcp_job_search_'+name, DEFAULT_ALLOWED_TOOLS)
            self.assertNotEqual((tool_policy(DEFAULT_ALLOWED_TOOLS, tool_name='mcp_job_search_'+name) or {}).get('action'), 'block')
        with running(make_mcp_server(adapter, TOKEN, port=0)) as server:
            code, result = mcp_request(server, {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
                'name':'review_generate','arguments':{'idempotency_key':'telegram'}}})
            self.assertEqual(code, 200)
            self.assertEqual(result['result']['structuredContent']['review_id'], first['review_id'])

    def test_host_failure_stops_until_user_resumes_and_maintenance_blocks_admission(self):
        config = RunnerConfig(application_config=self.root/'app.json', state_dir=self.root/'state',
            runtime_dir=self.root/'runtime', auth_home=self.root/'auth', model_image='example/app@sha256:'+'a'*64)
        rid = self.first()
        self.service.now = lambda: '2026-10-09T12:01:00Z'
        self.profile['facts'] = []
        self.assertEqual(drain_generation(config, self.store)['phase'], 'failed')
        self.assertEqual(drain_generation(config, self.store)['phase'], 'idle')
        self.assertTrue(self.service.call('generation-status')['can_resume'])
        with patch('job_search.job_reviews.old_method.generator.require_start', side_effect=RuntimeError('paused')):
            with self.assertRaisesRegex(ContractError, 'maintenance'):
                self.service.call('generation-resume', {'review_id':rid, 'idempotency_key':'paused'})
        self.assertEqual(self.store.metadata(rid)['phase'], 'failed')

    def test_admission_and_status_never_read_lists_or_applications(self):
        from job_search.job_reviews.old_method import generator
        def guarded(path):
            con = connect(path)
            con.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table in ('applications','curated_shortlists','curated_shortlist_items')
                else sqlite3.SQLITE_OK)
            return con
        with patch.object(generator, 'connect', guarded):
            rid = self.first()
            self.assertEqual(self.service.call('generation-status')['review_id'], rid)


if __name__ == '__main__':
    unittest.main()
