"""Historical evidence must not ask humans to reapply authoritative facts."""
from tests import test_redesign_workflows as fixtures
import unittest
from job_search.applications.understanding.analyzer import model_payload


class ProposalReconciliationTest(unittest.TestCase):
    setUp = fixtures.WorkflowsTest.setUp
    command = fixtures.WorkflowsTest.command
    message = fixtures.WorkflowsTest.message
    def analyze_fact(self, kind, value, *, during_analysis=None, link=True):
        message = self.message()
        app_id = self.app['id']
        self.analysis_payload = payload = {}
        if link:
            self.command('link_message', {'message_id': message['id'], 'application_id': app_id})
        class Archive:
            def read_message(self, reference): return 'Please reply.'
        class Analyzer:
            def analyze(self, context):
                payload.update(model_payload(context))
                if during_analysis: during_analysis()
                source = context.sources[0]
                evidence = [{'source_id': source.source_id, 'revision': source.revision,
                             'start': 0, 'end': 13, 'quote': 'Please reply.'}]
                return {'relevance': 'career_related', 'associations': [],
                        'facts': [{'kind': kind, 'value': value, 'target_id': app_id, 'evidence': evidence}],
                        'requests': [], 'temporal_facts': [], 'uncertainties': []}
        return self.runtime.analyze_message(message['id'], archive=Archive(), allowed_accounts=['account'],
            analyzer=Analyzer(), model_version='fixture', candidate_ids=[app_id], target_application_id=app_id)

    def test_closed_outcome_restatement_is_retained_as_superseded(self):
        app = self.command('close_application', {'application_id': self.app['id'],
            'expected_version': self.app['version'], 'outcome': 'rejected', 'reason': 'Previously recorded'})
        result = self.analyze_fact('outcome', {'outcome': 'rejected', 'reason': 'Old rejection email'})
        self.assertEqual(result['proposals'][0]['status'], 'superseded')
        self.assertIn('Already recorded', result['proposals'][0]['reason'])
        self.assertTrue(result['proposals'][0]['evidence'])
        self.assertEqual(self.analysis_payload['candidates'][0]['lifecycle'],
                         {'disposition': 'closed', 'outcome': 'rejected', 'pursuit_no': 1})
        view = self.runtime.queries.workspace(self.app['id'])
        self.assertEqual(view['application']['version'], app['version'])
        self.assertEqual(view['review']['items'], [])

    def test_conflicting_outcome_stays_pending(self):
        self.command('close_application', {'application_id': self.app['id'],
            'expected_version': self.app['version'], 'outcome': 'rejected', 'reason': 'Previously recorded'})
        result = self.analyze_fact('outcome', {'outcome': 'withdrawn', 'reason': 'Different outcome'})
        self.assertEqual(result['proposals'][0]['status'], 'pending')

    def submission(self):
        return self.command('record_submission', {'application_id': self.app['id'],
            'status': 'confirmed', 'occurred_at': '2026-10-01T12:00:00Z'})

    def test_exact_submission_is_superseded_without_mutating_it(self):
        record = self.submission()
        result = self.analyze_fact('submission', {k: record[k] for k in ('submission_id', 'status', 'occurred_at')})
        self.assertEqual(result['proposals'][0]['status'], 'superseded')
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.applications.get_record(con, 'submissions', record['id']), record)

    def test_different_timestamp_remains_a_review(self):
        record = self.submission()
        result = self.analyze_fact('submission', {'submission_id': record['id'], 'status': 'confirmed',
            'occurred_at': '2026-10-02T12:00:00Z'})
        self.assertEqual(result['proposals'][0]['status'], 'pending')

    def test_uncertain_association_is_not_dismissed(self):
        record = self.submission()
        result = self.analyze_fact('submission', {k: record[k] for k in ('submission_id', 'status', 'occurred_at')}, link=False)
        self.assertEqual(result['proposals'][0]['status'], 'pending')
        self.assertIn('unresolved_association', result['proposals'][0]['blockers'])

    def test_closing_during_analysis_does_not_discard_stale_context(self):
        def close():
            self.command('close_application', {'application_id': self.app['id'],
                'expected_version': self.app['version'], 'outcome': 'rejected', 'reason': 'Concurrent decision'})
        result = self.analyze_fact('outcome', {'outcome': 'rejected', 'reason': 'Email outcome'}, during_analysis=close)
        self.assertEqual(result['proposals'][0]['status'], 'pending')

    def test_changed_record_during_analysis_remains_a_review(self):
        record = self.submission()
        result = self.analyze_fact('submission', {k: record[k] for k in ('submission_id', 'status', 'occurred_at')},
            during_analysis=lambda: self.command('record_submission', {'application_id': self.app['id'],
                'submission_id': record['id'], 'expected_version': record['version'], 'status': 'retracted'}))
        self.assertEqual(result['proposals'][0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
