"""Revision-bound editorial ordering, separate from independent job judgments."""
from __future__ import annotations

import json
from ..contracts import ContractError, canonical_json, payload_sha256
from .adjudication import resolved_items
from .contracts import (RUBRIC_VERSION, REVIEW_SUMMARY_FIELDS, SELECTED, TARGETED,
                        exact, identifier, integer, text)

SCHEMA = r"""
CREATE TABLE job_review_calibration_entries (
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id),
 basis_sha256 TEXT NOT NULL, ordinal INTEGER NOT NULL,
 position INTEGER NOT NULL, related_group_json TEXT, actor TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(review_id,basis_sha256,ordinal),
 FOREIGN KEY(review_id,ordinal) REFERENCES job_review_items(review_id,ordinal)
);
CREATE TABLE job_review_calibrations (
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id),
 basis_sha256 TEXT NOT NULL, artifact_json TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(review_id,basis_sha256)
);
CREATE TABLE job_review_finalizer_grants (
 grant_id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL UNIQUE,
 review_id TEXT NOT NULL REFERENCES job_reviews(review_id), actor TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind='finalizer'), context_sha256 TEXT NOT NULL,
 basis_sha256 TEXT NOT NULL, runtime_id TEXT NOT NULL UNIQUE,
 runtime_json TEXT NOT NULL, launch_json TEXT,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE INDEX job_review_finalizer_grants_review ON job_review_finalizer_grants(review_id,expires_at);
"""


def is_v2(run):
    return json.loads(run['context_json']).get('rubric_version') == RUBRIC_VERSION


def related_group(value):
    if value is None:
        return None
    exact(value, ('id', 'label'), ('id', 'label'))
    return {'id': text(value['id'], 'related group id', 100),
            'label': text(value['label'], 'related group label', 200)}


def visible_explanation(assessment):
    """Material caveats are durable card text, not hidden evidence-panel content."""
    labels = {'close': 'Close fit', 'slight_stretch': 'Slight stretch',
              'bigger_stretch': 'Bigger stretch', 'broad_only': 'Broad only'}
    pieces = [labels.get(assessment['decision'], assessment['decision']) + ': ' + assessment['explanation']]
    condition = assessment.get('eligibility_condition')
    if condition and condition.casefold() not in pieces[0].casefold():
        pieces.append('Eligibility: ' + condition)
    if assessment.get('category') == 'alternative':
        pieces.append('Career alternative; outside the main software-building direction.')
    for name, observations in (('Gap', assessment.get('gaps', [])), ('Caveat', assessment.get('unknowns', []))):
        for observation in observations:
            if observation.casefold() not in ' '.join(pieces).casefold():
                pieces.append(name + ': ' + observation)
    next_step = {'apply': 'Apply', 'clarify': 'Clarify the condition before applying',
                 'explore': 'Explore whether this path is appealing'}.get(assessment.get('next_step'))
    if next_step:
        pieces.append('Next step: ' + next_step)
    result = ' '.join(pieces)
    # The curated-list API has always required a <=2,000 character explanation.
    # Never silently truncate caveats. Reviewer can make the original prose concise.
    if len(result) > 2000:
        raise ContractError('visible explanation exceeds 2000 characters; shorten prose while preserving material caveats')
    return result


