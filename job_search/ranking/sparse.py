"""Derive explicitly selected sparse policy versions without training or inference."""
from __future__ import annotations

import argparse
from contextlib import closing
import copy
import json
from pathlib import Path
import pickle
import sqlite3

from . import model
from .refresh import POLICIES

DERIVATION_VERSION = 1


def _policy_rows(con, policies):
    rows = con.execute(
        'SELECT s.* FROM proxy_students s JOIN proxy_runs r ON r.run_id=s.run_id '
        'ORDER BY r.created_at DESC,s.trained_at DESC'
    ).fetchall()
    selected = {}
    for row in rows:
        if row['policy_id'] in policies:
            selected.setdefault(row['policy_id'], dict(row))
    missing = set(policies) - set(selected)
    if missing:
        raise model.PreferenceModelError('missing policy mappings: ' + ', '.join(sorted(missing)))
    return selected


def _verified_source(state_db, row):
    artifact, record = model._load_artifact(state_db, row['model_run_id'], prepare_schema=False)
    manifest = json.loads(record['manifest_json'])
    _validate_identity(artifact, record, manifest)
    if manifest.get('training_source') != 'proxy:' + row['run_id'] + ':' + row['policy_id']:
        raise model.PreferenceModelError('source model training provenance does not match its policy')
    if row['model_revision'] != record['model_revision']:
        raise model.PreferenceModelError('policy mapping model revision does not match its artifact')
    model.scoring_components(artifact, active_components_only=True)
    if not isinstance(artifact.get('sparse_model'), dict) or not all(
        name in artifact['sparse_model'] for name in ('word', 'char', 'classifier')
    ):
        raise model.PreferenceModelError('source artifact has no trained sparse component')
    return artifact, dict(record), manifest


def _derive(artifact, record, source_manifest, policy, artifact_dir):
    if artifact['weights'].get('sparse') == 1 and model.scoring_components(
        artifact, active_components_only=True
    ) == ('sparse',):
        return artifact, record, source_manifest, False
    provenance = {
        'kind': 'sparse_component', 'version': DERIVATION_VERSION, 'policy': policy,
        'source_run_id': record['run_id'],
        'source_model_sha256': source_manifest['artifacts']['model.pkl'],
        'source_manifest_sha256': model.sha256_text(model.canonical_json(source_manifest)),
    }
    run_id = 'run_' + model.sha256_text(model.canonical_json(provenance))[:24]
    derived = dict(artifact, run_id=run_id, weights={'sparse': 1.0})
    # Preserve fitted components for research compatibility; the CPU workflow
    # explicitly computes active components only and forbids embeddings.
    model_bytes = pickle.dumps(derived, protocol=4)
    manifest = copy.deepcopy(source_manifest)
    for key in ('protected_evaluation', 'promotion_status', 'evaluation', 'out_of_fold'):
        manifest.pop(key, None)
    selection = {'name': 'sparse', 'weights': {'sparse': 1.0},
                 'selection_reason': 'operator selected existing sparse component; no retraining'}
    component = source_manifest.get('out_of_fold', {}).get('components', {}).get('sparse')
    manifest.update(
        run_id=run_id, created_at=model.utc_now(), derivation=provenance, selection=selection,
        artifacts={'model.pkl': model.sha256_bytes(model_bytes)},
        out_of_fold={
            'components': {'sparse': component} if component is not None else {},
            'selected': dict(selection),
            'evaluation_scope': 'inherited source component cross-validation; not independent evaluation',
        },
        evaluation_status='requires_independent_evaluation',
    )
    target = artifact_dir.resolve() / 'runs' / run_id
    derived_record = dict(record, run_id=run_id, created_at=manifest['created_at'],
                          manifest_json=model.canonical_json(manifest), artifact_path=str(target))
    return derived, derived_record, manifest, True


def _validate_identity(artifact, record, manifest, *, require_sparse=False):
    if not isinstance(artifact, dict) or not isinstance(manifest, dict):
        raise model.PreferenceModelError('invalid model artifact or manifest')
    for name in ('run_id', 'model_revision', 'text_version'):
        # Historical artifacts can omit text_version; the record and manifest
        # always provide it. A declared artifact value must agree too.
        artifact_declares = name != 'text_version' or name in artifact
        if manifest.get(name) != record[name] or (artifact_declares and artifact.get(name) != record[name]):
            raise model.PreferenceModelError('artifact identity does not match its model record')
    if record['text_version'] != model.TEXT_VERSION:
        raise model.PreferenceModelError('source text version is incompatible with current ranking features')
    if require_sparse and (artifact.get('weights') != {'sparse': 1.0} or
                           model.scoring_components(artifact, active_components_only=True) != ('sparse',)):
        raise model.PreferenceModelError('derived model artifact is not sparse-only')


def _validate_candidate(artifact, record, previous, expected):
    _validate_identity(artifact, record, previous, require_sparse=True)
    if any(previous.get(key) != expected.get(key) for key in (
        'run_id', 'model_revision', 'text_version', 'training_source', 'derivation', 'selection',
    )):
        raise model.PreferenceModelError('conflicting sparse model version already exists')


