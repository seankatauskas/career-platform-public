"""Lease-fenced provider results, allowing later stages to defer without replay."""
import json
from datetime import datetime

from job_search.commands import DomainError, encode

SCHEMA = '''
CREATE TABLE IF NOT EXISTS understand_stage_checkpoints (
 input_key TEXT NOT NULL REFERENCES understand_claims(input_key),
 request_key TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(input_key,request_key)
);
'''


def bounded_result(value, schema):
    """Keep only bounded schema fields, replacing invalid data with invalid markers.

    Checkpoints never retain prompts, source bodies or arbitrary response fields.
    Markers remain invalid under the requested schema, so pruning cannot turn a
    rejected finding into a valid one. The same value is used now and on resume.
    """
    if 'anyOf' in schema:
        choices = schema['anyOf']
        if isinstance(value, dict):
            candidates = choices
            for discriminator in ('kind', 'finding_id', 'target_id', 'topic'):
                if discriminator not in value:
                    continue
                selected = [c for c in candidates if value[discriminator] in c.get('properties', {}).get(discriminator, {}).get('enum', [])]
                if selected:
                    candidates = selected
                if len(candidates) == 1:
                    return bounded_result(value, candidates[0])
        selected = next((c for c in choices if c.get('type') == ('null' if value is None else 'string' if isinstance(value,str) else 'object')), choices[0])
        return bounded_result(value, selected)
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        maximum = schema.get('maxLength', 2000)
        if 'enum' in schema and value not in schema['enum']:
            return '__invalid_choice__'
        return value if len(value) <= maximum else '_' * (maximum + 1)
    if isinstance(value, list):
        if 'array' not in types:
            return []
        maximum = schema.get('maxItems', 80)
        return [bounded_result(v, schema['items']) for v in value[:maximum + 1]]
    if isinstance(value, dict):
        if 'object' not in types:
            return {}
        properties = schema.get('properties', {})
        result = {key: bounded_result(child, properties[key]) for key, child in value.items() if key in properties}
        if set(value) - set(properties):
            result['__invalid_field__'] = None
        return result
    return False


def _owned(connection, claim, now):
    row = connection.execute('SELECT * FROM understand_claims WHERE input_key=?', (claim['input_key'],)).fetchone()
    if (not row or row['status'] != 'processing' or row['token'] != claim['token']
            or row['generation'] != claim['generation']
            or datetime.fromisoformat(row['lease_until'].replace('Z', '+00:00')) <= datetime.fromisoformat(now.replace('Z', '+00:00'))):
        raise DomainError('version_conflict', 'Analysis checkpoint lease is no longer owned')


def read(connection, claim, request_key, now):
    _owned(connection, claim, now)
    row = connection.execute('SELECT result FROM understand_stage_checkpoints WHERE input_key=? AND request_key=?',
                             (claim['input_key'], request_key)).fetchone()
    return json.loads(row[0]) if row else None


def write(tx, claim, request_key, result):
    _owned(tx.connection, claim, tx.now)
    if (len(request_key) != 64 or set(request_key) - set('0123456789abcdef')
            or not isinstance(result, dict) or set(result) not in ({'value'}, {'error'})
            or len(encode(result).encode()) > 131072):
        raise DomainError('invalid_input', 'Invalid analysis checkpoint')
    with tx.scope('understanding'):
        tx.connection.execute('INSERT INTO understand_stage_checkpoints VALUES(?,?,?) ON CONFLICT(input_key,request_key) DO NOTHING',
                              (claim['input_key'], request_key, encode(result)))
    return {'checkpointed': True}
