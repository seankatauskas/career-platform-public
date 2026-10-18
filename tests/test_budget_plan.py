"""Bounded cost-basis changes cannot replace resources or weaken alerting."""
import copy
from pathlib import Path
import runpy
import unittest

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/check-budget-plan.py'))['check_plan']


def fixture():
    before = {'id': 'account:monthly', 'name': 'monthly', 'limit_amount': '100',
              'notification': [{'threshold': 80, 'subscriber_email_addresses': ['owner@example.com']}],
              'cost_types': [{'include_credit': True, 'include_refund': True, 'include_tax': True}]}
    after = copy.deepcopy(before)
    after['cost_types'][0].update(include_credit=False, include_refund=False)
    return {'resource_changes': [{'address': 'aws_budgets_budget.monthly', 'mode': 'managed',
                                 'change': {'actions': ['update'], 'before': before, 'after': after}}]}


class BudgetPlanTests(unittest.TestCase):
    def test_update_and_idempotent_plan(self):
        plan = fixture()
        self.assertEqual(CHECK(plan)['scope'], 'monthly-budget')
        change = plan['resource_changes'][0]['change']
        change.update(actions=['no-op'], before=copy.deepcopy(change['after']))
        CHECK(plan)

    def test_other_changes_and_replacements_rejected(self):
        for actions in (['update'], ['create'], ['delete'], ['delete', 'create']):
            plan = fixture()
            plan['resource_changes'].append({'address': 'aws_instance.host', 'mode': 'managed',
                                            'change': {'actions': actions}})
            with self.assertRaises(ValueError): CHECK(plan)
        for actions in (['create'], ['delete'], ['delete', 'create']):
            plan = fixture()
            plan['resource_changes'][0]['change']['actions'] = actions
            with self.assertRaises(ValueError): CHECK(plan)

    def test_budget_scope_and_delivery_unchanged(self):
        for key, value in (('id', 'other'), ('limit_amount', '200'), ('notification', []),
                           ('cost_filter', [{'name': 'Service', 'values': ['EC2']}])):
            plan = fixture()
            plan['resource_changes'][0]['change']['after'][key] = value
            with self.assertRaises(ValueError): CHECK(plan)

    def test_other_cost_types_and_unknown_values_rejected(self):
        for key, value in (('include_tax', False), ('include_credit', True), ('include_refund', None)):
            plan = fixture()
            plan['resource_changes'][0]['change']['after']['cost_types'][0][key] = value
            with self.assertRaises(ValueError): CHECK(plan)
        plan = fixture()
        plan['resource_changes'][0]['change']['after_unknown'] = {'notification': [True]}
        with self.assertRaises(ValueError): CHECK(plan)

    def test_missing_failed_or_duplicate_budget_rejected(self):
        for plan in ({}, {'resource_changes': []}, {'errored': True, **fixture()}):
            with self.assertRaises(ValueError): CHECK(plan)
        plan = fixture()
        plan['resource_changes'] *= 2
        with self.assertRaises(ValueError): CHECK(plan)


if __name__ == '__main__':
    unittest.main()
