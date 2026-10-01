"""Maintain explicitly selected personal policies without retraining or promotion."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

POLICIES = ('selective','broad')

def policy_runs(proxy_db: Path) -> dict[str,str]:
    if not proxy_db.is_file(): return {}
    with sqlite3.connect(proxy_db.resolve().as_uri()+'?mode=ro',uri=True) as con:
        rows=con.execute('SELECT s.policy_id,s.model_run_id FROM proxy_students s JOIN proxy_runs r ON r.run_id=s.run_id ORDER BY r.created_at DESC,s.trained_at DESC').fetchall()
    result={}
    for policy,run in rows:
        if policy in POLICIES: result.setdefault(policy,run)
    return result

def inspect_policies(preference_db: Path, proxy_db: Path, jobs_db: Path | None = None) -> dict:
    try:
        runs=policy_runs(proxy_db)
        result={}
        watermark = None
        if jobs_db and jobs_db.is_file():
            with sqlite3.connect(jobs_db.resolve().as_uri()+'?mode=ro', uri=True) as jobs:
                watermark = jobs.execute('SELECT MAX(last_seen) FROM jobs').fetchone()[0]
        with sqlite3.connect(preference_db.resolve().as_uri()+'?mode=ro',uri=True) as con:
            for policy in POLICIES:
                run=runs.get(policy)
                row=con.execute('SELECT artifact_path,manifest_json FROM preference_model_runs WHERE run_id=?',(run,)).fetchone()
                count=con.execute('SELECT COUNT(*) FROM preference_scores WHERE run_id=?',(run,)).fetchone()[0] if row else 0
                exists=bool(row and (Path(row[0])/'model.pkl').is_file())
                state=con.execute('SELECT value FROM preference_state WHERE key=?',('policy_refresh:'+policy,)).fetchone()
                receipt=json.loads(state[0]) if state else None
                latest = con.execute('SELECT MAX(scored_at) FROM preference_scores WHERE run_id=?', (run,)).fetchone()[0] if row else None
                stale = False
                if watermark:
                    observed = datetime.fromisoformat(watermark.replace('Z','+00:00'))
                    if observed.tzinfo is None: observed = observed.replace(tzinfo=timezone.utc)
                    stale = (datetime.now(timezone.utc)-observed).total_seconds() > 172800
                if receipt and watermark and receipt.get('source_watermark') != watermark:
                    stale = True
                result[policy]={'run_id':run,'artifact_present':exists,'score_count':count,
                    'status':'blocked' if not exists else ('stale' if stale else 'configured_unverified' if jobs_db and not receipt else 'ready') if count else 'configured_unverified',
                    'reason': 'missing_policy_or_artifact' if not exists else ('catalog_or_scores_stale' if stale else 'refresh_not_verified' if jobs_db and not receipt else 'scores_available') if count else 'awaiting_refresh',
                    'source_last_seen':watermark, 'latest_score_at':latest, 'freshness_verified':bool(receipt),
                    'last_refresh':receipt}
        return result
    except (OSError,sqlite3.Error,ValueError):
        return {p:{'status':'blocked','reason':'ranking_state_unavailable'} for p in POLICIES}

def refresh_policies(
    jobs_db: Path, preference_db: Path, proxy_db: Path, *, device='auto',
    inference_config=None, batch_size=256, sample_size=None,
) -> dict:
    """Embed and score a stable catalog view, saving completed batches.

    Samples exercise the real encoder and both models without publishing scores,
    pruning the corpus, or creating a whole-catalog completion receipt.
    """
    from contextlib import closing
    from . import model
    from job_search.collection.dedupe import prepare_families, prepared_families_are_current
    if not 1 <= batch_size <= 800:
        raise ValueError('batch_size must be between 1 and 800')
    if sample_size is not None and not 1 <= sample_size <= 800:
        raise ValueError('sample_size must be between 1 and 800')
    runs = policy_runs(proxy_db)
    missing = set(POLICIES) - set(runs)
    if missing:
        raise ValueError('missing policy mappings: ' + ', '.join(sorted(missing)))
    records = {policy: model._load_artifact(preference_db, runs[policy]) for policy in POLICIES}
    modules = model._optional_ml_modules()
    started = datetime.now(timezone.utc).isoformat()
    progress = {'status': 'preparing', 'started_at': started, 'processed_families': 0,
                'total_families': None, 'batches': 0, 'sample': sample_size is not None}

    def report(**values):
        progress.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        with model.connect_state(preference_db) as state:
            model._set_state(state, 'policy_sample_progress' if sample_size is not None
                             else 'policy_refresh_progress', json.dumps(progress, sort_keys=True))

    model.prepare_state(preference_db)
    report()
    try:
        dedupe = None
        if sample_size is None:
            dedupe = {'reused': True} if prepared_families_are_current(jobs_db) else prepare_families(jobs_db)
        with closing(model.connect_source(jobs_db)) as source, closing(model.connect_state(preference_db)) as state:
            # One SQLite read transaction binds validation, watermark, features,
            # and both policies to the same view while ingestion can continue in WAL.
            source.execute('BEGIN')
            model.validate_family_schema(source, require_complete=sample_size is None)
            watermark = source.execute('SELECT MAX(last_seen) FROM jobs').fetchone()[0]
            total = source.execute('SELECT COUNT(*) FROM job_families').fetchone()[0]
            family_ids = None
            if sample_size is not None:
                ids = [row[0] for row in source.execute('SELECT family_id FROM job_families ORDER BY family_id')]
                size = min(sample_size, len(ids))
                family_ids = [ids[round(i * (len(ids) - 1) / max(1, size - 1))] for i in range(size)]
                total = size
            report(status='embedding_and_scoring', total_families=total, source_watermark=watermark)
            state.execute('CREATE TEMP TABLE refresh_families (family_id TEXT PRIMARY KEY)')
            encoders = {}
            embedded = {}
            results = {policy: {'run_id': runs[policy], 'updated_families': 0} for policy in POLICIES}
            for documents in model.iter_family_document_batches(
                jobs_db, batch_size=batch_size, connection=source, family_ids=family_ids,
            ):
                for policy in POLICIES:
                    artifact, record = records[policy]
                    revision = str(record['model_revision'])
                    if revision not in encoders:
                        encoders[revision] = model.encoder_for_recorded_revision(revision, device, inference_config)
                        embedded[revision] = {'documents': 0, 'unique_texts': 0, 'embedded_texts': 0}
                for revision, encoder in encoders.items():
                    result = model.embed_documents(preference_db, documents, encoder,
                                                   prepare_schema=False, activate_revision=sample_size is None)
                    for key in embedded[revision]:
                        embedded[revision][key] += result[key]
                # Embedding transactions finish before the score writer begins.
                with state:
                    for policy in POLICIES:
                        artifact, record = records[policy]
                        if sample_size is not None:
                            predictions = model._score_document_batch(preference_db, documents, artifact, modules)
                            results[policy]['tested_families'] = results[policy].get('tested_families', 0) + len(predictions)
                        else:
                            results[policy]['updated_families'] += model.score_and_store_batch(
                                state, preference_db, documents, artifact, record, modules,
                            )
                    state.executemany('INSERT INTO refresh_families VALUES (?)', [(d.family_id,) for d in documents])
                report(processed_families=progress['processed_families'] + len(documents),
                       batches=progress['batches'] + 1, embeddings=embedded, policies=results)
            if progress['processed_families'] != total:
                raise model.PreferenceModelError('catalog snapshot did not yield every expected family')
            # Only a completed full pass may prune obsolete scores and certify freshness.
            receipts = {}
            if sample_size is None:
                with state:
                    for policy in POLICIES:
                        results[policy]['removed_families'] = state.execute(
                            'DELETE FROM preference_scores WHERE run_id=? AND family_id NOT IN '
                            '(SELECT family_id FROM refresh_families)', (runs[policy],),
                        ).rowcount
                        results[policy]['scored_families'] = state.execute(
                            'SELECT COUNT(*) FROM preference_scores WHERE run_id=?', (runs[policy],),
                        ).fetchone()[0]
                        if results[policy]['scored_families'] != total:
                            raise model.PreferenceModelError('score coverage does not match the completed catalog snapshot')
                        receipts[policy] = {'run_id': runs[policy], 'source_watermark': watermark,
                            'completed_at': datetime.now(timezone.utc).isoformat(), 'result': results[policy]}
                        model._set_state(state, 'policy_refresh:' + policy, json.dumps(receipts[policy], sort_keys=True))
            report(status='succeeded')
            return {'status': 'sample_passed' if sample_size is not None else 'ready',
                    'policies': receipts or results, 'dedupe': dedupe, 'embeddings': embedded,
                    'processed_families': progress['processed_families']}
    except Exception as exc:
        report(status='failed', error_type=type(exc).__name__)
        raise

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('db','state-db','proxy-db'):
        parser.add_argument('--'+flag,type=Path,required=True)
    parser.add_argument('--inference-config',type=Path)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--sample-size', type=int, help='Test up to 800 distributed families without publishing scores')
    parser.add_argument('--device',default='auto',choices=('auto','cpu','mps','cuda'))
    args=parser.parse_args(argv)
    from job_search.inference import configured_inference_path
    from job_search.inference.providers import InferenceTransportError
    try:
        result=refresh_policies(args.db,args.state_db,args.proxy_db,device=args.device,
            inference_config=configured_inference_path(args.inference_config),
            batch_size=args.batch_size, sample_size=args.sample_size)
    except InferenceTransportError as exc:
        if getattr(exc,'defer_without_attempt',False):
            import sys
            print(json.dumps({'type':'inference_usage_deferred','reason':exc.reason_code,'retry_at':exc.retry_at}),file=sys.stderr)
            return 76
        return 75 if exc.retryable else 78
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        import sys
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        if 'jobs have no current opportunity family' in str(exc):
            return 75
        return 78
    print(json.dumps(result,default=str));return 0

if __name__=='__main__': raise SystemExit(main())
