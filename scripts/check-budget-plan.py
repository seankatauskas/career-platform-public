#!/usr/bin/env python3
"""Allow only exclusion of credits/refunds from the existing monthly budget."""
import json
import sys


ADDRESS = 'aws_budgets_budget.monthly'
FLAGS = {'include_credit', 'include_refund'}


def has_unknown(value):
    if isinstance(value, dict):
        return any(has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(has_unknown(item) for item in value)
    return value is True


def check_plan(plan):
    changes = plan.get('resource_changes')
    if not isinstance(changes, list) or plan.get('errored'):
        raise ValueError('missing or failed Terraform plan')
    found = False
    for resource in changes:
        address, change = resource['address'], resource['change']
        actions = change['actions']
        if address == ADDRESS:
            if found or resource.get('mode') != 'managed':
                raise ValueError('expected one managed monthly budget')
            found = True
            if actions not in (['no-op'], ['update']) or has_unknown(change.get('after_unknown', {})):
                raise ValueError('budget must be an existing update with fully known values')
            before, after = change['before'], change['after']
            if not isinstance(before, dict) or not isinstance(after, dict) or not before.get('id'):
                raise ValueError('missing existing budget identity')
            if ({k: v for k, v in before.items() if k != 'cost_types'} !=
                    {k: v for k, v in after.items() if k != 'cost_types'}):
                raise ValueError('budget identity, limit, notifications and filters must remain unchanged')
            old_costs, new_costs = before.get('cost_types'), after.get('cost_types')
            if (not isinstance(old_costs, list) or not isinstance(new_costs, list)
                    or len(old_costs) != 1 or len(new_costs) != 1
                    or not isinstance(old_costs[0], dict) or not isinstance(new_costs[0], dict)):
                raise ValueError('expected one fully specified cost type configuration')
            old, new = old_costs[0], new_costs[0]
            if any(new.get(key) is not False for key in FLAGS):
                raise ValueError('credits and refunds must be excluded')
            if ({k: v for k, v in old.items() if k not in FLAGS} !=
                    {k: v for k, v in new.items() if k not in FLAGS}):
                raise ValueError('only credit and refund inclusion may change')
        elif resource.get('mode') == 'data' and actions in (['read'], ['no-op']):
            continue
        elif actions != ['no-op']:
            raise ValueError('plan changes infrastructure outside the budget scope: ' + address)
    if not found:
        raise ValueError('existing monthly budget missing from plan')
    return {'scope': 'monthly-budget', 'budget': ADDRESS, 'include_credit': False, 'include_refund': False}


if __name__ == '__main__':
    print(json.dumps(check_plan(json.load(sys.stdin))))
