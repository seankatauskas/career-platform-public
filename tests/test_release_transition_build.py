"""Predecessor builds preserve their exact pinned image and source identities."""
from pathlib import Path
import runpy
import unittest

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


if __name__ == '__main__':
    unittest.main()
