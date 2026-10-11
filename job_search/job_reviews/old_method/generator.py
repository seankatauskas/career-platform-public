"""User-requested list generation; continuity stays outside the model worker."""
from contextlib import closing
import json
import secrets

from ...contracts import ContractError, canonical_json, payload_sha256
from ...db import connect
from ...maintenance import require_start
from ..contracts import exact, identifier, stamp, timestamp
from . import WORKFLOW
from .screen import VERSION
from .window import DISCOVERY_WINDOW

ACTIONS = {'generate', 'generation-status', 'generation-resume'}
STREAM = 'default'
SCOPE = "json_extract(metadata_json,'$.workflow')=? AND json_extract(metadata_json,'$.generation_stream')=?"


class Generator:
    def __init__(self, service):
        self.service = service

    def _latest(self, con, *, active=False, published=False):
        state = " AND status='active'" if active else " AND status='published'" if published else ''
        return con.execute('SELECT * FROM job_reviews WHERE ' + SCOPE + state +
                           ' ORDER BY window_end DESC,sequence DESC LIMIT 1', (WORKFLOW, STREAM)).fetchone()

    def _get(self, con, rid):
        identifier(rid, 'review_id')
        row = con.execute('SELECT * FROM job_reviews WHERE review_id=? AND ' + SCOPE,
                          (rid, WORKFLOW, STREAM)).fetchone()
        if row is None:
            raise ContractError('not a list generator run')
        return row

    def _status(self, con, row=None):
        previous = self._latest(con, published=True)
        if row is None:
            row = self._latest(con, active=True) or self._latest(con)
        base = {'generation_stream': STREAM, 'covered_through': previous['window_end'] if previous else None,
                'needs_initial_start': row is None and previous is None}
        if row is None:
            return {**base, 'phase': 'idle', 'review_id': None}
        meta = json.loads(row['metadata_json'])
        return {**base, 'review_id': row['review_id'], 'status': row['status'], 'phase': meta['phase'],
                'window_start': row['window_start'], 'window_end': row['window_end'],
                'assessment_count': meta.get('assessment_count', 0),
                'can_resume': row['status'] == 'active' and meta['phase'] in ('failed', 'interrupted', 'dispatch_failed'),
                'error': meta.get('error'), 'dashboard_path': '#shortlist',
                'status_path': '/api/v1/job-reviews/generation-status?review_id=' + row['review_id'],
                'receipt': json.loads(row['receipt_json']) if row['receipt_json'] else None}

    def call(self, action, args=None):
        from ..service import FIELDS
        args = dict(args or {})
        exact(args, FIELDS[action])
        if len(canonical_json(args).encode()) > 64 * 1024:
            raise ContractError('review request exceeds 64 KiB')
        write = action != 'generation-status'
        with closing(connect(self.service.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if not write:
                return self._status(con, self._get(con, args['review_id']) if args.get('review_id') else None)
            key = identifier(args.get('idempotency_key'), 'idempotency_key')
            digest = payload_sha256({'action': action, 'args': args})
            prior = con.execute('SELECT * FROM job_review_commands WHERE idempotency_key=?', (key,)).fetchone()
            if prior:
                if prior['operation'] != action or prior['request_sha256'] != digest:
                    raise ContractError('idempotency key was reused for a different review command')
                return self._status(con, self._get(con, json.loads(prior['response_json'])['review_id']))
            try:
                require_start('review-runner')
            except (RuntimeError, OSError, ValueError):
                raise ContractError('list generation is paused for maintenance') from None
            if action == 'generation-resume':
                row = self._get(con, args.get('review_id'))
                meta = json.loads(row['metadata_json'])
                if row['status'] == 'active' and meta['phase'] in ('failed', 'interrupted', 'dispatch_failed'):
                    meta.update(phase='queued' if meta.get('inputs_ready') else 'awaiting_inputs', error=None)
                    con.execute('UPDATE job_reviews SET metadata_json=?,updated_at=? WHERE review_id=?',
                                (canonical_json(meta), self.service.now(), row['review_id']))
            else:
                row = self._latest(con, active=True)
                if row is None:
                    previous = self._latest(con, published=True)
                    if previous and args.get('initial_start') is not None:
                        raise ContractError('initial_start is only allowed for the first generator run')
                    if previous is None and args.get('initial_start') is None:
                        raise ContractError('first generation requires initial_start, an ISO timestamp with timezone')
                    start = timestamp(previous['window_end'] if previous else args['initial_start'])
                    end = timestamp(self.service.now())
                    if not start < end:
                        raise ContractError('generation start must be before now')
                    rid = 'oldreview_' + secrets.token_hex(16)
                    meta = {'workflow': WORKFLOW, 'generation_stream': STREAM, 'execution_mode': 'isolated',
                            'phase': 'awaiting_inputs', 'screen_version': VERSION, 'window_policy': DISCOVERY_WINDOW,
                            'publish': True, 'inputs_ready': False, 'assessment_count': 0, 'omitted': []}
                    con.execute('INSERT INTO job_reviews(review_id,mode,window_start,window_end,created_at,updated_at,status,context_json,context_sha256,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (rid, 'custom', stamp(start), stamp(end), stamp(end), stamp(end), 'active', '{}',
                                 payload_sha256({}), canonical_json(meta)))
                    row = self._get(con, rid)
            rid = row['review_id']
            con.execute('INSERT INTO job_review_commands VALUES(?,?,?,?,?)',
                        (key, action, digest, canonical_json({'review_id': rid}), self.service.now()))
            return self._status(con, self._get(con, rid))
