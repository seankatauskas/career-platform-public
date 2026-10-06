"""Approved career evidence and independently saved review scope regressions."""
import copy
import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from job_search.contracts import ContractError
from job_search.job_reviews.brief import (
    SCHEMA, SearchBriefMixin, current_search_brief, suggested_search_brief,
    validate_search_brief,
)
from job_search.job_reviews.context import profile_context, review_context, stored_profile_context
from job_search.resume_lab.career_store import CareerStore, empty_career_content


class SearchBriefTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.addCleanup(self.con.close)
        self.con.executescript(SCHEMA)
        self.service = SearchBriefMixin()
        self.service.now = lambda: '2026-10-04T12:00:00Z'

    def save(self, brief=None, revision=0):
        with self.con:
            return self.service._save_brief(self.con, {
                'brief': brief or suggested_search_brief(), 'expected_revision': revision,
            })

    def test_unsaved_suggestions_have_no_revision_or_user_attestation(self):
        first = current_search_brief(self.con)
        self.assertEqual(first['revision'], 0)
        self.assertIsNone(first['saved_at'])
        self.assertEqual(first['brief']['eligibility_facts'], [])
        first['brief']['notes'].append('Must not become a saved preference')
        self.assertEqual(current_search_brief(self.con)['brief']['notes'], [])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM job_review_search_briefs').fetchone()[0], 0)

    def test_saved_scope_is_independent_of_review_history_and_preserves_revisions(self):
        brief = suggested_search_brief()
        brief.update(broad_geography='worldwide', targeted_geography='us',
                     eligibility_facts=['I am a US citizen.'], notes=['I enjoy building software.'])
        first = self.save(brief)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(first['brief'], brief)
        brief['notes'].append('Explore developer tools.')
        second = self.save(brief, revision=1)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(first['brief']['notes'], ['I enjoy building software.'])
        self.assertEqual(len(self.con.execute('SELECT * FROM job_review_search_briefs').fetchall()), 2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
            self.con.execute('UPDATE job_review_search_briefs SET brief_json=? WHERE revision=1', ('{}',))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
            self.con.execute('DELETE FROM job_review_search_briefs WHERE revision=1')

    def test_stale_revision_cannot_replace_saved_preferences(self):
        first = self.save()
        with self.assertRaisesRegex(ContractError, 'changed'):
            self.save({**suggested_search_brief(), 'broad_geography': 'worldwide'}, revision=0)
        self.assertEqual(current_search_brief(self.con), first)

    def test_validation_does_not_infer_or_accept_unbounded_preferences(self):
        malformed = [
            {'unknown': 'value'},
            {**suggested_search_brief(), 'broad_geography': 'CA'},
            {**suggested_search_brief(), 'adjacent_roles': []},
            {**suggested_search_brief(), 'stretch_policy': ' '},
            {**suggested_search_brief(), 'eligibility_facts': ['note'] * 21},
            {**suggested_search_brief(), 'notes': 'A note'},
            {**suggested_search_brief(), 'notes': ['x' * 501]},
        ]
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_search_brief(value)
        for value in (True, -1, None):
            with self.subTest(revision=value), self.assertRaises(ContractError):
                self.save(revision=value)
        self.assertEqual(current_search_brief(self.con)['revision'], 0)


class ApprovedReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CareerStore(Path(self.tmp.name) / 'career.db')
        self.version = {'version_id': 'standard_version', 'plain_text': 'Software engineer\nPython'}
        self.resume = SimpleNamespace(
            get_career_profile=self.store.get_profile,
            service=SimpleNamespace(
                list_active_standards=lambda: [{'manual_rank': 1, 'standard_id': 'standard',
                                                'active_version_id': 'standard_version'}],
                store=SimpleNamespace(get_standard_version=lambda _identifier: self.version),
            ),
        )
        self.content = empty_career_content()
        self.content['identity']['name'] = 'Example Person'
        self.content['identity']['email'] = 'private@example.test'
        self.content['summary'] = 'Build reliable software with a focus on operational tools.'
        self.content['experience'] = [{'company': 'Example', 'role': 'Engineer',
                                      'bullets': ['Built incident tooling used by an operations team.']}]
        self.content['projects'] = [{'name': 'Personal project',
                                    'bullets': ['Developed an open-source deployment tool.']}]

    def approve(self, content=None, previous=None):
        draft = self.store.save_draft(content or self.content, expected_revision_id=previous)
        self.store.approve_revision(draft['revision_id'])
        return draft

    def test_seed_style_pending_draft_is_visible_but_never_evidence(self):
        draft = self.store.save_draft(self.content, provenance={'attestation': 'pending_user_review'})
        before = profile_context(self.resume)
        self.assertIsNone(before['profile_revision'])
        self.assertEqual(before['source_inventory']['career_fact_count'], 0)
        self.assertEqual(before['source_inventory']['resume_fact_count'], 2)
        self.assertTrue(before['source_inventory']['pending_career_draft'])
        self.assertNotIn('incident tooling', json.dumps(before))
        self.store.approve_revision(draft['revision_id'])
        after = profile_context(self.resume)
        self.assertEqual(after['profile_revision'], draft['revision_id'])
        self.assertEqual(after['source_inventory']['career_fact_count'], 5)
        self.assertEqual(after['source_inventory']['total_fact_count'], 7)
        self.assertFalse(after['source_inventory']['pending_career_draft'])
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])

    def test_full_approved_bank_includes_summary_and_preserves_employment_project_sources(self):
        approved = self.approve()
        context = profile_context(self.resume)
        by_source = {source: [fact for fact in context['facts'] if fact['source'] == source]
                     for source in ('summary', 'experience', 'projects')}
        self.assertEqual(by_source['summary'][0]['fact_id'], approved['revision_id'] + '.summary')
        self.assertEqual(by_source['summary'][0]['text'], self.content['summary'])
        self.assertEqual(by_source['experience'][1]['text'], self.content['experience'][0]['bullets'][0])
        self.assertEqual(by_source['projects'][1]['text'], self.content['projects'][0]['bullets'][0])
        self.assertNotIn('private@example.test', json.dumps(context))
        self.assertEqual(len({f['fact_id'] for f in context['facts']}), len(context['facts']))

    def test_draft_edits_do_not_change_approved_facts_or_evidence_fingerprint(self):
        approved = self.approve()
        before = profile_context(self.resume)
        changed = copy.deepcopy(approved['content'])
        changed['summary'] = 'Unapproved claims must not appear'
        changed['experience'][0]['bullets'][0]['text'] = 'Unapproved work'
        self.store.save_draft(changed, expected_revision_id=approved['revision_id'])
        after = profile_context(self.resume)
        self.assertEqual(after['facts'], before['facts'])
        self.assertEqual(after['fingerprint'], before['fingerprint'])
        self.assertTrue(after['source_inventory']['pending_career_draft'])
        self.assertNotIn('Unapproved', json.dumps(after))

    def test_retired_entries_and_facts_are_excluded_and_long_resume_lines_are_complete(self):
        approved = self.approve()
        changed = copy.deepcopy(approved['content'])
        changed['experience'][0]['bullets'][0]['retired'] = True
        changed['projects'][0]['retired'] = True
        self.approve(changed, previous=approved['revision_id'])
        self.version['plain_text'] = 'z' * 4500
        context = profile_context(self.resume)
        self.assertNotIn('incident tooling', json.dumps(context))
        self.assertNotIn('Personal project', json.dumps(context))
        resume_facts = [f for f in context['facts'] if f['source'] == 'resume']
        self.assertEqual(''.join(f['text'] for f in resume_facts), self.version['plain_text'])
        self.assertEqual([len(f['text']) for f in resume_facts], [2000, 2000, 500])

    def test_projection_keeps_new_context_without_model_diagnostics(self):
        self.approve()
        context = profile_context(self.resume)
        context['search_brief'] = {'revision': 1, 'saved_at': '2026-10-04T12:00:00Z',
                                   'brief': suggested_search_brief(), 'model_rank': 'MODEL_ONLY'}
        context['search_brief']['brief']['ranking_score'] = 'MODEL_ONLY'
        context['source_inventory']['ranking_score'] = 'MODEL_ONLY'
        context['source_inventory']['facts_by_source']['model_score'] = 'MODEL_ONLY'
        context['ranking_score'] = 'MODEL_ONLY'
        projected = review_context(context)
        self.assertEqual(projected['search_brief']['revision'], 1)
        self.assertEqual(projected['source_inventory']['career_fact_count'], 5)
        self.assertNotIn('MODEL_ONLY', json.dumps(projected))
        self.assertEqual(review_context(projected), projected)


