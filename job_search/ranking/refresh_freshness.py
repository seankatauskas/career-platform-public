"""Mutation evidence for reusing completed, embedding-free ranking passes.

Triggers cover SQL writers, including writers outside this package. An epoch and
database identity prevent a replaced database or repaired tracking schema from
inheriting a previous completion receipt. No catalog rows are read by this module.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import sqlite3
import sys
import uuid

VERSION = 2
SOURCE_TABLES = ('jobs', 'job_families', 'job_family_members',
                 'job_template_clusters', 'job_template_cluster_lineage')


def _names(kind):
    if kind not in ('source', 'scores'):
        raise ValueError('unsupported refresh tracker')
    prefix = 'ranking_refresh_' + kind
    return prefix, prefix + '_epoch', prefix + '_revisions'


def _schema(con, kind):
    prefix, epoch, revisions = _names(kind)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = ('jobs',) if kind == 'source' else ('preference_scores',)
    if not set(required) <= tables:
        raise ValueError('refresh tracking requires ' + ', '.join(required))
    tracked = [name for name in SOURCE_TABLES if name in tables] if kind == 'source' else ['preference_scores']
    result = {
        epoch: f'CREATE TABLE {epoch} (id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL)',
        revisions: f'CREATE TABLE {revisions} (scope TEXT PRIMARY KEY, revision INTEGER NOT NULL)',
    }
    if kind == 'source':
        result[prefix + '_events'] = ('CREATE TABLE ranking_refresh_source_events '
            '(revision INTEGER PRIMARY KEY, families_json TEXT NOT NULL)')
        # Journal entries are immutable. An out-of-band edit creates an explicit
        # revision gap, which prevents a valid-looking altered list from sealing
        # untouched scores. Retention only deletes old entries.
        result[prefix + '_events_update'] = (
            'CREATE TRIGGER ranking_refresh_source_events_update AFTER UPDATE ON ranking_refresh_source_events BEGIN '
            "INSERT INTO ranking_refresh_source_revisions(scope,revision) VALUES ('catalog',1) "
            'ON CONFLICT(scope) DO UPDATE SET revision=revision+1; END')
    for table in tracked:
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            name = prefix + '_' + table + '_' + event.lower()
            if kind == 'source':
                scopes = ["'catalog'"]
            elif event == 'UPDATE':
                # A changed run_id invalidates both the old and new policy cache.
                scopes = ['OLD.run_id', 'NEW.run_id']
            else:
                scopes = [('OLD' if event == 'DELETE' else 'NEW') + '.run_id']
            statements = [f'INSERT INTO {revisions}(scope,revision) VALUES ({scope},1) '
                          'ON CONFLICT(scope) DO UPDATE SET revision=revision+1;' for scope in scopes]
            condition = ''
            if event == 'UPDATE':
                columns = [r[1] for r in con.execute(f'PRAGMA table_info({table})')]
                if kind == 'source' and table == 'jobs':
                    columns = [c for c in columns if c != 'last_seen']
                condition = ' WHEN ' + ' OR '.join('OLD."' + c.replace('"', '""') + '" IS NOT NEW."' + c.replace('"', '""') + '"' for c in columns)
            if kind == 'source':
                refs = ('OLD', 'NEW') if event == 'UPDATE' else (('OLD',) if event == 'DELETE' else ('NEW',))
                if table == 'jobs':
                    selects = [f'SELECT family_id FROM job_family_members WHERE ats={ref}.ats AND job_id={ref}.id' for ref in refs] if 'job_family_members' in tables else []
                elif table == 'job_template_cluster_lineage':
                    selects = [f'SELECT family_id FROM job_template_clusters WHERE template_cluster_id IN ({ref}.template_cluster_id,{ref}.lineage_id)' for ref in refs]
                else:
                    selects = [f'SELECT {ref}.family_id AS family_id' for ref in refs]
                families = '(SELECT json_group_array(family_id) FROM (' + ' UNION '.join(selects) + '))' if selects else "'[]'"
                statements.append(f"INSERT INTO {prefix}_events SELECT revision,{families} FROM {revisions} WHERE scope='catalog';")
            result[name] = f'CREATE TRIGGER {name} AFTER {event} ON {table}{condition} BEGIN ' + ' '.join(statements) + ' END'

    return result


def _valid_schema(con, kind):
    prefix, _, _ = _names(kind)
    expected = _schema(con, kind)
    actual = {r[0]: r[1] for r in con.execute(
        "SELECT name,sql FROM sqlite_master WHERE name GLOB ?", (prefix + '_*',))}
    return actual == expected


def check_tracking(con, kind):
    """Validate metadata and the epoch without repairing or pruning anything."""
    _, epoch, _ = _names(kind)
    if not _valid_schema(con, kind):
        raise RuntimeError("Schema migration required: ranking refresh tracking mismatch")
    row = con.execute(f'SELECT value FROM {epoch} WHERE id=1').fetchone()
    if not row or not isinstance(row[0], str) or not re.fullmatch('[a-f0-9]{32}', row[0]):
        raise RuntimeError("Initialization required: ranking refresh epoch is missing")


def ensure_tracking(path: Path, kind: str):
    """Install or repair a small tracker atomically, never certifying old data."""
    from job_search.schema_lifecycle import require_current, read_connection
    if require_current():
        with closing(read_connection(path)) as con:
            check_tracking(con, kind)
        return
    prefix, epoch, revisions = _names(kind)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=5)) as con, con:
        con.execute('BEGIN IMMEDIATE')
        expected = _schema(con, kind)
        tables = {r[0]: r[1] for r in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
        table_names = [name for name, sql in expected.items() if sql.startswith('CREATE TABLE')]
        for name in table_names:
            if name in tables and tables[name] != expected[name]:
                raise ValueError('invalid ranking refresh tracking table')
            if name not in tables:
                con.execute(expected[name])
        row = con.execute(f'SELECT value FROM {epoch} WHERE id=1').fetchone()
        if _valid_schema(con, kind) and row and isinstance(row[0], str) and re.fullmatch('[a-f0-9]{32}', row[0]):
            if kind == 'source':
                con.execute('DELETE FROM ranking_refresh_source_events WHERE revision <= (SELECT COALESCE(MAX(revision),0)-100000 FROM ranking_refresh_source_events)')
            return
        triggers = list(con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB ?", (prefix + '_*',)))
        for (name,) in triggers:
            # Only module-owned identifiers are interpolated, quoted defensively.
            con.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for name, sql in expected.items():
            if name not in table_names:
                con.execute(sql)
        con.execute(f'DELETE FROM {revisions}')
        if kind == 'source':
            con.execute('DELETE FROM ranking_refresh_source_events')
        con.execute(f'INSERT OR REPLACE INTO {epoch}(id,value) VALUES (1,?)', (uuid.uuid4().hex,))


def revision_token(con: sqlite3.Connection, path: Path, kind: str, run_id=None):
    """Read evidence in the caller's transaction; missing/changed tracking fails closed."""
    _, epoch, revisions = _names(kind)
    if not _valid_schema(con, kind):
        return None
    row = con.execute(f'SELECT value FROM {epoch} WHERE id=1').fetchone()
    if not row or not isinstance(row[0], str) or not re.fullmatch('[a-f0-9]{32}', row[0]):
        return None
    value = con.execute(f'SELECT revision FROM {revisions} WHERE scope=?',
                        ('catalog' if kind == 'source' else run_id,)).fetchone()
    revision = value[0] if value else 0
    if type(revision) is not int or revision < 0:
        return None
    info = path.stat()
    return {'version': VERSION, 'epoch': row[0], 'revision': revision,
            'schema_version': con.execute('PRAGMA schema_version').fetchone()[0],
            'database_identity': [info.st_dev, info.st_ino]}


