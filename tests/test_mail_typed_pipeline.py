"""General typed mail-stage contracts; fixtures do not name production senders."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from job_search.applications.understanding import AnalysisInput, SharedAnalyzer
from job_search.applications.understanding.checkpoints import bounded_result
from job_search.applications.understanding.evidence import source_blocks, resolve_evidence
from job_search.applications.understanding.model_contract import canonical_identity_schema, canonical_record_schema, evidence_schema
from job_search.applications.understanding.validation import check, validate_diagnostic
from job_search.commands import DomainError
from tests import test_mail_evidence_blocks as fixtures
from tests.test_redesign_evidence import source

Provider = fixtures.Provider


class TypedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BlockTests()
        self.f.setUp()
        self.context = replace(self.f.context, candidates=(*self.f.context.candidates, {'id': 'app-2'}),
            context={'catalog_candidates': [{'id': 'catalog:one'}], 'records': {
                'app-1': {'submissions': [{'id': 'submission-1'}], 'interviews': [{'id': 'interview-1'}],
                          'assessments': [{'id': 'assessment-1'}], 'offers': [{'id': 'offer-1'}]},
                'app-2': {'interviews': [{'id': 'interview-2'}]}}})

    def test_receipt_binding_cannot_emit_any_existing_record_id_or_catalog(self):
        schema = canonical_identity_schema(self.context, {'facts:0': ('facts', self.f.receipt())})
        good = self.f.bound()
        check(good, schema)
        for record in ('submission-1', 'interview-1', 'assessment-1', 'offer-1'):
            bad = deepcopy(good)
            bad['records'] = [{'finding_id': 'facts:0', 'record_id': record}]
            with self.assertRaises(DomainError):
                check(bad, schema)
        self.assertEqual(schema['properties']['records']['maxItems'], 0)
        self.assertNotIn('bindings', schema['properties'])

    def test_record_ids_are_typed_by_fact_and_selected_application(self):
        fact = {'kind': 'interview', 'value': {'status': 'confirmed'}}
        schema = canonical_record_schema(self.context, {'facts:0': ('facts', fact)}, 'app-1', selected=True)
        good = [{'finding_id': 'facts:0', 'record_id': 'interview-1'}]
        check(good, schema)
        self.assertEqual(bounded_result(good, schema), good)
        for record in ('interview-2', 'assessment-1', 'invented'):
            bad = deepcopy(good)
            bad[0]['record_id'] = record
            with self.assertRaises(DomainError):
                check(bad, schema)

    def test_selected_blocks_need_no_retyping_even_with_repeated_text_unicode_or_long_lines(self):
        text = '🙂 again again\r\n' + ('x' * 520) + '\n'
        context = AnalysisInput((source(text),))
        blocks = source_blocks(context)[0]['blocks']
        self.assertEqual(''.join(b['text'] for b in blocks), text)
        for block in blocks:
            wire = [{'block_id': block['block_id']}]
            check(wire, evidence_schema(context))
            span = resolve_evidence(wire, context)[0]
            self.assertEqual(span['quote'], text[span['start']:span['end']])
            self.assertEqual(span['quote'], block['text'])
        bad = [{'block_id': blocks[0]['block_id'], 'quote': 'a transcription'}]
        with self.assertRaises(DomainError):
            check(bad, evidence_schema(context))

    def test_semantic_rejection_corrects_boilerplate_without_perpetual_attention(self):
        contact = {'kind': 'contact', 'value': 'recruiter_contact', 'target_id': None,
                   'confidence': None, 'evidence': self.f.receipt()['evidence']}
        verification = {'checks': [{'finding_id': 'facts:0', 'verdict': 'supported'},
                                  {'finding_id': 'facts:1', 'verdict': 'unsupported'}], 'coverage': 'complete'}
        provider = Provider([self.f.extracted([self.f.receipt(), contact]), self.f.bound()], verification)
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual([f['kind'] for f in result['facts']], ['submission'])
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(result.validation_diagnostic['issues'], [])
        self.assertEqual(result.validation_diagnostic['corrections'][0]['reason'], 'unsupported_finding_removed')
        validate_diagnostic(result.validation_diagnostic)
        identity = provider.calls[-1][0]
        self.assertEqual(len(identity['findings']), 1)

    def test_uncertain_semantics_keeps_finding_visible_without_identity(self):
        provider = Provider([self.f.extracted()], {'checks': [{'finding_id': 'facts:0', 'verdict': 'uncertain'}], 'coverage': 'complete'})
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(len(result['facts']), 1)
        self.assertIsNone(result['facts'][0]['target_id'])
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'uncertain_semantics')
        self.assertEqual(len(provider.calls), 2)
        validate_diagnostic(result.validation_diagnostic)

    def test_complete_source_read_can_flag_omitted_outcome_in_receipt_subject_email(self):
        provider = Provider([self.f.extracted(), self.f.bound()], {'checks': [{'finding_id': 'facts:0', 'verdict': 'supported'}], 'coverage': 'possibly_missing'})
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'possibly_missing_findings')

    def test_independent_request_survives_rejected_contact_and_receipt(self):
        request = {'kind': 'assessment', 'target_id': None, 'confidence': None,
            'outcome': 'Complete the assessment', 'requirement': 'required',
            'responsible_party': 'applicant', 'channel': 'portal',
            'evidence': [self.f.citation('Please complete the assessment.')]}
        extracted = self.f.extracted()
        extracted['requests'] = [request]
        bound = self.f.bound()
        provider = Provider([extracted, bound], {'checks': [
            {'finding_id': 'facts:0', 'verdict': 'supported'},
            {'finding_id': 'requests:0', 'verdict': 'supported'}], 'coverage': 'complete'})
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(result['requests'][0]['target_id'], 'app-1')
        self.assertEqual(result['facts'][0]['kind'], 'submission')

    def test_thread_or_outgoing_content_cannot_create_new_fact_even_if_model_supports_it(self):
        for source_role in ('outgoing', 'thread'):
            s = self.context.sources[0]
            s = replace(s, direction='outgoing') if source_role == 'outgoing' else replace(s, role='thread')
            context = replace(self.context, sources=(s,))
            # IDs depend on immutable identity/text, not role labels.
            provider = Provider([self.f.extracted()])
            result = SharedAnalyzer(provider).analyze(context)
            self.assertEqual(result['facts'], [])
            self.assertFalse(result.validation_diagnostic['issues'])
            self.assertEqual(result.validation_diagnostic['corrections'][0]['reason'], 'unsupported_finding_removed')

    def test_verifier_must_cover_each_finding_exactly_once(self):
        for checks in [[], [{'finding_id': 'facts:0', 'verdict': 'supported'}] * 2,
                       [{'finding_id': 'unknown', 'verdict': 'supported'}]]:
            provider = Provider([self.f.extracted()], {'checks': checks, 'coverage': 'complete'})
            result = SharedAnalyzer(provider).analyze(self.context)
            self.assertIsNone(result['facts'][0]['target_id'])
            self.assertEqual(result.validation_diagnostic['issues'][0]['stage'], 'verification')

    def test_semantic_stage_checkpoint_resumes_without_reextracting(self):
        from job_search.inference.usage import UsageDeferred
        class DeferredSemantic(Provider):
            deferred = False
            def generate(self, messages, **kwargs):
                if kwargs['schema_name'] == 'mail_semantic_verification_v2' and not self.deferred:
                    self.deferred = True
                    raise UsageDeferred('inference_daily_token_limit', '2099-01-01T00:00:00Z')
                return super().generate(messages, **kwargs)
        provider = DeferredSemantic([self.f.extracted(), self.f.bound()])
        saved = {}
        def checkpoint(key, value=None):
            if value is None:
                return deepcopy(saved.get(key))
            saved[key] = deepcopy(value)
        analyzer = SharedAnalyzer(provider)
        with self.assertRaises(UsageDeferred):
            analyzer.analyze_with_checkpoints(self.context, checkpoint)
        result = analyzer.analyze_with_checkpoints(self.context, checkpoint)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(sum(c[1]['schema_name'] == 'mail_extraction_v3' for c in provider.calls), 1)

    def test_each_phase_receives_only_its_own_output_contract(self):
        provider = Provider([self.f.extracted(), self.f.bound()])
        prompts = []
        generate = provider.generate
        def capture(messages, **kwargs):
            prompts.append((kwargs['schema_name'], messages[0]['content']))
            return generate(messages, **kwargs)
        provider.generate = capture
        SharedAnalyzer(provider).analyze(self.context)
        for phase, prompt in prompts:
            if phase == 'mail_extraction_v3':
                self.assertIn('Return fields relevance, associations', prompt)
            else:
                self.assertNotIn('Return fields relevance, associations', prompt)
                self.assertNotIn('For unrelated email, return empty', prompt)
                if 'verification' in phase:
                    self.assertNotIn('uncertainties', prompt)
                    self.assertNotIn('candidate_retrieval', prompt)
                self.assertIn('Return exactly fields checks and coverage' if 'verification' in phase
                              else 'Return exactly fields target_id, resolution, evidence, records and uncertainties', prompt)

    def test_wrong_verifier_envelope_gets_one_bounded_contract_correction(self):
        def verification(payload, kwargs):
            rows = [{'finding_id': f['finding_id'], 'verdict': 'supported'} for f in payload['findings']]
            value = {'checks': rows, 'coverage': 'complete'}
            if kwargs['schema_name'] == 'mail_semantic_verification_v2':
                value['facts'] = []
            return value
        provider = Provider([self.f.extracted(), self.f.bound()], verification)
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(result.validation_diagnostic['repairs'], 1)
        repair = provider.calls[2][0]
        self.assertEqual(repair['validation_errors'][0]['reason'], 'unknown_field')
        self.assertFalse(result.validation_diagnostic['issues'])

    def test_verifier_duplicate_check_repairs_only_failed_sibling(self):
        extracted = self.f.extracted([self.f.receipt(), self.f.receipt()])
        def verification(payload, kwargs):
            if kwargs['schema_name'] == 'mail_semantic_verification_v2':
                return {'checks': [{'finding_id': 'facts:0', 'verdict': 'supported'},
                    {'finding_id': 'facts:1', 'verdict': 'unsupported'},
                    {'finding_id': 'facts:1', 'verdict': 'supported'}], 'coverage': 'complete'}
            self.assertEqual([f['finding_id'] for f in payload['findings']], ['facts:1'])
            self.assertEqual(payload['previously_verified_findings'][0]['finding_id'], 'facts:0')
            return {'checks': [{'finding_id': 'facts:1', 'verdict': 'unsupported'}], 'coverage': 'complete'}
        provider = Provider([extracted, self.f.bound()], verification)
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(len(result['facts']), 1)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertFalse(result.validation_diagnostic['issues'])
        self.assertEqual(result.validation_diagnostic['corrections'][0]['position'], 1)

    def test_verifier_repair_failure_keeps_good_sibling_and_cannot_loop(self):
        extracted = self.f.extracted([self.f.receipt(), self.f.receipt()])
        def verification(payload, kwargs):
            return {'checks': [{'finding_id': 'facts:0', 'verdict': 'supported'}]
                    if kwargs['schema_name'] == 'mail_semantic_verification_v2' else [], 'coverage': 'complete'}
        provider = Provider([extracted, self.f.bound()], verification)
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertIsNone(result['facts'][1]['target_id'])
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'missing_binding')
        self.assertEqual(sum('verification' in c[1]['schema_name'] for c in provider.calls), 2)

    def test_verifier_repair_deferral_resumes_both_successful_stages(self):
        from job_search.inference.usage import UsageDeferred
        class RepairDeferred(Provider):
            deferred = False
            def generate(self, messages, **kwargs):
                if kwargs['schema_name'] == 'mail_semantic_verification_repair_v2' and not self.deferred:
                    self.deferred = True
                    raise UsageDeferred('inference_daily_token_limit', '2099-01-01T00:00:00Z')
                return super().generate(messages, **kwargs)
        def verification(payload, kwargs):
            if kwargs['schema_name'] == 'mail_semantic_verification_v2':
                return {'checks': [], 'coverage': 'complete'}
            return {'checks': [{'finding_id': f['finding_id'], 'verdict': 'supported'} for f in payload['findings']], 'coverage': 'complete'}
        provider = RepairDeferred([self.f.extracted(), self.f.bound()], verification)
        saved = {}
        def checkpoint(key, value=None):
            if value is None:
                return deepcopy(saved.get(key))
            saved[key] = deepcopy(value)
        analyzer = SharedAnalyzer(provider)
        with self.assertRaises(UsageDeferred):
            analyzer.analyze_with_checkpoints(self.context, checkpoint)
        result = analyzer.analyze_with_checkpoints(self.context, checkpoint)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        for name in ('mail_extraction_v3', 'mail_semantic_verification_v2'):
            self.assertEqual(sum(c[1]['schema_name'] == name for c in provider.calls), 1)
        self.assertEqual(result.validation_diagnostic['repairs'], 1)
        validate_diagnostic(result.validation_diagnostic)

    def test_three_distinct_stage_repairs_have_valid_bounded_diagnostic(self):
        from job_search.applications.understanding.analyzer import AnalysisResult
        result = AnalysisResult({}, [], 3)
        self.assertEqual(result.validation_diagnostic['version'], 3)
        validate_diagnostic(result.validation_diagnostic)
        with self.assertRaises(DomainError):
            validate_diagnostic({**result.validation_diagnostic, 'repairs': 4})

    def test_one_canonical_identity_assigns_receipt_and_request_without_individual_votes(self):
        extracted = self.f.extracted([self.f.receipt(), self.f.receipt()])
        extracted['associations'] = [
            {'kind': 'application', 'target_id': None, 'confidence': None, 'evidence': self.f.receipt()['evidence']},
            {'kind': 'application', 'target_id': None, 'confidence': None, 'evidence': self.f.receipt()['evidence']}]
        provider = Provider([extracted, self.f.bound()])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(len(result['associations']), 1)
        self.assertEqual(result['associations'][0]['target_id'], 'app-1')
        self.assertEqual({f['target_id'] for f in result['facts']}, {'app-1'})
        identity_schema = provider.calls[-1][1]['json_schema']
        self.assertNotIn('bindings', identity_schema['properties'])
        self.assertEqual(identity_schema['properties']['records']['maxItems'], 0)

    def test_per_finding_target_overrides_are_rejected_by_canonical_wire(self):
        wrong = self.f.bound()
        wrong['bindings'] = [{'finding_id': 'facts:0', 'target_id': 'app-2'}]
        provider = Provider([self.f.extracted(), wrong, self.f.bound()])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(len(result['associations']), 1)
        self.assertEqual(result.validation_diagnostic['repairs'], 1)

    def test_conflicting_employers_abstain_globally_and_remain_partial(self):
        conflict = self.f.bound(None)
        conflict['resolution'] = 'conflicting'
        provider = Provider([self.f.extracted(), conflict])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertTrue(all(f['target_id'] is None for f in result['facts']))
        self.assertEqual(result['associations'], [])
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'conflicting_identity')
        validate_diagnostic(result.validation_diagnostic)

    def test_more_than_one_current_authored_message_never_shares_a_canonical_target(self):
        context = replace(self.context, sources=(*self.context.sources, source('A different application', identity='other')))
        provider = Provider([self.f.extracted()])
        result = SharedAnalyzer(provider).analyze(context)
        self.assertIsNone(result['facts'][0]['target_id'])
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'multiple_authored_sources')
        self.assertEqual(len(provider.calls), 2)

    def test_thread_context_does_not_count_as_another_current_message(self):
        context = replace(self.context, sources=(*self.context.sources,
            replace(source('Earlier different application', identity='thread'), role='thread')))
        provider = Provider([self.f.extracted(), self.f.bound()])
        result = SharedAnalyzer(provider).analyze(context)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertFalse(result.validation_diagnostic['issues'])

    def test_existing_reviewed_message_link_cannot_be_silently_replaced(self):
        context = replace(self.context, context={**self.context.context,
            'mail_projection': {'target_application_id': 'app-1'}})
        provider = Provider([self.f.extracted(), self.f.bound('app-2')])
        result = SharedAnalyzer(provider).analyze(context)
        self.assertIsNone(result['facts'][0]['target_id'])
        self.assertFalse(any(a['target_id'] == 'app-2' for a in result['associations']))
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'existing_association_conflict')

    def test_catalog_identity_emits_one_job_association_and_keeps_receipt_unassigned(self):
        provider = Provider([self.f.extracted(), self.f.bound('catalog:one')])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual([(a['kind'], a['target_id']) for a in result['associations']], [('job', 'catalog:one')])
        self.assertIsNone(result['facts'][0]['target_id'])

    def test_typed_record_error_repairs_only_record_while_canonical_identity_and_siblings_stay_fixed(self):
        from job_search.applications.understanding.recipes import FACT_FIELDS
        fact = {'kind': 'interview', 'value': {field: None for field in FACT_FIELDS['interview']},
                'target_id': None, 'confidence': None, 'evidence': self.f.receipt()['evidence']}
        fact['value']['status'] = 'scheduled'
        bound = self.f.bound()
        bound['records'] = [{'finding_id': 'facts:1', 'record_id': 'interview-2'}]
        provider = Provider([self.f.extracted([self.f.receipt(), fact]), bound,
            {'records': [{'finding_id': 'facts:1', 'record_id': 'interview-1'}]}])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual({f['target_id'] for f in result['facts']}, {'app-1'})
        self.assertEqual(result['facts'][1]['value']['interview_id'], 'interview-1')
        self.assertFalse(result.validation_diagnostic['issues'])
        repair = provider.calls[-1]
        self.assertEqual(repair[1]['schema_name'], 'mail_record_repair_v1')
        self.assertEqual(repair[0]['canonical_target_id'], 'app-1')
        self.assertEqual([f['finding_id'] for f in repair[0]['findings']], ['facts:1'])
        self.assertNotIn('target_id', repair[1]['json_schema']['properties'])

    def test_record_repair_deferral_reuses_canonical_identity_and_other_completed_stages(self):
        from job_search.applications.understanding.recipes import FACT_FIELDS
        from job_search.inference.usage import UsageDeferred
        fact = {'kind': 'interview', 'value': {field: None for field in FACT_FIELDS['interview']},
                'target_id': None, 'confidence': None, 'evidence': self.f.receipt()['evidence']}
        fact['value']['status'] = 'scheduled'
        bound = self.f.bound()
        bound['records'] = [{'finding_id': 'facts:1', 'record_id': 'interview-2'}]
        class DeferredRecordProvider(Provider):
            deferred = False
            def generate(self, messages, **kwargs):
                if kwargs['schema_name'] == 'mail_record_repair_v1' and not self.deferred:
                    self.deferred = True
                    raise UsageDeferred('inference_daily_token_limit', '2099-01-01T00:00:00Z')
                return super().generate(messages, **kwargs)
        provider = DeferredRecordProvider([self.f.extracted([self.f.receipt(), fact]), bound,
            {'records': [{'finding_id': 'facts:1', 'record_id': 'interview-1'}]}])
        saved = {}
        def checkpoint(key, value=None):
            if value is None:
                return deepcopy(saved.get(key))
            saved[key] = deepcopy(value)
        analyzer = SharedAnalyzer(provider)
        with self.assertRaises(UsageDeferred):
            analyzer.analyze_with_checkpoints(self.context, checkpoint)
        result = analyzer.analyze_with_checkpoints(self.context, checkpoint)
        self.assertEqual(result['facts'][1]['value']['interview_id'], 'interview-1')
        for name in ('mail_extraction_v3', 'mail_semantic_verification_v2', 'mail_identity_v4'):
            self.assertEqual(sum(c[1]['schema_name'] == name for c in provider.calls), 1)

    def test_identity_input_has_no_forced_null_targets_or_lexical_retrieval_votes(self):
        context = replace(self.context, candidates=tuple({**c, 'retrieval_reasons': ['current_employer_partial']} for c in self.context.candidates),
            context={**self.context.context, 'mail_projection': {'association_required': True, 'target_application_id': None}})
        before = deepcopy(context.descriptor())
        provider = Provider([self.f.extracted(), self.f.bound()])
        result = SharedAnalyzer(provider).analyze(context)
        data = provider.calls[-1][0]
        self.assertNotIn('association', data)
        self.assertNotIn('reviewed_message_association', data)
        self.assertEqual([c['id'] for c in data['candidates']], [c['id'] for c in context.candidates])
        self.assertTrue(all('retrieval_reasons' not in c for c in data['candidates']))
        self.assertTrue(all('target_id' not in f['finding'] and 'confidence' not in f['finding'] for f in data['findings']))
        self.assertNotIn('submission_id', data['findings'][0]['finding']['value'])
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(context.descriptor(), before)

    def test_identity_input_preserves_an_actual_reviewed_link_as_context(self):
        context = replace(self.context, context={**self.context.context,
            'mail_projection': {'association_required': False, 'target_application_id': 'app-1'}})
        provider = Provider([self.f.extracted(), self.f.bound()])
        SharedAnalyzer(provider).analyze(context)
        data = provider.calls[-1][0]
        self.assertEqual(data['reviewed_message_association'], {'target_application_id': 'app-1'})
        self.assertNotIn('association', data)

    def test_real_same_employer_competing_roles_still_abstain_globally(self):
        context = replace(self.context, candidates=(
            {'id': 'app-1', 'job': {'employer': 'Example', 'title': 'Engineer, Platform'}},
            {'id': 'app-2', 'job': {'employer': 'Example', 'title': 'Engineer, Infrastructure'}}))
        bound = self.f.bound(None)
        bound['uncertainties'] = ['Two actual roles at the named employer fit the email.']
        provider = Provider([self.f.extracted(), bound])
        result = SharedAnalyzer(provider).analyze(context)
        self.assertIsNone(result['facts'][0]['target_id'])
        self.assertFalse(any(a['target_id'] for a in result['associations']))
        self.assertEqual(len(provider.calls[-1][0]['candidates']), 2)
        self.assertTrue(result['uncertainties'])

    def test_source_inventory_sees_no_model_claims_and_can_override_rubberstamped_contact(self):
        contact = {'kind': 'contact', 'value': 'recruiter_contact', 'target_id': None,
                   'confidence': .99, 'evidence': self.f.receipt()['evidence']}
        def inventory(payload, kwargs):
            self.assertEqual(set(payload), {'sources', 'coverage', 'thread_coverage', 'topics'})
            self.assertNotIn('findings', payload)
            self.assertNotIn('candidates', payload)
            return {'support': [{'topic': 'personal_communication', 'classification': 'routine_or_automated',
                                 'evidence': [self.f.citation('Your application is in the queue.')]}]}
        provider = Provider([self.f.extracted([self.f.receipt(), contact]), self.f.bound()], inventory=inventory)
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual([f['kind'] for f in result['facts']], ['submission'])
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertFalse(result.validation_diagnostic['issues'])
        self.assertEqual(result.validation_diagnostic['corrections'][0]['position'], 1)
        verify = next(p for p, kw in provider.calls if kw['schema_name'] == 'mail_semantic_verification_v2')
        self.assertEqual(verify['independent_support'][0]['classification'], 'routine_or_automated')

    def test_uncertain_inventory_keeps_contact_visible_for_review(self):
        contact = {'kind': 'contact', 'value': 'recruiter_contact', 'target_id': None,
                   'confidence': None, 'evidence': self.f.receipt()['evidence']}
        inventory = {'support': [{'topic': 'personal_communication', 'classification': 'uncertain',
                                 'evidence': self.f.receipt()['evidence']}]}
        provider = Provider([self.f.extracted([self.f.receipt(), contact]), self.f.bound()], inventory=inventory)
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual([f['kind'] for f in result['facts']], ['submission', 'contact'])
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertIsNone(result['facts'][1]['target_id'])
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'uncertain_semantics')
        self.assertNotIn('corrections', result.validation_diagnostic)

    def test_substantive_personal_support_preserves_legitimate_contact(self):
        contact = {'kind': 'contact', 'value': 'recruiter_contact', 'target_id': None,
                   'confidence': None, 'evidence': self.f.receipt()['evidence']}
        provider = Provider([self.f.extracted([contact]), self.f.bound()])
        result = SharedAnalyzer(provider).analyze(self.context)
        self.assertEqual(result['facts'][0]['kind'], 'contact')
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertFalse(result.validation_diagnostic['issues'])

    def temporal_case(self, classification, *, only_temporal=False, wire_error=False):
        f = fixtures.BlockTests()
        f.setUp()
        text = ('' if only_temporal else fixtures.TEXT) + 'The team usually replies in a week.\n'
        f.context = replace(f.context, sources=(source(text),))
        f.blocks = source_blocks(f.context)[0]['blocks']
        context = replace(self.context, sources=f.context.sources)
        temporal = {'kind': 'interval', 'wording': 'in a week', 'normalized': '2026-10-26',
            'timezone': None, 'missing': ['timezone', 'reference_date'], 'confidence': None,
            'evidence': [f.citation('The team usually replies in a week.')]}
        if wire_error:
            temporal['unexpected_field'] = 'not authorized'
        extracted = f.extracted([] if only_temporal else None)
        extracted['temporal_facts'] = [temporal]
        inventory = {'support': [{'topic': 'temporal_commitment', 'classification': classification,
                                 'evidence': temporal['evidence']}]}
        responses = [extracted, {'repairs': []}] + ([] if only_temporal else [f.bound()])
        return context, Provider(responses, inventory=inventory)

    def test_quarantined_false_time_interpretation_can_be_deliberately_rejected(self):
        context, provider = self.temporal_case('descriptive_response_estimate')
        result = SharedAnalyzer(provider).analyze(context)
        self.assertEqual(result['temporal_facts'], [])
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertFalse(result.validation_diagnostic['issues'])
        self.assertEqual(result.validation_diagnostic['corrections'][0]['category'], 'temporal_facts')
        verify = next(p for p, kw in provider.calls if kw['schema_name'] == 'mail_semantic_verification_v2')
        self.assertEqual(verify['quarantined_findings'], ['temporal_facts:0'])
        self.assertTrue(any(f['finding_id'] == 'temporal_facts:0' for f in verify['findings']))

    def test_supported_or_uncertain_quarantine_keeps_mapping_issue_and_never_publishes(self):
        for classification in ('actionable_commitment', 'uncertain'):
            context, provider = self.temporal_case(classification)
            result = SharedAnalyzer(provider).analyze(context)
            self.assertEqual(result['temporal_facts'], [])
            self.assertEqual(result['facts'][0]['target_id'], 'app-1')
            self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'incomplete_temporal')
            self.assertNotIn('corrections', result.validation_diagnostic)

    def test_all_quarantined_domain_candidates_can_be_rejected_without_claiming_a_fact(self):
        context, provider = self.temporal_case('descriptive_response_estimate', only_temporal=True)
        result = SharedAnalyzer(provider).analyze(context)
        self.assertTrue(all(result[group] == [] for group in ('facts', 'requests', 'associations', 'temporal_facts')))
        self.assertFalse(result.validation_diagnostic['issues'])
        self.assertEqual(len(result.validation_diagnostic['corrections']), 1)

    def test_wire_invalid_candidate_cannot_use_semantic_rejection_to_clear_error(self):
        context, provider = self.temporal_case('descriptive_response_estimate', wire_error=True)
        result = SharedAnalyzer(provider).analyze(context)
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'unknown_field')
        self.assertFalse(any(kw['schema_name'] == 'mail_source_support_v1' for _, kw in provider.calls))
        self.assertNotIn('corrections', result.validation_diagnostic)

    def test_positive_source_support_prevents_normalization_error_from_erasing_real_time(self):
        context, provider = self.temporal_case('actionable_commitment')
        def verification(payload, kwargs):
            return {'checks': [{'finding_id': f['finding_id'],
                               'verdict': 'unsupported' if f['category'] == 'temporal_facts' else 'supported'}
                              for f in payload['findings']], 'coverage': 'complete'}
        provider.verification = verification
        result = SharedAnalyzer(provider).analyze(context)
        self.assertEqual(result.validation_diagnostic['issues'][0]['reason'], 'incomplete_temporal')
        self.assertTrue(any(i['reason'] == 'uncertain_semantics' for i in result.validation_diagnostic['issues']))
        self.assertNotIn('corrections', result.validation_diagnostic)

    def test_source_inventory_deferral_resumes_without_repeating_extraction(self):
        from job_search.inference.usage import UsageDeferred
        contact = {'kind': 'contact', 'value': 'recruiter_contact', 'target_id': None,
                   'confidence': None, 'evidence': self.f.receipt()['evidence']}
        class InventoryDeferred(Provider):
            deferred = False
            def generate(self, messages, **kwargs):
                if kwargs['schema_name'] == 'mail_source_support_v1' and not self.deferred:
                    self.deferred = True
                    raise UsageDeferred('inference_daily_token_limit', '2099-01-01T00:00:00Z')
                return super().generate(messages, **kwargs)
        provider = InventoryDeferred([self.f.extracted([contact]), self.f.bound()])
        saved = {}
        def checkpoint(key, value=None):
            if value is None:
                return deepcopy(saved.get(key))
            saved[key] = deepcopy(value)
        analyzer = SharedAnalyzer(provider)
        with self.assertRaises(UsageDeferred):
            analyzer.analyze_with_checkpoints(self.context, checkpoint)
        result = analyzer.analyze_with_checkpoints(self.context, checkpoint)
        self.assertEqual(result['facts'][0]['target_id'], 'app-1')
        self.assertEqual(sum(kw['schema_name'] == 'mail_extraction_v3' for _, kw in provider.calls), 1)


if __name__ == '__main__':
    unittest.main()
