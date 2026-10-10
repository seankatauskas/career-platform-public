"""Trusted OLD METHOD persistence and publication using the existing review ledger."""
from contextlib import closing
import json
import secrets

from ...contracts import ContractError, canonical_json, payload_sha256
from ...db import connect
from ..brief import current_search_brief
from ..context import review_context
from ..contracts import RUBRIC_VERSION, SELECTED, TARGETED, fingerprint, identifier, stamp, timestamp
from ..cards import visible_explanation
from . import WORKFLOW
from .screen import VERSION
from .workspace import validate_result


class Store:
    def __init__(self, service):
        self.service = service
        self.db_path = service.db_path

    def _run(self, con, rid):
        identifier(rid, 'review_id')
        row = con.execute('SELECT * FROM job_reviews WHERE review_id=?', (rid,)).fetchone()
        if row is None or json.loads(row['metadata_json']).get('workflow') != WORKFLOW:
            raise ContractError('not an OLD METHOD review')
        return row

    def create(self, start, end, *, execution, publish=True):
        start, end = timestamp(start), timestamp(end)
        if not start < end <= timestamp(self.service.now()):
            raise ContractError('posting window must increase and cannot end in the future')
        # Existing read-only source projection. No prior review/list/application lookup.
        jobs, counts = self.service._snapshot(start, end, None)
        jobs = [j for j in jobs if not j.get('closed_at')]
        context = review_context(self.service.context_provider())
        if not context.get('facts'):
            raise ContractError('approved career facts or an active resume are required')
        rid, now = 'oldreview_' + secrets.token_hex(16), self.service.now()
        with closing(connect(self.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE')
            context.update(rubric_version=RUBRIC_VERSION, search_brief=current_search_brief(con))
            context['feedback'] = [dict(r) for r in con.execute('SELECT sequence,note,created_at FROM job_review_feedback ORDER BY sequence DESC LIMIT 20')]
            metadata = {'workflow': WORKFLOW, 'execution_mode': 'isolated', 'phase': 'queued',
                        'screen_version': VERSION, 'execution': execution, 'publish': publish,
                        'source_counts': counts, 'omitted': [], 'assessment_count': 0}
            con.execute('INSERT INTO job_reviews(review_id,mode,window_start,window_end,created_at,updated_at,status,context_json,context_sha256,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (rid, 'custom', stamp(start), stamp(end), now, now, 'active', canonical_json(context), payload_sha256(context), canonical_json(metadata)))
            con.executemany('INSERT INTO job_review_items(review_id,ordinal,ats,job_id,snapshot_json,snapshot_sha256) VALUES(?,?,?,?,?,?)',
                            [(rid, n, j['ats'], str(j['id']), canonical_json(j), fingerprint(j)) for n, j in enumerate(jobs, 1)])
        return self.status(rid)

    def packet(self, rid):
        with closing(connect(self.db_path)) as con:
            run = self._run(con, rid)
            jobs = [json.loads(r[0]) for r in con.execute('SELECT snapshot_json FROM job_review_items WHERE review_id=? ORDER BY ordinal', (rid,))]
            meta = json.loads(run['metadata_json'])
            return {'workflow': WORKFLOW, 'review_id': rid, 'screen_version': meta['screen_version'],
                    'window_start': run['window_start'], 'window_end': run['window_end'],
                    'context': json.loads(run['context_json']), 'context_sha256': run['context_sha256'],
                    'sources_sha256': payload_sha256([fingerprint(j) for j in jobs]), 'jobs': jobs}

    def metadata(self, rid):
        with closing(connect(self.db_path)) as con:
            return json.loads(self._run(con, rid)['metadata_json'])

    def status(self, rid):
        with closing(connect(self.db_path)) as con:
            run = self._run(con, rid)
            meta = json.loads(run['metadata_json'])
            return {'review_id': rid, 'workflow': WORKFLOW, 'status': run['status'],
                    **{k: meta[k] for k in ('phase', 'assessment_count', 'omitted', 'error', 'progress', 'execution', 'publish') if k in meta},
                    'receipt': json.loads(run['receipt_json']) if run['receipt_json'] else None}

    def update(self, rid, **changes):
        with closing(connect(self.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE')
            run = self._run(con, rid)
            if run['status'] != 'active':
                raise ContractError('review is no longer active')
            metadata = json.loads(run['metadata_json'])
            metadata.update(changes)
            con.execute('UPDATE job_reviews SET metadata_json=?,updated_at=? WHERE review_id=?',
                        (canonical_json(metadata), self.service.now(), rid))

    def checkpoint(self, rid, result, *, sealed=False):
        packet = self.packet(rid)
        validate_result(packet, result, require_sealed=sealed)
        with closing(connect(self.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE')
            run = self._run(con, rid)
            if run['status'] != 'active':
                raise ContractError('review is no longer active')
            for row in con.execute('SELECT ordinal,revision,assessment_json FROM job_review_items WHERE review_id=?', (rid,)).fetchall():
                value = result['assessments'].get(str(row['ordinal']))
                encoded = canonical_json(value) if value else None
                if encoded != row['assessment_json']:
                    revision = row['revision'] + 1
                    con.execute('INSERT INTO job_review_revisions VALUES(?,?,?,?,?,?,?)',
                                (rid, row['ordinal'], revision, 'primary', 'old-method-lead', self.service.now(), canonical_json(value)))
                    con.execute('UPDATE job_review_items SET revision=?,assessment_json=?,reviewer=? WHERE review_id=? AND ordinal=?',
                                (revision, encoded, 'old-method-lead', rid, row['ordinal']))
            metadata = json.loads(run['metadata_json'])
            metadata.update(result=result, assessment_count=len(result['assessments']))
            con.execute('UPDATE job_reviews SET metadata_json=?,version=version+1,updated_at=? WHERE review_id=?',
                        (canonical_json(metadata), self.service.now(), rid))

    def publish(self, rid, observations):
        packet = self.packet(rid)
        with closing(connect(self.db_path)) as con, con:
            con.execute('BEGIN IMMEDIATE')
            run = self._run(con, rid)
            if run['status'] == 'published':
                return json.loads(run['receipt_json'])
            if run['status'] != 'active':
                raise ContractError('review is no longer active')
            metadata = json.loads(run['metadata_json'])
            if not metadata['publish']:
                raise ContractError('unpublished qualification cannot publish')
            result = validate_result(packet, metadata.get('result', {}))
            available = {(v['ats'], v['job_id']): v for v in observations}
            selected, omitted = [], []
            for n in result['order']:
                source, value = packet['jobs'][n-1], result['assessments'][str(n)]
                current = self.service.catalog.get_job(source['ats'], str(source['id']))
                if current.get('closed_at'):
                    omitted.append({'ordinal': n, 'reason': 'closed'})
                    continue
                if fingerprint(current) != fingerprint(source):
                    raise ContractError('selected posting changed; start a fresh review for reassessment')
                if not timestamp(packet['window_start']) < timestamp(current['posted_at']) <= timestamp(packet['window_end']):
                    raise ContractError('selected posting is outside the window')
                observation = available.get((source['ats'], str(source['id'])))
                if observation is None or observation['status'] != 'open':
                    omitted.append({'ordinal': n, 'reason': observation['status'] if observation else 'unverified'})
                    continue
                selected.append((source, value))
            receipt = {'review_id': rid, 'lists': [], 'omitted': omitted}
            for kind in ('broad', 'targeted'):
                rows = [r for r in selected if kind == 'broad' or r[1]['decision'] in TARGETED]
                chunks = [rows[i:i+500] for i in range(0, len(rows), 500)] or [[]]
                for part, chunk in enumerate(chunks, 1):
                    payload = {'title': f'OLD METHOD | {kind.title()} | {run["window_start"]} to {run["window_end"]}' + (f' | Part {part}' if len(chunks)>1 else ''),
                               'window_start': run['window_start'], 'window_end': run['window_end'],
                               'idempotency_key': f'{rid}-{kind}-{part}',
                               'jobs': [{'ats': j['ats'], 'job_id': str(j['id']), 'explanation': visible_explanation(a)} for j, a in chunk]}
                    saved = self.service.curated.publish_in_transaction(con, payload)
                    con.execute('INSERT INTO job_review_publications VALUES(?,?,?,?)', (rid, saved['list_id'], kind, part))
                    receipt['lists'].append({**saved, 'kind': kind, 'part': part})
            metadata.update(phase='published', omitted=omitted, availability=observations)
            con.execute("UPDATE job_reviews SET status='published',metadata_json=?,receipt_json=?,updated_at=? WHERE review_id=?",
                        (canonical_json(metadata), canonical_json(receipt), self.service.now(), rid))
            return receipt