def scoring_signature(artifact, record, *, active_components_only):
    """Only a verified artifact manifest can produce a reusable score signature."""
    from . import model
    from job_search.collection import dedupe
    record = dict(record)
    try:
        digest = json.loads(record.get('manifest_json', '{}')).get('artifacts', {}).get('model.pkl')
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            return None
        # Include implementation bytes: changing feature/scoring behavior must not
        # silently keep an old seal merely because a version constant was missed.
        code = hashlib.sha256()
        for path in (Path(model.__file__), Path(dedupe.__file__), Path(dedupe.__file__).with_name('dedupe_incremental.py'), Path(__file__), Path(__file__).with_name('refresh.py')):
            code.update(path.read_bytes())
        dependencies = {}
        for name in ('numpy', 'scipy', 'scikit-learn'):
            try:
                dependencies[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                dependencies[name] = None
        value = {'version': VERSION, 'model_record': record, 'artifact_sha256': digest,
                 'weights': artifact['weights'], 'text_version': model.TEXT_VERSION,
                 'normalization_version': dedupe.NORMALIZATION_VERSION,
                 'components': model.scoring_components(artifact, active_components_only=active_components_only),
                 'active_components_only': active_components_only, 'code_sha256': code.hexdigest(),
                 'python': {'implementation': sys.implementation.name,
                            'version': list(sys.version_info[:3])},
                 'dependencies': dependencies}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, KeyError, OSError):
        return None


