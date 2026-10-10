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
from .finalizer import FinalizerAuthorityMixin, ALL_GRANTS, grant_table
from .routing import RoutingAuthorityMixin, MAX_SCREENING_JOBS, MAX_BULK_ITEMS
from .adjudication import AdjudicationAuthorityMixin

MODEL = 'gpt-6-astra'
REASONING_EFFORT = 'high'
LEASE_SECONDS = 30 * 60
MAX_ASSIGNMENT_JOBS = 20
MAX_BULK_REQUEST_BYTES = 256 * 1024


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


class ReviewAuthority(AdjudicationAuthorityMixin, RoutingAuthorityMixin, FinalizerAuthorityMixin):
    def __init__(self, service, *, approved_model=MODEL, approved_reasoning_effort=REASONING_EFFORT,
                 approved_check_model=None, approved_check_reasoning_effort=None,
                 approved_screening_model=None, approved_screening_reasoning_effort=None):
        identifier(approved_model, 'approved_model')
        identifier(approved_reasoning_effort, 'approved_reasoning_effort')
        self.service = service
        self.approved_model = approved_model
        self.approved_reasoning_effort = approved_reasoning_effort
        self.approved_check_model = approved_model if approved_check_model is None else approved_check_model
        self.approved_check_reasoning_effort = (approved_reasoning_effort if approved_check_reasoning_effort is None
                                               else approved_check_reasoning_effort)
        identifier(self.approved_check_model, 'approved_check_model')
        identifier(self.approved_check_reasoning_effort, 'approved_check_reasoning_effort')
        if (approved_screening_model is None) != (approved_screening_reasoning_effort is None):
            raise ContractError('screening model and effort must be configured together')
        if approved_screening_model is not None:
            identifier(approved_screening_model, 'approved_screening_model')
            identifier(approved_screening_reasoning_effort, 'approved_screening_reasoning_effort')
        self.approved_screening_model = approved_screening_model
        self.approved_screening_reasoning_effort = approved_screening_reasoning_effort

    @contextmanager
    def _transaction(self):
        with closing(connect(self.service.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE')
            yield con

    def _now(self):
        return stamp(timestamp(self.service.now()))

    def _managed_run(self, con, review_id, *, active=True):
        run = self.service._run(con, review_id, active)
        if unpack(run['metadata_json']).get('workflow') == 'old-method-v1':
            raise ContractError('OLD METHOD reviews require their dedicated host workflow')
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

    def start_benchmark(self, args):
        """Copy frozen source inputs into a separate coordinator-only benchmark."""
        return self._trusted('benchmark-start', args)

    def status(self, review_id):
        return self._trusted('status', {'review_id': review_id})

    def pending_checks(self, review_id, *, after=0, limit=200):
        """Page the complete trusted audit queue without exposing judgments."""
        after = integer(after, 'after', 0)
        limit = integer(limit, 'limit', 1, 200)
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            rows = con.execute('SELECT ordinal,ats,revision,snapshot_sha256,assessment_json,check_json '
                               'FROM job_review_items WHERE review_id=? ORDER BY ordinal', (review_id,)).fetchall()
            required, _ = self.service._audit_membership(con, run, rows)
            required = set(required)
            pending = [row['ordinal'] for row in rows if row['ordinal'] > after
                       and row['ordinal'] in required and row['check_json'] is None]
            page = pending[:limit]
            return {'ordinals': page, 'next_after': page[-1] if len(pending) > limit else None}

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

    def verify_availability(self, review_id, idempotency_key):
        return self.service._verify_availability({'review_id': review_id, 'idempotency_key': idempotency_key}, principal=_COORDINATOR)

    def issue(self, review_id, ordinals, kind, runtime, *, purpose='detailed'):
        if purpose not in ('detailed', 'screening') or purpose == 'screening' and kind != 'primary':
            raise ContractError('screening purpose is restricted to primary grants')
        if kind == 'adjudicator':
            return self._issue_adjudicator(review_id, ordinals, runtime)
        if kind == 'finalizer':
            if ordinals:
                raise ContractError('finalizer scope is the complete selected review, not a caller-selected subset')
            return self._issue_finalizer(review_id, runtime)
        """Issue a credential once; runtime identity is supplied by the launcher."""
        identifier(review_id, 'review_id')
        if kind not in ('primary', 'check'):
            raise ContractError('kind must be primary or check')
        maximum = MAX_SCREENING_JOBS if purpose == 'screening' else MAX_ASSIGNMENT_JOBS
        if not isinstance(ordinals, (list, tuple)) or not 1 <= len(ordinals) <= maximum:
            raise ContractError('assignment exceeds the purpose-specific job limit')
        ordinals = [integer(value, 'ordinal', 1) for value in ordinals]
        if len(set(ordinals)) != len(ordinals):
            raise ContractError('assignment ordinals must be unique')
        exact(runtime, ('runtime_id', 'model', 'reasoning_effort'), ('runtime_id', 'model', 'reasoning_effort'))
        identifier(runtime['runtime_id'], 'runtime_id')
        profile = ({'model': self.approved_check_model, 'reasoning_effort': self.approved_check_reasoning_effort}
                   if kind == 'check' else {'model': self.approved_model, 'reasoning_effort': self.approved_reasoning_effort}
                   if purpose == 'detailed' else {'model': self.approved_screening_model,
                   'reasoning_effort': self.approved_screening_reasoning_effort})
        if any(runtime[key] != value for key, value in profile.items()):
            raise ContractError('reviewer model and reasoning must match trusted runtime configuration')
        token = secrets.token_urlsafe(32)
        grant_id = 'grant_' + secrets.token_hex(16)
        actor = 'isolated_' + secrets.token_hex(16)
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            policy = self._ensure_execution_policy(con, review_id)
            if purpose == 'screening' and policy['screening'] is None:
                raise ReviewConflictError('review execution policy has no screening phase')
            now = self._now()
            expires = stamp(timestamp(now) + timedelta(seconds=LEASE_SECONDS))
            if con.execute('SELECT 1 FROM ' + ALL_GRANTS + ' WHERE runtime_id=?', (runtime['runtime_id'],)).fetchone():
                raise ReviewConflictError('runtime identity has already been assigned')
            items = []
            for ordinal in sorted(ordinals):
                item = self.service._item(con, {'review_id': review_id, 'ordinal': ordinal})
                if kind == 'primary' and item['assessment_json'] is not None:
                    raise ReviewConflictError('primary assignment requires an unassessed job')
                route = self._route_for_item(con, run, item)
                if purpose == 'screening' and route is not None:
                    raise ReviewConflictError('posting has already been screened')
                if kind == 'primary' and purpose == 'detailed' and policy['screening'] is not None and (
                        route is None or route['route'] != 'detailed'):
                    raise ReviewConflictError('posting must be routed before detailed review')
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
                            '(grant_id,token_sha256,review_id,actor,kind,context_sha256,runtime_id,runtime_json,created_at,expires_at,purpose) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (grant_id, _token_hash(token), review_id, actor, kind, run['context_sha256'],
                             runtime['runtime_id'], canonical_json(runtime), now, expires, purpose))
            except sqlite3.IntegrityError:
                raise ReviewConflictError('runtime identity has already been assigned') from None
            for item in items:
                con.execute('INSERT INTO job_review_grant_items VALUES(?,?,?,?)',
                            (grant_id, item['ordinal'], item['revision'], item['snapshot_sha256']))
                con.execute('UPDATE job_review_items SET claim_owner=?,claim_until=? WHERE review_id=? AND ordinal=?',
                            (actor, expires, review_id, item['ordinal']))
            stored = dict(con.execute('SELECT * FROM job_review_grants WHERE grant_id=?', (grant_id,)).fetchone())
            bounded(self._assignment(con, stored, run))
            return {'grant_id': grant_id, 'token': token, 'actor': actor, 'expires_at': expires,
                    'kind': kind, 'purpose': purpose, 'rubric_version': unpack(run['context_json']).get('rubric_version', 'job-review-v1')}

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
            grant = con.execute('SELECT * FROM ' + ALL_GRANTS + ' WHERE grant_id=?', (grant_id,)).fetchone()
            if (grant is None or grant['runtime_id'] != runtime_id or grant['revoked_at']
                    or grant['expires_at'] <= self._now()):
                raise ReviewAuthorizationError('reviewer launch cannot be recorded')
            self._managed_run(con, grant['review_id'])
            encoded = canonical_json(receipt)
            if grant['launch_json'] is not None and grant['launch_json'] != encoded:
                raise ReviewConflictError('reviewer launch identity is immutable')
            if con.execute('SELECT 1 FROM ' + ALL_GRANTS + ' WHERE grant_id<>? AND '
                           "json_extract(launch_json,'$.container_id')=?", (grant_id, receipt['container_id'])).fetchone():
                raise ReviewConflictError('a container cannot run multiple reviewer assignments')
            con.execute('UPDATE ' + grant_table(grant) + ' SET launch_json=? WHERE grant_id=?', (encoded, grant_id))
            return {'grant_id': grant_id, 'runtime_id': runtime_id, 'launched': True}

    def _grant(self, con, token):
        grant = con.execute('SELECT * FROM ' + ALL_GRANTS + ' WHERE token_sha256=?', (_token_hash(token),)).fetchone()
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
            row = con.execute('SELECT * FROM ' + ALL_GRANTS + ' WHERE grant_id=?', (grant_id,)).fetchone()
            if row is None or row['revoked_at'] or row['expires_at'] <= self._now():
                raise ReviewAuthorizationError('reviewer lease cannot be renewed')
            grant = dict(row)
            self._managed_run(con, grant['review_id'])
            if grant['kind'] == 'finalizer':
                self._finalizer_current(con, grant)
            elif grant['kind'] == 'adjudicator':
                self._adjudicator_current(con, grant)
            pending = []
            for slot in con.execute('SELECT * FROM job_review_grant_items WHERE grant_id=? ORDER BY ordinal', (grant_id,)).fetchall():
                if con.execute('SELECT 1 FROM job_review_commands WHERE idempotency_key=?',
                               (_command_key(grant_id, slot['ordinal']),)).fetchone():
                    continue
                pending.append(self._current(con, grant, slot))
            expires = stamp(timestamp(self._now()) + timedelta(seconds=LEASE_SECONDS))
            con.execute('UPDATE ' + grant_table(grant) + ' SET expires_at=? WHERE grant_id=?', (expires, grant_id))
            for item in pending:
                con.execute('UPDATE job_review_items SET claim_until=? WHERE review_id=? AND ordinal=? AND claim_owner=?',
                            (expires, grant['review_id'], item['ordinal'], grant['actor']))
            return {'grant_id': grant_id, 'expires_at': expires, 'pending_count': len(pending)}

    def _revoke(self, con, grant_id):
        row = con.execute('SELECT * FROM ' + ALL_GRANTS + ' WHERE grant_id=?', (grant_id,)).fetchone()
        if row is None:
            raise ContractError('reviewer assignment was not found')
        when = row['revoked_at'] or self._now()
        con.execute('UPDATE ' + grant_table(row) + ' SET revoked_at=? WHERE grant_id=?', (when, grant_id))
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
                'SELECT g.* FROM ' + ALL_GRANTS + ' g JOIN job_reviews r USING(review_id) '
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
            selected = ('SELECT g.grant_id FROM ' + ALL_GRANTS + ' g '
                        'JOIN job_reviews r USING(review_id) WHERE ' + scope)
            revoked = 0
            for table in ('job_review_grants', 'job_review_finalizer_grants', 'job_review_adjudicator_grants'):
                revoked += con.execute('UPDATE ' + table + ' SET revoked_at=? WHERE revoked_at IS NULL '
                                      'AND grant_id IN (' + selected + ')', (self._now(), *parameters)).rowcount
            # One pass over claims, not a full review scan for every historical grant.
            released = con.execute(
                'UPDATE job_review_items SET claim_owner=NULL,claim_until=NULL '
                'WHERE claim_owner IN (SELECT g.actor FROM job_review_grants g '
                'JOIN job_reviews r USING(review_id) WHERE ' + scope + ')', parameters
            ).rowcount
            return {'revoked_grants': revoked, 'released_claims': released, 'workers': workers}

    def _revoke_review_grants(self, con, review_id):
        for row in con.execute('SELECT grant_id FROM ' + ALL_GRANTS + ' WHERE review_id=? AND revoked_at IS NULL',
                               (review_id,)).fetchall():
            self._revoke(con, row[0])

    def _revoke_item_grants(self, con, review_id, ordinal):
        for row in con.execute('SELECT g.grant_id FROM job_review_grants g JOIN job_review_grant_items i USING(grant_id) '
                               'WHERE g.review_id=? AND i.ordinal=? AND g.revoked_at IS NULL',
                               (review_id, ordinal)).fetchall():
            self._revoke(con, row[0])

    def scoped_call(self, token, operation, args=None):
        fields = {'assignment': (), 'disagreement': ('ordinal', 'offset', 'limit'), 'resolutions': ('resolutions',), 'context': ('section', 'offset', 'limit'),
                  'job': ('ordinal', 'offset', 'limit'), 'assessment': ('ordinal', 'assessment'),
                  'assessments': ('assessments',), 'calibrations': ('calibrations',), 'routes': ('routes',),
                  'calibration': ('after', 'limit'), 'calibrate': ('ordinal', 'position', 'related_group'),
                  'order': ('ordinals', 'groups'), 'finalize': ()}
        if operation not in fields:
            raise ContractError('unknown reviewer operation')
        args = {} if args is None else args
        exact(args, fields[operation])
        args = dict(args)
        maximum = MAX_BULK_REQUEST_BYTES if operation in ('assessments', 'calibrations', 'routes', 'resolutions') else 64 * 1024
        if len(canonical_json(args).encode('utf-8')) > maximum:
            raise ContractError('review request exceeds its size limit')
        with self._transaction() as con:
            grant = self._grant(con, token)
            if grant['kind'] == 'adjudicator':
                return bounded(self._adjudicator_call(con, grant, operation, args))
            if operation in ('disagreement', 'resolutions'):
                raise ReviewAuthorizationError('adjudication requires its own scoped grant')
            if grant['purpose'] == 'screening' and operation not in ('assignment', 'context', 'job', 'routes'):
                raise ReviewAuthorizationError('screening cannot submit detailed judgments')
            if operation == 'routes' and (grant['purpose'] != 'screening' or grant['kind'] != 'primary'):
                raise ReviewAuthorizationError('routing is restricted to screening assignments')
            if operation in ('assessments', 'calibrations', 'routes'):
                return bounded(self._bulk(con, grant, operation, args))
            if operation == 'order':
                from .ordering import submit_order
                return bounded(submit_order(self, con, grant, args))
            if grant['kind'] == 'finalizer':
                return bounded(self._finalizer_call(con, grant, operation, args))
            if operation in ('calibration', 'calibrate', 'finalize'):
                raise ReviewAuthorizationError('calibration is outside this reviewer assignment')
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
                              'fingerprint', 'search_brief', 'source_inventory', 'section', 'facts', 'preferences', 'feedback', 'total', 'next_offset') if k in result}
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

    def _bulk(self, con, grant, operation, args):
        """Commit valid items independently, but fail the whole call on lost authority."""
        finalizer = operation == 'calibrations'
        if (grant['kind'] == 'finalizer') != finalizer:
            raise ReviewAuthorizationError('operation is outside this reviewer assignment')
        entries = args.get(operation)
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BULK_ITEMS:
            raise ContractError('bulk submission requires one to twenty items')
        ordinals = []
        for entry in entries:
            allowed = (('ordinal', 'position', 'related_group') if finalizer else
                       ('ordinal', 'route', 'reason_code', 'explanation', 'evidence', 'brief_revision', 'family', 'alignment')
                       if operation == 'routes' else ('ordinal', 'assessment'))
            required = ('ordinal', 'position') if finalizer else ('ordinal', 'route') if operation == 'routes' else allowed
            exact(entry, allowed, required)
            ordinals.append(integer(entry['ordinal'], 'ordinal', 1))
        if len(set(ordinals)) != len(ordinals):
            raise ContractError('bulk submission ordinals must be unique')
        # Preflight every scope before writing. Already committed items retain the
        # single-item replay behavior even though their lease has been cleared.
        if finalizer:
            calibration = self._finalizer_batch_basis(con, grant)
            selected = calibration[4]
            if any(ordinal not in selected for ordinal in ordinals):
                raise ReviewAuthorizationError('job is outside finalizer selected membership')
        else:
            run = self._managed_run(con, grant['review_id'])
            if run['context_sha256'] != grant['context_sha256']:
                raise ReviewConflictError('review context changed since assignment')
            for ordinal in ordinals:
                slot = self._slot(con, grant, ordinal)
                prior = con.execute('SELECT 1 FROM job_review_commands WHERE idempotency_key=?',
                                    (_command_key(grant['grant_id'], ordinal),)).fetchone()
                if not prior:
                    self._current(con, grant, slot)
        principal = _ReviewPrincipal('reviewer', grant['review_id'], grant['actor'], grant['kind'])
        results = []
        for entry in entries:
            con.execute('SAVEPOINT review_bulk_item')
            try:
                receipt = (self._finalizer_call(con, grant, 'calibrate', entry, _calibration=calibration) if finalizer else
                           self._route_one(con, grant, entry) if operation == 'routes' else
                           self._assess(con, grant, entry, principal))
            except (ReviewAuthorizationError, ReviewConflictError):
                raise
            except ContractError:
                con.execute('ROLLBACK TO review_bulk_item')
                results.append({'ordinal': entry['ordinal'], 'status': 'error', 'error': 'validation_failed'})
            else:
                results.append({'ordinal': entry['ordinal'], 'status': 'saved', 'receipt': receipt})
            finally:
                con.execute('RELEASE review_bulk_item')
        return {'results': results}

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
        return {'grant_id': grant['grant_id'], 'kind': grant['kind'], 'purpose': grant['purpose'], 'expires_at': grant['expires_at'],
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

    def _validate_provenance(self, con, review_id, *, ordinals=None):
        """A managed label alone never authorizes publication of untracked results."""
        run = self._managed_run(con, review_id, active=False)
        self._validate_finalizer_provenance(con, run)
        scope = '' if ordinals is None else ' AND ordinal IN (' + ','.join('?' for _ in ordinals) + ')'
        parameters = (review_id,) if ordinals is None else (review_id, *ordinals)
        for item in con.execute('SELECT * FROM job_review_items WHERE review_id=?' + scope, parameters).fetchall():
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
                if grant['purpose'] == 'screening':
                    route = con.execute('SELECT * FROM job_review_routes WHERE grant_id=? AND ordinal=?',
                                        (grant['grant_id'], item['ordinal'])).fetchone()
                    if (route is None or route['route'] != 'exclude' or route['expected_revision'] != grant['expected_revision']
                            or route['snapshot_sha256'] != item['snapshot_sha256'] or route['context_sha256'] != run['context_sha256']
                            or route['actor'] != grant['actor']):
                        raise ReviewConflictError('screening exclusion has no matching immutable route')
                    entry = unpack(route['value_json'])
                    expected = self._route_assessment(con, grant, item, entry)
                    if (route['request_sha256'] != payload_sha256(entry) or canonical_json(expected) != item[field]
                            or unpack(route['response_json']) != dict(unpack(receipt['response_json']), route='exclude')):
                        raise ReviewConflictError('screening exclusion differs from its validated routing receipt')
                runtimes.append(grant['runtime_id'])
            if len(runtimes) != len(set(runtimes)):
                raise ReviewConflictError('managed review reused the primary runtime for checking')
