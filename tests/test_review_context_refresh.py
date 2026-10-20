"""Current context, projection repair and lossless replay of pending evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter, AgentApplicationAdapter
from job_search.application_mail import capture_candidates
from job_search.applications.review import ReviewService
from job_search.applications.understanding.analyzer import understanding_schema, SharedAnalyzer, model_payload
from job_search.commands import CommandContext, Principal, DomainError
from job_search.review_migration import apply


class ReviewContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / 'owners.db')
        self.human = HumanApplicationAdapter(self.runtime, 'owner')
        self.serial, self.texts = 0, {}
        self.app = self.human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': 'role-one',
            'employer': 'Example Company Inc.', 'title': 'Engineer'}}, self.key())
        self.source = self.message('Thanks for applying to Example Company.')

    def key(self):
        self.serial += 1
        return str(self.serial)

    def message(self, text, *, account='account', direction='incoming', date='2026-10-14T12:00:00Z', thread='thread'):
        key = self.key()
        self.texts[key] = text
        return self.runtime.command(CommandContext(Principal('mail', 'worker', {'record_message'}), key, 'inferred'),
            'record_message', {'account_id': account, 'provider_message_id': key, 'source_version': 1,
                'direction': direction, 'authored_text': text, 'archive_ref': key, 'occurred_at': date, 'thread_id': thread})

    def output(self, context, *, target=True, submission_id=None, old_fact=False):
        source = context.sources[-1] if old_fact else context.sources[0]
        ref = {'source_id': source.source_id, 'revision': source.revision, 'start': 0, 'end': len(source.text), 'quote': source.text}
        return {'relevance': 'career_related', 'associations': [], 'requests': [], 'temporal_facts': [], 'uncertainties': [],
            'facts': [{'kind': 'submission', 'value': {'status': 'confirmed', 'submission_id': submission_id},
                       'target_id': self.app['id'] if target else None, 'evidence': [ref]}]}

    def analyze(self, *, prompt='3', retry=None, transform=None, retrieve=False):
        def run(context):
            self.context = context
            return transform(context) if transform else self.output(context)
        return self.runtime.analyze_message(self.source['id'], archive=SimpleNamespace(read_message=lambda ref: self.texts[ref]),
            allowed_accounts=('account',), analyzer=SimpleNamespace(prompt_version=prompt, analyze=run), model_version='fixture',
            candidate_ids=(self.app['id'],), candidate_coverage={'complete': True} if retrieve else None, processing_retry=retry)

    def queue_refresh(self, group_id=None):
        manifest = ReviewService(self.runtime).refresh_preview()
        if group_id is not None:
            manifest['groups'] = [group for group in manifest['groups'] if group['group_id'] == group_id]
            self.assertEqual(len(manifest['groups']), 1)
        receipt = apply(self.runtime, manifest, actor='owner')
        self.assertEqual(receipt['needs_refresh'], 0)
        self.assertEqual(receipt, apply(self.runtime, manifest, actor='owner'))
        with self.runtime.executor.read() as con:
            work = con.execute("SELECT payload FROM command_work WHERE kind='retry_processing' ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(work[0])

    def pending(self):
        with self.runtime.executor.read() as con:
            return self.runtime.understanding.list_pending(con)['items']

    def test_targeted_fact_supplies_pending_link_and_bad_receipt_id_is_removed(self):
        result = self.analyze(transform=lambda c: self.output(c, submission_id='invented'))
        link = next(p for p in result['proposals'] if p['operation'] == 'link_message')
        receipt = next(p for p in result['proposals'] if p['operation'] == 'record_submission_receipt')
        self.assertEqual(receipt['dependencies'], [link['id']])
        self.assertEqual(receipt['blockers'], [])
        self.assertNotIn('submission_id', receipt['input'])
        with self.runtime.executor.read() as con:
            self.assertIsNone(self.runtime.correspondence.association(con, self.source['id']))
            self.assertEqual(self.runtime.applications.list_records(con, self.app['id'], 'receipts')['items'], [])
        schema = understanding_schema(self.context)
        self.assertIn('"enum": [null]', json.dumps(schema))

    def test_explicit_unknown_association_does_not_guess_a_link(self):
        def output(context):
            result = self.output(context)
            result['associations'] = [{'kind': 'application', 'target_id': None, 'evidence': result['facts'][0]['evidence']}]
            return result
        result = self.analyze(transform=output)
        self.assertEqual([p['operation'] for p in result['proposals']], ['record_submission_receipt'])
        self.assertIn('unresolved_association', result['proposals'][0]['blockers'])

    def test_thread_is_account_and_time_scoped_and_cannot_create_new_facts(self):
        earlier = self.message('Example Company: Please complete the assessment.', date='2026-10-13T10:00:00Z')
        self.message('Secret other account', account='other', date='2026-10-13T10:00:00Z')
        self.message('Future message', date='2026-10-15T10:00:00Z')
        self.message('Future offset message', date='2026-10-14T08:00:00-06:00')
        self.message('Unsent draft', direction='draft', date='2026-10-13T10:00:00Z')
        self.source = self.message('Your application was received.', date='2026-10-14T13:00:00Z')
        result = self.analyze(retrieve=True, transform=lambda c: self.output(c, old_fact=True))
        self.assertIn(earlier['id'], [s.source_id for s in self.context.sources])
        self.assertEqual({s.direction for s in self.context.sources}, {'incoming'})
        self.assertFalse(any('Secret' in s.text or 'Future' in s.text or 'Unsent' in s.text for s in self.context.sources))
        self.assertEqual(self.context.candidates[0]['id'], self.app['id'])
        self.assertEqual(result['proposals'], [])

    def test_rejected_receipt_stays_rejected_when_refresh_adds_a_thread_citation(self):
        older = self.message('Example Company', date='2026-10-13T10:00:00Z')
        old = self.analyze()['proposals']
        receipt = next(p for p in old if p['operation'] == 'record_submission_receipt')
        self.human.command('review_changes', {'decisions': [{'proposal_id': receipt['id'], 'expected_version': receipt['version'],
            'decision': 'reject'}]}, self.key())
        retry = self.queue_refresh()
        def output(context):
            result = self.output(context)
            previous = next(s for s in context.sources if s.source_id == older['id'])
            result['facts'][0]['evidence'].append({'source_id': previous.source_id, 'revision': previous.revision,
                'start': 0, 'end': len(previous.text), 'quote': previous.text})
            return result
        self.analyze(prompt='5', retry=retry, transform=output)
        self.assertFalse(any(p['operation'] == 'record_submission_receipt' for p in self.pending()))
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.get(con, receipt['id'])['status'], 'rejected')

    def test_refresh_replaces_pending_atomically_without_accepting(self):
        old = self.analyze()['proposals']
        retry = self.queue_refresh()
        self.assertEqual({p['id'] for p in self.pending()}, {p['id'] for p in old})
        result = self.analyze(prompt=SharedAnalyzer.prompt_version, retry=retry)
        self.assertEqual(len(result['proposals']), 2)
        self.assertTrue(all(p['status'] == 'pending' and not p['blockers'] for p in result['proposals']))
        self.assertFalse({p['id'] for p in old} & {p['id'] for p in self.pending()})
        with self.runtime.executor.read() as con:
            self.assertTrue(all(self.runtime.understanding.get(con, p['id'])['status'] == 'superseded' for p in old))
        self.assertEqual(ReviewService(self.runtime).refresh_preview()['groups'], [])

    def test_bounded_optional_history_does_not_block_a_self_contained_receipt(self):
        for index in range(11):
            self.message('Earlier context ' + str(index), date='2026-10-13T10:00:00Z')
        result = self.analyze()
        self.assertTrue(self.context.coverage['complete'])
        self.assertFalse(self.context.context['thread_coverage']['complete'])
        self.assertEqual(len(self.context.sources), 11)
        self.assertTrue(all(not p['blockers'] for p in result['proposals']))

    def test_failed_refresh_preserves_pending_and_recovery_keeps_replacement_scope(self):
        old = self.analyze()['proposals']
        retry = self.queue_refresh()
        def fail(context):
            raise DomainError('invalid_output', 'Synthetic failure')
        self.assertEqual(self.analyze(prompt='5', retry=retry, transform=fail)['status'], 'failed')
        self.assertEqual({p['id'] for p in self.pending()}, {p['id'] for p in old})
        with self.runtime.executor.read() as con:
            issue = self.runtime.understanding.processing_for_source(con, self.source['id'], self.source['revision'])
        self.human.command('retry_processing', {'issue_id': issue['id'], 'expected_version': issue['version'],
            'expected_analysis_id': issue['analysis_id'], 'reason': 'Retry failed refresh'}, self.key())
        with self.runtime.executor.read() as con:
            next_retry = json.loads(con.execute("SELECT payload FROM command_work WHERE kind='retry_processing' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(next_retry['replaces'], retry['replaces'])

    def test_user_decision_during_refresh_is_not_overwritten(self):
        old = self.analyze()['proposals']
        retry = self.queue_refresh()
        self.human.command('review_changes', {'decisions': [{'proposal_id': p['id'], 'expected_version': p['version'],
            'decision': 'reject'} for p in old]}, self.key())
        self.assertEqual(self.analyze(prompt='5', retry=retry)['status'], 'superseded')
        self.assertEqual(self.pending(), [])
        self.assertEqual(ReviewService(self.runtime).page()['totals']['processing'], 0)

    def test_accepting_link_after_failed_refresh_retries_without_obsolete_replacement_scope(self):
        old = self.analyze()['proposals']
        retry = self.queue_refresh()
        def fail(context):
            raise DomainError('invalid_output', 'Synthetic failure')
        self.assertEqual(self.analyze(prompt='5', retry=retry, transform=fail)['status'], 'failed')
        link = next(p for p in old if p['operation'] == 'link_message')
        self.human.command('review_changes', {'decisions': [{'proposal_id': link['id'],
            'expected_version': link['version'], 'decision': 'accept'}]}, self.key())
        with self.runtime.executor.read() as con:
            queued = json.loads(con.execute("SELECT payload FROM command_work WHERE kind='retry_processing' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertNotIn('replaces', queued)
        self.assertNotEqual(queued['expected_version'], retry['expected_version'])
        result = self.analyze(prompt='5', retry=queued)
        self.assertNotEqual(result.get('status'), 'superseded')
        self.assertTrue(result['proposals'])
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.understanding.processing_for_source(con, self.source['id'], self.source['revision'])['status'], 'succeeded')
            self.assertEqual(self.runtime.understanding.get(con, link['id'])['status'], 'applied')

    def test_short_names_legal_suffixes_and_unrelated_titles(self):
        self.assertEqual(capture_candidates(self.runtime, self.source['id'], 'Thanks from Example Company')['candidate_ids'], [self.app['id']])
        self.assertEqual(capture_candidates(self.runtime, self.source['id'], 'Engineer application received')['candidate_ids'], [])
        with self.assertRaises(DomainError):
            AgentApplicationAdapter(self.runtime).call('refresh_review_group', {}, idempotency_key='forbidden')

    def test_full_role_title_retrieves_slug_employer_without_accepting_identity(self):
        candidate = self.human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': 'slug-role',
            'employer': 'acmecareersglobal', 'title': 'Software Engineer, Agentic Runtime'}}, self.key())
        result = capture_candidates(self.runtime, self.source['id'],
            'Acme has received your application for Software Engineer, Agentic Runtime.')
        self.assertEqual(result['candidate_ids'], [candidate['id']])
        with self.runtime.executor.read() as con:
            self.assertIsNone(self.runtime.correspondence.association(con, self.source['id']))
        self.assertEqual(capture_candidates(self.runtime, self.source['id'], 'An unrelated Engineer role')['candidate_ids'], [])

    def test_submission_schema_only_allows_projectable_status_vocabulary(self):
        self.analyze()
        facts = understanding_schema(self.context)['properties']['facts']['items']['anyOf']
        value = next(f['properties']['value'] for f in facts if f['properties']['kind']['enum'] == ['submission'])
        self.assertNotIn('anyOf', value)
        self.assertIn('confirmed', value['properties']['status']['enum'])
        self.assertNotIn('submission_status_confirmed', value['properties']['status']['enum'])

    def test_generic_receipts_retrieve_name_variants_without_role_titles(self):
        for slug, name, ats in [('northstar1', 'Northstar', 'greenhouse'),
                                ('riverbend-careers', 'Riverbend', 'ashby'),
                                ('joincedarworks', 'Cedar Works', 'lever'),
                                ('jobs-oakgrove-global', 'Oakgrove', 'external'),
                                ('equipe-munchen', 'München', 'greenhouse')]:
            with self.subTest(slug=slug):
                candidate = self.human.command('save_job', {'job_source': {'source': ats,
                    'source_id': slug, 'employer': slug, 'title': 'Software Engineer II'}}, self.key())
                text = 'Thank you for applying to ' + name + '! We have received your application.'
                result = capture_candidates(self.runtime, self.source['id'], text)
                self.assertEqual(result['candidate_ids'], [candidate['id']])
                self.assertTrue(result['coverage']['complete'])
                self.assertEqual(result['retrieval_reasons'][candidate['id']], ['current_employer_partial'])
                with self.runtime.executor.read() as con:
                    self.assertIsNone(self.runtime.correspondence.association(con, self.source['id']))
                    self.assertEqual(self.runtime.applications.get_job(con, candidate['job_id'])['employer'], slug)
                    self.assertEqual(self.runtime.applications.list_records(con, candidate['id'], 'receipts')['items'], [])

    def test_partial_name_hints_keep_competitors_and_report_candidate_limit(self):
        matches = []
        for slug in ['northstar1', 'northstar2', 'northstar-careers', 'northstarplus1', 'join-northstar']:
            matches.append(self.human.command('save_job', {'job_source': {'source': 'greenhouse',
                'source_id': slug, 'employer': slug, 'title': 'Engineer'}}, self.key())['id'])
        # Short overlapping words and entirely unrelated employers stay excluded.
        for slug, ats in [('careers', 'ashby'), ('ai1', 'greenhouse'), ('unrelated', 'external')]:
            self.human.command('save_job', {'job_source': {'source': ats,
                'source_id': slug, 'employer': slug, 'title': 'Engineer'}}, self.key())
        text = 'Thank you for applying to Northstar!'
        result = capture_candidates(self.runtime, self.source['id'], text)
        self.assertEqual(set(result['candidate_ids']), set(matches))
        self.assertEqual(set(result['retrieval_reasons']), set(matches))
        self.assertEqual(result['retrieval']['matched'], len(matches))
        self.assertEqual(capture_candidates(self.runtime, self.source['id'],
            'Thanks from NorthstarPlusPlus. Opportunities in AI.')['candidate_ids'], [])
        bounded = capture_candidates(self.runtime, self.source['id'], text, limit=2)
        self.assertEqual(len(bounded['candidate_ids']), 2)
        self.assertFalse(bounded['coverage']['complete'])
        self.assertFalse(bounded['retrieval']['complete'])
        self.assertEqual(bounded['retrieval']['matched'], len(matches))
        self.assertEqual(len(bounded['retrieval_reasons']), 2)
        self.assertEqual(set(capture_candidates(self.runtime, self.source['id'],
            'Your application was received.', thread_text=text)['candidate_ids']), set(matches))
        scan_bounded = capture_candidates(self.runtime, self.source['id'], text, limit=2, scan_limit=2)
        self.assertFalse(scan_bounded['retrieval']['complete'])
        self.assertEqual(scan_bounded['retrieval']['scanned'], 2)

    def test_refresh_recovers_empty_candidate_receipt_without_accepting_it(self):
        for slug, name, ats in [('northstar1', 'Northstar', 'greenhouse'),
                                ('riverbend-careers', 'Riverbend', 'ashby')]:
            with self.subTest(slug=slug):
                self.app = self.human.command('save_job', {'job_source': {'source': ats,
                    'source_id': slug, 'employer': slug, 'title': 'Software Engineer II'}}, self.key())
                self.source = self.message('Thank you for applying to ' + name + '! We have received your application.',
                                           thread=slug)
                # Reproduce a persisted pre-fix analysis with no candidate IDs.
                with patch('job_search.application_mail.capture_candidates', return_value={
                        'candidate_ids': [], 'coverage': {'complete': True}}):
                    old = self.analyze(retrieve=True, transform=lambda c: self.output(c, target=False))['proposals'][0]
                self.assertEqual(self.context.candidates, ())
                self.assertIn('needs_target', old['blockers'])
                retry = self.queue_refresh('email:' + self.source['id'])
                result = self.analyze(retrieve=True, retry=retry)
                self.assertEqual([c['id'] for c in self.context.candidates], [self.app['id']])
                self.assertEqual(self.context.candidates[0]['job']['employer'], slug)
                payload = model_payload(self.context)
                self.assertEqual(payload['candidate_retrieval']['method'], 'lexical_recall_v2')
                self.assertEqual(payload['candidates'][0]['retrieval_reasons'], ['current_employer_partial'])
                link = next(p for p in result['proposals'] if p['operation'] == 'link_message')
                receipt = next(p for p in result['proposals'] if p['operation'] == 'record_submission_receipt')
                self.assertEqual(receipt['application_id'], self.app['id'])
                self.assertEqual(receipt['dependencies'], [link['id']])
                self.assertNotIn('submission_id', receipt['input'])
                self.assertTrue(all(p['status'] == 'pending' and not p['blockers'] for p in result['proposals']))
                with self.runtime.executor.read() as con:
                    descriptor = self.runtime.understanding.get_analysis(con, result['analysis_id'])['descriptor']
                    self.assertEqual(descriptor['context']['candidate_retrieval'], payload['candidate_retrieval'])
                    self.assertEqual(descriptor['candidates'][0]['retrieval_reasons'], ['current_employer_partial'])
                    self.assertEqual(self.runtime.understanding.get(con, old['id'])['status'], 'superseded')
                    self.assertIsNone(self.runtime.correspondence.association(con, self.source['id']))
                    self.assertEqual(self.runtime.applications.list_records(con, self.app['id'], 'receipts')['items'], [])

    def test_whole_name_ranks_above_partial_overlap_without_excluding_it(self):
        candidate = self.human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': 'joined-role',
            'employer': 'examplerobotics', 'title': 'Developer'}}, self.key())
        result = capture_candidates(self.runtime, self.source['id'], 'Thank you for applying to Example Robotics!')
        self.assertEqual(result['candidate_ids'], [candidate['id'], self.app['id']])
        self.assertEqual(result['retrieval_reasons'][candidate['id']], ['current_employer_name'])
        self.assertEqual(result['retrieval_reasons'][self.app['id']], ['current_employer_partial'])

    def test_model_can_abstain_with_one_partial_candidate(self):
        self.app = self.human.command('save_job', {'job_source': {'source': 'ashby',
            'source_id': 'ambiguous', 'employer': 'join-northstar', 'title': 'Engineer'}}, self.key())
        self.source = self.message('Your application to Otherco was received. Our newsletter mentions Northstar.', thread='ambiguous')
        result = self.analyze(retrieve=True, transform=lambda c: self.output(c, target=False))
        self.assertEqual([c['id'] for c in self.context.candidates], [self.app['id']])
        self.assertEqual(len(result['proposals']), 1)
        self.assertIsNone(result['proposals'][0]['application_id'])
        self.assertIn('unresolved_association', result['proposals'][0]['blockers'])

    def test_truncated_retrieval_is_visible_to_model_and_blocks_projection(self):
        for index in range(21):
            self.app = self.human.command('save_job', {'job_source': {'source': 'ashby',
                'source_id': 'crowded-' + str(index), 'employer': 'join-northstar-' + str(index),
                'title': 'Engineer'}}, self.key())
        self.source = self.message('Thank you for applying to Northstar.', thread='crowded')
        def output(context):
            value = self.output(context)
            value['facts'][0]['target_id'] = context.candidates[0]['id']
            return value
        result = self.analyze(retrieve=True, transform=output)
        payload = model_payload(self.context)
        self.assertEqual(len(payload['candidates']), 20)
        self.assertEqual(payload['candidate_retrieval']['matched'], 21)
        self.assertFalse(payload['candidate_retrieval']['complete'])
        self.assertFalse(payload['coverage']['complete'])
        self.assertTrue(result['proposals'])
        self.assertTrue(all('incomplete_evidence_coverage' in p['blockers'] for p in result['proposals']))


if __name__ == '__main__':
    unittest.main()