class CalibrationMixin:
    def _calibration_basis(self, con, run):
        rows = con.execute('SELECT ordinal,revision,snapshot_sha256,assessment_json,check_json FROM job_review_items WHERE review_id=? ORDER BY ordinal',
                           (run['review_id'],)).fetchall()
        resolutions, effective_rows = resolved_items(con, run, rows)
        basis = payload_sha256({'context': run['context_sha256'], 'items': [list(r) for r in rows],
                                **({'resolutions': resolutions} if any(resolutions) else {})})
        selected = {r['ordinal']: dict(r) for r in effective_rows if r['assessment_json'] and json.loads(r['assessment_json'])['decision'] in SELECTED}
        return basis, selected

    def _calibration_state(self, con, run):
        if not is_v2(run):
            return None
        basis, selected = self._calibration_basis(con, run)
        artifact = con.execute('SELECT artifact_json FROM job_review_calibrations WHERE review_id=? AND basis_sha256=?',
                               (run['review_id'], basis)).fetchone()
        count = con.execute('SELECT COUNT(*) FROM job_review_calibration_entries WHERE review_id=? AND basis_sha256=?',
                            (run['review_id'], basis)).fetchone()[0]
        return {'basis_sha256': basis, 'selected_count': len(selected), 'staged_count': count,
                'complete': artifact is not None}

    def _calibration(self, con, args):
        run = self._run(con, args['review_id'])
        state = self._calibration_state(con, run)
        if state is None:
            raise ContractError('calibration requires a v2 review')
        after = integer(args.get('after', 0), 'after')
        limit = integer(args.get('limit', 5), 'limit', 1, 20)
        _, selected = self._calibration_basis(con, run)
        ordinals = [n for n in selected if n > after][:limit]
        items = []
        for ordinal in ordinals:
            item = self._item(con, {'review_id': run['review_id'], 'ordinal': ordinal})
            job = json.loads(item['snapshot_json'])
            staged = con.execute('SELECT position,related_group_json FROM job_review_calibration_entries WHERE review_id=? AND basis_sha256=? AND ordinal=?',
                                 (run['review_id'], state['basis_sha256'], ordinal)).fetchone()
            items.append({'ordinal': ordinal, 'revision': item['revision'],
                          'job': {k: job.get(k) for k in REVIEW_SUMMARY_FIELDS},
                          'assessment': json.loads(selected[ordinal]['assessment_json']),
                          'position': staged['position'] if staged else None,
                          'related_group': json.loads(staged['related_group_json']) if staged and staged['related_group_json'] else None})
        return {**state, 'items': items, 'next_after': ordinals[-1] if ordinals and any(n > ordinals[-1] for n in selected) else None}

    def _check_calibration_basis(self, con, args):
        run = self._run(con, args['review_id'], True)
        if not is_v2(run):
            raise ContractError('calibration requires a v2 review')
        basis, selected = self._calibration_basis(con, run)
        if args.get('basis_sha256') != basis:
            raise ContractError('review evidence changed; reload the complete calibration')
        status = self._status(con, {'review_id': run['review_id']}, _include_calibration=False)
        if status['counts'].get('pending') or status['audit_remaining_count'] or status['unresolved_disagreement_count']:
            raise ContractError('complete coverage and independent agreement before calibration')
        return run, basis, selected

    def _calibrate(self, con, args, *, _validated=None):
        if _validated is None:
            run, basis, selected = self._check_calibration_basis(con, args)
        else:
            connection, actor, run, basis, selected = _validated
            if (connection is not con or not con.in_transaction or args.get('actor') != actor
                    or args.get('review_id') != run['review_id']
                    or args.get('basis_sha256') != basis):
                raise ContractError('calibration batch state does not match this transaction')
        ordinal = integer(args.get('ordinal'), 'ordinal', 1)
        if ordinal not in selected:
            raise ContractError('calibration cannot change selected membership')
        position = integer(args.get('position'), 'position', 1, len(selected))
        group = related_group(args.get('related_group'))
        if con.execute('SELECT 1 FROM job_review_calibrations WHERE review_id=? AND basis_sha256=?', (run['review_id'], basis)).fetchone():
            raise ContractError('calibration is already finalized')
        con.execute('INSERT INTO job_review_calibration_entries VALUES(?,?,?,?,?,?,?) '
                    'ON CONFLICT(review_id,basis_sha256,ordinal) DO UPDATE SET position=excluded.position,related_group_json=excluded.related_group_json,actor=excluded.actor,created_at=excluded.created_at',
                    (run['review_id'], basis, ordinal, position, canonical_json(group) if group else None,
                     identifier(args.get('actor', 'coordinator'), 'actor'), self.now()))
        self._touch(con, run['review_id'])
        return {'review_id': run['review_id'], 'ordinal': ordinal, 'basis_sha256': basis, 'position': position, 'related_group': group}

    def _finalize(self, con, args):
        run, basis, selected = self._check_calibration_basis(con, args)
        prior = con.execute('SELECT artifact_json FROM job_review_calibrations WHERE review_id=? AND basis_sha256=?', (run['review_id'], basis)).fetchone()
        if prior:
            return {'review_id': run['review_id'], 'basis_sha256': basis, 'complete': True}
        entries = [dict(r) for r in con.execute('SELECT ordinal,position,related_group_json FROM job_review_calibration_entries WHERE review_id=? AND basis_sha256=? ORDER BY position,ordinal', (run['review_id'], basis))]
        if {e['ordinal'] for e in entries} != set(selected) or sorted(e['position'] for e in entries) != list(range(1, len(selected) + 1)):
            raise ContractError('calibration requires every selected posting exactly once in a unique complete order')
        groups = {}
        for entry in entries:
            value = json.loads(selected[entry['ordinal']]['assessment_json'])
            visible_explanation(value)
            if entry['related_group_json']:
                group = json.loads(entry['related_group_json'])
                old_label = groups.setdefault(group['id'], group['label'])
                if group['label'] != old_label:
                    raise ContractError('related group labels must agree')
        con.execute('INSERT INTO job_review_calibrations VALUES(?,?,?,?,?)',
                    (run['review_id'], basis, canonical_json(entries), identifier(args.get('actor', 'coordinator'), 'actor'), self.now()))
        self._touch(con, run['review_id'])
        return {'review_id': run['review_id'], 'basis_sha256': basis, 'complete': True}
