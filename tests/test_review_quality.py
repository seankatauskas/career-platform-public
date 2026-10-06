"""Offline v2 review quality, calibration, eligibility, and compatibility gates."""
import copy
import json
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.brief import suggested_search_brief
from job_search.job_reviews.contracts import validate_assessment
from job_search.job_reviews.reviewer_api import project_response
from job_search.job_reviews.reviewer_mcp import tools_for
from job_search.job_reviews.model_gateway import validate_request, validate_response, GatewayRejected
from job_search.job_reviews.codex_runtime import RuntimeConfig, readiness, IMAGE_LABEL, CONTRACT_LABEL
from tests import test_agent_job_reviews as fixtures
from tests import test_review_authority as authority_fixtures
from tests.test_codex_review_runtime import request, response


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.command('save-brief', brief=suggested_search_brief(), expected_revision=0)
        self.check_calls = []
        self.fixture.service.availability_checker = self.checker

    def checker(self, jobs):
        self.check_calls.append(copy.deepcopy(jobs))
        # A writer can acquire the ledger during the network phase.
        with sqlite3.connect(self.fixture.ledger.store.db_path, timeout=.01) as con:
            con.execute('BEGIN IMMEDIATE')
        return [dict(ats=j['ats'], job_id=j['id'], status='unknown', checked_at=self.fixture.now,
                     source='', reason='network unavailable in offline fixture') for j in jobs]

    def value(self, decision='close', **changes):
        defaults = dict(eligibility='no_known_barrier', eligibility_condition='', next_step='apply', category='core')
        defaults.update(changes)
        return self.fixture.assessment(decision, **defaults)

    def start(self):
        return self.fixture.start(rubric_version='job-review-v2')

    def agree(self, rid, ordinal=1, value=None):
        self.fixture.assess(rid, ordinal=ordinal, value=value or self.value())
        self.fixture.assess(rid, ordinal=ordinal, kind='check', value=value or self.value())

    def calibrate(self, rid, order, group=None):
        page = self.fixture.command('calibration', review_id=rid)
        for position, ordinal in enumerate(order, 1):
            self.fixture.command('calibrate', review_id=rid, basis_sha256=page['basis_sha256'], ordinal=ordinal,
                                 position=position, related_group=group)
        self.fixture.command('finalize', review_id=rid, basis_sha256=page['basis_sha256'])
        return page['basis_sha256']

    def test_user_brief_is_frozen_and_only_confirmed_policy_constrains_alternatives(self):
        self.assertEqual(self.fixture.command('brief')['revision'], 1)
        rid = self.start()
        value = self.value(category='alternative')
        with self.assertRaisesRegex(ContractError, 'career alternatives'):
            self.fixture.assess(rid, value=value)
        brief = suggested_search_brief()
        brief['adjacent_roles'] = 'targeted'
        self.fixture.command('save-brief', brief=brief, expected_revision=1)
        with self.assertRaisesRegex(ContractError, 'career alternatives'):
            self.fixture.assess(rid, value=value)
        newer = self.start()
        self.agree(newer, value=value)
        frozen = self.fixture.command('context', review_id=rid)
        self.assertEqual(frozen['search_brief']['revision'], 1)
        self.assertEqual(self.fixture.command('status', review_id=rid)['metadata']['search_brief']['revision'], 1)

    def test_v2_requires_separate_dimensions_and_hard_eligibility_is_not_targeted(self):
        rid = self.start()
        with self.assertRaises(ContractError):
            self.fixture.assess(rid, value=self.fixture.assessment())
        for changes in (dict(eligibility='unresolved'), dict(eligibility='ineligible', eligibility_condition='Required active clearance absent', next_step='explore')):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.fixture.assess(rid, value=self.value(**changes))
        self.agree(rid, value=self.value(eligibility='unresolved', eligibility_condition='Active clearance unconfirmed', next_step='clarify'))
        self.assertEqual(self.fixture.command('status', review_id=rid)['counts'], {'close': 1})

    def test_material_dimensions_disagree_without_comparing_wording(self):
        rid = self.start()
        self.fixture.assess(rid, value=self.value())
        self.fixture.assess(rid, kind='check', value=self.value(eligibility='unresolved', eligibility_condition='Need clearance confirmation', next_step='clarify'))
        self.assertEqual(self.fixture.command('status', review_id=rid)['disagreement_count'], 1)
        with self.assertRaisesRegex(ContractError, 'independent agreement'):
            self.calibrate(rid, [1])
        self.fixture.assess(rid, kind='check', value=self.value(explanation='Independently found API experience.'))
        self.assertEqual(self.fixture.command('status', review_id=rid)['disagreement_count'], 0)

    def test_unselected_metadata_differences_do_not_create_false_disagreements(self):
        rid = self.start()
        primary = self.value('exclude', stage='screening', reason_code='non_technical',
                             alignment='unrelated', next_step='explore')
        self.fixture.assess(rid, value=primary)
        check = self.value('exclude', stage='screening', reason_code='non_technical',
                           alignment='unrelated', eligibility='unresolved',
                           eligibility_condition='Eligibility was not investigated for unrelated work',
                           next_step='clarify', category='alternative')
        self.fixture.assess(rid, kind='check', value=check)
        self.assertEqual(self.fixture.command('status', review_id=rid)['disagreement_count'], 0)
        self.calibrate(rid, [])
        self.assertTrue(self.fixture.command('preview', review_id=rid)['ready'])
        for changes in (dict(alignment='unknown'), dict(decision='needs_info')):
            with self.subTest(changes=changes):
                self.fixture.assess(rid, kind='check', value=dict(check, **changes))
                self.assertEqual(self.fixture.command('status', review_id=rid)['disagreement_count'], 1)
                self.assertIn('reviewer_disagreements', self.fixture.command('preview', review_id=rid)['blockers'])

    def test_every_selected_dimension_still_requires_independent_agreement(self):
        rid = self.start()
        primary = self.value('broad_only')
        self.fixture.assess(rid, value=primary)
        for changes in (dict(category='alternative'), dict(next_step='explore'),
                        dict(eligibility='unresolved', eligibility_condition='Condition unknown', next_step='clarify')):
            with self.subTest(changes=changes):
                self.fixture.assess(rid, kind='check', value=dict(primary, **changes))
                self.assertEqual(self.fixture.command('status', review_id=rid)['disagreement_count'], 1)
        self.fixture.assess(rid, kind='check', value=primary)
        self.assertEqual(self.fixture.command('status', review_id=rid)['disagreement_count'], 0)

    def test_conditional_close_fit_stays_first_and_caveats_survive_publication(self):
        self.fixture.insert('b')
        rid = self.start()
        self.agree(rid, 1, self.value(priority=100, eligibility='unresolved', eligibility_condition='Active clearance not established by citizenship',
                                     next_step='clarify', gaps=['C++ production tenure is a gap'], unknowns=['Title and role body differ']))
        self.agree(rid, 2, self.value('slight_stretch', priority=1))
        group = {'id': 'example-api', 'label': 'Related API requisitions'}
        self.calibrate(rid, [1, 2], group)
        preview = self.fixture.command('preview', review_id=rid)
        self.assertIn('availability_check_required', preview['blockers'])
        self.fixture.command('verify-availability', review_id=rid)
        receipt = self.fixture.finish(rid)
        targeted = next(item for item in receipt['lists'] if item['kind'] == 'targeted')
        rows = self.fixture.service.curated.get(targeted['list_id'])['recommendations']
        self.assertEqual([row['id'] for row in rows], ['a', 'b'])
        explanation = rows[0]['explanation']
        for expected in ('Close fit', 'Active clearance', 'C++ production tenure', 'Title and role body differ', 'Clarify'):
            self.assertIn(expected, explanation)
        summary = self.fixture.service.publication_summaries(targeted['list_id'])[('ashby', 'a')]
        self.assertTrue(summary['narrative_complete'])
        self.assertEqual(summary['related_group'], group)
        self.assertEqual(summary['availability']['status'], 'unknown')
        self.assertEqual(summary['decision'], 'close')

    def test_calibration_membership_completeness_and_any_assessment_change_invalidates_it(self):
        self.fixture.insert('b')
        rid = self.start()
        self.agree(rid, 1)
        self.agree(rid, 2)
        page = self.fixture.command('calibration', review_id=rid, limit=1)
        self.assertEqual(page['next_after'], 1)
        for ordinal in (1, 2):
            self.fixture.command('calibrate', review_id=rid, ordinal=ordinal, basis_sha256=page['basis_sha256'], position=1)
        with self.assertRaisesRegex(ContractError, 'complete order'):
            self.fixture.command('finalize', review_id=rid, basis_sha256=page['basis_sha256'])
        self.fixture.command('calibrate', review_id=rid, ordinal=2, basis_sha256=page['basis_sha256'], position=2)
        self.fixture.command('finalize', review_id=rid, basis_sha256=page['basis_sha256'])
        self.fixture.assess(rid, ordinal=2, value=self.value(explanation='Updated assessment after new review.'))
        self.assertFalse(self.fixture.command('status', review_id=rid)['calibration']['complete'])
        with self.assertRaisesRegex(ContractError, 'evidence changed'):
            self.fixture.command('calibrate', review_id=rid, ordinal=1, basis_sha256=page['basis_sha256'], position=1)

    def test_availability_idempotency_unknowns_absence_and_basis_race(self):
        rid = self.start()
        self.agree(rid)
        self.calibrate(rid, [1])
        response = self.fixture.command('verify-availability', review_id=rid, idempotency_key='same-verification')
        self.assertEqual(self.fixture.command('verify-availability', review_id=rid, idempotency_key='same-verification'), response)
        self.assertEqual(len(self.check_calls), 1)
        old = self.fixture.command('preview', review_id=rid)
        self.fixture.service.availability_checker = lambda jobs: [dict(ats='ashby', job_id='a', status='absent', checked_at=self.fixture.now, source='https://api.ashbyhq.com/posting-api/job-board/Example', reason='Not in complete board')]
        self.fixture.command('verify-availability', review_id=rid)
        absent = self.fixture.command('preview', review_id=rid)
        self.assertEqual(absent['omitted'], [{'ordinal': 1, 'reason': 'absent_from_official_board'}])
        self.assertNotEqual(absent['preview_sha256'], old['preview_sha256'])
        def race(jobs):
            self.fixture.assess(rid, value=self.value())
            return self.checker(jobs)
        self.fixture.service.availability_checker = race
        with self.assertRaisesRegex(ContractError, 'changed during'):
            self.fixture.command('verify-availability', review_id=rid)
        with sqlite3.connect(self.fixture.db) as con:
            self.assertIsNone(con.execute('SELECT closed_at FROM jobs').fetchone()[0])

    def test_legacy_cards_keep_text_and_have_no_invented_eligibility(self):
        rid = self.fixture.start()
        self.fixture.assess(rid)
        self.fixture.assess(rid, kind='check')
        receipt = self.fixture.finish(rid)
        summary = self.fixture.service.publication_summaries(receipt['lists'][0]['list_id'])[('ashby', 'a')]
        self.assertNotIn('eligibility', summary)
        self.assertFalse(summary['narrative_complete'])

    def test_empty_v2_review_can_finalize_and_publish_without_network(self):
        with sqlite3.connect(self.fixture.db) as con:
            con.execute('DELETE FROM jobs')
        rid = self.start()
        self.calibrate(rid, [])
        self.fixture.command('verify-availability', review_id=rid)
        receipt = self.fixture.finish(rid)
        self.assertEqual([entry['job_count'] for entry in receipt['lists']], [0, 0])


