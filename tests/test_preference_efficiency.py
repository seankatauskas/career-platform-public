"""Offline policy-aware scoring, diagnostic coverage, and encoder lock regressions."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from job_search.ranking import labeler, model
from tests.test_job_labeler import make_database, seed_families_and_model


class Probabilities:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        assert key == (slice(None), 1)
        return self.values


class ScoringEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.root / 'state.db'
        self.documents = [model.build_feature_document({
            'family_id': f'family-{i}', 'title': f'Engineer {i}',
            'description': f'Build distributed services {i}',
        }) for i in range(2)]
        self.dense = Mock()
        self.dense.predict_proba.return_value = Probabilities([.7, .2])
        self.sparse = Mock()
        self.sparse.predict_proba.return_value = Probabilities([.6, .4])
        self.artifact = dict(weights={'sparse': 1.0}, model_revision='fixture',
                             dense_model=self.dense, sparse_model={'classifier': self.sparse},
                             neighbor_vectors=[], neighbor_targets=[], neighbor_family_ids=[], neighbor_k=2)
        self.modules = {'numpy': SimpleNamespace(asarray=lambda x, dtype: x), 'scipy_sparse': None}
        self.vectors = self.stack.enter_context(patch.object(model, 'load_combined_vectors', return_value=[[1], [2]]))
        self.neighbors = self.stack.enter_context(patch.object(model, '_neighbor_predict_details', return_value=(
            [.8, .1], [['liked-1'], ['liked-2']],
        )))
        self.matrix = self.stack.enter_context(patch.object(model, '_sparse_matrix', return_value='matrix'))
        self.phrases = self.stack.enter_context(patch.object(model, '_sparse_explanations', return_value=[['services'], []]))

    def score(self, active=False):
        return model._score_document_batch(self.state, self.documents, self.artifact,
                                           self.modules, active_components_only=active)

    def test_sparse_only_matches_full_scores_without_vectors_or_dense_predictors(self):
        full = self.score()
        self.vectors.reset_mock(); self.neighbors.reset_mock(); self.dense.reset_mock()
        del self.modules['numpy']
        active = self.score(True)
        self.assertEqual([x['final'] for x in active], [x['final'] for x in full])
        self.assertEqual([x['positive_sparse_phrases'] for x in active], [x['positive_sparse_phrases'] for x in full])
        self.vectors.assert_not_called(); self.neighbors.assert_not_called(); self.dense.predict_proba.assert_not_called()
        self.assertEqual(active[0]['computed_components'], ['sparse'])
        self.assertIsNone(active[0]['dense_linear'])
        self.assertIsNone(active[0]['dense_neighbor'])
        self.assertFalse(model.artifact_requires_embeddings(self.artifact, active_components_only=True))
        self.assertTrue(model.artifact_requires_embeddings(self.artifact))
        self.assertFalse(self.state.exists())

    def test_dense_only_and_hybrid_preserve_weighted_score(self):
        for weights in ({'dense_linear': 1.0}, {'dense_neighbor': 1.0},
                        {'dense_linear': .3, 'sparse': .7},
                        {'dense_linear': .2, 'dense_neighbor': .3, 'sparse': .5}):
            with self.subTest(weights=weights):
                self.artifact['weights'] = weights
                full = self.score()
                self.matrix.reset_mock(); self.neighbors.reset_mock(); self.dense.reset_mock()
                active = self.score(True)
                self.assertEqual([x['final'] for x in active], [x['final'] for x in full])
                if 'sparse' not in weights: self.matrix.assert_not_called()
                if 'dense_neighbor' not in weights: self.neighbors.assert_not_called()
                if 'dense_linear' not in weights: self.dense.predict_proba.assert_not_called()

    def test_invalid_active_weights_fail_before_scoring(self):
        for weights in ({}, {'sparse': 0}, {'sparse': -1}, {'sparse': float('nan')},
                        {'sparse': float('inf')}, {'sparse': True}, {'unknown': 1}, {'sparse': None}):
            with self.subTest(weights=weights), self.assertRaises(model.PreferenceModelError):
                self.artifact['weights'] = weights
                self.score(True)
        self.vectors.assert_not_called(); self.matrix.assert_not_called()

    def test_cache_covers_requested_components_and_preserves_uncomputed_markers(self):
        model.prepare_state(self.state)
        with model.connect_state(self.state) as con:
            con.execute('INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)',
                        ('run', 'now', 'fixture', model.TEXT_VERSION, 1, '{}', ''))
            def store(active):
                return model.score_and_store_batch(con, self.state, self.documents, self.artifact,
                                                   {'run_id': 'run'}, self.modules, active_components_only=active)
            self.assertEqual(store(True), 2)
            row = con.execute('SELECT explanation_json,dense_linear_score FROM preference_scores LIMIT 1').fetchone()
            explanation = json.loads(row[0])
            self.assertIsNone(explanation['components']['dense_linear'])
            self.assertEqual(explanation['computed_components'], ['sparse'])
            self.assertEqual(row[1], 0)
            self.assertEqual(store(True), 0)
            self.assertEqual(store(False), 2)  # Missing diagnostics must be computed.
            self.assertEqual(store(True), 0)   # Full rows satisfy a cheaper request.
            self.assertEqual(store(False), 0)
            for row in con.execute('SELECT family_id,explanation_json FROM preference_scores').fetchall():
                legacy = json.loads(row[1]); del legacy['computed_components']
                con.execute('UPDATE preference_scores SET explanation_json=? WHERE family_id=?', (json.dumps(legacy), row[0]))
            self.assertEqual(store(False), 0)  # Legacy diagnostics remain compatible.
            con.execute("UPDATE preference_scores SET explanation_json='invalid' WHERE family_id='family-0'")
            self.assertEqual(store(False), 1)


class DiagnosticCoverageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = make_database(directory.name)
        self.model_db = Path(directory.name) / 'preference.db'
        seed_families_and_model(self.db, self.model_db)

    def mark_sparse_only(self, family_id, score):
        explanation = {
            'computed_components': ['sparse'],
            'components': {'dense_linear': None, 'dense_neighbor': None, 'sparse': score},
        }
        with sqlite3.connect(self.model_db) as con:
            con.execute(
                'UPDATE preference_scores SET dense_linear_score=0, dense_neighbor_score=0, '
                'sparse_score=?, final_score=?, explanation_json=? WHERE family_id=?',
                (score, score, json.dumps(explanation), family_id),
            )

    def test_recommendations_hide_uncomputed_components_and_keep_legacy_diagnostics(self):
        self.mark_sparse_only('family-product', .9)
        result = labeler.recommendations(
            self.db, self.model_db, labeler._recommendation_options({}),
        )
        jobs = {row['family_id']: row for row in result['recommendations']}
        self.assertEqual(jobs['family-product']['score_components'], {
            'dense_linear': None, 'dense_neighbor': None, 'sparse': .9,
        })
        self.assertEqual(jobs['family-accounting']['score_components'], {
            'dense_linear': .4, 'dense_neighbor': .3, 'sparse': .2,
        })

    def test_candidate_scores_hide_uncomputed_components_and_keep_legacy_diagnostics(self):
        self.mark_sparse_only('family-product', .9)
        scores = labeler._candidate_scores(
            self.model_db, ['family-product', 'family-accounting'],
        )
        sparse = scores['family-product']
        self.assertIsNone(sparse['dense_linear_score'])
        self.assertIsNone(sparse['dense_neighbor_score'])
        self.assertEqual(sparse['sparse_score'], .9)
        self.assertEqual(sparse['final_score'], .9)
        self.assertEqual(scores['family-accounting']['dense_linear_score'], .4)
        self.assertEqual(scores['family-accounting']['dense_neighbor_score'], .3)

    def test_disagreement_with_only_sparse_scores_selects_uncertain_job(self):
        self.mark_sparse_only('family-product', .9)
        self.mark_sparse_only('family-accounting', .49)
        with patch.object(labeler, '_selection_policy', return_value=('dense_sparse_disagreement', .25)):
            selected = labeler.choose_job(
                self.db, labeler._filters({'days': ['30']}), self.model_db,
            )
        self.assertIsNotNone(selected)
        self.assertEqual(selected['family_id'], 'family-accounting')
        self.assertEqual(selected['selection_strategy'], 'disagreement_uncertainty_fallback')
        self.assertEqual(selected['selection_probability'], .25)


class EmbeddingTransactionTests(unittest.TestCase):
    def test_other_writer_is_not_blocked_while_encoder_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.db'
            document = model.build_feature_document({'family_id': 'one', 'title': 'Engineer', 'description': 'Build services.'})
            class WriterCheckingEncoder(model.HashingEncoder):
                def encode(self, texts):
                    with sqlite3.connect(state, timeout=0) as other:
                        other.execute("INSERT OR REPLACE INTO preference_state VALUES ('concurrent_writer','ok')")
                    return super().encode(texts)
            encoder = WriterCheckingEncoder(8)
            result = model.embed_documents(state, [document], encoder)
            self.assertGreater(result['embedded_texts'], 0)
            self.assertEqual(len(model.load_combined_vectors(state, [document], encoder.model_revision)), 1)

    def test_read_only_artifact_preflight_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'missing' / 'state.db'
            with self.assertRaises(model.PreferenceModelError):
                model._load_artifact(state, 'run', prepare_schema=False)
            self.assertFalse(state.parent.exists())


if __name__ == '__main__':
    unittest.main()
