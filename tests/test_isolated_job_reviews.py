"""Offline contracts for score-blind review evidence export and bounded import."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError, payload_sha256
from job_search.job_reviews import isolated
from tests import test_agent_job_reviews as fixtures


class IsolatedReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service

    def packet(self, **kwargs):
        rid = kwargs.pop('review_id', None) or self.fixture.start()
        return isolated.export_packet(self.service, review_id=rid, actor=kwargs.pop('actor', 'isolated-primary'),
                                      ordinals=kwargs.pop('ordinals', [1]), **kwargs)

    def result(self, packet):
        return {'packet_sha256': packet['packet_sha256'],
                'assessments': [{'ordinal': job['ordinal'], 'assessment': self.fixture.assessment()}
                                for job in packet['jobs']]}

    def test_packet_strips_structured_diagnostics_and_preserves_all_evidence_pages(self):
        self.fixture.profile['facts'] = [
            {'fact_id': f'fact-{i}', 'source': 'experience', 'text': f'Approved Python API evidence {i}'}
            for i in range(1, 45)]
        self.fixture.profile['fingerprint'] = payload_sha256(self.fixture.profile['facts'])
        self.fixture.profile['model_rank'] = 'MODEL_DIAGNOSTIC_SENTINEL'
        note = 'I enjoy ranking systems; this role sounds too senior.'
        self.fixture.command('feedback', note=note)
        description = 'Python APIs ' + 'Long evidence. ' * 2000
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('UPDATE jobs SET description=?', (description,))
        original = self.service.call
        calls = []

        def contaminated(action, args):
            calls.append((action, copy.deepcopy(args)))
            value = original(action, args)
            value['model_explanation'] = 'MODEL_DIAGNOSTIC_SENTINEL'
            value['previous_lists'] = [{'title': 'MODEL_DIAGNOSTIC_SENTINEL'}]
            if action == 'job':
                value['job']['model_score'] = 'MODEL_DIAGNOSTIC_SENTINEL'
                value['assessment'] = {'priority': 'MODEL_DIAGNOSTIC_SENTINEL'}
            return value

        rid = self.fixture.start(preferences=['Prefer appropriate seniority'])
        with patch.object(self.service, 'call', side_effect=contaminated):
            packet = self.packet(review_id=rid)
        self.assertNotIn('MODEL_DIAGNOSTIC_SENTINEL', json.dumps(packet))
        self.assertEqual(packet['jobs'][0]['job']['description'], description)
        self.assertEqual(len(packet['context']['facts']), 44)
        self.assertEqual(packet['context']['preferences'], ['Prefer appropriate seniority'])
        self.assertEqual(packet['context']['feedback'][0]['note'], note)
        self.assertGreater(len([a for a, _ in calls if a == 'job']), 1)
        self.assertTrue(all(args['blind'] for action, args in calls if action == 'job'))
        self.assertEqual({action for action, _ in calls}, {'context', 'job'})

    def test_import_uses_only_coordinator_identity_and_records_evidence(self):
        packet = self.packet()
        result = isolated.import_assessments(self.service, packet, self.result(packet))
        self.assertEqual(result['assessments'][0]['revision'], 1)
        with sqlite3.connect(self.fixture.ledger.store.db_path) as con:
            row = con.execute('SELECT reviewer,assessment_json FROM job_review_items').fetchone()
        self.assertEqual(row[0], 'isolated-primary')
        self.assertEqual(json.loads(row[1])['decision'], 'close')

    def test_check_packet_is_blind_and_preserves_independent_actor_rule(self):
        rid = self.fixture.start()
        self.fixture.assess(rid, actor='primary')
        packet = self.packet(review_id=rid, actor='independent', kind='check')
        self.assertNotIn('assessment', packet['jobs'][0])
        self.assertNotIn('reviewer', json.dumps(packet))
        isolated.import_assessments(self.service, packet, self.result(packet))
        with sqlite3.connect(self.fixture.ledger.store.db_path) as con:
            self.assertEqual(con.execute('SELECT reviewer,checker FROM job_review_items').fetchone(),
                             ('primary', 'independent'))

    def test_result_cannot_supply_actor_review_or_idempotency(self):
        packet = self.packet()
        for field in ('actor', 'review_id', 'kind', 'idempotency_key'):
            with self.subTest(field=field):
                result = self.result(packet)
                result[field] = 'foreign'
                with self.assertRaises(ContractError):
                    isolated.import_assessments(self.service, packet, result)
                result = self.result(packet)
                result['assessments'][0][field] = 'foreign'
                with self.assertRaises(ContractError):
                    isolated.import_assessments(self.service, packet, result)
        self.assertEqual(self.fixture.command('status', review_id=packet['review_id'])['counts'], {'pending': 1})

    def test_duplicate_foreign_missing_and_wrong_packet_results_rejected_before_writes(self):
        self.fixture.insert('b')
        packet = self.packet(ordinals=[1, 2])
        cases = []
        duplicate = self.result(packet)
        duplicate['assessments'][1]['ordinal'] = 1
        cases.append(duplicate)
        foreign = self.result(packet)
        foreign['assessments'][1]['ordinal'] = 99
        cases.append(foreign)
        missing = self.result(packet)
        missing['assessments'].pop()
        cases.append(missing)
        wrong_packet = self.result(packet)
        wrong_packet['packet_sha256'] = '0' * 64
        cases.append(wrong_packet)
        for result in cases:
            with self.assertRaises(ContractError):
                isolated.import_assessments(self.service, packet, result)
        self.assertEqual(self.fixture.command('status', review_id=packet['review_id'])['counts'], {'pending': 2})

    def test_all_evidence_is_validated_before_first_write(self):
        self.fixture.insert('b')
        packet = self.packet(ordinals=[1, 2])
        result = self.result(packet)
        result['assessments'][1]['assessment']['evidence'][0]['quote'] = 'not in this job'
        with self.assertRaisesRegex(ContractError, 'quote'):
            isolated.import_assessments(self.service, packet, result)
        self.assertEqual(self.fixture.command('status', review_id=packet['review_id'])['counts'], {'pending': 2})

    def test_stale_second_revision_rejects_before_first_write(self):
        self.fixture.insert('b')
        packet = self.packet(ordinals=[1, 2])
        self.fixture.assess(packet['review_id'], ordinal=2)
        with self.assertRaisesRegex(ContractError, 'changed since packet'):
            isolated.import_assessments(self.service, packet, self.result(packet))
        with sqlite3.connect(self.fixture.ledger.store.db_path) as con:
            self.assertIsNone(con.execute('SELECT assessment_json FROM job_review_items WHERE ordinal=1').fetchone()[0])

    def test_refreshed_source_rejects_old_packet(self):
        packet = self.packet()
        with sqlite3.connect(self.fixture.db) as con:
            con.execute("UPDATE jobs SET description=description || ' New responsibility.'")
        self.fixture.command('refresh-job', review_id=packet['review_id'], ordinal=1,
                             actor='coordinator', expected_revision=0)
        with self.assertRaisesRegex(ContractError, 'changed since packet'):
            isolated.import_assessments(self.service, packet, self.result(packet))

    def test_packet_integrity_and_no_foreign_fields(self):
        packet = self.packet()
        damaged = copy.deepcopy(packet)
        damaged['actor'] = 'another'
        with self.assertRaisesRegex(ContractError, 'fingerprint'):
            isolated.validate_packet(damaged)
        damaged = copy.deepcopy(packet)
        damaged['jobs'][0]['job']['model_score'] = 0.99
        damaged['packet_sha256'] = payload_sha256({k: v for k, v in damaged.items() if k != 'packet_sha256'})
        with self.assertRaises(ContractError):
            isolated.validate_packet(damaged)

    def test_export_requires_bounded_explicit_unique_ordinals(self):
        rid = self.fixture.start()
        for ordinals in ([], list(range(1, 22)), [1, 1], [True], [0], '1'):
            with self.subTest(ordinals=ordinals), self.assertRaises(ContractError):
                self.packet(review_id=rid, ordinals=ordinals)

    def test_feedback_is_unchanged_and_import_does_not_train(self):
        note = 'Exciting work, too senior because of organization-wide ownership.'
        self.fixture.command('feedback', note=note)
        packet = self.packet()
        original = self.service.call
        calls = []

        def tracked(action, args):
            calls.append(action)
            return original(action, args)

        with patch.object(self.service, 'call', side_effect=tracked):
            isolated.import_assessments(self.service, packet, self.result(packet))
        self.assertEqual(set(calls), {'context', 'job', 'assess'})
        self.assertEqual(self.fixture.command('context', section='feedback')['feedback'][0]['note'], note)
        self.assertEqual(len(self.fixture.command('context', section='feedback')['feedback']), 1)

    def test_oversize_packet_fails_without_truncating(self):
        with patch.object(isolated, 'MAX_PACKET_BYTES', 100):
            with self.assertRaisesRegex(ContractError, 'size limit'):
                self.packet()


if __name__ == '__main__':
    unittest.main()
