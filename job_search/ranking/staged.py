"""Budgeted selective candidate scoring, isolated from full-catalog ranking."""
from __future__ import annotations

import argparse
from contextlib import closing
import heapq
import json
import math
from pathlib import Path
import sqlite3
import uuid

from . import model
from .refresh import policy_runs


class BudgetExceeded(model.PreferenceModelError):
    """The candidate workload would exceed an explicit local input budget."""


class _BudgetEncoder:
    """Check every submitted batch, including unexpected cache misses.

    These are input-text limits, not a currency/token or provider-retry budget.
    Provider-side quota controls continue to apply independently.
    """
    def __init__(self, encoder, revision, allowed, max_texts, max_characters):
        self.encoder = encoder
        self.model_revision = revision
        self.allowed = allowed
        self.max_texts = max_texts
        self.max_characters = max_characters
        self.submitted_texts = self.submitted_characters = 0
        self.provenance = getattr(encoder, 'provenance', None)
        if encoder is not None and str(encoder.model_revision) != revision:
            raise model.PreferenceModelError('encoder revision differs from planned revision')

    def iter_encode_batches(self, texts):
        texts = list(texts)
        chars = sum(len(text) for text in texts)
        if (any(model.text_fingerprint(text) not in self.allowed for text in texts)
                or self.submitted_texts + len(texts) > self.max_texts
                or self.submitted_characters + chars > self.max_characters):
            raise BudgetExceeded('submitted embedding input exceeds the approved stage budget')
        if self.encoder is None:
            raise BudgetExceeded('a previously cached embedding disappeared; run preflight again')
        self.submitted_texts += len(texts)
        self.submitted_characters += chars
        stream = getattr(self.encoder, 'iter_encode_batches', None)
        if callable(stream):
            yield from stream(texts)
        else:
            yield texts, self.encoder.encode(texts)


def _add_bounded(heap, entry, size):
    if size <= 0:
        return
    if len(heap) < size:
        heapq.heappush(heap, entry)
    elif entry[:2] > heap[0][:2]:
        heapq.heapreplace(heap, entry)


def _open_family_ids(source, family_ids):
    # Keep the bounded family lookup outermost. Otherwise an index on closed_at
    # can make SQLite scan the entire open catalog for every 256-family batch.
    return {row[0] for row in source.execute(
        'SELECT DISTINCT m.family_id FROM job_family_members m CROSS JOIN jobs j '
        'ON j.ats=m.ats AND j.id=m.job_id WHERE j.closed_at IS NULL '
        'AND m.family_id IN (' + ','.join('?' for _ in family_ids) + ')', family_ids)}


def _embedding_demand(state, documents, revision):
    texts = {}
    for document in documents:
        for text in (document.title_metadata_text, *document.description_chunks):
            texts.setdefault(model.text_fingerprint(text), text)
    cached = set()
    keys = list(texts)
    for batch in model.iter_batches(keys, 800):
        cached.update(row[0] for row in state.execute(
            'SELECT text_fingerprint FROM preference_embedding_cache WHERE model_revision=? '
            'AND text_version=? AND text_fingerprint IN (' + ','.join('?' for _ in batch) + ')',
            (revision, model.TEXT_VERSION, *batch),
        ))
    missing = {key: value for key, value in texts.items() if key not in cached}
    return missing, {'unique_texts': len(texts), 'cached_texts': len(cached),
                     'missing_texts': len(missing),
                     'missing_characters': sum(map(len, missing.values()))}


