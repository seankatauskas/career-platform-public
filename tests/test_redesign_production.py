"""Actual host and worker composition against converted fictional state."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from job_search.application_migration import convert_snapshot,CANDIDATE_NAME
from job_search.application_installation import freeze_legacy,LEGACY_TASKS
from job_search.application_runtime import ApplicationRuntime
from job_search.runtime import RuntimeConfigV1,build_runtime
from job_search.system import build_dashboard_controller,build_hermes_sources_from_config
from job_search.application_agent_host import ProductionAgentTools
from job_search.commands import CommandContext,Principal
from tests.test_job_search_ledger import make_service,start

class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        path,self.legacy=make_service(self.tmp.name)
        self.old=start(self.legacy)['application']['application_id']
        report=convert_snapshot(path,self.root/'owners')
        self.runtime=ApplicationRuntime(self.root/'owners'/CANDIDATE_NAME)
        freeze_legacy(path,self.runtime,operator='fixture',report=report)
        self.config=replace(RuntimeConfigV1.defaults(self.root),application_backend='owners',
            application_db=path,application_owner_db=self.runtime.executor.path)
        self.environment=patch.dict('os.environ',{},clear=True);self.environment.start();self.addCleanup(self.environment.stop)

    def test_real_dashboard_and_mcp_use_same_owner_state(self):
        controller=build_dashboard_controller(self.config)
        created=controller.application_gateway.command('save_job',{'job_source':{'source':'test','source_id':'new'}},'new','human')
        sources=build_hermes_sources_from_config(self.config)
        tools=ProductionAgentTools(self.runtime,sources)
        ids={item['id'] for item in tools.invoke('list_applications',{})['items']}
        self.assertEqual(ids,{self.old,created['id']})
        self.assertEqual(len(self.legacy.list_applications()),1)
        health=tools.invoke('system_health',{})
        self.assertEqual(health['application_backend'],'owners')
        self.assertTrue(health['application_delivery']['paused'])
        self.assertIn('search_jobs',tools.tool_names)
        self.assertNotIn('approve_action',tools.tool_names)
        self.assertEqual({d['name'] for d in tools.tool_definitions()},set(tools.tool_names))

    def test_real_workers_register_owner_handlers_and_retire_legacy_writers(self):
        now=lambda:datetime(2026,10,9,12,tzinfo=timezone.utc)
        core=build_runtime(self.config,lane='core',base_environment={},now_provider=now)
        model=build_runtime(self.config,lane='model',base_environment={},now_provider=now)
        self.assertFalse(set(core.worker.task_handlers)&LEGACY_TASKS)
        self.assertFalse(set(model.worker.task_handlers)&LEGACY_TASKS)
        self.assertIn('ats.authoritative',core.worker.task_handlers)
        self.assertIn('applications.mail.understand',model.worker.task_handlers)
        context=SimpleNamespace(heartbeat=lambda:True,work_id='fixture',attempt=1)
        result=core.worker.task_handlers['application.dispatch']({},context)
        self.assertEqual(result.result['status'],'paused')
        core.worker.task_handlers['applications.tick']({},context)
        from job_search.db import connect
        with connect(self.config.application_db) as con:
            enabled={r[0] for r in con.execute('SELECT task_kind FROM schedule_specs WHERE enabled=1')}
        self.assertFalse(enabled&LEGACY_TASKS)

    def test_stale_legacy_config_cannot_restart_workers(self):
        from job_search.commands import DomainError
        with self.assertRaises(DomainError):build_runtime(replace(self.config,application_backend='legacy'),base_environment={})

    def test_retired_mail_flags_have_no_owner_authority(self):
        config=replace(self.config,mail_recruiting_only=True,remote_mail_inference_enabled=True,
            remote_mail_temporal_enabled=False,mail_understanding_mode="shared",mail_understanding_source_scope=None)
        config.validate()
        runtime=build_runtime(config,base_environment={})
        self.assertIn("applications.mail.sync",runtime.worker.task_handlers)

    def test_owner_profile_and_health_ignore_old_classifier_semantics(self):
        from job_search.application_production import configured_understanding_profile
        from job_search.dependency_health import dependency_health
        config=replace(self.config,remote_mail_inference_enabled=True,remote_mail_temporal_enabled=False,
            mail_classifier_config=self.root/'unused-legacy.json',mail_inference_config=self.root/'owner-profile.json')
        generation=SimpleNamespace(model='numind/NuExtract3',default_max_output_tokens=8192,credential_file=self.root/'fictional-key')
        profile=SimpleNamespace(structured_generation=generation)
        with patch('job_search.inference.load_inference_config',return_value=profile) as load, \
             patch('job_search.inference.config.load_credential',return_value='fictional'), \
             patch('job_search.inference.configured_inference_path',return_value=None), \
             patch('job_search.runtime._configured_remote_mail_profile',side_effect=AssertionError('legacy policy called')), \
             patch.dict('os.environ',{'JOB_SEARCH_MAIL_CLASSIFIER_CONFIG':'unused'}):
            self.assertIs(configured_understanding_profile(config,{}),profile)
            self.assertTrue(dependency_health(config)['inference']['remote_mail']['active'])
            self.assertEqual(load.call_args.args[0],config.mail_inference_config)
        with patch('job_search.inference.load_inference_config',return_value=profile):
            with self.assertRaises(ValueError):configured_understanding_profile(replace(config,remote_mail_inference_enabled=False),{})
            generation.default_max_output_tokens=4096
            with self.assertRaises(ValueError):configured_understanding_profile(config,{})

if __name__=='__main__':unittest.main()
