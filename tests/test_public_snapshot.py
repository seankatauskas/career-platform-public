"""Public snapshots never carry private history and publish at most once per date."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publisher', ROOT / 'scripts/publish-public-snapshot.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def git(path, *args):
    return subprocess.check_output(['git', *args], cwd=path, text=True).strip()


class PublicSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.public = self.root / 'public'
        for path in (self.source, self.public):
            path.mkdir()
            git(path, 'init', '--initial-branch=main')
            git(path, 'config', 'user.name', 'Snapshot test')
            git(path, 'config', 'user.email', 'snapshot@example.test')
        self.write('README.md', 'First version\n')
        self.write('LICENSE', 'Original attribution\n')
        self.commit()

    def write(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def commit(self):
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Private development detail')

    def export(self, name='snapshot'):
        path = self.root / name
        publisher.exporter.export_tree(self.source, 'HEAD', path)
        return path

    def test_initial_history_and_daily_coalescing(self):
        self.write('README.md', 'Second private version\n'); self.commit()
        first = self.export()
        result = publisher.publish_snapshot(first, self.public, '2026-09-30')
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(git(self.public, 'rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(git(self.public, 'log', '-1', '--format=%s'), 'Initial public release')
        self.assertNotIn('Private development detail', git(self.public, 'log', '--format=%B'))
        self.assertEqual((self.public / 'LICENSE').read_text(), 'Original attribution\n')
        self.write('README.md', 'Third private version\n'); self.commit()
        second = self.export('second')
        self.assertEqual(publisher.publish_snapshot(second, self.public, '2026-09-30')['status'], 'already_published')
        self.assertEqual((self.public / 'README.md').read_bytes(), (first / 'README.md').read_bytes())
        self.assertEqual(publisher.publish_snapshot(second, self.public, '2026-10-01')['status'], 'committed')
        self.assertEqual(git(self.public, 'rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(publisher.publish_snapshot(second, self.public, '2026-10-02')['status'], 'unchanged')
        self.assertEqual(git(self.public, 'rev-list', '--count', 'HEAD'), '2')

    def test_deletions_modes_and_directory_replacement(self):
        self.write('scripts/old/nested.py', 'print(1)\n'); self.commit()
        publisher.publish_snapshot(self.export(), self.public, '2026-09-30')
        (self.source / 'scripts/old/nested.py').unlink()
        (self.source / 'scripts/old').rmdir()
        executable = self.write('scripts/old', '#!/bin/sh\necho ready\n')
        executable.chmod(0o755); self.commit()
        publisher.publish_snapshot(self.export('second'), self.public, '2026-10-01')
        self.assertTrue((self.public / 'scripts/old').is_file())
        self.assertEqual((self.public / 'scripts/old').stat().st_mode & 0o777, 0o755)
        self.assertEqual(git(self.public, 'status', '--porcelain'), '')

    def test_committed_source_and_workflow_precedence(self):
        self.write('.github/workflow-examples/aws-deploy.yml', 'old example\n')
        self.write('.github/workflows/aws-deploy.yml', 'current deployment\n')
        self.write('.github/workflows/public-checks.yml', 'offline checks\n')
        self.write('.github/workflows/public-snapshot.yml', 'private publisher\n')
        self.commit()
        self.write('README.md', 'Uncommitted change\n')
        self.write('private/token', 'Untracked secret\n')
        output = self.export()
        self.assertTrue((output / 'README.md').read_text().startswith('First version'))
        self.assertFalse((output / 'private').exists())
        self.assertEqual((output / '.github/workflow-examples/aws-deploy.yml').read_text(), 'current deployment\n')
        self.assertFalse((output / '.github/workflows/aws-deploy.yml').exists())
        self.assertTrue((output / '.github/workflows/public-checks.yml').is_file())
        self.assertFalse((output / '.github/workflows/public-snapshot.yml').exists())
        self.assertTrue((output / '.github/workflow-examples/public-snapshot.yml').is_file())

    def test_review_source_is_exported_and_unknown_paths_still_require_review(self):
        reviewed = {
            'Dockerfile.codex-review': 'FROM python:3.12-slim\n',
            'Dockerfile.codex-review.dockerignore': '**\n!job_search/\n',
            'skills/career-job-review/SKILL.md': '# Review collected jobs\n',
            'skills/career-job-review/references/interface.md': '# Review interface\n',
        }
        for name, content in reviewed.items():
            self.write(name, content)
        self.commit()
        output = self.export()
        for name, content in reviewed.items():
            self.assertEqual((output / name).read_text(), content)
        for name in ('skills/unreviewed/SKILL.md', 'Dockerfile.unreviewed'):
            with self.subTest(name=name):
                path = self.write(name, 'Needs review\n')
                self.commit()
                with self.assertRaisesRegex(SystemExit, 'Unreviewed source path'):
                    self.export('rejected')
                path.unlink()
                self.commit()

    def test_private_content_and_nonregular_sources_rejected(self):
        sensitive = self.write('docs/note.md', 'sk-or-v1-' + 'a' * 64)
        self.commit()
        sensitive.write_text('Changed only in working copy')
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.export()
        sensitive.unlink(); self.commit()
        (self.source / 'docs/link.md').symlink_to('/etc/hosts'); self.commit()
        with self.assertRaisesRegex(ValueError, 'Non-regular'):
            self.export()

    def test_dirty_unmanaged_and_older_destinations_rejected(self):
        output = self.export()
        publisher.publish_snapshot(output, self.public, '2026-09-30')
        with self.assertRaisesRegex(ValueError, 'precedes'):
            publisher.publish_snapshot(output, self.public, '2026-09-29')
        (self.public / 'unexpected').write_text('Local edits')
        with self.assertRaisesRegex(ValueError, 'clean'):
            publisher.publish_snapshot(output, self.public, '2026-10-01')
        git(self.public, 'add', '--all'); git(self.public, 'commit', '-m', 'Manual edit')
        with self.assertRaisesRegex(ValueError, 'not produced'):
            publisher.publish_snapshot(output, self.public, '2026-10-01')

    def test_reviewed_ignored_source_is_preserved_without_runtime_files(self):
        self.write('.gitignore', '*.json\nprivate/\n')
        self.write('extension/package.json', '{"private":true}\n')
        git(self.source, 'add', '--force', 'extension/package.json')
        self.commit()
        output = self.export()
        publisher.publish_snapshot(output, self.public, '2026-09-30')
        self.assertIn('extension/package.json', git(self.public, 'ls-files'))
        (self.public / 'private').mkdir()
        (self.public / 'private/runtime.json').write_text('{"local":true}')
        self.write('extension/package.json', '{"private":true,"version":"2"}\n')
        self.commit()
        publisher.publish_snapshot(self.export('next'), self.public, '2026-10-01')
        self.assertEqual(git(self.public, 'show', 'HEAD:extension/package.json'), '{"private":true,"version":"2"}')
        self.assertNotIn('private/', git(self.public, 'ls-files'))


if __name__ == '__main__':
    unittest.main()
