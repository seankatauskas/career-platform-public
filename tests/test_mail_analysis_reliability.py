"""Reliability contracts: precise recovery, semantic scoring and honest evidence."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import unittest

from job_search.applications.understanding import SharedAnalyzer
from job_search.applications.understanding.evaluation import evaluate_case, summarize
from job_search.applications.understanding.validation import check, OutputError, validate_diagnostic
from job_search.commands import DomainError
from tests import test_mail_evidence_blocks as fixtures


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BlockTests()
        self.fixture.setUp()
        self.context = replace(self.fixture.context, candidates=(
            *self.fixture.context.candidates, {'id': 'app-2', 'job': {'employer': 'Other', 'title': 'Engineer'}}))
        self.case = {'name': 'synthetic-receipt', 'context': asdict(self.context), 'expected': [
            {'category': 'associations', 'kind': 'application', 'target_id': 'app-1'},
            {'category': 'facts', 'kind': 'submission', 'target_id': 'app-1', 'value': {'status': 'confirmed'}}]}

    def test_correct_fact_and_application_are_both_required(self):
        provider = fixtures.Provider([self.fixture.extracted(), self.fixture.bound()])
        report = evaluate_case(self.case, SharedAnalyzer(provider))
        self.assertTrue(report['passed'])
        self.assertEqual(report['matched_findings'], 2)
        self.assertNotIn('Thank you', json.dumps(report))
        self.assertNotIn('application is in the queue', json.dumps(report))

    def test_schema_valid_wrong_application_fails_semantic_gate(self):
        provider = fixtures.Provider([self.fixture.extracted(), self.fixture.bound('app-2')])
        report = evaluate_case(self.case, SharedAnalyzer(provider))
        self.assertFalse(report['passed'])
        self.assertEqual((report['wrong_targets'], report['missed_findings'], report['unexpected_findings']), (2, 2, 2))

    def test_abstention_is_measured_as_missing_for_an_unambiguous_case(self):
        report = evaluate_case(self.case, SharedAnalyzer(fixtures.Provider([self.fixture.extracted(), self.fixture.bound(None)])))
        self.assertFalse(report['passed'])
        self.assertEqual(report['wrong_targets'], 0)
        self.assertGreater(report['missed_findings'], 0)

    def test_ambiguous_case_passes_only_with_unassigned_fact(self):
        case = deepcopy(self.case)
        case['expected'] = [{'category': 'facts', 'kind': 'submission', 'target_id': None, 'value': {'status': 'confirmed'}}]
        report = evaluate_case(case, SharedAnalyzer(fixtures.Provider([self.fixture.extracted(), self.fixture.bound(None)])))
        self.assertTrue(report['passed'])
        wrong = evaluate_case(case, SharedAnalyzer(fixtures.Provider([self.fixture.extracted(), self.fixture.bound()])))
        self.assertFalse(wrong['passed'])
        self.assertGreater(wrong['wrong_targets'], 0)

    def test_empty_valid_output_and_missing_independent_request_fail(self):
        case = deepcopy(self.case)
        case['expected'].append({'category': 'requests', 'kind': 'assessment', 'target_id': 'app-1'})
        report = evaluate_case(case, SharedAnalyzer(fixtures.Provider([self.fixture.extracted(), self.fixture.bound()])))
        self.assertEqual(report['missed_findings'], 1)
        self.assertFalse(report['passed'])
        empty = evaluate_case(self.case, SharedAnalyzer(fixtures.Provider([self.fixture.extracted([])])))
        self.assertFalse(empty['passed'])
        self.assertEqual(empty['missed_findings'], 2)

    def test_duplicate_predictions_do_not_improve_recall(self):
        extraction = self.fixture.extracted([self.fixture.receipt(), self.fixture.receipt()])
        binding = self.fixture.bound()
        report = evaluate_case(self.case, SharedAnalyzer(fixtures.Provider([extraction, binding])))
        self.assertEqual(report['matched_findings'], 2)
        self.assertEqual(report['unexpected_findings'], 1)
        self.assertFalse(report['passed'])

    def test_unsupported_extra_fact_is_not_an_incorrect_application_identity(self):
        contact = {'kind': 'contact', 'value': 'recruiter_contact', 'target_id': None,
                   'confidence': None, 'evidence': self.fixture.receipt()['evidence']}
        provider = fixtures.Provider([self.fixture.extracted([self.fixture.receipt(), contact]), self.fixture.bound()])
        report = evaluate_case(self.case, SharedAnalyzer(provider))
        self.assertEqual(report['matched_findings'], 2)
        self.assertEqual(report['unexpected_findings'], 1)
        self.assertEqual(report['wrong_targets'], 0)
        self.assertFalse(report['passed'])

    def test_repeated_observations_are_not_independent_messages(self):
        report = evaluate_case(self.case, SharedAnalyzer(fixtures.Provider([self.fixture.extracted(), self.fixture.bound()])))
        summary = summarize([report, report, report])
        self.assertEqual((summary['observations'], summary['independent_messages']), (3, 1))

    def test_case_hashes_and_expected_targets_checked_before_inference(self):
        for altered in ('source', 'target'):
            case = deepcopy(self.case)
            if altered == 'source':
                case['context']['sources'][0]['text'] = 'modified source'
            else:
                case['expected'][0]['target_id'] = 'outside-context'
            provider = fixtures.Provider([])
            with self.assertRaises((DomainError, ValueError)):
                evaluate_case(case, SharedAnalyzer(provider))
            self.assertFalse(provider.calls)

    def test_precise_field_error_reaches_repair_without_leaking_value(self):
        bad = self.fixture.receipt()
        bad['value']['status'] = 'private-invalid-value'
        provider = fixtures.Provider([self.fixture.extracted([bad]),
            {'repairs': [{'finding_id': 'facts:0', 'finding': self.fixture.receipt()}]}, self.fixture.bound()])
        report = evaluate_case(self.case, SharedAnalyzer(provider))
        error = provider.calls[1][0]['failed_findings'][0]['error']
        self.assertEqual(error['path'], ['value', 'status'])
        self.assertEqual(error['reason'], 'invalid_choice')
        self.assertNotIn('private-invalid-value', json.dumps(error))
        self.assertTrue(report['passed'])

    def test_diagnostics_preserve_null_alternative_failure_and_hide_unknown_keys(self):
        schema = {'anyOf': [{'type': 'number', 'minimum': 0}, {'type': 'null'}]}
        with self.assertRaises(OutputError) as caught:
            check(-1, schema, ('confidence',))
        self.assertEqual((caught.exception.reason, caught.exception.path), ('invalid_number', ('confidence',)))
        bad = self.fixture.receipt()
        bad['private-message-body-as-a-key'] = 'private text'
        provider = fixtures.Provider([self.fixture.extracted([bad]), {'repairs': []}])
        with self.assertRaises(DomainError) as caught:
            SharedAnalyzer(provider).analyze(self.context)
        diagnostic = caught.exception.validation_diagnostic
        self.assertEqual(diagnostic['issues'][0]['reason'], 'unknown_field')
        self.assertNotIn('private', json.dumps(diagnostic))
        validate_diagnostic(diagnostic)
        injected = deepcopy(diagnostic)
        injected['issues'][0]['path'] = ['private-message-body-as-a-key']
        with self.assertRaises(DomainError):
            validate_diagnostic(injected)

    def test_unknown_binding_is_not_silently_accepted(self):
        for invalid in ('invented', ['malformed'], None):
            binding = self.fixture.bound()
            binding['bindings'] = [{'finding_id': invalid, 'target_id': 'app-1'}]
            provider = fixtures.Provider([self.fixture.extracted(), binding, self.fixture.bound()])
            report = evaluate_case(self.case, SharedAnalyzer(provider))
            self.assertTrue(report['passed'])
            self.assertEqual(provider.calls[3][0]['validation_errors'][0]['reason'], 'invalid_contract')

    def test_partial_valid_output_cannot_pass_reliability_gate(self):
        bad = self.fixture.receipt()
        bad['value']['status'] = 'bad'
        report = evaluate_case(self.case, SharedAnalyzer(fixtures.Provider([
            self.fixture.extracted([self.fixture.receipt(), bad]), {'repairs': []}, self.fixture.bound()])))
        self.assertEqual(report['matched_findings'], 2)
        self.assertFalse(report['passed'])
        validate_diagnostic(report['diagnostics'])

    def test_maximum_matching_respects_specific_and_general_expectations(self):
        from job_search.applications.understanding.evaluation import _match
        expected = [{'kind': 'submission'}, {'kind': 'submission', 'target_id': 'app-1'}]
        predicted = [{'kind': 'submission', 'target_id': 'app-1'}, {'kind': 'submission', 'target_id': 'app-2'}]
        self.assertEqual(_match(expected, predicted), ({0, 1}, {0, 1}))

    def test_repair_stage_resumes_after_deferral_without_reextracting(self):
        from job_search.inference.usage import UsageDeferred
        bad = self.fixture.receipt()
        bad['value']['status'] = 'bad'
        provider = fixtures.Provider([self.fixture.extracted([bad]),
            UsageDeferred('inference_daily_token_limit', '2099-01-01T00:00:00Z'),
            {'repairs': [{'finding_id': 'facts:0', 'finding': self.fixture.receipt()}]}, self.fixture.bound()])
        saved = {}
        def checkpoint(key, result=None):
            if result is None:
                return deepcopy(saved.get(key))
            saved[key] = deepcopy(result)
        analyzer = SharedAnalyzer(provider)
        with self.assertRaises(UsageDeferred):
            analyzer.analyze_with_checkpoints(self.context, checkpoint)
        result = analyzer.analyze_with_checkpoints(self.context, checkpoint)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual([call[1]['schema_name'] for call in provider.calls], [
            'mail_extraction_v3', 'mail_finding_repair_v3', 'mail_finding_repair_v3', 'mail_semantic_verification_v2', 'mail_identity_v4'])


if __name__ == '__main__':
    unittest.main()
