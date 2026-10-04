"""Replace one managed urgent outbox delivery with one actionable Telegram card."""
import json
from datetime import datetime,timedelta,timezone

from ..contracts import MutationContext,parse_utc
from ..db import connect
from ..notifications import NotificationSendError


class InteractionDeliveryPending(NotificationSendError):
    """Durable delivery is queued/claimed; release lease without spending retries."""
    def __init__(self,ticket_id,state='queued'):
        super().__init__('interaction delivery pending',retryable=True)
        self.ticket_id=ticket_id
        self.delivery_state=state
        self.retry_after_seconds=15


class InteractionNotificationSender:
    def __init__(self,ledger,fallback_sender,now_provider=None):
        self.ledger=ledger
        self.fallback=fallback_sender
        self.timeout_seconds=getattr(fallback_sender,'timeout_seconds',40)
        self.now=now_provider or (lambda:datetime.now(timezone.utc))

    def send(self,notification):
        if not notification.get('attention_decision_id') or not self.ledger.interactions.identity:
            return self.fallback.send(notification)
        with connect(self.ledger.store.db_path) as con:
            row=con.execute('SELECT c.* FROM attention_candidates c JOIN attention_decisions d USING(candidate_id) WHERE d.decision_id=?',(notification['attention_decision_id'],)).fetchone()
        if not row:
            raise NotificationSendError('attention candidate no longer available',retryable=False)
        candidate=dict(row);payload=json.loads(candidate.pop('payload_json'))
        candidate.update(title=payload.get('title','Career update'),summary=payload.get('body',''),revision=candidate['revision_no'])
        context=MutationContext('interaction-alert:'+notification['notification_id'],'system','attention_delivery')
        ticket=self.ledger.interactions.review_attention(candidate,context,notification_id=notification['notification_id'])
        if ticket['delivery_state']=='sent':
            raise InteractionDeliveryPending(ticket['ticket_id'],'sent')
        if ticket['delivery_state']=='claimed' and ticket['delivery_started_at'] and parse_utc(ticket['delivery_started_at'])+timedelta(minutes=2)<self.now():
            self.ledger.interactions.delivery_unknown(ticket['ticket_id'])
            raise NotificationSendError('delivery_reconciliation_required',retryable=False)
        if ticket['delivery_state']=='queued' and parse_utc(ticket['expires_at'])<=self.now():
            raise NotificationSendError('interaction review expired before delivery',retryable=False)
        if ticket['delivery_state']=='unknown':
            raise NotificationSendError('delivery_reconciliation_required',retryable=False)
        if ticket['delivery_state']=='cancelled':
            raise InteractionDeliveryPending(ticket['ticket_id'],'cancelled')
        # A crashed process after send intent has no proof of delivery. The plugin
        # reports unknown on restart; no sender attempts the external call again.
        raise InteractionDeliveryPending(ticket['ticket_id'],ticket['delivery_state'])
