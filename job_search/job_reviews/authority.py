"""Trusted coordinator and scoped authority for physically isolated reviewers.

The HTTP/MCP boundary receives only ``scoped_call``. Administrative methods are
internal capabilities and must never be installed as reviewer tools. Tokens are
returned once to the trusted launcher; only their SHA-256 hashes are persisted.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import timedelta

from ..contracts import ContractError, canonical_json, payload_sha256
from ..db import connect
from .contracts import (REVIEW_JOB_FIELDS, REVIEW_SUMMARY_FIELDS, bounded, exact,
                        identifier, integer, stamp, timestamp)
from .service import _COORDINATOR, _ReviewPrincipal, unpack

MODEL = 'gpt-6-astra'
REASONING_EFFORT = 'high'
LEASE_SECONDS = 30 * 60
MAX_ASSIGNMENT_JOBS = 20


class ReviewAuthorizationError(ContractError):
    """A reviewer capability is missing, invalid, expired or revoked."""


class ReviewConflictError(ContractError):
    """Assignment evidence or ownership changed, or a replay conflicts."""


def _token_hash(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii():
        raise ReviewAuthorizationError('reviewer authorization failed')
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _command_key(grant_id, ordinal):
    return f'grant:{grant_id}:{ordinal}'


class ReviewAuthority:
    def __init__(self, service, *, approved_model=MODEL, approved_reasoning_effort=REASONING_EFFORT):
        identifier(approved_model, 'approved_model')
        identifier(approved_reasoning_effort, 'approved_reasoning_effort')
        self.service = service
        self.approved_model = approved_model
        self.approved_reasoning_effort = approved_reasoning_effort

    @contextmanager
    def _transaction(self):
        with closing(connect(self.service.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE')
            yield con

    def _now(self):
        return stamp(timestamp(self.service.now()))

    def _managed_run(self, con, review_id, *, active=True):
        run = self.service._run(con, review_id, active)
        if unpack(run['metadata_json']).get('execution_mode') != 'isolated':
            raise ContractError('review is not managed by the isolated coordinator')
        return run

    def _trusted(self, action, args):
        with self._transaction() as con:
            if action != 'start':
                self._managed_run(con, args['review_id'], active=False)
            result = self.service._call_in_transaction(con, action, args, principal=_COORDINATOR)
            if action == 'start':
                self._managed_run(con, result['review_id'], active=False)
            return result

    def start(self, args):
        """Create a managed review from the complete strict posting window."""
        return self._trusted('start', args)

    def status(self, review_id):
        return self._trusted('status', {'review_id': review_id})

    def preview(self, review_id):
        with self._transaction() as con:
            self._managed_run(con, review_id, active=False)
            self._validate_provenance(con, review_id)
            return self.service._call_in_transaction(con, 'preview', {'review_id': review_id}, principal=_COORDINATOR)

    def publish(self, review_id, preview_sha256, idempotency_key):
        with self._transaction() as con:
            self._managed_run(con, review_id, active=False)
            self._validate_provenance(con, review_id)
            return self.service._call_in_transaction(con, 'publish', {
                'review_id': review_id, 'preview_sha256': preview_sha256,
                'idempotency_key': idempotency_key,
            }, principal=_COORDINATOR)

    def refresh(self, review_id, ordinal, expected_revision, idempotency_key=None):
        with self._transaction() as con:
            self._managed_run(con, review_id)
            self._revoke_item_grants(con, review_id, ordinal)
            return self.service._call_in_transaction(con, 'refresh-job', {
                'review_id': review_id, 'ordinal': ordinal, 'expected_revision': expected_revision,
                'actor': 'isolated-coordinator',
                'idempotency_key': idempotency_key or f'isolated-refresh:{review_id}:{ordinal}:{expected_revision}',
            }, principal=_COORDINATOR)

    def abandon(self, review_id, reason, idempotency_key):
        with self._transaction() as con:
            self._managed_run(con, review_id, active=False)
            self._revoke_review_grants(con, review_id)
            return self.service._call_in_transaction(con, 'abandon', {
                'review_id': review_id, 'reason': reason, 'idempotency_key': idempotency_key,
            }, principal=_COORDINATOR)

    def issue(self, review_id, ordinals, kind, runtime):
        """Issue a credential once; runtime identity is supplied by the launcher."""
        identifier(review_id, 'review_id')
        if kind not in ('primary', 'check'):
            raise ContractError('kind must be primary or check')
        if not isinstance(ordinals, (list, tuple)) or not 1 <= len(ordinals) <= MAX_ASSIGNMENT_JOBS:
            raise ContractError('assignment requires one to twenty explicit ordinals')
        ordinals = [integer(value, 'ordinal', 1) for value in ordinals]
        if len(set(ordinals)) != len(ordinals):
            raise ContractError('assignment ordinals must be unique')
        exact(runtime, ('runtime_id', 'model', 'reasoning_effort'), ('runtime_id', 'model', 'reasoning_effort'))
        identifier(runtime['runtime_id'], 'runtime_id')
        if runtime['model'] != self.approved_model or runtime['reasoning_effort'] != self.approved_reasoning_effort:
            raise ContractError('reviewer model and reasoning must match trusted runtime configuration')
        token = secrets.token_urlsafe(32)
        grant_id = 'grant_' + secrets.token_hex(16)
        actor = 'isolated_' + secrets.token_hex(16)
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            now = self._now()
            expires = stamp(timestamp(now) + timedelta(seconds=LEASE_SECONDS))
            items = []
            for ordinal in sorted(ordinals):
                item = self.service._item(con, {'review_id': review_id, 'ordinal': ordinal})
                if kind == 'primary' and item['assessment_json'] is not None:
                    raise ReviewConflictError('primary assignment requires an unassessed job')
                if kind == 'check' and (item['assessment_json'] is None or item['check_json'] is not None):
                    raise ReviewConflictError('check assignment requires an unchecked primary assessment')
                if item['claim_owner'] and item['claim_until'] and item['claim_until'] > now:
                    raise ReviewConflictError('job already has an active reviewer lease')
                conflict = con.execute(
                    'SELECT 1 FROM job_review_grant_items i JOIN job_review_grants g USING(grant_id) '
                    'WHERE g.review_id=? AND i.ordinal=? AND g.revoked_at IS NULL AND g.expires_at>? '
                    'AND NOT EXISTS (SELECT 1 FROM job_review_commands c '
                    "WHERE c.idempotency_key='grant:' || g.grant_id || ':' || i.ordinal)",
                    (review_id, ordinal, now)).fetchone()
                if conflict:
                    raise ReviewConflictError('job already has an active reviewer assignment')
                items.append(item)
            try:
                con.execute('INSERT INTO job_review_grants '
                            '(grant_id,token_sha256,review_id,actor,kind,context_sha256,runtime_id,runtime_json,created_at,expires_at) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (grant_id, _token_hash(token), review_id, actor, kind, run['context_sha256'],
                             runtime['runtime_id'], canonical_json(runtime), now, expires))
            except sqlite3.IntegrityError:
                raise ReviewConflictError('runtime identity has already been assigned') from None
            for item in items:
                con.execute('INSERT INTO job_review_grant_items VALUES(?,?,?,?)',
                            (grant_id, item['ordinal'], item['revision'], item['snapshot_sha256']))
                con.execute('UPDATE job_review_items SET claim_owner=?,claim_until=? WHERE review_id=? AND ordinal=?',
                            (actor, expires, review_id, item['ordinal']))
            return {'grant_id': grant_id, 'token': token, 'actor': actor, 'expires_at': expires}

    @staticmethod
    def _launch_receipt(receipt):
        exact(receipt, ('container_id', 'image_digest', 'config_sha256'),
              ('container_id', 'image_digest', 'config_sha256'))
        if not isinstance(receipt['container_id'], str) or not re.fullmatch(r'[0-9a-f]{64}', receipt['container_id']):
            raise ContractError('launch receipt requires a full container identifier')
        if not isinstance(receipt['image_digest'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', receipt['image_digest']):
            raise ContractError('launch receipt requires a pinned image digest')
        if not isinstance(receipt['config_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', receipt['config_sha256']):
            raise ContractError('launch receipt requires a runtime configuration digest')
        return dict(receipt)

    def mark_launch(self, grant_id, runtime_id, receipt):
        """Record the trusted launcher's verified container identity, never model output."""
        receipt = self._launch_receipt(receipt)
        with self._transaction() as con:
            grant = con.execute('SELECT * FROM job_review_grants WHERE grant_id=?', (grant_id,)).fetchone()
            if (grant is None or grant['runtime_id'] != runtime_id or grant['revoked_at']
                    or grant['expires_at'] <= self._now()):
                raise ReviewAuthorizationError('reviewer launch cannot be recorded')
            self._managed_run(con, grant['review_id'])
            encoded = canonical_json(receipt)
            if grant['launch_json'] is not None and grant['launch_json'] != encoded:
                raise ReviewConflictError('reviewer launch identity is immutable')
            if con.execute('SELECT 1 FROM job_review_grants WHERE grant_id<>? AND '
                           "json_extract(launch_json,'$.container_id')=?", (grant_id, receipt['container_id'])).fetchone():
                raise ReviewConflictError('a container cannot run multiple reviewer assignments')
            con.execute('UPDATE job_review_grants SET launch_json=? WHERE grant_id=?', (encoded, grant_id))
            return {'grant_id': grant_id, 'runtime_id': runtime_id, 'launched': True}

    def _grant(self, con, token):
        grant = con.execute('SELECT * FROM job_review_grants WHERE token_sha256=?', (_token_hash(token),)).fetchone()
        if grant is None or grant['revoked_at'] or grant['expires_at'] <= self._now():
            raise ReviewAuthorizationError('reviewer authorization failed')
        if grant['launch_json'] is None:
            raise ReviewAuthorizationError('reviewer runtime has not been launched')
        return dict(grant)

    def _slot(self, con, grant, ordinal):
        integer(ordinal, 'ordinal', 1)
        slot = con.execute('SELECT * FROM job_review_grant_items WHERE grant_id=? AND ordinal=?',
                           (grant['grant_id'], ordinal)).fetchone()
        if slot is None:
            raise ReviewAuthorizationError('job is outside this reviewer assignment')
        return dict(slot)

    def _current(self, con, grant, slot):
        run = self._managed_run(con, grant['review_id'])
        if run['context_sha256'] != grant['context_sha256']:
            raise ReviewConflictError('review context changed since assignment')
        item = self.service._item(con, {'review_id': grant['review_id'], 'ordinal': slot['ordinal']})
        if item['revision'] != slot['expected_revision'] or item['snapshot_sha256'] != slot['snapshot_sha256']:
            raise ReviewConflictError('review evidence changed since assignment')
        if item['claim_owner'] != grant['actor'] or not item['claim_until'] or item['claim_until'] <= self._now():
            raise ReviewConflictError('reviewer lease is no longer current')
        return item

    def renew(self, grant_id):
        identifier(grant_id, 'grant_id')
        with self._transaction() as con:
            row = con.execute('SELECT * FROM job_review_grants WHERE grant_id=?', (grant_id,)).fetchone()
            if row is None or row['revoked_at'] or row['expires_at'] <= self._now():
                raise ReviewAuthorizationError('reviewer lease cannot be renewed')
            grant = dict(row)
            self._managed_run(con, grant['review_id'])
            pending = []
            for slot in con.execute('SELECT * FROM job_review_grant_items WHERE grant_id=? ORDER BY ordinal', (grant_id,)).fetchall():
                if con.execute('SELECT 1 FROM job_review_commands WHERE idempotency_key=?',
                               (_command_key(grant_id, slot['ordinal']),)).fetchone():
                    continue
                pending.append(self._current(con, grant, slot))
            expires = stamp(timestamp(self._now()) + timedelta(seconds=LEASE_SECONDS))
            con.execute('UPDATE job_review_grants SET expires_at=? WHERE grant_id=?', (expires, grant_id))
            for item in pending:
                con.execute('UPDATE job_review_items SET claim_until=? WHERE review_id=? AND ordinal=? AND claim_owner=?',
                            (expires, grant['review_id'], item['ordinal'], grant['actor']))
            return {'grant_id': grant_id, 'expires_at': expires, 'pending_count': len(pending)}

    def _revoke(self, con, grant_id):
        row = con.execute('SELECT * FROM job_review_grants WHERE grant_id=?', (grant_id,)).fetchone()
        if row is None:
            raise ContractError('reviewer assignment was not found')
        when = row['revoked_at'] or self._now()
        con.execute('UPDATE job_review_grants SET revoked_at=? WHERE grant_id=?', (when, grant_id))
        con.execute('UPDATE job_review_items SET claim_owner=NULL,claim_until=NULL WHERE review_id=? AND claim_owner=?',
                    (row['review_id'], row['actor']))
        return {'grant_id': grant_id, 'revoked_at': when}

    def revoke(self, grant_id):
        identifier(grant_id, 'grant_id')
        with self._transaction() as con:
            return self._revoke(con, grant_id)

    def reconcile_interrupted(self, review_id=None):
        """Revoke prior assignments while preserving reviews and committed results.

        The trusted coordinator MUST hold the exclusive runner lock and stop new
        dispatch before calling this method. Only server-managed reviews and their
        validated runtime bindings participate. Returned inventory contains no
        credential or source evidence. The runtime must match both its worker label
        and these grant/container identities before terminating a live container.

        Already-revoked launches remain in the inventory: a crash after committing
        revocation but before Docker cleanup must be recoverable on the next start.
        A null container_id covers issuance or a launch that crashed before its
        receipt was recorded; the trusted grant label identifies that worker.
        """
        if review_id is not None:
            identifier(review_id, 'review_id')
        scope = "json_extract(r.metadata_json,'$.execution_mode')='isolated'"
        parameters = ()
        if review_id is not None:
            scope += ' AND g.review_id=?'
            parameters = (review_id,)
        with self._transaction() as con:
            if review_id is not None:
                self._managed_run(con, review_id, active=False)
            workers = []
            for grant in con.execute(
                'SELECT g.* FROM job_review_grants g JOIN job_reviews r USING(review_id) '
                'WHERE ' + scope + ' ORDER BY g.created_at,g.grant_id', parameters
            ):
                runtime = unpack(grant['runtime_json'])
                if (not isinstance(runtime, dict) or runtime.get('runtime_id') != grant['runtime_id']
                        or not isinstance(runtime.get('model'), str) or not runtime['model']
                        or not isinstance(runtime.get('reasoning_effort'), str) or not runtime['reasoning_effort']):
                    raise ReviewConflictError('reviewer recovery requires an intact runtime binding')
                identifier(grant['grant_id'], 'grant_id')
                identifier(grant['runtime_id'], 'runtime_id')
                launch = self._launch_receipt(unpack(grant['launch_json'])) if grant['launch_json'] else None
                workers.append({'grant_id': grant['grant_id'], 'review_id': grant['review_id'],
                                'runtime_id': grant['runtime_id'],
                                'container_id': launch['container_id'] if launch else None})
            selected = ('SELECT g.grant_id FROM job_review_grants g '
                        'JOIN job_reviews r USING(review_id) WHERE ' + scope)
            revoked = con.execute('UPDATE job_review_grants SET revoked_at=? WHERE revoked_at IS NULL '
                                  'AND grant_id IN (' + selected + ')', (self._now(), *parameters)).rowcount
            # One pass over claims, not a full review scan for every historical grant.
            released = con.execute(
                'UPDATE job_review_items SET claim_owner=NULL,claim_until=NULL '
                'WHERE claim_owner IN (SELECT g.actor FROM job_review_grants g '
                'JOIN job_reviews r USING(review_id) WHERE ' + scope + ')', parameters
            ).rowcount
            return {'revoked_grants': revoked, 'released_claims': released, 'workers': workers}

    def _revoke_review_grants(self, con, review_id):
        for row in con.execute('SELECT grant_id FROM job_review_grants WHERE review_id=? AND revoked_at IS NULL',
                               (review_id,)).fetchall():
            self._revoke(con, row[0])

    def _revoke_item_grants(self, con, review_id, ordinal):
        for row in con.execute('SELECT g.grant_id FROM job_review_grants g JOIN job_review_grant_items i USING(grant_id) '
                               'WHERE g.review_id=? AND i.ordinal=? AND g.revoked_at IS NULL',
                               (review_id, ordinal)).fetchall():
            self._revoke(con, row[0])

    def scoped_call(self, token, operation, args=None):
        fields = {'assignment': (), 'context': ('section', 'offset', 'limit'),
                  'job': ('ordinal', 'offset', 'limit'), 'assessment': ('ordinal', 'assessment')}
        if operation not in fields:
            raise ContractError('unknown reviewer operation')
        args = {} if args is None else args
        exact(args, fields[operation])
        args = dict(args)
        if len(canonical_json(args).encode('utf-8')) > 64 * 1024:
            raise ContractError('review request exceeds 64 KiB')
        with self._transaction() as con:
            grant = self._grant(con, token)
            principal = _ReviewPrincipal('reviewer', grant['review_id'], grant['actor'], grant['kind'])
            if operation == 'assessment':
                result = self._assess(con, grant, args, principal)
            else:
                run = self._managed_run(con, grant['review_id'])
                if run['context_sha256'] != grant['context_sha256']:
                    raise ReviewConflictError('review context changed since assignment')
                if operation == 'assignment':
                    result = self._assignment(con, grant, run)
                elif operation == 'context':
                    result = self.service._call_in_transaction(con, 'context',
                        {'review_id': grant['review_id'], **args}, principal=principal)
                    result = {k: result[k] for k in ('rubric_version', 'profile_revision', 'resume_versions',
                              'fingerprint', 'section', 'facts', 'preferences', 'feedback', 'total', 'next_offset') if k in result}
                else:
                    slot = self._slot(con, grant, args.get('ordinal'))
                    item = self._current(con, grant, slot)
                    page = self.service._call_in_transaction(con, 'job', {
                        'review_id': grant['review_id'], 'actor': grant['actor'], 'blind': True, **args,
                    }, principal=principal)
                    result = {k: page[k] for k in ('ordinal', 'revision', 'snapshot_sha256', 'description',
                              'offset', 'next_offset', 'description_chars')}
                    source = unpack(item['snapshot_json'])
                    result['job'] = {k: source[k] for k in REVIEW_JOB_FIELDS if k != 'description' and k in source}
            return bounded(result)

    def _assignment(self, con, grant, run):
        jobs = []
        for slot in con.execute('SELECT * FROM job_review_grant_items WHERE grant_id=? ORDER BY ordinal', (grant['grant_id'],)):
            item = self.service._item(con, {'review_id': grant['review_id'], 'ordinal': slot['ordinal']})
            source = unpack(item['snapshot_json'])
            submitted = con.execute('SELECT 1 FROM job_review_commands WHERE idempotency_key=?',
                                    (_command_key(grant['grant_id'], slot['ordinal']),)).fetchone() is not None
            if not submitted:
                self._current(con, grant, slot)
            jobs.append({'ordinal': slot['ordinal'], 'expected_revision': slot['expected_revision'],
                         'snapshot_sha256': slot['snapshot_sha256'], 'submitted': submitted,
                         'job': {k: source.get(k) for k in REVIEW_SUMMARY_FIELDS}})
        context = unpack(run['context_json'])
        return {'grant_id': grant['grant_id'], 'kind': grant['kind'], 'expires_at': grant['expires_at'],
                'context_fingerprint': context['fingerprint'], 'rubric_version': context.get('rubric_version'), 'jobs': jobs}

    def _assess(self, con, grant, args, principal):
        slot = self._slot(con, grant, args.get('ordinal'))
        request = {'review_id': grant['review_id'], 'actor': grant['actor'], 'kind': grant['kind'],
                   'ordinal': slot['ordinal'], 'expected_revision': slot['expected_revision'],
                   'assessment': args.get('assessment'),
                   'idempotency_key': _command_key(grant['grant_id'], slot['ordinal'])}
        prior = con.execute('SELECT * FROM job_review_commands WHERE idempotency_key=?', (request['idempotency_key'],)).fetchone()
        if prior:
            if prior['operation'] != 'assess' or prior['request_sha256'] != payload_sha256({'action': 'assess', 'args': request}):
                raise ReviewConflictError('assignment submission conflicts with its saved receipt')
            return unpack(prior['response_json'])
        self._current(con, grant, slot)
        if grant['kind'] == 'check':
            item = self.service._item(con, request)
            primary = con.execute('SELECT runtime_id FROM job_review_grants WHERE actor=? AND kind=?',
                                  (item['reviewer'], 'primary')).fetchone()
            if primary is None or primary[0] == grant['runtime_id']:
                raise ReviewConflictError('independent checking requires a separate recorded runtime')
        result = self.service._call_in_transaction(con, 'assess', request, principal=principal)
        # The legacy check path does not clear claims, while primary submission does.
        con.execute('UPDATE job_review_items SET claim_owner=NULL,claim_until=NULL WHERE review_id=? AND ordinal=? AND claim_owner=?',
                    (grant['review_id'], slot['ordinal'], grant['actor']))
        return result

    def _validate_provenance(self, con, review_id):
        """A managed label alone never authorizes publication of untracked results."""
        run = self._managed_run(con, review_id, active=False)
        for item in con.execute('SELECT * FROM job_review_items WHERE review_id=?', (review_id,)).fetchall():
            runtimes = []
            for kind, field, actor_field in (('primary', 'assessment_json', 'reviewer'), ('check', 'check_json', 'checker')):
                if item[field] is None:
                    continue
                grant = con.execute('SELECT g.*,i.expected_revision,i.snapshot_sha256 AS assigned_snapshot '
                    'FROM job_review_grants g JOIN job_review_grant_items i USING(grant_id) '
                    'WHERE g.review_id=? AND g.actor=? AND g.kind=? AND i.ordinal=?',
                    (review_id, item[actor_field], kind, item['ordinal'])).fetchone()
                if (grant is None or grant['assigned_snapshot'] != item['snapshot_sha256']
                        or grant['context_sha256'] != run['context_sha256']):
                    raise ReviewConflictError('managed assessment has no matching isolated runtime provenance')
                runtime = unpack(grant['runtime_json'])
                if (runtime.get('runtime_id') != grant['runtime_id']
                        or not isinstance(runtime.get('model'), str) or not runtime['model']
                        or not isinstance(runtime.get('reasoning_effort'), str) or not runtime['reasoning_effort']):
                    raise ReviewConflictError('managed assessment has invalid runtime configuration provenance')
                if grant['launch_json'] is None:
                    raise ReviewConflictError('managed assessment has no recorded container launch')
                self._launch_receipt(unpack(grant['launch_json']))
                receipt = con.execute('SELECT * FROM job_review_commands WHERE idempotency_key=?',
                                      (_command_key(grant['grant_id'], item['ordinal']),)).fetchone()
                recorded_revision = con.execute('SELECT * FROM job_review_revisions WHERE review_id=? AND ordinal=? AND revision=?',
                    (review_id, item['ordinal'], grant['expected_revision'] + 1)).fetchone()
                if (receipt is None or receipt['operation'] != 'assess' or recorded_revision is None
                        or recorded_revision['actor'] != grant['actor'] or recorded_revision['kind'] != kind
                        or recorded_revision['value_json'] != item[field]
                        or unpack(receipt['response_json']) != {'review_id': review_id, 'ordinal': item['ordinal'],
                                                               'revision': grant['expected_revision'] + 1}):
                    raise ReviewConflictError('managed assessment does not match its durable submission receipt')
                runtimes.append(grant['runtime_id'])
            if len(runtimes) != len(set(runtimes)):
                raise ReviewConflictError('managed review reused the primary runtime for checking')
