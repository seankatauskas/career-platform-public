"""Predecessor builds preserve their exact pinned image and source identities."""
from copy import deepcopy
from pathlib import Path
import runpy
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / 'scripts/release-transition-acceptance.py'))
build_args = helpers['predecessor_build_args']


class PredecessorBuildTests(unittest.TestCase):
    def test_mirror_preserves_predecessor_digest(self):
        digest = 'a' * 64
        source = '# syntax=docker/dockerfile:1\nARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm@sha256:' + digest + '\n'
        self.assertEqual(build_args(source, 'old-commit'), [
            'SOURCE_REVISION=old-commit',
            'PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:' + digest])

    def test_never_substitutes_unpinned_or_other_images(self):
        for image in ('python:latest', 'other.example/python@sha256:' + 'b' * 64,
                      'public.ecr.aws/docker/library/python@sha256:' + 'c' * 64):
            with self.subTest(image=image):
                self.assertEqual(len(build_args('ARG PYTHON_BASE_IMAGE=' + image, 'old')), 1)

    def test_bundled_frontend_changes_only_standard_directive(self):
        recipe = 'FROM ${PYTHON_BASE_IMAGE}\nCOPY . /app\n'
        convert = helpers['predecessor_dockerfile']
        self.assertEqual(convert('# syntax=docker/dockerfile:1\n' + recipe), recipe)
        self.assertEqual(convert(recipe), recipe)
        custom = '# syntax=example.com/custom:1\n' + recipe
        self.assertEqual(convert(custom), custom)


class PreparationPreservationTests(unittest.TestCase):
    def fixture(self):
        return {'files': {'config.json': 'configuration-hash'}, 'databases': {'data/jobs.db': {
            'family_preparation_state': {
                'columns': ['id', 'revision', 'sealed_revision', 'signature', 'source_revision', 'prepared_source_revision'],
                'rows': [[1, 5, 5, 'verified-signature', 2, 2]]},
            'jobs': {'columns': ['id', 'title'], 'rows': [['source-job', 'Original title']]},
        }}}

    def verify(self, before, after):
        from tests.fixtures.release_transition import assert_preserved
        with patch('tests.fixtures.release_transition.snapshot', return_value=after):
            return assert_preserved(Path('/unused-fixture'), before)

    def test_only_monotonic_derived_revision_is_allowed_to_change(self):
        before = self.fixture()
        after = deepcopy(before)
        after['databases']['data/jobs.db']['family_preparation_state']['rows'][0][1] = 9
        self.verify(before, after)
        self.assertEqual(after['databases']['data/jobs.db']['family_preparation_state']['rows'][0][1], 9)
        for column, value in ((1, 4), (1, True), (2, 9), (3, 'changed-signature'), (4, 3), (5, 3)):
            with self.subTest(column=column):
                changed = deepcopy(before)
                changed['databases']['data/jobs.db']['family_preparation_state']['rows'][0][column] = value
                with self.assertRaises(AssertionError):
                    self.verify(before, changed)

    def test_source_rows_tracker_rows_and_artifacts_still_must_be_preserved(self):
        before = self.fixture()
        for table in ('jobs', 'family_preparation_state'):
            with self.subTest(table=table):
                after = deepcopy(before)
                after['databases']['data/jobs.db'][table]['rows'] = []
                with self.assertRaises(AssertionError):
                    self.verify(before, after)
        after = deepcopy(before)
        after['files']['config.json'] = 'changed'
        with self.assertRaises(AssertionError):
            self.verify(before, after)


class PreparationResealTests(unittest.TestCase):
    def setUp(self):
        from job_search.collection import boards
        from job_search.collection.dedupe import prepare_families
        from tests.fixtures.release_transition import snapshot
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'data').mkdir()
        (self.root / 'config.json').write_text('{}')
        self.jobs = self.root / 'data/jobs.db'
        with sqlite3.connect(self.jobs) as con:
            boards._prepare(con)
        prepare_families(self.jobs)
        boards.save([{'ats': 'ashby', 'id': 'source-job', 'company': 'Fixture',
                      'title': 'Engineer', 'description': 'Fictional source text'}], self.jobs, '2026-10-17')
        self.before = snapshot(self.root)

    def verify(self):
        from tests.fixtures.release_transition import assert_preserved
        return assert_preserved(self.root, self.before, allow_preparation=True)

    def reseal(self):
        from job_search.collection.dedupe import prepare_families
        prepare_families(self.jobs)

    def test_verified_reseal_can_consume_covered_journal(self):
        self.reseal()
        result = self.verify()
        state = result['databases']['data/jobs.db']['family_preparation_state']['rows'][0]
        self.assertEqual(state[1], state[2])
        self.assertEqual(state[4], state[5])
        self.assertEqual(result['databases']['data/jobs.db']['family_preparation_dirty']['rows'], [])

    def test_journal_deletion_without_checkpoint_publication_is_rejected(self):
        with sqlite3.connect(self.jobs) as con:
            con.execute('DELETE FROM family_preparation_dirty')
        with self.assertRaisesRegex(AssertionError, 'persisted row lost'):
            self.verify()

    def test_corrupt_checkpoint_or_raw_source_is_rejected(self):
        self.reseal()
        from tests.fixtures.release_transition import snapshot
        correct = snapshot(self.root)
        for column, value in (('revision', -1), ('sealed_revision', 999),
                              ('prepared_source_revision', 0), ('signature', 'forged'),
                              ('source_revision', 999)):
            with self.subTest(column=column):
                changed = deepcopy(correct)
                table = changed['databases']['data/jobs.db']['family_preparation_state']
                table['rows'][0][table['columns'].index(column)] = value
                with patch('tests.fixtures.release_transition.snapshot', return_value=changed):
                    with self.assertRaises(AssertionError):
                        self.verify()
        for table in ('jobs', 'family_preparation_state'):
            with self.subTest(missing=table):
                changed = deepcopy(correct)
                changed['databases']['data/jobs.db'][table]['rows'] = []
                with patch('tests.fixtures.release_transition.snapshot', return_value=changed):
                    with self.assertRaises(AssertionError):
                        self.verify()

    def test_forged_current_seal_cannot_hide_bad_raw_fingerprints(self):
        self.reseal()
        with sqlite3.connect(self.jobs) as con:
            con.execute("UPDATE job_family_members SET source_fingerprint='corrupted'")
            con.execute('UPDATE family_preparation_state SET sealed_revision=revision')
        with self.assertRaisesRegex(AssertionError, 'differs from raw source'):
            self.verify()

    def test_prior_journal_gap_cannot_be_reclassified_as_consumed(self):
        self.reseal()
        self.before['databases']['data/jobs.db']['family_preparation_dirty']['rows'] = []
        with self.assertRaises(AssertionError):
            self.verify()


if __name__ == '__main__':
    unittest.main()