def _persist(state_db, artifact, record, manifest):
    """Serialize registration; never replace a version or conflicting directory."""
    with closing(model.connect_state(state_db)) as con, con:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT * FROM preference_model_runs WHERE run_id=?',
                               (record['run_id'],)).fetchone()
        if existing:
            verified_artifact, verified = model._load_artifact(state_db, record['run_id'], prepare_schema=False)
            previous = json.loads(verified['manifest_json'])
            _validate_candidate(verified_artifact, verified, previous, manifest)
            # Preserve the original serialization and timestamp across runtimes.
            return dict(verified), previous
        target = Path(record['artifact_path'])
        model_bytes = pickle.dumps(artifact, protocol=4)
        manifest_path = target / 'manifest.json'
        if target.exists():
            try:
                previous = json.loads(manifest_path.read_text(encoding='utf-8'))
                previous_bytes = (target / 'model.pkl').read_bytes()
                if model.sha256_bytes(previous_bytes) != previous.get('artifacts', {}).get('model.pkl'):
                    raise model.PreferenceModelError('sparse artifact hash verification failed')
                previous_artifact = pickle.loads(previous_bytes)
            except (OSError, ValueError, pickle.UnpicklingError, EOFError, AttributeError,
                    ImportError, TypeError) as exc:
                raise model.PreferenceModelError('incomplete sparse artifact directory; choose a clean artifact directory') from exc
            _validate_candidate(previous_artifact, record, previous, manifest)
            manifest = previous
            record = dict(record, created_at=manifest['created_at'], manifest_json=model.canonical_json(manifest))
        else:
            # Exclusive ownership also prevents a separate state database writing
            # the same directory. An in-progress competing writer fails safely.
            try:
                target.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise model.PreferenceModelError('sparse artifact directory is being created; retry') from exc
            model._atomic_write(target / 'model.pkl', model_bytes)
            model._atomic_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode())
        con.execute(
            'INSERT INTO preference_model_runs '
            '(run_id,created_at,model_revision,text_version,training_examples,manifest_json,artifact_path) '
            'VALUES (?,?,?,?,?,?,?)',
            tuple(record[key] for key in ('run_id', 'created_at', 'model_revision', 'text_version',
                                         'training_examples', 'manifest_json', 'artifact_path')),
        )
    return record, manifest


def derive_sparse_policies(state_db: Path, proxy_db: Path, artifact_dir: Path, *,
                           policies=POLICIES, activate: bool = False) -> dict:
    """Create candidate versions, optionally CAS-replacing the mapped policies.

    Source artifacts, scores, champion state and old audit records are preserved.
    All inputs are verified before writes. A mapping race leaves valid candidate
    artifacts but activates nothing. No provider, training or scoring is invoked.
    """
    policies = tuple(policies)
    if not policies or len(set(policies)) != len(policies) or set(policies) - set(POLICIES):
        raise model.PreferenceModelError('policies must be unique supported policy names and nonempty')
    with closing(model.connect_source(proxy_db)) as con:
        rows = _policy_rows(con, policies)
    prepared = {}
    for policy in policies:
        artifact, record, manifest = _verified_source(state_db, rows[policy])
        prepared[policy] = _derive(artifact, record, manifest, policy, artifact_dir)
    results = {}
    for policy, (artifact, record, manifest, changed) in prepared.items():
        if changed:
            record, manifest = _persist(state_db, artifact, record, manifest)
        results[policy] = {
            'source_run_id': rows[policy]['model_run_id'], 'run_id': record['run_id'],
            'artifact_path': record['artifact_path'], 'derived': changed,
            'evaluation_status': manifest.get('evaluation_status', 'unchanged_source_evaluation'),
        }
    if activate:
        # Existing DB only: never silently create an empty mapping database.
        with closing(sqlite3.connect(proxy_db.resolve().as_uri() + '?mode=rw', uri=True)) as con, con:
            con.row_factory = sqlite3.Row
            con.execute('BEGIN IMMEDIATE')
            current = _policy_rows(con, policies)
            if current != rows:
                raise model.PreferenceModelError('policy mappings changed during derivation; retry')
            for policy, result in results.items():
                if result['run_id'] == rows[policy]['model_run_id']:
                    continue
                con.execute(
                    'UPDATE proxy_students SET model_run_id=?,state_db=?,artifact_dir=?,score_json=?,trained_at=? '
                    'WHERE run_id=? AND policy_id=? AND model_run_id=?',
                    (result['run_id'], str(state_db.resolve()), str(artifact_dir.resolve()),
                     model.canonical_json({'status': 'not_scored', 'derivation': 'sparse_component',
                                           'source_run_id': result['source_run_id']}), model.utc_now(),
                     rows[policy]['run_id'], policy, result['source_run_id']),
                )
    return {'status': 'activated' if activate else 'candidates_ready', 'policies': results,
            'training_performed': False, 'inference_performed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-db', type=Path, required=True)
    parser.add_argument('--proxy-db', type=Path, required=True)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--policies', nargs='+', choices=POLICIES, default=list(POLICIES))
    parser.add_argument('--activate', action='store_true', help='Explicitly replace mapped policy versions after verification')
    args = parser.parse_args(argv)
    try:
        result = derive_sparse_policies(args.state_db, args.proxy_db, args.artifact_dir,
                                        policies=args.policies, activate=args.activate)
    except (model.PreferenceModelError, OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
