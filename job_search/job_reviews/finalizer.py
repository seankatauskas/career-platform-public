"""Separate scope for editorial calibration after independent judgments agree."""
from __future__ import annotations

import secrets
from datetime import timedelta
from ..contracts import ContractError, canonical_json, payload_sha256
from .contracts import RUBRIC_VERSION, bounded, exact, identifier, stamp, timestamp
from .service import _ReviewPrincipal, unpack

_GRANT_FIELDS = ('grant_id', 'token_sha256', 'review_id', 'actor', 'kind', 'context_sha256',
                 'runtime_id', 'runtime_json', 'launch_json', 'created_at', 'expires_at', 'revoked_at')
ALL_GRANTS = ('(SELECT ' + ','.join(_GRANT_FIELDS) + ',NULL AS basis_sha256,purpose FROM job_review_grants UNION ALL SELECT '
              + ','.join(_GRANT_FIELDS) + ",basis_sha256,'detailed' AS purpose FROM job_review_finalizer_grants UNION ALL SELECT "
              + ','.join(_GRANT_FIELDS) + ",NULL AS basis_sha256,'detailed' AS purpose FROM job_review_adjudicator_grants)")


def grant_table(grant):
    return {'finalizer': 'job_review_finalizer_grants', 'adjudicator': 'job_review_adjudicator_grants'}.get(grant['kind'], 'job_review_grants')


