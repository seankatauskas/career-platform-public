"""Compatible release selection and data-preserving interruption recovery."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from job_search import aws_ops as ops
from job_search.operation_journal import read, write_json
from job_search.release_policy import check_transition, deployment_mode, validate_policy
from tests import test_job_search_aws_ops as fixtures


class DeploymentModesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OperationsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.data = self.fixture.c, self.fixture.data
        self.previous = ops.release_path(self.c)
        self.target = self.fixture.install_candidate()
        for path in (self.previous, self.target):
            m = ops.manifest(path)
            m.update(startup_schema_protocol=2, runtime_contract_sha256='c' * 64)
            if path == self.target:
                m['release_policy']['deployment_mode'] = 'compatible'
                m['transition_validation']['compatible_deployment'] = {
                    'protocol': 2, 'passed': True, 'baseline_startup_read_only': True,
                    'baseline_diagnostics_read_only': True, 'candidate_diagnostics_read_only': True,
                    'candidate_startup_read_only': True, 'predecessor_after_candidate_writes': True,
                    'runtime_contract_sha256': 'c' * 64}
            (path / 'release.json').write_text(json.dumps(m))
        write_json(self.data / 'materialized-secrets.json', {})
        write_json(self.data / 'release-state.json', {'current': self.previous.name, 'backup': {'backup_id': 'keep'}})
        self.fixture.original_data()

    def mocks(self, stack, *, active=()):
        stack.enter_context(patch.object(ops, 'running_services', return_value=list(active)))
        stack.enter_context(patch.object(ops, 'preflight', return_value={'issues': []}))
        stack.enter_context(patch.object(ops, 'prune_local_images', return_value={'status': 'ok'}))
        stack.enter_context(patch('job_search.review_host.prepare_compatible', create=True))
        for name in ('prepare_snapshot', 'backup_unlocked', 'restore_unlocked', 'materialize_secrets', 'chown_runtime', 'available_space', 'prune_local_backups'):
            stack.enter_context(patch.object(ops, name, side_effect=AssertionError(name + ' must not run')))

    def test_compatible_deployment_retains_data_secrets_and_backups(self):
        with ExitStack() as stack:
            self.mocks(stack, active=['dashboard'])
            compose = stack.enter_context(patch.object(ops, 'compose', return_value=''))
            result = ops.deploy(self.c, self.target.name)
        self.assertEqual(result['deployment_mode'], 'compatible')
        self.assertEqual(result['deployment_mode_reason'], 'tested_compatible_edge')
        self.assertIsNone(result['backup'])
        self.assertEqual(result['local_backup_retention'], 'unchanged_no_snapshot')
        self.assertEqual(ops.release_path(self.c), self.target)
        self.fixture.assert_original_data()
        calls = [call.args[1:] for call in compose.call_args_list]
        self.assertEqual(sum('schema-check' in args for args in calls), 2)
        self.assertFalse(any('initialize' in args for args in calls))
        self.assertTrue(read(self.c)['writes_possible'])

    def test_prewrite_validation_failure_restores_only_release(self):
        def compose(c, *args, **kwargs):
            if args[:2] == ('up', '-d') and args[-1] == 'tools':
                (self.data / 'state' / 'original.txt').write_text('latest retained data')
                raise ops.OpsError('candidate tools failed')
            return ''
        with ExitStack() as stack:
            self.mocks(stack)
            stack.enter_context(patch.object(ops, 'compose', side_effect=compose))
            with self.assertRaisesRegex(ops.OpsError, 'tools failed'):
                ops.deploy(self.c, self.target.name)
        self.assertEqual(ops.release_path(self.c), self.previous)
        self.assertEqual((self.data / 'state' / 'original.txt').read_text(), 'latest retained data')
        self.assertEqual(json.loads((self.data / 'release-state.json').read_text())['backup']['backup_id'], 'keep')
        self.assertEqual(read(self.c)['phase'], 'deployment_reverted')

    def test_postwrite_failure_preserves_new_release_and_new_data(self):
        def healthy(c, services):
            if services == ['dashboard']:
                (self.data / 'state' / 'original.txt').write_text('new user work')
                raise ops.OpsError('startup unhealthy')
        with ExitStack() as stack:
            self.mocks(stack, active=['dashboard'])
            stack.enter_context(patch.object(ops, 'compose', return_value=''))
            stack.enter_context(patch.object(ops, 'wait_healthy', side_effect=healthy))
            stack.enter_context(patch.object(ops, 'pause'))
            with self.assertRaisesRegex(ops.OpsError, 'startup unhealthy'):
                ops.deploy(self.c, self.target.name)
            operation = read(self.c)
            self.assertTrue(operation['writes_possible'])
            result = ops.recover(self.c, operation['operation_id'])
        self.assertEqual(result['status'], 'recovered_paused')
        self.assertEqual(ops.release_path(self.c), self.target)
        self.assertEqual((self.data / 'state' / 'original.txt').read_text(), 'new user work')

    def test_gate_failure_at_first_resume_keeps_target_and_data(self):
        original = ops.set_gate
        def gate(c, allowed, **kwargs):
            if allowed == ['dashboard']:
                raise ops.OpsError('resume gate failed')
            return original(c, allowed, **kwargs)
        with ExitStack() as stack:
            self.mocks(stack, active=['dashboard'])
            stack.enter_context(patch.object(ops, 'compose', return_value=''))
            stack.enter_context(patch.object(ops, 'set_gate', side_effect=gate))
            stack.enter_context(patch.object(ops, 'pause'))
            with self.assertRaisesRegex(ops.OpsError, 'resume gate failed'):
                ops.deploy(self.c, self.target.name)
            operation = read(self.c)
            self.assertTrue(operation['writes_possible'])
            ops.recover(self.c, operation['operation_id'])
        self.assertEqual(ops.release_path(self.c), self.target)
        self.fixture.assert_original_data()

    def test_stopped_schema_failure_never_initializes_or_restores_data(self):
        def compose(c, *args, **kwargs):
            if '--offline' in args:
                raise ops.OpsError('uncheckpointed WAL')
            return ''
        with ExitStack() as stack:
            self.mocks(stack)
            stack.enter_context(patch.object(ops, 'compose', side_effect=compose))
            with self.assertRaisesRegex(ops.OpsError, 'uncheckpointed WAL'):
                ops.deploy(self.c, self.target.name)
        self.assertEqual(ops.release_path(self.c), self.previous)
        self.assertEqual(read(self.c)['phase'], 'deployment_reverted')
        self.fixture.assert_original_data()

    def test_recovery_refuses_changed_secret_inventory_without_replacing_data(self):
        from job_search.operation_journal import Operation
        op = Operation.begin(self.c, 'deploy', deployment_mode='compatible',
                             previous_release=self.previous.name, target_release=self.target.name,
                             retained_secret_versions={'unexpected': {}}, previous_release_state=None,
                             state_mutation_started=False)
        ops.point_current(self.c, self.target)
        with patch.object(ops, 'restore_unlocked', side_effect=AssertionError('must preserve data')):
            with self.assertRaisesRegex(ops.OpsError, 'secret versions changed'):
                ops.recover(self.c, op.id)
        self.assertFalse(read(self.c)['complete'])
        self.assertEqual(ops.release_path(self.c), self.target)
        self.fixture.assert_original_data()

    def test_process_death_after_pointer_switch_recovers_without_data_restore(self):
        program = '''
import json, os, signal, sys
from contextlib import ExitStack
from unittest.mock import patch
from job_search import aws_ops as ops
c=json.loads(sys.argv[1]); target=sys.argv[2]
original=ops.point_current
def switch(c, path):
    original(c,path)
    if path and path.name == target:
        os.kill(os.getpid(), signal.SIGKILL)
with ExitStack() as stack:
    for name in ('drain_workers','stop_project','wait_healthy'):
        stack.enter_context(patch.object(ops,name))
    for name in ('running_services','project_containers'):
        stack.enter_context(patch.object(ops,name,return_value=[]))
    stack.enter_context(patch.object(ops,'preflight',return_value={'issues':[]}))
    stack.enter_context(patch.object(ops,'compose',return_value=''))
    stack.enter_context(patch('job_search.review_host.prepare_compatible',create=True))
    stack.enter_context(patch.object(ops,'point_current',side_effect=switch))
    ops.deploy(c,target)
'''
        result = subprocess.run([sys.executable, '-c', program, json.dumps(self.c), self.target.name], capture_output=True)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
        operation = read(self.c)
        self.assertFalse(operation['complete'])
        self.assertEqual(ops.release_path(self.c), self.target)
        with patch.object(ops, 'restore_unlocked', side_effect=AssertionError('must preserve data')):
            ops.recover(self.c, operation['operation_id'])
        self.assertEqual(ops.release_path(self.c), self.previous)
        self.fixture.assert_original_data()
        self.assertFalse(json.loads((self.data / 'activation.json').read_text())['enabled'])

    def test_eligibility_falls_back_for_unproven_or_changed_contract(self):
        old, new = ops.manifest(self.previous), ops.manifest(self.target)
        check_transition(new, old)
        self.assertEqual(deployment_mode(new, old)[0], 'compatible')
        self.assertEqual(deployment_mode(old, new, rollback=True)[0], 'compatible')
        for which, key, replacement in (
            ('old', 'startup_schema_protocol', None), ('old', 'startup_schema_protocol', 1),
            ('new', 'startup_schema_protocol', 1), ('new', 'runtime_contract_sha256', 'd'*64),
            ('new', 'tectonic_version', 'changed'), ('new', 'hermes_base_image', 'changed'),
            ('new', 'transition_validation', {**new['transition_validation'], 'compatible_deployment': {}}),
        ):
            with self.subTest(key=key):
                before, after = dict(old), dict(new)
                (before if which == 'old' else after)[key] = replacement
                self.assertEqual(deployment_mode(after, before)[0], 'migration')
        for field in ('passed', 'baseline_startup_read_only', 'candidate_startup_read_only', 'baseline_diagnostics_read_only',
                      'candidate_diagnostics_read_only', 'predecessor_after_candidate_writes'):
            after = json.loads(json.dumps(new))
            after['transition_validation']['compatible_deployment'][field] = False
            self.assertEqual(deployment_mode(after, old)[0], 'migration')
        for invalid_protocol in (None, True, 1, 3):
            after = json.loads(json.dumps(new))
            after['transition_validation']['compatible_deployment']['protocol'] = invalid_protocol
            self.assertEqual(deployment_mode(after, old)[0], 'migration')
        for field in ('source_sha', 'baseline_sha'):
            after = json.loads(json.dumps(new))
            after['transition_validation'][field] = 'f' * 40
            with self.assertRaises(ops.OpsError):
                check_transition(after, old)
        self.assertEqual(deployment_mode(new, None), ('migration', 'first_install'))
        with self.assertRaisesRegex(ValueError, 'mode'):
            validate_policy({**new['release_policy'], 'deployment_mode': 'skip'})
        with self.assertRaisesRegex(ops.OpsError, 'differs'):
            check_transition(new, {**old, 'release_id': 'a'*40+'-9'})

    def test_schema_probe_failure_falls_back_before_operation(self):
        def compose(c, *args, **kwargs):
            if 'schema-check' in args:
                raise ops.OpsError('missing schema')
            return ''
        with patch.object(ops, 'compose', side_effect=compose), patch.object(ops, 'preflight', return_value={'issues': []}), patch.object(ops, 'running_services', return_value=[]), patch.object(ops, 'available_space') as space, patch.object(ops, 'install_prepared', return_value={'timings_seconds': {}}) as install:
            ops.deploy(self.c, self.target.name)
        self.assertEqual(install.call_args.kwargs['mode'], 'migration')
        self.assertEqual(install.call_args.kwargs['mode_reason'], 'installed_state_requires_migration')
        space.assert_called_once_with(self.c, local_snapshot=True)
        self.assertIsNone(read(self.c))


if __name__ == '__main__':
    unittest.main()
