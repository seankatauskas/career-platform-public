"""Routine receipt wording never invents review progress or a closing outcome."""
import json
import unittest
from types import SimpleNamespace

from tests import test_review_application_discovery as fixtures
from job_search.applications.review import ReviewService
from job_search.applications.understanding.analyzer import SharedAnalyzer, understanding_schema
from job_search.applications.understanding.recipes import fact_recipe
from job_search.commands import CommandContext, Principal
from job_search.review_migration import apply


class ReceiptOutcomeTest(unittest.TestCase):
    message = fixtures.DiscoveryTest.message

    def setUp(self):
        fixtures.DiscoveryTest.setUp(self)
        self.analysis_serial = 0
        self.app = self.human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': 'revel',
            'employer': 'revel', 'title': 'Frontend Software Engineer, Data Platform'}}, 'save')
        self.source, _ = self.message(propose=False, text='Thanks for applying to Revel!\n'
            'Thank you for applying for the Frontend Software Engineer, Data Platform role at Revel!\n'
            'Our team is looking over your application.')

    def output(self, context, outcome='under_review', receipt=True, request=False):
        self.context = context
        s = context.sources[0]
        def evidence(quote):
            start = s.text.index(quote)
            return [{'source_id': s.source_id, 'revision': s.revision,
                     'start': start, 'end': start + len(quote), 'quote': quote}]
        facts = [{'kind': 'outcome', 'value': {'outcome': outcome}, 'target_id': self.app['id'],
                  'evidence': evidence('Our team is looking over your application.')}]
        if receipt:
            facts.insert(0, {'kind': 'submission', 'value': {'status': 'confirmed'}, 'target_id': self.app['id'],
                            'evidence': evidence('Thanks for applying to Revel!')})
        requests = [{'kind': 'assessment', 'outcome': 'Complete the coding exercise', 'target_id': self.app['id'],
                     'requirement': 'required', 'responsible_party': 'applicant', 'channel': 'portal',
                     'evidence': evidence('Please complete the coding exercise.')} ] if request else []
        return {'relevance': 'career_related', 'associations': [], 'facts': facts, 'requests': requests,
                'temporal_facts': [], 'uncertainties': []}

    def analyze(self, **options):
        self.analysis_serial += 1
        return self.runtime.analyze_message(self.source['id'], archive=self.runtime.mail_reader.archive,
            allowed_accounts=['personal'], candidate_ids=[self.app['id']],
            analyzer=SimpleNamespace(prompt_version=SharedAnalyzer.prompt_version, analyze=lambda c: self.output(c, **options)),
            model_version='fixture-' + str(self.analysis_serial))

    def test_revel_receipt_preserved_when_model_invents_under_review(self):
        result = self.analyze()
        self.assertEqual({p['operation'] for p in result['proposals']}, {'link_message', 'record_submission_receipt'})
        self.assertTrue(all(p['status'] == 'pending' and not p['blockers'] for p in result['proposals']))
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.applications.get_application(con, self.app['id'])['disposition'], 'open')
            self.assertEqual(self.runtime.applications.list_records(con, self.app['id'], 'progress')['items'], [])
            self.assertEqual(self.runtime.understanding.processing_for_source(con, self.source['id'], self.source['revision'])['status'], 'succeeded')
        self.human.command('review_changes', {'decisions': [{'proposal_id': p['id'], 'expected_version': p['version'],
            'decision': 'accept'} for p in result['proposals']]}, 'accept-receipt')
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.applications.get_application(con, self.app['id'])['disposition'], 'open')
            self.assertEqual(self.runtime.applications.list_records(con, self.app['id'], 'progress')['items'], [])
            self.assertEqual(len(self.runtime.applications.list_records(con, self.app['id'], 'receipts')['items']), 1)

    def test_nonterminal_outcome_alone_creates_no_link_or_business_proposal(self):
        for outcome in ('under_review', 'reviewing', 'received', 'in_progress', 'unknown', None, []):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.analyze(outcome=outcome, receipt=False)['proposals'], [])

    def test_receipt_does_not_suppress_independent_applicant_request(self):
        self.source, _ = self.message('assessment', propose=False, text=self.bodies['one'] + '\nPlease complete the coding exercise.')
        result = self.analyze(request=True)
        self.assertEqual({p['operation'] for p in result['proposals']},
                         {'link_message', 'record_submission_receipt', 'record_request'})

    def test_thanks_for_applying_does_not_hide_an_explicit_rejection(self):
        rejection = 'We will not be moving forward with your application.'
        self.source, _ = self.message('rejected', propose=False,
            text='Thanks for applying to Revel!\n' + rejection)
        def analyze(context):
            s = context.sources[0]
            def fact(kind, value, quote):
                start = s.text.index(quote)
                return {'kind': kind, 'value': value, 'target_id': self.app['id'], 'evidence': [
                    {'source_id': s.source_id, 'revision': s.revision, 'start': start,
                     'end': start + len(quote), 'quote': quote}]}
            return {'relevance': 'career_related', 'associations': [], 'facts': [
                fact('submission', {'status': 'confirmed'}, 'Thanks for applying to Revel!'),
                fact('outcome', {'outcome': 'rejected'}, rejection)],
                'requests': [], 'temporal_facts': [], 'uncertainties': []}
        result = self.runtime.analyze_message(self.source['id'], archive=self.runtime.mail_reader.archive,
            allowed_accounts=['personal'], candidate_ids=[self.app['id']],
            analyzer=SimpleNamespace(analyze=analyze), model_version='rejection-fixture')
        closure = next(p for p in result['proposals'] if p['operation'] == 'close_application')
        self.assertEqual(closure['input']['outcome'], 'rejected')
        self.assertEqual(closure['blockers'], [])
        self.assertEqual(closure['status'], 'pending')

    def test_schema_restricts_outcomes_without_free_text_escape(self):
        self.analyze()
        facts = understanding_schema(self.context)['properties']['facts']['items']['anyOf']
        value = next(f['properties']['value'] for f in facts if f['properties']['kind']['enum'] == ['outcome'])
        self.assertEqual(value['type'], 'object')
        self.assertEqual(set(value['properties']['outcome']['enum']), {'accepted', 'rejected', 'withdrawn', 'stopped_pursuing'})
        from tests.fixtures.mail_provider import StagedMailProvider
        value = self.output(self.context)
        value['facts'] = value['facts'][:1]
        provider = StagedMailProvider(value)
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual([f['kind'] for f in result['facts']], ['submission'])
        prompt = provider.calls[0][0][0]['content']
        self.assertIn('adds no separate status, progress, contact, assessment, or outcome', prompt)
        self.assertIn('independent, explicit applicant request', prompt)

    def test_terminal_outcomes_remain_reviewable_and_unknowns_have_no_recipe(self):
        descriptor = {'versions': {}, 'context': {'closure_previews': {self.app['id']: {'expected_version': 1, 'expected_records': {}}}}}
        for outcome in ('accepted', 'rejected', 'withdrawn', 'stopped_pursuing', 'under_review', 'received', 'pending', None, []):
            for value in (outcome, {'outcome': outcome}):
                with self.subTest(value=value):
                    recipe = fact_recipe({'content': {'kind': 'outcome', 'value': value, 'evidence': []}}, self.app['id'], descriptor)
                    if isinstance(outcome, str) and outcome in {'accepted', 'rejected', 'withdrawn', 'stopped_pursuing'}:
                        self.assertEqual(recipe[0], 'close_application')
                        self.assertEqual(recipe[1]['outcome'], outcome)
                        self.assertEqual(recipe[2], [])
                    else:
                        self.assertIsNone(recipe)

    def test_reviewed_backlog_cleanup_removes_old_closure_but_keeps_receipt_and_history(self):
        result = self.analyze()
        with self.runtime.executor.read() as con:
            analysis = self.runtime.understanding.get_analysis(con, result['analysis_id'])
        finding = next(f for f in analysis['findings'] if f['content']['kind'] == 'outcome')
        link = next(p for p in result['proposals'] if p['operation'] == 'link_message')
        # Reproduce the proposal persisted by the previous mapper, through its owner API.
        old = self.agent.call('propose_changes', {'operation': 'close_application', 'application_id': self.app['id'],
            'input': {'application_id': self.app['id'], 'outcome': 'under_review', 'expected_version': 1,
                      'expected_records': {}, 'reason': 'Reviewed external outcome: under_review'},
            'analysis_id': analysis['id'], 'finding_id': finding['id'], 'evidence': finding['content']['evidence'],
            'blockers': ['needs_mapping'], 'dependencies': [link['id']]}, idempotency_key='legacy-closure')
        preview = ReviewService(self.runtime).migration_preview()
        group = next(g for g in preview['groups'] if g['group_id'] == 'email:' + self.source['id'])
        self.assertEqual(next(d for d in group['decisions'] if d['proposal_id'] == old['id'])['disposition'], 'supersede_nonterminal_outcome')
        preview['groups'] = [group]
        applied = apply(self.runtime, preview, actor='owner')
        self.assertEqual(applied['completed'], 1)
        self.assertEqual(applied, apply(self.runtime, preview, actor='owner'))
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.get(con, old['id'])['status'], 'superseded')
            self.assertEqual(self.runtime.understanding.get_analysis(con, analysis['id'])['output'], analysis['output'])
            self.assertTrue(all(self.runtime.understanding.get(con, p['id'])['status'] == 'pending' for p in result['proposals']))
            self.assertEqual(self.runtime.applications.get_application(con, self.app['id'])['disposition'], 'open')
        projected = self.runtime.executor.run(CommandContext(Principal('worker', 'worker', {'project_analysis'}), 'reproject', 'inferred'),
            'project_analysis', {}, lambda tx: self.runtime.understanding.project_analysis(tx, analysis_id=analysis['id'],
                projections=[{'finding_id': finding['id'], 'operation': 'close_application', 'input': old['input'], 'application_id': self.app['id']}]))
        self.assertEqual(projected['proposals'], [])


if __name__ == '__main__':
    unittest.main()
