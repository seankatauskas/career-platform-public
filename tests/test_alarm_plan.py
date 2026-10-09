"""A targeted Terraform rollout must never apply dependency changes."""
import copy
from pathlib import Path
import runpy
import unittest

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/check-alarm-plan.py'))['check_plan']
INSTANCE = 'i-0fa677e47dee0134c'


def fixture():
    resources = []
    for key in ('health', 'domain'):
        before = {'id': key, 'alarm_name': key, 'dimensions': {'InstanceId': INSTANCE},
                  'alarm_actions': ['alerts'], 'ok_actions': ['alerts'], 'threshold': 1}
        after = copy.deepcopy(before)
        after['dimensions'] = None
        after['metric_query'] = [{'metric': [{'dimensions': {'InstanceId': INSTANCE}}]} for _ in range(2)]
        resources.append({'address': f'aws_cloudwatch_metric_alarm.operations["{key}"]',
                          'mode': 'managed', 'change': {'actions': ['update'], 'before': before, 'after': after}})
    return {'resource_changes': resources}


class AlarmPlanTests(unittest.TestCase):
    def test_two_updates_and_unchanged_dependencies_pass(self):
        plan = fixture()
        plan['resource_changes'].append({'address': 'aws_instance.host', 'mode': 'managed', 'change': {'actions': ['no-op']}})
        self.assertEqual(CHECK(plan, INSTANCE)['scope'], 'health-alarms')

    def test_dependency_changes_are_rejected(self):
        for address in ('aws_instance.host', 'aws_iam_role.host', 'aws_sns_topic.alerts'):
            for actions in (['update'], ['create'], ['delete'], ['delete', 'create']):
                with self.subTest(address=address, actions=actions):
                    plan = fixture()
                    plan['resource_changes'].append({'address': address, 'mode': 'managed', 'change': {'actions': actions}})
                    with self.assertRaises(ValueError): CHECK(plan, INSTANCE)

    def test_alarm_creation_and_replacement_are_rejected(self):
        for actions in (['create'], ['delete'], ['delete', 'create']):
            plan = fixture()
            plan['resource_changes'][0]['change']['actions'] = actions
            with self.assertRaises(ValueError): CHECK(plan, INSTANCE)

    def test_changed_notification_threshold_or_identity_is_rejected(self):
        for key, value in (('alarm_actions', []), ('threshold', 2), ('alarm_name', 'other')):
            plan = fixture()
            plan['resource_changes'][0]['change']['after'][key] = value
            with self.assertRaises(ValueError): CHECK(plan, INSTANCE)

    def test_changed_instance_is_rejected(self):
        plan = fixture()
        plan['resource_changes'][0]['change']['after']['metric_query'][0]['metric'][0]['dimensions']['InstanceId'] = 'i-1234567890abcdef0'
        with self.assertRaises(ValueError): CHECK(plan, INSTANCE)

    def test_missing_alarm_or_failed_plan_is_rejected(self):
        for plan in ({}, {'errored': True, **fixture()}, {'resource_changes': fixture()['resource_changes'][:1]}):
            with self.assertRaises(ValueError): CHECK(plan, INSTANCE)


if __name__ == '__main__':
    unittest.main()
