"""Maintain explicitly selected personal policies without retraining or promotion."""
from __future__ import annotations
import argparse
from contextlib import closing, contextmanager
import json
import os
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

POLICIES = ('selective','broad')


class RefreshDatabaseBusyError(sqlite3.OperationalError):
    """A ranking database could not enter WAL yet; retry without taking a snapshot."""


@contextmanager
def _enable_refresh_wal(path: Path, *, timeout: float = 5):
    """Enable WAL and keep its sidecars available to read-only snapshot readers."""
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=timeout)) as con:
        try:
            mode = con.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if str(mode).lower() != 'wal':
                raise ValueError('ranking refresh requires WAL journal mode for concurrent ingestion')
            # Older SQLite builds need existing sidecars for mode=ro readers.
            # This read initializes them without holding a transaction or writer
            # lock; the connection stays alive only until refresh exits.
            con.execute('SELECT name FROM sqlite_master LIMIT 1').fetchall()
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                raise RefreshDatabaseBusyError('ranking database is busy; retry WAL activation') from exc
            raise
        yield


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
        total_families = None
        if jobs_db and jobs_db.is_file():
            with sqlite3.connect(jobs_db.resolve().as_uri()+'?mode=ro', uri=True) as jobs:
                watermark = jobs.execute('SELECT MAX(last_seen) FROM jobs').fetchone()[0]
                total_families = jobs.execute('SELECT COUNT(*) FROM job_families').fetchone()[0]
        with sqlite3.connect(preference_db.resolve().as_uri()+'?mode=ro',uri=True) as con:
            for policy in POLICIES:
                run=runs.get(policy)
                row=con.execute('SELECT artifact_path,manifest_json FROM preference_model_runs WHERE run_id=?',(run,)).fetchone()
                count=con.execute('SELECT COUNT(*) FROM preference_scores WHERE run_id=?',(run,)).fetchone()[0] if row else 0
                exists=bool(row and (Path(row[0])/'model.pkl').is_file())
                state=con.execute('SELECT value FROM preference_state WHERE key=?',('policy_refresh:'+policy,)).fetchone()
                receipt=json.loads(state[0]) if state else None
                verified = bool(isinstance(receipt, dict) and receipt.get('run_id') == run
                    and isinstance(receipt.get('result'), dict)
                    and receipt['result'].get('scored_families') == count
                    and (total_families is None or count == total_families))
                latest = con.execute('SELECT MAX(scored_at) FROM preference_scores WHERE run_id=?', (run,)).fetchone()[0] if row else None
                stale = False
                if watermark:
                    observed = datetime.fromisoformat(watermark.replace('Z','+00:00'))
                    if observed.tzinfo is None: observed = observed.replace(tzinfo=timezone.utc)
                    stale = (datetime.now(timezone.utc)-observed).total_seconds() > 172800
                if isinstance(receipt, dict) and watermark and receipt.get('source_watermark') != watermark:
                    stale = True
                if not exists:
                    status, reason = 'blocked', 'missing_policy_or_artifact'
                elif not count:
                    status, reason = 'configured_unverified', 'awaiting_refresh'
                elif jobs_db and not verified:
                    status, reason = 'configured_unverified', 'refresh_not_verified'
                elif stale:
                    status, reason = 'stale', 'catalog_or_scores_stale'
                else:
                    status, reason = 'ready', 'scores_available'
                result[policy]={'run_id':run,'artifact_present':exists,'score_count':count,
                    'status':status, 'reason':reason,
                    'source_last_seen':watermark, 'latest_score_at':latest, 'freshness_verified':verified and not stale,
                    'last_refresh':receipt}
        return result
    except (OSError,sqlite3.Error,ValueError):
        return {p:{'status':'blocked','reason':'ranking_state_unavailable'} for p in POLICIES}


def _identities(*paths):
    return [(info.st_dev, info.st_ino) for info in (path.stat() for path in paths)]


def _require_identities(expected, *paths):
    if _identities(*paths) != expected:
        raise RefreshDatabaseBusyError('ranking database was replaced; retry refresh')