def candidate_recall(examples, current_documents, selected_ids, manifests, *, eligible_ids=None, template_aliases=None):
    """Describe held-out retrieval by review slice; never label training recall quality.

    Callers must supply evaluation-role immutable examples, as the CLI's explicit
    evaluation-db option does. Changed/missing snapshots and training overlaps are
    excluded and counted. This is candidate retrieval, not selective-model quality.
    """
    counts = {'reviewed_families': len(examples), 'excluded_training_overlap': 0,
              'excluded_missing_or_changed_snapshot': 0, 'excluded_closed_families': 0}
    if not examples:
        return {'status': 'unavailable', 'reason': 'no_eligible_held_out_reviews',
                **counts, 'slices': {}}
    template_aliases = template_aliases or {}
    def canonical(key, value):
        return template_aliases.get(value, value) if key == 'template_cluster_id' else value
    keys = ('family_id', 'feature_fingerprint', 'template_cluster_id', 'leakage_group_id')
    training = {key: set() for key in keys}
    for manifest in manifests:
        labels = manifest.get('labels')
        if not isinstance(labels, list) or not labels or any(
            not isinstance(label, dict) or any(not label.get(key) for key in keys) for label in labels
        ):
            return {'status': 'unavailable', 'reason': 'complete_training_lineage_required'}
        for label in labels:
            for key in keys:
                training[key].add(canonical(key, label[key]))
    slices = {}
    for example in examples:
        doc = example.document
        values = (doc.family_id, doc.fingerprint, doc.template_cluster_id, doc.leakage_group_id)
        if any(canonical(key, value) in training[key] for key, value in zip(keys, values)):
            counts['excluded_training_overlap'] += 1
            continue
        current = current_documents.get(doc.family_id)
        if current is not None and eligible_ids is not None and doc.family_id not in eligible_ids:
            counts['excluded_closed_families'] += 1
            continue
        if current is None or current.fingerprint != doc.fingerprint:
            counts['excluded_missing_or_changed_snapshot'] += 1
            continue
        if any(canonical(key, value) in training[key] for key, value in zip(keys, (
            current.family_id, current.fingerprint, current.template_cluster_id, current.leakage_group_id,
        ))):
            counts['excluded_training_overlap'] += 1
            continue
        group = slices.setdefault(example.selection_strategy or 'unspecified', {
            'reviewed_families': 0, 'retained_families': 0,
            'interested_families': 0, 'retained_interested_families': 0})
        retained = doc.family_id in selected_ids
        group['reviewed_families'] += 1
        group['retained_families'] += int(retained)
        group['interested_families'] += int(example.target == 1)
        group['retained_interested_families'] += int(retained and example.target == 1)
    if not slices:
        return {'status': 'unavailable', 'reason': 'no_eligible_held_out_reviews',
                **counts, 'slices': {}}
    for group in slices.values():
        group['interested_candidate_recall'] = (group['retained_interested_families'] /
            group['interested_families'] if group['interested_families'] else None)
    return {'status': 'descriptive_review_coverage', **counts, 'slices': slices,
            'limitations': ['Review slices are not pooled or treated as catalog-wide recall.',
                           'Candidate recall does not measure selective-model precision or ranking quality.',
                           'Repeated tuning against these labels invalidates their held-out interpretation.']}


