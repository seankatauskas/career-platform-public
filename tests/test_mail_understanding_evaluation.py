"""Shared semantic gates count independent messages and reject malformed scores."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.mail.understanding_evaluation import evaluate_cases, load_report, validate_report


def case(identity='one'):
    return {'case_id': identity, 'expected': [{'kind': 'action', 'application_id': 'app', 'label': 'reply'}],
            'predicted': [{'kind': 'action', 'application_id': 'app', 'label': 'reply', 'confidence': .99}]}


class EvaluationSafetyTests(unittest.TestCase):
    def test_cli_reports_file_and_validation_errors_without_a_traceback(self):
        from job_search.cli import main
        from job_search.runtime import RuntimeConfigV1
        with TemporaryDirectory() as d, patch('job_search.cli.load_runtime_config', return_value=RuntimeConfigV1.defaults(Path(d))):
            source, output = Path(d) / 'cases.json', Path(d) / 'report.json'
            argv = ['mail-understanding', 'evaluate', '--input', str(source), '--output', str(output), '--producer-version', 'model']
            with self.assertRaises(SystemExit) as error:
                main(argv)
            self.assertIn('No such file', str(error.exception))
            self.assertFalse(output.exists())
            source.write_text('not json')
            with self.assertRaises(SystemExit) as error:
                main(argv)
            self.assertIn('Expecting value', str(error.exception))
            source.write_text('[]')
            with self.assertRaises(SystemExit) as error:
                main(argv)
            self.assertIn('1..10000 cases', str(error.exception))
            source.write_text(json.dumps([case()]))
            output.write_text('existing report')
            with self.assertRaises(SystemExit) as error:
                main(argv)
            self.assertIn('File exists', str(error.exception))
            self.assertEqual(output.read_text(), 'existing report')

    def test_many_findings_in_few_messages_cannot_pass_the_fifty_case_gate(self):
        rows = [case(str(i)) for i in range(7)]
        for row in rows:
            row['expected'] *= 8
            row['predicted'] *= 8
        result = evaluate_cases(rows, producer_version='model', dataset_kind='reviewed_private_holdout')
        metric = result['report']['classes']['action:reply']
        self.assertEqual(metric['high_confidence_predictions'], 56)
        self.assertEqual(metric['correct_predictions'], 56)
        self.assertEqual(metric['high_confidence_cases'], 7)
        report = validate_report(result['report'])
        self.assertFalse(report.allows('action', dict(application_id='app', kind='reply', confidence=.99, actor='applicant', obligation='required'), {'producer_version': 'model'}))

    def test_invalid_confidence_and_noncontract_case_shapes_fail_closed(self):
        for value in [float('nan'), float('inf'), -1, 1.01, True, '0.99', None]:
            row = case(); row['predicted'][0]['confidence'] = value
            with self.subTest(value=value), self.assertRaises(ContractError):
                evaluate_cases([row], producer_version='model')
        invalid = [case() for _ in range(6)]
        invalid[0]['unexpected'] = True
        invalid[1]['predicted'][0]['application_id'] = 1
        invalid[2]['predicted'][0]['label'] = 'send_money'
        invalid[3]['expected'] *= 9
        invalid[4]['case_id'] = []
        invalid[5]['invalid_outputs'] = 2
        for row in invalid:
            with self.assertRaises(ContractError):
                evaluate_cases([row], producer_version='model')
        with self.assertRaises(ContractError):
            evaluate_cases([case(), case()], producer_version='model')

    def test_wrong_app_false_positive_and_invalid_output_remain_in_metrics(self):
        wrong, invalid, duplicate, unknown = [case(str(i)) for i in range(4)]
        wrong['predicted'][0]['application_id'] = 'wrong-app'
        invalid['invalid_outputs'] = 1
        duplicate['predicted'] *= 2
        unknown['expected'] = []
        result = evaluate_cases([wrong, invalid, duplicate, unknown], producer_version='model')
        metric = result['report']['classes']['action:reply']
        self.assertEqual(metric['correct_predictions'], 1)
        self.assertEqual(metric['wrong_application_matches'], 2)
        self.assertEqual(metric['missed_findings'], 2)
        self.assertEqual(metric['high_confidence_predictions'], 4)
        self.assertEqual(result['metrics']['invalid_outputs'], 1)

    def test_private_report_requires_consistent_metrics_and_reviewed_dataset(self):
        data = evaluate_cases([case(str(i)) for i in range(50)], producer_version='model', dataset_kind='reviewed_private_holdout')['report']
        self.assertEqual(validate_report(data).data['classes']['action:reply']['high_confidence_cases'], 50)
        for name, value in [('high_confidence_cases', 51), ('wrong_application_matches', 1), ('expected_findings', 49), ('missed_findings', 1)]:
            invalid = deepcopy(data); invalid['classes']['action:reply'][name] = value
            with self.assertRaises(ContractError):
                validate_report(invalid)
        with TemporaryDirectory() as d:
            path = Path(d) / 'report.json'; path.write_text(json.dumps({**data, 'dataset_kind': 'synthetic'})); path.chmod(0o600)
            self.assertIsNone(load_report(path))
            path.write_text(json.dumps(data)); self.assertIsNotNone(load_report(path))
            path.chmod(0o644); self.assertIsNone(load_report(path))


if __name__ == '__main__':
    unittest.main()
