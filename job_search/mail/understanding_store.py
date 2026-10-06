"""One durable interpretation, independent reviewed findings, and resumable projections.

Full supplied sources are authenticated ciphertext. Read/review paths use only
previously verified bounded quotes and never need an archive encryption key.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta

from ..contracts import (ContractError, ConflictError,
                         MutationContext, canonical_json, payload_sha256, parse_utc,
                         validate_identifier, utc_now, MODEL_AUTO_APPLY_EVENT_TYPES)
from ..db import connect


def available(con):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='mail_understanding_analyses'").fetchone())


def owns_evidence(con, evidence_id):
    return bool(evidence_id and available(con) and con.execute(
        'SELECT 1 FROM mail_understanding_ownership WHERE evidence_id=?', (evidence_id,)).fetchone())


def _id(*parts):
    return hashlib.sha256(canonical_json(parts).encode()).hexdigest()


def _projection(con, finding_id):
    rows = con.execute('SELECT kind,target_id FROM mail_understanding_projections WHERE finding_id=? ORDER BY kind', (finding_id,)).fetchall()
    return [{'kind': r['kind'], 'id': r['target_id']} for r in rows]


def _detail(con, analysis_id):
    row = con.execute('SELECT * FROM mail_understanding_analyses WHERE analysis_id=?', (analysis_id,)).fetchone()
    if not row:
        raise ContractError('mail analysis not found')
    request = json.loads(row['request_json'])
    findings = []
    for f in con.execute('SELECT * FROM mail_understanding_findings WHERE analysis_id=? ORDER BY ordinal,type,finding_id', (analysis_id,)):
        projections = _projection(con, f['finding_id'])
        decision = con.execute('SELECT * FROM mail_understanding_decisions WHERE finding_id=?', (f['finding_id'],)).fetchone()
        status = decision['decision'] if decision else 'pending'
        for p in projections:
            if p['kind'] in ('event_proposal', 'temporal_proposal'):
                table, key = ('event_proposals', 'proposal_id') if p['kind']=='event_proposal' else ('temporal_proposals', 'temporal_proposal_id')
                delegated = con.execute(f'SELECT status FROM {table} WHERE {key}=?', (p['id'],)).fetchone()
                if delegated:
                    status = {'auto_applied':'accepted','conflict':'held','superseded':'rejected'}.get(delegated['status'], delegated['status'])
        value = json.loads(f['value_json'])
        item = {'finding_id': f['finding_id'], 'type': f['type'], 'value': value,
                'status': status, 'projection': projections[0] if projections else None}
        if decision and decision['application_id']:
            item['application_id'] = decision['application_id']
        if f['replacement_of']:
            item['replacement_of'] = f['replacement_of']
        if projections:
            item['projections'] = projections
        findings.append(item)
    evidence = con.execute('SELECT subject FROM mail_evidence WHERE evidence_id=?', (row['evidence_id'],)).fetchone()
    applications = {f.get('application_id') or f['value'].get('application_id') for f in findings}
    applications.discard(None)
    result = {k: row[k] for k in ('analysis_id','account_id','immutable_message_id','evidence_id','mode','replay_id','state','relevance','created_at')}
    result.update(subject=evidence['subject'] if evidence else '', coverage=request.get('coverage', []),
                  application_id=next(iter(applications)) if len(applications)==1 else None,
                  candidate_application_ids=[c['application_id'] for c in request.get('candidates', [])],
                  findings=findings)
    latest=con.execute("SELECT analysis_id FROM mail_understanding_analyses WHERE evidence_id=? AND mode=? AND replay_id IS ? AND state IN ('saved','projected') ORDER BY created_at DESC,rowid DESC LIMIT 1",(row['evidence_id'],row['mode'],row['replay_id'])).fetchone()
    result['current']=bool(latest and latest[0]==analysis_id)
    result['revision'] = payload_sha256(result)
    return result


def briefing_analyses(con):
    """Private read model, including replay provenance so callers can exclude it."""
    if not available(con):
        return []
    return [_detail(con, row[0]) for row in con.execute(
        "SELECT analysis_id FROM mail_understanding_analyses WHERE state IN ('saved','projected') ORDER BY created_at,analysis_id")]


def _sources_aad(analysis_id, source_id):
    return {'version': 1, 'kind': 'mail_understanding_source', 'analysis_id': analysis_id, 'source_id': source_id}


class MailUnderstandingService:
    def __init__(self, ledger, archive=None):
        self.ledger, self.store, self.archive = ledger, ledger.store, archive
        self.path = self.store.db_path

    def get(self, analysis_id):
        validate_identifier(analysis_id, 'analysis_id')
        with connect(self.path) as con:
            return _detail(con, analysis_id)

    def find_for_message(self, account_id, immutable_message_id, *, mode='shared', replay_id=None):
        with connect(self.path) as con:
            row = con.execute("SELECT analysis_id FROM mail_understanding_analyses WHERE account_id=? AND immutable_message_id=? AND mode=? AND replay_id IS ? AND state IN ('saved','projected') ORDER BY created_at DESC,analysis_id DESC LIMIT 1", (account_id,immutable_message_id,mode,replay_id)).fetchone()
            return _detail(con, row[0]) if row else None

    def owns_message(self, account_id, immutable_message_id):
        with connect(self.path) as con:
            return bool(available(con) and con.execute('SELECT 1 FROM mail_understanding_ownership o JOIN mail_evidence e USING(evidence_id) WHERE e.account_id=? AND e.immutable_message_id=?',(account_id,immutable_message_id)).fetchone())

    def own_evidence(self, evidence_id, context):
        self._trusted(context)
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            if not con.execute('SELECT 1 FROM mail_evidence WHERE evidence_id=?',(evidence_id,)).fetchone():
                raise ContractError('mail evidence not found')
            con.execute('INSERT OR IGNORE INTO mail_understanding_ownership VALUES(?,NULL,?)',(evidence_id,utc_now()))
        return {'evidence_id':evidence_id,'owned':True}

    def list_reviews(self, *, history=False, limit=100):
        if type(limit) is not int or not 1<=limit<=1000:
            raise ContractError('invalid mail review limit')
        with connect(self.path) as con:
            if not available(con):
                return []
            rows = briefing_analyses(con)
        result = []
        for row in reversed(rows):
            if (row['mode']=='replay') != bool(history) or row['mode']=='shadow' or not row['current']:
                continue
            if any(f['status'] in ('pending','held') for f in row['findings']):
                if not any(old['account_id']==row['account_id'] and old['immutable_message_id']==row['immutable_message_id'] for old in result):
                    result.append(row)
        return result[:limit]

    def _request(self, con, analysis_id):
        if self.archive is None:
            raise ContractError('authenticated mail archive is required for analysis')
        row = con.execute('SELECT request_json FROM mail_understanding_analyses WHERE analysis_id=?', (analysis_id,)).fetchone()
        if not row:
            raise ContractError('mail analysis not found')
        request = json.loads(row[0])
        for source in request['sources']:
            saved = con.execute('SELECT * FROM mail_understanding_sources WHERE analysis_id=? AND source_id=?', (analysis_id, source['source_id'])).fetchone()
            if not saved:
                raise ContractError('authenticated analysis source is missing')
            text = self.archive._open(saved, _sources_aad(analysis_id, source['source_id']), 'plaintext_sha256')
            if len(text)!=saved['plaintext_chars'] or hashlib.sha256(text.encode()).hexdigest()!=source['sha256']:
                raise ContractError('authenticated analysis source does not match manifest')
            source['text'] = text
        return request

    def get_request(self, analysis_id):
        with connect(self.path) as con:
            return self._request(con, analysis_id)

    def bind_inference_work(self,analysis_id,claim_token,work_id,revision,context):
        """Bind the provider owner before any request can leave the process."""
        self._trusted(context)
        validate_identifier(work_id,'work_id')
        if type(revision) is not int or revision<0:
            raise ContractError('invalid inference work revision')
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            stamp=utc_now()
            self._fence(con,analysis_id,claim_token,stamp)
            work=con.execute('SELECT status,recovery_revision FROM work_items WHERE work_id=?',(work_id,)).fetchone()
            if not work or work['status']!='running' or work['recovery_revision']!=revision:
                raise ConflictError('inference work no longer owns its revision')
            previous=con.execute('SELECT * FROM mail_understanding_inference_work WHERE analysis_id=? AND claim_token=?',(analysis_id,claim_token)).fetchone()
            if previous:
                if previous['work_id']!=work_id or previous['work_revision']!=revision:
                    raise ConflictError('analysis claim already has a different inference owner')
                return {'analysis_id':analysis_id,'work_id':work_id}
            invocations=con.execute('SELECT rowid AS invocation_rowid,invocation_id,state FROM inference_invocations WHERE work_id=?',(work_id,)).fetchall()
            before={'high_water':max((r['invocation_rowid'] for r in invocations),default=0),
                    'terminal_ids':sorted(r['invocation_id'] for r in invocations if r['state'] in ('completed','failed','cancelled')),
                    'unfinished_ids':sorted(r['invocation_id'] for r in invocations if r['state'] not in ('completed','failed','cancelled'))}
            con.execute('INSERT INTO mail_understanding_inference_work VALUES(?,?,?,?,?,?)',(analysis_id,claim_token,work_id,revision,canonical_json(before),stamp))
            return {'analysis_id':analysis_id,'work_id':work_id}

    def finish_inference_work(self,analysis_id,claim_token,work_id,invocation_ids,context):
        """Seal this attempt before the parent worker can process a sibling email."""
        self._trusted(context)
        if not isinstance(invocation_ids,(list,tuple,set)) or len(invocation_ids)>256:
            raise ContractError('inference attempt references must be a bounded array')
        for item in invocation_ids:validate_identifier(item,'invocation_id')
        ids=sorted(set(invocation_ids))
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            binding=con.execute('SELECT * FROM mail_understanding_inference_work WHERE analysis_id=? AND claim_token=?',(analysis_id,claim_token)).fetchone()
            if not binding or binding['work_id']!=work_id:
                raise ConflictError('inference result does not match its durable owner')
            for iid in ids:
                if not con.execute('SELECT 1 FROM inference_invocations WHERE invocation_id=? AND work_id=?',(iid,work_id)).fetchone():
                    raise ContractError('inference result reference belongs to different work')
            prior=con.execute('SELECT invocation_ids_json FROM mail_understanding_inference_results WHERE analysis_id=? AND claim_token=?',(analysis_id,claim_token)).fetchone()
            encoded=canonical_json(ids)
            if prior and prior[0]!=encoded:
                raise ConflictError('inference attempt references are already sealed')
            if not prior:
                con.execute('INSERT INTO mail_understanding_inference_results VALUES(?,?,?,?)',(analysis_id,claim_token,encoded,utc_now()))
        return {'analysis_id':analysis_id,'invocation_ids':ids}

    @staticmethod
    def _bound_invocations(con,analysis_id):
        binding=con.execute('SELECT rowid AS binding_seq,* FROM mail_understanding_inference_work WHERE analysis_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(analysis_id,)).fetchone()
        if not binding:return None,[]
        sealed=con.execute('SELECT invocation_ids_json FROM mail_understanding_inference_results WHERE analysis_id=? AND claim_token=?',(analysis_id,binding['claim_token'])).fetchone()
        invocations=con.execute('SELECT rowid AS invocation_rowid,* FROM inference_invocations WHERE work_id=?',(binding['work_id'],)).fetchall()
        if sealed:
            selected=set(json.loads(sealed[0]))
            return binding,[r for r in invocations if r['invocation_id'] in selected]
        before=json.loads(binding['before_invocations_json'])
        before_ids=set(before.get('terminal_ids',[])) if isinstance(before,dict) else set(before)
        high=before.get('high_water',0) if isinstance(before,dict) else max((r['invocation_rowid'] for r in invocations if r['invocation_id'] in before_ids),default=0)
        unfinished=set(before.get('unfinished_ids',[])) if isinstance(before,dict) else set()
        next_binding=con.execute('SELECT before_invocations_json FROM mail_understanding_inference_work WHERE work_id=? AND rowid>? ORDER BY rowid LIMIT 1',(binding['work_id'],binding['binding_seq'])).fetchone()
        upper=None
        if next_binding:
            after=json.loads(next_binding[0])
            upper=after.get('high_water',0) if isinstance(after,dict) else max((r['invocation_rowid'] for r in invocations if r['invocation_id'] in after),default=0)
        rows=[r for r in invocations if (r['invocation_id'] in unfinished or r['invocation_rowid']>high) and (upper is None or r['invocation_rowid']<=upper)]
        return binding,rows

    @classmethod
    def _audited_safe_failure(cls,con,analysis_id):
        binding,rows=cls._bound_invocations(con,analysis_id)
        if not binding or not rows:return False
        # Every request in this attempt must have an audited failed/absent outcome.
        # A completed synchronous call still needs explicit lost-result review.
        for row in rows:
            if row['state']!='failed' or not con.execute("SELECT 1 FROM inference_recovery_commands WHERE invocation_id=? AND actor_kind='user' AND resolution IN ('absent','failed') AND requested_at>=? LIMIT 1",(row['invocation_id'],binding['created_at'])).fetchone():
                return False
        return True

    def _message_blocker(self,con,request,stamp):
        rows=con.execute("SELECT * FROM mail_understanding_analyses WHERE account_id=? AND immutable_message_id=? AND state IN ('claimed','uncertain','failed') ORDER BY created_at,rowid",(request['account_id'],request['immutable_message_id'])).fetchall()
        for row in rows:
            if row['state']=='claimed' and row['lease_expires_at'] and row['lease_expires_at']>stamp:
                return {'analysis_id':row['analysis_id'],'claim_token':None,'state':'busy'}
            binding,invocations=self._bound_invocations(con,row['analysis_id'])
            if row['state']!='uncertain':
                outstanding=any(inv['state'] in ('submitting','unknown','accepted','completed') for inv in invocations)
                if not outstanding:continue
                # An expired processing claim with an uncheckpointed synchronous
                # result cannot justify repeating the provider request elsewhere.
                for inv in invocations:
                    if inv['state']=='completed':
                        con.execute("UPDATE inference_invocations SET state='unknown',reconciliation_reason='mail_analysis_not_checkpointed',updated_at=? WHERE invocation_id=?",(stamp,inv['invocation_id']))
                con.execute("UPDATE mail_understanding_analyses SET state='uncertain',last_error='usage_reconciliation_required',claim_token=NULL,lease_expires_at=NULL WHERE analysis_id=?",(row['analysis_id'],))
            if self._audited_safe_failure(con,row['analysis_id']):
                con.execute("UPDATE mail_understanding_analyses SET state='failed',last_error='audited_inference_failure',attempts=MIN(attempts,2),claim_token=NULL,lease_expires_at=NULL WHERE analysis_id=?",(row['analysis_id'],))
                continue
            return {'analysis_id':row['analysis_id'],'claim_token':None,'state':'uncertain'}
        return None

    def can_retry_message(self,account_id,immutable_message_id):
        """A historical replay may requeue reconciliation only after audited safety."""
        with connect(self.path) as con:
            rows=con.execute("SELECT * FROM mail_understanding_analyses WHERE account_id=? AND immutable_message_id=? AND state IN ('claimed','uncertain','failed')",(account_id,immutable_message_id)).fetchall()
            for row in rows:
                if row['state']=='claimed' and row['lease_expires_at'] and row['lease_expires_at']>utc_now():
                    return False
                _,invocations=self._bound_invocations(con,row['analysis_id'])
                outstanding=any(inv['state'] in ('submitting','unknown','accepted','completed') for inv in invocations)
                if (row['state']=='uncertain' or outstanding) and not self._audited_safe_failure(con,row['analysis_id']):
                    return False
            return True

    @staticmethod
    def _trusted(context):
        context.validate()
        if context.actor_kind!='system':
            raise ContractError('mail analysis processing requires trusted system actor')

    def claim(self, request, context, *, mode='shared', lease_seconds=300):
        self._trusted(context)
        if mode not in ('shared','shadow','replay') or type(lease_seconds) is not int or not 1<=lease_seconds<=3600:
            raise ContractError('invalid analysis claim')
        if self.archive is None:
            raise ContractError('authenticated mail archive is required for analysis')
        from .understanding_contracts import validate_request
        request = validate_request(request)
        for key in ('account_id','observation_id','evidence_id','producer_version','schema_version'):
            validate_identifier(request.get(key), key)
        if not isinstance(request.get('sources'), list) or not request['sources'] or len(request['sources'])>64:
            raise ContractError('analysis requires bounded sources')
        if mode=='replay' and not request.get('replay_id'):
            raise ContractError('historical analysis requires replay identity')
        digest = payload_sha256({'request':request,'mode':mode})
        analysis_id = _id('mail-analysis',digest)
        metadata = json.loads(canonical_json(request))
        sealed = []
        source_ids = set()
        for source in metadata['sources']:
            text = source.pop('text', None)
            sid = source.get('source_id')
            validate_identifier(sid,'source_id')
            if sid in source_ids or not isinstance(text,str) or not text or hashlib.sha256(text.encode()).hexdigest()!=source.get('sha256'):
                raise ContractError('invalid or duplicate analysis source')
            source_ids.add(sid)
            sealed.append((sid,self.archive._seal(text,_sources_aad(analysis_id,sid),1_000_000)))
        stamp = utc_now()
        expiry = (parse_utc(stamp)+timedelta(seconds=lease_seconds)).isoformat(timespec='seconds').replace('+00:00','Z')
        token = secrets.token_hex(24)
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            evidence = con.execute('SELECT * FROM mail_evidence WHERE evidence_id=?',(request['evidence_id'],)).fetchone()
            observation = con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?',(request['observation_id'],)).fetchone()
            if not evidence or not observation or evidence['account_id']!=request['account_id'] or evidence['immutable_message_id']!=request['immutable_message_id'] or observation['evidence_id']!=request['evidence_id'] or observation['direction']!='inbound':
                raise ContractError('analysis requires matching observed inbound evidence')
            blocker=self._message_blocker(con,request,stamp)
            if blocker:
                return blocker
            previous = con.execute('SELECT * FROM mail_understanding_analyses WHERE analysis_id=?',(analysis_id,)).fetchone()
            # The message is the concurrency boundary. A new candidate phase,
            # budget trim, or producer fingerprint cannot evade an existing claim.
            if not previous:
                previous=con.execute('SELECT * FROM mail_understanding_analyses WHERE account_id=? AND immutable_message_id=? AND mode=? AND replay_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1',(request['account_id'],request['immutable_message_id'],mode,request.get('replay_id'))).fetchone()
                if previous:
                    analysis_id=previous['analysis_id']
            if previous:
                if previous['state'] in ('saved','projected'):
                    return {'analysis_id':analysis_id,'claim_token':None,'state':'saved'}
                if previous['state']=='uncertain':
                    return {'analysis_id':analysis_id,'claim_token':None,'state':'uncertain'}
                if previous['state']=='claimed' and previous['lease_expires_at']>stamp:
                    return {'analysis_id':analysis_id,'claim_token':None,'state':'busy'}
                if json.loads(previous['request_json'])['producer_version']!=request['producer_version']:
                    return {'analysis_id':analysis_id,'claim_token':None,'state':'busy','reason':'producer_change_requires_replay'}
                if previous['attempts']>=3:
                    return {'analysis_id':analysis_id,'claim_token':None,'state':'busy'}
                con.execute("UPDATE mail_understanding_analyses SET state='claimed',claim_token=?,lease_expires_at=?,attempts=attempts+1,last_error='' WHERE analysis_id=?",(token,expiry,analysis_id))
            else:
                con.execute("INSERT INTO mail_understanding_analyses(analysis_id,input_sha256,account_id,immutable_message_id,evidence_id,observation_id,mode,replay_id,request_json,state,claim_token,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,'claimed',?,?,?)",(analysis_id,digest,request['account_id'],request['immutable_message_id'],request['evidence_id'],request['observation_id'],mode,request.get('replay_id'),canonical_json(metadata),token,expiry,stamp))
                for sid, value in sealed:
                    con.execute('INSERT INTO mail_understanding_sources VALUES(?,?,?,?,?,?,?,?)',(analysis_id,sid,value.key_id,value.nonce,value.ciphertext,value.aad_sha256,value.plaintext_sha256,value.plaintext_chars))
            if mode=='shared':
                con.execute('INSERT OR IGNORE INTO mail_understanding_ownership VALUES(?,?,?)',(request['evidence_id'],analysis_id,stamp))
            return {'analysis_id':analysis_id,'claim_token':token,'state':'claimed'}

    @staticmethod
    def _fence(con, analysis_id, token, stamp):
        row=con.execute('SELECT * FROM mail_understanding_analyses WHERE analysis_id=?',(analysis_id,)).fetchone()
        if not row or row['state']!='claimed' or not token or row['claim_token']!=token or row['lease_expires_at']<=stamp:
            raise ConflictError('mail analysis claim is stale')
        return row

    def save(self, analysis_id, claim_token, raw, context):
        self._trusted(context)
        from .understanding_contracts import validate_analysis
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            stamp=utc_now()
            self._fence(con,analysis_id,claim_token,stamp)
            request=self._request(con,analysis_id)
            result=validate_analysis(raw,request)
            if result['relevance']=='uncertain' and not result['uncertainties']:
                result['uncertainties'].append({'reason':'relevance_requires_review','description':'This message could not be confidently classified as career correspondence.','finding_type':'message','finding_index':None})
            terminal={'offer_accepted','rejection_received','withdrawn'}
            events={value['event_type'] for value in result['events']}
            if events & terminal and len(events-{'submission_confirmed','submission_observed'})>1:
                result['uncertainties'].append({'reason':'conflicting_application_outcomes','description':'Conflicting application outcomes need independent review.','finding_type':'message','finding_index':None})
            for typ,key in (('event','events'),('action','actions'),('temporal','temporal_facts'),('uncertainty','uncertainties')):
                for ordinal,value in enumerate(result[key]):
                    self._insert_finding(con,analysis_id,typ,ordinal,value,stamp)
            for ordinal,item in enumerate(request.get('coverage',[]),start=len(result['uncertainties'])):
                self._insert_finding(con,analysis_id,'uncertainty',ordinal,{'reason':'incomplete_coverage','description':str(item.get('reason','Incomplete source coverage'))[:256],'finding_type':'message','finding_index':None},stamp)
            con.execute("UPDATE mail_understanding_analyses SET state='saved',relevance=?,saved_at=?,claim_token=NULL,lease_expires_at=NULL WHERE analysis_id=?",(result['relevance'],stamp,analysis_id))
            return _detail(con,analysis_id)

    @staticmethod
    def _insert_finding(con,analysis_id,typ,ordinal,value,stamp,replacement_of=None):
        fid=_id(analysis_id,typ,ordinal)
        con.execute('INSERT INTO mail_understanding_findings VALUES(?,?,?,?,?,?,?)',(fid,analysis_id,typ,ordinal,canonical_json(value),replacement_of,stamp))
        return fid

    def fail(self, analysis_id, claim_token, reason, context):
        self._trusted(context)
        if not isinstance(reason,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}',reason):
            raise ContractError('analysis failure requires a bounded reason code')
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            self._fence(con,analysis_id,claim_token,utc_now())
            state='uncertain' if reason in ('provider_outcome_unknown','usage_reconciliation_required') else 'failed'
            con.execute('UPDATE mail_understanding_analyses SET state=?,last_error=?,claim_token=NULL,lease_expires_at=NULL WHERE analysis_id=?',(state,reason,analysis_id))
            if reason=='usage_deferred':
                con.execute('UPDATE mail_understanding_analyses SET attempts=MAX(0,attempts-1) WHERE analysis_id=?',(analysis_id,))
        return {'analysis_id':analysis_id,'state':state}

    @staticmethod
    def _record_projection(con, finding_id, kind, target_id, stamp):
        con.execute('INSERT OR IGNORE INTO mail_understanding_projections VALUES(?,?,?,?)',(finding_id,kind,target_id,stamp))

    @staticmethod
    def _record_decision(con, finding_id, decision, application_id, context, reason, stamp, policy_id=''):
        con.execute('INSERT INTO mail_understanding_decisions VALUES(?,?,?,?,?,?,?,?)',(_id('decision',finding_id),finding_id,decision,application_id,context.actor_kind,str(reason)[:1000],policy_id,stamp))

    def _make_proposal(self, con, analysis, finding, stamp, application_id=None):
        fid, typ = finding['finding_id'], finding['type']
        existing = _projection(con,fid)
        if existing:
            return existing[0]
        value=json.loads(finding['value_json'])
        app=application_id or value.get('application_id')
        request=json.loads(analysis['request_json'])
        quote=value['evidence'][0]
        sources={s['source_id']:s for s in request['sources']}
        source=sources[quote['source_id']]
        pid=_id('projection',fid,typ)
        if typ=='event':
            con.execute("INSERT INTO event_proposals(proposal_id,dedupe_key,evidence_id,proposed_application_id,event_type,producer_kind,producer_version,confidence,candidate_application_ids_json,evidence_quote,span_start,span_end,payload_json,verification_json,status,created_at,understanding_finding_id) VALUES(?,?,?,?,?,'model',?,?,?,?,?,?,'{}',?,'pending',?,?)",(pid,'understanding:'+fid,analysis['evidence_id'],app,value['event_type'],request['producer_version'],value['confidence'],canonical_json([c['application_id'] for c in request['candidates']]),quote['quote'],quote['start'],quote['end'],canonical_json({'analysis_id':analysis['analysis_id'],'finding_id':fid,'source_id':quote['source_id'],'source_sha256':source['sha256']}),stamp,fid))
            kind='event_proposal'
        elif typ=='temporal':
            normalized=(value['starts_at'] and value['ends_at']) if value['kind']=='interview' else value['due_at']
            archive_id=source.get('archive_id') or request.get('archive_id')
            if not normalized or not value['time_zone'] or not app or not archive_id:
                return None
            if not con.execute('SELECT 1 FROM mail_archive WHERE archive_id=?',(archive_id,)).fetchone():
                raise ContractError('temporal finding requires its authenticated archive')
            con.execute("INSERT INTO temporal_proposals(temporal_proposal_id,dedupe_key,archive_id,attachment_record_id,application_id,kind,starts_at,ends_at,due_at,time_zone,confidence,evidence_quote,span_start,span_end,source_sha256,producer_version,status,created_at,understanding_finding_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",(pid,'understanding:'+fid,archive_id,source.get('attachment_record_id'),app,value['kind'],value['starts_at'],value['ends_at'],value['due_at'],value['time_zone'],value['confidence'],quote['quote'],quote['start'],quote['end'],source['sha256'],request['producer_version'],stamp,fid))
            kind='temporal_proposal'
        else:
            return None
        self._record_projection(con,fid,kind,pid,stamp)
        return {'kind':kind,'id':pid}

    def project(self, analysis_id, context, *, evaluation_report=None):
        self._trusted(context)
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            analysis=con.execute('SELECT * FROM mail_understanding_analyses WHERE analysis_id=?',(analysis_id,)).fetchone()
            if not analysis or analysis['state'] not in ('saved','projected'):
                raise ConflictError('mail analysis is not saved')
            if analysis['mode']=='shadow':
                return _detail(con,analysis_id)
            request=json.loads(analysis['request_json'])
            stamp=utc_now()
            findings=con.execute('SELECT * FROM mail_understanding_findings WHERE analysis_id=? ORDER BY ordinal,type',(analysis_id,)).fetchall()
            uncertain=bool(request.get('coverage')) or any(f['type']=='uncertainty' for f in findings)
            can_auto=analysis['mode']=='shared' and evaluation_report is not None and not uncertain and request.get('candidate_context_complete')
            for finding in findings:
                if finding['replacement_of']:
                    continue
                value=json.loads(finding['value_json'])
                decision=con.execute('SELECT 1 FROM mail_understanding_decisions WHERE finding_id=?',(finding['finding_id'],)).fetchone()
                if decision:
                    continue
                if finding['type'] in ('event','temporal'):
                    projection=self._make_proposal(con,analysis,finding,stamp)
                    pending=projection and con.execute("SELECT 1 FROM event_proposals WHERE proposal_id=? AND status='pending'",(projection['id'],)).fetchone() if finding['type']=='event' else False
                    if finding['type']=='event' and pending and value['event_type'] in {e.value for e in MODEL_AUTO_APPLY_EVENT_TYPES} and can_auto and value.get('application_id') and evaluation_report.allows('event',value,request):
                        policy=self._event_policy(con,evaluation_report,value['event_type'],request,stamp)
                        if policy:
                            self.store._auto_apply_event_proposal(con,stamp,projection['id'],context,policy)
                elif finding['type']=='action' and can_auto and evaluation_report.allows('action',value,request) and self._eligible_action(value):
                    held=self._action_hold(con,analysis,finding,value)
                    if not held:
                        self._record_decision(con,finding['finding_id'],'accepted',value['application_id'],context,'Evaluated action gate',stamp,getattr(evaluation_report,'policy_id',''))
            self._project_actions(con,analysis,context,stamp)
            con.execute("UPDATE mail_understanding_analyses SET state='projected' WHERE analysis_id=?",(analysis_id,))
            return _detail(con,analysis_id)

    @staticmethod
    def _event_policy(con,report,event_type,request,stamp):
        factory=getattr(report,'event_policy',None)
        if not callable(factory):
            return None
        policy=factory(event_type)
        if not isinstance(policy,dict):
            return None
        policy_id=policy.get('policy_id') or getattr(report,'policy_id','')
        if not policy_id:
            return None
        required=('threshold','example_count','observed_precision','wrong_application_matches')
        if not all(key in policy for key in required):
            return None
        digest=policy.get('evaluation_sha256')
        if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
            return None
        con.execute('INSERT OR IGNORE INTO classifier_automation_policies(policy_id,event_type,producer_version,threshold,example_count,observed_precision,wrong_application_matches,evaluation_sha256,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)',(policy_id,event_type,request['producer_version'],policy['threshold'],policy['example_count'],policy['observed_precision'],policy['wrong_application_matches'],digest,stamp))
        return policy_id

    @staticmethod
    def _task_kind(value):
        return value.get('task_kind') if value['kind']=='other' else value['kind']

    @staticmethod
    def _eligible_action(value):
        mapped=value['kind'] in ('reply','send_availability','complete_assessment','offer_decision') or (value['kind']=='other' and value.get('task_kind') in ('send_document','follow_up'))
        return value.get('application_id') and mapped and value['actor']=='applicant' and value['obligation']=='required' and (value['kind'] not in ('reply','send_availability') or value['channel']=='email')

    @staticmethod
    def _action_evidence(con,analysis,value):
        """A cited prior inbound request is the only automatic renewal link."""
        evidence={analysis['evidence_id']}
        request=json.loads(analysis['request_json'])
        cited={item['source_id'] for item in value['evidence']}
        for source in request['sources']:
            if source['source_id'] not in cited or source['kind']!='prior_inbound' or not source.get('archive_id'):
                continue
            row=con.execute("SELECT old.evidence_id FROM lifecycle_mail_observations old JOIN lifecycle_mail_observations current ON current.observation_id=? AND old.account_id=current.account_id AND old.conversation_ref=current.conversation_ref WHERE old.archive_id=? AND old.direction='inbound' AND old.source_at<current.source_at",(analysis['observation_id'],source['archive_id'])).fetchone()
            if row and row['evidence_id']:evidence.add(row['evidence_id'])
        return sorted(evidence)

    def _matching_tasks(self,con,analysis,finding,value):
        evidence=self._action_evidence(con,analysis,value)
        marks=','.join('?' for _ in evidence)
        tasks=con.execute("SELECT * FROM lifecycle_tasks WHERE evidence_id IN ("+marks+") AND kind=? ORDER BY created_at,task_id",(*evidence,self._task_kind(value))).fetchall()
        semantic={k:v for k,v in value.items() if k not in ('confidence','application_id')}
        result=[]
        for task in tasks:
            siblings=con.execute("SELECT f.value_json FROM mail_understanding_projections p JOIN mail_understanding_findings f USING(finding_id) WHERE p.kind='task' AND p.target_id=? AND f.analysis_id=? AND f.finding_id<>?",(task['task_id'],analysis['analysis_id'],finding['finding_id'])).fetchall()
            # Distinct obligations within one analysis keep separate identities,
            # even when they happen to map to the same lifecycle task kind.
            if siblings and not any({k:v for k,v in json.loads(s[0]).items() if k not in ('confidence','application_id')}==semantic for s in siblings):
                continue
            result.append(task)
        return result

    def _action_hold(self, con, analysis, finding, value):
        app=value.get('application_id')
        if not self._eligible_action(value):
            return 'action_requires_clarification'
        application=self.store._application(con,app)
        if application['current_phase']=='terminal':
            return 'application_closed'
        observation=con.execute('SELECT * FROM lifecycle_mail_observations WHERE observation_id=?',(analysis['observation_id'],)).fetchone()
        if not observation or observation['direction']!='inbound':
            return 'mail_direction_changed'
        if con.execute("SELECT 1 FROM lifecycle_mail_observations WHERE account_id=? AND conversation_ref=? AND direction IN ('inbound','outbound') AND source_at>? LIMIT 1",(analysis['account_id'],observation['conversation_ref'],observation['source_at'])).fetchone():
            return 'newer_conversation_activity'
        # Conservative equivalence deliberately ignores model version and quote offsets.
        # A changed paraphrase cannot reopen a rejected or cancelled obligation.
        evidence=self._action_evidence(con,analysis,value)
        marks=','.join('?' for _ in evidence)
        previous=con.execute("SELECT f.finding_id,d.decision FROM mail_understanding_findings f JOIN mail_understanding_analyses a USING(analysis_id) JOIN mail_understanding_decisions d USING(finding_id) WHERE a.evidence_id IN ("+marks+") AND f.type='action' AND json_extract(f.value_json,'$.kind')=? AND f.finding_id<>? AND d.decision='rejected' LIMIT 1",(*evidence,value['kind'],finding['finding_id'])).fetchone()
        if previous and not finding['replacement_of']:
            return 'previously_rejected_request'
        prior=self._matching_tasks(con,analysis,finding,value)
        if any(t['status']!='open' or t['application_id']!=app for t in prior):
            return 'previous_task_resolved'
        if len(prior)>1:
            return 'ambiguous_previous_requests'
        return None

    def _project_actions(self, con, analysis, context, stamp):
        if analysis['mode']=='shadow':
            return
        from ..lifecycle.mail import link_accepted_evidence
        for finding in con.execute("SELECT f.*,d.application_id FROM mail_understanding_findings f JOIN mail_understanding_decisions d USING(finding_id) WHERE f.analysis_id=? AND f.type='action' AND d.decision='accepted' ORDER BY f.ordinal",(analysis['analysis_id'],)).fetchall():
            value=json.loads(finding['value_json'])
            value['application_id']=finding['application_id'] or value.get('application_id')
            existing=_projection(con,finding['finding_id'])
            if not existing:
                held=self._action_hold(con,analysis,finding,value)
                if held:
                    continue
                app=value['application_id']
                link_accepted_evidence(con,analysis['evidence_id'],app,context,stamp)
                kind=self._task_kind(value)
                matches=self._matching_tasks(con,analysis,finding,value)
                prior=matches[0] if matches else None
                if prior:
                    task_id=prior['task_id']
                else:
                    request=json.loads(analysis['request_json'])
                    task=self.ledger.lifecycle._create_task(con,app,{'kind':kind,'owner':'applicant','note':value['description'],'source_time':request['received_at'],'evidence_id':analysis['evidence_id'],'policy_version':'mail-understanding-v1'},context,stamp)
                    task_id=task['task_id']
                self._record_projection(con,finding['finding_id'],'task',task_id,stamp)
            else:
                task_id=next((p['id'] for p in existing if p['kind']=='task'),None)
            if task_id and value.get('temporal_index') is not None:
                self._apply_due_date(con,analysis,finding,value,task_id,context,stamp)

    def _apply_due_date(self,con,analysis,finding,value,task_id,context,stamp):
        temporal=con.execute("SELECT finding_id FROM mail_understanding_findings WHERE analysis_id=? AND type='temporal' AND ordinal=?",(analysis['analysis_id'],value['temporal_index'])).fetchone()
        if not temporal:
            return
        fid=temporal[0]
        for _ in range(32):
            replacement=con.execute('SELECT finding_id FROM mail_understanding_findings WHERE replacement_of=? ORDER BY ordinal DESC LIMIT 1',(fid,)).fetchone()
            if not replacement:break
            fid=replacement[0]
        proposal=con.execute("SELECT t.* FROM temporal_proposals t JOIN mail_understanding_projections p ON p.target_id=t.temporal_proposal_id AND p.kind='temporal_proposal' WHERE p.finding_id=? AND t.status='accepted' AND t.kind='deadline'",(fid,)).fetchone()
        task=con.execute("SELECT * FROM lifecycle_tasks WHERE task_id=? AND status='open'",(task_id,)).fetchone()
        if not proposal or not task or task['application_id']!=proposal['application_id'] or task['due_at']==proposal['due_at']:
            return
        from ..lifecycle.core import _revision, _cancel_task_notifications
        revised=dict(task)
        revised.update(due_at=proposal['due_at'],revision_no=task['revision_no']+1,updated_at=stamp)
        con.execute('UPDATE lifecycle_tasks SET due_at=?,revision_no=?,updated_at=? WHERE task_id=?',(revised['due_at'],revised['revision_no'],stamp,task_id))
        _revision(con,revised,'reviewed_deadline',context,stamp)
        _cancel_task_notifications(con,task_id)

    @staticmethod
    def _replacement(value, original, typ, request, temporal_count):
        """Review may change meaning, but can cite only the already verified text."""
        from .understanding_contracts import _FIELDS, _object, _enum, _text, _validate_temporal, ACTION_KINDS
        from .proposals import MAIL_EVENT_TYPES
        key={'event':'events','action':'actions','temporal':'temporal_facts'}.get(typ)
        if not key:
            raise ContractError('uncertainties cannot be replaced with actionable findings')
        value=dict(value) if isinstance(value,dict) else value
        task_kind=value.pop('task_kind',None) if isinstance(value,dict) else None
        value=dict(_object(value,_FIELDS[key],'reviewed replacement'))
        if task_kind is not None:
            if typ!='action' or value['kind']!='other' or task_kind not in ('send_document','follow_up'):
                raise ContractError('reviewed task mapping is invalid')
            value['task_kind']=task_kind
        if value['evidence']!=original['evidence'] or value['confidence']!=original['confidence']:
            raise ContractError('reviewed corrections must preserve verified evidence and confidence')
        candidates={c['application_id'] for c in request['candidates']}
        if value['application_id'] is not None and value['application_id'] not in candidates:
            raise ContractError('reviewed application is outside supplied candidates')
        if typ=='event':
            _enum(value['event_type'],{t.value for t in MAIL_EVENT_TYPES},'event_type')
        elif typ=='action':
            _enum(value['kind'],ACTION_KINDS,'action kind')
            _text(value['description'],'action description')
            _enum(value['actor'],{'applicant','employer','unknown'},'actor')
            _enum(value['obligation'],{'required','optional','unclear'},'obligation')
            _enum(value['channel'],{'email','portal','other','unknown'},'channel')
            index=value['temporal_index']
            if index is not None and (type(index) is not int or not 0<=index<temporal_count):
                raise ContractError('temporal reference is outside saved analysis')
        else:
            _validate_temporal(value,request['received_at'])
        return value

    def decide(self, analysis_id, revision, decisions, context):
        context.validate()
        if context.actor_kind!='user':
            raise ContractError('mail finding decisions require actor_kind=user')
        if not isinstance(revision,str) or not isinstance(decisions,list) or not 1<=len(decisions)<=32:
            raise ContractError('mail review requires a bounded decision batch and revision')
        request_payload={'analysis_id':analysis_id,'revision':revision,'decisions':decisions}
        def operation(con,stamp):
            detail=_detail(con,analysis_id)
            if detail['revision']!=revision:
                raise ConflictError('mail analysis review changed; refresh before deciding')
            if detail['mode']=='shadow' or not detail['current'] or detail['state'] not in ('saved','projected'):
                raise ContractError('analysis is not available for review')
            analysis=con.execute('SELECT * FROM mail_understanding_analyses WHERE analysis_id=?',(analysis_id,)).fetchone()
            request=json.loads(analysis['request_json'])
            candidates={c['application_id'] for c in request['candidates']}
            selected=set()
            current={f['finding_id']:f for f in detail['findings']}
            terminal={'offer_accepted','rejection_received','withdrawn'}
            accepted_events=[]
            for choice in decisions:
                if isinstance(choice,dict) and choice.get('decision')=='accepted' and choice.get('finding_id') in current:
                    item=current[choice['finding_id']]
                    if item['type']=='event':
                        candidate=choice.get('replacement') or item['value']
                        if isinstance(candidate,dict):accepted_events.append(candidate.get('event_type'))
            if set(accepted_events)&terminal and len(set(accepted_events)-{'submission_confirmed','submission_observed'})>1:
                raise ContractError('conflicting outcomes must be resolved independently')
            temporal_count=con.execute("SELECT COUNT(*) FROM mail_understanding_findings WHERE analysis_id=? AND type='temporal' AND replacement_of IS NULL",(analysis_id,)).fetchone()[0]
            for choice in decisions:
                if not isinstance(choice,dict) or not {'finding_id','decision','application_id','reason'}<=choice.keys() or set(choice)-{'finding_id','decision','application_id','reason','replacement'}:
                    raise ContractError('mail finding decision fields are invalid')
                fid=choice['finding_id']
                if fid in selected or fid not in current or current[fid]['status'] not in ('pending','held'):
                    raise ConflictError('mail finding is missing, duplicated, or already decided')
                selected.add(fid)
                decision=choice['decision']
                if decision not in ('accepted','rejected') or not isinstance(choice['reason'],str) or len(choice['reason'])>1000:
                    raise ContractError('invalid mail finding decision')
                original=con.execute('SELECT * FROM mail_understanding_findings WHERE finding_id=?',(fid,)).fetchone()
                value=json.loads(original['value_json'])
                finding=original
                app=choice['application_id'] or value.get('application_id')
                if app is not None and app not in candidates:
                    raise ContractError('reviewed application is outside supplied candidates')
                if app:
                    self.store._application(con,app)
                if choice.get('replacement') is not None:
                    if decision!='accepted':
                        raise ContractError('a corrected finding must be explicitly accepted')
                    replacement=self._replacement(choice['replacement'],value,original['type'],request,temporal_count)
                    replacement['application_id']=app
                    self._decide_finding(con,analysis,original,'rejected',app,context,'Replaced in reviewed correction',stamp)
                    ordinal=con.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM mail_understanding_findings WHERE analysis_id=? AND type=?',(analysis_id,original['type'])).fetchone()[0]
                    new_id=self._insert_finding(con,analysis_id,original['type'],ordinal,replacement,stamp,original['finding_id'])
                    finding=con.execute('SELECT * FROM mail_understanding_findings WHERE finding_id=?',(new_id,)).fetchone()
                self._decide_finding(con,analysis,finding,decision,app,context,choice['reason'],stamp)
            self._project_actions(con,analysis,context,stamp)
            return _detail(con,analysis_id)
        return self.store._idempotent('mail_understanding.decide',context,request_payload,operation)

    def _decide_finding(self,con,analysis,finding,decision,app,context,reason,stamp):
        fid,typ=finding['finding_id'],finding['type']
        value=json.loads(finding['value_json'])
        if typ in ('event','temporal'):
            if decision=='accepted' and not app:
                raise ContractError('acceptance requires an application selection')
            projection=self._make_proposal(con,analysis,finding,stamp,app)
            if projection:
                if typ=='event':
                    self.store._decide_event_proposal(con,stamp,projection['id'],decision,app,reason,context)
                else:
                    proposal=con.execute('SELECT application_id FROM temporal_proposals WHERE temporal_proposal_id=?',(projection['id'],)).fetchone()
                    if app and proposal['application_id']!=app:
                        raise ContractError('changing a temporal application requires a reviewed replacement')
                    self.store._decide_temporal_proposal(con,stamp,projection['id'],decision,reason,context)
                return
            if decision=='accepted':
                raise ContractError('temporal finding needs reviewed normalization and an authenticated archive')
        elif typ=='action' and decision=='accepted':
            value['application_id']=app
            if not self._eligible_action(value):
                raise ContractError('action needs an explicit applicant obligation and supported task mapping')
            held=self._action_hold(con,analysis,finding,value)
            if held:
                raise ConflictError('action requires reconciliation: '+held)
        self._record_decision(con,fid,decision,app,context,reason,stamp)
