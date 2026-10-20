"""Offline privacy, temporal isolation, abstention and replay integrity checks."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from job_search.inference.contracts import GenerationResult
from tests.probe_review_history import payload, replay, score, validate, VERSION


def dataset():
    text = 'Subject: Thanks for applying\nWe will not move forward.'
    return {'cases': [{'source_id': 'email-1', 'revision': '1',
        'first_review_at': '2026-10-19T12:00:00Z',
        'analysis_recorded_at': '2026-10-19T11:00:00Z',
        'context': {'sources': [{'source_id': 'email-1', 'revision': '1', 'text': text,
            'sha256': hashlib.sha256(text.encode()).hexdigest()}],
            'candidates': [{'id': 'app-1', 'job': {'title': 'Engineer', 'employer': 'Example'}}],
            'coverage': {'complete': True}, 'context': {}},
        'decisions': [{'proposal': {'id': 'p1', 'operation': 'close_application',
            'application_id': 'app-1', 'input': {'outcome': 'rejected'},
            'recorded_at': '2026-10-19T11:01:00Z', 'status': 'pending',
            'reason': 'must-never-reach-model'}, 'decision': 'accept',
            'reason': 'human-private-reason', 'reviewed_at': '2026-10-19T12:00:00Z'}]}]}


class Fake:
    model_revision = 'fake-fixed-model'
    generation_identity = 'fake-generation-1'
    max_input_tokens = 100000

    def __init__(self, output=None, fail=False):
        self.messages = []
        self.output = output
        self.fail = fail

    def count_tokens_upper_bound(self, text):
        return len(text)//3

    def generate(self, messages, **kwargs):
        self.messages.append(messages)
        if self.fail:
            raise RuntimeError('PRIVATE_BODY_OR_CREDENTIAL')
        value = self.output if self.output is not None else {'decisions': [
            {'proposal_id': 'p1', 'decision': 'accept', 'reason_code': 'supported_change'}]}
        return GenerationResult(json.dumps(value), {}, {})


class HistoryReplayTests(unittest.TestCase):
    def test_human_labels_reasons_and_status_never_enter_payload(self):
        case = dataset()['cases'][0]
        first = payload(case)
        altered = copy.deepcopy(case)
        altered['decisions'][0].update(decision='reject', reason='SECRET_LABEL', reviewed_at='2027-01-01T00:00:00Z')
        altered['decisions'][0]['proposal'].update(reason='SECRET_REASON', status='applied', version=99)
        altered['decisions'][0]['proposal']['input'].update(expected_version=99, expected_records={'private': 'GUARD_METADATA'})
        self.assertEqual(first, payload(altered))
        provider = Fake()
        report = replay(dataset(), provider)
        prompt = json.dumps(provider.messages)
        self.assertNotIn('human-private-reason', prompt)
        self.assertNotIn('must-never-reach-model', prompt)
        self.assertNotIn('first_review_at', prompt)
        self.assertNotIn('reviewed_at', prompt)
        self.assertNotIn('We will not move forward', json.dumps(report))

    def test_future_analysis_and_proposal_fail_before_paid_calls(self):
        for target, field in [('case', 'analysis_recorded_at'), ('proposal', 'recorded_at')]:
            data = dataset()
            value = data['cases'][0] if target == 'case' else data['cases'][0]['decisions'][0]['proposal']
            value[field] = '2026-10-19T12:00:00Z'
            provider = Fake()
            with self.assertRaises(ValueError):
                replay(data, provider)
            self.assertEqual(provider.messages, [])

    def test_source_hash_verified(self):
        data = dataset()
        data['cases'][0]['context']['sources'][0]['text'] += 'tampered'
        with self.assertRaises(ValueError):
            replay(data, Fake())

    def test_strict_output_ids_and_fields(self):
        good = {'proposal_id': 'p1', 'decision': 'accept', 'reason_code': 'supported_change'}
        for rows in [[], [good, good], [{**good, 'proposal_id': 'unknown'}],
                     [{**good, 'decision': 'maybe'}], [{**good, 'reason_code': 'raw prose'}],
                     [{**good, 'explanation': 'private text'}]]:
            with self.assertRaises(ValueError):
                validate({'decisions': rows}, ['p1'])
        with self.assertRaises(ValueError):
            validate({'decisions': [good, good]}, ['p1', 'p2'])

    def test_errors_are_redacted_and_never_default_to_reject(self):
        for fake, error in [(Fake(fail=True), 'provider_error'), (Fake(output={'decisions': []}), 'invalid_output')]:
            report = replay(dataset(), fake)
            self.assertEqual(report['results'][0]['error_code'], error)
            self.assertNotIn('PRIVATE_BODY_OR_CREDENTIAL', json.dumps(report))
            self.assertEqual(report['metrics']['false_rejects'], 0)
            self.assertEqual(report['metrics']['confusion']['accept']['error'], 1)
            self.assertEqual(report['metrics']['decision_coverage'], 0)

    def test_unknown_labels_abstentions_and_operation_counts(self):
        data = dataset()
        data['cases'][0]['decisions'].extend([
            {'proposal': {'id': 'p2', 'operation': 'record_progress'}, 'decision': 'reject'},
            {'proposal': {'id': 'p3', 'operation': 'record_progress'}, 'decision': 'unknown'},
        ])
        output = {'decisions': [
            {'proposal_id': 'p1', 'decision': 'needs_review', 'reason_code': 'incomplete_context'},
            {'proposal_id': 'p2', 'decision': 'accept', 'reason_code': 'supported_change'},
            {'proposal_id': 'p3', 'decision': 'reject', 'reason_code': 'unsupported_claim'},
        ]}
        metrics = replay(data, Fake(output))['metrics']
        self.assertEqual(metrics['unique_emails'], 1)
        self.assertEqual(metrics['unknown_labels'], 1)
        self.assertEqual(metrics['false_accepts'], 1)
        self.assertEqual(metrics['false_rejects'], 0)
        self.assertEqual(metrics['decision_coverage'], .5)
        self.assertEqual(metrics['accept_recall'], 0)
        self.assertEqual(metrics['acceptance_precision'], 0)
        self.assertEqual(metrics['agreement_overall'], 0)
        self.assertEqual(metrics['per_operation']['record_progress']['unknown_labels'], 1)

    def test_resume_binds_model_dataset_and_advisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'checkpoint.json'
            fake = Fake()
            first = replay(dataset(), fake, checkpoint_path=path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            second = replay(dataset(), fake, checkpoint_path=path)
            self.assertEqual(first, second)
            self.assertEqual(len(fake.messages), 1)
            fake.generation_identity = 'different'
            with self.assertRaises(ValueError):
                replay(dataset(), fake, checkpoint_path=path)
            fake.generation_identity = 'fake-generation-1'
            changed = dataset()
            changed['cases'][0]['decisions'][0]['decision'] = 'reject'
            with self.assertRaises(ValueError):
                replay(changed, fake, checkpoint_path=path)

    def test_limit_leaves_unrun_labels_out_of_false_rejects(self):
        fake = Fake()
        report = replay(dataset(), fake, limit=0)
        self.assertEqual(fake.messages, [])
        self.assertEqual(report['metrics']['confusion']['accept']['not_run'], 1)
        self.assertEqual(report['metrics']['false_rejects'], 0)

    def test_inconsistent_advisory_verdict_is_deferred_without_using_labels(self):
        from tests.probe_review_history import guarded_report
        data = dataset()
        report = replay(data, Fake({'decisions': [
            {'proposal_id': 'p1', 'decision': 'accept', 'reason_code': 'unsupported_claim'}]}))
        guarded = guarded_report(data, report)
        self.assertEqual(guarded['results'][0]['decisions'][0]['decision'], 'needs_review')
        self.assertEqual(report['results'][0]['decisions'][0]['decision'], 'accept')
        self.assertEqual(guarded['deferred_inconsistent_recommendations'], 1)
        changed = json.loads(json.dumps(data))
        changed['cases'][0]['decisions'][0]['decision'] = 'reject'
        self.assertEqual(guarded_report(changed, report)['results'], guarded['results'])
        self.assertTrue(guarded['calibration_only'])

    def test_coherent_advisory_verdict_is_unchanged(self):
        from tests.probe_review_history import guarded_report
        data = dataset()
        report = replay(data, Fake())
        guarded = guarded_report(data, report)
        self.assertEqual(guarded['results'], report['results'])
        self.assertEqual(guarded['deferred_inconsistent_recommendations'], 0)


if __name__ == '__main__':
    unittest.main()
