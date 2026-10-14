"""Synthetic regression cases for projection guardrails, not a model accuracy score."""
import tempfile
import unittest
from pathlib import Path
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter
from job_search.application_mail import capture_candidates
from job_search.applications.understanding import AnalysisInput, SourceText
from job_search.commands import CommandContext, Principal


class ReviewSemanticsTest(unittest.TestCase):
    def test_twenty_five_email_shapes_keep_the_right_review_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=ApplicationRuntime(Path(directory)/'owners.db')
            human=HumanApplicationAdapter(runtime,'owner')
            app=human.command('save_job',{'job_source':{'source':'fixture','source_id':'semantic-role','employer':'Example Company','title':'Engineer'}},'app')
            cases=[]
            for quote,kind in [('We will review your application.','recruiter_contact'),('We will be in touch if selected.','recruiter_contact'),('The Recruiting Team at Example','recruiter_contact'),('Newsletter: new engineering roles','marketing'),('Your application is under review.','review')]:
                cases.append(('facts',{'kind':'contact','value':{'kind':kind}},quote,set()))
            for quote in ['We will not proceed.','We selected another candidate.','Your application was unsuccessful.','We have decided not to move forward.']:
                cases.append(('facts',{'kind':'outcome','value':{'outcome':'rejected'}},quote,{'close_application'}))
            cases.append(('facts',{'kind':'submission','value':{'status':'pending_verification'}},'Your verification code is 123456.',set()))
            def request(kind='other',party='applicant'):
                return {'kind':kind,'outcome':'Complete the next step','requirement':'required','responsible_party':party,'channel':'portal'}
            cases.extend([
                ('requests',request(),'Thank you for completing your application.',set()),
                ('requests',request(),'Please complete the remaining application questions.',{'record_request'}),
                ('requests',request('assessment'),'Complete the attached design exercise.',{'record_request'}),
                ('requests',request('follow_up','employer'),'We will reply within one week.',set()),
            ])
            for quote,status in [('We received your application.','confirmed'),('Your application was received.','received'),('Thanks for applying.','attempted'),('Thank you for applying.','submitted'),('Application received','attempted'),('Your application has been received.','attempted'),('We have received your application.','submitted'),('Receipt for your application.','confirmed')]:
                cases.append(('facts',{'kind':'submission','value':{'status':status}},quote,{'record_submission_receipt'}))
            cases.extend([
                ('facts',{'kind':'submission','value':{'status':'confirmed','submission_id':'missing-attempt'}},'We received your application.',{'record_submission_receipt'}),
                ('facts',{'kind':'submission','value':{'status':'confirmed'},'target_id':None},'Your application to Another Employer was received.',{'record_submission_receipt'}),
                ('facts',{'kind':'contact','value':{'kind':'recruiter_contact'}},'I am the recruiter for this role and would like to discuss your experience.',{'record_progress'}),
            ])
            self.assertEqual(len(cases),25)
            for i,(category,finding,text,expected) in enumerate(cases):
                with self.subTest(case=i,text=text):
                    context=CommandContext(Principal('mail','worker',{'record_message'}),str(i),'inferred')
                    message=runtime.command(context,'record_message',{'account_id':'account','provider_message_id':str(i),'source_version':1,'direction':'incoming','authored_text':text,'archive_ref':'fixture:'+str(i)})
                    ref={'source_id':message['id'],'revision':message['revision'],'start':0,'end':len(text),'quote':text}
                    target=finding.get('target_id',app['id'])
                    output={'relevance':'career_related','associations':[],'facts':[],'requests':[],'temporal_facts':[],'uncertainties':[]}
                    if target:
                        output['associations']=[{'kind':'application','target_id':target,'evidence':[ref],'confidence':.9}]
                    output[category]=[{**finding,'target_id':target,'evidence':[ref],'confidence':.9}]
                    with runtime.executor.read() as con:
                        closure=runtime.applications.preview_closure(con,app['id'])
                    analysis_input=AnalysisInput((SourceText(message['id'],message['revision'],message['source_sha256'],text),),
                        candidates=({'id':app['id']},),versions={'application:'+app['id']:1,'message:'+message['id']:message['version']},
                        context={'mail_projection':{'message_id':message['id'],'revision':message['revision'],'target_application_id':None,'association_required':True},
                                 'records':{app['id']:{'submissions':[]}},'closure_previews':{app['id']:closure}})
                    analysis=runtime.executor.run(CommandContext(Principal('analysis','worker',{'record_analysis'}),str(i),'inferred'),'record_analysis',{},
                        lambda tx:runtime.understanding.store_analysis(tx,context=analysis_input,model_version='fixture',output=output))
                    projected=runtime.project_message_analysis(analysis['id'])['proposals']
                    self.assertEqual({p['operation'] for p in projected if p['operation']!='link_message'},expected)
                    self.assertTrue(all(p['status']=='pending' for p in projected))
                    if not expected:self.assertEqual(projected,[])
                    if i==22:
                        self.assertEqual(projected[-1]['blockers'], [])
                        self.assertNotIn('submission_id', projected[-1]['input'])
                    if i==23:self.assertIn('unresolved_association',projected[-1]['blockers'])
            workspace=runtime.queries.workspace(app['id'])
            self.assertTrue(all(not workspace['records'][kind]['items'] for kind in ['submissions','receipts','tasks','progress']))
            self.assertEqual(workspace['application']['disposition'],'open')

    def test_candidate_retrieval_matches_compound_employer_but_not_shared_title(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=ApplicationRuntime(Path(directory)/'owners.db')
            human=HumanApplicationAdapter(runtime,'owner')
            intended=human.command('save_job',{'job_source':{'source':'fixture','source_id':'jobone','employer':'Chai Discovery','title':'Engineer'}},'one')
            human.command('save_job',{'job_source':{'source':'fixture','source_id':'jobtwo','employer':'Another Employer','title':'Engineer'}},'two')
            self.assertEqual(capture_candidates(runtime,'unlinked','Receipt from chaidiscovery.com for Engineer')['candidate_ids'],[intended['id']])
            self.assertEqual(capture_candidates(runtime,'unlinked','Receipt for Engineer')['candidate_ids'],[])


if __name__=='__main__':unittest.main()