class FinalizerAuthorityMixin:
    def _issue_finalizer(self, review_id, runtime):
        from .authority import LEASE_SECONDS, ReviewConflictError, _token_hash
        exact(runtime, ('runtime_id', 'model', 'reasoning_effort'), ('runtime_id', 'model', 'reasoning_effort'))
        identifier(runtime['runtime_id'], 'runtime_id')
        if runtime['model'] != self.approved_model or runtime['reasoning_effort'] != self.approved_reasoning_effort:
            raise ContractError('finalizer model and reasoning must match trusted runtime configuration')
        token = secrets.token_urlsafe(32)
        grant_id, actor = 'finalizer_' + secrets.token_hex(16), 'isolated_' + secrets.token_hex(16)
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            self._ensure_execution_policy(con, review_id)
            state = self.service._calibration_state(con, run)
            if state is None:
                raise ContractError('finalization requires a v2 review')
            self.service._check_calibration_basis(con, {'review_id': review_id, 'basis_sha256': state['basis_sha256']})
            if state['complete']:
                raise ReviewConflictError('review calibration is already complete')
            if con.execute('SELECT 1 FROM job_review_finalizer_grants WHERE review_id=? AND revoked_at IS NULL AND expires_at>?',
                           (review_id, self._now())).fetchone():
                raise ReviewConflictError('review already has an active finalizer')
            if con.execute('SELECT 1 FROM ' + ALL_GRANTS + ' WHERE runtime_id=?', (runtime['runtime_id'],)).fetchone():
                raise ReviewConflictError('runtime identity has already been assigned')
            expires = stamp(timestamp(self._now()) + timedelta(seconds=LEASE_SECONDS))
            con.execute('INSERT INTO job_review_finalizer_grants '
                        '(grant_id,token_sha256,review_id,actor,kind,context_sha256,basis_sha256,runtime_id,runtime_json,created_at,expires_at) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (grant_id, _token_hash(token), review_id, actor, 'finalizer', run['context_sha256'], state['basis_sha256'],
                         runtime['runtime_id'], canonical_json(runtime), self._now(), expires))
            return {'grant_id': grant_id, 'token': token, 'actor': actor, 'expires_at': expires,
                    'kind': 'finalizer', 'purpose': 'detailed', 'rubric_version': RUBRIC_VERSION}

    def _finalizer_current(self, con, grant):
        from .authority import ReviewConflictError
        run = self._managed_run(con, grant['review_id'])
        if run['context_sha256'] != grant['context_sha256'] or self.service._calibration_basis(con, run)[0] != grant['basis_sha256']:
            raise ReviewConflictError('review changed since finalizer assignment')
        return run

    def _finalizer_batch_basis(self, con, grant):
        """Validate once while the caller holds its write transaction.

        The returned value lives only in one bulk call. Its items can change
        presentation entries, never source, judgments, context or membership.
        """
        from .authority import ReviewConflictError
        run = self._managed_run(con, grant['review_id'])
        if run['context_sha256'] != grant['context_sha256']:
            raise ReviewConflictError('review changed since finalizer assignment')
        try:
            state = self.service._check_calibration_basis(con, {
                'review_id': grant['review_id'], 'basis_sha256': grant['basis_sha256']})
        except ContractError:
            raise ReviewConflictError('review changed since finalizer assignment') from None
        return (con, grant['actor'], *state)

    def _finalizer_call(self, con, grant, operation, args, *, _calibration=None):
        from .authority import ReviewAuthorizationError
        if operation not in ('assignment', 'context', 'calibration', 'calibrate', 'finalize'):
            raise ReviewAuthorizationError('operation is outside the finalizer assignment')
        if _calibration is None:
            run = self._finalizer_current(con, grant)
        else:
            connection, actor, run, basis, _ = _calibration
            if (operation != 'calibrate' or connection is not con or not con.in_transaction
                    or actor != grant['actor'] or run['review_id'] != grant['review_id'] or basis != grant['basis_sha256']
                    or run['context_sha256'] != grant['context_sha256']):
                raise ReviewAuthorizationError('invalid finalizer batch scope')
        principal = _ReviewPrincipal('finalizer', grant['review_id'], grant['actor'], 'finalizer')
        if operation == 'assignment':
            context = unpack(run['context_json'])
            return {'grant_id': grant['grant_id'], 'kind': 'finalizer', 'purpose': 'detailed', 'expires_at': grant['expires_at'],
                    'context_fingerprint': context['fingerprint'], 'rubric_version': RUBRIC_VERSION,
                    'basis_sha256': grant['basis_sha256'], 'jobs': [],
                    'calibration': self.service._calibration_state(con, run)}
        if operation in ('calibrate', 'finalize'):
            # Worker controls presentation positions/groups only. The authority binds
            # evidence digest, identity, command identity, and selected membership.
            suffix = str(args['ordinal']) if operation == 'calibrate' else 'finalize'
            request = dict(args, review_id=grant['review_id'], actor=grant['actor'],
                           basis_sha256=grant['basis_sha256'], idempotency_key='grant:' + grant['grant_id'] + ':' + suffix)
            if operation == 'calibrate':
                request.setdefault('related_group', None)
        else:
            request = dict(args, review_id=grant['review_id'])
        return self.service._call_in_transaction(con, operation, request, principal=principal,
                                                 _calibration=_calibration)

    def _validate_finalizer_provenance(self, con, run):
        from .authority import ReviewConflictError
        state = self.service._calibration_state(con, run)
        if not state or not state['complete']:
            return
        seal = con.execute('SELECT * FROM job_review_calibrations WHERE review_id=? AND basis_sha256=?',
                           (run['review_id'], state['basis_sha256'])).fetchone()
        entries = con.execute('SELECT * FROM job_review_calibration_entries WHERE review_id=? AND basis_sha256=? ORDER BY position,ordinal',
                              (run['review_id'], state['basis_sha256'])).fetchall()
        artifact = [{k: entry[k] for k in ('ordinal', 'position', 'related_group_json')} for entry in entries]
        if unpack(seal['artifact_json']) != artifact:
            raise ReviewConflictError('final calibration does not match its sealed artifact')
        for operation, record in [('finalize', seal), *[('calibrate', e) for e in entries]]:
            grant = con.execute('SELECT * FROM job_review_finalizer_grants WHERE review_id=? AND actor=?',
                                (run['review_id'], record['actor'])).fetchone()
            if (grant is None or grant['basis_sha256'] != state['basis_sha256'] or grant['context_sha256'] != run['context_sha256']
                    or grant['launch_json'] is None):
                raise ReviewConflictError('final calibration has no matching isolated provenance')
            self._launch_receipt(unpack(grant['launch_json']))
            runtime = unpack(grant['runtime_json'])
            if runtime.get('runtime_id') != grant['runtime_id'] or not runtime.get('model') or not runtime.get('reasoning_effort'):
                raise ReviewConflictError('finalizer runtime provenance is invalid')
            suffix = str(record['ordinal']) if operation == 'calibrate' else 'finalize'
            key = 'grant:' + grant['grant_id'] + ':' + suffix
            request = {'review_id': run['review_id'], 'actor': grant['actor'], 'basis_sha256': state['basis_sha256'], 'idempotency_key': key}
            if operation == 'calibrate':
                request.update(ordinal=record['ordinal'], position=record['position'], related_group=unpack(record['related_group_json']))
            receipt = con.execute('SELECT * FROM job_review_commands WHERE idempotency_key=?', (key,)).fetchone()
            if receipt is None or receipt['operation'] != operation or receipt['request_sha256'] != payload_sha256({'action': operation, 'args': request}):
                raise ReviewConflictError('final calibration has no matching immutable submission receipt')
