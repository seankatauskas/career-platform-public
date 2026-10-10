"""Offline release coordination contracts; never contact AWS or GitHub."""
from __future__ import annotations

from copy import deepcopy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from job_search import release_coordinator as rc
from tests.test_prepared_release import fixture

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_status', ROOT / 'deploy/aws/release-status.py')
host_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_status)


class FakeCloud:
    instance = 'i-1234abcd'

    def __init__(self):
        self.manifest = {**fixture(), 'tectonic_version': '0.15.0'}
        self.policy = self.manifest['release_policy']
        self.settings = {'APP_REPOSITORY': self.manifest['app_image'].split('@')[0],
                         'HERMES_REPOSITORY': self.manifest['hermes_image'].split('@')[0],
                         'HERMES_BASE_IMAGE': self.manifest['hermes_base_image'], 'TECTONIC_VERSION': '0.15.0'}
        self.host_value = {'version': 1, 'state': 'idle', 'installed': self.policy['predecessor']}
        self.objects = {}
        self.writes = []
        self.receipt = self.upload(self.manifest)

    def upload(self, manifest):
        raw = rc.encoded(manifest)
        self.objects[f"releases/{manifest['release_id']}/manifest.json"] = raw
        return rc.verify(raw, manifest['release_id'], hashlib.sha256(raw).hexdigest())

    def host(self):
        return deepcopy(self.host_value)

    def get(self, key, *, optional=False):
        if optional:
            return self.objects.get(key)
        return self.objects[key]

    def put(self, key, value, *, immutable=False):
        raw = rc.encoded(value)
        if immutable and key in self.objects and self.objects[key] != raw:
            raise ValueError('immutable collision')
        self.objects[key] = raw
        self.writes.append(key)

    def select(self, number='9', source=None):
        return rc.select(self, source or self.manifest['source_sha'], number, self.policy, self.settings)


