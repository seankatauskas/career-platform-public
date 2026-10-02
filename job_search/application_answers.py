"""Private, immutable form snapshots attached to authenticated browser attempts."""
from __future__ import annotations

import hashlib
import json
import re
import time

from .contracts import ContractError, ConflictError, parse_utc, utc_now
from .db import connect

MAX_FIELDS = 400
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
CONTROLS = frozenset({'text', 'textarea', 'richtext', 'select', 'checkbox', 'radio', 'file'})


def exact_json(value):
    # Answer history must not normalize Unicode or otherwise rewrite the prose.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {'version', 'fields', 'omitted_fields', 'truncated_values'} or snapshot['version'] != 1:
        raise ContractError('invalid answer snapshot')
    for key in ('omitted_fields', 'truncated_values'):
        if type(snapshot[key]) is not int or not 0 <= snapshot[key] <= 100000:
            raise ContractError('invalid answer capture coverage')
    fields = snapshot['fields']
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise ContractError('too many captured fields')
    seen = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) != {'field_key', 'prompt', 'section', 'control', 'value'}:
            raise ContractError('invalid captured field')
        for key, limit in (('field_key', 64000), ('prompt', 2000), ('section', 2000)):
            if not isinstance(field[key], str) or len(field[key]) > limit:
                raise ContractError('invalid captured field label')
        if not field['field_key'] or not field['prompt'] or field['field_key'] in seen:
            raise ContractError('duplicate or missing captured field identity')
        seen.add(field['field_key'])
        control, value = field['control'], field['value']
        if not isinstance(control, str) or control not in CONTROLS:
            raise ContractError('unsupported captured control')
        if control in {'checkbox', 'radio'}:
            valid = type(value) is bool
        elif control in {'select', 'file'}:
            valid = isinstance(value, list) and len(value) <= 400 and all(isinstance(v, str) and len(v) <= 64000 for v in value)
        else:
            valid = isinstance(value, str) and len(value) <= 64000
        if not valid:
            raise ContractError('invalid captured value')
    try:
        size = len(exact_json(snapshot).encode('utf-8'))
    except UnicodeEncodeError as exc:
        raise ContractError('invalid answer text encoding') from exc
    if size > MAX_SNAPSHOT_BYTES:
        raise ContractError('answer snapshot is too large')
    return snapshot


def save_snapshot(path, device, body):
    from .browser_tracking import identify_job
    if set(body) - {'device_token','capture_id','attempt_id','page_url','captured_at','snapshot'}:
        raise ContractError('unsupported answer capture fields')
    for key in ('capture_id', 'attempt_id'):
        if not isinstance(body.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', body[key]):
            raise ContractError('invalid answer capture identity')
    captured_at = body.get('captured_at')
    if not isinstance(captured_at, str) or parse_utc(captured_at).timestamp() > time.time()+300:
        raise ContractError('invalid answer capture time')
    identity = identify_job(body.get('page_url'))
    snapshot = validate_snapshot(body.get('snapshot'))
    request_hash = hashlib.sha256(exact_json({key:value for key,value in body.items() if key != 'device_token'}).encode('utf-8')).hexdigest()
    with connect(path) as con:
        con.execute('BEGIN IMMEDIATE')
        attempt = con.execute('SELECT a.application_id,a.device_id,p.ats,p.job_id,p.job_url_snapshot FROM browser_attempts a JOIN applications p USING(application_id) WHERE attempt_id=?', (body['attempt_id'],)).fetchone()
        if not attempt or attempt['device_id'] != device:
            raise ContractError('unknown browser attempt')
        if (attempt['ats'], attempt['job_id']) != (identity['ats'], identity['job_id']):
            raise ConflictError('answer capture belongs to another application')
        # The attempt already passed board validation when it was recorded.
        try:
            known = identify_job(attempt['job_url_snapshot'])
        except ContractError:
            known = None
        if known and known['board'].casefold() != identity['board'].casefold():
            raise ConflictError('answer capture board does not match application')
        previous = con.execute('SELECT * FROM application_answer_snapshots WHERE capture_id=?', (body['capture_id'],)).fetchone()
        if previous:
            if previous['request_sha256'] != request_hash or previous['attempt_id'] != body['attempt_id']:
                raise ConflictError('answer capture ID was reused with different data')
        else:
            con.execute('INSERT INTO application_answer_snapshots VALUES (?,?,?,?,?,?,?,?)',
                (body['capture_id'],attempt['application_id'],body['attempt_id'],captured_at,utc_now(),identity['canonical_url'],request_hash,exact_json(snapshot)))
    return {'saved':True,'capture_id':body['capture_id'],'application_id':attempt['application_id'],'field_count':len(snapshot['fields'])}


def application_snapshots(path, application_id):
    with connect(path) as con:
        rows = con.execute('SELECT s.*,a.status AS attempt_status FROM application_answer_snapshots s JOIN browser_attempts a USING(attempt_id) WHERE s.application_id=? ORDER BY s.captured_at DESC,s.rowid DESC', (application_id,)).fetchall()
    return [{**{key:row[key] for key in ('capture_id','attempt_id','captured_at','recorded_at','page_url','attempt_status')},
             'snapshot':json.loads(row['snapshot_json'])} for row in rows]