def _require_models(preference_db, proxy_db, runs, signatures, active_components_only):
    from . import model, refresh_freshness as freshness
    current = policy_runs(proxy_db)
    for policy, signature in signatures.items():
        if current.get(policy) != runs[policy]:
            raise RefreshDatabaseBusyError('ranking policy changed; retry refresh')
        artifact, record = model._load_artifact(preference_db, runs[policy], prepare_schema=False)
        if freshness.scoring_signature(artifact, record, active_components_only=active_components_only) != signature:
            raise RefreshDatabaseBusyError('ranking model changed; retry refresh')


@contextmanager
def _prepared_snapshot(jobs_db, *, tracked):
    """Keep family validation and scoring in the same read snapshot."""
    from . import model, refresh_freshness as freshness
    from job_search.collection.dedupe import prepare_families, prepared_families_are_current
    if not tracked:
        dedupe = {'reused': True} if prepared_families_are_current(jobs_db) else prepare_families(jobs_db)
        with closing(model.connect_source(jobs_db)) as source:
            source.execute('BEGIN')
            yield source, dedupe
        return
    source = model.connect_source(jobs_db)
    try:
        source.execute('BEGIN')
        dedupe = {'reused': True}
        if not prepared_families_are_current(jobs_db, connection=source):
            source.close()
            dedupe = prepare_families(jobs_db)
            # Preparation may have introduced derived tables since tracking began.
            freshness.ensure_tracking(jobs_db, 'source')
            source = model.connect_source(jobs_db)
            source.execute('BEGIN')
            if not prepared_families_are_current(jobs_db, connection=source):
                raise RefreshDatabaseBusyError('catalog changed during family preparation; retry refresh')
        yield source, dedupe
    finally:
        source.close()


def _reuse_completed(jobs_db, preference_db, proxy_db, runs, signatures, active_components_only):
    from . import model, refresh_freshness as freshness
    identities = _identities(jobs_db, preference_db)
    with closing(model.connect_source(jobs_db)) as source, closing(model.connect_state(preference_db)) as state, state:
        source.execute('BEGIN')
        state.execute('BEGIN IMMEDIATE')
        source_token = freshness.revision_token(source, jobs_db, 'source')
        receipts = {policy: freshness.reusable_receipt(state, preference_db, policy, runs[policy], signature, source_token)
                    for policy, signature in signatures.items()}
        if not all(receipts.values()):
            return None
        totals = {receipt['reuse']['family_count'] for receipt in receipts.values()}
        watermarks = {receipt['source_watermark'] for receipt in receipts.values()}
        if len(totals) != 1 or len(watermarks) != 1:
            return None
        _require_models(preference_db, proxy_db, runs, signatures, active_components_only)
        _require_identities(identities, jobs_db, preference_db)
        return receipts, totals.pop(), watermarks.pop()

