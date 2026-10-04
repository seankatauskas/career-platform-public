"""Recruiter-confirmed offered slots, with explicit remote ownership boundaries."""
import json
import re
import uuid
from datetime import datetime
from ..contracts import ContractError, ConflictError, MutationContext, canonical_json, parse_utc, utc_now
from ..db import connect


from .slots import authored_text, matched_slots


class CommitmentMixin:
    def reconcile_confirmations(self, context, *, limit=100):
        if context.actor_kind!='system':raise ContractError('confirmation reconciliation requires trusted runtime')
        with connect(self.store.db_path) as con:
            candidates=[dict(r) for r in con.execute("SELECT m.*,p.proposal_id,p.payload_json,p.application_id FROM career_send_proposals p JOIN lifecycle_mail_observations sent ON sent.evidence_id=p.observed_evidence_id AND sent.direction='outbound' JOIN lifecycle_mail_observations m ON m.account_id=p.account_id AND m.conversation_ref=json_extract(p.payload_json,'$.conversation_ref') JOIN lifecycle_mail_links l ON l.observation_id=m.observation_id AND l.application_id=p.application_id WHERE p.status='observed_sent' AND m.direction='inbound' AND m.evidence_id IS NOT NULL AND m.source_at>sent.source_at AND json_array_length(p.payload_json,'$.offered_slots')>0 AND NOT EXISTS (SELECT 1 FROM career_send_proposals newer JOIN lifecycle_mail_observations ns ON ns.evidence_id=newer.observed_evidence_id WHERE newer.application_id=p.application_id AND newer.account_id=p.account_id AND newer.status='observed_sent' AND json_extract(newer.payload_json,'$.conversation_ref')=m.conversation_ref AND json_array_length(newer.payload_json,'$.offered_slots')>0 AND ns.source_at>sent.source_at AND ns.source_at<m.source_at) AND NOT EXISTS (SELECT 1 FROM career_commitments c WHERE c.proposal_id=p.proposal_id AND c.confirmation_evidence_id=m.evidence_id) AND NOT EXISTS (SELECT 1 FROM career_confirmation_checks k WHERE k.proposal_id=p.proposal_id AND k.evidence_id=m.evidence_id) ORDER BY m.source_at LIMIT ?",(limit,))]
        results=[]
        for item in candidates:
            def ignored():
                with connect(self.store.db_path) as con:
                    con.execute('INSERT OR IGNORE INTO career_confirmation_checks VALUES (?,?,?)',(item['proposal_id'],item['evidence_id'],self._now()))
            proposal=json.loads(item['payload_json']); slots=proposal['offered_slots']
            if not slots or item['conversation_ref']!=proposal['conversation_ref'] or item['sender'].lower() not in proposal['recipients']:
                ignored();continue
            evidence=self.store.get_mail_evidence(item['evidence_id'])
            text=evidence['excerpt']
            # Do not accept quoted offers, questions, negative or merely tentative answers.
            authored=authored_text(text)
            affirmative=bool(re.search(r'\b(confirmed|scheduled|see you|that works|works for me|works)\b',authored,re.I))
            negative=bool(re.search(r'\b(?:not|cannot|can.t|won.t|doesn.t|don.t|unable|unavailable|tentative|cancel(?:led|ed|ling|ing)?|reschedul\w*|maybe)\b|\?',authored,re.I))
            if not affirmative and not negative:
                ignored();continue
            matched=matched_slots(authored,slots)
            with connect(self.store.db_path) as con:
                newer=con.execute("SELECT 1 FROM lifecycle_mail_observations WHERE account_id=? AND conversation_ref=? AND direction='inbound' AND source_at>? LIMIT 1",(item['account_id'],item['conversation_ref'],item['source_at'])).fetchone()
            if newer:
                ignored();continue
            with connect(self.store.db_path) as con:
                prior=con.execute("SELECT * FROM career_commitments WHERE proposal_id=? AND round_id IS NOT NULL AND status IN ('created','linked_invite','pending','needs_review') ORDER BY created_at DESC LIMIT 2",(item['proposal_id'],)).fetchall()
            clear_cancel=bool(re.search(r'(?i)\b(cancelled|canceled|cancelling|canceling)\b',authored)) and not re.search(r'(?i)\b(not|reschedul\w*|maybe)\b|\?',authored) and len(prior)==1
            status='pending_cancel' if clear_cancel else 'pending' if affirmative and not negative and len(matched)==1 else 'needs_review'
            slot={'starts_at':prior[0]['starts_at'],'ends_at':prior[0]['ends_at']} if clear_cancel else matched[0] if len(matched)==1 else slots[0]
            cid=uuid.uuid4().hex
            with connect(self.store.db_path) as con:
                con.execute('INSERT OR IGNORE INTO career_confirmation_checks VALUES (?,?,?)',(item['proposal_id'],item['evidence_id'],self._now()))
                con.execute('INSERT OR IGNORE INTO career_commitments (commitment_id,proposal_id,confirmation_evidence_id,starts_at,ends_at,status,transaction_id,organizer,details_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (cid,item['proposal_id'],item['evidence_id'],slot['starts_at'],slot['ends_at'],status,'career-'+cid,item['sender'].lower(),canonical_json({'reason':'' if status in {'pending','pending_cancel'} else 'ambiguous_recruiter_confirmation',**confirmed_details(authored)}),self._now(),self._now()))
            if status in {'pending','pending_cancel'}:
                from .lifecycle import project
                try:project(self,cid)
                except ConflictError:
                    status='needs_review'
                    with connect(self.store.db_path) as con:
                        con.execute("UPDATE career_commitments SET status='needs_review',version=version+1 WHERE commitment_id=?",(cid,))
            results.append({'commitment_id':cid,'status':status})
        return results

    def list_commitments(self, *, application_id=None, limit=100):
        if type(limit) is not int or not 1<=limit<=500:raise ContractError('invalid limit')
        with connect(self.store.db_path) as con:
            sql='SELECT c.*,p.application_id FROM career_commitments c JOIN career_send_proposals p USING(proposal_id)'
            params=[]
            if application_id:sql+=' WHERE p.application_id=?';params.append(application_id)
            return {'commitments':[dict(r) for r in con.execute(sql+' ORDER BY c.created_at DESC LIMIT ?',(*params,limit))]}

    def reconcile_commitments(self, context, *, limit=20):
        from .service import enabled
        if type(limit) is not int or not 1<=limit<=500:raise ContractError('invalid limit')
        context.validate()
        if context.actor_kind!='system':raise ContractError('calendar reconciliation requires trusted runtime')
        if not self.outlook:return {'status':'unavailable','results':[]}
        with connect(self.store.db_path) as con:
            if not enabled(con,'calendar_commitments'):return {'status':'paused','results':[]}
            pending=[dict(r) for r in con.execute("SELECT c.*,p.application_id,p.account_id,p.payload_json FROM career_commitments c JOIN career_send_proposals p USING(proposal_id) WHERE c.status IN ('pending','pending_cancel','created','linked_invite') AND p.status='observed_sent' ORDER BY c.updated_at LIMIT ?",(limit,))]
        results=[]
        for item in pending:
            try:
                from .lifecycle import project
                if item['status'] in {'pending','pending_cancel'}:project(self,item['commitment_id'])
                results.append(self._reconcile_commitment(item,context))
                project(self,item['commitment_id'])
                with connect(self.store.db_path) as con:
                    con.execute('UPDATE career_commitments SET updated_at=? WHERE commitment_id=?',(self._now(),item['commitment_id']))
            except Exception as exc:
                # No speculative writes after missing event, version conflict, or outage.
                with connect(self.store.db_path) as con:
                    con.execute("UPDATE career_commitments SET status='needs_review',details_json=?,updated_at=?,version=version+1 WHERE commitment_id=?",(canonical_json({'reason':type(exc).__name__}),self._now(),item['commitment_id']))
                results.append({'commitment_id':item['commitment_id'],'status':'needs_review'})
        return {'status':'checked','results':results}

    def _reconcile_commitment(self,item,context):
        from .service import enabled
        if item['account_id']!=self.account_id:raise ConflictError('calendar account mismatch')
        with connect(self.store.db_path) as con:
            app=self.store._application(con,item['application_id'])
            links=con.execute('SELECT 1 FROM lifecycle_mail_links l JOIN lifecycle_mail_observations m USING(observation_id) WHERE m.evidence_id=? AND l.application_id=? AND m.direction=\'inbound\'',(item['confirmation_evidence_id'],item['application_id'])).fetchone()
            if not links:raise ConflictError('confirmation association changed')
            confirmation=con.execute('SELECT * FROM lifecycle_mail_observations WHERE evidence_id=?',(item['confirmation_evidence_id'],)).fetchone()
            if con.execute("SELECT 1 FROM lifecycle_mail_observations WHERE account_id=? AND conversation_ref=? AND direction='inbound' AND source_at>? LIMIT 1",(confirmation['account_id'],confirmation['conversation_ref'],confirmation['source_at'])).fetchone():
                raise ConflictError('newer recruiter reply requires review')
            if app['current_phase']=='terminal' and item['status']!='pending_cancel':raise ConflictError('application terminal; calendar cleanup requires review')
        if not item['remote_id']:
            with connect(self.store.db_path) as con:
                con.execute('BEGIN IMMEDIATE')
                prior=con.execute("SELECT * FROM career_commitments WHERE proposal_id=? AND commitment_id<>? AND owned=1 AND status NOT IN ('superseded','cancelled','dismissed')",(item['proposal_id'],item['commitment_id'])).fetchall()
                if len(prior)>1:raise ConflictError('ambiguous prior app commitment')
                if prior:
                    previous=prior[0]
                    item.update(remote_id=previous['remote_id'],etag=previous['etag'],transaction_id=previous['transaction_id'],owned=1,round_id=previous['round_id'])
                    con.execute('UPDATE career_commitments SET remote_id=?,etag=?,transaction_id=?,round_id=?,owned=1,version=version+1 WHERE commitment_id=?',(item['remote_id'],item['etag'],item['transaction_id'],item['round_id'],item['commitment_id']))
                    con.execute("UPDATE career_commitments SET status='superseded',version=version+1 WHERE commitment_id=?",(previous['commitment_id'],))
        # Existing employer-owned invite wins. Never change or RSVP that event.
        events=list(self.outlook.read_interview_events(item['starts_at'],item['ends_at'])) if item['status']!='pending_cancel' else []
        invites=[e for e in events if e.get('organizer','').lower()==item['organizer'] and e.get('is_organizer') is False and not e.get('is_cancelled') and e.get('starts_at')==item['starts_at'] and e.get('ends_at')==item['ends_at']]
        if len(invites)>1:raise ConflictError('ambiguous employer invitations')
        if item['status']=='linked_invite':
            event=self.outlook.read_interview_event(item['remote_id'])
            if event.get('is_organizer') is not False or event.get('organizer','').lower()!=item['organizer']:raise ConflictError('organizer ownership changed')
            previous=json.loads(item['details_json']).get('calendar_modified_at','')
            modified=event.get('modified_at','')
            if previous and modified<previous:return {'commitment_id':item['commitment_id'],'status':'linked_invite'}
            if previous==modified and event.get('change_key')!=item['etag']:raise ConflictError('divergent calendar revision')
            status='cancelled' if event.get('is_cancelled') else 'linked_invite'
            with connect(self.store.db_path) as con:
                con.execute('UPDATE career_commitments SET status=?,starts_at=?,ends_at=?,etag=?,details_json=?,updated_at=?,version=version+1 WHERE commitment_id=?',(status,event['starts_at'],event['ends_at'],event['change_key'],canonical_json({'calendar_modified_at':modified}),self._now(),item['commitment_id']))
            return {'commitment_id':item['commitment_id'],'status':status,'event':event}
        owned=None
        if item['remote_id']:
            owned=self.outlook.read_owned_event(item['remote_id'])
            if owned.get('transactionId')!=item['transaction_id'] or owned.get('attendees') or owned.get('sensitivity')!='private' or owned.get('isOrganizer') is not True:
                raise ConflictError('app calendar ownership lost')
            if owned.get('changeKey')!=item['etag']:raise ConflictError('owned event changed outside application')
        if item['status']=='pending_cancel':
            if not owned:raise ConflictError('only owned commitments can be removed')
            self._calendar_allowed()
            self.outlook.delete_private_commitment(item['remote_id'],owned.get('@odata.etag'))
            self._save_commitment(item['commitment_id'],'cancelled',item['remote_id'],item['etag'],[])
            return {'commitment_id':item['commitment_id'],'status':'cancelled'}
        if invites:
            if owned:
                self._calendar_allowed()
                self.outlook.delete_private_commitment(item['remote_id'],owned.get('@odata.etag'))
            event=invites[0]
            self._save_commitment(item['commitment_id'],'linked_invite',event['remote_id'],event['change_key'],[])
            with connect(self.store.db_path) as con:
                con.execute('UPDATE career_commitments SET details_json=? WHERE commitment_id=?',(canonical_json({'calendar_modified_at':event['modified_at']}),item['commitment_id']))
            return {'commitment_id':item['commitment_id'],'status':'linked_invite'}
        if owned:
            from ..outlook.calendar import _graph_time
            if _graph_time(owned.get('start'))!=item['starts_at'] or _graph_time(owned.get('end'))!=item['ends_at']:
                if parse_utc(item['starts_at'])<=parse_utc(self._now()):raise ConflictError('confirmed slot is past')
                conflicts=self._calendar_conflicts(item,exclude={item['remote_id']})
                self._calendar_allowed()
                result=self.outlook.write_private_commitment(item['starts_at'],item['ends_at'],item['transaction_id'],remote_id=item['remote_id'],etag=owned.get('@odata.etag'),**calendar_details(item))
                self._save_commitment(item['commitment_id'],'created',item['remote_id'],result['changeKey'],conflicts)
            elif item['status']!='created':
                self._save_commitment(item['commitment_id'],'created',item['remote_id'],item['etag'],json.loads(item['conflict_json']))
            return {'commitment_id':item['commitment_id'],'status':'created'}
        if parse_utc(item['starts_at'])<=parse_utc(self._now()):raise ConflictError('confirmed slot is no longer future')
        conflicts=self._calendar_conflicts(item)
        # Exact app-owned legacy hold can be upgraded only after identity + version checks.
        hold=None
        with connect(self.store.db_path) as con:
            rows=con.execute("SELECT e.remote_id,p.action_id,p.remote_idempotency_key,p.payload_json FROM action_executions e JOIN action_proposals p USING(action_id) WHERE p.application_id=? AND p.account_id=? AND p.kind='calendar_tentative_hold' AND e.status='succeeded'",(item['application_id'],self.account_id)).fetchall()
        for saved in rows:
            payload=json.loads(saved['payload_json'])
            if payload.get('starts_at')==item['starts_at'] and payload.get('ends_at')==item['ends_at']:
                if hold:raise ConflictError('ambiguous app holds')
                hold=dict(saved)
        self._calendar_allowed()
        if hold:
            current=self.outlook.read_owned_event(hold['remote_id'])
            if current.get('attendees') or current.get('isOrganizer') is not True or current.get('sensitivity')!='private' or current.get('showAs')!='tentative':raise ConflictError('hold ownership changed')
            # Legacy executor uses action ID as transaction ID; assert provenance.
            if current.get('transactionId')!=hold['remote_idempotency_key']:raise ConflictError('hold transaction mismatch')
            result=self.outlook.write_private_commitment(item['starts_at'],item['ends_at'],hold['remote_idempotency_key'],remote_id=hold['remote_id'],etag=current.get('@odata.etag'),**calendar_details(item))
            with connect(self.store.db_path) as con:con.execute('UPDATE career_commitments SET transaction_id=? WHERE commitment_id=?',(hold['remote_idempotency_key'],item['commitment_id']))
            conflicts=[v for v in conflicts if v!=hold['remote_id']]
        else:
            result=self.outlook.write_private_commitment(item['starts_at'],item['ends_at'],item['transaction_id'],**calendar_details(item))
        if not result.get('id') or not result.get('changeKey'):raise ConflictError('calendar write missing identity')
        self._save_commitment(item['commitment_id'],'created',result['id'],result['changeKey'],conflicts)
        # Local commitment alone never becomes employer-confirmation evidence.
        return {'commitment_id':item['commitment_id'],'status':'created','conflicts':conflicts}

    def _calendar_conflicts(self,item,*,exclude=()):
        blocks=list(self.outlook.read_calendar_view(item['starts_at'],item['ends_at']))
        return [b.remote_id for b in blocks if b.remote_id not in exclude and not b.is_cancelled and b.show_as not in {'free','workingElsewhere'} and b.starts_at<item['ends_at'] and b.ends_at>item['starts_at']]

    def _calendar_allowed(self):
        from .service import enabled
        with connect(self.store.db_path) as con:
            if not enabled(con,'calendar_commitments'):raise ConflictError('calendar commitments paused')

    def _save_commitment(self,cid,status,remote_id,etag,conflicts):
        with connect(self.store.db_path) as con:
            con.execute('UPDATE career_commitments SET status=?,remote_id=?,etag=?,conflict_json=?,updated_at=?,owned=?,version=version+1 WHERE commitment_id=?',(status,remote_id,etag,canonical_json(conflicts),self._now(),int(status=='created'),cid))

    def _propose_invite(self,item,event,context):
        details={'status':'confirmed','starts_at':event['starts_at'],'ends_at':event['ends_at'],'time_zone':'UTC','evidence_id':item['confirmation_evidence_id'],
            'calendar_account_id':self.account_id,'calendar_event_id':event['remote_id'],'calendar_uid':event.get('ical_uid',''),
            'calendar_modified_at':event['modified_at'],'calendar_change_key':event['change_key'],'organizer':item['organizer']}
        with connect(self.store.db_path) as con:
            existing=con.execute('SELECT round_id FROM interview_rounds WHERE calendar_account_id=? AND calendar_event_id=?',(self.account_id,event['remote_id'])).fetchone()
        if existing:details['round_id']=existing['round_id']
        self.ledger.lifecycle.propose_interview_revision(item['application_id'],details,MutationContext('career-invite-'+item['commitment_id'],'system','outlook_calendar',event['remote_id']))

    def review_commitment(self, commitment_id, decision, context, *, expected_version, starts_at=None, ends_at=None):
        """Resolve ambiguous mail using an explicit user decision; writes remain paused-gated."""
        from .service import audit
        context.validate()
        if context.actor_kind!='user' or decision not in {'confirmed','cancelled','dismissed'}:
            raise ContractError('calendar review requires explicit user confirmation')
        if type(expected_version) is not int:raise ContractError('calendar version is required')
        request={'id':commitment_id,'decision':decision,'version':expected_version,'start':starts_at,'end':ends_at}
        def operation(con,stamp):
            saved=con.execute('SELECT c.*,p.payload_json,p.status AS send_status FROM career_commitments c JOIN career_send_proposals p USING(proposal_id) WHERE commitment_id=?',(commitment_id,)).fetchone()
            if not saved:raise ContractError('commitment not found')
            if saved['version']!=expected_version:raise ConflictError('calendar review is stale')
            if saved['status'] in {'linked_invite','cancelled','dismissed'}:raise ConflictError('commitment cannot be edited')
            if saved['send_status']!='observed_sent':raise ConflictError('offered reply has not been observed in Sent')
            payload=json.loads(saved['payload_json'])
            start,end=starts_at or saved['starts_at'],ends_at or saved['ends_at']
            if decision=='confirmed':
                if not any(s['starts_at']==start and s['ends_at']==end for s in payload['offered_slots']):
                    raise ContractError('confirmation must match an observed-sent offered slot')
                if parse_utc(start)<=parse_utc(self._now()):raise ConflictError('confirmed slot is past')
            state='pending' if decision=='confirmed' else 'pending_cancel' if decision=='cancelled' else 'dismissed'
            con.execute('UPDATE career_commitments SET status=?,starts_at=?,ends_at=?,updated_at=?,version=version+1 WHERE commitment_id=?',(state,start,end,self._now(),commitment_id))
            self._audit(con,saved['proposal_id'],'calendar_'+decision,context,request)
            return dict(con.execute('SELECT * FROM career_commitments WHERE commitment_id=?',(commitment_id,)).fetchone())
        return self.store._idempotent('career.review_commitment',context,request,operation)


def confirmed_details(text):
    from urllib.parse import urlsplit
    location=re.search(r'(?im)^location:\s*([^\n]{1,1000})',text)
    meeting=re.search(r'(?im)^(?:join(?: meeting)?|meeting (?:link|url)):\s*(https://\S{1,2000})',text)
    url=meeting[1].rstrip('.,)>') if meeting else ''
    if url:
        parsed=urlsplit(url)
        if not parsed.hostname or '.' not in parsed.hostname or parsed.username or parsed.password:
            url=''
    return {'location':location[1].strip() if location else '', 'join_url':url}


def calendar_details(item):
    details=json.loads(item['details_json'])
    return {k:details.get(k,'') for k in ('location','join_url')}
