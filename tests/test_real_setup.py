"""Real-use regression boundaries, using fictional data and no live services."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from job_search.runtime import RuntimeConfigV1, load_runtime_config
from job_search.setup import initialize, inspect
from job_search.activation import controls, set_control, mail_start
from job_search.db import connect
from job_search.scheduler import seed_default_schedules
from job_search.worker import Worker
from tests.test_job_search_automation import enqueue_work
from tests.test_resume_lab_gateway import make_gateway, start_application, _pdf, FakeExtractor, JOB
from job_search.sync import OutlookMailCoordinator

ROOT=Path(__file__).resolve().parents[1]

class RealSetupTests(unittest.TestCase):
    def test_private_setup_is_paused_repeatable_and_does_not_touch_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'private'; config=root/'config.json'
            self.assertEqual(initialize(config,root,ROOT)['status'],'initialized')
            before=config.read_bytes(); c=load_runtime_config(config,required=True)
            self.assertEqual(c.resume_mode,'standard'); self.assertEqual(c.shortlist_policy,'selective')
            self.assertTrue(c.outlook_new_messages_only)
            self.assertEqual(len(controls(c.application_db)),10)
            self.assertFalse(any(r['enabled'] for r in controls(c.application_db)))
            self.assertEqual(initialize(config,root,ROOT)['status'],'already_initialized')
            self.assertEqual(config.read_bytes(),before)
            self.assertEqual(config.stat().st_mode & 0o777,0o600)
            self.assertFalse(inspect(c)['career']['approved_profile'])

    def test_activation_preserves_pause_and_fences_existing_queued_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'private'; initialize(root/'config.json',root,ROOT)
            c=load_runtime_config(root/'config.json',required=True)
            c=replace(c,scraper_contact='operator@unit.example.test',outlook_client_id='11111111-1111-1111-1111-111111111111')
            now=datetime.now(timezone.utc)
            seed_default_schedules(c.application_db,now,c.environment({}))
            with connect(c.application_db) as con:
                self.assertFalse(con.execute("SELECT enabled FROM schedule_specs WHERE task_kind='ats.authoritative'").fetchone()[0])
            enqueue_work(c.application_db,'paused-ranking','opportunity.preference_refresh')
            w=Worker(c.application_db,lane='core')
            self.assertIsNone(w._claim_work(now))
            r=set_control(c,'ranking',True,expected_revision=0,command_id='rank-on')
            self.assertEqual(r,set_control(c,'ranking',True,expected_revision=0,command_id='rank-on'))
            self.assertIsNotNone(w._claim_work(now))
            with self.assertRaises(ValueError):set_control(c,'ranking',False,expected_revision=0,command_id='stale')
            set_control(c,'mail',True,expected_revision=0,command_id='mail-on')
            first=mail_start(c.application_db,c.outlook_account_id)
            set_control(c,'mail',False,expected_revision=1,command_id='mail-off')
            set_control(c,'mail',True,expected_revision=2,command_id='mail-on-again')
            self.assertEqual(first,mail_start(c.application_db,c.outlook_account_id))

    def test_standard_pdf_is_selected_unchanged_without_compiler_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);gateway=make_gateway(root);_,app=start_application(root)
            gateway.model=Mock();gateway.toolchain=None
            content='Taylor Example\nSoftware Engineer\nPython and PostgreSQL production experience.'
            pdf=_pdf(content)
            imported=gateway.import_existing_standard('Standard',1,pdf=pdf,tex_source='user authored source',
                intended_text=content,extractor=FakeExtractor())
            repeat=gateway.import_existing_standard('Standard',1,pdf=pdf,tex_source='user authored source',
                intended_text=content,extractor=FakeExtractor())
            self.assertEqual(imported['standard_version_id'],repeat['standard_version_id'])
            result=gateway.use_standard(JOB,application_id=app,idempotency_key='standard-selection')
            selected=result['selection'];artifact=gateway.get_artifact(selected['artifact_id'])
            self.assertEqual(artifact.content,pdf)
            self.assertEqual(artifact.sha256,hashlib.sha256(pdf).hexdigest())
            self.assertFalse(gateway.model.mock_calls)
            gateway.use_standard(JOB,application_id=app,idempotency_key='standard-selection')
            with self.assertRaises(ValueError):
                gateway.import_existing_standard('Bad',2,pdf=pdf,tex_source='source',intended_text='Completely unrelated text',extractor=FakeExtractor())

    def test_old_and_unrelated_mail_never_fetches_bodies_or_invokes_models(self):
        state=Mock(); mail=Mock(); classifier=Mock(); ingestor=Mock()
        base={'account_id':'personal','folder_ref':'inbox','immutable_message_id':'old','conversation_id':'thread-old','sender':'recruiter@example.test','subject':'Interview','received_at':'2026-09-01T00:00:00Z'}
        state.pending_messages.return_value=[base,{**base,'immutable_message_id':'unrelated','sender':'orders@example.test','subject':'Grocery receipt','received_at':'2026-09-28T00:00:00Z'}]
        service=Mock(); service.list_mail_candidates.return_value=[]
        coordinator=OutlookMailCoordinator(mail,state,service,classifier=classifier,secure_ingestor=ingestor,
            received_since='2026-09-27T00:00:00Z',recruiting_only=True)
        result=coordinator.process_pending()
        self.assertEqual(result.ignored,2)
        mail.read_message_body.assert_not_called();classifier.classify.assert_not_called();ingestor.ingest.assert_not_called()

    def test_launchd_retains_virtual_environment_entry_point(self):
        import plistlib
        from job_search.launchd import render_launch_agents
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); initialize(root/'state/config.json',root/'state',ROOT)
            c=load_runtime_config(root/'state/config.json',required=True)
            target=root/'python-base';target.write_text('fixture')
            entry=root/'venv/bin/python';entry.parent.mkdir(parents=True);entry.symlink_to(target)
            values=render_launch_agents(c,config_path=root/'state/config.json',python_executable=entry)
            self.assertTrue(all(plistlib.loads(v)['ProgramArguments'][0]==str(entry) for v in values.values()))

    def test_old_metadata_does_not_starve_new_mail_batches(self):
        from job_search.outlook.state import SQLiteOutlookState
        from job_search.outlook.mail import MailChange
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();initialize(root/'config.json',root,ROOT)
            state=SQLiteOutlookState(root/'applications.db')
            changes=[]
            for i in range(10):
                changes.append(MailChange(immutable_id=str(i),removed=False,received_at='2026-09-01T00:00:00Z'))
            changes.append(MailChange(immutable_id='new',removed=False,received_at='2026-09-28T00:00:00Z'))
            state.stage_changes('personal','inbox',changes)
            pending=state.pending_messages(1,received_since='2026-09-27T00:00:00Z')
            self.assertEqual([row['immutable_message_id'] for row in pending],['new'])

    def test_mail_activation_is_read_on_each_task_and_metadata_has_no_body(self):
        from job_search.worker import OutlookMailTaskHandler
        from job_search.outlook.mail import MAIL_SELECT
        from job_search.sync import SyncResult
        self.assertNotIn('bodyPreview',MAIL_SELECT)
        coordinator=Mock(); cutoff=Mock(return_value=None)
        handler=OutlookMailTaskHandler(coordinator,account_id='personal',activation_start=cutoff)
        with self.assertRaises(ValueError):handler({},Mock(attempt=1))
        coordinator.sync_folder.assert_not_called()
        cutoff.return_value='2026-09-27T00:00:00Z'
        coordinator.sync_folder.return_value=SyncResult(0,1,True,False)
        coordinator.process_pending.return_value=SyncResult(0,1,True,False)
        handler({},Mock(attempt=1))
        self.assertEqual(coordinator.received_since,cutoff.return_value)
        coordinator.sync_folder.assert_called_once()

    def test_full_state_transfer_preserves_history_and_pauses_without_credentials(self):
        from job_search.state_transfer import export_state, import_state
        from job_search.ranking import model, proxy
        from job_search.resume_lab.career_store import CareerStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); source=root/'source'
            initialize(source/'config.json',source,ROOT)
            c=load_runtime_config(source/'config.json',required=True)
            with sqlite3.connect(c.jobs_db) as con: con.execute('CREATE TABLE jobs(last_seen TEXT)')
            model.prepare_state(c.preference_db);proxy.prepare_schema(c.proxy_db)
            model_dir=source/'models'/'run_fixture';model_dir.mkdir(parents=True)
            raw=b'fixture model bytes';(model_dir/'model.pkl').write_bytes(raw)
            manifest={'model_revision':'fixture@1','artifacts':{'model.pkl':hashlib.sha256(raw).hexdigest()}}
            (model_dir/'manifest.json').write_text(json.dumps(manifest))
            with sqlite3.connect(c.preference_db) as con:
                con.execute('INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)',('run_fixture','2026-09-27','fixture@1','1',2,json.dumps(manifest),str(model_dir)))
            ledger,app=start_application(source)
            # A registered resume and its selected PDF must survive with the same IDs.
            gateway=make_gateway(source)
            c=replace(c,resume_lab_db=gateway.service.store.db_path,resume_artifact_root=gateway.artifacts._root)
            profile=CareerStore(c.resume_lab_db).save_draft({'identity':{'name':'Taylor Example'}})
            text='Taylor Example Python PostgreSQL'
            imported=gateway.import_existing_standard('Standard',1,pdf=_pdf(text),tex_source='source',intended_text=text,extractor=FakeExtractor())
            selected=gateway.use_standard(JOB,application_id=app,idempotency_key='transfer-selection')['selection']
            with connect(c.application_db) as con:
                con.execute("INSERT INTO outlook_activation VALUES ('personal','2026-09-27T00:00:00Z')")
                before=con.execute('SELECT COUNT(*) FROM application_events').fetchone()[0]
            archive=root/'transfer.tar.gz'
            with self.assertRaises(ValueError):export_state(c,archive)
            receipt=export_state(c,archive,writers_stopped=True)
            import tarfile
            with tarfile.open(archive) as tar:
                self.assertFalse(any('private/' in n or 'config.json' in n or 'oauth' in n for n in tar.getnames()))
            destination=root/'restored'
            result=import_state(archive,receipt['sha256'],destination,portable_key=c.portable_encryption_key_file)
            self.assertEqual(result['status'],'imported_paused')
            with connect(destination/'applications.db') as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM application_events').fetchone()[0],before)
                self.assertEqual(con.execute('SELECT started_at FROM outlook_activation').fetchone()[0],'2026-09-27T00:00:00Z')
            self.assertFalse(any(r['enabled'] for r in controls(destination/'applications.db')))
            from job_search.resume_lab.store import ResumeLabStore
            self.assertEqual(CareerStore(destination/'career.db').get_profile()['draft_revision_id'],profile['revision_id'])
            with sqlite3.connect(destination/'preferences.db') as con:
                model_path=Path(con.execute('SELECT artifact_path FROM preference_model_runs').fetchone()[0])
            self.assertEqual((model_path/'model.pkl').read_bytes(),raw)
            self.assertEqual(result['models']['run_fixture'],'verify_embedding_identity')
            restored=ResumeLabStore(destination/'career.db')
            self.assertEqual(restored.current_application_selection(app)['artifact_id'],selected['artifact_id'])
            self.assertEqual(restored.list_active_standards()[0]['active_version_id'],imported['standard_version_id'])
            with self.assertRaises(ValueError):import_state(archive,receipt['sha256'],destination,portable_key=c.portable_encryption_key_file)

    def test_publication_guard_rejects_personal_artifacts_but_allows_templates(self):
        spec=importlib.util.spec_from_file_location('guard',ROOT/'scripts/check-private-files.py')
        guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
        for name in ['resume.pdf','docs/personal.tex','private/profile.json','state.db-wal','resume-provenance.json','backup.tar.gz']:
            self.assertTrue(guard.private_path(name),name)
        self.assertFalse(guard.private_path('job_search/resume_lab/jake_template.tex'))

if __name__=='__main__':unittest.main()