class CoordinationTests(unittest.TestCase):
    def test_workflow_status_includes_all_active_pages_and_deduplicates_transitions(self):
        run = {'id': 17, 'name': 'Deploy', 'status': 'in_progress', 'head_sha': 'a' * 40,
               'html_url': 'https://github.com/example/repo/actions/runs/17',
               'path': '.github/workflows/aws-deploy.yml'}
        unrelated = {**run, 'id': 18, 'path': '.github/workflows/ci.yml'}
        pages = [{'workflow_runs': [unrelated]}, {'workflow_runs': [run]}]
        with patch.object(rc.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(pages))) as command:
            result = rc.workflow_activity('example/repo')
        self.assertEqual(result['state'], 'observed')
        self.assertEqual([item['id'] for item in result['runs']], [17])
        self.assertEqual(command.call_count, 5)
        self.assertTrue(all('--paginate' in call.args[0] for call in command.call_args_list))

    def test_workflow_query_failure_preserves_host_status(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('gh', 30)):
            with self.subTest(error=error), patch.object(rc.subprocess, 'run', side_effect=error):
                result = rc.status(FakeCloud(), 'example/repo')
            self.assertEqual(result['host']['state'], 'idle')
            self.assertEqual(result['workflows'], {'state': 'unknown', 'runs': []})

    def test_queued_duplicate_reuses_exact_receipt_without_another_build(self):
        cloud = FakeCloud()
        first = cloud.select()
        self.assertEqual(first['action'], 'build')
        rc.publish(cloud, first, cloud.receipt)
        second = cloud.select('10')
        self.assertEqual(second['action'], 'reuse')
        self.assertEqual(second['receipt'], cloud.receipt)
        self.assertEqual(second['release_id'], first['release_id'])

    def test_new_commits_are_a_separate_candidate_and_do_not_change_frozen_selection(self):
        cloud = FakeCloud()
        first = cloud.select()
        rc.publish(cloud, first, cloud.receipt)
        later = cloud.select('10', source='b' * 40)
        self.assertEqual(later['action'], 'build')
        self.assertNotEqual(first['key'], later['key'])
        self.assertEqual(first['source_sha'], 'a' * 40)

    def test_production_change_blocks_selection_publication_and_deployment(self):
        cloud = FakeCloud()
        selected = cloud.select()
        cloud.host_value['installed'] = {'release_id': 'e' * 40 + '-10', 'source_sha': 'e' * 40}
        for action in (cloud.select, lambda: rc.publish(cloud, selected, cloud.receipt),
                       lambda: rc.check_deploy(cloud, cloud.receipt)):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'predecessor'):
                action()
        self.assertEqual(cloud.writes, [])

    def test_busy_unknown_and_recovering_hosts_cannot_start_work(self):
        for state in ('maintenance', 'recovery_required', 'unknown'):
            cloud = FakeCloud()
            cloud.host_value['state'] = state
            for action in (cloud.select, lambda: rc.check_deploy(cloud, cloud.receipt)):
                with self.subTest(state=state), self.assertRaises(ValueError):
                    action()
            self.assertEqual(cloud.writes, [])

    def test_already_installed_is_a_noop_but_changed_build_settings_are_rejected(self):
        cloud = FakeCloud()
        cloud.host_value['installed'] = {'release_id': cloud.receipt['release_id'], 'source_sha': 'a' * 40}
        self.assertEqual(cloud.select()['action'], 'already_installed')
        self.assertEqual(rc.check_deploy(cloud, cloud.receipt)['status'], 'already_installed')
        cloud.settings['TECTONIC_VERSION'] = 'different'
        with self.assertRaisesRegex(ValueError, 'build settings'):
            cloud.select()
        self.assertEqual(cloud.writes, [])

    def test_first_install_requires_explicit_null_predecessor(self):
        cloud = FakeCloud()
        cloud.host_value['installed'] = None
        with self.assertRaises(ValueError):
            cloud.select()
        cloud.policy['predecessor'] = None
        self.assertEqual(cloud.select()['action'], 'build')

    def test_corrupted_manifest_and_wrong_candidate_receipt_fail_closed(self):
        cloud = FakeCloud()
        selected = cloud.select()
        rc.publish(cloud, selected, cloud.receipt)
        key = f"releases/{cloud.receipt['release_id']}/manifest.json"
        cloud.objects[key] += b' '
        with self.assertRaisesRegex(ValueError, 'checksum'):
            cloud.select('10')
        cloud = FakeCloud()
        selected = cloud.select()
        other = {**cloud.manifest, 'release_id': 'f' * 40 + '-10', 'source_sha': 'f' * 40,
                 'transition_validation': {**cloud.manifest['transition_validation'], 'source_sha': 'f' * 40}}
        cloud.put(selected['key'], cloud.upload(other))
        with self.assertRaisesRegex(ValueError, 'selected source'):
            cloud.select()

    def test_retry_after_candidate_publication_repairs_missing_latest_pointer(self):
        cloud = FakeCloud()
        selected = cloud.select()
        cloud.put(selected['key'], cloud.receipt, immutable=True)
        self.assertEqual(cloud.select('10')['action'], 'reuse')
        self.assertIn(f'{rc.PREFIX}/{cloud.instance}/latest.json', cloud.objects)

    def test_settings_and_predecessor_are_part_of_deduplication_identity(self):
        cloud = FakeCloud()
        first = cloud.select()
        rc.publish(cloud, first, cloud.receipt)
        cloud.settings['TECTONIC_VERSION'] = '0.16.0'
        changed = cloud.select('10')
        self.assertEqual(changed['action'], 'build')
        self.assertNotEqual(first['key'], changed['key'])

    def test_status_distinguishes_installed_ready_stale_and_unknown(self):
        cloud = FakeCloud()
        rc.publish(cloud, cloud.select(), cloud.receipt)
        with patch.object(rc, 'workflow_activity', return_value={'state': 'observed', 'runs': []}):
            self.assertEqual(rc.status(cloud, 'owner/repo')['latest_prepared']['disposition'], 'ready')
            cloud.host_value['installed'] = {'release_id': cloud.receipt['release_id'], 'source_sha': 'a' * 40}
            self.assertEqual(rc.status(cloud, 'owner/repo')['latest_prepared']['disposition'], 'installed')
            cloud.host_value['installed'] = {'release_id': 'e' * 40 + '-1', 'source_sha': 'e' * 40}
            self.assertEqual(rc.status(cloud, 'owner/repo')['latest_prepared']['disposition'], 'stale')
            cloud.host_value.update(state='maintenance', installed=None)
            self.assertEqual(rc.status(cloud, 'owner/repo')['latest_prepared']['disposition'], 'unknown')

    def test_missing_record_is_distinct_from_denied_listing(self):
        cloud = rc.Cloud('bucket', 'i-abcd', 'status')
        with patch.object(cloud, 'aws', return_value={'Contents': []}):
            self.assertIsNone(cloud.get('releases/coordination/missing', optional=True))
        with patch.object(cloud, 'aws', side_effect=RuntimeError('AccessDenied')):
            with self.assertRaisesRegex(RuntimeError, 'AccessDenied'):
                cloud.get('releases/coordination/missing', optional=True)

    def test_host_poll_only_retries_eventual_visibility_and_rejects_unknown_outcome(self):
        cloud = rc.Cloud('bucket', 'i-abcd', 'fixed-status')
        with patch.object(cloud, 'aws', side_effect=[{'Command': {'CommandId': 'cmd'}},
                RuntimeError('InvocationDoesNotExist'), {'Status': 'InProgress'},
                {'Status': 'Success', 'StandardOutputContent': json.dumps(FakeCloud().host())}]), patch.object(rc.time, 'sleep'):
            self.assertEqual(cloud.host()['state'], 'idle')
        with patch.object(cloud, 'aws', side_effect=[{'Command': {'CommandId': 'cmd'}}, RuntimeError('AccessDenied')]):
            with self.assertRaisesRegex(RuntimeError, 'AccessDenied'):
                cloud.host()

    def test_cli_selection_emits_skip_outputs_for_a_second_request(self):
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, selection, receipt = root / 'policy.json', root / 'selection.json', root / 'receipt.json'
            policy.write_bytes(rc.encoded(cloud.policy))
            env = {**cloud.settings, 'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main',
                   'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_NUMBER': '9',
                   'GITHUB_OUTPUT': str(root / 'outputs'), 'GITHUB_ENV': str(root / 'env')}
            argv = ['coordinator', 'select', '--policy', str(policy), '--selection', str(selection), '--receipt', str(receipt)]
            with patch.object(rc, 'Cloud', return_value=cloud), patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, 'argv', argv), patch('sys.stdout', new_callable=io.StringIO), \
                    patch.object(rc.subprocess, 'check_output', side_effect=['a' * 40, '', 'a' * 40, '']):
                rc.main()
                self.assertEqual((root / 'outputs').read_text(), 'build=true\n')
                rc.publish(cloud, json.loads(selection.read_text()), cloud.receipt)
                os.environ['GITHUB_RUN_NUMBER'] = '10'
                rc.main()
                self.assertEqual((root / 'outputs').read_text(), 'build=true\nbuild=false\n')
                self.assertEqual(json.loads(receipt.read_text()), cloud.receipt)
                self.assertNotIn('-10', (root / 'env').read_text())

    def test_policy_proposal_preserves_compatibility_and_does_not_edit_source(self):
        cloud = FakeCloud()
        updated = {'release_id': 'e' * 40 + '-11', 'source_sha': 'e' * 40}
        cloud.host_value['installed'] = updated
        with tempfile.TemporaryDirectory() as directory:
            original, proposal = Path(directory) / 'policy.json', Path(directory) / 'proposal.json'
            original.write_bytes(rc.encoded(cloud.policy))
            argv = ['coordinator', 'propose-policy', '--policy', str(original), '--output', str(proposal)]
            with patch.object(rc, 'Cloud', return_value=cloud), patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, 'argv', argv), patch('sys.stdout', new_callable=io.StringIO):
                rc.main()
            self.assertEqual(json.loads(original.read_text()), cloud.policy)
            self.assertEqual(json.loads(proposal.read_text()), {**cloud.policy, 'predecessor': updated, 'test_baseline_sha': 'e' * 40})

    def test_cli_rejects_a_moving_or_dirty_checkout_before_cloud_access(self):
        cloud = FakeCloud()
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40}
        for head, dirty in [('b' * 40, ''), ('a' * 40, ' M job_search/service.py')]:
            with patch.object(rc, 'Cloud', return_value=cloud), patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, 'argv', ['coordinator', 'select']), patch('sys.stderr', new_callable=io.StringIO), \
                    patch.object(rc.subprocess, 'check_output', side_effect=[head, dirty]), patch.object(cloud, 'host') as host:
                with self.assertRaises(SystemExit):
                    rc.main()
                host.assert_not_called()

    def test_immutable_receipt_retry_requires_identical_bytes(self):
        cloud = rc.Cloud('bucket', 'i-abcd', 'status')
        value = {'a': 1}
        with patch.object(cloud, 'aws', side_effect=RuntimeError('PreconditionFailed')), \
                patch.object(cloud, 'get', return_value=rc.encoded(value)):
            cloud.put('key', value, immutable=True)
        with patch.object(cloud, 'aws', side_effect=RuntimeError('PreconditionFailed')), \
                patch.object(cloud, 'get', return_value=b'other'):
            with self.assertRaisesRegex(ValueError, 'another candidate'):
                cloud.put('key', value, immutable=True)

    def test_workflows_keep_manual_installation_and_serialized_builds(self):
        workflows = ROOT / '.github/workflows'
        # Public snapshots keep deployment workflows as inactive examples.
        # Still run every contract assertion against those exported sources.
        if not (workflows / 'aws-release.yml').exists():
            workflows = ROOT / '.github/workflow-examples'
        release = (workflows / 'aws-release.yml').read_text()
        deploy = (workflows / 'aws-deploy.yml').read_text()
        status = (workflows / 'aws-release-status.yml').read_text()
        self.assertIn('group: career-platform-release-build', release)
        self.assertIn('cancel-in-progress: false', release)
        self.assertIn("github.ref == 'refs/heads/main'", release)
        self.assertLess(release.index('release_coordinator select'), release.index('docker build'))
        self.assertNotIn('ssm send-command', release)
        self.assertNotIn('workflow_run:', deploy)
        self.assertIn('group: career-platform-production', deploy)
        self.assertLess(deploy.index('release_coordinator check-deploy'), deploy.index('ssm send-command'))
        self.assertNotIn('\nconcurrency:', status)
        self.assertIn("github.triggering_actor == 'seankatauskas'", status)

    def test_private_production_workflows_have_no_hosted_fallback(self):
        workflows = ROOT / '.github/workflows'
        if not (workflows / 'aws-release.yml').exists():
            workflows = ROOT / '.github/workflow-examples'
        for name in ('aws-release', 'aws-deploy', 'aws-release-status', 'aws-terraform'):
            with self.subTest(workflow=name):
                source = (workflows / (name + '.yml')).read_text()
                self.assertIn('runs-on: [self-hosted, Linux, ARM64, career-platform-linux]', source)
                self.assertNotIn('runs-on: ubuntu-', source)
                self.assertIn('environment: production', source)
                self.assertIn('id-token: write', source)
                self.assertIn("github.triggering_actor == 'seankatauskas'", source)
                self.assertIn('bash scripts/prepare-aws-runner.sh', source)
                self.assertEqual(source.count('uses: aws-actions/configure-aws-credentials@'),
                                 source.count('unset-current-credentials: true'))
                self.assertIn('if: always() && env.CAREER_RUNNER_ROOT', source)
                self.assertNotIn('$HOME/.docker/config.json', source)
        release = (workflows / 'aws-release.yml').read_text()
        self.assertIn('docker build --platform linux/amd64 --file Dockerfile.codex-review', release)
        self.assertIn('-B -m tests.probe_old_method_container', release)
        snapshot = (workflows / 'public-snapshot.yml').read_text()
        self.assertIn('runs-on: [self-hosted, macOS, ARM64, career-platform]', snapshot)
        self.assertNotIn('runs-on: ubuntu-', snapshot)


class HostStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.releases, self.data = self.root / 'app', self.root / 'data'
        self.releases.mkdir(); self.data.mkdir()

    def inspect(self):
        return host_status.inspect(self.releases, self.data)

    def install(self):
        manifest = fixture()
        target = self.releases / 'releases' / manifest['release_id']
        target.mkdir(parents=True)
        (target / 'release.json').write_text(json.dumps({**manifest, 'private': 'DO-NOT-OUTPUT'}))
        (self.releases / 'current').symlink_to(target)
        return manifest

    def test_first_install_and_installed_identity_are_read_only_and_allowlisted(self):
        self.assertEqual(self.inspect()['state'], 'idle')
        self.assertIsNone(self.inspect()['installed'])
        manifest = self.install()
        before = sorted(str(p) for p in self.root.rglob('*'))
        result = self.inspect()
        self.assertEqual(result['installed'], {key: manifest[key] for key in ('release_id', 'source_sha')})
        self.assertNotIn('DO-NOT-OUTPUT', json.dumps(result))
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_both_host_locks_prevent_reporting_an_unstable_release(self):
        self.install()
        for path in (self.releases / '.install.lock', self.data / '.operations.lock'):
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.inspect()
                self.assertEqual(result['state'], 'maintenance')
                self.assertIsNone(result['installed'])
            finally:
                os.close(fd)

    def test_interrupted_or_malformed_journal_and_dangling_release_fail_closed(self):
        journal = self.data / 'operations/current.json'
        journal.parent.mkdir()
        journal.write_text(json.dumps({'version': 1, 'operation_id': 'b' * 32, 'kind': 'deploy', 'complete': False}))
        self.assertEqual(self.inspect()['state'], 'recovery_required')
        journal.write_text('[]')
        self.assertEqual(self.inspect()['state'], 'unknown')
        journal.unlink()
        (self.releases / 'current').symlink_to(self.releases / 'releases/missing')
        self.assertEqual(self.inspect()['state'], 'unknown')

    def test_missing_root_and_symlink_lock_are_unknown_without_modification(self):
        self.assertEqual(host_status.inspect(self.root / 'missing', self.data)['state'], 'unknown')
        (self.releases / '.install.lock').symlink_to(self.root / 'missing')
        self.assertEqual(self.inspect()['state'], 'unknown')
        self.assertFalse((self.root / 'missing').exists())


if __name__ == '__main__':
    unittest.main()
