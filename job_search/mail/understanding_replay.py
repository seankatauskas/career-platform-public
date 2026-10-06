"""Fixed-membership, archive-only shared analysis of all linked inbound history."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from ..contracts import ContractError, ConflictError, MutationContext, canonical_json, parse_utc, utc_now, validate_identifier
from .context import CandidateApplication
from .archive_source import _parts

SCHEMA = r'''
CREATE TABLE mail_understanding_replays (
 replay_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, status TEXT NOT NULL,
 claim_token TEXT, lease_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE mail_understanding_replay_items (
 replay_id TEXT NOT NULL REFERENCES mail_understanding_replays(replay_id),
 ordinal INTEGER NOT NULL, observation_id TEXT NOT NULL REFERENCES lifecycle_mail_observations(observation_id),
 snapshot_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 analysis_id TEXT, reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 PRIMARY KEY(replay_id,ordinal), UNIQUE(replay_id,observation_id)
);
CREATE TRIGGER mail_understanding_replay_snapshot_immutable
BEFORE UPDATE OF replay_id,ordinal,observation_id,snapshot_json ON mail_understanding_replay_items
BEGIN SELECT RAISE(ABORT,'replay membership is immutable'); END;
'''


def connect(path):
    from ..db import connect as database_connect
    return database_connect(path)


class _ReplaySourceIssue(ContractError):
    def __init__(self, reason):
        super().__init__(reason)
        self.replay_reason = reason


class UnderstandingReplay:
    def __init__(self, ledger, runtime=None, *, clock=utc_now):
        self.ledger, self.runtime, self.clock = ledger, runtime, clock
        self.store = ledger.store

    def preview(self, account_id):
        validate_identifier(account_id, 'account_id')
        with connect(self.store.db_path) as con:
            row = con.execute("SELECT COUNT(*) AS messages, SUM(CASE WHEN archive_id IS NULL THEN 1 ELSE 0 END) AS missing_archives FROM lifecycle_mail_observations m WHERE account_id=? AND direction='inbound' AND evidence_id IS NOT NULL AND EXISTS (SELECT 1 FROM lifecycle_mail_links l WHERE l.observation_id=m.observation_id)",(account_id,)).fetchone()
            return dict(account_id=account_id,messages=row['messages'],missing_archives=row['missing_archives'] or 0,review_only=True)

    def start(self, account_id, context):
        context.validate()
        if context.actor_kind != 'user':
            raise ContractError('history reanalysis requires an operator decision')
        validate_identifier(account_id,'account_id')
        def operation(con, stamp):
            replay_id = str(uuid.uuid4())
            con.execute("INSERT INTO mail_understanding_replays VALUES (?,?,'pending',NULL,NULL,?,?)",(replay_id,account_id,stamp,stamp))
            rows = con.execute("SELECT m.* FROM lifecycle_mail_observations m WHERE account_id=? AND direction='inbound' AND evidence_id IS NOT NULL AND EXISTS (SELECT 1 FROM lifecycle_mail_links l WHERE l.observation_id=m.observation_id) ORDER BY source_at,observation_id",(account_id,)).fetchall()
            for index,row in enumerate(rows):
                con.execute('INSERT INTO mail_understanding_replay_items(replay_id,ordinal,observation_id,snapshot_json,updated_at) VALUES (?,?,?,?,?)',(replay_id,index,row['observation_id'],canonical_json(dict(row)),stamp))
            return {'replay_id':replay_id,'messages':len(rows),'review_only':True}
        return self.store._idempotent('mail_understanding.replay.start',context,{'account_id':account_id},operation)

    def inspect(self, replay_id):
        validate_identifier(replay_id,'replay_id')
        with connect(self.store.db_path) as con:
            row = con.execute('SELECT * FROM mail_understanding_replays WHERE replay_id=?',(replay_id,)).fetchone()
            if row is None:
                raise ContractError('history reanalysis not found')
            result = {k:v for k,v in dict(row).items() if k not in {'claim_token','lease_until'}}
            result['counts'] = {r['status']:r['n'] for r in con.execute('SELECT status,COUNT(*) AS n FROM mail_understanding_replay_items WHERE replay_id=? GROUP BY status',(replay_id,))}
            result['issues'] = [dict(r) for r in con.execute("SELECT ordinal,observation_id,status,reason FROM mail_understanding_replay_items WHERE replay_id=? AND status IN ('failed','unavailable','reconciliation') ORDER BY ordinal LIMIT 50",(replay_id,))]
            result['review_only'] = True
            return result

    def cancel(self, replay_id, context):
        context.validate()
        if context.actor_kind != 'user':
            raise ContractError('history cancellation requires an operator decision')
        def operation(con,stamp):
            row = con.execute('SELECT status FROM mail_understanding_replays WHERE replay_id=?',(replay_id,)).fetchone()
            if not row:
                raise ContractError('history reanalysis not found')
            if row['status'] != 'completed':
                con.execute("UPDATE mail_understanding_replays SET status='cancelled',claim_token=NULL,lease_until=NULL,updated_at=? WHERE replay_id=?",(stamp,replay_id))
            return {'replay_id':replay_id,'status':'completed' if row['status']=='completed' else 'cancelled'}
        return self.store._idempotent('mail_understanding.replay.cancel',context,{'replay_id':replay_id},operation)

    def run_batch(self, replay_id, *, limit=50, retry_failed=False):
        validate_identifier(replay_id, 'replay_id')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ContractError('history batch size must be 1..50')
        if type(retry_failed) is not bool:
            raise ContractError('retry_failed must be a boolean')
        if self.runtime is None or self.runtime.analyzer is None or self.runtime.mode == 'paused':
            raise ContractError('history reanalysis requires a configured active analyzer')
        token = str(uuid.uuid4())
        def lease(stamp):
            return (parse_utc(stamp)+timedelta(seconds=300)).isoformat(timespec='seconds').replace('+00:00','Z')
        with connect(self.store.db_path) as con:
            con.execute('BEGIN IMMEDIATE')
            job = con.execute('SELECT * FROM mail_understanding_replays WHERE replay_id=?',(replay_id,)).fetchone()
            if not job:
                raise ContractError('history reanalysis not found')
            if job['status'] == 'cancelled':
                raise ConflictError('history reanalysis was cancelled')
            stamp = self.clock()
            if job['claim_token'] and job['lease_until'] > stamp:
                raise ConflictError('history reanalysis is already running')
            con.execute("UPDATE mail_understanding_replays SET status='running',claim_token=?,lease_until=?,updated_at=? WHERE replay_id=?",(token,lease(stamp),stamp,replay_id))
            if retry_failed:
                con.execute("UPDATE mail_understanding_replay_items SET status='pending',reason='' WHERE replay_id=? AND status IN ('failed','unavailable')",(replay_id,))
                # The durable service verifies user reconciliation against the
                # exact provider attempts; a new batch cannot authorize a retry.
                for held in con.execute("SELECT ordinal,snapshot_json FROM mail_understanding_replay_items WHERE replay_id=? AND status='reconciliation'",(replay_id,)).fetchall():
                    snapshot = json.loads(held['snapshot_json'])
                    if self.runtime.service.can_retry_message(snapshot['account_id'],snapshot['immutable_message_id']):
                        con.execute("UPDATE mail_understanding_replay_items SET status='pending',reason='' WHERE replay_id=? AND ordinal=?",(replay_id,held['ordinal']))
            rows = [dict(r) for r in con.execute("SELECT * FROM mail_understanding_replay_items WHERE replay_id=? AND status='pending' ORDER BY ordinal LIMIT ?",(replay_id,limit))]
        def heartbeat():
            if self.runtime.mode == 'paused':
                return False
            stamp = self.clock()
            with connect(self.store.db_path) as con:
                return con.execute("UPDATE mail_understanding_replays SET lease_until=?,updated_at=? WHERE replay_id=? AND claim_token=? AND status='running' AND lease_until>?",(lease(stamp),stamp,replay_id,token,stamp)).rowcount == 1
        try:
            for item in rows:
                if self.runtime.mode == 'paused':
                    break
                if not heartbeat():
                    raise ConflictError('history reanalysis lease lost')
                observation = json.loads(item['snapshot_json'])
                status, reason, analysis_id = 'done','',None
                try:
                    if not observation.get('archive_id'):
                        status,reason = 'unavailable','archive_missing'
                    else:
                        with connect(self.store.db_path) as con:
                            live = con.execute('SELECT direction,evidence_id,archive_id FROM lifecycle_mail_observations WHERE observation_id=?',(observation['observation_id'],)).fetchone()
                            if not live or live['direction'] != 'inbound':
                                raise _ReplaySourceIssue('message_direction_changed')
                            if live['evidence_id'] != observation['evidence_id'] or live['archive_id'] != observation['archive_id']:
                                raise _ReplaySourceIssue('message_source_changed')
                            candidates = [CandidateApplication(application_id=r['application_id'],ats=r['ats'],job_id=r['job_id'],employer=r['employer_snapshot'],title=r['title_snapshot'],company_slug=r['company_slug_snapshot'],phase=r['current_phase'],match_context='previously linked email conversation') for r in con.execute('SELECT a.* FROM applications a JOIN lifecycle_mail_links l USING(application_id) WHERE l.observation_id=? ORDER BY a.application_id LIMIT 21',(observation['observation_id'],))]
                        try:
                            text = self.runtime.archive.read_message(observation['archive_id'])
                        except (ContractError, OSError) as exc:
                            raise _ReplaySourceIssue('archive_unavailable') from exc
                        subject,body = _parts(text)
                        result = self.runtime.process(observation,subject=subject,body=body,candidates=candidates[:20],candidate_context_complete=len(candidates)<=20,
                            coverage=[dict(source_id='history-archive',reason='legacy_archive_quoted_history_unavailable')],heartbeat=heartbeat,replay_id=replay_id)
                        if result.get('state') in {'busy','paused'}:
                            break
                        analysis_id = result['analysis_id']
                except Exception as exc:
                    if getattr(exc,'defer_without_attempt',False):
                        break
                    status = 'reconciliation' if getattr(exc,'outcome_unknown',False) else ('unavailable' if getattr(exc,'replay_reason',None) == 'archive_unavailable' else 'failed')
                    reason = getattr(exc, 'replay_reason', type(exc).__name__)
                with connect(self.store.db_path) as con:
                    con.execute('BEGIN IMMEDIATE')
                    if not con.execute("SELECT 1 FROM mail_understanding_replays WHERE replay_id=? AND claim_token=? AND status='running' AND lease_until>?",(replay_id,token,self.clock())).fetchone():
                        raise ConflictError('history reanalysis lease lost')
                    con.execute('UPDATE mail_understanding_replay_items SET status=?,reason=?,analysis_id=?,updated_at=? WHERE replay_id=? AND ordinal=?',(status,reason,analysis_id,self.clock(),replay_id,item['ordinal']))
        finally:
            with connect(self.store.db_path) as con:
                pending = con.execute("SELECT 1 FROM mail_understanding_replay_items WHERE replay_id=? AND status IN ('pending','failed','unavailable','reconciliation') LIMIT 1",(replay_id,)).fetchone()
                con.execute('UPDATE mail_understanding_replays SET status=?,claim_token=NULL,lease_until=NULL,updated_at=? WHERE replay_id=? AND claim_token=?',('pending' if pending else 'completed',self.clock(),replay_id,token))
        return self.inspect(replay_id)
