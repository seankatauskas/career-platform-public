"""Staged mail analysis: exact evidence, bounded repairs and human-only updates."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from job_search.applications.understanding import AnalysisInput, SharedAnalyzer
from job_search.applications.understanding.analyzer import AnalysisResult
from job_search.applications.understanding.evidence import source_blocks, resolve_evidence
from job_search.applications.understanding.contracts import validate_analysis
from job_search.commands import DomainError
from tests.test_redesign_evidence import source, span
from tests import test_redesign_evidence as evidence_tests


TEXT = 'SUBJECT\nThank you for applying to Example!\nBODY\nThank you for applying to Example!\nYour application is in the queue.\nPlease complete the assessment.\n'


class Provider:
    max_input_tokens = 200000
    count_tokens_upper_bound = staticmethod(len)

    def __init__(self, responses, verification=None, inventory=None):
        self.responses = list(responses)
        self.calls = []
        self.verification = verification
        self.inventory = inventory

    def generate(self, messages, **kwargs):
        self.calls.append((json.loads(messages[1]['content']), kwargs))
        if kwargs['schema_name'] == 'mail_source_support_v1':
            payload = self.calls[-1][0]
            first = next(b for s in payload['sources'] for b in s['blocks'] if b['text'])
            response = self.inventory or {'support': [{'topic': topic,
                'classification': 'substantive_personal' if topic == 'personal_communication' else 'actionable_commitment',
                'evidence': [{'block_id': first['block_id']}]} for topic in payload['topics']]}
            if callable(response):
                response = response(payload, kwargs)
            return SimpleNamespace(text=json.dumps(response), usage={})
        if kwargs['schema_name'].startswith('mail_semantic_verification'):
            payload = self.calls[-1][0]
            response = self.verification or {'checks': [{'finding_id': f['finding_id'], 'verdict': 'supported'} for f in payload['findings']], 'coverage': 'complete'}
            if callable(response):
                response = response(payload, kwargs)
            return SimpleNamespace(text=json.dumps(response), usage={})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=json.dumps(response), usage={})


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.context = AnalysisInput((source(TEXT),), ({'id': 'app-1', 'job': {'employer': 'Example', 'title': 'Engineer'}},))
        self.blocks = source_blocks(self.context)[0]['blocks']

    def citation(self, text, occurrence=0):
        blocks = [b for b in self.blocks if text in b['text']]
        return {'block_id': blocks[occurrence]['block_id']}

    def receipt(self):
        return {'kind': 'submission', 'value': {'status': 'confirmed', 'submission_id': None, 'occurred_at': None},
                'target_id': None, 'confidence': 0.99, 'evidence': [self.citation('Your application is in the queue.')]}

    def extracted(self, facts=None):
        return {'relevance': 'career_related', 'associations': [], 'facts': facts if facts is not None else [self.receipt()],
                'requests': [], 'temporal_facts': [], 'uncertainties': []}

    def bound(self, target='app-1'):
        return {'target_id': target, 'resolution': 'matched' if target else 'ambiguous', 'records': [],
                'evidence': [self.citation('Thank you for applying to Example!', 1)], 'uncertainties': []}

    def test_repeated_subject_body_text_resolves_selected_body_without_offsets(self):
        first = resolve_evidence([self.citation('Thank you for applying to Example!', 0)], self.context)[0]
        second = resolve_evidence([self.citation('Thank you for applying to Example!', 1)], self.context)[0]
        self.assertNotEqual(first['start'], second['start'])
        self.assertGreater(second['start'], TEXT.index('BODY'))
        self.assertEqual(second['quote'], TEXT[second['start']:second['end']])

    def test_unicode_and_whitespace_roundtrip_and_stable_revision_scoped_ids(self):
        text = '🙂 e\u0301\r\n  Please  reply.\n' + 'long ' * 300
        context = AnalysisInput((source(text),))
        blocks = source_blocks(context)[0]['blocks']
        self.assertEqual(''.join(b['text'] for b in blocks), text)
        second = AnalysisInput((source('Other', identity='other'), source(text)))
        self.assertEqual(source_blocks(second)[1]['blocks'], blocks)
        stale = replace(context, sources=(replace(context.sources[0], revision='rev2'),))
        self.assertNotEqual(source_blocks(stale)[0]['blocks'][0]['block_id'], blocks[0]['block_id'])
        with self.assertRaises(DomainError):
            resolve_evidence([{'block_id': blocks[1]['block_id'], 'quote': '  Please  reply.'}], stale)
        actual = resolve_evidence([{'block_id': blocks[1]['block_id'], 'quote': '  Please  reply.'}], context)[0]
        self.assertEqual(actual, span('  Please  reply.', context.sources[0]))

    def test_no_normalization_guess_or_cross_block_quote(self):
        for quote in ('Thank you for applying to EXAMPLE!', 'queue.\nPlease', 'Invented receipt'):
            with self.subTest(quote=quote), self.assertRaises(DomainError):
                resolve_evidence([dict(self.citation('Your application is in the queue.'), quote=quote)], self.context)
        repeated = AnalysisInput((source('Again. Again.'),))
        block = source_blocks(repeated)[0]['blocks'][0]
        with self.assertRaises(DomainError):
            resolve_evidence([{'block_id': block['block_id'], 'quote': 'Again.'}], repeated)

    def test_extraction_cannot_see_or_select_candidates_identity_is_separate(self):
        provider = Provider([self.extracted(), self.bound()])
        result = SharedAnalyzer(provider).analyze(self.context)
        first, verification, second = provider.calls
        self.assertNotIn('candidates', first[0])
        self.assertNotIn('records', first[0])
        self.assertEqual(second[0]['candidates'][0]['id'], 'app-1')
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(result['associations'][0]['target_id'], 'app-1')
        self.assertEqual(result.validation_diagnostic, {'version': 2, 'issues': [], 'repairs': 0})
        validate_analysis(result, self.context)

    def test_one_bad_finding_repaired_without_changing_verified_sibling(self):
        broken = deepcopy(self.receipt()); broken['value']['status'] = 'invented-status'
        repaired = deepcopy(self.receipt())
        provider = Provider([self.extracted([self.receipt(), broken]),
            {'repairs': [{'finding_id': 'facts:1', 'finding': repaired}]},
            self.bound()])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(len(result['facts']), 2)
        self.assertEqual(len(provider.calls[1][0]['failed_findings']), 1)
        self.assertEqual(provider.calls[1][0]['failed_findings'][0]['finding_id'], 'facts:1')
        self.assertEqual(result.validation_diagnostic['issues'], [])
        self.assertEqual(result.validation_diagnostic['repairs'], 1)

    def test_unrepairable_finding_preserves_valid_receipt_and_reports_partial(self):
        broken = deepcopy(self.receipt()); broken['value']['status'] = 'invented-status'
        provider = Provider([self.extracted([self.receipt(), broken]), {'repairs': []}, self.bound()])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(len(result['facts']), 1)
        self.assertEqual(result.validation_diagnostic['issues'][0]['category'], 'facts')
        self.assertEqual(result.validation_diagnostic['issues'][0]['position'], 1)
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'invalid_choice')
        self.assertNotIn('invented-status', json.dumps(result.validation_diagnostic))

    def test_identity_failure_preserves_unassigned_receipt_and_repairs_only_identity(self):
        provider = Provider([self.extracted(), {'target_id': 'app-1'}, {'target_id': 'app-1'}])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(len(result['facts']), 1)
        self.assertIsNone(result['facts'][0]['target_id'])
        self.assertEqual([c[1]['schema_name'] for c in provider.calls], ['mail_extraction_v3', 'mail_semantic_verification_v2', 'mail_identity_v4', 'mail_identity_v4'])
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'invalid_contract')

    def test_ambiguous_match_preserves_receipt_without_inventing_identity(self):
        provider = Provider([self.extracted(), self.bound(None)])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertIsNone(result['facts'][0]['target_id'])
        self.assertFalse(result.validation_diagnostic['issues'])

    def test_incomplete_retrieval_never_selects_by_elimination(self):
        context = replace(self.context, context={'candidate_retrieval': {'complete': False}})
        provider = Provider([self.extracted()])
        result = SharedAnalyzer(provider).analyze(context)
        self.assertIsNone(result['facts'][0]['target_id'])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'incomplete_retrieval')

    def test_failed_binding_cannot_rewrite_verified_fact(self):
        bad = self.bound(); bad['finding'] = {'kind': 'outcome', 'value': 'rejected'}
        provider = Provider([self.extracted(), bad, self.bound()])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(result['facts'][0]['value'], {'status': 'confirmed'})
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(result.validation_diagnostic['repairs'], 1)

    def test_cannot_bind_another_applications_record_or_submission_attempt(self):
        for record in ('submission-1', 'interview-1'):
            context = replace(self.context, context={'records': {'app-1': {'submissions': [{'id': 'submission-1'}]},
                'app-2': {'interviews': [{'id': 'interview-1'}]}}})
            bad = self.bound(); bad['records'] = [{'finding_id': 'facts:0', 'record_id': record}]
            provider = Provider([self.extracted(), bad, {'records': []}])
            result = SharedAnalyzer(provider).analyze(context)
            self.assertEqual(result['facts'][0]['target_id'], 'app-1')
            self.assertNotIn('submission_id', result['facts'][0]['value'])
            self.assertFalse(result.validation_diagnostic['issues'])


class ResumeTests(unittest.TestCase):
    def test_runtime_resumes_after_identity_allowance_deferral_without_reextracting(self):
        import tempfile
        from pathlib import Path
        from job_search.application_runtime import ApplicationRuntime
        from job_search.application_transport import HumanApplicationAdapter
        from job_search.applications.understanding import SourceText
        from job_search.commands import CommandContext, Principal
        from job_search.inference.usage import UsageDeferred
        with tempfile.TemporaryDirectory() as temporary:
            runtime = ApplicationRuntime(Path(temporary) / 'owners.db')
            human = HumanApplicationAdapter(runtime, 'fixture')
            app = human.command('save_job', {'job_source': {'source': 'fixture', 'source_id': 'role',
                'employer': 'Example', 'title': 'Engineer'}}, 'app')
            message = runtime.command(CommandContext(Principal('mail', 'worker', {'record_message'}), 'message', 'inferred'),
                'record_message', {'account_id': 'test', 'provider_message_id': 'one', 'source_version': 1,
                'direction': 'incoming', 'authored_text': TEXT, 'archive_ref': 'archive'})
            builder = BlockTests(); builder.setUp()
            builder.context = AnalysisInput((SourceText(message['id'], message['revision'], message['source_sha256'], TEXT),))
            builder.blocks = source_blocks(builder.context)[0]['blocks']
            provider = Provider([builder.extracted(), UsageDeferred('inference_daily_token_limit', '2099-01-01T00:00:00Z'), builder.bound(app['id'])])
            class Archive:
                def read_message(self, ref): return TEXT
            def run():
                return runtime.analyze_message(message['id'], archive=Archive(), allowed_accounts=('test',),
                    candidate_ids=[app['id']], analyzer=SharedAnalyzer(provider), model_version='fixture')
            with self.assertRaises(UsageDeferred):
                run()
            with runtime.executor.read() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM understand_stage_checkpoints').fetchone()[0], 2)
                self.assertEqual(con.execute('SELECT count(*) FROM understand_analyses').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT status FROM understand_claims').fetchone()[0], 'deferred')
            result = run()
            self.assertEqual([c[1]['schema_name'] for c in provider.calls], ['mail_extraction_v3', 'mail_semantic_verification_v2', 'mail_identity_v4', 'mail_identity_v4'])
            self.assertEqual({p['operation'] for p in result['proposals']}, {'link_message', 'record_submission_receipt'})
            self.assertTrue(all(p['status'] == 'pending' for p in result['proposals']))
            with runtime.executor.read() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM understand_stage_checkpoints').fetchone()[0], 0)
                self.assertIsNone(runtime.correspondence.association(con, message['id']))
                self.assertEqual(runtime.applications.list_records(con, app['id'], 'receipts')['items'], [])

    def test_checkpoint_content_excludes_arbitrary_response_fields(self):
        from job_search.applications.understanding.checkpoints import bounded_result
        from job_search.applications.understanding.model_contract import extraction_schema
        fixture = BlockTests(); fixture.setUp()
        response = fixture.extracted()
        response['full_email_dump'] = 'private source body should not be cached'
        response['facts'][0]['value']['status'] = 'another private value'
        bounded = bounded_result(response, extraction_schema(fixture.context))
        self.assertNotIn('private', json.dumps(bounded))
        from job_search.applications.understanding.validation import check
        with self.assertRaises(DomainError):
            check(bounded, extraction_schema(fixture.context))


class ApprovalTests(unittest.TestCase):
    setUp = evidence_tests.EvidenceTest.setUp
    run_op = evidence_tests.EvidenceTest.run_op
    project = evidence_tests.EvidenceTest.project
    def test_semantic_corrections_persist_without_reopening_processing_attention(self):
        from tests.test_redesign_evidence import output
        context = AnalysisInput((source(),), ({'id': 'app-1'},))
        diagnostic = {'version': 3, 'repairs': 0, 'issues': [], 'corrections': [
            {'stage': 'verification', 'category': 'facts', 'position': 1, 'reason': 'unsupported_finding_removed'}]}
        analysis = self.run_op('record_analysis', lambda tx: self.under.store_analysis(tx,
            context=context, model_version='semantic', output=output(), validation_diagnostic=diagnostic))
        proposals = self.project(analysis)
        self.assertTrue(all(p['status'] == 'pending' for p in proposals))
        self.run_op('project_analysis', lambda tx: self.under.record_projection(tx, analysis_id=analysis['id'], status='succeeded'))
        with self.executor.read() as con:
            issue = self.under.processing_for_source(con, 'message', 'revision-1')
            self.assertEqual(issue['status'], 'succeeded')
            self.assertEqual(issue['validation_diagnostic'], diagnostic)

    # Reuse the real persistence/projection fixture, not a mock of approval.
    def test_checkpoints_are_lease_fenced_and_cleared_on_terminal_result(self):
        from job_search.applications.understanding import checkpoints
        from tests.test_redesign_evidence import output
        context = AnalysisInput((source(),), ({'id': 'app-1'},))
        claim = self.run_op('analyze_observation', lambda tx: self.under.claim_analysis(tx, context=context, model_version='m'))
        key = 'a' * 64
        self.run_op('record_analysis', lambda tx: checkpoints.write(tx, claim, key, {'value': {'relevance': 'unrelated'}}))
        with self.executor.read() as connection:
            self.assertIsNotNone(checkpoints.read(connection, claim, key, self.now))
            with self.assertRaises(DomainError):
                checkpoints.read(connection, dict(claim, token='stale'), key, self.now)
        self.run_op('record_analysis', lambda tx: self.under.store_analysis(tx, context=context, model_version='m',
            output=output(), claim=claim))
        with self.executor.read() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM understand_stage_checkpoints').fetchone()[0], 0)
        with self.assertRaises(DomainError):
            self.run_op('record_analysis', lambda tx: checkpoints.write(tx, claim, key, {'value': {}}))

    def test_checkpoint_migration_is_explicit_and_startup_does_not_write(self):
        from job_search.commands import CommandExecutor
        from job_search.applications.understanding.api import _CHECKPOINT_PREVIOUS, SCHEMA, SCHEMA_MIGRATIONS
        from job_search.schema_lifecycle import explicit_migration
        from unittest.mock import patch
        from pathlib import Path
        import os
        path = Path(self.tmp.name) / 'previous.db'
        old = CommandExecutor(path, {'understanding': _CHECKPOINT_PREVIOUS})
        with old.read() as connection:
            before = [tuple(r) for r in connection.execute('SELECT * FROM command_schema_versions')]
        with patch.dict(os.environ, {'JOB_SEARCH_SCHEMA_MODE': 'require_current'}):
            with self.assertRaises(RuntimeError):
                CommandExecutor(path, {'understanding': SCHEMA}, schema_migrations=SCHEMA_MIGRATIONS)
            with old.read() as connection:
                self.assertEqual(before, [tuple(r) for r in connection.execute('SELECT * FROM command_schema_versions')])
            with explicit_migration():
                upgraded = CommandExecutor(path, {'understanding': SCHEMA}, schema_migrations=SCHEMA_MIGRATIONS)
            with upgraded.read() as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM understand_stage_checkpoints').fetchone()[0], 0)
            CommandExecutor(path, {'understanding': SCHEMA}, schema_migrations=SCHEMA_MIGRATIONS)

    def test_partial_analysis_is_persisted_and_projects_pending_only(self):
        from tests.test_redesign_evidence import source as domain_source, output
        context = AnalysisInput((domain_source(),), ({'id': 'app-1'},))
        diagnostics = {'version': 2, 'repairs': 1, 'issues': [{'stage': 'extraction', 'category': 'requests', 'position': 1, 'reason': 'quote_not_found'}]}
        analysis = self.run_op('record_analysis', lambda tx: self.under.store_analysis(tx,
            context=context, model_version='staged', output=output(), validation_diagnostic=diagnostics))
        proposals = self.project(analysis)
        self.assertTrue(proposals)
        self.assertTrue(all(p['status'] == 'pending' for p in proposals))
        self.run_op('project_analysis', lambda tx: self.under.record_projection(tx, analysis_id=analysis['id'], status='succeeded'))
        with self.executor.read() as con:
            issue = self.under.processing_for_source(con, 'message', 'revision-1')
            self.assertEqual(issue['status'], 'open')
            self.assertEqual(issue['failure_code'], 'partial_analysis')
            self.assertEqual(issue['validation_diagnostic'], diagnostics)
        with self.assertRaises(DomainError):
            self.run_op('review_changes', lambda tx: self.under.resolve(tx,
                proposal_id=proposals[0]['id'], expected_version=1, decision='accepted'))


if __name__ == '__main__':
    unittest.main()