def read_value(con, key):
    row = con.execute('SELECT value FROM preference_state WHERE key=?', (key,)).fetchone()
    try:
        value = json.loads(row[0]) if row else None
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def cache_key(run_id):
    return 'policy_refresh_cache:' + run_id


def cache_matches(con, path, run_id, signature):
    cached = read_value(con, cache_key(run_id))
    current = revision_token(con, path, 'scores', run_id)
    return bool(signature and current and cached and cached.get('signature') == signature
                and cached.get('scores') == current)


def cache_receipt(con, path, run_id, signature):
    current = revision_token(con, path, 'scores', run_id)
    if current is None:
        raise ValueError('ranking refresh score tracker changed')
    return {'signature': signature, 'scores': current}


def reusable_receipt(con, state_path, policy, run_id, signature, source_token):
    receipt = read_value(con, 'policy_refresh:' + policy)
    seal = receipt.get('reuse') if receipt else None
    result = receipt.get('result') if receipt else None
    current = revision_token(con, state_path, 'scores', run_id)
    try:
        completed = datetime.fromisoformat(receipt['completed_at'].replace('Z', '+00:00'))
        if completed.tzinfo is None:
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not (signature and source_token and isinstance(seal, dict) and isinstance(result, dict)
            and receipt.get('run_id') == run_id and seal.get('signature') == signature
            and isinstance(receipt.get('completed_at'), str) and 'source_watermark' in receipt
            and (receipt['source_watermark'] is None or isinstance(receipt['source_watermark'], str))
            and seal.get('source') == source_token and seal.get('complete') is True
            and type(seal.get('family_count')) is int and seal['family_count'] >= 0
            and result.get('scored_families') == seal['family_count']
            and current and seal.get('scores') == current
            and cache_matches(con, state_path, run_id, signature)):
        return None
    return receipt


def changed_families(source, previous, current):
    """Prove contiguous journal coverage since a completed source snapshot.

    Events older than the retained window, malformed rows, a replaced database,
    or tracker/schema repairs require a full pass. Events contain their own
    family lists so deleting an event cannot quietly lose a family update.
    """
    if not isinstance(previous, dict) or not current:
        return None
    if any(previous.get(k) != current.get(k) for k in current if k != 'revision'):
        return None
    start, end = previous.get('revision'), current['revision']
    if type(start) is not int or start < 0 or start > end:
        return None
    rows = source.execute('SELECT revision,families_json FROM ranking_refresh_source_events WHERE revision>? AND revision<=? ORDER BY revision', (start, end))
    expected, families = start + 1, set()
    for revision, payload in rows:
        if revision != expected:
            return None
        expected += 1
        try:
            ids = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(ids, list) or any(not isinstance(f, str) or not f.startswith('fam_') for f in ids):
            return None
        families.update(ids)
    return sorted(families) if expected == end + 1 else None


def incremental_families(source, state, state_path, policy, run_id, signature, source_token):
    receipt = read_value(state, 'policy_refresh:' + policy)
    seal = receipt.get('reuse') if receipt else None
    if not isinstance(seal, dict) or not seal.get('complete') or seal.get('signature') != signature or receipt.get('run_id') != run_id:
        return None
    # Completed rows may have been changed or partially refreshed. Require the
    # original score revision; a partial pass safely falls back to fingerprint
    # checks and keeps committed batches rather than blessing unvisited rows.
    if seal.get('scores') != revision_token(state, state_path, 'scores', run_id):
        return None
    if not cache_matches(state, state_path, run_id, signature):
        return None
    return changed_families(source, seal.get('source'), source_token)
