"""Offline staged selection, spend guards, scope isolation, and recall checks."""
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.collection.dedupe import prepare_families
from job_search.ranking import model, staged
from tests.test_preference_model import _source_db


class StagedRankingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.jobs, self.state = self.root / 'jobs.db', self.root / 'state.db'
        self.proxy, self.output = self.root / 'proxy.db', self.root / 'stage.db'
        _source_db(self.jobs)
        with sqlite3.connect(self.jobs) as con:
            con.execute('PRAGMA journal_mode=WAL')
            for i in range(2, 8):
                con.execute('INSERT INTO jobs(ats,id,company,title,description,last_seen) VALUES (?,?,?,?,?,?)',
                            ('ashby', str(i), 'Example', f'Engineer {i}', f'Build specific systems number {i}', '2026-08-01'))
        prepare_families(self.jobs)
        model.prepare_state(self.state)
        self.docs = model.load_family_documents(self.jobs)
        self.runs = {'broad': 'run_b', 'selective': 'run_s'}
        self.encoder = model.HashingEncoder(8)
        self.manifest = {'selection': {'weights': {'dense_linear': 1}}, 'labels': [{
            'family_id': 'training', 'feature_fingerprint': 'training_fp',
            'template_cluster_id': 'training_template', 'leakage_group_id': 'training_leakage'}]}
        with sqlite3.connect(self.state) as con:
            for run in self.runs.values():
                con.execute('INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)',
                            (run, '2026-08-01', self.encoder.model_revision, model.TEXT_VERSION, 1, json.dumps(self.manifest), ''))
            for index, doc in enumerate(self.docs):
                con.execute('INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?,?)',
                            ('run_b', doc.family_id, doc.fingerprint, 0, 0, index / 10, index / 10, '{}', '2026-08-01'))
            for policy, run in self.runs.items():
                con.execute('INSERT INTO preference_state VALUES (?,?)', ('policy_refresh:' + policy,
                    json.dumps({'run_id': run, 'source_watermark': '2026-08-01', 'result': {'scored_families': len(self.docs)}})))
            con.execute("INSERT INTO preference_state VALUES ('embedding_model_revision','old_active_revision')")
        self.stack.enter_context(patch.object(staged, 'policy_runs', return_value=self.runs))
        self.load = self.stack.enter_context(patch.object(model, '_load_artifact', return_value=(
            {'weights': {'dense_linear': 1}, 'model_revision': self.encoder.model_revision},
            {'manifest_json': json.dumps(self.manifest)},
        )))
        self.stack.enter_context(patch.object(model, '_optional_ml_modules', return_value={}))
        self.factory = self.stack.enter_context(patch.object(model, 'encoder_for_recorded_revision', return_value=self.encoder))
        def predict(state, documents, artifact, modules, **kwargs):
            model.load_combined_vectors(state, documents, artifact['model_revision'])
            return [{'final': .7, 'dense_linear': .7, 'computed_components': ['dense_linear']} for _ in documents]
        self.predict = self.stack.enter_context(patch.object(model, '_score_document_batch', side_effect=predict))

    def run_stage(self, **kwargs):
        return staged.stage_selective(self.jobs, self.state, self.proxy, **kwargs)

    def policy_state(self):
        with sqlite3.connect(self.state) as con:
            return (con.execute('SELECT * FROM preference_scores ORDER BY run_id,family_id').fetchall(),
                    con.execute('SELECT * FROM preference_state ORDER BY key').fetchall())

    def test_dry_run_is_read_only_and_deterministic_with_exploration(self):
        with sqlite3.connect(self.jobs) as con:
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = self.state.read_bytes(), self.jobs.read_bytes()
        a = self.run_stage(candidate_limit=3, exploration_count=1)
        b = self.run_stage(candidate_limit=3, exploration_count=1)
        self.assertEqual(a, b)
        self.assertEqual(a['selected_families'], 3)
        self.assertEqual(a['exploration_count'], 1)
        self.assertEqual(a['status'], 'planned')
        self.assertEqual(before, (self.state.read_bytes(), self.jobs.read_bytes()))
        self.factory.assert_not_called()
        self.load.assert_not_called()
        self.predict.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_both_budgets_fail_before_model_or_encoder_or_output(self):
        before = self.policy_state()
        for options in ({'max_missing_texts': 0}, {'max_missing_characters': 1}):
            result = self.run_stage(candidate_limit=2, **options)
            self.assertEqual(result['status'], 'budget_exceeded')
            with self.assertRaises(staged.BudgetExceeded):
                self.run_stage(candidate_limit=2, execute=True, stage_db=self.output, **options)
        self.assertEqual(before, self.policy_state())
        self.factory.assert_not_called()
        self.load.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_execute_stores_separate_scope_and_cached_rerun_needs_no_encoder(self):
        before = self.policy_state()
        a = self.run_stage(candidate_limit=3, execute=True, stage_db=self.output)
        self.assertEqual(a['status'], 'completed_candidates')
        self.assertGreater(a['submitted_texts'], 0)
        self.assertEqual(before, self.policy_state())
        with sqlite3.connect(self.output) as con:
            rows = con.execute('SELECT family_id,broad_score,selective_score FROM staged_scores ORDER BY broad_score DESC').fetchall()
            self.assertEqual([row[0] for row in rows], [doc.family_id for doc in reversed(self.docs[-3:])])
            self.assertEqual([row[2] for row in rows], [.7, .7, .7])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='preference_scores'").fetchone()[0], 0)
        self.factory.reset_mock()
        b = self.run_stage(candidate_limit=3, execute=True, stage_db=self.output, max_missing_texts=0, max_missing_characters=0)
        self.assertEqual(b['submitted_texts'], 0)
        self.factory.assert_not_called()
        self.assertEqual(before, self.policy_state())

    def test_changed_scores_or_grouping_block_before_inference(self):
        with sqlite3.connect(self.jobs) as con:
            con.execute("UPDATE jobs SET description='Changed without watermark' WHERE id='2'")
        with self.assertRaisesRegex(model.PreferenceModelError, 'stale or incomplete'):
            self.run_stage(execute=True, stage_db=self.output)
        prepare_families(self.jobs)
        with self.assertRaisesRegex(model.PreferenceModelError, 'missing or changed'):
            self.run_stage(execute=True, stage_db=self.output)
        self.factory.assert_not_called()

    def test_bad_receipt_incomplete_scores_and_wrong_output_are_blocked(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            self.run_stage(execute=True, stage_db=self.state)
        alias = self.root / 'hardlinked.db'
        os.link(self.state, alias)
        with self.assertRaisesRegex(ValueError, 'separate'):
            self.run_stage(execute=True, stage_db=alias)
        with sqlite3.connect(self.state) as con:
            con.execute("DELETE FROM preference_scores WHERE family_id=?", (self.docs[0].family_id,))
        with self.assertRaisesRegex(model.PreferenceModelError, 'missing or changed'):
            self.run_stage(execute=True, stage_db=self.output)
        with sqlite3.connect(self.state) as con:
            con.execute("DELETE FROM preference_state WHERE key='policy_refresh:broad'")
        with self.assertRaisesRegex(model.PreferenceModelError, 'receipt'):
            self.run_stage(execute=True, stage_db=self.output)
        self.factory.assert_not_called()

    def test_only_open_member_families_are_candidates_even_when_canonical_is_closed(self):
        with sqlite3.connect(self.jobs) as con:
            con.execute("UPDATE jobs SET closed_at='2026-08-02'")
            con.execute("UPDATE jobs SET description=? WHERE id='1'", ('Build reliable systems and review changes. ' * 20,))
            con.execute("INSERT INTO jobs(ats,id,company,title,description,last_seen,closed_at) "
                        "SELECT ats,'z-open-sibling',company,title,description,last_seen,NULL FROM jobs WHERE id='1'")
        prepare_families(self.jobs)
        documents = model.load_family_documents(self.jobs)
        with sqlite3.connect(self.jobs) as con:
            open_family = con.execute("SELECT family_id FROM job_family_members WHERE job_id='z-open-sibling'").fetchone()[0]
            self.assertEqual(con.execute('SELECT canonical_job_id FROM job_families WHERE family_id=?', (open_family,)).fetchone()[0], '1')
        with sqlite3.connect(self.state) as con:
            con.execute("DELETE FROM preference_scores WHERE run_id='run_b'")
            for doc in documents:
                con.execute('INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?,?)',
                            ('run_b', doc.family_id, doc.fingerprint, 0, 0, .5, .5, '{}', '2026-08-01'))
        for exploration in (0, 1):
            result = self.run_stage(candidate_limit=3, exploration_count=exploration, execute=True, stage_db=self.output)
            self.assertEqual(result['eligible_families'], 1)
            self.assertEqual(result['excluded_closed_families'], 6)
            self.assertEqual(result['candidate_fraction_of_eligible'], 1)
            with sqlite3.connect(self.output) as con:
                self.assertEqual(con.execute('SELECT family_id FROM staged_scores WHERE stage_id=?', (result['stage_id'],)).fetchall(), [(open_family,)])
        with sqlite3.connect(self.jobs) as con:
            con.execute("UPDATE jobs SET closed_at='2026-08-02'")
        result = self.run_stage(candidate_limit=3, exploration_count=1)
        self.assertEqual(result['selected_families'], 0)
        self.assertEqual(result['eligible_families'], 0)

    def test_preflight_releases_state_reads_for_concurrent_rollback_journal_writer(self):
        with sqlite3.connect(self.state) as con:
            self.assertEqual(con.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
        original = model.iter_family_document_batches
        writes = []
        def documents_with_progress_write(*args, **kwargs):
            for batch in original(*args, **kwargs):
                yield batch
                with sqlite3.connect(self.state, timeout=0) as writer:
                    writer.execute("INSERT OR REPLACE INTO preference_state VALUES ('policy_refresh_progress','fixture progress')")
                writes.append(True)
        with patch.object(model, 'iter_family_document_batches', side_effect=documents_with_progress_write):
            result = self.run_stage(candidate_limit=2)
        self.assertEqual(result['status'], 'planned')
        self.assertTrue(writes)
        self.factory.assert_not_called()

    def test_completed_broad_refresh_during_scan_blocks_before_encoder(self):
        original = model.iter_family_document_batches
        def documents_with_completed_refresh(*args, **kwargs):
            for batch in original(*args, **kwargs):
                yield batch
                with sqlite3.connect(self.state, timeout=0) as writer:
                    writer.execute("UPDATE preference_state SET value='{}' WHERE key='policy_refresh:broad'")
        with patch.object(model, 'iter_family_document_batches', side_effect=documents_with_completed_refresh):
            with self.assertRaisesRegex(model.PreferenceModelError, 'changed during preflight'):
                self.run_stage(candidate_limit=2, execute=True, stage_db=self.output)
        self.factory.assert_not_called()
        self.load.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_open_family_lookup_does_not_scan_unrelated_open_postings(self):
        with sqlite3.connect(self.jobs) as con:
            con.execute('CREATE INDEX jobs_closed_at ON jobs(closed_at)')
            con.executemany('INSERT INTO jobs(ats,id,closed_at) VALUES (?,?,NULL)',
                            [('ashby', 'unrelated' + str(i)) for i in range(5000)])
            steps = []
            con.set_progress_handler(lambda: steps.append(1) or 0, 1)
            ids = [self.docs[0].family_id]
            self.assertEqual(staged._open_family_ids(con, ids), set(ids))
            con.set_progress_handler(None, 0)
            # A catalog scan takes tens of thousands of VM operations here;
            # a bounded indexed lookup takes under 100 on supported SQLite.
            self.assertLess(len(steps), 500)

    def test_rollback_journal_catalog_rejected_without_mutation_or_inference(self):
        with sqlite3.connect(self.jobs) as con:
            self.assertEqual(con.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
        before = self.jobs.read_bytes(), self.state.read_bytes()
        with self.assertRaisesRegex(model.PreferenceModelError, 'requires a WAL jobs database'):
            self.run_stage(candidate_limit=2, execute=True, stage_db=self.output)
        self.assertEqual(before, (self.jobs.read_bytes(), self.state.read_bytes()))
        with sqlite3.connect(self.jobs) as con:
            self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.factory.assert_not_called()
        self.load.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_runtime_guard_counts_every_submission_and_unknown_text(self):
        texts = ['alpha', 'beta']
        guard = staged._BudgetEncoder(self.encoder, self.encoder.model_revision,
            {model.text_fingerprint(text) for text in texts}, 2, 9)
        list(guard.iter_encode_batches(texts))
        with self.assertRaises(staged.BudgetExceeded):
            list(guard.iter_encode_batches(['alpha']))
        guard = staged._BudgetEncoder(self.encoder, self.encoder.model_revision, set(), 100, 100)
        with self.assertRaises(staged.BudgetExceeded):
            list(guard.iter_encode_batches(['unexpected']))

    def test_failed_prediction_preserves_policy_state_and_marks_stage_failed(self):
        before = self.policy_state()
        self.predict.side_effect = RuntimeError('offline fixture failure')
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            self.run_stage(candidate_limit=2, execute=True, stage_db=self.output)
        self.assertEqual(before, self.policy_state())
        with sqlite3.connect(self.output) as con:
            self.assertEqual(con.execute('SELECT status FROM staged_runs').fetchone()[0], 'failed')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM staged_scores').fetchone()[0], 0)
        self.factory.reset_mock()
        self.predict.side_effect = lambda *args, **kwargs: [{'final': .5} for doc in args[1]]
        result = self.run_stage(candidate_limit=2, execute=True, stage_db=self.output, max_missing_texts=0)
        self.assertEqual(result['status'], 'completed_candidates')
        self.factory.assert_not_called()

    def test_recall_without_eligible_reviews_is_unavailable_with_exclusion_counts(self):
        doc = self.docs[0]
        manifest = {'labels': [{'family_id': doc.family_id, 'feature_fingerprint': doc.fingerprint,
                               'template_cluster_id': doc.template_cluster_id,
                               'leakage_group_id': doc.leakage_group_id}]}
        self.assertEqual(staged.candidate_recall([], {}, set(), [{}])['reason'],
                         'no_eligible_held_out_reviews')
        for examples in ([], [model.TrainingExample('review', doc, 1)]):
            result = staged.candidate_recall(examples, {doc.family_id: doc}, {doc.family_id}, [manifest])
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['reason'], 'no_eligible_held_out_reviews')
            self.assertEqual(result['reviewed_families'], len(examples))
            self.assertEqual(result['excluded_training_overlap'], len(examples))
            self.assertEqual(result['slices'], {})

    def test_recall_excludes_training_groups_changed_text_and_separates_slices(self):
        docs = [replace(doc, template_cluster_id='unique' + str(i), leakage_group_id='unique' + str(i))
                for i, doc in enumerate(self.docs)]
        examples = [model.TrainingExample(str(i), doc, 1, selection_strategy='uniform' if i < 3 else 'top')
                    for i, doc in enumerate(docs)]
        manifest = {'labels': [{'family_id': 'other_training_family', 'feature_fingerprint': 'other_fp',
                              'template_cluster_id': 'historic_alias', 'leakage_group_id': 'other_group'}]}
        current = {doc.family_id: doc for doc in docs}
        current[docs[1].family_id] = replace(docs[1], title_metadata_text='Changed snapshot')
        result = staged.candidate_recall(examples, current, {docs[2].family_id}, [manifest, manifest],
            template_aliases={'historic_alias': docs[0].template_cluster_id})
        self.assertEqual(result['excluded_training_overlap'], 1)
        self.assertEqual(result['excluded_missing_or_changed_snapshot'], 1)
        self.assertEqual(result['slices']['uniform']['interested_candidate_recall'], 1)
        self.assertEqual(result['slices']['top']['interested_candidate_recall'], 0)
        unavailable = staged.candidate_recall(examples, current, set(), [{}])
        self.assertEqual(unavailable['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