class FinalizerTests(unittest.TestCase):
    def setUp(self):
        self.scope = authority_fixtures.AuthorityTests('runTest')
        self.scope.setUp()
        self.addCleanup(self.scope.doCleanups)
        self.fixture = self.scope.fixture
        self.authority = self.scope.authority
        self.value = self.fixture.assessment(eligibility='unresolved', eligibility_condition='Clearance is unconfirmed', next_step='clarify', category='core')
        self.fixture.service.availability_checker = lambda jobs: [dict(ats=j['ats'], job_id=j['id'], status='unknown', checked_at=self.fixture.now, source='', reason='offline') for j in jobs]

    def ready(self):
        rid = self.scope.start(rubric_version='job-review-v2')
        for kind in ('primary', 'check'):
            grant = self.scope.issue(rid, kind=kind)
            self.scope.assess(grant, value=self.value)
        return rid, grant

    def test_separate_finalizer_scope_resume_and_submission_provenance(self):
        rid, reviewer = self.ready()
        for operation in ('calibration', 'finalize'):
            with self.assertRaises(ContractError):
                self.authority.scoped_call(reviewer['token'], operation)
        grant = self.scope.issue(rid, ordinals=(), kind='finalizer')
        api = lambda operation, args=None: self.authority.scoped_call(grant['token'], operation, args)
        with self.assertRaises(ContractError):
            api('assessment', {'ordinal': 1, 'assessment': self.value})
        with self.assertRaises(ContractError):
            api('job', {'ordinal': 1})
        self.assertEqual(api('calibration')['selected_count'], 1)
        result = api('calibrate', {'ordinal': 1, 'position': 1})
        self.assertEqual(api('calibrate', {'ordinal': 1, 'position': 1}), result)
        with self.assertRaises(ContractError):
            api('calibrate', {'ordinal': 1, 'position': 1, 'related_group': {'id': 'changed', 'label': 'Changed'}})
        recovered = self.authority.reconcile_interrupted(rid)
        self.assertIn(grant['grant_id'], [entry['grant_id'] for entry in recovered['workers']])
        replacement = self.scope.issue(rid, ordinals=(), kind='finalizer')
        self.authority.scoped_call(replacement['token'], 'finalize')
        self.authority.verify_availability(rid, 'verify-managed')
        self.assertTrue(self.authority.preview(rid)['ready'])
        with sqlite3.connect(self.fixture.ledger.store.db_path) as con:
            con.execute("UPDATE job_review_calibration_entries SET related_group_json=? WHERE review_id=?", (json.dumps({'id': 'tampered', 'label': 'Tampered'}), rid))
        with self.assertRaisesRegex(ContractError, 'sealed artifact'):
            self.authority.preview(rid)

    def test_finalizer_cannot_be_issued_before_independent_checks_or_override_basis(self):
        rid = self.scope.start(rubric_version='job-review-v2')
        with self.assertRaisesRegex(ContractError, 'complete coverage'):
            self.scope.issue(rid, ordinals=(), kind='finalizer')
        primary = self.scope.issue(rid)
        self.scope.assess(primary, value=self.value)
        with self.assertRaisesRegex(ContractError, 'complete coverage'):
            self.scope.issue(rid, ordinals=(), kind='finalizer')
        check = self.scope.issue(rid, kind='check')
        self.scope.assess(check, value=self.value)
        final = self.scope.issue(rid, ordinals=(), kind='finalizer')
        with self.assertRaises(ContractError):
            self.authority.scoped_call(final['token'], 'finalize', {'basis_sha256': 'forged'})
        self.authority.refresh(rid, 1, 2)
        with self.assertRaisesRegex(ContractError, 'changed since'):
            self.authority.scoped_call(final['token'], 'calibration')

    def test_scope_specific_gateway_schemas_and_projection(self):
        primary = {t['name'] for t in tools_for('primary', 'job-review-v2')}
        final = {t['name'] for t in tools_for('finalizer', 'job-review-v2')}
        self.assertEqual(primary, {'review_assignment', 'review_context', 'review_job', 'review_assessment', 'review_assessments'})
        self.assertNotIn('review_assessment', final)
        self.assertIn('review_calibrate', final)
        safe = validate_request(request(), kind='finalizer', rubric_version='job-review-v2')
        self.assertEqual({t['name'] for t in safe['tools']}, {'mcp__review__' + name for name in final})
        with self.assertRaises(GatewayRejected):
            validate_response(response('mcp__review__review_calibrate'))
        validate_response(response('mcp__review__review_calibrate'), kind='finalizer')
        clean = project_response('calibration', {'basis_sha256': 'a' * 64, 'items': [dict(ordinal=1, revision=2, job={'title': 'Engineer', 'model_score': 'SENTINEL'}, assessment=dict(self.value, model_score='SENTINEL'))]})
        self.assertNotIn('SENTINEL', json.dumps(clean))
        from job_search.job_reviews.reviewer_api import ReviewerTransportError
        clean['items'][0]['related_group'] = {'id': {'model_score': 'SENTINEL'}, 'label': 'Bad'}
        with self.assertRaises(ReviewerTransportError):
            project_response('calibration', clean)

    def test_runtime_contract_capability_preserves_old_images_for_v1_only(self):
        config = RuntimeConfig(image='example/reviewer@sha256:' + '1' * 64)
        class Result:
            returncode = 0
            stdout = json.dumps([{'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': {IMAGE_LABEL: '0.160.0'}}}])
        with patch('job_search.job_reviews.codex_runtime._docker', return_value=Result()):
            self.assertTrue(readiness(config)['ready'])
            self.assertEqual(readiness(replace(config, review_contract_version=2))['reason'], 'runtime_review_contract_mismatch')


if __name__ == '__main__':
    unittest.main()
