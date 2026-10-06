"""Current-evidence adjudication without rewriting either independent judgment."""
from __future__ import annotations

import json
import secrets
from datetime import timedelta
from ..contracts import ContractError, canonical_json, payload_sha256
from .contracts import (RUBRIC_VERSION, SELECTED, REVIEW_JOB_FIELDS, REVIEW_SUMMARY_FIELDS,
                        exact, integer, identifier, text, stamp, timestamp, bounded, MAX_RESPONSE_BYTES)


def differences(primary, check, v2=True):
    if not primary or not check:
        return []
    fields = ['decision', 'alignment', 'duplicate_of']
    if v2 and (primary.get('decision') in SELECTED or check.get('decision') in SELECTED):
        fields += ['eligibility', 'next_step', 'category']
    return [key for key in fields if primary.get(key) != check.get(key)]


def resolution_basis(run, item):
    return payload_sha256({'review_id': run['review_id'], 'ordinal': item['ordinal'],
        'context_sha256': run['context_sha256'], 'rubric_version': json.loads(run['context_json']).get('rubric_version'),
        'revision': item['revision'], 'snapshot_sha256': item['snapshot_sha256'],
        'primary': item['assessment_json'], 'check': item['check_json']})


def current_resolution(con, run, item):
    basis = resolution_basis(run, item)
    row = con.execute('SELECT * FROM job_review_resolutions WHERE review_id=? AND ordinal=? AND basis_sha256=?',
                      (run['review_id'], item['ordinal'], basis)).fetchone()
    if row is None:
        return None
    grant = con.execute('SELECT g.*,i.basis_sha256 AS item_basis FROM job_review_adjudicator_grants g '
        'JOIN job_review_adjudicator_items i USING(grant_id) WHERE g.grant_id=? AND i.ordinal=?',
        (row['grant_id'], item['ordinal'])).fetchone()
    value = json.loads(row['request_json'])
    if (grant is None or not grant['launch_json'] or grant['review_id'] != run['review_id']
            or grant['actor'] != row['actor'] or grant['context_sha256'] != run['context_sha256']
            or grant['item_basis'] != basis or payload_sha256(value) != row['request_sha256']
            or value.get('choice') != row['choice'] or value.get('basis_sha256') != basis
            or value.get('ordinal') != item['ordinal']):
        raise ContractError('adjudication provenance is invalid')
    from .authority import ReviewAuthority
    ReviewAuthority._launch_receipt(json.loads(grant['launch_json']))
    receipt = con.execute('SELECT * FROM job_review_commands WHERE idempotency_key=?',
                          ('grant:' + row['grant_id'] + ':' + str(item['ordinal']),)).fetchone()
    expected = {'review_id': run['review_id'], 'ordinal': item['ordinal'], 'basis_sha256': basis, 'choice': row['choice']}
    if (receipt is None or receipt['operation'] != 'resolve' or receipt['request_sha256'] != row['request_sha256']
            or json.loads(receipt['response_json']) != expected):
        raise ContractError('adjudication submission receipt is invalid')
    return dict(row)


def effective_item(con, run, item):
    return _effective_item(item, current_resolution(con, run, item))


def _effective_item(item, resolution):
    result = dict(item)
    if resolution and resolution['choice'] in ('primary', 'check'):
        result['assessment_json'] = item['assessment_json' if resolution['choice'] == 'primary' else 'check_json']
    return result


def resolved_items(con, run, items):
    """Resolve one transaction's rows, validating only actual resolution records.

    Absence is checked afresh on this connection. Historical/stale records still
    go through current_resolution; no decision or provenance survives this call.
    """
    recorded = {row[0] for row in con.execute(
        'SELECT DISTINCT ordinal FROM job_review_resolutions WHERE review_id=?', (run['review_id'],))}
    resolutions = [current_resolution(con, run, item) if item['ordinal'] in recorded else None
                   for item in items]
    return resolutions, [_effective_item(item, resolution) for item, resolution in zip(items, resolutions)]


