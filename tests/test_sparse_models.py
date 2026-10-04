"""Sparse policy version lineage, integrity and explicit activation regressions."""
import json
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pickle
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search.ranking import model, refresh, sparse


class SparseModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state.db'
        self.proxy = self.root / 'proxy.db'
        self.artifacts = self.root / 'artifacts'
        model.prepare_state(self.state)
        with closing(sqlite3.connect(self.proxy)) as con, con:
            con.executescript('''
                CREATE TABLE proxy_runs(run_id TEXT PRIMARY KEY, created_at TEXT);
                CREATE TABLE proxy_students(run_id TEXT,policy_id TEXT,model_run_id TEXT,
                    model_revision TEXT,training_examples INTEGER,state_db TEXT,
                    artifact_dir TEXT,score_json TEXT,trained_at TEXT,
                    PRIMARY KEY(run_id,policy_id));
                INSERT INTO proxy_runs VALUES('proxy_parent','2026-10-01');
            ''')
        self.sources = {}
        for policy, weights in [('selective', {'dense_linear': 1.0}), ('broad', {'sparse': 1.0})]:
            run_id = 'run_' + policy
            artifact = {'run_id': run_id, 'model_revision': 'recorded_encoder',
                        'text_version': model.TEXT_VERSION, 'weights': weights,
                        'sparse_model': {'word': policy, 'char': policy, 'classifier': policy},
                        'dense_model': policy, 'neighbor_vectors': [1, 2]}
            manifest = {key: artifact[key] for key in ('run_id', 'model_revision', 'text_version')}
            manifest.update(training_source='proxy:proxy_parent:' + policy,
                            labels=[{'family_id': 'training_' + policy, 'target': 1}],
                            training_examples=1, selection={'name': list(weights)[0], 'weights': weights},
                            out_of_fold={'components': {'sparse': {'average_precision': .83}},
                                         'selected': {'average_precision': .86},
                                         'baselines': {'random': .5}, 'candidates': [{'dense': .86}]},
                            protected_evaluation={'precision': .99},
                            artifacts={'model.pkl': model.sha256_bytes(pickle.dumps(artifact, protocol=4))})
            directory = self.artifacts / 'runs' / run_id
            directory.mkdir(parents=True)
            (directory / 'model.pkl').write_bytes(pickle.dumps(artifact, protocol=4))
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            with closing(sqlite3.connect(self.state)) as con, con:
                con.execute('INSERT INTO preference_model_runs VALUES(?,?,?,?,?,?,?)',
                            (run_id, '2026-10-01', 'recorded_encoder', model.TEXT_VERSION,
                             1, json.dumps(manifest), str(directory)))
                con.execute('INSERT OR REPLACE INTO preference_state VALUES(?,?)',
                            ('policy_refresh:' + policy, json.dumps({'run_id': run_id, 'status': 'ready'})))
            with closing(sqlite3.connect(self.proxy)) as con, con:
                con.execute('INSERT INTO proxy_students VALUES(?,?,?,?,?,?,?,?,?)',
                            ('proxy_parent', policy, run_id, 'recorded_encoder', 1,
                             str(self.state), str(self.artifacts), '{"scored":100}', '2026-10-01'))
            self.sources[policy] = (artifact, manifest)
        self.train = patch.object(model, 'train_model', side_effect=AssertionError('training prohibited')).start()
        self.addCleanup(patch.stopall)
        self.encoder = patch.object(model, 'encoder_for_recorded_revision', side_effect=AssertionError('inference prohibited')).start()
        self.embed = patch.object(model, 'embed_documents', side_effect=AssertionError('embedding prohibited')).start()

    def derive(self, **kwargs):
        return sparse.derive_sparse_policies(self.state, self.proxy, self.artifacts, **kwargs)

    def state_rows(self):
        with closing(sqlite3.connect(self.state)) as con, con:
            return con.execute('SELECT * FROM preference_model_runs ORDER BY run_id').fetchall()

    def test_candidate_has_distinct_identity_preserves_training_and_invalidates_dense_metrics(self):
        before = self.state_rows()
        result = self.derive()
        selective = result['policies']['selective']
        self.assertNotEqual(selective['run_id'], 'run_selective')
        self.assertEqual(refresh.policy_runs(self.proxy), {'selective': 'run_selective', 'broad': 'run_broad'})
        artifact, record = model._load_artifact(self.state, selective['run_id'])
        manifest = json.loads(record['manifest_json'])
        self.assertEqual(artifact['weights'], {'sparse': 1.0})
        self.assertEqual(artifact['sparse_model'], self.sources['selective'][0]['sparse_model'])
        self.assertEqual(manifest['labels'], self.sources['selective'][1]['labels'])
        self.assertEqual(manifest['training_source'], 'proxy:proxy_parent:selective')
        self.assertEqual(manifest['out_of_fold']['components']['sparse'], {'average_precision': .83})
        self.assertNotIn('average_precision', manifest['out_of_fold']['selected'])
        self.assertNotIn('protected_evaluation', manifest)
        self.assertNotIn('candidates', manifest['out_of_fold'])
        self.assertEqual(manifest['evaluation_status'], 'requires_independent_evaluation')
        self.assertEqual(manifest['derivation']['source_run_id'], 'run_selective')
        self.assertTrue(all(row in self.state_rows() for row in before))
        with closing(sqlite3.connect(self.state)) as con, con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM preference_scores WHERE run_id=?',
                                         (selective['run_id'],)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT value FROM preference_state WHERE key='policy_refresh:selective'").fetchone()[0],
                             json.dumps({'run_id': 'run_selective', 'status': 'ready'}))
        self.assertFalse(result['policies']['broad']['derived'])
        self.assertEqual(result['policies']['broad']['run_id'], 'run_broad')
        self.train.assert_not_called()
        self.encoder.assert_not_called()
        self.embed.assert_not_called()

    def test_repeat_candidate_and_activation_are_idempotent_and_preserve_sources(self):
        original_files = {path: path.read_bytes() for path in self.artifacts.rglob('*') if path.is_file()}
        candidate = self.derive()
        rows = self.state_rows()
        files = {path: path.read_bytes() for path in self.artifacts.rglob('*') if path.is_file()}
        self.assertEqual(self.derive(), candidate)
        self.assertEqual(self.state_rows(), rows)
        self.assertEqual(files, {path: path.read_bytes() for path in files})
        active = self.derive(activate=True)
        self.assertEqual(active['policies']['selective']['run_id'], candidate['policies']['selective']['run_id'])
        with closing(sqlite3.connect(self.proxy)) as con, con:
            before = con.execute('SELECT * FROM proxy_students ORDER BY policy_id').fetchall()
            receipt = json.loads(con.execute("SELECT score_json FROM proxy_students WHERE policy_id='selective'").fetchone()[0])
            self.assertEqual(receipt['status'], 'not_scored')
        self.derive(activate=True)
        with closing(sqlite3.connect(self.proxy)) as con, con:
            self.assertEqual(before, con.execute('SELECT * FROM proxy_students ORDER BY policy_id').fetchall())
        self.assertEqual(original_files, {path: path.read_bytes() for path in original_files})

    def test_all_sources_verified_before_any_candidate_write(self):
        (self.artifacts / 'runs' / 'run_broad' / 'model.pkl').write_bytes(b'corrupted')
        before = self.state_rows()
        with self.assertRaisesRegex(model.PreferenceModelError, 'hash verification'):
            self.derive(activate=True)
        self.assertEqual(self.state_rows(), before)
        self.assertEqual(len(list((self.artifacts / 'runs').iterdir())), 2)
        self.assertEqual(refresh.policy_runs(self.proxy)['selective'], 'run_selective')

    def test_wrong_policy_provenance_is_rejected(self):
        with closing(sqlite3.connect(self.proxy)) as con, con:
            con.execute("UPDATE proxy_students SET model_run_id='run_broad' WHERE policy_id='selective'")
        with self.assertRaisesRegex(model.PreferenceModelError, 'training provenance'):
            self.derive(activate=True)
        self.assertEqual(len(self.state_rows()), 2)

    def test_missing_sparse_component_and_identity_mismatch_are_rejected(self):
        artifact, record = model._load_artifact(self.state, 'run_selective')
        with patch.object(model, '_load_artifact', return_value=(dict(artifact, sparse_model=None), record)):
            with self.assertRaisesRegex(model.PreferenceModelError, 'sparse component'):
                self.derive(policies=('selective',))
        with patch.object(model, '_load_artifact', return_value=(dict(artifact, run_id='wrong'), record)):
            with self.assertRaisesRegex(model.PreferenceModelError, 'identity'):
                self.derive(policies=('selective',))

    def test_candidate_corruption_does_not_overwrite_or_activate(self):
        candidate = self.derive()['policies']['selective']
        path = Path(candidate['artifact_path']) / 'model.pkl'
        path.write_bytes(b'corruption')
        with self.assertRaisesRegex(model.PreferenceModelError, 'hash verification'):
            self.derive(activate=True)
        self.assertEqual(path.read_bytes(), b'corruption')
        self.assertEqual(refresh.policy_runs(self.proxy)['selective'], 'run_selective')

    def test_mapping_race_activates_neither_policy(self):
        persist = sparse._persist
        def raced(*args, **kwargs):
            result = persist(*args, **kwargs)
            with closing(sqlite3.connect(self.proxy)) as con, con:
                con.execute("UPDATE proxy_students SET trained_at='newer' WHERE policy_id='broad'")
            return result
        with patch.object(sparse, '_persist', side_effect=raced):
            with self.assertRaisesRegex(model.PreferenceModelError, 'mappings changed'):
                self.derive(activate=True)
        self.assertEqual(refresh.policy_runs(self.proxy), {'selective': 'run_selective', 'broad': 'run_broad'})

    def test_orphaned_complete_candidate_can_be_registered_without_rewriting(self):
        candidate = self.derive()['policies']['selective']
        path = Path(candidate['artifact_path'])
        before = {file.name: file.read_bytes() for file in path.iterdir()}
        with closing(sqlite3.connect(self.state)) as con, con:
            con.execute('DELETE FROM preference_model_runs WHERE run_id=?', (candidate['run_id'],))
        self.assertEqual(self.derive()['policies']['selective']['run_id'], candidate['run_id'])
        self.assertEqual(before, {file.name: file.read_bytes() for file in path.iterdir()})

    def test_invalid_policy_list_and_missing_database_do_not_create_files(self):
        for policies in ((), ('selective', 'selective'), ('other',)):
            with self.assertRaises(model.PreferenceModelError):
                self.derive(policies=policies)
        missing = self.root / 'missing.db'
        with self.assertRaises((model.PreferenceModelError, sqlite3.Error, ValueError)):
            sparse.derive_sparse_policies(self.state, missing, self.artifacts, activate=True)
        self.assertFalse(missing.exists())

    def test_historical_artifact_without_text_version_uses_validated_manifest(self):
        original_load = model._load_artifact
        def historical(*args, **kwargs):
            artifact, row = original_load(*args, **kwargs)
            artifact.pop('text_version', None)
            return artifact, row
        with patch.object(model, '_load_artifact', side_effect=historical):
            result = self.derive(activate=True)
        self.assertEqual(refresh.policy_runs(self.proxy)['selective'], result['policies']['selective']['run_id'])

    def test_registered_and_orphaned_artifact_wrong_weights_cannot_be_activated(self):
        candidate = self.derive()['policies']['selective']
        directory = Path(candidate['artifact_path'])
        artifact, row = model._load_artifact(self.state, candidate['run_id'])
        artifact['weights'] = {'dense_linear': 1.0}
        content = pickle.dumps(artifact, protocol=4)
        manifest = json.loads(row['manifest_json'])
        manifest['artifacts']['model.pkl'] = model.sha256_bytes(content)
        (directory / 'model.pkl').write_bytes(content)
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        with closing(sqlite3.connect(self.state)) as con, con:
            con.execute('UPDATE preference_model_runs SET manifest_json=? WHERE run_id=?',
                        (json.dumps(manifest), candidate['run_id']))
        with self.assertRaisesRegex(model.PreferenceModelError, 'not sparse-only'):
            self.derive(activate=True)
        with closing(sqlite3.connect(self.state)) as con, con:
            con.execute('DELETE FROM preference_model_runs WHERE run_id=?', (candidate['run_id'],))
        with self.assertRaisesRegex(model.PreferenceModelError, 'not sparse-only'):
            self.derive(activate=True)
        self.assertEqual(refresh.policy_runs(self.proxy)['selective'], 'run_selective')

    def test_simultaneous_candidate_creation_preserves_one_verified_version(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.derive(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.state_rows()), 3)
        candidate = results[0]['policies']['selective']
        artifact, record = model._load_artifact(self.state, candidate['run_id'])
        self.assertEqual(artifact['weights'], {'sparse': 1.0})
        self.assertEqual(json.loads((Path(record['artifact_path']) / 'manifest.json').read_text()),
                         json.loads(record['manifest_json']))


if __name__ == '__main__':
    unittest.main()
