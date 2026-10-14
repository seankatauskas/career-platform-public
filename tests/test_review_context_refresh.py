"""Current context, projection repair and lossless replay of pending evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter, AgentApplicationAdapter
from job_search.application_mail import capture_candidates
from job_search.applications.review import ReviewService
from job_search.applications.understanding.analyzer import understanding_schema, _anchor_evidence, SharedAnalyzer
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

    def queue_refresh(self):
        manifest = ReviewService(self.runtime).refresh_preview()
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

    def test_joined_employer_slug_matches_display_name_without_substrings(self):
        candidate = self.human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': 'joined-role',
            'employer': 'examplerobotics', 'title': 'Developer'}}, self.key())
        self.assertEqual(capture_candidates(self.runtime, self.source['id'],
            'Thank you for applying to Example Robotics!')['candidate_ids'], [candidate['id']])
        self.assertEqual(capture_candidates(self.runtime, self.source['id'],
            'Thank you for applying to Example RoboticsPlus!')['candidate_ids'], [])


if __name__ == '__main__':
    unittest.main()
