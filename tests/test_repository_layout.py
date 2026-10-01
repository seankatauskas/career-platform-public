"""Keep development files discoverable without crowding runtime entry points."""
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_development_files_stay_in_their_directories(self):
        misplaced = []
        for pattern in ('*.py', 'test_*.mjs', 'requirements-*.txt', '*.example.json'):
            misplaced.extend(path.name for path in ROOT.glob(pattern))
        misplaced.extend(path.name for path in ROOT.glob('*.md')
                         if path.name not in {'README.md', 'AGENTS.md', 'CLAUDE.md'})
        self.assertEqual(misplaced, [], 'Use job_search/, scripts/, tests/, requirements/, examples/, or docs/ instead of the root')

    def test_navigation_links_resolve(self):
        for name in ('README.md', 'docs/README.md', 'docs/commands.md', 'tests/README.md', 'examples/README.md', 'requirements/README.md'):
            page = ROOT / name
            for target in re.findall(r'\]\(([^\s)]+)\)', page.read_text()):
                if re.match(r'[a-z]+:|#', target):
                    continue
                dest = target.split('#', 1)[0]
                self.assertTrue((page.parent / dest).exists(), f'{name}: missing link {target}')

    def test_module_commands_start_without_live_services(self):
        modules = ['job_search']
        for folder in ('collection', 'ranking', 'salary'):
            for source in sorted((ROOT / 'job_search' / folder).glob('*.py')):
                if '__name__ == "__main__"' in source.read_text():
                    modules.append(f'job_search.{folder}.{source.stem}')
        self.assertGreater(len(modules), 10)
        for module in modules:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-m', module, '--help'], cwd=ROOT,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout.lower())

    def test_source_move_preserves_state_paths_and_assets(self):
        from job_search.collection import boards
        from job_search.ranking import labeler as ranking
        from job_search.salary import labeler, review, v3_review
        self.assertEqual(boards.HERE, ROOT)
        self.assertEqual(boards.BOARDS_CACHE, ROOT / 'boards.json')
        self.assertTrue(boards.BOARDS_SEED.is_file())
        for module in (ranking, labeler, review, v3_review):
            self.assertEqual(module.ROOT, ROOT)
            for asset in ('index.html', 'app.js', 'styles.css'):
                self.assertTrue((module.ASSET_DIR / asset).is_file(), module.__name__ + ': ' + asset)

    def test_dependency_includes_resolve(self):
        for requirements in (ROOT / 'requirements').glob('*.txt'):
            for include in re.findall(r'^-r\s+(\S+)', requirements.read_text(), re.M):
                self.assertTrue((requirements.parent / include).is_file(), f'{requirements.name}: missing {include}')


if __name__ == '__main__':
    unittest.main()
