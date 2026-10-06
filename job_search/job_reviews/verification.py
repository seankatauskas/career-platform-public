"""Trusted availability observation, with network work outside ledger transactions."""
from __future__ import annotations

from contextlib import closing
import json
import os
from ..contracts import ContractError, canonical_json, payload_sha256
from ..db import connect
from .contracts import TARGETED, exact, identifier, timestamp
from .calibration import is_v2


class VerificationMixin:
    def _verify_availability(self, args, *, principal=None):
        from .service import FIELDS
        args = dict(args or {})
        exact(args, FIELDS['verify-availability'], ('review_id', 'idempotency_key'))
        identifier(args['review_id'], 'review_id')
        key = identifier(args['idempotency_key'], 'idempotency_key')
        digest = payload_sha256({'action': 'verify-availability', 'args': args})
        with closing(connect(self.db_path)) as con, con:
            con.execute('BEGIN')
            self._guard_managed(con, 'verify-availability', args, principal)
            prior = self._verification_receipt(con, key, digest)
            if prior is not None:
                return prior
            run = self._run(con, args['review_id'], True)
            state = self._calibration_state(con, run)
            if not state or not state['complete']:
                raise ContractError('finalize the review before checking availability')
            basis, selected = self._calibration_basis(con, run)
            targets = []
            for ordinal, item in selected.items():
                if json.loads(item['assessment_json'])['decision'] in TARGETED:
                    raw = self._item(con, {'review_id': run['review_id'], 'ordinal': ordinal})
                    targets.append({'ordinal': ordinal, 'snapshot_sha256': raw['snapshot_sha256'],
                                    'job': json.loads(raw['snapshot_json'])})
        jobs = [item['job'] for item in targets]
        if self.availability_checker is None:
            from .availability import check_boards
            results = check_boards(jobs, contact=os.getenv('JOB_SCRAPER_CONTACT', ''))
        else:
            results = self.availability_checker(jobs)
        expected = {(item['job']['ats'], item['job']['id']) for item in targets}
        by_key = {}
        if not isinstance(results, list):
            raise ContractError('availability checker must return observations')
        for result in results:
            exact(result, ('ats', 'job_id', 'status', 'checked_at', 'source', 'reason'),
                  ('ats', 'job_id', 'status', 'checked_at', 'source', 'reason'))
            identity = (result['ats'], result['job_id'])
            if identity not in expected or identity in by_key or result['status'] not in ('open', 'absent', 'unknown'):
                raise ContractError('availability observation does not match selected postings')
            timestamp(result['checked_at'])
            if any(not isinstance(result[field], str) or len(result[field]) > 2000 for field in ('source', 'reason')):
                raise ContractError('invalid availability observation text')
            by_key[identity] = result
        if set(by_key) != expected:
            raise ContractError('availability checker omitted a selected posting')
        with closing(connect(self.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE')
            self._guard_managed(con, 'verify-availability', args, principal)
            prior = self._verification_receipt(con, key, digest)
            if prior is not None:
                return prior
            run = self._run(con, args['review_id'], True)
            if self._calibration_basis(con, run)[0] != basis:
                raise ContractError('review changed during availability checking; retry against its current evidence')
            for item in targets:
                result = by_key[(item['job']['ats'], item['job']['id'])]
                con.execute('INSERT INTO job_review_availability VALUES(?,?,?,?) '
                            'ON CONFLICT(review_id,ordinal) DO UPDATE SET snapshot_sha256=excluded.snapshot_sha256,result_json=excluded.result_json',
                            (run['review_id'], item['ordinal'], item['snapshot_sha256'], canonical_json(result)))
            self._touch(con, run['review_id'])
            response = {'review_id': run['review_id'], 'checked_count': len(results),
                        'open_count': sum(r['status'] == 'open' for r in results),
                        'absent_count': sum(r['status'] == 'absent' for r in results),
                        'unknown_count': sum(r['status'] == 'unknown' for r in results)}
            con.execute('INSERT INTO job_review_commands VALUES(?,?,?,?,?)',
                        (key, 'verify-availability', digest, canonical_json(response), self.now()))
            return response

    @staticmethod
    def _verification_receipt(con, key, digest):
        prior = con.execute('SELECT * FROM job_review_commands WHERE idempotency_key=?', (key,)).fetchone()
        if prior:
            if prior['operation'] != 'verify-availability' or prior['request_sha256'] != digest:
                raise ContractError('idempotency key was reused for a different review command')
            return json.loads(prior['response_json'])
        return None

    def _availability(self, con, run):
        if not is_v2(run):
            return {}
        return {r['ordinal']: json.loads(r['result_json']) for r in con.execute(
            'SELECT a.ordinal,a.result_json FROM job_review_availability a JOIN job_review_items i USING(review_id,ordinal) '
            'WHERE a.review_id=? AND a.snapshot_sha256=i.snapshot_sha256', (run['review_id'],))}
