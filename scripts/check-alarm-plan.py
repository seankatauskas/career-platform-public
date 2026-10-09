#!/usr/bin/env python3
"""Reject an alarm rollout plan that could mutate any other infrastructure."""
import argparse
import json
import re
import sys


ALARMS = {f'aws_cloudwatch_metric_alarm.operations["{key}"]' for key in ('health', 'domain')}


def check_plan(plan, instance_id):
    if not re.fullmatch(r'i-[0-9a-f]{8,17}', instance_id):
        raise ValueError('expected the configured production instance ID')
    changes = plan.get('resource_changes')
    if not isinstance(changes, list) or plan.get('errored'):
        raise ValueError('missing or failed Terraform plan')
    found = set()
    for resource in changes:
        address = resource['address']
        change = resource['change']
        actions = change['actions']
        if address in ALARMS:
            found.add(address)
            if actions not in (['no-op'], ['update']):
                raise ValueError('alarm rollout must update existing alarms only')
            before, after = change['before'], change['after']
            for key in ('id', 'alarm_name', 'alarm_actions', 'ok_actions',
                        'actions_enabled', 'comparison_operator', 'threshold',
                        'evaluation_periods', 'treat_missing_data'):
                if before.get(key) != after.get(key):
                    raise ValueError('alarm identity, delivery, or threshold changed: ' + key)
            metrics = [q['metric'][0] for q in after['metric_query'] if q.get('metric')]
            if len(metrics) != 2 or any(m['dimensions'] != {'InstanceId': instance_id} for m in metrics):
                raise ValueError('alarm metrics must retain the installed instance')
            if before.get('dimensions') not in (None, {}, {'InstanceId': instance_id}):
                raise ValueError('existing alarm targets another instance')
        elif resource.get('mode') == 'data' and actions in (['read'], ['no-op']):
            continue
        elif actions != ['no-op']:
            raise ValueError('plan changes infrastructure outside the alarm scope: ' + address)
    if found != ALARMS:
        raise ValueError('both existing health alarms must be present in the plan')
    return {'scope': 'health-alarms', 'instance_id': instance_id, 'alarms': sorted(found)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-id', required=True)
    args = parser.parse_args()
    print(json.dumps(check_plan(json.load(sys.stdin), args.instance_id)))


if __name__ == '__main__':
    main()
