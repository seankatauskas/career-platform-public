"""Batched policy refresh, partial failure, and concurrent-ingestion regressions."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from job_search.collection.dedupe import prepare_families
from job_search.ranking import model, refresh
from tests.test_preference_model import _source_db


class PolicyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.jobs = self.root / 'jobs.db'
        self.state = self.root / 'state.db'
        _source_db(self.jobs)
        with sqlite3.connect(self.jobs) as con:
            con.execute('PRAGMA journal_mode=WAL')
            for i in (2, 3):
                con.execute('INSERT INTO jobs(ats,id,company,title,description,last_seen) VALUES (?,?,?,?,?,?)',
                            ('ashby', str(i), 'Example', f'Engineer {i}', f'Design services {i}', '2026-09-28'))
        prepare_families(self.jobs)
        model.prepare_state(self.state)
        self.runs = {'selective': 'run_a', 'broad': 'run_b'}
        self.encoder = model.HashingEncoder(8)
        with sqlite3.connect(self.state) as con:
            con.executemany('INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)', [
                (run, '2026-09-28', self.encoder.model_revision, model.TEXT_VERSION, 1, '{}', '')
                for run in self.runs.values()
            ])
        self.seen = []
        self.stack.enter_context(patch.object(refresh, 'policy_runs', return_value=self.runs))
        self.stack.enter_context(patch.object(model, '_load_artifact', side_effect=lambda db, run: (
            {'weights': {}, 'model_revision': self.encoder.model_revision},
            {'run_id': run, 'model_revision': self.encoder.model_revision},
        )))
        self.stack.enter_context(patch.object(model, '_optional_ml_modules', return_value={}))
        self.stack.enter_context(patch.object(model, 'encoder_for_recorded_revision', return_value=self.encoder))
        self.predict = self.stack.enter_context(patch.object(model, '_score_document_batch', side_effect=self.predictions))
        self.train = self.stack.enter_context(patch.object(model, 'train_model'))

    def predictions(self, state, documents, artifact, modules):
        # Exercise real cache fingerprints and vectors, with no optional ML dependencies.
        model.load_combined_vectors(state, documents, artifact['model_revision'])
        self.seen.extend(documents)
        return [dict(dense_linear=.7, dense_neighbor=.6, sparse=.8, final=.7,
                     similar_liked_family_ids=[], positive_sparse_phrases=[]) for _ in documents]

    def run_refresh(self, **kwargs):
        return refresh.refresh_policies(self.jobs, self.state, self.root / 'proxy.db', batch_size=1, **kwargs)

    def scores(self):
        with sqlite3.connect(self.state) as con:
            return con.execute('SELECT run_id,family_id,feature_fingerprint,scored_at FROM preference_scores ORDER BY run_id,family_id').fetchall()

    def receipts(self):
        with sqlite3.connect(self.state) as con:
            return {r[0]: json.loads(r[1]) for r in con.execute("SELECT key,value FROM preference_state WHERE key LIKE 'policy_refresh:%'")}

    def test_full_refresh_both_policies_and_unchanged_resume(self):
        with patch.object(model, 'prepare_state', wraps=model.prepare_state) as prepare:
            result = self.run_refresh()
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(result['dedupe'], {'reused': True})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(self.scores()), 6)
        self.assertEqual(len(self.receipts()), 2)
        self.assertEqual(result['processed_families'], 3)
        self.train.assert_not_called()
        old = self.scores()
        self.predict.reset_mock()
        result = self.run_refresh()
        self.assertEqual(self.scores(), old)
        self.predict.assert_not_called()
        self.assertTrue(all(r['result']['updated_families'] == 0 for r in result['policies'].values()))

    def test_backfill_deadline_is_scoped_to_ranking(self):
        from job_search.pipeline import build_opportunity_handlers, PREFERENCE_TASK, LOCATION_TASK
        _, handlers = build_opportunity_handlers(project_root=self.root, jobs_db=self.jobs,
            preference_db=self.state, environment_provider=lambda: {}, policy_refresh=True,
            proxy_db=self.root / 'proxy.db')
        self.assertEqual(handlers[PREFERENCE_TASK].command.timeout_seconds, 7200)
        self.assertEqual(handlers[LOCATION_TASK].command.timeout_seconds, 3600)

    def test_failed_second_batch_retains_first_and_resumes_without_false_receipt(self):
        calls = 0
        def fail_later(*args):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise model.PreferenceModelError('fixture batch failure')
            return self.predictions(*args)
        self.predict.side_effect = fail_later
        with self.assertRaisesRegex(model.PreferenceModelError, 'fixture batch failure'):
            self.run_refresh()
        self.assertEqual(len(self.scores()), 2)
        committed = self.scores()
        self.assertEqual(self.receipts(), {})
        self.predict.side_effect = self.predictions
        result = self.run_refresh()
        self.assertEqual(len(self.scores()), 6)
        self.assertTrue(all(row in self.scores() for row in committed))
        self.assertEqual([r['result']['updated_families'] for r in result['policies'].values()], [2, 2])

    def test_sample_preserves_scores_and_completion_receipts(self):
        self.run_refresh()
        scores, receipts = self.scores(), self.receipts()
        with model.connect_state(self.state) as con:
            model._set_state(con, 'embedding_model_revision', 'fixture-other-revision')
        result = self.run_refresh(sample_size=2)
        self.assertEqual(result['status'], 'sample_passed')
        self.assertEqual(result['processed_families'], 2)
        self.assertEqual(self.scores(), scores)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual([p['tested_families'] for p in result['policies'].values()], [2, 2])
        with model.connect_state(self.state) as con:
            self.assertEqual(model._state_value(con, 'embedding_model_revision'), 'fixture-other-revision')

    def test_new_jobs_during_scoring_do_not_change_snapshot_or_watermark(self):
        wrote = False
        def insert_during_score(*args):
            nonlocal wrote
            if not wrote:
                wrote = True
                with sqlite3.connect(self.jobs) as con:
                    con.execute("INSERT INTO jobs(ats,id,title,description,last_seen) VALUES ('ashby','4','New role','New description','2026-09-29')")
                    con.execute("UPDATE jobs SET description='Changed while scoring' WHERE id='3'")
            return self.predictions(*args)
        self.predict.side_effect = insert_during_score
        result = self.run_refresh()
        self.assertEqual(result['processed_families'], 3)
        self.assertTrue(all(r['source_watermark'] == '2026-09-28' for r in self.receipts().values()))
        self.assertNotIn('Changed while scoring', [d.description_text for d in self.seen])
        self.predict.side_effect = self.predictions
        self.assertEqual(self.run_refresh()['processed_families'], 4)

    def test_failure_does_not_prune_previous_scores(self):
        self.run_refresh()
        with sqlite3.connect(self.jobs) as con:
            con.execute("DELETE FROM jobs WHERE id='3'")
            con.execute("UPDATE jobs SET description='new content' WHERE id='2'")
        before = self.scores()
        self.predict.side_effect = RuntimeError('fixture failure')
        with self.assertRaises(RuntimeError):
            self.run_refresh()
        self.assertEqual(self.scores(), before)
        self.predict.side_effect = self.predictions
        result = self.run_refresh()
        self.assertEqual(len(self.scores()), 4)
        self.assertTrue(all(r['result']['removed_families'] > 0 for r in result['policies'].values()))


if __name__ == '__main__':
    unittest.main()
