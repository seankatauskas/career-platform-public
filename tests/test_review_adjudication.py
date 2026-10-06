"""Offline source-bound adjudication, immutable originals and effective publication."""
import copy
import json
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from job_search.contracts import ContractError, canonical_json
from job_search.job_reviews.authority import ReviewAuthorizationError, ReviewConflictError
from job_search.job_reviews.codex_runtime import preload_evidence, review_prompt, RuntimeConfig, readiness
from job_search.job_reviews.reviewer_api import ReviewerClient, assignment_proxy
from job_search.job_reviews.reviewer_mcp import tools_for, ReviewerMCP, serve_stdio
from job_search.job_reviews.model_gateway import validate_response, GatewayRejected
from tests.test_codex_review_runtime import response
from tests import test_review_authority as authority_fixtures


class AdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.h = authority_fixtures.AuthorityTests('runTest'); self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.a, self.f = self.h.authority, self.h.fixture
        self.rid = self.h.start(rubric_version='job-review-v2')
        self.primary = self.value('close')
        self.check = self.value('broad_only', next_step='explore')
        for kind, value in [('primary', self.primary), ('check', self.check)]:
            g = self.h.issue(self.rid, kind=kind)
            self.h.assess(g, value=value)
            if kind == 'primary': self.a.revoke(g['grant_id'])
            else: self.check_grant = g

    def value(self, decision, **kwargs):
        return self.f.assessment(decision, eligibility='no_known_barrier', eligibility_condition='',
                                 category='core', **dict({'next_step': 'apply'}, **kwargs))

    def issue(self):
        return self.h.issue(self.rid, kind='adjudicator')

    def load(self, grant):
        class Client:
            def call(_self, operation, args):
                return self.a.scoped_call(grant['token'], operation, args)
        return preload_evidence(Client())

    def entry(self, grant, choice='check'):
        value = self.a.scoped_call(grant['token'], 'disagreement', {'ordinal': 1})
        return {'ordinal': 1, 'basis_sha256': value['basis_sha256'], 'choice': choice,
                'checked_dimensions': value['differing_dimensions'],
                'explanation': 'Python APIs support broad relevance; scale evidence remains absent.',
                'evidence': [{'field': 'description', 'quote': 'Python APIs', 'fact_id': 'fact-1'}]}

    def save(self, grant, entry):
        return self.a.scoped_call(grant['token'], 'resolutions', {'resolutions': [entry]})['results'][0]

    def large_pair(self, unicode=False):
        quote = ('Python APIs ' + '🙂' * 480) if unicode else ('Build Python APIs and SQL systems. ' * 40)[:1000]
        with sqlite3.connect(self.f.db) as con:
            con.execute('UPDATE jobs SET description=?', (quote,))
        self.rid = self.a.start({'mode':'custom', 'window_start':'2026-10-01T00:00:00Z',
            'rubric_version':'job-review-v2', 'idempotency_key':'large-pair-' + str(unicode)})['review_id']
        for kind, alignment in [('primary','core'), ('check','adjacent')]:
            value = self.value('exclude', stage='detailed', alignment=alignment, reason_code='qualification',
                next_step='explore', explanation=('Specialist scope is not established. '*60)[:2000],
                evidence=[{'field':'description','quote':quote,'fact_id':'fact-1'} for _ in range(12)],
                **{key:[(phrase*30)[:500] for _ in range(10)] for key,phrase in [
                    ('strengths','Python APIs are demonstrated. '), ('gaps','Specialist depth is absent. '),
                    ('unknowns','Scope remains uncertain. ')]})
            g = self.h.issue(self.rid, kind=kind)
            self.h.assess(g, value=value)
            self.a.revoke(g['grant_id'])
        with self.h.ledger() as con:
            row = con.execute('SELECT assessment_json,check_json FROM job_review_items WHERE review_id=?', (self.rid,)).fetchone()
        return [json.loads(v) for v in row]

    def test_large_pair_pages_require_contiguous_reads_and_preload_exact_unicode_evidence(self):
        for unicode in (False, True):
            with self.subTest(unicode=unicode):
                expected = self.large_pair(unicode)
                self.assertGreater(len(canonical_json(expected).encode()), 56*1024)
                g = self.issue()
                first = self.a.scoped_call(g['token'], 'disagreement', {'ordinal':1})
                self.assertEqual(first['encoding'], 'canonical_json')
                self.assertLess(len(canonical_json(first).encode()), 56*1024)
                for section in ('facts','preferences','feedback'):
                    self.a.scoped_call(g['token'],'context',{'section':section})
                self.h.read(g)
                entry = {'ordinal':1, 'basis_sha256':first['basis_sha256'], 'choice':'check',
                    'checked_dimensions':['alignment'], 'explanation':'Python APIs do not establish specialist depth.',
                    'evidence':[{'field':'description','quote':'Python APIs','fact_id':'fact-1'}]}
                last = self.a.scoped_call(g['token'], 'disagreement',
                    {'ordinal':1,'offset':first['total_chars']-10})
                self.assertIsNone(last['next_offset'])
                self.assertEqual(self.save(g,entry)['status'],'error')
                self.assertEqual(self.a.scoped_call(g['token'],'disagreement',{'ordinal':1}),first)
                with self.h.ledger() as con:
                    self.assertEqual(con.execute('SELECT judgments_read FROM job_review_adjudicator_items WHERE grant_id=?',
                        (g['grant_id'],)).fetchone()[0],0)
                    self.assertEqual(con.execute("SELECT through_offset FROM job_review_adjudicator_reads WHERE grant_id=? AND section='judgments:1'",
                        (g['grant_id'],)).fetchone()[0],first['next_offset'])
                with tempfile.TemporaryDirectory() as tmp:
                    with assignment_proxy(Path(tmp)/'review.sock',self.a,g['token']):
                        packet=preload_evidence(ReviewerClient(Path(tmp)/'review.sock'))
                        initialize={'jsonrpc':'2.0','id':0,'method':'initialize','params':{
                            'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{}}}
                        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
                            'name':'review_disagreement','arguments':{'ordinal':1}}}
                        output=io.BytesIO()
                        source=io.BytesIO(b'\n'.join(json.dumps(r).encode() for r in (initialize,request))+b'\n')
                        self.assertEqual(serve_stdio(Path(tmp)/'review.sock',input_stream=source,output_stream=output),0)
                        result=json.loads(output.getvalue().splitlines()[-1])['result']
                        self.assertFalse(result['isError'])
                        self.assertEqual(json.loads(result['content'][0]['text'])['content'],first['content'])
                actual=packet['jobs'][0]['disagreement']
                self.assertEqual([actual['primary'],actual['check']],expected)
                self.assertEqual(self.save(g,entry)['status'],'saved')
                self.assertEqual(self.save(g,entry)['status'],'saved')
                self.a.revoke(g['grant_id'])

    def test_disagreement_page_basis_change_and_preload_corruption_fail_closed(self):
        self.large_pair(True);g=self.issue()
        for corruption in ('basis_sha256','payload_sha256','ordinal','content'):
            class Client:
                def call(_self,operation,args):
                    value=self.a.scoped_call(g['token'],operation,args)
                    if operation=='disagreement' and args.get('offset',0)>0:
                        value=dict(value)
                        value[corruption]=('x'+value['content'][1:] if corruption=='content' else
                            999 if corruption=='ordinal' else '0'*64)
                    return value
            with self.subTest(corruption=corruption), self.assertRaises(ValueError):
                preload_evidence(Client())
        first=self.a.scoped_call(g['token'],'disagreement',{'ordinal':1})
        with self.h.ledger() as con,con:
            value=json.loads(con.execute('SELECT check_json FROM job_review_items WHERE review_id=?',(self.rid,)).fetchone()[0])
            value['explanation']='Changed current judgment.'
            con.execute('UPDATE job_review_items SET check_json=? WHERE review_id=?',(canonical_json(value),self.rid))
        with self.assertRaises(ReviewConflictError):
            self.a.scoped_call(g['token'],'disagreement',{'ordinal':1,'offset':first['next_offset']})

    def test_whole_check_choice_preserves_originals_raw_counts_and_drives_finalization(self):
        g = self.issue(); packet = self.load(g); entry = self.entry(g)
        self.assertEqual(packet['jobs'][0]['disagreement']['primary'], self.primary)
        self.assertEqual(self.save(g, entry)['status'], 'saved')
        self.assertEqual(self.save(g, entry)['status'], 'saved')
        self.assertEqual(self.save(g, dict(entry, choice='primary'))['status'], 'error')
        status = self.a.status(self.rid)
        self.assertEqual((status['disagreement_count'], status['resolved_disagreement_count'], status['unresolved_disagreement_count']), (1,1,0))
        with self.h.ledger() as con:
            originals = con.execute('SELECT assessment_json,check_json FROM job_review_items').fetchone()
            self.assertEqual([json.loads(v) for v in originals], [self.primary, self.check])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_review_revisions').fetchone()[0], 2)
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE job_review_resolutions SET choice='primary'")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('DELETE FROM job_review_resolutions')
        self.a.revoke(g['grant_id'])
        final = self.h.issue(self.rid, ordinals=(), kind='finalizer')
        page = self.a.scoped_call(final['token'], 'calibration', {})
        self.assertEqual(page['items'][0]['assessment'], self.check)
        self.a.scoped_call(final['token'], 'calibrate', {'ordinal':1,'position':1})
        self.a.scoped_call(final['token'], 'finalize', {})
        self.a.verify_availability(self.rid, 'availability')
        preview = self.a.preview(self.rid)
        self.assertEqual(preview['targeted_count'], 0)
        self.assertEqual(preview['broad_count'], 1)
        self.assertEqual(preview['blockers'], [])
        self.a.publish(self.rid, preview['preview_sha256'], 'publish-resolved')

    def test_missing_reads_bad_sources_fields_and_facts_cannot_resolve(self):
        g = self.issue(); entry = self.entry(g)
        self.assertEqual(self.save(g, entry)['status'], 'error')
        self.load(g)
        for changes in ({'checked_dimensions':['decision']}, {'explanation':''},
                        {'evidence':[{'field':'description','quote':'invented','fact_id':'fact-1'}]},
                        {'evidence':[{'field':'description','quote':'Python APIs','fact_id':'invented'}]},
                        {'assessment':self.primary}, {'choice':'blend'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.save(g, dict(entry, **changes))['status'], 'error')
        self.assertEqual(self.a.status(self.rid)['unresolved_disagreement_count'], 1)

    def test_unresolved_and_interruption_are_durable_one_attempt_per_basis(self):
        g = self.issue(); self.load(g)
        self.assertEqual(self.save(g, self.entry(g, 'unresolved'))['status'], 'saved')
        self.a.revoke(g['grant_id'])
        self.assertEqual(self.a.pending_adjudications(self.rid), [])
        status = self.a.status(self.rid)
        self.assertEqual(status['unresolved_disagreement_count'], 1)
        self.assertEqual(status['adjudication']['unresolved'], {'count': 1, 'ordinals': [1]})
        self.assertEqual(status['adjudication']['incomplete']['count'], 0)
        with self.assertRaises(ReviewConflictError): self.issue()
        with self.assertRaises(ContractError): self.h.issue(self.rid, ordinals=(), kind='finalizer')
        self.assertIn('reviewer_disagreements', self.a.preview(self.rid)['blockers'])

    def test_stale_check_and_source_invalidate_resolution_and_grant(self):
        g = self.issue(); self.load(g); entry = self.entry(g); self.save(g, entry)
        with self.h.ledger() as con, con:
            changed = dict(self.check, explanation='New checked reasoning.')
            con.execute('UPDATE job_review_items SET check_json=?', (json.dumps(changed),))
        self.assertEqual(self.a.status(self.rid)['unresolved_disagreement_count'], 1)
        with self.assertRaises(ReviewConflictError): self.save(g, entry)
        with self.assertRaises(ReviewConflictError): self.a.scoped_call(g['token'], 'assignment', {})
        with self.h.ledger() as con, con:
            con.execute('UPDATE job_review_items SET check_json=?,snapshot_sha256=?', (json.dumps(self.check, separators=(',',':'), sort_keys=True), 'a'*64))
        self.assertEqual(self.a.status(self.rid)['unresolved_disagreement_count'], 1)
        with self.assertRaises(ReviewConflictError): self.a.renew(g['grant_id'])

    def test_authority_is_scoped_and_reviewer_cannot_read_judgments(self):
        g = self.issue()
        for op,args in [('assessment',{'ordinal':1,'assessment':self.primary}), ('calibration',{}),
                        ('disagreement',{'ordinal':2}), ('resolutions',{'resolutions':[{'ordinal':2}]})]:
            with self.subTest(operation=op), self.assertRaises(ReviewAuthorizationError):
                self.a.scoped_call(g['token'],op,args)
        old = self.check_grant
        with self.assertRaises(ReviewAuthorizationError):
            self.a.scoped_call(old['token'],'disagreement',{'ordinal':1})
        self.a.revoke(g['grant_id'])
        with self.assertRaises(ReviewAuthorizationError): self.load(g)

    def test_private_api_preload_and_model_tool_boundary(self):
        g = self.issue()
        with tempfile.TemporaryDirectory() as tmp:
            with assignment_proxy(Path(tmp)/'review.sock', self.a, g['token']):
                client = ReviewerClient(Path(tmp)/'review.sock')
                packet = preload_evidence(client)
                mcp = ReviewerMCP(client); mcp.initialized = True
                request = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':
                    {'name':'review_resolutions','arguments':{'resolutions':[self.entry(g)]}}}
                result = mcp.handle(request)
                self.assertFalse(result['result']['isError'])
                saved = json.loads(result['result']['content'][0]['text'])
                self.assertEqual(saved['results'][0]['status'],'saved')
                # Whitespace remains valid JSON and exercises the actual >64 KiB
                # stdio envelope without inflating any individual evidence field.
                initialize = {'jsonrpc':'2.0','id':0,'method':'initialize','params':{
                    'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{}}}
                frame = json.dumps(request).encode() + b' ' * 70000 + b'\n'
                output = io.BytesIO()
                self.assertEqual(serve_stdio(Path(tmp)/'review.sock', input_stream=io.BytesIO(
                    json.dumps(initialize).encode()+b'\n'+frame), output_stream=output), 0)
                replay = json.loads(output.getvalue().splitlines()[-1])['result']
                self.assertFalse(replay['isError'])
                self.assertEqual(json.loads(replay['content'][0]['text'])['results'][0]['status'], 'saved')
        self.assertEqual(packet['assignment']['kind'], 'adjudicator')
        self.assertIn('not a blind checker',review_prompt(packet))
        self.assertNotIn('previous_lists',json.dumps(packet))
        self.assertNotIn('previous_assessments',json.dumps(packet))
        names = {t['name'] for t in tools_for('adjudicator','job-review-v2')}
        self.assertEqual(names, {'review_assignment','review_context','review_job','review_disagreement','review_resolutions'})
        validate_response(response('mcp__review__review_resolutions'), kind='adjudicator', rubric_version='job-review-v2')
        for kind, tool in [('check','review_disagreement'),('adjudicator','review_assessment')]:
            with self.assertRaises(GatewayRejected):
                validate_response(response('mcp__review__'+tool),kind=kind,rubric_version='job-review-v2')

    def test_context_change_and_missing_receipt_fail_closed(self):
        g = self.issue(); self.load(g); self.save(g, self.entry(g))
        with self.h.ledger() as con, con:
            con.execute("DELETE FROM job_review_commands WHERE operation='resolve'")
        with self.assertRaisesRegex(ContractError, 'receipt'):
            self.a.preview(self.rid)
        with self.h.ledger() as con, con:
            con.execute("UPDATE job_reviews SET context_sha256=?", ('c'*64,))
        self.assertEqual(self.a.status(self.rid)['unresolved_disagreement_count'],1)
        with self.assertRaises(ReviewConflictError): self.load(g)

    def test_interrupted_unsaved_attempt_is_not_redispatched(self):
        self.assertEqual(self.a.status(self.rid)['adjudication']['pending']['count'], 1)
        g=self.issue()
        self.assertEqual(self.a.status(self.rid)['adjudication']['active']['count'], 1)
        self.a.reconcile_interrupted(self.rid)
        self.assertEqual(self.a.pending_adjudications(self.rid),[])
        with self.assertRaises(ReviewConflictError): self.issue()
        self.assertEqual(self.a.status(self.rid)['unresolved_disagreement_count'],1)
        state = self.a.status(self.rid)['adjudication']
        self.assertEqual(state['incomplete'], {'count': 1, 'ordinals': [1]})
        self.assertEqual(state['unresolved']['count'], 0)
        with self.assertRaises(ReviewAuthorizationError): self.load(g)

    def test_bulk_scope_and_basis_failure_never_partially_commit(self):
        g=self.issue();self.load(g);entry=self.entry(g)
        for bad in (dict(entry,ordinal=2),dict(entry,basis_sha256='0'*64)):
            entries=[entry,bad] if bad['ordinal']==2 else [bad]
            with self.assertRaises((ReviewAuthorizationError,ReviewConflictError)):
                self.a.scoped_call(g['token'],'resolutions',{'resolutions':entries})
            self.assertEqual(self.a.status(self.rid)['resolved_disagreement_count'],0)

    def test_expiry_launch_and_independent_runtime_requirements(self):
        with self.assertRaises(ReviewConflictError):
            self.a.issue(self.rid,[1],'adjudicator',{'runtime_id':'runtime-2',
                'model':'gpt-6-astra','reasoning_effort':'high'})
        g=self.h.issue(self.rid,kind='adjudicator',launched=False)
        with self.assertRaises(ReviewAuthorizationError): self.load(g)
        self.a.mark_launch(g['grant_id'],'runtime-3',self.h.launch_receipt('runtime-3'))
        with self.h.ledger() as con, con:
            con.execute("UPDATE job_review_adjudicator_grants SET expires_at='2000-01-01T00:00:00Z'")
        with self.assertRaises(ReviewAuthorizationError): self.load(g)

    def test_selected_check_can_change_membership_without_rewriting_primary(self):
        # New fixture, since current-basis originals intentionally cannot be edited
        # through the adjudicator. Exercise exclusion -> selected effective member.
        h=authority_fixtures.AuthorityTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        rid=h.start(rubric_version='job-review-v2')
        excluded=self.value('exclude')
        for kind,value in [('primary',excluded),('check',self.check)]:
            grant=h.issue(rid,kind=kind);h.assess(grant,value=value);h.authority.revoke(grant['grant_id'])
        grant=h.issue(rid,kind='adjudicator')
        class Client:
            def call(_self,op,args):return h.authority.scoped_call(grant['token'],op,args)
        packet=preload_evidence(Client());d=packet['jobs'][0]['disagreement']
        entry={'ordinal':1,'basis_sha256':d['basis_sha256'],'choice':'check',
            'checked_dimensions':d['differing_dimensions'],'explanation':'Python API overlap supports broad exploration.',
            'evidence':[{'field':'description','quote':'Python APIs','fact_id':'fact-1'}]}
        saved=Client().call('resolutions',{'resolutions':[entry]})
        self.assertEqual(saved['results'][0]['status'],'saved')
        status=h.authority.status(rid)
        self.assertEqual(status['counts'],{'exclude':1})
        self.assertEqual(status['effective_counts'],{'broad_only':1})
        self.assertEqual(status['calibration']['selected_count'],1)
        final=h.issue(rid,ordinals=(),kind='finalizer')
        page=h.authority.scoped_call(final['token'],'calibration',{})
        self.assertEqual(page['items'][0]['assessment'],self.check)

    def test_effective_removal_cannot_leave_a_duplicate_without_retained_target(self):
        h=authority_fixtures.AuthorityTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.fixture.insert('b')
        rid=h.start(rubric_version='job-review-v2')
        duplicate=self.value('exclude',reason_code='duplicate',duplicate_of=1)
        for kind in ('primary','check'):
            grant=h.issue(rid,ordinals=(1,2),kind=kind)
            first=self.primary if kind=='primary' else self.value('exclude')
            h.assess(grant,ordinal=1,value=first);h.assess(grant,ordinal=2,value=duplicate)
            h.authority.revoke(grant['grant_id'])
        grant=h.issue(rid,kind='adjudicator')
        class Client:
            def call(_self,op,args):return h.authority.scoped_call(grant['token'],op,args)
        packet=preload_evidence(Client());dispute=packet['jobs'][0]['disagreement']
        entry={'ordinal':1,'basis_sha256':dispute['basis_sha256'],'choice':'check',
            'checked_dimensions':dispute['differing_dimensions'],'explanation':'Specialty gap prevents recommendation.',
            'evidence':[{'field':'description','quote':'Python APIs'}]}
        self.assertEqual(Client().call('resolutions',{'resolutions':[entry]})['results'][0]['status'],'saved')
        preview=h.authority.preview(rid)
        self.assertIn('duplicate_without_retained_recommendation',preview['blockers'])
        self.assertEqual(preview['broad_count'],0)

    def test_adjudicator_capability_fails_closed_for_old_image(self):
        from job_search.job_reviews.runner import ProductionRuntime
        from job_search.job_reviews.runner_config import RunnerConfig
        config = RunnerConfig(application_config=self.f.root/'app.json', state_dir=self.f.root/'state',
            runtime_dir=self.f.root/'runtime', auth_home=self.f.root/'auth', model_image='sha256:'+'1'*64)
        runtime = ProductionRuntime(config,self.a)
        self.assertTrue(runtime._runtime_config('job-review-v2').adjudication_enabled)
        self.assertFalse(runtime._runtime_config('job-review-v1').adjudication_enabled)
        config=RuntimeConfig('sha256:'+'1'*64,adjudication_enabled=True)
        labels={'org.career-platform.review.codex-version':'0.160.0'}
        mock=Mock(returncode=0,stdout=json.dumps([{'Os':'linux','Architecture':'amd64','Config':{'Labels':labels}}]))
        with patch('job_search.job_reviews.codex_runtime._docker',return_value=mock):
            self.assertEqual(readiness(config)['reason'],'runtime_adjudication_contract_mismatch')
            labels['org.career-platform.review.adjudication-version']='1'
            mock.stdout=json.dumps([{'Os':'linux','Architecture':'amd64','Config':{'Labels':labels}}])
            self.assertTrue(readiness(config)['ready'])


if __name__ == '__main__': unittest.main()
