"""Offline lifecycle and release checks for the trusted review coordinator."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from job_search import aws_ops
from job_search.operation_journal import OpsError
from job_search import review_host as host

IMAGE = '123456789012.dkr.ecr.us-east-2.amazonaws.com/career/app@sha256:' + 'a' * 64
REVIEW_IMAGE = IMAGE[:-64] + 'b' * 64


class ReviewHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'data'
        self.release_root = self.root / 'releases'
        self.release = self.release_root / 'releases' / ('a' * 40 + '-1')
        self.release.mkdir(parents=True)
        self.data.mkdir()
        self.c = {'data_root': str(self.data), 'release_root': str(self.release_root)}
        self.m = {'reviewer_image': REVIEW_IMAGE, 'app_image': IMAGE, 'release_id': self.release.name}
        self.source = {'version': 1, 'resume_mode': 'standard',
                       **{key: '/var/lib/job-search/' + key for key in
                          ('application_db', 'jobs_db', 'preference_db', 'proxy_db',
                           'resume_lab_db', 'resume_artifact_root')},
                       'inference_config': '/run/job-search/inference.json',
                       'outlook_enabled': True, 'api_key': 'DO-NOT-COPY-PRIVATE-SETTINGS'}

    def test_projection_maps_only_evidence_paths_and_keeps_source_unchanged(self):
        before = json.dumps(self.source)
        result = host.application_projection(self.source, self.data, self.release)
        self.assertEqual(result['jobs_db'], str(self.data / 'state/jobs_db'))
        self.assertEqual(result['project_root'], str(self.release))
        self.assertNotIn('inference_config', result)
        self.assertNotIn('outlook_enabled', result)
        self.assertNotIn('api_key', result)
        self.assertEqual(json.dumps(self.source), before)

    def test_projection_rejects_escapes_relative_paths_and_symlinks(self):
        for path in ('/etc/passwd', '../jobs.db', '/var/lib/job-search/../private', '/var/lib/job-search'):
            with self.subTest(path=path), self.assertRaises(OpsError):
                host.application_projection({**self.source, 'jobs_db': path}, self.data, self.release)
        (self.data / 'state').mkdir()
        (self.data / 'state/jobs_db').symlink_to(self.root / 'outside')
        with self.assertRaises(OpsError):
            host.application_projection(self.source, self.data, self.release)

    def test_native_export_checks_digest_version_and_reuses_immutable_binary(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:2] == ['docker', 'create']:
                return 'c' * 64
            if args[:2] == ['docker', 'cp']:
                stage = Path(args[-1])
                (stage / 'bin').mkdir()
                (stage / 'bin/codex').write_bytes(b'fixture-codex')
                (stage / 'SHA256SUMS').write_text(hashlib.sha256(b'fixture-codex').hexdigest() + '  bin/codex\n')
            if args[-1] == '--version':
                return 'codex-cli 0.160.0\n'
            return ''
        binary = host._native_codex(self.c, REVIEW_IMAGE, run)
        self.assertEqual(binary.read_bytes(), b'fixture-codex')
        self.assertTrue(binary.stat().st_mode & 0o111)
        self.assertEqual(host._native_codex(self.c, REVIEW_IMAGE, run), binary)
        self.assertEqual(sum(args[:2] == ['docker', 'create'] for args in calls), 1)
        binary.write_bytes(b'tampered')
        with self.assertRaises(OpsError):
            host._native_codex(self.c, REVIEW_IMAGE, run)

    def test_prepare_defaults_to_no_timer_and_does_not_modify_private_application_config(self):
        (self.data / 'private').mkdir()
        source = self.data / 'private/config.json'
        source.write_text(json.dumps(self.source))
        original = source.read_bytes()
        units = self.root / 'units'
        units.mkdir()
        (units / host.TIMER).write_text('old enabled timer')
        (self.release / 'deploy/aws').mkdir(parents=True)
        unit_source = Path(__file__).resolve().parents[1] / 'deploy/aws' / host.UNIT
        (self.release / 'deploy/aws' / host.UNIT).write_text(unit_source.read_text())
        run = Mock(return_value='')
        with patch.object(host, '_native_codex', return_value=Path('/verified/bin/codex')):
            result = host.prepare(self.c, self.release, self.m, run, systemd_root=units)
        self.assertFalse(result['schedule_enabled'])
        self.assertIsNone(result['schedule_calendar'])
        self.assertFalse((units / host.TIMER).exists())
        self.assertIn((['systemctl', 'disable', '--now', host.TIMER],), [c.args for c in run.call_args_list])
        runner = json.loads((self.data / 'operations/review-config' / self.release.name / 'runner.json').read_text())
        self.assertFalse(runner['schedule_enabled'])
        self.assertEqual(runner['model_image'], REVIEW_IMAGE)
        self.assertEqual(runner['model'], 'gpt-6-astra')
        self.assertEqual(runner['reasoning_effort'], 'high')
        self.assertEqual(runner['batch_size'], 20)
        self.assertEqual(runner['concurrency'], 2)
        self.assertEqual(runner['assignment_timeout_seconds'], 1200)
        self.assertEqual(runner['invocation_timeout_seconds'], 3600)
        self.assertTrue(runner['preload_enabled'])
        self.assertFalse(runner['screening_enabled'])
        self.assertFalse(runner['benchmark_check_all'])
        self.assertIsNone(runner['screening_model'])
        self.assertIsNone(runner['screening_reasoning_effort'])
        self.assertEqual(runner['screening_batch_size'], 200)
        self.assertEqual(source.read_bytes(), original)
        self.assertNotIn('DO-NOT-COPY', json.dumps(runner))
        self.assertTrue((self.data / 'operations/review-auth').is_dir())

    def test_prepare_requires_existing_application_repository(self):
        with self.assertRaises(OpsError):
            host.prepare(self.c, self.release, {**self.m, 'reviewer_image': REVIEW_IMAGE.replace('/career/app', '/other')}, Mock())

    def test_busy_runner_blocks_maintenance_and_only_labeled_workers_stop(self):
        state = self.data / 'state/review-runner'
        state.mkdir(parents=True)
        lock = state / 'runner.lock'
        with lock.open('w') as stream:
            lock.chmod(0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(host.runner_busy(self.c))
            with self.assertRaises(OpsError):
                host.drain(self.c, Mock(), timeout=0)
        run = Mock(side_effect=['abcdef123456\n', '', ''])
        host.drain(self.c, run)
        self.assertEqual(run.call_args_list[0].args[0][3], 'label=' + host.WORKER_LABEL)
        self.assertEqual(run.call_args_list[1].args[0], ['docker', 'stop', '--time', '30', 'abcdef123456'])
        self.assertFalse(host.runner_busy(self.c))

    def test_status_never_includes_errors_prompts_or_credentials(self):
        path = self.data / 'state/review-runner/status.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'status': 'failed', 'run_id': 'run-1', 'error': 'SECRET',
                                    'prompt': 'PRIVATE', 'token': 'TOKEN', 'assessments': ['PRIVATE']}))
        result = host.status(self.c)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('TOKEN', json.dumps(result))

    def test_gate_permits_manual_reviewer_only_outside_maintenance(self):
        with patch.object(aws_ops, 'release_path', return_value=self.release), patch.object(aws_ops, 'manifest', return_value=self.m):
            aws_ops.set_gate(self.c, ['dashboard'])
            value = json.loads((self.data / 'maintenance/gate.json').read_text())
            self.assertEqual(value['allowed_services'], ['dashboard', 'review-runner'])
            aws_ops.set_gate(self.c, ['dashboard'], draining=True)
            value = json.loads((self.data / 'maintenance/gate.json').read_text())
            self.assertEqual(value['allowed_services'], ['dashboard'])
            self.assertTrue(value['draining'])

    def test_project_writers_include_isolated_workers(self):
        with patch.object(aws_ops, 'run', side_effect=['abcdef123456\n', 'bcdefa123456\n']):
            self.assertEqual(aws_ops.project_containers(), ['abcdef123456', 'bcdefa123456'])

    def test_pause_and_recovery_request_active_coordinator_drain_before_stopping(self):
        def assert_gate(c, run, **kwargs):
            value = json.loads((self.data / 'maintenance/gate.json').read_text())
            self.assertEqual(value['allowed_services'], [])
            self.assertTrue(value['draining'])
        with patch.object(host, 'drain', side_effect=assert_gate), patch.object(aws_ops, 'compose'), patch.object(aws_ops, 'running_services', return_value=[]):
            self.assertEqual(aws_ops.pause(self.c)['status'], 'paused')
        with patch.object(host, 'drain', side_effect=assert_gate), patch.object(aws_ops, 'project_containers', return_value=[]):
            aws_ops.stop_project(self.c)

    def test_manifest_rejects_unpinned_or_foreign_reviewer_image(self):
        base = {'version': 1, **self.m, 'hermes_image': IMAGE, 'hermes_base_image': IMAGE,
                'tectonic_version': '0.15.0'}
        for image in ('reviewer:latest', REVIEW_IMAGE.replace('/career/app', '/different')):
            (self.release / 'release.json').write_text(json.dumps({**base, 'reviewer_image': image}))
            with self.assertRaises(OpsError):
                aws_ops.manifest(self.release)


if __name__ == '__main__':
    unittest.main()
