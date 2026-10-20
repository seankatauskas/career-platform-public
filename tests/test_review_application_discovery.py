"""External correspondence uses owner identity, versioned review and atomic links."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter, AgentApplicationAdapter
from job_search.application_mail import ApplicationMailReader
from job_search.applications.discovery import ApplicationDiscovery
from job_search.applications.review import ReviewService
from job_search.commands import CommandContext, Principal, DomainError
from job_search.integration import LocalJobCatalog


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)
        self.catalog = LocalJobCatalog(path / 'jobs.db')
        with sqlite3.connect(self.catalog.jobs_db) as con:
            con.execute('CREATE TABLE jobs (ats TEXT,id TEXT,company TEXT,title TEXT,jobUrl TEXT,closed_at TEXT)')
        self.runtime = ApplicationRuntime(path / 'owners.db', job_catalog=self.catalog)
        self.human = HumanApplicationAdapter(self.runtime, 'owner')
        self.agent = AgentApplicationAdapter(self.runtime)
        self.bodies = {}
        class Archive:
            def for_account(archive, account):
                return archive
            def read_message(archive, ref):
                return self.bodies[ref]
        self.runtime.mail_reader = ApplicationMailReader(self.runtime, Archive(), ['personal'])
        self.discovery = ApplicationDiscovery(self.runtime)

    def message(self, key='one', text=None, direction='incoming', account='personal', propose=True):
        text = text or 'Thank you for applying for the Platform Engineer position at Northwind.'
        self.bodies[key] = text
        source = self.runtime.command(CommandContext(Principal('mail', 'worker', {'record_message'}), key, 'inferred'),
            'record_message', {'account_id':account, 'provider_message_id':key, 'source_version':1,
                'direction':direction, 'authored_text':text, 'archive_ref':key, 'thread_id':key})
        if not propose:
            return source, None
        ref = {'owner':'correspondence', 'source_id':source['id'], 'revision':source['revision'], 'sha256':source['source_sha256']}
        proposal = self.agent.call('propose_changes', {'operation':'record_submission_receipt',
            'application_id':None, 'input':{'application_id':None,'evidence':[ref]}, 'evidence':[ref],
            'blockers':['needs_target','unresolved_association']}, idempotency_key='proposal-' + key)
        return source, proposal

    def group(self, source):
        return ReviewService(self.runtime).page(group_id='email:' + source['id'])['items'][0]

    def preview(self, source, **fields):
        group = self.group(source)
        result = self.discovery.preview(group_id=group['id'], expected_digest=group['preview_digest'], **fields)
        command = {'group_id':group['id'],'expected_digest':group['preview_digest'],
                   'expected_match_digest':result.get('match_digest'), 'employer':result['employer'], 'title':result['title']}
        return result, command

    def create(self, value, key='create'):
        return self.human.command('create_application_from_review', value, key)

    def failed_analysis(self, source):
        return self.runtime.executor.run(CommandContext(Principal('worker', 'worker', {'record_analysis'}), 'failure', 'inferred'),
            'record_analysis', {}, lambda tx:self.runtime.understanding.record_source_failure(tx,
                source_refs=[{'source_id':source['id'], 'revision':source['revision'], 'sha256':source['source_sha256']}],
                failure_code='invalid_output'))

    def test_logicbroker_creation_retries_with_reviewed_identity_and_clears_only_on_success(self):
        source, _ = self.message(text='Thank you for applying for the Software Engineer position at Logicbroker, Inc.', propose=False)
        failed = self.failed_analysis(source)
        _, command = self.preview(source)
        result = self.create(command)
        self.assertEqual(result, self.create(command))
        self.assertEqual(result['pending_proposals'], [])
        with self.runtime.executor.read() as con:
            issue = self.runtime.understanding.processing_for_source(con, source['id'], source['revision'])
            self.assertEqual(issue['status'], 'retry_queued')
            self.assertEqual(issue['analysis_id'], failed['id'])
            retries = [i for i in self.runtime.executor.work_page(con)['items'] if i['kind'] == 'retry_processing']
            self.assertEqual(len(retries), 1)
        self.assertEqual(len(self.runtime.queries.dashboard_review(group='processing')['items']), 1)
        def analyze(context):
            self.assertEqual(context.context['mail_projection']['target_application_id'], result['application_id'])
            self.assertFalse(context.context['mail_projection']['association_required'])
            self.assertIn(result['application_id'], [c['id'] for c in context.candidates])
            evidence = [{'source_id':source['id'], 'revision':source['revision'], 'start':0,
                         'end':len(self.bodies['one']), 'quote':self.bodies['one']}]
            return {'relevance':'career_related', 'associations':[], 'facts':[{'kind':'submission',
                'target_id':result['application_id'], 'value':{'status':'confirmed'}, 'evidence':evidence}],
                'requests':[], 'temporal_facts':[], 'uncertainties':[]}
        processed = self.runtime.analyze_message(source['id'], archive=self.runtime.mail_reader.archive,
            allowed_accounts=['personal'], analyzer=SimpleNamespace(analyze=analyze), model_version='fixture',
            candidate_coverage={'complete':True}, processing_retry=retries[0]['payload'])
        self.assertTrue(processed['proposals'])
        self.assertTrue(all(p['status'] == 'pending' for p in processed['proposals']))
        self.assertEqual(self.runtime.queries.dashboard_review(group='processing')['items'], [])
        with self.runtime.executor.read() as con:
            issue = self.runtime.understanding.processing_for_source(con, source['id'], source['revision'])
            self.assertEqual(issue['status'], 'succeeded')
            self.assertEqual(issue['attempt_count'], 2)
            self.assertEqual(self.runtime.understanding.get_analysis(con, failed['id'])['status'], 'failed')
        self.assertEqual(self.runtime.queries.workspace(result['application_id'])['records']['receipts']['items'], [])

    def test_failed_creation_rolls_back_link_and_queued_recovery_together(self):
        source, original = self.message()
        self.failed_analysis(source)
        _, command = self.preview(source)
        retry = self.runtime.understanding.retry_processing_after_link
        def fail_after_queue(tx, **value):
            retry(tx, **value)
            raise DomainError('version_conflict', 'Later operation failed')
        with patch.object(self.runtime.understanding, 'retry_processing_after_link', side_effect=fail_after_queue):
            with self.assertRaises(DomainError):
                self.create(command)
        with self.runtime.executor.read() as con:
            self.assertIsNone(self.runtime.correspondence.association(con, source['id']))
            self.assertEqual(self.runtime.understanding.processing_for_source(con, source['id'], source['revision'])['status'], 'open')
            self.assertFalse(any(i['kind'] == 'retry_processing' for i in self.runtime.executor.work_page(con)['items']))
            self.assertEqual(self.runtime.understanding.get(con, original['id'])['status'], 'pending')
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])

    def test_create_uses_owner_identity_links_email_and_leaves_receipt_pending(self):
        source, original = self.message()
        before = self.runtime.queries.list_applications()
        preview, command = self.preview(source)
        self.assertTrue(preview['can_create'])
        self.assertEqual(before, self.runtime.queries.list_applications())
        result = self.create(command)
        self.assertEqual(result, self.create(command))
        workspace = self.runtime.queries.workspace(result['application_id'])
        self.assertEqual(workspace['progress']['stage'], 'tracking')
        for kind in ('submissions','receipts','tasks','progress'):
            self.assertEqual(workspace['records'][kind]['items'], [])
        with self.runtime.executor.read() as con:
            app = self.runtime.applications.get_application(con, result['application_id'])
            job = self.runtime.applications.get_job(con, app['job_id'])
            self.assertEqual(job['sources'], [{'source':'discovery','source_id':'mail:' + source['id']}])
            self.assertEqual(self.runtime.correspondence.association(con, source['id'])['application_id'], app['id'])
            self.assertEqual(self.runtime.understanding.get(con, original['id'])['status'], 'superseded')
            pending = self.runtime.understanding.get(con, result['pending_proposals'][0])
            self.assertEqual(pending['status'], 'pending')
            self.assertEqual(pending['blockers'], [])
            self.assertEqual(self.runtime.understanding.get(con, result['association_proposal_id'])['status'], 'applied')
        self.human.command('review_changes', {'decisions':[{'proposal_id':pending['id'],'expected_version':pending['version'],'decision':'accept'}]}, 'receipt')
        self.assertEqual(len(self.runtime.queries.workspace(app['id'])['records']['receipts']['items']), 1)

    def test_existing_or_late_application_prevents_duplicate_creation(self):
        source, _ = self.message()
        _, command = self.preview(source)
        self.human.command('save_job', {'job_source':{'source':'workday','source_id':'j1','employer':'North Wind LLC','title':'Senior Platform Engineer'}}, 'existing')
        with self.assertRaises(DomainError):
            self.create(command)
        result, _ = self.preview(source)
        self.assertFalse(result['can_create'])
        self.assertEqual(len(result['applications']), 1)

    def test_late_and_closed_catalog_roles_prevent_creation(self):
        source, _ = self.message()
        _, command = self.preview(source)
        with sqlite3.connect(self.catalog.jobs_db) as con:
            for index in range(25):
                con.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', ('other-ats', str(index), 'northwind-inc.', 'Platform Engineer', '', '2025-01-01'))
        with self.assertRaises(DomainError):
            self.create(command)
        result, _ = self.preview(source)
        self.assertFalse(result['can_create'])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['jobs']), 20)

    def catalog_command(self, source):
        preview, command = self.preview(source, employer='Northwind', title='Platform Engineer')
        self.assertFalse(preview['can_create'])
        choice = preview['catalog_choices'][0]
        return {**command, 'catalog_id': choice['id'], 'expected_match_digest': choice['match_digest'],
                'employer': choice['job']['employer'], 'title': choice['job']['title'],
                'search_employer': preview['employer'], 'search_title': preview['title']}

    def add_catalog_role(self, identity='role-one'):
        with sqlite3.connect(self.catalog.jobs_db) as con:
            con.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                ('ashby', identity, 'Northwind Inc.', 'Senior Platform Engineer',
                 'https://example.test/jobs/' + identity, '2025-01-01'))

    def test_searched_catalog_role_can_be_selected_without_analysis_candidate(self):
        source, original = self.message()
        self.add_catalog_role()
        self.assertEqual(self.group(source)['catalog_candidates'], [])
        command = self.catalog_command(source)
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])
        result = self.create(command)
        self.assertEqual(result, self.create(command))
        workspace = self.runtime.queries.workspace(result['application_id'])
        self.assertEqual(workspace['progress']['stage'], 'tracking')
        for kind in ('submissions', 'receipts', 'tasks', 'progress'):
            self.assertEqual(workspace['records'][kind]['items'], [])
        with self.runtime.executor.read() as con:
            job = self.runtime.applications.get_job(con, result['job_id'])
            self.assertEqual(job['sources'], [{'source': 'ashby', 'source_id': 'role-one'}])
            self.assertEqual(job['recorded_snapshot']['job_url'], 'https://example.test/jobs/role-one')
            self.assertEqual(self.runtime.correspondence.association(con, source['id'])['application_id'], result['application_id'])
            self.assertEqual(self.runtime.understanding.get(con, original['id'])['status'], 'superseded')
            self.assertTrue(result['pending_proposals'])
            for identifier in result['pending_proposals']:
                self.assertEqual(self.runtime.understanding.get(con, identifier)['status'], 'pending')

    def test_catalog_selection_reuses_late_application_by_exact_posting_identity(self):
        source, _ = self.message()
        self.add_catalog_role()
        command = self.catalog_command(source)
        existing = self.human.command('save_job', {'job_source': {
            'source': 'ashby', 'source_id': 'role-one', 'employer': 'Northwind Inc.',
            'title': 'Senior Platform Engineer'}}, 'existing-catalog-role')
        result = self.create(command)
        self.assertEqual(result['application_id'], existing['id'])
        self.assertEqual(len(self.runtime.queries.list_applications()['items']), 1)

    def test_catalog_selection_binds_search_identity_and_selected_posting(self):
        source, _ = self.message()
        self.add_catalog_role()
        self.add_catalog_role('role-two')
        command = self.catalog_command(source)
        variants = [dict(command, catalog_id='catalog:ashby:role-two'),
                    dict(command, search_employer='Another employer'),
                    dict(command, search_title='A different role'),
                    dict(command, employer='Changed employer'),
                    dict(command, search_title=None)]
        for index, value in enumerate(variants):
            with self.subTest(value=value), self.assertRaises(DomainError):
                self.create(value, 'tampered-' + str(index))
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])

    def test_catalog_selection_rejects_changed_or_removed_posting(self):
        for field, value in [('title', 'Changed role'), ('jobUrl', 'https://example.test/changed'),
                             ('closed_at', None), ('company', 'Different company')]:
            with self.subTest(field=field):
                source, _ = self.message('changed-' + field)
                with sqlite3.connect(self.catalog.jobs_db) as con:
                    con.execute('DELETE FROM jobs')
                self.add_catalog_role()
                command = self.catalog_command(source)
                with sqlite3.connect(self.catalog.jobs_db) as con:
                    con.execute(f'UPDATE jobs SET {field}=?', (value,))
                with self.assertRaises(DomainError):
                    self.create(command, 'stale-' + field)
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])

    def test_catalog_selection_rolls_back_application_when_link_fails(self):
        source, original = self.message()
        self.add_catalog_role()
        command = self.catalog_command(source)
        with patch.object(self.runtime.workflows, 'review_changes', side_effect=DomainError('version_conflict', 'Link failed')):
            with self.assertRaises(DomainError):
                self.create(command)
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])
        with self.runtime.executor.read() as con:
            self.assertIsNone(self.runtime.correspondence.association(con, source['id']))
            self.assertEqual(self.runtime.understanding.get(con, original['id'])['status'], 'pending')

    def test_catalog_preview_retains_location_and_rejects_location_change(self):
        source, _ = self.message()
        with sqlite3.connect(self.catalog.jobs_db) as con:
            con.execute('ALTER TABLE jobs ADD COLUMN location TEXT')
            con.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                ('ashby', 'located-role', 'Northwind', 'Platform Engineer',
                 'https://example.test/job', None, 'Chicago'))
        preview, _ = self.preview(source)
        self.assertEqual(preview['catalog_choices'][0]['job']['location'], 'Chicago')
        command = self.catalog_command(source)
        with sqlite3.connect(self.catalog.jobs_db) as con:
            con.execute('UPDATE jobs SET location=?', ('New York',))
        with self.assertRaises(DomainError):
            self.create(command)

    def test_catalog_choice_flows_through_dashboard_gateway(self):
        from job_search.application_gateway import ApplicationGateway
        from job_search.service import JobSearchLedger
        source, _ = self.message()
        self.add_catalog_role()
        gateway = ApplicationGateway(self.runtime, JobSearchLedger(Path(self.tmp.name) / 'gateway.db'), catalog=self.catalog)
        group = self.group(source)
        matched, result = gateway.http_query('/api/v1/application-owner/discovery-preview', {
            'group_id': [group['id']], 'expected_digest': [group['preview_digest']],
            'employer': ['Northwind'], 'title': ['Platform Engineer']})
        self.assertTrue(matched)
        choice = result['catalog_choices'][0]
        command = {'group_id': group['id'], 'expected_digest': group['preview_digest'],
                   'expected_match_digest': choice['match_digest'], 'catalog_id': choice['id'],
                   'employer': choice['job']['employer'], 'title': choice['job']['title'],
                   'search_employer': result['employer'], 'search_title': result['title']}
        applied = gateway.command('create_application_from_review', command, 'catalog-gateway', 'authenticated-owner')
        self.assertEqual(applied['status'], 'applied')
        self.assertTrue(applied['pending_proposals'])

    def test_review_source_subject_is_verified_without_an_extra_archive_read(self):
        import hashlib
        from job_search.mail.sanitizer import sanitize_mail
        text = sanitize_mail('Northwind application update', 'Thank you for applying.').text
        self.bodies['header'] = text
        source = self.runtime.command(CommandContext(Principal('mail', 'worker', {'record_message'}), 'header', 'inferred'),
            'record_message', {'account_id': 'personal', 'provider_message_id': 'header', 'source_version': 1,
                'direction': 'incoming', 'authored_text': text, 'archive_ref': 'header',
                'metadata': {'subject_hash': hashlib.sha256(b'Northwind application update').hexdigest()}})
        archive = self.runtime.mail_reader.archive
        with patch.object(archive, 'read_message', wraps=archive.read_message) as read:
            result = self.runtime.mail_reader.read_review_source(source['id'], source['revision'], source['source_sha256'])
        self.assertEqual(result['subject'], 'Northwind application update')
        self.assertEqual(read.call_count, 1)
        # Unstructured fixtures or headers without preserved hash never acquire
        # an invented display subject.
        other, _ = self.message('no-header')
        result = self.runtime.mail_reader.read_review_source(other['id'], other['revision'], other['source_sha256'])
        self.assertIsNone(result['subject'])
        self.runtime.mail_reader.allowed_accounts = frozenset()
        with self.assertRaises(DomainError):
            self.runtime.mail_reader.read_review_source(source['id'], source['revision'], source['source_sha256'])

    def test_ambiguous_identity_is_manual_and_unavailable_catalog_stays_blocked(self):
        source, _ = self.message(text='Please contact our recruiting team.')
        result, _ = self.preview(source)
        self.assertFalse(result['can_create'])
        result, command = self.preview(source, employer='Northwind', title='Platform Engineer')
        self.assertTrue(result['can_create'])
        self.runtime.job_catalog = None
        with self.assertRaises(DomainError):
            self.create(command)
        result, _ = self.preview(source, employer='Northwind', title='Platform Engineer')
        self.assertFalse(result['can_create'])

    def test_changed_source_group_or_identity_invalidates_approval(self):
        source, proposal = self.message()
        _, command = self.preview(source)
        with self.assertRaises(DomainError):
            self.create({**command, 'employer':'Different'}, 'edited')
        self.human.command('review_changes', {'decisions':[{'proposal_id':proposal['id'],'expected_version':proposal['version'],'decision':'reject'}]}, 'reject')
        with self.assertRaises(DomainError):
            self.create(command)
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])

    def test_application_and_proposal_replacement_roll_back_on_link_failure(self):
        source, original = self.message()
        _, command = self.preview(source)
        with patch.object(self.runtime.correspondence, 'link_message', side_effect=DomainError('version_conflict', 'changed')):
            with self.assertRaises(DomainError):
                self.create(command)
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])
        with self.runtime.executor.read() as con:
            self.assertIsNone(self.runtime.correspondence.association(con, source['id']))
            self.assertEqual(self.runtime.understanding.get(con, original['id'])['status'], 'pending')
            self.assertEqual(len(self.runtime.understanding.list_pending(con)['items']), 1)
        self.assertEqual(self.create(command)['status'], 'applied')

    def test_repeated_email_does_not_create_a_second_job(self):
        first, _ = self.message()
        second, _ = self.message('two')
        _, first_command = self.preview(first)
        _, second_command = self.preview(second)
        self.create(first_command)
        with self.assertRaises(DomainError):
            self.create(second_command, 'second')
        self.assertEqual(len(self.runtime.queries.list_applications()['items']), 1)

    def test_agent_and_inferred_authority_cannot_create(self):
        source, _ = self.message()
        _, command = self.preview(source)
        with self.assertRaises(DomainError):
            self.agent.call('create_application_from_review', command, idempotency_key='agent')
        for kind, origin in [('worker','direct'),('human','inferred')]:
            with self.assertRaises(DomainError):
                self.runtime.command(CommandContext(Principal('untrusted',kind,{'create_application_from_review'}),kind,origin), 'create_application_from_review',command)

    def test_source_permissions_direction_and_archive_coverage(self):
        for key, direction, account in [('sent','outgoing','personal'),('other','incoming','other')]:
            source, _ = self.message(key, direction=direction, account=account)
            with self.assertRaises(DomainError):
                self.preview(source)
        source, _ = self.message()
        self.bodies.clear()
        result, _ = self.preview(source)
        self.assertFalse(result['can_create'])

    def test_quoted_history_does_not_supply_creation_identity(self):
        source, _ = self.message(text='Thanks for the update.\nOn Monday Recruiter wrote:\nThank you for applying for the Platform Engineer position at Northwind.')
        result, _ = self.preview(source)
        self.assertFalse(result['can_create'])

    def test_changed_message_revision_rejects_old_preview(self):
        source, _ = self.message()
        _, command = self.preview(source)
        self.runtime.command(CommandContext(Principal('mail','worker',{'record_message'}),'revision','inferred'),
            'record_message', {'account_id':'personal','provider_message_id':'one','source_version':2,
                'direction':'incoming','authored_text':'Different correspondence.','archive_ref':'changed'})
        with self.assertRaises(DomainError):
            self.create(command)
        self.assertEqual(self.runtime.queries.list_applications()['items'], [])

    def test_concurrent_sources_create_only_one_matching_application(self):
        from concurrent.futures import ThreadPoolExecutor
        first, _ = self.message()
        second, _ = self.message('two')
        commands = [self.preview(source)[1] for source in (first, second)]
        def create(index):
            try:
                return self.create(commands[index], 'concurrent-' + str(index))['status']
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(create, range(2))), ['applied', 'version_conflict'])
        self.assertEqual(len(self.runtime.queries.list_applications()['items']), 1)

    def test_gateway_preview_and_command_use_same_owner_and_account_grants(self):
        from job_search.application_gateway import ApplicationGateway
        from job_search.service import JobSearchLedger
        gateway = ApplicationGateway(self.runtime, JobSearchLedger(Path(self.tmp.name) / 'operations.db'), catalog=self.catalog)
        source, _ = self.message()
        group = self.group(source)
        matched, result = gateway.http_query('/api/v1/application-owner/discovery-preview', {
            'group_id':[group['id']], 'expected_digest':[group['preview_digest']]})
        self.assertTrue(matched and result['can_create'])
        command = {**self.preview(source)[1]}
        result = gateway.command('create_application_from_review', command, 'gateway-create', 'authenticated-owner')
        self.assertEqual(result['status'], 'applied')


if __name__ == '__main__':
    unittest.main()
