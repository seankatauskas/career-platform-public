"""Transaction-local resolution fast paths preserve evidence and audit semantics."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from job_search.contracts import ContractError, payload_sha256
from job_search.job_reviews.adjudication import resolved_items
from job_search.job_reviews.runner import ReviewCoordinator
from tests import test_review_adjudication as fixtures
from tests import test_review_runner as runner_fixtures


class ResolutionScanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdjudicationTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.authority, self.rid = self.fixture.a, self.fixture.rid

    def rows(self, con):
        run = self.authority.service._run(con, self.rid)
        items = con.execute('SELECT * FROM job_review_items WHERE review_id=? ORDER BY ordinal', (self.rid,)).fetchall()
        return run, items

    def test_absent_resolutions_skip_per_item_work_and_keep_exact_original_basis(self):
        with self.authority._transaction() as con, \
                patch('job_search.job_reviews.adjudication.current_resolution', side_effect=AssertionError('unneeded lookup')):
            run, items = self.rows(con)
            resolutions, effective = resolved_items(con, run, items)
            self.assertEqual(resolutions, [None])
            self.assertEqual(effective, [dict(item) for item in items])
            required, audited = self.authority.service._audit_membership(con, run, items)
            self.assertEqual(required, self.authority.service._audit(items, self.rid))
            self.assertEqual(audited, effective)
            basis, selected = self.authority.service._calibration_basis(con, run)
            fields = ('ordinal', 'revision', 'snapshot_sha256', 'assessment_json', 'check_json')
            expected = payload_sha256({'context': run['context_sha256'],
                                       'items': [[row[k] for k in fields] for row in items]})
            self.assertEqual(basis, expected)
            self.assertEqual(selected[1]['assessment_json'], items[0]['assessment_json'])

    def test_same_transaction_new_resolution_is_seen_and_receipts_still_validated(self):
        grant = self.fixture.issue()
        self.fixture.load(grant)
        entry = self.fixture.entry(grant)
        with self.authority._transaction() as con:
            run, items = self.rows(con)
            self.assertEqual(resolved_items(con, run, items)[0], [None])
            owner = self.authority._grant(con, grant['token'])
            self.authority._save_resolution(con, owner, run, items[0], entry)
            resolutions, effective = resolved_items(con, run, items)
            self.assertEqual(resolutions[0]['choice'], 'check')
            self.assertEqual(json.loads(effective[0]['assessment_json']), self.fixture.check)
            con.execute("DELETE FROM job_review_commands WHERE operation='resolve'")
            with self.assertRaisesRegex(ContractError, 'receipt'):
                resolved_items(con, run, items)
            with self.assertRaisesRegex(ContractError, 'receipt'):
                self.authority.service._calibration_basis(con, run)

    def test_stale_resolution_never_replaces_current_original(self):
        grant = self.fixture.issue()
        self.fixture.load(grant)
        self.fixture.save(grant, self.fixture.entry(grant))
        with self.authority._transaction() as con:
            run, items = self.rows(con)
            self.assertEqual(resolved_items(con, run, items)[0][0]['choice'], 'check')
            con.execute('UPDATE job_review_items SET revision=revision+1 WHERE review_id=?', (self.rid,))
            run, items = self.rows(con)
            resolutions, effective = resolved_items(con, run, items)
            self.assertEqual(resolutions, [None])
            self.assertEqual(effective[0]['assessment_json'], items[0]['assessment_json'])


class RefillScanTests(unittest.TestCase):
    def test_single_free_checker_slot_does_not_fill_whole_pool_again(self):
        fixture = runner_fixtures.RunnerTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        coordinator = ReviewCoordinator(replace(fixture.config, concurrency=32), fixture.authority,
                                        None, is_draining=lambda: False)
        # The fixed required set includes in-flight ordinals. Filtering is after
        # sampling; a free slot must neither duplicate them nor replace samples.
        calls = []
        required = list(range(1, 1601))
        def pending(_rid, *, after=0, limit=200):
            calls.append(after)
            available = [n for n in required if n > after]
            page = available[:limit]
            return {'ordinals': page, 'next_after': page[-1] if len(available) > limit else None}
        with patch.object(fixture.authority, 'pending_checks', side_effect=pending):
            result = coordinator._pending('synthetic', 'check', set(range(1, 621)), free_slots=1)
        self.assertEqual(result, list(range(621, 641)))
        self.assertEqual(calls, [0, 200, 400, 600])


if __name__ == '__main__':
    unittest.main()