class AdjudicationAuthorityMixin:
    def pending_adjudications(self, review_id):
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            if json.loads(run['context_json']).get('rubric_version') != RUBRIC_VERSION:
                return []
            result = []
            for item in con.execute('SELECT * FROM job_review_items WHERE review_id=? ORDER BY ordinal', (review_id,)):
                if not differences(json.loads(item['assessment_json'] or 'null'), json.loads(item['check_json'] or 'null')):
                    continue
                basis = resolution_basis(run, item)
                if not con.execute('SELECT 1 FROM job_review_adjudicator_items WHERE review_id=? AND ordinal=? AND basis_sha256=?',
                                   (review_id, item['ordinal'], basis)).fetchone():
                    result.append(item['ordinal'])
            return result

    def _issue_adjudicator(self, review_id, ordinals, runtime):
        from .authority import LEASE_SECONDS, ReviewConflictError, _token_hash
        from .finalizer import ALL_GRANTS
        exact(runtime, ('runtime_id', 'model', 'reasoning_effort'), ('runtime_id', 'model', 'reasoning_effort'))
        identifier(runtime['runtime_id'], 'runtime_id')
        if not isinstance(ordinals, (list, tuple)) or not 1 <= len(ordinals) <= 20:
            raise ContractError('adjudication requires one to twenty ordinals')
        ordinals = [integer(n, 'ordinal', 1) for n in ordinals]
        if len(set(ordinals)) != len(ordinals):
            raise ContractError('adjudication ordinals must be unique')
        token, gid, actor = secrets.token_urlsafe(32), 'adjudicator_' + secrets.token_hex(16), 'isolated_' + secrets.token_hex(16)
        with self._transaction() as con:
            run = self._managed_run(con, review_id)
            policy = self._ensure_execution_policy(con, review_id)
            if any(runtime[key] != value for key, value in policy['check'].items()):
                raise ContractError('adjudicator requires the frozen checker profile')
            if json.loads(run['context_json']).get('rubric_version') != RUBRIC_VERSION:
                raise ContractError('adjudication requires v2')
            if con.execute('SELECT 1 FROM ' + ALL_GRANTS + ' WHERE runtime_id=?', (runtime['runtime_id'],)).fetchone():
                raise ReviewConflictError('runtime identity has already been assigned')
            items = []
            for n in ordinals:
                item = self.service._item(con, {'review_id': review_id, 'ordinal': n})
                if not differences(json.loads(item['assessment_json'] or 'null'), json.loads(item['check_json'] or 'null')):
                    raise ReviewConflictError('adjudication requires two differing current judgments')
                basis = resolution_basis(run, item)
                if con.execute('SELECT 1 FROM job_review_adjudicator_items WHERE review_id=? AND ordinal=? AND basis_sha256=?',
                               (review_id, n, basis)).fetchone():
                    raise ReviewConflictError('adjudication was already attempted for this basis')
                items.append((n, basis))
            self._validate_provenance(con, review_id, ordinals=ordinals)
            now = self._now(); expires = stamp(timestamp(now) + timedelta(seconds=LEASE_SECONDS))
            con.execute('INSERT INTO job_review_adjudicator_grants '
                '(grant_id,token_sha256,review_id,actor,kind,context_sha256,runtime_id,runtime_json,created_at,expires_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)', (gid, _token_hash(token), review_id, actor, 'adjudicator', run['context_sha256'],
                                              runtime['runtime_id'], canonical_json(runtime), now, expires))
            con.executemany('INSERT INTO job_review_adjudicator_items(grant_id,review_id,ordinal,basis_sha256) VALUES(?,?,?,?)',
                            [(gid, review_id, n, basis) for n, basis in items])
            return {'grant_id': gid, 'token': token, 'actor': actor, 'expires_at': expires,
                    'kind': 'adjudicator', 'purpose': 'detailed', 'rubric_version': RUBRIC_VERSION}

    def _adjudicator_current(self, con, grant, ordinal=None):
        from .authority import ReviewAuthorizationError, ReviewConflictError
        run = self._managed_run(con, grant['review_id'])
        if run['context_sha256'] != grant['context_sha256']:
            raise ReviewConflictError('adjudication context changed')
        slots = con.execute('SELECT * FROM job_review_adjudicator_items WHERE grant_id=? ORDER BY ordinal', (grant['grant_id'],)).fetchall()
        items = {}
        for slot in slots:
            item = self.service._item(con, {'review_id': grant['review_id'], 'ordinal': slot['ordinal']})
            if resolution_basis(run, item) != slot['basis_sha256']:
                raise ReviewConflictError('adjudication evidence changed')
            items[slot['ordinal']] = item
        if ordinal is not None and ordinal not in items:
            raise ReviewAuthorizationError('job is outside adjudication assignment')
        return run, items

    def _adjudicator_call(self, con, grant, operation, args):
        from .authority import ReviewAuthorizationError
        from .service import _ReviewPrincipal
        if operation not in ('assignment', 'context', 'job', 'disagreement', 'resolutions'):
            raise ReviewAuthorizationError('operation is outside adjudication assignment')
        run, items = self._adjudicator_current(con, grant, args.get('ordinal'))
        if operation == 'assignment':
            context = json.loads(run['context_json'])
            return {'grant_id': grant['grant_id'], 'kind': 'adjudicator', 'purpose': 'detailed',
                    'expires_at': grant['expires_at'], 'rubric_version': RUBRIC_VERSION,
                    'context_fingerprint': context['fingerprint'], 'jobs': [
                        {'ordinal': n, 'expected_revision': r['revision'], 'snapshot_sha256': r['snapshot_sha256'],
                         'submitted': current_resolution(con, run, r) is not None,
                         'job': {k: json.loads(r['snapshot_json']).get(k) for k in REVIEW_SUMMARY_FIELDS}}
                        for n, r in items.items()]}
        principal = _ReviewPrincipal('reviewer', run['review_id'], grant['actor'], 'adjudicator')
        if operation == 'context':
            value = self.service._call_in_transaction(con, 'context', dict(args, review_id=run['review_id']), principal=principal)
            section, offset = value['section'], args.get('offset', 0)
            prior = con.execute('SELECT through_offset FROM job_review_adjudicator_reads WHERE grant_id=? AND section=?',
                                (grant['grant_id'], section)).fetchone()
            if offset <= (prior[0] if prior else 0):
                con.execute('INSERT INTO job_review_adjudicator_reads VALUES(?,?,?) ON CONFLICT DO UPDATE SET through_offset=MAX(through_offset,excluded.through_offset)',
                            (grant['grant_id'], section, offset + len(value[section])))
            return {k: value[k] for k in ('rubric_version', 'profile_revision', 'resume_versions', 'fingerprint',
                    'search_brief', 'source_inventory', 'section', 'facts', 'preferences', 'feedback', 'total', 'next_offset') if k in value}
        if operation == 'job':
            value = self.service._call_in_transaction(con, 'job', dict(args, review_id=run['review_id'], actor=grant['actor'], blind=True), principal=principal)
            result = {k: value[k] for k in ('ordinal', 'revision', 'snapshot_sha256', 'description', 'offset', 'next_offset', 'description_chars')}
            source = json.loads(items[args['ordinal']]['snapshot_json'])
            result['job'] = {k: source[k] for k in REVIEW_JOB_FIELDS if k != 'description' and k in source}
            return result
        if operation == 'disagreement':
            item = items[args['ordinal']]
            primary, check = json.loads(item['assessment_json']), json.loads(item['check_json'])
            value = {'ordinal': item['ordinal'], 'basis_sha256': resolution_basis(run, item),
                'primary': primary, 'check': check, 'differing_dimensions': differences(primary, check)}
            serialized = canonical_json(value)
            offset = integer(args.get('offset', 0), 'offset', 0, len(serialized))
            limit = integer(args.get('limit', 4000), 'limit', 1, 4000)
            # MCP embeds this JSON as a text string; reserve its bounded envelope
            # as well as the HTTP object size, including the maximum request id.
            if (len(serialized.encode('utf-8')) <= MAX_RESPONSE_BYTES
                    and len(canonical_json(serialized).encode('utf-8')) + 2048 <= MAX_RESPONSE_BYTES):
                if offset:
                    raise ContractError('unpaged disagreement requires offset zero')
                con.execute('UPDATE job_review_adjudicator_items SET judgments_read=1 WHERE grant_id=? AND ordinal=?',
                            (grant['grant_id'], item['ordinal']))
                return value
            end = min(len(serialized), offset + limit)
            result = bounded({'ordinal': item['ordinal'], 'basis_sha256': value['basis_sha256'],
                'encoding': 'canonical_json', 'payload_sha256': payload_sha256(value),
                'content': serialized[offset:end], 'offset': offset, 'total_chars': len(serialized),
                'next_offset': end if end < len(serialized) else None})
            section = 'judgments:' + str(item['ordinal'])
            prior = con.execute('SELECT through_offset FROM job_review_adjudicator_reads WHERE grant_id=? AND section=?',
                                (grant['grant_id'], section)).fetchone()
            if offset <= (prior[0] if prior else 0):
                through = max(prior[0] if prior else 0, end)
                con.execute('INSERT INTO job_review_adjudicator_reads VALUES(?,?,?) '
                    'ON CONFLICT DO UPDATE SET through_offset=MAX(through_offset,excluded.through_offset)',
                    (grant['grant_id'], section, through))
                if through == len(serialized):
                    con.execute('UPDATE job_review_adjudicator_items SET judgments_read=1 WHERE grant_id=? AND ordinal=?',
                                (grant['grant_id'], item['ordinal']))
            return result
        entries = args.get('resolutions')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
            raise ContractError('resolutions require one to twenty entries')
        ordinals = [integer(e.get('ordinal'), 'ordinal', 1) for e in entries if isinstance(e, dict)]
        if len(ordinals) != len(entries) or len(set(ordinals)) != len(entries):
            raise ContractError('resolutions require unique ordinals')
        if not set(ordinals) <= set(items):
            raise ReviewAuthorizationError('job is outside adjudication assignment')
        for e in entries:
            if e.get('basis_sha256') != resolution_basis(run, items[e['ordinal']]):
                from .authority import ReviewConflictError
                raise ReviewConflictError('resolution basis changed')
        results = []
        for entry in entries:
            try:
                receipt = self._save_resolution(con, grant, run, items[entry['ordinal']], entry)
                results.append({'ordinal': entry['ordinal'], 'status': 'saved', 'receipt': receipt})
            except ContractError:
                results.append({'ordinal': entry['ordinal'], 'status': 'error', 'error': 'validation_failed'})
        return {'results': results}

    def _save_resolution(self, con, grant, run, item, entry):
        from .authority import ReviewConflictError
        exact(entry, ('ordinal', 'basis_sha256', 'choice', 'checked_dimensions', 'explanation', 'evidence'),
              ('ordinal', 'basis_sha256', 'choice', 'checked_dimensions', 'explanation', 'evidence'))
        prior = current_resolution(con, run, item)
        if prior:
            if prior['request_json'] != canonical_json(entry):
                raise ReviewConflictError('resolution submission is immutable')
            return {'review_id': run['review_id'], 'ordinal': item['ordinal'], 'basis_sha256': prior['basis_sha256'], 'choice': prior['choice']}
        if entry['choice'] not in ('primary', 'check', 'unresolved'):
            raise ContractError('invalid whole-assessment choice')
        text(entry['explanation'], 'explanation', 1500)
        dims = differences(json.loads(item['assessment_json']), json.loads(item['check_json']))
        if (not isinstance(entry['checked_dimensions'], list)
                or any(not isinstance(d, str) for d in entry['checked_dimensions'])
                or sorted(entry['checked_dimensions']) != sorted(dims)):
            raise ContractError('explain every differing dimension exactly once')
        context, job = json.loads(run['context_json']), json.loads(item['snapshot_json'])
        for section in ('facts', 'preferences', 'feedback'):
            read = con.execute('SELECT through_offset FROM job_review_adjudicator_reads WHERE grant_id=? AND section=?', (grant['grant_id'], section)).fetchone()
            if read is None or read[0] < len(context.get(section, [])):
                raise ContractError('read all frozen context before adjudication')
        read = con.execute('SELECT through_offset FROM job_review_reads WHERE review_id=? AND ordinal=? AND actor=? AND snapshot_sha256=?',
                           (run['review_id'], item['ordinal'], grant['actor'], item['snapshot_sha256'])).fetchone()
        paired = con.execute('SELECT judgments_read FROM job_review_adjudicator_items WHERE grant_id=? AND ordinal=?', (grant['grant_id'], item['ordinal'])).fetchone()
        if not read or read[0] < len(job.get('description') or '') or not paired[0]:
            raise ContractError('read complete source and both current judgments')
        evidence = entry['evidence']
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
            raise ContractError('resolution requires source evidence')
        facts = {f['fact_id'] for f in context['facts']}
        for e in evidence:
            exact(e, ('field', 'quote', 'fact_id'), ('field', 'quote'))
            quote = text(e['quote'], 'quote', 1000)
            if e['field'] not in REVIEW_JOB_FIELDS or quote not in str(job.get(e['field']) or '') or ('fact_id' in e and e['fact_id'] not in facts):
                raise ContractError('resolution evidence must match frozen sources')
        if not any(e['field'] == 'description' for e in evidence):
            raise ContractError('resolution requires a description quote')
        if entry['choice'] != 'unresolved':
            chosen = json.loads(item['assessment_json' if entry['choice'] == 'primary' else 'check_json'])
            if chosen['decision'] in SELECTED and not any(e.get('fact_id') for e in evidence):
                raise ContractError('selected resolution requires approved fact evidence')
        con.execute('INSERT INTO job_review_resolutions VALUES(?,?,?,?,?,?,?,?,?)',
            (run['review_id'], item['ordinal'], entry['basis_sha256'], grant['grant_id'], grant['actor'], entry['choice'],
             canonical_json(entry), payload_sha256(entry), self._now()))
        receipt = {'review_id': run['review_id'], 'ordinal': item['ordinal'], 'basis_sha256': entry['basis_sha256'], 'choice': entry['choice']}
        con.execute('INSERT INTO job_review_commands VALUES(?,?,?,?,?)',
            ('grant:' + grant['grant_id'] + ':' + str(item['ordinal']), 'resolve', payload_sha256(entry), canonical_json(receipt), self._now()))
        self.service._touch(con, run['review_id'])
        return receipt
