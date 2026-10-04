"""Offline acceptance of strict, resumable, evidence-backed agent reviews."""
import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from job_search.contracts import ContractError, payload_sha256
from job_search.dashboard import DashboardController, make_server
from job_search.hermes import HermesAdapter
from job_search.hermes_mcp import make_mcp_server
from job_search.integration import LocalJobCatalog
from job_search.job_reviews.client import ReviewClient, endpoint
from job_search.job_reviews.service import JobReviews
from job_search.service import JobSearchLedger
from tests.test_curated_shortlists import running
from tests.test_job_search_dashboard import FakePreferences, request, session, post
from tests.test_job_search_hermes_runtime import minimal_adapter, mcp_request, TOKEN


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'jobs.db'
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,title TEXT,company TEXT,location TEXT,description TEXT,jobUrl TEXT,posted_at TEXT,source_updated_at TEXT,first_seen TEXT,last_seen TEXT,closed_at TEXT,PRIMARY KEY(ats,id))')
        self.insert('a')
        self.ledger = JobSearchLedger(self.root / 'ledger.db')
        self.catalog = LocalJobCatalog(self.db)
        self.facts = [{'fact_id': 'fact-1', 'source': 'experience', 'text': 'Developed Python APIs and SQL systems'}]
        self.profile = {'facts': self.facts, 'fingerprint': payload_sha256(self.facts), 'resume_versions': ['resume-1']}
        self.now = '2026-10-02T01:00:00.000000Z'
        self.service = JobReviews(self.ledger.store.db_path, self.catalog, lambda: copy.deepcopy(self.profile), now=lambda: self.now)
        self.sequence = 0

    def insert(self, jid, **changes):
        job = dict(ats='ashby', id=jid, title='Software Engineer', company='Example', location='US',
                   description='Build Python APIs and SQL systems.', jobUrl='https://example.test/' + jid,
                   posted_at='2026-10-01T12:00:00Z', source_updated_at=None,
                   first_seen='2026-10-01T13:00:00Z', last_seen='2026-10-02T00:30:00Z', closed_at=None)
        job.update(changes)
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO jobs VALUES (' + ','.join('?' for _ in job) + ')', tuple(job.values()))

    def command(self, action, **args):
        from job_search.job_reviews.service import WRITES
        self.sequence += 1
        if action in WRITES:
            args.setdefault('idempotency_key', f'cmd-{self.sequence}')
        return self.service.call(action, args)

    def start(self, **args):
        return self.command('start', mode='custom', window_start='2026-10-01T00:00:00Z', **args)['review_id']

    def assessment(self, decision='close', **args):
        value = dict(stage='detailed', decision=decision, family='backend', alignment='core',
                     reason_code='fit', explanation='Python APIs align; production scale is unspecified.',
                     evidence=[{'field': 'description', 'quote': 'Python APIs', 'fact_id': 'fact-1'}],
                     strengths=['Python API experience'], gaps=[], unknowns=['Team scale'], borderline=False)
        if decision not in ('exclude', 'needs_info'):
            value['priority'] = 1
        value.update(args)
        return value

    def assess(self, rid, ordinal=1, kind='primary', value=None, actor=None):
        actor = actor or ('reviewer' if kind == 'primary' else 'checker')
        offset = 0
        while True:
            job = self.command('job', review_id=rid, ordinal=ordinal, actor=actor, offset=offset)
            if job['next_offset'] is None:
                break
            offset = job['next_offset']
        return self.command('assess', review_id=rid, ordinal=ordinal, actor=actor, kind=kind,
                            expected_revision=job['revision'], assessment=value or self.assessment())

    def finish(self, rid):
        preview = self.command('preview', review_id=rid)
        self.assertTrue(preview['ready'], preview)
        return self.command('publish', review_id=rid, preview_sha256=preview['preview_sha256'])

    def test_strict_window_late_undated_and_no_modified_fallback(self):
        self.insert('start', posted_at='2026-10-01T00:00:00Z')
        self.insert('end', posted_at=self.now)
        self.insert('later', posted_at='2026-10-02T01:00:01Z')
        self.insert('late', posted_at='2026-09-30T12:00:00Z')
        self.insert('older', posted_at='2026-09-01T12:00:00Z')
        self.insert('undated', posted_at=None, source_updated_at='2026-10-01T12:00:00Z')
        self.insert('offset', posted_at='2026-10-01T08:00:00-05:00')
        rid = self.start()
        batch = self.command('batch', review_id=rid)
        self.assertEqual([i['job']['id'] for i in batch['items']], ['a', 'offset', 'end'])
        status = self.command('status', review_id=rid)
        self.assertEqual(status['metadata']['late_arrivals'], {'recent': 2, 'older': 1, 'undated': 1})

    def test_resume_pages_claims_and_concurrent_reviewers(self):
        for i in range(24): self.insert(str(i))
        rid = self.start()
        claims = list(ThreadPoolExecutor(2).map(lambda actor: self.command('claim', review_id=rid, actor=actor), ['one', 'two']))
        self.assertFalse(set(claims[0]['ordinals']) & set(claims[1]['ordinals']))
        found, after = [], 0
        while True:
            page = self.command('batch', review_id=rid, after=after, limit=7)
            found.extend(i['ordinal'] for i in page['items'])
            if page['next_after'] is None: break
            after = page['next_after']
        self.assertEqual(found, list(range(1, 26)))
        self.now = '2026-10-02T02:00:00.000000Z'
        recovered = self.command('claim', review_id=rid, actor='replacement')
        self.assertEqual(recovered['ordinals'], list(range(1, 11)))
        restarted = JobReviews(self.ledger.store.db_path, self.catalog, lambda: self.profile)
        self.assertEqual(restarted.call('status', {'review_id': rid})['total'], 25)

    def test_source_evidence_full_reads_and_optimistic_conflicts(self):
        with sqlite3.connect(self.db) as con: con.execute('UPDATE jobs SET description=?', ('prefix '*2000 + 'Python APIs',))
        rid = self.start()
        args = dict(review_id=rid, ordinal=1, actor='r', expected_revision=0, assessment=self.assessment())
        with self.assertRaisesRegex(ContractError, 'all frozen'):
            self.command('assess', **args)
        self.command('job', review_id=rid, ordinal=1, actor='r', offset=10000)
        with self.assertRaisesRegex(ContractError, 'all frozen'):
            self.command('assess', **args)
        self.assess(rid, actor='r')
        with self.assertRaisesRegex(ContractError, 'changed'):
            self.command('assess', **args)
        with self.assertRaisesRegex(ContractError, 'another reviewer'):
            self.assess(rid, actor='r', kind='check')
        bad = self.assessment(evidence=[{'field':'description','quote':'invented','fact_id':'fact-1'}])
        with self.assertRaisesRegex(ContractError, 'quote'):
            self.assess(rid, value=bad)
        split_link = self.assessment(evidence=[
            {'field': 'title', 'quote': 'Engineer', 'fact_id': 'fact-1'},
            {'field': 'description', 'quote': 'Python APIs'}])
        with self.assertRaisesRegex(ContractError, 'linked profile evidence'):
            self.assess(rid, value=split_link)

    def test_publication_gate_atomic_retry_and_changed_input(self):
        rid = self.start()
        self.assertIn('unassessed_jobs', self.command('preview', review_id=rid)['blockers'])
        self.assess(rid)
        self.assertIn('independent_checks_required', self.command('preview', review_id=rid)['blockers'])
        self.assess(rid, kind='check', value=self.assessment('broad_only'))
        self.assertIn('reviewer_disagreements', self.command('preview', review_id=rid)['blockers'])
        self.assess(rid, kind='check', value=self.assessment(alignment='adjacent'))
        self.assertIn('reviewer_disagreements', self.command('preview', review_id=rid)['blockers'])
        self.assess(rid, kind='check')
        preview = self.command('preview', review_id=rid)
        original = self.service.curated.publish_in_transaction
        def fail_second(con, supplied, **kw):
            if 'Prioritized' in supplied['title']: raise RuntimeError('simulated crash')
            return original(con, supplied, **kw)
        with patch.object(self.service.curated, 'publish_in_transaction', side_effect=fail_second), self.assertRaises(RuntimeError):
            self.finish(rid)
        self.assertEqual(self.service.curated.lists()['lists'], [])
        receipt = self.finish(rid)
        self.assertEqual([r['job_count'] for r in receipt['lists']], [1, 1])
        self.assertEqual(self.command('publish', review_id=rid, preview_sha256=preview['preview_sha256']), receipt)
        with self.assertRaisesRegex(ContractError, 'different preview'):
            self.command('publish', review_id=rid, preview_sha256='different')

    def test_material_change_refresh_and_profile_drift(self):
        rid = self.start()
        self.assess(rid); self.assess(rid, kind='check')
        old = self.command('preview', review_id=rid)
        with sqlite3.connect(self.db) as con: con.execute("UPDATE jobs SET description=description || ' and Java' WHERE id='a'")
        self.assertIn('selected_posting_changed', self.command('preview', review_id=rid)['blockers'])
        self.assertEqual(self.command('preview', review_id=rid)['blocking_jobs'],
                         [{'ordinal': 1, 'reason': 'selected_posting_changed'}])
        self.command('refresh-job', review_id=rid, ordinal=1, actor='reviewer', expected_revision=2)
        self.assess(rid); self.assess(rid, kind='check')
        with self.assertRaisesRegex(ContractError, 'fresh preview'):
            self.command('publish', review_id=rid, preview_sha256=old['preview_sha256'])
        self.profile['fingerprint'] = 'new-profile'
        self.assertTrue(self.command('preview', review_id=rid)['profile_changed_since_start'])
        self.assertNotEqual(self.command('context', review_id=rid)['fingerprint'], self.profile['fingerprint'])

    def test_recurrence_custom_windows_empty_lists_and_dst_titles(self):
        self.service.curated.publish({'title':'Broad SWE + adjacent | Previous', 'window_start':'2026-09-29T00:00:00Z', 'window_end':'2026-10-01T00:00:00Z', 'idempotency_key':'legacy-b', 'jobs':[]})
        self.service.curated.publish({'title':'Profile matches + stretches | Previous', 'window_start':'2026-09-29T00:00:00Z', 'window_end':'2026-10-01T00:00:00Z', 'idempotency_key':'legacy-t', 'jobs':[]})
        rid = self.command('start')['review_id']
        with self.assertRaisesRegex(ContractError, 'resume'):
            self.command('start')
        self.command('abandon', review_id=rid, reason='start a later review')
        self.assertEqual(self.command('context')['previous_review']['window_end'], '2026-10-01T00:00:00Z')
        custom = self.start()
        self.assess(custom); self.assess(custom, kind='check'); self.finish(custom)
        self.assertEqual(self.command('context')['previous_review']['window_end'], '2026-10-01T00:00:00Z')
        rid = self.command('start')['review_id']
        self.assess(rid); self.assess(rid, kind='check'); self.finish(rid)
        self.assertEqual(self.command('context')['previous_review']['window_end'], self.now)
        self.now = '2026-11-02T10:00:00.000000Z'
        empty = self.command('start', mode='custom', window_start='2026-11-01T06:00:00Z')['review_id']
        receipt = self.finish(empty)
        self.assertIn('CDT', receipt['lists'][0]['title']); self.assertIn('CST', receipt['lists'][0]['title'])
        self.assertEqual([r['job_count'] for r in receipt['lists']], [0, 0])

    def test_audit_sampling_closed_jobs_and_duplicate_validation(self):
        self.insert('b'); rid = self.start()
        self.assess(rid, value=self.assessment('exclude', reason_code='requirements'))
        self.assess(rid, ordinal=2)
        status = self.command('status', review_id=rid)
        self.assertEqual(status['audit_remaining'], [1, 2])
        self.assess(rid, kind='check', value=self.assessment('exclude', reason_code='requirements'))
        self.assess(rid, ordinal=2, kind='check')
        with sqlite3.connect(self.db) as con: con.execute("UPDATE jobs SET closed_at='2026-10-02T00:00:00Z' WHERE id='b'")
        preview = self.command('preview', review_id=rid)
        self.assertEqual(preview['omitted'], [{'ordinal':2,'reason':'closed'}])
        self.assertEqual(self.finish(rid)['lists'][0]['job_count'], 0)

    def test_many_roles_split_without_transport_sized_publication(self):
        for i in range(501): self.insert(str(i))
        rid = self.start()
        for ordinal in range(1, 503):
            self.assess(rid, ordinal=ordinal, value=self.assessment('broad_only', priority=ordinal))
        receipt = self.finish(rid)
        self.assertEqual([r['job_count'] for r in receipt['lists']], [500, 2, 0])

    def test_duplicate_target_must_survive_publication(self):
        self.insert('b'); rid = self.start()
        duplicate = self.assessment('exclude', reason_code='duplicate', duplicate_of=2)
        self.assess(rid, value=duplicate); self.assess(rid, kind='check', value=duplicate)
        self.assess(rid, ordinal=2); self.assess(rid, ordinal=2, kind='check')
        self.assertTrue(self.command('preview', review_id=rid)['ready'])
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE jobs SET closed_at='2026-10-02T00:00:00Z' WHERE id='b'")
        preview = self.command('preview', review_id=rid)
        self.assertFalse(preview['ready'])
        self.assertEqual(preview['blocking_jobs'],
                         [{'ordinal': 1, 'reason': 'duplicate_without_retained_recommendation'}])

    def test_dashboard_client_access_and_mcp_untruncated_description(self):
        controller = DashboardController(self.ledger, FakePreferences(), jobs=self.catalog)
        controller.job_reviews = self.service
        with running(make_server(controller, port=0)) as server:
            client = ReviewClient(f'http://127.0.0.1:{server.server_address[1]}')
            rid = client.call('start', dict(mode='custom',window_start='2026-10-01T00:00:00Z',idempotency_key='http-start'))['review_id']
            self.assertEqual(client.call('status', {'review_id':rid})['total'], 1)
            status, _, _ = request(server, 'POST', '/api/v1/job-reviews/start', body={'idempotency_key':'bad'})
            self.assertEqual(status, 403)
        adapter = minimal_adapter()
        adapter = HermesAdapter(replace(adapter._capabilities, review_call=self.service.call))
        self.assertIn('review_start', adapter.tool_names)
        with running(make_mcp_server(adapter, TOKEN, port=0)) as server:
            status, result = mcp_request(server, {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'review_status','arguments':{'review_id':rid}}})
            self.assertEqual(status, 200)
            self.assertEqual(result['result']['structuredContent']['total'], 1)
        with self.assertRaises(ContractError): endpoint('https://public.example.com')
        with self.assertRaises(ContractError): endpoint('https://user:password@private.example.ts.net')

    def test_approved_context_excludes_drafts_and_feedback_is_explicit(self):
        from tests.test_career_resume import gateway_at
        from job_search.job_reviews.context import profile_context
        gateway, _ = gateway_at(self.root)
        before = profile_context(gateway)
        profile = gateway.get_career_profile()
        content = copy.deepcopy(profile['draft']['content'])
        content['experience'][0]['bullets'][0]['text'] = 'Unapproved imaginary technology'
        gateway.save_career_profile(content, expected_revision_id=profile['draft_revision_id'], idempotency_key='draft-edit')
        self.assertEqual(before, profile_context(gateway))
        saved = self.command('feedback', note='Prefer backend roles', idempotency_key='user-note')
        self.assertEqual(saved, self.command('feedback', note='Prefer backend roles', idempotency_key='user-note'))
        with self.assertRaisesRegex(ContractError, 'reused'):
            self.command('feedback', note='Different note', idempotency_key='user-note')
        rid = self.start()
        frozen = self.command('context', review_id=rid, section='feedback')['feedback']
        self.assertEqual([v['note'] for v in frozen], ['Prefer backend roles'])

    def test_independent_blind_read_and_lossless_assessment_pagination(self):
        rid = self.start()
        self.assess(rid)
        read = self.command('job', review_id=rid, ordinal=1, actor='checker', blind=True)
        self.assertIsNone(read['assessment'])
        encoded, offset = '', 0
        while True:
            part = self.command('assessment', review_id=rid, ordinal=1, offset=offset, limit=37)
            encoded += part['text']
            if part['next_offset'] is None: break
            offset = part['next_offset']
        self.assertEqual(json.loads(encoded)['evidence'][0]['fact_id'], 'fact-1')

    def test_review_candidates_and_source_payload_are_independent_of_model_ranking(self):
        self.insert('b', posted_at='2026-10-01T11:00:00Z')
        self.insert('c')
        with sqlite3.connect(self.db) as con:
            con.execute('ALTER TABLE jobs ADD COLUMN ranking_score REAL')
            con.execute('ALTER TABLE jobs ADD COLUMN model_explanation TEXT')
            con.execute('CREATE TABLE preference_scores(job_id TEXT, score REAL)')
            con.executemany('INSERT INTO preference_scores VALUES (?,?)', [('a', .99), ('b', .01), ('c', .5)])
            con.execute("UPDATE jobs SET ranking_score=.99, model_explanation='MODEL_ONLY_SENTINEL'")
        # Future catalog presentation enrichment must not become review evidence.
        self.catalog._DETAIL_FIELDS += ('ranking_score', 'model_explanation')
        statements = []
        original_connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            con = original_connect(*args, **kwargs)
            con.set_trace_callback(statements.append)
            return con
        with patch('job_search.job_reviews.service.sqlite3.connect', side_effect=traced_connect):
            first = self.start()
            batch = self.command('batch', review_id=first)
            read = self.command('job', review_id=first, ordinal=1, actor='reviewer')
        self.assertEqual([item['job']['id'] for item in batch['items']], ['b', 'a', 'c'])
        self.assertFalse(any('preference_scores' in s for s in statements))
        self.assertNotIn('ranking_score', json.dumps(batch))
        self.assertNotIn('MODEL_ONLY_SENTINEL', json.dumps(read))
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE jobs SET ranking_score=1-ranking_score')
            con.execute('UPDATE preference_scores SET score=1-score')
        second = self.start()
        self.assertEqual(batch, self.command('batch', review_id=second))
        self.command('refresh-job', review_id=first, ordinal=1, actor='reviewer', expected_revision=0)
        with sqlite3.connect(self.ledger.store.db_path) as con:
            snapshots = [json.loads(r[0]) for r in con.execute('SELECT snapshot_json FROM job_review_items')]
        self.assertTrue(all('ranking_score' not in job and 'model_explanation' not in job for job in snapshots))

    def test_context_projects_evidence_and_preserves_explicit_feedback(self):
        note = 'I disliked the high-scoring roles; prefer backend roles without staff ownership.'
        self.command('feedback', note=note)
        self.profile.update(ranking_score=.99, model_explanation='MODEL_ONLY_SENTINEL')
        self.profile['facts'][0]['model_rank'] = 1
        self.profile['facts'].append({'fact_id': 'heading', 'source': 'experience',
                                     'text': {'company': 'Example', 'role': 'Engineer', 'model_score': .99}})
        rid = self.start(preferences=['I enjoy building ranking systems.'])
        context = self.command('context', review_id=rid)
        self.assertNotIn('ranking_score', context)
        self.assertNotIn('MODEL_ONLY_SENTINEL', json.dumps(context))
        self.assertNotIn('model_rank', context['facts'][0])
        self.assertEqual(context['facts'][1]['text'], {'company': 'Example', 'role': 'Engineer'})
        self.assertEqual(self.command('context', review_id=rid, section='preferences')['preferences'],
                         ['I enjoy building ranking systems.'])
        self.assertEqual(self.command('context', review_id=rid, section='feedback')['feedback'][0]['note'], note)
        # Old frozen contexts get the same projection without rewriting their receipts.
        with sqlite3.connect(self.ledger.store.db_path) as con:
            encoded = json.loads(con.execute('SELECT context_json FROM job_reviews WHERE review_id=?', (rid,)).fetchone()[0])
            encoded['model_explanation'] = 'MODEL_ONLY_SENTINEL'
            con.execute('UPDATE job_reviews SET context_json=? WHERE review_id=?', (json.dumps(encoded), rid))
        self.assertNotIn('MODEL_ONLY_SENTINEL', json.dumps(self.command('context', review_id=rid)))


if __name__ == '__main__':
    unittest.main()
