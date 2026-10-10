"""Offline acceptance for input-only OLD METHOD reviews and their publication boundary."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError, payload_sha256
from job_search.db import connect
from job_search.integration import LocalJobCatalog
from job_search.job_reviews.service import JobReviews
from job_search.job_reviews.old_method.store import Store
from job_search.job_reviews.old_method.workspace import Review, write_json
from job_search.job_reviews.old_method.screen import family, screen
from job_search.job_reviews.old_method.runtime import command, read_output
from job_search.job_reviews.codex_runtime import RuntimeConfig
from job_search.job_reviews.runner_config import RunnerConfig
from job_search.job_reviews.old_method.host import dispatch, execution, run_review
from job_search.service import JobSearchLedger


def assessment(decision='close'):
    return {'stage': 'detailed', 'decision': decision, 'family': 'backend', 'alignment': 'core',
            'reason_code': 'fit', 'explanation': 'Python API work matches the approved experience.',
            'evidence': [{'field': 'description', 'quote': 'Python APIs', 'fact_id': 'f1'}],
            'strengths': ['Python'], 'gaps': [], 'unknowns': [], 'borderline': False,
            'eligibility': 'no_known_barrier', 'eligibility_condition': '', 'next_step': 'apply',
            'category': 'core', **({'priority': 1} if decision != 'exclude' else {})}


class OldMethodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root/'jobs.db'
        with sqlite3.connect(self.jobs) as con:
            con.execute('CREATE TABLE jobs(ats TEXT,id TEXT,title TEXT,company TEXT,location TEXT,description TEXT,jobUrl TEXT,posted_at TEXT,source_updated_at TEXT,first_seen TEXT,last_seen TEXT,closed_at TEXT,PRIMARY KEY(ats,id))')
        self.insert('a')
        self.ledger = JobSearchLedger(self.root/'ledger.db')
        self.profile = {'facts': [{'fact_id': 'f1', 'source': 'resume', 'text': 'Built Python APIs'}],
                        'fingerprint': 'frozen-profile', 'resume_versions': ['resume-1']}
        self.service = JobReviews(self.ledger.store.db_path, LocalJobCatalog(self.jobs), lambda: copy.deepcopy(self.profile),
                                  now=lambda: '2026-10-09T12:00:00Z')
        self.store = Store(self.service)
        self.execution = {'model': 'gpt-6-astra', 'reasoning_effort': 'high', 'image': 'sha256:'+'a'*64, 'codex_version': '0.160.0'}

    def insert(self, jid, **changes):
        row = dict(ats='ashby', id=jid, title='Software Engineer', company='example', location='United States',
                   description='Build Python APIs.', jobUrl='https://jobs.ashbyhq.com/example/'+jid,
                   posted_at='2026-10-08T12:00:00Z', source_updated_at='2026-10-09T10:00:00Z',
                   first_seen='2026-10-09T10:00:00Z', last_seen='2026-10-09T10:00:00Z', closed_at=None)
        row.update(changes)
        with sqlite3.connect(self.jobs) as con:
            con.execute('INSERT INTO jobs VALUES ('+','.join('?' for _ in row)+')', tuple(row.values()))

    def start(self, **kwargs):
        return self.store.create('2026-10-08T00:00:00Z', '2026-10-09T12:00:00Z', execution=self.execution, **kwargs)['review_id']

    def workspace(self, rid):
        directory = self.root/rid
        directory.mkdir(exist_ok=True)
        write_json(directory/'input.json', self.store.packet(rid))
        return Review(directory/'input.json', directory/'output')

    def finish(self, rid, choices=None):
        r = self.workspace(rid)
        choices = {1: 'close'} if choices is None else choices
        r.sources(list(choices))
        r.save([{'ordinal': n, 'assessment': assessment(d)} for n, d in choices.items()])
        r.finish([n for n, d in choices.items() if d != 'exclude'])
        self.store.checkpoint(rid, r.state, sealed=True)
        return r

    def observations(self, rid, status='open'):
        return [{'ats': j['ats'], 'job_id': j['id'], 'status': status, 'checked_at': '2026-10-09T12:00:00Z'} for j in self.store.packet(rid)['jobs']]

    def test_screen_exact_exemptions_and_unchanged_ambiguous_cases(self):
        for title in ('Software Engineer', 'Senior Software Developer', 'DevOps Engineer', 'Site Reliability Engineer'):
            self.assertNotEqual(family({'title': title, 'description': ''}), 'Other occupations', title)
        for title in ('Product Engineer', 'Infrastructure Engineer', 'Solutions Engineer', 'Junior Automation Engineer', 'Software Product Manager'):
            self.assertEqual(family({'title': title, 'description': ''}), 'Other occupations', title)
        self.assertEqual(family({'title': 'Product Engineer', 'description': 'Python Python'}), 'General software')
        self.assertEqual(family({'title': 'Software Engineer - Backend', 'description': ''}), 'Backend')

    def test_strict_posted_window_excludes_updates_missing_and_closed(self):
        self.insert('start', posted_at='2026-10-08T00:00:00Z')
        self.insert('end', posted_at='2026-10-09T12:00:00Z')
        self.insert('updated', posted_at='2025-01-01T00:00:00Z')
        self.insert('missing', posted_at=None)
        self.insert('closed', closed_at='2026-10-09T10:00:00Z')
        packet = self.store.packet(self.start())
        self.assertEqual([j['id'] for j in packet['jobs']], ['a', 'end'])
        self.assertNotIn('applications', packet)
        self.assertNotIn('previous_lists', packet)

    def test_freezes_resume_and_sources(self):
        rid = self.start()
        before = self.store.packet(rid)
        self.profile['facts'][0]['text'] = 'Changed later'
        self.insert('new')
        self.assertEqual(self.store.packet(rid), before)

    def test_prior_lists_and_application_tables_are_never_read(self):
        from job_search.job_reviews.old_method import store as module
        real = connect
        def guarded(path):
            con = real(path)
            def authorize(action, table, column, *rest):
                if action == sqlite3.SQLITE_READ and table in ('applications', 'curated_shortlists', 'curated_shortlist_items'):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            con.set_authorizer(authorize)
            return con
        with patch.object(module, 'connect', guarded):
            rid = self.start()
            self.assertEqual(len(self.store.packet(rid)['jobs']), 1)
        first = self.start()
        self.finish(first)
        self.store.publish(first, self.observations(first))
        second = self.start()
        self.finish(second)
        self.assertEqual(self.store.publish(second, self.observations(second))['lists'][0]['job_count'], 1)

    def test_recovery_search_restoration_and_unexamined_accounting(self):
        self.insert('b', title='Junior Automation Engineer')
        self.insert('c', title='Chef')
        rid = self.start()
        r = self.workspace(rid)
        self.assertEqual(r.search('automation', rejected_only=True)[0]['ordinal'], 2)
        r.restore([2])
        r.sources([2])
        r.save([{'ordinal': 2, 'assessment': assessment()}])
        r.finish([2])
        self.assertEqual(r.progress()['without_saved_assessment'], 2)
        self.store.checkpoint(rid, r.state, sealed=True)
        self.assertEqual(self.store.publish(rid, self.observations(rid))['lists'][0]['job_count'], 1)

    def test_invalid_evidence_missing_read_and_invalid_order_fail(self):
        r = self.workspace(self.start())
        with self.assertRaises(ContractError):
            r.save([{'ordinal': 1, 'assessment': assessment()}])
        r.sources([1])
        bad = assessment(); bad['evidence'][0]['quote'] = 'invented text'
        with self.assertRaises(ContractError):
            r.save([{'ordinal': 1, 'assessment': bad}])
        r.save([{'ordinal': 1, 'assessment': assessment()}])
        for order in ([], [1, 1], [2]):
            with self.assertRaises(ContractError):
                r.finish(order)
        r.finish([1])

    def test_checkpoint_resume_requires_same_packet(self):
        rid = self.start()
        r = self.finish(rid)
        again = Review(r.directory.parent/'input.json', r.directory)
        self.assertEqual(again.state, r.state)
        changed = self.store.packet(rid)
        changed['context']['facts'][0]['text'] = 'different'
        write_json(r.directory.parent/'input.json', changed)
        with self.assertRaises(ContractError):
            Review(r.directory.parent/'input.json', r.directory)

    def test_only_open_broad_and_targeted_are_published(self):
        self.insert('b'); self.insert('c')
        rid = self.start()
        self.finish(rid, {1: 'broad_only', 2: 'close', 3: 'close'})
        observations = self.observations(rid)
        observations[1]['status'] = 'unknown'; observations[2]['status'] = 'absent'
        receipt = self.store.publish(rid, observations)
        self.assertEqual([x['job_count'] for x in receipt['lists']], [1, 0])
        self.assertEqual(len(receipt['omitted']), 2)
        self.assertEqual(self.store.publish(rid, []), receipt)

    def test_missing_output_and_changed_source_do_not_publish(self):
        rid = self.start()
        with self.assertRaises(ContractError):
            self.store.publish(rid, [])
        self.finish(rid)
        with sqlite3.connect(self.jobs) as con:
            con.execute("UPDATE jobs SET description='New requirements'")
        with self.assertRaises(ContractError):
            self.store.publish(rid, self.observations(rid))

    def test_both_lists_rollback_when_second_write_fails(self):
        rid = self.start(); self.finish(rid)
        original = self.service.curated.publish_in_transaction
        def fail_second(con, value, **kwargs):
            if 'Targeted' in value['title']:
                raise ContractError('synthetic second-write failure')
            return original(con, value, **kwargs)
        with patch.object(self.service.curated, 'publish_in_transaction', fail_second), self.assertRaises(ContractError):
            self.store.publish(rid, self.observations(rid))
        with sqlite3.connect(self.ledger.store.db_path) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM curated_shortlists').fetchone()[0], 0)
        self.assertEqual(self.store.status(rid)['status'], 'active')

    def test_empty_sealed_list_and_unpublished_qualification(self):
        rid = self.start(); self.finish(rid, {})
        self.assertEqual([x['job_count'] for x in self.store.publish(rid, [])['lists']], [0, 0])
        rid = self.start(publish=False); self.finish(rid)
        with self.assertRaises(ContractError):
            self.store.publish(rid, self.observations(rid))

    def test_generic_managed_writer_cannot_change_workflow(self):
        rid = self.start()
        with self.assertRaises(ContractError):
            self.service.call('publish', {'review_id': rid, 'preview_sha256': 'x', 'idempotency_key': 'bad'})
        self.assertEqual(self.service.call('status', {'review_id': rid})['audit_required_count'], 0)
        from job_search.job_reviews.authority import ReviewAuthority
        authority = ReviewAuthority(self.service)
        with self.assertRaises(ContractError):
            authority.pending_checks(rid)
        with self.assertRaises(ContractError):
            authority.status(rid)

    def test_worker_mounts_only_frozen_inputs_output_and_model_socket(self):
        c = RuntimeConfig(image=self.execution['image'])
        argv = command(c, 'worker', '/private/output', '/private/model.sock', '/private/evidence')
        text = ' '.join(argv)
        self.assertIn('--network none', text)
        self.assertIn('--read-only', text)
        self.assertIn('target=/evidence,readonly', text)
        self.assertNotIn('/review/review.sock', text)
        self.assertNotIn('application_db', text)
        self.assertNotIn('AWS_', text)

    def test_actual_worker_configuration_reaches_image_readiness(self):
        from job_search.job_reviews.old_method import runtime
        config = RunnerConfig(application_config=self.root/'app.json', state_dir=self.root/'state', runtime_dir=self.root/'runtime',
                              auth_home=self.root/'auth', model_image='example/app@sha256:'+'a'*64)
        with patch.object(runtime, 'readiness', return_value={'ready': False}) as readiness:
            with self.assertRaisesRegex(ContractError, 'pinned review image is not ready'):
                with runtime.worker(config, {}, self.root/'attempt'):
                    self.fail('unavailable image must not launch')
        profile = readiness.call_args.args[0]
        self.assertEqual(profile.memory, '3g')
        self.assertEqual(profile.review_contract_version, 2)

    def test_publication_does_not_query_application_history(self):
        from job_search.job_reviews.old_method import store as module
        rid = self.start(); self.finish(rid)
        def guarded(path):
            con = connect(path)
            con.set_authorizer(lambda action, table, *rest:
                               sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'applications' else sqlite3.SQLITE_OK)
            return con
        with patch.object(module, 'connect', guarded):
            self.assertEqual(self.store.publish(rid, self.observations(rid))['lists'][0]['job_count'], 1)

    def test_host_runs_input_only_worker_and_resumes_failed_progress(self):
        app = self.root/'app.json'
        app.write_text(json.dumps({'version': 1, 'scraper_contact': 'test@example.test'}))
        app.chmod(0o600)
        config = RunnerConfig(application_config=app, state_dir=self.root/'state', runtime_dir=self.root/'runtime',
                              auth_home=self.root/'auth', model_image='example/app@sha256:'+'a'*64)
        rid = self.store.create('2026-10-08T00:00:00Z', '2026-10-09T12:00:00Z', execution=execution(config))['review_id']
        attempts = []
        @contextmanager
        def launch(config, packet, root, previous, telemetry):
            attempts.append(previous)
            root.mkdir(parents=True, exist_ok=True)
            write_json(root/'input.json', packet)
            r = Review(root/'input.json', root/'output')
            if previous:
                r.state = previous
            else:
                r.sources([1]); r.save([{'ordinal': 1, 'assessment': assessment()}])
            if len(attempts)>1:
                r.finish([1])
            class Handle:
                output_directory = r.directory
                def poll(self): return 1 if len(attempts)==1 else 0
                def wait(self): return self.poll()
            yield Handle()
        with self.assertRaises(ContractError):
            run_review(config, self.store, rid, launch=launch)
        self.assertEqual(self.store.status(rid)['assessment_count'], 1)
        result = run_review(config, self.store, rid, launch=launch,
                            availability=lambda jobs, **kw: self.observations(rid))
        self.assertEqual(result['status'], 'published')
        self.assertEqual(len(attempts[1]['assessments']), 1)

    def test_shutdown_keeps_checkpoint_without_publication(self):
        config = RunnerConfig(application_config=self.root/'app.json', state_dir=self.root/'state', runtime_dir=self.root/'runtime',
                              auth_home=self.root/'auth', model_image='example/app@sha256:'+'a'*64)
        rid = self.store.create('2026-10-08T00:00:00Z', '2026-10-09T12:00:00Z', execution=execution(config))['review_id']
        @contextmanager
        def launch(config, packet, root, previous, telemetry):
            write_json(root/'input.json', packet)
            r = Review(root/'input.json', root/'output')
            r.sources([1]); r.save([{'ordinal': 1, 'assessment': assessment()}])
            class Handle:
                output_directory = r.directory
                def poll(self): return None
                def terminate(self): pass
            yield Handle()
        with patch('job_search.job_reviews.old_method.host.draining', return_value=True):
            status = run_review(config, self.store, rid, launch=launch)
        self.assertEqual(status['phase'], 'interrupted')
        self.assertEqual(status['assessment_count'], 1)
        self.assertIsNone(status['receipt'])

    def test_sealed_result_resumes_validation_without_more_inference(self):
        config = RunnerConfig(application_config=self.root/'app.json', state_dir=self.root/'state', runtime_dir=self.root/'runtime',
                              auth_home=self.root/'auth', model_image='example/app@sha256:'+'a'*64)
        rid = self.store.create('2026-10-08T00:00:00Z', '2026-10-09T12:00:00Z',
                                execution=execution(config), publish=False)['review_id']
        self.finish(rid)
        def forbidden(*args, **kwargs):
            self.fail('sealed resume must not invoke the model')
        self.assertEqual(run_review(config, self.store, rid, launch=forbidden)['phase'], 'qualified')

    def test_generic_status_keeps_unexamined_distinct_from_pending(self):
        rid = self.start()
        status = self.service.call('status', {'review_id': rid})
        self.assertEqual(status['counts'], {'unexamined': 1})
        self.assertEqual(status['effective_counts'], {'unexamined': 1})

    def test_output_reader_rejects_symlinks(self):
        path = self.root/'result.json'
        path.symlink_to(self.jobs)
        with self.assertRaises(OSError):
            read_output(path)

    def test_background_dispatch_is_supervised_and_retains_maintenance_gate(self):
        calls = []
        with patch.dict('os.environ', {'JOB_SEARCH_MAINTENANCE_GATE': '/private/maintenance/gate.json'}):
            dispatch('oldreview_example', self.root/'runner.json', run=lambda args, **kwargs: calls.append(args))
        self.assertEqual(calls[0][0], 'systemd-run')
        self.assertIn('--setenv=JOB_SEARCH_MAINTENANCE_GATE=/private/maintenance/gate.json', calls[0])


if __name__ == '__main__':
    unittest.main()
