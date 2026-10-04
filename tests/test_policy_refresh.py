"""Batched policy refresh, partial failure, and concurrent-ingestion regressions."""
import json
import gc
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.load = self.stack.enter_context(patch.object(model, '_load_artifact', side_effect=lambda db, run, **kwargs: (
            {'weights': {}, 'model_revision': self.encoder.model_revision},
            {'run_id': run, 'model_revision': self.encoder.model_revision},
        )))
        self.stack.enter_context(patch.object(model, '_optional_ml_modules', return_value={}))
        self.encoder_factory = self.stack.enter_context(patch.object(model, 'encoder_for_recorded_revision', return_value=self.encoder))
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

    def journal_modes(self):
        modes = []
        for path in (self.jobs, self.state):
            with closing(sqlite3.connect(path)) as con:
                modes.append(con.execute('PRAGMA journal_mode').fetchone()[0])
        return modes

    def test_full_refresh_both_policies_and_unchanged_resume(self):
        self.assertEqual(self.journal_modes(), ['delete', 'delete'])
        with patch.object(model, 'prepare_state', wraps=model.prepare_state) as prepare:
            result = self.run_refresh()
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(result['dedupe'], {'reused': True})
        self.assertEqual(self.journal_modes(), ['wal', 'wal'])
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
        self.assertEqual(self.journal_modes(), ['delete', 'delete'])
        wrote = False
        def insert_during_score(*args):
            nonlocal wrote
            if not wrote:
                wrote = True
                with closing(sqlite3.connect(self.jobs, timeout=0)) as con, con:
                    con.execute("INSERT INTO jobs(ats,id,title,description,last_seen) VALUES ('ashby','4','New role','New description','2026-09-29')")
                    con.execute("UPDATE jobs SET description='Changed while scoring' WHERE id='3'")
                with closing(sqlite3.connect(self.state, timeout=0)) as con, con:
                    con.execute("INSERT INTO preference_state VALUES ('concurrent_fixture','committed')")
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

    def configure_sparse(self):
        self.load.side_effect = lambda db, run, **kwargs: (
            {'weights': {'sparse': 1.0}, 'model_revision': self.encoder.model_revision},
            {'run_id': run, 'model_revision': self.encoder.model_revision,
             'manifest_json': json.dumps({'artifacts': {'model.pkl': 'a' * 64}})},
        )
        def sparse_predictions(state, documents, artifact, modules, *, active_components_only=False):
            self.assertTrue(active_components_only)
            return [dict(dense_linear=None, dense_neighbor=None, sparse=.8, final=.8,
                         similar_liked_family_ids=[], positive_sparse_phrases=[],
                         computed_components=['sparse']) for _ in documents]
        self.predict.side_effect = sparse_predictions

    def test_sparse_selected_policy_needs_no_encoder_or_other_mapping(self):
        self.configure_sparse()
        with patch.object(refresh, 'policy_runs', return_value={'broad': 'run_b'}), \
             patch.object(model, 'embed_documents', side_effect=AssertionError('must not embed')):
            result = self.run_refresh(policies=('broad',), active_components_only=True, no_embeddings=True)
        self.encoder_factory.assert_not_called()
        self.assertEqual(result['selected_policies'], ['broad'])
        self.assertEqual(result['embeddings'], {})
        self.assertEqual(set(result['policies']), {'broad'})
        self.assertEqual({row[0] for row in self.scores()}, {'run_b'})
        self.assertEqual(set(self.receipts()), {'policy_refresh:broad'})
        self.assertEqual(self.load.call_count, 2)  # Recheck identity before certification.
        self.assertTrue(all(call.args == (self.state, 'run_b') and call.kwargs == {'prepare_schema': False}
                            for call in self.load.call_args_list))

    def test_selected_policy_leaves_other_scores_and_receipts_unchanged(self):
        self.run_refresh()
        selective_scores = [row for row in self.scores() if row[0] == 'run_a']
        selective_receipt = self.receipts()['policy_refresh:selective']
        with sqlite3.connect(self.jobs) as con:
            con.execute("DELETE FROM jobs WHERE id='3'")
        self.configure_sparse()
        self.encoder_factory.reset_mock()
        result = self.run_refresh(policies=('broad',), active_components_only=True, no_embeddings=True)
        self.assertEqual([row for row in self.scores() if row[0] == 'run_a'], selective_scores)
        self.assertEqual(self.receipts()['policy_refresh:selective'], selective_receipt)
        self.assertEqual(len([row for row in self.scores() if row[0] == 'run_b']), 2)
        self.assertEqual(set(result['policies']), {'broad'})
        self.encoder_factory.assert_not_called()

    def test_both_sparse_policies_publish_independent_complete_scores_without_embeddings(self):
        self.configure_sparse()
        with patch.object(model, 'embed_documents', side_effect=AssertionError('must not embed')):
            result = self.run_refresh(policies=('broad', 'selective'), active_components_only=True, no_embeddings=True)
        self.encoder_factory.assert_not_called()
        self.train.assert_not_called()
        self.assertEqual(result['embeddings'], {})
        self.assertEqual(len(self.scores()), 6)
        for policy, run_id in self.runs.items():
            receipt = result['policies'][policy]
            self.assertEqual(receipt['run_id'], run_id)
            self.assertEqual(receipt['result']['scored_families'], 3)
        self.predict.reset_mock()
        result = self.run_refresh(policies=('broad', 'selective'), active_components_only=True, no_embeddings=True)
        self.predict.assert_not_called()
        self.assertTrue(all(r['result']['updated_families'] == 0 for r in result['policies'].values()))

    def test_dense_selective_blocks_both_policy_refresh_before_any_broad_write(self):
        self.configure_sparse()
        sparse_loader = self.load.side_effect
        def mixed_loader(db, run, **kwargs):
            artifact, record = sparse_loader(db, run, **kwargs)
            if run == self.runs['selective']:
                artifact['weights'] = {'dense_linear': 1.0}
            return artifact, record
        self.load.side_effect = mixed_loader
        before = self.state.read_bytes(), self.jobs.read_bytes()
        with self.assertRaisesRegex(ValueError, 'require embeddings: selective'):
            self.run_refresh(policies=('broad', 'selective'), active_components_only=True, no_embeddings=True)
        self.assertEqual((self.state.read_bytes(), self.jobs.read_bytes()), before)
        self.encoder_factory.assert_not_called()
        self.predict.assert_not_called()

    def test_sparse_sample_does_not_publish_or_certify(self):
        self.configure_sparse()
        result = self.run_refresh(policies=('broad',), active_components_only=True,
                                  no_embeddings=True, sample_size=2)
        self.assertEqual(result['status'], 'sample_passed')
        self.assertEqual(result['policies']['broad']['tested_families'], 2)
        self.assertEqual(self.scores(), [])
        self.assertEqual(self.receipts(), {})
        self.encoder_factory.assert_not_called()

    def test_no_embeddings_rejects_dense_artifact_before_mutation(self):
        self.load.side_effect = lambda db, run, **kwargs: (
            {'weights': {'dense_linear': 1.0}, 'model_revision': self.encoder.model_revision},
            {'run_id': run, 'model_revision': self.encoder.model_revision},
        )
        before = self.state.read_bytes()
        jobs_before = self.jobs.read_bytes()
        self.assertEqual(self.journal_modes(), ['delete', 'delete'])
        with patch.object(model, 'prepare_state') as prepare, \
             patch.object(model, 'connect_state') as connect:
            with self.assertRaisesRegex(ValueError, 'require embeddings: broad'):
                self.run_refresh(policies=('broad',), active_components_only=True, no_embeddings=True)
        prepare.assert_not_called()
        connect.assert_not_called()
        self.encoder_factory.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.jobs.read_bytes(), jobs_before)
        self.assertEqual(self.journal_modes(), ['delete', 'delete'])

    def test_busy_wal_activation_fails_before_snapshot_or_inference_and_cli_retries(self):
        enable_wal = refresh._enable_refresh_wal
        with closing(sqlite3.connect(self.jobs)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT COUNT(*) FROM jobs').fetchone()
            with patch.object(refresh, '_enable_refresh_wal', side_effect=lambda path: enable_wal(path, timeout=0)), \
                    patch.object(model, 'prepare_state') as prepare:
                with self.assertRaisesRegex(refresh.RefreshDatabaseBusyError, 'retry WAL activation'):
                    self.run_refresh()
            prepare.assert_not_called()
        self.assertEqual(self.journal_modes(), ['delete', 'delete'])
        self.encoder_factory.assert_not_called()
        self.predict.assert_not_called()
        with patch.object(refresh, 'refresh_policies', side_effect=refresh.RefreshDatabaseBusyError('busy')), \
                patch('builtins.print'):
            self.assertEqual(refresh.main(['--db', str(self.jobs), '--state-db', str(self.state),
                                          '--proxy-db', str(self.root / 'proxy.db')]), 75)

    def test_wal_activation_does_not_create_a_missing_catalog(self):
        self.jobs.unlink()
        with patch.object(model, 'prepare_state') as prepare:
            with self.assertRaises(sqlite3.OperationalError):
                self.run_refresh()
        self.assertFalse(self.jobs.exists())
        prepare.assert_not_called()
        self.encoder_factory.assert_not_called()

    def test_unsupported_wal_fails_closed_and_closes_initializer(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = ('delete',)
        with patch.object(refresh.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'requires WAL'):
                with refresh._enable_refresh_wal(self.jobs):
                    self.fail('entered refresh without WAL')
        connection.close.assert_called_once()

    def test_read_only_catalog_remains_readable_after_wal_initializer_closes(self):
        gc.collect()
        with refresh._enable_refresh_wal(self.jobs):
            pass
        gc.collect()
        with closing(model.connect_source(self.jobs)) as source:
            self.assertEqual(source.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 3)

    def test_preparation_busy_retries_but_ordinary_errors_remain_permanent(self):
        cases = (
            (sqlite3.OperationalError('database is locked'), 75),
            (sqlite3.OperationalError('database is busy'), 75),
            (sqlite3.OperationalError('no such table: jobs'), 78),
            (ValueError('locked model configuration'), 78),
        )
        for failure, expected in cases:
            with self.subTest(failure=str(failure)), \
                    patch('job_search.collection.dedupe.prepared_families_are_current', return_value=False), \
                    patch('job_search.collection.dedupe.prepare_families', side_effect=failure), \
                    patch('builtins.print'):
                self.assertEqual(refresh.main(['--db', str(self.jobs), '--state-db', str(self.state),
                                              '--proxy-db', str(self.root / 'proxy.db')]), expected)
        self.encoder_factory.assert_not_called()
        self.predict.assert_not_called()

    def test_policy_selection_rejects_empty_duplicate_and_unknown_names(self):
        for policies in ((), ('broad', 'broad'), ('unknown',)):
            with self.subTest(policies=policies), self.assertRaisesRegex(ValueError, 'unique supported'):
                self.run_refresh(policies=policies)
        self.load.assert_not_called()

    def test_cli_forwards_explicit_sparse_mode(self):
        with patch.object(refresh, 'refresh_policies', return_value={'status': 'ready'}) as run, \
             patch('job_search.inference.configured_inference_path', side_effect=AssertionError('CPU ranking must not resolve provider configuration')), \
             patch('builtins.print'):
            self.assertEqual(refresh.main(['--db', str(self.jobs), '--state-db', str(self.state),
                '--proxy-db', str(self.root / 'proxy.db'), '--policy', 'broad',
                '--active-components-only', '--no-embeddings']), 0)
        self.assertEqual(run.call_args.kwargs['policies'], ['broad'])
        self.assertTrue(run.call_args.kwargs['active_components_only'])
        self.assertTrue(run.call_args.kwargs['no_embeddings'])
        self.assertIsNone(run.call_args.kwargs['inference_config'])

    def test_inspection_requires_receipt_for_current_model_and_complete_coverage(self):
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.jobs) as con:
            con.execute('UPDATE jobs SET last_seen=?', (stamp,))
        self.run_refresh()
        (self.root / 'model.pkl').write_bytes(b'test artifact presence')
        with sqlite3.connect(self.state) as con:
            con.execute('UPDATE preference_model_runs SET artifact_path=?', (str(self.root),))
        def inspect():
            return refresh.inspect_policies(self.state, self.root / 'proxy.db', self.jobs)['selective']
        self.assertEqual(inspect()['status'], 'ready')
        self.assertTrue(inspect()['freshness_verified'])
        with sqlite3.connect(self.state) as con:
            original = con.execute("SELECT value FROM preference_state WHERE key='policy_refresh:selective'").fetchone()[0]
            receipt = json.loads(original)
            receipt['run_id'] = 'old_dense_run'
            con.execute("UPDATE preference_state SET value=? WHERE key='policy_refresh:selective'", (json.dumps(receipt),))
        self.assertEqual(inspect()['status'], 'configured_unverified')
        self.assertFalse(inspect()['freshness_verified'])
        with sqlite3.connect(self.state) as con:
            con.execute("UPDATE preference_state SET value=? WHERE key='policy_refresh:selective'", (original,))
            con.execute('DELETE FROM preference_scores WHERE run_id=? AND family_id=(SELECT MIN(family_id) FROM preference_scores WHERE run_id=?)', (self.runs['selective'], self.runs['selective']))
        self.assertEqual(inspect()['status'], 'configured_unverified')
        self.assertFalse(inspect()['freshness_verified'])

    def sparse_refresh(self, **kwargs):
        return self.run_refresh(active_components_only=True, no_embeddings=True, **kwargs)

    def test_python_runtime_change_invalidates_sparse_score_reuse(self):
        from types import SimpleNamespace
        from job_search.ranking import refresh_freshness
        self.configure_sparse()
        self.sparse_refresh()
        self.assertTrue(self.sparse_refresh()['reused'])
        current = refresh_freshness.sys
        changed = SimpleNamespace(implementation=current.implementation,
                                  version_info=(*current.version_info[:2], current.version_info[2] + 1))
        with patch.object(refresh_freshness, 'sys', changed):
            result = self.sparse_refresh()
        self.assertFalse(result.get('reused', False))
        self.assertEqual([p['result']['updated_families'] for p in result['policies'].values()], [3, 3])

    def progress(self):
        with sqlite3.connect(self.state) as con:
            return json.loads(con.execute("SELECT value FROM preference_state WHERE key='policy_refresh_progress'").fetchone()[0])

    def test_sparse_reuse_skips_every_catalog_scan_and_keeps_completion_receipts(self):
        self.configure_sparse()
        self.sparse_refresh()
        before, receipts = self.scores(), self.receipts()
        with patch.object(model, 'iter_family_document_batches', side_effect=AssertionError('document scan')), \
             patch.object(model, 'prepare_state', side_effect=AssertionError('feature cache migration scan')), \
             patch.object(model, '_optional_ml_modules', side_effect=AssertionError('ML initialization')), \
             patch('job_search.collection.dedupe.prepared_families_are_current', side_effect=AssertionError('fingerprint scan')), \
             patch('job_search.collection.dedupe.prepare_families', side_effect=AssertionError('preparation')):
            result = self.sparse_refresh()
        self.assertTrue(result['reused'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['checked_families'], 0)
        self.assertEqual(result['processed_families'], 0)
        self.assertEqual(result['total_families'], 3)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual(self.scores(), before)
        self.assertTrue(all(p['result']['reused_families'] == 3 for p in result['policies'].values()))
        self.assertEqual(self.progress()['embeddings'], {})
        self.assertEqual(self.progress()['checked_families'], 0)

    def test_same_watermark_source_edits_invalidate_sparse_reuse(self):
        self.configure_sparse()
        self.sparse_refresh()
        for assignment in ("description='Changed description'", "department='New department'", "team='New team'", "location='Elsewhere'", "closed_at='2026-10-01'"):
            with self.subTest(assignment=assignment):
                with sqlite3.connect(self.jobs) as con:
                    con.execute('UPDATE jobs SET ' + assignment + " WHERE id='2'")
                result = self.sparse_refresh()
                self.assertFalse(result['reused'])
                self.assertEqual(result['checked_families'], 3)
                self.assertTrue(self.sparse_refresh()['reused'])

    def test_new_deleted_and_reassigned_families_do_not_reuse_by_count(self):
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.jobs) as con:
            con.execute("DELETE FROM jobs WHERE id='3'")
            con.execute("INSERT INTO jobs(ats,id,title,description,last_seen) VALUES ('ashby','4','New role','New work','2026-09-28')")
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertEqual(result['total_families'], 3)
        with sqlite3.connect(self.jobs) as con:
            con.execute("UPDATE job_family_members SET family_id='invalid' WHERE job_id='2'")
        self.assertFalse(self.sparse_refresh()['reused'])
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_tampered_score_with_same_fingerprint_is_recomputed(self):
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.state) as con:
            con.execute("UPDATE preference_scores SET final_score=0.01 WHERE run_id='run_b'")
        self.predict.reset_mock()
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertEqual(result['policies']['broad']['result']['updated_families'], 3)
        self.assertEqual(result['policies']['selective']['result']['updated_families'], 0)
        with sqlite3.connect(self.state) as con:
            self.assertEqual(con.execute("SELECT DISTINCT final_score FROM preference_scores WHERE run_id='run_b'").fetchall(), [(0.8,)])
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_deleted_scores_and_malformed_explanations_are_repaired(self):
        self.configure_sparse()
        self.sparse_refresh()
        for statement in ("DELETE FROM preference_scores WHERE run_id='run_b' AND family_id=(SELECT MIN(family_id) FROM preference_scores)",
                          "UPDATE preference_scores SET explanation_json='invalid' WHERE run_id='run_b'"):
            with self.subTest(statement=statement):
                with sqlite3.connect(self.state) as con:
                    con.execute(statement)
                result = self.sparse_refresh()
                self.assertFalse(result['reused'])
                self.assertEqual(result['policies']['broad']['result']['scored_families'], 3)
                self.assertTrue(self.sparse_refresh()['reused'])

    def test_unrelated_policy_repairs_leave_other_policy_reusable(self):
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.state) as con:
            con.execute("UPDATE preference_scores SET sparse_score=0.01 WHERE run_id='run_b'")
        self.assertTrue(self.sparse_refresh(policies=('selective',))['reused'])
        self.assertFalse(self.sparse_refresh(policies=('broad',))['reused'])
        self.assertTrue(self.sparse_refresh(policies=('selective',))['reused'])

    def test_artifact_change_invalidates_cached_rows_and_full_receipt(self):
        self.configure_sparse()
        self.sparse_refresh()
        original = self.load.side_effect
        def changed(db, run, **kwargs):
            artifact, record = original(db, run, **kwargs)
            if run == 'run_b':
                record['manifest_json'] = json.dumps({'artifacts': {'model.pkl': 'b' * 64}})
            return artifact, record
        self.load.side_effect = changed
        result = self.sparse_refresh()
        self.assertEqual(result['policies']['broad']['result']['updated_families'], 3)
        self.assertEqual(result['policies']['selective']['result']['updated_families'], 0)
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_dropped_tracking_trigger_invalidates_receipt_and_rotates_epoch(self):
        self.configure_sparse()
        self.sparse_refresh()
        prior = self.receipts()['policy_refresh:broad']['reuse']['source']['epoch']
        with sqlite3.connect(self.jobs) as con:
            con.execute('DROP TRIGGER ranking_refresh_source_jobs_update')
            con.execute("UPDATE jobs SET team='Edited while untracked' WHERE id='2'")
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertNotEqual(result['policies']['broad']['reuse']['source']['epoch'], prior)
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_sparse_partial_failure_keeps_committed_checkpoint_and_resumes(self):
        self.configure_sparse()
        predict = self.predict.side_effect
        calls = 0
        def fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError('batch failure')
            return predict(*args, **kwargs)
        self.predict.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'batch failure'):
            self.sparse_refresh()
        self.assertEqual(self.receipts(), {})
        previous = self.scores()
        self.assertEqual(len(previous), 2)
        self.predict.side_effect = predict
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertTrue(all(r in self.scores() for r in previous))
        self.assertTrue(all(p['result']['updated_families'] == 2 for p in result['policies'].values()))

    def test_interrupted_forced_repair_restarts_until_all_untrusted_rows_are_replaced(self):
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.state) as con:
            con.execute("UPDATE preference_scores SET final_score=0.01 WHERE run_id='run_b'")
        predict = self.predict.side_effect
        calls = 0
        def fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('repair interrupted')
            return predict(*args, **kwargs)
        self.predict.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'repair interrupted'):
            self.sparse_refresh()
        self.predict.side_effect = predict
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertEqual(result['policies']['broad']['result']['updated_families'], 3)
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_noop_after_pruning_reports_zero_new_removals(self):
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.jobs) as con:
            con.execute("DELETE FROM jobs WHERE id='3'")
        changed = self.sparse_refresh()
        self.assertTrue(all(r['result']['removed_families'] == 1 for r in changed['policies'].values()))
        reused = self.sparse_refresh()
        self.assertTrue(all(r['result']['removed_families'] == 0 for r in reused['policies'].values()))
        self.assertTrue(all(r['result']['removed_families'] == 1 for r in self.receipts().values()))

    def test_unsealed_sample_and_incomplete_receipt_cannot_skip_full_pass(self):
        self.configure_sparse()
        self.sparse_refresh(sample_size=2)
        self.assertFalse(self.sparse_refresh()['reused'])
        with sqlite3.connect(self.state) as con:
            key = 'policy_refresh:broad'
            receipt = json.loads(con.execute('SELECT value FROM preference_state WHERE key=?', (key,)).fetchone()[0])
            receipt['reuse']['complete'] = False
            con.execute('UPDATE preference_state SET value=? WHERE key=?', (json.dumps(receipt), key))
        self.assertFalse(self.sparse_refresh()['reused'])

    def test_malformed_receipt_metadata_falls_back_to_validation(self):
        self.configure_sparse()
        self.sparse_refresh()
        for field, value in (('source_watermark', {}), ('completed_at', 'invalid')):
            with self.subTest(field=field):
                with sqlite3.connect(self.state) as con:
                    key = 'policy_refresh:broad'
                    receipt = json.loads(con.execute('SELECT value FROM preference_state WHERE key=?', (key,)).fetchone()[0])
                    receipt[field] = value
                    con.execute('UPDATE preference_state SET value=? WHERE key=?', (json.dumps(receipt), key))
                self.assertFalse(self.sparse_refresh()['reused'])
        with sqlite3.connect(self.state) as con:
            receipt = json.loads(con.execute("SELECT value FROM preference_state WHERE key='policy_refresh:broad'").fetchone()[0])
            del receipt['source_watermark']
            con.execute("UPDATE preference_state SET value=? WHERE key='policy_refresh:broad'", (json.dumps(receipt),))
        self.assertFalse(self.sparse_refresh()['reused'])

    def test_invalid_score_tracker_cannot_match_null_checkpoint(self):
        from job_search.ranking import refresh_freshness as freshness
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.state) as con:
            checkpoint = freshness.read_value(con, freshness.cache_key('run_b'))
            checkpoint['scores'] = None
            model._set_state(con, freshness.cache_key('run_b'), json.dumps(checkpoint))
            con.execute('DROP TRIGGER ranking_refresh_scores_preference_scores_update')
            self.assertFalse(freshness.cache_matches(con, self.state, 'run_b', checkpoint['signature']))
            with self.assertRaisesRegex(ValueError, 'tracker changed'):
                freshness.cache_receipt(con, self.state, 'run_b', checkpoint['signature'])
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertEqual(result['policies']['broad']['result']['updated_families'], 3)

    def test_run_id_edit_invalidates_both_score_scopes(self):
        from job_search.ranking import refresh_freshness as freshness
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.state) as con:
            before = {run: freshness.revision_token(con, self.state, 'scores', run) for run in self.runs.values()}
            family = con.execute("SELECT MIN(family_id) FROM preference_scores WHERE run_id='run_a'").fetchone()[0]
            con.execute("DELETE FROM preference_scores WHERE run_id='run_b' AND family_id=?", (family,))
            con.execute("UPDATE preference_scores SET run_id='run_b' WHERE run_id='run_a' AND family_id=?", (family,))
            after = {run: freshness.revision_token(con, self.state, 'scores', run) for run in self.runs.values()}
        self.assertTrue(all(before[run] != after[run] for run in self.runs.values()))
        self.assertFalse(self.sparse_refresh()['reused'])
        self.assertEqual(len(self.scores()), 6)

    def test_concurrent_score_mutation_cannot_be_certified(self):
        self.configure_sparse()
        iterator = model.iter_family_document_batches
        def changed(*args, **kwargs):
            for index, batch in enumerate(iterator(*args, **kwargs)):
                if index == 1:
                    with sqlite3.connect(self.state) as con:
                        con.execute("UPDATE preference_scores SET final_score=0.01 WHERE run_id='run_b'")
                yield batch
        with patch.object(model, 'iter_family_document_batches', side_effect=changed):
            with self.assertRaisesRegex(refresh.RefreshDatabaseBusyError, 'scores changed'):
                self.sparse_refresh()
        self.assertEqual(self.receipts(), {})
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertEqual(result['policies']['broad']['result']['updated_families'], 3)

    def test_source_mutation_during_scoring_seals_only_original_snapshot(self):
        self.configure_sparse()
        predict = self.predict.side_effect
        wrote = False
        def changed(*args, **kwargs):
            nonlocal wrote
            if not wrote:
                wrote = True
                with closing(sqlite3.connect(self.jobs, timeout=0)) as con, con:
                    con.execute("UPDATE jobs SET team='Concurrent team' WHERE id='2'")
            return predict(*args, **kwargs)
        self.predict.side_effect = changed
        self.sparse_refresh()
        self.predict.side_effect = predict
        self.assertFalse(self.sparse_refresh()['reused'])
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_changed_model_before_certification_cannot_publish_receipt(self):
        self.configure_sparse()
        original = self.load.side_effect
        calls = 0
        def changed(*args, **kwargs):
            nonlocal calls
            calls += 1
            artifact, record = original(*args, **kwargs)
            if calls > 2:
                record['manifest_json'] = json.dumps({'artifacts': {'model.pkl': 'b' * 64}})
            return artifact, record
        self.load.side_effect = changed
        with self.assertRaisesRegex(refresh.RefreshDatabaseBusyError, 'model changed'):
            self.sparse_refresh()
        self.assertEqual(self.receipts(), {})

    def test_legacy_completion_requires_one_upgrade_pass(self):
        self.configure_sparse()
        self.sparse_refresh()
        with sqlite3.connect(self.state) as con:
            for key, value in con.execute("SELECT key,value FROM preference_state WHERE key LIKE 'policy_refresh:%'").fetchall():
                value = json.loads(value)
                value.pop('reuse')
                con.execute('UPDATE preference_state SET value=? WHERE key=?', (json.dumps(value), key))
            con.execute("DELETE FROM preference_state WHERE key LIKE 'policy_refresh_cache:%'")
        result = self.sparse_refresh()
        self.assertFalse(result['reused'])
        self.assertTrue(all(p['result']['updated_families'] == 3 for p in result['policies'].values()))
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_replaced_catalog_cannot_reuse_old_database_identity(self):
        self.configure_sparse()
        self.sparse_refresh()
        replacement = self.root / 'replacement.db'
        with closing(sqlite3.connect(self.jobs)) as source, closing(sqlite3.connect(replacement)) as destination:
            source.backup(destination)
        replacement.replace(self.jobs)
        self.assertFalse(self.sparse_refresh()['reused'])
        self.assertTrue(self.sparse_refresh()['reused'])

    def test_full_description_edit_between_embedding_chunks_changes_fingerprint(self):
        from dataclasses import replace
        document = model.build_feature_document({'family_id': 'family', 'title': 'Engineer', 'description': 'Original text'})
        changed = replace(document, description_text='Different sparse text', description_chunks=document.description_chunks)
        self.assertNotEqual(document.fingerprint, changed.fingerprint)

    def test_progress_has_exact_worker_attempt_identity_without_inference_scope(self):
        self.configure_sparse()
        with patch.dict(os.environ, {'JOB_SEARCH_INVOCATION_WORK': 'work_' + 'a' * 32, 'JOB_SEARCH_INVOCATION_REVISION': '27'}):
            self.sparse_refresh()
        self.assertEqual(self.progress()['invocation_work_id'], 'work_' + 'a' * 32)
        self.assertEqual(self.progress()['invocation_revision'], 27)
        with patch.dict(os.environ, {'JOB_SEARCH_INVOCATION_WORK': 'arbitrary', 'JOB_SEARCH_INVOCATION_REVISION': '-1'}):
            self.sparse_refresh()
        self.assertNotIn('invocation_work_id', self.progress())


if __name__ == '__main__':
    unittest.main()
