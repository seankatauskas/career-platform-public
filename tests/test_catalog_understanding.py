"""Catalog identities remain reviewable without fabricated browser applications."""
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_review_application_discovery as discovery_fixtures
from job_search.application_mail import capture_candidates
from job_search.applications.understanding.analyzer import model_payload, understanding_schema, SharedAnalyzer
from job_search.applications.understanding.contracts import validate_analysis
from job_search.commands import DomainError


class CatalogUnderstandingTest(unittest.TestCase):
    setUp = discovery_fixtures.DiscoveryTest.setUp
    message = discovery_fixtures.DiscoveryTest.message
    group = discovery_fixtures.DiscoveryTest.group
    create = discovery_fixtures.DiscoveryTest.create

    def posting(self, identity='7703668003', company='axon', title='Software Engineer I', location='Seattle', closed=None):
        with sqlite3.connect(self.catalog.jobs_db) as con:
            if 'location' not in {r[1] for r in con.execute('PRAGMA table_info(jobs)')}:
                con.execute('ALTER TABLE jobs ADD COLUMN location TEXT')
                con.execute('CREATE INDEX jobs_company ON jobs(ats,company)')
                con.execute('CREATE UNIQUE INDEX jobs_identity ON jobs(ats,id)')
            con.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', ('greenhouse', identity, company, title,
                f'https://job-boards.greenhouse.io/{company}/jobs/{identity}', closed, location))

    def analyze(self, text=None, choose=True, prior_application=None):
        source, _ = self.message(text=text or 'Axon: We received your application for Software Engineer I in Seattle, posting 7703668003.')
        self.source = source
        if prior_application:
            from job_search.commands import CommandContext, Principal
            self.runtime.executor.run(CommandContext(Principal('owner', 'human', frozenset({'*'})), 'prior-link'),
                'link_message', {}, lambda tx: self.runtime.correspondence.link_message(tx, message_id=source['id'], application_id=prior_application))
        def run(context):
            self.context = context
            s = context.sources[0]
            evidence = [{'source_id': s.source_id, 'revision': s.revision, 'start': 0, 'end': len(s.text), 'quote': s.text}]
            target = context.context['catalog_candidates'][0]['id'] if choose else None
            self.output = {'relevance': 'career_related', 'associations': [{'kind': 'job', 'target_id': target, 'evidence': evidence}],
                'facts': [{'kind': 'submission', 'value': {'status': 'confirmed'}, 'target_id': None, 'evidence': evidence}],
                'requests': [], 'temporal_facts': [], 'uncertainties': [] if choose else ['Location or posting ID is missing.']}
            return self.output
        return self.runtime.analyze_message(source['id'], archive=self.runtime.mail_reader.archive,
            allowed_accounts=('personal',), analyzer=SimpleNamespace(prompt_version=SharedAnalyzer.prompt_version, analyze=run),
            model_version='fixture', candidate_coverage={'complete': True})

    def selection(self):
        group = self.group(self.source)
        candidate = group['catalog_candidates'][0]
        return {'group_id': group['id'], 'expected_digest': group['preview_digest'],
            'expected_match_digest': candidate['match_digest'], 'catalog_id': candidate['id'],
            'employer': candidate['job']['employer'], 'title': candidate['job']['title']}

    def test_catalog_reaches_model_without_creating_an_application(self):
        self.posting()
        result = self.analyze()
        payload = model_payload(self.context)
        self.assertEqual(payload['candidates'], [])
        role = payload['catalog_candidates'][0]
        self.assertFalse(role['browser_submission_observed'])
        self.assertFalse(role['application_recorded'])
        self.assertEqual(role['job']['location'], 'Seattle')
        self.assertTrue(self.context.coverage['complete'])
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])
        self.assertTrue(self.group(self.source)['catalog_candidates'][0]['suggested'])
        self.assertTrue(all(p['application_id'] is None for p in result['proposals']))
        schema = understanding_schema(self.context)
        application, job = schema['properties']['associations']['items']['anyOf']
        self.assertNotIn(role['id'], application['properties']['target_id']['enum'])
        self.assertIn(role['id'], job['properties']['target_id']['enum'])
        # A catalog target cannot leak into application mutation recipes.
        self.output['facts'][0]['target_id'] = role['id']
        with self.assertRaises(DomainError):
            validate_analysis(self.output, self.context)

    def test_selection_creates_exact_identity_but_receipt_stays_pending(self):
        self.posting()
        self.analyze()
        command = self.selection()
        result = self.create(command)
        self.assertEqual(result, self.create(command))
        workspace = self.runtime.queries.workspace(result['application_id'])
        self.assertEqual(workspace['progress']['stage'], 'tracking')
        for kind in ('submissions', 'receipts', 'observations'):
            with self.runtime.executor.read() as con:
                self.assertEqual(self.runtime.applications.list_records(con, result['application_id'], kind)['items'], [])
        with self.runtime.executor.read() as con:
            job = self.runtime.applications.get_job(con, result['job_id'])
            self.assertEqual(job['sources'], [{'source': 'greenhouse', 'source_id': '7703668003'}])
            self.assertEqual(self.runtime.correspondence.association(con, self.source['id'])['application_id'], result['application_id'])
            receipts = [self.runtime.understanding.get(con, p) for p in result['pending_proposals']]
            self.assertTrue(all(p['status'] == 'pending' and not p['blockers'] for p in receipts))

    def test_late_browser_or_saved_identity_is_reused(self):
        self.posting()
        self.analyze()
        command = self.selection()
        existing = self.human.command('save_job', {'job_source': {'source': 'greenhouse', 'source_id': '7703668003',
            'employer': 'axon', 'title': 'Software Engineer I'}}, 'late')
        result = self.create(command)
        self.assertEqual(result['application_id'], existing['id'])
        self.assertEqual(len(self.runtime.queries.list_applications()['items']), 1)

    def test_changed_or_forged_selection_cannot_create(self):
        self.posting()
        self.analyze()
        command = self.selection()
        with self.assertRaises(DomainError):
            self.create({**command, 'catalog_id': 'catalog:greenhouse:forged'})
        with sqlite3.connect(self.catalog.jobs_db) as con:
            con.execute("UPDATE jobs SET location='Romania'")
        with self.assertRaises(DomainError):
            self.create(command)
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])

    def test_same_title_locations_and_closed_postings_remain_competitors(self):
        self.posting()
        self.posting('7818932003', location='Romania')
        self.posting('7576576003', closed='2026-10-01', location='Seattle')
        self.posting('1234567890', title='Software Engineer II')
        self.analyze('Axon: We received your application for Software Engineer I.', choose=False)
        candidates = self.context.context['catalog_candidates']
        self.assertEqual({c['job']['source_id'] for c in candidates}, {'7703668003', '7818932003', '7576576003'})
        self.assertFalse(any(c['suggested'] for c in self.group(self.source)['catalog_candidates']))

    def test_partial_board_names_id_only_and_existing_application_deduplication(self):
        self.posting(company='careers-axon1')
        for text in ('Axon Software Engineer I', 'Your posting 7703668003 was received'):
            result = self.catalog.understanding_candidates(text)
            self.assertEqual(len(result['candidates']), 1)
        self.human.command('save_job', {'job_source': {'source': 'greenhouse', 'source_id': '7703668003',
            'employer': 'careers-axon1', 'title': 'Software Engineer I'}}, 'saved')
        source, _ = self.message(text='Axon Software Engineer I')
        result = capture_candidates(self.runtime, source['id'], 'Axon Software Engineer I')
        self.assertEqual(result['catalog_candidates'], [])
        self.assertEqual(len(result['candidate_ids']), 1)

    def test_catalog_overflow_and_unavailability_are_explicit(self):
        for i in range(21):
            self.posting(str(7703668003+i))
        self.analyze(choose=False)
        self.assertFalse(self.context.context['catalog_retrieval']['complete'])
        self.assertFalse(self.context.coverage['complete'])
        self.assertEqual(len(self.context.context['catalog_candidates']), 20)
        self.catalog.jobs_db.unlink()
        result = capture_candidates(self.runtime, self.source['id'], 'Axon')
        self.assertEqual(result['catalog_retrieval']['reason'], 'catalog_unavailable')

    def test_incomplete_application_scan_does_not_claim_absence(self):
        self.posting()
        source, _ = self.message()
        with patch.object(self.runtime.applications, 'list_applications', return_value={'items': [], 'truncated': True}):
            result = capture_candidates(self.runtime, source['id'], 'Axon Software Engineer I')
        self.assertIsNone(result['catalog_candidates'][0]['application_recorded'])
        self.assertIsNone(result['catalog_candidates'][0]['browser_submission_observed'])
        self.assertFalse(result['coverage']['complete'])

    def test_catalog_match_cannot_inherit_an_earlier_wrong_application_link(self):
        self.posting()
        existing = self.human.command('save_job', {'job_source': {'source': 'greenhouse', 'source_id': '9999999999',
            'employer': 'Other Company', 'title': 'Software Engineer I'}}, 'other')
        result = self.analyze(prior_application=existing['id'])
        self.assertTrue(result['proposals'])
        self.assertTrue(all(p['application_id'] is None and 'needs_target' in p['blockers'] for p in result['proposals']))
        self.output['facts'][0]['target_id'] = existing['id']
        with self.assertRaises(DomainError):
            validate_analysis(self.output, self.context)

    def test_prompt_explains_catalog_caveat_and_full_evidence(self):
        self.posting()
        self.analyze()
        captured = {}
        def generate(messages, **kwargs):
            captured['instructions'] = messages[0]['content']
            captured['payload'] = json.loads(messages[1]['content'])
            return SimpleNamespace(text=json.dumps(self.output), usage={})
        provider = SimpleNamespace(max_input_tokens=200000, count_tokens_upper_bound=lambda text: len(text)//3, generate=generate)
        self.assertEqual(SharedAnalyzer(provider).analyze(self.context), self.output)
        self.assertIn('extension may have missed', captured['instructions'])
        self.assertIn('never proof', captured['instructions'])
        self.assertEqual(captured['payload']['sources'][0]['text'], self.bodies['one'])


if __name__ == '__main__':
    unittest.main()
