"""Exact, one-use reviews bound to authenticated Telegram transport events.

This service is not an LLM capability. Only the dedicated ingress may call ingest;
the dashboard supplies its own authenticated MutationContext for user commands.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from ..contracts import ContractError, ConflictError, MutationContext, canonical_json, payload_sha256, parse_utc
from ..db import connect


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _numeric(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}', value):
        raise ContractError('invalid ' + name)
    return value


class InteractionsService:
    def __init__(self, ledger, *, actions=None, attention=None, identity=None, now_provider=None):
        self.ledger = ledger
        self.path = ledger.store.db_path
        self._actions = actions
        self._attention = attention
        self.identity = dict(identity or {})
        self.now = now_provider or (lambda: datetime.now(timezone.utc))
        if self.identity:
            if set(self.identity) != {'bot_id', 'user_id', 'chat_id'}:
                raise ContractError('interaction identity requires bot, user, and private chat')
            for key, value in self.identity.items():
                _numeric(value, key)

    @property
    def actions(self):
        return self._actions or self.ledger.career_actions

    @property
    def attention(self):
        return self._attention or self.ledger.attention

    def issue(self, kind, target_id, review, *, payload_hash, source_version, context, notification_id=None):
        if context.actor_kind not in ('system', 'user'):
            raise ContractError('review delivery requires a trusted caller')
        if not self.identity:
            raise ContractError('Telegram interactions are not configured')
        if kind not in ('send', 'attention') or not isinstance(review, dict):
            raise ContractError('invalid review')
        if not re.fullmatch(r'[0-9a-f]{64}', payload_hash):
            raise ContractError('invalid review payload hash')
        if len(canonical_json(review).encode()) > 24000:
            raise ContractError('review is too large')
        stamp = _stamp(self.now())
        expires = _stamp(self.now() + timedelta(minutes=15))
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            if notification_id:
                linked = con.execute('SELECT * FROM interaction_tickets WHERE notification_id=?',(notification_id,)).fetchone()
                if linked:
                    return self._view(linked)
            existing = con.execute("SELECT * FROM interaction_tickets WHERE kind=? AND target_id=? AND payload_sha256=? AND source_version=? AND bot_id=? AND user_id=? AND chat_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                (kind,target_id,payload_hash,str(source_version),self.identity['bot_id'],self.identity['user_id'],self.identity['chat_id'],stamp)).fetchone()
            if existing and existing['notification_id'] == notification_id:
                return self._view(existing)
            con.execute("UPDATE interaction_tickets SET status='cancelled' WHERE kind=? AND target_id=? AND status='pending'", (kind,target_id))
            ticket_id = secrets.token_hex(16)
            con.execute('INSERT INTO interaction_tickets(ticket_id,kind,target_id,payload_sha256,source_version,bot_id,user_id,chat_id,review_json,created_at,expires_at,notification_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (ticket_id,kind,target_id,payload_hash,str(source_version),self.identity['bot_id'],self.identity['user_id'],self.identity['chat_id'],canonical_json(review),stamp,expires,notification_id))
            return self._view(con.execute('SELECT * FROM interaction_tickets WHERE ticket_id=?',(ticket_id,)).fetchone())

    @staticmethod
    def _view(row):
        result = dict(row)
        result['review'] = json.loads(result.pop('review_json'))
        return result

    def pending_reviews(self, limit=10):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 25:
            raise ContractError('invalid review limit')
        if not self.identity:
            return {'items': []}
        with connect(self.path) as con:
            rows = con.execute("SELECT * FROM interaction_tickets WHERE status='pending' AND delivery_state='queued' AND message_id IS NULL AND expires_at>? AND bot_id=? AND user_id=? AND chat_id=? ORDER BY created_at,ticket_id LIMIT ?",
                (_stamp(self.now()),self.identity['bot_id'],self.identity['user_id'],self.identity['chat_id'],limit)).fetchall()
        return {'items':[self._view(row) for row in rows]}

    def claim_delivery(self,ticket_id):
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM interaction_tickets WHERE ticket_id=?',(ticket_id,)).fetchone()
            if not row or any(row[k] != self.identity.get(k) for k in ('bot_id','user_id','chat_id')):
                raise ContractError('review not found')
            if row['delivery_state'] != 'queued':
                return {'send_allowed':False,'state':row['delivery_state']}
            if row['status']!='pending' or row['expires_at'] <= _stamp(self.now()):
                con.execute("UPDATE interaction_tickets SET delivery_state='cancelled' WHERE ticket_id=?",(ticket_id,))
                return {'send_allowed':False,'state':'cancelled'}
            if row['notification_id']:
                notification = con.execute('SELECT * FROM notification_outbox WHERE notification_id=?',(row['notification_id'],)).fetchone()
                if not notification or notification['status'] not in ('pending','delivering'):
                    con.execute("UPDATE interaction_tickets SET delivery_state='cancelled' WHERE ticket_id=?",(ticket_id,))
                    return {'send_allowed':False,'state':'cancelled'}
                if not self.attention.validate_delivery(con,dict(notification),_stamp(self.now())):
                    return {'send_allowed':False,'state':'deferred'}
            elif row['kind']=='send':
                # Read the exact immutable proposal before exposing its review.
                proposal = self.actions.get_proposal(row['target_id'])
                if proposal['status']!='pending' or proposal['payload_hash']!=row['payload_sha256'] or proposal['source_hash']!=row['source_version']:
                    con.execute("UPDATE interaction_tickets SET delivery_state='cancelled' WHERE ticket_id=?",(ticket_id,))
                    return {'send_allowed':False,'state':'cancelled'}
            con.execute("UPDATE interaction_tickets SET delivery_state='claimed',delivery_started_at=?,delivery_revision=delivery_revision+1 WHERE ticket_id=?",(_stamp(self.now()),ticket_id))
            return {'send_allowed':True,'state':'claimed'}

    def delivery_unknown(self,ticket_id):
        with connect(self.path) as con:
            row = con.execute('SELECT * FROM interaction_tickets WHERE ticket_id=?',(ticket_id,)).fetchone()
            if not row or any(row[k] != self.identity.get(k) for k in ('bot_id','user_id','chat_id')):
                raise ContractError('review not found')
            con.execute("UPDATE interaction_tickets SET delivery_state='unknown',delivery_revision=delivery_revision+1 WHERE ticket_id=? AND delivery_state='claimed'",(ticket_id,))
        return {'state':'unknown'}

    def mark_delivered(self, ticket_id, message_id):
        _numeric(message_id,'message_id')
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM interaction_tickets WHERE ticket_id=?',(ticket_id,)).fetchone()
            if not row or any(row[k] != self.identity.get(k) for k in ('bot_id','user_id','chat_id')):
                raise ContractError('review not found')
            if row['message_id'] and row['message_id'] != message_id:
                raise ConflictError('review already has a delivery receipt')
            if row['delivery_state'] not in ('claimed','unknown','sent') and not (row['delivery_state']=='cancelled' and row['delivery_started_at']):
                raise ConflictError('review has no durable delivery intent')
            if row['message_id']==message_id and row['delivery_state']=='sent':
                return {'delivered':True}
            con.execute("UPDATE interaction_tickets SET message_id=?,delivery_state='sent',delivery_revision=delivery_revision+1 WHERE ticket_id=?",(message_id,ticket_id))
            if row['notification_id']:
                # A late receipt records truth without resurrecting a cancelled alert.
                con.execute("UPDATE notification_outbox SET status='delivered',delivered_at=?,last_error='',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE notification_id=? AND (status IN ('pending','delivering') OR (status='dead' AND last_error='delivery_reconciliation_required'))",(_stamp(self.now()),row['notification_id']))
        return {'delivered':True}

    def list_delivery_recovery(self,limit=25,offset=0):
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:
            raise ContractError('invalid delivery recovery page')
        cutoff=_stamp(self.now()-timedelta(minutes=2))
        with connect(self.path) as con:
            rows=con.execute("SELECT t.*,n.status notification_status,n.last_error notification_error FROM interaction_tickets t LEFT JOIN notification_outbox n USING(notification_id) WHERE t.status IN ('pending','processing') AND (t.delivery_state='unknown' OR (t.delivery_state='claimed' AND t.delivery_started_at<?)) ORDER BY t.created_at,t.ticket_id LIMIT ? OFFSET ?",(cutoff,limit+1,offset)).fetchall()
        items=[]
        for row in rows[:limit]:
            review=json.loads(row['review_json'])
            items.append({**{k:row[k] for k in ('ticket_id','kind','target_id','payload_sha256','source_version','delivery_revision','notification_id','notification_status','notification_error','delivery_state','delivery_started_at')},'identity':{k:row[k] for k in ('bot_id','user_id','chat_id')},'title':review.get('subject') or review.get('title') or 'Career review'})
        return {'items':items,'complete':len(rows)<=limit,'next_offset':offset+limit if len(rows)>limit else None}

    def reconcile_delivery(self,values,context):
        required={'ticket_id','decision','expected_revision','payload_sha256','source_version','identity'}
        if context.actor_kind!='user' or not isinstance(values,dict) or set(values)!=required:
            raise ContractError('delivery recovery requires an exact trusted user decision')
        if values['decision'] not in ('received','abandon') or type(values['expected_revision']) is not int:
            raise ContractError('invalid delivery recovery decision')
        digest=payload_sha256(values)
        stamp=_stamp(self.now())
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            prior=con.execute('SELECT * FROM interaction_delivery_decisions WHERE idempotency_key=?',(context.idempotency_key,)).fetchone()
            if prior:
                if prior['request_sha256']!=digest:raise ConflictError('recovery command changed')
                return json.loads(prior['result_json'])
            row=con.execute('SELECT * FROM interaction_tickets WHERE ticket_id=?',(values['ticket_id'],)).fetchone()
            if not row:raise ContractError('review not found')
            identity={k:row[k] for k in ('bot_id','user_id','chat_id')}
            if identity!=self.identity or identity!=values['identity'] or row['payload_sha256']!=values['payload_sha256'] or row['source_version']!=values['source_version'] or row['delivery_revision']!=values['expected_revision']:
                raise ConflictError('review identity or version changed; refresh before deciding')
            if row['status'] not in ('pending','processing') or not (row['delivery_state']=='unknown' or (row['delivery_state']=='claimed' and row['delivery_started_at']<_stamp(self.now()-timedelta(minutes=2)))):
                raise ConflictError('delivery no longer requires reconciliation')
            received=values['decision']=='received'
            if row['notification_id']:
                notification=con.execute('SELECT * FROM notification_outbox WHERE notification_id=?',(row['notification_id'],)).fetchone()
                if notification and notification['status']=='dead' and notification['last_error']!='delivery_reconciliation_required':
                    raise ConflictError('notification has a different terminal outcome')
                if notification and notification['status']!='cancelled':
                    con.execute("UPDATE notification_outbox SET status=?,delivered_at=?,last_error=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE notification_id=? AND (status IN ('pending','delivering') OR (status='dead' AND last_error='delivery_reconciliation_required'))",('delivered' if received else 'cancelled',stamp if received else None,'' if received else 'interaction delivery abandoned by user',row['notification_id']))
            con.execute('UPDATE interaction_tickets SET delivery_state=?,status=?,delivery_revision=delivery_revision+1 WHERE ticket_id=?',('sent' if received else 'cancelled',row['status'] if received else 'cancelled',row['ticket_id']))
            result={'ticket_id':row['ticket_id'],'decision':values['decision'],'delivery_revision':row['delivery_revision']+1,'message':'Receipt recorded. This does not approve an email; use the dashboard for any remaining decision.' if received else 'Delivery abandoned. It will not be resent.'}
            con.execute('INSERT INTO interaction_delivery_decisions VALUES (?,?,?,?,?,?,?,?,?,?)',(secrets.token_hex(16),context.idempotency_key,digest,row['ticket_id'],values['decision'],row['delivery_revision'],'user',context.source_ref,canonical_json(result),stamp))
            return result

    def _envelope(self, value):
        allowed = {'bot_id','user_id','chat_id','chat_type','update_id','message_id','reply_to_message_id','callback_id','ticket_id','command','until','text','forwarded','edited','is_bot'}
        if not isinstance(value,dict) or set(value)-allowed or len(canonical_json(value).encode()) > 8192:
            raise ContractError('invalid interaction envelope')
        if not self.identity or any(value.get(k) != v for k,v in self.identity.items()):
            raise ContractError('interaction sender is not authorized')
        if value.get('chat_type') != 'private' or any(value.get(k) for k in ('forwarded','edited','is_bot')):
            raise ContractError('only original private human messages can authorize an action')
        for key in ('update_id','message_id'):
            _numeric(value.get(key),key)
        if value.get('reply_to_message_id') is not None:
            _numeric(value['reply_to_message_id'],'reply_to_message_id')
        return dict(value)

    def ingest(self, envelope):
        envelope = self._envelope(envelope)
        command_id = 'telegram:' + envelope['bot_id'] + ':' + envelope['update_id']
        digest = payload_sha256(envelope)
        stamp = _stamp(self.now())
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            saved = con.execute('SELECT * FROM interaction_commands WHERE command_id=?',(command_id,)).fetchone()
            if saved and saved['envelope_sha256'] != digest:
                raise ConflictError('Telegram update was already recorded with different content')
            if saved and saved['status'] == 'completed':
                return json.loads(saved['result_json'])
            if not saved:
                con.execute("INSERT INTO interaction_commands(command_id,envelope_sha256,envelope_json,status,created_at) VALUES (?,?,?,'pending',?)",(command_id,digest,canonical_json(envelope),stamp))
        return self._process(command_id,envelope)

    def _finish(self, command_id, result, ticket_id=None):
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("UPDATE interaction_commands SET status='completed',result_json=?,completed_at=?,ticket_id=? WHERE command_id=?",(canonical_json(result),_stamp(self.now()),ticket_id,command_id))
            if ticket_id:
                con.execute("UPDATE interaction_tickets SET status='consumed' WHERE ticket_id=? AND command_id=?",(ticket_id,command_id))
        return result

    def _process(self, command_id, envelope):
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            received_at=con.execute('SELECT created_at FROM interaction_commands WHERE command_id=?',(command_id,)).fetchone()[0]
            # A callback's message is the original review. Text must reply directly to it.
            message = envelope['message_id'] if envelope.get('callback_id') else envelope.get('reply_to_message_id')
            row = con.execute('SELECT * FROM interaction_tickets WHERE bot_id=? AND chat_id=? AND message_id=?',
                (envelope['bot_id'],envelope['chat_id'],message)).fetchone() if message else None
            error = None
            if not row or row['user_id'] != envelope['user_id'] or (envelope.get('ticket_id') and envelope['ticket_id'] != row['ticket_id']):
                error = 'Reply directly to the review you want to act on, or use its buttons.'
            elif row['status'] not in ('pending','processing') or (row['command_id'] and row['command_id'] != command_id):
                error = 'This review has already been resolved. Ask for a fresh review.'
            elif row['expires_at'] <= _stamp(self.now()) and row['command_id'] != command_id:
                error = 'This review has expired. Ask for a fresh review.'
            ticket = dict(row) if row else None
            command = envelope.get('command')
            if not command:
                phrase = re.sub(r'[.!]+$', '',str(envelope.get('text','')).strip().casefold())
                command = {'yes send it':'approve','send it':'approve','yes':'approve','no':'reject','cancel':'reject','got it':'ack','ack':'ack','acknowledge':'ack','snooze 1h':'snooze','snooze 1 hour':'snooze'}.get(phrase)
            allowed = ('approve','reject') if ticket and ticket['kind'] == 'send' else ('ack','snooze')
            if command not in allowed:
                error = 'Use Send / Cancel for this email, or acknowledge / snooze for a reminder. Changes need a new review.'
            if not error:
                con.execute("UPDATE interaction_tickets SET status='processing',command_id=? WHERE ticket_id=?",(command_id,ticket['ticket_id']))
        if error:
            return self._finish(command_id,{'status':'clarification','message':error})
        context = MutationContext(command_id,'user','telegram_interaction',source_ref=envelope['user_id']+':'+envelope['chat_id'])
        try:
            if ticket['kind'] == 'send':
                result = self.actions.decide_proposal(ticket['target_id'], 'approved' if command == 'approve' else 'rejected', ticket['payload_sha256'], context, expected_source_hash=ticket['source_version'])
                message = 'Approved for sending.' if command == 'approve' else 'Cancelled.'
            elif command == 'ack':
                result = self.attention.acknowledge(ticket['target_id'],context,expected_revision=int(ticket['source_version']))
                message = 'Acknowledged. The underlying obligation is unchanged.'
            else:
                until = envelope.get('until') or _stamp(parse_utc(received_at)+timedelta(hours=1))
                end = parse_utc(until)
                if end <= self.now() or end > self.now()+timedelta(days=30):
                    raise ContractError('snooze must end within the next 30 days')
                result = self.attention.snooze(ticket['target_id'],until,context,expected_revision=int(ticket['source_version']))
                message = 'Snoozed until '+until+'.'
        except (ContractError,ConflictError) as exc:
            return self._finish(command_id,{'status':'rejected','message':str(exc)[:400]},ticket['ticket_id'])
        return self._finish(command_id,{'status':'completed','message':message,'result':result},ticket['ticket_id'])

    def resume_pending(self, limit=25):
        with connect(self.path) as con:
            rows = con.execute("SELECT command_id,envelope_json FROM interaction_commands WHERE status='pending' ORDER BY created_at LIMIT ?",(max(1,min(int(limit),25)),)).fetchall()
        return [self.ingest(json.loads(row['envelope_json'])) for row in rows]

    def review_action(self, proposal_id, context):
        proposal = self.actions.get_proposal(proposal_id)
        if proposal['status'] != 'pending':
            raise ConflictError('only pending proposals can be reviewed')
        review = {key:proposal.get(key) for key in ('proposal_id','application_id','account_id','recipients','subject','body','offered_slots','expires_at')}
        review['operation'] = 'Send email'
        if context.actor_kind not in ('hermes','system','user'):
            raise ContractError('invalid review requester')
        trusted = MutationContext(context.idempotency_key,'system','stored_proposal_review',source_ref=proposal_id)
        return self.issue('send',proposal_id,review,payload_hash=proposal['payload_hash'],source_version=proposal['source_hash'],context=trusted)

    def review_attention(self, candidate, context, *, notification_id=None):
        review = {key:candidate.get(key) for key in ('candidate_id','title','summary','due_at','application_id','revision')}
        review['operation'] = 'Acknowledge or snooze'
        return self.issue('attention',candidate['candidate_id'],review,payload_hash=payload_sha256(review),source_version=str(candidate['revision']),context=context,notification_id=notification_id)
