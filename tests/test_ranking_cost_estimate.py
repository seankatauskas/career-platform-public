"""Read-only preflight coverage and embedding demand, with real SQLite snapshots."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.collection.dedupe import prepare_families
from job_search.ranking import cost_estimate, model
from tests.test_preference_model import _source_db


class RankingCostEstimateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.jobs, self.state, self.proxy = [self.root / name for name in ('jobs.db', 'state.db', 'proxy.db')]
        _source_db(self.jobs)
        with sqlite3.connect(self.jobs) as con:
            con.execute('INSERT INTO jobs(ats,id,company,title,description,last_seen) VALUES (?,?,?,?,?,?)',
                        ('ashby', '2', 'Other', 'Engineer', 'Design storage engines.', '2026-09-28'))
        prepare_families(self.jobs)
        model.prepare_state(self.state)
        with sqlite3.connect(self.proxy) as con:
            con.executescript('CREATE TABLE proxy_runs(run_id TEXT,created_at TEXT); '
                'CREATE TABLE proxy_students(policy_id TEXT,model_run_id TEXT,run_id TEXT,trained_at TEXT); '
                "INSERT INTO proxy_runs VALUES ('proxy','2026-10-01'); "
                "INSERT INTO proxy_students VALUES ('broad','broad_run','proxy','2026-10-01'); "
                "INSERT INTO proxy_students VALUES ('selective','selective_run','proxy','2026-10-01');")
        self.revision = 'fixture-revision'
        with sqlite3.connect(self.state) as con:
            for policy, weights in [('broad', {'sparse': 1}), ('selective', {'dense_linear': 1})]:
                con.execute('INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)',
                    (policy + '_run', '2026-10-01', self.revision, model.TEXT_VERSION, 1,
                     json.dumps({'selection': {'weights': weights}}), str(self.root / 'missing-artifact')))

    def estimate(self, **kwargs):
        return cost_estimate.estimate_refresh_cost(self.jobs, self.state, self.proxy, **kwargs)

    def test_read_only_without_loading_artifacts_or_providers(self):
        before = {path: path.read_bytes() for path in (self.jobs, self.state, self.proxy)}
        with patch.object(model, '_load_artifact', side_effect=AssertionError('unpickle forbidden')), \
             patch.object(model, 'encoder_for_recorded_revision', side_effect=AssertionError('provider forbidden')), \
             patch.object(model, 'prepare_state', side_effect=AssertionError('write forbidden')):
            result = self.estimate()
        self.assertTrue(result['complete_prepared_snapshot'])
        self.assertEqual(result['inspected_families'], 2)
        self.assertEqual(result['jobs_without_family'], 0)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertFalse(result['policies']['broad']['artifact_present'])

    def test_partial_coverage_and_bounded_inspection_are_explicit(self):
        bounded = self.estimate(max_families=1)
        self.assertFalse(bounded['complete_prepared_snapshot'])
        self.assertEqual(bounded['inspected_families'], 1)
        with sqlite3.connect(self.jobs) as con:
            con.execute("INSERT INTO jobs(ats,id,title) VALUES ('lever','3','Unprepared')")
        partial = self.estimate()
        self.assertEqual(partial['status'], 'partial_prepared_snapshot')
        self.assertEqual(partial['jobs_without_family'], 1)
        self.assertEqual(partial['prepared_families'], 2)
        self.assertFalse(partial['complete_prepared_snapshot'])

    def test_sparse_policy_avoids_all_embedding_demand(self):
        result = self.estimate(policies=('broad',), active_components_only=True)
        self.assertFalse(result['requires_embeddings'])
        self.assertEqual(result['embeddings'], {})
        self.assertEqual(result['policies']['broad']['new_or_changed_scores'], 2)
        full = self.estimate(policies=('broad',))
        self.assertTrue(full['requires_embeddings'])
        self.assertGreater(full['embeddings'][self.revision]['missing_texts'], 0)

    def test_shared_revision_and_repeated_texts_are_deduplicated(self):
        documents = list(model.load_family_documents(self.jobs))
        texts = {model.text_fingerprint(text): text for doc in documents
                 for text in (doc.title_metadata_text, *doc.description_chunks)}
        self.assertLess(len(texts), sum(1 + len(doc.description_chunks) for doc in documents))
        cached = next(iter(texts))
        with sqlite3.connect(self.state) as con:
            con.execute('INSERT INTO preference_embedding_cache VALUES (?,?,?,?,?,?)',
                        (self.revision, model.TEXT_VERSION, cached, b'\0\0\0\0', 1, '2026-10-01'))
        full = self.estimate()
        single = self.estimate(policies=('selective',), active_components_only=True)
        self.assertEqual(full['embeddings'], single['embeddings'])
        demand = full['embeddings'][self.revision]
        self.assertEqual(demand['unique_texts'], len(texts))
        self.assertEqual(demand['cached_texts'], 1)
        self.assertEqual(demand['missing_texts'], len(texts) - 1)
        self.assertEqual(demand['missing_characters'], sum(len(text) for fp, text in texts.items() if fp != cached))

    def test_unchanged_score_fingerprint_reduces_pending_count(self):
        document = model.load_family_documents(self.jobs)[0]
        with sqlite3.connect(self.state) as con:
            con.execute('INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?,?)',
                        ('broad_run', document.family_id, document.fingerprint, 0, 0, .5, .5, '{}', '2026-10-01'))
        self.assertEqual(self.estimate()['policies']['broad']['new_or_changed_scores'], 1)
        with sqlite3.connect(self.jobs) as con:
            con.execute("UPDATE jobs SET description='Changed source text'")
        self.assertEqual(self.estimate()['policies']['broad']['new_or_changed_scores'], 2)

    def test_missing_inputs_never_create_databases(self):
        for name in ('jobs', 'state', 'proxy'):
            with self.subTest(name=name):
                paths = dict(jobs=self.jobs, state=self.state, proxy=self.proxy)
                missing = self.root / ('missing-' + name + '.db')
                paths[name] = missing
                with self.assertRaises((ValueError, model.PreferenceModelError, sqlite3.Error)):
                    cost_estimate.estimate_refresh_cost(paths['jobs'], paths['state'], paths['proxy'])
                self.assertFalse(missing.exists())

    def test_invalid_metadata_and_options_fail_closed(self):
        for manifest in ([], {}, {'selection': []}, {'selection': {'weights': {'sparse': -1}}},
                         {'selection': {'weights': {'surprise': 1}}}):
            with self.subTest(manifest=manifest), sqlite3.connect(self.state) as con:
                con.execute('UPDATE preference_model_runs SET manifest_json=?', (json.dumps(manifest),))
            for active in (False, True):
                with self.assertRaises((ValueError, model.PreferenceModelError)):
                    self.estimate(active_components_only=active)
        for kwargs in ({'policies': ()}, {'policies': ('broad', 'broad')}, {'max_families': 0}):
            with self.assertRaises(ValueError):
                self.estimate(**kwargs)


if __name__ == '__main__':
    unittest.main()
