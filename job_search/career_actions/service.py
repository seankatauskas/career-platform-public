"""Immutable approvals and write-ahead delivery state, separate from draft actions.

An HTTP 202 is accepted for processing, never evidence that a message was sent.
Only a persisted outbound mailbox observation resolves delivery. Interrupted send
attempts are quarantined; having a draft ID does not make sending safe to retry.
"""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from datetime import timedelta
from collections.abc import Mapping
from ..contracts import ContractError, ConflictError, MutationContext, canonical_json, parse_utc, utc_now
from ..db import connect
from .agenda import AgendaMixin
from .commitments import CommitmentMixin


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def addresses(values):
    if not isinstance(values, list):
        raise ContractError('recipient list is invalid')
    result = []
    for value in values:
        address = (value.get('emailAddress') or {}).get('address') if isinstance(value, Mapping) else None
        if not isinstance(address, str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+', address) or len(address)>320:
            raise ContractError('recipient address is invalid')
        result.append(address.lower())
    return result


def row(saved):
    result = dict(saved)
    result.update(json.loads(result.pop('payload_json')))
    return result


def audit(con, pid, operation, context, details=None, *, stamp=None):
    con.execute('INSERT INTO career_action_audit (proposal_id,operation,actor_kind,source_ref,details_json,created_at) VALUES (?,?,?,?,?,?)',
        (pid,operation,context.actor_kind,context.source_ref,canonical_json(details or {}),stamp or utc_now()))


def enabled(con, capability):
    saved = con.execute('SELECT enabled FROM automation_controls WHERE capability=?',(capability,)).fetchone()
    return bool(saved and saved[0])


class CareerActionService(AgendaMixin, CommitmentMixin):
    def __init__(self, ledger, *, outlook=None, account_id='', reply_provider=None, now_provider=None, availability_policy=None):
        self.ledger, self.store = ledger, ledger.store
        self.outlook, self.account_id, self.reply_provider = outlook, account_id, reply_provider
        self.now_provider = now_provider
        from ..availability import AvailabilityPolicy
        self.availability_policy = availability_policy or AvailabilityPolicy()

    def _now(self):
        value = self.now_provider() if self.now_provider else utc_now()
        return value if isinstance(value,str) else value.isoformat(timespec="seconds").replace("+00:00","Z")

    def _audit(self, con, pid, operation, context, details=None):
        audit(con,pid,operation,context,details,stamp=self._now())

    def _source(self, app, evidence_id):
        evidence = self.store.get_mail_evidence(evidence_id)
        account = self.account_id or evidence['account_id']
        self.store.resolve_reply_evidence(evidence_id, app, account)
        with connect(self.store.db_path) as con:
            self.ledger.lifecycle._evidence(con,app,evidence_id)
            newer = con.execute("SELECT 1 FROM lifecycle_mail_observations WHERE account_id=? AND conversation_ref=? AND direction='inbound' AND source_at>? AND immutable_message_id<>? LIMIT 1",
                (account,digest({'account':account,'conversation':evidence['conversation_id']}),evidence['received_at'],evidence['immutable_message_id'])).fetchone()
            application = self.store._application(con,app)
            if application['current_phase']=='terminal':
                raise ConflictError('application is terminal')
        if newer:
            raise ConflictError('newer incoming mail requires a new reply review')
        if not self.outlook:
            with connect(self.store.db_path) as con:
                saved=con.execute('SELECT * FROM career_reply_sources WHERE evidence_id=? AND application_id=?',(evidence_id,app)).fetchone()
            if not saved or parse_utc(self._now())-parse_utc(saved['checked_at'])>timedelta(minutes=15):
                raise ContractError('verified reply source unavailable or stale')
            return evidence,json.loads(saved['source_json'])
        message = self.outlook.read_message_body(evidence['immutable_message_id'])
        if message.get('id') != evidence['immutable_message_id'] or message.get('isDraft') is not False:
            raise ContractError('reply source must be the observed incoming message')
        from ..mail.sanitizer import sanitize_mail
        live_body=message.get('body') or {}
        sanitized=sanitize_mail(str(message.get('subject') or ''),str(live_body.get('content') or ''),body_kind=str(live_body.get('contentType') or 'text'),max_chars=2048)
        if sanitized.content_sha256!=evidence['body_sha256']:
            raise ConflictError('source evidence changed; synchronize mail before drafting')
        recipients = addresses(message.get('replyTo') or [message.get('from') or message.get('sender')])
        if not recipients or len(recipients)>10:
            raise ContractError('reply requires one to ten exact recipients')
        # Recipients can come from Reply-To; never infer a changed address from prose.
        source_hash = digest({key:message.get(key) for key in (
            'id','conversationId','lastModifiedDateTime','body','replyTo','from','sender','subject','isDraft')})
        subject = str(message.get('subject') or '')
        if not subject.lower().startswith('re:'):
            subject = 'Re: ' + subject
        return evidence, {'message_id':evidence['immutable_message_id'], 'conversation_id':str(message.get('conversationId') or ''),'conversation_ref':digest({'account':account,'conversation':str(message.get('conversationId') or '')}),
            'recipients':recipients,'subject':subject,'source_hash':source_hash}

    def propose_reply(self, application_id, evidence_id, body, context, *, offered_slots=()):
        context.validate()
        if not isinstance(body,str) or not body.strip() or len(body)>20000 or any(ord(c)<32 and c not in '\r\n\t' for c in body):
            raise ContractError('reply body must be bounded exact text')
        evidence, source = self._source(application_id,evidence_id)
        slots=[]
        if not isinstance(offered_slots,(list,tuple)) or len(offered_slots)>8:
            raise ContractError('at most eight offered slots')
        for slot in offered_slots:
            if not isinstance(slot,Mapping) or set(slot)-{'starts_at','ends_at','time_zone'}:
                raise ContractError('invalid offered slot')
            start,end=parse_utc(slot['starts_at']),parse_utc(slot['ends_at'])
            if start<=parse_utc(self._now()) or end<=start or end-start>timedelta(hours=8):
                raise ContractError('offered slot must be future and bounded')
            slots.append({'starts_at':slot['starts_at'],'ends_at':slot['ends_at'],'time_zone':str(slot.get('time_zone') or 'UTC')})
        if slots:
            from .slots import label
            rendered='Offered times:\n'+'\n'.join(label(slot) for slot in slots)
            if rendered not in body:
                body=body.rstrip()+'\n\n'+rendered
        payload={**source,'body':body,'offered_slots':slots}
        hashed=digest(payload)
        def operation(con,stamp):
            stamp=self._now()
            old=con.execute("SELECT * FROM career_send_proposals WHERE application_id=? AND evidence_id=? AND payload_hash=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",(application_id,evidence_id,hashed,stamp)).fetchone()
            if old:return row(old)
            pid=uuid.uuid4().hex
            expires=(parse_utc(stamp)+timedelta(minutes=15)).isoformat().replace('+00:00','Z')
            con.execute('INSERT INTO career_send_proposals (proposal_id,application_id,account_id,evidence_id,payload_json,payload_hash,source_hash,status,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,\'pending\',?,?,?)',
                (pid,application_id,evidence['account_id'],evidence_id,canonical_json(payload),hashed,source['source_hash'],expires,stamp,stamp))
            self._audit(con,pid,'proposed',context)
            return row(con.execute('SELECT * FROM career_send_proposals WHERE proposal_id=?',(pid,)).fetchone())
        return self.store._idempotent('career.propose_reply',context,{'app':application_id,'evidence':evidence_id,'payload':payload},operation)

    def _present(self,saved):
        result=row(saved)
        if result['status'] in {'pending','approved'} and result['expires_at']<=self._now():result['status']='expired'
        return result

    def get_proposal(self, proposal_id):
        with connect(self.store.db_path) as con:
            saved=con.execute('SELECT * FROM career_send_proposals WHERE proposal_id=?',(proposal_id,)).fetchone()
            if not saved:raise ContractError('reply proposal not found')
            return self._present(saved)

    def list_proposals(self, *, application_id=None, status=None, limit=100):
        if type(limit) is not int or not 1<=limit<=500:raise ContractError('invalid limit')
        where,params=[],[]
        for field,value in [('application_id',application_id),('status',status)]:
            if value is not None:where.append(field+'=?');params.append(value)
        with connect(self.store.db_path) as con:
            return {'proposals':[self._present(r) for r in con.execute('SELECT * FROM career_send_proposals'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY created_at DESC,proposal_id LIMIT ?',(*params,limit))]}

    def decide_proposal(self, proposal_id, decision, payload_hash, context, *, expected_source_hash=None):
        context.validate()
        if context.actor_kind!='user' or decision not in {'approved','rejected'}:
            raise ContractError('exact send approval requires an explicit user decision')
        def operation(con,stamp):
            stamp=self._now()
            saved=con.execute('SELECT * FROM career_send_proposals WHERE proposal_id=?',(proposal_id,)).fetchone()
            if not saved:raise ContractError('reply proposal not found')
            if saved['payload_hash']!=payload_hash or (expected_source_hash is not None and saved['source_hash']!=expected_source_hash):
                raise ConflictError('review content changed')
            if saved['status']!='pending':raise ConflictError('proposal is no longer pending')
            if saved['expires_at']<=stamp:raise ConflictError('reply review expired')
            con.execute('UPDATE career_send_proposals SET status=?,updated_at=? WHERE proposal_id=?',(decision,stamp,proposal_id))
            self._audit(con,proposal_id,decision,context,{'payload_hash':payload_hash,'source_hash':saved['source_hash']})
            return row(con.execute('SELECT * FROM career_send_proposals WHERE proposal_id=?',(proposal_id,)).fetchone())
        return self.store._idempotent('career.decide_reply',context,{'id':proposal_id,'decision':decision,'hash':payload_hash,'source_hash':expected_source_hash},operation)

    def _state(self,pid,status,context,*,error='',remote_id=None,send_requested=False):
        with connect(self.store.db_path) as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("UPDATE career_send_proposals SET status=?,error_code=?,updated_at=? WHERE proposal_id=? AND status<>'observed_sent'",(status,error,self._now(),pid))
            if remote_id is not None:con.execute('UPDATE career_send_proposals SET remote_id=? WHERE proposal_id=?',(remote_id,pid))
            if send_requested:con.execute('UPDATE career_send_proposals SET send_requested_at=? WHERE proposal_id=?',(self._now(),pid))
            self._audit(con,pid,status,context,{'error_code':error})

    @staticmethod
    def _verify_draft(draft, proposal):
        body=draft.get('body') or {}
        if (draft.get('isDraft') is not True or addresses(draft.get('toRecipients') or []) != proposal['recipients']
            or draft.get('ccRecipients') or draft.get('bccRecipients') or draft.get('hasAttachments')
            or draft.get('subject')!=proposal['subject'] or body.get('contentType','').lower()!='text'
            or body.get('content')!=proposal['body']):
            raise ConflictError('remote draft differs from exact approved message')

    def execute(self, proposal_id, context):
        context.validate()
        if context.actor_kind not in {'system','user'}:raise ContractError('execution requires trusted runtime')
        proposal=self.get_proposal(proposal_id)
        if proposal['status']!='approved':return proposal
        if not self.outlook:raise ContractError('Outlook unavailable')
        with connect(self.store.db_path) as con:
            if not enabled(con,'outlook_send'):raise ConflictError('Outlook sending is paused')
        self.outlook.preflight_action('outlook_reply_send')
        try:
            _,source=self._source(proposal['application_id'],proposal['evidence_id'])
        except ConflictError:
            self._state(proposal_id,'expired',context,error='source_changed')
            return self.get_proposal(proposal_id)
        if source['source_hash']!=proposal['source_hash']:
            self._state(proposal_id,'expired',context,error='source_changed');return self.get_proposal(proposal_id)
        with connect(self.store.db_path) as con:
            con.execute('BEGIN IMMEDIATE')
            if not enabled(con,'outlook_send'):raise ConflictError('Outlook sending is paused')
            claimed=con.execute("UPDATE career_send_proposals SET status='executing',updated_at=? WHERE proposal_id=? AND status='approved' AND expires_at>?",(self._now(),proposal_id,self._now())).rowcount
            if not claimed:return self.get_proposal(proposal_id)
            self._audit(con,proposal_id,'claimed',context)
        try:
            created=self.outlook.create_reply_draft(proposal['message_id'])
            remote=created.get('id')
            if not isinstance(remote,str) or not remote:raise ConflictError('draft creation returned no identity')
            self._state(proposal_id,'executing',context,remote_id=remote)
            self.outlook.update_reply_draft(remote,proposal['body'])
            self._verify_draft(self.outlook.read_message_body(remote),proposal)
            _,source=self._source(proposal['application_id'],proposal['evidence_id'])
            if source['source_hash']!=proposal['source_hash']:raise ConflictError('source changed while preparing draft')
            with connect(self.store.db_path) as con:
                if not enabled(con,'outlook_send'):raise ConflictError('Outlook sending is paused')
            if proposal['expires_at']<=self._now():raise ConflictError('send approval expired')
            self._validate_offered_availability(proposal)
            # Commit intent BEFORE sending. A crash from here must never resend.
            self._state(proposal_id,'executing',context,send_requested=True)
            self.outlook.send_reply_draft(remote)
            self._state(proposal_id,'accepted',context)
        except Exception as exc:
            current=self.get_proposal(proposal_id)
            self._state(proposal_id,'uncertain' if current['send_requested_at'] else 'failed',context,error=type(exc).__name__)
        return self.get_proposal(proposal_id)

    def observe_sent(self, observation, context):
        context.validate()
        if context.actor_kind!='system':raise ContractError('mail reconciliation requires trusted runtime')
        observation_id=observation.get('observation_id')
        with connect(self.store.db_path) as con:
            con.execute('BEGIN IMMEDIATE')
            observed=con.execute("SELECT * FROM lifecycle_mail_observations WHERE observation_id=? AND direction='outbound'",(observation_id,)).fetchone()
            if not observed or not observed['evidence_id']:return {'matched':False}
            saved=con.execute("SELECT * FROM career_send_proposals WHERE account_id=? AND remote_id=? AND send_requested_at IS NOT NULL AND status IN ('accepted','uncertain','executing')",(observed['account_id'],observed['immutable_message_id'])).fetchone()
            if not saved:return {'matched':False}
            from ..mail.sanitizer import sanitize_mail
            approved=json.loads(saved['payload_json'])
            expected=sanitize_mail(approved['subject'],approved['body'],max_chars=2048)
            evidence=con.execute('SELECT body_sha256 FROM mail_evidence WHERE evidence_id=?',(observed['evidence_id'],)).fetchone()
            recipients=[r.lower() for r in json.loads(observed['recipients_json'])]
            if evidence['body_sha256']!=expected.content_sha256 or sorted(recipients)!=sorted(approved['recipients']):
                con.execute("UPDATE career_send_proposals SET status='uncertain',error_code='sent_content_differs',updated_at=? WHERE proposal_id=?",(self._now(),saved['proposal_id']))
                self._audit(con,saved['proposal_id'],'sent_content_differs',context)
                return {'matched':False,'reason':'sent_content_differs'}
            con.execute("UPDATE career_send_proposals SET status='observed_sent',observed_evidence_id=?,updated_at=? WHERE proposal_id=?",(observed['evidence_id'],self._now(),saved['proposal_id']))
            con.execute('INSERT OR IGNORE INTO lifecycle_mail_links VALUES (?,?,1,\'approved_send\',?)',(observation_id,saved['application_id'],self._now()))
            self._audit(con,saved['proposal_id'],'observed_sent',context,{'evidence_id':observed['evidence_id']})
            for task in con.execute("SELECT task_id FROM lifecycle_tasks WHERE application_id=? AND evidence_id=? AND kind IN ('reply','send_availability') AND status='open'",(saved['application_id'],saved['evidence_id'])).fetchall():
                self.ledger.lifecycle._transition_task(con,task['task_id'],'complete',{'reason':'Observed approved reply in Sent mail','evidence_id':observed['evidence_id'],'source_time':observed['source_at']},context,self._now())
            return {'matched':True,'proposal_id':saved['proposal_id']}

    def reconcile_mail(self, context, *, limit=100):
        if type(limit) is not int or not 1<=limit<=500:raise ContractError('invalid limit')
        with connect(self.store.db_path) as con:
            observations=[dict(r) for r in con.execute("SELECT m.* FROM lifecycle_mail_observations m JOIN career_send_proposals p ON m.account_id=p.account_id AND m.immutable_message_id=p.remote_id WHERE m.direction='outbound' AND p.status IN ('accepted','uncertain','executing') ORDER BY m.updated_at LIMIT ?",(limit,))]
        results=[self.observe_sent(o,context) for o in observations]
        obligations=self.record_reply_obligations(context,limit=limit)
        sources=self.prepare_reply_sources(context,limit=limit) if self.outlook else []
        confirmations=self.reconcile_confirmations(context,limit=limit)
        # Lifecycle state is independent of the calendar write capability. Repair
        # interrupted projections even while external calendar mutations are paused.
        from .lifecycle import project
        with connect(self.store.db_path) as con:
            pending=[r[0] for r in con.execute("SELECT commitment_id FROM career_commitments WHERE status IN ('pending','pending_cancel') ORDER BY created_at LIMIT ?",(limit,))]
        for commitment_id in pending:
            try:project(self,commitment_id)
            except ConflictError:
                with connect(self.store.db_path) as con:
                    con.execute("UPDATE career_commitments SET status='needs_review',version=version+1 WHERE commitment_id=?",(commitment_id,))
        return {'observed':results,'confirmations':confirmations,'sources':sources,'reply_obligations':obligations}

    def run_tick(self, context, *, limit=20):
        # Stale running work is never returned to approved, even if a draft exists.
        cutoff=(parse_utc(self._now())-timedelta(minutes=10)).isoformat().replace('+00:00','Z')
        with connect(self.store.db_path) as con:
            con.execute("UPDATE career_send_proposals SET status='uncertain',error_code='interrupted_execution' WHERE status='executing' AND updated_at<?",(cutoff,))
            con.execute("UPDATE career_send_proposals SET status='expired',updated_at=? WHERE status IN ('pending','approved') AND expires_at<=?",(self._now(),self._now()))
            active=enabled(con,'outlook_send')
        results=[]
        if active:
            for proposal in self.list_proposals(status='approved',limit=limit)['proposals']:
                results.append(self.execute(proposal['proposal_id'],context))
        return {'executions':results,'reconciliation':self.reconcile_mail(context)}

    def prepare_reply(self, application_id, evidence_id, context):
        if not self.reply_provider:return {'status':'missing_information','missing_information':['reply_provider_unavailable']}
        evidence,source=self._source(application_id,evidence_id)
        with connect(self.store.db_path) as con:
            existing=con.execute("SELECT * FROM career_send_proposals WHERE application_id=? AND evidence_id=? AND source_hash=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",(application_id,evidence_id,source['source_hash'],self._now())).fetchone()
            if existing:
                proposal=row(existing)
                return {'status':'READY','proposal_id':proposal['proposal_id'],'proposal':proposal,'missing_information':[]}
            application=dict(self.store._application(con,application_id))
        with connect(self.store.db_path) as con:
            snapshot=con.execute('SELECT source_json,checked_at FROM career_reply_sources WHERE evidence_id=? AND application_id=?',(evidence_id,application_id)).fetchone()
        if snapshot and parse_utc(self._now())-parse_utc(snapshot['checked_at'])<=timedelta(minutes=15):
            frozen=json.loads(snapshot['source_json'])
            if frozen['source_hash']==source['source_hash']:source=frozen
        from .preparation import generate_reply
        prepared=generate_reply(self.reply_provider,application,evidence,source)
        missing=prepared.get('missing_information',[])
        if missing or not prepared.get('body'):
            return {'status':'missing_information','missing_information':missing or ['reply_body']}
        proposal=self.propose_reply(application_id,evidence_id,prepared['body'],context,offered_slots=prepared.get('offered_slots',[]))
        return {'status':'READY','proposal_id':proposal['proposal_id'],'proposal':proposal,'missing_information':[]}

    def prepare_reply_context(self, application_id, evidence_id, context):
        context.validate()
        if context.actor_kind!='system' or not self.outlook:
            raise ContractError('source verification requires the Outlook runtime')
        evidence,source=self._source(application_id,evidence_id)
        from .slots import authored_text
        text=authored_text(evidence['excerpt'])
        with connect(self.store.db_path) as con:
            availability_task=con.execute("SELECT 1 FROM lifecycle_tasks WHERE application_id=? AND evidence_id=? AND kind='send_availability' AND status='open'",(application_id,evidence_id)).fetchone()
        needs_availability=bool(availability_task or re.search(r'(?i)\b(availability|available|what times|when (can|could)|schedule|scheduling)\b',text))
        source['availability_requested']=needs_availability
        source['candidate_slots']=[]
        source['availability_missing_information']=[]
        if needs_availability:
            duration=re.search(r'(?i)\b(\d{1,3})[- ]*(?:minute|min)\b',text)
            minutes=int(duration[1]) if duration else self.availability_policy.default_duration_minutes
            if not 15<=minutes<=240 or minutes%15:
                source['availability_missing_information']=['interview_duration']
            else:
                from ..availability import AvailabilityPlanner
                planned=AvailabilityPlanner(self.outlook,self.availability_policy).propose_slots(now=parse_utc(self._now()),duration_minutes=minutes)
                source['candidate_slots']=[{'starts_at':slot.starts_at,'ends_at':slot.ends_at,'time_zone':slot.timezone_name} for slot in planned]
                source['availability_checked_at']=self._now()
                if not planned:source['availability_missing_information']=['no_free_slots_in_calendar_window']
        with connect(self.store.db_path) as con:
            con.execute('INSERT INTO career_reply_sources VALUES (?,?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET application_id=excluded.application_id,source_json=excluded.source_json,source_hash=excluded.source_hash,checked_at=excluded.checked_at',
                (evidence_id,application_id,canonical_json(source),source['source_hash'],self._now()))
        return {'evidence_id':evidence_id,'source_hash':source['source_hash']}

    def prepare_reply_sources(self, context, *, limit=100):
        with connect(self.store.db_path) as con:
            tasks=[dict(r) for r in con.execute("SELECT DISTINCT application_id,evidence_id FROM lifecycle_tasks WHERE status='open' AND owner='applicant' AND kind IN ('reply','send_availability') AND evidence_id IS NOT NULL LIMIT ?",(limit,))]
        results=[]
        for task in tasks:
            try:results.append(self.prepare_reply_context(task['application_id'],task['evidence_id'],context))
            except Exception as exc:results.append({'evidence_id':task['evidence_id'],'reason':type(exc).__name__})
        return results

    def record_reply_obligations(self, context, *, limit=100):
        from .slots import authored_text
        if context.actor_kind!='system':raise ContractError('mail obligations require trusted runtime')
        with connect(self.store.db_path) as con:
            rows=[dict(r) for r in con.execute("SELECT m.*,l.application_id,e.excerpt FROM lifecycle_mail_observations m JOIN lifecycle_mail_links l USING(observation_id) JOIN mail_evidence e USING(evidence_id) JOIN applications a ON a.application_id=l.application_id WHERE m.direction='inbound' AND a.current_phase<>'terminal' AND NOT EXISTS (SELECT 1 FROM lifecycle_tasks t WHERE t.application_id=l.application_id AND t.evidence_id=m.evidence_id AND t.kind IN ('reply','send_availability')) AND NOT EXISTS (SELECT 1 FROM lifecycle_mail_observations newer WHERE newer.account_id=m.account_id AND newer.conversation_ref=m.conversation_ref AND newer.direction IN ('inbound','outbound') AND newer.source_at>m.source_at) ORDER BY m.source_at DESC LIMIT ?",(limit,))]
        results=[]
        for item in rows:
            body=authored_text(item['excerpt'])
            if '?' not in body and not re.search(r'(?i)\b(please (reply|respond)|let me know|could you|can you)\b',body):continue
            ctx=MutationContext('career-question-'+item['observation_id']+'-'+item['application_id'],'system','outlook_mail',item['observation_id'])
            values={'kind':'reply','owner':'applicant','note':'Recruiter reply requested; review the linked message.','evidence_id':item['evidence_id'],'source_time':item['source_at']}
            def operation(con,stamp):
                return self.ledger.lifecycle._create_task(con,item['application_id'],values,ctx,self._now())
            results.append(self.store._idempotent('career.reply_obligation',ctx,{'application_id':item['application_id'],**values},operation))
        return results

    def _validate_offered_availability(self, proposal):
        from ..availability import AvailabilityPlanner
        planner=AvailabilityPlanner(self.outlook,self.availability_policy)
        for slot in proposal['offered_slots']:
            if not planner.revalidate(slot['starts_at'],slot['ends_at'],now=parse_utc(self._now())):
                raise ConflictError('offered slot is no longer available')