class StoredReviewContextTests(unittest.TestCase):
    def test_host_authority_reads_same_approved_evidence_without_artifact_ownership(self):
        from job_search.job_reviews.runner import build_authority
        from job_search.resume_lab.artifacts import ArtifactSecurityError, ResumePdfArtifactRepository
        from job_search.resume_lab.contracts import ClaimOrigin, ResumeClaim, StandardVersionInput
        from tests.test_career_resume import gateway_at

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway, _ = gateway_at(root)
            version = StandardVersionInput('source', 'Approved resume text',
                                          (ResumeClaim('claim', 'Approved resume text', ClaimOrigin.USER_ATTESTED),))
            gateway.service.create_standard('Active resume', 1, version, actor_kind='user')
            gateway.service.create_standard('Inactive resume', 2,
                StandardVersionInput('source', 'Inactive text', (ResumeClaim('inactive', 'Inactive text', ClaimOrigin.USER_ATTESTED),)),
                actor_kind='user', active=False)
            profile = gateway.get_career_profile()
            pending = copy.deepcopy(profile['approved']['content'])
            pending['summary'] = 'Unapproved imaginary work'
            gateway.save_career_profile(pending, expected_revision_id=profile['draft_revision_id'],
                                       idempotency_key='pending-edit')
            expected = profile_context(gateway)
            db = root / 'resume.db'
            # Flush fixture writes before comparing bytes; the existing writer
            # helpers can otherwise checkpoint their WAL during garbage collection.
            gc.collect()
            with closing(sqlite3.connect(db)) as con:
                con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            before = db.read_bytes()
            artifacts = root / 'artifacts'
            def artifact_state():
                # Traversing directories can itself update atime on Linux. Keep
                # every identity, ownership, content and mutation-time check.
                fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                          'st_size', 'st_mtime_ns', 'st_ctime_ns')
                result = {}
                for path in [artifacts, *artifacts.rglob('*')]:
                    info = path.stat()
                    result[path.relative_to(artifacts)] = tuple(getattr(info, key) for key in fields)
                return result
            before_artifacts = artifact_state()
            # Simulate a coordinator UID different from the application's owner.
            # The actual artifact guard must still reject that caller.
            host_uid = 0 if os.getuid() else 10001
            with patch('job_search.resume_lab.artifacts.os.getuid', return_value=host_uid):
                with self.assertRaises(ArtifactSecurityError):
                    ResumePdfArtifactRepository(artifacts)
                application = SimpleNamespace(resume_lab_db=db, resume_artifact_root=artifacts,
                    application_db=root / 'applications.db', jobs_db=root / 'jobs.db')
                config = SimpleNamespace(application_config=root / 'app.json',
                                         model='test-model', reasoning_effort='high',
                                         check_profile=lambda: {'model': 'test-model', 'reasoning_effort': 'high'},
                                         screening_model=None, screening_reasoning_effort=None)
                with patch('job_search.runtime.load_runtime_config', return_value=application), \
                     patch('job_search.resume_lab.gateway.ResumePdfArtifactRepository', side_effect=AssertionError('PDF access')), \
                     patch('job_search.resume_lab.service.ResumeLabStore', side_effect=AssertionError('schema mutation')), \
                     patch('job_search.resume_lab.career_store.CareerStore.__init__', side_effect=AssertionError('schema mutation')):
                    actual = build_authority(config).service.context_provider()
            self.assertEqual(actual, expected)
            self.assertTrue(actual['source_inventory']['pending_career_draft'])
            self.assertNotIn('Unapproved imaginary work', json.dumps(actual))
            self.assertNotIn('Inactive text', json.dumps(actual))
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(artifact_state(), before_artifacts)

    @unittest.skipIf(os.geteuid() == 0, 'uses a nonroot-owned fixture to exercise root dispatch')
    def test_root_reader_drops_only_child_identity_and_keeps_live_wal_evidence(self):
        from job_search.resume_lab.service import ResumeLabService
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'resume.db'
            ResumeLabService(db)
            store = CareerStore(db)
            content = empty_career_content()
            content['identity']['name'] = 'Example Person'
            content['summary'] = 'Approved text retained in the live WAL'
            content['skills'] = [{'category': 'Languages', 'items': ['Python']}]
            draft = store.save_draft(content)
            store.approve_revision(draft['revision_id'])
            expected = stored_profile_context(db)
            owner = db.stat()
            run = subprocess.run

            def owner_child(command, **kwargs):
                self.assertEqual(command[:3], [sys.executable, '-I', '-'])
                self.assertEqual(kwargs.pop('user'), owner.st_uid)
                self.assertEqual(kwargs.pop('group'), owner.st_gid)
                self.assertEqual(kwargs.pop('extra_groups'), ())
                self.assertEqual(kwargs['umask'], 0o077)
                self.assertEqual(kwargs['cwd'], '/')
                self.assertEqual(kwargs['env'], {})
                self.assertEqual(kwargs['timeout'], 30)
                # The test already runs as that owner; a nonroot test process
                # cannot clear supplementary groups. Execute the exact child code.
                return run(command, **kwargs)

            with patch('job_search.resume_lab.evidence.os.geteuid', return_value=0), \
                 patch('job_search.resume_lab.evidence.subprocess.run', side_effect=owner_child) as child:
                actual = stored_profile_context(db)
            child.assert_called_once()
            self.assertEqual(actual, expected)
            self.assertEqual(actual['profile_revision'], draft['revision_id'])
            self.assertTrue(all(p.stat().st_uid == owner.st_uid for p in Path(directory).iterdir()))

    def test_missing_database_is_not_created_and_symlink_is_rejected(self):
        from job_search.resume_lab.contracts import ResumeBoundaryError
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'missing.db'
            with self.assertRaises(FileNotFoundError):
                stored_profile_context(db)
            self.assertFalse(db.exists())
            link = Path(directory) / 'linked.db'
            link.symlink_to(db)
            with self.assertRaises(ResumeBoundaryError):
                stored_profile_context(link)

    @unittest.skipUnless(os.geteuid() == 0, 'requires root to exercise a real application UID')
    def test_root_child_creates_owner_sidecars_and_reads_uncheckpointed_wal(self):
        from job_search.resume_lab.evidence import read_review_evidence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'evidence.db'
            with closing(sqlite3.connect(db)) as con:
                con.executescript('''
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE career_profile_state(singleton, draft_revision_id, approved_revision_id);
                    CREATE TABLE career_profile_revisions(revision_id, content_json);
                    CREATE TABLE resume_standards(active, active_version_id, manual_rank, standard_id);
                    CREATE TABLE resume_standard_versions(version_id, plain_text);
                    INSERT INTO career_profile_state VALUES(1, 'first', 'first');
                    INSERT INTO career_profile_revisions VALUES('first', '{"summary":"initial"}');
                ''')
            self.assertEqual(list(root.iterdir()), [db])
            os.chown(root, 10001, 10001)
            os.chown(db, 10001, 10001)
            before = db.read_bytes()
            career, _ = read_review_evidence(db)
            self.assertEqual(career['approved']['content']['summary'], 'initial')
            self.assertEqual(db.read_bytes(), before)
            self.assertTrue(all(p.stat().st_uid == 10001 for p in root.iterdir()))
            with closing(sqlite3.connect(db)) as writer:
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('INSERT INTO career_profile_revisions VALUES(?,?)',
                               ('next', '{"summary":"approved in live WAL"}'))
                writer.execute("UPDATE career_profile_state SET approved_revision_id='next'")
                writer.commit()
                self.assertGreater(Path(str(db) + '-wal').stat().st_size, 0)
                career, _ = read_review_evidence(db)
                self.assertEqual(career['approved']['content']['summary'], 'approved in live WAL')
                self.assertTrue(all(p.stat().st_uid == 10001 for p in root.iterdir()))


if __name__ == '__main__':
    unittest.main()
