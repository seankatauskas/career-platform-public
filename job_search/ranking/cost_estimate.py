"""Read-only, bounded ranking preflight; never loads models or calls providers."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

from . import model
from .refresh import POLICIES, policy_runs


def estimate_refresh_cost(jobs_db: Path, preference_db: Path, proxy_db: Path, *,
                          policies=POLICIES, active_components_only=False,
                          max_families=5000) -> dict:
    """Count actual cache demand for inspected prepared families, without mutation.

    Text counts deduplicate across families and policies sharing one revision.
    A bounded or incompletely prepared corpus is never reported as a full estimate.
    Model metadata is read as JSON; no pickle, encoder, or inference config is loaded.
    """
    policies = tuple(policies)
    if not policies or len(set(policies)) != len(policies) or set(policies) - set(POLICIES):
        raise ValueError('policies must be unique supported policy names and nonempty')
    if type(max_families) is not int or not 1 <= max_families <= 100000:
        raise ValueError('max_families must be between 1 and 100000')
    runs = policy_runs(proxy_db)
    missing = set(policies) - set(runs)
    if missing:
        raise ValueError('missing policy mappings: ' + ', '.join(sorted(missing)))
    with closing(model.connect_source(jobs_db)) as source, \
         closing(model.connect_source(preference_db)) as state:
        source.execute('BEGIN')
        state.execute('BEGIN')
        summaries = {}
        revisions = set()
        for policy in policies:
            row = state.execute('SELECT model_revision,text_version,manifest_json,artifact_path FROM '
                                'preference_model_runs WHERE run_id=?', (runs[policy],)).fetchone()
            if row is None:
                raise ValueError('model metadata unavailable for ' + policy)
            manifest = json.loads(row['manifest_json'])
            selection = manifest.get('selection') if isinstance(manifest, dict) else None
            weights = selection.get('weights') if isinstance(selection, dict) else None
            if not isinstance(weights, dict):
                raise ValueError('model component metadata unavailable for ' + policy)
            artifact = {'weights': weights}
            model.scoring_components(artifact, active_components_only=True)
            requires = model.artifact_requires_embeddings(
                artifact, active_components_only=active_components_only)
            revision = str(row['model_revision'])
            if not revision.strip() or row['text_version'] != model.TEXT_VERSION:
                raise ValueError('unsupported model revision or text version for ' + policy)
            if requires:
                revisions.add(revision)
            summaries[policy] = {
                'run_id': runs[policy], 'weights': weights,
                'requires_embeddings': requires,
                'artifact_present': (Path(row['artifact_path']) / 'model.pkl').is_file(),
                'new_or_changed_scores': 0,
            }
        postings = source.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        tables = model._source_tables(source)
        prepared = {'job_families', 'job_family_members'}.issubset(tables)
        family_count = source.execute('SELECT COUNT(*) FROM job_families').fetchone()[0] if prepared else 0
        unrepresented = source.execute(
            'SELECT COUNT(*) FROM jobs j LEFT JOIN job_family_members m ON '
            'm.ats=j.ats AND m.job_id=j.id WHERE m.family_id IS NULL'
        ).fetchone()[0] if prepared else postings
        ids = [row[0] for row in source.execute(
            'SELECT f.family_id FROM job_families f JOIN jobs j ON '
            'j.ats=f.canonical_ats AND j.id=f.canonical_job_id ORDER BY f.family_id LIMIT ?',
            (max_families,),
        )] if prepared else []
        texts = {}
        inspected = 0
        for offset in range(0, len(ids), 256):
            batch_ids = ids[offset:offset + 256]
            for documents in model.iter_family_document_batches(
                jobs_db, connection=source, family_ids=batch_ids, batch_size=256,
            ):
                inspected += len(documents)
                for policy in policies:
                    marks = ','.join('?' for _ in batch_ids)
                    existing = dict(state.execute(
                        'SELECT family_id,feature_fingerprint FROM preference_scores '
                        'WHERE run_id=? AND family_id IN (' + marks + ')',
                        (runs[policy], *batch_ids),
                    ).fetchall())
                    summaries[policy]['new_or_changed_scores'] += sum(
                        existing.get(document.family_id) != document.fingerprint for document in documents)
                if revisions:
                    for document in documents:
                        for text in (document.title_metadata_text, *document.description_chunks):
                            texts.setdefault(model.text_fingerprint(text), (len(text), len(text.encode('utf-8'))))
        embeddings = {}
        fingerprints = list(texts)
        cache_present = 'preference_embedding_cache' in model._source_tables(state)
        for revision in sorted(revisions):
            cached = set()
            if cache_present:
                for offset in range(0, len(fingerprints), 800):
                    batch = fingerprints[offset:offset + 800]
                    cached.update(row[0] for row in state.execute(
                        'SELECT text_fingerprint FROM preference_embedding_cache WHERE '
                        'model_revision=? AND text_version=? AND text_fingerprint IN ('
                        + ','.join('?' for _ in batch) + ')',
                        (revision, model.TEXT_VERSION, *batch),
                    ))
            missing_texts = [texts[fingerprint] for fingerprint in fingerprints if fingerprint not in cached]
            embeddings[revision] = {
                'unique_texts': len(texts), 'cached_texts': len(cached),
                'missing_texts': len(missing_texts),
                'missing_characters': sum(chars for chars, _ in missing_texts),
                'missing_utf8_bytes': sum(size for _, size in missing_texts),
                'approximate_input_tokens': sum(math.ceil(chars / 4) for chars, _ in missing_texts),
            }
        complete = prepared and unrepresented == 0 and inspected == family_count
        return {
            'complete_prepared_snapshot': complete,
            'status': 'complete_prepared_snapshot' if complete else 'partial_prepared_snapshot',
            'postings': postings, 'prepared_families': family_count,
            'jobs_without_family': unrepresented, 'inspected_families': inspected,
            'max_families': max_families, 'policies': summaries, 'embeddings': embeddings,
            'active_components_only': active_components_only,
            'requires_embeddings': bool(revisions),
            'limitations': [
                'Counts cover only inspected, already prepared families; no grouping is run.',
                'Prepared-family source fingerprints are not revalidated; changed canonical choices may change demand.',
                'Token estimate is ceil(characters/4) per missing text, not tokenizer output or billed usage.',
                'Provider request overhead, retries, compute time, and prices are not estimated.',
                'Score counts compare feature fingerprints; missing diagnostic components can require additional local scoring.',
            ],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('db', 'state-db', 'proxy-db'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--policy', action='append', choices=POLICIES)
    parser.add_argument('--active-components-only', action='store_true')
    parser.add_argument('--max-families', type=int, default=5000)
    args = parser.parse_args(argv)
    try:
        result = estimate_refresh_cost(args.db, args.state_db, args.proxy_db,
            policies=args.policy if args.policy is not None else POLICIES,
            active_components_only=args.active_components_only, max_families=args.max_families)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'unavailable', 'error_type': type(exc).__name__, 'reason': str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