def refresh_policies(
    jobs_db: Path, preference_db: Path, proxy_db: Path, *, device='auto',
    inference_config=None, batch_size=256, sample_size=None,
    policies=POLICIES, active_components_only=False, no_embeddings=False,
) -> dict:
    """Embed and score a stable catalog view, saving completed batches.

    Samples exercise selected models without publishing scores,
    pruning the corpus, or creating a whole-catalog completion receipt.
    """
    from . import model
    from . import refresh_freshness as freshness
    if not 1 <= batch_size <= 800:
        raise ValueError('batch_size must be between 1 and 800')
    if sample_size is not None and not 1 <= sample_size <= 800:
        raise ValueError('sample_size must be between 1 and 800')
    policies = tuple(policies)
    if not policies or len(set(policies)) != len(policies) or set(policies) - set(POLICIES):
        raise ValueError('policies must be unique supported policy names and nonempty')
    runs = policy_runs(proxy_db)
    missing = set(policies) - set(runs)
    if missing:
        raise ValueError('missing policy mappings: ' + ', '.join(sorted(missing)))
    load_options = {'prepare_schema': False} if no_embeddings else {}
    records = {policy: model._load_artifact(preference_db, runs[policy], **load_options) for policy in policies}
    embedding_policies = {
        policy for policy, (artifact, _) in records.items()
        if model.artifact_requires_embeddings(artifact, active_components_only=active_components_only)
    }
    if no_embeddings and embedding_policies:
        raise ValueError('selected policies require embeddings: ' + ', '.join(sorted(embedding_policies)))
    scoring_options = {'active_components_only': True} if active_components_only else {}
    tracked = sample_size is None and no_embeddings and all(
        model.scoring_components(artifact, active_components_only=active_components_only) == ('sparse',)
        for artifact, _ in records.values())
    signatures = {policy: freshness.scoring_signature(artifact, record, active_components_only=active_components_only)
                  for policy, (artifact, record) in records.items()} if tracked else {}
    # Refresh writes state even for a sample. Establish WAL before either the
    # reuse check or a long scoring snapshot, so collectors can still commit.
    # A rejected no-embedding request must not change either database's mode.
    with _enable_refresh_wal(jobs_db), _enable_refresh_wal(preference_db):
        started = datetime.now(timezone.utc).isoformat()
        progress = {'status': 'checking' if tracked else 'preparing', 'started_at': started, 'processed_families': 0,
                    'checked_families': 0, 'reused': False,
                    'total_families': None, 'batches': 0, 'sample': sample_size is not None,
                    'selected_policies': list(policies), 'active_components_only': active_components_only,
                    'no_embeddings': no_embeddings}
        work_id = os.environ.get('JOB_SEARCH_INVOCATION_WORK', '')
        revision = os.environ.get('JOB_SEARCH_INVOCATION_REVISION', '')
        if re.fullmatch(r'work_[a-f0-9]{32}', work_id) and re.fullmatch(r'[0-9]{1,18}', revision):
            progress.update(invocation_work_id=work_id, invocation_revision=int(revision))

        def report(**values):
            progress.update(values, updated_at=datetime.now(timezone.utc).isoformat())
            with model.connect_state(preference_db) as state:
                model._set_state(state, 'policy_sample_progress' if sample_size is not None
                                 else 'policy_refresh_progress', json.dumps(progress, sort_keys=True))

        try:
            prepared = False
            if tracked:
                with closing(model.connect_source(preference_db)) as existing:
                    tables = model._source_tables(existing)
                if not {'preference_state', 'preference_scores'} <= tables:
                    model.prepare_state(preference_db)
                    prepared = True
                freshness.ensure_tracking(jobs_db, 'source')
                freshness.ensure_tracking(preference_db, 'scores')
                report()
                reused = _reuse_completed(jobs_db, preference_db, proxy_db, runs, signatures, active_components_only)
                if reused:
                    receipts, total, watermark = reused
                    results = {policy: {'run_id': runs[policy], 'checked_families': 0,
                               'recomputed_families': 0, 'updated_families': 0, 'removed_families': 0, 'reused_families': total}
                               for policy in policies}
                    report(status='succeeded', reused=True, reuse_reason='inputs_unchanged',
                           total_families=total, source_watermark=watermark, policies=results, embeddings={})
                    # Preserve the original completion timestamp and coverage seal;
                    # only this invocation's result describes zero new work.
                    returned = {policy: {**receipt, 'result': {**receipt['result'], **results[policy]}}
                                for policy, receipt in receipts.items()}
                    return {'status': 'ready', 'reused': True, 'reason': 'already_current',
                            'reuse_reason': 'inputs_unchanged', 'policies': returned, 'dedupe': {'reused': True},
                            'embeddings': {}, 'processed_families': 0, 'checked_families': 0,
                            'total_families': total, 'selected_policies': list(policies),
                            'active_components_only': active_components_only, 'no_embeddings': no_embeddings}
            if not prepared:
                # Legacy feature-document cleanup can scan the entire cache.
                # A verified no-op must return before this migration path.
                model.prepare_state(preference_db)
            if tracked:
                freshness.ensure_tracking(preference_db, 'scores')
            modules = model._optional_ml_modules()
            report(status='preparing')
            identities = _identities(jobs_db, preference_db)
            # Samples never change family metadata or publish completion receipts.
            @contextmanager
            def sample_snapshot():
                with closing(model.connect_source(jobs_db)) as source:
                    source.execute('BEGIN')
                    yield source, None
            snapshot = sample_snapshot() if sample_size is not None else _prepared_snapshot(jobs_db, tracked=tracked)
            with snapshot as (source, dedupe), closing(model.connect_state(preference_db)) as state:
                # One SQLite read transaction binds validation, watermark, features,
                # and selected policies to the same view while ingestion can continue in WAL.
                model.validate_family_schema(source, require_complete=sample_size is None)
                source_token = freshness.revision_token(source, jobs_db, 'source') if tracked else None
                expected_scores = {}
                force_rescore = {}
                if tracked:
                    with state:
                        state.execute('BEGIN IMMEDIATE')
                        for policy in policies:
                            run_id = runs[policy]
                            expected_scores[policy] = freshness.revision_token(state, preference_db, 'scores', run_id)
                            force_rescore[policy] = not freshness.cache_matches(state, preference_db, run_id, signatures[policy]) and bool(
                                state.execute('SELECT 1 FROM preference_scores WHERE run_id=? LIMIT 1', (run_id,)).fetchone())
                watermark = source.execute('SELECT MAX(last_seen) FROM jobs').fetchone()[0]
                total = source.execute('SELECT COUNT(*) FROM job_families').fetchone()[0]
                family_ids = None
                if sample_size is not None:
                    ids = [row[0] for row in source.execute('SELECT family_id FROM job_families ORDER BY family_id')]
                    size = min(sample_size, len(ids))
                    family_ids = [ids[round(i * (len(ids) - 1) / max(1, size - 1))] for i in range(size)]
                    total = size
                report(status='embedding_and_scoring' if embedding_policies else 'scoring',
                       total_families=total, source_watermark=watermark)
                state.execute('CREATE TEMP TABLE refresh_families (family_id TEXT PRIMARY KEY)')
                encoders = {}
                embedded = {}
                results = {policy: {'run_id': runs[policy], 'updated_families': 0,
                                   'checked_families': 0, 'recomputed_families': 0, 'reused_families': 0}
                           for policy in policies}
                for documents in model.iter_family_document_batches(
                    jobs_db, batch_size=batch_size, connection=source, family_ids=family_ids,
                ):
                    for policy in policies:
                        if policy not in embedding_policies:
                            continue
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
                        if tracked:
                            state.execute('BEGIN IMMEDIATE')
                            _require_identities(identities, jobs_db, preference_db)
                            for policy in policies:
                                current = freshness.revision_token(state, preference_db, 'scores', runs[policy])
                                if current is None or current != expected_scores[policy]:
                                    raise RefreshDatabaseBusyError('ranking scores changed during refresh; retry refresh')
                        for policy in policies:
                            artifact, record = records[policy]
                            if sample_size is not None:
                                predictions = model._score_document_batch(preference_db, documents, artifact, modules, **scoring_options)
                                results[policy]['tested_families'] = results[policy].get('tested_families', 0) + len(predictions)
                            else:
                                results[policy]['updated_families'] += model.score_and_store_batch(
                                    state, preference_db, documents, artifact, record, modules,
                                    **scoring_options, **({'force_rescore': force_rescore[policy]} if tracked else {}),
                                )
                            results[policy]['checked_families'] += len(documents)
                            results[policy]['recomputed_families'] = results[policy]['updated_families']
                            results[policy]['reused_families'] = results[policy]['checked_families'] - results[policy]['updated_families']
                        if tracked:
                            for policy in policies:
                                checkpoint = freshness.cache_receipt(state, preference_db, runs[policy], signatures[policy])
                                expected_scores[policy] = checkpoint['scores']
                                # A forced repair cannot bless unvisited old rows.
                                if not force_rescore[policy]:
                                    model._set_state(state, freshness.cache_key(runs[policy]), json.dumps(checkpoint, sort_keys=True))
                        state.executemany('INSERT INTO refresh_families VALUES (?)', [(d.family_id,) for d in documents])
                    report(processed_families=progress['processed_families'] + len(documents),
                           checked_families=progress['checked_families'] + len(documents),
                           batches=progress['batches'] + 1, embeddings=embedded, policies=results)
                if progress['processed_families'] != total:
                    raise model.PreferenceModelError('catalog snapshot did not yield every expected family')
                # Only a completed full pass may prune obsolete scores and certify freshness.
                receipts = {}
                if sample_size is None:
                    with state:
                        if tracked:
                            state.execute('BEGIN IMMEDIATE')
                            _require_models(preference_db, proxy_db, runs, signatures, active_components_only)
                            _require_identities(identities, jobs_db, preference_db)
                            for policy in policies:
                                current = freshness.revision_token(state, preference_db, 'scores', runs[policy])
                                if current is None or current != expected_scores[policy]:
                                    raise RefreshDatabaseBusyError('ranking scores changed before completion; retry refresh')
                        for policy in policies:
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
                                'active_components_only': active_components_only,
                                'completed_at': datetime.now(timezone.utc).isoformat(), 'result': results[policy]}
                            if tracked:
                                checkpoint = freshness.cache_receipt(state, preference_db, runs[policy], signatures[policy])
                                model._set_state(state, freshness.cache_key(runs[policy]), json.dumps(checkpoint, sort_keys=True))
                                if signatures[policy] and source_token:
                                    receipts[policy]['reuse'] = {**checkpoint, 'source': source_token,
                                        'complete': True, 'family_count': total}
                            model._set_state(state, 'policy_refresh:' + policy, json.dumps(receipts[policy], sort_keys=True))
                report(status='succeeded')
                return {'status': 'sample_passed' if sample_size is not None else 'ready',
                        'reused': False, 'checked_families': progress['checked_families'], 'total_families': total,
                        'policies': receipts or results, 'dedupe': dedupe, 'embeddings': embedded,
                        'processed_families': progress['processed_families'],
                        'selected_policies': list(policies), 'active_components_only': active_components_only,
                        'no_embeddings': no_embeddings}
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
    parser.add_argument('--policy', action='append', choices=POLICIES,
                        help='Refresh only this policy; repeat to select several (default: both)')
    parser.add_argument('--active-components-only', action='store_true',
                        help='Compute only model components with nonzero weight')
    parser.add_argument('--no-embeddings', action='store_true',
                        help='Refuse models requiring embeddings before changing state')
    parser.add_argument('--device',default='auto',choices=('auto','cpu','mps','cuda'))
    args=parser.parse_args(argv)
    from job_search.inference import configured_inference_path
    from job_search.inference.providers import InferenceTransportError
    try:
        result=refresh_policies(args.db,args.state_db,args.proxy_db,device=args.device,
            inference_config=None if args.no_embeddings else configured_inference_path(args.inference_config),
            batch_size=args.batch_size, sample_size=args.sample_size,
            policies=POLICIES if args.policy is None else args.policy,
            active_components_only=args.active_components_only, no_embeddings=args.no_embeddings)
    except InferenceTransportError as exc:
        if getattr(exc,'defer_without_attempt',False):
            import sys
            print(json.dumps({'type':'inference_usage_deferred','reason':exc.reason_code,'retry_at':exc.retry_at}),file=sys.stderr)
            return 76
        return 75 if exc.retryable else 78
    except RefreshDatabaseBusyError as exc:
        import sys
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        return 75
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        import sys
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        # A collector commit can invalidate preparation's WAL read snapshot.
        # Retry that safe abort instead of permanently disabling this work item.
        if isinstance(exc, sqlite3.OperationalError) and any(
                term in str(exc).lower() for term in ('locked', 'busy')):
            return 75
        if 'jobs have no current opportunity family' in str(exc):
            return 75
        return 78
    print(json.dumps(result,default=str));return 0

if __name__=='__main__': raise SystemExit(main())