def stage_selective(jobs_db: Path, preference_db: Path, proxy_db: Path, *,
                    candidate_limit=500, exploration_count=0, max_missing_texts=1000,
                    max_missing_characters=1000000, execute=False, stage_db=None,
                    device='cpu', inference_config=None, evaluation_db=None):
    """Dry-run by default; an explicit execute writes only separately scoped scores.

    Requires an already prepared, fully broad-scored WAL catalog so the source
    snapshot cannot block ingestion. Never changes journal modes or prepares a
    catalog, changes full-policy scores/receipts, or activates an embedding revision.
    """
    for value, name, low, high in (
        (candidate_limit, 'candidate_limit', 1, 5000),
        (exploration_count, 'exploration_count', 0, candidate_limit),
        (max_missing_texts, 'max_missing_texts', 0, 100000),
        (max_missing_characters, 'max_missing_characters', 0, 100000000),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{name} must be an integer between {low} and {high}')
    if execute:
        if stage_db is None:
            raise ValueError('execute requires a separate stage_db')
        stage_db = Path(stage_db)
        for protected in (jobs_db, preference_db, proxy_db, evaluation_db):
            if protected is not None and (stage_db.resolve() == Path(protected).resolve()
                    or (stage_db.exists() and Path(protected).exists() and stage_db.samefile(protected))):
                raise ValueError('stage_db must be separate from all source databases')
    runs = policy_runs(proxy_db)
    if not {'broad', 'selective'} <= runs.keys():
        raise model.PreferenceModelError('broad and selective policy mappings are required')
    examples = model.load_evaluation_examples(evaluation_db) if evaluation_db is not None else []
    reviewed_ids = {example.document.family_id for example in examples}
    reviewed_docs = {}
    reviewed_eligible_ids = set()
    # Keep source features stable, but release each state read immediately so
    # progress/cache writers also work with rollback-journal preference databases.
    # An unchanged completion receipt fences the batched state reads below.
    with closing(model.connect_source(jobs_db)) as source, closing(model.connect_source(preference_db)) as state:
        if str(source.execute('PRAGMA journal_mode').fetchone()[0]).lower() != 'wal':
            raise model.PreferenceModelError(
                'staging requires a WAL jobs database; run the guarded broad refresh '
                'on the working copy first')
        source.execute('BEGIN')
        from job_search.collection.dedupe import prepared_families_are_current
        if not prepared_families_are_current(jobs_db, connection=source):
            raise model.PreferenceModelError('prepared catalog is stale or incomplete; refresh broad first')
        model.validate_family_schema(source)
        template_aliases = model._template_lineage_aliases(source) if examples else {}
        watermark = source.execute('SELECT MAX(last_seen) FROM jobs').fetchone()[0]
        total = source.execute('SELECT COUNT(*) FROM job_families').fetchone()[0]
        raw_receipt = state.execute("SELECT value FROM preference_state WHERE key='policy_refresh:broad'").fetchone()
        receipt = json.loads(raw_receipt[0]) if raw_receipt else {}
        if (receipt.get('run_id') != runs['broad'] or receipt.get('source_watermark') != watermark
                or receipt.get('result', {}).get('scored_families') != total):
            raise model.PreferenceModelError('a complete broad refresh receipt for this catalog is required')
        records = {}
        manifests = []
        for policy in ('broad', 'selective'):
            record = state.execute('SELECT * FROM preference_model_runs WHERE run_id=?', (runs[policy],)).fetchone()
            if record is None or record['text_version'] != model.TEXT_VERSION:
                raise model.PreferenceModelError('current model metadata is required')
            records[policy] = record
            manifests.append(json.loads(record['manifest_json']))
        selective_weights = manifests[1].get('selection', {}).get('weights')
        if not isinstance(selective_weights, dict):
            raise model.PreferenceModelError('selective component metadata is required')
        requires_embeddings = model.artifact_requires_embeddings(
            {'weights': selective_weights}, active_components_only=True)
        top, exploration = [], []
        seen = eligible_count = 0
        # Both heaps are bounded. Exploration uses stable hash order and excludes
        # top candidates afterwards, so reserve enough to fill its own quota.
        for documents in model.iter_family_document_batches(jobs_db, connection=source, batch_size=256):
            ids = [doc.family_id for doc in documents]
            scores = {row['family_id']: row for row in state.execute(
                'SELECT family_id,feature_fingerprint,final_score FROM preference_scores '
                'WHERE run_id=? AND family_id IN (' + ','.join('?' for _ in ids) + ')', (runs['broad'], *ids))}
            eligible_ids = _open_family_ids(source, ids)
            for doc in documents:
                row = scores.get(doc.family_id)
                if row is None or row['feature_fingerprint'] != doc.fingerprint or not math.isfinite(row['final_score']):
                    raise model.PreferenceModelError('broad scores are missing or changed; refresh broad first')
                seen += 1
                if doc.family_id in reviewed_ids:
                    reviewed_docs[doc.family_id] = doc
                    if doc.family_id in eligible_ids:
                        reviewed_eligible_ids.add(doc.family_id)
                if doc.family_id not in eligible_ids:
                    continue
                eligible_count += 1
                score = float(row['final_score'])
                _add_bounded(top, (score, doc.family_id, doc), candidate_limit - exploration_count)
                if exploration_count:
                    _add_bounded(exploration, (model.sha256_text('staged-exploration-v1:' + doc.family_id), doc.family_id, doc, score), candidate_limit)
        if seen != total:
            raise model.PreferenceModelError('catalog family coverage is incomplete')
        selected = {entry[1]: (entry[2], entry[0], 'broad_top') for entry in top}
        for _, family_id, doc, score in sorted(exploration, reverse=True):
            if len(selected) >= min(candidate_limit, eligible_count):
                break
            selected.setdefault(family_id, (doc, score, 'exploration'))
        selected_rows = sorted(selected.values(), key=lambda item: (-item[1], item[0].family_id))
        documents = [row[0] for row in selected_rows]
        revision = str(records['selective']['model_revision'])
        missing, demand = _embedding_demand(state, documents, revision) if requires_embeddings else ({}, {
            'unique_texts': 0, 'cached_texts': 0, 'missing_texts': 0, 'missing_characters': 0})
        final_receipt = state.execute(
            "SELECT value FROM preference_state WHERE key='policy_refresh:broad'").fetchone()
        if final_receipt is None or final_receipt[0] != raw_receipt[0]:
            raise model.PreferenceModelError('broad refresh changed during preflight; retry staging')
    report = {'status': 'planned', 'scope': 'selective_candidates_only',
              'source_watermark': watermark, 'broad_run_id': runs['broad'], 'selective_run_id': runs['selective'],
              'catalog_families': total, 'eligible_families': eligible_count,
              'excluded_closed_families': total - eligible_count,
              'eligibility': 'at_least_one_member_posting_open', 'selected_families': len(documents),
              'candidate_fraction_of_catalog': len(documents) / total if total else 0,
              'candidate_fraction_of_eligible': len(documents) / eligible_count if eligible_count else 0,
              'selection_fingerprint': model.sha256_text(model.canonical_json([
                  [doc.family_id, doc.fingerprint, score, reason] for doc, score, reason in selected_rows])),
              'candidate_limit': candidate_limit, 'exploration_count': sum(row[2] == 'exploration' for row in selected_rows),
              'budget': {'max_missing_texts': max_missing_texts, 'max_missing_characters': max_missing_characters},
              'embeddings': demand, 'requires_embeddings': requires_embeddings,
              'limitations': ['Omitted families have no selective result in this stage.',
                             'Input character/text caps do not bound provider retries, billed tokens, or currency.',
                             'Broad candidate selection can omit jobs the selective model would favor.']}
    if evaluation_db is not None:
        report['evaluation'] = candidate_recall(examples, reviewed_docs, set(selected), manifests, eligible_ids=reviewed_eligible_ids, template_aliases=template_aliases)
    if demand['missing_texts'] > max_missing_texts or demand['missing_characters'] > max_missing_characters:
        report['status'] = 'budget_exceeded'
        if execute:
            raise BudgetExceeded('stage preflight exceeds text or character budget; no encoder was created')
        return report
    if not execute:
        return report
    artifact, record = model._load_artifact(preference_db, runs['selective'], prepare_schema=False)
    if (artifact.get('model_revision') != revision or artifact.get('weights') != selective_weights
            or record['manifest_json'] != records['selective']['manifest_json']):
        raise model.PreferenceModelError('selective model changed after preflight')
    modules = model._optional_ml_modules()
    encoder = None
    if requires_embeddings:
        underlying = model.encoder_for_recorded_revision(revision, device, inference_config) if missing else None
        encoder = _BudgetEncoder(underlying, revision, set(missing), max_missing_texts, max_missing_characters)
    stage_id = 'stage_' + uuid.uuid4().hex
    # This DB deliberately has no preference_scores or policy_refresh state.
    with closing(sqlite3.connect(stage_db)) as out:
        out.executescript('''CREATE TABLE IF NOT EXISTS staged_runs (
            stage_id TEXT PRIMARY KEY, status TEXT NOT NULL, report_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS staged_scores (
            stage_id TEXT NOT NULL, family_id TEXT NOT NULL, feature_fingerprint TEXT NOT NULL,
            candidate_reason TEXT NOT NULL, broad_score REAL NOT NULL,
            selective_score REAL NOT NULL, prediction_json TEXT NOT NULL,
            PRIMARY KEY(stage_id,family_id));''')
        report['stage_id'] = stage_id
        report['status'] = 'running'
        out.execute('INSERT INTO staged_runs VALUES (?,?,?)', (stage_id, 'running', json.dumps(report, sort_keys=True)))
        out.commit()
        try:
            for batch in model.iter_batches(selected_rows, 256):
                batch_docs = [row[0] for row in batch]
                if encoder is not None:
                    model.embed_documents(preference_db, batch_docs, encoder, activate_revision=False, prepare_schema=False)
                predictions = model._score_document_batch(preference_db, batch_docs, artifact, modules, active_components_only=True)
                if len(predictions) != len(batch_docs):
                    raise model.PreferenceModelError('selective scorer returned incomplete results')
                with out:
                    out.executemany('INSERT INTO staged_scores VALUES (?,?,?,?,?,?,?)', [
                        (stage_id, doc.family_id, doc.fingerprint, reason, score, prediction['final'], json.dumps(prediction, sort_keys=True))
                        for (doc, score, reason), prediction in zip(batch, predictions)])
            report['status'] = 'completed_candidates'
            report['submitted_texts'] = encoder.submitted_texts if encoder else 0
            report['submitted_characters'] = encoder.submitted_characters if encoder else 0
        except Exception as exc:
            report.update(status='failed', error_type=type(exc).__name__)
            raise
        finally:
            with out:
                out.execute('UPDATE staged_runs SET status=?,report_json=? WHERE stage_id=?',
                            (report['status'], json.dumps(report, sort_keys=True), stage_id))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('db', 'state-db', 'proxy-db'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--stage-db', type=Path)
    parser.add_argument('--execute', action='store_true', help='Explicitly score candidates; default only plans')
    parser.add_argument('--candidate-limit', type=int, default=500)
    parser.add_argument('--exploration-count', type=int, default=0)
    parser.add_argument('--max-missing-texts', type=int, default=1000)
    parser.add_argument('--max-missing-characters', type=int, default=1000000)
    parser.add_argument('--evaluation-db', type=Path, help='Optional held-out reviewed snapshot; do not tune budgets against protected labels')
    parser.add_argument('--device', default='cpu', choices=('auto', 'cpu', 'mps', 'cuda'))
    parser.add_argument('--inference-config', type=Path)
    args = parser.parse_args(argv)
    try:
        result = stage_selective(args.db, args.state_db, args.proxy_db,
            candidate_limit=args.candidate_limit, exploration_count=args.exploration_count,
            max_missing_texts=args.max_missing_texts, max_missing_characters=args.max_missing_characters,
            execute=args.execute, stage_db=args.stage_db, evaluation_db=args.evaluation_db,
            device=args.device, inference_config=args.inference_config)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'blocked', 'error_type': type(exc).__name__, 'reason': str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] != 'budget_exceeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
