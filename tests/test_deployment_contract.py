"""Compatible deployment identity excludes presentation but includes persisted writers."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from job_search.deployment_contract import (AUDIT_PATH, AUDITED_BACKEND, fingerprint, from_commit,
    reviewed_backend, source_protocol, audit_current_from_commit, protocol_from_commit)
from tests.test_aws_release_package import repository, git


class ContractTests(unittest.TestCase):
    def test_only_static_web_and_docs_can_change_without_new_runtime_contract(self):
        files = {'job_search/aws_ops.py': b'ops', 'compose.cloud.yaml': b'compose',
                 'job_search/web/view.js': b'old', 'job_search/web/view.css': b'old',
                 'docs/operations/example.md': b'old', 'job_search/application_runtime.py': b'old'}
        original = fingerprint(files)
        for path in ('job_search/web/view.js', 'job_search/web/view.css', 'docs/operations/example.md'):
            self.assertEqual(original, fingerprint({**files, path: b'new'}))
        for path in ('job_search/application_runtime.py', 'job_search/new_module.py',
                     'job_search/web/loader.py', 'requirements/cloud.txt', 'compose.cloud.yaml',
                     'Dockerfile', 'deploy/hermes/cont-init.d/entrypoint', 'deploy/aws/job-search-review.service'):
            self.assertNotEqual(original, fingerprint({**files, path: b'new'}))

    def reviewed(self, contents=b'old read-only diagnostics'):
        files = {'job_search/aws_ops.py': b'ops', 'compose.cloud.yaml': b'compose',
                 'job_search/runtime_readiness.py': contents}
        files[AUDIT_PATH] = json.dumps({'protocol': 2, 'modules': {
            'job_search/runtime_readiness.py': hashlib.sha256(contents).hexdigest()}}).encode()
        return files

    def test_only_exact_reviewed_diagnostics_can_change(self):
        before = self.reviewed()
        after = self.reviewed(b'new read-only diagnostics')
        self.assertEqual(fingerprint(before), fingerprint(after))
        self.assertNotEqual(fingerprint(before), fingerprint({**before,
            'job_search/runtime_readiness.py': b'unreviewed mutation'}))
        # A review declaration cannot approve changes outside its narrow scope.
        for path in ('job_search/readiness.py', 'job_search/schema_lifecycle.py',
                     'job_search/dependency_snapshot.py', 'job_search/runtime.py',
                     'job_search/ranking/model.py', 'job_search/resume_lab/store.py',
                     'job_search/deployment_contract.py', 'job_search/release_policy.py',
                     'requirements/cloud.txt', 'compose.cloud.yaml', '.dockerignore',
                     'deploy/aws/runtime-config.json.tftpl'):
            with self.subTest(path=path):
                self.assertNotEqual(fingerprint(before), fingerprint({**after, path: b'changed'}))
        self.assertNotEqual(fingerprint(before), fingerprint(before,
            modes={'job_search/runtime_readiness.py': '100755'}))

    def test_invalid_incomplete_or_stale_audits_do_not_exempt_source(self):
        files = self.reviewed()
        original = fingerprint(files)
        valid = json.loads(files[AUDIT_PATH])
        declarations = [None, b'invalid', b'[]', b'null', b'\xff',
            json.dumps({**valid, 'protocol': 1}).encode(),
            json.dumps({**valid, 'protocol': True}).encode(),
            json.dumps({**valid, 'modules': {}}).encode(),
            json.dumps({**valid, 'modules': {'job_search/runtime_readiness.py': '0' * 64}}).encode(),
            json.dumps({**valid, 'modules': {**valid['modules'], 'job_search/runtime.py': '0' * 64}}).encode(),
            json.dumps({**valid, 'modules': {'job_search/runtime_readiness.py': True}}).encode()]
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                changed = {**files, AUDIT_PATH: declaration}
                self.assertFalse(reviewed_backend(changed, declaration))
                self.assertNotEqual(original, fingerprint(changed))

    def test_protocol_is_read_from_immutable_source_not_presence_alone(self):
        self.assertEqual(source_protocol({}), 0)
        base = {'job_search/schema_lifecycle.py': b'boundary'}
        self.assertEqual(source_protocol(base), 1)
        for content, expected in ((b'PROTOCOL = 2', 2), (b'PROTOCOL = 3', 3),
                                  (b'PROTOCOL = True', 1), (b'not python !', 1)):
            self.assertEqual(source_protocol({**base, 'job_search/deployment_contract.py': content}), expected)

    def test_audit_and_protocol_ignore_uncommitted_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository(root)
            for path, content in self.reviewed().items():
                (root / path).write_bytes(content)
            (root / 'job_search/schema_lifecycle.py').write_text('boundary')
            (root / 'job_search/deployment_contract.py').write_text('PROTOCOL = 2')
            git(root, 'add', '.')
            git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'review')
            source = git(root, 'rev-parse', 'HEAD')
            original = from_commit(root, source)
            self.assertTrue(audit_current_from_commit(root, source))
            self.assertEqual(protocol_from_commit(root, source), 2)
            (root / 'job_search/runtime_readiness.py').write_text('dirty unreviewed diagnostics')
            self.assertTrue(audit_current_from_commit(root, source))
            self.assertEqual(from_commit(root, source), original)
            git(root, 'add', '.')
            git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'stale audit')
            stale = git(root, 'rev-parse', 'HEAD')
            self.assertFalse(audit_current_from_commit(root, stale))
            self.assertNotEqual(from_commit(root, stale), original)

    def test_commit_contract_ignores_dirty_worktree_and_changes_when_runtime_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = repository(root)
            original = from_commit(root, source)
            (root / 'job_search/worker.py').write_text('dirty runtime')
            self.assertEqual(original, from_commit(root, source))
            git(root, 'add', 'job_search/worker.py')
            git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'changed')
            self.assertNotEqual(original, from_commit(root, git(root, 'rev-parse', 'HEAD')))


if __name__ == '__main__':
    unittest.main()
