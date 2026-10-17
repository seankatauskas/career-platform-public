"""One visible update can atomically accept its exact email-link prerequisite."""
import unittest
from unittest.mock import patch

from tests import test_redesign_workflows as fixtures
from job_search.applications.review import ReviewService
from job_search.commands import DomainError


class ReviewBundleTest(unittest.TestCase):
    command = fixtures.WorkflowsTest.command
    proposal = fixtures.WorkflowsTest.proposal
    message = fixtures.WorkflowsTest.message

    def setUp(self):
        fixtures.WorkflowsTest.setUp(self)
        self.source = self.message()
        self.ref = {'owner':'correspondence', 'source_id':self.source['id'],
                    'revision':self.source['revision'], 'sha256':self.source['source_sha256']}
        self.link = self.proposal('link_message', {'message_id':self.source['id'], 'application_id':self.app['id']}, evidence=[self.ref])
        self.receipt = self.proposal('record_submission_receipt', {'application_id':self.app['id'], 'evidence':[self.ref]},
                                    evidence=[self.ref], dependencies=[self.link['id']])

    def payload(self, *proposals, decision='accept'):
        group = ReviewService(self.runtime).page(group_id='email:' + self.source['id'])['items'][0]
        return {'group_id':group['id'], 'expected_digest':group['preview_digest'], 'decisions':[
            {'proposal_id':p['id'], 'expected_version':p['version'], 'decision':decision} for p in proposals]}

    def assert_untouched(self):
        with self.runtime.executor.read() as con:
            self.assertIsNone(self.runtime.correspondence.association(con, self.source['id']))
            self.assertEqual(self.runtime.understanding.get(con, self.link['id'])['status'], 'pending')
            self.assertEqual(self.runtime.understanding.get(con, self.receipt['id'])['status'], 'pending')
            self.assertEqual(self.runtime.applications.list_records(con, self.app['id'], 'receipts')['items'], [])

    def test_receipt_and_link_apply_once_without_accepting_independent_task(self):
        task = self.proposal('create_task', {'application_id':self.app['id'], 'kind':'reply', 'description':'Reply'},
                             evidence=[self.ref], dependencies=[self.link['id']])
        payload = self.payload(self.receipt, self.link)
        result = self.human.command('review_changes', payload, 'one-click')
        self.assertEqual(result, self.human.command('review_changes', payload, 'one-click'))
        self.assertEqual(len(result['decisions']), 2)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.correspondence.association(con, self.source['id'])['application_id'], self.app['id'])
            self.assertEqual(len(self.runtime.applications.list_records(con, self.app['id'], 'receipts')['items']), 1)
            self.assertEqual(self.runtime.understanding.get(con, task['id'])['status'], 'pending')
            self.assertEqual(self.runtime.applications.list_records(con, self.app['id'], 'tasks')['items'], [])

    def test_new_proposal_invalidates_displayed_group_without_partial_link(self):
        payload = self.payload(self.receipt, self.link)
        self.proposal('add_note', {'application_id':self.app['id'], 'text':'New interpretation'}, evidence=[self.ref])
        with self.assertRaises(DomainError) as error:
            self.command('review_changes', payload)
        self.assertEqual(error.exception.code, 'version_conflict')
        self.assert_untouched()

    def test_foreign_proposal_and_partial_preview_are_rejected(self):
        foreign = self.proposal('add_note', {'application_id':self.app['id'], 'text':'Other evidence'})
        for payload in (self.payload(self.receipt, self.link, foreign),
                        {k:v for k,v in self.payload(self.receipt,self.link).items() if k != 'expected_digest'},
                        {**self.payload(self.receipt,self.link), 'decisions':[{'proposal_id':[]}]}):
            with self.assertRaises(DomainError):
                self.command('review_changes', payload)
            self.assert_untouched()

    def test_failure_after_link_rolls_back_entire_bundle(self):
        with patch.object(self.runtime.applications, 'record_submission_receipt', side_effect=DomainError('invalid_input', 'fixture')):
            with self.assertRaises(DomainError):
                self.command('review_changes', self.payload(self.receipt,self.link))
        self.assert_untouched()

    def test_missing_dependency_is_never_implicitly_accepted_on_server(self):
        with self.assertRaises(DomainError):
            self.command('review_changes', self.payload(self.receipt))
        self.assert_untouched()

    def test_dismiss_preserves_evidence_without_linking_or_recording_receipt(self):
        self.command('review_changes', self.payload(self.receipt, self.link, decision='reject'))
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.correspondence.get(con, self.source['id'])['revision'], self.source['revision'])
            self.assertIsNone(self.runtime.correspondence.association(con, self.source['id']))
            self.assertEqual(self.runtime.understanding.get(con, self.receipt['id'])['status'], 'rejected')
            self.assertEqual(self.runtime.applications.list_records(con, self.app['id'], 'receipts')['items'], [])


if __name__ == '__main__':
    unittest.main()
